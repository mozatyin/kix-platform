"""Tests — Wave F animation library service + router."""

from __future__ import annotations

import os

import pytest

from app.main import app
from app.deps import get_current_user
from app.services import wavef_animation_library as svc


def _override_user():
    async def _fake():
        return {
            "sub": "u-anim",
            "brand_id": "b-anim",
            "device_sig": "dev",
            "session_id": "s",
            "is_day1": False,
            "exp": 0,
        }
    return _fake


# ── Service ──────────────────────────────────────────────────────────────


def test_list_primitives_includes_spec_required_four():
    ids = {p["id"] for p in svc.list_primitives()}
    assert "confetti" in ids
    assert "sparkle" in ids
    assert "jackpot" in ids
    assert "slot-roll" in ids


def test_get_primitive_known_and_unknown():
    assert svc.get_primitive("confetti")["js_entry"] == "confetti"
    assert svc.get_primitive("not-a-real-thing") is None


def test_palette_fallback_when_no_brand():
    pal = svc.palette_for(None)
    assert len(pal) == 4
    assert all(c.startswith("#") for c in pal)


def test_palette_with_brand_hex_returns_brand_first():
    pal = svc.palette_for("#3366aa")
    assert pal[0] == "#3366aa"
    assert len(pal) == 4
    # Should include white as a contrast colour.
    assert "#ffffff" in pal


def test_palette_rejects_garbage_returns_fallback():
    pal = svc.palette_for("not-a-hex")
    assert pal == svc.palette_for(None)


def test_assets_dir_resolves():
    d = svc.assets_dir()
    assert d.endswith(os.path.join("landing", "sdk", "animations"))


def test_each_primitive_has_css_on_disk():
    # Critical guard: every primitive must ship with a CSS asset.
    for p in svc.list_primitives():
        assert svc.asset_exists(p["id"]), f"missing CSS for {p['id']}"


def test_fx_js_present():
    assert svc.fx_js_exists()


# ── Router ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_list(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        r = await client.get("/api/v1/wavef/animations")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 4
        assert body["fx_js_present"] is True
        assert all(item["asset_present"] for item in body["items"])
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_palette_with_brand(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        r = await client.get(
            "/api/v1/wavef/animations/palette",
            params={"brand_primary": "ff6600"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["brand_primary"] == "#ff6600"
        assert len(body["palette"]) == 4
        assert body["palette"][0] == "#ff6600"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_get_known_and_404(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        r1 = await client.get("/api/v1/wavef/animations/confetti")
        assert r1.status_code == 200
        assert r1.json()["js_entry"] == "confetti"

        r2 = await client.get("/api/v1/wavef/animations/no-such")
        assert r2.status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)
