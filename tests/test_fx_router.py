"""FX router — admin token gate, configure/convert/expire/history/health."""

from __future__ import annotations

import pytest

ADMIN = "kix-dev-secret-change-in-production"


@pytest.mark.asyncio
async def test_health(client, clean_redis):
    res = await client.get("/api/v1/fx/health")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert "supported_currencies" in body


@pytest.mark.asyncio
async def test_configure_rates_requires_admin(client, clean_redis):
    res = await client.post(
        "/api/v1/fx/rates/configure",
        json={"admin_token": "wrong", "pairs": [
            {"from_currency": "USD", "to_currency": "EUR", "rate": "0.9"},
        ]},
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_configure_rates_happy(client, clean_redis):
    res = await client.post(
        "/api/v1/fx/rates/configure",
        json={
            "admin_token": ADMIN,
            "pairs": [
                {"from_currency": "USD", "to_currency": "EUR", "rate": "0.9"},
            ],
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["configured"] == 1


@pytest.mark.asyncio
async def test_configure_rejects_identity_pair(client, clean_redis):
    res = await client.post(
        "/api/v1/fx/rates/configure",
        json={
            "admin_token": ADMIN,
            "pairs": [
                {"from_currency": "USD", "to_currency": "USD", "rate": "1"},
            ],
        },
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_convert_uses_configured_rate(client, clean_redis):
    await client.post(
        "/api/v1/fx/rates/configure",
        json={"admin_token": ADMIN, "pairs": [
            {"from_currency": "USD", "to_currency": "EUR", "rate": "0.5"},
        ]},
    )
    res = await client.post(
        "/api/v1/fx/convert",
        json={"amount_cents": 10_000, "from_currency": "USD", "to_currency": "EUR"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["equivalent_cents"] == 5_000


@pytest.mark.asyncio
async def test_convert_invalid_currency_422(client, clean_redis):
    res = await client.post(
        "/api/v1/fx/convert",
        json={"amount_cents": 100, "from_currency": "US", "to_currency": "EUR"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_expire_unknown_pair_404(client, clean_redis):
    res = await client.post(
        "/api/v1/fx/rates/expire/USD:GBP",
        params={"admin_token": ADMIN},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_history_endpoint_returns_list(client, clean_redis):
    await client.post(
        "/api/v1/fx/rates/configure",
        json={"admin_token": ADMIN, "pairs": [
            {"from_currency": "USD", "to_currency": "JPY", "rate": "150"},
        ]},
    )
    res = await client.get("/api/v1/fx/rates/USD/JPY/history")
    assert res.status_code == 200
    body = res.json()
    assert isinstance(body["entries"], list)
    assert len(body["entries"]) >= 1
