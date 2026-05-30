"""Tests for the Wave-E Step-5 re-engagement automation.

Covers:
  * Lapse detection + cascade selection thresholds (§3 trigger conditions)
  * Cascade timing — step_idx advances + next_due_ts honours the offsets
  * Channel selection — WhatsApp > push > email, falls back to whichever
    the user is reachable on
  * Frequency cap — second send within 7d is suppressed
  * Quiet hours suppression (SGT 22:00-07:00)
  * Opt-out + just-redeemed suppression
  * Audit log entries written for start + send + suppress
  * Stats counters increment correctly
  * HTTP endpoints (start-cascade, cascade-stats, at-risk-cohort, test-cascade)
  * Worker run_once iterates brands + ticks cascades
  * TriSoul-personalised tone reflected in crafted message

All tests use the standard ``client`` + ``clean_redis`` fixtures from
``tests/conftest.py``. We monkeypatch ``datetime.now`` indirectly by
passing explicit ``now_utc`` / ``now`` arguments so the suite is
deterministic regardless of when CI runs.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import pytest

from app.services import reengagement_orchestrator as reo
from app.services.reengagement_orchestrator import (
    CASCADE_BLUEPRINTS,
    CascadeType,
    active_cascade_key,
    atrisk_cohort_key,
    audit_log_key,
    last_send_key,
    optout_key,
    stats_key,
)
from app.workers import reengagement_worker as worker

ADMIN_TOKEN = os.getenv("KIX_ADMIN_TOKEN", "admin-dev-token")
ADMIN_HEADERS = {"X-Admin-Token": ADMIN_TOKEN}


# ── helpers ──────────────────────────────────────────────────────────────


async def _seed_user(
    r,
    brand_id: str,
    user_id: str,
    *,
    last_visit_days_ago: float = 0.0,
    unused_vouchers: int = 0,
    ltv: float = 0.0,
    has_email: bool = False,
    has_whatsapp: bool = False,
    has_push: bool = False,
    now: float | None = None,
) -> None:
    now = now if now is not None else time.time()
    if last_visit_days_ago >= 0:
        await r.set(
            f"user:{user_id}:last_visit:{brand_id}",
            str(now - last_visit_days_ago * 86_400),
        )
    if unused_vouchers:
        await r.set(
            f"voucher:user:{user_id}:brand:{brand_id}:unused",
            str(unused_vouchers),
        )
    if ltv:
        await r.set(f"ltv:{brand_id}:{user_id}", str(ltv))
    if has_email:
        await r.hset(f"user_profile:{user_id}", mapping={"email": f"{user_id}@example.com"})
    if has_whatsapp:
        await r.set(f"whatsapp:opt:{brand_id}:{user_id}", "1")
    if has_push:
        await r.sadd(f"kid:{user_id}:push_devices", "dev_1")
        await r.hset(
            "push_device:dev_1",
            mapping={"kid": user_id, "platform": "android", "token": "tok", "active": "1"},
        )
    await r.sadd(f"brand:{brand_id}:users", user_id)


# ── 1. Lapse detection / cascade selection ──────────────────────────────


@pytest.mark.asyncio
async def test_lapse_score_and_cascade_select(clean_redis):
    r = clean_redis
    now = 1_700_000_000.0

    # Fresh user (visited 1d ago) → no cascade.
    await _seed_user(r, "b1", "u_fresh", last_visit_days_ago=1, now=now)
    score = await reo.compute_lapse_score(r, "u_fresh", "b1", now=now)
    assert score < 0.1, f"fresh user lapse_score should be low, got {score}"
    assert await reo.select_cascade(r, "u_fresh", "b1", now=now) == ""

    # 7d silence → LIGHT.
    await _seed_user(r, "b1", "u_light", last_visit_days_ago=8, now=now)
    assert await reo.select_cascade(r, "u_light", "b1", now=now) == CascadeType.LIGHT

    # 14d silence + unused voucher → MEDIUM.
    await _seed_user(
        r, "b1", "u_med", last_visit_days_ago=15, unused_vouchers=2, now=now,
    )
    assert await reo.select_cascade(r, "u_med", "b1", now=now) == CascadeType.MEDIUM

    # 30d silence + LTV $25 → HEAVY.
    await _seed_user(
        r, "b1", "u_heavy", last_visit_days_ago=32, ltv=25.0, now=now,
    )
    assert await reo.select_cascade(r, "u_heavy", "b1", now=now) == CascadeType.HEAVY

    # 60d silence → WIN_BACK (overrides HEAVY even with LTV).
    await _seed_user(
        r, "b1", "u_win", last_visit_days_ago=65, ltv=100.0, now=now,
    )
    assert await reo.select_cascade(r, "u_win", "b1", now=now) == CascadeType.WIN_BACK

    # Lapse score saturates near 1 for 30+ day silence.
    saturated = await reo.compute_lapse_score(r, "u_win", "b1", now=now)
    assert saturated >= 0.99


# ── 2. Cascade timing — start + advance ─────────────────────────────────


@pytest.mark.asyncio
async def test_cascade_start_and_advance_steps(clean_redis):
    r = clean_redis
    now = 1_700_000_000.0
    await _seed_user(
        r, "b1", "u_med", last_visit_days_ago=15, unused_vouchers=1,
        has_push=True, has_email=True, now=now,
    )

    started = await reo.start_cascade(
        r, brand_id="b1", user_id="u_med",
        cascade_type=CascadeType.MEDIUM, now=now,
    )
    assert started["status"] == "started"
    assert started["steps_total"] == 3

    # Re-starting the same cascade is a no-op.
    again = await reo.start_cascade(
        r, brand_id="b1", user_id="u_med",
        cascade_type=CascadeType.MEDIUM, now=now,
    )
    assert again["status"] == "already_active"

    # First step is due immediately (offset_days=0).
    r1 = await reo.send_cascade_step(
        r, brand_id="b1", user_id="u_med", now=now,
    )
    assert r1["status"] == "sent"
    assert r1["step_idx"] == 0
    # Channel preference: WhatsApp not enabled → falls back to push.
    assert r1["channel"] == "push"

    # Second step (offset_days=3) is not due yet.
    r2 = await reo.send_cascade_step(
        r, brand_id="b1", user_id="u_med", now=now + 1,
    )
    assert r2["status"] in ("not_due", "suppressed")

    # Jump to D3 (also past freq cap of 7d would block — but freq-cap is
    # 7d, so D3 should still be capped). Verify cap behaviour explicitly:
    r3 = await reo.send_cascade_step(
        r, brand_id="b1", user_id="u_med", now=now + 3 * 86_400,
    )
    assert r3["status"] == "suppressed"
    assert r3["reason"] == "frequency_cap"

    # Past the cap (8d later) → next step fires.
    r4 = await reo.send_cascade_step(
        r, brand_id="b1", user_id="u_med", now=now + 8 * 86_400,
    )
    assert r4["status"] == "sent"
    assert r4["step_idx"] == 1


# ── 3. Channel selection precedence ─────────────────────────────────────


@pytest.mark.asyncio
async def test_channel_selection_prefers_whatsapp_then_push_then_email(clean_redis):
    r = clean_redis
    now = 1_700_000_000.0

    # Has all 3 channels → WhatsApp wins for the light cascade (its
    # blueprint puts WhatsApp first).
    await _seed_user(
        r, "b1", "u_all", last_visit_days_ago=10,
        has_email=True, has_whatsapp=True, has_push=True, now=now,
    )
    ch_all = await reo.recommend_channel(r, "u_all", "b1")
    assert ch_all == "whatsapp"

    # Only push + email → push wins.
    await _seed_user(
        r, "b1", "u_pe", last_visit_days_ago=10,
        has_email=True, has_push=True, now=now,
    )
    assert await reo.recommend_channel(r, "u_pe", "b1") == "push"

    # Only email.
    await _seed_user(
        r, "b1", "u_e", last_visit_days_ago=10, has_email=True, now=now,
    )
    assert await reo.recommend_channel(r, "u_e", "b1") == "email"

    # Unreachable.
    await _seed_user(r, "b1", "u_none", last_visit_days_ago=10, now=now)
    assert await reo.recommend_channel(r, "u_none", "b1") == ""


# ── 4. Frequency cap ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_frequency_cap_blocks_second_send_within_7d(clean_redis):
    r = clean_redis
    now = 1_700_000_000.0
    await r.set(last_send_key("b1", "u1"), str(now - 3 * 86_400))
    assert await reo.frequency_capped(r, "b1", "u1", now=now) is True
    # 8 days later → cap released.
    assert await reo.frequency_capped(r, "b1", "u1", now=now + 8 * 86_400) is False


# ── 5. Quiet hours ───────────────────────────────────────────────────────


def test_quiet_hours_sgt_window():
    # 23:00 SGT = 15:00 UTC → quiet
    t_quiet = datetime(2026, 5, 30, 15, 0, tzinfo=timezone.utc)
    assert reo.in_quiet_hours_sgt(t_quiet) is True
    # 12:00 SGT = 04:00 UTC → not quiet
    t_busy = datetime(2026, 5, 30, 4, 0, tzinfo=timezone.utc)
    assert reo.in_quiet_hours_sgt(t_busy) is False
    # 03:00 SGT = 19:00 UTC (previous day) → quiet
    t_late = datetime(2026, 5, 30, 19, 0, tzinfo=timezone.utc)
    assert reo.in_quiet_hours_sgt(t_late) is True


# ── 6. Opt-out + just-redeemed suppression ──────────────────────────────


@pytest.mark.asyncio
async def test_opt_out_and_just_redeemed_suppress(clean_redis):
    r = clean_redis
    now = 1_700_000_000.0

    # Opt-out wins.
    await r.set(optout_key("b1", "u_opt"), "1")
    suppressed, reason = await reo.is_suppressed(
        r, "b1", "u_opt", now=now,
        now_utc=datetime(2026, 5, 30, 4, 0, tzinfo=timezone.utc),
    )
    assert suppressed is True
    assert reason == "opted_out"

    # Just-redeemed (within last 6h).
    await r.set(f"user:u_red:last_redeem:b1", str(now - 1800))
    suppressed, reason = await reo.is_suppressed(
        r, "b1", "u_red", now=now,
        now_utc=datetime(2026, 5, 30, 4, 0, tzinfo=timezone.utc),
    )
    assert suppressed is True
    assert reason == "just_redeemed"


# ── 7. Audit log entries ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_log_records_start_and_send(clean_redis):
    r = clean_redis
    now = 1_700_000_000.0
    await _seed_user(
        r, "b1", "u_aud", last_visit_days_ago=10, has_email=True, now=now,
    )

    await reo.start_cascade(
        r, brand_id="b1", user_id="u_aud",
        cascade_type=CascadeType.LIGHT, now=now,
    )
    await reo.send_cascade_step(
        r, brand_id="b1", user_id="u_aud", now=now,
    )

    raw = await r.lrange(audit_log_key("b1", "u_aud"), 0, -1)
    events = [json.loads(x if isinstance(x, str) else x.decode()) for x in raw]
    event_names = [e["event"] for e in events]
    assert "cascade_start" in event_names
    assert "send" in event_names


# ── 8. Stats counters ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stats_increment_on_send_and_suppress(clean_redis):
    r = clean_redis
    now = 1_700_000_000.0
    # Use MEDIUM (3 steps) so a second tick still has work to do and
    # can land in the suppressed-by-frequency-cap branch.
    await _seed_user(
        r, "b1", "u_s", last_visit_days_ago=15, unused_vouchers=1,
        has_push=True, now=now,
    )
    await reo.start_cascade(
        r, brand_id="b1", user_id="u_s",
        cascade_type=CascadeType.MEDIUM, now=now,
    )
    await reo.send_cascade_step(r, brand_id="b1", user_id="u_s", now=now)

    stats = await reo.cascade_stats(r, "b1")
    assert stats["stats"].get("started", 0) >= 1
    assert stats["stats"].get("sent", 0) >= 1
    assert stats["stats"].get("sent_push", 0) >= 1

    # Tick again 3 days in — step 2 (offset_days=3) is now due, but the
    # 7-day frequency cap fires → suppressed counter bumps.
    await reo.send_cascade_step(
        r, brand_id="b1", user_id="u_s", now=now + 3 * 86_400 + 1,
    )
    stats2 = await reo.cascade_stats(r, "b1")
    assert stats2["stats"].get("suppressed", 0) >= 1


# ── 9. Endpoint: start-cascade ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_endpoint_start_cascade(client, clean_redis):
    r = clean_redis
    # Seed user so the cascade has something to act on (not required by
    # start-cascade, but mirrors real usage).
    await _seed_user(
        r, "demo_brand", "kid_42", last_visit_days_ago=10,
        has_email=True,
    )

    resp = await client.post(
        "/api/v1/reengagement/demo_brand/start-cascade",
        json={"user_id": "kid_42", "cascade_type": "light"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] in ("started", "already_active")
    assert body["cascade_type"] == "light"


# ── 10. Endpoint: cascade-stats + at-risk-cohort ────────────────────────


@pytest.mark.asyncio
async def test_endpoint_stats_and_at_risk(client, clean_redis):
    r = clean_redis
    now = 1_700_000_000.0
    await _seed_user(
        r, "demo_brand", "kid_99", last_visit_days_ago=10,
        has_push=True, now=now,
    )
    await reo.start_cascade(
        r, brand_id="demo_brand", user_id="kid_99",
        cascade_type=CascadeType.LIGHT, now=now,
    )

    # stats
    resp = await client.get(
        "/api/v1/reengagement/demo_brand/cascade-stats", headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["brand_id"] == "demo_brand"
    assert body["stats"].get("started", 0) >= 1
    assert body["at_risk_count"] >= 1

    # cohort
    resp2 = await client.get(
        "/api/v1/reengagement/demo_brand/at-risk-cohort", headers=ADMIN_HEADERS,
    )
    assert resp2.status_code == 200
    cohort = resp2.json()
    assert cohort["count"] >= 1
    assert any(row["user_id"] == "kid_99" for row in cohort["cohort"])


# ── 11. Admin: test-cascade fast-forwards through all steps ─────────────


@pytest.mark.asyncio
async def test_admin_test_cascade_fast_forward(client, clean_redis):
    r = clean_redis
    await _seed_user(
        r, "demo_brand", "kid_777", last_visit_days_ago=20,
        unused_vouchers=2, has_push=True, has_email=True,
    )
    resp = await client.post(
        "/api/v1/admin/reengagement/test-cascade",
        json={
            "admin_token": ADMIN_TOKEN,
            "brand_id": "demo_brand",
            "user_id": "kid_777",
            "cascade_type": "medium",
            "fast_forward": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cascade_started"]["status"] in ("started", "already_active")
    # With fast_forward, sent_count should walk multiple steps. The
    # exact number depends on suppression — at minimum the first step.
    assert body["cascade_sent"]["sent_count"] >= 1


# ── 12. Worker run_once + evaluate_users ───────────────────────────────


@pytest.mark.asyncio
async def test_worker_run_once_opens_cascades_for_lapsed_users(clean_redis):
    r = clean_redis
    now = 1_700_000_000.0

    # Two lapsed users + one fresh user.
    await _seed_user(r, "bX", "lapsed_1", last_visit_days_ago=10, has_push=True, now=now)
    await _seed_user(r, "bX", "lapsed_2", last_visit_days_ago=20, unused_vouchers=1, has_email=True, now=now)
    await _seed_user(r, "bX", "fresh", last_visit_days_ago=1, has_push=True, now=now)

    # Run a quiet-time scan so we can assert "started without suppression"
    # — we use a non-quiet UTC hour just for the orchestrator's own logic.
    # The worker doesn't decide quiet hours itself; that's per-send.
    report = await worker.run_once(r, brand_ids=["bX"], now=now)
    assert report["brands_processed"] == 1
    started_uids = {row["user_id"] for row in report["reports"][0]["started"]}
    assert "lapsed_1" in started_uids
    assert "lapsed_2" in started_uids
    assert "fresh" not in started_uids

    # Cascade types selected appropriately.
    cascades = {row["user_id"]: row.get("cascade_type") for row in report["reports"][0]["started"]}
    assert cascades["lapsed_1"] == CascadeType.LIGHT
    assert cascades["lapsed_2"] == CascadeType.MEDIUM


# ── 13. TriSoul-personalised craft_message tone ────────────────────────


@pytest.mark.asyncio
async def test_craft_message_uses_trisoul_tone_when_available(
    clean_redis, monkeypatch,
):
    r = clean_redis

    async def _fake_features(uid, redis):
        return {"urgency": 0.9, "social": 0.1}

    # Patch the optional TriSoul fetch.
    monkeypatch.setattr(reo, "_trisoul_features", lambda redis, uid: _fake_features(uid, redis))

    msg = await reo.craft_message(
        r, "kid_x", "b1", lapse_days=15,
        cascade_type=CascadeType.MEDIUM, step_idx=0,
        brand_name="Tea House",
    )
    assert msg["tone"] == "urgent"
    assert msg["subject"].startswith("[Today only] ")
    assert "Tea House" in msg["subject"]
    assert msg["offer_pct"] == 10
    assert msg["personalised"] is True

    # Without TriSoul features → defaults to "warm" tone, no prefix.
    async def _empty(uid, redis):
        return {}

    monkeypatch.setattr(reo, "_trisoul_features", lambda redis, uid: _empty(uid, redis))
    msg2 = await reo.craft_message(
        r, "kid_y", "b1", lapse_days=15,
        cascade_type=CascadeType.MEDIUM, step_idx=0,
        brand_name="Tea House",
    )
    assert msg2["tone"] == "warm"
    assert not msg2["subject"].startswith("[")
    assert msg2["personalised"] is False
