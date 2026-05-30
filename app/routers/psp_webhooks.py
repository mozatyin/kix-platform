"""PSP webhook receiver — top 5 non-Stripe payment methods.

One endpoint per PSP for unambiguous routing + signature checking,
plus shared post-processing that:

  1. Verifies the signature via the PSP wrapper.
  2. Standardises the event into KiX canonical shape.
  3. De-duplicates by ``(psp, charge_id, event_type)``.
  4. Credits the brand wallet on ``charge.succeeded``.
  5. Emits an audit log entry.

The wallet credit re-uses the same Redis key shape that
:mod:`app.routers.wallet` and :mod:`app.routers.stripe_webhook`
use — we never import wallet's mutation API, only its key helpers.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Header, HTTPException, Request, status

from app.redis_client import get_redis
from app.services.payment_psps import all_psp_codes, get_psp_client
from app.services.payment_psps._common import emit_audit, read_audit_log

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Idempotency keys ─────────────────────────────────────────────────────
def _k_psp_event_seen(psp: str, charge_id: str, event_type: str) -> str:
    return f"psp:{psp}:event:{charge_id}:{event_type}"


def _k_psp_balance(brand_id: str) -> str:
    """Match :mod:`app.routers.wallet._k_balance` so we stay compatible."""
    return f"wallet:{brand_id}:balance_cents"


def _k_psp_tx(brand_id: str) -> str:
    """Match :mod:`app.routers.stripe_webhook` transaction list shape."""
    return f"wallet:{brand_id}:transactions"


# ── Shared processor ─────────────────────────────────────────────────────
async def _process_psp_webhook(
    psp_code: str,
    request: Request,
    signature: str,
) -> dict[str, Any]:
    """Common path: verify → parse → standardise → credit → audit."""
    client = get_psp_client(psp_code)
    raw_body = await request.body()

    try:
        event = client.verify_webhook(raw_body, signature)
    except ValueError as exc:
        logger.warning("[%s] webhook signature rejected: %s", psp_code, exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_signature", "psp": psp_code},
        )

    canonical = client.process_event(event)
    charge_id = canonical.get("charge_id") or ""
    event_type = canonical.get("event_type") or ""

    r: aioredis.Redis = await get_redis()

    # ── Idempotency: only credit once per (psp, charge_id, event_type) ──
    seen_key = _k_psp_event_seen(psp_code, charge_id, event_type)
    # SET NX with 30-day expiry; if the key already existed, this is a replay.
    claim = await r.set(seen_key, str(time.time()), nx=True, ex=60 * 60 * 24 * 30)
    if not claim:
        emit_audit(
            {
                "psp": psp_code,
                "action": "webhook_replay",
                "charge_id": charge_id,
                "event_type": event_type,
            }
        )
        return {
            "received": True,
            "psp": psp_code,
            "event_type": event_type,
            "duplicate": True,
        }

    # ── Money flow: only credit on success events ───────────────────────
    if event_type == "charge.succeeded":
        brand_id = canonical.get("brand_id")
        amount = int(canonical.get("amount_cents") or 0)
        if brand_id and amount > 0:
            await r.incrby(_k_psp_balance(brand_id), amount)
            await r.lpush(
                _k_psp_tx(brand_id),
                json.dumps(
                    {
                        "type": f"{psp_code}_charge",
                        "amount": amount,
                        "gateway_tx_id": charge_id,
                        "currency": canonical.get("currency"),
                        "reference_id": canonical.get("reference_id"),
                        "ts": time.time(),
                    },
                    ensure_ascii=False,
                ),
            )
            await r.ltrim(_k_psp_tx(brand_id), 0, 10_000)
            emit_audit(
                {
                    "psp": psp_code,
                    "action": "wallet_credit",
                    "brand_id": brand_id,
                    "amount": amount,
                    "charge_id": charge_id,
                }
            )
        else:
            logger.info(
                "[%s] charge.succeeded with no brand_id/amount; charge_id=%s",
                psp_code,
                charge_id,
            )

    # Refund / failure events: audit only, no wallet mutation.
    elif event_type in {"refund.succeeded", "charge.failed", "refund.failed"}:
        emit_audit(
            {
                "psp": psp_code,
                "action": f"event_{event_type}",
                "charge_id": charge_id,
                "amount": canonical.get("amount_cents"),
            }
        )

    return {
        "received": True,
        "psp": psp_code,
        "event_type": event_type,
        "charge_id": charge_id,
    }


# ─────────────────────────────────────────────────────────────────────────
# Per-PSP endpoints. Each just calls the shared processor with its own
# signature-header convention.
# ─────────────────────────────────────────────────────────────────────────
@router.post("/paynow")
async def paynow_webhook(
    request: Request,
    x_paynow_signature: str = Header("", alias="X-PayNow-Signature"),
) -> dict[str, Any]:
    return await _process_psp_webhook("paynow", request, x_paynow_signature)


@router.post("/grabpay")
async def grabpay_webhook(
    request: Request,
    x_grab_signature: str = Header("", alias="X-Grab-Signature"),
) -> dict[str, Any]:
    return await _process_psp_webhook("grabpay", request, x_grab_signature)


@router.post("/alipay")
async def alipay_webhook(
    request: Request,
    sign: str = Header("", alias="sign"),
) -> dict[str, Any]:
    return await _process_psp_webhook("alipay", request, sign)


@router.post("/wechat")
async def wechat_webhook(
    request: Request,
    wechatpay_signature: str = Header("", alias="Wechatpay-Signature"),
) -> dict[str, Any]:
    return await _process_psp_webhook("wechat_pay", request, wechatpay_signature)


@router.post("/ovo")
async def ovo_webhook(
    request: Request,
    x_ovo_signature: str = Header("", alias="X-OVO-Signature"),
) -> dict[str, Any]:
    return await _process_psp_webhook("ovo", request, x_ovo_signature)


# ─────────────────────────────────────────────────────────────────────────
# Health endpoints — surfaced at /api/v1/health/psp/...
# ─────────────────────────────────────────────────────────────────────────
health_router = APIRouter()


@health_router.get("/psp/all")
async def all_psp_health() -> dict[str, Any]:
    out: dict[str, Any] = {"psps": {}}
    for code in all_psp_codes():
        try:
            out["psps"][code] = get_psp_client(code).health_check()
        except Exception as exc:  # noqa: BLE001
            out["psps"][code] = {"psp": code, "ready": False, "error": str(exc)}
    out["overall_ready"] = all(p.get("ready") for p in out["psps"].values())
    return out


@health_router.get("/psp/{psp_code}")
async def one_psp_health(psp_code: str) -> dict[str, Any]:
    try:
        client = get_psp_client(psp_code)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "unknown_psp", "psp_code": psp_code},
        )
    return client.health_check()


# Audit-log read endpoint — useful for finance / SRE dashboards. Read-only.
@health_router.get("/psp/_audit")
async def psp_audit_tail(limit: int = 100) -> dict[str, Any]:
    log = read_audit_log()
    return {"count": len(log), "entries": log[-max(0, min(limit, 1000)):]}
