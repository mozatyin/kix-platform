"""Social router — friends + idempotency + 404 paths."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_friend_request_happy(client, clean_redis):
    res = await client.post(
        "/api/v1/social/friends/request",
        json={"from_user": "u1", "to_user": "u2", "brand_id": "b1"},
    )
    assert res.status_code == 201
    assert res.json()["ok"] is True


@pytest.mark.asyncio
async def test_friend_request_self_400(client, clean_redis):
    res = await client.post(
        "/api/v1/social/friends/request",
        json={"from_user": "u1", "to_user": "u1", "brand_id": "b1"},
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_friend_request_missing_field_422(client, clean_redis):
    res = await client.post(
        "/api/v1/social/friends/request",
        json={"from_user": "u1"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_friend_accept_404(client, clean_redis):
    res = await client.post(
        "/api/v1/social/friends/accept",
        json={"request_id": "no_such_req"},
    )
    assert res.status_code == 404
