"""Tests for /api/v1/health/stripe-mode endpoint (A2 readiness surface)."""
import pytest
import time

from app.redis_client import get_redis

pytestmark = pytest.mark.asyncio


async def test_stripe_mode_health_returns_mode():
    from app.main import app
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/api/v1/health/stripe-mode")
    assert r.status_code == 200
    data = r.json()
    assert "mode" in data
    assert data["mode"] in ("live", "test", "mock")
    assert "warnings" in data
    assert isinstance(data["warnings"], list)
    assert "is_ready_for_real_money" in data


async def test_stripe_mode_health_warns_when_no_cron_history():
    r = await get_redis()
    # Clear cron history
    await r.delete("billing_cron:day91:last_run_ts")

    from app.main import app
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/v1/health/stripe-mode")
    data = resp.json()
    # Should warn about missing cron history
    assert any("never run" in w or "missing" in w for w in data["warnings"])


async def test_stripe_mode_health_records_recent_cron():
    r = await get_redis()
    # Simulate a recent cron run
    now = time.time()
    await r.set("billing_cron:day91:last_run_ts", str(now))
    await r.set("billing_cron:day91:renewed_count_24h", "5")
    await r.set("billing_cron:day91:failed_count_24h", "1")

    from app.main import app
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/v1/health/stripe-mode")
    data = resp.json()
    assert data["day91_renewals_24h"] == 5
    assert data["day91_failures_24h"] == 1
    assert data["last_run_ts"] is not None


async def test_mock_mode_in_test_region_no_warning():
    """In test/dev region, mock mode is expected — no CRITICAL warning."""
    from app.main import app
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/v1/health/stripe-mode")
    data = resp.json()
    # Should NOT have CRITICAL warning about prod + mock
    has_critical_prod_warning = any(
        "CRITICAL" in w and "production region but Stripe in mock" in w
        for w in data["warnings"]
    )
    # In test/dev region, no critical prod warning
    if not data.get("production_region"):
        assert not has_critical_prod_warning


async def test_is_ready_for_real_money_false_in_mock():
    """Mock mode → is_ready_for_real_money=False."""
    from app.main import app
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/v1/health/stripe-mode")
    data = resp.json()
    if data["mode"] == "mock":
        assert data["is_ready_for_real_money"] is False
