"""Tests for the prize fulfillment service + router (Wave-E item 3).

Coverage (18 tests):
  1.  Migration 0008 metadata + IF NOT EXISTS guards
  2.  create_prize_pool: happy path + validation errors
  3.  create_prize_pool rejects unsupported prize_type / fulfillment_method
  4.  Jurisdictional cap: SG > SGD 10,000 refused
  5.  Jurisdictional bans: CN cash prize blocked
  6.  Instant-win roll: 100% probability → guaranteed win, inventory-1
  7.  Instant-win roll: 0% probability → no_win, inventory unchanged
  8.  Instant-win roll: inventory exhausted → roll returns no_win
  9.  Instant-win roll: rate-limit kicks in after threshold
  10. record_winner: race-safe atomic decrement under concurrent calls
  11. US W-9 trigger flag set on winners with value ≥ $600
  12. EU eligibility blocks without recorded GDPR consent
  13. Sweepstakes enter + draw end-to-end
  14. verify_contact_info + double-verify rejection
  15. initiate_fulfillment requires contact verification + EU legal ack
  16. mark_claimed terminal-state transitions (shipped, delivered)
  17. expire_unclaimed returns inventory to pool
  18. Admin endpoints require X-Admin-Token (403 without)
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services import prize_fulfillment as svc  # noqa: E402


# ── 1. Migration metadata ────────────────────────────────────────────────


def test_migration_0008_metadata_and_idempotency():
    spec = importlib.util.spec_from_file_location(
        "_prize_mig", "migrations/versions/0008_prizes.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0008_prizes"
    assert mod.down_revision == "0007_audit_log"
    assert mod.branch_labels is None

    src = (REPO_ROOT / "migrations/versions/0008_prizes.py").read_text()
    create_count = src.count("CREATE TABLE") + src.count("CREATE INDEX")
    guarded = src.count("IF NOT EXISTS")
    assert create_count > 0
    assert guarded >= create_count, (
        f"unguarded DDL: {create_count} CREATEs vs {guarded} guards"
    )
    # Required columns are present.
    for col in (
        "prize_id", "brand_id", "campaign_id", "prize_type",
        "win_probability_pct", "instant_win", "sweepstakes_draw_at",
        "fulfillment_method", "legal_disclaimer", "jurisdiction",
        "claim_status", "fulfillment_data", "contact_info_verified",
        "legal_acknowledgment_at",
    ):
        assert col in src, f"missing column {col}"


# ── 2. create_prize_pool happy path ──────────────────────────────────────


@pytest.mark.asyncio
async def test_create_prize_pool_happy_path(clean_redis):
    r = clean_redis
    out = await svc.create_prize_pool(
        r,
        brand_id="brand_a",
        prizes=[
            {
                "name": "Free Coffee",
                "prize_type": "voucher",
                "value_cents": 500,
                "inventory_count": 100,
                "win_probability_pct": 10.0,
                "instant_win": True,
                "fulfillment_method": "digital_voucher",
                "jurisdiction": "sg",
            }
        ],
    )
    assert len(out) == 1
    assert out[0]["name"] == "Free Coffee"
    assert out[0]["instant_win"] is True
    assert out[0]["inventory_count"] == 100
    assert out[0]["inventory_claimed"] == 0
    assert out[0]["win_probability_pct"] == 10.0

    # Roundtrip read
    p = await svc.get_prize(r, out[0]["prize_id"])
    assert p["name"] == "Free Coffee"


# ── 3. invalid prize_type / fulfillment_method ───────────────────────────


@pytest.mark.asyncio
async def test_invalid_prize_type_rejected(clean_redis):
    with pytest.raises(svc.PrizeError) as ei:
        await svc.create_prize_pool(
            clean_redis,
            brand_id="brand_x",
            prizes=[{"name": "n", "prize_type": "magic_beans"}],
        )
    assert ei.value.code == "invalid_prize_type"


@pytest.mark.asyncio
async def test_invalid_fulfillment_method_rejected(clean_redis):
    with pytest.raises(svc.PrizeError) as ei:
        await svc.create_prize_pool(
            clean_redis,
            brand_id="brand_x",
            prizes=[
                {
                    "name": "n",
                    "prize_type": "voucher",
                    "fulfillment_method": "carrier_pigeon",
                }
            ],
        )
    assert ei.value.code == "invalid_fulfillment_method"


# ── 4. SG value cap ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sg_value_cap_enforced(clean_redis):
    with pytest.raises(svc.PrizeError) as ei:
        await svc.create_prize_pool(
            clean_redis,
            brand_id="brand_sg",
            prizes=[
                {
                    "name": "Luxury Watch",
                    "prize_type": "physical",
                    "value_cents": svc.SG_DEFAULT_PRIZE_CAP_CENTS + 100,
                    "jurisdiction": "sg",
                }
            ],
        )
    assert ei.value.code == "value_above_jurisdiction_cap"


# ── 5. CN cash prize blocked ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cn_cash_prize_blocked(clean_redis):
    with pytest.raises(svc.PrizeError) as ei:
        await svc.create_prize_pool(
            clean_redis,
            brand_id="brand_cn",
            prizes=[
                {
                    "name": "Cash 100",
                    "prize_type": "cash",
                    "value_cents": 10_000,
                    "jurisdiction": "cn",
                }
            ],
        )
    assert ei.value.code == "jurisdiction_disallows_prize_type"


# ── 6. Instant-win 100% probability guaranteed ───────────────────────────


@pytest.mark.asyncio
async def test_instant_win_100pct_guaranteed(clean_redis):
    r = clean_redis
    [prize] = await svc.create_prize_pool(
        r,
        brand_id="brand_w",
        prizes=[
            {
                "name": "Sure Win",
                "prize_type": "voucher",
                "value_cents": 100,
                "inventory_count": 3,
                "win_probability_pct": 100.0,
                "instant_win": True,
                "campaign_id": "camp_w",
                "jurisdiction": "sg",
            }
        ],
    )
    result = await svc.try_instant_win(
        r,
        user_id="user_aaa",
        campaign_id="camp_w",
        jurisdiction="sg",
        user_age=30,
    )
    assert result.won is True
    assert result.prize_id == prize["prize_id"]
    assert result.winner_id is not None

    refreshed = await svc.get_prize(r, prize["prize_id"])
    assert refreshed["inventory_claimed"] == 1


# ── 7. Instant-win 0% probability never wins ─────────────────────────────


@pytest.mark.asyncio
async def test_instant_win_0pct_never_wins(clean_redis):
    r = clean_redis
    [prize] = await svc.create_prize_pool(
        r,
        brand_id="brand_w",
        prizes=[
            {
                "name": "Never",
                "prize_type": "voucher",
                "value_cents": 100,
                "inventory_count": 5,
                "win_probability_pct": 0.0,
                "instant_win": True,
                "campaign_id": "camp_n",
                "jurisdiction": "sg",
            }
        ],
    )
    result = await svc.try_instant_win(
        r,
        user_id="user_n",
        campaign_id="camp_n",
        jurisdiction="sg",
        user_age=30,
    )
    assert result.won is False
    refreshed = await svc.get_prize(r, prize["prize_id"])
    assert refreshed["inventory_claimed"] == 0


# ── 8. Inventory exhaustion ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_instant_win_inventory_exhausted(clean_redis):
    r = clean_redis
    [prize] = await svc.create_prize_pool(
        r,
        brand_id="brand_e",
        prizes=[
            {
                "name": "Limited",
                "prize_type": "voucher",
                "value_cents": 100,
                "inventory_count": 1,
                "win_probability_pct": 100.0,
                "instant_win": True,
                "campaign_id": "camp_e",
                "jurisdiction": "sg",
            }
        ],
    )
    # First win consumes the only inventory slot.
    r1 = await svc.try_instant_win(
        r, user_id="ua", campaign_id="camp_e", jurisdiction="sg", user_age=30,
    )
    assert r1.won is True
    # Second roll finds an exhausted pool → no_win.
    r2 = await svc.try_instant_win(
        r, user_id="ub", campaign_id="camp_e", jurisdiction="sg", user_age=30,
    )
    assert r2.won is False
    assert r2.reason in ("no_win", "empty_pool")
    refreshed = await svc.get_prize(r, prize["prize_id"])
    assert refreshed["inventory_claimed"] == 1


# ── 9. Rate limit ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_instant_win_rate_limited(clean_redis):
    r = clean_redis
    await svc.create_prize_pool(
        r,
        brand_id="brand_r",
        prizes=[
            {
                "name": "Roll-bait",
                "prize_type": "voucher",
                "value_cents": 1,
                "inventory_count": 1000,
                "win_probability_pct": 0.0,
                "instant_win": True,
                "campaign_id": "camp_r",
                "jurisdiction": "sg",
            }
        ],
    )
    # Burn the rate-limit budget.
    for _ in range(svc.RATE_LIMIT_ATTEMPTS_PER_HOUR):
        rr = await svc.try_instant_win(
            r, user_id="rate_user", campaign_id="camp_r",
            jurisdiction="sg", user_age=30,
        )
        assert rr.won is False
        assert rr.reason in ("no_win",)
    blocked = await svc.try_instant_win(
        r, user_id="rate_user", campaign_id="camp_r",
        jurisdiction="sg", user_age=30,
    )
    assert blocked.reason == "rate_limited"


# ── 10. Concurrent record_winner atomicity ───────────────────────────────


@pytest.mark.asyncio
async def test_record_winner_atomic_under_contention(clean_redis):
    r = clean_redis
    [prize] = await svc.create_prize_pool(
        r,
        brand_id="brand_atom",
        prizes=[
            {
                "name": "Race",
                "prize_type": "voucher",
                "value_cents": 100,
                "inventory_count": 3,
                "win_probability_pct": 100.0,
                "instant_win": True,
                "jurisdiction": "sg",
            }
        ],
    )
    pid = prize["prize_id"]

    async def _one(uid):
        try:
            return await svc.record_winner(
                r, prize_id=pid, user_id=uid,
                brand_id="brand_atom", jurisdiction="sg",
            )
        except svc.PrizeError as exc:
            return f"err:{exc.code}"

    results = await asyncio.gather(*[_one(f"u{i}") for i in range(10)])
    successes = [x for x in results if not x.startswith("err:")]
    exhausted = [x for x in results if x == "err:inventory_exhausted"]
    # Inventory was 3 — exactly 3 winners, exactly 7 exhausted errors.
    assert len(successes) == 3
    assert len(exhausted) == 7
    refreshed = await svc.get_prize(r, pid)
    assert refreshed["inventory_claimed"] == 3


# ── 11. US W-9 flag for ≥ $600 prizes ────────────────────────────────────


@pytest.mark.asyncio
async def test_us_w9_flag_on_high_value_prize(clean_redis):
    r = clean_redis
    [prize] = await svc.create_prize_pool(
        r,
        brand_id="brand_us",
        prizes=[
            {
                "name": "Big Cash",
                "prize_type": "cash",
                "value_cents": svc.US_W9_THRESHOLD_CENTS + 1,
                "inventory_count": 5,
                "win_probability_pct": 100.0,
                "instant_win": True,
                "jurisdiction": "us",
            }
        ],
    )
    wid = await svc.record_winner(
        r, prize_id=prize["prize_id"], user_id="us_user",
        brand_id="brand_us", jurisdiction="us",
    )
    w = await svc.get_winner(r, wid)
    assert w["fulfillment_data"].get("w9_required") is True
    assert (
        w["fulfillment_data"].get("irs_1099_threshold_cents")
        == svc.US_W9_THRESHOLD_CENTS
    )

    # Below the threshold: no flag.
    [low] = await svc.create_prize_pool(
        r,
        brand_id="brand_us",
        prizes=[
            {
                "name": "Small Cash",
                "prize_type": "cash",
                "value_cents": 100,
                "inventory_count": 5,
                "win_probability_pct": 100.0,
                "instant_win": True,
                "jurisdiction": "us",
            }
        ],
    )
    wid2 = await svc.record_winner(
        r, prize_id=low["prize_id"], user_id="us_user2",
        brand_id="brand_us", jurisdiction="us",
    )
    w2 = await svc.get_winner(r, wid2)
    assert "w9_required" not in w2["fulfillment_data"]


# ── 12. EU GDPR consent gate ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_eu_eligibility_requires_gdpr_consent(clean_redis):
    r = clean_redis
    ok, why, _ev = await svc.verify_legal_eligibility(
        r, user_id="eu_user", jurisdiction="eu", user_age=25,
    )
    assert ok is False
    assert why == "gdpr_consent_required"

    # Now record the consent flag.
    await r.set("user:eu_user:gdpr_consent", "1700000000")
    ok2, why2, ev2 = await svc.verify_legal_eligibility(
        r, user_id="eu_user", jurisdiction="eu", user_age=25,
    )
    assert ok2 is True
    assert ev2["gdpr_consent_at"] == 1700000000


# ── 13. Sweepstakes end-to-end ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweepstakes_enter_and_draw(clean_redis, monkeypatch):
    r = clean_redis
    future = svc._now() + 60
    [prize] = await svc.create_prize_pool(
        r,
        brand_id="brand_s",
        prizes=[
            {
                "name": "Grand Prize",
                "prize_type": "physical",
                "value_cents": 50_000,
                "inventory_count": 2,
                "instant_win": False,
                "sweepstakes_draw_at": future,
                "jurisdiction": "sg",
            }
        ],
    )
    pid = prize["prize_id"]
    for uid in ("u1", "u2", "u3", "u4", "u5"):
        await svc.enter_sweepstakes(
            r, prize_id=pid, user_id=uid, jurisdiction="sg", user_age=30,
        )

    # Cannot draw before draw_at.
    with pytest.raises(svc.PrizeError) as ei:
        await svc.draw_sweepstakes(r, prize_id=pid)
    assert ei.value.code == "sweepstakes_not_ready"

    # Fast-forward by patching _now.
    monkeypatch.setattr(svc, "_now", lambda: future + 1)
    winners = await svc.draw_sweepstakes(r, prize_id=pid)
    assert len(winners) == 2
    refreshed = await svc.get_prize(r, pid)
    assert refreshed["inventory_claimed"] == 2


# ── 14. verify_contact_info ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_contact_then_double_verify(clean_redis):
    r = clean_redis
    [prize] = await svc.create_prize_pool(
        r, brand_id="b14",
        prizes=[
            {
                "name": "p", "prize_type": "voucher", "value_cents": 100,
                "inventory_count": 1, "win_probability_pct": 100.0,
                "instant_win": True, "jurisdiction": "sg",
            }
        ],
    )
    wid = await svc.record_winner(
        r, prize_id=prize["prize_id"], user_id="u14",
        brand_id="b14", jurisdiction="sg",
    )
    out = await svc.verify_contact_info(
        r, winner_id=wid,
        contact_method="email", contact_value="me@example.com",
    )
    assert out["ok"] is True
    w = await svc.get_winner(r, wid)
    assert w["contact_info_verified"] is True
    # Raw email never stored — only hash.
    assert "me@example.com" not in str(w["fulfillment_data"])
    assert w["fulfillment_data"].get("contact_value_hash")

    # Double-verify rejected.
    with pytest.raises(svc.PrizeError) as ei:
        await svc.verify_contact_info(
            r, winner_id=wid,
            contact_method="email", contact_value="me@example.com",
        )
    assert ei.value.code == "already_verified"


# ── 15. initiate_fulfillment preconditions ───────────────────────────────


@pytest.mark.asyncio
async def test_initiate_fulfillment_requires_verification(clean_redis):
    r = clean_redis
    [prize] = await svc.create_prize_pool(
        r, brand_id="b15",
        prizes=[
            {
                "name": "Prize", "prize_type": "voucher",
                "value_cents": 100, "inventory_count": 1,
                "win_probability_pct": 100.0, "instant_win": True,
                "jurisdiction": "eu",
            }
        ],
    )
    # Pre-record GDPR consent so record_winner works downstream.
    await r.set("user:eu_user_15:gdpr_consent", str(svc._now()))
    wid = await svc.record_winner(
        r, prize_id=prize["prize_id"], user_id="eu_user_15",
        brand_id="b15", jurisdiction="eu",
    )

    # 1) Without contact verification → 412.
    with pytest.raises(svc.PrizeError) as ei:
        await svc.initiate_fulfillment(r, winner_id=wid)
    assert ei.value.code == "contact_not_verified"

    # 2) Verify contact, still missing legal_ack for EU.
    await svc.verify_contact_info(
        r, winner_id=wid,
        contact_method="email", contact_value="x@y.com",
    )
    with pytest.raises(svc.PrizeError) as ei2:
        await svc.initiate_fulfillment(r, winner_id=wid)
    assert ei2.value.code == "legal_ack_required"

    # 3) Record acknowledgment, succeed, notification queued.
    await svc.record_legal_acknowledgment(r, winner_id=wid)
    res = await svc.initiate_fulfillment(r, winner_id=wid, locale="zh-Hans-SG")
    assert res["ok"] is True
    queued = await r.lrange("prize:notify_queue", 0, -1)
    assert len(queued) == 1
    import json as _json
    decoded = _json.loads(queued[0])
    assert "恭喜" in decoded["subject"]
    assert decoded["locale"] == "zh-Hans-SG"


# ── 16. mark_claimed transitions ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_claimed_transitions(clean_redis):
    r = clean_redis
    [prize] = await svc.create_prize_pool(
        r, brand_id="b16",
        prizes=[
            {
                "name": "Mailbox", "prize_type": "physical",
                "value_cents": 5000, "inventory_count": 1,
                "win_probability_pct": 100.0, "instant_win": True,
                "fulfillment_method": "mail", "jurisdiction": "sg",
            }
        ],
    )
    wid = await svc.record_winner(
        r, prize_id=prize["prize_id"], user_id="u16",
        brand_id="b16", jurisdiction="sg",
    )
    await svc.verify_contact_info(
        r, winner_id=wid, contact_method="email", contact_value="u@x.com",
    )
    await svc.initiate_fulfillment(r, winner_id=wid)
    # Now ship.
    out_s = await svc.mark_claimed(
        r, winner_id=wid, evidence={"tracking": "TRACK123"},
        new_status="shipped",
    )
    assert out_s["claim_status"] == "shipped"
    out_d = await svc.mark_claimed(
        r, winner_id=wid, evidence={"signed_by": "John"},
        new_status="delivered",
    )
    assert out_d["claim_status"] == "delivered"

    with pytest.raises(svc.PrizeError):
        await svc.mark_claimed(
            r, winner_id=wid, new_status="cancelled",
        )


# ── 17. expire_unclaimed returns inventory ───────────────────────────────


@pytest.mark.asyncio
async def test_expire_unclaimed_returns_inventory(clean_redis, monkeypatch):
    r = clean_redis
    [prize] = await svc.create_prize_pool(
        r, brand_id="b17",
        prizes=[
            {
                "name": "Returner", "prize_type": "voucher",
                "value_cents": 100, "inventory_count": 5,
                "win_probability_pct": 100.0, "instant_win": True,
                "jurisdiction": "sg",
            }
        ],
    )
    wid = await svc.record_winner(
        r, prize_id=prize["prize_id"], user_id="u17",
        brand_id="b17", jurisdiction="sg",
    )
    refreshed = await svc.get_prize(r, prize["prize_id"])
    assert refreshed["inventory_claimed"] == 1

    # Fast-forward past deadline.
    future = svc._now() + (svc.DEFAULT_CLAIM_DEADLINE_DAYS + 1) * 86_400
    res = await svc.expire_unclaimed(r, now=future)
    assert res["expired"] == 1
    assert res["returned_to_pool"] == 1
    refreshed2 = await svc.get_prize(r, prize["prize_id"])
    assert refreshed2["inventory_claimed"] == 0
    w = await svc.get_winner(r, wid)
    assert w["claim_status"] == "expired"


# ── 18. Admin endpoints require admin token ──────────────────────────────


@pytest.mark.asyncio
async def test_admin_endpoints_require_admin_token(client, monkeypatch):
    # Without token: 403.
    monkeypatch.setenv("KIX_ADMIN_TOKEN", "the-token-xyz")
    res = await client.get("/api/v1/admin/prizes/winners/queue")
    assert res.status_code == 403

    res2 = await client.get(
        "/api/v1/admin/prizes/winners/queue",
        headers={"X-Admin-Token": "wrong"},
    )
    assert res2.status_code == 403

    # With correct token: 200.
    res3 = await client.get(
        "/api/v1/admin/prizes/winners/queue",
        headers={"X-Admin-Token": "the-token-xyz"},
    )
    assert res3.status_code == 200
    body = res3.json()
    assert "queue" in body and "count" in body
