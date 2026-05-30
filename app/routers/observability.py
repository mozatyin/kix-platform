"""Observability router — production telemetry for ML + viral effects.

Endpoints
---------
* ``GET  /api/v1/observability/ml/{model_name}/metrics`` — 7d ML metrics
* ``GET  /api/v1/observability/viral/{brand_id}/kfactor`` — current K-factor
* ``GET  /api/v1/observability/anomalies`` — recent anomalies
* ``GET  /api/v1/observability/dashboard-data`` — aggregate dashboard payload
* ``POST /api/v1/observability/ml/{model_name}/track`` — manual prediction
  feedback (e.g. attribution worker attaching ``actual`` to a stored
  prediction)
* ``POST /api/v1/observability/viral/{brand_id}/event`` — manual viral
  event ingestion (additive, used by tests and one-off backfills)
* ``GET  /api/v1/health/observability`` — infra health probe

All endpoints are READ-MOSTLY and brand-scoped where applicable —
tenancy is enforced by the path parameter being baked into the Redis
key template inside :mod:`app.services.ml_observability`.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import ml_observability as obs

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────


class TrackPredictionIn(BaseModel):
    features: dict[str, Any] = Field(default_factory=dict)
    prediction: float
    actual: bool | float | None = None
    audit_event_id: str | None = None


class AttachActualIn(BaseModel):
    prediction_id: str
    actual: bool | float


class ViralEventIn(BaseModel):
    event_type: str
    user_id: str
    inviter_id: str | None = None


# ── ML metrics endpoint ───────────────────────────────────────────────────


@router.get("/api/v1/observability/ml/{model_name}/metrics")
async def ml_metrics(
    model_name: str,
    days: int = Query(7, ge=1, le=90),
) -> dict[str, Any]:
    """Trailing-N-day ML metrics for one named model.

    Returns accuracy, precision, recall, AUC, calibration MAE, drift
    score (against the prior equal-length window), and a sample-size
    breakdown.
    """
    now = time.time()
    return await obs.compute_ml_metrics(model_name, now - days * 86400, now)


@router.post("/api/v1/observability/ml/{model_name}/track")
async def track_prediction(
    model_name: str, body: TrackPredictionIn
) -> dict[str, str]:
    """Manual prediction-tracking surface (additive).

    The hot path (auction.py / push_engine) calls
    :func:`app.services.ml_observability.track_ml_prediction` directly;
    this endpoint exists so attribution workers and integration tests
    can record predictions without importing the service module.
    """
    pred_id = await obs.track_ml_prediction(
        model_name=model_name,
        features=body.features,
        prediction=body.prediction,
        actual=body.actual,
        audit_event_id=body.audit_event_id,
    )
    return {"prediction_id": pred_id}


@router.post("/api/v1/observability/ml/{model_name}/attach-actual")
async def attach_actual(model_name: str, body: AttachActualIn) -> dict[str, Any]:
    """Late-binding feedback — caller supplies the auction outcome once
    the bid is settled."""
    ok = await obs.attach_actual_outcome(
        model_name, body.prediction_id, body.actual
    )
    if not ok:
        raise HTTPException(404, "prediction_not_found")
    return {"updated": True}


@router.get("/api/v1/observability/ml/{model_name}/audit/{prediction_id}")
async def reconstruct_chain(
    model_name: str, prediction_id: str
) -> dict[str, Any]:
    """Reconstruct the decision chain for one prediction — returns the
    raw record plus the linked ``audit_event_id`` (caller resolves the
    audit row from PG)."""
    chain = await obs.reconstruct_decision_chain(model_name, prediction_id)
    if chain is None:
        raise HTTPException(404, "prediction_not_found")
    return chain


# ── Viral / K-factor endpoints ────────────────────────────────────────────


@router.get("/api/v1/observability/viral/{brand_id}/kfactor")
async def viral_kfactor(
    brand_id: str,
    window_days: int = Query(7, ge=1, le=90),
) -> dict[str, Any]:
    """Real-time K-factor for one brand.

    Returns the requested window plus 1d/7d/30d trailing values and an
    explosion flag (per the existing inheritance-depth-cap fix).
    """
    k_now = await obs.compute_kfactor_realtime(brand_id, window_days=window_days)
    trailing = await obs.kfactor_trailing(brand_id)
    return {
        "brand_id": brand_id,
        "kfactor": k_now,
        "window_days": window_days,
        "trailing": trailing,
        "explosion_warning": k_now > obs.KFACTOR_EXPLOSION_THRESHOLD,
        "below_productive_floor": k_now < obs.KFACTOR_PRODUCTIVE_FLOOR,
    }


@router.post("/api/v1/observability/viral/{brand_id}/event")
async def viral_event(brand_id: str, body: ViralEventIn) -> dict[str, Any]:
    """Additive viral-event ingestion (separate from
    :mod:`app.routers.network_effect` to keep that hot path untouched)."""
    await obs.track_viral_event(
        brand_id=brand_id,
        event_type=body.event_type,
        user_id=body.user_id,
        inviter_id=body.inviter_id,
    )
    return {"recorded": True}


# ── Anomaly & dashboard endpoints ─────────────────────────────────────────


@router.get("/api/v1/observability/anomalies")
async def anomalies(
    model_name: str = Query("_global"),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """List anomalies recorded against the trailing baseline (within
    the last 30 days)."""
    out = await obs.recent_anomalies(model_name=model_name, limit=limit)
    return {"model_name": model_name, "anomalies": out, "count": len(out)}


@router.get("/api/v1/observability/dashboard-data")
async def dashboard_data(
    brand_id: list[str] | None = Query(None),
    model_name: list[str] | None = Query(None),
) -> dict[str, Any]:
    """One-shot aggregate the ops dashboard polls every 30 s."""
    return await obs.dashboard_snapshot(
        brand_ids=brand_id, model_names=model_name
    )


# ── Infra health probe ────────────────────────────────────────────────────


@router.get("/api/v1/health/observability")
async def observability_health() -> dict[str, Any]:
    """Tiny probe for the observability infra itself.

    Reports Redis liveness and a bounded key-count snapshot so the ops
    dashboard can detect telemetry outages even when the rest of the
    platform is happy.
    """
    return await obs.observability_health()
