"""Tests — Wave F brand-color extraction."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.main import app
from app.deps import get_current_user
from app.services import wavef_brand_color as svc


def _override_user():
    async def _fake():
        return {
            "sub": "u-color",
            "brand_id": "b1",
            "device_sig": "dev",
            "session_id": "s",
            "is_day1": False,
            "exp": 0,
        }
    return _fake


def _png_bytes(rgbs: list[tuple[int, int, int]], w: int = 64, h: int = 64) -> bytes:
    img = Image.new("RGB", (w, h))
    pixels = []
    n = len(rgbs)
    band = max(1, w // n)
    for y in range(h):
        for x in range(w):
            pixels.append(rgbs[min(x // band, n - 1)])
    img.putdata(pixels)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── Service ──────────────────────────────────────────────────────────────


def test_extract_dominant_solid_red():
    data = _png_bytes([(220, 30, 30)])
    res = svc.extract_palette(data, k=2)
    assert res["dominant"], res
    primary = res["palette"]["primary"]
    # Red channel should dominate.
    r = int(primary[1:3], 16)
    g = int(primary[3:5], 16)
    b = int(primary[5:7], 16)
    assert r > g + 50 and r > b + 50, primary


def test_extract_drops_pure_white_background():
    # Mostly white with one strong color.
    data = _png_bytes([(255, 255, 255)] * 6 + [(10, 80, 200)] * 2)
    res = svc.extract_palette(data, k=2)
    primary = res["palette"]["primary"]
    r = int(primary[1:3], 16)
    g = int(primary[3:5], 16)
    b = int(primary[5:7], 16)
    # Should be blue-leaning, not white.
    assert not (r > 240 and g > 240 and b > 240), primary
    assert b > r, primary


def test_extract_text_on_dark_is_white():
    data = _png_bytes([(10, 10, 60)])  # dark navy
    res = svc.extract_palette(data, k=1)
    assert res["palette"]["text_on_primary"] == "#FFFFFF"


def test_extract_text_on_light_is_black():
    data = _png_bytes([(245, 240, 80)], w=32, h=32)  # light yellow
    res = svc.extract_palette(data, k=1, drop_neutrals=False)
    assert res["palette"]["text_on_primary"] == "#000000"


def test_invalid_bytes_raises():
    with pytest.raises(ValueError):
        svc.extract_palette(b"not a png")


# ── Router ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_requires_auth(client):
    res = await client.post("/api/v1/wavef/brand-color/extract")
    assert res.status_code in (401, 403, 422)


@pytest.mark.asyncio
async def test_router_extracts_via_upload(client):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        data = _png_bytes([(220, 30, 30)])
        res = await client.post(
            "/api/v1/wavef/brand-color/extract",
            files={"file": ("logo.png", data, "image/png")},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert "dominant" in body and len(body["dominant"]) >= 1
        assert body["palette"]["primary"].startswith("#")
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_rejects_empty_upload(client):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        res = await client.post(
            "/api/v1/wavef/brand-color/extract",
            files={"file": ("logo.png", b"", "image/png")},
        )
        assert res.status_code == 400
    finally:
        app.dependency_overrides.pop(get_current_user, None)
