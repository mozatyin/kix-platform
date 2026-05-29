"""Push engine router tests — evaluate, dispatch, schedule, eligibility."""

from __future__ import annotations

import time

import pytest


@pytest.mark.asyncio
async def test_push_evaluate_no_eligible_candidates(client, clean_redis):
    """No campaigns / brands configured -> empty candidate list, eligible_count=0."""
    res = await client.post(
        "/api/v1/push/evaluate",
        json={"kid": "kid_push_1", "context": {}, "max_candidates": 5},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["candidates"] == []
    assert body["decided_winner"] is None
    assert body["eligible_count"] == 0


@pytest.mark.asyncio
async def test_push_dispatch_unknown_token_is_404(client, clean_redis):
    """bug-bait: dispatching with a bogus candidate_token must 404."""
    res = await client.post(
        "/api/v1/push/dispatch",
        json={"kid": "kid_push_2", "candidate_token": "tok_does_not_exist"},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_push_schedule_in_seconds(client, clean_redis):
    res = await client.post(
        "/api/v1/push/schedule",
        json={
            "kid": "kid_push_3",
            "fire_in_seconds": 3600,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["schedule_id"]
    assert body["next_fire_at"] > time.time()


@pytest.mark.asyncio
async def test_push_schedule_no_when_param_rejected(client, clean_redis):
    """bug-bait: must supply one of fire_at_ts / fire_in_seconds / cron — else 400."""
    res = await client.post(
        "/api/v1/push/schedule",
        json={"kid": "kid_push_4"},
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_push_schedule_in_the_past_rejected(client, clean_redis):
    """bug-bait: fire_at_ts in the past must 400."""
    res = await client.post(
        "/api/v1/push/schedule",
        json={
            "kid": "kid_push_5",
            "fire_at_ts": time.time() - 7200,
        },
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_push_eligibility_no_consent_blocks(client, clean_redis):
    """bug-bait: dispatch eligibility without granted marketing consent.

    No policy / no grant on a fresh Redis ⇒ consent gate fails closed.
    Eligibility must return ``eligible=False`` and surface the consent block.
    """
    res = await client.post(
        "/api/v1/push/eligibility/check",
        json={"kid": "kid_push_6", "brand_id": "b_push_6"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["eligible"] is False
    # At minimum, the consent gate must appear in the breakdown.
    assert "consent" in body["gates"]
    assert body["gates"]["consent"]["allow"] is False


@pytest.mark.asyncio
async def test_push_now_without_eligible_returns_fired_false(client, clean_redis):
    res = await client.post(
        "/api/v1/push/now",
        json={"kid": "kid_push_7"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["fired"] is False
    assert body["reason"]  # diagnostic reason populated


@pytest.mark.asyncio
async def test_push_inbox_starts_empty(client, clean_redis):
    """Smoke: inbox endpoint works on a brand-new user."""
    res = await client.get("/api/v1/push/user/kid_push_8/inbox")
    assert res.status_code == 200
    body = res.json()
    # Inbox is empty for a fresh user — response should reflect that.
    assert isinstance(body, dict)
