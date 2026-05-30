"""Tests — Wave F geofenced voucher service + router."""

from __future__ import annotations

import pytest

from app.main import app
from app.deps import get_current_user
from app.services import wavef_geofenced_voucher as svc


def _override_user(user_id: str = "u-geo-1", brand_id: str = "b-geo"):
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


# Singapore CBD anchor
ANCHOR_LAT = 1.2839
ANCHOR_LNG = 103.8607


# ── Service ──────────────────────────────────────────────────────────────


def test_haversine_known_distance():
    # 1 deg of lat ≈ 111 km
    d = svc.haversine_m(0.0, 0.0, 1.0, 0.0)
    assert 110_000 < d < 112_000


@pytest.mark.asyncio
async def test_set_and_get_geofence(clean_redis):
    r = clean_redis
    await svc.set_geofence(
        r, "v1", anchor_lat=ANCHOR_LAT, anchor_lng=ANCHOR_LNG,
        radius_m=200, brand_id="b1",
    )
    fence = await svc.get_geofence(r, "v1")
    assert fence is not None
    assert fence["radius_m"] == 200
    assert fence["owner_brand_id"] == "b1"


@pytest.mark.asyncio
async def test_check_inside_radius_allowed(clean_redis):
    r = clean_redis
    await svc.set_geofence(
        r, "v2", anchor_lat=ANCHOR_LAT, anchor_lng=ANCHOR_LNG, radius_m=500,
    )
    # ~10 m east of anchor
    res = await svc.check_geo(
        r, "v2", ANCHOR_LAT, ANCHOR_LNG + 0.0001, user_id="u1"
    )
    assert res["allowed"] is True
    assert res["distance_m"] < 500


@pytest.mark.asyncio
async def test_check_outside_radius_denied(clean_redis):
    r = clean_redis
    await svc.set_geofence(
        r, "v3", anchor_lat=ANCHOR_LAT, anchor_lng=ANCHOR_LNG, radius_m=100,
    )
    # ~2.2 km east (well beyond 100 m)
    res = await svc.check_geo(
        r, "v3", ANCHOR_LAT, ANCHOR_LNG + 0.02, user_id="u1"
    )
    assert res["allowed"] is False
    assert res["reason"] == "GEO_DENIED"


@pytest.mark.asyncio
async def test_check_no_fence_is_backward_compat(clean_redis):
    r = clean_redis
    res = await svc.check_geo(r, "no-fence", 1.0, 2.0, user_id="u1")
    assert res["allowed"] is True
    assert res["radius_m"] is None


@pytest.mark.asyncio
async def test_check_missing_location_denied(clean_redis):
    r = clean_redis
    await svc.set_geofence(
        r, "v4", anchor_lat=ANCHOR_LAT, anchor_lng=ANCHOR_LNG, radius_m=100,
    )
    res = await svc.check_geo(r, "v4", None, None, user_id="u1")
    assert res["allowed"] is False


@pytest.mark.asyncio
async def test_audit_truncated_to_4_decimals(clean_redis):
    r = clean_redis
    await svc.set_geofence(
        r, "v5", anchor_lat=ANCHOR_LAT, anchor_lng=ANCHOR_LNG, radius_m=100,
    )
    await svc.check_geo(
        r, "v5", 1.28395123456, 103.86075678, user_id="u-audit"
    )
    log = await svc.audit_log(r, "v5")
    assert log
    assert log[-1]["lat_t"] == 1.284
    assert log[-1]["lng_t"] == 103.8608


# ── Router ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_set_and_check_inside(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        r1 = await client.post(
            "/api/v1/wavef/geo-voucher/v-api-1/fence",
            json={
                "anchor_lat": ANCHOR_LAT,
                "anchor_lng": ANCHOR_LNG,
                "radius_m": 500,
            },
        )
        assert r1.status_code == 200, r1.text
        r2 = await client.post(
            "/api/v1/wavef/geo-voucher/v-api-1/check",
            json={"lat": ANCHOR_LAT, "lng": ANCHOR_LNG + 0.0001},
        )
        assert r2.status_code == 200
        assert r2.json()["allowed"] is True
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_check_outside_returns_403(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        await client.post(
            "/api/v1/wavef/geo-voucher/v-api-2/fence",
            json={
                "anchor_lat": ANCHOR_LAT,
                "anchor_lng": ANCHOR_LNG,
                "radius_m": 100,
            },
        )
        r = await client.post(
            "/api/v1/wavef/geo-voucher/v-api-2/check",
            json={"lat": ANCHOR_LAT, "lng": ANCHOR_LNG + 0.05},
        )
        assert r.status_code == 403
        body = r.json()
        assert body["detail"]["code"] == "GEO_DENIED"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_get_fence_404_when_unset(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        r = await client.get("/api/v1/wavef/geo-voucher/missing/fence")
        assert r.status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)
