"""Attribution router tests — invite tokens, impressions, journey."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_attribution_health(client, clean_redis):
    res = await client.get("/api/v1/attribution/health")
    assert res.status_code == 200, res.text
    body = res.json()
    # Health probe should at minimum report status.
    assert "status" in body or "ok" in body


@pytest.mark.asyncio
async def test_attribution_token_create(client, clean_redis):
    res = await client.post(
        "/api/v1/attribution/token/create",
        json={
            "brand_id": "brand_attr_1",
            "user_id": "user_attr_1",
            "ttl_seconds": 3600,
            "context": {"campaign_id": "camp_x"},
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["invite_token"]
    assert body["share_url_suffix"].startswith("?ref=")
    assert body["expires_at"] > 0


@pytest.mark.asyncio
async def test_attribution_track_impression_requires_target_brand(client, clean_redis):
    res = await client.post(
        "/api/v1/attribution/track/impression",
        json={
            "user_id": "user_attr_imp",
            # target_brand omitted on purpose
        },
    )
    assert res.status_code == 400, res.text


@pytest.mark.asyncio
async def test_attribution_track_impression_basic(client, clean_redis):
    res = await client.post(
        "/api/v1/attribution/track/impression",
        json={
            "user_id": "user_attr_ok",
            "target_brand": "brand_attr_ok",
        },
    )
    # Consent enforcement may 403 in strict mode; permissive default → 200.
    assert res.status_code in (200, 403), res.text
    if res.status_code == 200:
        body = res.json()
        assert body["ok"] is True
        assert body["event_id"]


@pytest.mark.asyncio
async def test_attribution_token_create_rejects_bad_ttl(client, clean_redis):
    res = await client.post(
        "/api/v1/attribution/token/create",
        json={
            "brand_id": "brand_ttl",
            "user_id": "user_ttl",
            "ttl_seconds": 1,  # below the 60s floor
        },
    )
    assert res.status_code == 422
