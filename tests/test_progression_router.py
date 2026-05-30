"""Progression router — XP, badges, streak, check-in (smoke + key paths)."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_create_badge(client, clean_redis):
    res = await client.post(
        "/api/v1/progression/brand/b1/badges",
        json={"id": "badge_first", "name": "First Win", "xp_reward": 10},
    )
    assert res.status_code == 200
    assert res.json()["badge_id"] == "badge_first"


@pytest.mark.asyncio
async def test_list_badges_empty(client, clean_redis):
    res = await client.get("/api/v1/progression/brand/b_empty/badges")
    assert res.status_code == 200
    assert res.json() == []


@pytest.mark.asyncio
async def test_award_xp_happy(client, clean_redis):
    res = await client.post(
        "/api/v1/progression/award/xp",
        json={"user_id": "u1", "brand_id": "b1", "amount": 50},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["xp_awarded"] == 50
    assert body["total_xp"] == 50


@pytest.mark.asyncio
async def test_award_xp_zero_amount_400(client, clean_redis):
    res = await client.post(
        "/api/v1/progression/award/xp",
        json={"user_id": "u1", "brand_id": "b1", "amount": 0},
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_award_badge_404_unknown(client, clean_redis):
    res = await client.post(
        "/api/v1/progression/award/badge",
        json={"user_id": "u1", "brand_id": "b1", "badge_id": "no_such_badge"},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_get_progression(client, clean_redis):
    res = await client.get(
        "/api/v1/progression/user/u1/progression",
        params={"brand_id": "b1"},
    )
    assert res.status_code == 200
    assert res.json()["user_id"] == "u1"
