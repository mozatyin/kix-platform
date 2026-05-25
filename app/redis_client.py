"""Redis async client and Lua script loader for KiX Platform."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

from app.config import settings

# ── Module-level state ────────────────────────────────────────────────────
redis_pool: aioredis.Redis | None = None
lua_scripts: dict[str, Any] = {}

LUA_DIR = Path(__file__).resolve().parent.parent / "lua"


async def init_redis() -> None:
    """Create the Redis connection pool from settings."""
    global redis_pool
    redis_pool = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
    )


async def close_redis() -> None:
    """Gracefully close the Redis connection pool."""
    global redis_pool
    if redis_pool is not None:
        await redis_pool.aclose()
        redis_pool = None


async def get_redis() -> aioredis.Redis:
    """FastAPI dependency returning the Redis instance."""
    if redis_pool is None:
        raise RuntimeError("Redis pool not initialised — call init_redis() first")
    return redis_pool


async def load_lua_scripts(r: aioredis.Redis) -> None:
    """Read all .lua files from the lua/ directory and register them.

    Scripts are stored in the module-level ``lua_scripts`` dict keyed by
    filename without extension (e.g. ``energy_regen``).
    """
    if not LUA_DIR.is_dir():
        return

    for lua_file in sorted(LUA_DIR.glob("*.lua")):
        script_body = lua_file.read_text(encoding="utf-8")
        script_obj = r.register_script(script_body)
        lua_scripts[lua_file.stem] = script_obj
