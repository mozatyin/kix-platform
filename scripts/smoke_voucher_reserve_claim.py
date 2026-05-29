"""Smoke test for the voucher 5-minute reserve + claim flow.

Validates the new endpoints added to ``app/routers/vouchers.py``:

  * POST /api/v1/vouchers/issue                (with holder_type=device_fp)
  * POST /api/v1/vouchers/{vid}/reserve
  * POST /api/v1/vouchers/{vid}/claim
  * POST /api/v1/vouchers/{vid}/release
  * GET  /api/v1/vouchers/reserved/by-device/{fp}
  * GET  /api/v1/vouchers/reserved/by-kid/{kid}
  * POST /api/v1/vouchers/admin/cleanup-expired-reservations

Flow probed (the QR→game→voucher critical fix):
  1. Issue voucher to a device fingerprint (anonymous game win)
  2. Reserve it (5-minute hold)
  3. Verify reservation listed by device
  4. Idempotent re-reserve extends TTL
  5. Claim with phone+otp → ensure_kid upgrades anon→registered
  6. Voucher now permanently bound to the new kid
  7. Re-claiming the same voucher fails (no active reservation)
  8. Second voucher: release flow returns it to ``issued``
  9. Third voucher: cleanup-expired-reservations on a manually-aged
     reservation returns the voucher to ``issued``

Runs in-process via httpx.ASGITransport. Requires a live local Redis.

Run:
    .venv/bin/python scripts/smoke_voucher_reserve_claim.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402
from app.redis_client import close_redis, get_redis, init_redis  # noqa: E402


GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"


def ok(msg: str) -> None:
    print(f"{GREEN}PASS{RESET} {msg}")


def fail(msg: str, detail: Any = "") -> None:
    print(f"{RED}FAIL{RESET} {msg} -- {detail}")
    raise AssertionError(f"{msg} -- {detail}")


def info(msg: str) -> None:
    print(f"{YELLOW}info{RESET} {msg}")


RUN_TAG = int(time.time())
BRAND_ID = f"smoke_brand_{RUN_TAG}"
DEVICE_FP = f"smoke_dev_fp_{RUN_TAG}"
DEVICE_FP_RELEASE = f"smoke_dev_fp_release_{RUN_TAG}"
DEVICE_FP_EXPIRE = f"smoke_dev_fp_expire_{RUN_TAG}"
PHONE = f"+1555{RUN_TAG % 10_000_000:07d}"
OTP = "stub-otp-token"
ADMIN_TOKEN = "stub-admin-token-for-smoke"


async def issue_voucher(c: httpx.AsyncClient, device_fp: str) -> str:
    """Issue a voucher with holder_type=device_fp; return voucher_id."""
    r = await c.post(
        "/api/v1/vouchers/issue",
        params={"issuer_brand_id": BRAND_ID},
        json={
            "user_id": device_fp,
            "holder_type": "device_fp",
            "redeemable_at": "issuer_only",
            "value_cents": 500,
            "source": "game_win",
        },
    )
    if r.status_code != 201:
        fail("issue voucher", f"{r.status_code} {r.text}")
    body = r.json()
    vid = body["voucher_id"]
    if body["status"] != "issued":
        fail("issued status", body)
    ok(f"issue → {vid} status=issued holder={device_fp}")
    return vid


async def step_reserve_claim_happy_path(c: httpx.AsyncClient) -> None:
    print("\n=== Step 1-7: reserve → claim happy path ===")
    vid = await issue_voucher(c, DEVICE_FP)

    # 1. Reserve
    r = await c.post(
        f"/api/v1/vouchers/{vid}/reserve",
        json={"device_fingerprint": DEVICE_FP, "ttl_seconds": 300},
    )
    if r.status_code != 200:
        fail("reserve", f"{r.status_code} {r.text}")
    res = r.json()
    if res["status"] != "reserved":
        fail("reserved status", res)
    if not res.get("reservation_token"):
        fail("reservation_token returned", res)
    initial_token = res["reservation_token"]
    initial_expires = res["expires_at"]
    ok(f"reserve → token={initial_token[:8]}.. expires={initial_expires}")

    # 2. By-device listing shows it
    r = await c.get(f"/api/v1/vouchers/reserved/by-device/{DEVICE_FP}")
    if r.status_code != 200:
        fail("by-device list", f"{r.status_code} {r.text}")
    listing = r.json()
    vids = [v["voucher_id"] for v in listing["reservations"]]
    if vid not in vids:
        fail("by-device contains vid", listing)
    ok(f"by-device → {len(vids)} reservation(s) include vid")

    # 3. Idempotent re-reserve (same fp): extends TTL, reuses token.
    await asyncio.sleep(1)
    r = await c.post(
        f"/api/v1/vouchers/{vid}/reserve",
        json={"device_fingerprint": DEVICE_FP, "ttl_seconds": 600},
    )
    if r.status_code != 200:
        fail("re-reserve same fp", f"{r.status_code} {r.text}")
    res2 = r.json()
    if not res2.get("extended"):
        fail("extended=True", res2)
    if res2["reservation_token"] != initial_token:
        fail("token preserved on extend", res2)
    if res2["expires_at"] <= initial_expires:
        fail("expires_at extended", res2)
    ok(f"re-reserve idempotent: extended, new_expires={res2['expires_at']}")

    # 4. Reserve by a different fp → 409
    r = await c.post(
        f"/api/v1/vouchers/{vid}/reserve",
        json={"device_fingerprint": "other_fp_xyz"},
    )
    if r.status_code != 409:
        fail("reserve by other fp → 409", f"{r.status_code} {r.text}")
    ok("reserve by other fp → 409 as expected")

    # 5. Claim with phone+otp (anon → registered kid upgrade)
    r = await c.post(
        f"/api/v1/vouchers/{vid}/claim",
        json={
            "device_fingerprint": DEVICE_FP,
            "phone": PHONE,
            "otp": OTP,
        },
    )
    if r.status_code != 200:
        fail("claim", f"{r.status_code} {r.text}")
    claim = r.json()
    if claim["status"] != "claimed":
        fail("claimed status", claim)
    kid = claim["kid"]
    if not kid:
        fail("kid returned", claim)
    if not claim.get("is_new_kid"):
        info(
            "is_new_kid=False — kid pre-existed (e.g. same device_fp registered "
            "in a prior smoke run); acceptable for idempotence"
        )
    voucher_after = claim["voucher"]
    if voucher_after.get("holder_user_id") != kid:
        fail("voucher bound to kid", voucher_after)
    if voucher_after.get("holder_type") != "kid":
        fail("holder_type=kid", voucher_after)
    ok(f"claim → kid={kid} is_new={claim.get('is_new_kid')}")

    # 6. Voucher detail confirms claimed + reservation HASH cleared
    r = await c.get(f"/api/v1/vouchers/{vid}")
    if r.status_code != 200:
        fail("get voucher", f"{r.status_code} {r.text}")
    v = r.json()
    if v["status"] != "claimed":
        fail("status=claimed after claim", v)
    if v.get("reserved_for_device_fp"):
        fail("reserved_for_device_fp cleared", v)
    ok(f"voucher detail: status=claimed holder={v['holder_user_id']}")

    # 7. Re-claim should now fail — no active reservation
    r = await c.post(
        f"/api/v1/vouchers/{vid}/claim",
        json={"kid": kid},
    )
    if r.status_code != 404:
        fail("re-claim already claimed → 404", f"{r.status_code} {r.text}")
    ok("re-claim already-claimed voucher → 404 as expected")


async def step_release_flow(c: httpx.AsyncClient) -> None:
    print("\n=== Step 8: release flow ===")
    vid = await issue_voucher(c, DEVICE_FP_RELEASE)

    r = await c.post(
        f"/api/v1/vouchers/{vid}/reserve",
        json={"device_fingerprint": DEVICE_FP_RELEASE},
    )
    if r.status_code != 200:
        fail("reserve for release test", r.text)

    # Wrong holder release → 403
    r = await c.post(
        f"/api/v1/vouchers/{vid}/release",
        json={"device_fingerprint": "wrong_fp"},
    )
    if r.status_code != 403:
        fail("wrong-fp release → 403", f"{r.status_code} {r.text}")
    ok("wrong-fp release → 403")

    # Correct release
    r = await c.post(
        f"/api/v1/vouchers/{vid}/release",
        json={"device_fingerprint": DEVICE_FP_RELEASE},
    )
    if r.status_code != 200:
        fail("release", f"{r.status_code} {r.text}")
    rel = r.json()
    if not rel.get("released") or rel["status"] != "issued":
        fail("released back to issued", rel)
    ok(f"release → status=issued released={rel['released']}")

    # Voucher hash confirms
    r = await c.get(f"/api/v1/vouchers/{vid}")
    v = r.json()
    if v["status"] != "issued":
        fail("voucher status=issued after release", v)
    ok("voucher back to 'issued' after release")


async def step_cleanup_expired(c: httpx.AsyncClient) -> None:
    print("\n=== Step 9: cleanup-expired-reservations ===")
    vid = await issue_voucher(c, DEVICE_FP_EXPIRE)

    r = await c.post(
        f"/api/v1/vouchers/{vid}/reserve",
        json={"device_fingerprint": DEVICE_FP_EXPIRE, "ttl_seconds": 30},
    )
    if r.status_code != 200:
        fail("reserve for expire test", r.text)

    # Age the reservation: rewrite expires_at to a past timestamp and let
    # the cleanup endpoint observe it (we cannot wait 5 minutes in a smoke).
    redis = await get_redis()
    past = int(time.time()) - 10
    await redis.hset(
        f"voucher:{vid}:reservation", "expires_at", str(past),
    )
    ok("reservation expires_at aged into the past")

    # Dry-run first
    r = await c.post(
        "/api/v1/vouchers/admin/cleanup-expired-reservations",
        json={"admin_token": ADMIN_TOKEN, "dry_run": True, "limit": 1000},
    )
    if r.status_code != 200:
        fail("cleanup dry-run", f"{r.status_code} {r.text}")
    dry = r.json()
    if vid not in dry.get("released_voucher_ids", []):
        fail("dry-run identified vid", dry)
    ok(f"dry-run scanned={dry['scanned']} released={dry['released']}")

    # Real run
    r = await c.post(
        "/api/v1/vouchers/admin/cleanup-expired-reservations",
        json={"admin_token": ADMIN_TOKEN, "dry_run": False, "limit": 1000},
    )
    if r.status_code != 200:
        fail("cleanup live", f"{r.status_code} {r.text}")
    live = r.json()
    if vid not in live.get("released_voucher_ids", []):
        fail("live run released vid", live)
    ok(f"live cleanup released={live['released']}")

    # Voucher back to issued
    r = await c.get(f"/api/v1/vouchers/{vid}")
    v = r.json()
    if v["status"] != "issued":
        fail("voucher 'issued' after cleanup", v)
    ok("voucher back to 'issued' after cleanup-expired")


async def step_bad_status_paths(c: httpx.AsyncClient) -> None:
    print("\n=== Step 10: invalid-status guardrails ===")
    # Reserve a non-existent voucher → 404
    r = await c.post(
        "/api/v1/vouchers/does_not_exist/reserve",
        json={"device_fingerprint": "x"},
    )
    if r.status_code != 404:
        fail("reserve unknown vid → 404", r.status_code)
    ok("reserve unknown vid → 404")

    # Claim with no reservation → 404
    vid = await issue_voucher(c, f"noresfp_{RUN_TAG}")
    r = await c.post(
        f"/api/v1/vouchers/{vid}/claim",
        json={"device_fingerprint": f"noresfp_{RUN_TAG}", "phone": PHONE, "otp": OTP},
    )
    if r.status_code != 404:
        fail("claim without reservation → 404", f"{r.status_code} {r.text}")
    ok("claim without active reservation → 404")


async def main() -> int:
    start_ts = time.time()
    await init_redis()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30.0
        ) as c:
            await step_reserve_claim_happy_path(c)
            await step_release_flow(c)
            await step_cleanup_expired(c)
            await step_bad_status_paths(c)
    finally:
        await close_redis()
    elapsed = time.time() - start_ts
    print(f"\n{GREEN}ALL SMOKE TESTS PASSED{RESET} ({elapsed:.2f}s)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except AssertionError:
        sys.exit(1)
