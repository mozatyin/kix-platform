"""Tests for the Alpha-Merchant programme tooling.

Covers the public + admin routes in ``app.routers.alpha_program``, the four
``alpha_*`` email templates registered via ``app.email_templates.alpha``,
and the ``app.workers.alpha_cohort_worker`` automated-touch logic.

The suite uses the standard ``client`` + ``clean_redis`` fixtures from
``tests/conftest.py``. Admin endpoints require ``KIX_ADMIN_TOKEN`` — we
match the default ``admin-dev-token`` baked into the router so the tests
run without env-var setup, mirroring the ``test_email_templates.py``
convention.
"""

from __future__ import annotations

import os
import time

import pytest

from app.email_templates import EMAIL_TEMPLATES  # side-effect-registered
from app.routers.alpha_program import (
    DEFAULT_COHORT,
    FEEDBACK_CATEGORIES,
    _normalize_code,
    in_quiet_hours,
)
from app.services.email_template_service import email_queue_key, render_email
from app.workers.alpha_cohort_worker import _touch_key, run_once

ADMIN_TOKEN = os.getenv("KIX_ADMIN_TOKEN", "admin-dev-token")
ADMIN_HEADERS = {"X-Admin-Token": ADMIN_TOKEN}


# ── helpers ──────────────────────────────────────────────────────────────


async def _mint_invite(client, **overrides) -> dict:
    body = {
        "admin_token": ADMIN_TOKEN,
        "merchant_email": "owner@example.sg",
        "merchant_name": "Tea House",
        "store_count": 2,
        "notes": "Referred by Jane",
    }
    body.update(overrides)
    r = await client.post("/api/v1/alpha/invite", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _signup(client, code: str, brand_id: str = "alpha_tea") -> dict:
    r = await client.post(
        f"/api/v1/alpha/signup/{code}",
        json={
            "brand_id": brand_id,
            "brand_name": "Tea House SG",
            "contact_name": "Jane Tan",
            "locale": "en-SG",
        },
    )
    return r.json() if r.status_code == 200 else {"_status": r.status_code, "_body": r.text}


# ── 1. Invite creation ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invite_creation_returns_code_and_url(client, clean_redis):
    out = await _mint_invite(client)
    assert out["invite_code"].startswith("KIX-")
    # 8 alphanumeric chars + 2 dashes + "KIX-" prefix → total 13
    assert len(out["invite_code"]) == 13
    assert out["signup_url"].startswith("/landing/alpha.html?code=")
    assert out["cohort"] == DEFAULT_COHORT


# ── 2. Signup with valid code ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_signup_with_valid_code(client, clean_redis):
    inv = await _mint_invite(client)
    res = await _signup(client, inv["invite_code"], brand_id="alpha_brand_a")
    assert "_status" not in res, res
    assert res["brand_id"] == "alpha_brand_a"
    assert res["cohort"] == DEFAULT_COHORT
    assert res["welcome_email_queued"] is True
    # ~90 days in the future
    assert "T" in res["trial_ends_at"]


# ── 3. Signup with invalid code ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_signup_with_invalid_code(client, clean_redis):
    r = await client.post(
        "/api/v1/alpha/signup/KIX-NOPE-NOPE",
        json={
            "brand_id": "alpha_brand_x",
            "brand_name": "X",
            "contact_name": "Y",
        },
    )
    assert r.status_code == 404
    assert "invite_code_unknown" in r.text


# ── 4. Cohort listing ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cohort_listing_admin_returns_members(client, clean_redis):
    inv = await _mint_invite(client)
    await _signup(client, inv["invite_code"], brand_id="alpha_cohort_a")
    inv2 = await _mint_invite(client, merchant_email="b@example.sg")
    await _signup(client, inv2["invite_code"], brand_id="alpha_cohort_b")

    r = await client.get("/api/v1/alpha/cohort", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    data = r.json()
    bids = {m["brand_id"] for m in data["members"]}
    assert "alpha_cohort_a" in bids and "alpha_cohort_b" in bids
    assert data["size"] >= 2


# ── 5. Feedback submission (various categories) ──────────────────────────


@pytest.mark.asyncio
async def test_feedback_submit_all_categories(client, clean_redis):
    for cat in sorted(FEEDBACK_CATEGORIES):
        r = await client.post(
            "/api/v1/alpha/feedback/submit",
            json={
                "brand_id": "alpha_test",
                "category": cat,
                "rating": 4,
                "comment": f"category-{cat}",
                "page_context": "/portal/dashboard",
                "recent_actions": ["click:nav", "click:create_campaign"],
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["feedback_id"].startswith("fb_")

    # Invalid category → 422
    r = await client.post(
        "/api/v1/alpha/feedback/submit",
        json={"brand_id": "x", "category": "zzz", "rating": 3},
    )
    assert r.status_code == 422


# ── 6. Health check identifies at-risk ───────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_flags_no_campaign_after_grace(client, clean_redis):
    inv = await _mint_invite(client)
    await _signup(client, inv["invite_code"], brand_id="alpha_atrisk")

    # Backdate signup 10 days ago — past the no-campaign 5-day grace
    past = time.time() - 10 * 86_400
    await clean_redis.hset(
        "brand_subscription:alpha_atrisk",
        mapping={"started_at": str(past)},
    )

    r = await client.get(
        "/api/v1/alpha/health-check/alpha_atrisk",
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["at_risk"] is True
    assert "no_campaign" in data["risk_reasons"]


# ── 7. Auto-email triggers fire ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_worker_enqueues_day3_for_eligible_brand(client, clean_redis):
    inv = await _mint_invite(client)
    await _signup(client, inv["invite_code"], brand_id="alpha_day3")

    # Backdate signup 4 days ago so day-3 is due
    past = time.time() - 4 * 86_400
    await clean_redis.hset(
        "brand_subscription:alpha_day3", mapping={"started_at": str(past)}
    )

    # Force "now" outside quiet hours by setting it to UTC 04:00 → SGT 12:00.
    import datetime as _dt
    now_ts = _dt.datetime(2030, 1, 1, 4, 0, 0, tzinfo=_dt.timezone.utc).timestamp()

    report = await run_once(clean_redis, now=now_ts)
    enqueued_brands = {
        a["brand_id"]
        for a in report["actions"]
        if a["touch"] == "alpha_day3_checkin" and a["status"] == "enqueued"
    }
    assert "alpha_day3" in enqueued_brands

    # Touch key set → re-running does not re-enqueue
    assert await clean_redis.exists(_touch_key("alpha_day3", "alpha_day3_checkin"))
    report2 = await run_once(clean_redis, now=now_ts)
    enq2 = [
        a for a in report2["actions"]
        if a["brand_id"] == "alpha_day3" and a["touch"] == "alpha_day3_checkin"
    ]
    assert enq2 == [], "day-3 should fire exactly once"


# ── 8. 90-day free trial applied ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_signup_grants_starter_trial(client, clean_redis):
    inv = await _mint_invite(client, trial_days=90)
    res = await _signup(client, inv["invite_code"], brand_id="alpha_starter")
    assert "_status" not in res
    sub = await clean_redis.hgetall("brand_subscription:alpha_starter")
    sub_str = {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in sub.items()
    }
    assert sub_str["tier"] == "starter"
    assert sub_str["first_year_free"] == "true"
    # expires_at ~ started_at + 90 days (allow 1-hour drift)
    started = float(sub_str["started_at"])
    expires = float(sub_str["expires_at"])
    assert abs((expires - started) - 90 * 86_400) < 3_600


# ── 9. Idempotent signup (re-using code blocked) ─────────────────────────


@pytest.mark.asyncio
async def test_signup_code_can_only_be_redeemed_once(client, clean_redis):
    inv = await _mint_invite(client)
    res = await _signup(client, inv["invite_code"], brand_id="alpha_once")
    assert "_status" not in res

    r = await client.post(
        f"/api/v1/alpha/signup/{inv['invite_code']}",
        json={
            "brand_id": "alpha_other",
            "brand_name": "Other",
            "contact_name": "Z",
        },
    )
    assert r.status_code == 409
    assert "invite_already_redeemed" in r.text


# ── 10. Cohort tag stored ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cohort_tag_persisted_on_signup(client, clean_redis):
    inv = await _mint_invite(client, cohort="2026q2")
    await _signup(client, inv["invite_code"], brand_id="alpha_q2")

    tag = await clean_redis.get("brand:alpha_q2:alpha_cohort")
    tag_str = tag.decode() if isinstance(tag, bytes) else tag
    assert tag_str == "2026q2"

    members = await clean_redis.smembers("alpha:cohort:2026q2")
    members_str = {
        (m.decode() if isinstance(m, bytes) else m) for m in members
    }
    assert "alpha_q2" in members_str


# ── 11. Admin endpoints require admin token ──────────────────────────────


@pytest.mark.asyncio
async def test_admin_endpoints_reject_missing_token(client, clean_redis):
    r = await client.get("/api/v1/alpha/cohort")
    assert r.status_code == 403

    r = await client.get("/api/v1/alpha/health-check/whatever")
    assert r.status_code == 403

    r = await client.get("/api/v1/alpha/feedback/list")
    assert r.status_code == 403

    # Wrong token still 403
    r = await client.get(
        "/api/v1/alpha/cohort", headers={"X-Admin-Token": "wrong"}
    )
    assert r.status_code == 403


# ── 12. Health metrics calculated correctly ──────────────────────────────


@pytest.mark.asyncio
async def test_health_metrics_aggregate_from_redis(client, clean_redis):
    inv = await _mint_invite(client)
    await _signup(client, inv["invite_code"], brand_id="alpha_metrics")

    # Seed canonical metrics keys used by reporting / wallet
    await clean_redis.set("brand:alpha_metrics:campaigns:count", "3")
    await clean_redis.set("brand:alpha_metrics:spend:total_cents", "12_345".replace("_", ""))
    await clean_redis.set("brand:alpha_metrics:last_login", str(time.time()))

    r = await client.get(
        "/api/v1/alpha/health-check/alpha_metrics", headers=ADMIN_HEADERS
    )
    assert r.status_code == 200
    data = r.json()
    assert data["campaigns_created"] == 3
    assert data["spend_total_cents"] == 12345
    assert data["spend_total_sgd"] == round(12345 / 100, 2)
    assert data["at_risk"] is False


# ── 13. Quiet hours respected for auto-emails ────────────────────────────


@pytest.mark.asyncio
async def test_worker_defers_during_quiet_hours(client, clean_redis):
    inv = await _mint_invite(client)
    await _signup(client, inv["invite_code"], brand_id="alpha_quiet")
    past = time.time() - 4 * 86_400
    await clean_redis.hset(
        "brand_subscription:alpha_quiet", mapping={"started_at": str(past)}
    )

    # UTC 17:00 → SGT 01:00 (inside the 22:00–08:00 quiet window)
    import datetime as _dt
    quiet_ts = _dt.datetime(2030, 1, 1, 17, 0, 0, tzinfo=_dt.timezone.utc).timestamp()
    assert in_quiet_hours(_dt.datetime.fromtimestamp(quiet_ts, _dt.timezone.utc))

    report = await run_once(clean_redis, now=quiet_ts)
    assert report["quiet_hours"] is True
    deferred = [
        a for a in report["actions"]
        if a["brand_id"] == "alpha_quiet" and a["status"] == "deferred_quiet_hours"
    ]
    assert deferred, "day-3 touch should be deferred during quiet hours"
    # No touch key written
    assert not await clean_redis.exists(
        _touch_key("alpha_quiet", "alpha_day3_checkin")
    )


# ── 14. Worker dry-run mode ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_worker_dry_run_does_not_enqueue(client, clean_redis):
    inv = await _mint_invite(client)
    await _signup(client, inv["invite_code"], brand_id="alpha_dry")
    past = time.time() - 4 * 86_400
    await clean_redis.hset(
        "brand_subscription:alpha_dry", mapping={"started_at": str(past)}
    )

    import datetime as _dt
    now_ts = _dt.datetime(2030, 1, 1, 4, 0, 0, tzinfo=_dt.timezone.utc).timestamp()

    # Snapshot existing email queue length
    before = await clean_redis.llen(email_queue_key("alpha_dry"))
    report = await run_once(clean_redis, now=now_ts, dry_run=True)
    after = await clean_redis.llen(email_queue_key("alpha_dry"))

    assert report["dry_run"] is True
    assert after == before, "dry-run must not push onto the email queue"
    would = [
        a for a in report["actions"]
        if a["brand_id"] == "alpha_dry" and a["status"] == "would_enqueue"
    ]
    assert would, "dry-run should still report intended actions"
    # Touch key NOT written
    assert not await clean_redis.exists(
        _touch_key("alpha_dry", "alpha_day3_checkin")
    )


# ── 15. Feedback page captures context (browser, screen_size, actions) ──


@pytest.mark.asyncio
async def test_feedback_persists_context_fields(client, clean_redis):
    payload = {
        "brand_id": "alpha_ctx",
        "category": "bug",
        "rating": 2,
        "comment": "Filters reset when navigating away",
        "page_context": "/portal/audiences",
        "browser": "Mozilla/5.0 SamplerUA",
        "screen_size": "1440x900",
        "recent_actions": ["click:nav", "click:filter", "submit:form"],
    }
    r = await client.post("/api/v1/alpha/feedback/submit", json=payload)
    assert r.status_code == 201
    fid = r.json()["feedback_id"]

    rec = await clean_redis.hgetall(f"alpha:feedback:{fid}")
    rec_s = {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in rec.items()
    }
    assert rec_s["browser"].startswith("Mozilla/5.0")
    assert rec_s["screen_size"] == "1440x900"
    assert "filter" in rec_s["recent_actions"]
    assert rec_s["page_context"] == "/portal/audiences"

    # And it shows up in the admin list
    r = await client.get(
        "/api/v1/alpha/feedback/list", headers=ADMIN_HEADERS
    )
    assert r.status_code == 200
    ids = {item["feedback_id"] for item in r.json()["items"]}
    assert fid in ids


# ── Bonus: smoke-test the 4 alpha email templates render in both locales ─


@pytest.mark.asyncio
async def test_alpha_email_templates_registered_and_render():
    expected = {
        "alpha_welcome",
        "alpha_day3_checkin",
        "alpha_week1_summary",
        "alpha_monthly_survey",
    }
    assert expected.issubset(EMAIL_TEMPLATES.keys())

    out = render_email(
        "alpha_welcome",
        "en-SG",
        brand_name="Tea House",
        contact_name="Jane",
        trial_days="90",
        portal_url="https://partner.letskix.com/x",
    )
    assert "Tea House" in out["subject"] or "Tea House" in out["body_text"]
    assert "Jane" in out["body_text"]
    assert "90" in out["body_text"]

    out_zh = render_email(
        "alpha_day3_checkin",
        "zh-Hans-SG",
        brand_name="老茶馆",
        contact_name="李华",
        feedback_url="/landing/alpha-feedback.html",
    )
    assert "李华" in out_zh["body_text"]


# ── Bonus: invite-code normalization ─────────────────────────────────────


def test_invite_code_normalization_handles_mixed_case_and_dashes():
    assert _normalize_code("kix-a7m9-pnq2") == "KIXA7M9PNQ2"
    assert _normalize_code("KIX-A7M9-PNQ2") == "KIXA7M9PNQ2"
    assert _normalize_code(" kix a7m9  pnq2 ") == "KIXA7M9PNQ2"
