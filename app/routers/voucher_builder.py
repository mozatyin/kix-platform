"""Voucher Builder — conditional voucher templates with rich validation.

A richer cousin of `vouchers.py`. Here a *template* defines the rules
(value, conditions, expiry, stackability, transferability); concrete
*vouchers* are issued from a template and live until they are redeemed
or expire.

Storage layout (Redis, brand-isolated):

    brand:{bid}:voucher_templates:{tid}        JSON (template)
    brand:{bid}:voucher_templates                SET of template_ids
    voucher:{vid}                                HASH (state)
    user:{uid}:vouchers:{bid}                    LIST of voucher_ids
    voucher_code:{code}                          STRING → voucher_id
    voucher_template:{tid}:issued                INT (total issued)
    voucher_template:{tid}:user:{uid}:count      INT (per-user usage)

Atomicity:
    * total_supply is enforced via INCR + rollback if cap exceeded.
    * redeem is an atomic state transition (WATCH/MULTI).
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Pydantic models ────────────────────────────────────────────────────────


class VoucherValue(BaseModel):
    type: Literal["percent", "fixed", "free_item", "cashback"]
    amount: float = Field(..., ge=0)
    currency: str = Field("USD", min_length=3, max_length=3)

    @field_validator("amount")
    @classmethod
    def _percent_bounds(cls, v: float, info) -> float:  # noqa: ANN001
        # field_validator doesn't see other fields in this pydantic version
        # — bounds enforced again at use-site for percent.
        return v


class VoucherDateRange(BaseModel):
    from_: str | None = Field(None, alias="from")
    to: str | None = None

    model_config = {"populate_by_name": True}


class VoucherConditions(BaseModel):
    min_purchase_cents: int | None = Field(None, ge=0)
    min_items: int | None = Field(None, ge=0)
    specific_sku: list[str] | None = None
    first_time_user_only: bool | None = None
    tier_required: str | None = None
    valid_dates: VoucherDateRange | None = None
    valid_days_of_week: list[int] | None = None  # 0=Mon … 6=Sun
    usage_limit_per_user: int | None = Field(None, ge=1)
    total_supply: int | None = Field(None, ge=1)

    @field_validator("valid_days_of_week")
    @classmethod
    def _days_in_range(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return v
        for d in v:
            if not 0 <= d <= 6:
                raise ValueError("valid_days_of_week entries must be in 0..6")
        return v


class TemplateCreateRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    template_id: str | None = None
    name: str = Field(..., min_length=1)
    description: str = ""
    value: VoucherValue
    conditions: VoucherConditions = Field(default_factory=VoucherConditions)
    expires_in_days: int = Field(30, ge=1, le=3650)
    stackable: bool = False
    transferable: bool = True


class TemplateIssueRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    brand_id: str = Field(..., min_length=1)
    reason: str = ""


class VoucherValidateRequest(BaseModel):
    purchase_amount_cents: int = Field(..., ge=0)
    items: list[dict[str, Any]] = Field(default_factory=list)
    user_id: str


class VoucherRedeemRequest(BaseModel):
    pos_id: str = Field(..., min_length=1)
    purchase_amount_cents: int = Field(..., ge=0)
    items: list[dict[str, Any]] = Field(default_factory=list)


class VoucherTransferRequest(BaseModel):
    to_user_id: str = Field(..., min_length=1)


# ── Redis key helpers ─────────────────────────────────────────────────────


def _k_template(brand_id: str, tid: str) -> str:
    return f"brand:{brand_id}:voucher_templates:{tid}"


def _k_template_set(brand_id: str) -> str:
    return f"brand:{brand_id}:voucher_templates"


def _k_voucher(vid: str) -> str:
    return f"voucher:{vid}"


def _k_user_vouchers(user_id: str, brand_id: str) -> str:
    return f"user:{user_id}:vouchers:{brand_id}"


def _k_code(code: str) -> str:
    return f"voucher_code:{code}"


def _k_template_issued(tid: str) -> str:
    return f"voucher_template:{tid}:issued"


def _k_user_template_count(tid: str, user_id: str) -> str:
    return f"voucher_template:{tid}:user:{user_id}:count"


def _k_user_brand_first_purchase(brand_id: str, user_id: str) -> str:
    """Marker key — present once a user has made at least one purchase."""
    return f"brand:{brand_id}:user:{user_id}:has_purchased"


def _k_user_tier(brand_id: str, user_id: str) -> str:
    return f"brand:{brand_id}:user:{user_id}:tier"


# ── Helpers ────────────────────────────────────────────────────────────────


def _now() -> int:
    return int(time.time())


def _gen_code() -> str:
    # 12-char alphanumeric, uppercase
    raw = secrets.token_urlsafe(10).upper()
    cleaned = "".join(c for c in raw if c.isalnum())
    return cleaned[:12].ljust(12, "X")


def _parse_iso(s: str) -> int | None:
    """Return epoch seconds for an ISO date(time) string, or None."""
    if not s:
        return None
    try:
        # Accept dates and datetimes
        if "T" not in s and len(s) == 10:
            s = s + "T00:00:00+00:00"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:  # noqa: BLE001
        return None


async def _load_template(
    r: aioredis.Redis, brand_id: str, tid: str
) -> dict[str, Any]:
    raw = await r.get(_k_template(brand_id, tid))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Voucher template not found",
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Corrupt voucher template",
        )


async def _load_voucher(r: aioredis.Redis, vid: str) -> dict[str, str]:
    state = await r.hgetall(_k_voucher(vid))
    if not state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Voucher not found",
        )
    return state


def _compute_discount(
    value: dict[str, Any], purchase_amount_cents: int
) -> int:
    """Return discount in cents for a voucher value over a purchase."""
    vtype = value.get("type")
    amount = float(value.get("amount", 0))
    if vtype == "percent":
        # amount is 0..100
        pct = max(0.0, min(100.0, amount))
        return int(purchase_amount_cents * pct / 100.0)
    if vtype == "fixed":
        return min(int(amount * 100), purchase_amount_cents)
    if vtype == "cashback":
        # Cashback doesn't reduce the purchase price; returned separately
        return 0
    if vtype == "free_item":
        # Caller decides — return 0 here, let POS pick item
        return 0
    return 0


async def _check_conditions(
    r: aioredis.Redis,
    *,
    template: dict[str, Any],
    user_id: str,
    purchase_amount_cents: int,
    items: list[dict[str, Any]],
    brand_id: str,
) -> tuple[bool, str | None]:
    """Validate template conditions against the supplied context.

    Returns (ok, failure_reason).
    """
    cond = template.get("conditions") or {}

    # min_purchase_cents
    mp = cond.get("min_purchase_cents")
    if mp is not None and purchase_amount_cents < int(mp):
        return False, "min_purchase_not_met"

    # min_items
    mi = cond.get("min_items")
    if mi is not None and len(items) < int(mi):
        return False, "min_items_not_met"

    # specific_sku — at least one matching sku must appear
    skus = cond.get("specific_sku")
    if skus:
        bag = {str(it.get("sku")) for it in items if it.get("sku")}
        if not bag.intersection(set(skus)):
            return False, "sku_not_in_purchase"

    # first_time_user_only
    if cond.get("first_time_user_only"):
        has_purchased = await r.exists(
            _k_user_brand_first_purchase(brand_id, user_id)
        )
        if has_purchased:
            return False, "not_first_time_user"

    # tier_required
    req_tier = cond.get("tier_required")
    if req_tier:
        cur_tier = await r.get(_k_user_tier(brand_id, user_id))
        if (cur_tier or "").lower() != str(req_tier).lower():
            return False, "tier_required_not_met"

    # valid_dates
    vd = cond.get("valid_dates") or {}
    now = _now()
    from_ts = _parse_iso(vd.get("from") or vd.get("from_") or "")
    to_ts = _parse_iso(vd.get("to") or "")
    if from_ts and now < from_ts:
        return False, "not_yet_valid"
    if to_ts and now > to_ts:
        return False, "date_window_expired"

    # valid_days_of_week (0=Mon…6=Sun)
    dow = cond.get("valid_days_of_week")
    if dow:
        today = datetime.now(timezone.utc).weekday()
        if today not in dow:
            return False, "not_valid_today"

    # usage_limit_per_user — enforced against the template-level count
    upl = cond.get("usage_limit_per_user")
    if upl is not None:
        tid = template.get("template_id", "")
        used = int(
            await r.get(_k_user_template_count(tid, user_id)) or 0
        )
        if used >= int(upl):
            return False, "per_user_limit_reached"

    return True, None


def _voucher_status_label(
    state: dict[str, str],
    *,
    now: int | None = None,
) -> str:
    """Compute a human voucher status from its hash."""
    now = now if now is not None else _now()
    if state.get("redeemed_at"):
        return "redeemed"
    exp = state.get("expires_at")
    if exp and now > int(exp):
        return "expired"
    return state.get("status", "active")


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post(
    "/templates/create",
    summary="Create a voucher template with conditions",
    status_code=status.HTTP_201_CREATED,
)
async def create_template(
    body: TemplateCreateRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    tid = body.template_id or uuid4().hex[:16]
    if body.value.type == "percent" and body.value.amount > 100:
        raise HTTPException(
            status_code=400, detail="percent amount must be 0..100"
        )
    template = {
        "template_id": tid,
        "brand_id": body.brand_id,
        "name": body.name,
        "description": body.description,
        "value": body.value.model_dump(),
        "conditions": body.conditions.model_dump(by_alias=True),
        "expires_in_days": body.expires_in_days,
        "stackable": body.stackable,
        "transferable": body.transferable,
        "created_at": _now(),
    }
    pipe = r.pipeline()
    pipe.set(_k_template(body.brand_id, tid), json.dumps(template))
    pipe.sadd(_k_template_set(body.brand_id), tid)
    await pipe.execute()
    return {"ok": True, "template_id": tid}


@router.get(
    "/templates/{template_id}",
    summary="Get a voucher template",
)
async def get_template(
    template_id: str,
    brand_id: str = Query(..., min_length=1),
    r: aioredis.Redis = Depends(get_redis),
):
    return await _load_template(r, brand_id, template_id)


@router.post(
    "/templates/{template_id}/issue",
    summary="Issue a voucher from a template to a user",
    status_code=status.HTTP_201_CREATED,
)
async def issue_voucher(
    template_id: str,
    body: TemplateIssueRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    template = await _load_template(r, body.brand_id, template_id)

    # Enforce total_supply atomically.
    total_supply = (template.get("conditions") or {}).get("total_supply")
    if total_supply is not None:
        issued = await r.incr(_k_template_issued(template_id))
        if issued > int(total_supply):
            # Roll back the counter.
            await r.decr(_k_template_issued(template_id))
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="Voucher supply exhausted",
            )
    else:
        await r.incr(_k_template_issued(template_id))

    vid = uuid4().hex[:16]
    code = _gen_code()
    # Ensure code uniqueness
    for _ in range(5):
        if await r.setnx(_k_code(code), vid):
            break
        code = _gen_code()
    else:
        raise HTTPException(
            status_code=500, detail="Failed to allocate unique code"
        )

    expires_at = _now() + int(template["expires_in_days"]) * 86400
    voucher = {
        "voucher_id": vid,
        "code": code,
        "template_id": template_id,
        "brand_id": body.brand_id,
        "user_id": body.user_id,
        "status": "active",
        "issued_at": str(_now()),
        "expires_at": str(expires_at),
        "reason": body.reason or "",
        "stackable": "1" if template.get("stackable") else "0",
        "transferable": "1" if template.get("transferable") else "0",
        "value": json.dumps(template["value"]),
    }
    pipe = r.pipeline()
    pipe.hset(_k_voucher(vid), mapping=voucher)
    pipe.expireat(_k_voucher(vid), expires_at + 30 * 86400)  # keep 30d after
    pipe.rpush(_k_user_vouchers(body.user_id, body.brand_id), vid)
    await pipe.execute()

    logger.info(
        "Voucher issued: vid=%s template=%s user=%s brand=%s",
        vid,
        template_id,
        body.user_id,
        body.brand_id,
    )
    return {
        "voucher_id": vid,
        "code": code,
        "expires_at": expires_at,
    }


@router.post(
    "/{voucher_id}/validate",
    summary="Check whether a voucher is currently usable",
)
async def validate_voucher(
    voucher_id: str,
    body: VoucherValidateRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    state = await _load_voucher(r, voucher_id)
    brand_id = state.get("brand_id", "")
    template_id = state.get("template_id", "")

    # Owner check (allow user_id mismatch only if voucher is transferable
    # and code matches — here we use user_id field).
    if state.get("user_id") != body.user_id:
        return {
            "valid": False,
            "reason": "wrong_owner",
            "actual_discount_cents": 0,
        }

    status_label = _voucher_status_label(state)
    if status_label != "active":
        return {
            "valid": False,
            "reason": status_label,
            "actual_discount_cents": 0,
        }

    template = await _load_template(r, brand_id, template_id)
    ok, reason = await _check_conditions(
        r,
        template=template,
        user_id=body.user_id,
        purchase_amount_cents=body.purchase_amount_cents,
        items=body.items,
        brand_id=brand_id,
    )
    if not ok:
        return {
            "valid": False,
            "reason": reason,
            "actual_discount_cents": 0,
        }

    discount = _compute_discount(template["value"], body.purchase_amount_cents)
    return {
        "valid": True,
        "reason": None,
        "actual_discount_cents": discount,
    }


@router.post(
    "/{voucher_id}/redeem",
    summary="Redeem a voucher at point-of-sale",
)
async def redeem_voucher(
    voucher_id: str,
    body: VoucherRedeemRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    state = await _load_voucher(r, voucher_id)
    brand_id = state.get("brand_id", "")
    template_id = state.get("template_id", "")
    user_id = state.get("user_id", "")

    status_label = _voucher_status_label(state)
    if status_label != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Voucher status is '{status_label}', cannot redeem",
        )

    template = await _load_template(r, brand_id, template_id)
    ok, reason = await _check_conditions(
        r,
        template=template,
        user_id=user_id,
        purchase_amount_cents=body.purchase_amount_cents,
        items=body.items,
        brand_id=brand_id,
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Conditions not met: {reason}",
        )

    discount = _compute_discount(template["value"], body.purchase_amount_cents)
    final_amount = max(0, body.purchase_amount_cents - discount)

    # Atomic state flip with WATCH/MULTI to prevent double-redeem.
    vkey = _k_voucher(voucher_id)
    async with r.pipeline(transaction=True) as pipe:
        try:
            await pipe.watch(vkey)
            cur_status = await pipe.hget(vkey, "status")
            cur_redeemed = await pipe.hget(vkey, "redeemed_at")
            if cur_redeemed:
                await pipe.unwatch()
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Voucher already redeemed",
                )
            if cur_status != "active":
                await pipe.unwatch()
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Voucher status changed to {cur_status}",
                )
            pipe.multi()
            pipe.hset(
                vkey,
                mapping={
                    "status": "redeemed",
                    "redeemed_at": str(_now()),
                    "pos_id": body.pos_id,
                    "discount_applied_cents": str(discount),
                    "final_amount_cents": str(final_amount),
                },
            )
            pipe.incr(_k_user_template_count(template_id, user_id))
            # Mark that this user has now purchased (kills first-time-only)
            pipe.set(_k_user_brand_first_purchase(brand_id, user_id), "1")
            await pipe.execute()
        except aioredis.WatchError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Concurrent redemption detected",
            )

    logger.info(
        "Voucher redeemed: vid=%s pos=%s discount=%d final=%d",
        voucher_id,
        body.pos_id,
        discount,
        final_amount,
    )
    return {
        "ok": True,
        "discount_applied_cents": discount,
        "final_amount_cents": final_amount,
    }


@router.get(
    "/{user_id}",
    summary="List vouchers for a user with computed validity",
)
async def list_user_vouchers(
    user_id: str,
    brand_id: str = Query(..., min_length=1),
    r: aioredis.Redis = Depends(get_redis),
):
    vids = await r.lrange(_k_user_vouchers(user_id, brand_id), 0, -1)
    out: list[dict[str, Any]] = []
    now = _now()
    for vid in vids:
        state = await r.hgetall(_k_voucher(vid))
        if not state:
            continue
        out.append(
            {
                "voucher_id": vid,
                "code": state.get("code"),
                "template_id": state.get("template_id"),
                "brand_id": state.get("brand_id"),
                "status": _voucher_status_label(state, now=now),
                "expires_at": int(state.get("expires_at") or 0),
                "issued_at": int(state.get("issued_at") or 0),
                "transferable": state.get("transferable") == "1",
                "stackable": state.get("stackable") == "1",
                "value": json.loads(state.get("value") or "{}"),
            }
        )
    return {"user_id": user_id, "brand_id": brand_id, "vouchers": out}


@router.post(
    "/{voucher_id}/transfer",
    summary="Transfer a voucher to another user (if transferable)",
)
async def transfer_voucher(
    voucher_id: str,
    body: VoucherTransferRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    state = await _load_voucher(r, voucher_id)
    if state.get("transferable") != "1":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Voucher is not transferable",
        )
    status_label = _voucher_status_label(state)
    if status_label != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Voucher status is '{status_label}', cannot transfer",
        )

    brand_id = state.get("brand_id", "")
    from_user = state.get("user_id", "")
    if from_user == body.to_user_id:
        raise HTTPException(
            status_code=400, detail="Cannot transfer to self"
        )

    pipe = r.pipeline()
    pipe.hset(
        _k_voucher(voucher_id),
        mapping={
            "user_id": body.to_user_id,
            "transferred_from": from_user,
            "transferred_at": str(_now()),
        },
    )
    # Update user lists. LREM removes from old, RPUSH adds to new.
    pipe.lrem(_k_user_vouchers(from_user, brand_id), 0, voucher_id)
    pipe.rpush(_k_user_vouchers(body.to_user_id, brand_id), voucher_id)
    await pipe.execute()
    return {
        "ok": True,
        "voucher_id": voucher_id,
        "from_user_id": from_user,
        "to_user_id": body.to_user_id,
    }
