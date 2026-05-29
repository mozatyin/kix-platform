"""Online inference — low-latency model cache + heuristic fallback.

The cache holds at most one booster per model name in memory and
refreshes from the registry once per ``RELOAD_INTERVAL`` (default 1h).
Refreshes are best-effort: if the registry / disk is unreachable we
keep using the stale model rather than dropping back to the heuristic
mid-flight.

Every public ``predict_*`` coroutine is **never-raise**:

  * On any internal error the heuristic fallback is used and a warning
    is logged. The auction / push hot path stays alive even if the ML
    subsystem is misconfigured.

  * When ``KIX_ML_ENABLED`` is false (the default), the predictors
    return the heuristic value directly without consulting the cache —
    zero extra latency.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Mapping

from app.ml import is_enabled
from app.ml.features import extract_features
from app.ml.models import ModelNotAvailable, _BoosterModel
from app.ml.registry import load_latest_model

logger = logging.getLogger(__name__)


# ── In-memory cache ──────────────────────────────────────────────────


class ModelCache:
    """Tiny TTL cache for loaded boosters.

    Stored value can be ``None`` to memoize "no model trained yet" and
    avoid repeated disk hits.
    """

    RELOAD_INTERVAL = 3600  # seconds

    def __init__(self) -> None:
        self._models: dict[str, _BoosterModel | None] = {}
        self._load_ts: dict[str, float] = {}

    async def get(
        self, name: str, r: Any | None = None,
    ) -> _BoosterModel | None:
        now = time.time()
        last = self._load_ts.get(name, 0.0)
        if name in self._models and now - last < self.RELOAD_INTERVAL:
            return self._models[name]

        try:
            model = await load_latest_model(name, r=r)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ModelCache: load %s failed: %s", name, exc)
            model = self._models.get(name)  # keep stale on error
        self._models[name] = model
        self._load_ts[name] = now
        return model

    def invalidate(self, name: str | None = None) -> None:
        """Force a reload on the next ``get()``."""
        if name is None:
            self._load_ts.clear()
        else:
            self._load_ts.pop(name, None)


# Module-level singleton — one cache per process.
CACHE = ModelCache()


# ── Heuristic fallbacks ──────────────────────────────────────────────
# These mirror the formulas currently embedded in auction.py / push_engine.py
# so the platform is bit-identical when ML is disabled.


def _fallback_quality_score(campaign: Mapping[str, Any]) -> float:
    """``0.3 + min(ctr×8, 0.4) + min(cvr×6, 0.3)`` — the legacy heuristic."""
    try:
        ctr = float(campaign.get("ctr_30d") or campaign.get("ctr") or 0.01)
    except (TypeError, ValueError):
        ctr = 0.01
    try:
        cvr = float(campaign.get("cvr_30d") or campaign.get("cvr") or 0.01)
    except (TypeError, ValueError):
        cvr = 0.01
    return max(0.0, min(1.0, 0.3 + min(ctr * 8, 0.4) + min(cvr * 6, 0.3)))


def _fallback_relevance_score(
    campaign: Mapping[str, Any],
    user: Mapping[str, Any],
    context: Mapping[str, Any],
) -> float:
    """Compact version of ``push_engine.relevance_score`` — degrades to 0.5
    when no features are present so the call site still has a workable
    multiplier."""
    score = 0.0
    contributed = False
    brand_cats = campaign.get("categories") or []
    user_cats = user.get("favorite_categories") or []
    if brand_cats and user_cats:
        overlap = set(map(str, brand_cats)) & set(map(str, user_cats))
        if overlap:
            score += 0.40 * (len(overlap) / max(len(brand_cats), 1))
        contributed = True
    if "time_of_day" in (context or {}) and user.get("active_hours"):
        try:
            if int(context["time_of_day"]) in [int(h) for h in user["active_hours"]]:
                score += 0.15
            contributed = True
        except (TypeError, ValueError):
            pass
    if not contributed:
        return 0.5
    return max(0.0, min(1.0, score + 0.30))  # +0.30 = geo/freshness prior


def _fallback_smart_bid(
    campaign: Mapping[str, Any],
    stats: Mapping[str, Any] | None = None,
) -> int:
    """``target_cpa × CVR``, capped by ``max_bid_cents`` — the legacy formula."""
    try:
        target_cpa = int(campaign.get("target_cpa_cents", 0))
    except (TypeError, ValueError):
        target_cpa = 0
    try:
        max_bid = int(campaign.get("max_bid_cents", 0))
    except (TypeError, ValueError):
        max_bid = 0
    s = stats or {}
    try:
        clicks = int(s.get("clicks", 0))
        imps = int(s.get("impressions", 0))
    except (TypeError, ValueError):
        clicks, imps = 0, 0
    cvr = 0.01 if imps < 50 else max(0.001, min(1.0, clicks / max(imps, 1)))
    if target_cpa <= 0:
        return max_bid
    optimal = int(target_cpa * cvr)
    return min(optimal, max_bid) if max_bid > 0 else optimal


# ── Public predictors ────────────────────────────────────────────────


async def predict_quality_score(
    campaign: Mapping[str, Any],
    user: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    r: Any | None = None,
) -> float:
    """Returns a quality score in [0, 1].

    ML path → fallback to heuristic on any failure. The fallback
    matches the formula already wired into ``auction.py`` so toggling
    the feature flag is bit-safe.
    """
    if not is_enabled():
        return _fallback_quality_score(campaign)
    try:
        model = await CACHE.get("quality_score", r=r)
        if not model:
            return _fallback_quality_score(campaign)
        features = await extract_features(campaign, user, context, r)
        score = model.predict([features])[0]
        return float(min(max(score, 0.0), 1.0))
    except ModelNotAvailable:
        return _fallback_quality_score(campaign)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ML quality_score inference failed: %s", exc)
        return _fallback_quality_score(campaign)


async def predict_relevance_score(
    campaign: Mapping[str, Any],
    user: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    r: Any | None = None,
) -> float:
    """Returns a relevance score in [0, 1] for a push candidate."""
    if not is_enabled():
        return _fallback_relevance_score(campaign, user or {}, context or {})
    try:
        model = await CACHE.get("relevance_score", r=r)
        if not model:
            return _fallback_relevance_score(campaign, user or {}, context or {})
        features = await extract_features(campaign, user, context, r)
        score = model.predict([features])[0]
        return float(min(max(score, 0.0), 1.0))
    except ModelNotAvailable:
        return _fallback_relevance_score(campaign, user or {}, context or {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("ML relevance_score inference failed: %s", exc)
        return _fallback_relevance_score(campaign, user or {}, context or {})


async def predict_smart_bid(
    campaign: Mapping[str, Any],
    user: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    stats: Mapping[str, Any] | None = None,
    r: Any | None = None,
) -> int:
    """Returns an integer bid in cents, capped to ``max_bid_cents``."""
    if not is_enabled():
        return _fallback_smart_bid(campaign, stats)
    try:
        model = await CACHE.get("smart_bid", r=r)
        if not model:
            return _fallback_smart_bid(campaign, stats)
        features = await extract_features(campaign, user, context, r)
        raw = float(model.predict([features])[0])
        # ML output is value-per-impression in cents. Cap by max_bid.
        try:
            max_bid = int(campaign.get("max_bid_cents", 0))
        except (TypeError, ValueError):
            max_bid = 0
        bid = max(0, int(raw))
        if max_bid > 0:
            bid = min(bid, max_bid)
        return bid
    except ModelNotAvailable:
        return _fallback_smart_bid(campaign, stats)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ML smart_bid inference failed: %s", exc)
        return _fallback_smart_bid(campaign, stats)
