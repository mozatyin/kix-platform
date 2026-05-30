"""Health router — liveness, readiness, region, metrics smoke tests."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_health_liveness(client, clean_redis):
    res = await client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert body["uptime_seconds"] >= 0


@pytest.mark.asyncio
async def test_readiness_returns_503_when_no_config(client, clean_redis):
    """Empty Redis → no brand configs → readiness must report not_ready."""
    res = await client.get("/ready")
    assert res.status_code == 503
    assert res.json()["status"] == "not_ready"


@pytest.mark.asyncio
async def test_readiness_returns_200_when_config_loaded(client, clean_redis):
    await clean_redis.set("config:test-brand", '{"x":1}')
    res = await client.get("/ready")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ready"
    assert body["checks"]["brands_count"] >= 1


@pytest.mark.asyncio
async def test_region_health_returns_config(client, clean_redis):
    res = await client.get("/api/v1/health/region")
    assert res.status_code == 200
    body = res.json()
    assert "region" in body
    assert "primary_currency" in body
    assert "compliance_jurisdiction" in body


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_text(client, clean_redis):
    res = await client.get("/metrics")
    assert res.status_code == 200
    assert "text/plain" in res.headers.get("content-type", "")
