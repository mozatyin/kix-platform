"""Tests — Wave G #3 Viral Amplifier + Orchestrator.

15+ tests covering 7 triggers, fatigue cap, quiet hours, depth cap,
chain-bonus, A/B variants, K-factor per trigger, audit log, performance.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from app.services import viral_amplifier as va
from app.services import viral_orchestrator as vo


# ── Helpers ───────────────────────────────────────────────────────────────


def _force_daytime(monkeypatch) -> None:
    """Force is_quiet_hours()==False (SG 13:00 local)."""

    fixed = datetime(2026, 6, 1, 5, 0, tzinfo=timezone.utc).timestamp()  # 13:00 SG
    monkeypatch.setattr(va, "_now", lambda: int(fixed))


def _force_quiet(monkeypatch) -> None:
    fixed = datetime(2026, 6, 1, 17, 0, tzinfo=timezone.utc).timestamp()  # 01:00 SG
    monkeypatch.setattr(va, "_now", lambda: int(fixed))


# ── 1. Each of the 7 triggers fires correctly ────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", list(va.ALL_AMP_TRIGGERS))
async def test_each_trigger_fires(clean_redis, monkeypatch, trigger):
    _force_daytime(monkeypatch)
    r = clean_redis
    res = await va.emit_trigger(
        r,
        user_id=f"u_{trigger}",
        brand_id="b1",
        trigger=trigger,
        context={"score": 123, "voucher_cents": 500},
    )
    assert res["sent"] is True
    assert res["trigger"] == trigger
    assert res["invite_token"]
    assert res["share_url"].startswith("https://play.kix.app")
    assert res["share_text"]
    assert res["ab_arm"] in va.TRIGGER_VARIANTS[trigger]


# ── 2. K-factor computed per trigger ─────────────────────────────────────


@pytest.mark.asyncio
async def test_kfactor_per_trigger(clean_redis, monkeypatch):
    _force_daytime(monkeypatch)
    r = clean_redis
    # 4 sends across 2 triggers, 2 redemptions (1 each trigger).
    em1 = await va.emit_trigger(
        r, user_id="u1", brand_id="b1", trigger=va.TRIGGER_VOUCHER_WON,
    )
    em2 = await va.emit_trigger(
        r, user_id="u1", brand_id="b1",
        trigger=va.TRIGGER_GAME_COMPLETION,
    )
    await va.emit_trigger(
        r, user_id="u2", brand_id="b1", trigger=va.TRIGGER_VOUCHER_WON,
    )
    await va.emit_trigger(
        r, user_id="u2", brand_id="b1",
        trigger=va.TRIGGER_GAME_COMPLETION,
    )
    await va.record_redemption(
        r, invite_token=em1["invite_token"],
        redeemer_user_id="z1", brand_id="b1",
    )
    await va.record_redemption(
        r, invite_token=em2["invite_token"],
        redeemer_user_id="z2", brand_id="b1",
    )

    br = await va.kfactor_breakdown(r, "b1", window_days=1)
    assert br["total_sent"] == 4
    assert br["total_redeemed"] == 2
    assert br["cumulative_k"] == 0.5
    assert br["per_trigger"][va.TRIGGER_VOUCHER_WON]["k_factor"] == 0.5
    assert br["per_trigger"][va.TRIGGER_GAME_COMPLETION]["k_factor"] == 0.5
    assert br["historical_baseline_k"] == 0.40
    assert br["delta_vs_baseline"] == round(0.5 - 0.40, 4)
    # cumulative below target
    assert br["self_sustaining"] is False


# ── 3. Compounding chain → fresh invite token on redeem ──────────────────


@pytest.mark.asyncio
async def test_compounding_chain_advances_depth(clean_redis, monkeypatch):
    _force_daytime(monkeypatch)
    r = clean_redis
    em = await va.emit_trigger(
        r, user_id="alice", brand_id="b1",
        trigger=va.TRIGGER_VOUCHER_WON,
    )
    rd = await va.record_redemption(
        r, invite_token=em["invite_token"],
        redeemer_user_id="bob", brand_id="b1",
    )
    assert rd["redeemed"] is True
    assert rd["new_depth"] == 1
    # Bob now emits next-leg invite at depth=1
    em2 = await va.emit_trigger(
        r, user_id="bob", brand_id="b1",
        trigger=va.TRIGGER_VOUCHER_WON, inherited_depth=1,
    )
    assert em2["sent"] is True
    assert em2["depth"] == 1


# ── 4. Fatigue cap (max 3/day per user) ──────────────────────────────────


@pytest.mark.asyncio
async def test_daily_fatigue_cap_three(clean_redis, monkeypatch):
    _force_daytime(monkeypatch)
    r = clean_redis
    for i in range(va.DAILY_USER_QUOTA):
        res = await va.emit_trigger(
            r, user_id="fatigued", brand_id="b1",
            trigger=va.TRIGGER_GAME_COMPLETION,
        )
        assert res["sent"] is True, f"call {i} should pass"
    res4 = await va.emit_trigger(
        r, user_id="fatigued", brand_id="b1",
        trigger=va.TRIGGER_GAME_COMPLETION,
    )
    assert res4["sent"] is False
    assert res4["reason"] == "daily_quota_exhausted"


# ── 5. Quiet hours respected ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_quiet_hours_blocks_emit(clean_redis, monkeypatch):
    _force_quiet(monkeypatch)
    r = clean_redis
    res = await va.emit_trigger(
        r, user_id="night_owl", brand_id="b1",
        trigger=va.TRIGGER_GAME_COMPLETION,
    )
    assert res["sent"] is False
    assert res["reason"] == "quiet_hours"


# ── 6. Depth cap at 7 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_depth_cap_seven(clean_redis, monkeypatch):
    _force_daytime(monkeypatch)
    r = clean_redis
    res = await va.emit_trigger(
        r, user_id="deep", brand_id="b1",
        trigger=va.TRIGGER_VOUCHER_WON,
        inherited_depth=va.MAX_INHERITANCE_DEPTH,  # at cap
    )
    assert res["sent"] is False
    assert res["reason"] == "depth_cap_reached"
    assert res["depth_cap"] == 7


# ── 7. Depth bonus reward when chain hits 5+ ─────────────────────────────


@pytest.mark.asyncio
async def test_depth_bonus_awarded_at_threshold(clean_redis, monkeypatch):
    _force_daytime(monkeypatch)
    r = clean_redis
    info = await va.record_chain_depth(r, "winner", va.DEPTH_BONUS_THRESHOLD)
    assert info["depth_bonus_awarded"] is True
    assert info["depth_bonus_cents"] == va.DEPTH_BONUS_VOUCHER_CENTS
    # idempotent — second hit at same depth: no double-bonus
    info2 = await va.record_chain_depth(
        r, "winner", va.DEPTH_BONUS_THRESHOLD + 1
    )
    assert info2["depth_bonus_awarded"] is False


# ── 8. Trigger orchestrator picks best (highest prior_K + recency) ───────


@pytest.mark.asyncio
async def test_orchestrator_picks_best_trigger(clean_redis, monkeypatch):
    _force_daytime(monkeypatch)
    r = clean_redis
    # birthday has prior_K=0.65 (highest among candidates).
    res = await vo.decide_and_emit(
        r,
        user_id="picky",
        brand_id="b1",
        candidate_triggers=[
            va.TRIGGER_GAME_COMPLETION,
            va.TRIGGER_BIRTHDAY,
            va.TRIGGER_RE_ENGAGEMENT,
        ],
        context={},
    )
    assert res["sent"] is True
    assert res["selection"]["chosen"] == va.TRIGGER_BIRTHDAY


# ── 9. A/B variants assigned + sticky per user ──────────────────────────


@pytest.mark.asyncio
async def test_ab_variants_sticky(clean_redis, monkeypatch):
    _force_daytime(monkeypatch)
    r = clean_redis
    arm1 = await va._pick_ab_arm(r, "stuck", va.TRIGGER_VOUCHER_WON)
    arm2 = await va._pick_ab_arm(r, "stuck", va.TRIGGER_VOUCHER_WON)
    assert arm1 == arm2
    assert arm1 in va.TRIGGER_VARIANTS[va.TRIGGER_VOUCHER_WON]


# ── 10. Audit log entries are written ───────────────────────────────────


@pytest.mark.asyncio
async def test_audit_log_entries(clean_redis, monkeypatch):
    _force_daytime(monkeypatch)
    r = clean_redis
    em = await va.emit_trigger(
        r, user_id="audited", brand_id="b1",
        trigger=va.TRIGGER_VOUCHER_WON,
    )
    await va.record_redemption(
        r, invite_token=em["invite_token"],
        redeemer_user_id="bob", brand_id="b1",
    )
    raw = await r.lrange(va._k_audit("b1", va._today_ymd()), 0, -1)
    assert len(raw) == 2
    import json
    events = [json.loads(x) for x in raw]
    assert {e["ev"] for e in events} == {"emit", "redeem"}


# ── 11. Cooldown between two emits for same user ────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_cooldown(clean_redis, monkeypatch):
    _force_daytime(monkeypatch)
    r = clean_redis
    first = await vo.decide_and_emit(
        r,
        user_id="cool",
        brand_id="b1",
        candidate_triggers=[va.TRIGGER_VOUCHER_WON],
    )
    assert first["sent"] is True
    # immediate retry → blocked by MIN_GAP_SEC
    second = await vo.decide_and_emit(
        r,
        user_id="cool",
        brand_id="b1",
        candidate_triggers=[va.TRIGGER_VOUCHER_WON],
    )
    assert second["sent"] is False
    assert second["reason"] == "cooldown"


# ── 12. Redeem idempotency ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redeem_idempotent(clean_redis, monkeypatch):
    _force_daytime(monkeypatch)
    r = clean_redis
    em = await va.emit_trigger(
        r, user_id="i", brand_id="b1", trigger=va.TRIGGER_BIRTHDAY,
    )
    a = await va.record_redemption(
        r, invite_token=em["invite_token"],
        redeemer_user_id="x", brand_id="b1",
    )
    b = await va.record_redemption(
        r, invite_token=em["invite_token"],
        redeemer_user_id="x", brand_id="b1",
    )
    assert a["redeemed"] is True
    assert b["redeemed"] is False and b["reason"] == "already"


# ── 13. Self-redeem & brand mismatch rejected ───────────────────────────


@pytest.mark.asyncio
async def test_self_and_cross_brand_redeem_rejected(clean_redis, monkeypatch):
    _force_daytime(monkeypatch)
    r = clean_redis
    em = await va.emit_trigger(
        r, user_id="self", brand_id="b1",
        trigger=va.TRIGGER_GEOFENCE_FRIEND,
    )
    rself = await va.record_redemption(
        r, invite_token=em["invite_token"],
        redeemer_user_id="self", brand_id="b1",
    )
    rxbrand = await va.record_redemption(
        r, invite_token=em["invite_token"],
        redeemer_user_id="other", brand_id="b2",
    )
    assert rself["redeemed"] is False and rself["reason"] == "self_redeem"
    assert rxbrand["redeemed"] is False and rxbrand["reason"] == "brand_mismatch"


# ── 14. Performance under load ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_performance_under_load(clean_redis, monkeypatch):
    _force_daytime(monkeypatch)
    r = clean_redis
    t0 = time.time()
    # 200 emits across 200 distinct users → ~quota-1 each.
    for i in range(200):
        res = await va.emit_trigger(
            r, user_id=f"perf_{i}", brand_id="b_perf",
            trigger=va.TRIGGER_ACHIEVEMENT_UNLOCK,
        )
        assert res["sent"] is True
    dt = time.time() - t0
    # Loose threshold: 200 emits should finish under 5 s on local redis.
    assert dt < 5.0
    br = await va.kfactor_breakdown(r, "b_perf", window_days=1)
    assert br["total_sent"] == 200


# ── 15. Cumulative K crosses 1.0 → self_sustaining flag flips ───────────


@pytest.mark.asyncio
async def test_self_sustaining_flag(clean_redis, monkeypatch):
    _force_daytime(monkeypatch)
    r = clean_redis
    # Force scenario where redemptions >= sends (e.g. multi-redeem via
    # multi-leg accounting in real life). Here we just synthesize.
    sent_key = va._k_trigger_sent_day("b_sat", va.TRIGGER_BIRTHDAY,
                                      va._today_ymd())
    red_key = va._k_trigger_redeemed_day("b_sat", va.TRIGGER_BIRTHDAY,
                                         va._today_ymd())
    await r.set(sent_key, 10, ex=86_400)
    await r.set(red_key, 12, ex=86_400)
    br = await va.kfactor_breakdown(r, "b_sat", window_days=1)
    assert br["cumulative_k"] >= 1.0
    assert br["self_sustaining"] is True
    assert br["delta_vs_baseline"] > 0.5  # well above 0.40 baseline


# ── 16. Orchestrator filters unknown trigger candidates ────────────────


@pytest.mark.asyncio
async def test_orchestrator_filters_unknown(clean_redis, monkeypatch):
    _force_daytime(monkeypatch)
    r = clean_redis
    res = await vo.decide_and_emit(
        r, user_id="filt", brand_id="b1",
        candidate_triggers=["bogus_trigger", "nonexistent"],
    )
    assert res["sent"] is False
    assert res["reason"] == "no_valid_candidates"


# ── 17. Quota_remaining decreases predictably ──────────────────────────


@pytest.mark.asyncio
async def test_quota_remaining_decrement(clean_redis, monkeypatch):
    _force_daytime(monkeypatch)
    r = clean_redis
    assert await va.quota_remaining(r, "qq") == va.DAILY_USER_QUOTA
    await va.emit_trigger(
        r, user_id="qq", brand_id="b1",
        trigger=va.TRIGGER_GAME_COMPLETION,
    )
    assert await va.quota_remaining(r, "qq") == va.DAILY_USER_QUOTA - 1
