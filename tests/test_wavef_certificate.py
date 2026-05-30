"""Tests — Wave F game-completion certificate."""

from __future__ import annotations

import pytest

from app.main import app
from app.deps import get_current_user
from app.services import wavef_certificate as svc


def _override_user():
    async def _fake():
        return {
            "sub": "u-cert",
            "brand_id": "b1",
            "device_sig": "dev",
            "session_id": "s",
            "is_day1": False,
            "exp": 0,
        }
    return _fake


# ── Service ──────────────────────────────────────────────────────────────


def test_render_svg_has_player_brand_score():
    svg, code = svc.render_svg(
        player_name="Alice",
        brand_name="KFC",
        game_name="Shrimp Attack",
        score=1500,
    )
    assert svg.startswith("<svg")
    assert "Alice" in svg
    assert "KFC" in svg
    assert "Shrimp Attack" in svg
    assert "1500" in svg
    # 10-char verification code, uppercase hex
    assert len(code) == 10
    assert code == code.upper()


def test_render_escapes_xml_metachars():
    svg, _code = svc.render_svg(
        player_name="<script>x</script>",
        brand_name="Brand & Co.",
        game_name="A>B",
        score=10,
    )
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg
    assert "&amp;" in svg


def test_verification_code_is_deterministic():
    _svg1, c1 = svc.render_svg(
        player_name="A", brand_name="B", game_name="G", score=1,
        issued_at=__import__("datetime").datetime(2026, 5, 30, 10, 0),
    )
    _svg2, c2 = svc.render_svg(
        player_name="A", brand_name="B", game_name="G", score=1,
        issued_at=__import__("datetime").datetime(2026, 5, 30, 10, 0),
    )
    assert c1 == c2


# ── Router ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_render_json(client):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        res = await client.post(
            "/api/v1/wavef/certificate/render",
            json={
                "player_name": "Bob",
                "brand_name": "Starbucks",
                "game_name": "Coffee Quiz",
                "score": 2500,
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["svg"].startswith("<svg")
        assert "Bob" in body["svg"]
        assert len(body["verification_code"]) == 10
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_render_raw_svg(client):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        res = await client.post(
            "/api/v1/wavef/certificate/svg",
            json={
                "player_name": "Carol",
                "brand_name": "McDonalds",
                "game_name": "Monopoly",
                "score": 9999,
            },
        )
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("image/svg+xml")
        assert res.text.startswith("<svg")
        assert "X-Verification-Code" in res.headers or "x-verification-code" in res.headers
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_rejects_bad_hex_color(client):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        res = await client.post(
            "/api/v1/wavef/certificate/render",
            json={
                "player_name": "X",
                "brand_name": "Y",
                "game_name": "Z",
                "score": 1,
                "primary_color": "not-a-color",
            },
        )
        assert res.status_code == 422
    finally:
        app.dependency_overrides.pop(get_current_user, None)
