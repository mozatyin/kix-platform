"""Merchant Wallet + Billing router.

Brands top up balance (like Google Ads) and spend on campaigns (CPA / CPS /
CPM / CPV). All amounts are integer cents — never floats — to avoid rounding
drift. Charges are atomic via Redis WATCH/MULTI with daily-budget guards and
audit trails. Auto-recharge fires an event when balance drops below a brand
threshold; the actual payment integration is stubbed for MVP.

Redis schema
------------
    wallet:{brand_id}:balance              INT (cents)
    wallet:{brand_id}:currency             STRING (CNY/USD/...)
    wallet:{brand_id}:daily_spent:{date}   INT (EX 86400)
    wallet:{brand_id}:daily_budget         INT (cents, 0 = no cap)
    wallet:{brand_id}:total_spent          INT (lifetime)
    wallet:{brand_id}:last_topup_at        FLOAT (unix ts)
    wallet:{brand_id}:auto_recharge        HASH  (config)
    wallet:{brand_id}:transactions         LIST  (chronological tx_ids)
    wallet:topup:{topup_id}                HASH
    wallet:charge:{charge_id}              HASH
    wallet:refund:{refund_id}              HASH
    wallet:auto_recharge_needed            LIST  (events for worker)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator

from app.api_standards import error_response, list_response, mint_id
from app.i18n.currency import convert as fx_convert
from app.redis_client import get_redis
from app.region import get_region_config, get_primary_currency, is_currency_supported

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ────────────────────────────────────────────────────────────
# Payment methods supported globally. The active region narrows the user-
# facing default list via `app.region.get_payment_methods()`.
SUPPORTED_PAYMENT_METHODS = {
    "alipay", "wechat", "stripe", "paypal",
    "gopay", "ovo", "dana", "paynow", "grabpay",
    "credit_card", "apple_pay", "google_pay", "sepa", "ideal",
}

# Charge reason → category. The reason vocabulary deliberately spans far
# beyond ad-spend (老田 P0) so commerce, ride-hailing, marketplace and
# subscription flows can post against the same wallet primitive.
REASON_CATEGORY_MAP: dict[str, str] = {
    # Ad-spend
    "cpa_conversion": "ad_spend",
    "cps_commission": "ad_spend",
    "cpm_impression": "ad_spend",
    "cpv_visit": "ad_spend",
    "cpe_engagement": "ad_spend",
    # Consumer / marketplace revenue
    "consumer_purchase": "consumer_revenue",
    "marketplace_take_rate": "consumer_revenue",
    "subscription_renewal": "consumer_revenue",
    "deposit_hold": "consumer_revenue",
    "ride_revenue": "consumer_revenue",
    "rental_revenue": "consumer_revenue",
    "service_revenue": "consumer_revenue",
    # Settlements
    "payout_to_merchant": "settlement",
    "refund_to_consumer": "settlement",
    "voucher_redemption": "settlement",
    "dispute_resolution": "settlement",
    # Fees
    "fee_kix_platform": "fee",
    "fee_payment_gateway": "fee",
    "fee_other": "fee",
    # Fallback
    "other": "other",
}

SUPPORTED_CHARGE_REASONS = set(REASON_CATEGORY_MAP.keys())
SUPPORTED_CATEGORIES = ("ad_spend", "consumer_revenue", "settlement", "fee", "other")

# Region-aware default currency. Falls back to CNY when KIX_REGION is unset
# (matches MVP launch region). Override with the active region's primary
# currency for new wallet creations.
DEFAULT_CURRENCY = get_primary_currency() or "CNY"
REFUND_WINDOW_SECONDS = 30 * 24 * 3600  # 30 days
MAX_WATCH_RETRIES = 8
TX_LIST_MAX = 10_000  # cap to bound memory


# ── Key helpers ──────────────────────────────────────────────────────────
def _k_balance(b: str) -> str:
    return f"wallet:{b}:balance"


def _k_currency(b: str) -> str:
    return f"wallet:{b}:currency"


def _k_daily_spent(b: str, day: str) -> str:
    return f"wallet:{b}:daily_spent:{day}"


def _k_daily_budget(b: str) -> str:
    return f"wallet:{b}:daily_budget"


def _k_total_spent(b: str) -> str:
    return f"wallet:{b}:total_spent"


def _k_last_topup(b: str) -> str:
    return f"wallet:{b}:last_topup_at"


def _k_auto_recharge(b: str) -> str:
    return f"wallet:{b}:auto_recharge"


def _k_tx_list(b: str) -> str:
    return f"wallet:{b}:transactions"


def _k_topup(topup_id: str) -> str:
    return f"wallet:topup:{topup_id}"


def _k_charge(charge_id: str) -> str:
    return f"wallet:charge:{charge_id}"


def _k_refund(refund_id: str) -> str:
    return f"wallet:refund:{refund_id}"


_AUTO_RECHARGE_EVENT_LIST = "wallet:auto_recharge_needed"


# ── v2 Auto-recharge schema ──────────────────────────────────────────────
AUTORECHARGE_THRESHOLD_PCT = 0.20
AUTORECHARGE_WARNING_PCT = 0.30
AUTORECHARGE_MIN_THRESHOLD_CENTS = 1_000
AUTORECHARGE_MIN_TOPUP_CENTS = 5_000
AUTORECHARGE_FAILURE_BACKOFF_SEC = 86_400
AUTORECHARGE_TOPUP_DAYS = 7
AUTORECHARGE_TOPUP_RATIO = 0.5


def _k_autorecharge_v2(brand_id: str) -> str:
    return f"brand:{brand_id}:autorecharge"


def _k_autorecharge_v2_opt_out(brand_id: str) -> str:
    return f"brand:{brand_id}:autorecharge:opt_out"


def _k_autorecharge_v2_paused(brand_id: str) -> str:
    return f"brand:{brand_id}:autorecharge:paused_until"


def _k_autorecharge_v2_log(brand_id: str) -> str:
    return f"wallet:{brand_id}:autorecharge_log"


def _k_wallet_low_notif(brand_id: str) -> str:
    return f"notification:brand:{brand_id}:wallet_low"


def _k_autorecharge_failed_notif(brand_id: str) -> str:
    return f"notification:brand:{brand_id}:autorecharge_failed"


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Daily-budget backpressure (sim feedback: log-flood on cap-hit) ────────
#
# When a brand's daily wallet cap (or a campaign's per-day budget) is
# exhausted, the auction must STOP awarding impressions to that brand for
# the rest of the UTC day. Without this, every auction:
#   * picks the brand again,
#   * tries to charge,
#   * gets daily_budget_exceeded,
#   * floods the log with the same 402 stack trace.
#
# Mechanism: set a Redis flag keyed by ``campaign_id`` (and a brand-level
# fallback flag for callers that don't have a campaign in scope) with a
# TTL ending at the next UTC midnight. The auction's ``_has_budget``
# (campaign side) + ``run_auction`` (brand side) short-circuit when the
# flag is set.
BUDGET_BLOCKED_CAMPAIGN_KEY = "auction:campaign:{cid}:budget_blocked"
BUDGET_BLOCKED_BRAND_KEY = "auction:brand:{brand_id}:budget_blocked"
BUDGET_EXHAUSTED_NOTIFICATION_KEY = (
    "notification:brand:{brand_id}:budget_exhausted"
)


def _seconds_until_utc_midnight(now: float | None = None) -> int:
    """Return whole seconds remaining until the next UTC midnight.

    Used as the TTL for budget-blocked flags so they auto-clear at the
    rollover. Always returns at least 1 to avoid a 0-TTL (PERSIST).
    """
    now_dt = (
        datetime.fromtimestamp(now, tz=timezone.utc)
        if now is not None
        else datetime.now(timezone.utc)
    )
    next_midnight = datetime(
        now_dt.year, now_dt.month, now_dt.day, tzinfo=timezone.utc
    ) + timedelta(days=1)
    return max(1, int((next_midnight - now_dt).total_seconds()))


async def _set_budget_blocked(
    r: aioredis.Redis,
    *,
    brand_id: str,
    campaign_id: str | None,
    reason: str,
) -> bool:
    """Set the campaign + brand budget-block flags. Returns True iff this
    call was the first to flip the flag (so callers can fire a single
    notification instead of one-per-charge-attempt).
    """
    ttl = _seconds_until_utc_midnight()
    first_block = False

    brand_key = BUDGET_BLOCKED_BRAND_KEY.format(brand_id=brand_id)
    # NX so the first hit wins and downstream "first time" notification
    # logic doesn't fire twice.
    set_first_brand = await r.set(brand_key, reason, nx=True, ex=ttl)
    if set_first_brand:
        first_block = True

    if campaign_id:
        cam_key = BUDGET_BLOCKED_CAMPAIGN_KEY.format(cid=campaign_id)
        await r.set(cam_key, reason, ex=ttl)

    return bool(first_block)


async def _maybe_emit_budget_exhausted_notification(
    r: aioredis.Redis, brand_id: str, reason: str, *, campaign_id: str | None
) -> None:
    """Emit a once-per-day notification when budget first exhausts.

    Uses ``SET NX EX(until_midnight)`` so re-firing within the same UTC
    day is a no-op. The notification payload mirrors the budget-status
    contract so dashboards can render a single banner without follow-up
    reads.
    """
    import json as _json

    notif_key = BUDGET_EXHAUSTED_NOTIFICATION_KEY.format(brand_id=brand_id)
    ttl = _seconds_until_utc_midnight()
    payload = _json.dumps(
        {
            "brand_id": brand_id,
            "reason": reason,
            "campaign_id": campaign_id,
            "first_seen_at": int(time.time()),
            "unblock_at_ts": int(time.time()) + ttl,
        }
    )
    set_first = await r.set(notif_key, payload, nx=True, ex=ttl)
    if set_first:
        logger.info(
            "budget_exhausted notification emitted brand=%s reason=%s",
            brand_id,
            reason,
        )


# ── Pydantic models ──────────────────────────────────────────────────────
class TopupRequest(BaseModel):
    amount_cents: int = Field(..., gt=0, le=100_000_000)  # ≤ ¥1M / $1M
    payment_method: Literal["alipay", "wechat", "stripe", "paypal"]
    payment_token: str | None = None
    currency: str | None = None  # defaults to existing wallet currency
    # When the payment currency differs from the wallet's base currency and
    # ``auto_fx`` is true, we transparently convert via the FX engine and
    # credit the wallet in its base currency. When false (legacy default for
    # callers that opt in to strict matching), a currency mismatch raises
    # 409 currency_mismatch.
    auto_fx: bool = True

    @field_validator("currency")
    @classmethod
    def _cur(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().upper()
        if len(v) != 3 or not v.isalpha():
            raise ValueError("currency must be a 3-letter ISO code")
        return v


class TopupResponse(BaseModel):
    topup_id: str
    status: Literal["pending", "confirmed"]
    new_balance_cents: int
    amount_cents: int
    currency: str


class TopupConfirmRequest(BaseModel):
    payment_gateway_response: dict = Field(default_factory=dict)


class ChargeRequest(BaseModel):
    amount_cents: int = Field(..., gt=0, le=10_000_000)
    reason: Literal[
        # Ad-spend (existing)
        "cpa_conversion",
        "cps_commission",
        "cpm_impression",
        "cpv_visit",
        "cpe_engagement",
        # Consumer / marketplace revenue
        "consumer_purchase",
        "marketplace_take_rate",
        "subscription_renewal",
        "deposit_hold",
        "ride_revenue",
        "rental_revenue",
        "service_revenue",
        # Settlements
        "payout_to_merchant",
        "refund_to_consumer",
        "dispute_resolution",
        "voucher_redemption",
        # Internal fees
        "fee_kix_platform",
        "fee_payment_gateway",
        "fee_other",
        # Fallback
        "other",
    ]
    # Optional accounting category override; auto-derived from `reason`
    # via REASON_CATEGORY_MAP when not supplied.
    category: Literal[
        "ad_spend", "consumer_revenue", "settlement", "fee", "other"
    ] | None = None
    # When reason="other", caller MUST supply reason_detail for audit trail.
    reason_detail: str | None = Field(default=None, max_length=256)
    # Both optional — server auto-generates a UUID if neither is provided.
    # `idempotency_key` is treated as an alias-fallback for `reference_id` so
    # direct API callers can use whichever name matches their conventions.
    reference_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=128)
    campaign_id: str | None = None
    # FX universal plumbing: when ``currency`` is supplied and differs from
    # the wallet's base currency, ``auto_fx=True`` converts ``amount_cents``
    # via the FX engine and charges in wallet currency. ``auto_fx=False``
    # preserves the strict-match behaviour (409 currency_mismatch).
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    auto_fx: bool = True
    allow_stale_rate: bool = False

    @field_validator("currency")
    @classmethod
    def _cur(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().upper()
        if len(v) != 3 or not v.isalpha():
            raise ValueError("currency must be a 3-letter ISO code")
        return v

    @model_validator(mode="after")
    def _detail_required_for_other(self):
        if self.reason == "other" and (
            not self.reason_detail or not self.reason_detail.strip()
        ):
            raise ValueError(
                "reason_detail is required when reason='other' (audit trail)"
            )
        return self


class ChargeResponse(BaseModel):
    ok: bool
    new_balance_cents: int
    charge_id: str | None = None
    reason: str | None = None  # e.g. "insufficient_funds"
    idempotent: bool = False   # true when reference_id replay returned cached charge


class RefundRequest(BaseModel):
    charge_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=512)


class RefundResponse(BaseModel):
    ok: bool
    new_balance_cents: int
    refund_id: str | None = None


class AutoRechargeConfig(BaseModel):
    enabled: bool
    threshold_cents: int = Field(50_000, ge=0)
    recharge_amount_cents: int = Field(500_000, gt=0)
    payment_method: Literal["alipay", "wechat", "stripe", "paypal"] | None = None
    payment_token: str | None = None


class WalletStatus(BaseModel):
    brand_id: str
    balance_cents: int
    currency: str
    daily_spent_cents: int
    total_spent_cents: int
    last_topup_at: float | None
    auto_recharge_config: AutoRechargeConfig | None


class DailyBudgetRequest(BaseModel):
    daily_budget_cents: int = Field(..., ge=0)  # 0 disables the cap


class DailyBudgetStatus(BaseModel):
    today_spent_cents: int
    today_budget_cents: int
    remaining_cents: int
    # How much more we can still charge today before the cap blocks.
    # Equals `remaining_cents` when a cap is set; -1 when no cap (unlimited).
    would_block_charge_cents: int
    paused: bool


class Transaction(BaseModel):
    id: str
    type: Literal["topup", "charge", "refund"]
    amount_cents: int
    ts: float
    status: str
    reason: str | None = None
    category: str | None = None
    reason_detail: str | None = None
    reference_id: str | None = None
    campaign_id: str | None = None
    payment_method: str | None = None


class Forecast(BaseModel):
    avg_daily_spend_cents: int
    days_until_empty: float | None
    recommendation: Literal["topup_now", "reduce_bids", "add_campaign", "healthy"]


# ── Internal helpers ─────────────────────────────────────────────────────
async def _get_currency(brand_id: str, r: aioredis.Redis) -> str:
    cur = await r.get(_k_currency(brand_id))
    return cur or DEFAULT_CURRENCY


async def _get_brand_locale(brand_id: str, r: aioredis.Redis) -> str:
    """Best-effort lookup of a brand's preferred locale.

    Reads ``brand:{bid}:locale`` (set by the portal) and falls back to
    ``en-SG``. Used by the additive email notification hooks so the
    welcome/wallet/dispute emails go out in the right language.
    """
    val = await r.get(f"brand:{brand_id}:locale")
    if not val:
        return "en-SG"
    if isinstance(val, (bytes, bytearray)):
        val = val.decode()
    return val or "en-SG"


async def _get_auto_recharge_config(
    brand_id: str, r: aioredis.Redis
) -> AutoRechargeConfig | None:
    raw = await r.hgetall(_k_auto_recharge(brand_id))
    if not raw:
        return None
    try:
        return AutoRechargeConfig(
            enabled=raw.get("enabled", "0") == "1",
            threshold_cents=int(raw.get("threshold_cents", "0")),
            recharge_amount_cents=int(raw.get("recharge_amount_cents", "0") or 1),
            payment_method=raw.get("payment_method") or None,
            payment_token=raw.get("payment_token") or None,
        )
    except (ValueError, TypeError):
        logger.warning("Malformed auto_recharge config for brand=%s", brand_id)
        return None


async def _maybe_auto_recharge(
    brand_id: str, new_balance: int, r: aioredis.Redis
) -> None:
    """Emit an auto_recharge event if balance dropped below threshold.

    For MVP we just enqueue; a worker handles the real payment provider.
    """
    cfg = await _get_auto_recharge_config(brand_id, r)
    if cfg is None or not cfg.enabled:
        return
    if new_balance >= cfg.threshold_cents:
        return
    if not cfg.payment_token or not cfg.payment_method:
        return

    # De-dupe: only fire if no pending event already exists for this brand.
    flag = f"wallet:{brand_id}:auto_recharge_pending"
    set_ok = await r.set(flag, "1", nx=True, ex=600)
    if not set_ok:
        return

    event = {
        "brand_id": brand_id,
        "balance_cents": str(new_balance),
        "threshold_cents": str(cfg.threshold_cents),
        "recharge_amount_cents": str(cfg.recharge_amount_cents),
        "payment_method": cfg.payment_method,
        "ts": str(time.time()),
    }
    import json as _json

    await r.rpush(_AUTO_RECHARGE_EVENT_LIST, _json.dumps(event))
    logger.info(
        "auto_recharge_needed emitted brand=%s balance=%s threshold=%s",
        brand_id,
        new_balance,
        cfg.threshold_cents,
    )


# ── v2 Auto-recharge helpers ─────────────────────────────────────────────
class AutoRechargeV2Settings(BaseModel):
    enabled: bool
    threshold_cents: int = Field(..., ge=0)
    topup_cents: int = Field(..., gt=0)
    payment_method_id: str | None = None
    last_triggered_ts: float | None = None


async def _v2_get_settings(brand_id, r):
    raw = await r.hgetall(_k_autorecharge_v2(brand_id))
    if not raw:
        return None
    try:
        return AutoRechargeV2Settings(
            enabled=raw.get("enabled", "0") == "1",
            threshold_cents=int(raw.get("threshold_cents", "0")),
            topup_cents=int(raw.get("topup_cents", "0") or 1),
            payment_method_id=raw.get("payment_method_id") or None,
            last_triggered_ts=(
                float(raw["last_triggered_ts"])
                if raw.get("last_triggered_ts")
                else None
            ),
        )
    except (ValueError, TypeError):
        logger.warning("Malformed autorecharge v2 config brand=%s", brand_id)
        return None


async def _v2_avg_daily_spend_cents(r, brand_id, window_days=7):
    now = time.time()
    window_start = now - window_days * 86400
    ids = await r.lrange(_k_tx_list(brand_id), -5000, -1)
    total = 0
    for tx_id in reversed(ids):
        ch = await r.hgetall(_k_charge(tx_id))
        if not ch:
            continue
        ts = float(ch.get("ts") or 0.0)
        if ts < window_start:
            break
        if ch.get("status") == "completed":
            total += int(ch.get("amount") or 0)
    if total <= 0:
        return 0
    return total // window_days


async def _v2_compute_defaults(r, brand_id):
    avg = await _v2_avg_daily_spend_cents(r, brand_id)
    threshold = max(
        AUTORECHARGE_MIN_THRESHOLD_CENTS,
        int(avg * AUTORECHARGE_THRESHOLD_PCT),
    )
    topup = max(
        AUTORECHARGE_MIN_TOPUP_CENTS,
        int(avg * AUTORECHARGE_TOPUP_DAYS * AUTORECHARGE_TOPUP_RATIO),
    )
    return threshold, topup


async def _v2_get_default_verified_pm(brand_id, r):
    default_pm = await r.get(f"brand:{brand_id}:payment_method:default")
    if default_pm:
        raw = await r.hgetall(f"payment_method:{default_pm}")
        if raw and raw.get("status") == "active" and raw.get("verified") == "1":
            return default_pm
    pm_ids = await r.smembers(f"brand:{brand_id}:payment_methods")
    for pm_id in sorted(pm_ids):
        raw = await r.hgetall(f"payment_method:{pm_id}")
        if raw and raw.get("status") == "active" and raw.get("verified") == "1":
            return pm_id
    return None


async def enable_autorecharge_v2_for_upgrade(brand_id, r):
    """Default-enable on STARTER+ upgrade. Skips if opted out or no verified PM."""
    opted_out = await r.get(_k_autorecharge_v2_opt_out(brand_id))
    if opted_out == "1":
        return None
    pm_id = await _v2_get_default_verified_pm(brand_id, r)
    if not pm_id:
        return None
    threshold, topup = await _v2_compute_defaults(r, brand_id)
    now = time.time()
    await r.hset(
        _k_autorecharge_v2(brand_id),
        mapping={
            "enabled": "1",
            "threshold_cents": str(threshold),
            "topup_cents": str(topup),
            "payment_method_id": pm_id,
            "last_triggered_ts": "",
            "created_at": str(now),
            "updated_at": str(now),
            "source": "tier_upgrade",
        },
    )
    logger.info(
        "autorecharge_v2 default-enabled brand=%s pm=%s threshold=%s topup=%s",
        brand_id, pm_id, threshold, topup,
    )
    return {
        "enabled": True,
        "threshold_cents": threshold,
        "topup_cents": topup,
        "payment_method_id": pm_id,
    }


async def _v2_fire_gateway_recharge(brand_id, pm_id, amount_cents, reference_id, r):
    """Call the payment_methods gateway. Auto-stubbed in dev/CI."""
    from app.routers.payment_methods import _gateway_charge as _pm_gateway_charge

    wallet_currency = await _get_currency(brand_id, r)
    return await _pm_gateway_charge(
        pm_id, amount_cents, wallet_currency, reference_id, r
    )


async def _v2_maybe_trigger(brand_id, new_balance, r):
    """Fire auto-recharge after a charge if balance < threshold."""
    cfg = await _v2_get_settings(brand_id, r)
    if cfg is None or not cfg.enabled or not cfg.payment_method_id:
        return

    warning_at = int(
        cfg.threshold_cents * (AUTORECHARGE_WARNING_PCT / AUTORECHARGE_THRESHOLD_PCT)
    )
    if 0 < new_balance < warning_at:
        await r.set(_k_wallet_low_notif(brand_id), "1", ex=86400)
        # Locale-aware email notification (additive — best-effort; never
        # crashes the auto-recharge path). See app/email_templates/.
        try:
            from app.services.email_template_service import enqueue_email
            await enqueue_email(
                r,
                brand_id=brand_id,
                template_id="wallet_low_balance",
                locale=await _get_brand_locale(brand_id, r),
                brand_name=brand_id,
                balance_display=f"{new_balance / 100:.2f}",
                threshold_display=f"{warning_at / 100:.2f}",
            )
        except Exception as exc:  # pragma: no cover — never block recharge
            logger.warning("wallet_low_email enqueue failed brand=%s: %s", brand_id, exc)

    if new_balance >= cfg.threshold_cents:
        return

    paused_until_raw = await r.get(_k_autorecharge_v2_paused(brand_id))
    if paused_until_raw:
        try:
            if time.time() < float(paused_until_raw):
                return
        except (ValueError, TypeError):
            pass

    now_ts = time.time()
    trigger_bucket = int(now_ts)
    idem_key = f"autorecharge_idem:{brand_id}:{trigger_bucket}"
    claimed = await r.set(idem_key, "1", nx=True, ex=600)
    if not claimed:
        return

    reference_id = f"autorecharge:{brand_id}:{trigger_bucket}"
    try:
        result = await _v2_fire_gateway_recharge(
            brand_id, cfg.payment_method_id, cfg.topup_cents, reference_id, r
        )
    except Exception as exc:
        result = {"success": False, "error": f"exception:{exc.__class__.__name__}"}

    import json as _json
    log_entry = {
        "ts": now_ts,
        "reference_id": reference_id,
        "topup_cents": cfg.topup_cents,
        "threshold_cents": cfg.threshold_cents,
        "balance_at_trigger": new_balance,
        "payment_method_id": cfg.payment_method_id,
        "ok": bool(result.get("success")),
        "error": None if result.get("success") else result.get("error"),
    }
    try:
        await r.rpush(_k_autorecharge_v2_log(brand_id), _json.dumps(log_entry))
        await r.ltrim(_k_autorecharge_v2_log(brand_id), -500, -1)
    except Exception as exc:
        logger.warning("autorecharge_v2 log write failed: %s", exc)

    if result.get("success"):
        await r.incrby(_k_balance(brand_id), cfg.topup_cents)
        await r.hset(
            _k_autorecharge_v2(brand_id),
            mapping={
                "last_triggered_ts": str(now_ts),
                "updated_at": str(now_ts),
            },
        )
        await r.delete(_k_wallet_low_notif(brand_id))
        logger.info(
            "autorecharge_v2 fired brand=%s amount=%s gateway_tx=%s",
            brand_id, cfg.topup_cents, result.get("gateway_tx_id"),
        )
    else:
        await r.set(
            _k_autorecharge_failed_notif(brand_id),
            str(now_ts),
            ex=AUTORECHARGE_FAILURE_BACKOFF_SEC,
        )
        await r.set(
            _k_autorecharge_v2_paused(brand_id),
            str(now_ts + AUTORECHARGE_FAILURE_BACKOFF_SEC),
            ex=AUTORECHARGE_FAILURE_BACKOFF_SEC,
        )
        logger.warning(
            "autorecharge_v2 failed brand=%s error=%s pausing 24h",
            brand_id, result.get("error"),
        )


async def _append_tx(
    r: aioredis.Redis, brand_id: str, tx_id: str, pipe: aioredis.client.Pipeline | None = None
) -> None:
    """Push a tx id to the brand transactions list (chronological)."""
    target = pipe if pipe is not None else r
    target.rpush(_k_tx_list(brand_id), tx_id) if pipe is not None else await r.rpush(
        _k_tx_list(brand_id), tx_id
    )
    # Trim from the left to keep most recent TX_LIST_MAX entries.
    if pipe is not None:
        pipe.ltrim(_k_tx_list(brand_id), -TX_LIST_MAX, -1)
    else:
        await r.ltrim(_k_tx_list(brand_id), -TX_LIST_MAX, -1)


# ── GET /{brand_id} ──────────────────────────────────────────────────────
@router.get("/{brand_id}", response_model=WalletStatus)
async def get_wallet(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> WalletStatus:
    balance = int(await r.get(_k_balance(brand_id)) or 0)
    currency = await _get_currency(brand_id, r)
    daily_spent = int(await r.get(_k_daily_spent(brand_id, _today_str())) or 0)
    total_spent = int(await r.get(_k_total_spent(brand_id)) or 0)
    last_topup = await r.get(_k_last_topup(brand_id))
    last_topup_ts = float(last_topup) if last_topup else None
    auto_cfg = await _get_auto_recharge_config(brand_id, r)

    return WalletStatus(
        brand_id=brand_id,
        balance_cents=balance,
        currency=currency,
        daily_spent_cents=daily_spent,
        total_spent_cents=total_spent,
        last_topup_at=last_topup_ts,
        auto_recharge_config=auto_cfg,
    )


# ── POST /{brand_id}/topup ───────────────────────────────────────────────
@router.post("/{brand_id}/topup", response_model=TopupResponse)
async def create_topup(
    brand_id: str,
    body: TopupRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> TopupResponse:
    """Initiate a top-up. Records as `pending` until /confirm is called.

    In production this is where you'd hit the payment provider to create
    the actual checkout intent and return its client_secret. For MVP we
    just persist the record and let the caller drive confirmation.
    """
    # Lock in the wallet's currency on first ever topup.
    cur_existing = await r.get(_k_currency(brand_id))
    wallet_currency = (cur_existing or body.currency or DEFAULT_CURRENCY).upper()
    payment_currency = (body.currency or wallet_currency).upper()

    if cur_existing is None:
        await r.set(_k_currency(brand_id), wallet_currency)
    elif body.currency and body.currency != cur_existing:
        if not body.auto_fx:
            # api_standards: error_response keeps existing detail fields for
            # backwards compatibility (wallet_currency / requested / hint).
            raise error_response(
                status.HTTP_409_CONFLICT,
                "currency_mismatch",
                message=None,
                wallet_currency=cur_existing,
                requested=body.currency,
                hint="set auto_fx=true to convert via FX engine",
            )

    credited_amount = body.amount_cents
    fx_rate: str | None = None
    fx_stale = False
    fx_expires_at: float | None = None
    if (
        body.auto_fx
        and payment_currency != wallet_currency
    ):
        from app.routers.fx import convert_amount as _fx_convert
        conv = await _fx_convert(
            r,
            body.amount_cents,
            payment_currency,
            wallet_currency,
            allow_stale=False,
        )
        credited_amount = int(conv["equivalent_cents"])
        fx_rate = conv["rate"]
        fx_stale = bool(conv["stale"])
        fx_expires_at = conv["expires_at"]

    # api_standards: KiX ID format (dpt_<22hex>).
    topup_id = mint_id("dpt")
    now = time.time()
    mapping = {
        "topup_id": topup_id,
        "brand_id": brand_id,
        "amount": credited_amount,
        "currency": wallet_currency,
        "payment_amount": body.amount_cents,
        "payment_currency": payment_currency,
        "payment_method": body.payment_method,
        "payment_token": body.payment_token or "",
        "status": "pending",
        "created_at": now,
        "confirmed_at": "",
    }
    if fx_rate is not None:
        mapping["fx_rate"] = fx_rate
        mapping["fx_stale"] = "1" if fx_stale else "0"
        mapping["fx_expires_at"] = str(fx_expires_at or "")
    await r.hset(_k_topup(topup_id), mapping=mapping)

    balance = int(await r.get(_k_balance(brand_id)) or 0)
    return TopupResponse(
        topup_id=topup_id,
        status="pending",
        new_balance_cents=balance,
        amount_cents=credited_amount,
        currency=wallet_currency,
    )


# ── POST /{brand_id}/topup/{topup_id}/confirm ────────────────────────────
@router.post(
    "/{brand_id}/topup/{topup_id}/confirm", response_model=TopupResponse
)
async def confirm_topup(
    brand_id: str,
    topup_id: str,
    body: TopupConfirmRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> TopupResponse:
    """Confirm a pending top-up — idempotent on `topup_id`.

    Called by the payment provider webhook (or after a redirect). The
    second call returns the same record without double-crediting.
    """
    key = _k_topup(topup_id)

    while True:
        try:
            async with r.pipeline(transaction=True) as pipe:
                await pipe.watch(key, _k_balance(brand_id))
                tu = await pipe.hgetall(key)
                if not tu:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail={"error": "topup_not_found", "topup_id": topup_id},
                    )
                if tu.get("brand_id") != brand_id:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail={"error": "brand_mismatch"},
                    )

                amount = int(tu.get("amount") or 0)
                currency = tu.get("currency", DEFAULT_CURRENCY)
                existing_status = tu.get("status", "pending")

                # Idempotent: if already confirmed, just return state.
                if existing_status == "confirmed":
                    await pipe.unwatch()
                    balance = int(await r.get(_k_balance(brand_id)) or 0)
                    return TopupResponse(
                        topup_id=topup_id,
                        status="confirmed",
                        new_balance_cents=balance,
                        amount_cents=amount,
                        currency=currency,
                    )

                if existing_status == "failed":
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={"error": "topup_failed"},
                    )

                now = time.time()
                pipe.multi()
                pipe.incrby(_k_balance(brand_id), amount)
                pipe.set(_k_last_topup(brand_id), now)
                pipe.hset(
                    key,
                    mapping={
                        "status": "confirmed",
                        "confirmed_at": now,
                        "gateway_response": _safe_json(body.payment_gateway_response),
                    },
                )
                pipe.rpush(_k_tx_list(brand_id), topup_id)
                pipe.ltrim(_k_tx_list(brand_id), -TX_LIST_MAX, -1)
                results = await pipe.execute()

                new_balance = int(results[0])
                logger.info(
                    "topup confirmed brand=%s topup_id=%s amount=%s new_balance=%s",
                    brand_id,
                    topup_id,
                    amount,
                    new_balance,
                )
                return TopupResponse(
                    topup_id=topup_id,
                    status="confirmed",
                    new_balance_cents=new_balance,
                    amount_cents=amount,
                    currency=currency,
                )
        except aioredis.WatchError:
            continue


def _safe_json(o: dict) -> str:
    import json as _json

    try:
        return _json.dumps(o, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


# ── POST /{brand_id}/charge ──────────────────────────────────────────────
@router.post("/{brand_id}/charge")
async def charge(
    brand_id: str,
    body: ChargeRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Atomically deduct from balance for a campaign event.

    Returns 402 if balance is insufficient or the brand-wide daily cap is
    exceeded. The Redis transaction uses WATCH on all keys we read so a
    concurrent topup/charge re-runs the optimistic loop.
    """
    today = _today_str()
    balance_key = _k_balance(brand_id)
    daily_key = _k_daily_spent(brand_id, today)
    daily_budget_key = _k_daily_budget(brand_id)
    total_key = _k_total_spent(brand_id)

    # FX universal plumbing: when caller passes ``currency`` different from
    # the wallet's base, convert through the FX engine (auto_fx=True) before
    # any balance / daily-cap math runs. Both legs are persisted on the
    # charge record for audit.
    wallet_currency = await _get_currency(brand_id, r)
    request_currency = (body.currency or wallet_currency).upper()
    charge_amount = body.amount_cents
    fx_rate: str | None = None
    fx_stale = False
    fx_expires_at: float | None = None
    if body.currency and request_currency != wallet_currency:
        if not body.auto_fx:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "currency_mismatch",
                    "wallet_currency": wallet_currency,
                    "requested": request_currency,
                    "hint": "set auto_fx=true to convert via FX engine",
                },
            )
        from app.routers.fx import convert_amount as _fx_convert
        conv = await _fx_convert(
            r,
            body.amount_cents,
            request_currency,
            wallet_currency,
            allow_stale=body.allow_stale_rate,
        )
        charge_amount = int(conv["equivalent_cents"])
        fx_rate = conv["rate"]
        fx_stale = bool(conv["stale"])
        fx_expires_at = conv["expires_at"]

    # reference_id is optional for direct API users. Prefer the explicit
    # `reference_id`; fall back to `idempotency_key`; otherwise mint a UUID
    # so audit / refund flows always have a stable handle.
    ref_id = body.reference_id or body.idempotency_key or uuid4().hex

    # Idempotency guard (老田 bug): same reference_id must NOT produce two
    # distinct charges. We index ref_id → charge_id in Redis for 24h and
    # short-circuit replays before touching the WATCH/MULTI loop. Only
    # applied when the caller supplied an explicit reference_id or
    # idempotency_key (auto-minted UUIDs are never replays).
    explicit_ref = bool(body.reference_id or body.idempotency_key)
    idem_key = f"wallet:{brand_id}:charge_idem:{ref_id}" if explicit_ref else None
    if idem_key is not None:
        existing_charge_id = await r.get(idem_key)
        if existing_charge_id:
            existing = await r.hgetall(_k_charge(existing_charge_id))
            if existing:
                current_balance = int(await r.get(balance_key) or 0)
                return ChargeResponse(
                    ok=True,
                    new_balance_cents=current_balance,
                    charge_id=existing_charge_id,
                    idempotent=True,
                )

    attempts = 0
    while attempts < MAX_WATCH_RETRIES:
        attempts += 1
        try:
            async with r.pipeline(transaction=True) as pipe:
                await pipe.watch(balance_key, daily_key, daily_budget_key)

                balance = int(await pipe.get(balance_key) or 0)
                if balance < charge_amount:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_402_PAYMENT_REQUIRED,
                        detail={
                            "ok": False,
                            "reason": "insufficient_funds",
                            "balance_cents": balance,
                            "amount_cents": charge_amount,
                            "requested_amount_cents": body.amount_cents,
                            "requested_currency": request_currency,
                            "wallet_currency": wallet_currency,
                        },
                    )

                daily_spent = int(await pipe.get(daily_key) or 0)
                daily_budget = int(await pipe.get(daily_budget_key) or 0)
                if (
                    daily_budget > 0
                    and daily_spent + charge_amount > daily_budget
                ):
                    await pipe.unwatch()
                    # ── Auction backpressure ───────────────────────────
                    # Set the brand + (optional) campaign budget-block
                    # flags so subsequent auctions short-circuit instead
                    # of attempting another doomed charge. Done OUTSIDE
                    # the WATCH/MULTI block so a Redis hiccup never
                    # silently extends the budget.
                    try:
                        await _set_budget_blocked(
                            r,
                            brand_id=brand_id,
                            campaign_id=body.campaign_id,
                            reason="daily_budget_exceeded",
                        )
                        # Once-per-day notification (no log-flood).
                        await _maybe_emit_budget_exhausted_notification(
                            r,
                            brand_id,
                            "daily_budget_exceeded",
                            campaign_id=body.campaign_id,
                        )
                    except Exception as exc:  # never break the 402 path
                        logger.warning(
                            "budget backpressure side-effect failed: %s", exc
                        )

                    raise HTTPException(
                        status_code=status.HTTP_402_PAYMENT_REQUIRED,
                        detail={
                            "ok": False,
                            "error": "daily_budget_exceeded",
                            "reason": "daily_budget_exceeded",
                            "daily_spent_cents": daily_spent,
                            "daily_budget_cents": daily_budget,
                            "attempted": charge_amount,
                        },
                    )

                charge_id = uuid4().hex
                now = time.time()
                category = body.category or REASON_CATEGORY_MAP.get(
                    body.reason, "other"
                )

                charge_mapping: dict[str, Any] = {
                    "charge_id": charge_id,
                    "brand_id": brand_id,
                    "amount": charge_amount,
                    "currency": wallet_currency,
                    "reason": body.reason,
                    "category": category,
                    "reason_detail": body.reason_detail or "",
                    "reference_id": ref_id,
                    "campaign_id": body.campaign_id or "",
                    "ts": now,
                    "status": "completed",
                }
                if fx_rate is not None:
                    charge_mapping["requested_amount"] = body.amount_cents
                    charge_mapping["requested_currency"] = request_currency
                    charge_mapping["fx_rate"] = fx_rate
                    charge_mapping["fx_stale"] = "1" if fx_stale else "0"
                    charge_mapping["fx_expires_at"] = str(fx_expires_at or "")

                pipe.multi()
                pipe.decrby(balance_key, charge_amount)
                pipe.incrby(daily_key, charge_amount)
                pipe.expire(daily_key, 86400 + 3600)  # +1h safety overlap
                pipe.incrby(total_key, charge_amount)
                pipe.hset(_k_charge(charge_id), mapping=charge_mapping)
                pipe.rpush(_k_tx_list(brand_id), charge_id)
                pipe.ltrim(_k_tx_list(brand_id), -TX_LIST_MAX, -1)
                await pipe.execute()

                new_balance = balance - charge_amount
                logger.info(
                    "charge brand=%s amount=%s req=%s %s reason=%s new_balance=%s",
                    brand_id,
                    charge_amount,
                    body.amount_cents,
                    request_currency,
                    body.reason,
                    new_balance,
                )

                # Claim the idempotency key now that the charge committed.
                # SET NX so a concurrent racer that lost the WATCH loop but
                # already claimed the ref_id wins; we leave the loser's
                # charge_id intact (the duplicate row will be garbage-
                # collected by ops alerts on idempotent=False replays).
                if idem_key is not None:
                    try:
                        await r.set(idem_key, charge_id, nx=True, ex=86400)
                    except Exception as exc:  # never break the charge path
                        logger.warning("charge_idem claim failed: %s", exc)

                # Best-effort: fire auto-recharge check outside the txn.
                try:
                    await _maybe_auto_recharge(brand_id, new_balance, r)
                except Exception as exc:  # never break the charge path
                    logger.warning("auto_recharge check failed: %s", exc)

                # v2 default-on auto-recharge (STARTER+).
                try:
                    await _v2_maybe_trigger(brand_id, new_balance, r)
                except Exception as exc:  # never break the charge path
                    logger.warning("autorecharge_v2 trigger failed: %s", exc)

                # Outbound webhook fan-out: wallet.charged + balance_low.
                try:
                    from app.routers.webhooks_outbound import (
                        fan_out_webhook_to_brand,
                    )
                    await fan_out_webhook_to_brand(
                        brand_id,
                        "wallet.charged",
                        {
                            "charge_id": charge_id,
                            "amount_cents": charge_amount,
                            "currency": wallet_currency,
                            "reason": body.reason,
                            "category": category,
                            "campaign_id": body.campaign_id or None,
                            "reference_id": ref_id,
                            "new_balance_cents": new_balance,
                        },
                        r,
                    )
                    cfg = await _get_auto_recharge_config(brand_id, r)
                    if cfg is not None and new_balance < cfg.threshold_cents:
                        await fan_out_webhook_to_brand(
                            brand_id,
                            "wallet.balance_low",
                            {
                                "balance_cents": new_balance,
                                "threshold_cents": cfg.threshold_cents,
                                "currency": wallet_currency,
                            },
                            r,
                        )
                except Exception as exc:  # never break the charge path
                    logger.warning("webhook fan-out (charge) failed: %s", exc)

                return ChargeResponse(
                    ok=True,
                    new_balance_cents=new_balance,
                    charge_id=charge_id,
                )
        except aioredis.WatchError:
            continue

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "charge_contention", "attempts": attempts},
    )


# ── POST /{brand_id}/refund ──────────────────────────────────────────────
@router.post("/{brand_id}/refund", response_model=RefundResponse)
async def refund(
    brand_id: str,
    body: RefundRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> RefundResponse:
    """Reverse a charge — credits balance back and reduces daily/total spend.

    Within the refund window. Idempotent on `charge_id` (a second refund
    on the same charge returns 409).
    """
    ckey = _k_charge(body.charge_id)
    balance_key = _k_balance(brand_id)
    today = _today_str()
    daily_key = _k_daily_spent(brand_id, today)
    total_key = _k_total_spent(brand_id)

    attempts = 0
    while attempts < MAX_WATCH_RETRIES:
        attempts += 1
        try:
            async with r.pipeline(transaction=True) as pipe:
                await pipe.watch(ckey, balance_key, daily_key, total_key)

                ch = await pipe.hgetall(ckey)
                if not ch:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail={"error": "charge_not_found"},
                    )
                if ch.get("brand_id") != brand_id:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail={"error": "brand_mismatch"},
                    )
                if ch.get("status") != "completed":
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "error": "charge_not_refundable",
                            "status": ch.get("status"),
                        },
                    )

                ch_ts = float(ch.get("ts") or 0.0)
                if time.time() - ch_ts > REFUND_WINDOW_SECONDS:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "error": "refund_window_expired",
                            "charge_ts": ch_ts,
                            "window_seconds": REFUND_WINDOW_SECONDS,
                        },
                    )

                amount = int(ch.get("amount") or 0)
                refund_id = uuid4().hex
                now = time.time()

                # Refund landed on a different UTC day than the charge?
                # We still credit back to today's daily_spent so brand-day
                # accounting matches actual cash flow today. If you'd rather
                # keep historical days clean, flip this to skip the daily
                # decrement when day-of-charge != today.
                pipe.multi()
                pipe.incrby(balance_key, amount)
                pipe.decrby(daily_key, amount)
                pipe.decrby(total_key, amount)
                pipe.hset(
                    ckey,
                    mapping={
                        "status": "refunded",
                        "refund_id": refund_id,
                        "refunded_at": now,
                    },
                )
                pipe.hset(
                    _k_refund(refund_id),
                    mapping={
                        "refund_id": refund_id,
                        "brand_id": brand_id,
                        "charge_id": body.charge_id,
                        "amount": amount,
                        "reason": body.reason,
                        "ts": now,
                        "status": "completed",
                    },
                )
                pipe.rpush(_k_tx_list(brand_id), refund_id)
                pipe.ltrim(_k_tx_list(brand_id), -TX_LIST_MAX, -1)
                results = await pipe.execute()

                new_balance = int(results[0])
                logger.info(
                    "refund brand=%s charge_id=%s amount=%s new_balance=%s",
                    brand_id,
                    body.charge_id,
                    amount,
                    new_balance,
                )
                return RefundResponse(
                    ok=True,
                    new_balance_cents=new_balance,
                    refund_id=refund_id,
                )
        except aioredis.WatchError:
            continue

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "refund_contention", "attempts": attempts},
    )


# ── POST /{brand_id}/auto-recharge/configure ─────────────────────────────
@router.post(
    "/{brand_id}/auto-recharge/configure", response_model=AutoRechargeConfig
)
async def configure_auto_recharge(
    brand_id: str,
    body: AutoRechargeConfig,
    r: aioredis.Redis = Depends(get_redis),
) -> AutoRechargeConfig:
    if body.enabled and (not body.payment_method or not body.payment_token):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_payment_method_or_token",
                "hint": "enabled=true requires payment_method and payment_token",
            },
        )

    await r.hset(
        _k_auto_recharge(brand_id),
        mapping={
            "enabled": "1" if body.enabled else "0",
            "threshold_cents": body.threshold_cents,
            "recharge_amount_cents": body.recharge_amount_cents,
            "payment_method": body.payment_method or "",
            "payment_token": body.payment_token or "",
            "updated_at": time.time(),
        },
    )
    return body


# ── v2 Auto-recharge endpoints ───────────────────────────────────────────
class AutoRechargeV2UpdateRequest(BaseModel):
    threshold_cents: int | None = Field(default=None, ge=0)
    topup_cents: int | None = Field(default=None, gt=0)
    payment_method_id: str | None = None
    enabled: bool | None = None


class AutoRechargeV2View(BaseModel):
    brand_id: str
    enabled: bool
    threshold_cents: int
    topup_cents: int
    payment_method_id: str | None
    last_triggered_ts: float | None
    opted_out: bool
    paused_until: float | None
    wallet_low_warning: bool


async def _v2_view(brand_id, r):
    cfg = await _v2_get_settings(brand_id, r)
    opted_out = (await r.get(_k_autorecharge_v2_opt_out(brand_id))) == "1"
    paused_raw = await r.get(_k_autorecharge_v2_paused(brand_id))
    paused_until = None
    if paused_raw:
        try:
            paused_until = float(paused_raw)
        except (ValueError, TypeError):
            paused_until = None
    wallet_low = (await r.get(_k_wallet_low_notif(brand_id))) == "1"
    if cfg is None:
        return AutoRechargeV2View(
            brand_id=brand_id, enabled=False, threshold_cents=0, topup_cents=0,
            payment_method_id=None, last_triggered_ts=None, opted_out=opted_out,
            paused_until=paused_until, wallet_low_warning=wallet_low,
        )
    return AutoRechargeV2View(
        brand_id=brand_id, enabled=cfg.enabled,
        threshold_cents=cfg.threshold_cents, topup_cents=cfg.topup_cents,
        payment_method_id=cfg.payment_method_id,
        last_triggered_ts=cfg.last_triggered_ts, opted_out=opted_out,
        paused_until=paused_until, wallet_low_warning=wallet_low,
    )


@router.get("/{brand_id}/autorecharge", response_model=AutoRechargeV2View)
async def get_autorecharge_v2(
    brand_id: str, r: aioredis.Redis = Depends(get_redis),
) -> AutoRechargeV2View:
    """Current default-on auto-recharge settings for a brand."""
    return await _v2_view(brand_id, r)


@router.put("/{brand_id}/autorecharge", response_model=AutoRechargeV2View)
async def update_autorecharge_v2(
    brand_id: str,
    body: AutoRechargeV2UpdateRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> AutoRechargeV2View:
    """Update threshold / topup / payment method. Re-enabling clears opt-out."""
    current = await _v2_get_settings(brand_id, r)
    if current is None:
        threshold, topup = await _v2_compute_defaults(r, brand_id)
        pm_id = await _v2_get_default_verified_pm(brand_id, r)
        current = AutoRechargeV2Settings(
            enabled=False, threshold_cents=threshold, topup_cents=topup,
            payment_method_id=pm_id, last_triggered_ts=None,
        )
    new_enabled = current.enabled if body.enabled is None else body.enabled
    new_threshold = (
        body.threshold_cents
        if body.threshold_cents is not None
        else current.threshold_cents
    )
    new_topup = (
        body.topup_cents if body.topup_cents is not None else current.topup_cents
    )
    new_pm_id = body.payment_method_id or current.payment_method_id
    if new_enabled and not new_pm_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_payment_method",
                "hint": "add a verified payment method before enabling auto-recharge",
            },
        )
    now = time.time()
    await r.hset(
        _k_autorecharge_v2(brand_id),
        mapping={
            "enabled": "1" if new_enabled else "0",
            "threshold_cents": str(new_threshold),
            "topup_cents": str(new_topup),
            "payment_method_id": new_pm_id or "",
            "updated_at": str(now),
        },
    )
    if new_enabled:
        await r.delete(_k_autorecharge_v2_opt_out(brand_id))
    return await _v2_view(brand_id, r)


@router.post(
    "/{brand_id}/autorecharge/disable", response_model=AutoRechargeV2View
)
async def disable_autorecharge_v2(
    brand_id: str, r: aioredis.Redis = Depends(get_redis),
) -> AutoRechargeV2View:
    """Opt out of auto-recharge. Persists across future tier upgrades."""
    await r.hset(
        _k_autorecharge_v2(brand_id),
        mapping={"enabled": "0", "updated_at": str(time.time())},
    )
    await r.set(_k_autorecharge_v2_opt_out(brand_id), "1")
    return await _v2_view(brand_id, r)


@router.post("/{brand_id}/autorecharge/test-trigger")
async def test_trigger_autorecharge_v2(
    brand_id: str, r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Manually fire auto-recharge for testing / merchant validation."""
    cfg = await _v2_get_settings(brand_id, r)
    if cfg is None or not cfg.enabled or not cfg.payment_method_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "autorecharge_not_configured"},
        )
    balance = int(await r.get(_k_balance(brand_id)) or 0)
    await r.delete(_k_autorecharge_v2_paused(brand_id))
    await _v2_maybe_trigger(brand_id, -1, r)
    new_balance = int(await r.get(_k_balance(brand_id)) or 0)
    view = await _v2_view(brand_id, r)
    return {
        "ok": True,
        "previous_balance_cents": balance,
        "new_balance_cents": new_balance,
        "settings": view.model_dump(),
    }


# ── GET /{brand_id}/transactions ─────────────────────────────────────────
@router.get("/{brand_id}/transactions", response_model=list[Transaction])
async def list_transactions(
    brand_id: str,
    from_ts: float | None = Query(None, alias="from"),
    to_ts: float | None = Query(None, alias="to"),
    tx_type: Literal["topup", "charge", "refund"] | None = Query(
        None, alias="type"
    ),
    category: Literal[
        "ad_spend", "consumer_revenue", "settlement", "fee", "other"
    ] | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    r: aioredis.Redis = Depends(get_redis),
) -> list[Transaction]:
    """List transactions sorted by time desc, optionally filtered.

    We over-fetch from the brand list (newest first) and walk until we've
    collected `limit` matches in window. This avoids needing a secondary
    sorted-set index.
    """
    # Pull a slab from the tail (newest). 5× limit headroom for filter
    # misses, capped at TX_LIST_MAX.
    slab = min(limit * 5, TX_LIST_MAX)
    ids = await r.lrange(_k_tx_list(brand_id), -slab, -1)
    ids.reverse()  # newest first

    results: list[Transaction] = []
    for tx_id in ids:
        if len(results) >= limit:
            break

        # Resolve type by probing in order: charge → topup → refund.
        # Cheap because keys are O(1) and most lookups hit on first try.
        tx, kind = await _load_tx(r, tx_id)
        if tx is None:
            continue

        if tx_type and kind != tx_type:
            continue

        ts = float(tx.get("ts") or tx.get("created_at") or 0.0)
        if from_ts is not None and ts < from_ts:
            continue
        if to_ts is not None and ts > to_ts:
            continue

        # Resolve category: persisted field, else derive from reason, else None.
        reason_val = tx.get("reason")
        cat = tx.get("category") or (
            REASON_CATEGORY_MAP.get(reason_val) if reason_val else None
        )
        if category and cat != category:
            continue

        results.append(
            Transaction(
                id=tx_id,
                type=kind,  # type: ignore[arg-type]
                amount_cents=int(tx.get("amount") or 0),
                ts=ts,
                status=tx.get("status", "unknown"),
                reason=reason_val,
                category=cat,
                reason_detail=tx.get("reason_detail") or None,
                reference_id=tx.get("reference_id"),
                campaign_id=tx.get("campaign_id") or None,
                payment_method=tx.get("payment_method") or None,
            )
        )

    return results


async def _load_tx(
    r: aioredis.Redis, tx_id: str
) -> tuple[dict | None, str]:
    """Resolve a tx id to its hash + kind. Returns (None, '') if missing."""
    for kind, k in (
        ("charge", _k_charge(tx_id)),
        ("topup", _k_topup(tx_id)),
        ("refund", _k_refund(tx_id)),
    ):
        h = await r.hgetall(k)
        if h:
            return h, kind
    return None, ""


# ── GET /{brand_id}/summary ──────────────────────────────────────────────
class WalletSummary(BaseModel):
    brand_id: str
    by_category: dict[str, int]
    net: int
    tx_scanned: int


@router.get("/{brand_id}/summary", response_model=WalletSummary)
async def wallet_summary(
    brand_id: str,
    from_ts: float | None = Query(None, alias="from"),
    to_ts: float | None = Query(None, alias="to"),
    scan_limit: int = Query(TX_LIST_MAX, ge=1, le=TX_LIST_MAX),
    r: aioredis.Redis = Depends(get_redis),
) -> WalletSummary:
    """Aggregate by accounting category (sign-aware).

    Sign convention (treasury POV — money leaving the brand wallet is
    negative, money flowing in is positive):
        charge      → negative (debits balance)
        topup       → positive (credits balance)
        refund      → positive (credits balance)
    Categories only apply to charge tx; topup/refund roll under their own
    pseudo-categories ``topup`` and ``refund`` so the rollup is lossless.
    """
    # Pull as much of the tail as the caller permits.
    ids = await r.lrange(_k_tx_list(brand_id), -scan_limit, -1)
    ids.reverse()  # newest first

    by_category: dict[str, int] = {c: 0 for c in SUPPORTED_CATEGORIES}
    by_category["topup"] = 0
    by_category["refund"] = 0
    scanned = 0

    for tx_id in ids:
        tx, kind = await _load_tx(r, tx_id)
        if tx is None:
            continue
        ts = float(tx.get("ts") or tx.get("created_at") or 0.0)
        if from_ts is not None and ts < from_ts:
            continue
        if to_ts is not None and ts > to_ts:
            continue

        amount = int(tx.get("amount") or 0)
        scanned += 1

        if kind == "charge":
            if tx.get("status") != "completed":
                # Refunded / failed charges don't count toward category spend.
                continue
            reason_val = tx.get("reason")
            cat = tx.get("category") or (
                REASON_CATEGORY_MAP.get(reason_val, "other")
                if reason_val
                else "other"
            )
            by_category[cat] = by_category.get(cat, 0) - amount
        elif kind == "topup":
            if tx.get("status") != "confirmed":
                continue
            by_category["topup"] += amount
        elif kind == "refund":
            if tx.get("status") not in ("completed", "refunded"):
                continue
            by_category["refund"] += amount

    net = sum(by_category.values())
    # Strip categories with zero movement for a cleaner payload, but always
    # keep the canonical ad_spend / consumer_revenue / settlement / fee
    # buckets so dashboards have stable keys.
    canonical = set(SUPPORTED_CATEGORIES)
    by_category = {
        k: v for k, v in by_category.items() if v != 0 or k in canonical
    }

    return WalletSummary(
        brand_id=brand_id,
        by_category=by_category,
        net=net,
        tx_scanned=scanned,
    )


# ── GET /{brand_id}/daily-budget-status ──────────────────────────────────
@router.get("/{brand_id}/daily-budget-status", response_model=DailyBudgetStatus)
async def daily_budget_status(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> DailyBudgetStatus:
    today = _today_str()
    spent = int(await r.get(_k_daily_spent(brand_id, today)) or 0)
    budget = int(await r.get(_k_daily_budget(brand_id)) or 0)
    remaining = max(0, budget - spent) if budget > 0 else 0
    paused = budget > 0 and spent >= budget
    # -1 sentinel = no cap (unlimited). Positive = max additional cents that
    # would still pass the cap on the next /charge call.
    would_block = remaining if budget > 0 else -1
    return DailyBudgetStatus(
        today_spent_cents=spent,
        today_budget_cents=budget,
        remaining_cents=remaining,
        would_block_charge_cents=would_block,
        paused=paused,
    )


# ── POST /{brand_id}/daily-budget ────────────────────────────────────────
@router.post("/{brand_id}/daily-budget", response_model=DailyBudgetStatus)
async def set_daily_budget(
    brand_id: str,
    body: DailyBudgetRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> DailyBudgetStatus:
    """Brand-wide daily cap (cents). 0 disables the cap.

    Independent of per-campaign budgets — this is the brand-level circuit
    breaker.
    """
    await r.set(_k_daily_budget(brand_id), body.daily_budget_cents)
    return await daily_budget_status(brand_id, r)


# ── GET /{brand_id}/forecast ─────────────────────────────────────────────
@router.get("/{brand_id}/forecast", response_model=Forecast)
async def forecast(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> Forecast:
    """Naive 7-day-avg forecast — good enough for a dashboard widget.

    Walks the recent transactions list, sums charge amounts in the trailing
    7-day window, divides by 7. days_until_empty = balance / avg.
    """
    balance = int(await r.get(_k_balance(brand_id)) or 0)
    now = time.time()
    window_start = now - 7 * 86400

    # Pull a generous slab — most brands will have << 5000 tx/week.
    ids = await r.lrange(_k_tx_list(brand_id), -5000, -1)
    total_charged = 0
    for tx_id in reversed(ids):
        ch = await r.hgetall(_k_charge(tx_id))
        if not ch:
            continue
        ts = float(ch.get("ts") or 0.0)
        if ts < window_start:
            break  # list is roughly time-ordered; stop scanning old data
        if ch.get("status") == "completed":
            total_charged += int(ch.get("amount") or 0)

    avg_daily = total_charged // 7 if total_charged else 0
    days_left: float | None
    if avg_daily <= 0:
        days_left = None
        rec: Literal[
            "topup_now", "reduce_bids", "add_campaign", "healthy"
        ] = "add_campaign" if balance > 0 else "topup_now"
    else:
        days_left = round(balance / avg_daily, 2)
        if days_left < 3:
            rec = "topup_now"
        elif days_left < 7:
            rec = "reduce_bids"
        else:
            rec = "healthy"

    return Forecast(
        avg_daily_spend_cents=avg_daily,
        days_until_empty=days_left,
        recommendation=rec,
    )


# ── Marketplace take-rate (老胡 P0) ───────────────────────────────────────
#
# For C2C / marketplace brands. Configure a per-category take-rate (basis
# points) plus a minimum fee. On every consumer-to-platform sale, the
# marketplace calls /marketplace-charge with the gross amount; the seller's
# user wallet is debited by `take_amount` and the marketplace brand wallet
# is credited the same amount as platform revenue.

_DEFAULT_TAKE_RATE_BPS = 200       # 2%
_DEFAULT_MIN_FEE_CENTS = 10
_MAX_TAKE_RATE_BPS = 10_000        # 100% guard rail


def _k_take_rate_config(brand_id: str) -> str:
    return f"wallet:{brand_id}:take_rate"


def _k_user_balance(user_id: str) -> str:
    """Per-user wallet balance — sellers' funds for marketplace settlement."""
    return f"wallet:user:{user_id}:balance"


def _k_user_tx_list(user_id: str) -> str:
    return f"wallet:user:{user_id}:transactions"


class CurrencyTakeRate(BaseModel):
    """Per-currency override for marketplace take-rate.

    Lets 老胡 charge a different bps for USD listings vs CNY listings without
    duplicating the whole config. When a charge's currency matches one of
    these keys, the override wins over ``default_rate_bps`` and any matching
    ``category_rates``.
    """
    default_rate_bps: int = Field(..., ge=0, le=_MAX_TAKE_RATE_BPS)
    category_rates: dict[str, int] = Field(default_factory=dict)
    minimum_fee_cents: int | None = Field(default=None, ge=0, le=10_000_000)

    @field_validator("category_rates")
    @classmethod
    def _cat_rates(cls, v: dict[str, int]) -> dict[str, int]:
        for cat, bps in v.items():
            if not isinstance(bps, int) or bps < 0 or bps > _MAX_TAKE_RATE_BPS:
                raise ValueError(
                    f"category_rates[{cat}] must be int 0..{_MAX_TAKE_RATE_BPS}"
                )
            if not cat or len(cat) > 64:
                raise ValueError("category names must be 1..64 chars")
        return v


class TakeRateConfigureRequest(BaseModel):
    default_rate_bps: int = Field(_DEFAULT_TAKE_RATE_BPS, ge=0, le=_MAX_TAKE_RATE_BPS)
    category_rates: dict[str, int] = Field(default_factory=dict)
    minimum_fee_cents: int = Field(_DEFAULT_MIN_FEE_CENTS, ge=0, le=10_000_000)
    # 老胡 P0 extension: per-currency overrides. Keys are 3-letter ISO codes
    # ("USD", "EUR", ...). Each value is a full bps+category bundle that
    # supersedes the default when a transaction lands in that currency.
    currency_rates: dict[str, CurrencyTakeRate] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _expand_int_currency_rates(cls, data: Any) -> Any:
        """Merchant-intuitive alias: a plain int value under ``currency_rates``
        is treated as ``{default_rate_bps: <int>}`` so callers don't need to
        spell out the whole :class:`CurrencyTakeRate` shape for the common
        "just override the default bps for USD" case.
        """
        if not isinstance(data, dict):
            return data
        cr = data.get("currency_rates")
        if not isinstance(cr, dict):
            return data
        expanded: dict[str, Any] = {}
        for k, v in cr.items():
            if isinstance(v, int) and not isinstance(v, bool):
                expanded[k] = {"default_rate_bps": v}
            else:
                expanded[k] = v
        data = {**data, "currency_rates": expanded}
        return data

    @field_validator("category_rates")
    @classmethod
    def _cat_rates(cls, v: dict[str, int]) -> dict[str, int]:
        for cat, bps in v.items():
            if not isinstance(bps, int) or bps < 0 or bps > _MAX_TAKE_RATE_BPS:
                raise ValueError(
                    f"category_rates[{cat}] must be int 0..{_MAX_TAKE_RATE_BPS}"
                )
            if not cat or len(cat) > 64:
                raise ValueError("category names must be 1..64 chars")
        return v

    @field_validator("currency_rates")
    @classmethod
    def _cur_rates(cls, v: dict[str, CurrencyTakeRate]) -> dict[str, CurrencyTakeRate]:
        normalized: dict[str, CurrencyTakeRate] = {}
        for cur, cfg in v.items():
            cur_u = (cur or "").strip().upper()
            if len(cur_u) != 3 or not cur_u.isalpha():
                raise ValueError(f"currency_rates key {cur!r} must be 3-letter ISO")
            normalized[cur_u] = cfg
        return normalized


class TakeRateConfigResponse(BaseModel):
    brand_id: str
    default_rate_bps: int
    category_rates: dict[str, int]
    minimum_fee_cents: int
    currency_rates: dict[str, CurrencyTakeRate] = Field(default_factory=dict)


class MarketplaceChargeRequest(BaseModel):
    # transaction_id is optional now — merchants who don't already mint their
    # own settlement IDs get an auto-generated UUID. Still used for
    # idempotency, so callers that *do* pass one keep their de-dup guarantee.
    transaction_id: str = Field(
        default_factory=lambda: uuid4().hex,
        min_length=1,
        max_length=128,
    )
    listing_id: str = Field(..., min_length=1, max_length=128)
    seller_user_id: str = Field(..., min_length=1, max_length=128)
    buyer_user_id: str = Field(..., min_length=1, max_length=128)
    gross_amount_cents: int = Field(..., ge=0, le=10_000_000_000)
    category: str | None = Field(None, max_length=64)


class MarketplaceChargeResponse(BaseModel):
    ok: bool
    charge_id: str
    transaction_id: str
    listing_id: str
    take_amount_cents: int
    seller_net_cents: int
    take_rate_bps_applied: int
    minimum_fee_applied: bool
    idempotent: bool = False
    seller_balance_cents: int
    marketplace_balance_cents: int


async def _load_take_rate_config(
    r: aioredis.Redis, brand_id: str
) -> dict[str, Any]:
    raw = await r.hgetall(_k_take_rate_config(brand_id))
    if not raw:
        return {
            "default_rate_bps": _DEFAULT_TAKE_RATE_BPS,
            "category_rates": {},
            "minimum_fee_cents": _DEFAULT_MIN_FEE_CENTS,
            "currency_rates": {},
        }
    import json as _json
    try:
        cat_rates = _json.loads(raw.get("category_rates") or "{}")
    except (ValueError, TypeError):
        cat_rates = {}
    try:
        cur_rates = _json.loads(raw.get("currency_rates") or "{}")
    except (ValueError, TypeError):
        cur_rates = {}
    return {
        "default_rate_bps": int(raw.get("default_rate_bps") or _DEFAULT_TAKE_RATE_BPS),
        "category_rates": cat_rates,
        "minimum_fee_cents": int(raw.get("minimum_fee_cents") or _DEFAULT_MIN_FEE_CENTS),
        "currency_rates": cur_rates if isinstance(cur_rates, dict) else {},
    }


def _resolve_take_rate(
    cfg: dict[str, Any],
    *,
    category: str | None,
    currency: str | None,
) -> tuple[int, int]:
    """Pick the bps + minimum_fee that apply to this charge.

    Resolution order (most specific wins):
      1. ``currency_rates[CUR].category_rates[cat]``
      2. ``currency_rates[CUR].default_rate_bps``
      3. ``category_rates[cat]``
      4. ``default_rate_bps``

    minimum_fee_cents follows the same path with a fallback to the top-level
    ``minimum_fee_cents``.
    """
    rate_bps = int(cfg["default_rate_bps"])
    min_fee = int(cfg["minimum_fee_cents"])

    cur_key = (currency or "").strip().upper()
    cur_overrides = cfg.get("currency_rates") or {}
    cur_cfg = cur_overrides.get(cur_key) if cur_key else None

    if cur_cfg:
        # currency_rates entry can be a dict (loaded from JSON) or a model.
        if isinstance(cur_cfg, CurrencyTakeRate):
            cur_cfg = cur_cfg.model_dump()
        rate_bps = int(cur_cfg.get("default_rate_bps") or rate_bps)
        cur_cat = cur_cfg.get("category_rates") or {}
        if category and category in cur_cat:
            rate_bps = int(cur_cat[category])
        if cur_cfg.get("minimum_fee_cents") is not None:
            min_fee = int(cur_cfg["minimum_fee_cents"])
    else:
        cat_rates = cfg.get("category_rates") or {}
        if category and category in cat_rates:
            rate_bps = int(cat_rates[category])

    return rate_bps, min_fee


@router.post(
    "/{brand_id}/take-rate/configure",
    response_model=TakeRateConfigResponse,
    summary="Configure marketplace take-rate (basis points) per category + minimum fee",
)
async def configure_take_rate(
    brand_id: str,
    body: TakeRateConfigureRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> TakeRateConfigResponse:
    import json as _json
    currency_rates_serial = {
        cur: cfg.model_dump() for cur, cfg in body.currency_rates.items()
    }
    await r.hset(
        _k_take_rate_config(brand_id),
        mapping={
            "default_rate_bps": str(body.default_rate_bps),
            "category_rates": _json.dumps(body.category_rates),
            "minimum_fee_cents": str(body.minimum_fee_cents),
            "currency_rates": _json.dumps(currency_rates_serial),
            "updated_at": str(time.time()),
        },
    )
    return TakeRateConfigResponse(
        brand_id=brand_id,
        default_rate_bps=body.default_rate_bps,
        category_rates=body.category_rates,
        minimum_fee_cents=body.minimum_fee_cents,
        currency_rates=body.currency_rates,
    )


@router.get(
    "/{brand_id}/take-rate",
    response_model=TakeRateConfigResponse,
    summary="Get the configured take-rate for a marketplace brand",
)
async def get_take_rate(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> TakeRateConfigResponse:
    cfg = await _load_take_rate_config(r, brand_id)
    # Re-hydrate currency_rates dict-of-dicts back to CurrencyTakeRate models.
    cur_rates_raw = cfg.get("currency_rates") or {}
    cur_rates_models: dict[str, CurrencyTakeRate] = {}
    for cur, sub in cur_rates_raw.items():
        if not isinstance(sub, dict):
            continue
        try:
            cur_rates_models[cur] = CurrencyTakeRate(**sub)
        except Exception:
            continue
    return TakeRateConfigResponse(
        brand_id=brand_id,
        default_rate_bps=cfg["default_rate_bps"],
        category_rates=cfg["category_rates"],
        minimum_fee_cents=cfg["minimum_fee_cents"],
        currency_rates=cur_rates_models,
    )


@router.post(
    "/{brand_id}/marketplace-charge",
    response_model=MarketplaceChargeResponse,
    summary="C2C sale: debit seller's user-wallet, credit marketplace's brand-wallet by take amount",
)
async def marketplace_charge(
    brand_id: str,
    body: MarketplaceChargeRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> MarketplaceChargeResponse:
    """Settle a consumer-to-platform sale.

    Computes::

        take_amount = max(gross × rate_bps / 10_000, minimum_fee_cents)
        seller_net  = gross_amount - take_amount

    Side-effects:
      * decrements ``wallet:user:{seller}:balance`` by ``take_amount``
      * increments ``wallet:{brand}:balance`` by ``take_amount``
      * records charge under ``wallet:charge:{charge_id}`` with
        reason ``marketplace_take_rate`` + category=consumer_revenue
      * idempotent on ``transaction_id`` via
        ``wallet:{brand}:marketplace_idem:{tx_id}`` (24h TTL)

    Note: the seller's *gross* proceeds (gross - take) are not credited
    here — payment-rail settlement to the seller belongs in payouts.
    What we touch is the seller's marketplace fee balance: the convention
    is that sellers maintain a positive user wallet balance to absorb
    take-rate fees; if the balance can't cover the fee, we still post the
    charge (going negative is allowed for accounting) but emit a warning.
    """
    cfg = await _load_take_rate_config(r, brand_id)
    brand_currency = await _get_currency(brand_id, r)
    rate_bps, minimum_fee = _resolve_take_rate(
        cfg, category=body.category, currency=brand_currency
    )

    raw_take = (body.gross_amount_cents * rate_bps) // 10_000
    take_amount = max(raw_take, minimum_fee)
    # Never take more than gross.
    if take_amount > body.gross_amount_cents:
        take_amount = body.gross_amount_cents
    seller_net = body.gross_amount_cents - take_amount
    min_fee_applied = take_amount == minimum_fee and raw_take < minimum_fee

    # Idempotency on transaction_id.
    idem_key = f"wallet:{brand_id}:marketplace_idem:{body.transaction_id}"
    existing_charge_id = await r.get(idem_key)
    if existing_charge_id:
        existing = await r.hgetall(_k_charge(existing_charge_id))
        if existing:
            seller_bal = int(await r.get(_k_user_balance(body.seller_user_id)) or 0)
            mp_bal = int(await r.get(_k_balance(brand_id)) or 0)
            return MarketplaceChargeResponse(
                ok=True,
                charge_id=existing_charge_id,
                transaction_id=body.transaction_id,
                listing_id=body.listing_id,
                take_amount_cents=int(existing.get("amount") or take_amount),
                seller_net_cents=seller_net,
                take_rate_bps_applied=rate_bps,
                minimum_fee_applied=min_fee_applied,
                idempotent=True,
                seller_balance_cents=seller_bal,
                marketplace_balance_cents=mp_bal,
            )

    charge_id = uuid4().hex
    now = time.time()
    today = _today_str()

    pipe = r.pipeline()
    pipe.decrby(_k_user_balance(body.seller_user_id), take_amount)
    pipe.incrby(_k_balance(brand_id), take_amount)
    pipe.incrby(_k_daily_spent(brand_id, today), 0)  # daily_spent is ad-spend
    pipe.hset(
        _k_charge(charge_id),
        mapping={
            "charge_id": charge_id,
            "brand_id": brand_id,
            "amount": take_amount,
            "reason": "marketplace_take_rate",
            "category": "consumer_revenue",
            "reference_id": body.transaction_id,
            "listing_id": body.listing_id,
            "seller_user_id": body.seller_user_id,
            "buyer_user_id": body.buyer_user_id,
            "gross_amount_cents": body.gross_amount_cents,
            "seller_net_cents": seller_net,
            "take_rate_bps": rate_bps,
            "minimum_fee_applied": "1" if min_fee_applied else "0",
            "ts": now,
            "status": "completed",
        },
    )
    pipe.rpush(_k_tx_list(brand_id), charge_id)
    pipe.ltrim(_k_tx_list(brand_id), -TX_LIST_MAX, -1)
    pipe.rpush(_k_user_tx_list(body.seller_user_id), charge_id)
    pipe.ltrim(_k_user_tx_list(body.seller_user_id), -TX_LIST_MAX, -1)
    results = await pipe.execute()
    seller_balance_after = int(results[0])
    marketplace_balance_after = int(results[1])

    # Claim idempotency now that the charge committed.
    try:
        await r.set(idem_key, charge_id, nx=True, ex=86400)
    except Exception as exc:
        logger.warning("marketplace_charge idem claim failed: %s", exc)

    if seller_balance_after < 0:
        logger.warning(
            "marketplace_charge: seller wallet went negative seller=%s balance=%s",
            body.seller_user_id, seller_balance_after,
        )

    logger.info(
        "marketplace_charge brand=%s tx=%s listing=%s seller=%s "
        "gross=%s take=%s rate_bps=%s",
        brand_id, body.transaction_id, body.listing_id, body.seller_user_id,
        body.gross_amount_cents, take_amount, rate_bps,
    )

    return MarketplaceChargeResponse(
        ok=True,
        charge_id=charge_id,
        transaction_id=body.transaction_id,
        listing_id=body.listing_id,
        take_amount_cents=take_amount,
        seller_net_cents=seller_net,
        take_rate_bps_applied=rate_bps,
        minimum_fee_applied=min_fee_applied,
        idempotent=False,
        seller_balance_cents=seller_balance_after,
        marketplace_balance_cents=marketplace_balance_after,
    )


# ── Multi-currency: topup-with-fx (老田 P0) ───────────────────────────────
#
# When a merchant pays in USD but their wallet is denominated in CNY, we
# convert through the FX engine and credit the wallet's base currency.
# Both legs are persisted on the topup record for audit; the user-facing
# `amount_cents` is always the wallet-currency figure.

class TopupWithFxRequest(BaseModel):
    amount_cents: int = Field(..., gt=0, le=100_000_000)
    payment_method: Literal["alipay", "wechat", "stripe", "paypal"]
    payment_token: str | None = None
    payment_currency: str = Field(..., min_length=3, max_length=3)
    # If omitted, defaults to wallet's existing base currency.
    convert_to_currency: str | None = Field(default=None, min_length=3, max_length=3)
    allow_stale_rate: bool = False

    @model_validator(mode="before")
    @classmethod
    def _alias_currency(cls, data: Any) -> Any:
        """Merchant-intuitive alias: accept ``currency`` for ``payment_currency``."""
        if isinstance(data, dict):
            if "currency" in data and "payment_currency" not in data:
                data = {**data, "payment_currency": data["currency"]}
        return data

    @field_validator("payment_currency", "convert_to_currency")
    @classmethod
    def _cur(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().upper()
        if len(v) != 3 or not v.isalpha():
            raise ValueError("currency must be a 3-letter ISO code")
        return v


class TopupWithFxResponse(BaseModel):
    topup_id: str
    status: Literal["pending", "confirmed"]
    payment_amount_cents: int
    payment_currency: str
    credited_amount_cents: int
    wallet_currency: str
    fx_rate: str
    fx_expires_at: float | None
    fx_stale: bool
    new_balance_cents: int


@router.post(
    "/{brand_id}/topup-with-fx",
    response_model=TopupWithFxResponse,
    summary="Top up across currencies. Converts payment_currency → wallet currency via FX engine.",
)
async def topup_with_fx(
    brand_id: str,
    body: TopupWithFxRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> TopupWithFxResponse:
    """Multi-currency top-up.

    Flow:
      1. Resolve wallet base currency (lock on first topup if unset).
      2. Convert ``payment_amount`` → wallet currency via :mod:`fx.convert_amount`.
      3. Persist a pending topup with both legs annotated, then immediately
         confirm (single-shot — caller's payment provider has already
         settled in their own currency).

    For two-phase flows where the payment-provider webhook drives confirm,
    keep using `/topup` and pass the post-FX cents directly.
    """
    from app.routers.fx import convert_amount as _fx_convert

    cur_existing = await r.get(_k_currency(brand_id))
    target_currency = (
        body.convert_to_currency or cur_existing or DEFAULT_CURRENCY
    ).upper()

    if cur_existing is None:
        await r.set(_k_currency(brand_id), target_currency)
    elif body.convert_to_currency and body.convert_to_currency != cur_existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "currency_mismatch",
                "wallet_currency": cur_existing,
                "requested": body.convert_to_currency,
            },
        )

    conv = await _fx_convert(
        r,
        body.amount_cents,
        body.payment_currency,
        target_currency,
        allow_stale=body.allow_stale_rate,
    )
    credited = int(conv["equivalent_cents"])

    topup_id = uuid4().hex
    now = time.time()

    # Persist topup with both legs and commit credit atomically.
    pipe = r.pipeline(transaction=True)
    pipe.hset(
        _k_topup(topup_id),
        mapping={
            "topup_id": topup_id,
            "brand_id": brand_id,
            "amount": credited,
            "currency": target_currency,
            "payment_amount": body.amount_cents,
            "payment_currency": body.payment_currency,
            "fx_rate": conv["rate"],
            "fx_expires_at": str(conv["expires_at"] or ""),
            "fx_stale": "1" if conv["stale"] else "0",
            "payment_method": body.payment_method,
            "payment_token": body.payment_token or "",
            "status": "confirmed",
            "created_at": now,
            "confirmed_at": now,
        },
    )
    pipe.incrby(_k_balance(brand_id), credited)
    pipe.set(_k_last_topup(brand_id), now)
    pipe.rpush(_k_tx_list(brand_id), topup_id)
    pipe.ltrim(_k_tx_list(brand_id), -TX_LIST_MAX, -1)
    results = await pipe.execute()
    new_balance = int(results[1])

    logger.info(
        "topup_with_fx brand=%s pay=%s %s → credited=%s %s rate=%s",
        brand_id, body.amount_cents, body.payment_currency,
        credited, target_currency, conv["rate"],
    )

    return TopupWithFxResponse(
        topup_id=topup_id,
        status="confirmed",
        payment_amount_cents=body.amount_cents,
        payment_currency=body.payment_currency,
        credited_amount_cents=credited,
        wallet_currency=target_currency,
        fx_rate=conv["rate"],
        fx_expires_at=conv["expires_at"],
        fx_stale=bool(conv["stale"]),
        new_balance_cents=new_balance,
    )


# ── Multi-currency marketplace charge ────────────────────────────────────

class MarketplaceChargeMultiCurrencyRequest(BaseModel):
    transaction_id: str = Field(..., min_length=1, max_length=128)
    listing_id: str = Field(..., min_length=1, max_length=128)
    seller_user_id: str = Field(..., min_length=1, max_length=128)
    buyer_user_id: str = Field(..., min_length=1, max_length=128)
    gross_amount_cents: int = Field(..., ge=0, le=10_000_000_000)
    gross_currency: str = Field(..., min_length=3, max_length=3)
    category: str | None = Field(None, max_length=64)
    allow_stale_rate: bool = False

    @field_validator("gross_currency")
    @classmethod
    def _cur(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 3 or not v.isalpha():
            raise ValueError("gross_currency must be a 3-letter ISO code")
        return v


class MarketplaceChargeMultiCurrencyResponse(BaseModel):
    ok: bool
    charge_id: str
    transaction_id: str
    listing_id: str
    gross_amount_cents: int
    gross_currency: str
    gross_in_wallet_currency_cents: int
    wallet_currency: str
    take_amount_cents: int
    take_amount_gross_currency_cents: int
    seller_net_cents: int
    take_rate_bps_applied: int
    minimum_fee_applied: bool
    fx_rate: str
    fx_stale: bool
    idempotent: bool = False
    seller_balance_cents: int
    marketplace_balance_cents: int


@router.post(
    "/{brand_id}/marketplace-charge-multi-currency",
    response_model=MarketplaceChargeMultiCurrencyResponse,
    summary="Marketplace take-rate charge with FX conversion to wallet currency.",
)
async def marketplace_charge_multi_currency(
    brand_id: str,
    body: MarketplaceChargeMultiCurrencyRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> MarketplaceChargeMultiCurrencyResponse:
    """Like :func:`marketplace_charge` but accepts a foreign-currency gross.

    Steps:
      1. Look up wallet base currency.
      2. Convert ``gross_amount_cents`` (in ``gross_currency``) → wallet currency.
      3. Resolve take-rate (honors ``currency_rates`` override on
         ``gross_currency`` if present).
      4. Compute take on the converted wallet-currency amount.
      5. Debit seller / credit marketplace in wallet currency; persist both
         legs.

    Idempotent on ``transaction_id`` like the single-currency variant.
    """
    from app.routers.fx import convert_amount as _fx_convert

    wallet_currency = await _get_currency(brand_id, r)

    if body.gross_currency == wallet_currency:
        # No-op FX — synthesise the conversion record so accounting is uniform.
        conv = {
            "equivalent_cents": body.gross_amount_cents,
            "rate": "1",
            "expires_at": None,
            "stale": False,
        }
    else:
        conv = await _fx_convert(
            r,
            body.gross_amount_cents,
            body.gross_currency,
            wallet_currency,
            allow_stale=body.allow_stale_rate,
        )

    gross_wallet_cents = int(conv["equivalent_cents"])

    cfg = await _load_take_rate_config(r, brand_id)
    # Resolve using the buyer's currency so 老胡's USD-listings override hits.
    rate_bps, minimum_fee = _resolve_take_rate(
        cfg, category=body.category, currency=body.gross_currency
    )

    raw_take_wallet = (gross_wallet_cents * rate_bps) // 10_000
    take_wallet = max(raw_take_wallet, minimum_fee)
    if take_wallet > gross_wallet_cents:
        take_wallet = gross_wallet_cents
    seller_net_wallet = gross_wallet_cents - take_wallet
    min_fee_applied = take_wallet == minimum_fee and raw_take_wallet < minimum_fee

    # Take in gross-currency terms (informational; gross_amount × bps / 10k)
    raw_take_gross = (body.gross_amount_cents * rate_bps) // 10_000

    # Idempotency on transaction_id (shares key with single-currency variant).
    idem_key = f"wallet:{brand_id}:marketplace_idem:{body.transaction_id}"
    existing_charge_id = await r.get(idem_key)
    if existing_charge_id:
        existing = await r.hgetall(_k_charge(existing_charge_id))
        if existing:
            seller_bal = int(await r.get(_k_user_balance(body.seller_user_id)) or 0)
            mp_bal = int(await r.get(_k_balance(brand_id)) or 0)
            return MarketplaceChargeMultiCurrencyResponse(
                ok=True,
                charge_id=existing_charge_id,
                transaction_id=body.transaction_id,
                listing_id=body.listing_id,
                gross_amount_cents=body.gross_amount_cents,
                gross_currency=body.gross_currency,
                gross_in_wallet_currency_cents=gross_wallet_cents,
                wallet_currency=wallet_currency,
                take_amount_cents=int(existing.get("amount") or take_wallet),
                take_amount_gross_currency_cents=raw_take_gross,
                seller_net_cents=seller_net_wallet,
                take_rate_bps_applied=rate_bps,
                minimum_fee_applied=min_fee_applied,
                fx_rate=str(conv["rate"]),
                fx_stale=bool(conv["stale"]),
                idempotent=True,
                seller_balance_cents=seller_bal,
                marketplace_balance_cents=mp_bal,
            )

    charge_id = uuid4().hex
    now = time.time()

    pipe = r.pipeline()
    pipe.decrby(_k_user_balance(body.seller_user_id), take_wallet)
    pipe.incrby(_k_balance(brand_id), take_wallet)
    pipe.hset(
        _k_charge(charge_id),
        mapping={
            "charge_id": charge_id,
            "brand_id": brand_id,
            "amount": take_wallet,
            "currency": wallet_currency,
            "reason": "marketplace_take_rate",
            "category": "consumer_revenue",
            "reference_id": body.transaction_id,
            "listing_id": body.listing_id,
            "seller_user_id": body.seller_user_id,
            "buyer_user_id": body.buyer_user_id,
            "gross_amount_cents": body.gross_amount_cents,
            "gross_currency": body.gross_currency,
            "gross_in_wallet_currency_cents": gross_wallet_cents,
            "seller_net_cents": seller_net_wallet,
            "take_rate_bps": rate_bps,
            "minimum_fee_applied": "1" if min_fee_applied else "0",
            "fx_rate": str(conv["rate"]),
            "fx_stale": "1" if conv["stale"] else "0",
            "ts": now,
            "status": "completed",
        },
    )
    pipe.rpush(_k_tx_list(brand_id), charge_id)
    pipe.ltrim(_k_tx_list(brand_id), -TX_LIST_MAX, -1)
    pipe.rpush(_k_user_tx_list(body.seller_user_id), charge_id)
    pipe.ltrim(_k_user_tx_list(body.seller_user_id), -TX_LIST_MAX, -1)
    results = await pipe.execute()
    seller_balance_after = int(results[0])
    marketplace_balance_after = int(results[1])

    try:
        await r.set(idem_key, charge_id, nx=True, ex=86400)
    except Exception as exc:
        logger.warning("marketplace_charge_mc idem claim failed: %s", exc)

    if seller_balance_after < 0:
        logger.warning(
            "marketplace_charge_mc: seller wallet went negative seller=%s balance=%s",
            body.seller_user_id, seller_balance_after,
        )

    logger.info(
        "marketplace_charge_mc brand=%s tx=%s gross=%s %s → wallet=%s %s "
        "take=%s rate_bps=%s",
        brand_id, body.transaction_id, body.gross_amount_cents,
        body.gross_currency, gross_wallet_cents, wallet_currency,
        take_wallet, rate_bps,
    )

    return MarketplaceChargeMultiCurrencyResponse(
        ok=True,
        charge_id=charge_id,
        transaction_id=body.transaction_id,
        listing_id=body.listing_id,
        gross_amount_cents=body.gross_amount_cents,
        gross_currency=body.gross_currency,
        gross_in_wallet_currency_cents=gross_wallet_cents,
        wallet_currency=wallet_currency,
        take_amount_cents=take_wallet,
        take_amount_gross_currency_cents=raw_take_gross,
        seller_net_cents=seller_net_wallet,
        take_rate_bps_applied=rate_bps,
        minimum_fee_applied=min_fee_applied,
        fx_rate=str(conv["rate"]),
        fx_stale=bool(conv["stale"]),
        idempotent=False,
        seller_balance_cents=seller_balance_after,
        marketplace_balance_cents=marketplace_balance_after,
    )


# ── Internal: commission reversal helper ─────────────────────────────────
#
# Called by disputes / pixel-refund / admin endpoints. Refunds the wallet
# charge then, if the charge accrued commission to a partner brand, claws
# that commission back via the inter-brand ledger.

async def _internal_refund_for_reversal(
    r: aioredis.Redis,
    brand_id: str,
    charge_id: str,
    amount_cents: int,
    reason: str,
) -> tuple[bool, str | None, str | None]:
    """Best-effort refund + commission claw-back.

    Returns ``(ok, refund_id, error)``. Idempotent on ``charge_id`` —
    second call returns the existing refund.
    """
    ckey = _k_charge(charge_id)
    balance_key = _k_balance(brand_id)
    today = _today_str()
    daily_key = _k_daily_spent(brand_id, today)
    total_key = _k_total_spent(brand_id)

    attempts = 0
    while attempts < MAX_WATCH_RETRIES:
        attempts += 1
        try:
            async with r.pipeline(transaction=True) as pipe:
                await pipe.watch(ckey, balance_key, daily_key, total_key)
                ch = await pipe.hgetall(ckey)
                if not ch:
                    await pipe.unwatch()
                    return False, None, "charge_not_found"
                if ch.get("brand_id") != brand_id:
                    await pipe.unwatch()
                    return False, None, "brand_mismatch"
                ch_status = ch.get("status")
                if ch_status == "refunded":
                    await pipe.unwatch()
                    return True, ch.get("last_refund_id") or ch.get("refund_id"), None
                if ch_status not in ("completed", "disputed", "partially_refunded"):
                    await pipe.unwatch()
                    return False, None, f"charge_not_refundable:{ch_status}"

                already = int(ch.get("refunded_amount") or 0)
                ch_amount = int(ch.get("amount") or 0)
                remaining = ch_amount - already
                refund_amt = min(amount_cents, remaining)
                if refund_amt <= 0:
                    await pipe.unwatch()
                    return False, None, "nothing_to_refund"

                refund_id = uuid4().hex
                now = time.time()
                new_refunded = already + refund_amt
                final_status = (
                    "refunded" if new_refunded >= ch_amount else "partially_refunded"
                )

                pipe.multi()
                pipe.incrby(balance_key, refund_amt)
                pipe.decrby(daily_key, refund_amt)
                pipe.decrby(total_key, refund_amt)
                pipe.hset(
                    ckey,
                    mapping={
                        "status": final_status,
                        "refunded_amount": new_refunded,
                        "last_refund_id": refund_id,
                        "last_refunded_at": now,
                    },
                )
                pipe.hset(
                    _k_refund(refund_id),
                    mapping={
                        "refund_id": refund_id,
                        "brand_id": brand_id,
                        "charge_id": charge_id,
                        "amount": refund_amt,
                        "reason": reason,
                        "ts": now,
                        "status": "completed",
                        "source": "reversal",
                    },
                )
                pipe.rpush(_k_tx_list(brand_id), refund_id)
                pipe.ltrim(_k_tx_list(brand_id), -TX_LIST_MAX, -1)
                await pipe.execute()
                return True, refund_id, None
        except aioredis.WatchError:
            continue

    return False, None, "contention"


class ReverseCommissionRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)
    # Defaults to the full charge amount; pass to reverse a partial refund.
    amount_cents: int | None = Field(default=None, ge=1)


class ReverseCommissionResponse(BaseModel):
    ok: bool
    charge_id: str
    refund_id: str | None
    refunded_amount_cents: int
    commission_clawback: dict | None = None
    error: str | None = None


@router.post(
    "/internal/reverse-commission/{charge_id}",
    response_model=ReverseCommissionResponse,
    summary="Refund a charge AND claw back any commission paid to a partner brand.",
)
async def reverse_commission(
    charge_id: str,
    body: ReverseCommissionRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> ReverseCommissionResponse:
    """Admin / internal: refund a charge + reverse paid commission.

    Looks up ``wallet:charge:{charge_id}``. Refunds the brand wallet, and
    if the charge has a ``commission_recipient_brand_id`` field, fires an
    inter-brand transfer in the opposite direction to claw back the
    commission. Idempotent: replays return the original refund_id.
    """
    ch = await r.hgetall(_k_charge(charge_id))
    if not ch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "charge_not_found", "charge_id": charge_id},
        )
    brand_id = ch.get("brand_id", "")
    if not brand_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "charge_missing_brand"},
        )
    ch_amount = int(ch.get("amount") or 0)
    refund_amt = body.amount_cents or ch_amount

    ok, refund_id, err = await _internal_refund_for_reversal(
        r, brand_id, charge_id, refund_amt, reason=body.reason
    )
    if not ok:
        return ReverseCommissionResponse(
            ok=False,
            charge_id=charge_id,
            refund_id=None,
            refunded_amount_cents=0,
            error=err,
        )

    # Optional commission claw-back leg.
    commission_clawback: dict | None = None
    recipient_brand = ch.get("commission_recipient_brand_id") or ""
    commission_paid = int(ch.get("commission_paid_cents") or 0)
    if recipient_brand and commission_paid > 0:
        # Pro-rate the commission claw-back when this is a partial refund.
        if ch_amount > 0 and refund_amt < ch_amount:
            clawback_amt = (commission_paid * refund_amt) // ch_amount
        else:
            clawback_amt = commission_paid

        if clawback_amt > 0:
            try:
                from app.routers.payouts import _inter_brand_transfer_impl
                ledger_entry = await _inter_brand_transfer_impl(
                    r,
                    from_brand_id=recipient_brand,
                    to_brand_id=brand_id,
                    amount_cents=clawback_amt,
                    reason="commission_reversal",
                    reference_id=f"reversal:{charge_id}",
                    metadata={"original_charge_id": charge_id, "refund_id": refund_id},
                )
                commission_clawback = ledger_entry
            except Exception as exc:
                logger.warning(
                    "commission clawback failed charge=%s err=%s", charge_id, exc
                )
                commission_clawback = {"error": str(exc)}

    return ReverseCommissionResponse(
        ok=True,
        charge_id=charge_id,
        refund_id=refund_id,
        refunded_amount_cents=refund_amt,
        commission_clawback=commission_clawback,
    )


# ─────────────────────────────────────────────────────────────────────────
# Multi-currency wallet (additive — does not displace single-currency)
# ─────────────────────────────────────────────────────────────────────────
#
# Redis schema additions (all keyed under the existing wallet namespace
# so cleanup tooling already covers them):
#
#     wallet:brand:{bid}:balances        HASH  field=<CCY>  value=<cents>
#
# The legacy single-currency balance (``wallet:{bid}:balance`` /
# ``wallet:{bid}:currency``) remains the source of truth for the primary
# currency. The HASH below shadows it: writes go to BOTH on primary-
# currency ops, and reads merge the two. Brands that only ever transact
# in one currency see no behavioural change.

_MC_HASH_KEY_FMT = "wallet:brand:{bid}:balances"


def _k_mc_balances(brand_id: str) -> str:
    return _MC_HASH_KEY_FMT.format(bid=brand_id)


async def _mc_get_all_balances(
    r: aioredis.Redis,
    brand_id: str,
) -> dict[str, int]:
    """Return ``{currency: balance_cents}`` for every currency the brand holds.

    Merges the legacy single-currency balance with the multi-currency
    HASH so existing wallets show up even before they're migrated.
    """
    legacy_balance = int(await r.get(_k_balance(brand_id)) or 0)
    legacy_currency = (
        await r.get(_k_currency(brand_id))
    ) or get_primary_currency()
    legacy_currency = legacy_currency.upper()

    raw = await r.hgetall(_k_mc_balances(brand_id))
    out: dict[str, int] = {
        cur.upper(): int(val) for cur, val in raw.items() if val
    }
    # Legacy balance wins when both present (it's the live mutating field).
    if legacy_balance or legacy_currency in out:
        out[legacy_currency] = legacy_balance
    return out


class MultiCurrencyBalances(BaseModel):
    """Response envelope for ``GET /{brand_id}/balances``."""

    brand_id: str
    primary_currency: str
    balances: dict[str, int]  # {ISO: cents}


@router.get("/{brand_id}/balances", response_model=MultiCurrencyBalances)
async def get_multi_currency_balances(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> MultiCurrencyBalances:
    """Return every wallet balance the brand holds, keyed by currency.

    The legacy single-currency endpoint :func:`get_wallet` still works
    and reads the same primary-currency balance — this endpoint is
    additive for brands that operate in multiple currencies.
    """
    balances = await _mc_get_all_balances(r, brand_id)
    primary = (
        await r.get(_k_currency(brand_id))
    ) or get_primary_currency()
    return MultiCurrencyBalances(
        brand_id=brand_id,
        primary_currency=primary.upper(),
        balances=balances,
    )


class ConvertRequest(BaseModel):
    """Inter-currency conversion between two of a brand's wallet balances."""

    from_currency: str = Field(..., min_length=3, max_length=8)
    to_currency: str = Field(..., min_length=3, max_length=8)
    amount_cents: int = Field(..., gt=0, le=10_000_000_000)


class ConvertResponse(BaseModel):
    brand_id: str
    from_currency: str
    to_currency: str
    debited_cents: int
    credited_cents: int
    balances: dict[str, int]
    note: str


@router.post("/{brand_id}/convert", response_model=ConvertResponse)
async def convert_wallet_currency(
    brand_id: str,
    body: ConvertRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> ConvertResponse:
    """Convert between the brand's wallet currencies (STUB FX).

    Uses :func:`app.i18n.currency.convert`, which currently returns the
    input amount with a WARN log (no live FX rate plumbed in). The
    debit/credit still execute against the brand's balances so the API
    contract is real — only the rate is a placeholder.
    """
    src = body.from_currency.upper()
    dst = body.to_currency.upper()
    if src == dst:
        raise error_response(
            status.HTTP_400_BAD_REQUEST,
            "same_currency",
            "from_currency must differ from to_currency",
        )

    balances = await _mc_get_all_balances(r, brand_id)
    src_bal = balances.get(src, 0)
    if src_bal < body.amount_cents:
        raise error_response(
            status.HTTP_409_CONFLICT,
            "insufficient_balance",
            f"wallet {src} balance {src_bal} < requested {body.amount_cents}",
            available_cents=src_bal,
            currency=src,
        )

    credited = fx_convert(body.amount_cents, src, dst)

    # Debit src, credit dst. We mutate the legacy single-currency keys
    # only when src/dst match the brand's pinned currency; everything
    # else lives in the multi-currency HASH.
    primary_raw = await r.get(_k_currency(brand_id))
    primary = (primary_raw or get_primary_currency()).upper()

    async with r.pipeline(transaction=True) as pipe:
        if src == primary:
            pipe.decrby(_k_balance(brand_id), body.amount_cents)
        else:
            pipe.hincrby(_k_mc_balances(brand_id), src, -body.amount_cents)
        if dst == primary:
            pipe.incrby(_k_balance(brand_id), credited)
        else:
            pipe.hincrby(_k_mc_balances(brand_id), dst, credited)
        await pipe.execute()

    new_balances = await _mc_get_all_balances(r, brand_id)
    note = (
        "FX rate stubbed — amount unchanged across currencies; "
        "wire a real FX provider before enabling cross-currency settlement."
    )
    logger.warning(
        "wallet_convert_stub brand=%s %s→%s amount=%s",
        brand_id, src, dst, body.amount_cents,
    )
    return ConvertResponse(
        brand_id=brand_id,
        from_currency=src,
        to_currency=dst,
        debited_cents=body.amount_cents,
        credited_cents=credited,
        balances=new_balances,
        note=note,
    )


@router.get("/{brand_id}/balance")
async def get_primary_balance_alias(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Backwards-compat alias returning only the primary-currency balance.

    Brands that haven't opted into multi-currency walle ts continue to
    hit this — same data, less wire format.
    """
    balance = int(await r.get(_k_balance(brand_id)) or 0)
    primary_raw = await r.get(_k_currency(brand_id))
    primary = (primary_raw or get_primary_currency()).upper()
    return {
        "brand_id": brand_id,
        "currency": primary,
        "balance_cents": balance,
    }


# ── POST /{brand_id}/topup/checkout ──────────────────────────────────────
# Real-money Stripe top-up path. The legacy ``/topup`` + ``/topup/.../confirm``
# endpoints above stay for backwards compatibility (callers that already
# embed payment-gateway state machines, plus tests using
# ``payment_gateway_response={"mock": True}``). New clients should:
#
#   1. POST /topup/checkout  → returns ``{checkout_url}``
#   2. Redirect user to ``checkout_url``
#   3. On payment success, Stripe fires ``payment_intent.succeeded`` to the
#      webhook (``/api/v1/webhooks/stripe``). The handler resolves the
#      pending topup record and credits the wallet exactly once.
#
# Setting ``?mock=true`` on this endpoint forces the legacy direct-credit
# path so dashboard QA can fund a brand without touching Stripe.
class TopupCheckoutRequest(BaseModel):
    amount_cents: int = Field(..., gt=0, le=100_000_000)
    currency: str | None = None
    success_url: str = Field(..., min_length=1, max_length=1024)
    cancel_url: str = Field(..., min_length=1, max_length=1024)

    @field_validator("currency")
    @classmethod
    def _cur(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().upper()
        if len(v) != 3 or not v.isalpha():
            raise ValueError("currency must be a 3-letter ISO code")
        return v


class TopupCheckoutResponse(BaseModel):
    topup_id: str
    checkout_url: str
    session_id: str
    status: Literal["pending", "confirmed"]
    amount_cents: int
    currency: str
    mode: str  # "live" / "test" / "mock"


@router.post("/{brand_id}/topup/checkout", response_model=TopupCheckoutResponse)
async def create_topup_checkout(
    brand_id: str,
    body: TopupCheckoutRequest,
    mock: bool = Query(default=False, description="legacy mock=true bypass"),
    r: aioredis.Redis = Depends(get_redis),
) -> TopupCheckoutResponse:
    """Create a Stripe Checkout session for the wallet top-up.

    Backwards-compat: ``?mock=true`` skips Stripe and immediately credits
    the wallet (matches the original MVP behaviour preserved on
    ``/topup`` + ``/topup/{id}/confirm``).
    """
    from app.services import stripe_live

    cur_existing = await r.get(_k_currency(brand_id))
    wallet_currency = (cur_existing or body.currency or DEFAULT_CURRENCY).upper()
    if cur_existing is None:
        await r.set(_k_currency(brand_id), wallet_currency)

    topup_id = mint_id("dpt")
    now = time.time()

    # ── Mock fast-path: credit immediately, return a sentinel URL ───────
    if mock or stripe_live.is_mock():
        await r.hset(
            _k_topup(topup_id),
            mapping={
                "topup_id": topup_id,
                "brand_id": brand_id,
                "amount": body.amount_cents,
                "currency": wallet_currency,
                "payment_method": "stripe_mock",
                "status": "confirmed",
                "created_at": now,
                "confirmed_at": now,
                "mock": "1",
            },
        )
        await r.incrby(_k_balance(brand_id), body.amount_cents)
        await r.set(_k_last_topup(brand_id), now)
        await r.rpush(_k_tx_list(brand_id), topup_id)
        await r.ltrim(_k_tx_list(brand_id), -TX_LIST_MAX, -1)
        session = stripe_live.create_topup_checkout_session(
            brand_id,
            body.amount_cents,
            body.success_url,
            body.cancel_url,
            currency=wallet_currency,
            reference_id=topup_id,
        )
        return TopupCheckoutResponse(
            topup_id=topup_id,
            checkout_url=session["checkout_url"],
            session_id=session["session_id"],
            status="confirmed",
            amount_cents=body.amount_cents,
            currency=wallet_currency,
            mode="mock",
        )

    # ── Live / test mode: create Checkout, wait for webhook ─────────────
    try:
        session = stripe_live.create_topup_checkout_session(
            brand_id,
            body.amount_cents,
            body.success_url,
            body.cancel_url,
            currency=wallet_currency,
            reference_id=topup_id,
        )
    except Exception as exc:  # noqa: BLE001 — surface to the API as 502
        logger.exception("topup checkout create failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "stripe_checkout_failed", "hint": str(exc)[:200]},
        ) from exc

    await r.hset(
        _k_topup(topup_id),
        mapping={
            "topup_id": topup_id,
            "brand_id": brand_id,
            "amount": body.amount_cents,
            "currency": wallet_currency,
            "payment_method": "stripe",
            "status": "pending",
            "created_at": now,
            "confirmed_at": "",
            "stripe_session_id": session["session_id"],
        },
    )
    # Index session→topup so webhook handlers can resolve the topup.
    await r.set(
        f"stripe_session:{session['session_id']}:topup_id",
        topup_id,
        ex=7 * 86400,
    )

    return TopupCheckoutResponse(
        topup_id=topup_id,
        checkout_url=session["checkout_url"],
        session_id=session["session_id"],
        status="pending",
        amount_cents=body.amount_cents,
        currency=wallet_currency,
        mode=session.get("mode") or stripe_live.get_mode(),
    )

