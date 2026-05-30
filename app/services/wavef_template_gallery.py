"""Campaign template gallery — Wave F obvious-win #8.

Inspired by BRAME's library of 10K+ proven campaigns. Marketers
clone a "ready to launch" archetype (mechanics + reward + T&Cs +
historic KPI benchmarks), tweak one field, ship.

Storage:
    Seeded YAML files in ``data/wavef_templates/*.yaml`` are loaded
    into Redis at first access (lazy). Each template lives in
    ``wavef:template:{tid}`` HASH and the master set is
    ``wavef:templates:all`` (SET of tid).

Cloning into a real campaign is wired through whatever campaign
creation hook is supplied by the caller (DI). The router uses a
minimal in-Redis stub so spec is testable without coupling to the
existing campaigns module.

NEW file — no existing module touched.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Optional
from uuid import uuid4

import redis.asyncio as aioredis


_TEMPLATE_DIR_ENV = "KIX_WAVEF_TEMPLATE_DIR"
_DEFAULT_TEMPLATE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "wavef_templates")
)


# ── Keys ─────────────────────────────────────────────────────────────────


def _k_tpl(tid: str) -> str:
    return f"wavef:template:{tid}"


def _k_all() -> str:
    return "wavef:templates:all"


def _k_loaded() -> str:
    return "wavef:templates:loaded_flag"


# ── Seed catalog (also written as YAML on first call) ────────────────────


_SEED: list[dict[str, Any]] = [
    {
        "id": "qsr_lunch_rush",
        "vertical": "qsr",
        "region": "global",
        "mechanics": ["spin_wheel", "scratch_card"],
        "reward_floor": 0.05,
        "duration_days": 14,
        "expected_kpis": {"ctr": 0.18, "redemption": 0.62},
        "default_terms_template": "us_qsr_v1",
        "description": "Lunch-hour QSR engagement boost.",
    },
    {
        "id": "fashion_seasonal_drop",
        "vertical": "fashion",
        "region": "global",
        "mechanics": ["memory_match", "spin_wheel"],
        "reward_floor": 0.10,
        "duration_days": 21,
        "expected_kpis": {"ctr": 0.21, "redemption": 0.45},
        "default_terms_template": "us_fashion_v1",
        "description": "Seasonal drop reveal + discount voucher.",
    },
    {
        "id": "grocery_loyalty_weekly",
        "vertical": "grocery",
        "region": "global",
        "mechanics": ["daily_checkin", "scratch_card"],
        "reward_floor": 0.03,
        "duration_days": 30,
        "expected_kpis": {"ctr": 0.14, "redemption": 0.72},
        "default_terms_template": "us_grocery_v1",
        "description": "Weekly basket-builder loyalty.",
    },
    {
        "id": "telco_data_top_up",
        "vertical": "telco",
        "region": "apac",
        "mechanics": ["spin_wheel"],
        "reward_floor": 0.08,
        "duration_days": 7,
        "expected_kpis": {"ctr": 0.20, "redemption": 0.55},
        "default_terms_template": "apac_telco_v1",
        "description": "Data top-up bonus spin.",
    },
    {
        "id": "fnb_happy_hour_flash",
        "vertical": "fnb",
        "region": "global",
        "mechanics": ["flash_promo", "scratch_card"],
        "reward_floor": 0.15,
        "duration_days": 3,
        "expected_kpis": {"ctr": 0.28, "redemption": 0.68},
        "default_terms_template": "us_fnb_v1",
        "description": "Happy-hour urgency flash with scratch reward.",
    },
    {
        "id": "saas_referral_both_win",
        "vertical": "saas",
        "region": "global",
        "mechanics": ["referral"],
        "reward_floor": 5.00,
        "duration_days": 60,
        "expected_kpis": {"ctr": 0.11, "redemption": 0.40},
        "default_terms_template": "global_saas_v1",
        "description": "Refer-a-friend dual reward.",
    },
]


# ── YAML helpers (optional dependency: graceful fallback to JSON) ────────


def _dump_yaml(obj: dict) -> str:
    try:
        import yaml  # type: ignore
        return yaml.safe_dump(obj, sort_keys=False)
    except Exception:  # pragma: no cover — PyYAML not installed
        return json.dumps(obj, indent=2)


def _load_yaml(text: str) -> dict:
    try:
        import yaml  # type: ignore
        loaded = yaml.safe_load(text)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        try:
            return json.loads(text)
        except Exception:
            return {}


def seed_to_disk(template_dir: Optional[str] = None) -> str:
    """Write seed templates to disk as YAML and return the dir path."""
    d = template_dir or os.environ.get(_TEMPLATE_DIR_ENV) or _DEFAULT_TEMPLATE_DIR
    os.makedirs(d, exist_ok=True)
    for tpl in _SEED:
        path = os.path.join(d, f"{tpl['id']}.yaml")
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(_dump_yaml(tpl))
    return d


def _read_templates_from_disk(template_dir: str) -> list[dict]:
    out: list[dict] = []
    if not os.path.isdir(template_dir):
        return out
    for fn in sorted(os.listdir(template_dir)):
        if not (fn.endswith(".yaml") or fn.endswith(".yml")):
            continue
        try:
            with open(os.path.join(template_dir, fn), encoding="utf-8") as f:
                tpl = _load_yaml(f.read())
            if tpl and isinstance(tpl, dict) and "id" in tpl:
                out.append(tpl)
        except Exception:  # pragma: no cover
            continue
    return out


# ── Public API ───────────────────────────────────────────────────────────


async def ensure_loaded(
    r: aioredis.Redis,
    *,
    template_dir: Optional[str] = None,
    force: bool = False,
) -> int:
    """Idempotently load templates from disk (or seed) into Redis."""
    if not force and await r.get(_k_loaded()):
        return int(await r.scard(_k_all()))
    d = template_dir or os.environ.get(_TEMPLATE_DIR_ENV) or _DEFAULT_TEMPLATE_DIR
    if not os.path.isdir(d) and not template_dir:
        # Self-seed for clean envs.
        seed_to_disk(d)
    templates = _read_templates_from_disk(d) or list(_SEED)
    pipe = r.pipeline(transaction=True)
    for tpl in templates:
        tid = tpl["id"]
        pipe.hset(
            _k_tpl(tid),
            mapping={
                "id": tid,
                "vertical": str(tpl.get("vertical", "")),
                "region": str(tpl.get("region", "global")),
                "json": json.dumps(tpl),
            },
        )
        pipe.sadd(_k_all(), tid)
    pipe.set(_k_loaded(), "1", ex=24 * 3600)
    await pipe.execute()
    return len(templates)


async def list_templates(
    r: aioredis.Redis,
    *,
    vertical: Optional[str] = None,
    mechanic: Optional[str] = None,
    region: Optional[str] = None,
) -> list[dict]:
    await ensure_loaded(r)
    tids = await r.smembers(_k_all())
    out: list[dict] = []
    for tid_raw in sorted(tids):
        tid = tid_raw if isinstance(tid_raw, str) else tid_raw.decode()
        tpl = await get_template(r, tid)
        if tpl is None:
            continue
        if vertical and tpl.get("vertical") != vertical:
            continue
        if region and tpl.get("region") not in (None, "global", region):
            continue
        if mechanic and mechanic not in (tpl.get("mechanics") or []):
            continue
        out.append(tpl)
    return out


async def get_template(r: aioredis.Redis, tid: str) -> Optional[dict]:
    raw = await r.hget(_k_tpl(tid), "json")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


async def clone_template(
    r: aioredis.Redis,
    tid: str,
    *,
    brand_id: str,
    overrides: Optional[dict] = None,
    creator: Optional[Callable[[dict], Any]] = None,
) -> dict:
    """Clone a template into a campaign-shaped payload.

    If a ``creator`` callable is provided it is invoked with the merged
    campaign dict and its return value used as ``campaign_id``. Otherwise
    a synthetic ``cmp_<hex>`` id is generated and a stub record stored at
    ``wavef:tpl:clone:{cid}`` so the call is observable end-to-end without
    coupling to the existing campaign creation module.
    """
    tpl = await get_template(r, tid)
    if tpl is None:
        raise KeyError(tid)
    merged = dict(tpl)
    merged.update(overrides or {})
    merged["brand_id"] = brand_id
    merged["template_id"] = tid
    merged["created_ms"] = int(time.time() * 1000)
    if creator is not None:
        cid = creator(merged)
        if not isinstance(cid, str):  # async-friendly: support coroutine
            try:
                import asyncio
                if asyncio.iscoroutine(cid):
                    cid = await cid
            except Exception:
                pass
    else:
        cid = f"cmp_{uuid4().hex[:22]}"
        await r.hset(
            f"wavef:tpl:clone:{cid}",
            mapping={"json": json.dumps(merged), "template_id": tid},
        )
    return {
        "campaign_id": cid,
        "template_id": tid,
        "brand_id": brand_id,
        "merged": merged,
    }
