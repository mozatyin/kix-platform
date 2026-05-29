"""KiX Platform — Machine-Learning subsystem.

Three production models live here:

  1. **Quality Score predictor** — replaces the heuristic
     ``CTR×8 + CVR×6`` used to rank auctions.
  2. **Relevance Score** — refines the linear push relevance
     (cat / geo / time / freshness).
  3. **Smart Bid optimizer** — replaces the linear
     ``target_cpa × CVR`` bid resolver.

All inference paths are **fallback-safe**: if the model is not loaded,
hasn't been trained yet, or LightGBM is not installed, the predictor
falls back to the same heuristic the platform ships today. The feature
flag ``KIX_ML_ENABLED`` (default false) governs whether the auction /
push routers ask the ML subsystem at all.

Layout::

    app/ml/__init__.py          — this file (public surface)
    app/ml/features.py          — feature extraction from Redis state
    app/ml/models.py            — LightGBM model definitions
    app/ml/trainer.py           — offline training pipeline
    app/ml/inference.py         — online inference cache + fallbacks
    app/ml/registry.py          — model versioning + A/B switch
    app/ml/data.py              — training data pipeline (event replay)
"""
from __future__ import annotations

import os

# ── Feature flag ─────────────────────────────────────────────────────
# Default false: the heuristic continues to drive every call site until
# a model has been trained and explicitly activated by an operator.
ML_ENABLED: bool = os.environ.get("KIX_ML_ENABLED", "false").lower() in (
    "1", "true", "yes", "on",
)


def is_enabled() -> bool:
    """Runtime check (re-reads env so tests can flip the flag)."""
    return os.environ.get(
        "KIX_ML_ENABLED", "true" if ML_ENABLED else "false",
    ).lower() in ("1", "true", "yes", "on")


# Public re-exports — keep the import surface small so call sites stay
# stable even if internals are refactored.
from app.ml.inference import (  # noqa: E402
    predict_quality_score,
    predict_relevance_score,
    predict_smart_bid,
    ModelCache,
)
from app.ml.features import FEATURES, extract_features  # noqa: E402

__all__ = [
    "ML_ENABLED",
    "is_enabled",
    "FEATURES",
    "extract_features",
    "predict_quality_score",
    "predict_relevance_score",
    "predict_smart_bid",
    "ModelCache",
]
