"""Pricing router — rule configure, quote, list, delete."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_quote_no_rules_returns_base(client, clean_redis):
    res = await client.post(
        "/api/v1/pricing/quote",
        json={
            "brand_id": "b1",
            "sku_or_listing_id": "sku1",
            "base_price_cents": 1000,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["quoted_price_cents"] == 1000
    assert body["multiplier_applied"] == 1.0


@pytest.mark.asyncio
async def test_configure_rule_happy(client, clean_redis):
    res = await client.post(
        "/api/v1/pricing/rule/configure",
        json={
            "brand_id": "b1",
            "sku_or_listing_id": "sku1",
            "rules": [
                {"trigger": "demand", "condition": {"threshold": 5}, "multiplier": 1.5},
            ],
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "rule_id" in body
    assert len(body["rules"]) == 1


@pytest.mark.asyncio
async def test_configure_rule_missing_rules_422(client, clean_redis):
    res = await client.post(
        "/api/v1/pricing/rule/configure",
        json={"brand_id": "b1", "sku_or_listing_id": "sku1", "rules": []},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_quote_applies_surge_when_demand_high(client, clean_redis):
    await client.post(
        "/api/v1/pricing/rule/configure",
        json={
            "brand_id": "b2",
            "sku_or_listing_id": "sku2",
            "rules": [
                {"trigger": "demand", "condition": {"threshold": 5}, "multiplier": 1.5},
            ],
        },
    )
    res = await client.post(
        "/api/v1/pricing/quote",
        json={
            "brand_id": "b2",
            "sku_or_listing_id": "sku2",
            "base_price_cents": 1000,
            "context": {"demand_index": 10},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["multiplier_applied"] == 1.5
    assert body["quoted_price_cents"] == 1500
    assert len(body["rules_fired"]) == 1


@pytest.mark.asyncio
async def test_list_brand_rules_empty(client, clean_redis):
    res = await client.get("/api/v1/pricing/brand/no_brand/rules")
    assert res.status_code == 200
    assert res.json() == []


@pytest.mark.asyncio
async def test_delete_rule_404(client, clean_redis):
    res = await client.delete("/api/v1/pricing/rule/not_a_real_rule")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_delete_rule_success(client, clean_redis):
    create = await client.post(
        "/api/v1/pricing/rule/configure",
        json={
            "brand_id": "b3",
            "sku_or_listing_id": "sku3",
            "rules": [
                {"trigger": "demand", "condition": {"threshold": 1}, "multiplier": 2.0},
            ],
        },
    )
    rule_id = create.json()["rule_id"]
    res = await client.delete(f"/api/v1/pricing/rule/{rule_id}")
    assert res.status_code == 200
    assert res.json()["ok"] is True
