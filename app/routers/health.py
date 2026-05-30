"""Health router — liveness, readiness, and pool/metrics endpoints."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import (
    get_read_db,
    has_read_replica,
    pool_stats,
    read_engine,
    write_engine,
)
from app.redis_client import get_redis
from app.region import CURRENT_REGION, get_region_config
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


# ── PostgreSQL health + pool inspection ────────────────────────────────────


async def _probe_engine(label: str, eng) -> dict[str, Any]:
    """Run ``SELECT 1`` against an engine and time it."""
    started = time.perf_counter()
    try:
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {
            "label": label,
            "ok": True,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    except Exception as exc:  # pragma: no cover — diagnostics path
        return {
            "label": label,
            "ok": False,
            "error": str(exc),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }


async def _replica_lag_seconds(db: AsyncSession) -> float | None:
    """Return replica lag in seconds, or ``None`` if not on a replica.

    Uses Postgres' standard recovery functions. On the primary,
    ``pg_is_in_recovery()`` returns false and we report ``None``.
    """
    try:
        # Single round-trip — returns (in_recovery, lag_seconds_or_null).
        row = (
            await db.execute(
                text(
                    "SELECT pg_is_in_recovery() AS in_recovery, "
                    "EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp())) "
                    "AS lag_seconds"
                )
            )
        ).first()
        if row is None or not row.in_recovery:
            return None
        return float(row.lag_seconds) if row.lag_seconds is not None else 0.0
    except Exception:
        return None


@router.get("/api/v1/health/pg")
async def health_pg(db: AsyncSession = Depends(get_read_db)) -> JSONResponse:
    """PostgreSQL liveness + pool stats + replica lag.

    Returns ``200`` when both engines respond to ``SELECT 1``; ``503``
    otherwise.  ``replica_lag_seconds`` is ``null`` when the read
    endpoint is not actually on a replica.
    """
    write_probe = await _probe_engine("write", write_engine)
    if has_read_replica():
        read_probe = await _probe_engine("read", read_engine)
    else:
        read_probe = {
            "label": "read",
            "ok": write_probe["ok"],
            "latency_ms": write_probe["latency_ms"],
            "aliased_to": "write",
        }

    lag = await _replica_lag_seconds(db)

    body = {
        "status": "ok" if write_probe["ok"] and read_probe["ok"] else "degraded",
        "engines": {"write": write_probe, "read": read_probe},
        "pool": pool_stats(),
        "replica_configured": has_read_replica(),
        "replica_lag_seconds": lag,
    }
    code = 200 if body["status"] == "ok" else 503
    return JSONResponse(status_code=code, content=body)


# ── Prometheus-style metrics endpoint ─────────────────────────────────────


def _render_pool_metrics() -> str:
    """Render pool stats in Prometheus text-exposition format."""
    stats = pool_stats()
    lines: list[str] = []

    def emit(metric: str, help_text: str, value: Any, labels: dict[str, str]) -> None:
        if value is None:
            return
        label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
        lines.append(f"# HELP {metric} {help_text}")
        lines.append(f"# TYPE {metric} gauge")
        lines.append(f"{metric}{{{label_str}}} {value}")

    for engine_label, snap in (("write", stats["write"]), ("read", stats["read"])):
        if snap is None:
            continue
        labels = {"engine": engine_label}
        emit(
            "kix_pg_pool_size",
            "Configured pool_size (steady-state connection target).",
            snap.get("size"),
            labels,
        )
        emit(
            "kix_pg_pool_checked_out",
            "Connections currently lent out (in use).",
            snap.get("checked_out"),
            labels,
        )
        emit(
            "kix_pg_pool_checked_in",
            "Idle connections sitting in the pool.",
            snap.get("checked_in"),
            labels,
        )
        emit(
            "kix_pg_pool_overflow",
            "Open overflow connections beyond pool_size (-1 = none).",
            snap.get("overflow"),
            labels,
        )
        emit(
            "kix_pg_pool_max_overflow",
            "Configured max_overflow ceiling.",
            snap.get("max_overflow"),
            labels,
        )
        emit(
            "kix_pg_pool_total_capacity",
            "pool_size + max_overflow.",
            snap.get("total_capacity"),
            labels,
        )
        # Approximate "waiting" — when checked_out >= size + max_overflow,
        # callers will block on pool_timeout. We surface a derived gauge
        # so dashboards can alert on saturation.
        size = snap.get("size") or 0
        checked_out = snap.get("checked_out") or 0
        max_overflow = snap.get("max_overflow") or 0
        capacity = size + max_overflow
        saturation = max(0, checked_out - capacity)
        emit(
            "kix_pg_pool_waiting_estimate",
            "Estimated callers blocked waiting for a connection.",
            saturation,
            labels,
        )

    return "\n".join(lines) + "\n"


@router.get("/api/v1/health/region")
async def region_health() -> dict[str, Any]:
    """Region-aware health probe.

    Returns the active region's compliance jurisdiction, primary currency,
    applicable laws, and supported payment methods. Used by GeoDNS health
    checks and on-call dashboards to verify each region pod is serving the
    correct configuration.
    """
    cfg = get_region_config()
    return {
        "region": CURRENT_REGION,
        "region_name": cfg.get("region_name"),
        "compliance_jurisdiction": cfg["compliance_jurisdiction"],
        "applicable_laws": cfg.get("applicable_laws", []),
        "primary_currency": cfg["primary_currency"],
        "supported_currencies": cfg.get("supported_currencies", []),
        "payment_methods": cfg.get("payment_methods", []),
        "languages": cfg.get("languages", []),
        "data_residency_required": cfg.get("data_residency_required", False),
        "timestamp": int(time.time()),
    }


@router.get("/metrics")
async def metrics() -> PlainTextResponse:
    """Prometheus-style metrics endpoint (pool gauges only for now)."""
    return PlainTextResponse(
        content=_render_pool_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# ── Stripe live readiness ────────────────────────────────────────────────


@router.get("/api/v1/health/stripe-mode")
async def stripe_mode_health(
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Surface Stripe mode + day-91 charge cron health to ops.

    Critical for production: if mode='mock' in prod, NO real money flows.
    Day-91 cron count proves the auto-renewal loop is actually firing.
    """
    try:
        from app.services.stripe_live import get_mode
        mode = get_mode()
    except Exception as exc:
        mode = f"error:{exc}"

    # Last 24h day-91 cron stats from Redis
    try:
        renewals = int(await r.get("billing_cron:day91:renewed_count_24h") or 0)
        failures = int(await r.get("billing_cron:day91:failed_count_24h") or 0)
        last_run_ts = await r.get("billing_cron:day91:last_run_ts")
        last_run_ts = float(last_run_ts) if last_run_ts else None
    except Exception:
        renewals = failures = 0
        last_run_ts = None

    # Production warnings
    warnings = []
    is_production_region = CURRENT_REGION not in ("dev", "local", "test", None)
    if is_production_region and mode == "mock":
        warnings.append(
            "CRITICAL: production region but Stripe in mock mode — "
            "no real charges will flow. Set STRIPE_API_KEY=sk_live_*."
        )
    if last_run_ts is None:
        warnings.append("day-91 cron has never run (or counter missing)")
    elif last_run_ts and (time.time() - last_run_ts) > 86400 * 2:
        warnings.append(
            f"day-91 cron last ran {(time.time() - last_run_ts) / 3600:.1f}h ago "
            f"(>48h — stale)"
        )

    return {
        "mode": mode,  # live / test / mock
        "production_region": is_production_region,
        "day91_renewals_24h": renewals,
        "day91_failures_24h": failures,
        "last_run_ts": last_run_ts,
        "warnings": warnings,
        "is_ready_for_real_money": (mode != "mock" and not warnings),
    }
