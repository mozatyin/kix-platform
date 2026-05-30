"""Commerce loop router — coupons, energy packs, rewards, upsell, store."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_configure_coupons_happy(client, clean_redis):
    res = await client.post(
        "/api/v1/commerce/coupons/configure",
        json={
            "brand_id": "brand_c1",
            "tiers": [
                {
                    "name": "bronze",
                    "min_score": 100,
                    "discount_type": "percent",
                    "discount_value": 10,
                },
            ],
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["tiers_count"] == 1


@pytest.mark.asyncio
async def test_configure_coupons_missing_brand_422(client, clean_redis):
    res = await client.post(
        "/api/v1/commerce/coupons/configure",
        json={"tiers": []},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_claim_coupon_no_tier_400(client, clean_redis):
    res = await client.post(
        "/api/v1/commerce/coupons/claim",
        json={
            "user_id": "u1",
            "brand_id": "brand_no_tier",
            "score": 100,
            "game_slug": "tic_tac_toe",
            "session_id": "sess_1",
        },
    )
    # No tiers configured → 400 (or 403 from conditions gate)
    assert res.status_code in (400, 403)


@pytest.mark.asyncio
async def test_list_coupons_empty_user(client, clean_redis):
    res = await client.get(
        "/api/v1/commerce/coupons/u_empty",
        params={"brand_id": "any"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 0


@pytest.mark.asyncio
async def test_configure_energy_packs(client, clean_redis):
    res = await client.post(
        "/api/v1/commerce/energy/configure",
        json={
            "brand_id": "b_e1",
            "packs": [
                {"id": "pack_a", "energy": 5, "price_cents": 100, "currency": "USD"},
            ],
        },
    )
    assert res.status_code == 200, res.text


@pytest.mark.asyncio
async def test_configure_upsell(client, clean_redis):
    res = await client.post(
        "/api/v1/commerce/upsell/configure",
        json={
            "brand_id": "b_up",
            "rules": [
                {
                    "when_amount_below": 100,
                    "upgrade_text": "buy 2 more",
                    "upgrade_price": 50,
                },
            ],
        },
    )
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_configure_store(client, clean_redis):
    res = await client.post(
        "/api/v1/commerce/store/configure",
        json={
            "brand_id": "b_store",
            "items": [
                {"id": "i1", "name": "T-shirt", "point_cost": 100, "stock": 10},
            ],
        },
    )
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_get_store_after_configure(client, clean_redis):
    await client.post(
        "/api/v1/commerce/store/configure",
        json={
            "brand_id": "b_s2",
            "items": [
                {"id": "i1", "name": "Hat", "point_cost": 50, "stock": 5},
            ],
        },
    )
    res = await client.get("/api/v1/commerce/store/b_s2")
    assert res.status_code == 200
