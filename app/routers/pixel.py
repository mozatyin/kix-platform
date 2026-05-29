"""Conversion Pixel — Google-Analytics-style JS pixel for merchants.

Merchants register a pixel (bound to their brand + allowed origins) and embed
the returned <script> snippet on their site. The browser SDK auto-fires a
`pageview`, then merchants call `kix.identify(...)`, `kix.purchase(...)`,
`kix.signup(...)`, etc.

POST /api/v1/pixel/event records the event:
  * pageview / add_to_cart       → counter bump only (lightweight)
  * purchase                     → attribution.track_conversion (commission split)
  * signup                       → attribution.track_visit (acquisition record)
  * refund / return / order_cancelled → auto-reverses commission via dispute

POST /api/v1/pixel/events/batch records up to 100 events in one round-trip
(critical for high-volume mobile clients) — counts as one rate-limit hit.

CORS / abuse:
  * Each pixel has an `allowed_origins` allowlist. The `origin` field of the
    payload + the HTTP `Origin` header must both match — otherwise 403.
  * Per-pixel rate limit: 1000 events/minute (rolling) for web origins;
    halved (500/min) when the request is from a native-app origin (no
    HTTP Origin header → body.origin is authoritative, so we need a tighter
    budget to deter spoofing).
  * `user_id` from the client is treated as a hint only; we never grant
    privileges based on it.

Supported origin formats
------------------------
  http(s)://<host>...                   browser / webview
  wx<16+ alphanumeric>                  WeChat Mini-Program App-ID
                                        (e.g. wx1234567890abcdef)
  alipay:<16+ alphanumeric>             Alipay Mini-Program App-ID
  ios:<reverse-DNS bundle id>           iOS native SDK
                                        (e.g. ios:com.huangbaby.app)
  android:<package_name>                Android native SDK
  kix-native:<merchant_token>           generic native-app catch-all

Native-app origins do not send an HTTP `Origin` header; the body field is
authoritative for those, but rate-limited harder.

Redis schema
------------
    pixel:{pixel_id}                  HASH  {brand_id, allowed_origins (JSON),
                                             created_at, status}
    pixel:{pixel_id}:stats            HASH  {pageviews, purchases, signups,
                                             add_to_carts, attributed,
                                             total_amount_cents}
    pixel:{pixel_id}:ratelimit:{min}  STRING  INCR + EX 120
    brand:{bid}:pixels                SET   of pixel_ids
    pixel_event:{event_id}            HASH  audit record (TTL 7 days)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator
import redis.asyncio as aioredis

from app.redis_client import get_redis
from app.routers import attribution as attr_mod

# disputes import is intentionally deferred to call site to avoid an import
# cycle if disputes.py ever imports from pixel.py.

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ──────────────────────────────────────────────────────────────

EVENT_TTL_SECONDS = 7 * 24 * 60 * 60       # audit retention
RATE_LIMIT_PER_MINUTE = 1000               # per pixel_id (web)
RATE_LIMIT_PER_MINUTE_NATIVE = 500         # native app origins — half budget
MAX_ALLOWED_ORIGINS = 50                   # safety cap on allowlist length
MAX_BATCH_EVENTS = 100                     # cap per /events/batch call
DEFAULT_REFUND_WINDOW_DAYS = 30            # refund-eligible window
SUPPORTED_EVENTS = {
    # ── Core (legacy) ──────────────────────────────────────────────────
    "pageview",
    "add_to_cart",
    "purchase",
    "signup",
    "custom",
    "refund",
    "return",
    "order_cancelled",

    # ── Engagement ─────────────────────────────────────────────────────
    "view_content",
    "search",
    "scroll",
    "view_video",
    "click_button",
    "click_link",
    "video_complete",

    # ── Commerce funnel ────────────────────────────────────────────────
    "view_item",
    "view_listing",
    "add_to_wishlist",
    "remove_from_cart",
    "initiate_checkout",
    "add_payment_info",
    "complete_registration",
    "apply_coupon",
    "purchase_success",
    "purchase_fail",

    # ── Subscription / SaaS ────────────────────────────────────────────
    "start_trial",
    "subscribe",
    "upgrade",
    "downgrade",
    "cancel_subscription",
    "trial_end",
    "renewal_success",
    "renewal_fail",

    # ── Lead gen ───────────────────────────────────────────────────────
    "lead_form_view",
    "lead_form_submit",
    "schedule_demo",
    "contact",

    # ── Social / engagement ────────────────────────────────────────────
    "share",
    "comment",
    "like",
    "follow",
    "achievement_unlocked",
    "level_up",
    "tutorial_start",
    "tutorial_complete",

    # ── Game-specific ──────────────────────────────────────────────────
    "game_start",
    "game_end",
    "game_win",
    "game_lose",
    "voucher_claim",
    "voucher_redeem",

    # ── Misc ───────────────────────────────────────────────────────────
    "donate",
    "schedule_appointment",
    "checkin",
    "rate",
    "install",
    "uninstall",
}
REFUND_LIKE_EVENTS = {"refund", "return", "order_cancelled"}
# Events that should flow into the attribution pipeline as conversions
# (purchase-like value-bearing terminal events). The standard `purchase`
# event is the canonical one; success/subscribe duplicates are mapped onto
# the same attribution path.
PURCHASE_LIKE_EVENTS = {"purchase", "purchase_success", "subscribe", "renewal_success"}
# Events that should be treated as new-user acquisitions (signup-like).
SIGNUP_LIKE_EVENTS = {"signup", "complete_registration"}
DEFAULT_SDK_URL = "https://api.kix.gg/sdk/kix-pixel.js"
DEFAULT_EVENT_URL = "https://api.kix.gg/api/v1/pixel/event"

# Origin format regex: web URL OR mini-program / native app identifier.
# We DELIBERATELY do not use HttpUrl any more — Chinese mini-program merchants
# don't send a URL-shaped origin.
_ORIGIN_RE = re.compile(
    r"^(https?://[^\s]+"
    r"|wx[a-zA-Z0-9]{16,}"
    r"|alipay:[a-zA-Z0-9]{16,}"
    r"|(?:ios|android|kix-native):[a-zA-Z0-9._-]+)$"
)
# Native-app origins never come with an HTTP Origin header.
_NATIVE_ORIGIN_PREFIXES = ("wx", "alipay:", "ios:", "android:", "kix-native:")


def _origin_is_native(o: str) -> bool:
    return bool(o) and o.startswith(_NATIVE_ORIGIN_PREFIXES)


def _origin_is_valid(o: str) -> bool:
    return bool(_ORIGIN_RE.match(o))


# ── Pydantic models ────────────────────────────────────────────────────────

class PixelRegisterRequest(BaseModel):
    brand_id: str
    allowed_origins: list[str] = Field(default_factory=list)
    # How long after a `purchase` we accept a corresponding `refund`/`return`
    # event and reverse commission. Outside this window the refund is still
    # audited but does not auto-dispute. 0 disables auto-reversal.
    refund_eligible_within_days: int = Field(
        default=DEFAULT_REFUND_WINDOW_DAYS, ge=0, le=365
    )

    @field_validator("allowed_origins")
    @classmethod
    def _validate_origins(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_ALLOWED_ORIGINS:
            raise ValueError(f"allowed_origins exceeds {MAX_ALLOWED_ORIGINS}")
        cleaned: list[str] = []
        for raw in v:
            if not raw:
                continue
            o = raw.strip()
            # Only URL-shaped origins lose a trailing slash; mini-program /
            # native ids are opaque tokens — leave them alone.
            if o.startswith(("http://", "https://")):
                o = o.rstrip("/")
            if not _origin_is_valid(o):
                raise ValueError(
                    f"origin must be http(s) URL or wx<appid>/alipay:/ios:/"
                    f"android:/kix-native: identifier: {raw}"
                )
            cleaned.append(o)
        # dedupe, preserve order
        seen: set[str] = set()
        out: list[str] = []
        for o in cleaned:
            if o not in seen:
                seen.add(o)
                out.append(o)
        return out


class PixelRegisterResponse(BaseModel):
    pixel_id: str
    brand_id: str
    allowed_origins: list[str]
    refund_eligible_within_days: int
    embed_snippet: str
    sdk_url: str
    created_at: float


class EnhancedData(BaseModel):
    """Hashed PII for Enhanced Conversions matching.

    Pixel events lose cookies under ITP/3P-cookie deprecation. Enhanced
    Conversions let merchants send SHA-256-hashed PII (collected at
    checkout / login) so the server can attribute the event to a kid even
    without a cookie. All fields are lowercase + trimmed before hashing
    on the client side — never accept raw PII here.
    """
    email_sha256: str | None = Field(None, min_length=64, max_length=64)
    phone_sha256: str | None = Field(None, min_length=64, max_length=64)
    first_name_sha256: str | None = Field(None, min_length=64, max_length=64)
    last_name_sha256: str | None = Field(None, min_length=64, max_length=64)
    address_hash: str | None = Field(None, min_length=32, max_length=128)
    external_id: str | None = Field(None, max_length=128)


class PixelEventRequest(BaseModel):
    pixel_id: str
    event_type: str = Field(..., min_length=1, max_length=64)
    user_id: str | None = None
    device_fingerprint: str
    order_id: str | None = None
    event_id: str | None = Field(
        None,
        max_length=128,
        description=(
            "Optional client-provided dedup key, paired with a matching CAPI "
            "event for de-duplication between pixel + server events."
        ),
    )
    amount_cents: int | None = Field(default=None, ge=0)
    currency: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    referrer: str | None = None
    origin: str
    url: str | None = None
    enhanced_data: EnhancedData | None = None


class PixelBatchEventRequest(BaseModel):
    """Single event inside a batch — same shape as PixelEventRequest minus
    pixel_id (which is enforced at the envelope level so the batch can't span
    multiple pixels)."""
    event_type: str = Field(..., min_length=1, max_length=64)
    user_id: str | None = None
    device_fingerprint: str
    order_id: str | None = None
    event_id: str | None = Field(None, max_length=128)
    amount_cents: int | None = Field(default=None, ge=0)
    currency: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    referrer: str | None = None
    origin: str | None = None  # falls back to envelope origin
    url: str | None = None
    enhanced_data: EnhancedData | None = None


class PixelBatchRequest(BaseModel):
    pixel_id: str
    origin: str | None = None  # envelope-level default for child events
    events: list[PixelBatchEventRequest] = Field(default_factory=list)

    @field_validator("events")
    @classmethod
    def _cap_events(cls, v: list[PixelBatchEventRequest]) -> list[PixelBatchEventRequest]:
        if len(v) == 0:
            raise ValueError("events must not be empty")
        if len(v) > MAX_BATCH_EVENTS:
            raise ValueError(f"events exceeds max batch size {MAX_BATCH_EVENTS}")
        return v


class PixelBatchEventResult(BaseModel):
    index: int
    event_id: str | None = None
    event_type: str
    status: str  # "accepted" | "rejected"
    attributed: bool | None = None
    matched: bool = False
    kid: str | None = None
    deduplicated: bool = False
    error: str | None = None


class PixelBatchResponse(BaseModel):
    ok: bool
    accepted: int
    rejected: int
    results: list[PixelBatchEventResult]


class PixelEventResponse(BaseModel):
    ok: bool
    event_id: str
    event_type: str
    attributed: bool | None = None
    source_brand: str | None = None
    attributed_event_id: str | None = None
    matched: bool = False
    kid: str | None = None
    deduplicated: bool = False


class PixelStatsResponse(BaseModel):
    pixel_id: str
    brand_id: str
    total_pageviews: int
    total_add_to_carts: int
    total_purchases: int
    total_signups: int
    total_amount_cents: int
    attributed_purchases: int
    attributed_rate: float


# ── Helpers ────────────────────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _new_pixel_id() -> str:
    # Short URL-safe id; collisions across registrations are infeasible.
    return "px_" + secrets.token_urlsafe(12)


def _normalize_origin(o: str | None) -> str:
    if not o:
        return ""
    s = o.strip()
    # Only URL-shaped origins get trailing-slash normalization; mini-program /
    # native ids are opaque tokens (case-sensitive bundle ids, etc.).
    if s.startswith(("http://", "https://")):
        return s.rstrip("/")
    return s


def _build_snippet(pixel_id: str, sdk_url: str) -> str:
    return (
        "<!-- KiX Pixel -->\n"
        f'<script async src="{sdk_url}" data-pixel="{pixel_id}"></script>\n'
        "<!-- After signup: <script>kix.identify(\"user_123\");</script> -->\n"
        "<!-- After purchase: <script>kix.purchase(\"order_123\", 5000);</script> -->"
    )


async def _load_pixel(r: aioredis.Redis, pixel_id: str) -> dict[str, Any]:
    raw = await r.hgetall(f"pixel:{pixel_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="pixel_not_found")
    if raw.get("status") == "deleted":
        raise HTTPException(status_code=404, detail="pixel_deleted")
    try:
        allowed = json.loads(raw.get("allowed_origins") or "[]")
    except json.JSONDecodeError:
        allowed = []
    try:
        refund_days = int(raw.get("refund_eligible_within_days") or DEFAULT_REFUND_WINDOW_DAYS)
    except (TypeError, ValueError):
        refund_days = DEFAULT_REFUND_WINDOW_DAYS
    return {
        "pixel_id": pixel_id,
        "brand_id": raw.get("brand_id", ""),
        "allowed_origins": list(allowed),
        "refund_eligible_within_days": refund_days,
        "created_at": float(raw.get("created_at") or 0),
        "status": raw.get("status", "active"),
    }


async def _check_rate_limit(
    r: aioredis.Redis,
    pixel_id: str,
    *,
    native: bool = False,
) -> None:
    """Per-pixel sliding-minute rate limit.

    Native-app origins get a tighter budget — they don't have an HTTP Origin
    header to cross-check against, so the body field alone is authoritative.
    """
    minute = int(_now() // 60)
    key = f"pixel:{pixel_id}:ratelimit:{minute}"
    cnt = await r.incr(key)
    if cnt == 1:
        await r.expire(key, 120)
    limit = RATE_LIMIT_PER_MINUTE_NATIVE if native else RATE_LIMIT_PER_MINUTE
    if cnt > limit:
        raise HTTPException(status_code=429, detail="rate_limit_exceeded")


def _check_cors(
    pixel_record: dict[str, Any],
    payload_origin: str,
    header_origin: str | None,
) -> str:
    """Validates the payload `origin` (always) and HTTP `Origin` header
    (when applicable).

    Web origins (http/https): the HTTP `Origin` header is cross-checked when
    present — protects against a hostile page lying in the body.

    Native-app origins (wx*/alipay:/ios:/android:/kix-native:): mobile clients
    do not send an HTTP `Origin` header at all. We accept `body.origin` as
    authoritative for those, and rely on tighter rate-limiting + the
    `allowed_origins` allowlist to bound abuse.

    Empty allowlist means "anything goes" — useful for testing but discouraged
    in production. Returns the normalized origin string for logging.
    """
    allowed = pixel_record.get("allowed_origins") or []
    p_origin = _normalize_origin(payload_origin)
    h_origin = _normalize_origin(header_origin)
    if not p_origin:
        raise HTTPException(status_code=400, detail="missing_origin")
    if not _origin_is_valid(p_origin):
        raise HTTPException(status_code=400, detail="malformed_origin")
    if allowed:
        if p_origin not in allowed:
            raise HTTPException(status_code=403, detail="origin_not_allowed")
        # Header cross-check only meaningful for web origins. Native-app
        # SDKs (WeChat/Alipay/iOS/Android) do not set HTTP Origin, so we skip
        # the cross-check there — the merchant explicitly identifies itself
        # via body.origin and is bound by the tighter native rate limit.
        if not _origin_is_native(p_origin):
            if h_origin and h_origin not in allowed:
                raise HTTPException(status_code=403, detail="origin_header_mismatch")
    return p_origin


async def _record_audit_event(
    r: aioredis.Redis,
    *,
    pixel_id: str,
    brand_id: str,
    event_type: str,
    user_id: str | None,
    device_fingerprint: str,
    origin: str,
    amount_cents: int | None,
    currency: str | None,
    order_id: str | None,
    referrer: str | None,
    url: str | None,
    meta: dict[str, Any],
    attributed: bool | None,
    source_brand: str | None,
) -> str:
    event_id = uuid4().hex
    key = f"pixel_event:{event_id}"
    payload = {
        "event_id": event_id,
        "pixel_id": pixel_id,
        "brand_id": brand_id,
        "event_type": event_type,
        "user_id": user_id or "",
        "device_fingerprint": device_fingerprint or "",
        "origin": origin or "",
        "amount_cents": str(int(amount_cents or 0)),
        "currency": currency or "",
        "order_id": order_id or "",
        "referrer": referrer or "",
        "url": url or "",
        "meta": json.dumps(meta or {}, separators=(",", ":")),
        "timestamp": f"{_now():.6f}",
        "attributed": "1" if attributed else "0",
        "source_brand": source_brand or "",
    }
    pipe = r.pipeline(transaction=False)
    pipe.hset(key, mapping=payload)
    pipe.expire(key, EVENT_TTL_SECONDS)
    # Index purchases by (pixel_id, order_id) so refunds can find them later.
    # We keep the index alive for the full refund window (capped at the
    # default to cap memory in pathological cases — pixels with long
    # custom windows will still match within EVENT_TTL_SECONDS).
    if event_type == "purchase" and order_id:
        index_key = f"pixel:{pixel_id}:order:{order_id}"
        pipe2 = r.pipeline(transaction=False)
        pipe2.set(index_key, event_id)
        pipe2.expire(index_key, EVENT_TTL_SECONDS)
        await pipe2.execute()
    await pipe.execute()
    return event_id


async def _find_purchase_by_order(
    r: aioredis.Redis,
    pixel_id: str,
    order_id: str,
) -> dict[str, Any] | None:
    """Looks up the original purchase audit row for an order_id under a pixel.

    Returns None if missing/expired.
    """
    event_id = await r.get(f"pixel:{pixel_id}:order:{order_id}")
    if not event_id:
        return None
    raw = await r.hgetall(f"pixel_event:{event_id}")
    if not raw:
        return None
    return raw


async def _open_refund_dispute(
    r: aioredis.Redis,
    *,
    brand_id: str,
    pixel_id: str,
    order_id: str,
    refund_event_id: str,
    purchase_event: dict[str, Any],
    refund_amount_cents: int | None,
) -> str | None:
    """Opens an internal dispute to reverse commission on a refunded order.

    Prefers an in-process `disputes.open_internal(...)` helper when available
    (avoids a round-trip + duplicates the per-brand limit only once). Falls
    back to constructing an OpenDisputeRequest and calling the route handler
    directly. Returns the dispute_id on success, None on failure.
    """
    try:
        from app.routers import disputes as disputes_mod
    except Exception as exc:  # pragma: no cover - import guard
        logger.warning("disputes module unavailable: %s", exc)
        return None

    purchase_event_id = purchase_event.get("event_id") or ""
    attributed_source_brand = purchase_event.get("source_brand") or None
    # The purchase event_id doubles as our conversion handle. We don't have
    # the wallet charge_id at this layer (it's internal to attribution), so
    # we reference the conversion via `conversion_id`.
    evidence_text = (
        f"Auto-reversal: refund/return event {refund_event_id} received for "
        f"order_id={order_id} (originally attributed conversion "
        f"{purchase_event_id})."
    )

    helper = getattr(disputes_mod, "open_internal", None)
    if callable(helper):
        try:
            result = await helper(
                r=r,
                brand_id=brand_id,
                conversion_id=purchase_event_id,
                category="refund_attributed",
                evidence={
                    "order_id": order_id,
                    "refund_event_id": refund_event_id,
                    "pixel_id": pixel_id,
                    "refund_amount_cents": refund_amount_cents,
                    "source_brand": attributed_source_brand,
                },
            )
            # helper return shape is implementation-defined; try common keys.
            if isinstance(result, dict):
                return result.get("dispute_id")
            return getattr(result, "dispute_id", None)
        except Exception as exc:
            logger.exception(
                "disputes.open_internal failed: pixel=%s order=%s err=%s",
                pixel_id, order_id, exc,
            )
            return None

    # Fallback: call the public open_dispute route handler in-process.
    # `refund_attributed` may not be in the public category Literal (which
    # is fine — we use the closest existing match and carry refund context
    # in evidence_text so admins can see the reason).
    try:
        OpenReq = getattr(disputes_mod, "OpenDisputeRequest", None)
        opener = getattr(disputes_mod, "open_dispute", None)
        if OpenReq is None or opener is None:
            logger.warning("disputes.open_dispute unavailable; refund not reversed")
            return None
        body = OpenReq(
            brand_id=brand_id,
            conversion_id=purchase_event_id,
            category="wrong_attribution",
            evidence_text=evidence_text,
        )
        resp = await opener(body, r)
        return getattr(resp, "dispute_id", None)
    except HTTPException as http_exc:
        # 409 = duplicate dispute already exists; 429 = brand limit hit.
        logger.info(
            "refund dispute open returned %s for order=%s: %s",
            http_exc.status_code, order_id, http_exc.detail,
        )
        return None
    except Exception as exc:
        logger.exception(
            "refund dispute open failed: pixel=%s order=%s err=%s",
            pixel_id, order_id, exc,
        )
        return None


async def _match_enhanced_data(
    enhanced: dict[str, Any] | None,
    r: aioredis.Redis,
) -> str | None:
    """Look up a KiX ID (kid) by hashed PII supplied with the event.

    Lookups against ``kid:email:<hash>`` / ``kid:phone:<hash>`` first
    (canonical kid_id router indexes), with the older
    ``identity:email:<hash>`` / ``identity:phone:<hash>`` aliases used as a
    fallback for environments still on the legacy index naming. Returns
    ``None`` if nothing matched. Cheap (≤2 GETs in the common case) — safe
    to call on every event.
    """
    if not enhanced:
        return None

    email_h = enhanced.get("email_sha256")
    if email_h:
        kid = await r.get(f"kid:email:{email_h}")
        if kid:
            return kid
        kid = await r.get(f"identity:email:{email_h}")
        if kid:
            return kid

    phone_h = enhanced.get("phone_sha256")
    if phone_h:
        kid = await r.get(f"kid:phone:{phone_h}")
        if kid:
            return kid
        kid = await r.get(f"identity:phone:{phone_h}")
        if kid:
            return kid

    ext_id = enhanced.get("external_id")
    if ext_id:
        kid = await r.get(f"identity:external:{ext_id}")
        if kid:
            return kid

    addr_h = enhanced.get("address_hash")
    if addr_h:
        kid = await r.get(f"identity:address:{addr_h}")
        if kid:
            return kid

    return None


async def _dedup_event_id(
    event_id: str | None,
    r: aioredis.Redis,
    *,
    window_seconds: int = 3600,
) -> bool:
    """Returns True if ``event_id`` has been seen within the dedup window.

    Used to deduplicate browser pixel events against the corresponding
    server-side CAPI conversion. Uses ``SET NX EX`` so concurrent callers
    can race safely: exactly one wins, all others are flagged as duplicate.
    """
    if not event_id:
        return False
    key = f"capi:dedup:{event_id}"
    set_ok = await r.set(key, "1", nx=True, ex=window_seconds)
    return not set_ok


async def _bump_stats(
    r: aioredis.Redis,
    pixel_id: str,
    *,
    event_type: str,
    amount_cents: int | None,
    attributed: bool,
) -> None:
    key = f"pixel:{pixel_id}:stats"
    pipe = r.pipeline(transaction=False)
    # Always keep a per-type counter so the new 30+ event types are visible
    # in stats (`type:view_content`, `type:start_trial`, …) without bloating
    # the legacy top-level fields that dashboards already read.
    pipe.hincrby(key, f"type:{event_type}", 1)
    if event_type == "pageview":
        pipe.hincrby(key, "pageviews", 1)
    elif event_type == "add_to_cart":
        pipe.hincrby(key, "add_to_carts", 1)
    elif event_type in PURCHASE_LIKE_EVENTS:
        pipe.hincrby(key, "purchases", 1)
        if amount_cents:
            pipe.hincrby(key, "total_amount_cents", int(amount_cents))
        if attributed:
            pipe.hincrby(key, "attributed_purchases", 1)
    elif event_type in SIGNUP_LIKE_EVENTS:
        pipe.hincrby(key, "signups", 1)
        if attributed:
            pipe.hincrby(key, "attributed_signups", 1)
    elif event_type in REFUND_LIKE_EVENTS:
        pipe.hincrby(key, "refunds", 1)
        if amount_cents:
            pipe.hincrby(key, "refunded_amount_cents", int(amount_cents))
        if attributed:
            # `attributed` here means a dispute was successfully opened.
            pipe.hincrby(key, "refund_attributed", 1)
    else:
        pipe.hincrby(key, "custom", 1)
    await pipe.execute()


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/register", response_model=PixelRegisterResponse, status_code=201)
async def register_pixel(
    req: PixelRegisterRequest,
    request: Request,
    r: aioredis.Redis = Depends(get_redis),
):
    """Creates a new pixel for a brand and returns the embed snippet."""
    if not req.brand_id:
        raise HTTPException(status_code=422, detail="brand_id_required")

    pixel_id = _new_pixel_id()
    ts = _now()
    sdk_url = str(request.base_url).rstrip("/") + "/sdk/kix-pixel.js"

    record = {
        "brand_id": req.brand_id,
        "allowed_origins": json.dumps(req.allowed_origins),
        "refund_eligible_within_days": str(int(req.refund_eligible_within_days)),
        "created_at": f"{ts:.6f}",
        "status": "active",
    }
    pipe = r.pipeline(transaction=True)
    pipe.hset(f"pixel:{pixel_id}", mapping=record)
    pipe.sadd(f"brand:{req.brand_id}:pixels", pixel_id)
    await pipe.execute()

    logger.info(
        "pixel registered: pixel_id=%s brand_id=%s origins=%d refund_days=%d",
        pixel_id, req.brand_id, len(req.allowed_origins),
        req.refund_eligible_within_days,
    )

    return PixelRegisterResponse(
        pixel_id=pixel_id,
        brand_id=req.brand_id,
        allowed_origins=req.allowed_origins,
        refund_eligible_within_days=req.refund_eligible_within_days,
        embed_snippet=_build_snippet(pixel_id, sdk_url),
        sdk_url=sdk_url,
        created_at=ts,
    )


@router.get("/{pixel_id}/snippet", response_class=PlainTextResponse)
async def get_snippet(
    pixel_id: str,
    request: Request,
    r: aioredis.Redis = Depends(get_redis),
):
    """Returns the merchant embed snippet for an existing pixel."""
    await _load_pixel(r, pixel_id)
    sdk_url = str(request.base_url).rstrip("/") + "/sdk/kix-pixel.js"
    return PlainTextResponse(content=_build_snippet(pixel_id, sdk_url))


async def _process_event(
    r: aioredis.Redis,
    *,
    pixel: dict[str, Any],
    event_type: str,
    user_id: str | None,
    device_fingerprint: str,
    order_id: str | None,
    amount_cents: int | None,
    currency: str | None,
    meta: dict[str, Any],
    referrer: str | None,
    origin: str,
    url: str | None,
    enhanced_data: dict[str, Any] | None = None,
    client_event_id: str | None = None,
) -> tuple[str, bool | None, str | None, str | None, bool, str | None, bool]:
    """Shared ingestion path — audit + stats + side-effects.

    Returns (event_id, attributed, source_brand, attributed_event_id,
    matched, matched_kid, deduplicated).
    Raises HTTPException for client-input validation errors so the caller can
    decide whether to surface (single endpoint) or capture per-row (batch).
    """
    pixel_id = pixel["pixel_id"]
    brand_id = pixel["brand_id"]
    attributed: bool | None = None
    source_brand: str | None = None
    attributed_event_id: str | None = None

    # ── Dedup against CAPI / pixel sibling event ─────────────────────────
    # Browser + server-side CAPI commonly fire the same conversion twice;
    # the merchant supplies a stable `event_id` on both sides so we can
    # collapse them. Duplicates are still audited (with a flag) but skip
    # attribution + stats so commission isn't double-counted.
    deduplicated = await _dedup_event_id(client_event_id, r) if client_event_id else False

    # ── Enhanced Conversions: resolve kid from hashed PII when user_id missing
    matched_kid: str | None = None
    if enhanced_data:
        matched_kid = await _match_enhanced_data(enhanced_data, r)
        if matched_kid and not user_id:
            # Promote the matched kid into the user_id slot so attribution +
            # downstream brand:users sets see a real identity, not just a
            # device fingerprint. Client-supplied user_id (if any) wins to
            # avoid surprising the merchant.
            user_id = matched_kid
    matched = matched_kid is not None

    # ── Attribution side-effects ──────────────────────────────────────────
    # NOTE: user_id from client is a hint only — we never look it up for
    # auth. attribution.track_* functions also treat it as a key only.
    # When deduplicated, we skip attribution entirely — the sibling event
    # (pixel or CAPI) already booked the commission.
    if deduplicated:
        pass
    else:
        try:
            if event_type in PURCHASE_LIKE_EVENTS:
                if not order_id:
                    raise HTTPException(status_code=422, detail="order_id_required")
                if amount_cents is None:
                    raise HTTPException(status_code=422, detail="amount_cents_required")
                effective_uid = user_id or f"anon:{device_fingerprint}"
                conv_req = attr_mod.ConversionCheckRequest(
                    user_id=effective_uid,
                    target_brand=brand_id,
                    order_id=order_id,
                    amount_cents=int(amount_cents),
                    context={
                        "pixel_id": pixel_id,
                        "origin": origin,
                        "referrer": referrer,
                        "currency": currency,
                        "device_fingerprint": device_fingerprint,
                        "meta": meta,
                        "underlying_event_type": event_type,
                    },
                )
                conv_resp = await attr_mod.track_conversion(conv_req, r)
                attributed = bool(conv_resp.attributed)
                source_brand = conv_resp.source_brand
                attributed_event_id = conv_resp.attributed_event_id

            elif event_type in SIGNUP_LIKE_EVENTS:
                if not user_id:
                    raise HTTPException(status_code=422, detail="user_id_required")
                event_id_inner, ts = await attr_mod._persist_event(
                    r,
                    stage=attr_mod.STAGE_VISIT,
                    user_id=user_id,
                    device_fingerprint=device_fingerprint,
                    target_brand=brand_id,
                    meta={
                        "pixel_id": pixel_id,
                        "origin": origin,
                        "referrer": referrer,
                        "source": "pixel_signup",
                        "meta": meta,
                        "underlying_event_type": event_type,
                    },
                )
                is_new = await r.sadd(f"brand:{brand_id}:users", user_id)
                if is_new:
                    await r.hset(
                        f"brand:{brand_id}:user_first_seen",
                        user_id,
                        f"{ts:.6f}",
                    )
                attr_event = await attr_mod.find_attribution(
                    r, user_id, brand_id, attr_mod.ATTRIBUTION_WINDOW_SECONDS
                )
                if attr_event:
                    attributed = True
                    source_brand = attr_event.get("source_brand")
                    attributed_event_id = attr_event.get("event_id")
                else:
                    attributed = False
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception(
                "pixel event attribution failure: pixel_id=%s type=%s err=%s",
                pixel_id, event_type, exc,
            )
            # Don't fail the merchant page — audit still recorded below.

    # ── Audit ─────────────────────────────────────────────────────────────
    event_id = await _record_audit_event(
        r,
        pixel_id=pixel_id,
        brand_id=brand_id,
        event_type=event_type,
        user_id=user_id,
        device_fingerprint=device_fingerprint,
        origin=origin,
        amount_cents=amount_cents,
        currency=currency,
        order_id=order_id,
        referrer=referrer,
        url=url,
        meta=meta,
        attributed=attributed,
        source_brand=source_brand,
    )

    # ── Refund / return / cancel handling ────────────────────────────────
    # Look up the original purchase, then open an internal dispute to
    # reverse commission. The audit row is already persisted regardless
    # — auto-reversal failure must not erase the refund record.
    refund_dispute_opened = False
    if event_type in REFUND_LIKE_EVENTS and order_id:
        refund_days = pixel.get(
            "refund_eligible_within_days", DEFAULT_REFUND_WINDOW_DAYS
        )
        if refund_days > 0:
            original = await _find_purchase_by_order(r, pixel_id, order_id)
            if original is None:
                logger.info(
                    "refund received but no matching purchase: pixel=%s order=%s",
                    pixel_id, order_id,
                )
            else:
                # Within the eligibility window?
                try:
                    purchase_ts = float(original.get("timestamp") or 0)
                except (TypeError, ValueError):
                    purchase_ts = 0.0
                age_days = (_now() - purchase_ts) / 86400.0 if purchase_ts else 0.0
                was_attributed = original.get("attributed") == "1"
                if not was_attributed:
                    # Nothing to reverse — original purchase wasn't attributed.
                    pass
                elif age_days > refund_days:
                    logger.info(
                        "refund outside eligibility window: pixel=%s order=%s "
                        "age_days=%.2f window=%d",
                        pixel_id, order_id, age_days, refund_days,
                    )
                else:
                    dispute_id = await _open_refund_dispute(
                        r,
                        brand_id=brand_id,
                        pixel_id=pixel_id,
                        order_id=order_id,
                        refund_event_id=event_id,
                        purchase_event=original,
                        refund_amount_cents=amount_cents,
                    )
                    if dispute_id:
                        refund_dispute_opened = True
                        # Surface dispute_id on the audit row for traceability.
                        await r.hset(
                            f"pixel_event:{event_id}",
                            "refund_dispute_id",
                            dispute_id,
                        )

    if event_type in REFUND_LIKE_EVENTS:
        # For refund-like events, `attributed` field of the response means
        # "we opened an auto-reversal dispute". Keep semantics distinct from
        # purchase attribution.
        attributed = refund_dispute_opened

    # ── Stats ─────────────────────────────────────────────────────────────
    # Dedup'd events do NOT bump stats — the sibling event already did.
    if not deduplicated:
        await _bump_stats(
            r,
            pixel_id,
            event_type=event_type,
            amount_cents=amount_cents,
            attributed=bool(attributed),
        )

    # Annotate the audit row with enhanced-match + dedup flags for ops + ML.
    if matched or deduplicated or client_event_id:
        try:
            extra = {
                "matched": "1" if matched else "0",
                "deduplicated": "1" if deduplicated else "0",
            }
            if matched_kid:
                extra["matched_kid"] = matched_kid
            if client_event_id:
                extra["client_event_id"] = client_event_id
            await r.hset(f"pixel_event:{event_id}", mapping=extra)
        except Exception:  # pragma: no cover — best-effort annotation
            pass

    return (
        event_id,
        attributed,
        source_brand,
        attributed_event_id,
        matched,
        matched_kid,
        deduplicated,
    )


@router.post("/event", response_model=PixelEventResponse)
async def record_event(
    req: PixelEventRequest,
    request: Request,
    origin_header: str | None = Header(default=None, alias="Origin"),
    r: aioredis.Redis = Depends(get_redis),
):
    """Single ingestion endpoint for all browser-side / mobile pixel events.

    Validates origin, rate-limits, audits, bumps stats, and bridges purchase
    + signup events into the attribution pipeline. Refund/return events
    auto-open a dispute to reverse commission.
    """
    pixel = await _load_pixel(r, req.pixel_id)
    origin = _check_cors(pixel, req.origin, origin_header)
    await _check_rate_limit(r, req.pixel_id, native=_origin_is_native(origin))

    if req.event_type not in SUPPORTED_EVENTS:
        raise HTTPException(status_code=422, detail="unsupported_event_type")

    (
        event_id,
        attributed,
        source_brand,
        attributed_event_id,
        matched,
        matched_kid,
        deduplicated,
    ) = await _process_event(
        r,
        pixel=pixel,
        event_type=req.event_type,
        user_id=req.user_id,
        device_fingerprint=req.device_fingerprint,
        order_id=req.order_id,
        amount_cents=req.amount_cents,
        currency=req.currency,
        meta=req.meta,
        referrer=req.referrer,
        origin=origin,
        url=req.url,
        enhanced_data=(req.enhanced_data.model_dump(exclude_none=True)
                       if req.enhanced_data else None),
        client_event_id=req.event_id,
    )

    return PixelEventResponse(
        ok=True,
        event_id=event_id,
        event_type=req.event_type,
        attributed=attributed,
        source_brand=source_brand,
        attributed_event_id=attributed_event_id,
        matched=matched,
        kid=matched_kid,
        deduplicated=deduplicated,
    )


@router.post("/events/batch", response_model=PixelBatchResponse)
async def record_events_batch(
    req: PixelBatchRequest,
    request: Request,
    origin_header: str | None = Header(default=None, alias="Origin"),
    r: aioredis.Redis = Depends(get_redis),
):
    """Batch event ingestion (up to 100 events / call).

    Critical for high-volume mobile clients (e.g. 老黄's 50K events/day):
    one HTTP round-trip per batch, one origin/rate-limit check per batch,
    parallel per-event processing inside.

    Partial success: per-event errors land in `results[i].error` with
    `status=rejected`; the envelope is `ok=True` whenever at least one
    event accepted.
    """
    pixel = await _load_pixel(r, req.pixel_id)

    # Envelope origin — falls back to first child's origin if envelope omitted.
    envelope_origin_raw = req.origin or (req.events[0].origin if req.events else None)
    if not envelope_origin_raw:
        raise HTTPException(status_code=400, detail="missing_origin")
    origin = _check_cors(pixel, envelope_origin_raw, origin_header)

    # One rate-limit slot per batch — that's the whole point of batching.
    await _check_rate_limit(r, req.pixel_id, native=_origin_is_native(origin))

    async def _one(idx: int, ev: PixelBatchEventRequest) -> PixelBatchEventResult:
        if ev.event_type not in SUPPORTED_EVENTS:
            return PixelBatchEventResult(
                index=idx,
                event_type=ev.event_type,
                status="rejected",
                error="unsupported_event_type",
            )
        # Per-event origin must agree with envelope (defend against a batch
        # smuggling events from an off-pixel origin).
        ev_origin = _normalize_origin(ev.origin) if ev.origin else origin
        if ev_origin != origin:
            return PixelBatchEventResult(
                index=idx,
                event_type=ev.event_type,
                status="rejected",
                error="origin_mismatch_within_batch",
            )
        try:
            (
                event_id,
                attributed,
                _src,
                _attr_id,
                matched,
                matched_kid,
                deduplicated,
            ) = await _process_event(
                r,
                pixel=pixel,
                event_type=ev.event_type,
                user_id=ev.user_id,
                device_fingerprint=ev.device_fingerprint,
                order_id=ev.order_id,
                amount_cents=ev.amount_cents,
                currency=ev.currency,
                meta=ev.meta,
                referrer=ev.referrer,
                origin=origin,
                url=ev.url,
                enhanced_data=(ev.enhanced_data.model_dump(exclude_none=True)
                               if ev.enhanced_data else None),
                client_event_id=ev.event_id,
            )
            return PixelBatchEventResult(
                index=idx,
                event_id=event_id,
                event_type=ev.event_type,
                status="accepted",
                attributed=attributed,
                matched=matched,
                kid=matched_kid,
                deduplicated=deduplicated,
            )
        except HTTPException as http_exc:
            detail = http_exc.detail if isinstance(http_exc.detail, str) else "validation_error"
            return PixelBatchEventResult(
                index=idx,
                event_type=ev.event_type,
                status="rejected",
                error=detail,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("batch event %d failed: %s", idx, exc)
            return PixelBatchEventResult(
                index=idx,
                event_type=ev.event_type,
                status="rejected",
                error="internal_error",
            )

    results = await asyncio.gather(
        *[_one(i, ev) for i, ev in enumerate(req.events)]
    )
    accepted = sum(1 for r_ in results if r_.status == "accepted")
    rejected = len(results) - accepted

    return PixelBatchResponse(
        ok=accepted > 0,
        accepted=accepted,
        rejected=rejected,
        results=list(results),
    )


@router.get("/{pixel_id}/stats", response_model=PixelStatsResponse)
async def get_stats(
    pixel_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Returns rolled-up counters for a pixel."""
    pixel = await _load_pixel(r, pixel_id)
    raw = await r.hgetall(f"pixel:{pixel_id}:stats")

    def _i(key: str) -> int:
        try:
            return int(raw.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    pageviews = _i("pageviews")
    purchases = _i("purchases")
    signups = _i("signups")
    add_to_carts = _i("add_to_carts")
    attributed_purch = _i("attributed_purchases")
    total_amount = _i("total_amount_cents")
    rate = (attributed_purch / purchases) if purchases else 0.0

    return PixelStatsResponse(
        pixel_id=pixel_id,
        brand_id=pixel["brand_id"],
        total_pageviews=pageviews,
        total_add_to_carts=add_to_carts,
        total_purchases=purchases,
        total_signups=signups,
        total_amount_cents=total_amount,
        attributed_purchases=attributed_purch,
        attributed_rate=round(rate, 4),
    )


@router.delete("/{pixel_id}", status_code=204)
async def delete_pixel(
    pixel_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Soft-deletes a pixel (events are still recorded as 404)."""
    pixel = await _load_pixel(r, pixel_id)
    pipe = r.pipeline(transaction=True)
    pipe.hset(f"pixel:{pixel_id}", "status", "deleted")
    pipe.srem(f"brand:{pixel['brand_id']}:pixels", pixel_id)
    await pipe.execute()
    return Response(status_code=204)


@router.get("/brand/{brand_id}", response_model=list[str])
async def list_brand_pixels(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Lists active pixel_ids registered to a brand."""
    members = await r.smembers(f"brand:{brand_id}:pixels")
    return sorted(members)
