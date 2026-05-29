"""Creative-gen router tests — request, A/B test create + record."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_creative_request_returns_queued(client, clean_redis):
    """A creative_gen request returns 202 — either queued for build or
    pending human moderation review (moderation LLM unavailable in tests).
    """
    res = await client.post(
        "/api/v1/creative-gen/request",
        json={
            "brand_id": "b_cg_1",
            "name": "Summer Promo",
            "spec": {
                "game_type": "match3",
                "brand_description": "A friendly neighborhood cafe in Singapore",
                "brand_color": "#FF6B35",
                "goal": "engagement",
                "reward": "voucher",
                "duration_seconds": 60,
            },
        },
    )
    assert res.status_code == 202, res.text
    body = res.json()
    # Two valid 202 shapes:
    #   1) {"creative_id": "crv_...", "status": "queued", ...}
    #   2) {"detail": {"status": "pending_review", ...}} when moderation
    #      side-channel defers to human review.
    if "creative_id" in body:
        assert body["creative_id"].startswith("crv_")
        assert body["status"] == "queued"
    else:
        assert body.get("detail", {}).get("status") == "pending_review"


@pytest.mark.asyncio
async def test_creative_request_invalid_brand_color_rejected(client, clean_redis):
    """bug-bait: brand_color must match #RRGGBB pattern."""
    res = await client.post(
        "/api/v1/creative-gen/request",
        json={
            "brand_id": "b_cg_2",
            "name": "X",
            "spec": {
                "game_type": "match3",
                "brand_description": "abc",
                "brand_color": "not-a-hex",
            },
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_creative_request_unknown_game_type_rejected(client, clean_redis):
    """bug-bait: game_type outside the Literal whitelist is 422."""
    res = await client.post(
        "/api/v1/creative-gen/request",
        json={
            "brand_id": "b_cg_3",
            "name": "X",
            "spec": {
                "game_type": "platformer",  # not in the Literal
                "brand_description": "abc",
            },
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_creative_request_duration_out_of_range(client, clean_redis):
    """bug-bait: duration_seconds must be 10..600."""
    res = await client.post(
        "/api/v1/creative-gen/request",
        json={
            "brand_id": "b_cg_4",
            "name": "X",
            "spec": {
                "game_type": "match3",
                "brand_description": "abc",
                "duration_seconds": 9999,
            },
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_creative_status_unknown_id_is_404(client, clean_redis):
    """bug-bait: get status on a non-existent creative_id must 404."""
    res = await client.get("/api/v1/creative-gen/crv_does_not_exist")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_creative_ab_test_requires_two_creatives(client, clean_redis):
    """bug-bait: AB test with only one creative_id violates min_length=2 → 422."""
    res = await client.post(
        "/api/v1/creative-gen/ab-test/create",
        json={
            "campaign_id": "cmp_cg_1",
            "creative_ids": ["crv_solo"],
            "traffic_split": [1.0],
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_creative_ab_test_unknown_creative_is_404(client, clean_redis):
    """AB test referencing creatives that don't exist must 404, never silently create."""
    res = await client.post(
        "/api/v1/creative-gen/ab-test/create",
        json={
            "campaign_id": "cmp_cg_2",
            "creative_ids": ["crv_missing_a", "crv_missing_b"],
            "traffic_split": [0.5, 0.5],
        },
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_creative_ab_test_record_unknown_test_is_404(client, clean_redis):
    """bug-bait: recording an event against an unknown ab_test_id must 404."""
    res = await client.post(
        "/api/v1/creative-gen/ab-test/ab_does_not_exist/record",
        json={"creative_id": "crv_x", "event": "impression"},
    )
    assert res.status_code == 404
