"""WhatsApp OTP service + router tests.

Covers the no-DB paths (request-otp, expired/wrong code, rate limit,
locale switching, mock-mode determinism, health) end-to-end via the
ASGI client; covers verify-otp at the service layer directly so we
don't need a PG fixture for the happy path.

Mock mode is forced by ensuring ``WHATSAPP_API_TOKEN`` is unset in the
test process — the autouse ``_env_isolation`` fixture in conftest
restores env between tests so we never bleed real creds into a test.
"""

from __future__ import annotations

import json
import os

import pytest

from app.services import whatsapp_otp


# ── Helpers ───────────────────────────────────────────────────────────────


async def _seed_brand(redis, brand_id: str = "b_wa_test") -> str:
    """Write a minimal brand config so get_brand_config does not 404."""
    cfg = {
        "brand_id": brand_id,
        "energy": {
            "cap": 100,
            "regen_per_hour": 10,
            "welcome_back_threshold_hours": 12,
            "welcome_back_bonus": 5,
        },
    }
    await redis.set(f"config:{brand_id}", json.dumps(cfg))
    return brand_id


@pytest.fixture(autouse=True)
def _force_mock_mode(monkeypatch):
    """Defence-in-depth: no test ever touches the live WhatsApp API."""
    monkeypatch.delenv("WHATSAPP_API_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    yield


# ── Service-level: pure unit tests ────────────────────────────────────────


def test_normalise_phone_accepts_common_formats():
    assert whatsapp_otp.normalise_phone("+65 9123-4567") == "+6591234567"
    assert whatsapp_otp.normalise_phone("0065 91234567") == "+6591234567"
    assert whatsapp_otp.normalise_phone("(65) 9123 4567") == "+6591234567"


def test_normalise_phone_rejects_garbage():
    with pytest.raises(ValueError):
        whatsapp_otp.normalise_phone("not-a-phone")
    with pytest.raises(ValueError):
        whatsapp_otp.normalise_phone("")


def test_render_message_for_all_supported_locales():
    code = "123456"
    en = whatsapp_otp.render_message(code, "en")
    zh = whatsapp_otp.render_message(code, "zh")
    ms = whatsapp_otp.render_message(code, "ms")
    id_ = whatsapp_otp.render_message(code, "id")
    th = whatsapp_otp.render_message(code, "th")
    vi = whatsapp_otp.render_message(code, "vi")
    assert "KiX" in en and "123456" in en
    assert "验证码" in zh and "5 分钟" in zh
    assert "Kod KiX" in ms
    assert "Kode KiX" in id_
    assert "KiX" in th and "123456" in th
    assert "KiX" in vi and "5 phút" in vi


def test_render_message_falls_back_for_unknown_locale():
    msg = whatsapp_otp.render_message("000000", "xx-YY")
    assert msg.startswith("Your KiX code")  # English fallback


def test_render_message_strips_bcp47_region_to_base():
    assert whatsapp_otp.render_message("111111", "zh-Hans-CN").startswith(
        "您的 KiX 验证码"
    )


def test_get_mode_defaults_to_mock():
    assert whatsapp_otp.get_mode() == "mock"


def test_get_mode_live_when_both_env_vars_set(monkeypatch):
    monkeypatch.setenv("WHATSAPP_API_TOKEN", "fake-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1234567890")
    assert whatsapp_otp.get_mode() == "live"


# ── Service-level: redis-bound flows ──────────────────────────────────────


@pytest.mark.asyncio
async def test_send_then_verify_mock_mode_roundtrip(clean_redis):
    res = await whatsapp_otp.send_otp(
        clean_redis, "+6591234567", "en", brand_id="b_wa_test"
    )
    assert res["status"] == "sent"
    assert res["mode"] == "mock"
    assert res["phone"] == "+6591234567"
    assert res["expires_in"] == 300
    # Mock mode echoes the code — that's the whole point.
    assert "debug_code" in res
    assert len(res["debug_code"]) == 6
    assert res["debug_code"].isdigit()

    verified = await whatsapp_otp.verify_otp(
        clean_redis,
        "+6591234567",
        res["debug_code"],
        brand_id="b_wa_test",
    )
    assert verified["status"] == "verified"
    assert verified["phone"] == "+6591234567"
    assert verified["short_token"]
    assert verified["expires_in"] == 600


@pytest.mark.asyncio
async def test_verify_otp_rejects_wrong_code(clean_redis):
    await whatsapp_otp.send_otp(
        clean_redis, "+6591234567", brand_id="b_wa_test"
    )
    with pytest.raises(ValueError, match="invalid code"):
        await whatsapp_otp.verify_otp(
            clean_redis, "+6591234567", "000000", brand_id="b_wa_test"
        )


@pytest.mark.asyncio
async def test_verify_otp_rejects_expired_or_unknown(clean_redis):
    # No send first.
    with pytest.raises(ValueError, match="expired or never sent"):
        await whatsapp_otp.verify_otp(
            clean_redis, "+6591234567", "123456", brand_id="b_wa_test"
        )


@pytest.mark.asyncio
async def test_verify_otp_locks_after_max_attempts(clean_redis):
    await whatsapp_otp.send_otp(
        clean_redis, "+6591234567", brand_id="b_wa_test"
    )
    # Burn through MAX_VERIFY_ATTEMPTS with wrong codes.
    for _ in range(5):
        with pytest.raises(ValueError):
            await whatsapp_otp.verify_otp(
                clean_redis,
                "+6591234567",
                "000000",
                brand_id="b_wa_test",
            )
    # Next attempt (even correct hypothetically) hits lockout
    with pytest.raises(ValueError, match="too many"):
        await whatsapp_otp.verify_otp(
            clean_redis,
            "+6591234567",
            "111111",
            brand_id="b_wa_test",
        )


@pytest.mark.asyncio
async def test_send_otp_rate_limit_triggers_after_three(clean_redis):
    phone = "+6597777777"
    for _ in range(3):
        await whatsapp_otp.send_otp(
            clean_redis, phone, brand_id="b_wa_test"
        )
    with pytest.raises(PermissionError, match="rate limit"):
        await whatsapp_otp.send_otp(
            clean_redis, phone, brand_id="b_wa_test"
        )


@pytest.mark.asyncio
async def test_short_token_is_single_use(clean_redis):
    res = await whatsapp_otp.send_otp(
        clean_redis, "+6593333333", brand_id="b_wa_test"
    )
    v = await whatsapp_otp.verify_otp(
        clean_redis,
        "+6593333333",
        res["debug_code"],
        brand_id="b_wa_test",
    )
    tok = v["short_token"]
    first = await whatsapp_otp.consume_short_token(clean_redis, tok)
    assert first is not None
    assert first["phone"] == "+6593333333"
    # Single-use — second consume is a miss.
    second = await whatsapp_otp.consume_short_token(clean_redis, tok)
    assert second is None


@pytest.mark.asyncio
async def test_deterministic_code_generator_for_tests(clean_redis):
    whatsapp_otp.set_code_generator(lambda: "424242")
    try:
        res = await whatsapp_otp.send_otp(
            clean_redis, "+6592222222", brand_id="b_wa_test"
        )
        assert res["debug_code"] == "424242"
    finally:
        whatsapp_otp.reset_code_generator()


@pytest.mark.asyncio
async def test_health_check_reports_mock_mode(clean_redis):
    info = await whatsapp_otp.health_check(clean_redis)
    assert info["service"] == "whatsapp_otp"
    assert info["mode"] == "mock"
    assert info["redis"] == "ok"
    assert "en" in info["supported_locales"]
    assert "zh" in info["supported_locales"]
    assert info["rate_limit_per_hour"] == 3


# ── Router-level: ASGI ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_otp_unknown_brand_is_404(client, clean_redis):
    res = await client.post(
        "/api/v1/auth/whatsapp/request-otp",
        json={"phone": "+6591234567", "brand_id": "b_unknown"},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_request_otp_missing_phone_is_422(client, clean_redis):
    res = await client.post(
        "/api/v1/auth/whatsapp/request-otp",
        json={"brand_id": "b_wa_test"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_request_otp_invalid_phone_is_422(client, clean_redis):
    await _seed_brand(clean_redis)
    res = await client.post(
        "/api/v1/auth/whatsapp/request-otp",
        json={"phone": "garbage", "brand_id": "b_wa_test"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_request_otp_mock_mode_returns_debug_code(client, clean_redis):
    await _seed_brand(clean_redis)
    res = await client.post(
        "/api/v1/auth/whatsapp/request-otp",
        json={
            "phone": "+6591234567",
            "brand_id": "b_wa_test",
            "locale": "en",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "mock"
    assert body["status"] == "sent"
    assert len(body["debug_code"]) == 6
    assert body["debug_message"].startswith("Your KiX code")


@pytest.mark.asyncio
async def test_request_otp_locale_switch_changes_message(client, clean_redis):
    await _seed_brand(clean_redis)
    res = await client.post(
        "/api/v1/auth/whatsapp/request-otp",
        json={
            "phone": "+6591234567",
            "brand_id": "b_wa_test",
            "locale": "zh",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert "验证码" in body["debug_message"]


@pytest.mark.asyncio
async def test_request_otp_rate_limit_returns_429(client, clean_redis):
    await _seed_brand(clean_redis)
    for _ in range(3):
        ok = await client.post(
            "/api/v1/auth/whatsapp/request-otp",
            json={"phone": "+6594444444", "brand_id": "b_wa_test"},
        )
        assert ok.status_code == 200
    blocked = await client.post(
        "/api/v1/auth/whatsapp/request-otp",
        json={"phone": "+6594444444", "brand_id": "b_wa_test"},
    )
    assert blocked.status_code == 429


@pytest.mark.asyncio
async def test_whatsapp_refresh_unknown_token_is_401(client, clean_redis):
    res = await client.post(
        "/api/v1/auth/whatsapp/refresh",
        json={"refresh_token": "rt_nope_xxxx"},
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_verify_otp_wrong_code_is_401(client, clean_redis):
    await _seed_brand(clean_redis)
    # Send a code first so we hit the "invalid code" branch, not "expired".
    await client.post(
        "/api/v1/auth/whatsapp/request-otp",
        json={"phone": "+6591111111", "brand_id": "b_wa_test"},
    )
    res = await client.post(
        "/api/v1/auth/whatsapp/verify-otp",
        json={
            "phone": "+6591111111",
            "code": "000000",
            "brand_id": "b_wa_test",
        },
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_health_endpoint_returns_mock_in_test_env(client, clean_redis):
    res = await client.get("/api/v1/auth/whatsapp/health")
    assert res.status_code == 200
    body = res.json()
    assert body["service"] == "whatsapp_otp"
    assert body["mode"] == "mock"
