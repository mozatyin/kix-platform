"""Moderation router — scan, queue add, policies."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_scan_clean_content(client, clean_redis):
    res = await client.post(
        "/api/v1/moderation/scan",
        json={"content_type": "text", "content": "hello world", "context": "ad_creative"},
    )
    assert res.status_code == 200, res.text
    assert "verdict" in res.json()


@pytest.mark.asyncio
async def test_scan_missing_content_422(client, clean_redis):
    res = await client.post(
        "/api/v1/moderation/scan",
        json={"content_type": "text"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_scan_invalid_content_type_422(client, clean_redis):
    res = await client.post(
        "/api/v1/moderation/scan",
        json={"content_type": "INVALID", "content": "hi"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_get_policies(client, clean_redis):
    res = await client.get("/api/v1/moderation/policies")
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_queue_add_returns_review_id(client, clean_redis):
    res = await client.post(
        "/api/v1/moderation/queue/add",
        json={"content": "needs human review", "content_type": "text"},
    )
    assert res.status_code == 200, res.text
    assert "review_id" in res.json()
