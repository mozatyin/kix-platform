"""Portal auth router — register/login/logout error paths."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_register_invalid_email_422(client, clean_redis):
    res = await client.post(
        "/api/v1/portal/auth/register",
        json={"email": "not-an-email", "password": "secret123", "brand_name": "Foo"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_register_short_password_422(client, clean_redis):
    res = await client.post(
        "/api/v1/portal/auth/register",
        json={"email": "a@b.com", "password": "x", "brand_name": "Foo"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_register_empty_brand_name_422(client, clean_redis):
    res = await client.post(
        "/api/v1/portal/auth/register",
        json={"email": "a@b.com", "password": "secret123", "brand_name": "  "},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_register_missing_field_422(client, clean_redis):
    res = await client.post(
        "/api/v1/portal/auth/register",
        json={"email": "a@b.com"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_login_unknown_email_401(client, clean_redis):
    res = await client.post(
        "/api/v1/portal/auth/login",
        json={"email": "nobody@nowhere.com", "password": "wrong"},
    )
    assert res.status_code in (401, 404)


@pytest.mark.asyncio
async def test_logout_without_token(client, clean_redis):
    res = await client.post("/api/v1/portal/auth/logout")
    # may accept or require auth
    assert res.status_code in (200, 401, 422)
