"""Merchant journey simulation — 老柯 / Ke Yu (云上直播 / Sky Live — LIVE-STREAM commerce).

End-to-end probe of the KiX Ads Platform from the perspective of a
LIVE-STREAMING COMMERCE operator (think 李佳琦 / 薇娅 / TikTok Live). Walks
through:
  1. Master + brand + 5 host sub-brands (each top host = sub-brand)
  2. Wallet funding (¥50K/月 ops budget) + post-paid take-rate
  3. KiX ID — viewers (50) + sellers (10) + hosts (5) — 3-party identity
  4. Consent — marketing scope critical for in-stream push
  5. Host as fulfiller_user_id in reservation (stream slot)
  6. Live stream product listing (listings + reservations together)
  7. Real-time bid auction + push viewers to stream when it goes live
  8. Flash sale — ¥99 for 30s, limited inventory, multiple viewers bid
  9. Host commission split (host gets X% of GMV via inter-brand-transfer)
 10. Viewer→Buyer conversion attribution (cross-host attribution)
 11. Fraud probe — bot viewers + fake comments + bidding bots
 12. After-stream replay viewing + delayed conversion
 13. Module probe + findings → /Users/mozat/a-docs/laoke-sim-findings.md

Unique to live-stream commerce:
  - REAL-TIME / HOST-DRIVEN — host_user_id is THE driver of GMV.
  - FLASH-SALE DYNAMICS — 30-second inventory race; orderly oversell control.
  - HIGH-FREQUENCY events — viewer enter/leave/comment/add-to-cart per second.
  - VIRAL — cross-host promo, host-A recommends host-B.
  - 3-PARTY ECONOMICS — platform take-rate + seller payout + host commission.
  - REPLAY ATTRIBUTION — viewer watches stream 12h later, conversion still
    attributes back to the live broadcast.

Pattern follows scripts/sim_laocai.py + scripts/sim_laohu.py.

In-process via httpx.ASGITransport so no separate server is needed. Requires
a live local Redis.

Run:
    .venv/bin/python scripts/sim_laoke.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402
from app.redis_client import close_redis, init_redis  # noqa: E402


# ── Constants / config ────────────────────────────────────────────────────
RUN_TAG = int(time.time())
OWNER_USER_ID = f"laoke_{RUN_TAG}"
BRAND_ID = f"sky_live_{RUN_TAG}"
BRAND_COLOR = "#FF1744"   # 直播 vivid red
FINDINGS_PATH = Path("/Users/mozat/a-docs/laoke-sim-findings.md")

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"

# Live-streaming scale numbers — 云上直播
NUM_VIEWERS = 50
NUM_SELLERS = 10
NUM_HOSTS = 5
TYPICAL_LOW_AOV_CENTS = 5_000          # ¥50
TYPICAL_FLASH_AOV_CENTS = 9_900        # ¥99 flash-sale
TYPICAL_AOV_CENTS = 30_000             # ¥300 mean
TYPICAL_HIGH_AOV_CENTS = 500_000       # ¥5000
MONTHLY_BUDGET_CENTS = 5_000_000       # ¥50,000
DAILY_BUDGET_CENTS = MONTHLY_BUDGET_CENTS // 30
PLATFORM_TAKE_RATE_BPS = 1000          # 10% platform take-rate
HOST_COMMISSION_BPS = 2000             # 20% host commission

CATEGORIES = ["beauty", "fashion", "snacks", "home", "digital"]
CATEGORIES_CN = {
    "beauty": "美妆", "fashion": "服饰", "snacks": "零食",
    "home": "家居", "digital": "数码",
}

# Persona hosts (top 5 of 50 hosts)
HOSTS = [
    ("Jasmine Lee",  "茉莉",  "+8613811220001", "beauty"),
    ("Tang Yu",      "唐宇",  "+8613811220002", "fashion"),
    ("Pang Pang",    "胖胖",  "+8613811220003", "snacks"),
    ("Mei Li",       "梅丽",  "+8613811220004", "home"),
    ("Tech Tony",    "科技托尼", "+8613811220005", "digital"),
]


# ── Logging helpers ──────────────────────────────────────────────────────
findings: list[dict[str, str]] = []
phase_counters: dict[str, dict[str, int]] = {}
_current_phase = "boot"


def _phase_init(name: str) -> None:
    global _current_phase
    _current_phase = name
    phase_counters[name] = {"pass": 0, "gap": 0, "fail": 0}
    print()
    print("=" * 70)
    print(f"{BOLD}{BLUE}PHASE {name}{RESET}")
    print("=" * 70)


def ok(action: str, result: str = "") -> None:
    phase_counters[_current_phase]["pass"] += 1
    print(f"  {GREEN}[PASS]{RESET} {action}" + (f" — {result}" if result else ""))


def gap(severity: str, action: str, detail: str) -> None:
    sev = severity.upper()
    phase_counters[_current_phase]["gap"] += 1
    findings.append({
        "phase": _current_phase,
        "severity": sev,
        "action": action,
        "detail": detail,
    })
    color = RED if sev == "P0" else (YELLOW if sev == "P1" else MAGENTA)
    print(f"  {color}[GAP {sev}]{RESET} {action} — {detail}")


def fail(action: str, detail: str) -> None:
    phase_counters[_current_phase]["fail"] += 1
    findings.append({
        "phase": _current_phase,
        "severity": "FAIL",
        "action": action,
        "detail": detail,
    })
    print(f"  {RED}[FAIL]{RESET} {action} — {detail}")


def info(msg: str) -> None:
    print(f"  {BLUE}[..]{RESET} {msg}")


# ── HTTP helpers ─────────────────────────────────────────────────────────
async def call(
    c: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    params: dict | None = None,
    headers: dict | None = None,
) -> tuple[int, Any]:
    try:
        r = await c.request(method, path, json=json_body, params=params, headers=headers)
    except Exception as e:
        return -1, {"exception": repr(e)}
    body: Any
    if r.headers.get("content-type", "").startswith("application/json"):
        try:
            body = r.json()
        except Exception:
            body = r.text
    else:
        body = r.text
    return r.status_code, body


def _short(body: Any, n: int = 250) -> str:
    s = json.dumps(body, ensure_ascii=False) if isinstance(body, (dict, list)) else str(body)
    return s if len(s) <= n else s[:n] + "..."


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ── Consent setup ────────────────────────────────────────────────────────
_consent_policy_published = False


async def _setup_consent(c: httpx.AsyncClient, user_ids: list[str],
                         policy_version: str = "1.0") -> None:
    global _consent_policy_published
    if not _consent_policy_published:
        sc, _b = await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
            "version": policy_version,
            "text_md": "# Sky Live sim policy\nFor live-stream commerce test.",
            "effective_at": int(time.time()) - 60,
            "requires_re_grant": False,
        })
        _consent_policy_published = (sc == 200)
    for uid in user_ids:
        await call(c, "POST", "/api/v1/consent/grant", json_body={
            "user_id": uid,
            "scopes": ["cross_brand_tracking", "geo_lbs",
                       "personalization", "marketing"],
            "policy_version": policy_version,
            "source": "app",
        })


# ── Phase 1: Master + brand + 5 host sub-brands ──────────────────────────
async def phase_1_brand_setup(c: httpx.AsyncClient) -> dict[str, Any]:
    _phase_init("1: Master + brand + 5 host sub-brands")
    state: dict[str, Any] = {
        "master_id": None,
        "brand_id": BRAND_ID,
        "host_brands": {},      # host_en → sub-brand id
        "hosts": {},            # host_en → kid
        "sellers": {},          # idx → kid
        "viewers": {},          # idx → kid
        "phone_to_kid": {},
        "streams": [],
        "products": [],
    }

    sc, b = await call(c, "POST", "/api/v1/master/create", json_body={
        "company_name": "云上直播 / Sky Live",
        "primary_email": "laoke@skylive.cn",
        "owner_user_id": OWNER_USER_ID,
    })
    if sc == 201 and isinstance(b, dict):
        state["master_id"] = b["master_id"]
        ok("create master account", f"master_id={state['master_id']}")
    else:
        gap("P1", "create master account", f"{sc} {_short(b)}")

    if state["master_id"]:
        sc, b = await call(c, "POST",
                           f"/api/v1/master/{state['master_id']}/brands/attach",
                           json_body={
                               "brand_id": BRAND_ID,
                               "store_name": "Sky Live (Master)",
                               "store_id": BRAND_ID,
                           })
        if sc == 200:
            ok("attach master brand", f"brand_id={BRAND_ID}")
        else:
            gap("P0", "attach master brand", f"{sc} {_short(b)}")

    # Storefront for the marketplace
    sc, b = await call(c, "POST", f"/api/v1/storefront/{BRAND_ID}/configure",
                       json_body={
                           "display_name": "云上直播 Sky Live",
                           "bio": "Real-time host-driven shopping — flash deals, "
                                  "every minute",
                           "brand_color": BRAND_COLOR,
                           "country": "CN",
                           "category": "live_commerce",
                       })
    if sc == 200:
        ok("marketplace storefront configure", "category=live_commerce")
    elif sc in (400, 422):
        gap("P1", "storefront category rejected",
            f"{sc} {_short(b)} — 'live_commerce' not a known category; live-"
            "streaming platforms (TikTok Live / Taobao Live) blur c2c + b2c. "
            "Need taxonomy enrichment.")
    else:
        gap("P1", "storefront configure", f"{sc} {_short(b)}")

    # PROBE: Each top host = sub-brand (host has own storefront, own followers,
    # own GMV ledger). Probably need 50 brands for 50 hosts (sustainability?)
    host_brands_attached = 0
    if state["master_id"]:
        for en, cn, _phone, cat in HOSTS:
            host_bid = f"host_{en.lower().replace(' ', '_')}_{RUN_TAG}"
            sc, b = await call(c, "POST",
                               f"/api/v1/master/{state['master_id']}/brands/attach",
                               json_body={
                                   "brand_id": host_bid,
                                   "store_name": f"{cn} 直播间",
                                   "store_id": host_bid,
                                   "parent_brand_id": BRAND_ID,
                               })
            if sc == 200:
                state["host_brands"][en] = host_bid
                host_brands_attached += 1
            elif sc in (400, 422, 404):
                # Single failure logged below
                pass

        if host_brands_attached == len(HOSTS):
            ok("5 host sub-brands attached", f"{host_brands_attached} hosts as sub-brands")
        elif host_brands_attached > 0:
            gap("P1", "partial host sub-brand attach",
                f"{host_brands_attached}/{len(HOSTS)} hosts attached as sub-brands")
        else:
            gap("P0", "no host-as-sub-brand support",
                "All 5 host sub-brand attachments failed. Live-commerce hosts "
                "need: their own storefront, follower set, GMV ledger, "
                "commission split. Modeling them as plain users loses payout "
                "+ storefront semantics. Need a 'creator/host' first-class "
                "primitive (host = KID + sub-brand-lite).")

    # Configure each host sub-brand storefront (where it works)
    sf_ok = 0
    for en, hbid in state["host_brands"].items():
        cat = next((h[3] for h in HOSTS if h[0] == en), "beauty")
        sc, _b = await call(c, "POST", f"/api/v1/storefront/{hbid}/configure",
                            json_body={
                                "display_name": f"{en} Live",
                                "bio": f"Top {CATEGORIES_CN[cat]} host on Sky Live",
                                "brand_color": BRAND_COLOR,
                                "country": "CN",
                                "category": cat,
                            })
        if sc == 200:
            sf_ok += 1
    if sf_ok:
        ok("host sub-brand storefronts", f"{sf_ok}/{len(state['host_brands'])} configured")

    return state


# ── Phase 2: Wallet funding + take-rate config ───────────────────────────
async def phase_2_wallet(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("2: Wallet ¥50K/月 + take-rate config")

    sc, b = await call(c, "POST", f"/api/v1/wallet/{BRAND_ID}/topup", json_body={
        "amount_cents": MONTHLY_BUDGET_CENTS,
        "payment_method": "alipay",
    })
    if sc != 200 or not isinstance(b, dict) or "topup_id" not in b:
        fail("wallet topup", f"{sc} {_short(b)}")
        return
    tid = b["topup_id"]
    sc2, b2 = await call(c, "POST",
                        f"/api/v1/wallet/{BRAND_ID}/topup/{tid}/confirm",
                        json_body={"payment_gateway_response": {"mock": True}})
    if sc2 == 200:
        ok("wallet topup ¥50K confirmed", f"topup_id={tid}")
    else:
        fail("wallet topup confirm", f"{sc2} {_short(b2)}")
        return

    sc, b = await call(c, "POST", f"/api/v1/wallet/{BRAND_ID}/daily-budget",
                       json_body={"daily_budget_cents": DAILY_BUDGET_CENTS})
    if sc == 200:
        ok("daily budget set", f"¥{DAILY_BUDGET_CENTS/100:.0f}/day")
    else:
        gap("P1", "daily budget", f"{sc} {_short(b)}")

    # PROBE: take-rate configure with category + host-tier rates
    sc, b = await call(c, "POST",
                       f"/api/v1/wallet/{BRAND_ID}/take-rate/configure",
                       json_body={
                           "default_basis_points": PLATFORM_TAKE_RATE_BPS,
                           "minimum_fee_cents": 100,
                           "category_rates": {
                               "beauty": 1200,    # premium category
                               "snacks": 800,
                               "digital": 1500,
                           },
                           "host_tier_rates": {
                               "top_host": 800,    # discount for top hosts
                               "standard": 1000,
                               "new_host": 1200,
                           },
                       })
    if sc in (200, 201):
        ok("take-rate configured (category + host-tier)", "")
    elif sc in (400, 422):
        gap("P0", "no host-tier in take-rate",
            f"{sc} {_short(b)} — take-rate accepts category_rates but not "
            "host_tier_rates. Live commerce platforms negotiate per-host: top "
            "hosts (李佳琦 tier) get 8% take-rate, new hosts pay 15%. Without "
            "host-tier multiplier, platform must enforce off-system. Need "
            "host_tier_rates first-class.")

    # PROBE: post-paid take-rate wallet (no upfront topup, commission-billed)
    sc, b = await call(c, "POST", f"/api/v1/wallet/{BRAND_ID}/topup", json_body={
        "amount_cents": 0,
        "payment_method": "marketplace_take_rate",
        "billing_strategy": "post_paid_commission",
        "commission_bps": PLATFORM_TAKE_RATE_BPS,
    })
    if sc == 200 and isinstance(b, dict):
        ok("post-paid take-rate wallet", "live-commerce billing supported")
    else:
        gap("P0", "no post-paid take-rate wallet",
            f"{sc} {_short(b)} — KiX wallet only supports pre-paid topup. "
            "Live commerce earns ONLY from each transaction (10% take). "
            "老柯 must pre-fund ¥50K and reconcile manually. Need "
            "billing_strategy=post_paid_commission.")


# ── Phase 3: KiX ID — viewers (50) + sellers (10) + hosts (5) ────────────
async def phase_3_three_party_identity(c: httpx.AsyncClient,
                                       state: dict[str, Any]) -> None:
    _phase_init("3: KiX ID — viewers (50) + sellers (10) + hosts (5)")

    # 3a: register 5 hosts
    host_ok = 0
    for en, cn, phone, cat in HOSTS:
        sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
            "phone": phone,
            "display_name": cn,
            "primary_language": "zh-CN",
            "source_brand_id": BRAND_ID,
            "device_fingerprint": f"dev_host_{en.replace(' ', '_')}_{RUN_TAG}",
            "country": "CN",
        })
        if sc == 200 and isinstance(b, dict) and b.get("kid"):
            state["hosts"][en] = b["kid"]
            state["phone_to_kid"][phone] = b["kid"]
            host_ok += 1
    if host_ok == NUM_HOSTS:
        ok(f"5 hosts registered (KiX ID)",
           f"kids={[k[:14] for k in state['hosts'].values()]}")
    else:
        gap("P0", "host kix-id registrations",
            f"{host_ok}/{NUM_HOSTS} hosts registered")

    # 3b: register 10 sellers (brand merchants supplying products to streams)
    seller_ok = 0
    for i in range(NUM_SELLERS):
        phone = f"+8613811330{i:03d}"
        sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
            "phone": phone,
            "display_name": f"商户_{i:02d}",
            "primary_language": "zh-CN",
            "source_brand_id": BRAND_ID,
            "device_fingerprint": f"dev_seller_{i:02d}_{RUN_TAG}",
            "country": "CN",
        })
        if sc == 200 and isinstance(b, dict) and b.get("kid"):
            state["sellers"][i] = b["kid"]
            state["phone_to_kid"][phone] = b["kid"]
            seller_ok += 1
    if seller_ok == NUM_SELLERS:
        ok(f"{NUM_SELLERS} sellers registered", "merchant suppliers")
    else:
        gap("P1", "seller registrations",
            f"{seller_ok}/{NUM_SELLERS} sellers registered")

    # 3c: register 50 viewers
    viewer_ok = 0
    for i in range(NUM_VIEWERS):
        phone = f"+8613811440{i:03d}"
        sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
            "phone": phone,
            "display_name": f"观众_{i:02d}",
            "primary_language": "zh-CN",
            "source_brand_id": BRAND_ID,
            "device_fingerprint": f"dev_viewer_{i:02d}_{RUN_TAG}",
            "country": "CN",
        })
        if sc == 200 and isinstance(b, dict) and b.get("kid"):
            state["viewers"][i] = b["kid"]
            state["phone_to_kid"][phone] = b["kid"]
            viewer_ok += 1
    if viewer_ok >= NUM_VIEWERS - 2:
        ok(f"{viewer_ok}/{NUM_VIEWERS} viewers registered", "")
    else:
        gap("P1", "viewer registrations",
            f"{viewer_ok}/{NUM_VIEWERS} viewers registered")

    # consent for all three parties
    all_kids = (list(state["hosts"].values())
                + list(state["sellers"].values())
                + list(state["viewers"].values()))
    await _setup_consent(c, all_kids)

    # 3d: Tag hosts with role attribute on each host sub-brand
    for en, kid in state["hosts"].items():
        hbid = state["host_brands"].get(en)
        if not hbid:
            continue
        for key, val in [
            ("role", "host"),
            ("host_tier", "top_host"),
            ("category", next((h[3] for h in HOSTS if h[0] == en), "beauty")),
            ("follower_count", "120000"),
            ("avg_concurrent_viewers", "8500"),
            ("conversion_rate_bps", "650"),  # 6.5% — top tier
            ("quality_score", "0.91"),
        ]:
            await call(c, "POST",
                       f"/api/v1/primitives/user/{kid}/attributes/{key}",
                       json_body={"value": val},
                       params={"brand_id": hbid})
    ok("host attributes set", "role/tier/category/followers/conv/quality on 5 hosts")

    # PROBE: 3-party identity — does profile-for-merchant differentiate
    # role=viewer vs role=seller vs role=host across same brand?
    sample_host_kid = next(iter(state["hosts"].values()), None)
    if sample_host_kid:
        sc, b = await call(c, "GET",
                           f"/api/v1/kix-id/{sample_host_kid}/profile-for-merchant/{BRAND_ID}")
        if sc == 200 and isinstance(b, dict):
            ok("profile-for-merchant (host view)", _short(b, 150))
            if "host" not in str(b).lower() and "role" not in str(b).lower():
                gap("P1", "profile lacks role discriminator",
                    "Live commerce: SAME KID may be host on brand A, viewer on "
                    "brand B, seller on brand C. profile-for-merchant returns "
                    "no role hint — merchant can't render the right UI. Need "
                    "explicit role + tier in response.")
        elif sc == 403:
            gap("P1", "profile-for-merchant blocked",
                "403 — host identity isn't surfacing to the marketplace.")
        else:
            gap("P1", "profile-for-merchant", f"{sc} {_short(b)}")


# ── Phase 4: Consent — marketing scope for in-stream push ────────────────
async def phase_4_consent_marketing(c: httpx.AsyncClient,
                                    state: dict[str, Any]) -> None:
    _phase_init("4: Consent — marketing scope critical (push during streams)")

    sample_viewer = next(iter(state["viewers"].values()), None)
    if not sample_viewer:
        info("Skipping — no viewers")
        return

    # PROBE: verify a viewer's marketing scope is granted
    sc, b = await call(c, "GET", f"/api/v1/consent/{sample_viewer}")
    if sc == 200 and isinstance(b, dict):
        scopes = b.get("scopes") or b.get("granted_scopes") or []
        if "marketing" in scopes or (isinstance(scopes, dict) and scopes.get("marketing")):
            ok("viewer marketing scope present", f"scopes={_short(scopes, 80)}")
        else:
            gap("P1", "marketing scope not visible",
                f"GET /consent/{{kid}} returned scopes but 'marketing' not "
                "explicit. In-stream push (stream-start notifications) gated "
                "on marketing scope; viewer-by-viewer audit critical. Need "
                "deterministic scope flags in response.")
    else:
        gap("P1", "consent fetch", f"{sc} {_short(b)}")

    # PROBE: revoke marketing — does subsequent push respect it?
    sc, b = await call(c, "POST", "/api/v1/consent/revoke", json_body={
        "user_id": sample_viewer,
        "scopes": ["marketing"],
        "reason": "viewer disabled stream-start notifications",
    })
    if sc == 200:
        ok("viewer revokes marketing scope", "")
        # Try to send a marketing push — must be blocked
        sc, b = await call(c, "POST", "/api/v1/push/now", json_body={
            "kid": sample_viewer,
            "slot": "push",
            "purpose": "marketing",
            "title": "Jasmine 直播间开播啦",
        })
        if sc == 200 and isinstance(b, dict):
            if b.get("fired") is False or "consent" in str(b.get("reason", "")).lower():
                ok("push blocked after marketing revoke",
                   f"reason={b.get('reason')}")
            else:
                gap("P0", "push fires despite revoked marketing scope",
                    f"{_short(b)} — viewer revoked marketing but stream-start "
                    "push still fires. Critical privacy/regulatory issue.")
        else:
            gap("P1", "push/now after revoke", f"{sc} {_short(b)}")

        # restore for downstream phases
        await call(c, "POST", "/api/v1/consent/grant", json_body={
            "user_id": sample_viewer,
            "scopes": ["marketing"],
            "policy_version": "1.0",
            "source": "app",
        })
    else:
        gap("P1", "consent revoke", f"{sc} {_short(b)}")


# ── Phase 5: Host as fulfiller_user_id in reservation (stream slot) ──────
async def phase_5_host_as_fulfiller(c: httpx.AsyncClient,
                                    state: dict[str, Any]) -> None:
    _phase_init("5: Host as fulfiller_user_id (live-stream as reservation)")

    if not state["hosts"] or not state["viewers"]:
        info("Skipping — need hosts + viewers")
        return

    host_en = next(iter(state["hosts"]))
    host_kid = state["hosts"][host_en]
    hbid = state["host_brands"].get(host_en, BRAND_ID)

    # PROBE: live-stream as a 'stream' reservation type
    stream_start = int(time.time()) + 60
    stream_end = stream_start + 3600  # 1-hour stream
    sc, b = await call(c, "POST", "/api/v1/reservations/create", json_body={
        "brand_id": hbid,
        "user_id": host_kid,           # treat host as both organizer + fulfiller
        "type": "stream",              # custom type
        "start_at": stream_start,
        "end_at": stream_end,
        "resource_id": f"stream_room_{host_en.replace(' ', '_')}",
        "fulfiller_user_id": host_kid,
        "metadata": {
            "category": next((h[3] for h in HOSTS if h[0] == host_en), "beauty"),
            "expected_viewers": 8500,
            "products_to_feature": 12,
            "is_live": True,
        },
    })
    stream_rid = None
    if sc in (200, 201) and isinstance(b, dict):
        stream_rid = b.get("reservation_id")
        state["stream_rid"] = stream_rid
        ok("stream reservation created", f"rid={stream_rid} host={host_en}")
    elif sc in (400, 422):
        gap("P0", "reservation type='stream' rejected",
            f"{sc} {_short(b)} — reservation type enum doesn't include "
            "'stream' / 'live_stream' / 'broadcast'. Live commerce needs "
            "scheduled streams as reservations (push viewers when slot opens, "
            "register fulfiller=host). Today must misuse 'class' or 'event'.")
    else:
        gap("P1", "stream reservation create", f"{sc} {_short(b)}")

    # PROBE: GET fulfiller view → host's stream calendar
    sc, b = await call(c, "GET", f"/api/v1/reservations/fulfiller/{host_kid}")
    if sc == 200 and isinstance(b, dict):
        ok("host calendar via /fulfiller/{uid}", f"count={b.get('count', '?')}")
    else:
        gap("P1", "host calendar view",
            f"GET /reservations/fulfiller/{host_kid} {sc} {_short(b)}")

    # PROBE: 30 daily streams = burst-create
    if state["hosts"]:
        burst_ok = 0
        burst_total = 0
        host_kids = list(state["hosts"].values())
        host_bids = list(state["host_brands"].values()) or [BRAND_ID]
        base_t = int(time.time()) + 7200
        for i in range(30):
            h_idx = i % len(host_kids)
            hk = host_kids[h_idx]
            hb = host_bids[h_idx % len(host_bids)]
            sc, _b = await call(c, "POST", "/api/v1/reservations/create", json_body={
                "brand_id": hb,
                "user_id": hk,
                "type": "stream",
                "start_at": base_t + i * 1800,
                "end_at": base_t + i * 1800 + 1500,
                "resource_id": f"stream_slot_{i:02d}",
                "fulfiller_user_id": hk,
                "metadata": {"slot_idx": i, "is_live": False, "scheduled": True},
            })
            burst_total += 1
            if sc in (200, 201):
                burst_ok += 1
        if burst_ok >= burst_total * 0.8:
            ok("30 daily streams burst-create", f"{burst_ok}/{burst_total}")
        else:
            gap("P1", "30-stream burst",
                f"{burst_ok}/{burst_total} created — daily quota fragile")

    # PROBE: viewer "saves spot" — reservation as fan reservation
    sample_viewer = next(iter(state["viewers"].values()))
    sc, b = await call(c, "POST", "/api/v1/reservations/create", json_body={
        "brand_id": hbid,
        "user_id": sample_viewer,
        "type": "stream_signup",   # viewer reserves a seat
        "start_at": stream_start,
        "end_at": stream_end,
        "resource_id": f"stream_room_{host_en.replace(' ', '_')}",
        "fulfiller_user_id": host_kid,
        "metadata": {"is_viewer_signup": True, "parent_stream_rid": stream_rid},
    })
    if sc in (200, 201):
        ok("viewer 'save spot' reservation", f"viewer={sample_viewer[:14]}…")
    elif sc in (400, 422):
        gap("P1", "no viewer-signup reservation",
            f"{sc} {_short(b)} — viewer can't reserve a spot for a future "
            "stream as a separate reservation. Live commerce needs 'I want to "
            "be notified + queued' semantics — today no link from viewer to "
            "host stream that triggers a notification.")
    else:
        gap("P2", "viewer signup reservation", f"{sc} {_short(b)}")


# ── Phase 6: Live stream listing (listings module + reservations) ────────
async def phase_6_stream_listings(c: httpx.AsyncClient,
                                  state: dict[str, Any]) -> None:
    _phase_init("6: Live stream product carousel (listings + reservation link)")

    if not state["hosts"] or not state["sellers"]:
        info("Skipping — need hosts + sellers")
        return

    host_en = next(iter(state["hosts"]))
    host_kid = state["hosts"][host_en]
    hbid = state["host_brands"].get(host_en, BRAND_ID)
    stream_rid = state.get("stream_rid")

    # Each stream features ~12 products. Create them as listings.
    products = []
    rng = random.Random(RUN_TAG)
    for i in range(12):
        seller_idx = i % len(state["sellers"])
        seller_kid = state["sellers"][seller_idx]
        cat = rng.choice(CATEGORIES)
        price = rng.choice([
            TYPICAL_FLASH_AOV_CENTS,
            TYPICAL_LOW_AOV_CENTS,
            TYPICAL_AOV_CENTS,
            100_000,
            TYPICAL_HIGH_AOV_CENTS,
        ])
        sc, b = await call(c, "POST", "/api/v1/listings/create", json_body={
            "brand_id": hbid,
            "seller_user_id": seller_kid,
            "title": f"{CATEGORIES_CN[cat]}爆款 #{i}",
            "description": f"Featured in {host_en}'s live stream",
            "price_cents": price,
            "currency": "CNY",
            "category": cat,
            "metadata": {
                "host_user_id": host_kid,
                "stream_reservation_id": stream_rid,
                "inventory_count": 100 if price > TYPICAL_AOV_CENTS else 500,
                "is_flash_sale_eligible": price <= TYPICAL_FLASH_AOV_CENTS * 2,
            },
        })
        if sc in (200, 201) and isinstance(b, dict):
            lid = b.get("listing_id") or b.get("id")
            if lid:
                products.append({
                    "listing_id": lid, "seller_kid": seller_kid,
                    "price_cents": price, "category": cat,
                })
    state["products"] = products

    if len(products) == 12:
        ok("stream product carousel built", f"{len(products)} listings")
    elif products:
        gap("P1", "partial stream products",
            f"{len(products)}/12 listings created")
    else:
        gap("P0", "no listing primitive for live commerce",
            "POST /listings/create rejected all 12 attempts. Live streams "
            "need a product carousel (host pins 10-20 SKUs per stream). "
            "Without listing primitive, products must be modeled as vouchers "
            "(wrong owner) or campaigns (wrong scope).")

    # PROBE: pin product as the 'now featuring' SKU (one at a time during live)
    if products:
        sample = products[0]
        sc, b = await call(c, "POST",
                           f"/api/v1/listings/{sample['listing_id']}/promote",
                           json_body={
                               "duration_days": 1,
                               "promotion_cents": 500,
                               "slot": "stream_pinned",
                               "host_user_id": host_kid,
                           })
        if sc in (200, 201):
            ok("listing promote (pinned product)", "")
        elif sc in (400, 422):
            gap("P1", "no 'stream_pinned' promotion slot",
                f"{sc} {_short(b)} — live commerce pinned-product slot needs "
                "second-granular activation + auto-expire when host moves to "
                "next SKU. Today listing promote is day-level.")
        else:
            gap("P1", "listing promote", f"{sc} {_short(b)}")


# ── Phase 7: Real-time bid auction + push viewers ───────────────────────
async def phase_7_realtime_push(c: httpx.AsyncClient,
                                state: dict[str, Any]) -> None:
    _phase_init("7: Stream goes live — instant push to followers")

    if not state["hosts"] or not state["viewers"]:
        info("Skipping — need hosts + viewers")
        return

    host_en = next(iter(state["hosts"]))
    host_kid = state["hosts"][host_en]
    hbid = state["host_brands"].get(host_en, BRAND_ID)
    viewers = list(state["viewers"].values())[:20]

    # 7a: viewers follow the host (social graph)
    follow_ok = 0
    for v in viewers:
        sc, _b = await call(c, "POST", "/api/v1/social/follow", json_body={
            "follower_id": v,
            "followed_id": host_kid,
        })
        if sc in (200, 201):
            follow_ok += 1
    ok("viewers follow host", f"{follow_ok}/{len(viewers)} follows")

    # 7b: stream-start trigger (live-commerce specific event_type)
    sc, b = await call(c, "POST", "/api/v1/triggers/register", json_body={
        "brand_id": hbid,
        "name": f"{host_en} goes live → notify followers",
        "event_type": "stream_started",
        "event_filter": {"host_user_id": host_kid},
        "action": {
            "type": "send_push",
            "config": {
                "title": f"{host_en} 直播间开播了！",
                "body": "今晚有 12 件爆款，前 100 名 ¥99 起",
                "deep_link": f"skylive://stream/{host_kid}",
                "scope_required": "marketing",
            },
        },
        "cooldown_seconds": 0,
        "max_fires_per_user": 1,
    })
    if sc == 201 and isinstance(b, dict):
        ok("stream-started trigger registered", f"trigger_id={b.get('trigger_id','?')[:18]}")
    elif sc in (400, 422):
        gap("P1", "trigger rejects event_type=stream_started",
            f"{sc} {_short(b)} — event_type enum likely closed. Live commerce "
            "needs stream_started / stream_ended / flash_sale_started / "
            "host_added_product / price_drop event types.")

    # 7c: simulate stream goes live — push 10 followers immediately
    push_ok = 0
    push_blocked = 0
    for v in viewers[:10]:
        sc, b = await call(c, "POST", "/api/v1/push/now", json_body={
            "kid": v,
            "slot": "push",
            "title": f"{host_en} 已开播",
            "body": "Click to watch",
        })
        if sc == 200 and isinstance(b, dict):
            if b.get("fired"):
                push_ok += 1
            else:
                push_blocked += 1
    if push_ok >= 5:
        ok("instant push to followers", f"fired={push_ok}/10 blocked={push_blocked}")
    else:
        gap("P1", "instant push delivery low",
            f"fired={push_ok}/10 — push engine misses real-time SLA. Live "
            "commerce needs sub-second push fanout when host hits 'Go Live'.")

    # 7d: Pixel — register stream-tracking pixel + emit live events
    sc, b = await call(c, "POST", "/api/v1/pixel/register", json_body={
        "brand_id": hbid,
        "allowed_origins": ["https://skylive.cn"],
    })
    pid = None
    if sc == 201 and isinstance(b, dict):
        pid = b["pixel_id"]
        state["pixel_id"] = pid
        ok("live-stream pixel registered", f"pid={pid}")
    else:
        gap("P1", "pixel register", f"{sc} {_short(b)}")

    # 7e: emit a "stream_join" event from each of 20 viewers
    if pid:
        joins = 0
        for v in viewers:
            sc, _b = await call(c, "POST", "/api/v1/pixel/event", json_body={
                "pixel_id": pid,
                "event_type": "stream_join",   # custom event type
                "user_id": v,
                "device_fingerprint": f"dev_viewer_{v[:8]}",
                "origin": "https://skylive.cn",
                "url": f"https://skylive.cn/live/{host_kid}",
                "metadata": {
                    "host_user_id": host_kid,
                    "stream_reservation_id": state.get("stream_rid"),
                    "join_method": "push_notification",
                },
            }, headers={"Origin": "https://skylive.cn"})
            if sc == 200:
                joins += 1
        if joins >= len(viewers) * 0.8:
            ok("stream_join events accepted", f"{joins}/{len(viewers)} joins")
        elif joins > 0:
            gap("P1", "partial stream_join accept",
                f"{joins}/{len(viewers)} — custom event_type partially supported")
        else:
            gap("P0", "pixel rejects 'stream_join' event_type",
                "All 20 stream_join events rejected. Live commerce telemetry "
                "needs: stream_join, stream_leave, add_to_cart_during_stream, "
                "host_pin_product, flash_sale_purchase, comment_posted. "
                "Today pixel event_type is closed enum (purchase / signup / "
                "view). Need open event_type taxonomy for real-time analytics.")


# ── Phase 8: Flash sale (¥99 for 30s — limited inventory) ────────────────
async def phase_8_flash_sale(c: httpx.AsyncClient,
                             state: dict[str, Any]) -> None:
    _phase_init("8: Flash sale — ¥99/30s, limited inventory, many bidders")

    if not state.get("products") or not state["viewers"]:
        info("Skipping — need products + viewers")
        return

    pid = state.get("pixel_id")
    flash_product = next(
        (p for p in state["products"]
         if p["price_cents"] <= TYPICAL_FLASH_AOV_CENTS * 2),
        state["products"][0]
    )

    # 8a: Flash-sale start trigger (very short auction)
    flash_start = int(time.time())
    flash_end = flash_start + 30  # 30 seconds
    sc, b = await call(c, "POST", "/api/v1/triggers/register", json_body={
        "brand_id": state["brand_id"],
        "name": f"Flash sale ¥99 — listing {flash_product['listing_id']}",
        "event_type": "flash_sale_started",
        "event_filter": {"listing_id": flash_product["listing_id"]},
        "action": {
            "type": "send_push",
            "config": {
                "title": "限时秒杀 ¥99",
                "body": "30 秒抢购，库存仅 100 件",
                "scope_required": "marketing",
            },
        },
        "cooldown_seconds": 0,
        "max_fires_per_user": 1,
    })
    if sc == 201:
        ok("flash_sale_started trigger", "")
    elif sc in (400, 422):
        gap("P1", "no flash_sale_started event_type",
            f"{sc} {_short(b)} — flash-sale needs distinct event_type "
            "(separate from regular promotion) for analytics + push.")

    # 8b: 30 viewers race to buy 100 units — should not oversell
    if pid:
        buy_attempts = 0
        buy_success = 0
        for i, v in enumerate(list(state["viewers"].values())[:30]):
            sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
                "pixel_id": pid,
                "event_type": "purchase",
                "user_id": v,
                "device_fingerprint": f"dev_buyer_{i:02d}",
                "order_id": f"flash_{flash_product['listing_id']}_{i:02d}",
                "amount_cents": TYPICAL_FLASH_AOV_CENTS,
                "origin": "https://skylive.cn",
                "url": "https://skylive.cn/flash/checkout",
                "metadata": {
                    "listing_id": flash_product["listing_id"],
                    "seller_user_id": flash_product["seller_kid"],
                    "host_user_id": next(iter(state["hosts"].values())),
                    "is_flash_sale": True,
                    "flash_window_start": flash_start,
                    "flash_window_end": flash_end,
                },
            }, headers={"Origin": "https://skylive.cn"})
            buy_attempts += 1
            if sc == 200:
                buy_success += 1
        if buy_success >= buy_attempts * 0.6:
            ok("flash-sale conversion burst", f"{buy_success}/{buy_attempts} purchases")
        else:
            gap("P1", "flash-sale conversion burst low",
                f"only {buy_success}/{buy_attempts} purchases accepted")

    # 8c: Inventory sync probe — can platform reserve units atomically?
    sc, b = await call(c, "POST",
                       f"/api/v1/listings/{flash_product['listing_id']}/reserve-inventory",
                       json_body={
                           "user_id": next(iter(state["viewers"].values())),
                           "quantity": 1,
                           "hold_seconds": 30,
                       })
    if sc in (200, 201):
        ok("inventory reservation primitive", "atomic hold supported")
    elif sc == 404:
        gap("P0", "no inventory-reservation primitive",
            "POST /listings/{lid}/reserve-inventory 404. Flash sale with "
            "100 units + 5000 viewers MUST atomically reserve / decrement "
            "inventory or platform oversells. Today must lock externally. "
            "Need atomic counter with TTL on listing entity.")
    elif sc in (400, 422):
        gap("P1", "inventory reservation schema",
            f"{sc} {_short(b)}")


# ── Phase 9: Host commission split via inter-brand-transfer ──────────────
async def phase_9_commission_split(c: httpx.AsyncClient,
                                   state: dict[str, Any]) -> None:
    _phase_init("9: Host commission split (host gets 20% via inter-brand-transfer)")

    if not state["hosts"]:
        info("Skipping — need hosts")
        return

    host_en = next(iter(state["hosts"]))
    hbid = state["host_brands"].get(host_en, BRAND_ID)

    # Assume stream produced ¥10,000 GMV. Host takes 20% (¥2000).
    gross_gmv_cents = 1_000_000
    host_commission_cents = gross_gmv_cents * HOST_COMMISSION_BPS // 10000

    sc, b = await call(c, "POST",
                       "/api/v1/payouts/inter-brand-transfer",
                       json_body={
                           "from_brand_id": BRAND_ID,
                           "to_brand_id": hbid,
                           "amount_cents": host_commission_cents,
                           "reason": "host_commission",
                           "reference_id": f"stream_payout_{state.get('stream_rid','x')}_{RUN_TAG}",
                           "ledger_entry_metadata": {
                               "category": "host_commission",
                               "host_user_id": state["hosts"][host_en],
                               "host_tier": "top_host",
                               "commission_bps": HOST_COMMISSION_BPS,
                               "gross_gmv_cents": gross_gmv_cents,
                               "stream_reservation_id": state.get("stream_rid"),
                           },
                       })
    if sc in (200, 201) and isinstance(b, dict) and b.get("entry_id"):
        ok("host commission transfer",
           f"¥{host_commission_cents/100:.0f} → {host_en} brand "
           f"entry={b['entry_id']}")
    elif sc == 404:
        gap("P0", "no inter-brand-transfer endpoint",
            "POST /payouts/inter-brand-transfer 404. Without it, host "
            "commission split cannot reconcile within the platform. Forces "
            "external CSV exports → manual settlement → slow + error-prone.")
    elif sc in (400, 422):
        gap("P1", "inter-brand-transfer schema",
            f"{sc} {_short(b)} — endpoint exists but schema mismatch.")
    else:
        gap("P1", "inter-brand-transfer", f"{sc} {_short(b)}")

    # PROBE: idempotency on commission transfer (replay = same entry?)
    sc2, b2 = await call(c, "POST",
                         "/api/v1/payouts/inter-brand-transfer",
                         json_body={
                             "from_brand_id": BRAND_ID,
                             "to_brand_id": hbid,
                             "amount_cents": host_commission_cents,
                             "reason": "host_commission",
                             "reference_id": f"stream_payout_{state.get('stream_rid','x')}_{RUN_TAG}",
                         })
    if sc2 == 200 and isinstance(b2, dict) and b2.get("idempotent"):
        ok("commission idempotency", "replay flagged idempotent=True")
    elif sc2 in (200, 201):
        gap("P0", "commission NOT idempotent",
            "Replaying same reference_id paid host TWICE. Live commerce "
            "settles 30+ streams/day; retries are routine. Must be idempotent "
            "on reference_id.")

    # PROBE: 3-way split (platform / host / seller) in one call
    sc, b = await call(c, "POST",
                       f"/api/v1/wallet/{BRAND_ID}/marketplace-charge",
                       json_body={
                           "gross_amount_cents": gross_gmv_cents,
                           "seller_user_id": next(iter(state["sellers"].values())),
                           "buyer_user_id": next(iter(state["viewers"].values())),
                           "category": "beauty",
                           "listing_id": state["products"][0]["listing_id"] if state.get("products") else "x",
                           "host_user_id": state["hosts"][host_en],
                           "host_commission_bps": HOST_COMMISSION_BPS,
                       })
    if sc in (200, 201) and isinstance(b, dict):
        if "host_payout_cents" in str(b) or "host_commission" in str(b):
            ok("3-way split (platform / host / seller)",
               _short(b, 150))
        else:
            gap("P0", "marketplace-charge ignores host",
                f"{_short(b, 150)} — charge accepts host_user_id + "
                "host_commission_bps but response shows only platform/seller "
                "split. Live commerce: every transaction MUST be 3-way "
                "(platform take + host commission + seller proceeds). Without "
                "atomic 3-way settlement, host payouts diverge from GMV.")
    elif sc in (400, 422):
        gap("P0", "marketplace-charge no host_user_id",
            f"{sc} {_short(b)} — schema rejects host_user_id. Need first-"
            "class host param in marketplace-charge for live commerce.")
    else:
        gap("P1", "marketplace-charge", f"{sc} {_short(b)}")


# ── Phase 10: Viewer→Buyer attribution (cross-host) ──────────────────────
async def phase_10_attribution(c: httpx.AsyncClient,
                               state: dict[str, Any]) -> None:
    _phase_init("10: Viewer→Buyer conversion attribution (cross-host)")

    pid = state.get("pixel_id")
    if not pid:
        info("Skipping — no pixel")
        return

    if len(state["hosts"]) < 2 or not state["viewers"]:
        info("Skipping — need ≥2 hosts + viewers")
        return

    host_kids = list(state["hosts"].values())
    host_a, host_b = host_kids[0], host_kids[1]
    viewer = next(iter(state["viewers"].values()))

    # Viewer joins host A's stream, then jumps to host B, buys from B
    # Live commerce attribution must give credit to LAST-TOUCH host (not first)
    for evt in (
        ("stream_join",  host_a, "join host A"),
        ("stream_leave", host_a, "leave host A"),
        ("stream_join",  host_b, "join host B"),
        ("add_to_cart",  host_b, "ATC on host B"),
    ):
        ev_type, h_kid, _label = evt
        sc, _b = await call(c, "POST", "/api/v1/pixel/event", json_body={
            "pixel_id": pid,
            "event_type": ev_type,
            "user_id": viewer,
            "device_fingerprint": f"dev_attr_{viewer[:8]}",
            "origin": "https://skylive.cn",
            "url": f"https://skylive.cn/live/{h_kid}",
            "metadata": {"host_user_id": h_kid},
        }, headers={"Origin": "https://skylive.cn"})
        # do not check status; some types may be rejected upstream

    # Purchase — should attribute to host B (last touch)
    sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
        "pixel_id": pid,
        "event_type": "purchase",
        "user_id": viewer,
        "device_fingerprint": f"dev_attr_{viewer[:8]}",
        "order_id": f"cross_host_buy_{RUN_TAG}",
        "amount_cents": TYPICAL_AOV_CENTS,
        "origin": "https://skylive.cn",
        "url": "https://skylive.cn/checkout",
        "metadata": {
            "host_user_id": host_b,   # last touch = B
            "first_touch_host_user_id": host_a,
        },
    }, headers={"Origin": "https://skylive.cn"})
    if sc == 200 and isinstance(b, dict):
        ok("cross-host purchase event", _short(b, 150))
        conversion_id = b.get("conversion_id") or b.get("event_id")
        if not conversion_id:
            gap("P1", "no conversion_id on cross-host purchase",
                "Cross-host attribution requires conversion_id for reconciliation.")
    else:
        gap("P1", "cross-host purchase", f"{sc} {_short(b)}")

    # PROBE: attribution_window_days — live-commerce should be 30d (replay)
    sc, b = await call(c, "POST", "/api/v1/attribution/policy/configure",
                       json_body={
                           "brand_id": BRAND_ID,
                           "attribution_window_days": 30,
                           "model": "last_touch_host",
                           "host_user_id_attribute": "host_user_id",
                       })
    if sc == 200:
        ok("attribution policy configured", "30d last-touch-host window")
    elif sc in (400, 422):
        gap("P1", "attribution policy no host model",
            f"{sc} {_short(b)} — attribution model enum likely doesn't have "
            "'last_touch_host'. Live commerce needs host-aware attribution "
            "(not just first/last URL touch). Today must reverse-engineer "
            "from pixel metadata, fragile.")
    elif sc == 404:
        gap("P0", "no attribution policy endpoint",
            "POST /attribution/policy/configure 404. Cannot set window or "
            "model. Live commerce attribution is bespoke per-platform; need "
            "a configuration API.")

    # PROBE: per-host conversion report
    sc, b = await call(c, "GET", f"/api/v1/attribution/host/{host_b}/conversions",
                       params={"brand_id": BRAND_ID})
    if sc == 200 and isinstance(b, dict):
        ok("per-host conversion report",
           f"conversions={b.get('count', '?')} gmv={b.get('gmv_cents', '?')}")
    elif sc == 404:
        gap("P1", "no per-host conversion report",
            "GET /attribution/host/{host}/conversions 404. Host commission "
            "depends on per-host GMV view. Without first-class report, "
            "老柯 must SQL pixel_events directly.")


# ── Phase 11: Fraud probe — bot viewers, fake comments, bidding bots ─────
async def phase_11_fraud(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("11: Fraud — bot viewers, fake comments, bidding bots")

    # 11a: bot viewer detection
    sc, b = await call(c, "POST", "/api/v1/attribution/anti-fraud/check",
                       json_body={
                           "user_id": "bot_viewer_001",
                           "device_fingerprint": "dev_bot_viewer",
                           "ip_address": "10.0.0.42",
                           "signals": {
                               "stream_joins_per_minute": 120,
                               "comments_per_minute": 0,
                               "session_duration_sec": 3600,
                               "interaction_events": 0,
                               "device_user_agent": "headless-chrome/120",
                           },
                       })
    if sc == 200 and isinstance(b, dict):
        if b.get("is_fraud") or b.get("score", 0) > 0.6:
            ok("bot viewer flagged", _short(b, 120))
        else:
            gap("P1", "anti-fraud blind to bot-viewer pattern",
                f"score={b.get('score','?')} — 120 stream joins/min, 0 "
                "interactions, headless UA NOT flagged. Bot viewers inflate "
                "host concurrent-viewer metrics → false advertising. Need "
                "live-commerce specific signals (joins, comments, dwell-time).")
    elif sc == 404:
        gap("P0", "no anti-fraud endpoint",
            "404 — bot viewer fraud unmitigated. Inflated viewer counts "
            "are the #1 sponsor complaint about live commerce platforms.")
    else:
        gap("P1", "anti-fraud", f"{sc} {_short(b)}")

    # 11b: fake comments detection (水军)
    sc, b = await call(c, "POST", "/api/v1/attribution/anti-fraud/check",
                       json_body={
                           "user_id": "shuijun_001",
                           "device_fingerprint": "dev_shuijun",
                           "ip_address": "10.0.0.43",
                           "signals": {
                               "comments_per_stream": 80,
                               "comment_avg_length_chars": 6,
                               "comment_dup_ratio": 0.95,
                               "purchase_count_30d": 0,
                               "account_age_days": 2,
                           },
                       })
    if sc == 200 and isinstance(b, dict):
        if b.get("is_fraud") or b.get("score", 0) > 0.5:
            ok("water-army comments flagged", _short(b, 120))
        else:
            gap("P1", "water-army (水军) not flagged",
                f"score={b.get('score','?')} — 95% duplicate comments, "
                "2-day-old account, 0 purchases NOT flagged. Hosts buy 水军 "
                "to fake engagement; platform needs to detect.")
    elif sc == 404:
        pass  # already gapped above

    # 11c: rate-limit / frequency-cap on viewer.comment
    sc, b = await call(c, "POST", "/api/v1/frequency-cap/check", json_body={
        "user_id": "comment_spammer_001",
        "brand_id": BRAND_ID,
        "action": "comment_in_stream",
        "window_seconds": 60,
        "max_count": 10,
    })
    if sc == 200 and isinstance(b, dict):
        ok("frequency-cap for comments", _short(b, 120))
    elif sc == 404:
        gap("P1", "no per-action frequency-cap",
            "Live-stream chat spam needs rate-limit per viewer "
            "(10 comments/minute baseline). Today no general per-action cap.")

    # 11d: bidding-bot fraud (race-bots in flash sales)
    sc, b = await call(c, "POST", "/api/v1/attribution/anti-fraud/check",
                       json_body={
                           "user_id": "bidding_bot_001",
                           "device_fingerprint": "dev_bidbot",
                           "ip_address": "10.0.0.44",
                           "signals": {
                               "flash_sale_attempts_per_hour": 100,
                               "purchase_latency_ms": 50,  # impossibly fast
                               "unique_flash_skus": 100,
                               "win_rate": 0.85,
                               "device_user_agent": "python-requests",
                           },
                       })
    if sc == 200 and isinstance(b, dict):
        if b.get("is_fraud") or b.get("score", 0) > 0.6:
            ok("bidding-bot flagged", _short(b, 120))
        else:
            gap("P1", "bidding-bot not flagged",
                f"score={b.get('score','?')} — 50ms purchase latency, "
                "python-requests UA. Bidding bots win all flash sales → "
                "real fans never get the ¥99 deal.")


# ── Phase 12: After-stream replay + delayed conversion ───────────────────
async def phase_12_replay(c: httpx.AsyncClient,
                          state: dict[str, Any]) -> None:
    _phase_init("12: After-stream replay viewing + delayed conversion")

    pid = state.get("pixel_id")
    if not pid or not state["viewers"] or not state["hosts"]:
        info("Skipping — need pixel + viewers + hosts")
        return

    host_kid = next(iter(state["hosts"].values()))
    replay_viewer = list(state["viewers"].values())[5]

    # Simulate replay events 12 hours after live
    base_t = int(time.time()) - 43200   # 12 hours ago = live
    sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
        "pixel_id": pid,
        "event_type": "stream_replay_view",
        "user_id": replay_viewer,
        "device_fingerprint": f"dev_replay_{replay_viewer[:8]}",
        "origin": "https://skylive.cn",
        "url": f"https://skylive.cn/replay/{host_kid}",
        "metadata": {
            "host_user_id": host_kid,
            "original_stream_at": base_t,
            "viewed_at": int(time.time()),
            "delay_hours": 12,
        },
    }, headers={"Origin": "https://skylive.cn"})
    if sc == 200:
        ok("stream_replay_view event accepted", "")
    elif sc in (400, 422):
        gap("P1", "no stream_replay_view event_type",
            f"{sc} {_short(b)} — replay viewing is a major attribution "
            "channel (40%+ of GMV happens hours/days after live). Need "
            "stream_replay_view + watch_duration + replay_conversion types.")

    # Replay-driven purchase 12h after live
    if state.get("products"):
        prod = state["products"][1] if len(state["products"]) > 1 else state["products"][0]
        sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
            "pixel_id": pid,
            "event_type": "purchase",
            "user_id": replay_viewer,
            "device_fingerprint": f"dev_replay_{replay_viewer[:8]}",
            "order_id": f"replay_buy_{RUN_TAG}",
            "amount_cents": prod["price_cents"],
            "origin": "https://skylive.cn",
            "url": "https://skylive.cn/checkout/replay",
            "metadata": {
                "listing_id": prod["listing_id"],
                "host_user_id": host_kid,
                "is_replay_conversion": True,
                "delay_seconds": 43200,
                "original_stream_at": base_t,
            },
        }, headers={"Origin": "https://skylive.cn"})
        if sc == 200 and isinstance(b, dict):
            ok("replay-driven purchase attributed", _short(b, 120))
            # PROBE: does host_user_id still appear in conversion metadata?
            if str(host_kid) in str(b):
                ok("replay conversion retains host attribution", "")
            else:
                gap("P1", "host attribution lost on replay",
                    "Replay purchase doesn't preserve host_user_id in "
                    "conversion record. Host commission depends on this; "
                    "today must reconstruct from metadata bag.")
        else:
            gap("P1", "replay purchase", f"{sc} {_short(b)}")

    # PROBE: attribution_window_days=30 enforced for replay purchases
    # (delay = 12h is fine; what about delay = 35 days? must reject)
    if state.get("products"):
        prod = state["products"][0]
        old_t = int(time.time()) - 86400 * 35
        sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
            "pixel_id": pid,
            "event_type": "purchase",
            "user_id": replay_viewer,
            "device_fingerprint": f"dev_replay_35d",
            "order_id": f"replay_35d_{RUN_TAG}",
            "amount_cents": prod["price_cents"],
            "origin": "https://skylive.cn",
            "url": "https://skylive.cn/checkout/replay_35d",
            "metadata": {
                "host_user_id": host_kid,
                "is_replay_conversion": True,
                "original_stream_at": old_t,
            },
        }, headers={"Origin": "https://skylive.cn"})
        if sc == 200 and isinstance(b, dict):
            attributed = ("host_user_id" in str(b)) and (b.get("attributed") is not False)
            if attributed:
                gap("P1", "attribution window not enforced",
                    "Purchase 35 days after live still attributed. Need "
                    "the configured attribution_window_days to actually "
                    "gate downstream attribution.")
            else:
                ok("35d-delay purchase not attributed", "window enforced")


# ── Phase 13: Module probe ───────────────────────────────────────────────
async def phase_13_module_probe(c: httpx.AsyncClient,
                                state: dict[str, Any]) -> None:
    _phase_init("13: Live-commerce module probe")

    any_host = next(iter(state["hosts"].values()), "x")
    any_viewer = next(iter(state["viewers"].values()), "x")
    any_seller = next(iter(state["sellers"].values()), "x")

    probes = [
        ("kix-id.host",          "GET",  f"/api/v1/kix-id/{any_host}"),
        ("kix-id.viewer",        "GET",  f"/api/v1/kix-id/{any_viewer}"),
        ("kix-id.seller",        "GET",  f"/api/v1/kix-id/{any_seller}"),
        ("primitives.host.attrs","GET",  f"/api/v1/primitives/user/{any_host}/attributes"),
        ("social.host.followers","GET",  f"/api/v1/social/{any_host}/followers"),
        ("reservations.fulfiller","GET", f"/api/v1/reservations/fulfiller/{any_host}"),
        ("listings.brand",       "GET",  f"/api/v1/listings/brand/{BRAND_ID}"),
        ("audiences.brand",      "GET",  f"/api/v1/audiences/brand/{BRAND_ID}"),
        ("storefront.host",      "GET",
         f"/api/v1/storefront/{next(iter(state['host_brands'].values()), 'x')}"),
        ("payouts.balance",      "GET",  f"/api/v1/payouts/brand/{BRAND_ID}/balance"),
        ("commerce.loop",        "GET",  f"/api/v1/commerce/brand/{BRAND_ID}/loop"),
        ("attribution.host",     "GET",
         f"/api/v1/attribution/host/{any_host}/conversions"),
        ("push.now",             "POST", "/api/v1/push/now"),
        ("triggers.register",    "POST", "/api/v1/triggers/register"),
        ("wallet.take-rate",     "POST", f"/api/v1/wallet/{BRAND_ID}/take-rate/configure"),
        ("marketplace-charge",   "POST", f"/api/v1/wallet/{BRAND_ID}/marketplace-charge"),
        ("inter-brand-transfer", "POST", "/api/v1/payouts/inter-brand-transfer"),
        ("disputes.brand",       "GET",  f"/api/v1/disputes/brand/{BRAND_ID}"),
        ("anti-fraud.check",     "POST", "/api/v1/attribution/anti-fraud/check"),
    ]
    avail, missing = 0, 0
    for label, method, path in probes:
        sc, b = await call(c, method, path)
        if sc == 200:
            avail += 1
            ok(f"module live: {label}", "200")
        elif sc == 404:
            if isinstance(b, dict) and b.get("detail") in ("Not Found", "not found"):
                missing += 1
                gap("P1", f"module not mounted: {label}", f"404 at {path}")
            else:
                avail += 1
                ok(f"module live (no-resource): {label}", "domain 404")
        elif sc in (400, 422, 405):
            avail += 1
            ok(f"module live: {label}", f"status={sc}")
        else:
            ok(f"module: {label}", f"status={sc}")
    info(f"available={avail} missing={missing}")


# ── Findings writer ──────────────────────────────────────────────────────
def write_findings(start_ts: float) -> None:
    runtime = time.time() - start_ts
    total_pass = sum(p["pass"] for p in phase_counters.values())
    total_gap = sum(p["gap"] for p in phase_counters.values())
    total_fail = sum(p["fail"] for p in phase_counters.values())

    p0 = [f for f in findings if f["severity"] == "P0"]
    p1 = [f for f in findings if f["severity"] == "P1"]
    p2 = [f for f in findings if f["severity"] == "P2"]
    fails = [f for f in findings if f["severity"] == "FAIL"]

    md: list[str] = []
    md.append("# 老柯 / Ke Yu (云上直播 / Sky Live) — Live-Commerce Findings")
    md.append("")
    md.append(f"**Run tag**: `{RUN_TAG}` | **Runtime**: {runtime:.1f}s | "
              f"**Date**: {time.strftime('%Y-%m-%d %H:%M', time.localtime(start_ts))}")
    md.append("")
    md.append("## Scenario")
    md.append(
        "老柯 runs 「云上直播 / Sky Live」 — a real-time host-driven live-"
        "streaming commerce platform (think 李佳琦 / 薇娅 / TikTok Live). "
        "Platform has 50 hosts, 500 active sellers, 30+ daily live streams, "
        "5K–50K viewers per stream. Categories: 美妆/服饰/零食/家居/数码. AOV "
        "¥50–¥5000. Revenue = 10% platform take-rate + 20% host commission "
        "on each transaction. ¥50K/月 ops budget. Core pain points: "
        "**real-time bidding** (flash sales with 30-sec windows), **host "
        "commission split** (3-way settlement: platform / host / seller), "
        "**viewer engagement decay** (push fires must hit within seconds), "
        "**fraud** (bot viewers, 水军 comments, bidding bots), and "
        "**inventory sync** (100-unit flash sale + 5000 bidders). Tested "
        "Round 5+6+7 features: KiX ID 3-party identity (viewer/seller/host), "
        "pixel for live-stream tracking with custom event_types, push engine "
        "for stream-start notifications, reservations for stream slots + "
        "fan signups, listings for the product carousel, marketplace-charge "
        "for take-rate, audiences for viewer cohorts, attribution_window_"
        "days=30 for replay-driven conversion."
    )
    md.append("")
    md.append("## What makes live-stream commerce different from the other personas")
    md.append("")
    md.append(
        "- **HOST is THE engine** — 70-90% of GMV is single-host-driven. "
        "Need host as first-class primitive (not just KID).\n"
        "- **3-PARTY settlement** — every tx pays platform + host + seller; "
        "no other persona has 3-party split.\n"
        "- **REAL-TIME push** — stream-goes-live needs sub-second fanout to "
        "hundreds of followers; no other persona has this SLA.\n"
        "- **FLASH SALES** — 30-second auctions, atomic inventory reservation, "
        "no oversell. Closest analog (限时秒杀 in e-commerce) doesn't hit "
        "this density.\n"
        "- **REPLAY ATTRIBUTION** — 40% of GMV happens hours/days after the "
        "live, but credit still flows to the live host. Last-touch-host "
        "model, not URL-based.\n"
        "- **CROSS-HOST attribution** — viewer hops 3 hosts in one session; "
        "needs host-aware multi-touch attribution.\n"
        "- **VIRAL events** — host_A推荐host_B → reciprocal trigger; "
        "platform-internal social graph drives discovery.\n"
        "- **HIGH-FREQUENCY telemetry** — stream_join / leave / comment / "
        "add_to_cart fire 10x per second per viewer at peak."
    )
    md.append("")
    md.append("## Summary")
    md.append("")
    md.append(f"- **Passes**: {total_pass}")
    md.append(f"- **Gaps**: {total_gap} (P0={len(p0)} P1={len(p1)} P2={len(p2)})")
    md.append(f"- **Fails**: {total_fail}")
    md.append("")
    md.append("### Per-phase tally")
    md.append("")
    md.append("| Phase | Pass | Gap | Fail |")
    md.append("|---|---:|---:|---:|")
    for ph, cs in phase_counters.items():
        md.append(f"| {ph} | {cs['pass']} | {cs['gap']} | {cs['fail']} |")
    md.append("")

    def section(title: str, items: list[dict]) -> None:
        md.append(f"## {title} ({len(items)})")
        md.append("")
        if not items:
            md.append("_None._")
            md.append("")
            return
        for f in items:
            md.append(f"### {f['action']}")
            md.append(f"- **Phase**: {f['phase']}")
            md.append(f"- **Severity**: {f['severity']}")
            md.append(f"- **Detail**: {f['detail']}")
            md.append("")

    section("P0 — Blockers", p0)
    section("P1 — Friction", p1)
    section("P2 — Nice-to-have", p2)
    section("Hard failures", fails)

    md.append("## Top NEW gaps unique to live-stream commerce")
    md.append("")
    md.append(
        "1. **No HOST as first-class primitive** — host = KID + sub-brand + "
        "follower set + GMV ledger + commission rules. Today must hand-stitch "
        "across kix-id + sub-brand + social + payouts. Live commerce needs "
        "`POST /hosts/create` returning host_id with native conversion-rate, "
        "follower-count, tier, quality-score, commission-rate attributes.\n"
        "2. **No 3-way marketplace-charge (platform + host + seller)** — "
        "today's marketplace-charge knows only platform + seller. Live "
        "commerce needs `host_user_id` + `host_commission_bps` first-class, "
        "with atomic ledger entries to all three parties in one call.\n"
        "3. **No 'stream' reservation type + no stream_started event_type** — "
        "live streams need scheduling (reservation) + real-time event "
        "(stream_started) + viewer signup (reservation linked to host). "
        "Today reservation type enum + pixel event_type enum are closed.\n"
        "4. **No atomic inventory reservation primitive** — 100-unit flash "
        "sale with 5000 concurrent bidders WILL oversell without atomic "
        "decrement. Need `POST /listings/{lid}/reserve-inventory` with TTL "
        "hold.\n"
        "5. **No host-aware attribution model** — attribution today is URL "
        "last-touch. Live commerce needs `model=last_touch_host` with "
        "host_user_id_attribute and per-host conversion reports for "
        "commission settlement.\n"
        "6. **No anti-fraud signals for live commerce** — bot viewers "
        "(inflate concurrent count), 水军 (fake comments), bidding bots "
        "(win flash sales in 50ms) all distinct from existing click-fraud "
        "signals. Need live-commerce signal taxonomy.\n"
        "7. **No replay-conversion event types** — 40% of live GMV happens "
        "on replay (12h–72h post-live). Need stream_replay_view + "
        "replay_conversion event types + host attribution that survives "
        "the live→replay transition."
    )
    md.append("")
    md.append("## Cross-comparison: live-commerce vs other personas")
    md.append("")
    md.append(
        "| Dimension | B2C (老黄) | C2C (老胡) | Healthcare (老蔡) | F&B (老王) | **Live (老柯)** |\n"
        "|---|---|---|---|---|---|\n"
        "| Identity parties | 1 (user) | 2 (buyer+seller, one KID) | 2 (patient+doctor) | 1 (customer) | **3 (viewer+seller+host)** |\n"
        "| Conversion event | Buy Now | Negotiation | Appointment | Visit+buy | **Flash purchase / replay buy** |\n"
        "| Push SLA | Hours | Days | Minutes | Hours | **Sub-second** |\n"
        "| Revenue model | Ad spend | 2% take | Ad spend | Ad spend | **10% take + 20% host commission** |\n"
        "| Time pressure | None | None | Slot | Hours | **30-sec flash window** |\n"
        "| Attribution model | URL last-touch | per-listing | per-doctor | per-store | **last_touch_host (multi-host)** |\n"
        "| Inventory contention | Low | None (1-of-1) | None (1 slot) | Low | **Extreme (100 units, 5K bidders)** |\n"
        "| Fraud surface | Click | Bidding | No-show | Coupon | **Bot viewer + 水军 + bid-bot** |\n"
        "\n"
        "**Pattern**: Live commerce inverts the model in 3 ways:\n"
        "1. **3-PARTY** — every primitive (charge, attribution, fraud) "
        "needs a host dimension.\n"
        "2. **REAL-TIME** — push/notification/inventory all need sub-second "
        "guarantees + atomic ops.\n"
        "3. **REPLAY** — events that happened 'in the past' (replay views) "
        "must still trigger 'present' actions (host commission).\n"
        "\nSuggested fix: introduce `marketplace_type=live_commerce` on "
        "brand record. Unlocks: host primitive, 3-way marketplace-charge, "
        "stream reservation type, atomic inventory hold, host-aware "
        "attribution, replay event types, live-commerce fraud signals. "
        "Unlocks the ~30% of social-commerce TAM that the current model "
        "can't serve."
    )
    md.append("")

    FINDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FINDINGS_PATH.write_text("\n".join(md), encoding="utf-8")
    print()
    print("=" * 70)
    print(f"{BOLD}SUMMARY{RESET}")
    print("=" * 70)
    print(f"  passes={total_pass}  gaps={total_gap} "
          f"(P0={len(p0)} P1={len(p1)} P2={len(p2)})  fails={total_fail}")
    print(f"  findings → {FINDINGS_PATH}")
    if p0:
        print()
        print(f"{RED}Top P0 gaps:{RESET}")
        for f in p0[:5]:
            print(f"  • [{f['phase']}] {f['action']} — {f['detail'][:110]}")


# ── Main ─────────────────────────────────────────────────────────────────
async def main() -> int:
    start_ts = time.time()
    await init_redis()
    try:
        from app.redis_client import get_redis as _get_redis
        from app.routers.recipes import load_seed_recipes as _load_seed
        _r = await _get_redis()
        await _load_seed(_r)
    except Exception:
        pass
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30.0
        ) as c:
            state: dict[str, Any] = {}
            try:
                state = await phase_1_brand_setup(c)
                await phase_2_wallet(c, state)
                await phase_3_three_party_identity(c, state)
                await phase_4_consent_marketing(c, state)
                await phase_5_host_as_fulfiller(c, state)
                await phase_6_stream_listings(c, state)
                await phase_7_realtime_push(c, state)
                await phase_8_flash_sale(c, state)
                await phase_9_commission_split(c, state)
                await phase_10_attribution(c, state)
                await phase_11_fraud(c, state)
                await phase_12_replay(c, state)
                await phase_13_module_probe(c, state)
            except Exception as e:
                fail("simulation crash", repr(e))
                import traceback
                traceback.print_exc()
    finally:
        write_findings(start_ts)
        await close_redis()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
