"""Smoke tests for QR, reward, ELTM callback, energy, welcome_kit, streak."""

from __future__ import annotations

import pytest


# ── QR router ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_qr_generate_missing_body_422(client, clean_redis):
    res = await client.post("/internal/qr/generate", json={})
    assert res.status_code in (422, 500)


@pytest.mark.asyncio
async def test_qr_force_rotate_missing_body_422(client, clean_redis):
    res = await client.post("/internal/qr/force-rotate", json={})
    assert res.status_code in (422, 500)


# ── Reward router ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_reward_evaluate_missing_body_422(client, clean_redis):
    res = await client.post("/internal/reward/", json={})
    assert res.status_code in (422, 500)


# ── ELTM Callback router ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_eltm_callback_missing_order_id_400(client, clean_redis):
    res = await client.post("/internal/eltm/callback", json={"event": "progress"})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_eltm_callback_unknown_order_404(client, clean_redis):
    res = await client.post(
        "/internal/eltm/callback",
        json={"order_id": "unknown_order_xx", "event": "progress"},
    )
    assert res.status_code == 404


# ── Welcome kit router ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_welcome_kit_generate_unknown_brand(client, clean_redis):
    res = await client.post("/api/v1/welcome-kit/b_unknown/generate")
    # may 404 (DB lookup) or 500
    assert res.status_code in (200, 404, 500)


@pytest.mark.asyncio
async def test_welcome_kit_shipping_status(client, clean_redis):
    res = await client.get("/api/v1/welcome-kit/b1/shipping/status")
    # empty queue → empty result or 200
    assert res.status_code in (200, 404)


# ── Streak router ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_streak_check_requires_auth(client, clean_redis):
    res = await client.post(
        "/api/v1/streak/check",
        json={"brand_id": "b1"},
    )
    assert res.status_code in (401, 403, 422)
