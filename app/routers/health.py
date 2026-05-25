"""Health router — liveness and readiness checks."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.redis_client import get_redis
from app.schemas import HealthResponse, ReadyResponse, ReadyCheck

import redis.asyncio as aioredis

router = APIRouter()

# Capture module load time as startup time
_startup_time = time.time()


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Liveness probe — always returns 200 if the process is running."""
    return HealthResponse(
        status="ok",
        version="5.0.0",
        uptime_seconds=int(time.time() - _startup_time),
    )


@router.get("/ready", response_model=ReadyResponse)
async def readiness_check(
    r: aioredis.Redis = Depends(get_redis),
) -> JSONResponse:
    """Readiness probe — checks Redis connectivity and config availability."""
    # Check Redis
    try:
        await r.ping()
        redis_status = "ok"
    except Exception:
        return JSONResponse(
            status_code=503,
            content=ReadyResponse(
                status="not_ready",
                checks=ReadyCheck(
                    redis="error",
                    config_loaded=False,
                    brands_count=0,
                ),
            ).model_dump(),
        )

    # Check if any brand configs are loaded
    # Use SCAN to avoid blocking on large keyspaces
    brands_count = 0
    cursor = 0
    while True:
        cursor, keys = await r.scan(cursor=cursor, match="config:*", count=100)
        brands_count += len(keys)
        if cursor == 0:
            break

    config_loaded = brands_count > 0

    if not config_loaded:
        return JSONResponse(
            status_code=503,
            content=ReadyResponse(
                status="not_ready",
                checks=ReadyCheck(
                    redis=redis_status,
                    config_loaded=False,
                    brands_count=0,
                ),
            ).model_dump(),
        )

    return JSONResponse(
        status_code=200,
        content=ReadyResponse(
            status="ready",
            checks=ReadyCheck(
                redis=redis_status,
                config_loaded=True,
                brands_count=brands_count,
            ),
        ).model_dump(),
    )
