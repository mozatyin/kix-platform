"""Users router — me/vouchers, redeem (auth-gated smoke)."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_list_my_vouchers_requires_auth(client, clean_redis):
    res = await client.get("/api/v1/users/me/vouchers")
    assert res.status_code in (401, 403, 422)


@pytest.mark.asyncio
async def test_redeem_voucher_requires_auth(client, clean_redis):
    res = await client.post("/api/v1/users/me/vouchers/1/redeem")
    assert res.status_code in (401, 403, 422)


@pytest.mark.asyncio
async def test_redeem_voucher_invalid_id(client, clean_redis):
    res = await client.post(
        "/api/v1/users/me/vouchers/not_an_int/redeem",
        headers={"Authorization": "Bearer x"},
    )
    # 422 from path-int parse OR 401 from bad bearer
    assert res.status_code in (401, 403, 422)
