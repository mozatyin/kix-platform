"""Offline training pipeline — one entry point per ML task.

Each ``train_*_model`` coroutine:

  1. Pulls a labeled training set from the Redis event streams
     (see :mod:`app.ml.data`).
  2. Splits 80/20 into train + validation.
  3. Fits a LightGBM booster (binary objective for QS / Relevance,
     regression for SmartBid).
  4. Writes the artifact to ``app/ml/_artifacts/{name}__{version}.lgb``
     and registers it via :mod:`app.ml.registry`.
  5. Returns the new version id + validation metrics.

LightGBM and scikit-learn are optional deps — when missing, the
trainer raises ``RuntimeError("ml deps not installed")`` so the
operator gets a clear error instead of a cryptic ``ImportError``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.ml.data import build_training_set, feature_names
from app.ml.models import (
    HAS_LGB,
    ModelMetadata,
    QualityScoreModel,
    RelevanceScoreModel,
    SmartBidModel,
    _BoosterModel,
)
from app.ml.registry import artifact_path, register_model

logger = logging.getLogger(__name__)

# Optional deps -------------------------------------------------------
try:
    import lightgbm as lgb  # type: ignore
    import numpy as np  # type: ignore
    from sklearn.model_selection import train_test_split  # type: ignore
    from sklearn.metrics import (  # type: ignore
        roc_auc_score, mean_squared_error,
    )
    HAS_DEPS = HAS_LGB
except Exception:  # pragma: no cover
    lgb = None  # type: ignore
    np = None  # type: ignore
    train_test_split = None  # type: ignore
    roc_auc_score = None  # type: ignore
    mean_squared_error = None  # type: ignore
    HAS_DEPS = False


MIN_TRAINING_ROWS = 100  # below this we refuse to train (overfit risk).


def _require_deps() -> None:
    if not HAS_DEPS:
        raise RuntimeError(
            "ml deps not installed — `pip install lightgbm scikit-learn numpy`"
        )


# ── Generic trainer ──────────────────────────────────────────────────


async def _train_one(
    r: Any,
    *,
    model_cls: type[_BoosterModel],
    label: str,
    train_period_days: int,
    hyperparams: dict[str, Any] | None,
    num_boost_round: int = 200,
) -> dict[str, Any]:
    _require_deps()

    X, y = await build_training_set(
        r, period_days=train_period_days, label=label,
    )
    if len(X) < MIN_TRAINING_ROWS:
        raise RuntimeError(
            f"insufficient training data: {len(X)} rows "
            f"(need ≥ {MIN_TRAINING_ROWS}). Run the platform for longer "
            f"or seed events first."
        )

    # ── Train / val split ────────────────────────────────────────────
    X_np = np.array(X, dtype=np.float64)
    y_np = np.array(y, dtype=np.float64)

    stratify = y_np if label != "revenue" and len(set(y_np.tolist())) > 1 else None
    X_train, X_val, y_train, y_val = train_test_split(
        X_np, y_np, test_size=0.2, random_state=42, stratify=stratify,
    )

    # ── Hyperparams (caller override merges over the defaults) ───────
    params = dict(model_cls.DEFAULT_PARAMS)
    if hyperparams:
        params.update(hyperparams)

    train_set = lgb.Dataset(X_train, y_train, feature_name=feature_names())
    val_set = lgb.Dataset(X_val, y_val, reference=train_set,
                          feature_name=feature_names())

    booster = lgb.train(
        params,
        train_set,
        num_boost_round=num_boost_round,
        valid_sets=[val_set],
        callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=False)],
    )

    # ── Eval ─────────────────────────────────────────────────────────
    preds = booster.predict(X_val)
    metrics: dict[str, float] = {}
    if params.get("objective") == "binary":
        try:
            metrics["auc"] = float(roc_auc_score(y_val, preds))
        except ValueError:
            # single-class val split — degrade gracefully.
            metrics["auc"] = 0.5
        # Precision / recall @ 0.5.
        bin_preds = (preds >= 0.5).astype(int)
        tp = int(((bin_preds == 1) & (y_val == 1)).sum())
        fp = int(((bin_preds == 1) & (y_val == 0)).sum())
        fn = int(((bin_preds == 0) & (y_val == 1)).sum())
        metrics["precision"] = tp / (tp + fp) if (tp + fp) else 0.0
        metrics["recall"] = tp / (tp + fn) if (tp + fn) else 0.0
    else:
        metrics["rmse"] = float(mean_squared_error(y_val, preds) ** 0.5)

    # ── Persist + register ───────────────────────────────────────────
    version = str(int(time.time()))
    out_path = artifact_path(model_cls.name, version)
    metadata = ModelMetadata(
        name=model_cls.name,
        version=version,
        feature_names=feature_names(),
        metrics=metrics,
        trained_at=int(time.time()),
        train_samples=len(X_train),
        val_samples=len(X_val),
        hyperparams=params,
    )
    model = model_cls(booster=booster, metadata=metadata)
    model.save(out_path)
    await register_model(
        r,
        name=model_cls.name,
        path=out_path,
        metrics=metrics,
        version=version,
        hyperparams=params,
        train_samples=len(X_train),
        val_samples=len(X_val),
        activate=True,
    )

    return {
        "name": model_cls.name,
        "version": version,
        "metrics": metrics,
        "train_samples": len(X_train),
        "val_samples": len(X_val),
        "path": str(out_path),
    }


# ── Per-task entry points ────────────────────────────────────────────


async def train_quality_score_model(
    r: Any,
    train_period_days: int = 30,
    hyperparams: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Train P(conversion | impression). Returns version + metrics."""
    return await _train_one(
        r,
        model_cls=QualityScoreModel,
        label="conversion",
        train_period_days=train_period_days,
        hyperparams=hyperparams,
    )


async def train_relevance_score_model(
    r: Any,
    train_period_days: int = 30,
    hyperparams: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Train P(click | push). Returns version + metrics."""
    return await _train_one(
        r,
        model_cls=RelevanceScoreModel,
        label="click",
        train_period_days=train_period_days,
        hyperparams=hyperparams,
    )


async def train_smart_bid_model(
    r: Any,
    train_period_days: int = 30,
    hyperparams: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Train per-impression value (cents). Returns version + metrics."""
    return await _train_one(
        r,
        model_cls=SmartBidModel,
        label="revenue",
        train_period_days=train_period_days,
        hyperparams=hyperparams,
    )


# ── Job registry (for async POST /train endpoints) ───────────────────

_JOBS: dict[str, dict[str, Any]] = {}


def _new_job(name: str) -> str:
    job_id = f"mljob_{name}_{int(time.time() * 1000)}"
    _JOBS[job_id] = {
        "job_id": job_id,
        "model_name": name,
        "status": "running",
        "started_at": int(time.time()),
        "result": None,
        "error": None,
    }
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    return _JOBS.get(job_id)


def list_jobs() -> list[dict[str, Any]]:
    return list(_JOBS.values())


async def _run_job(job_id: str, coro: Any) -> None:
    try:
        result = await coro
        _JOBS[job_id].update(status="done", result=result,
                             finished_at=int(time.time()))
    except Exception as exc:  # noqa: BLE001
        logger.exception("training job %s failed", job_id)
        _JOBS[job_id].update(status="failed", error=str(exc),
                             finished_at=int(time.time()))


def launch_training_job(
    r: Any,
    model_name: str,
    train_period_days: int = 30,
    hyperparams: dict[str, Any] | None = None,
) -> str:
    """Schedule a training job, return ``job_id``. Non-blocking."""
    name_map = {
        "quality_score": train_quality_score_model,
        "relevance_score": train_relevance_score_model,
        "smart_bid": train_smart_bid_model,
    }
    fn = name_map.get(model_name)
    if not fn:
        raise KeyError(f"unknown model: {model_name!r}")
    job_id = _new_job(model_name)
    coro = fn(r, train_period_days=train_period_days, hyperparams=hyperparams)
    asyncio.create_task(_run_job(job_id, coro))
    return job_id
