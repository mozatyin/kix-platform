"""LightGBM model definitions.

Three model classes — one per prediction task — that share a common
training-config surface. Each class is a thin wrapper over a raw
``lgb.Booster`` so we can:

  * Override ``predict()`` to apply task-specific output post-processing
    (sigmoid clipping for quality / relevance, dollar bound for bid).
  * Carry training-time metadata (feature list, AUC, version) on the
    same object that gets pickled to disk.
  * Provide a uniform ``load()`` / ``save()`` API for the registry.

LightGBM is an optional dep — importing this module without it should
not crash the API. We guard the import and fall back to a "null" model
class that always raises ``ModelNotAvailable`` when ``predict()`` is
called, so the inference layer can detect the condition and use the
heuristic path.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# ── Optional dep: lightgbm ───────────────────────────────────────────
try:
    import lightgbm as lgb  # type: ignore
    HAS_LGB = True
except Exception:  # pragma: no cover — exercised in dep-free CI
    lgb = None  # type: ignore
    HAS_LGB = False


class ModelNotAvailable(RuntimeError):
    """Raised when a prediction is requested but no booster is loaded."""


@dataclass
class ModelMetadata:
    """Serialised alongside the booster for the registry."""

    name: str
    version: str
    feature_names: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    trained_at: int = 0
    train_samples: int = 0
    val_samples: int = 0
    hyperparams: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True)

    @classmethod
    def from_json(cls, blob: str) -> "ModelMetadata":
        data = json.loads(blob)
        return cls(**data)


# ── Base wrapper ─────────────────────────────────────────────────────


class _BoosterModel:
    """Common LightGBM booster wrapper. Subclasses tune post-processing."""

    DEFAULT_PARAMS: dict[str, Any] = {
        "objective": "binary",
        "metric": "auc",
        "num_leaves": 31,
        "learning_rate": 0.05,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "min_data_in_leaf": 20,
        "verbose": -1,
    }

    def __init__(
        self,
        booster: Any | None = None,
        metadata: ModelMetadata | None = None,
    ) -> None:
        self.booster = booster
        self.metadata = metadata or ModelMetadata(name=self.name, version="0")

    # Subclasses override.
    name: str = "base"

    # ── Persistence ──────────────────────────────────────────────────

    def save(self, path: str | Path) -> None:
        if not self.booster:
            raise ModelNotAvailable("nothing to save")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.booster.save_model(str(path))
        # Sidecar metadata so the registry can load it without re-training.
        path.with_suffix(path.suffix + ".meta.json").write_text(
            self.metadata.to_json()
        )

    @classmethod
    def load(cls, path: str | Path) -> "_BoosterModel":
        if not HAS_LGB:
            raise ModelNotAvailable("lightgbm not installed")
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(str(path))
        booster = lgb.Booster(model_file=str(path))
        meta_path = path.with_suffix(path.suffix + ".meta.json")
        metadata: ModelMetadata
        if meta_path.exists():
            metadata = ModelMetadata.from_json(meta_path.read_text())
        else:
            metadata = ModelMetadata(name=cls.name, version="unknown")
        return cls(booster=booster, metadata=metadata)

    # ── Inference ────────────────────────────────────────────────────

    def predict(self, features: Sequence[Sequence[float]]) -> list[float]:
        if not self.booster:
            raise ModelNotAvailable(f"{self.name} booster not loaded")
        raw = self.booster.predict(features)
        return self._postprocess(list(raw))

    def _postprocess(self, raw: list[float]) -> list[float]:
        # Subclasses override. Default: clamp to [0, 1].
        return [max(0.0, min(1.0, float(x))) for x in raw]


# ── Concrete tasks ───────────────────────────────────────────────────


class QualityScoreModel(_BoosterModel):
    """Predict P(conversion | impression) — used as the auction multiplier.

    The output is treated as a quality score in [0, 1] and multiplied
    into ``bid × QS × pacing`` exactly like the heuristic value today.
    """

    name = "quality_score"


class RelevanceScoreModel(_BoosterModel):
    """Predict P(engagement | push) — used by the push engine.

    Replaces the hand-tuned ``0.4 cat + 0.3 geo + 0.15 time + 0.15
    freshness`` sum. Same [0, 1] range, same downstream filter.
    """

    name = "relevance_score"


class SmartBidModel(_BoosterModel):
    """Predict the optimal bid (in cents) for a (campaign, user, context).

    Regression task — the output is a non-negative integer cent value,
    clipped to ``max_bid_cents`` at inference time so the model can
    never overspend a campaign's headroom.
    """

    name = "smart_bid"

    DEFAULT_PARAMS: dict[str, Any] = {
        **_BoosterModel.DEFAULT_PARAMS,
        "objective": "regression",
        "metric": "rmse",
    }

    def _postprocess(self, raw: list[float]) -> list[float]:
        # Bid is in cents — clip to non-negative, no upper bound here
        # (the auction layer enforces ``max_bid_cents`` per campaign).
        return [max(0.0, float(x)) for x in raw]


# ── Registry of trainable tasks ──────────────────────────────────────

MODEL_CLASSES: dict[str, type[_BoosterModel]] = {
    QualityScoreModel.name: QualityScoreModel,
    RelevanceScoreModel.name: RelevanceScoreModel,
    SmartBidModel.name: SmartBidModel,
}


def get_model_class(name: str) -> type[_BoosterModel]:
    cls = MODEL_CLASSES.get(name)
    if not cls:
        raise KeyError(f"unknown ML task: {name!r}")
    return cls
