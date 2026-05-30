"""Tests for the 100-free-per-country mechanic (Wave H Opp #3)."""
import pytest

from app.redis_client import get_redis
from app.services import country_slots as svc

pytestmark = pytest.mark.asyncio


async def _cleanup(cc: str, *brand_ids: str):
    r = await get_redis()
    await r.delete(f"country_slots:{cc}:claimed")
    for bid in brand_ids:
        await r.delete(f"country_slots:by_brand:{bid}")


# ── Service layer ──────────────────────────────────────────────────────


async def test_claim_first_slot_succeeds():
    await _cleanup("SG", "test-sg-merchant-1")
    claim = await svc.claim_slot("SG", "test-sg-merchant-1")
    assert claim is not None
    assert claim.country_code == "SG"
    assert claim.brand_id == "test-sg-merchant-1"
    assert claim.founding is True


async def test_claim_is_idempotent_per_brand():
    await _cleanup("ID", "test-id-merchant-idem")
    c1 = await svc.claim_slot("ID", "test-id-merchant-idem")
    c2 = await svc.claim_slot("ID", "test-id-merchant-idem")
    assert c1 is not None and c2 is not None
    assert c1.country_code == c2.country_code
    assert c1.brand_id == c2.brand_id


async def test_summary_reflects_claims():
    await _cleanup("TH")
    initial = await svc.get_summary("TH")
    assert initial["remaining"] == 100
    assert initial["claimed"] == 0

    await svc.claim_slot("TH", "test-th-1")
    await svc.claim_slot("TH", "test-th-2")
    after = await svc.get_summary("TH")
    assert after["claimed"] == 2
    assert after["remaining"] == 98


async def test_capacity_exhaustion_returns_none():
    """101st brand in same country gets None (no slot)."""
    cc = "TZ"  # Tanzania — fresh country
    await _cleanup(cc)
    # Fill all 100
    for i in range(100):
        await svc.claim_slot(cc, f"test-tz-{i}")
    # 101st should fail
    over = await svc.claim_slot(cc, "test-tz-overflow")
    assert over is None


async def test_is_founding_after_claim():
    await _cleanup("VN", "test-vn-founding")
    assert await svc.is_founding("test-vn-founding") is False
    await svc.claim_slot("VN", "test-vn-founding")
    assert await svc.is_founding("test-vn-founding") is True


async def test_release_frees_slot():
    cc = "MM"  # Myanmar
    await _cleanup(cc, "test-mm-churn")
    await svc.claim_slot(cc, "test-mm-churn")
    assert (await svc.get_summary(cc))["claimed"] == 1

    released = await svc.release_slot("test-mm-churn")
    assert released == 1
    assert (await svc.get_summary(cc))["claimed"] == 0
    # Now another brand can claim
    assert await svc.claim_slot(cc, "test-mm-replacement") is not None


async def test_country_code_normalization():
    await _cleanup("PH", "test-ph-lc")
    # Lowercase input → normalized to upper
    c = await svc.claim_slot("ph", "test-ph-lc")
    assert c is not None
    assert c.country_code == "PH"


async def test_invalid_country_returns_none():
    c = await svc.claim_slot("", "test-empty")
    assert c is None
    c = await svc.claim_slot("SG", "")
    assert c is None


async def test_list_open_countries_returns_sorted():
    open_list = await svc.list_open_countries(limit=5)
    assert len(open_list) <= 5
    # Sorted by remaining desc
    remaining = [c["remaining"] for c in open_list]
    assert remaining == sorted(remaining, reverse=True)


# ── HTTP API ───────────────────────────────────────────────────────────


async def test_get_summary_endpoint():
    from app.main import app
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/api/v1/country-slots/KR")
    assert r.status_code == 200
    data = r.json()
    assert data["country_code"] == "KR"
    assert data["total"] == 100
    assert 0 <= data["claimed"] <= 100
    assert data["remaining"] == data["total"] - data["claimed"]


async def test_get_summary_rejects_bad_country_code():
    from app.main import app
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/api/v1/country-slots/SGP")  # 3 chars
    assert r.status_code == 400


async def test_claim_endpoint_succeeds():
    cc = "LK"  # Sri Lanka — fresh
    await _cleanup(cc, "http-test-lk-1")

    from app.main import app
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/api/v1/country-slots/claim",
            json={"country_code": cc, "brand_id": "http-test-lk-1"},
        )
    assert r.status_code == 201
    data = r.json()
    assert data["brand_id"] == "http-test-lk-1"
    assert data["founding"] is True
    assert data["take_rate_bps"] == 0


async def test_claim_endpoint_409_when_full():
    cc = "KH"  # Cambodia
    await _cleanup(cc)
    # Fill via service
    for i in range(100):
        await svc.claim_slot(cc, f"khfill-{i}")

    from app.main import app
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/api/v1/country-slots/claim",
            json={"country_code": cc, "brand_id": "kh-overflow"},
        )
    assert r.status_code == 409
    assert "100 founding slots" in r.json()["detail"]


async def test_brand_founding_status_endpoint():
    cc = "AE"
    await _cleanup(cc, "ae-status-merchant")
    await svc.claim_slot(cc, "ae-status-merchant")

    from app.main import app
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/api/v1/country-slots/brand/ae-status-merchant/status")
    assert r.status_code == 200
    data = r.json()
    assert data["is_founding"] is True
    assert data["take_rate_bps"] == 0


async def test_brand_not_founding_default():
    from app.main import app
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/api/v1/country-slots/brand/never-claimed-merchant/status")
    assert r.status_code == 200
    data = r.json()
    assert data["is_founding"] is False
    assert data["take_rate_bps"] == 500  # 5% default
