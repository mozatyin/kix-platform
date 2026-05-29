"""Auth router tests — error paths, refresh validation, centrifugo.

The successful ``POST /token`` flow inserts a row into the ``user_profiles``
table (FK -> ``brand_configs``), so it requires a real PG fixture beyond
the Redis-only conftest. These tests cover the no-DB paths: schema
validation, unknown-brand 404, refresh-token rotation safety, and the
centrifugo auth-required gate.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_auth_token_unknown_brand_is_404(client, clean_redis):
    """No brand config in Redis → /token must surface a clean 404."""
    res = await client.post(
        "/api/v1/auth/token",
        json={"brand_id": "b_auth_unknown", "device_sig": "dev_auth_x"},
    )
    assert res.status_code == 404
    assert "Brand config not found" in res.text


@pytest.mark.asyncio
async def test_auth_token_missing_brand_id_is_422(client, clean_redis):
    """bug-bait: missing required field must be 422 from pydantic."""
    res = await client.post(
        "/api/v1/auth/token",
        json={"device_sig": "dev_auth_only"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_auth_token_missing_device_sig_is_422(client, clean_redis):
    res = await client.post(
        "/api/v1/auth/token",
        json={"brand_id": "b_auth_x"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_auth_refresh_unknown_token_is_unauthorized(client, clean_redis):
    """bug-bait: bogus refresh token must be 401, never 500.

    This is the canonical "expired/invalid token" guard — it must not
    leak into a 200 or a 500 traceback under any circumstance.
    """
    res = await client.post(
        "/api/v1/auth/token/refresh",
        json={
            "refresh_token": "rt_does_not_exist_xxxx",
            "device_sig": "dev_x",
        },
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_auth_refresh_missing_token_is_422(client, clean_redis):
    """bug-bait: pydantic must reject a refresh request missing the token."""
    res = await client.post(
        "/api/v1/auth/token/refresh",
        json={"device_sig": "dev_only"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_auth_centrifugo_requires_authorization(client, clean_redis):
    """bug-bait: missing Authorization header must be rejected, not allowed.

    This is the closest auth-router probe to "scope validation": no token =
    no centrifugo channel grant.
    """
    res = await client.post("/api/v1/auth/centrifugo-token")
    assert res.status_code in {401, 403, 422}


@pytest.mark.asyncio
async def test_auth_centrifugo_rejects_bad_bearer(client, clean_redis):
    """bug-bait: malformed Authorization header must not 200."""
    res = await client.post(
        "/api/v1/auth/centrifugo-token",
        headers={"Authorization": "Bearer not-a-real-jwt"},
    )
    assert res.status_code in {401, 403}
