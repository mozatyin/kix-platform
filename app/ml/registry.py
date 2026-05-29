"""Model registry + versioning + A/B switch.

The registry tracks one entry per ``(model_name, version)`` pair on a
Redis hash, plus a per-model "active version" pointer. Inference looks
up the active version and loads the corresponding ``.lgb`` file from
disk; ``activate()`` flips the pointer atomically so an operator can
A/B between an old and new model without restarting the API.

Redis layout
============

::

    ml:model:{name}                HASH  metadata for the active version
        active_version             str   pointer to a registry entry
        previous_version           str   last activated version (for rollback)

    ml:registry:{name}             HASH  version_id → JSON metadata blob
    ml:registry:{name}:order       LIST  version_ids in insertion order

All paths under :func:`models_dir` are absolute and OS-agnostic. The
default lives under ``$KIX_ML_MODELS_DIR`` (or ``app/ml/_artifacts``).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from app.ml.models import (
    HAS_LGB,
    ModelMetadata,
    ModelNotAvailable,
    _BoosterModel,
    get_model_class,
)

logger = logging.getLogger(__name__)


# ── Path helpers ─────────────────────────────────────────────────────


def models_dir() -> Path:
    """Where ``.lgb`` artifacts live on disk."""
    base = os.environ.get("KIX_ML_MODELS_DIR")
    if base:
        return Path(base)
    # Default: a sibling _artifacts/ folder inside the package.
    return Path(__file__).parent / "_artifacts"


def _artifact_path(name: str, version: str) -> Path:
    return models_dir() / f"{name}__{version}.lgb"


# ── Redis keys ───────────────────────────────────────────────────────


def _model_key(name: str) -> str:
    return f"ml:model:{name}"


def _registry_key(name: str) -> str:
    return f"ml:registry:{name}"


def _registry_order_key(name: str) -> str:
    return f"ml:registry:{name}:order"


# ── Registration / activation ────────────────────────────────────────


async def register_model(
    r: Any,
    name: str,
    path: str | Path,
    metrics: dict[str, float] | None = None,
    version: str | None = None,
    hyperparams: dict[str, Any] | None = None,
    train_samples: int = 0,
    val_samples: int = 0,
    activate: bool = True,
) -> str:
    """Record a freshly trained artifact in the registry.

    Returns the assigned ``version`` (auto-generated as a unix
    timestamp if not provided).
    """
    if version is None:
        version = str(int(time.time()))

    entry = {
        "name": name,
        "version": version,
        "path": str(path),
        "metrics": metrics or {},
        "registered_at": int(time.time()),
        "hyperparams": hyperparams or {},
        "train_samples": train_samples,
        "val_samples": val_samples,
    }
    if r is not None:
        await r.hset(_registry_key(name), version, json.dumps(entry))
        await r.rpush(_registry_order_key(name), version)
        if activate:
            await activate_model(r, name, version)
    return version


async def activate_model(r: Any, name: str, version: str) -> None:
    """Flip the active pointer for ``name`` to ``version``."""
    current = await r.hget(_model_key(name), "active_version")
    if current and current != version:
        await r.hset(_model_key(name), "previous_version", current)
    await r.hset(_model_key(name), "active_version", version)
    await r.hset(_model_key(name), "activated_at", int(time.time()))


async def get_active_version(r: Any, name: str) -> str | None:
    if r is None:
        return None
    return await r.hget(_model_key(name), "active_version")


async def list_versions(r: Any, name: str) -> list[dict[str, Any]]:
    """All known versions for a model, newest last."""
    if r is None:
        return []
    raw = await r.hgetall(_registry_key(name))
    return [json.loads(blob) for blob in raw.values()]


async def list_all_models(r: Any) -> list[dict[str, Any]]:
    """List every registered model + active version + last-known metrics."""
    if r is None:
        return []
    out: list[dict[str, Any]] = []
    # Scan all ml:model:* keys for active pointers.
    cursor = 0
    seen: set[str] = set()
    while True:
        cursor, keys = await r.scan(cursor=cursor, match="ml:model:*", count=100)
        for k in keys:
            name = k.split("ml:model:", 1)[-1]
            if name in seen:
                continue
            seen.add(name)
            active = await r.hget(_model_key(name), "active_version")
            previous = await r.hget(_model_key(name), "previous_version")
            metrics: dict[str, Any] = {}
            if active:
                blob = await r.hget(_registry_key(name), active)
                if blob:
                    metrics = json.loads(blob).get("metrics", {})
            out.append({
                "name": name,
                "active_version": active,
                "previous_version": previous,
                "metrics": metrics,
            })
        if cursor == 0:
            break
    return out


async def get_model_metrics(r: Any, name: str) -> dict[str, Any]:
    active = await get_active_version(r, name)
    if not active:
        return {"name": name, "active_version": None, "metrics": {}}
    blob = await r.hget(_registry_key(name), active)
    if not blob:
        return {"name": name, "active_version": active, "metrics": {}}
    return json.loads(blob)


# ── Loader (used by inference) ───────────────────────────────────────


async def load_latest_model(name: str, r: Any | None = None) -> _BoosterModel | None:
    """Materialize the booster for ``name`` from disk.

    Returns ``None`` (rather than raising) when the model isn't
    available — inference uses the heuristic fallback in that case.
    """
    if not HAS_LGB:
        return None
    try:
        version = await get_active_version(r, name) if r is not None else None
        path: Path
        if version:
            path = _artifact_path(name, version)
        else:
            # No registry entry — try a "default" artifact on disk so
            # someone can drop a model file in and start using it
            # without a Redis registration round-trip.
            path = _artifact_path(name, "default")
        if not path.exists():
            return None
        cls = get_model_class(name)
        return cls.load(path)
    except ModelNotAvailable:
        return None
    except Exception as exc:  # noqa: BLE001 — log and degrade
        logger.warning("registry: failed to load %s: %s", name, exc)
        return None


def artifact_path(name: str, version: str) -> Path:
    """Public alias for callers (the trainer) that need to know where
    to write a model file before registering it."""
    return _artifact_path(name, version)
