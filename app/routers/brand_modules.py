"""Brand modules router — gamification module enablement and configuration.

Storage model:
  Redis HASH at key  ``brand:{brand_id}:modules``
  field  = module_id (e.g. "share_to_win", "energy_invite")
  value  = JSON {id, enabled, params, updated_at}

Endpoints:
  GET    /brands/{brand_id}/modules                       — list all configured modules
  POST   /brands/{brand_id}/modules/{module_id}/toggle    — enable / disable
  POST   /brands/{brand_id}/modules/{module_id}/config    — save module params
  GET    /brands/{brand_id}/modules/{module_id}           — get single module
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(brand_id: str) -> str:
    return f"brand:{brand_id}:modules"


@router.get("/brands/{brand_id}/modules")
async def list_modules(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Return all configured modules for a brand (enabled or disabled)."""
    raw = await r.hgetall(_key(brand_id))
    out = []
    for module_id, payload in raw.items():
        try:
            out.append(json.loads(payload))
        except json.JSONDecodeError:
            logger.warning("brand_modules: bad json for %s/%s", brand_id, module_id)
            continue
    return out


@router.get("/brands/{brand_id}/modules/{module_id}")
async def get_module(
    brand_id: str,
    module_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await r.hget(_key(brand_id), module_id)
    if raw is None:
        return {"id": module_id, "enabled": False, "params": {}}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"id": module_id, "enabled": False, "params": {}}


@router.post("/brands/{brand_id}/modules/{module_id}/toggle")
async def toggle_module(
    brand_id: str,
    module_id: str,
    body: dict,
    r: aioredis.Redis = Depends(get_redis),
):
    """Enable / disable a module. Body: {"enabled": bool}."""
    existing_raw = await r.hget(_key(brand_id), module_id)
    if existing_raw:
        try:
            config = json.loads(existing_raw)
        except json.JSONDecodeError:
            config = {"id": module_id, "params": {}}
    else:
        config = {"id": module_id, "params": {}}

    config["id"] = module_id
    config["enabled"] = bool(body.get("enabled", True))
    config["updated_at"] = _now_iso()

    await r.hset(_key(brand_id), module_id, json.dumps(config))
    logger.info(
        "brand_modules: brand=%s module=%s enabled=%s",
        brand_id, module_id, config["enabled"],
    )
    return {"ok": True, "module": config}


@router.post("/brands/{brand_id}/modules/{module_id}/config")
async def config_module(
    brand_id: str,
    module_id: str,
    body: dict,
    r: aioredis.Redis = Depends(get_redis),
):
    """Persist module-specific params. Body = arbitrary JSON object."""
    existing_raw = await r.hget(_key(brand_id), module_id)
    if existing_raw:
        try:
            config = json.loads(existing_raw)
        except json.JSONDecodeError:
            config = {"id": module_id, "enabled": True}
    else:
        config = {"id": module_id, "enabled": True}

    config["id"] = module_id
    config["params"] = body or {}
    config["updated_at"] = _now_iso()

    await r.hset(_key(brand_id), module_id, json.dumps(config))
    logger.info(
        "brand_modules: brand=%s module=%s config saved (%d keys)",
        brand_id, module_id, len(config["params"]),
    )
    return {"ok": True, "module": config}
