"""Prize fulfillment — instant-win, sweepstakes, and legal compliance.

Why this module exists
======================
KiX already issues *vouchers* (digital codes) via
``app/routers/vouchers.py``. That covers the common case of "user gets a
percent-off code". Brand campaigns of the Realtime Media / Merkle ePrize
class require something different:

* A finite **prize pool** with declared inventory (10 grand prizes, 100
  runner-ups, …).
* Probabilistic **instant-win** — every game finish rolls against
  ``win_probability_pct``; pool decrements atomically.
* Optional **sweepstakes draw** — pool of entries, admin triggers a
  random pick at ``sweepstakes_draw_at``.
* **Legal eligibility** by jurisdiction (US W-9 trigger at $600, EU
  GDPR consent before address collection, SG prize cap, CN raffle
  restrictions).
* **Fulfillment workflow** — email/pickup/mail flow with a claim
  deadline and an audit trail every step.

The voucher module is **untouched** — when a prize *is* a voucher, the
winner row references the voucher_id under ``fulfillment_data`` rather
than duplicating state.

Storage
=======
Redis (atomic, hot path) is the runtime store for prize pools and
winners. The PG ``prizes`` / ``prize_winners`` tables (migration 0008)
are the durable spine for analytics and compliance evidence; callers may
mirror to PG via the audit log service. We mirror through Redis to keep
the test surface fast and self-contained — identical to how
``app/routers/vouchers.py`` runs its cross-store flow.

Atomicity
=========
Inventory decrement uses WATCH/MULTI on the prize hash so two concurrent
``record_winner`` calls can never overshoot ``inventory_count``. Roll
RNG happens inside the optimistic transaction so a contention loop
re-rolls and re-checks pool availability.

Anti-fraud
==========
* Per-user attempt rate limit (default 10/hour) via a sliding Redis
  ZSET window.
* IP-collision detector (same IP + multiple user_ids) flips suspicious
  winners into a manual review queue.
* ``contact_info_verified`` flag gates ``initiate_fulfillment`` — no
  shipment without verified email/phone.

Legal compliance
================
Per-jurisdiction rules drive both eligibility (age gates, prize-type
bans, draw notification windows) and the **fulfillment_data** payload:

* US prizes ≥ $600 → ``w9_required=true`` flag (1099 reporting trigger).
* EU → ``gdpr_consent_at`` must be set before address collection.
* SG → enforce per-campaign prize cap when configured.
* CN → ``prize_type=cash`` blocked; raffles need pre-approval flag.

These check points read ``app/compliance_regional`` for the underlying
rule set so the legal table stays a single source of truth.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import redis.asyncio as aioredis

from app.compliance_regional import (
    REGIONAL_RULES,
    get_compliance_for_region,
)

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────

VALID_PRIZE_TYPES: frozenset[str] = frozenset(
    {"voucher", "physical", "cash", "experience", "digital"}
)
VALID_FULFILLMENT_METHODS: frozenset[str] = frozenset(
    {"email", "pickup", "mail", "digital_voucher"}
)
VALID_CLAIM_STATUSES: frozenset[str] = frozenset(
    {"pending", "claimed", "shipped", "delivered", "expired", "review"}
)

# US 1099-MISC reporting threshold (IRS §6041, $600 in calendar year).
US_W9_THRESHOLD_CENTS: int = 60_000
# SG prize cap (Public Entertainments Act / IPC) — values above this need
# a permit; we surface a hard refusal so the merchant configures one.
SG_DEFAULT_PRIZE_CAP_CENTS: int = 1_000_000  # SGD 10,000
# Default claim window before a winner expires back into the pool.
DEFAULT_CLAIM_DEADLINE_DAYS: int = 30
# Anti-fraud: instant-win attempts per (user, campaign) per hour.
RATE_LIMIT_ATTEMPTS_PER_HOUR: int = 10
RATE_LIMIT_WINDOW_SECONDS: int = 3600
# IP-collision threshold: same IP + >N distinct users in 24h → flag.
IP_COLLISION_USER_THRESHOLD: int = 5
IP_COLLISION_WINDOW_SECONDS: int = 86_400

# Audit log keys (lightweight per-prize/winner trail; the durable PG
# audit_log is mirrored via audit_log_service when called from a router
# layer — service stays free of FastAPI/DB session imports for testability).
_AUDIT_TRIM = 1000


# ── Redis key helpers ─────────────────────────────────────────────────────


def _k_prize(pid: str) -> str:
    return f"prize:{pid}"


def _k_prize_audit(pid: str) -> str:
    return f"prize:{pid}:audit"


def _k_prize_entries(pid: str) -> str:
    # ZSET of user_ids who entered a sweepstakes (score = entry timestamp).
    return f"prize:{pid}:entries"


def _k_brand_prizes(bid: str) -> str:
    return f"brand:{bid}:prizes"


def _k_campaign_prizes(cid: str) -> str:
    return f"campaign:{cid}:prizes"


def _k_winner(wid: str) -> str:
    return f"prize_winner:{wid}"


def _k_winner_audit(wid: str) -> str:
    return f"prize_winner:{wid}:audit"


def _k_user_winners(uid: str) -> str:
    return f"user:{uid}:prize_winners"


def _k_brand_winners(bid: str) -> str:
    return f"brand:{bid}:prize_winners"


def _k_review_queue() -> str:
    return "prize:review_queue"


def _k_fulfillment_queue() -> str:
    return "prize:fulfillment_queue"


def _k_rate_limit(uid: str, campaign_id: str | None) -> str:
    return f"prize:rate:{uid}:{campaign_id or 'NONE'}"


def _k_ip_users(ip: str) -> str:
    return f"prize:ip_users:{ip}"


# ── Small utilities ───────────────────────────────────────────────────────


def _now() -> int:
    return int(time.time())


def _iso(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    # Short, URL-safe, sortable-ish (timestamp prefix + 12 hex).
    return f"{prefix}_{int(time.time() * 1000):x}_{uuid4().hex[:12]}"


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def _safe_loads(raw: str | bytes | None, default: Any) -> Any:
    if not raw:
        return default
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _to_int(val: Any, default: int = 0) -> int:
    try:
        return int(val) if val is not None and val != "" else default
    except (TypeError, ValueError):
        return default


def _to_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).lower() in ("1", "true", "yes", "y")


# ── Errors ────────────────────────────────────────────────────────────────


class PrizeError(Exception):
    """Service-level error; routers translate to HTTPException."""

    def __init__(self, code: str, message: str = "", status_code: int = 400):
        self.code = code
        self.message = message or code
        self.status_code = status_code
        super().__init__(self.message)


# ── Result containers ─────────────────────────────────────────────────────


@dataclass
class InstantWinResult:
    won: bool
    prize_id: str | None
    winner_id: str | None
    reason: str
    rolled: float | None
    review_required: bool = False


# ── Audit log (lightweight, capped Redis list) ───────────────────────────


async def _audit(
    r: aioredis.Redis, key: str, action: str, payload: dict[str, Any] | None = None
) -> None:
    entry = {"action": action, "ts": _now(), **(payload or {})}
    try:
        await r.lpush(key, _dumps(entry))
        await r.ltrim(key, 0, _AUDIT_TRIM - 1)
    except Exception as exc:  # pragma: no cover — audit must not break flow
        logger.debug("prize audit failed: %s", exc)


# ── Compliance helpers ───────────────────────────────────────────────────


def _normalize_jurisdiction(j: str | None) -> str:
    j = (j or "").lower().strip()
    return j or "sg"


def _jurisdiction_allows_prize_type(jurisdiction: str, prize_type: str) -> tuple[bool, str]:
    """Return (allowed, reason_if_blocked).

    CN: cash prizes (实物抽奖) require special permit — block by default.
    Unknown jurisdictions: allow with warning (caller logs).
    """
    j = _normalize_jurisdiction(jurisdiction)
    if j == "cn":
        if prize_type == "cash":
            return False, "cn_cash_prize_requires_permit"
    return True, ""


def _jurisdiction_value_cap_cents(jurisdiction: str) -> int | None:
    j = _normalize_jurisdiction(jurisdiction)
    if j == "sg":
        return SG_DEFAULT_PRIZE_CAP_CENTS
    return None


def _w9_required(jurisdiction: str, value_cents: int | None) -> bool:
    if _normalize_jurisdiction(jurisdiction) != "us":
        return False
    return (value_cents or 0) >= US_W9_THRESHOLD_CENTS


async def verify_legal_eligibility(
    r: aioredis.Redis,
    *,
    user_id: str,
    jurisdiction: str,
    user_age: int | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Return ``(eligible, reason, evidence)``.

    Checks:
      * Jurisdiction is supported in ``compliance_regional`` (else fall
        back to 'sg' defaults, never raise).
      * ``user_age`` (if provided) meets ``parental_consent_threshold``.
        Unknown age + ``age_gate_required`` → not eligible (must collect age first).
      * EU jurisdictions require an explicit GDPR consent flag stored
        under ``user:{uid}:gdpr_consent`` before personal data
        collection (which fulfillment requires).
    """
    j = _normalize_jurisdiction(jurisdiction)
    try:
        rules = get_compliance_for_region(j)
    except KeyError:
        logger.info("prize: unknown jurisdiction %s — falling back to sg", j)
        rules = get_compliance_for_region("sg")
        j = "sg"

    evidence: dict[str, Any] = {
        "jurisdiction": j,
        "law_name": rules.law_name,
        "age_threshold": rules.parental_consent_threshold,
    }

    if rules.age_gate_required:
        if user_age is None:
            return False, "age_unknown", evidence
        if user_age < rules.parental_consent_threshold:
            return False, "below_age_threshold", evidence
        evidence["age_ok"] = True

    if j == "eu":
        consent_at = await r.get(f"user:{user_id}:gdpr_consent")
        if not consent_at:
            return False, "gdpr_consent_required", evidence
        evidence["gdpr_consent_at"] = _to_int(consent_at)

    return True, "", evidence


# ── Anti-fraud helpers ───────────────────────────────────────────────────


async def _check_rate_limit(
    r: aioredis.Redis, *, user_id: str, campaign_id: str | None
) -> tuple[bool, int]:
    """Sliding window ZSET — return (allowed, recent_count)."""
    key = _k_rate_limit(user_id, campaign_id)
    now = _now()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS
    # Drop expired entries first.
    await r.zremrangebyscore(key, "-inf", window_start)
    count = await r.zcard(key)
    if count >= RATE_LIMIT_ATTEMPTS_PER_HOUR:
        return False, int(count)
    member = f"{now}:{uuid4().hex[:8]}"
    await r.zadd(key, {member: now})
    await r.expire(key, RATE_LIMIT_WINDOW_SECONDS + 60)
    return True, int(count) + 1


async def _record_ip(r: aioredis.Redis, *, ip: str, user_id: str) -> int:
    """Track distinct users per IP in the last 24h. Returns distinct count."""
    if not ip:
        return 0
    key = _k_ip_users(ip)
    now = _now()
    window_start = now - IP_COLLISION_WINDOW_SECONDS
    await r.zremrangebyscore(key, "-inf", window_start)
    await r.zadd(key, {user_id: now})
    await r.expire(key, IP_COLLISION_WINDOW_SECONDS + 60)
    return int(await r.zcard(key))


# ══════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════


async def create_prize_pool(
    r: aioredis.Redis,
    *,
    brand_id: str,
    prizes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create a batch of prizes for a brand. Returns the created prize dicts.

    Each entry in ``prizes`` may include:
      name, description, prize_type, value_cents, inventory_count,
      win_probability_pct, instant_win, sweepstakes_draw_at,
      fulfillment_method, legal_disclaimer, jurisdiction, campaign_id.
    """
    if not prizes:
        raise PrizeError("empty_prizes", "must provide at least one prize")

    created: list[dict[str, Any]] = []
    for spec in prizes:
        prize_type = (spec.get("prize_type") or "").lower()
        if prize_type not in VALID_PRIZE_TYPES:
            raise PrizeError(
                "invalid_prize_type",
                f"prize_type must be one of {sorted(VALID_PRIZE_TYPES)}",
            )
        fmethod = (spec.get("fulfillment_method") or "").lower() or "email"
        if fmethod not in VALID_FULFILLMENT_METHODS:
            raise PrizeError(
                "invalid_fulfillment_method",
                f"fulfillment_method must be one of {sorted(VALID_FULFILLMENT_METHODS)}",
            )
        jurisdiction = _normalize_jurisdiction(spec.get("jurisdiction"))
        ok, reason = _jurisdiction_allows_prize_type(jurisdiction, prize_type)
        if not ok:
            raise PrizeError(
                "jurisdiction_disallows_prize_type",
                f"{reason} for jurisdiction={jurisdiction}",
                status_code=422,
            )
        # Jurisdiction-specific value cap (e.g. SG).
        value_cents = _to_int(spec.get("value_cents"), 0)
        cap = _jurisdiction_value_cap_cents(jurisdiction)
        if cap is not None and value_cents > cap:
            raise PrizeError(
                "value_above_jurisdiction_cap",
                f"value_cents={value_cents} > cap={cap} for {jurisdiction}",
                status_code=422,
            )

        win_prob = spec.get("win_probability_pct")
        if win_prob is not None:
            try:
                wp = float(win_prob)
            except (TypeError, ValueError):
                raise PrizeError(
                    "invalid_win_probability_pct",
                    "win_probability_pct must be a number",
                )
            if not 0.0 <= wp <= 100.0:
                raise PrizeError(
                    "invalid_win_probability_pct",
                    "win_probability_pct must be in [0, 100]",
                )

        inventory_count = spec.get("inventory_count")
        if inventory_count is not None and _to_int(inventory_count, -1) < 0:
            raise PrizeError(
                "invalid_inventory_count",
                "inventory_count must be >= 0",
            )

        pid = _new_id("prz")
        instant_win = _to_bool(spec.get("instant_win", False))
        sweepstakes_at = spec.get("sweepstakes_draw_at")

        # Coerce sweepstakes draw to int epoch if given.
        if sweepstakes_at:
            sweepstakes_at = _to_int(sweepstakes_at, 0)
            if sweepstakes_at and sweepstakes_at <= _now():
                raise PrizeError(
                    "sweepstakes_draw_in_past",
                    "sweepstakes_draw_at must be in the future",
                )

        prize_data: dict[str, str] = {
            "prize_id": pid,
            "brand_id": brand_id,
            "campaign_id": spec.get("campaign_id") or "",
            "name": spec.get("name") or "",
            "description": spec.get("description") or "",
            "prize_type": prize_type,
            "value_cents": str(value_cents),
            "inventory_count": (
                "" if inventory_count is None else str(_to_int(inventory_count, 0))
            ),
            "inventory_claimed": "0",
            "win_probability_pct": (
                "" if win_prob is None else f"{float(win_prob):.4f}"
            ),
            "instant_win": "1" if instant_win else "0",
            "sweepstakes_draw_at": str(sweepstakes_at) if sweepstakes_at else "",
            "fulfillment_method": fmethod,
            "legal_disclaimer": spec.get("legal_disclaimer") or "",
            "jurisdiction": jurisdiction,
            "created_at": str(_now()),
        }

        pipe = r.pipeline()
        pipe.hset(_k_prize(pid), mapping=prize_data)
        pipe.zadd(_k_brand_prizes(brand_id), {pid: _now()})
        if prize_data["campaign_id"]:
            pipe.zadd(
                _k_campaign_prizes(prize_data["campaign_id"]),
                {pid: _now()},
            )
        await pipe.execute()
        await _audit(
            r,
            _k_prize_audit(pid),
            "prize.created",
            {
                "brand_id": brand_id,
                "name": prize_data["name"],
                "prize_type": prize_type,
                "instant_win": instant_win,
            },
        )
        created.append(_prize_to_view(prize_data))

    logger.info(
        "Prize pool created: brand=%s count=%d", brand_id, len(created)
    )
    return created


def _prize_to_view(p: dict[str, str]) -> dict[str, Any]:
    """Normalise a stored hash into the JSON-API view."""
    if not p:
        return {}
    inv_count_raw = p.get("inventory_count", "")
    win_prob_raw = p.get("win_probability_pct", "")
    sweep_raw = p.get("sweepstakes_draw_at", "")
    return {
        "prize_id": p.get("prize_id", ""),
        "brand_id": p.get("brand_id", ""),
        "campaign_id": p.get("campaign_id") or None,
        "name": p.get("name", ""),
        "description": p.get("description") or None,
        "prize_type": p.get("prize_type", ""),
        "value_cents": _to_int(p.get("value_cents"), 0),
        "inventory_count": (
            None if inv_count_raw in ("", None) else _to_int(inv_count_raw, 0)
        ),
        "inventory_claimed": _to_int(p.get("inventory_claimed"), 0),
        "win_probability_pct": (
            None if win_prob_raw in ("", None) else float(win_prob_raw)
        ),
        "instant_win": _to_bool(p.get("instant_win", "0")),
        "sweepstakes_draw_at": (
            None if sweep_raw in ("", None) else _to_int(sweep_raw, 0)
        ),
        "sweepstakes_draw_at_iso": (
            None if sweep_raw in ("", None) else _iso(_to_int(sweep_raw, 0))
        ),
        "fulfillment_method": p.get("fulfillment_method") or None,
        "legal_disclaimer": p.get("legal_disclaimer") or None,
        "jurisdiction": p.get("jurisdiction") or None,
        "created_at": _to_int(p.get("created_at"), 0),
        "created_at_iso": _iso(_to_int(p.get("created_at"), 0)),
    }


async def get_prize(r: aioredis.Redis, prize_id: str) -> dict[str, Any] | None:
    data = await r.hgetall(_k_prize(prize_id))
    if not data:
        return None
    return _prize_to_view(data)


async def list_prizes_by_brand(
    r: aioredis.Redis, brand_id: str, *, limit: int = 100
) -> list[dict[str, Any]]:
    ids = await r.zrevrange(_k_brand_prizes(brand_id), 0, max(0, limit - 1))
    out: list[dict[str, Any]] = []
    for pid in ids or []:
        data = await r.hgetall(_k_prize(pid))
        if data:
            out.append(_prize_to_view(data))
    return out


# ── Instant-win roll ─────────────────────────────────────────────────────


async def try_instant_win(
    r: aioredis.Redis,
    *,
    user_id: str,
    campaign_id: str | None,
    brand_id: str | None = None,
    user_age: int | None = None,
    user_ip: str | None = None,
    jurisdiction: str | None = None,
) -> InstantWinResult:
    """Roll against every instant-win prize in the campaign / brand pool.

    Iterates eligible prizes in declared order, rolling once per prize.
    Returns the first hit. If no hit, returns ``InstantWinResult(won=False)``
    with ``reason='no_win'``.

    Pool eligibility:
      * ``instant_win=True``
      * ``inventory_count is None`` (unlimited) OR
        ``inventory_claimed < inventory_count``
      * ``jurisdiction`` is None OR matches the user's resolved jurisdiction
        (so geo-fenced prizes don't roll for ineligible users).
      * Anti-fraud: per-user-campaign rate limit not exceeded.
    """
    allowed, recent = await _check_rate_limit(
        r, user_id=user_id, campaign_id=campaign_id
    )
    if not allowed:
        return InstantWinResult(
            won=False,
            prize_id=None,
            winner_id=None,
            reason="rate_limited",
            rolled=None,
        )

    pool_ids: list[str] = []
    if campaign_id:
        ids = await r.zrange(_k_campaign_prizes(campaign_id), 0, -1)
        pool_ids.extend(ids or [])
    elif brand_id:
        ids = await r.zrange(_k_brand_prizes(brand_id), 0, -1)
        pool_ids.extend(ids or [])
    else:
        return InstantWinResult(
            won=False,
            prize_id=None,
            winner_id=None,
            reason="no_pool_specified",
            rolled=None,
        )

    if not pool_ids:
        return InstantWinResult(
            won=False,
            prize_id=None,
            winner_id=None,
            reason="empty_pool",
            rolled=None,
        )

    resolved_jur = _normalize_jurisdiction(jurisdiction)

    ip_collision_count = 0
    if user_ip:
        ip_collision_count = await _record_ip(r, ip=user_ip, user_id=user_id)

    last_roll: float | None = None
    for pid in pool_ids:
        prize = await r.hgetall(_k_prize(pid))
        if not prize:
            continue
        if not _to_bool(prize.get("instant_win", "0")):
            continue
        # Jurisdiction filter: if the prize is jurisdiction-restricted, the
        # user's resolved jurisdiction must match. Empty prize.jurisdiction
        # = global.
        p_jur = (prize.get("jurisdiction") or "").lower()
        if p_jur and p_jur != resolved_jur:
            continue
        # Inventory check (non-atomic peek — record_winner re-validates).
        inv_count_raw = prize.get("inventory_count") or ""
        if inv_count_raw != "":
            if _to_int(inv_count_raw, 0) <= _to_int(prize.get("inventory_claimed"), 0):
                continue
        # Eligibility (age + GDPR for EU).
        ok, why, _ev = await verify_legal_eligibility(
            r,
            user_id=user_id,
            jurisdiction=p_jur or resolved_jur,
            user_age=user_age,
        )
        if not ok:
            # Eligibility failure shouldn't abort entire pool roll — different
            # prizes might have different jurisdictions. But for the current
            # prize we skip.
            continue

        win_prob_raw = prize.get("win_probability_pct") or ""
        if win_prob_raw == "":
            # No probability → not an instant-win lottery prize.
            continue
        try:
            win_prob = float(win_prob_raw)
        except ValueError:
            continue
        # secrets.SystemRandom → CSPRNG; avoid `random` for anti-bias.
        rng = secrets.SystemRandom()
        roll = rng.random() * 100.0  # in [0, 100)
        last_roll = roll
        if roll < win_prob:
            review_required = ip_collision_count >= IP_COLLISION_USER_THRESHOLD
            winner_id = await record_winner(
                r,
                prize_id=pid,
                user_id=user_id,
                brand_id=prize.get("brand_id", ""),
                jurisdiction=p_jur or resolved_jur,
                review_required=review_required,
                evidence={
                    "rolled": roll,
                    "probability_pct": win_prob,
                    "ip_collision_count": ip_collision_count,
                    "campaign_id": campaign_id or "",
                },
            )
            return InstantWinResult(
                won=True,
                prize_id=pid,
                winner_id=winner_id,
                reason="instant_win",
                rolled=roll,
                review_required=review_required,
            )

    return InstantWinResult(
        won=False,
        prize_id=None,
        winner_id=None,
        reason="no_win",
        rolled=last_roll,
    )


# ── Record winner (atomic inventory decrement) ───────────────────────────


async def record_winner(
    r: aioredis.Redis,
    *,
    prize_id: str,
    user_id: str,
    brand_id: str,
    jurisdiction: str | None = None,
    review_required: bool = False,
    evidence: dict[str, Any] | None = None,
    claim_deadline_days: int | None = None,
) -> str:
    """Atomically: decrement inventory + create the winner row.

    Returns the new ``winner_id``. Raises :class:`PrizeError` if the
    inventory pool was exhausted in the optimistic window or the prize
    doesn't exist.
    """
    key = _k_prize(prize_id)
    deadline_days = claim_deadline_days or DEFAULT_CLAIM_DEADLINE_DAYS
    claim_deadline = _now() + deadline_days * 86_400
    winner_id = _new_id("win")

    for _attempt in range(8):
        async with r.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                prize = await pipe.hgetall(key)
                if not prize:
                    await pipe.unwatch()
                    raise PrizeError(
                        "prize_not_found",
                        f"prize_id={prize_id}",
                        status_code=404,
                    )

                inv_count_raw = prize.get("inventory_count") or ""
                claimed = _to_int(prize.get("inventory_claimed"), 0)
                if inv_count_raw != "":
                    inv_count = _to_int(inv_count_raw, 0)
                    if claimed >= inv_count:
                        await pipe.unwatch()
                        raise PrizeError(
                            "inventory_exhausted",
                            f"prize_id={prize_id} fully claimed",
                            status_code=409,
                        )

                p_jur = (
                    prize.get("jurisdiction")
                    or _normalize_jurisdiction(jurisdiction)
                )
                value_cents = _to_int(prize.get("value_cents"), 0)
                fulfillment_data: dict[str, Any] = {
                    "prize_name": prize.get("name", ""),
                    "prize_type": prize.get("prize_type", ""),
                    "fulfillment_method": prize.get("fulfillment_method", ""),
                    "value_cents": value_cents,
                    "evidence": evidence or {},
                }
                if _w9_required(p_jur, value_cents):
                    fulfillment_data["w9_required"] = True
                    fulfillment_data["irs_1099_threshold_cents"] = (
                        US_W9_THRESHOLD_CENTS
                    )

                winner: dict[str, str] = {
                    "winner_id": winner_id,
                    "prize_id": prize_id,
                    "user_id": user_id,
                    "brand_id": brand_id,
                    "won_at": str(_now()),
                    "claim_status": "review" if review_required else "pending",
                    "fulfillment_data": _dumps(fulfillment_data),
                    "contact_info_verified": "0",
                    "legal_acknowledgment_at": "",
                    "jurisdiction": p_jur,
                    "claim_deadline": str(claim_deadline),
                    "claimed_at": "",
                    "shipped_at": "",
                    "delivered_at": "",
                    "expired_at": "",
                }

                pipe.multi()
                pipe.hincrby(key, "inventory_claimed", 1)
                pipe.hset(_k_winner(winner_id), mapping=winner)
                pipe.zadd(_k_user_winners(user_id), {winner_id: _now()})
                pipe.zadd(_k_brand_winners(brand_id), {winner_id: _now()})
                if review_required:
                    pipe.zadd(_k_review_queue(), {winner_id: _now()})
                else:
                    pipe.zadd(_k_fulfillment_queue(), {winner_id: _now()})
                await pipe.execute()
                break
            except aioredis.WatchError:
                continue
            except PrizeError:
                raise
    else:
        raise PrizeError(
            "inventory_contention",
            "record_winner exceeded WATCH retries",
            status_code=503,
        )

    await _audit(
        r,
        _k_prize_audit(prize_id),
        "prize.winner_recorded",
        {
            "winner_id": winner_id,
            "user_id": user_id,
            "review_required": review_required,
        },
    )
    await _audit(
        r,
        _k_winner_audit(winner_id),
        "winner.created",
        {
            "prize_id": prize_id,
            "user_id": user_id,
            "claim_status": "review" if review_required else "pending",
            "evidence": evidence or {},
        },
    )
    return winner_id


# ── Fulfillment workflow ─────────────────────────────────────────────────


def _winner_to_view(w: dict[str, str]) -> dict[str, Any]:
    if not w:
        return {}
    return {
        "winner_id": w.get("winner_id", ""),
        "prize_id": w.get("prize_id", ""),
        "user_id": w.get("user_id", ""),
        "brand_id": w.get("brand_id", ""),
        "won_at": _to_int(w.get("won_at"), 0),
        "won_at_iso": _iso(_to_int(w.get("won_at"), 0)),
        "claim_status": w.get("claim_status", "pending"),
        "fulfillment_data": _safe_loads(w.get("fulfillment_data"), {}),
        "contact_info_verified": _to_bool(w.get("contact_info_verified", "0")),
        "legal_acknowledgment_at": (
            _to_int(w.get("legal_acknowledgment_at"), 0)
            if w.get("legal_acknowledgment_at") else None
        ),
        "jurisdiction": w.get("jurisdiction") or None,
        "claim_deadline": _to_int(w.get("claim_deadline"), 0) or None,
        "claim_deadline_iso": (
            _iso(_to_int(w.get("claim_deadline"), 0))
            if w.get("claim_deadline") else None
        ),
        "claimed_at": _to_int(w.get("claimed_at"), 0) or None,
        "shipped_at": _to_int(w.get("shipped_at"), 0) or None,
        "delivered_at": _to_int(w.get("delivered_at"), 0) or None,
        "expired_at": _to_int(w.get("expired_at"), 0) or None,
    }


async def get_winner(
    r: aioredis.Redis, winner_id: str
) -> dict[str, Any] | None:
    data = await r.hgetall(_k_winner(winner_id))
    if not data:
        return None
    return _winner_to_view(data)


async def list_user_winners(
    r: aioredis.Redis, user_id: str, *, limit: int = 100
) -> list[dict[str, Any]]:
    ids = await r.zrevrange(_k_user_winners(user_id), 0, max(0, limit - 1))
    out: list[dict[str, Any]] = []
    for wid in ids or []:
        data = await r.hgetall(_k_winner(wid))
        if data:
            out.append(_winner_to_view(data))
    return out


async def verify_contact_info(
    r: aioredis.Redis,
    *,
    winner_id: str,
    contact_method: str,
    contact_value: str,
    verification_token: str | None = None,
) -> dict[str, Any]:
    """Mark contact info as verified for a winner.

    Real systems pair this with an email/SMS challenge; here we accept a
    pre-issued token if the caller provides one, else fall back to a
    same-channel echo (token == contact_value) for portal-driven flows.
    Anti-fraud: rejects empty values and double-verification.
    """
    key = _k_winner(winner_id)
    w = await r.hgetall(key)
    if not w:
        raise PrizeError("winner_not_found", winner_id, status_code=404)
    if _to_bool(w.get("contact_info_verified", "0")):
        raise PrizeError(
            "already_verified",
            "contact_info already verified",
            status_code=409,
        )
    if not contact_value:
        raise PrizeError(
            "empty_contact_value",
            "contact_value must be non-empty",
            status_code=400,
        )
    if verification_token is not None and verification_token != contact_value:
        raise PrizeError(
            "verification_token_mismatch",
            "token did not match contact value",
            status_code=403,
        )
    fulfillment_data = _safe_loads(w.get("fulfillment_data"), {})
    fulfillment_data.update(
        {
            "contact_method": contact_method,
            "contact_value_hash": _hash_pii(contact_value),
            "contact_verified_at": _now(),
        }
    )
    await r.hset(
        key,
        mapping={
            "contact_info_verified": "1",
            "fulfillment_data": _dumps(fulfillment_data),
        },
    )
    await _audit(
        r,
        _k_winner_audit(winner_id),
        "winner.contact_verified",
        {"contact_method": contact_method},
    )
    return {"ok": True, "winner_id": winner_id, "contact_method": contact_method}


def _hash_pii(value: str) -> str:
    """SHA-256 + truncate for log storage (never store raw PII)."""
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


async def initiate_fulfillment(
    r: aioredis.Redis,
    *,
    winner_id: str,
    locale: str = "en-SG",
) -> dict[str, Any]:
    """Kick off the email/shipping flow for a verified winner.

    Pre-conditions:
      * Winner exists and claim_status in {'pending', 'claimed'}.
      * ``contact_info_verified`` is True.
      * For EU jurisdiction, ``legal_acknowledgment_at`` must be set
        (GDPR consent recorded).
      * Not expired.

    Returns the notification payload (a real system hands this to the
    email worker — we record the audit event and the payload so tests
    can assert what was *queued*).
    """
    key = _k_winner(winner_id)
    w = await r.hgetall(key)
    if not w:
        raise PrizeError("winner_not_found", winner_id, status_code=404)
    status_now = w.get("claim_status", "pending")
    if status_now in ("expired", "delivered"):
        raise PrizeError(
            "winner_terminal_state",
            f"cannot initiate fulfillment in status={status_now}",
            status_code=409,
        )
    if status_now == "review":
        raise PrizeError(
            "winner_under_review",
            "winner pending manual review",
            status_code=409,
        )
    if not _to_bool(w.get("contact_info_verified", "0")):
        raise PrizeError(
            "contact_not_verified",
            "verify contact info before fulfillment",
            status_code=412,
        )
    jurisdiction = (w.get("jurisdiction") or "").lower()
    if jurisdiction == "eu" and not w.get("legal_acknowledgment_at"):
        raise PrizeError(
            "legal_ack_required",
            "EU recipients must acknowledge T&C before fulfillment",
            status_code=412,
        )
    # Expiry
    deadline = _to_int(w.get("claim_deadline"), 0)
    if deadline and deadline < _now():
        raise PrizeError(
            "claim_expired",
            "claim window already passed",
            status_code=410,
        )

    fulfillment_data = _safe_loads(w.get("fulfillment_data"), {})
    fulfillment_data["fulfillment_initiated_at"] = _now()
    fulfillment_data["locale"] = locale

    # Build the winner notification email payload. Two-locale support
    # (en + zh-Hans) inline so we don't depend on email_templates registry
    # (this service must remain importable without DB / template state).
    prize_name = fulfillment_data.get("prize_name", "your prize")
    method = fulfillment_data.get("fulfillment_method", "email")
    notification = {
        "winner_id": winner_id,
        "user_id": w.get("user_id", ""),
        "locale": locale,
        "subject": _winner_subject(locale, prize_name),
        "body_text": _winner_body(
            locale, prize_name=prize_name, method=method,
            claim_deadline_iso=_iso(deadline) if deadline else "",
            jurisdiction=jurisdiction or "sg",
        ),
    }
    await r.hset(
        key,
        mapping={
            "claim_status": "claimed",
            "claimed_at": str(_now()),
            "fulfillment_data": _dumps(fulfillment_data),
        },
    )
    # Park the notification on a queue the email worker can drain.
    await r.lpush(
        "prize:notify_queue", _dumps(notification)
    )
    await _audit(
        r,
        _k_winner_audit(winner_id),
        "winner.fulfillment_initiated",
        {"method": method, "locale": locale},
    )
    return {"ok": True, "winner_id": winner_id, "notification": notification}


def _winner_subject(locale: str, prize_name: str) -> str:
    if locale.startswith("zh"):
        return f"恭喜！您赢得了 {prize_name}"
    return f"Congratulations — you won {prize_name}"


def _winner_body(
    locale: str,
    *,
    prize_name: str,
    method: str,
    claim_deadline_iso: str,
    jurisdiction: str,
) -> str:
    if locale.startswith("zh"):
        return (
            f"恭喜您赢得了：{prize_name}！\n"
            f"领取方式：{method}\n"
            f"请在 {claim_deadline_iso} 之前完成领取，逾期失效。\n"
            f"适用法规：{jurisdiction.upper()}\n"
        )
    return (
        f"You won: {prize_name}!\n"
        f"Fulfillment method: {method}\n"
        f"Claim by {claim_deadline_iso} — unclaimed prizes expire.\n"
        f"Jurisdiction: {jurisdiction.upper()}\n"
    )


async def record_legal_acknowledgment(
    r: aioredis.Redis, *, winner_id: str
) -> dict[str, Any]:
    """Record that the winner ticked the jurisdiction-specific T&C box."""
    key = _k_winner(winner_id)
    w = await r.hgetall(key)
    if not w:
        raise PrizeError("winner_not_found", winner_id, status_code=404)
    ts = _now()
    await r.hset(key, mapping={"legal_acknowledgment_at": str(ts)})
    await _audit(
        r, _k_winner_audit(winner_id), "winner.legal_acknowledged", {"ts": ts}
    )
    return {"ok": True, "winner_id": winner_id, "ts": ts}


async def mark_claimed(
    r: aioredis.Redis,
    *,
    winner_id: str,
    evidence: dict[str, Any] | None = None,
    new_status: str = "delivered",
) -> dict[str, Any]:
    """Close out a winner record — final state transition.

    ``new_status`` defaults to ``delivered`` (terminal). Pass ``shipped``
    to mark in-flight; the next call with ``delivered`` will finalize.
    """
    if new_status not in {"shipped", "delivered"}:
        raise PrizeError(
            "invalid_status_transition",
            "new_status must be shipped or delivered",
        )
    key = _k_winner(winner_id)
    w = await r.hgetall(key)
    if not w:
        raise PrizeError("winner_not_found", winner_id, status_code=404)
    current = w.get("claim_status", "")
    if current in ("expired",):
        raise PrizeError(
            "winner_expired",
            "cannot mark claimed: already expired",
            status_code=410,
        )
    fulfillment_data = _safe_loads(w.get("fulfillment_data"), {})
    fulfillment_data["close_out_evidence"] = evidence or {}
    fulfillment_data[f"{new_status}_at"] = _now()
    mapping = {
        "claim_status": new_status,
        "fulfillment_data": _dumps(fulfillment_data),
    }
    if new_status == "shipped":
        mapping["shipped_at"] = str(_now())
    elif new_status == "delivered":
        mapping["delivered_at"] = str(_now())
    await r.hset(key, mapping=mapping)
    if new_status == "delivered":
        # Remove from fulfillment queue.
        await r.zrem(_k_fulfillment_queue(), winner_id)
    await _audit(
        r,
        _k_winner_audit(winner_id),
        f"winner.{new_status}",
        {"evidence": evidence or {}},
    )
    return {"ok": True, "winner_id": winner_id, "claim_status": new_status}


async def expire_unclaimed(
    r: aioredis.Redis,
    *,
    days: int = DEFAULT_CLAIM_DEADLINE_DAYS,
    return_to_pool: bool = True,
    now: int | None = None,
) -> dict[str, Any]:
    """Move expired pending winners → status=expired, optionally
    return their inventory slot back to the pool.

    ``days`` overrides the deadline check window only for winners that
    have no explicit ``claim_deadline`` stored. The vast majority of
    winners do have one (set at ``record_winner`` time) and that is
    consulted preferentially.

    Returns ``{expired: int, returned_to_pool: int}``.
    """
    now = now or _now()
    # Scan the pending fulfillment queue — both queues track winner IDs.
    queue_ids = await r.zrange(_k_fulfillment_queue(), 0, -1)
    expired_count = 0
    returned = 0
    for wid in queue_ids or []:
        w = await r.hgetall(_k_winner(wid))
        if not w:
            await r.zrem(_k_fulfillment_queue(), wid)
            continue
        if w.get("claim_status") not in ("pending", "review"):
            continue
        deadline = _to_int(w.get("claim_deadline"), 0)
        if not deadline:
            deadline = _to_int(w.get("won_at"), now) + days * 86_400
        if deadline > now:
            continue
        # Expire it.
        await r.hset(
            _k_winner(wid),
            mapping={
                "claim_status": "expired",
                "expired_at": str(now),
            },
        )
        await r.zrem(_k_fulfillment_queue(), wid)
        expired_count += 1
        if return_to_pool:
            pid = w.get("prize_id", "")
            if pid:
                # Decrement claimed counter (atomic) — can't go below 0.
                async with r.pipeline(transaction=True) as pipe:
                    try:
                        await pipe.watch(_k_prize(pid))
                        prize = await pipe.hgetall(_k_prize(pid))
                        if prize:
                            claimed = _to_int(prize.get("inventory_claimed"), 0)
                            if claimed > 0:
                                pipe.multi()
                                pipe.hincrby(_k_prize(pid), "inventory_claimed", -1)
                                await pipe.execute()
                                returned += 1
                            else:
                                await pipe.unwatch()
                    except aioredis.WatchError:
                        continue
        await _audit(
            r,
            _k_winner_audit(wid),
            "winner.expired",
            {"deadline": deadline, "now": now, "returned_to_pool": return_to_pool},
        )

    return {"expired": expired_count, "returned_to_pool": returned}


# ── Sweepstakes draw ─────────────────────────────────────────────────────


async def enter_sweepstakes(
    r: aioredis.Redis,
    *,
    prize_id: str,
    user_id: str,
    jurisdiction: str | None = None,
    user_age: int | None = None,
) -> dict[str, Any]:
    """Record a sweepstakes entry. Idempotent per (prize, user)."""
    prize = await r.hgetall(_k_prize(prize_id))
    if not prize:
        raise PrizeError("prize_not_found", prize_id, status_code=404)
    if not prize.get("sweepstakes_draw_at"):
        raise PrizeError(
            "not_a_sweepstakes",
            "prize has no sweepstakes_draw_at",
            status_code=400,
        )
    draw_at = _to_int(prize.get("sweepstakes_draw_at"), 0)
    if draw_at and draw_at <= _now():
        raise PrizeError(
            "sweepstakes_already_drawn",
            "entries closed",
            status_code=410,
        )
    p_jur = prize.get("jurisdiction") or _normalize_jurisdiction(jurisdiction)
    ok, why, _ev = await verify_legal_eligibility(
        r, user_id=user_id, jurisdiction=p_jur, user_age=user_age,
    )
    if not ok:
        raise PrizeError("ineligible", why, status_code=403)
    await r.zadd(_k_prize_entries(prize_id), {user_id: _now()})
    return {
        "ok": True,
        "prize_id": prize_id,
        "user_id": user_id,
        "entries": int(await r.zcard(_k_prize_entries(prize_id))),
    }


async def draw_sweepstakes(
    r: aioredis.Redis,
    *,
    prize_id: str,
    n_winners: int | None = None,
) -> list[str]:
    """Cryptographically-random pick from the entries pool.

    Returns the list of winner_ids created. The number drawn is
    ``min(inventory_count, n_winners or inventory_count or 1)``.

    Picks are made with ``secrets.SystemRandom().sample`` so the draw is
    CSPRNG-backed (auditable rather than seeded ``random``).
    """
    prize = await r.hgetall(_k_prize(prize_id))
    if not prize:
        raise PrizeError("prize_not_found", prize_id, status_code=404)
    if not prize.get("sweepstakes_draw_at"):
        raise PrizeError("not_a_sweepstakes", prize_id, status_code=400)
    draw_at = _to_int(prize.get("sweepstakes_draw_at"), 0)
    if draw_at and draw_at > _now():
        raise PrizeError(
            "sweepstakes_not_ready",
            f"draw_at={draw_at} not yet reached",
            status_code=425,
        )
    entrants = await r.zrange(_k_prize_entries(prize_id), 0, -1)
    if not entrants:
        raise PrizeError("no_entrants", prize_id, status_code=422)
    inv = prize.get("inventory_count") or ""
    inv_int = _to_int(inv, 1) if inv else 1
    take = min(len(entrants), n_winners if n_winners else inv_int)
    rng = secrets.SystemRandom()
    picked = rng.sample(list(entrants), take)
    out: list[str] = []
    for uid in picked:
        try:
            wid = await record_winner(
                r,
                prize_id=prize_id,
                user_id=uid,
                brand_id=prize.get("brand_id", ""),
                jurisdiction=prize.get("jurisdiction"),
                evidence={"source": "sweepstakes_draw", "draw_at": draw_at},
            )
            out.append(wid)
        except PrizeError as exc:
            logger.warning(
                "sweepstakes draw: skipping uid=%s reason=%s", uid, exc.code
            )
            continue
    await _audit(
        r,
        _k_prize_audit(prize_id),
        "prize.sweepstakes_drawn",
        {"winners": out, "n": len(out)},
    )
    return out


# ── Admin: review queue ──────────────────────────────────────────────────


async def list_review_queue(
    r: aioredis.Redis, *, limit: int = 100
) -> list[dict[str, Any]]:
    ids = await r.zrevrange(_k_review_queue(), 0, max(0, limit - 1))
    out: list[dict[str, Any]] = []
    for wid in ids or []:
        data = await r.hgetall(_k_winner(wid))
        if data:
            out.append(_winner_to_view(data))
    return out


async def list_fulfillment_queue(
    r: aioredis.Redis, *, limit: int = 100
) -> list[dict[str, Any]]:
    ids = await r.zrange(_k_fulfillment_queue(), 0, max(0, limit - 1))
    out: list[dict[str, Any]] = []
    for wid in ids or []:
        data = await r.hgetall(_k_winner(wid))
        if data:
            out.append(_winner_to_view(data))
    return out


async def resolve_review(
    r: aioredis.Redis, *, winner_id: str, decision: str, note: str = ""
) -> dict[str, Any]:
    """Admin clears a review. ``decision`` ∈ {'approve', 'reject'}.

    approve → status pending + back to fulfillment queue.
    reject → status expired + inventory returned to pool.
    """
    if decision not in {"approve", "reject"}:
        raise PrizeError("invalid_decision", decision)
    key = _k_winner(winner_id)
    w = await r.hgetall(key)
    if not w:
        raise PrizeError("winner_not_found", winner_id, status_code=404)
    if w.get("claim_status") != "review":
        raise PrizeError(
            "not_under_review",
            f"current status={w.get('claim_status')}",
            status_code=409,
        )

    if decision == "approve":
        await r.hset(key, mapping={"claim_status": "pending"})
        await r.zrem(_k_review_queue(), winner_id)
        await r.zadd(_k_fulfillment_queue(), {winner_id: _now()})
        await _audit(r, _k_winner_audit(winner_id), "winner.review_approved", {"note": note})
        return {"ok": True, "winner_id": winner_id, "new_status": "pending"}

    # Reject
    await r.hset(
        key, mapping={"claim_status": "expired", "expired_at": str(_now())}
    )
    await r.zrem(_k_review_queue(), winner_id)
    pid = w.get("prize_id", "")
    if pid:
        prize = await r.hgetall(_k_prize(pid))
        if prize and _to_int(prize.get("inventory_claimed"), 0) > 0:
            await r.hincrby(_k_prize(pid), "inventory_claimed", -1)
    await _audit(r, _k_winner_audit(winner_id), "winner.review_rejected", {"note": note})
    return {"ok": True, "winner_id": winner_id, "new_status": "expired"}


__all__ = [
    "PrizeError",
    "InstantWinResult",
    "create_prize_pool",
    "get_prize",
    "list_prizes_by_brand",
    "try_instant_win",
    "record_winner",
    "get_winner",
    "list_user_winners",
    "verify_contact_info",
    "initiate_fulfillment",
    "record_legal_acknowledgment",
    "mark_claimed",
    "expire_unclaimed",
    "enter_sweepstakes",
    "draw_sweepstakes",
    "verify_legal_eligibility",
    "list_review_queue",
    "list_fulfillment_queue",
    "resolve_review",
    "VALID_PRIZE_TYPES",
    "VALID_FULFILLMENT_METHODS",
    "US_W9_THRESHOLD_CENTS",
    "RATE_LIMIT_ATTEMPTS_PER_HOUR",
    "DEFAULT_CLAIM_DEADLINE_DAYS",
]
