"""Currency conversion engine — multi-currency FX rates for KiX wallet & payouts.

Stores configurable FX rates per currency pair so wallet / payouts / inter-brand
ledger can move money across CNY / USD / EUR / IDR / JPY / SGD / etc. Production
intent is to swap the `_apply_rate` helper for a live FX provider call (Wise /
Currencylayer / OXR); the storage layer + audit history stays identical.

Money is always integer ``amount_cents`` — never floats — but FX rates are
stored as decimal strings to keep precision (e.g. ``"0.13746"``). Conversion
math is done in :class:`decimal.Decimal` with banker's rounding, then snapped
back to integer cents.

Redis schema
------------
    fx:rate:{FROM}:{TO}              HASH  {rate, expires_at, source, updated_at}
    fx:rate_history:{FROM}:{TO}      LIST  JSON entries (newest left, ≤100)
    fx:pairs                         SET   "FROM:TO" pair keys for discovery

Admin auth mirrors payouts.py — pre-shared key compared with constant time.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Literal
from uuid import uuid4

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import settings
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ────────────────────────────────────────────────────────────
DEFAULT_RATE_TTL_SECONDS = 24 * 3600       # 24h before a rate counts as stale
MAX_HISTORY_PER_PAIR = 100
SUPPORTED_CURRENCIES: set[str] = {
    "CNY", "USD", "EUR", "GBP", "JPY", "SGD", "HKD", "TWD",
    "IDR", "MYR", "THB", "VND", "PHP", "INR", "KRW", "AUD",
}
MIN_RATE = Decimal("0.0000001")
MAX_RATE = Decimal("1000000")
ROUND_QUANT = Decimal("1")  # cents are integers

_ADMIN_TOKEN_FALLBACK = settings.jwt_secret


# ── Redis keys ───────────────────────────────────────────────────────────
def _k_rate(from_cur: str, to_cur: str) -> str:
    return f"fx:rate:{from_cur}:{to_cur}"


def _k_history(from_cur: str, to_cur: str) -> str:
    return f"fx:rate_history:{from_cur}:{to_cur}"


_K_PAIRS_SET = "fx:pairs"


def _pair_key(from_cur: str, to_cur: str) -> str:
    return f"{from_cur}:{to_cur}"


# ── Pydantic models ──────────────────────────────────────────────────────
def _validate_currency(v: str) -> str:
    v = (v or "").strip().upper()
    if len(v) != 3 or not v.isalpha():
        raise ValueError("currency must be a 3-letter ISO code")
    return v


class FxRatePair(BaseModel):
    from_currency: str
    to_currency: str
    rate: str = Field(..., description="Decimal rate as string, e.g. '0.13746'.")
    expires_at: float | None = Field(
        default=None,
        description="Unix ts when this rate goes stale. Defaults to now+24h.",
    )
    source: str | None = Field(default=None, max_length=64)

    @field_validator("from_currency", "to_currency")
    @classmethod
    def _cur(cls, v: str) -> str:
        return _validate_currency(v)

    @field_validator("rate")
    @classmethod
    def _rate(cls, v: str) -> str:
        try:
            d = Decimal(str(v))
        except (InvalidOperation, ValueError):
            raise ValueError(f"rate must parse as Decimal, got {v!r}")
        if d <= 0:
            raise ValueError("rate must be > 0")
        if d < MIN_RATE or d > MAX_RATE:
            raise ValueError(f"rate must be within [{MIN_RATE}, {MAX_RATE}]")
        # Normalize: strip trailing zeros, keep at least one digit after '.'.
        return format(d.normalize(), "f")


class ConfigureRatesRequest(BaseModel):
    admin_token: str
    pairs: list[FxRatePair] = Field(..., min_length=1, max_length=200)

    @model_validator(mode="before")
    @classmethod
    def _accept_flat_pair(cls, data: Any) -> Any:
        """Merchant-intuitive alias: accept a flat single-pair shape too.

        Canonical form is ``{admin_token, pairs: [{from_currency, to_currency,
        rate, ...}]}`` but callers often POST ``{admin_token, from_currency,
        to_currency, rate, ...}`` for a single pair. Auto-wrap into a
        single-element ``pairs`` list when ``pairs`` is absent.
        """
        if not isinstance(data, dict):
            return data
        if "pairs" in data and data["pairs"]:
            return data
        if "from_currency" in data and "to_currency" in data and "rate" in data:
            pair_keys = (
                "from_currency", "to_currency", "rate", "expires_at", "source",
            )
            wrapped = {k: data[k] for k in pair_keys if k in data}
            new_data = {k: v for k, v in data.items() if k not in pair_keys}
            new_data["pairs"] = [wrapped]
            return new_data
        return data


class ConfigureRatesResponse(BaseModel):
    configured: int
    pairs: list[str]
    ts: float


class FxRateResponse(BaseModel):
    from_currency: str
    to_currency: str
    rate: str
    expires_at: float | None
    updated_at: float
    source: str | None = None
    stale: bool


class ConvertRequest(BaseModel):
    amount_cents: int = Field(..., ge=0, le=10**14)
    from_currency: str
    to_currency: str

    @field_validator("from_currency", "to_currency")
    @classmethod
    def _cur(cls, v: str) -> str:
        return _validate_currency(v)


class ConvertResponse(BaseModel):
    amount_cents: int
    from_currency: str
    equivalent_cents: int
    to_currency: str
    rate: str
    expires_at: float | None
    stale: bool


class ExpireRateResponse(BaseModel):
    pair_key: str
    expired: bool


# ── Helpers ──────────────────────────────────────────────────────────────
def _check_admin(token: str) -> None:
    if not token or not secrets.compare_digest(token, _ADMIN_TOKEN_FALLBACK):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "admin_token_invalid"},
        )


def _now() -> float:
    return time.time()


async def _load_rate(
    r: aioredis.Redis, from_cur: str, to_cur: str
) -> dict[str, Any] | None:
    raw = await r.hgetall(_k_rate(from_cur, to_cur))
    if not raw:
        return None
    try:
        expires_at_raw = raw.get("expires_at") or ""
        expires_at = float(expires_at_raw) if expires_at_raw else None
    except (ValueError, TypeError):
        expires_at = None
    try:
        updated_at = float(raw.get("updated_at") or 0)
    except (ValueError, TypeError):
        updated_at = 0.0
    return {
        "from_currency": from_cur,
        "to_currency": to_cur,
        "rate": raw.get("rate") or "0",
        "expires_at": expires_at,
        "updated_at": updated_at,
        "source": raw.get("source") or None,
    }


def _is_stale(expires_at: float | None, now: float | None = None) -> bool:
    if expires_at is None:
        return False
    return (now or _now()) >= expires_at


def _apply_rate(amount_cents: int, rate: str) -> int:
    """Convert ``amount_cents`` using a string rate, returning integer cents.

    Uses :class:`Decimal` with banker's rounding so .5 cents alternates
    direction — this minimises systematic drift over millions of conversions
    relative to a single-direction rounder.
    """
    if amount_cents == 0:
        return 0
    d_amount = Decimal(amount_cents)
    d_rate = Decimal(rate)
    converted = (d_amount * d_rate).quantize(ROUND_QUANT, rounding=ROUND_HALF_EVEN)
    return int(converted)


async def convert_amount(
    r: aioredis.Redis,
    amount_cents: int,
    from_currency: str,
    to_currency: str,
    *,
    allow_stale: bool = False,
) -> dict[str, Any]:
    """Public helper for other routers (wallet, payouts) to convert money.

    Returns a dict with ``equivalent_cents``, ``rate``, ``expires_at`` and
    ``stale``. Raises :class:`HTTPException` if no rate is configured.

    Pair-equality short-circuits: ``CNY → CNY`` returns the amount unchanged
    with a synthetic ``"1"`` rate.
    """
    from_currency = _validate_currency(from_currency)
    to_currency = _validate_currency(to_currency)

    if from_currency == to_currency:
        return {
            "amount_cents": amount_cents,
            "from_currency": from_currency,
            "equivalent_cents": amount_cents,
            "to_currency": to_currency,
            "rate": "1",
            "expires_at": None,
            "stale": False,
        }

    rec = await _load_rate(r, from_currency, to_currency)
    if rec is None:
        # Try inverse pair as a fallback (rate_inv = 1/rate_direct).
        inv = await _load_rate(r, to_currency, from_currency)
        if inv is not None:
            try:
                d_inv = Decimal(inv["rate"])
                if d_inv > 0:
                    rec = {
                        **inv,
                        "from_currency": from_currency,
                        "to_currency": to_currency,
                        "rate": format(
                            (Decimal(1) / d_inv).normalize(), "f"
                        ),
                        "source": (inv.get("source") or "inverse") + ":inverse",
                    }
            except (InvalidOperation, ValueError):
                rec = None

    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "fx_rate_not_configured",
                "from_currency": from_currency,
                "to_currency": to_currency,
            },
        )

    stale = _is_stale(rec["expires_at"])
    if stale and not allow_stale:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "fx_rate_stale",
                "from_currency": from_currency,
                "to_currency": to_currency,
                "expires_at": rec["expires_at"],
            },
        )

    equivalent_cents = _apply_rate(amount_cents, rec["rate"])
    return {
        "amount_cents": amount_cents,
        "from_currency": from_currency,
        "equivalent_cents": equivalent_cents,
        "to_currency": to_currency,
        "rate": rec["rate"],
        "expires_at": rec["expires_at"],
        "stale": stale,
    }


# ── Endpoints ────────────────────────────────────────────────────────────
@router.post(
    "/rates/configure",
    response_model=ConfigureRatesResponse,
    summary="Admin: store one or more FX rates (overwrites existing pairs).",
)
async def configure_rates(
    body: ConfigureRatesRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> ConfigureRatesResponse:
    """Bulk upsert FX rates. Each pair gets persisted + history-appended.

    History keeps the last :data:`MAX_HISTORY_PER_PAIR` entries so we can
    audit "what rate was applied when". Expiry defaults to now+24h if the
    caller doesn't specify one — stale rates are *kept* (for audit) but
    flagged by ``stale=True`` and rejected by /convert unless explicitly
    allowed.
    """
    _check_admin(body.admin_token)

    now = _now()
    configured_keys: list[str] = []

    # Batch into a single pipeline for atomicity-ish + fewer RTTs. We don't
    # need MULTI/EXEC strictness — each pair is independent.
    pipe = r.pipeline(transaction=False)
    for p in body.pairs:
        if p.from_currency == p.to_currency:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "identity_pair",
                    "pair": _pair_key(p.from_currency, p.to_currency),
                },
            )
        expires_at = p.expires_at if p.expires_at is not None else (
            now + DEFAULT_RATE_TTL_SECONDS
        )
        if expires_at <= now:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "expires_at_in_past",
                    "expires_at": expires_at,
                    "now": now,
                },
            )

        pair_key = _pair_key(p.from_currency, p.to_currency)
        mapping = {
            "rate": p.rate,
            "expires_at": str(expires_at),
            "updated_at": str(now),
            "source": p.source or "manual",
        }
        pipe.hset(_k_rate(p.from_currency, p.to_currency), mapping=mapping)
        pipe.sadd(_K_PAIRS_SET, pair_key)
        hist_entry = json.dumps({
            "rate": p.rate,
            "expires_at": expires_at,
            "source": p.source or "manual",
            "ts": now,
            "event_id": uuid4().hex,
        }, separators=(",", ":"))
        pipe.lpush(_k_history(p.from_currency, p.to_currency), hist_entry)
        pipe.ltrim(
            _k_history(p.from_currency, p.to_currency), 0, MAX_HISTORY_PER_PAIR - 1
        )
        configured_keys.append(pair_key)

    await pipe.execute()
    logger.info("fx rates configured pairs=%s", configured_keys)

    return ConfigureRatesResponse(
        configured=len(configured_keys),
        pairs=configured_keys,
        ts=now,
    )


@router.get(
    "/rates",
    summary="Get current FX rate(s). Filter by from_currency/to_currency.",
)
async def get_rates(
    from_currency: str | None = Query(default=None),
    to_currency: str | None = Query(default=None),
    include_stale: bool = Query(default=True),
    limit: int = Query(default=200, ge=1, le=1000),
    r: aioredis.Redis = Depends(get_redis),
):
    """Look up a single rate or list all known pairs.

    When both ``from_currency`` and ``to_currency`` are supplied, returns a
    single :class:`FxRateResponse`. Otherwise returns a list, optionally
    filtered on one leg.
    """
    if from_currency:
        from_currency = _validate_currency(from_currency)
    if to_currency:
        to_currency = _validate_currency(to_currency)

    if from_currency and to_currency:
        rec = await _load_rate(r, from_currency, to_currency)
        if rec is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "fx_rate_not_configured",
                    "from_currency": from_currency,
                    "to_currency": to_currency,
                },
            )
        stale = _is_stale(rec["expires_at"])
        if stale and not include_stale:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "fx_rate_stale"},
            )
        return FxRateResponse(
            from_currency=rec["from_currency"],
            to_currency=rec["to_currency"],
            rate=rec["rate"],
            expires_at=rec["expires_at"],
            updated_at=rec["updated_at"],
            source=rec["source"],
            stale=stale,
        )

    # List mode — walk the pairs set.
    members = await r.smembers(_K_PAIRS_SET)
    now = _now()
    out: list[FxRateResponse] = []
    for pk in members:
        try:
            a, b = pk.split(":", 1)
        except ValueError:
            continue
        if from_currency and a != from_currency:
            continue
        if to_currency and b != to_currency:
            continue
        rec = await _load_rate(r, a, b)
        if rec is None:
            continue
        stale = _is_stale(rec["expires_at"], now)
        if stale and not include_stale:
            continue
        out.append(
            FxRateResponse(
                from_currency=a,
                to_currency=b,
                rate=rec["rate"],
                expires_at=rec["expires_at"],
                updated_at=rec["updated_at"],
                source=rec["source"],
                stale=stale,
            )
        )
        if len(out) >= limit:
            break
    return out


@router.post(
    "/convert",
    response_model=ConvertResponse,
    summary="Convert an integer-cent amount from one currency to another.",
)
async def convert(
    body: ConvertRequest,
    allow_stale: bool = Query(default=False),
    r: aioredis.Redis = Depends(get_redis),
) -> ConvertResponse:
    """Single-leg conversion. Falls back to inverse rate if direct pair missing."""
    result = await convert_amount(
        r,
        body.amount_cents,
        body.from_currency,
        body.to_currency,
        allow_stale=allow_stale,
    )
    return ConvertResponse(**result)


@router.post(
    "/rates/expire/{pair_key}",
    response_model=ExpireRateResponse,
    summary="Force-expire a single FX rate. Pair key is 'FROM:TO' (e.g. 'CNY:USD').",
)
async def expire_rate(
    pair_key: str,
    admin_token: str = Query(...),
    r: aioredis.Redis = Depends(get_redis),
) -> ExpireRateResponse:
    """Mark a configured rate as stale by setting ``expires_at`` to now-1s.

    The rate record itself is kept so /convert can still flip into
    ``allow_stale=true`` mode (audit) and history is preserved.
    """
    _check_admin(admin_token)
    try:
        from_cur, to_cur = pair_key.split(":", 1)
        from_cur = _validate_currency(from_cur)
        to_cur = _validate_currency(to_cur)
    except (ValueError, Exception):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_pair_key", "expected": "FROM:TO"},
        )

    key = _k_rate(from_cur, to_cur)
    exists = await r.exists(key)
    if not exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "fx_rate_not_configured",
                "pair_key": _pair_key(from_cur, to_cur),
            },
        )

    now = _now()
    await r.hset(key, mapping={"expires_at": str(now - 1.0)})
    logger.info("fx rate expired pair=%s", _pair_key(from_cur, to_cur))
    return ExpireRateResponse(
        pair_key=_pair_key(from_cur, to_cur),
        expired=True,
    )


@router.get(
    "/rates/{from_currency}/{to_currency}/history",
    summary="Return up to N historical rate entries for a pair (newest first).",
)
async def rate_history(
    from_currency: str,
    to_currency: str,
    limit: int = Query(default=20, ge=1, le=MAX_HISTORY_PER_PAIR),
    r: aioredis.Redis = Depends(get_redis),
):
    from_currency = _validate_currency(from_currency)
    to_currency = _validate_currency(to_currency)
    raw = await r.lrange(_k_history(from_currency, to_currency), 0, limit - 1)
    out: list[dict[str, Any]] = []
    for entry in raw:
        try:
            out.append(json.loads(entry))
        except (json.JSONDecodeError, TypeError):
            continue
    return {"from_currency": from_currency, "to_currency": to_currency, "entries": out}


@router.get("/health")
async def fx_health(r: aioredis.Redis = Depends(get_redis)):
    pong = await r.ping()
    n_pairs = await r.scard(_K_PAIRS_SET)
    return {
        "ok": bool(pong),
        "configured_pairs": int(n_pairs or 0),
        "supported_currencies": sorted(SUPPORTED_CURRENCIES),
        "default_rate_ttl_seconds": DEFAULT_RATE_TTL_SECONDS,
    }
