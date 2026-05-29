"""Stripe webhook idempotency tests.

Focus: two-phase ``processing → completed`` claim must keep money-flow
side-effects exactly-once even when:

  - the same event is delivered twice concurrently (different instances)
  - the handler crashes mid-flight (TTL must release the claim)
  - the handler completes (long-TTL ``completed`` marker rejects retries)

Signature verification is bypassed via monkeypatch of
``stripe.Webhook.construct_event`` — we are exercising the idempotency
state machine, not Stripe's HMAC.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
import stripe

from app.routers import stripe_webhook as sw
from app.routers.wallet import _k_balance


# ── Helpers ──────────────────────────────────────────────────────────────
def _make_event(event_id: str, brand_id: str, amount: int = 5_000) -> dict[str, Any]:
    """Build a minimal payment_intent.succeeded event."""
    return {
        "id": event_id,
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": f"pi_{uuid.uuid4().hex[:16]}",
                "amount": amount,
                "metadata": {"brand_id": brand_id, "reference_id": "ref_test"},
            }
        },
    }


@pytest.fixture
def stub_signature(monkeypatch):
    """Bypass Stripe HMAC verification; return whatever JSON the client posts."""

    def _fake_construct_event(payload, sig_header, secret):  # noqa: ARG001
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8")
        return json.loads(payload)

    monkeypatch.setattr(stripe.Webhook, "construct_event", _fake_construct_event)
    monkeypatch.setattr(sw, "STRIPE_WEBHOOK_SECRET", "whsec_test_fixture")
    yield


# ── Single-shot success path ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_webhook_success_credits_wallet_and_marks_completed(
    client, clean_redis, stub_signature
):
    brand_id = "test_brand_webhook_single"
    event = _make_event("evt_single_001", brand_id, amount=7_500)

    res = await client.post(
        "/api/v1/webhooks/stripe",
        content=json.dumps(event),
        headers={"stripe-signature": "t=0,v1=stub", "content-type": "application/json"},
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"received": True, "event_type": "payment_intent.succeeded"}

    # Wallet credited exactly once.
    balance = int(await clean_redis.get(_k_balance(brand_id)) or 0)
    assert balance == 7_500

    # Idempotency marker promoted to "completed".
    state = await clean_redis.get(sw._k_event_seen("evt_single_001"))
    if isinstance(state, bytes):
        state = state.decode()
    assert state == sw.EVENT_STATE_COMPLETED


@pytest.mark.asyncio
async def test_webhook_sequential_duplicate_returns_duplicate_no_double_credit(
    client, clean_redis, stub_signature
):
    brand_id = "test_brand_webhook_dup"
    event = _make_event("evt_dup_001", brand_id, amount=3_000)
    body = json.dumps(event)
    headers = {"stripe-signature": "t=0,v1=stub", "content-type": "application/json"}

    res1 = await client.post("/api/v1/webhooks/stripe", content=body, headers=headers)
    res2 = await client.post("/api/v1/webhooks/stripe", content=body, headers=headers)

    assert res1.status_code == 200
    assert res2.status_code == 200
    assert res1.json().get("duplicate") is not True
    assert res2.json().get("duplicate") is True

    balance = int(await clean_redis.get(_k_balance(brand_id)) or 0)
    assert balance == 3_000, "duplicate webhook must NOT double-credit"


# ── The headline race: two concurrent deliveries ─────────────────────────
@pytest.mark.asyncio
async def test_webhook_concurrent_same_event(client, clean_redis, stub_signature):
    """Two parallel deliveries of the same event_id.

    Expectation:
      - Wallet balance increases by ``amount`` exactly once.
      - One response is the success path (200, no ``duplicate`` flag).
      - The other is either 503 (loser sees "processing") OR
        200+duplicate=True (loser arrived after winner promoted to
        "completed"). Both outcomes are race-safe under the contract.
    """
    brand_id = "test_brand_webhook_race"
    amount = 9_999
    event = _make_event("evt_race_001", brand_id, amount=amount)
    body = json.dumps(event)
    headers = {"stripe-signature": "t=0,v1=stub", "content-type": "application/json"}

    async def fire():
        return await client.post(
            "/api/v1/webhooks/stripe", content=body, headers=headers
        )

    r1, r2 = await asyncio.gather(fire(), fire(), return_exceptions=False)
    statuses = sorted([r1.status_code, r2.status_code])

    # Allowed outcome shapes:
    #   (200, 200) — winner success + loser saw "completed"
    #   (200, 503) — winner success + loser saw "processing"
    assert statuses in ([200, 200], [200, 503]), (
        f"unexpected status pair {statuses}: {r1.text!r} / {r2.text!r}"
    )

    if statuses == [200, 200]:
        payloads = [r1.json(), r2.json()]
        success = [p for p in payloads if not p.get("duplicate")]
        duplicate = [p for p in payloads if p.get("duplicate")]
        assert len(success) == 1, f"exactly one success expected, got {payloads}"
        assert len(duplicate) == 1, f"exactly one duplicate expected, got {payloads}"

    # The load-bearing invariant: side-effect is exactly-once.
    balance = int(await clean_redis.get(_k_balance(brand_id)) or 0)
    assert balance == amount, (
        f"wallet must credit exactly once under concurrency; got {balance}"
    )

    # Final state must be "completed" (winner promoted it).
    state = await clean_redis.get(sw._k_event_seen("evt_race_001"))
    if isinstance(state, bytes):
        state = state.decode()
    assert state == sw.EVENT_STATE_COMPLETED


# ── Crash recovery: handler failure releases the claim ───────────────────
@pytest.mark.asyncio
async def test_webhook_handler_failure_releases_claim_for_retry(
    client, clean_redis, stub_signature, monkeypatch
):
    """If the handler raises, the ``processing`` claim must be deleted so
    Stripe's retry can re-process the event."""
    brand_id = "test_brand_webhook_crash"
    event = _make_event("evt_crash_001", brand_id, amount=4_200)
    body = json.dumps(event)
    headers = {"stripe-signature": "t=0,v1=stub", "content-type": "application/json"}

    # Force the success handler to blow up on the first call.
    calls = {"n": 0}
    real_handler = sw._handle_payment_succeeded

    async def flaky(r, obj, evt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated downstream outage")
        return await real_handler(r, obj, evt)

    monkeypatch.setattr(sw, "_handle_payment_succeeded", flaky)

    res1 = await client.post("/api/v1/webhooks/stripe", content=body, headers=headers)
    assert res1.status_code == 500, res1.text

    # Claim must have been released (so a retry doesn't see "processing"
    # forever, and doesn't get short-circuited as "completed").
    assert await clean_redis.get(sw._k_event_seen("evt_crash_001")) is None

    # Stripe retry: should succeed and credit the wallet exactly once.
    res2 = await client.post("/api/v1/webhooks/stripe", content=body, headers=headers)
    assert res2.status_code == 200, res2.text
    assert res2.json().get("duplicate") is not True

    balance = int(await clean_redis.get(_k_balance(brand_id)) or 0)
    assert balance == 4_200, "retry after crash must credit exactly once"


# ── Audit trail ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_webhook_writes_audit_entry(client, clean_redis, stub_signature):
    brand_id = "test_brand_webhook_audit"
    event = _make_event("evt_audit_001", brand_id, amount=100)
    res = await client.post(
        "/api/v1/webhooks/stripe",
        content=json.dumps(event),
        headers={"stripe-signature": "t=0,v1=stub", "content-type": "application/json"},
    )
    assert res.status_code == 200

    entries = await clean_redis.lrange(sw._k_audit(), 0, -1)
    assert entries, "expected at least one audit entry"
    head = entries[0]
    if isinstance(head, bytes):
        head = head.decode()
    parsed = json.loads(head)
    assert parsed["event_id"] == "evt_audit_001"
    assert parsed["type"] == "payment_intent.succeeded"
    assert "received_at" in parsed
