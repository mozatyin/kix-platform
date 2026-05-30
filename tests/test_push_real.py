"""Tests for real-mode FCM/APNS push integration.

All tests run in **mock mode** — we never call live Firebase. The point
is to verify:

  * client surface (send/multicast/topics) returns the expected shape,
  * worker drains the queue and records delivery state,
  * rate-limit kicks in at the configured threshold,
  * stale-token cleanup, frequency-cap, idempotency, retry on transient
    failure, and click tracking all work as documented.

The last 3 tests use ``monkeypatch`` to swap in fake Firebase functions
so we can assert the integration contract (params we pass, errors we
surface) without burning real quota.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from app.services import apns_client, fcm_client
from app.workers import push_worker


# ── Helpers ───────────────────────────────────────────────────────────────


def _valid_token(seed: int = 0) -> str:
    """Return a structurally-valid FCM-shaped token."""
    return (
        "dEadBeEf" * 6 + f"_kix_{seed:08x}"
    )  # 48 + ~13 chars, no whitespace


@pytest_asyncio.fixture(loop_scope="session", autouse=True)
async def _reset_mock_state(clean_redis):
    """Reset module-local mock counters before each test."""
    fcm_client._reset_mock_stats()
    yield


# ── Client surface tests ──────────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_send_to_token_returns_expected_shape(clean_redis):
    """Single-token send in mock mode returns a plausible response."""
    result = await fcm_client.send_to_token(
        token=_valid_token(1),
        title="Hello",
        body="World",
    )
    assert result["success"] is True
    assert result["mode"] == "mock"
    assert "message_id" in result
    assert result["title"] == "Hello"
    assert result["token_prefix"] == _valid_token(1)[:8]


@pytest.mark.asyncio(loop_scope="session")
async def test_invalid_token_detected(clean_redis):
    """Token validation rejects too-short / whitespace tokens."""
    assert fcm_client.validate_token("") is False
    assert fcm_client.validate_token("short") is False
    assert fcm_client.validate_token("has whitespace " * 5) is False
    assert fcm_client.validate_token(_valid_token(2)) is True

    result = await fcm_client.send_to_token("short", "t", "b")
    assert result["success"] is False
    assert result["error"] == "invalid_token"
    assert result["stale"] is True


@pytest.mark.asyncio(loop_scope="session")
async def test_multicast_batches_at_500(clean_redis):
    """Multicast > 500 tokens splits into multiple batches."""
    tokens = [_valid_token(i) for i in range(1234)]
    result = await fcm_client.send_multicast(
        tokens=tokens, title="bcast", body="hi",
    )
    assert result["success"] is True
    assert result["success_count"] == 1234
    # 1234 / 500 = 2.468 → 3 batches
    assert result["batches_sent"] == 3
    assert len(result["responses"]) == 1234


@pytest.mark.asyncio(loop_scope="session")
async def test_topic_subscribe_unsubscribe(clean_redis):
    """Subscribe and unsubscribe both succeed in mock mode."""
    tok = _valid_token(3)
    sub = await fcm_client.subscribe_to_topic([tok], "brand-toast-box")
    assert sub["success"] is True
    assert sub["success_count"] == 1
    assert sub["topic"] == "brand-toast-box"

    unsub = await fcm_client.unsubscribe_from_topic([tok], "brand-toast-box")
    assert unsub["success"] is True

    # Invalid topic name rejected.
    bad = await fcm_client.subscribe_to_topic([tok], "has:colon")
    assert bad["success"] is False
    assert bad["error"] == "invalid_topic_chars"


@pytest.mark.asyncio(loop_scope="session")
async def test_rate_limit_kicks_in(clean_redis, monkeypatch):
    """Lower the cap so the quota guard fires at a manageable count."""
    monkeypatch.setattr(fcm_client, "MAX_PUSHES_PER_HOUR", 5)

    tok = _valid_token(4)
    results = [
        await fcm_client.send_to_token(tok, "t", "b") for _ in range(7)
    ]
    successes = [r for r in results if r.get("success")]
    failures = [r for r in results if not r.get("success")]
    assert len(successes) == 5
    assert len(failures) == 2
    assert all(r["error"] == "rate_limited" for r in failures)
    assert failures[0]["retry_after_s"] > 0


@pytest.mark.asyncio(loop_scope="session")
async def test_topic_broadcast_via_send_to_topic(clean_redis):
    """send_to_topic returns a fanout response."""
    result = await fcm_client.send_to_topic(
        topic="news", title="t", body="b", data={"k": "v"},
    )
    assert result["success"] is True
    assert result["topic"] == "news"
    assert result["data"] == {"k": "v"}


# ── Worker tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_worker_drains_queue_and_records_delivery(clean_redis):
    """End-to-end: register device, enqueue push, worker delivers."""
    r = clean_redis
    kid = "kid_test_drain"
    tok = _valid_token(10)

    device_id = await push_worker.device_register(
        r, kid=kid, platform="android", token=tok,
    )
    assert device_id

    push_id = "push_test_drain"
    await r.hset(
        f"push:{push_id}",
        mapping={"push_id": push_id, "kid": kid, "title": "T", "body": "B"},
    )
    # Enqueue onto the correct shard.
    await r.lpush(push_worker.outbound_queue_key(push_id), push_id)

    result = await push_worker.run_once()
    assert result["delivered"] == 1
    assert result["failed"] == 0

    # Delivery state recorded.
    delivery_status = await r.get(
        push_worker.DELIVERY_KEY.format(push_id=push_id)
    )
    assert delivery_status == "sent"


@pytest.mark.asyncio(loop_scope="session")
async def test_worker_idempotency_on_duplicate_push_id(clean_redis):
    """Re-enqueuing the same push_id is rejected by the idempotency guard."""
    r = clean_redis
    kid = "kid_idemp"
    tok = _valid_token(11)
    await push_worker.device_register(r, kid=kid, platform="android", token=tok)

    push_id = "push_idemp_1"
    await r.hset(f"push:{push_id}", mapping={"push_id": push_id, "kid": kid})
    payload = {"push_id": push_id, "kid": kid, "title": "x", "body": "y"}

    # First call delivers.
    first = await push_worker.deliver_push(payload)
    assert first["success"] is True

    # Second call is short-circuited.
    second = await push_worker.deliver_push(payload)
    assert second["success"] is False
    assert second.get("idempotent") is True


@pytest.mark.asyncio(loop_scope="session")
async def test_worker_stale_token_cleanup(clean_redis, monkeypatch):
    """Stale tokens reported by FCM are deactivated + removed from kid set."""
    r = clean_redis
    kid = "kid_stale"
    tok = _valid_token(12)
    device_id = await push_worker.device_register(
        r, kid=kid, platform="android", token=tok,
    )

    # Patch _send_to_platform to report stale.
    async def _fake_send(platform, token, payload):
        return {
            "success": False,
            "error": "UnregisteredError",
            "stale": True,
            "platform": platform,
            "attempts": 1,
        }
    monkeypatch.setattr(push_worker, "_send_to_platform", _fake_send)

    res = await push_worker.deliver_push(
        {"push_id": "px_stale", "kid": kid, "title": "t", "body": "b"}
    )
    assert res["success"] is False
    assert res["stale_cleaned"] == 1

    # Device record marked inactive + removed from kid set.
    dev = await r.hgetall(f"push_device:{device_id}")
    assert dev.get("active") == "0"
    assert not await r.sismember(f"kid:{kid}:push_devices", device_id)


@pytest.mark.asyncio(loop_scope="session")
async def test_worker_retry_on_transient_failure(clean_redis, monkeypatch):
    """Worker's inline retry loop retries up to INLINE_RETRY_ATTEMPTS."""
    call_count = {"n": 0}

    async def _flaky_send(token, title, body, **kw):
        call_count["n"] += 1
        if call_count["n"] < 3:
            return {"success": False, "error": "transient_network", "mode": "mock"}
        return {"success": True, "mode": "mock", "message_id": "ok"}

    monkeypatch.setattr(fcm_client, "send_to_token", _flaky_send)
    # Shorten sleep so the test is fast.
    monkeypatch.setattr(push_worker, "INLINE_RETRY_BASE_SLEEP", 0.001)

    result = await push_worker._send_to_platform(
        "android", _valid_token(20), {"title": "t", "body": "b"}
    )
    assert result["success"] is True
    assert result["attempts"] == 3


@pytest.mark.asyncio(loop_scope="session")
async def test_click_tracking(clean_redis):
    """record_click writes the clicked state + timestamp."""
    r = clean_redis
    push_id = "push_click_1"
    ok = await push_worker.record_click(push_id, ts=1234567890.0)
    assert ok is True
    state = await r.get(push_worker.DELIVERY_KEY.format(push_id=push_id))
    assert state == "clicked"
    click_ts = await r.get(f"push:click:{push_id}")
    assert click_ts == "1234567890.0"


# ── HTTP endpoint tests ───────────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_register_token_endpoint(clean_redis, client):
    """POST /api/v1/push/register-token registers a device."""
    resp = await client.post(
        "/api/v1/push/register-token",
        json={
            "kid": "kid_http_reg",
            "platform": "android",
            "token": _valid_token(30),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "registered"
    assert body["device_id"].startswith("pd_")
    assert body["mode"] in ("mock", "live")


@pytest.mark.asyncio(loop_scope="session")
async def test_register_token_rejects_bad_token(clean_redis, client):
    """Bad token format returns 400."""
    resp = await client.post(
        "/api/v1/push/register-token",
        json={"kid": "kid_x", "platform": "android", "token": "tooshort"},
    )
    assert resp.status_code == 400
    assert "invalid push token" in resp.json()["detail"]


@pytest.mark.asyncio(loop_scope="session")
async def test_topic_subscribe_endpoint(clean_redis, client):
    """POST /api/v1/push/topic/subscribe stores membership on our side."""
    kid = "kid_topic_sub"
    # Register first so there are tokens to subscribe.
    await client.post(
        "/api/v1/push/register-token",
        json={"kid": kid, "platform": "android", "token": _valid_token(40)},
    )

    resp = await client.post(
        "/api/v1/push/topic/subscribe",
        json={"kid": kid, "topic": "brand-broadcasts"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["success_count"] == 1
    assert body["topic"] == "brand-broadcasts"

    # List endpoint shows the topic.
    lst = await client.get(f"/api/v1/push/topic/list?kid={kid}")
    assert "brand-broadcasts" in lst.json()["topics"]


@pytest.mark.asyncio(loop_scope="session")
async def test_topic_broadcast_endpoint(clean_redis, client):
    """POST /api/v1/push/topic/{topic}/broadcast fans out via FCM."""
    resp = await client.post(
        "/api/v1/push/topic/brand-broadcasts-toast-box/broadcast",
        json={"title": "New offer!", "body": "20% off today"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["topic"] == "brand-broadcasts-toast-box"
    assert body["mode"] in ("mock", "live")


@pytest.mark.asyncio(loop_scope="session")
async def test_push_health_endpoint(clean_redis, client):
    """GET /api/v1/push/health surfaces mode + counters."""
    # Trigger one send so last_sent_ts is populated.
    await fcm_client.send_to_token(_valid_token(50), "t", "b")
    resp = await client.get("/api/v1/push/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "mode" in body
    assert "configured" in body
    assert body["rate_limit_per_hour"] == fcm_client.MAX_PUSHES_PER_HOUR
    assert body["last_sent_ts"] is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_apns_client_delegates_to_fcm(clean_redis):
    """APNS client routes through FCM (badge/sound get forwarded)."""
    result = await apns_client.send_to_token(
        token=_valid_token(60), title="t", body="b", badge=3, sound="bell.caf",
    )
    assert result["success"] is True
    assert result["badge"] == 3
    assert result["sound"] == "bell.caf"


# ── Contract tests (verify Firebase API call shape) ───────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_contract_send_passes_data_payload(clean_redis, monkeypatch):
    """Worker forwards push_id + deep_link as data fields to the client."""
    captured: dict[str, object] = {}

    async def _spy(token, title, body, **kw):
        captured["token"] = token
        captured["title"] = title
        captured["body"] = body
        captured["data"] = kw.get("data")
        captured["badge"] = kw.get("badge")
        captured["platform"] = kw.get("platform")
        return {"success": True, "mode": "mock", "message_id": "spy"}

    monkeypatch.setattr(fcm_client, "send_to_token", _spy)

    payload = {
        "push_id": "push_contract_1",
        "title": "T",
        "body": "B",
        "deep_link": "kix://offer/123",
        "brand_id": "brand_x",
    }
    result = await push_worker._send_to_platform(
        "ios", _valid_token(70), payload
    )
    assert result["success"] is True
    assert captured["data"]["push_id"] == "push_contract_1"
    assert captured["data"]["deep_link"] == "kix://offer/123"
    assert captured["data"]["brand_id"] == "brand_x"
    assert captured["badge"] == 1   # ios gets default badge=1
    assert captured["platform"] == "ios"


@pytest.mark.asyncio(loop_scope="session")
async def test_contract_no_simulated_field_in_envelope(clean_redis):
    """Regression: ensure the legacy ``simulated: True`` flag is gone."""
    result = await push_worker._send_to_platform(
        "android", _valid_token(71), {"title": "t", "body": "b"}
    )
    # The new real-mode envelope uses ``mode`` (mock|live), not the old
    # ``simulated`` boolean. Gap analysis R2 flagged this as the
    # single-most-visible "we're not really pushing" symptom.
    assert "simulated" not in result
    assert result.get("mode") in ("mock", "live")


@pytest.mark.asyncio(loop_scope="session")
async def test_contract_frequency_cap_blocks_via_rate_limit(
    clean_redis, monkeypatch
):
    """Quota guard returns rate_limited error envelope that pipeline can detect."""
    monkeypatch.setattr(fcm_client, "MAX_PUSHES_PER_HOUR", 1)
    tok = _valid_token(80)
    first = await fcm_client.send_to_token(tok, "t", "b")
    assert first["success"] is True
    second = await fcm_client.send_to_token(tok, "t", "b")
    assert second["success"] is False
    assert second["error"] == "rate_limited"
    # Downstream worker logs this so structured-log search works.
    assert "retry_after_s" in second
