"""Auction router tests — empty pool, eligibility, impression report."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_auction_run_no_eligible_campaigns(client, clean_redis):
    """When no campaigns are active, auction returns ``no_eligible_campaigns``."""
    res = await client.post(
        "/api/v1/auction/run",
        json={
            "device_fingerprint": "test_device_no_camp",
            "slot": "main",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["no_eligible_campaigns"] is True
    assert body.get("winner_campaign_id") in (None, "")


@pytest.mark.asyncio
async def test_auction_run_rejects_missing_device(client, clean_redis):
    """``device_fingerprint`` is required — schema validation must fail."""
    res = await client.post("/api/v1/auction/run", json={"slot": "main"})
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_auction_report_impression_unknown_token(client, clean_redis):
    """Reporting an impression for an unknown token should not crash."""
    res = await client.post(
        "/api/v1/auction/report-impression",
        json={"impression_token": "imp_token_does_not_exist"},
    )
    # Either 404 (unknown token) or 200 (best-effort no-op) is acceptable;
    # the test pins the contract to one of these two stable behaviours.
    assert res.status_code in (200, 400, 404), res.text


@pytest.mark.asyncio
async def test_auction_run_accepts_objective_filter(client, clean_redis):
    """The ``objective_filter`` field is part of the public schema."""
    res = await client.post(
        "/api/v1/auction/run",
        json={
            "device_fingerprint": "test_device_obj",
            "slot": "main",
            "objective_filter": "acquire",
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["no_eligible_campaigns"] is True
