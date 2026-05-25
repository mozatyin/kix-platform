"""Shared FastAPI dependencies for KiX Platform."""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.config import settings

bearer_scheme = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    """Decode and validate the JWT access token.

    Returns a dict with claims: sub, brand_id, device_sig, session_id,
    is_day1, exp.
    """
    token = credentials.credentials
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return {
        "sub": payload.get("sub"),
        "brand_id": payload.get("brand_id"),
        "device_sig": payload.get("device_sig"),
        "session_id": payload.get("session_id"),
        "is_day1": payload.get("is_day1"),
        "exp": payload.get("exp"),
    }
