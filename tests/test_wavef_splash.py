"""Tests — Wave F pre-game brand splash (spec #15)."""

from __future__ import annotations

import pytest

from app.main import app
from app.deps import get_current_user
from app.services import wavef_splash as svc


def _override_user(uid: str = "u-splash-1"):
    async def _fake():
        return {
            "sub": uid,
            "brand_id": "b1",
            "device_sig": "dev",
            "session_id": "s",
            "is_day1": False,
            "exp": 0,
        }
    return _fake


# ── Service ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_and_get(clean_redis):
    cfg = await svc.set_config(
        clean_redis,
        campaign_id="c1",
        logo_url="https://cdn.example.com/logo.png",
        tagline="Refresh your day",
        duration_ms=2500,
        brand_primary="#FF5500",
        show_max_per_day=2,
    )
    assert cfg["enabled"] is True
    assert cfg["duration_ms"] == 2500
    assert cfg["brand_primary"] == "#FF5500"


@pytest.mark.asyncio
async def test_get_missing_returns_none(clean_redis):
    assert await svc.get_config(clean_redis, "nope") is None


@pytest.mark.asyncio
async def test_validate_duration(clean_redis):
    with pytest.raises(ValueError):
        await svc.set_config(
            clean_redis,
            campaign_id="c2",
            logo_url="https://cdn.example.com/logo.png",
            duration_ms=42,
        )


@pytest.mark.asyncio
async def test_validate_hex_colour(clean_redis):
    with pytest.raises(ValueError):
        await svc.set_config(
            clean_redis,
            campaign_id="c3",
            logo_url="https://cdn.example.com/logo.png",
            brand_primary="not-a-colour",
        )


@pytest.mark.asyncio
async def test_logo_required_when_enabled(clean_redis):
    with pytest.raises(ValueError):
        await svc.set_config(
            clean_redis,
            campaign_id="c4",
            logo_url="",
            enabled=True,
        )


@pytest.mark.asyncio
async def test_disable_keeps_row_but_flags_disabled(clean_redis):
    await svc.set_config(
        clean_redis,
        campaign_id="c5",
        logo_url="https://cdn.example.com/logo.png",
    )
    cfg = await svc.disable(clean_redis, "c5")
    assert cfg["enabled"] is False
    cfg2 = await svc.get_config(clean_redis, "c5")
    assert cfg2 is not None and cfg2["enabled"] is False


# ── Router ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_get_204_when_unset(client, clean_redis):
    res = await client.get("/api/v1/wavef/splash/unknown")
    assert res.status_code == 204


@pytest.mark.asyncio
async def test_router_put_then_get(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        put = await client.put(
            "/api/v1/wavef/splash/rc1",
            json={
                "logo_url": "https://cdn.example.com/logo.png",
                "tagline": "Hello",
                "duration_ms": 2000,
                "brand_primary": "#123456",
                "show_max_per_day": 1,
            },
        )
        assert put.status_code == 200, put.text
        get = await client.get("/api/v1/wavef/splash/rc1")
        assert get.status_code == 200
        body = get.json()
        assert body["enabled"] is True
        assert body["tagline"] == "Hello"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_delete_disables(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        await client.put(
            "/api/v1/wavef/splash/rc2",
            json={"logo_url": "https://cdn.example.com/logo.png"},
        )
        d = await client.delete("/api/v1/wavef/splash/rc2")
        assert d.status_code == 200
        assert d.json()["enabled"] is False
        # GET now returns 204
        g = await client.get("/api/v1/wavef/splash/rc2")
        assert g.status_code == 204
    finally:
        app.dependency_overrides.pop(get_current_user, None)
