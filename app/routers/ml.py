"""ML admin endpoints — train / list / activate / metrics.

All endpoints require an ``admin_token`` that matches the
``KIX_ADMIN_TOKEN`` env var (constant-time comparison via
:mod:`app.security`). Training is async — POST returns a ``job_id``
immediately and the booster lands in the registry minutes later.

Endpoints
---------

  ``POST /api/v1/ml/train/{model_name}``
      Body: ``{admin_token, train_period_days?, hyperparams?}``
      → ``{job_id, model_name, status}``

  ``GET  /api/v1/ml/jobs/{job_id}``
      → job status (running / done / failed) + result

  ``GET  /api/v1/ml/models``
      → list every registered model + active version + last metrics.

  ``POST /api/v1/ml/models/{model_name}/activate``
      Body: ``{admin_token, version_id}`` — flips the active pointer.

  ``GET  /api/v1/ml/models/{model_name}/metrics``
      → AUC / precision / recall / drift score for the active version.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.ml import inference as ml_inference
from app.ml import is_enabled
from app.ml import registry, trainer
from app.redis_client import get_redis
from app.security import check_admin_token

router = APIRouter()


# ── Auth ─────────────────────────────────────────────────────────────


class _AdminBody(BaseModel):
    admin_token: str = Field(..., description="Matches KIX_ADMIN_TOKEN.")


def _require_admin(token: str) -> None:
    if not check_admin_token(token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "forbidden", "message": "admin token required"},
        )


# ── Bodies ───────────────────────────────────────────────────────────


class TrainBody(_AdminBody):
    train_period_days: int = Field(default=30, ge=1, le=365)
    hyperparams: dict[str, Any] | None = None


class ActivateBody(_AdminBody):
    version_id: str = Field(..., description="Registered version to activate.")


# ── Endpoints ────────────────────────────────────────────────────────


_VALID_MODELS = {"quality_score", "relevance_score", "smart_bid"}


@router.post("/train/{model_name}")
async def train_model(model_name: str, body: TrainBody) -> dict[str, Any]:
    _require_admin(body.admin_token)
    if model_name not in _VALID_MODELS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "unknown_model",
                    "message": f"valid: {sorted(_VALID_MODELS)}"},
        )
    r = await get_redis()
    try:
        job_id = trainer.launch_training_job(
            r,
            model_name=model_name,
            train_period_days=body.train_period_days,
            hyperparams=body.hyperparams,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "unknown_model", "message": str(exc)},
        ) from None
    return {"job_id": job_id, "model_name": model_name, "status": "running"}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = trainer.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "job_not_found", "message": job_id},
        )
    return job


@router.get("/jobs")
async def list_jobs() -> dict[str, Any]:
    jobs = trainer.list_jobs()
    return {
        "items": jobs,
        "count": len(jobs),
        "total": len(jobs),
        "has_more": False,
        "limit": len(jobs),
        "offset": 0,
    }


@router.get("/models")
async def list_models() -> dict[str, Any]:
    r = await get_redis()
    items = await registry.list_all_models(r)
    return {
        "items": items,
        "count": len(items),
        "total": len(items),
        "has_more": False,
        "limit": len(items),
        "offset": 0,
        "ml_enabled": is_enabled(),
    }


@router.post("/models/{model_name}/activate")
async def activate(model_name: str, body: ActivateBody) -> dict[str, Any]:
    _require_admin(body.admin_token)
    r = await get_redis()
    versions = await registry.list_versions(r, model_name)
    known = {v.get("version") for v in versions}
    if body.version_id not in known:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "version_not_found",
                    "message": f"versions: {sorted(known)}"},
        )
    await registry.activate_model(r, model_name, body.version_id)
    # Bust the in-process cache so the next inference picks the new artifact.
    ml_inference.CACHE.invalidate(model_name)
    return {
        "model_name": model_name,
        "active_version": body.version_id,
        "status": "activated",
    }


@router.get("/models/{model_name}/metrics")
async def model_metrics(model_name: str) -> dict[str, Any]:
    r = await get_redis()
    return await registry.get_model_metrics(r, model_name)


@router.get("/health")
async def ml_health() -> dict[str, Any]:
    """Cheap status probe — useful for ops dashboards."""
    return {
        "ml_enabled": is_enabled(),
        "cache_loaded": list(ml_inference.CACHE._models.keys()),
    }
