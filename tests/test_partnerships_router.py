"""Partnerships router — propose/accept/reject/terminate, stats."""

from __future__ import annotations

import pytest


async def _propose(client, p="brand_a", t="brand_b") -> str:
    res = await client.post(
        "/api/v1/partnerships/propose",
        json={
            "proposer_brand_id": p,
            "target_brand_id": t,
            "type": "joint_campaign",
            "proposer_signatory_user_id": "u_proposer",
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["partnership_id"]


@pytest.mark.asyncio
async def test_propose_happy_path(client, clean_redis):
    pid = await _propose(client)
    assert pid.startswith("partnership_") or len(pid) > 0


@pytest.mark.asyncio
async def test_propose_self_partnership_rejected(client, clean_redis):
    res = await client.post(
        "/api/v1/partnerships/propose",
        json={
            "proposer_brand_id": "same",
            "target_brand_id": "same",
            "type": "joint_campaign",
            "proposer_signatory_user_id": "u",
        },
    )
    assert res.status_code in (400, 409, 422)


@pytest.mark.asyncio
async def test_propose_invalid_type_422(client, clean_redis):
    res = await client.post(
        "/api/v1/partnerships/propose",
        json={
            "proposer_brand_id": "a",
            "target_brand_id": "b",
            "type": "bogus_type",
            "proposer_signatory_user_id": "u",
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_get_partnership_404(client, clean_redis):
    res = await client.get("/api/v1/partnerships/partnership_nope")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_accept_partnership(client, clean_redis):
    pid = await _propose(client)
    res = await client.post(
        f"/api/v1/partnerships/{pid}/accept",
        json={"brand_id": "brand_b", "signatory_user_id": "u_target"},
    )
    assert res.status_code == 200, res.text


@pytest.mark.asyncio
async def test_reject_partnership(client, clean_redis):
    pid = await _propose(client, p="b1", t="b2")
    res = await client.post(
        f"/api/v1/partnerships/{pid}/reject",
        json={"brand_id": "b2", "reason": "no thanks"},
    )
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_accept_by_non_party_forbidden(client, clean_redis):
    pid = await _propose(client, p="x", t="y")
    res = await client.post(
        f"/api/v1/partnerships/{pid}/accept",
        json={"brand_id": "intruder", "signatory_user_id": "u_x"},
    )
    assert res.status_code in (403, 404)
