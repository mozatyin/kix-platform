"""JWT and refresh-token service for KiX Platform R5."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
import redis.asyncio as aioredis

from app.config import settings

# ── Constants ────────────────────────────────────────────────────────────
_REFRESH_TTL = 2_592_000  # 30 days in seconds


def create_access_token(
    user_id: str,
    brand_id: str,
    device_sig: str,
    session_id: str | None = None,
    is_day1: bool = False,
) -> str:
    """Create a short-lived HS256 JWT (15 min).

    Payload: sub, brand_id, device_sig (first 16 chars of SHA-256),
    session_id, is_day1, iat, exp.
    """
    now = datetime.now(timezone.utc)
    device_hash = hashlib.sha256(device_sig.encode()).hexdigest()[:16]
    payload = {
        "sub": user_id,
        "brand_id": brand_id,
        "device_sig": device_hash,
        "session_id": session_id,
        "is_day1": is_day1,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT. Raises on expiry or invalid signature."""
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as exc:
        raise ValueError(f"Invalid token: {exc}") from exc


def create_refresh_token() -> str:
    """Generate an opaque random refresh token (URL-safe, 32 bytes)."""
    return secrets.token_urlsafe(32)


async def store_refresh_token(
    r: aioredis.Redis,
    token: str,
    user_id: str,
    brand_id: str,
    device_sig: str,
) -> None:
    """Store refresh token as a Redis HASH with 30-day TTL."""
    key = f"refresh_token:{token}"
    await r.hset(key, mapping={
        "user_id": user_id,
        "brand_id": brand_id,
        "device_sig": device_sig,
    })
    await r.expire(key, _REFRESH_TTL)


async def validate_refresh_token(
    r: aioredis.Redis,
    token: str,
) -> dict | None:
    """Validate and rotate a refresh token.

    Returns the stored data dict if valid, else None.
    On success the old token is deleted (rotation — Rule 3).
    """
    key = f"refresh_token:{token}"
    data = await r.hgetall(key)
    if not data:
        return None
    # Rotation: delete old token immediately
    await r.delete(key)
    return data
