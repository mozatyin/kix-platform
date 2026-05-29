"""Feature extraction for the ML subsystem.

A single ordered ``FEATURES`` list is the source of truth for the
column order of every vector fed into LightGBM. Training and inference
**must** use this list — any reordering / addition becomes a model
version bump.

All extraction functions are best-effort: if a Redis key is missing or
a field can't be cast, the feature degrades to a neutral default (0
for counts, a small positive prior for rates) instead of raising. This
keeps the inference path latency-bounded and crash-free.
"""
from __future__ import annotations

import time
from typing import Any, Iterable, Mapping

# ── Canonical feature order (DO NOT REORDER) ─────────────────────────
# Each entry maps to one column in the LightGBM training matrix. Adding
# a feature here without retraining will silently corrupt predictions
# — bump the model version (registry.py) when this list changes.
FEATURES: list[str] = [
    # Campaign features ────────────────────────────────────────────────
    "campaign_age_days",
    "campaign_objective",          # categorical → hashed int
    "bid_strategy",                # categorical → hashed int
    "max_bid_cents",
    "daily_budget_cents",
    "total_spend_cents",
    # Performance history (30-day rolling) ─────────────────────────────
    "campaign_impressions_30d",
    "campaign_clicks_30d",
    "campaign_conversions_30d",
    "campaign_ctr_30d",
    "campaign_cvr_30d",
    "campaign_cpa_30d",
    # User features (when available) ───────────────────────────────────
    "user_age_bucket",
    "user_gender",                 # categorical → hashed int
    "user_country",                # categorical → hashed int
    "user_device",                 # categorical → hashed int
    "user_total_conversions_30d",
    "user_avg_session_seconds",
    # Context features ─────────────────────────────────────────────────
    "hour_of_day",
    "day_of_week",
    "time_since_last_impression_hours",
    "user_journey_length",
    "user_tier",
    # Brand features ───────────────────────────────────────────────────
    "brand_industry",              # categorical → hashed int
    "brand_creative_freshness_days",
    "brand_quality_score_avg",
]


# ── Categorical encoding ─────────────────────────────────────────────
# Tree models don't accept strings. We use a stable string-hash (Python
# ``hash()`` is salted per-process, so we roll our own) so the same
# category maps to the same integer in training and serving.

def _stable_hash(value: Any, buckets: int = 1024) -> int:
    """Deterministic small-int hash for categorical features."""
    if value is None:
        return 0
    s = str(value).strip().lower()
    if not s:
        return 0
    # FNV-1a 32-bit (good distribution, no external dep).
    h = 0x811c9dc5
    for ch in s.encode("utf-8"):
        h ^= ch
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h % buckets


def _to_float(value: Any, default: float = 0.0) -> float:
    """Best-effort cast — returns ``default`` on any failure."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


# ── Sync vector builder (used by trainer + inference) ────────────────

def features_to_vector(
    campaign: Mapping[str, Any] | None,
    user: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
) -> list[float]:
    """Project three dicts onto the canonical ``FEATURES`` order.

    This is the **sync** path — does no Redis I/O. The async
    :func:`extract_features` helper wraps this with Redis lookups for
    live inference. Trainers should bulk-fetch then call this directly.
    """
    c = campaign or {}
    u = user or {}
    ctx = context or {}

    now = float(ctx.get("now") or time.time())
    created = _to_float(c.get("created_at"), default=now)
    age_days = max(0.0, (now - created) / 86400.0)

    # Performance derivatives — compute lazily from raw counts when the
    # rolled-up rate isn't already on the campaign hash.
    imps = _to_int(c.get("impressions_30d") or c.get("impressions", 0))
    clicks = _to_int(c.get("clicks_30d") or c.get("clicks", 0))
    convs = _to_int(c.get("conversions_30d") or c.get("conversions", 0))
    spend = _to_int(c.get("total_spend_cents") or c.get("spend_cents", 0))

    ctr = _to_float(c.get("ctr_30d"))
    if ctr == 0.0 and imps > 0:
        ctr = clicks / imps
    cvr = _to_float(c.get("cvr_30d"))
    if cvr == 0.0 and clicks > 0:
        cvr = convs / clicks
    cpa = _to_float(c.get("cpa_30d"))
    if cpa == 0.0 and convs > 0:
        cpa = spend / convs

    vector = [
        age_days,                                                   # campaign_age_days
        float(_stable_hash(c.get("objective"))),                    # campaign_objective
        float(_stable_hash(c.get("bid_strategy") or c.get("bid_optimization"))),
        float(_to_int(c.get("max_bid_cents"))),
        float(_to_int(c.get("daily_budget_cents"))),
        float(spend),
        float(imps),
        float(clicks),
        float(convs),
        ctr,
        cvr,
        cpa,
        float(_to_int(u.get("age_bucket"))),
        float(_stable_hash(u.get("gender"))),
        float(_stable_hash(u.get("country"))),
        float(_stable_hash(u.get("device"))),
        float(_to_int(u.get("total_conversions_30d"))),
        _to_float(u.get("avg_session_seconds")),
        float(_to_int(ctx.get("hour_of_day"), default=time.localtime(now).tm_hour)),
        float(_to_int(ctx.get("day_of_week"), default=time.localtime(now).tm_wday)),
        _to_float(ctx.get("time_since_last_impression_hours"), default=24.0),
        float(_to_int(ctx.get("user_journey_length"))),
        float(_to_int(u.get("tier") or ctx.get("user_tier"))),
        float(_stable_hash(c.get("brand_industry") or c.get("industry"))),
        _to_float(c.get("brand_creative_freshness_days"), default=7.0),
        _to_float(c.get("brand_quality_score_avg"), default=0.5),
    ]

    # Defensive: a missing FEATURES entry above would mis-align the
    # matrix. Assert at build time so any drift surfaces in tests.
    assert len(vector) == len(FEATURES), (
        f"feature drift: {len(vector)} values vs {len(FEATURES)} columns"
    )
    return vector


# ── Async wrapper for online inference ───────────────────────────────

async def extract_features(
    campaign: Mapping[str, Any] | None,
    user: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
    r: Any = None,
) -> list[float]:
    """Async feature extraction — pulls supplementary Redis state.

    Most fields are expected to already be on the ``campaign`` /
    ``user`` dicts (the auction router has them hot in-process). Redis
    is only consulted when keys are missing — keeps the typical online
    path at zero extra round-trips.
    """
    c = dict(campaign or {})
    u = dict(user or {})
    ctx = dict(context or {})

    cid = c.get("campaign_id")
    if r is not None and cid:
        # Backfill 30-day rollups from Redis if not provided inline.
        try:
            if "impressions_30d" not in c and "impressions" not in c:
                stats = await r.hgetall(f"campaign:stats:{cid}")
                for k in ("impressions", "clicks", "conversions",
                          "revenue_cents", "spend_cents"):
                    if k in stats and k not in c:
                        c[k] = stats[k]
        except Exception:
            # Redis hiccup → fall through with whatever we have.
            pass

    return features_to_vector(c, u, ctx)


def batch_to_matrix(rows: Iterable[Mapping[str, Any]]) -> list[list[float]]:
    """Convert an iterable of ``{campaign, user, context}`` dicts into
    a 2-D matrix for LightGBM training. Order-preserving.
    """
    return [
        features_to_vector(row.get("campaign"), row.get("user"), row.get("context"))
        for row in rows
    ]
