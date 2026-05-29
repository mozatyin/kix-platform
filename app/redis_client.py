"""Redis async client and Lua script loader for KiX Platform.

Supports three deployment modes selectable via ``REDIS_MODE``:

* ``single``    — single instance (default; existing behaviour)
* ``cluster``   — Redis Cluster (sharded)
* ``sentinel``  — HA single-master via Redis Sentinel

In cluster mode, multi-key commands (MSET / MGET / transactions) require
all keys to land in the same hash slot. Use the ``hash_tagged_key()``
helper below — it emits ``{tag}`` braces only when running on a cluster,
keeping single-mode keys unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

from app.config import settings

# ── Module-level state ────────────────────────────────────────────────────
# ``redis_pool`` is kept as the public name for backward compatibility with
# the many routers/tests that import it directly. In cluster mode it holds
# a ``RedisCluster`` instance; in single/sentinel mode it holds a ``Redis``.
redis_pool: Any | None = None
_redis_mode: str = "single"
lua_scripts: dict[str, Any] = {}

LUA_DIR = Path(__file__).resolve().parent.parent / "lua"


async def init_redis() -> None:
    """Create the Redis client according to ``REDIS_MODE``.

    Mode selection precedence:
      1. ``REDIS_MODE`` environment variable
      2. Default: ``"single"`` (preserves legacy behaviour)
    """
    global redis_pool, _redis_mode

    mode = os.environ.get("REDIS_MODE", "single").lower()
    _redis_mode = mode

    if mode == "cluster":
        from redis.asyncio.cluster import RedisCluster

        nodes_env = os.environ.get("REDIS_CLUSTER_NODES", "")
        if not nodes_env:
            raise RuntimeError(
                "REDIS_MODE=cluster requires REDIS_CLUSTER_NODES "
                "(comma-separated host:port list)"
            )
        startup_nodes = []
        for entry in nodes_env.split(","):
            entry = entry.strip()
            if not entry:
                continue
            host, _, port = entry.partition(":")
            startup_nodes.append({"host": host, "port": int(port or 6379)})

        redis_pool = RedisCluster(
            startup_nodes=startup_nodes,
            decode_responses=True,
            skip_full_coverage_check=True,
        )
    elif mode == "sentinel":
        from redis.asyncio.sentinel import Sentinel

        sentinels_env = os.environ.get("REDIS_SENTINELS", "")
        master_name = os.environ.get("REDIS_MASTER_NAME", "mymaster")
        if not sentinels_env:
            raise RuntimeError(
                "REDIS_MODE=sentinel requires REDIS_SENTINELS "
                "(comma-separated host:port list)"
            )
        sentinel_endpoints = []
        for entry in sentinels_env.split(","):
            entry = entry.strip()
            if not entry:
                continue
            host, _, port = entry.partition(":")
            sentinel_endpoints.append((host, int(port or 26379)))

        sentinel = Sentinel(sentinel_endpoints, decode_responses=True)
        redis_pool = sentinel.master_for(master_name)
    else:
        # Single instance (default). Honours both the legacy ``REDIS_URL``
        # env var (via ``settings.redis_url``) and an optional max_connections.
        max_conn = int(os.environ.get("REDIS_MAX_CONNECTIONS", "100"))
        redis_pool = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=max_conn,
        )


async def close_redis() -> None:
    """Gracefully close the Redis connection pool."""
    global redis_pool
    if redis_pool is not None:
        # Both ``Redis`` and ``RedisCluster`` expose ``aclose`` in
        # redis-py >= 5.x.
        close = getattr(redis_pool, "aclose", None) or getattr(
            redis_pool, "close", None
        )
        if close is not None:
            try:
                await close()
            except Exception:  # pragma: no cover — never fail shutdown
                pass
        redis_pool = None


async def get_redis() -> Any:
    """FastAPI dependency returning the active Redis client.

    Returns the underlying ``Redis`` or ``RedisCluster`` instance.
    Callers should treat the return as a duck-typed async Redis client —
    both implement the standard command surface.
    """
    if redis_pool is None:
        raise RuntimeError("Redis pool not initialised — call init_redis() first")
    return redis_pool


def get_redis_sync() -> Any:
    """Non-async accessor for middleware / hot paths that already have
    the client cached. Returns ``None`` if Redis is not yet initialised.
    """
    return redis_pool


def is_cluster_mode() -> bool:
    """Returns True iff the active client is a Redis Cluster."""
    return _redis_mode == "cluster"


def hash_tagged_key(template: str, tag: str) -> str:
    """Format ``template`` with a cluster-aware hash tag.

    Args:
        template: a format string containing the placeholder ``{tag}``.
            Example: ``"wallet:{tag}:balance"``.
        tag: the value to substitute. In cluster mode the value is wrapped
            in braces so the Redis Cluster slot hash uses only ``tag``,
            guaranteeing co-location of all keys that share the same tag.

    Returns:
        In single/sentinel mode: ``"wallet:brand_x:balance"``.
        In cluster mode:        ``"wallet:{brand_x}:balance"``.
    """
    if is_cluster_mode():
        return template.format(tag=f"{{{tag}}}")
    return template.format(tag=tag)


async def load_lua_scripts(r: aioredis.Redis) -> None:
    """Read all .lua files from the lua/ directory and register them.

    Scripts are stored in the module-level ``lua_scripts`` dict keyed by
    filename without extension (e.g. ``energy_regen``).

    NOTE: In cluster mode, ``register_script`` returns an object that
    requires all KEYS args to live in the same hash slot when invoked.
    Callers must hash-tag their keys accordingly.
    """
    if not LUA_DIR.is_dir():
        return

    register = getattr(r, "register_script", None)
    if register is None:  # pragma: no cover — cluster fallback
        return

    for lua_file in sorted(LUA_DIR.glob("*.lua")):
        script_body = lua_file.read_text(encoding="utf-8")
        script_obj = register(script_body)
        lua_scripts[lua_file.stem] = script_obj
