"""Primitives router tests — XP grant, tier resolution, achievement progress."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_primitives_xp_grant_basic(client, clean_redis):
    res = await client.post(
        "/api/v1/primitives/currency/xp/grant",
        json={"user_id": "u_prim_1", "brand_id": "b_prim_1", "amount": 100},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["balance"] == 100
    assert body["currency"] == "xp"


@pytest.mark.asyncio
async def test_primitives_xp_accumulates_across_grants(client, clean_redis):
    payload = {"user_id": "u_prim_2", "brand_id": "b_prim_2", "amount": 50}
    r1 = await client.post("/api/v1/primitives/currency/xp/grant", json=payload)
    r2 = await client.post("/api/v1/primitives/currency/xp/grant", json=payload)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["balance"] == 50
    assert r2.json()["balance"] == 100


@pytest.mark.asyncio
async def test_primitives_xp_grant_negative_rejected(client, clean_redis):
    """bug-bait: negative XP must be 422 (amount has Field(gt=0))."""
    res = await client.post(
        "/api/v1/primitives/currency/xp/grant",
        json={"user_id": "u_prim_3", "brand_id": "b_prim_3", "amount": -100},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_primitives_xp_grant_zero_rejected(client, clean_redis):
    """bug-bait: amount=0 must also be 422 (gt=0)."""
    res = await client.post(
        "/api/v1/primitives/currency/xp/grant",
        json={"user_id": "u_prim_4", "brand_id": "b_prim_4", "amount": 0},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_primitives_tier_uses_default_ladder(client, clean_redis):
    # Grant 150 XP -> silver (threshold 100) on the default ladder.
    await client.post(
        "/api/v1/primitives/currency/xp/grant",
        json={"user_id": "u_prim_5", "brand_id": "b_prim_5", "amount": 150},
    )
    res = await client.get(
        "/api/v1/primitives/user/u_prim_5/tier?brand_id=b_prim_5"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["xp"] == 150
    assert body["tier"] == "silver"
    # Next tier (gold) lives at 1000 XP on the default ladder.
    assert body["next_tier_threshold"] == 1000


@pytest.mark.asyncio
async def test_primitives_tier_configure_then_resolve(client, clean_redis):
    cfg = await client.post(
        "/api/v1/primitives/tier/configure",
        json={
            "brand_id": "b_prim_6",
            "tiers": [
                {"name": "bronze", "xp_min": 0, "perks": []},
                {"name": "silver", "xp_min": 200, "perks": []},
                {"name": "gold", "xp_min": 500, "perks": []},
            ],
        },
    )
    assert cfg.status_code == 200, cfg.text

    await client.post(
        "/api/v1/primitives/currency/xp/grant",
        json={"user_id": "u_prim_6", "brand_id": "b_prim_6", "amount": 250},
    )
    res = await client.get(
        "/api/v1/primitives/user/u_prim_6/tier?brand_id=b_prim_6"
    )
    assert res.status_code == 200
    assert res.json()["tier"] == "silver"


@pytest.mark.asyncio
async def test_primitives_tier_configure_empty_rejected(client, clean_redis):
    """bug-bait: empty tier list must be 422."""
    res = await client.post(
        "/api/v1/primitives/tier/configure",
        json={"brand_id": "b_prim_7", "tiers": []},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_primitives_achievement_progress_unknown_id_is_404(client, clean_redis):
    """bug-bait: progressing an unknown achievement must be 404, not 500."""
    res = await client.post(
        "/api/v1/primitives/achievement/ach_nope_xxx/progress",
        json={"user_id": "u_prim_8", "increment": 1},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_primitives_achievement_progress_completion(client, clean_redis):
    create = await client.post(
        "/api/v1/primitives/brand/b_prim_9/achievements",
        json={
            "id": "ach_first_win",
            "name": "First Win",
            "description": "win a game",
            "target_metric": "wins",
            "target_value": 2,
            "xp_reward": 25,
        },
    )
    assert create.status_code == 200, create.text

    # First increment — not yet complete
    r1 = await client.post(
        "/api/v1/primitives/achievement/ach_first_win/progress",
        json={"user_id": "u_prim_9", "increment": 1},
    )
    assert r1.status_code == 200
    assert r1.json()["completed"] is False

    # Second increment — completes + awards XP
    r2 = await client.post(
        "/api/v1/primitives/achievement/ach_first_win/progress",
        json={"user_id": "u_prim_9", "increment": 1},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["completed"] is True
    assert body["newly_completed"] is True
    assert body["xp_awarded"] == 25
