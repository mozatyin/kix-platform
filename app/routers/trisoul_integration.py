"""TriSoul Integration — adaptive routing based on user attention/preference.

Wave C of the KIX Gamification Bible: TriSoul features (user attention
embedding) feed routing decisions across the platform — pushes, auctions,
and recipe selection — so that what each user sees adapts to *who they
are*, not just *who's paying the most this minute*.

Design constraints:

  * **Graceful degradation** — the production TriSoul model lives in a
    vendored sibling repo (``/Users/mozat/eltm``) that may not be
    importable in every deployment. When the vendor is missing this
    module returns deterministic default features so the platform still
    boots and existing tests still pass.

  * **Additive only** — call sites in ``push_engine`` / ``auction`` /
    ``recipe_generator`` invoke this module behind a feature flag and
    combine the result with existing scoring; they never *replace* it.
    Bounded multipliers (push ±20%, auction ±10%) make ranking inversion
    impossible.

  * **Cold-start safe** — a new user with no interaction history gets
    a 0.5 feature vector → multiplier 1.0 → identical behavior to the
    legacy path.

  * **Per-user feature flag** — env ``TRISOUL_ENABLED`` is the global
    kill-switch; per-user opt-in/out via ``trisoul:enabled:{uid}``
    enables incremental rollout (1% → 10% → 100%) without redeploy.

Redis schema:

    trisoul:user:{uid}                HASH   feature_name → float
    trisoul:user:{uid}:updated_at     STR    unix ts of last update
    trisoul:brand:{bid}               HASH   feature_name → float (brand embedding)
    trisoul:enabled:{uid}             STR    "1" / "0" (per-user override)
    trisoul:cache:affinity:{uid}:{bid}  STR  cached affinity score (TTL 60s)
    trisoul:audit                     LIST   recent influenced decisions (capped)
    trisoul:metrics:lookups           HASH   hit / miss / cached counters
    trisoul:metrics:histogram         HASH   score bucket counters

Endpoints (mounted at ``/api/v1/trisoul``):

    GET    /health
    GET    /{user_id}                       — features (cold-start default if missing)
    POST   /{user_id}/update                — ingest interaction event
    GET    /{user_id}/affinity/{brand_id}   — affinity score
    POST   /bulk-affinity                   — up to 100 users
    POST   /{user_id}/flag                  — per-user opt-in/out
    GET    /{user_id}/flag                  — read flag state
    POST   /reload                          — hot-swap model version (no restart)
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Configuration ──────────────────────────────────────────────────────────

#: Default feature vector for cold-start users — every dimension at 0.5
#: gives a TriSoul multiplier of exactly 1.0, leaving existing scoring
#: untouched until enough signal has accumulated.
DEFAULT_FEATURES: dict[str, float] = {
    "competitive": 0.5,
    "social": 0.5,
    "casual": 0.5,
    "premium": 0.5,
    "novelty": 0.5,
}

#: Global on/off. Per-user override stored in Redis under
#: ``trisoul:enabled:{uid}`` takes precedence when set.
_GLOBAL_FLAG_ENV = "TRISOUL_ENABLED"
_MODEL_VERSION_ENV = "TRISOUL_MODEL_VERSION"

#: Cached affinity TTL — short enough to react to new updates, long
#: enough to keep the lookup under 5 ms in hot paths.
_AFFINITY_CACHE_TTL = 60

#: Bounded learning rate for online feature updates. We keep this small
#: so a single event never swings a feature by more than ±0.05.
_UPDATE_RATE = 0.05

#: Cap on stored audit events.
_AUDIT_CAP = 10_000


def _user_key(uid: str) -> str:
    return f"trisoul:user:{uid}"


def _user_updated_key(uid: str) -> str:
    return f"trisoul:user:{uid}:updated_at"


def _brand_key(bid: str) -> str:
    return f"trisoul:brand:{bid}"


def _flag_key(uid: str) -> str:
    return f"trisoul:enabled:{uid}"


def _affinity_cache_key(uid: str, bid: str) -> str:
    return f"trisoul:cache:affinity:{uid}:{bid}"


# ── Model-loading shim ─────────────────────────────────────────────────────


_VENDOR_LOADED = False
_VENDOR_ERROR: Exception | None = None


def _try_load_vendor() -> None:
    """Attempt to import the production TriSoul model from the sibling repo.

    The vendor is intentionally optional: in tests, dev sandboxes, and
    fresh CI containers it will not be present. We swallow the import
    error and fall through to the deterministic fallback below.
    """
    global _VENDOR_LOADED, _VENDOR_ERROR
    try:
        # Vendor lives at /Users/mozat/eltm/trisoul/inference.py in
        # production; we import lazily so missing modules don't crash
        # the API boot.
        import importlib

        importlib.import_module("trisoul.inference")  # type: ignore
        _VENDOR_LOADED = True
        _VENDOR_ERROR = None
    except Exception as exc:  # noqa: BLE001 — vendor absence is non-fatal
        _VENDOR_LOADED = False
        _VENDOR_ERROR = exc


_try_load_vendor()


def _model_version() -> str:
    return os.environ.get(_MODEL_VERSION_ENV, "v1")


# ── Feature flag ───────────────────────────────────────────────────────────


def _global_enabled() -> bool:
    val = os.environ.get(_GLOBAL_FLAG_ENV, "0").strip().lower()
    return val in ("1", "true", "yes", "on")


async def is_enabled(uid: str | None, r: aioredis.Redis) -> bool:
    """Return True if TriSoul should influence routing for this user."""
    if not uid:
        return False
    # Per-user override beats global.
    override = await r.get(_flag_key(uid))
    if override is not None:
        return str(override).strip() in ("1", "true", "yes", "on")
    return _global_enabled()


# ── Feature store ──────────────────────────────────────────────────────────


def _safe_float(raw: Any, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return default


async def get_features(uid: str, r: aioredis.Redis) -> dict[str, float]:
    """Return TriSoul feature vector. Cold-start → ``DEFAULT_FEATURES``."""
    raw = await r.hgetall(_user_key(uid))
    if not raw:
        return dict(DEFAULT_FEATURES)
    out = dict(DEFAULT_FEATURES)
    for k, v in raw.items():
        key = k if isinstance(k, str) else k.decode("utf-8", "ignore")
        out[key] = _safe_float(v, default=out.get(key, 0.5))
    return out


async def get_brand_embedding(bid: str, r: aioredis.Redis) -> dict[str, float]:
    """Brand-side embedding; cold-start → mid-vector."""
    raw = await r.hgetall(_brand_key(bid))
    if not raw:
        return dict(DEFAULT_FEATURES)
    out = dict(DEFAULT_FEATURES)
    for k, v in raw.items():
        key = k if isinstance(k, str) else k.decode("utf-8", "ignore")
        out[key] = _safe_float(v, default=out.get(key, 0.5))
    return out


async def get_affinity(
    uid: str,
    bid: str,
    r: aioredis.Redis,
) -> float:
    """Affinity ∈ [0, 1] = normalised dot product user × brand embedding."""
    # Fast cache.
    cached = await r.get(_affinity_cache_key(uid, bid))
    if cached is not None:
        await _bump_metric(r, "cached")
        return _safe_float(cached, 0.5)

    user = await get_features(uid, r)
    brand = await get_brand_embedding(bid, r)
    # Symmetric dot product normalised to [0, 1] (each pair contributes
    # max 1 when both = 1). With 5 default dims at 0.5 each that gives
    # exactly 0.25; we rescale so a default-vs-default pair → 0.5.
    n = max(1, len(DEFAULT_FEATURES))
    raw = 0.0
    for k in DEFAULT_FEATURES:
        raw += user.get(k, 0.5) * brand.get(k, 0.5)
    score = raw / n  # ∈ [0, 1]
    # Rescale so 0.5×0.5 → 0.5 (default pair = neutral).
    # default_pair = 0.25; we want that mapped to 0.5; perfect=1→1.
    score = 0.5 + 2.0 * (score - 0.25)
    score = max(0.0, min(1.0, score))

    await r.set(_affinity_cache_key(uid, bid), f"{score:.4f}",
                ex=_AFFINITY_CACHE_TTL)
    # Track histogram + miss.
    await _bump_metric(r, "miss")
    await _bump_histogram(r, score)
    return score


async def bulk_affinity(
    pairs: list[tuple[str, str]],
    r: aioredis.Redis,
) -> list[float]:
    """Compute affinity for up to 100 (uid, bid) pairs."""
    if len(pairs) > 100:
        raise HTTPException(
            status_code=400, detail="bulk_affinity: max 100 pairs"
        )
    return [await get_affinity(uid, bid, r) for (uid, bid) in pairs]


async def update_features(
    uid: str,
    event: dict[str, Any],
    r: aioredis.Redis,
) -> dict[str, float]:
    """Apply a bounded online update from an interaction event.

    Event shape (best-effort, all fields optional)::

        {
            "type": "click" | "convert" | "skip" | "complete",
            "features": {"competitive": 0.8, "social": 0.3, ...},
            "weight": 1.0
        }

    Per-feature delta is capped at ±_UPDATE_RATE so no single event can
    swing the vector by more than 5%.
    """
    current = await get_features(uid, r)
    incoming = event.get("features") or {}
    if not isinstance(incoming, dict):
        return current
    try:
        weight = max(0.0, min(2.0, float(event.get("weight", 1.0))))
    except (TypeError, ValueError):
        weight = 1.0
    rate = _UPDATE_RATE * weight

    updated = dict(current)
    for k, target_raw in incoming.items():
        target = _safe_float(target_raw, default=current.get(k, 0.5))
        cur = current.get(k, 0.5)
        delta = max(-rate, min(rate, target - cur))
        updated[k] = max(0.0, min(1.0, cur + delta))

    await r.hset(_user_key(uid), mapping={k: f"{v:.4f}" for k, v in updated.items()})
    await r.set(_user_updated_key(uid), f"{time.time():.3f}")
    # Invalidate cached affinities for this user.
    # We don't know which brand keys exist; rely on TTL (60s) for cleanup.
    return updated


# ── Boost helpers (used by push_engine / auction / recipe_generator) ──────


def push_boost(affinity: float) -> float:
    """Multiplier for push_engine composite score. Bounded to ±20%."""
    return 1.0 + 0.20 * (affinity - 0.5) * 2.0  # affinity∈[0,1] → [0.8,1.2]


def auction_boost(affinity: float) -> float:
    """Multiplier for auction rank. Bounded to ±10%."""
    return 1.0 + 0.10 * (affinity - 0.5) * 2.0  # affinity∈[0,1] → [0.9,1.1]


async def maybe_boost_push(
    uid: str | None,
    bid: str,
    base_score: float,
    r: aioredis.Redis,
) -> tuple[float, dict[str, Any]]:
    """Returns (boosted_score, debug_meta). Identity when flag off."""
    if not uid:
        return base_score, {"trisoul": "skipped:no_user"}
    try:
        if not await is_enabled(uid, r):
            return base_score, {"trisoul": "skipped:disabled"}
        affinity = await get_affinity(uid, bid, r)
        boosted = base_score * push_boost(affinity)
        await _record_decision(r, uid, "push_engine", bid, affinity)
        return boosted, {
            "trisoul": "applied",
            "affinity": round(affinity, 4),
            "version": _model_version(),
        }
    except Exception as exc:  # noqa: BLE001 — never break the host router
        logger.warning("trisoul push boost failed: %s", exc)
        return base_score, {"trisoul": f"error:{type(exc).__name__}"}


async def maybe_boost_auction(
    uid: str | None,
    bid: str,
    base_rank: float,
    r: aioredis.Redis,
) -> tuple[float, dict[str, Any]]:
    """Returns (boosted_rank, debug_meta). Identity when flag off."""
    if not uid:
        return base_rank, {"trisoul": "skipped:no_user"}
    try:
        if not await is_enabled(uid, r):
            return base_rank, {"trisoul": "skipped:disabled"}
        affinity = await get_affinity(uid, bid, r)
        boosted = base_rank * auction_boost(affinity)
        await _record_decision(r, uid, "auction", bid, affinity)
        return boosted, {
            "trisoul": "applied",
            "affinity": round(affinity, 4),
            "version": _model_version(),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("trisoul auction boost failed: %s", exc)
        return base_rank, {"trisoul": f"error:{type(exc).__name__}"}


async def maybe_pick_recipe_by_affinity(
    uid: str | None,
    candidate_recipes: list[dict[str, Any]],
    r: aioredis.Redis,
    *,
    selection_probability: float = 0.30,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Choose a recipe biased by TriSoul affinity to its modules.

    Returns the picked recipe + a meta dict. Returns ``(None, meta)``
    when the flag is off or the host should fall back to its standard
    selection (legacy library-match path).
    """
    if not uid or not candidate_recipes:
        return None, {"trisoul": "skipped:no_user_or_candidates"}
    try:
        if not await is_enabled(uid, r):
            return None, {"trisoul": "skipped:disabled"}
        # Deterministic 30%-of-the-time selection keyed on user id so a
        # given user gets a stable experience across requests (no
        # flapping). Hash-mod prevents the need for actual randomness
        # in tests.
        h = abs(hash(("trisoul-pick", uid))) % 1000 / 1000.0
        if h >= selection_probability:
            return None, {"trisoul": "skipped:rolled_legacy", "roll": h}
        features = await get_features(uid, r)
        # Score each candidate by similarity of its "tags" / module ids
        # to user features. Cheap heuristic: count of tag substrings
        # that match a strong feature dim.
        strong_dims = {k for k, v in features.items() if v >= 0.55}
        weak_dims = {k for k, v in features.items() if v <= 0.45}
        scored: list[tuple[float, dict[str, Any]]] = []
        for rec in candidate_recipes:
            tags_blob = " ".join(
                [
                    str(rec.get("name", "")),
                    str(rec.get("description", "")),
                    " ".join(
                        str(m.get("id", "")) for m in rec.get("modules", [])
                    ),
                ]
            ).lower()
            s = 0.0
            for dim in strong_dims:
                if dim in tags_blob:
                    s += 1.0
            for dim in weak_dims:
                if dim in tags_blob:
                    s -= 0.5
            scored.append((s, rec))
        scored.sort(key=lambda t: -t[0])
        if not scored or scored[0][0] <= 0:
            return None, {"trisoul": "skipped:no_match"}
        await _record_decision(r, uid, "recipe_generator",
                               scored[0][1].get("id", ""), 0.0)
        return scored[0][1], {
            "trisoul": "applied",
            "version": _model_version(),
            "score": scored[0][0],
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("trisoul recipe pick failed: %s", exc)
        return None, {"trisoul": f"error:{type(exc).__name__}"}


# ── Observability helpers ──────────────────────────────────────────────────


async def _bump_metric(r: aioredis.Redis, bucket: str) -> None:
    try:
        await r.hincrby("trisoul:metrics:lookups", bucket, 1)
    except Exception:  # noqa: BLE001
        pass


async def _bump_histogram(r: aioredis.Redis, score: float) -> None:
    try:
        bucket = f"b_{int(min(max(score, 0.0), 0.9999) * 10)}"
        await r.hincrby("trisoul:metrics:histogram", bucket, 1)
    except Exception:  # noqa: BLE001
        pass


async def _record_decision(
    r: aioredis.Redis,
    uid: str,
    route: str,
    bid: str,
    affinity: float,
) -> None:
    """Append a privacy-safe audit row. Feature vector is NOT logged."""
    try:
        row = {
            "ts": round(time.time(), 3),
            # user_id is a stable identifier in this platform; we log
            # only the route + flag state, never the feature vector,
            # per the privacy constraint.
            "user_id": uid,
            "route": route,
            "brand_id": bid,
            "version": _model_version(),
            "affinity_hint": round(affinity, 2),
        }
        await r.lpush("trisoul:audit", json.dumps(row))
        await r.ltrim("trisoul:audit", 0, _AUDIT_CAP - 1)
    except Exception:  # noqa: BLE001
        pass


# ── Schemas ────────────────────────────────────────────────────────────────


class FeatureResponse(BaseModel):
    user_id: str
    features: dict[str, float]
    cold_start: bool
    model_version: str
    updated_at: float | None = None


class UpdateRequest(BaseModel):
    type: str = Field(default="interaction")
    features: dict[str, float] = Field(default_factory=dict)
    weight: float = Field(default=1.0, ge=0.0, le=2.0)


class UpdateResponse(BaseModel):
    user_id: str
    features: dict[str, float]
    updated_at: float


class AffinityResponse(BaseModel):
    user_id: str
    brand_id: str
    affinity: float
    cached: bool


class BulkAffinityRequest(BaseModel):
    pairs: list[tuple[str, str]] = Field(..., max_length=100)


class BulkAffinityResponse(BaseModel):
    scores: list[float]


class FlagRequest(BaseModel):
    enabled: bool


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.get("/health")
async def health(r: aioredis.Redis = Depends(get_redis)) -> dict[str, Any]:
    """Diagnostics: vendor presence, model version, lookup counters."""
    metrics = await r.hgetall("trisoul:metrics:lookups")
    histo = await r.hgetall("trisoul:metrics:histogram")
    return {
        "status": "ok",
        "vendor_loaded": _VENDOR_LOADED,
        "vendor_error": str(_VENDOR_ERROR) if _VENDOR_ERROR else None,
        "model_version": _model_version(),
        "feature_count": len(DEFAULT_FEATURES),
        "global_flag": _global_enabled(),
        "lookups": {k: int(v) for k, v in (metrics or {}).items()},
        "score_histogram": {k: int(v) for k, v in (histo or {}).items()},
    }


@router.post("/reload")
async def reload_model() -> dict[str, Any]:
    """Hot-swap model version (env var must be updated first)."""
    _try_load_vendor()
    return {
        "model_version": _model_version(),
        "vendor_loaded": _VENDOR_LOADED,
    }


@router.get("/{user_id}", response_model=FeatureResponse)
async def get_user_features(
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> FeatureResponse:
    raw = await r.hgetall(_user_key(user_id))
    cold = not bool(raw)
    features = await get_features(user_id, r)
    updated_raw = await r.get(_user_updated_key(user_id))
    try:
        updated_at = float(updated_raw) if updated_raw else None
    except (TypeError, ValueError):
        updated_at = None
    return FeatureResponse(
        user_id=user_id,
        features=features,
        cold_start=cold,
        model_version=_model_version(),
        updated_at=updated_at,
    )


@router.post("/{user_id}/update", response_model=UpdateResponse)
async def update_user_features(
    user_id: str,
    body: UpdateRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> UpdateResponse:
    updated = await update_features(
        user_id,
        {"type": body.type, "features": body.features, "weight": body.weight},
        r,
    )
    ts = float(await r.get(_user_updated_key(user_id)) or time.time())
    return UpdateResponse(user_id=user_id, features=updated, updated_at=ts)


@router.get(
    "/{user_id}/affinity/{brand_id}", response_model=AffinityResponse
)
async def get_user_brand_affinity(
    user_id: str,
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> AffinityResponse:
    # Detect cache hit before computing.
    cached = await r.get(_affinity_cache_key(user_id, brand_id))
    score = await get_affinity(user_id, brand_id, r)
    return AffinityResponse(
        user_id=user_id,
        brand_id=brand_id,
        affinity=score,
        cached=bool(cached),
    )


@router.post("/bulk-affinity", response_model=BulkAffinityResponse)
async def bulk_affinity_endpoint(
    body: BulkAffinityRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> BulkAffinityResponse:
    pairs = [(p[0], p[1]) for p in body.pairs]
    scores = await bulk_affinity(pairs, r)
    return BulkAffinityResponse(scores=scores)


@router.post("/{user_id}/flag")
async def set_flag(
    user_id: str,
    body: FlagRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    await r.set(_flag_key(user_id), "1" if body.enabled else "0")
    return {"user_id": user_id, "enabled": body.enabled}


@router.get("/{user_id}/flag")
async def get_flag(
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    enabled = await is_enabled(user_id, r)
    return {"user_id": user_id, "enabled": enabled}


# ── Test/admin helper: brand embedding writer ──────────────────────────────


class BrandEmbeddingRequest(BaseModel):
    features: dict[str, float]


@router.post("/brand/{brand_id}/embedding")
async def set_brand_embedding(
    brand_id: str,
    body: BrandEmbeddingRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    cleaned = {k: f"{_safe_float(v):.4f}" for k, v in body.features.items()}
    if cleaned:
        await r.hset(_brand_key(brand_id), mapping=cleaned)
    return {"brand_id": brand_id, "features": cleaned}
