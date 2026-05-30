"""Tests — Wave F email/SMS capture gate (spec #11)."""

from __future__ import annotations

import pytest

from app.main import app
from app.deps import get_current_user
from app.services import wavef_capture as svc


def _override_user(user_id: str = "u-cap-1", brand_id: str = "b1"):
    async def _fake():
        return {
            "sub": user_id,
            "brand_id": brand_id,
            "device_sig": "dev",
            "session_id": "s",
            "is_day1": False,
            "exp": 0,
        }
    return _fake


# ── Service ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_and_export_email(clean_redis):
    r = clean_redis
    res = await svc.submit(
        r,
        campaign_id="c1",
        brand_id="b1",
        email="Alice@Example.com",
        phone=None,
        sms_opt_in=False,
        marketing_opt_in=True,
    )
    assert res == {"accepted": True, "idempotent": False}

    rows = await svc.export_records(r, "c1")
    assert len(rows) == 1
    # plaintext is decrypted on export
    assert rows[0]["email"] == "alice@example.com"
    assert rows[0]["marketing_opt_in"] is True


@pytest.mark.asyncio
async def test_submit_requires_email_or_phone(clean_redis):
    with pytest.raises(ValueError):
        await svc.submit(
            clean_redis,
            campaign_id="c1",
            brand_id="b1",
            email=None,
            phone=None,
            sms_opt_in=False,
            marketing_opt_in=False,
        )


@pytest.mark.asyncio
async def test_submit_rejects_bad_email(clean_redis):
    with pytest.raises(ValueError):
        await svc.submit(
            clean_redis,
            campaign_id="c1",
            brand_id="b1",
            email="not-an-email",
            phone=None,
            sms_opt_in=False,
            marketing_opt_in=False,
        )


@pytest.mark.asyncio
async def test_duplicate_submit_is_idempotent(clean_redis):
    r = clean_redis
    args = dict(
        campaign_id="c2",
        brand_id="b1",
        email="bob@example.com",
        phone=None,
        sms_opt_in=False,
        marketing_opt_in=False,
    )
    r1 = await svc.submit(r, **args)
    r2 = await svc.submit(r, **args)
    assert r1["idempotent"] is False
    assert r2["idempotent"] is True
    rows = await svc.export_records(r, "c2")
    assert len(rows) == 1  # not duplicated


@pytest.mark.asyncio
async def test_optout_filters_export(clean_redis):
    r = clean_redis
    await svc.submit(
        r,
        campaign_id="c3",
        brand_id="b1",
        email="carol@example.com",
        phone=None,
        sms_opt_in=False,
        marketing_opt_in=False,
    )
    rows_before = await svc.export_records(r, "c3")
    assert len(rows_before) == 1

    await svc.optout(r, brand_id="b1", email="carol@example.com")
    rows_after = await svc.export_records(r, "c3")
    assert rows_after == []


@pytest.mark.asyncio
async def test_optout_blocks_future_submit(clean_redis):
    r = clean_redis
    await svc.optout(r, brand_id="b1", email="dan@example.com")
    res = await svc.submit(
        r,
        campaign_id="c4",
        brand_id="b1",
        email="dan@example.com",
        phone=None,
        sms_opt_in=False,
        marketing_opt_in=False,
    )
    assert res == {"accepted": False, "reason": "opted_out"}


@pytest.mark.asyncio
async def test_phone_capture_normalisation(clean_redis):
    r = clean_redis
    await svc.submit(
        r,
        campaign_id="c5",
        brand_id="b1",
        email=None,
        phone="+65 9123 4567",
        sms_opt_in=True,
        marketing_opt_in=False,
    )
    rows = await svc.export_records(r, "c5")
    assert len(rows) == 1
    assert rows[0]["phone"] == "+6591234567"
    assert rows[0]["sms_opt_in"] is True


def test_to_csv_header_only_when_empty():
    txt = svc.to_csv([])
    assert txt.startswith("email,phone,sms_opt_in")
    assert txt.count("\n") == 1


# ── Router ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_submit_and_export(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user(brand_id="b1")
    try:
        r = await client.post(
            "/api/v1/wavef/capture/submit",
            json={
                "campaign_id": "rc1",
                "email": "router@example.com",
                "sms_opt_in": True,
                "marketing_opt_in": True,
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["accepted"] is True

        # export requires admin
        res = await client.get(
            "/api/v1/wavef/capture/rc1/export",
            headers={"X-Admin-Token": "admin-dev-token"},
        )
        assert res.status_code == 200
        body = res.text
        assert "router@example.com" in body
        assert body.startswith("email,phone,sms_opt_in")
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_export_rejects_without_admin(client, clean_redis):
    res = await client.get("/api/v1/wavef/capture/whatever/export")
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_router_optout_public(client, clean_redis):
    res = await client.post(
        "/api/v1/wavef/capture/optout",
        json={"brand_id": "b1", "email": "x@example.com"},
    )
    assert res.status_code == 200
    assert res.json()["added"] >= 1
