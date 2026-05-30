"""WhatsApp OTP auth router — phone-based login / registration.

Wave E item 6. Lives alongside the existing email/device-sig auth
router (``app/routers/auth.py``) — both stay reachable so existing
clients keep working while SEA consumer / SMB merchant traffic migrates
to phone-OTP.

Endpoints
---------
* ``POST /api/v1/auth/whatsapp/request-otp``
* ``POST /api/v1/auth/whatsapp/verify-otp``   → returns JWT pair
* ``POST /api/v1/auth/whatsapp/refresh``
* ``GET  /api/v1/auth/whatsapp/health``

The verify endpoint upserts a ``UserProfile`` row keyed by the
hashed phone number (stored in ``device_sig`` so we re-use the
existing ``UNIQUE(brand_id, device_sig)`` constraint without a schema
migration on the hot path). The matching ``auth_method = 'whatsapp'``
column is added in the additive migration in the sibling commit; this
router tolerates either the new column being present OR absent so we
can ship the router first and migrate later.

Mock mode
---------
When no WhatsApp Business credentials are present in the env, the
service returns the generated code in the ``request-otp`` response under
``debug_code``. This is gated by ``mode`` (which the response reports
explicitly), so dev / CI / Cypress can chain into verify without an
inbox. Production deploys MUST set both ``WHATSAPP_API_TOKEN`` and
``WHATSAPP_PHONE_NUMBER_ID`` — health endpoint surfaces the mode.
"""

from __future__ import annotations

import hashlib
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import UserProfile
from app.redis_client import get_redis
from app.schemas import ErrorResponse, TokenResponse
from app.services import whatsapp_otp
from app.services.energy import init_energy, regen_energy
from app.services.session import get_brand_config
from app.services.token import (
    create_access_token,
    create_refresh_token,
    store_refresh_token,
    validate_refresh_token,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────


class RequestOtpBody(BaseModel):
    phone: str = Field(..., description="E.164-ish phone, e.g. +6591234567")
    brand_id: str
    locale: str | None = Field(
        default="en",
        description="One of en/zh/ms/id/th/vi (BCP-47 stripped to base).",
    )


class RequestOtpResponse(BaseModel):
    status: str
    mode: str
    phone: str
    expires_in: int
    rate_remaining: int
    debug_code: str | None = None
    debug_message: str | None = None


class VerifyOtpBody(BaseModel):
    phone: str
    code: str
    brand_id: str


class WhatsAppRefreshBody(BaseModel):
    refresh_token: str


# ── Helpers ───────────────────────────────────────────────────────────────


def _device_sig_for_phone(phone: str) -> str:
    """Stable, opaque device_sig derived from the normalised phone.

    Re-uses the existing ``UNIQUE(brand_id, device_sig)`` constraint on
    ``user_profiles`` so phone-based users can co-exist with the legacy
    device-sig users without a schema change. The original phone is
    NOT stored — only its SHA-256 prefix lands in the column.
    """
    return "wa:" + hashlib.sha256(phone.encode()).hexdigest()[:24]


async def _upsert_phone_user(
    db: AsyncSession, brand_id: str, phone: str
) -> tuple[UserProfile, bool]:
    """Look up or create a UserProfile for a phone-verified caller."""
    device_sig = _device_sig_for_phone(phone)
    stmt = select(UserProfile).where(
        UserProfile.brand_id == brand_id,
        UserProfile.device_sig == device_sig,
    )
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is not None:
        return user, False

    now = datetime.now(timezone.utc)
    user = UserProfile(
        user_id=uuid.uuid4(),
        brand_id=brand_id,
        device_sig=device_sig,
        display_name=f"Player_{random.randint(1000, 9999)}",
        day1_expires_at=now + timedelta(hours=24),
    )
    # Best-effort: set auth_method if the column exists (additive
    # migration in sibling commit). We tolerate the column missing so
    # the router can ship ahead of the migration.
    try:
        setattr(user, "auth_method", "whatsapp")
    except Exception:  # pragma: no cover — attribute may not exist yet
        pass
    db.add(user)
    await db.flush()
    return user, True


# ── POST /request-otp ─────────────────────────────────────────────────────


@router.post(
    "/request-otp",
    response_model=RequestOtpResponse,
    responses={
        404: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
)
async def request_otp(
    body: RequestOtpBody,
    r: aioredis.Redis = Depends(get_redis),
):
    """Generate + deliver a 6-digit OTP to the caller's WhatsApp."""
    # Validate brand exists so we can't spray OTPs for unknown brands.
    await get_brand_config(r, body.brand_id)

    try:
        result = await whatsapp_otp.send_otp(
            r, body.phone, body.locale, brand_id=body.brand_id
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        )
    return result


# ── POST /verify-otp ──────────────────────────────────────────────────────


@router.post(
    "/verify-otp",
    response_model=TokenResponse,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
async def verify_otp(
    body: VerifyOtpBody,
    db: AsyncSession = Depends(get_db),
    r: aioredis.Redis = Depends(get_redis),
):
    """Verify a code, link/create the user, return access+refresh JWT."""
    config = await get_brand_config(r, body.brand_id)

    try:
        verified = await whatsapp_otp.verify_otp(
            r, body.phone, body.code, brand_id=body.brand_id
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        )

    phone = verified["phone"]
    user, is_new = await _upsert_phone_user(db, body.brand_id, phone)
    user_id = str(user.user_id)
    device_sig = user.device_sig

    if is_new:
        await init_energy(r, body.brand_id, user_id)
        energy_balance = 100
    else:
        energy_balance, _ = await regen_energy(
            r, body.brand_id, user_id, config
        )

    now = datetime.now(timezone.utc)
    is_day1 = now < user.day1_expires_at

    access_token = create_access_token(
        user_id=user_id,
        brand_id=body.brand_id,
        device_sig=device_sig,
        is_day1=is_day1,
    )
    refresh = create_refresh_token()
    await store_refresh_token(r, refresh, user_id, body.brand_id, device_sig)
    user.last_seen_at = now

    # Audit — separate from the service-level audit so the JWT issuance
    # itself is traceable, mirroring auth.login in app/routers/auth.py.
    try:
        from app.services.audit_log_service import (
            record_event_fire_and_forget,
        )
        await record_event_fire_and_forget(
            actor_id=user_id,
            actor_type="customer",
            action="auth.whatsapp.login",
            target_type="user",
            target_id=user_id,
            brand_id=body.brand_id,
            result="success",
            payload={
                "is_new_user": bool(is_new),
                "is_day1": bool(is_day1),
                "auth_method": "whatsapp",
            },
        )
    except Exception as exc:
        logger.warning("audit_log (auth.whatsapp.login) skipped: %s", exc)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh,
        user_id=user_id,
        energy=energy_balance,
        is_day1=is_day1,
        day1_expires_at=(
            user.day1_expires_at.isoformat() if is_day1 else None
        ),
    )


# ── POST /refresh ─────────────────────────────────────────────────────────


@router.post(
    "/refresh",
    response_model=TokenResponse,
    responses={401: {"model": ErrorResponse}},
)
async def refresh(
    body: WhatsAppRefreshBody,
    db: AsyncSession = Depends(get_db),
    r: aioredis.Redis = Depends(get_redis),
):
    """Refresh a phone-auth JWT pair.

    Identical mechanic to ``/auth/token/refresh`` — kept under the
    whatsapp/* prefix so the mobile client doesn't need branching logic.
    """
    data = await validate_refresh_token(r, body.refresh_token)
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    user_id = data["user_id"]
    brand_id = data["brand_id"]
    device_sig = data["device_sig"]

    stmt = select(UserProfile).where(UserProfile.user_id == uuid.UUID(user_id))
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    now = datetime.now(timezone.utc)
    is_day1 = now < user.day1_expires_at

    access_token = create_access_token(
        user_id=user_id,
        brand_id=brand_id,
        device_sig=device_sig,
        is_day1=is_day1,
    )
    new_refresh = create_refresh_token()
    await store_refresh_token(r, new_refresh, user_id, brand_id, device_sig)

    balance_key = f"energy:balance:{brand_id}:{user_id}"
    raw_balance = await r.get(balance_key)
    energy_balance = int(raw_balance) if raw_balance is not None else 0

    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh,
        user_id=user_id,
        energy=energy_balance,
        is_day1=is_day1,
        day1_expires_at=user.day1_expires_at.isoformat() if is_day1 else None,
    )


# ── GET /health ───────────────────────────────────────────────────────────


@router.get("/health")
async def health(r: aioredis.Redis = Depends(get_redis)):
    return await whatsapp_otp.health_check(r)
