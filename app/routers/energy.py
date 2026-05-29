"""Energy router — QR grants and balance queries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models import EnergyTransaction
from app.redis_client import get_redis
from app.schemas import (
    EnergyGrantRequest,
    EnergyGrantResponse,
    ErrorResponse,
)
from app.services.energy import grant_energy
from app.services.session import get_brand_config

logger = logging.getLogger(__name__)

router = APIRouter()

# ── QR token validation constants ────────────────────────────────────────
_QR_GRACE_PERIOD = 30  # seconds


def _verify_qr_token(qr_token: str, expected_brand_id: str) -> dict:
    """Verify a QR token's HMAC-SHA256 signature and time window.

    Token format: base64url(payload_json).base64url(signature)

    Payload fields:
        b  — brand_id
        s  — start timestamp (unix)
        e  — end timestamp (unix)
        n  — nonce (idempotency key)

    Raises HTTPException on any validation failure.
    Returns the decoded payload dict.
    """
    parts = qr_token.split(".", 1)
    if len(parts) != 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed QR token",
        )

    payload_b64, sig_b64 = parts

    # Verify HMAC-SHA256 signature
    expected_sig = hmac.new(
        settings.qr_signing_secret.encode(),
        payload_b64.encode(),
        hashlib.sha256,
    ).digest()

    try:
        actual_sig = base64.urlsafe_b64decode(sig_b64 + "==")  # pad for safety
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid QR signature encoding",
        )

    if not hmac.compare_digest(expected_sig, actual_sig):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid QR signature",
        )

    # Decode payload
    try:
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + "==")
        payload = json.loads(payload_bytes)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid QR payload encoding",
        )

    # Time window check with grace period
    now = int(time.time())
    start_ts = payload.get("s", 0)
    end_ts = payload.get("e", 0)

    if now < start_ts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="QR token not yet valid",
        )
    if now > end_ts + _QR_GRACE_PERIOD:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="QR token expired",
        )

    # Brand check
    if payload.get("b") != expected_brand_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="QR token brand mismatch",
        )

    return payload


# ── POST /grant ──────────────────────────────────────────────────────────


@router.post(
    "/grant",
    response_model=EnergyGrantResponse,
    responses={
        400: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
    },
)
async def grant_qr_energy(
    body: EnergyGrantRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    r: aioredis.Redis = Depends(get_redis),
):
    """Grant energy from a QR code scan.

    Validates QR token (HMAC + time window), then calls the Lua grant
    script which handles idempotency and cooldown atomically.
    """
    user_id = user["sub"]
    brand_id = user["brand_id"]

    # 1. Verify brand_id matches request
    if body.brand_id != brand_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Brand mismatch between token and request",
        )

    # 2. Validate QR token (HMAC, time window, brand)
    payload = _verify_qr_token(body.qr_token, brand_id)
    nonce = payload["n"]

    # 3. Get brand config
    config = await get_brand_config(r, brand_id)

    # 4. Call grant_energy via Lua
    try:
        new_balance, actual_granted = await grant_energy(
            r, brand_id, user_id, nonce, config
        )
    except Exception as exc:
        err = str(exc)
        if "ALREADY_GRANTED" in err:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This QR code has already been used",
            )
        if "COOLDOWN_ACTIVE" in err:
            # Extract remaining seconds from error message
            remaining = 0
            if ":" in err:
                try:
                    remaining = int(err.split(":")[-1])
                except ValueError:
                    pass
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Cooldown active, try again in {remaining}s",
                headers={"Retry-After": str(remaining)},
            )
        raise

    # 5. Log EnergyTransaction to PG (async, non-blocking — fire and forget)
    try:
        tx = EnergyTransaction(
            brand_id=brand_id,
            user_id=user_id,
            operation="qr_grant",
            amount=actual_granted,
            balance_after=new_balance,
            metadata_json={"nonce": nonce, "qr_brand": payload.get("b")},
        )
        db.add(tx)
        # Commit happens via get_db dependency on response
    except Exception:
        logger.warning(
            "Failed to log energy transaction for user=%s nonce=%s",
            user_id, nonce,
        )

    # 6. Calculate next_grant_available_at (now + cooldown, default 4h)
    cooldown_ttl = config.get("qr_cooldown_ttl", 14400)
    next_available = datetime.now(timezone.utc) + timedelta(seconds=cooldown_ttl)

    # 7. Dashboard daily counters (best-effort, never block response).
    #    These feed /api/v1/dashboards/{brand_id}/today — see dashboards.py.
    try:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        scans_key = f"brand:{brand_id}:qr_scans:{day}"
        users_key = f"brand:{brand_id}:scanning_users:{day}"
        await r.sadd(scans_key, f"{user_id}:{nonce}")
        await r.expire(scans_key, 60 * 60 * 24 * 35)
        await r.sadd(users_key, user_id)
        await r.expire(users_key, 60 * 60 * 24 * 35)
        # Streak: mark today as an active day for this brand.
        await r.sadd(f"brand:{brand_id}:active_days", day)
    except Exception:  # pragma: no cover
        logger.warning("dashboard counters failed for brand=%s", brand_id)

    return EnergyGrantResponse(
        energy_granted=actual_granted,
        energy_balance=new_balance,
        next_grant_available_at=next_available.isoformat(),
    )
