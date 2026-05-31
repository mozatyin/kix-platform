"""Tests for app/routers/storehub.py — Wave L+ StoreHub integration router."""
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.routers.storehub import (
    ConnectRequest, _brand_key, _dedup_key, _status_key,
    connect, disconnect, status_get, webhook,
)


@pytest.fixture
def mock_redis():
    storage = {"hashes": {}, "strings": {}, "streams": []}
    r = AsyncMock()
    async def hset(key, mapping=None, **kw):
        m = mapping or kw
        if isinstance(m, dict):
            storage["hashes"].setdefault(key, {}).update({k: str(v) for k, v in m.items()})
        return len(m) if isinstance(m, dict) else 1
    async def hgetall(key):
        return storage["hashes"].get(key, {})
    async def hincrby(key, field, amount):
        storage["hashes"].setdefault(key, {})
        prev = int(storage["hashes"][key].get(field, 0))
        new = prev + amount
        storage["hashes"][key][field] = str(new)
        return new
    async def setnx(key, value):
        if key in storage["strings"]: return False
        storage["strings"][key] = str(value)
        return True
    async def expire(key, ttl): return True
    async def incr(key):
        prev = int(storage["strings"].get(key, 0))
        storage["strings"][key] = str(prev + 1)
        return prev + 1
    async def delete(key):
        deleted = 0
        if key in storage["hashes"]: del storage["hashes"][key]; deleted += 1
        if key in storage["strings"]: del storage["strings"][key]; deleted += 1
        return deleted
    async def xadd(stream, fields, maxlen=None):
        storage["streams"].append({"stream": stream, "fields": fields})
        return f"id-{len(storage['streams'])}"
    r.hset = AsyncMock(side_effect=hset)
    r.hgetall = AsyncMock(side_effect=hgetall)
    r.hincrby = AsyncMock(side_effect=hincrby)
    r.setnx = AsyncMock(side_effect=setnx)
    r.expire = AsyncMock(side_effect=expire)
    r.incr = AsyncMock(side_effect=incr)
    r.delete = AsyncMock(side_effect=delete)
    r.xadd = AsyncMock(side_effect=xadd)
    return r, storage


@pytest.fixture
def patched_redis(mock_redis, monkeypatch):
    r, storage = mock_redis
    async def fake_get_redis(): return r
    monkeypatch.setattr("app.routers.storehub.get_redis", fake_get_redis)
    return r, storage


def _make_request(body, headers=None):
    req = MagicMock()
    req.headers = headers or {}
    async def get_body(): return body
    req.body = get_body
    return req


def _signed_headers(body, secret):
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-StoreHub-Signature": f"sha256={sig}"}


# ── /connect ──

@pytest.mark.asyncio
async def test_connect_stores_credentials(patched_redis):
    r, storage = patched_redis
    req = ConnectRequest(brand_id="brand_abc", api_token="sh-token-xxxx-yyyy",
                         outlet_ids=["out-1", "out-2"], webhook_secret="secret-min-16-chars!")
    resp = await connect(req)
    assert resp.ok
    assert resp.outlet_count == 2
    assert resp.brand_id == "brand_abc"
    assert resp.webhook_url == "/api/v1/integrations/storehub/webhook/brand_abc"
    assert resp.mode in ("mock", "live")
    creds = storage["hashes"][_brand_key("brand_abc")]
    assert "api_token" in creds
    assert creds["outlets"] == "out-1,out-2"


@pytest.mark.asyncio
async def test_connect_initializes_status(patched_redis):
    r, storage = patched_redis
    await connect(ConnectRequest(brand_id="b", api_token="t" * 8,
                                  outlet_ids=[], webhook_secret="s" * 16))
    assert _status_key("b") in storage["hashes"]
    assert storage["hashes"][_status_key("b")]["events_24h"] == "0"


# ── /status ──

@pytest.mark.asyncio
async def test_status_unconnected_brand(patched_redis):
    r, storage = patched_redis
    resp = await status_get("never-connected")
    assert not resp.connected
    assert resp.outlet_count == 0
    assert resp.events_24h == 0


@pytest.mark.asyncio
async def test_status_connected_brand_shape(patched_redis):
    r, storage = patched_redis
    storage["hashes"][_brand_key("b1")] = {
        "api_token": "tok", "webhook_secret": "sec",
        "outlets": "o1,o2,o3", "mode": "mock", "connected_at": "1700000000",
    }
    storage["hashes"][_status_key("b1")] = {
        "events_24h": "12", "fraud_flagged_24h": "1", "last_event_at": "1700001234",
    }
    resp = await status_get("b1")
    assert resp.connected
    assert resp.outlet_count == 3
    assert resp.events_24h == 12
    assert resp.fraud_flagged_24h == 1
    assert resp.last_event_at == 1700001234


# ── /disconnect ──

@pytest.mark.asyncio
async def test_disconnect_removes_keys(patched_redis):
    r, storage = patched_redis
    storage["hashes"][_brand_key("b")] = {"api_token": "x"}
    storage["hashes"][_status_key("b")] = {"events_24h": "5"}
    resp = await disconnect("b")
    assert resp["ok"]
    assert _brand_key("b") not in storage["hashes"]


# ── /webhook ──

@pytest.mark.asyncio
async def test_webhook_unknown_brand_returns_ok_logs(patched_redis):
    body = json.dumps({"order_id": "X", "completed_at": "2026-05-31T10:00:00Z", "total": 5}).encode()
    req = _make_request(body)
    resp = await webhook("unknown-brand", req)
    assert resp == {"ok": False, "error": "unknown_brand", "brand_id": "unknown-brand"}


@pytest.mark.asyncio
async def test_webhook_happy_path_in_mock_mode(patched_redis, monkeypatch):
    r, storage = patched_redis
    monkeypatch.setenv("STOREHUB_MODE", "mock")
    storage["hashes"][_brand_key("b1")] = {
        "api_token": "tok", "webhook_secret": "secret-16-chars-abc",
        "outlets": "o1", "mode": "mock",
    }
    body = json.dumps({
        "order_id": "ORD-001", "completed_at": "2026-05-31T10:00:00Z",
        "total": 15.50, "currency": "SGD",
        "customer": {"phone": "+6591234567", "email": "a@b.com"},
        "outlet_id": "o1",
    }).encode()
    req = _make_request(body)
    resp = await webhook("b1", req)
    assert resp["ok"]
    assert resp["order_id"] == "ORD-001"
    assert resp["amount_cents"] == 1550
    assert len(storage["streams"]) == 1
    assert storage["streams"][0]["fields"]["order_id"] == "ORD-001"
    assert storage["hashes"][_status_key("b1")]["events_24h"] == "1"


@pytest.mark.asyncio
async def test_webhook_dedup_skips_duplicate(patched_redis, monkeypatch):
    r, storage = patched_redis
    monkeypatch.setenv("STOREHUB_MODE", "mock")
    storage["hashes"][_brand_key("b1")] = {
        "api_token": "tok", "webhook_secret": "secret-16-chars-abc",
        "outlets": "o1", "mode": "mock",
    }
    body = json.dumps({
        "order_id": "DUP-1", "completed_at": "2026-05-31T10:00:00Z",
        "total": 5.00, "customer": {"phone": "+6591234567"},
    }).encode()
    req1 = _make_request(body)
    r1 = await webhook("b1", req1)
    assert r1["ok"] and not r1.get("duplicate")
    req2 = _make_request(body)
    r2 = await webhook("b1", req2)
    assert r2["ok"] and r2.get("duplicate")
    assert len(storage["streams"]) == 1


@pytest.mark.asyncio
async def test_webhook_bad_signature_in_live_mode(patched_redis, monkeypatch):
    from fastapi import HTTPException
    r, storage = patched_redis
    monkeypatch.setenv("STOREHUB_MODE", "live")
    storage["hashes"][_brand_key("b1")] = {
        "api_token": "tok", "webhook_secret": "secret-16-chars-abc",
        "outlets": "o1", "mode": "live",
    }
    body = json.dumps({"order_id": "X", "completed_at": "2026-05-31T10:00:00Z", "total": 5}).encode()
    bad_sig_headers = {"X-StoreHub-Signature": "sha256=baddddddd"}
    req = _make_request(body, headers=bad_sig_headers)
    with pytest.raises(HTTPException) as exc:
        await webhook("b1", req)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_webhook_good_signature_in_live_mode(patched_redis, monkeypatch):
    r, storage = patched_redis
    monkeypatch.setenv("STOREHUB_MODE", "live")
    secret = "secret-16-chars-abc"
    storage["hashes"][_brand_key("b1")] = {
        "api_token": "tok", "webhook_secret": secret, "outlets": "o1", "mode": "live",
    }
    body = json.dumps({"order_id": "X1", "completed_at": "2026-05-31T10:00:00Z",
                       "total": 5, "customer": {"phone": "+6591234567"}}).encode()
    req = _make_request(body, headers=_signed_headers(body, secret))
    resp = await webhook("b1", req)
    assert resp["ok"]
    assert resp["order_id"] == "X1"


@pytest.mark.asyncio
async def test_webhook_velocity_fraud_flag(patched_redis, monkeypatch):
    r, storage = patched_redis
    monkeypatch.setenv("STOREHUB_MODE", "mock")
    storage["hashes"][_brand_key("b1")] = {
        "api_token": "tok", "webhook_secret": "secret-16-chars-abc",
        "outlets": "o1", "mode": "mock",
    }
    for i in range(4):
        body = json.dumps({
            "order_id": f"VEL-{i}", "completed_at": "2026-05-31T10:00:00Z",
            "total": 10.00, "customer": {"phone": "+6591234567"},
        }).encode()
        req = _make_request(body)
        await webhook("b1", req)
    last_event = storage["streams"][-1]
    assert last_event["fields"]["fraud_flagged"] == "1"
    assert "velocity" in last_event["fields"]["fraud_reason"]
    assert int(storage["hashes"][_status_key("b1")]["fraud_flagged_24h"]) >= 1


@pytest.mark.asyncio
async def test_webhook_invalid_json_returns_ok_logged(patched_redis, monkeypatch):
    r, storage = patched_redis
    monkeypatch.setenv("STOREHUB_MODE", "mock")
    storage["hashes"][_brand_key("b1")] = {
        "api_token": "tok", "webhook_secret": "secret-16-chars-abc",
        "outlets": "o1", "mode": "mock",
    }
    body = b"this is not json"
    req = _make_request(body)
    resp = await webhook("b1", req)
    assert not resp["ok"]
    assert resp["error"] == "parse_error"
