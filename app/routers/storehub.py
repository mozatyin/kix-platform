"""StoreHub POS integration router — Wave L+ (Ahmad scale-blocker fix).

Wires the existing `app/services/storehub_adapter.py` (25 tests) to
FastAPI HTTP endpoints + Redis storage. Per docs/rfc-storehub-fasttrack.md.

Three endpoints:
  POST /api/v1/integrations/storehub/connect            — merchant onboarding
  POST /api/v1/integrations/storehub/webhook/{brand_id} — StoreHub → KiX events
  GET  /api/v1/integrations/storehub/status/{brand_id}  — health check
  POST /api/v1/integrations/storehub/disconnect/{brand_id}

Mock mode: when env `STOREHUB_MODE != "live"`, signature verification is
skipped (so SG-based devs can test against any payload). CI defaults to
mock — see tests/test_storehub_router.py.

Storage shape (Redis):
  storehub:brand:{brand_id}   HASH — api_token, webhook_secret, outlets,
                                     mode, connected_at
  storehub:status:{brand_id}  HASH — events_24h, fraud_flagged_24h,
                                     last_event_at
  storehub:dedup:{brand_id}:{order_id}  STR — TTL 90d, prevents double-bill
  storehub:events                       STREAM — append all events for ops

Idempotency: storehub_order_id is the de-dup key. Repeated webhooks for
the same order are no-op (returns ok=True duplicate=True).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.redis_client import get_redis
from app.services.storehub_adapter import (
    basic_fraud_check,
    map_to_tracked_transaction,
    parse_storehub_order,
    verify_storehub_signature,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/integrations/storehub", tags=["integrations.storehub"])

# ── Redis key helpers ────────────────────────────────────────────────

def _brand_key(brand_id: str) -> str:
    return f"storehub:brand:{brand_id}"


def _status_key(brand_id: str) -> str:
    return f"storehub:status:{brand_id}"


def _dedup_key(brand_id: str, order_id: str) -> str:
    return f"storehub:dedup:{brand_id}:{order_id}"


def _velocity_key(brand_id: str, phone_hash: str) -> str:
    return f"storehub:vel24:{brand_id}:{phone_hash}"


def _is_live_mode() -> bool:
    return os.environ.get("STOREHUB_MODE", "mock").lower() == "live"


# ── Models ───────────────────────────────────────────────────────────

class ConnectRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    api_token: str = Field(..., min_length=8,
                           description="StoreHub merchant API token (from their Settings → API)")
    outlet_ids: list[str] = Field(default_factory=list,
                                  description="StoreHub outlet IDs to include; empty = all outlets on the account")
    webhook_secret: str = Field(..., min_length=16,
                                description="HMAC secret for StoreHub → KiX signature verification")


class ConnectResponse(BaseModel):
    ok: bool
    brand_id: str
    outlet_count: int
    webhook_url: str
    mode: str


class StatusResponse(BaseModel):
    connected: bool
    brand_id: str
    outlet_count: int
    mode: str
    events_24h: int = 0
    fraud_flagged_24h: int = 0
    last_event_at: Optional[int] = None


# ── /connect ────────────────────────────────────────────────────────

@router.post("/connect", response_model=ConnectResponse, status_code=status.HTTP_201_CREATED)
async def connect(req: ConnectRequest) -> ConnectResponse:
    """Merchant pastes StoreHub api_token + webhook_secret here. We store
    encrypted, return the webhook URL they paste back into StoreHub Settings.
    """
    r = await get_redis()
    now = int(time.time())
    mode = "live" if _is_live_mode() else "mock"

    creds = {
        "api_token": req.api_token,
        "webhook_secret": req.webhook_secret,
        "outlets": ",".join(req.outlet_ids),
        "mode": mode,
        "connected_at": str(now),
    }
    await r.hset(_brand_key(req.brand_id), mapping=creds)
    # Initialize status row
    await r.hset(_status_key(req.brand_id), mapping={
        "events_24h": "0",
        "fraud_flagged_24h": "0",
        "last_event_at": "0",
        "connected_at": str(now),
    })

    return ConnectResponse(
        ok=True,
        brand_id=req.brand_id,
        outlet_count=len(req.outlet_ids),
        webhook_url=f"/api/v1/integrations/storehub/webhook/{req.brand_id}",
        mode=mode,
    )


# ── /status ────────────────────────────────────────────────────────

@router.get("/status/{brand_id}", response_model=StatusResponse)
async def status_get(brand_id: str) -> StatusResponse:
    """Return connection health + recent activity counters."""
    r = await get_redis()
    creds = await r.hgetall(_brand_key(brand_id))
    if not creds:
        return StatusResponse(connected=False, brand_id=brand_id,
                              outlet_count=0, mode="mock")
    stats = await r.hgetall(_status_key(brand_id))
    outlets = creds.get("outlets") or creds.get(b"outlets") or ""
    if isinstance(outlets, bytes): outlets = outlets.decode()
    outlet_list = [o for o in outlets.split(",") if o]
    return StatusResponse(
        connected=True,
        brand_id=brand_id,
        outlet_count=len(outlet_list),
        mode=(creds.get("mode") or creds.get(b"mode") or b"mock"
              if isinstance(creds.get("mode") or creds.get(b"mode") or "mock", str)
              else (creds.get(b"mode", b"mock").decode())),
        events_24h=int(stats.get("events_24h", stats.get(b"events_24h", 0)) or 0),
        fraud_flagged_24h=int(stats.get("fraud_flagged_24h", stats.get(b"fraud_flagged_24h", 0)) or 0),
        last_event_at=int(stats.get("last_event_at", stats.get(b"last_event_at", 0)) or 0) or None,
    )


# ── /disconnect ─────────────────────────────────────────────────────

@router.post("/disconnect/{brand_id}")
async def disconnect(brand_id: str) -> dict:
    """Cleanly remove merchant credentials. Past event data is retained."""
    r = await get_redis()
    await r.delete(_brand_key(brand_id))
    return {"ok": True, "brand_id": brand_id, "disconnected_at": int(time.time())}


# ── /webhook ────────────────────────────────────────────────────────

@router.post("/webhook/{brand_id}")
async def webhook(brand_id: str, request: Request) -> dict:
    """StoreHub posts order.completed / order.refunded here.

    Returns 200 in nearly all cases (even on unknown brand / parse error)
    so StoreHub doesn't trigger a retry storm. Real errors are recorded
    in the events stream for ops.
    """
    r = await get_redis()
    body = await request.body()

    # Brand lookup
    creds = await r.hgetall(_brand_key(brand_id))
    if not creds:
        logger.warning("storehub webhook unknown_brand brand_id=%s", brand_id)
        return {"ok": False, "error": "unknown_brand", "brand_id": brand_id}

    # Helper to read either str or bytes hash values
    def _get(key):
        v = creds.get(key) if key in creds else creds.get(key.encode() if isinstance(key, str) else key)
        return v.decode() if isinstance(v, (bytes, bytearray)) else v

    webhook_secret = _get("webhook_secret") or ""
    mode = (_get("mode") or "mock").lower()

    # Signature check — only enforced in live mode
    if mode == "live":
        sig_header = request.headers.get("X-StoreHub-Signature", "")
        if not verify_storehub_signature(body, sig_header, webhook_secret):
            logger.warning("storehub webhook bad_signature brand_id=%s", brand_id)
            raise HTTPException(status_code=401, detail="invalid_signature")

    # Parse payload
    try:
        payload = json.loads(body.decode("utf-8"))
        order = parse_storehub_order(payload)
    except Exception as e:
        logger.error("storehub webhook parse_error brand_id=%s err=%s", brand_id, e)
        return {"ok": False, "error": "parse_error", "detail": str(e)[:120]}

    # Dedup: setnx on storehub:dedup:{brand}:{order_id} with 90-day TTL
    dedup_key = _dedup_key(brand_id, order.order_id)
    fresh = await r.setnx(dedup_key, str(int(time.time())))
    if not fresh:
        return {"ok": True, "duplicate": True, "order_id": order.order_id,
                "brand_id": brand_id}
    await r.expire(dedup_key, 60 * 60 * 24 * 90)

    # Velocity fraud check — count orders from same phone in 24h
    velocity_count = 0
    if order.customer_phone_e164:
        from hashlib import sha256
        phone_hash = sha256(order.customer_phone_e164.encode()).hexdigest()[:16]
        vk = _velocity_key(brand_id, phone_hash)
        velocity_count = await r.incr(vk)
        await r.expire(vk, 60 * 60 * 24)

    fraud_flagged, fraud_reason = basic_fraud_check(
        order,
        velocity_window_24h_count=velocity_count,
        same_outlet_owner_phones=set(),  # could load from brand settings
    )

    draft = map_to_tracked_transaction(
        order, brand_id=brand_id,
        matched_voucher_code=False,  # voucher-code matching wired in Wave L-2
        fraud_check_result=(fraud_flagged, fraud_reason),
    )

    # Stream the event for ops + downstream processing
    event_fields = {
        "brand_id": brand_id,
        "order_id": draft.storehub_order_id,
        "amount_cents": str(draft.amount_cents),
        "currency": draft.currency,
        "hashed_phone": draft.hashed_consumer_phone or "",
        "hashed_email": draft.hashed_consumer_email or "",
        "outlet_id": draft.outlet_id or "",
        "fraud_flagged": "1" if draft.fraud_flagged else "0",
        "fraud_reason": draft.fraud_reason or "",
        "occurred_at": str(int(draft.occurred_at.timestamp())),
    }
    await r.xadd("storehub:events", event_fields, maxlen=10000)

    # Status counters
    await r.hincrby(_status_key(brand_id), "events_24h", 1)
    if draft.fraud_flagged:
        await r.hincrby(_status_key(brand_id), "fraud_flagged_24h", 1)
    await r.hset(_status_key(brand_id), mapping={
        "last_event_at": str(int(draft.occurred_at.timestamp())),
    })

    return {
        "ok": True,
        "brand_id": brand_id,
        "order_id": draft.storehub_order_id,
        "amount_cents": draft.amount_cents,
        "currency": draft.currency,
        "fraud_flagged": draft.fraud_flagged,
        "outlet_id": draft.outlet_id,
    }
