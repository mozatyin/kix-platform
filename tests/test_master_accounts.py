"""Master accounts router tests — create, attach brand, RBAC, role updates."""

from __future__ import annotations

import pytest


async def _create_master(client, *, company="Acme Co", email="owner@acme.example.com",
                         owner="u_owner_1"):
    res = await client.post(
        "/api/v1/master/create",
        json={
            "company_name": company,
            "primary_email": email,
            "owner_user_id": owner,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


@pytest.mark.asyncio
async def test_master_create_basic(client, clean_redis):
    body = await _create_master(client, owner="u_master_1")
    assert body["master_id"].startswith("m_")
    assert body["owner_member_id"].startswith("mb_")


@pytest.mark.asyncio
async def test_master_create_invalid_email_rejected(client, clean_redis):
    """bug-bait: bad email must be 422 (EmailStr validation)."""
    res = await client.post(
        "/api/v1/master/create",
        json={
            "company_name": "X",
            "primary_email": "not-an-email",
            "owner_user_id": "u_master_2",
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_master_attach_brand_round_trip(client, clean_redis):
    m = await _create_master(client, owner="u_master_3", email="m3@acme.example.com")
    mid = m["master_id"]
    res = await client.post(
        f"/api/v1/master/{mid}/brands/attach",
        json={"brand_id": "b_master_3", "store_name": "Acme Downtown"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["brand_id"] == "b_master_3"

    # Verify it shows up on /master_id detail.
    detail = await client.get(f"/api/v1/master/{mid}")
    assert detail.status_code == 200
    brand_ids = [b["brand_id"] for b in detail.json()["brands"]]
    assert "b_master_3" in brand_ids


@pytest.mark.asyncio
async def test_master_attach_already_owned_brand_is_conflict(client, clean_redis):
    """bug-bait: a brand can be owned by only one master."""
    m1 = await _create_master(client, owner="u_master_4a", email="m4a@acme.example.com")
    m2 = await _create_master(client, owner="u_master_4b", email="m4b@acme.example.com")
    await client.post(
        f"/api/v1/master/{m1['master_id']}/brands/attach",
        json={"brand_id": "b_master_4_shared"},
    )
    res = await client.post(
        f"/api/v1/master/{m2['master_id']}/brands/attach",
        json={"brand_id": "b_master_4_shared"},
    )
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_master_owner_is_auto_hq_admin(client, clean_redis):
    """RBAC check: owner of a master can perform wallet.topup on its brands."""
    m = await _create_master(client, owner="u_master_5", email="m5@acme.example.com")
    mid = m["master_id"]
    await client.post(
        f"/api/v1/master/{mid}/brands/attach",
        json={"brand_id": "b_master_5"},
    )

    res = await client.post(
        "/api/v1/master/auth/check",
        json={
            "user_id": "u_master_5",
            "action": "wallet.topup",
            "brand_id": "b_master_5",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["allowed"] is True
    assert body["role"] == "hq_admin"


@pytest.mark.asyncio
async def test_master_auth_check_non_member_denied(client, clean_redis):
    """bug-bait: a user with no membership must be denied (not 500)."""
    res = await client.post(
        "/api/v1/master/auth/check",
        json={
            "user_id": "u_random_outsider",
            "action": "wallet.topup",
            "brand_id": "b_anything",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["allowed"] is False
    assert body["reason"] == "no_membership"


@pytest.mark.asyncio
async def test_master_demote_last_hq_admin_forbidden(client, clean_redis):
    """bug-bait: demoting the *only* hq_admin would lock everyone out — must 403."""
    m = await _create_master(client, owner="u_master_6", email="m6@acme.example.com")
    mid = m["master_id"]
    owner_member_id = m["owner_member_id"]

    res = await client.post(
        f"/api/v1/master/{mid}/members/{owner_member_id}/role",
        json={"role": "viewer"},
    )
    # Both 403 codes from the two guards (last-admin / owner-demote) are correct.
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_master_detach_brand_round_trip(client, clean_redis):
    m = await _create_master(client, owner="u_master_7", email="m7@acme.example.com")
    mid = m["master_id"]
    await client.post(
        f"/api/v1/master/{mid}/brands/attach",
        json={"brand_id": "b_master_7"},
    )
    res = await client.post(
        f"/api/v1/master/{mid}/brands/detach",
        json={"brand_id": "b_master_7"},
    )
    assert res.status_code == 200
    assert res.json()["detached"] is True
