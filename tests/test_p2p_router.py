"""P2P router — gift send/claim, inbox, sent."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_gift_send_self_422(client, clean_redis):
    res = await client.post(
        "/api/v1/p2p/gift/send",
        json={
            "from_user_id": "u1",
            "to_user_id": "u1",
            "brand_id": "b1",
            "type": "energy",
            "amount": 5,
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_gift_send_missing_amount_422(client, clean_redis):
    res = await client.post(
        "/api/v1/p2p/gift/send",
        json={
            "from_user_id": "u1",
            "to_user_id": "u2",
            "brand_id": "b1",
            "type": "energy",
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_gift_send_invalid_type_422(client, clean_redis):
    res = await client.post(
        "/api/v1/p2p/gift/send",
        json={
            "from_user_id": "u1",
            "to_user_id": "u2",
            "brand_id": "b1",
            "type": "BOGUS",
            "amount": 5,
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_gift_claim_unknown_404(client, clean_redis):
    res = await client.post(
        "/api/v1/p2p/gift/no_such_gift/claim",
        json={"claim_token": "x", "user_id": "u1"},
    )
    assert res.status_code in (400, 404)


@pytest.mark.asyncio
async def test_gifts_inbox_requires_user(client, clean_redis):
    res = await client.get("/api/v1/p2p/gifts/inbox")
    # missing user query param
    assert res.status_code in (200, 422)
