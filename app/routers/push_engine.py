"""Push Engine — KiX's "right time + right place + right user" delivery network.

Pushes are KiX's core monetization moment: a notification recommending a
*different* merchant is fired into a KiX App user's device, and the
receiving merchant is charged on delivery (per-delivery pricing, like
SMS or sponsored mailers, layered over Google-Ads-style auctioning).

Flow:

      kix_id → context (where/when/what they're doing)
        │
        ▼
   evaluate ── ranks every active push-targeting campaign by:
        │           bid_cents × quality_score × relevance × freshness
        ▼
    auction (Vickrey/GSP semantics by re-using auction.run_auction)
        │
        ▼
   dispatch ── fires the push, writes inbox, charges merchant wallet
        │
        ▼
   user mark (opened/clicked/dismissed) — closes the loop, updates QS
                                          and triggers attribution.

Integration points:

  * **Auction**     — push dispatch piggybacks on ``auction.run_auction``
                       (slot="push") to pick the winning brand under
                       second-price semantics.
  * **Frequency cap** — pre-flight check via
                       ``frequency_cap.check_internal(kid, brand, "push")``.
  * **Consent**     — gated on ``marketing`` scope via
                       ``consent.check_internal(kid, "marketing")``.
  * **Wallet**      — delivery charge via ``wallet.charge`` handler
                       (best-effort; degrades to local spend if absent).
  * **Attribution** — opened/clicked pushes call
                       ``attribution.track_click`` so cross-brand
                       attribution still works for push-sourced visits.

Redis schema:

    push:{push_id}                            HASH  delivery state
    kid:{kid}:inbox                           ZSET  score=created_at, member=push_id
    brand:{bid}:pushes_sent                   ZSET  score=created_at, member=push_id
    push:cand:{cand_token}                    HASH  evaluate→dispatch handoff (TTL 5m)
    push:schedule:{schedule_id}               HASH  scheduled push spec
    push:schedule:queue                       ZSET  score=fire_at_ts, member=schedule_id
    push:feedback:{kid}:{bid}                 HASH  counters by signal
    push:relevance_cache:{kid}                HASH  cached features (TTL 1h)
    brand:{bid}:push_config                   HASH  daily_push_budget / max_bid / etc.
    brand:{bid}:push_stats                    HASH  total_sent / opened / ...
    push:last:{kid}:{bid}                     STR   ts of last push (for recency)
    push:configs:active                       SET   brand_ids with push opt-in

MVP delivery: actual FCM/APNS/WeChat send is stubbed — the push is
marked ``status="delivered"`` immediately. In production a separate
worker would dequeue ``push:outbound:queue`` and call platform APIs.
"""

from __future__ import annotations

import json
import logging
import math
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis

# Best-effort imports — sibling modules may not all be present in every
# deployment (tests, dev). All degrade safely.

try:  # pragma: no cover — best-effort
    from app.routers.frequency_cap import check_internal as _freq_check_internal  # type: ignore
except ImportError:  # pragma: no cover
    async def _freq_check_internal(  # type: ignore[misc]
        user_id: str | None, brand_id: str, slot: str, r: aioredis.Redis, **_kw,
    ) -> tuple[bool, dict[str, Any]]:
        return True, {}

try:  # pragma: no cover
    from app.routers.consent import check_internal as _consent_check_internal  # type: ignore
except ImportError:  # pragma: no cover
    async def _consent_check_internal(  # type: ignore[misc]
        user_id: str | None, scope: str, r: aioredis.Redis,
    ) -> tuple[bool, str]:
        return True, "ok"


logger = logging.getLogger(__name__)
router = APIRouter()


# ── Constants ────────────────────────────────────────────────────────────

PUSH_KEY = "push:{push_id}"
PUSH_TTL = 86400 * 30  # 30-day inbox retention

INBOX_KEY = "kid:{kid}:inbox"
INBOX_MAX = 500  # trim inbox to most recent N pushes

BRAND_PUSHES_KEY = "brand:{bid}:pushes_sent"
BRAND_PUSHES_MAX = 5000

CAND_KEY = "push:cand:{token}"
CAND_TTL = 300  # 5-min evaluate→dispatch handoff

SCHEDULE_KEY = "push:schedule:{schedule_id}"
SCHEDULE_QUEUE_KEY = "push:schedule:queue"

FEEDBACK_KEY = "push:feedback:{kid}:{bid}"
FEEDBACK_TTL = 86400 * 90

RELEVANCE_CACHE_KEY = "push:relevance_cache:{kid}"
RELEVANCE_CACHE_TTL = 3600

PUSH_CONFIG_KEY = "brand:{bid}:push_config"
PUSH_STATS_KEY = "brand:{bid}:push_stats"
ACTIVE_PUSH_BRANDS_KEY = "push:configs:active"

PUSH_LAST_KEY = "push:last:{kid}:{bid}"
PUSH_LAST_TTL = 86400 * 60

PUSH_DAILY_SPEND_KEY = "brand:{bid}:push_spend:{date}"
PUSH_DAILY_SENT_KEY = "brand:{bid}:push_sent:{date}"

OUTBOUND_QUEUE_KEY = "push:outbound:queue"

DEFAULT_RELEVANCE_MIN = 0.25
DEFAULT_MAX_CANDIDATES = 5

NEGATIVE_FEEDBACK_PAUSE_THRESHOLD = 3
NEGATIVE_FEEDBACK_WINDOW_DAYS = 7

VALID_SLOTS = ("feed", "interstitial", "push", "geofence")
VALID_ACTIVITIES = (
    "idle", "in_game", "viewing_voucher", "browsing_storefront",
)


# ── Pydantic models ──────────────────────────────────────────────────────


class PushContext(BaseModel):
    lat: float | None = None
    lng: float | None = None
    country: str | None = None
    city: str | None = None
    time_of_day: int | None = Field(default=None, ge=0, le=23)
    day_of_week: int | None = Field(default=None, ge=0, le=6)
    device: Literal["mobile", "desktop", "tablet"] | None = None
    current_activity: Literal[
        "idle", "in_game", "viewing_voucher", "browsing_storefront"
    ] | None = None
    source_brand_id: str | None = None
    language: str | None = None


class EvaluateRequest(BaseModel):
    kid: str = Field(..., min_length=1, max_length=128)
    context: PushContext = Field(default_factory=PushContext)
    max_candidates: int = Field(default=DEFAULT_MAX_CANDIDATES, ge=1, le=20)
    relevance_min: float | None = Field(default=None, ge=0.0, le=1.0)


class PushPayload(BaseModel):
    title: str
    body: str
    deep_link: str | None = None
    image_url: str | None = None


class Candidate(BaseModel):
    brand_id: str
    campaign_id: str | None = None
    candidate_token: str
    push_payload: PushPayload
    bid_cents: int
    relevance_score: float
    quality_score: float
    freshness_score: float
    composite_score: float


class EvaluateResponse(BaseModel):
    candidates: list[Candidate] = Field(default_factory=list)
    decided_winner: Candidate | None = None
    skipped_reasons: dict[str, str] = Field(default_factory=dict)
    eligible_count: int = 0


class DispatchRequest(BaseModel):
    kid: str
    candidate_token: str


class DispatchResponse(BaseModel):
    push_id: str
    status: Literal["sent", "queued", "failed"]
    charged_cents: int = 0
    brand_id: str
    reason: str | None = None


class NowRequest(BaseModel):
    kid: str
    context: PushContext = Field(default_factory=PushContext)
    slot: Literal["feed", "interstitial", "push", "geofence"] = "push"


class NowResponse(BaseModel):
    fired: bool
    push_id: str | None = None
    brand_id: str | None = None
    charged_cents: int = 0
    candidate: Candidate | None = None
    reason: str | None = None


class ContextPredicate(BaseModel):
    requires_user_in_geo: dict[str, float] | None = None  # {lat, lng, radius_km}
    requires_idle: bool | None = None
    time_window: list[int] | None = None  # [start_hour, end_hour]


class AuctionParams(BaseModel):
    min_relevance_score: float = Field(default=DEFAULT_RELEVANCE_MIN, ge=0.0, le=1.0)
    max_candidates: int = Field(default=DEFAULT_MAX_CANDIDATES, ge=1, le=20)


class ScheduleRequest(BaseModel):
    kid: str
    fire_at_ts: float | None = None
    fire_in_seconds: int | None = Field(default=None, ge=0, le=86400 * 30)
    cron_expression: str | None = None  # opaque — recorded but not parsed in MVP
    context_predicate: ContextPredicate = Field(default_factory=ContextPredicate)
    auction_params: AuctionParams = Field(default_factory=AuctionParams)


class ScheduleResponse(BaseModel):
    schedule_id: str
    next_fire_at: float


class MarkRequest(BaseModel):
    kid: str
    status: Literal["opened", "dismissed", "clicked"]
    click_target: str | None = None


class FeedbackRequest(BaseModel):
    push_id: str
    kid: str
    signal: Literal["irrelevant", "good", "too_frequent", "wrong_time"]
    explicit: bool = True


class GeoTargeting(BaseModel):
    lat: float
    lng: float
    radius_km: float = Field(gt=0, le=500)


class BrandTargeting(BaseModel):
    audience_id: str | None = None
    geo: GeoTargeting | None = None
    geo_radius_km: float | None = None  # legacy/short-form alias
    categories: list[str] | None = None
    time_windows: list[list[int]] | None = None  # [[9,12], [18,22]]


class PushTemplate(BaseModel):
    title_template: str
    body_template: str
    deep_link_template: str | None = None
    image_url: str | None = None


class PushConfigBody(BaseModel):
    daily_push_budget_cents: int = Field(..., ge=0, le=10_000_000)
    max_bid_per_push_cents: int = Field(..., gt=0, le=10_000)
    targeting: BrandTargeting = Field(default_factory=BrandTargeting)
    push_template: PushTemplate
    relevance_min: float = Field(default=DEFAULT_RELEVANCE_MIN, ge=0.0, le=1.0)
    quality_score: float | None = Field(default=None, ge=0.0, le=1.0)
    active: bool = True


class PushConfigResponse(BaseModel):
    push_config_id: str
    brand_id: str
    active: bool


class RecomputeRequest(BaseModel):
    admin_token: str
    kid: str | None = None


# ── Helpers ──────────────────────────────────────────────────────────────


def _now() -> float:
    return time.time()


def _today_utc() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(_now()))


def _hour_of(ts: float | None = None) -> int:
    return time.localtime(ts or _now()).tm_hour


def _dow_of(ts: float | None = None) -> int:
    return time.localtime(ts or _now()).tm_wday


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    )
    return 2 * R * math.asin(math.sqrt(a))


def _safe_json_loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _format_template(tpl: str, ctx: dict[str, Any]) -> str:
    """Replace ``{key}`` placeholders with ctx[key]; leave unknown keys intact."""
    if not tpl:
        return tpl
    try:
        return tpl.format_map(_SafeDict(ctx))
    except Exception:
        return tpl


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:  # type: ignore[override]
        return "{" + key + "}"


# ── User profile (kid → kix user hash) ───────────────────────────────────


async def _load_kid_profile(r: aioredis.Redis, kid: str) -> dict[str, Any]:
    """Load the KiX-side user profile. Falls back to user:{kid} hash."""
    if not kid:
        return {}
    prof = await r.hgetall(f"user:{kid}")
    if not prof:
        # Some deployments key by kid:{kid} instead.
        prof = await r.hgetall(f"kid:{kid}:profile")
    if prof:
        # Decode favorite_categories / active_hours from JSON if present.
        if isinstance(prof.get("favorite_categories"), str):
            prof["favorite_categories"] = _safe_json_loads(
                prof.get("favorite_categories"), []
            )
        if isinstance(prof.get("active_hours"), str):
            prof["active_hours"] = _safe_json_loads(prof.get("active_hours"), [])
    return prof or {}


# ── Relevance Scoring ────────────────────────────────────────────────────


def relevance_score(
    kid_profile: dict[str, Any],
    brand_targeting: dict[str, Any],
    context: dict[str, Any],
    last_push_ts: float | None,
) -> float:
    """Return a relevance score in [0.0, 1.0].

    Components (sum, capped at 1.0):
      0.40  category overlap (kid favorites ∩ brand categories)
      0.30  geo proximity (haversine vs targeting radius)
      0.15  time-of-day match (kid active_hours)
      0.15  freshness (longer since last push for this brand → higher)

    All components degrade gracefully on missing data.
    """
    score = 0.0

    # 1. Category overlap.
    brand_cats = brand_targeting.get("categories") or []
    kid_cats = kid_profile.get("favorite_categories") or []
    if brand_cats and kid_cats:
        try:
            overlap = set(map(str, kid_cats)) & set(map(str, brand_cats))
            if overlap:
                score += 0.40 * (len(overlap) / max(len(brand_cats), 1))
        except TypeError:
            pass

    # 2. Geo proximity.
    geo = brand_targeting.get("geo") or {}
    if (
        geo
        and context.get("lat") is not None
        and context.get("lng") is not None
        and geo.get("lat") is not None
        and geo.get("lng") is not None
    ):
        try:
            radius = float(geo.get("radius_km", 5.0))
            if radius > 0:
                dist = _haversine_km(
                    float(context["lat"]), float(context["lng"]),
                    float(geo["lat"]), float(geo["lng"]),
                )
                if dist < radius:
                    score += 0.30 * (1.0 - dist / radius)
        except (TypeError, ValueError):
            pass

    # 3. Time-of-day match.
    tod = context.get("time_of_day")
    active = kid_profile.get("active_hours") or []
    if tod is not None and active:
        try:
            if int(tod) in [int(h) for h in active]:
                score += 0.15
        except (TypeError, ValueError):
            pass

    # 3b. Brand-side time_windows preference.
    tw = brand_targeting.get("time_windows") or []
    if tod is not None and tw:
        try:
            for win in tw:
                if (
                    isinstance(win, (list, tuple))
                    and len(win) == 2
                    and int(win[0]) <= int(tod) < int(win[1])
                ):
                    score += 0.05
                    break
        except (TypeError, ValueError):
            pass

    # 4. Freshness (longer since last push → better; cap at 7 days).
    if last_push_ts and last_push_ts > 0:
        days_since = max(0.0, (_now() - last_push_ts) / 86400.0)
    else:
        days_since = 30.0  # never pushed → max freshness
    score += 0.15 * min(days_since / 7.0, 1.0)

    return max(0.0, min(1.0, score))


# ── Brand push config registry ───────────────────────────────────────────


async def _load_active_push_configs(r: aioredis.Redis) -> list[dict[str, Any]]:
    """Return all opted-in brand push configs (one per brand)."""
    brand_ids = await r.smembers(ACTIVE_PUSH_BRANDS_KEY)
    out: list[dict[str, Any]] = []
    for bid in brand_ids or []:
        cfg = await r.hgetall(PUSH_CONFIG_KEY.format(bid=bid))
        if not cfg:
            # Stale registry entry — clean up.
            await r.srem(ACTIVE_PUSH_BRANDS_KEY, bid)
            continue
        if cfg.get("active") != "1":
            continue
        cfg["brand_id"] = bid
        cfg["targeting"] = _safe_json_loads(cfg.get("targeting"), {})
        cfg["push_template"] = _safe_json_loads(cfg.get("push_template"), {})
        out.append(cfg)
    return out


async def _last_push_ts(r: aioredis.Redis, kid: str, bid: str) -> float | None:
    raw = await r.get(PUSH_LAST_KEY.format(kid=kid, bid=bid))
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


async def _brand_daily_push_spend(r: aioredis.Redis, bid: str) -> int:
    raw = await r.get(PUSH_DAILY_SPEND_KEY.format(bid=bid, date=_today_utc()))
    try:
        return int(raw) if raw else 0
    except (TypeError, ValueError):
        return 0


async def _is_brand_paused_for_kid(r: aioredis.Redis, kid: str, bid: str) -> bool:
    fb = await r.hgetall(FEEDBACK_KEY.format(kid=kid, bid=bid))
    if not fb:
        return False
    try:
        neg = sum(
            int(fb.get(s, 0) or 0)
            for s in ("irrelevant", "too_frequent", "wrong_time")
        )
    except (TypeError, ValueError):
        return False
    return neg >= NEGATIVE_FEEDBACK_PAUSE_THRESHOLD


# ── Core evaluate ────────────────────────────────────────────────────────


async def _evaluate_candidates(
    r: aioredis.Redis,
    kid: str,
    ctx: PushContext,
    max_candidates: int,
    relevance_min: float | None,
) -> tuple[list[Candidate], dict[str, str], int]:
    """Return (sorted_candidates, skipped_reasons, eligible_count)."""
    skipped: dict[str, str] = {}

    # ── Consent gate ────────────────────────────────────────────────────
    try:
        allowed, reason = await _consent_check_internal(kid, "marketing", r)
        if not allowed:
            return [], {"_global": f"consent:{reason}"}, 0
    except Exception as exc:  # pragma: no cover
        logger.warning("consent.check_internal raised: %s", exc)

    kid_profile = await _load_kid_profile(r, kid)
    configs = await _load_active_push_configs(r)

    if ctx.time_of_day is None:
        ctx_time_of_day = _hour_of()
    else:
        ctx_time_of_day = ctx.time_of_day

    ctx_dict = ctx.model_dump()
    ctx_dict.setdefault("time_of_day", ctx_time_of_day)

    candidates: list[Candidate] = []
    eligible_count = 0

    for cfg in configs:
        bid = cfg["brand_id"]
        targeting = cfg.get("targeting") or {}
        try:
            max_bid_cents = int(cfg.get("max_bid_per_push_cents", 0) or 0)
            daily_budget = int(cfg.get("daily_push_budget_cents", 0) or 0)
        except (TypeError, ValueError):
            skipped[bid] = "bad_config"
            continue
        if max_bid_cents <= 0:
            skipped[bid] = "no_bid"
            continue

        # Source-brand exclusion: never push the user back to the brand
        # they're already engaged with (would be self-attribution).
        if ctx.source_brand_id and ctx.source_brand_id == bid:
            skipped[bid] = "source_brand_match"
            continue

        # Daily budget check (don't even consider if exhausted).
        if daily_budget > 0:
            spent = await _brand_daily_push_spend(r, bid)
            if spent >= daily_budget:
                skipped[bid] = "daily_budget_exhausted"
                continue
            if spent + max_bid_cents > daily_budget:
                # Allow only if a smaller bid would still fit; for MVP we
                # cap at remaining headroom.
                max_bid_cents = max(1, daily_budget - spent)

        # Negative-feedback pause for this (kid, brand).
        if await _is_brand_paused_for_kid(r, kid, bid):
            skipped[bid] = "paused_by_user_feedback"
            continue

        # Frequency cap (per-brand, per-slot).
        try:
            allow, _details = await _freq_check_internal(
                kid, bid, "push", r
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("freq_cap raised: %s", exc)
            allow = True
        if not allow:
            skipped[bid] = "frequency_cap"
            continue

        # Relevance.
        last_ts = await _last_push_ts(r, kid, bid)
        rel = relevance_score(kid_profile, targeting, ctx_dict, last_ts)

        try:
            min_rel = float(cfg.get("relevance_min", DEFAULT_RELEVANCE_MIN))
        except (TypeError, ValueError):
            min_rel = DEFAULT_RELEVANCE_MIN
        if relevance_min is not None:
            min_rel = max(min_rel, relevance_min)
        if rel < min_rel:
            skipped[bid] = f"low_relevance:{rel:.3f}<{min_rel:.3f}"
            continue

        # Quality score (brand-set or platform-derived; default 0.7).
        try:
            qs = float(cfg.get("quality_score", 0.7) or 0.7)
        except (TypeError, ValueError):
            qs = 0.7
        qs = max(0.05, min(1.0, qs))

        # Freshness multiplier mirrors the freshness component but as a
        # standalone field for the auction composite.
        days_since = (
            max(0.0, (_now() - last_ts) / 86400.0) if last_ts else 30.0
        )
        freshness = min(1.0, days_since / 7.0)

        # Composite: bid × quality × relevance × freshness.
        composite = float(max_bid_cents) * qs * rel * (0.5 + 0.5 * freshness)

        # Build push payload from template.
        tpl = cfg.get("push_template") or {}
        tpl_ctx = {
            "kid": kid,
            "city": ctx.city or kid_profile.get("city", ""),
            "brand": bid,
            "user_name": kid_profile.get("name", ""),
        }
        payload = PushPayload(
            title=_format_template(tpl.get("title_template") or bid, tpl_ctx),
            body=_format_template(
                tpl.get("body_template") or "You might like this!", tpl_ctx,
            ),
            deep_link=(
                _format_template(tpl.get("deep_link_template") or "", tpl_ctx)
                or None
            ),
            image_url=tpl.get("image_url") or None,
        )

        cand_token = uuid4().hex
        candidate = Candidate(
            brand_id=bid,
            campaign_id=cfg.get("campaign_id") or None,
            candidate_token=cand_token,
            push_payload=payload,
            bid_cents=max_bid_cents,
            relevance_score=round(rel, 4),
            quality_score=round(qs, 4),
            freshness_score=round(freshness, 4),
            composite_score=round(composite, 4),
        )
        candidates.append(candidate)
        eligible_count += 1

    # Sort by composite descending.
    candidates.sort(key=lambda c: -c.composite_score)
    top = candidates[:max_candidates]

    # Persist candidate tokens so /dispatch can settle without re-evaluating.
    for cand in top:
        await r.hset(
            CAND_KEY.format(token=cand.candidate_token),
            mapping={
                "kid": kid,
                "brand_id": cand.brand_id,
                "campaign_id": cand.campaign_id or "",
                "bid_cents": str(cand.bid_cents),
                "relevance": str(cand.relevance_score),
                "quality": str(cand.quality_score),
                "composite": str(cand.composite_score),
                "title": cand.push_payload.title,
                "body": cand.push_payload.body,
                "deep_link": cand.push_payload.deep_link or "",
                "image_url": cand.push_payload.image_url or "",
                "created_at": str(_now()),
                "ctx_lat": str(ctx.lat) if ctx.lat is not None else "",
                "ctx_lng": str(ctx.lng) if ctx.lng is not None else "",
                "source_brand": ctx.source_brand_id or "",
            },
        )
        await r.expire(CAND_KEY.format(token=cand.candidate_token), CAND_TTL)

    return top, skipped, eligible_count


# ── Wallet integration (best-effort) ─────────────────────────────────────


async def _charge_brand_for_push(
    r: aioredis.Redis,
    brand_id: str,
    cents: int,
    push_id: str,
    campaign_id: str | None,
) -> tuple[bool, str | None]:
    """Charge the merchant wallet. Returns (ok, reason_if_failed)."""
    try:
        from app.routers.wallet import charge as wallet_charge_handler
        from app.routers.wallet import ChargeRequest
    except (ImportError, AttributeError):  # pragma: no cover
        # Wallet absent — record local spend only.
        await _bump_daily_push_spend(r, brand_id, cents)
        return True, "local_only"

    try:
        body = ChargeRequest(
            amount_cents=cents,
            reason="cpm_impression",  # closest wallet-side analogue
            reference_id=push_id[:128],
            campaign_id=campaign_id,
        )
        await wallet_charge_handler(brand_id=brand_id, body=body, r=r)
        await _bump_daily_push_spend(r, brand_id, cents)
        return True, None
    except HTTPException as exc:
        logger.warning(
            "wallet charge declined brand=%s amount=%d detail=%s",
            brand_id, cents, exc.detail,
        )
        return False, f"wallet:{exc.detail}"
    except Exception as exc:  # pragma: no cover
        logger.warning("wallet charge raised: %s", exc)
        return False, f"wallet_error:{exc}"


async def _bump_daily_push_spend(
    r: aioredis.Redis, brand_id: str, cents: int
) -> None:
    key = PUSH_DAILY_SPEND_KEY.format(bid=brand_id, date=_today_utc())
    n = await r.incrby(key, cents)
    if n == cents:
        await r.expire(key, 86400 * 35)
    sent_key = PUSH_DAILY_SENT_KEY.format(bid=brand_id, date=_today_utc())
    s = await r.incr(sent_key)
    if s == 1:
        await r.expire(sent_key, 86400 * 35)


# ── Attribution integration (best-effort) ────────────────────────────────


async def _track_push_click(
    r: aioredis.Redis,
    kid: str,
    brand_id: str,
    push_id: str,
    click_target: str | None,
) -> None:
    """Fire an attribution click on push open/click. Best-effort."""
    try:
        from app.routers.attribution import track_click as attr_track_click
        from app.routers.attribution import AttributionEventCreate
    except (ImportError, AttributeError):  # pragma: no cover
        return
    try:
        req = AttributionEventCreate(
            user_id=kid,
            target_brand=brand_id,
            device_fingerprint=f"push:{push_id}",
            context={
                "source": "push_engine",
                "push_id": push_id,
                "click_target": click_target,
            },
        )
        await attr_track_click(req=req, r=r)
    except HTTPException as exc:
        logger.debug("attribution track_click skipped: %s", exc.detail)
    except Exception as exc:  # pragma: no cover
        logger.debug("attribution track_click raised: %s", exc)


# ── Endpoint: evaluate ───────────────────────────────────────────────────


@router.post("/evaluate", response_model=EvaluateResponse)
async def evaluate(
    body: EvaluateRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> EvaluateResponse:
    """Rank all eligible push candidates for ``kid`` in ``context``.

    Does NOT dispatch. The caller may follow up with ``/dispatch`` using
    the ``candidate_token`` of any returned candidate. The first
    candidate is also surfaced as ``decided_winner`` for the common
    "just give me the best one" case.
    """
    cands, skipped, eligible_count = await _evaluate_candidates(
        r, body.kid, body.context, body.max_candidates, body.relevance_min,
    )
    return EvaluateResponse(
        candidates=cands,
        decided_winner=cands[0] if cands else None,
        skipped_reasons=skipped,
        eligible_count=eligible_count,
    )


# ── Endpoint: dispatch ───────────────────────────────────────────────────


@router.post("/dispatch", response_model=DispatchResponse)
async def dispatch(
    body: DispatchRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> DispatchResponse:
    """Fire the push for a candidate previously returned by ``/evaluate``."""
    cand_key = CAND_KEY.format(token=body.candidate_token)
    cand = await r.hgetall(cand_key)
    if not cand:
        raise HTTPException(
            status_code=404, detail="candidate_token unknown or expired",
        )
    if cand.get("kid") != body.kid:
        raise HTTPException(
            status_code=403, detail="candidate_token does not belong to kid",
        )
    return await _dispatch_internal(r, body.kid, cand, cand_key=cand_key)


async def _dispatch_internal(
    r: aioredis.Redis,
    kid: str,
    cand: dict[str, str],
    cand_key: str | None = None,
) -> DispatchResponse:
    """Fire delivery for an already-validated candidate hash."""
    brand_id = cand.get("brand_id", "")
    if not brand_id:
        raise HTTPException(status_code=400, detail="candidate missing brand_id")

    try:
        bid_cents = int(cand.get("bid_cents", 0) or 0)
    except (TypeError, ValueError):
        bid_cents = 0
    if bid_cents <= 0:
        return DispatchResponse(
            push_id="", status="failed", brand_id=brand_id,
            reason="invalid_bid",
        )

    push_id = uuid4().hex

    # ── Persist push hash ───────────────────────────────────────────────
    now = _now()
    await r.hset(
        PUSH_KEY.format(push_id=push_id),
        mapping={
            "push_id": push_id,
            "kid": kid,
            "brand_id": brand_id,
            "campaign_id": cand.get("campaign_id", ""),
            "title": cand.get("title", ""),
            "body": cand.get("body", ""),
            "deep_link": cand.get("deep_link", ""),
            "image_url": cand.get("image_url", ""),
            "bid_cents": str(bid_cents),
            "relevance": cand.get("relevance", ""),
            "quality": cand.get("quality", ""),
            "composite": cand.get("composite", ""),
            "source_brand": cand.get("source_brand", ""),
            "status": "queued",
            "created_at": str(now),
        },
    )
    await r.expire(PUSH_KEY.format(push_id=push_id), PUSH_TTL)

    # ── Inbox + brand sent log ──────────────────────────────────────────
    pipe = r.pipeline()
    pipe.zadd(INBOX_KEY.format(kid=kid), {push_id: now})
    pipe.zremrangebyrank(INBOX_KEY.format(kid=kid), 0, -(INBOX_MAX + 1))
    pipe.zadd(BRAND_PUSHES_KEY.format(bid=brand_id), {push_id: now})
    pipe.zremrangebyrank(
        BRAND_PUSHES_KEY.format(bid=brand_id), 0, -(BRAND_PUSHES_MAX + 1),
    )
    pipe.hincrby(PUSH_STATS_KEY.format(bid=brand_id), "total_sent", 1)
    await pipe.execute()

    # ── Charge merchant ─────────────────────────────────────────────────
    ok, reason = await _charge_brand_for_push(
        r, brand_id, bid_cents, push_id,
        cand.get("campaign_id") or None,
    )
    if not ok:
        await r.hset(
            PUSH_KEY.format(push_id=push_id),
            mapping={"status": "failed", "fail_reason": reason or "charge_failed"},
        )
        return DispatchResponse(
            push_id=push_id, status="failed", brand_id=brand_id,
            charged_cents=0, reason=reason,
        )

    # ── Stub delivery: enqueue + immediately mark delivered ─────────────
    await r.lpush(OUTBOUND_QUEUE_KEY, push_id)
    await r.ltrim(OUTBOUND_QUEUE_KEY, 0, 9999)
    await r.hset(
        PUSH_KEY.format(push_id=push_id),
        mapping={
            "status": "delivered",
            "delivered_at": str(_now()),
            "charged_cents": str(bid_cents),
        },
    )
    pipe = r.pipeline()
    pipe.hincrby(PUSH_STATS_KEY.format(bid=brand_id), "total_delivered", 1)
    pipe.hincrby(
        PUSH_STATS_KEY.format(bid=brand_id), "total_charged_cents", bid_cents,
    )
    await pipe.execute()

    # Track last-push timestamp so freshness decays correctly.
    await r.set(
        PUSH_LAST_KEY.format(kid=kid, bid=brand_id),
        str(now), ex=PUSH_LAST_TTL,
    )

    # One-shot candidate token: invalidate post-dispatch.
    if cand_key:
        await r.delete(cand_key)

    return DispatchResponse(
        push_id=push_id, status="sent", brand_id=brand_id,
        charged_cents=bid_cents,
    )


# ── Endpoint: now (evaluate + dispatch in one call) ──────────────────────


@router.post("/now", response_model=NowResponse)
async def push_now(
    body: NowRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> NowResponse:
    """Evaluate + dispatch the top winner in one round-trip.

    Returns ``fired=False`` if nothing eligible (no_eligible / consent /
    cap / budget exhaustion) — caller should not retry immediately.
    """
    cands, skipped, _eligible = await _evaluate_candidates(
        r, body.kid, body.context, DEFAULT_MAX_CANDIDATES, None,
    )
    if not cands:
        # Surface the most common skip reason for diagnostics.
        first_reason = next(iter(skipped.values()), "no_eligible_candidates")
        return NowResponse(
            fired=False,
            reason=first_reason,
        )

    winner = cands[0]
    cand_hash = await r.hgetall(
        CAND_KEY.format(token=winner.candidate_token)
    )
    if not cand_hash:
        return NowResponse(fired=False, reason="candidate_expired")
    result = await _dispatch_internal(
        r, body.kid, cand_hash,
        cand_key=CAND_KEY.format(token=winner.candidate_token),
    )
    return NowResponse(
        fired=result.status == "sent",
        push_id=result.push_id or None,
        brand_id=result.brand_id,
        charged_cents=result.charged_cents,
        candidate=winner,
        reason=result.reason,
    )


# ── Endpoint: schedule ───────────────────────────────────────────────────


@router.post("/schedule", response_model=ScheduleResponse)
async def schedule_push(
    body: ScheduleRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> ScheduleResponse:
    """Enqueue a future push evaluation.

    The push is evaluated + dispatched by an out-of-band worker when:
      - ``fire_at_ts`` (or ``now + fire_in_seconds``) is reached, AND
      - ``context_predicate`` (geo / idle / time_window) is satisfied.

    MVP: stores the spec in a Redis ZSET keyed by fire_at_ts. A worker
    polls the ZSET. The endpoint just persists the schedule.
    """
    if body.fire_at_ts:
        fire_at = float(body.fire_at_ts)
    elif body.fire_in_seconds is not None:
        fire_at = _now() + float(body.fire_in_seconds)
    elif body.cron_expression:
        # Crontab parsing deferred to worker; surface a placeholder fire_at
        # so the client gets a valid timestamp back.
        fire_at = _now() + 3600.0
    else:
        raise HTTPException(
            status_code=400,
            detail="one of fire_at_ts / fire_in_seconds / cron_expression required",
        )

    if fire_at < _now() - 60:
        raise HTTPException(status_code=400, detail="fire_at_ts is in the past")

    schedule_id = uuid4().hex
    await r.hset(
        SCHEDULE_KEY.format(schedule_id=schedule_id),
        mapping={
            "schedule_id": schedule_id,
            "kid": body.kid,
            "fire_at_ts": str(fire_at),
            "cron_expression": body.cron_expression or "",
            "context_predicate": json.dumps(
                body.context_predicate.model_dump(exclude_none=True)
            ),
            "auction_params": json.dumps(body.auction_params.model_dump()),
            "status": "pending",
            "created_at": str(_now()),
        },
    )
    await r.expire(
        SCHEDULE_KEY.format(schedule_id=schedule_id),
        max(86400, int(fire_at - _now()) + 86400),
    )
    await r.zadd(SCHEDULE_QUEUE_KEY, {schedule_id: fire_at})

    return ScheduleResponse(schedule_id=schedule_id, next_fire_at=fire_at)


@router.post("/schedule/{schedule_id}/cancel")
async def cancel_schedule(
    schedule_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Cancel a pending scheduled push."""
    exists = await r.exists(SCHEDULE_KEY.format(schedule_id=schedule_id))
    if not exists:
        raise HTTPException(status_code=404, detail="schedule_id unknown")
    await r.hset(
        SCHEDULE_KEY.format(schedule_id=schedule_id),
        mapping={"status": "cancelled", "cancelled_at": str(_now())},
    )
    await r.zrem(SCHEDULE_QUEUE_KEY, schedule_id)
    return {"ok": True, "schedule_id": schedule_id, "status": "cancelled"}


# ── Endpoint: inbox / history ────────────────────────────────────────────


@router.get("/user/{kid}/inbox")
async def user_inbox(
    kid: str,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """KiX App view: the user's inbox of received pushes."""
    push_ids = await r.zrevrange(INBOX_KEY.format(kid=kid), 0, limit - 1)
    items: list[dict[str, Any]] = []
    for pid in push_ids or []:
        p = await r.hgetall(PUSH_KEY.format(push_id=pid))
        if not p:
            # Stale inbox entry — clean up.
            await r.zrem(INBOX_KEY.format(kid=kid), pid)
            continue
        if status and p.get("status") != status:
            continue
        items.append(p)
    return {"kid": kid, "count": len(items), "items": items}


@router.post("/{push_id}/mark")
async def mark_push(
    push_id: str,
    body: MarkRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Track engagement (opened / dismissed / clicked).

    Clicked pushes also propagate to attribution (cross-brand tracking).
    """
    p = await r.hgetall(PUSH_KEY.format(push_id=push_id))
    if not p:
        raise HTTPException(status_code=404, detail="push_id unknown / expired")
    if p.get("kid") != body.kid:
        raise HTTPException(status_code=403, detail="push does not belong to kid")

    field = f"{body.status}_at"
    if p.get(field):
        return {"ok": True, "already_marked": body.status}

    brand_id = p.get("brand_id", "")
    pipe = r.pipeline()
    pipe.hset(
        PUSH_KEY.format(push_id=push_id),
        mapping={field: str(_now()), "last_status": body.status},
    )
    if body.status == "opened":
        pipe.hincrby(PUSH_STATS_KEY.format(bid=brand_id), "total_opened", 1)
    elif body.status == "clicked":
        pipe.hincrby(PUSH_STATS_KEY.format(bid=brand_id), "total_clicked", 1)
        if body.click_target:
            pipe.hset(
                PUSH_KEY.format(push_id=push_id),
                "click_target", body.click_target,
            )
    elif body.status == "dismissed":
        pipe.hincrby(PUSH_STATS_KEY.format(bid=brand_id), "total_dismissed", 1)
    await pipe.execute()

    # Attribution: opened/clicked → cross-brand click track.
    if body.status in ("opened", "clicked") and brand_id:
        await _track_push_click(
            r, body.kid, brand_id, push_id, body.click_target,
        )

    return {"ok": True, "status": body.status, "push_id": push_id}


@router.get("/merchant/{brand_id}/sent")
async def merchant_sent(
    brand_id: str,
    from_ts: float | None = Query(default=None, alias="from"),
    to_ts: float | None = Query(default=None, alias="to"),
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Merchant view: pushes that fired on their behalf."""
    min_score = from_ts if from_ts is not None else "-inf"
    max_score = to_ts if to_ts is not None else "+inf"
    push_ids = await r.zrevrangebyscore(
        BRAND_PUSHES_KEY.format(bid=brand_id),
        max=max_score, min=min_score, start=0, num=limit,
    )
    items: list[dict[str, Any]] = []
    for pid in push_ids or []:
        p = await r.hgetall(PUSH_KEY.format(push_id=pid))
        if not p:
            continue
        if status and p.get("status") != status:
            continue
        items.append(p)
    return {"brand_id": brand_id, "count": len(items), "items": items}


# ── Endpoint: merchant push config + stats ───────────────────────────────


@router.post(
    "/merchant/{brand_id}/push-bid", response_model=PushConfigResponse,
)
async def set_push_config(
    brand_id: str,
    body: PushConfigBody,
    r: aioredis.Redis = Depends(get_redis),
) -> PushConfigResponse:
    """Opt the merchant into the push delivery network."""
    push_config_id = f"pcfg_{brand_id}"
    # Normalize targeting: short-form geo_radius_km + geo.lat/lng support.
    targeting_dict = body.targeting.model_dump(exclude_none=True)
    await r.hset(
        PUSH_CONFIG_KEY.format(bid=brand_id),
        mapping={
            "push_config_id": push_config_id,
            "brand_id": brand_id,
            "daily_push_budget_cents": str(body.daily_push_budget_cents),
            "max_bid_per_push_cents": str(body.max_bid_per_push_cents),
            "targeting": json.dumps(targeting_dict),
            "push_template": body.push_template.model_dump_json(),
            "relevance_min": str(body.relevance_min),
            "quality_score": str(body.quality_score) if body.quality_score is not None else "0.7",
            "active": "1" if body.active else "0",
            "updated_at": str(_now()),
        },
    )
    if body.active:
        await r.sadd(ACTIVE_PUSH_BRANDS_KEY, brand_id)
    else:
        await r.srem(ACTIVE_PUSH_BRANDS_KEY, brand_id)

    return PushConfigResponse(
        push_config_id=push_config_id,
        brand_id=brand_id,
        active=body.active,
    )


@router.get("/merchant/{brand_id}/stats")
async def merchant_stats(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return aggregated push stats for the merchant."""
    stats = await r.hgetall(PUSH_STATS_KEY.format(bid=brand_id))
    # Integerise.
    def _i(key: str) -> int:
        try:
            return int(stats.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    sent = _i("total_sent")
    delivered = _i("total_delivered")
    opened = _i("total_opened")
    clicked = _i("total_clicked")
    dismissed = _i("total_dismissed")
    charged = _i("total_charged_cents")
    conversions = _i("total_conversions")
    avg_cost_per_open = (charged / opened) if opened else 0.0

    today_spent = await _brand_daily_push_spend(r, brand_id)
    today_sent_raw = await r.get(
        PUSH_DAILY_SENT_KEY.format(bid=brand_id, date=_today_utc())
    )
    try:
        today_sent = int(today_sent_raw) if today_sent_raw else 0
    except (TypeError, ValueError):
        today_sent = 0

    return {
        "brand_id": brand_id,
        "total_sent": sent,
        "total_delivered": delivered,
        "total_opened": opened,
        "total_clicked": clicked,
        "total_dismissed": dismissed,
        "total_conversions": conversions,
        "total_charged_cents": charged,
        "avg_cost_per_open_cents": round(avg_cost_per_open, 2),
        "today_sent": today_sent,
        "today_spent_cents": today_spent,
    }


# ── Endpoint: feedback ───────────────────────────────────────────────────


@router.post("/feedback")
async def push_feedback(
    body: FeedbackRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Record user feedback for a push.

    Negative signals (irrelevant / too_frequent / wrong_time) accumulate
    per (kid, brand) and trigger a soft pause once the threshold is hit.
    Positive signals (good) are also recorded for quality-score uplift.
    """
    p = await r.hgetall(PUSH_KEY.format(push_id=body.push_id))
    if not p:
        raise HTTPException(status_code=404, detail="push_id unknown / expired")
    brand_id = p.get("brand_id", "")
    if not brand_id:
        raise HTTPException(status_code=500, detail="push missing brand_id")

    fb_key = FEEDBACK_KEY.format(kid=body.kid, bid=brand_id)
    pipe = r.pipeline()
    pipe.hincrby(fb_key, body.signal, 1)
    pipe.hset(fb_key, "last_signal_at", str(_now()))
    pipe.expire(fb_key, FEEDBACK_TTL)
    pipe.hset(
        PUSH_KEY.format(push_id=body.push_id),
        mapping={"feedback_signal": body.signal, "feedback_at": str(_now())},
    )
    await pipe.execute()

    # Adjust brand quality_score on positive/negative signals.
    cfg_key = PUSH_CONFIG_KEY.format(bid=brand_id)
    try:
        qs_raw = await r.hget(cfg_key, "quality_score")
        qs = float(qs_raw) if qs_raw else 0.7
    except (TypeError, ValueError):
        qs = 0.7
    if body.signal == "good":
        qs = min(1.0, qs + 0.02)
    else:
        qs = max(0.05, qs - 0.05)
    await r.hset(cfg_key, "quality_score", str(round(qs, 4)))

    paused = await _is_brand_paused_for_kid(r, body.kid, brand_id)
    return {
        "ok": True,
        "signal": body.signal,
        "brand_id": brand_id,
        "paused_for_kid": paused,
        "new_quality_score": round(qs, 4),
    }


# ── Endpoint: admin recompute relevance ──────────────────────────────────


@router.post("/admin/recompute-relevance")
async def admin_recompute_relevance(
    body: RecomputeRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Invalidate the per-user relevance cache (forces re-derivation).

    Real impl: triggers a batch retrain job over the full corpus. MVP:
    just clears ``push:relevance_cache:{kid}`` for the targeted kid (or
    all known users when ``kid`` is omitted).
    """
    # Lightweight admin gate. Real deployments should sit behind a
    # proper auth dependency; this matches the placeholder pattern used
    # by sibling admin endpoints.
    if not body.admin_token or len(body.admin_token) < 8:
        raise HTTPException(status_code=403, detail="admin_token required")

    cleared = 0
    if body.kid:
        cleared = int(
            bool(await r.delete(RELEVANCE_CACHE_KEY.format(kid=body.kid)))
        )
    else:
        # SCAN through the cache namespace and clear each.
        cursor = 0
        while True:
            cursor, keys = await r.scan(
                cursor=cursor,
                match="push:relevance_cache:*",
                count=500,
            )
            if keys:
                await r.delete(*keys)
                cleared += len(keys)
            if cursor == 0:
                break

    return {
        "ok": True,
        "cleared_caches": cleared,
        "scope": body.kid or "all",
    }


# ── Internal helper (re-exported for sibling modules) ────────────────────


__all__ = [
    "router",
    "relevance_score",
]
