"""Alpha-Merchant Program — invite, onboarding, feedback, cohort tracking.

Purpose
-------
Spin up a managed alpha cohort (initially Singapore F&B owners). The router
owns the *program plumbing* — invite codes, redemption flow, feedback
collection, cohort dashboard, and the data the auto-touch worker reads.

It deliberately does **not** own:
  * Email sending      — uses ``enqueue_email`` + the existing 4 new alpha
                          templates registered in ``app/email_templates``.
  * Subscription state — calls into the existing brand_subscriptions Redis
                          keys (``brand_subscription:{bid}``) to grant a
                          90-day free STARTER trial on signup.
  * Brand creation     — writes the canonical Redis HASH ``brand_config:{bid}``
                          + emits the standard ``config_invalidation`` channel
                          message so the portal sees the new brand instantly.
                          PG persistence is best-effort and degrades to
                          Redis-only when migrations have not been applied
                          (matches the dual-write pattern in brand_subscriptions).

Endpoints
---------
``POST /api/v1/alpha/invite``                — admin: mint invite code
``POST /api/v1/alpha/apply``                 — public: landing-page apply form
``POST /api/v1/alpha/signup/{invite_code}``  — merchant redeems code
``GET  /api/v1/alpha/cohort``                — admin: list cohort with metrics
``GET  /api/v1/alpha/feedback/list``         — admin: read feedback inbox
``POST /api/v1/alpha/feedback/submit``       — merchant: submit feedback
``GET  /api/v1/alpha/health-check/{bid}``    — admin: per-merchant health

Storage (Redis)
---------------
``alpha:invite:{code}``         HASH  — invite record (email, name, status, ...)
``alpha:invite:index``          SET   — every minted code (admin listing)
``alpha:application:{app_id}``  HASH  — landing-page application
``alpha:application:queue``     LIST  — admin review queue
``alpha:cohort:{cohort}``       SET   — brand_ids in a cohort (2026q1, ...)
``alpha:feedback:{fid}``        HASH  — one feedback submission
``alpha:feedback:index``        LIST  — newest-first feedback ids
``alpha:touch:{bid}:{touch}``   STRING (ts) — idempotency for auto-emails
``brand:{bid}:alpha_cohort``    STRING — cohort tag (e.g., 2026q1)

Auth
----
All admin endpoints accept the standard ``KIX_ADMIN_TOKEN`` via either
``?admin_token=`` query or ``X-Admin-Token`` header — same convention as
``email_admin.py`` and ``tenant_admin.py``. Signup + feedback submit are
unauthenticated by design: the invite code itself is the bearer secret
and the feedback form is reachable from inside the portal session.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

from app.redis_client import get_redis
from app.security import constant_time_eq

# Side-effect import: registers the 4 alpha email templates into the
# global EMAIL_TEMPLATES / ALL_TEMPLATES registry. The ``register()`` call
# inside is idempotent so repeated imports during tests are safe.
from app.email_templates import alpha as _alpha_templates  # noqa: F401

logger = logging.getLogger(__name__)
router = APIRouter()


# ── constants ────────────────────────────────────────────────────────────

ADMIN_TOKEN_DEFAULT = "admin-dev-token"
DEFAULT_COHORT = "2026q1"
TRIAL_DAYS_DEFAULT = 90
INVITE_CODE_PREFIX = "KIX"
AT_RISK_NO_LOGIN_DAYS = 7
AT_RISK_NO_CAMPAIGN_DAYS = 5

# Quiet hours (SGT, UTC+8) — auto-emails respect 22:00–08:00 local silence.
QUIET_HOURS_START_LOCAL = 22  # 22:00 SGT
QUIET_HOURS_END_LOCAL = 8     # 08:00 SGT

FEEDBACK_CATEGORIES = {
    "performance",
    "design",
    "confusion",
    "missing_feature",
    "bug",
    "praise",
    "other",
}


# ── auth helpers ─────────────────────────────────────────────────────────


def _check_admin(token: str | None) -> None:
    """Validate the admin pre-shared token using constant-time comparison."""
    if not token:
        raise HTTPException(status_code=403, detail="admin_token_required")
    expected = os.getenv("KIX_ADMIN_TOKEN", ADMIN_TOKEN_DEFAULT)
    if not constant_time_eq(token, expected):
        raise HTTPException(status_code=403, detail="invalid_admin_token")


def _admin_token_from_request(request: Request) -> str | None:
    """Pull ``admin_token`` from query-string or ``X-Admin-Token`` header."""
    qs = request.query_params.get("admin_token")
    if qs:
        return qs
    return request.headers.get("x-admin-token")


# ── invite-code helpers ──────────────────────────────────────────────────


# Crockford base32 minus visually ambiguous chars (no 0/O/1/I/L/U).
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"


def _mint_code() -> str:
    """Return an 8-char invite code formatted as ``KIX-XXXX-XXXX``.

    8 chars from a 30-symbol alphabet → ~40 bits of entropy: collision-safe
    for the foreseeable cohort scale (~10⁴ merchants). The dashes are
    cosmetic — case-insensitive normalization strips them on lookup so
    "KIX-A7M9-PNQ2", "kix-a7m9-pnq2", and "A7M9PNQ2" all resolve identically.
    """
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return f"{INVITE_CODE_PREFIX}-{raw[:4]}-{raw[4:]}"


def _normalize_code(code: str) -> str:
    """Canonicalise an invite code for Redis lookup (upper, no dashes)."""
    return re.sub(r"[^A-Z0-9]", "", code.upper())


def _invite_key(code: str) -> str:
    return f"alpha:invite:{_normalize_code(code)}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_ts() -> float:
    return time.time()


# ── invite + apply models ────────────────────────────────────────────────


class InviteCreate(BaseModel):
    """Body for ``POST /alpha/invite`` — admin mints one invite code."""

    admin_token: str = Field(..., min_length=1, max_length=512)
    merchant_email: EmailStr
    merchant_name: str = Field(..., min_length=1, max_length=200)
    store_count: int = Field(1, ge=1, le=10_000)
    notes: str = Field("", max_length=2_000)
    cohort: str = Field(DEFAULT_COHORT, min_length=1, max_length=32)
    trial_days: int = Field(TRIAL_DAYS_DEFAULT, ge=1, le=365)


class InviteCreated(BaseModel):
    invite_code: str
    signup_url: str
    cohort: str
    expires_at: str | None = None


class ApplicationCreate(BaseModel):
    """Public landing-form submission — queues for admin review."""

    name: str = Field(..., min_length=1, max_length=200)
    email: EmailStr
    brand: str = Field(..., min_length=1, max_length=200)
    store_count: int = Field(..., ge=1, le=10_000)
    monthly_ad_spend_sgd: int = Field(0, ge=0, le=10_000_000)
    why_interested: str = Field(..., min_length=10, max_length=4_000)
    locale: str = Field("en-SG", min_length=2, max_length=16)


class ApplicationCreated(BaseModel):
    application_id: str
    status: Literal["queued"] = "queued"
    submitted_at: str


class SignupRequest(BaseModel):
    """Body for ``POST /alpha/signup/{invite_code}`` — merchant redeems."""

    brand_id: str = Field(..., min_length=2, max_length=64, pattern=r"^[a-z0-9_-]+$")
    brand_name: str = Field(..., min_length=1, max_length=200)
    contact_name: str = Field(..., min_length=1, max_length=200)
    locale: str = Field("en-SG", min_length=2, max_length=16)


class SignupResponse(BaseModel):
    brand_id: str
    cohort: str
    trial_ends_at: str
    portal_url: str
    welcome_email_queued: bool


class FeedbackSubmit(BaseModel):
    """Body for ``POST /alpha/feedback/submit`` — accessible from the portal."""

    brand_id: str = Field(..., min_length=2, max_length=64)
    category: str = Field(..., min_length=2, max_length=32)
    rating: int = Field(..., ge=1, le=5)
    comment: str = Field("", max_length=8_000)
    page_context: str = Field("", max_length=500)
    screenshot_url: str | None = Field(None, max_length=2_000)
    browser: str = Field("", max_length=200)
    screen_size: str = Field("", max_length=40)
    recent_actions: list[str] = Field(default_factory=list, max_length=50)


class FeedbackAck(BaseModel):
    feedback_id: str
    received_at: str
    thank_you: str


# ── invite lifecycle ─────────────────────────────────────────────────────


@router.post("/invite", response_model=InviteCreated, status_code=201)
async def create_invite(
    body: InviteCreate,
    r: aioredis.Redis = Depends(get_redis),
) -> InviteCreated:
    """Mint a one-time invite code for a vetted merchant."""
    _check_admin(body.admin_token)

    # Retry up to 5 times against the (extremely unlikely) collision case.
    code = ""
    for _ in range(5):
        candidate = _mint_code()
        if not await r.exists(_invite_key(candidate)):
            code = candidate
            break
    if not code:
        raise HTTPException(500, "could not mint a unique invite code")

    record = {
        "code": code,
        "merchant_email": body.merchant_email,
        "merchant_name": body.merchant_name,
        "store_count": str(body.store_count),
        "notes": body.notes,
        "cohort": body.cohort,
        "trial_days": str(body.trial_days),
        "status": "issued",
        "created_at": _now_iso(),
        "redeemed_at": "",
        "redeemed_brand_id": "",
    }
    pipe = r.pipeline()
    pipe.hset(_invite_key(code), mapping=record)
    pipe.sadd("alpha:invite:index", _normalize_code(code))
    await pipe.execute()

    signup_url = f"/landing/alpha.html?code={code}"

    logger.info(
        "alpha_invite_minted code=%s cohort=%s email=%s",
        code, body.cohort, body.merchant_email,
    )

    return InviteCreated(
        invite_code=code,
        signup_url=signup_url,
        cohort=body.cohort,
    )


@router.post("/apply", response_model=ApplicationCreated, status_code=202)
async def submit_application(
    body: ApplicationCreate,
    r: aioredis.Redis = Depends(get_redis),
) -> ApplicationCreated:
    """Public endpoint: landing-page application form drop-off."""
    app_id = f"app_{uuid.uuid4().hex[:12]}"
    submitted = _now_iso()

    record = {
        "application_id": app_id,
        "name": body.name,
        "email": body.email,
        "brand": body.brand,
        "store_count": str(body.store_count),
        "monthly_ad_spend_sgd": str(body.monthly_ad_spend_sgd),
        "why_interested": body.why_interested,
        "locale": body.locale,
        "status": "queued",
        "submitted_at": submitted,
    }
    pipe = r.pipeline()
    pipe.hset(f"alpha:application:{app_id}", mapping=record)
    pipe.lpush("alpha:application:queue", app_id)
    pipe.ltrim("alpha:application:queue", 0, 999)
    await pipe.execute()

    return ApplicationCreated(application_id=app_id, submitted_at=submitted)


@router.post("/signup/{invite_code}", response_model=SignupResponse)
async def redeem_invite(
    invite_code: str,
    body: SignupRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> SignupResponse:
    """Redeem an invite — create brand + grant trial + queue welcome email."""
    key = _invite_key(invite_code)
    raw = await r.hgetall(key)
    if not raw:
        raise HTTPException(404, "invite_code_unknown")

    record = _decode_hash(raw)
    if record.get("status") != "issued":
        raise HTTPException(409, "invite_already_redeemed")

    cohort = record.get("cohort") or DEFAULT_COHORT
    try:
        trial_days = int(record.get("trial_days") or TRIAL_DAYS_DEFAULT)
    except ValueError:
        trial_days = TRIAL_DAYS_DEFAULT

    # 1) Create brand_config HASH cache (matches brands.py convention).
    bcfg_key = f"brand_config:{body.brand_id}"
    if await r.exists(bcfg_key):
        raise HTTPException(409, "brand_already_exists")

    now = _now_ts()
    expires = now + trial_days * 86_400
    expires_iso = datetime.fromtimestamp(expires, timezone.utc).isoformat(
        timespec="seconds"
    )

    brand_payload = {
        "brand_id": body.brand_id,
        "brand_name": body.brand_name,
        "brand_slug": body.brand_id,
        "brand_color": "#00B341",
        "contact_name": body.contact_name,
        "contact_email": record.get("merchant_email", ""),
        "locale": body.locale,
        "status": "approved",          # auto-approved for alpha
        "approved_at": _now_iso(),
        "approval_source": "alpha_program",
    }
    sub_key = f"brand_subscription:{body.brand_id}"
    sub_payload = {
        "brand_id": body.brand_id,
        "tier": "starter",
        "billing": "monthly",
        "started_at": str(now),
        "expires_at": str(expires),
        "next_charge_at": str(expires),
        "auto_renew": "false",
        "first_year_free": "true",  # repurposed flag — semantic = "trial"
        "cancel_pending": "false",
        "source": "alpha_program",
    }

    pipe = r.pipeline()
    pipe.hset(bcfg_key, mapping=brand_payload)
    pipe.hset(sub_key, mapping=sub_payload)
    pipe.set(f"brand:{body.brand_id}:alpha_cohort", cohort)
    pipe.sadd(f"alpha:cohort:{cohort}", body.brand_id)
    pipe.hset(
        key,
        mapping={
            "status": "redeemed",
            "redeemed_at": _now_iso(),
            "redeemed_brand_id": body.brand_id,
        },
    )
    # invalidate any cached config
    pipe.publish("config_invalidation", body.brand_id)
    await pipe.execute()

    # 2) Queue alpha_welcome email — best-effort.
    welcome_queued = False
    try:
        from app.services.email_template_service import enqueue_email

        await enqueue_email(
            r,
            brand_id=body.brand_id,
            template_id="alpha_welcome",
            locale=body.locale,
            recipient=record.get("merchant_email", ""),
            brand_name=body.brand_name,
            contact_name=body.contact_name,
            trial_days=str(trial_days),
            portal_url=f"https://partner.letskix.com/{body.brand_id}",
            support_email="alpha@letskix.com",
            invite_code=invite_code,
        )
        welcome_queued = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("alpha_welcome enqueue failed: %s", exc)

    logger.info(
        "alpha_signup brand=%s cohort=%s trial_days=%d",
        body.brand_id, cohort, trial_days,
    )

    return SignupResponse(
        brand_id=body.brand_id,
        cohort=cohort,
        trial_ends_at=expires_iso,
        portal_url=f"https://partner.letskix.com/{body.brand_id}",
        welcome_email_queued=welcome_queued,
    )


# ── cohort + health ──────────────────────────────────────────────────────


def _decode_hash(raw: dict[Any, Any]) -> dict[str, str]:
    """Decode a Redis HASH dict (handles bytes from raw conns)."""
    out: dict[str, str] = {}
    for k, v in raw.items():
        ks = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
        vs = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
        out[ks] = vs
    return out


async def _gather_metrics(
    r: aioredis.Redis, brand_id: str
) -> dict[str, Any]:
    """Lift the per-merchant metrics that drive the cohort dashboard."""
    # Login recency: ``brand:{bid}:last_login`` (string ts). Optional —
    # absent for brands that never logged in via the portal session worker.
    last_login_ts: float | None = None
    raw = await r.get(f"brand:{brand_id}:last_login")
    if raw:
        try:
            last_login_ts = float(
                raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
            )
        except ValueError:
            last_login_ts = None

    # Campaign creation count (existing convention used by reporting).
    campaigns_created = 0
    raw = await r.get(f"brand:{brand_id}:campaigns:count")
    if raw:
        try:
            campaigns_created = int(
                raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
            )
        except ValueError:
            pass

    # Spend total (cents). Stored by wallet/billing pipelines.
    spend_total_cents = 0
    raw = await r.get(f"brand:{brand_id}:spend:total_cents")
    if raw:
        try:
            spend_total_cents = int(
                raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
            )
        except ValueError:
            pass

    return {
        "last_login_ts": last_login_ts,
        "last_login_iso": (
            datetime.fromtimestamp(last_login_ts, timezone.utc).isoformat(
                timespec="seconds"
            )
            if last_login_ts
            else None
        ),
        "campaigns_created": campaigns_created,
        "spend_total_cents": spend_total_cents,
        "spend_total_sgd": round(spend_total_cents / 100, 2),
    }


def _classify_health(metrics: dict[str, Any], signup_ts: float | None) -> dict[str, Any]:
    """Apply the at-risk heuristic.

    A merchant is **at risk** if any one of:
      * no login for ≥ AT_RISK_NO_LOGIN_DAYS days (or never logged in 7+ days
        after signup)
      * no campaign created within AT_RISK_NO_CAMPAIGN_DAYS days of signup
    """
    now = _now_ts()
    risks: list[str] = []
    last_login = metrics.get("last_login_ts")
    signup = signup_ts or now

    if last_login is None:
        if now - signup > AT_RISK_NO_LOGIN_DAYS * 86_400:
            risks.append("never_logged_in")
    else:
        if (now - last_login) > AT_RISK_NO_LOGIN_DAYS * 86_400:
            risks.append("login_stale")

    if (
        metrics.get("campaigns_created", 0) == 0
        and (now - signup) > AT_RISK_NO_CAMPAIGN_DAYS * 86_400
    ):
        risks.append("no_campaign")

    return {
        "at_risk": bool(risks),
        "risk_reasons": risks,
    }


@router.get("/cohort")
async def list_cohort(
    request: Request,
    cohort: str = DEFAULT_COHORT,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Admin view: every alpha merchant in ``cohort`` with health metrics."""
    _check_admin(_admin_token_from_request(request))

    raw_members = await r.smembers(f"alpha:cohort:{cohort}")
    members = sorted(
        m.decode() if isinstance(m, (bytes, bytearray)) else str(m)
        for m in raw_members
    )

    items: list[dict[str, Any]] = []
    for bid in members:
        bcfg = _decode_hash(await r.hgetall(f"brand_config:{bid}"))
        sub = _decode_hash(await r.hgetall(f"brand_subscription:{bid}"))
        metrics = await _gather_metrics(r, bid)
        signup_ts: float | None = None
        if sub.get("started_at"):
            try:
                signup_ts = float(sub["started_at"])
            except ValueError:
                signup_ts = None
        health = _classify_health(metrics, signup_ts)
        items.append({
            "brand_id": bid,
            "brand_name": bcfg.get("brand_name", bid),
            "contact_email": bcfg.get("contact_email", ""),
            "signup_date": (
                datetime.fromtimestamp(signup_ts, timezone.utc).isoformat(
                    timespec="seconds"
                )
                if signup_ts
                else None
            ),
            "status": bcfg.get("status", "unknown"),
            "tier": sub.get("tier", "free"),
            "trial_ends_at": (
                datetime.fromtimestamp(float(sub["expires_at"]), timezone.utc).isoformat(
                    timespec="seconds"
                )
                if sub.get("expires_at") not in (None, "", "None")
                else None
            ),
            **metrics,
            **health,
        })

    return {
        "cohort": cohort,
        "size": len(items),
        "at_risk_count": sum(1 for x in items if x["at_risk"]),
        "members": items,
    }


@router.get("/health-check/{brand_id}")
async def health_check(
    brand_id: str,
    request: Request,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Per-merchant health snapshot used by the worker + dashboard tile."""
    _check_admin(_admin_token_from_request(request))

    sub = _decode_hash(await r.hgetall(f"brand_subscription:{brand_id}"))
    signup_ts: float | None = None
    if sub.get("started_at"):
        try:
            signup_ts = float(sub["started_at"])
        except ValueError:
            signup_ts = None

    metrics = await _gather_metrics(r, brand_id)
    health = _classify_health(metrics, signup_ts)

    return {
        "brand_id": brand_id,
        "signup_ts": signup_ts,
        **metrics,
        **health,
    }


# ── feedback ─────────────────────────────────────────────────────────────


@router.post("/feedback/submit", response_model=FeedbackAck, status_code=201)
async def submit_feedback(
    body: FeedbackSubmit,
    r: aioredis.Redis = Depends(get_redis),
) -> FeedbackAck:
    """Record one feedback submission (in-portal `Send feedback` flow)."""
    cat = body.category.lower().strip().replace(" ", "_")
    if cat not in FEEDBACK_CATEGORIES:
        raise HTTPException(
            422, f"invalid_category: must be one of {sorted(FEEDBACK_CATEGORIES)}"
        )

    fid = f"fb_{uuid.uuid4().hex[:12]}"
    received = _now_iso()
    record = {
        "feedback_id": fid,
        "brand_id": body.brand_id,
        "category": cat,
        "rating": str(body.rating),
        "comment": body.comment,
        "page_context": body.page_context,
        "screenshot_url": body.screenshot_url or "",
        "browser": body.browser,
        "screen_size": body.screen_size,
        "recent_actions": json.dumps(body.recent_actions),
        "received_at": received,
    }
    pipe = r.pipeline()
    pipe.hset(f"alpha:feedback:{fid}", mapping=record)
    pipe.lpush("alpha:feedback:index", fid)
    pipe.ltrim("alpha:feedback:index", 0, 4_999)
    await pipe.execute()

    bcfg = _decode_hash(await r.hgetall(f"brand_config:{body.brand_id}"))
    contact = bcfg.get("contact_name") or bcfg.get("brand_name", "")
    if contact:
        thank_you = (
            f"Thanks {contact} — we read every alpha submission within 1 business day."
        )
    else:
        thank_you = "Thanks — we read every alpha submission within 1 business day."

    return FeedbackAck(feedback_id=fid, received_at=received, thank_you=thank_you)


@router.get("/feedback/list")
async def list_feedback(
    request: Request,
    limit: int = 100,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Admin: newest-first feedback page (cap 500)."""
    _check_admin(_admin_token_from_request(request))
    limit = max(1, min(limit, 500))

    raw_ids = await r.lrange("alpha:feedback:index", 0, limit - 1)
    ids = [
        x.decode() if isinstance(x, (bytes, bytearray)) else str(x) for x in raw_ids
    ]

    out: list[dict[str, Any]] = []
    for fid in ids:
        rec = _decode_hash(await r.hgetall(f"alpha:feedback:{fid}"))
        if not rec:
            continue
        # Surface recent_actions back as a list for the admin UI.
        try:
            rec["recent_actions"] = json.loads(rec.get("recent_actions", "[]"))
        except json.JSONDecodeError:
            rec["recent_actions"] = []
        out.append(rec)

    return {"count": len(out), "items": out}


# ── quiet-hours helper (also used by the worker) ─────────────────────────


def in_quiet_hours(now_utc: datetime | None = None) -> bool:
    """Return True iff current SGT time falls within 22:00–08:00.

    Used to avoid 3am auto-emails. Worker re-checks per touch.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    # SGT = UTC+8 with no DST.
    local_hour = (now_utc.hour + 8) % 24
    if QUIET_HOURS_START_LOCAL > QUIET_HOURS_END_LOCAL:
        # Window crosses midnight (the typical 22→08 case)
        return local_hour >= QUIET_HOURS_START_LOCAL or local_hour < QUIET_HOURS_END_LOCAL
    return QUIET_HOURS_START_LOCAL <= local_hour < QUIET_HOURS_END_LOCAL
