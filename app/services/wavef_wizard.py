"""Self-serve campaign wizard — Wave F spec #14.

A four-step draft state machine that composes existing KiX modules:
``mechanic`` (spec #13) → ``assets`` → ``reward`` → ``review/publish``.

The wizard *itself* is a thin Redis-backed draft store. Publish
validates the draft against the mechanic's JSONSchema (spec #13) and
returns a synthesised ``campaign_id`` ready for the real campaign
service to pick up.

Redis schema
------------
::

    wizard:draft:{did}           HASH {brand_id, step, mechanic_id,
                                       assets_json, reward_json,
                                       created_at_ms, updated_at_ms,
                                       published, campaign_id}
    wizard:user:{uid}:drafts     ZSET score=updated_at_ms, member=did

NEW file.
"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from app.services import wavef_mechanics as mech_svc


STEPS = ("mechanic", "assets", "reward", "review")


def _k_draft(did: str) -> str:
    return f"wizard:draft:{did}"


def _k_user(uid: str) -> str:
    return f"wizard:user:{uid}:drafts"


def _ts() -> int:
    return int(time.time() * 1000)


async def create_draft(r, *, uid: str, brand_id: str) -> dict:
    did = uuid4().hex[:12]
    now = _ts()
    payload = {
        "draft_id": did,
        "uid": uid,
        "brand_id": brand_id,
        "step": "mechanic",
        "mechanic_id": "",
        "assets_json": "{}",
        "reward_json": "{}",
        "created_at_ms": str(now),
        "updated_at_ms": str(now),
        "published": "0",
        "campaign_id": "",
    }
    await r.hset(_k_draft(did), mapping=payload)
    await r.zadd(_k_user(uid), {did: now})
    return _to_dict(payload)


def _to_dict(raw: dict) -> dict:
    norm: dict[str, Any] = {}
    for k, v in (raw or {}).items():
        k = k.decode() if isinstance(k, bytes) else k
        v = v.decode() if isinstance(v, bytes) else v
        norm[k] = v
    try:
        assets = json.loads(norm.get("assets_json", "{}"))
    except (json.JSONDecodeError, TypeError):
        assets = {}
    try:
        reward = json.loads(norm.get("reward_json", "{}"))
    except (json.JSONDecodeError, TypeError):
        reward = {}
    return {
        "draft_id": norm.get("draft_id", ""),
        "uid": norm.get("uid", ""),
        "brand_id": norm.get("brand_id", ""),
        "step": norm.get("step", "mechanic"),
        "mechanic_id": norm.get("mechanic_id", ""),
        "assets": assets,
        "reward": reward,
        "created_at_ms": int(norm.get("created_at_ms", "0") or 0),
        "updated_at_ms": int(norm.get("updated_at_ms", "0") or 0),
        "published": norm.get("published", "0") == "1",
        "campaign_id": norm.get("campaign_id", ""),
    }


async def get_draft(r, did: str) -> dict | None:
    raw = await r.hgetall(_k_draft(did))
    if not raw:
        return None
    return _to_dict(raw)


async def patch_draft(
    r,
    did: str,
    *,
    step: str | None = None,
    mechanic_id: str | None = None,
    assets: dict | None = None,
    reward: dict | None = None,
) -> dict:
    """Apply a step patch. Each field is independently optional."""
    cur = await get_draft(r, did)
    if cur is None:
        raise ValueError("draft not found")
    if cur["published"]:
        raise ValueError("draft already published — cannot patch")
    updates: dict[str, str] = {}
    if step is not None:
        if step not in STEPS:
            raise ValueError(f"unknown step: {step}")
        updates["step"] = step
    if mechanic_id is not None:
        if mech_svc.get_mechanic(mechanic_id) is None:
            raise ValueError(f"unknown mechanic: {mechanic_id}")
        updates["mechanic_id"] = mechanic_id
    if assets is not None:
        if not isinstance(assets, dict):
            raise ValueError("assets must be a JSON object")
        updates["assets_json"] = json.dumps(assets)
    if reward is not None:
        if not isinstance(reward, dict):
            raise ValueError("reward must be a JSON object")
        updates["reward_json"] = json.dumps(reward)
    now = _ts()
    updates["updated_at_ms"] = str(now)
    await r.hset(_k_draft(did), mapping=updates)
    await r.zadd(_k_user(cur["uid"]), {did: now})
    return await get_draft(r, did)


def _check(schema: dict, value: Any, *, path: str = "") -> list[str]:
    """Minimal JSONSchema subset validator (object/array/primitives)."""
    errors: list[str] = []
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            errors.append(f"{path or 'value'}: expected object")
            return errors
        for k in schema.get("required") or []:
            if k not in value:
                errors.append(f"{path or 'value'}: missing required '{k}'")
        for prop, sub in (schema.get("properties") or {}).items():
            if prop in value:
                errors.extend(_check(sub, value[prop], path=f"{path}.{prop}"))
    elif t == "array":
        if not isinstance(value, list):
            errors.append(f"{path}: expected array")
            return errors
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: too few items ({len(value)} < {schema['minItems']})")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: too many items ({len(value)} > {schema['maxItems']})")
    elif t == "string":
        if not isinstance(value, str):
            errors.append(f"{path}: expected string")
            return errors
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: string too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: string too long")
    elif t == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"{path}: expected integer")
            return errors
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum")
    elif t == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            errors.append(f"{path}: expected number")
            return errors
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum")
    elif t == "boolean":
        if not isinstance(value, bool):
            errors.append(f"{path}: expected boolean")
    return errors


def validate_draft(draft: dict) -> list[str]:
    """Return a list of validation errors. Empty == ready to publish."""
    errors: list[str] = []
    if not draft.get("mechanic_id"):
        errors.append("mechanic_id is required")
        return errors
    if not draft.get("assets"):
        errors.append("assets are required")
    if not draft.get("reward"):
        errors.append("reward is required")
    schema = mech_svc.get_schema(draft["mechanic_id"])
    if schema is None:
        errors.append("unknown mechanic_id")
        return errors
    config = {**(draft.get("assets") or {}), **(draft.get("reward") or {})}
    # Allow either flat or nested; we just check the mechanic schema
    # against the merged config keys present.
    errors.extend(_check(schema, config, path="config"))
    return errors


async def publish(r, did: str) -> dict:
    draft = await get_draft(r, did)
    if draft is None:
        raise ValueError("draft not found")
    if draft["published"]:
        raise PermissionError("already published")
    errors = validate_draft(draft)
    if errors:
        raise ValueError("validation failed: " + "; ".join(errors))
    campaign_id = "cmp_" + uuid4().hex[:18]
    await r.hset(
        _k_draft(did),
        mapping={
            "published": "1",
            "campaign_id": campaign_id,
            "updated_at_ms": str(_ts()),
        },
    )
    return {"published": True, "campaign_id": campaign_id, "draft_id": did}


async def list_drafts(r, uid: str) -> list[dict]:
    members = await r.zrevrange(_k_user(uid), 0, 49)
    out: list[dict] = []
    for did in members or []:
        did = did.decode() if isinstance(did, bytes) else did
        d = await get_draft(r, did)
        if d is not None:
            out.append(d)
    return out
