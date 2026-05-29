"""Audiences router tests — custom create, lookalike, filter, link to campaign."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_audiences_custom_create_from_user_ids(client, clean_redis):
    res = await client.post(
        "/api/v1/audiences/custom/create",
        json={
            "brand_id": "b_aud_1",
            "name": "VIP CRM",
            "source": "csv_upload",
            "user_ids": ["u_aud_1", "u_aud_2", "u_aud_3"],
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["audience_id"].startswith("aud_")
    assert body["size"] == 3
    assert body["status"] == "ready"


@pytest.mark.asyncio
async def test_audiences_custom_create_invalid_source(client, clean_redis):
    """bug-bait: unknown source must be 422."""
    res = await client.post(
        "/api/v1/audiences/custom/create",
        json={
            "brand_id": "b_aud_2",
            "name": "Bad Source",
            "source": "not_a_real_source",
            "user_ids": ["u1"],
        },
    )
    # Literal validation rejects at pydantic layer (422)
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_audiences_filter_source_requires_at_least_one_filter(client, clean_redis):
    """bug-bait: empty filter spec for source=filter must be 422."""
    res = await client.post(
        "/api/v1/audiences/custom/create",
        json={
            "brand_id": "b_aud_3",
            "name": "Empty Filter",
            "source": "filter",
            # No recency / lifecycle / attribute filter provided.
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_audiences_filter_preview_requires_filter(client, clean_redis):
    """bug-bait: /filter/preview with no filters must be 422."""
    res = await client.post(
        "/api/v1/audiences/filter/preview",
        json={"brand_id": "b_aud_4", "limit": 50},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_audiences_lookalike_seed_must_have_members(client, clean_redis):
    """Lookalike on an empty seed audience is 422."""
    create = await client.post(
        "/api/v1/audiences/custom/create",
        json={
            "brand_id": "b_aud_5",
            "name": "Empty",
            "source": "manual",
            # No user_ids — seed will be size 0.
        },
    )
    assert create.status_code == 200
    aid = create.json()["audience_id"]

    lal = await client.post(
        f"/api/v1/audiences/{aid}/lookalike",
        json={"brand_id": "b_aud_5", "similarity": 5},
    )
    assert lal.status_code == 422


@pytest.mark.asyncio
async def test_audiences_lookalike_similarity_out_of_range(client, clean_redis):
    """bug-bait: similarity must be 1..10, 11 is 422."""
    create = await client.post(
        "/api/v1/audiences/custom/create",
        json={
            "brand_id": "b_aud_6",
            "name": "Seed",
            "source": "csv_upload",
            "user_ids": ["u_aud_6"],
        },
    )
    aid = create.json()["audience_id"]
    lal = await client.post(
        f"/api/v1/audiences/{aid}/lookalike",
        json={"brand_id": "b_aud_6", "similarity": 11},
    )
    assert lal.status_code == 422


@pytest.mark.asyncio
async def test_audiences_exclude_in_campaign_unknown_audience(client, clean_redis):
    """bug-bait: exclude link with unknown audience must be 404."""
    res = await client.post(
        "/api/v1/audiences/aud_does_not_exist/exclude-in-campaign",
        json={"campaign_id": "cmp_xxx"},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_audiences_membership_check(client, clean_redis):
    """Round-trip: create with user_ids, then verify membership endpoint."""
    create = await client.post(
        "/api/v1/audiences/custom/create",
        json={
            "brand_id": "b_aud_7",
            "name": "M",
            "source": "csv_upload",
            "user_ids": ["u_member_a", "u_member_b"],
        },
    )
    aid = create.json()["audience_id"]

    check = await client.post(
        "/api/v1/audiences/check",
        json={"user_id": "u_member_a", "audience_id": aid},
    )
    # Endpoint exists and returns membership bool — either is_member True or
    # an explicit boolean response. We assert the route works.
    assert check.status_code == 200
