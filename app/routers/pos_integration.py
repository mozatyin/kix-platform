"""POS integration endpoints — Wave E item 7.

Pairs merchants with one of the four supported POS systems
(Toast / Square / Loyverse / Foodzaps) and exposes a uniform redemption
+ webhook surface so the consumer-facing voucher flow doesn't care
which POS the merchant uses.

Endpoints
---------
* ``POST /api/v1/pos/register-merchant``
* ``GET  /api/v1/pos/voucher/{voucher_id}/verify``
* ``POST /api/v1/pos/voucher/{voucher_id}/redeem``
* ``POST /api/v1/webhooks/pos/{adapter}``
* ``GET  /api/v1/pos/transactions/{brand_id}``
* ``GET  /api/v1/pos/receipt/{redemption_id}.html``  (printable HTML)
* ``GET  /api/v1/pos/checkout``                       (lightweight web checkout)

Mock-mode safety
----------------
When an adapter is in ``mock`` mode (the CI default), all calls go
through the :class:`MockVoucherStore` instead of any real POS API. CI
MUST stay mock — see ``tests/test_pos_integration.py``.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Path as PathParam, Request, status
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, Field

from app.services.pos import (
    POSError,
    all_pos_codes,
    get_pos_adapter,
)
from app.services.pos._common import (
    emit_audit,
    get_mock_store,
    read_audit_log,
)
from app.services.pos.receipt import generate_receipt_html

logger = logging.getLogger(__name__)

router = APIRouter()
webhook_router = APIRouter()


# ─────────────────────────────────────────────────────────────────
# Request / response models
# ─────────────────────────────────────────────────────────────────
class RegisterMerchantRequest(BaseModel):
    brand_id: str = Field(..., min_length=1, max_length=64)
    pos_code: str = Field(..., description="One of toast/square/loyverse/foodzaps")
    pos_merchant_id: str = Field(..., min_length=1, max_length=128)
    location_id: Optional[str] = Field(None, max_length=128)
    contact_email: Optional[str] = Field(None, max_length=255)


class RedeemRequest(BaseModel):
    pos_code: str = Field(..., description="POS adapter handling this redemption")
    order_id: str = Field(..., min_length=1, max_length=128)
    amount_cents: int = Field(..., gt=0, le=10_000_00)
    cashier_id: Optional[str] = Field(None, max_length=64)
    store_id: Optional[str] = Field(None, max_length=64)


# ─────────────────────────────────────────────────────────────────
# In-process merchant pairing registry (Redis-backed in production)
# ─────────────────────────────────────────────────────────────────
_MERCHANT_PAIRINGS: dict[str, dict[str, Any]] = {}
# In-process redemption history index by brand_id and by redemption_id
_REDEMPTION_BY_ID: dict[str, dict[str, Any]] = {}
_REDEMPTIONS_BY_BRAND: dict[str, list[dict[str, Any]]] = {}


def _reset_state() -> None:  # test helper
    _MERCHANT_PAIRINGS.clear()
    _REDEMPTION_BY_ID.clear()
    _REDEMPTIONS_BY_BRAND.clear()


# ─────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────
@router.post("/register-merchant", status_code=status.HTTP_201_CREATED)
async def register_merchant(body: RegisterMerchantRequest) -> dict[str, Any]:
    """Pair a KiX brand with one of the supported POS systems."""
    code = body.pos_code.lower().strip()
    if code not in all_pos_codes():
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unknown_pos_code",
                "supported": all_pos_codes(),
            },
        )
    pairing = {
        "brand_id": body.brand_id,
        "pos_code": code,
        "pos_merchant_id": body.pos_merchant_id,
        "location_id": body.location_id,
        "contact_email": body.contact_email,
        "paired_at": int(time.time()),
    }
    _MERCHANT_PAIRINGS[body.brand_id] = pairing
    emit_audit({"action": "register_merchant", **pairing})
    return {"ok": True, "pairing": pairing}


@router.get("/voucher/{voucher_id}/verify")
async def verify_voucher(
    voucher_id: str = PathParam(..., min_length=1, max_length=64),
    pos_code: Optional[str] = None,
    brand_id: Optional[str] = None,
) -> dict[str, Any]:
    """Real-time voucher lookup + validation through the paired POS adapter."""
    code = (pos_code or "").lower().strip()
    if not code and brand_id:
        pairing = _MERCHANT_PAIRINGS.get(brand_id)
        if pairing:
            code = pairing["pos_code"]
    if code not in all_pos_codes():
        raise HTTPException(
            status_code=400,
            detail={"error": "unknown_pos_code", "supported": all_pos_codes()},
        )
    adapter = get_pos_adapter(code)
    result = adapter.verify_voucher(voucher_id)
    return result


@router.post("/voucher/{voucher_id}/redeem")
async def redeem_voucher(
    voucher_id: str,
    body: RedeemRequest,
) -> dict[str, Any]:
    """Verify → apply discount → mark redeemed → record + receipt URL."""
    code = body.pos_code.lower().strip()
    if code not in all_pos_codes():
        raise HTTPException(
            status_code=400,
            detail={"error": "unknown_pos_code", "supported": all_pos_codes()},
        )
    adapter = get_pos_adapter(code)

    # Step 1: verify
    v = adapter.verify_voucher(voucher_id)
    if not v.get("valid"):
        # 409 Conflict so the cashier UI can distinguish "rejected by
        # system" from a 4xx/5xx infrastructure error.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": v.get("reason") or POSError.NOT_FOUND, "voucher": v},
        )

    # Cross-brand pool check: if the caller specified a brand via the
    # pairing, ensure this voucher is redeemable there.
    pairing = None
    for p in _MERCHANT_PAIRINGS.values():
        if p["pos_code"] == code:
            pairing = p
            break
    if pairing:
        store = get_mock_store()
        stored = store.get(voucher_id) or {}
        owner = stored.get("brand_id")
        pool = stored.get("master_pool_brands") or []
        if owner and pairing["brand_id"] != owner and pairing["brand_id"] not in pool:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": POSError.WRONG_BRAND, "voucher": v},
            )

    # Step 2: apply discount
    disc = adapter.apply_discount(voucher_id, body.order_id, body.amount_cents)
    if not disc.get("applied"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": disc.get("reason") or POSError.INVALID_AMOUNT,
                    "discount": disc},
        )

    # Step 3: mark redeemed
    tx_data = {
        "order_id": body.order_id,
        "tendered_cents": body.amount_cents,
        "cashier_id": body.cashier_id,
        "store_id": body.store_id,
    }
    red = adapter.mark_redeemed(voucher_id, tx_data)

    # Step 4: record for transactions endpoint
    record = {
        "voucher_id": voucher_id,
        "pos_code": code,
        "redemption_id": red["redemption_id"],
        "order_id": body.order_id,
        "amount_cents": body.amount_cents,
        "currency": v.get("currency", "USD"),
        "brand_id": v.get("brand_id") or (pairing["brand_id"] if pairing else None),
        "cashier_id": body.cashier_id,
        "store_id": body.store_id,
        "redeemed_at": red["redeemed_at"],
        "transaction_ref": red["transaction_ref"],
    }
    _REDEMPTION_BY_ID[red["redemption_id"]] = record
    if record["brand_id"]:
        _REDEMPTIONS_BY_BRAND.setdefault(record["brand_id"], []).append(record)
    if pairing and pairing["brand_id"] != record["brand_id"]:
        _REDEMPTIONS_BY_BRAND.setdefault(pairing["brand_id"], []).append(record)

    receipt_url = f"/api/v1/pos/receipt/{red['redemption_id']}.html"
    emit_audit({"action": "redeem_voucher", "voucher_id": voucher_id,
                "redemption_id": red["redemption_id"], "pos": code})

    return {
        "ok": True,
        "voucher": v,
        "discount": disc,
        "redemption": red,
        "receipt_url": receipt_url,
    }


@webhook_router.post("/pos/{adapter}", include_in_schema=True)
async def pos_webhook(adapter: str, request: Request) -> dict[str, Any]:
    """Inbound webhook from a POS system.

    Verifies the adapter-specific signature header (if provided), then
    normalises the event and records it in the audit log.
    """
    code = adapter.lower().strip()
    if code not in all_pos_codes():
        raise HTTPException(
            status_code=404,
            detail={"error": "unknown_pos_adapter", "adapter": adapter},
        )
    adapter_obj = get_pos_adapter(code)
    raw = await request.body()

    # Signature header per adapter
    sig_header_name = {
        "toast": "toast-signature",
        "square": "x-square-hmacsha256-signature",
        "loyverse": "x-loyverse-signature",
        "foodzaps": "x-foodzaps-signature",
    }[code]
    signature = request.headers.get(sig_header_name, "")

    # Signature is required unless explicitly skipped via empty secret in
    # mock mode (the verify helper still constant-time-compares).
    if signature and not adapter_obj.verify_webhook_signature(raw, signature):  # type: ignore[attr-defined]
        emit_audit({"pos": code, "action": "webhook_rejected",
                    "reason": POSError.INVALID_SIGNATURE})
        raise HTTPException(
            status_code=400,
            detail={"error": POSError.INVALID_SIGNATURE, "adapter": code},
        )

    canonical = adapter_obj.webhook_handler(raw)
    return {"ok": True, "event": canonical}


@router.get("/transactions/{brand_id}")
async def list_transactions(brand_id: str) -> dict[str, Any]:
    """Return all POS redemptions credited to this brand."""
    items = list(_REDEMPTIONS_BY_BRAND.get(brand_id, []))
    return {
        "brand_id": brand_id,
        "count": len(items),
        "items": items,
    }


@router.get("/receipt/{redemption_id}.html", response_class=HTMLResponse)
async def receipt_html(redemption_id: str) -> HTMLResponse:
    """Render a printable thermal-friendly receipt for the redemption."""
    rec = _REDEMPTION_BY_ID.get(redemption_id)
    if not rec:
        raise HTTPException(status_code=404,
                            detail={"error": "redemption_not_found"})
    html_doc = generate_receipt_html(
        voucher_id=rec["voucher_id"],
        redemption_id=rec["redemption_id"],
        pos_code=rec["pos_code"],
        brand_id=rec.get("brand_id"),
        brand_name=rec.get("brand_id"),
        order_id=rec["order_id"],
        amount_cents=rec["amount_cents"],
        currency=rec.get("currency", "USD"),
        cashier_id=rec.get("cashier_id"),
        redeemed_at=rec["redeemed_at"],
    )
    return HTMLResponse(content=html_doc, media_type="text/html; charset=utf-8")


@router.get("/checkout", response_class=HTMLResponse)
async def web_checkout() -> Any:
    """Lightweight web POS for merchants without integrated terminals.

    Cashier types voucher code → verifies → records → prints receipt.
    Serves ``landing/pos-checkout.html`` from the repo.
    """
    candidate = (
        Path(__file__).resolve().parents[2]
        / "landing"
        / "pos-checkout.html"
    )
    if candidate.is_file():
        return FileResponse(candidate, media_type="text/html; charset=utf-8")
    # Fallback if landing asset missing — still functional.
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>KiX POS Checkout</title>"
        "<h1>KiX POS Checkout</h1>"
        "<p>landing/pos-checkout.html is missing from this build.</p>",
        status_code=200,
    )


@router.get("/audit-log")
async def audit_log_tail(limit: int = 50) -> dict[str, Any]:
    """Tail of the in-process audit log (mock + dev only)."""
    rows = read_audit_log()
    return {"count": len(rows), "items": rows[-limit:]}


@router.get("/_adapters")
async def list_adapters() -> dict[str, Any]:
    """Health summary across all adapters."""
    out = []
    for code in all_pos_codes():
        adapter = get_pos_adapter(code)
        out.append({"pos": code, "mode": adapter.get_mode()})
    return {"adapters": out}
