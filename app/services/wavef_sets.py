"""Collect-a-set mechanic (Monopoly-style) — Wave F spec #12.

Brand sets up a "collect N of M pieces" campaign. Each game completion
calls :func:`draw` and receives one piece chosen by weighted random.
When the user has at least ``target`` distinct pieces (including the
grand-prize piece, if any), :func:`redeem` flips a once-and-only-once
flag and reports success.

Anti-frustration boost
----------------------
When the user is one piece away from completing the set AND the
missing piece carries the ``grand`` flag (i.e. the deliberately rare
finisher), every additional draw boosts the grand piece's weight by
``+10%`` (additive) up to ``5x`` the base. This mirrors the
goal-gradient stretch seen in the McDonald's Monopoly mechanic.

Redis schema
------------
::

    sets:cmp:{cid}                 HASH {brand_id, pieces_json, target}
    sets:cmp:{cid}:user:{uid}      HASH piece_id -> count
    sets:cmp:{cid}:user:{uid}:done STRING "1"   redemption flag
    sets:cmp:{cid}:user:{uid}:misses STRING int draws on near-complete state

NEW file.
"""

from __future__ import annotations

import json
import random
import time
from uuid import uuid4


def _k_cfg(cid: str) -> str:
    return f"sets:cmp:{cid}"


def _k_inv(cid: str, uid: str) -> str:
    return f"sets:cmp:{cid}:user:{uid}"


def _k_done(cid: str, uid: str) -> str:
    return f"sets:cmp:{cid}:user:{uid}:done"


def _k_misses(cid: str, uid: str) -> str:
    return f"sets:cmp:{cid}:user:{uid}:misses"


async def create_campaign(
    r,
    *,
    brand_id: str,
    name: str,
    pieces: list[dict],
    target: int,
) -> dict:
    """Create a collect-a-set campaign.

    Each piece needs: ``id``, ``label``, ``rarity_weight`` (>0). At most
    one piece can have ``grand: True``.
    """
    if target < 2:
        raise ValueError("target must be >= 2")
    if not pieces or len(pieces) < target:
        raise ValueError("pieces must contain at least `target` entries")
    grand_count = sum(1 for p in pieces if p.get("grand"))
    if grand_count > 1:
        raise ValueError("at most one piece may be flagged grand")
    norm: list[dict] = []
    seen: set[str] = set()
    for p in pieces:
        pid = str(p.get("id") or "").strip()
        if not pid or pid in seen:
            raise ValueError(f"piece id must be unique and non-empty: {pid!r}")
        seen.add(pid)
        w = float(p.get("rarity_weight", 1.0))
        if w <= 0:
            raise ValueError("rarity_weight must be > 0")
        norm.append({
            "id": pid,
            "label": str(p.get("label") or pid),
            "rarity_weight": w,
            "grand": bool(p.get("grand")),
        })
    cid = uuid4().hex[:12]
    await r.hset(
        _k_cfg(cid),
        mapping={
            "brand_id": brand_id,
            "name": name,
            "pieces_json": json.dumps(norm),
            "target": str(int(target)),
            "created_at_ms": str(int(time.time() * 1000)),
        },
    )
    return {
        "campaign_id": cid,
        "brand_id": brand_id,
        "name": name,
        "pieces": norm,
        "target": int(target),
    }


async def get_campaign(r, cid: str) -> dict | None:
    raw = await r.hgetall(_k_cfg(cid))
    if not raw:
        return None
    norm: dict[str, str] = {}
    for k, v in raw.items():
        norm[k.decode() if isinstance(k, bytes) else k] = (
            v.decode() if isinstance(v, bytes) else v
        )
    try:
        pieces = json.loads(norm.get("pieces_json", "[]"))
    except (json.JSONDecodeError, TypeError):
        pieces = []
    return {
        "campaign_id": cid,
        "brand_id": norm.get("brand_id", ""),
        "name": norm.get("name", ""),
        "pieces": pieces,
        "target": int(norm.get("target", "0") or 0),
    }


async def _inventory_map(r, cid: str, uid: str) -> dict[str, int]:
    raw = await r.hgetall(_k_inv(cid, uid))
    out: dict[str, int] = {}
    for k, v in (raw or {}).items():
        k = k.decode() if isinstance(k, bytes) else k
        v = v.decode() if isinstance(v, bytes) else v
        try:
            out[k] = int(v)
        except (TypeError, ValueError):
            out[k] = 0
    return out


async def inventory(r, cid: str, uid: str) -> dict | None:
    cfg = await get_campaign(r, cid)
    if cfg is None:
        return None
    inv = await _inventory_map(r, cid, uid)
    distinct = sum(1 for c in inv.values() if c > 0)
    done = bool(await r.get(_k_done(cid, uid)))
    return {
        "campaign_id": cid,
        "uid": uid,
        "counts": inv,
        "distinct": distinct,
        "target": cfg["target"],
        "complete": distinct >= cfg["target"],
        "redeemed": done,
    }


def _weighted_pick(
    pieces: list[dict],
    weights: list[float],
    rng: random.Random,
) -> dict:
    total = sum(weights)
    if total <= 0:
        return pieces[0]
    pick = rng.random() * total
    cum = 0.0
    for p, w in zip(pieces, weights):
        cum += w
        if pick <= cum:
            return p
    return pieces[-1]


def _boosted_weights(
    pieces: list[dict],
    inv: dict[str, int],
    target: int,
    misses: int,
) -> list[float]:
    """Apply anti-frustration boost.

    Active only when the user is one piece short AND the missing piece
    is the grand piece. Boost is +10% additive per missed draw, capped
    at 5x base weight.
    """
    distinct = sum(1 for c in inv.values() if c > 0)
    weights = [float(p["rarity_weight"]) for p in pieces]
    if distinct != target - 1:
        return weights
    grand = next((p for p in pieces if p.get("grand")), None)
    if grand is None or inv.get(grand["id"], 0) > 0:
        return weights
    boost = min(5.0, 1.0 + 0.10 * max(0, misses))
    return [w * boost if p["id"] == grand["id"] else w for p, w in zip(pieces, weights)]


async def draw(
    r,
    cid: str,
    uid: str,
    *,
    rng: random.Random | None = None,
) -> dict:
    """Draw one piece, weighted by rarity (+ anti-frustration)."""
    cfg = await get_campaign(r, cid)
    if cfg is None:
        raise ValueError("campaign not found")
    if await r.get(_k_done(cid, uid)):
        raise ValueError("user has already redeemed this set")
    inv = await _inventory_map(r, cid, uid)
    misses_raw = await r.get(_k_misses(cid, uid))
    misses = int(misses_raw) if misses_raw else 0

    weights = _boosted_weights(cfg["pieces"], inv, cfg["target"], misses)
    rng = rng or random.Random()
    pick = _weighted_pick(cfg["pieces"], weights, rng)
    await r.hincrby(_k_inv(cid, uid), pick["id"], 1)

    # Track misses: still one-short after this draw and grand still missing?
    new_inv = dict(inv)
    new_inv[pick["id"]] = new_inv.get(pick["id"], 0) + 1
    distinct = sum(1 for c in new_inv.values() if c > 0)
    grand = next((p for p in cfg["pieces"] if p.get("grand")), None)
    if (
        distinct == cfg["target"] - 1
        and grand is not None
        and new_inv.get(grand["id"], 0) == 0
    ):
        await r.incr(_k_misses(cid, uid))
    else:
        await r.delete(_k_misses(cid, uid))

    return {"piece": pick, "distinct": distinct, "target": cfg["target"]}


async def redeem(r, cid: str, uid: str) -> dict:
    """Mark the grand prize as claimed. 400 if incomplete, 409 if redone."""
    cfg = await get_campaign(r, cid)
    if cfg is None:
        raise ValueError("campaign not found")
    inv = await _inventory_map(r, cid, uid)
    distinct = sum(1 for c in inv.values() if c > 0)
    if distinct < cfg["target"]:
        raise ValueError("set incomplete")
    # SETNX-style: only first claim wins.
    set_ok = await r.set(_k_done(cid, uid), "1", nx=True)
    if not set_ok:
        # Already claimed once.
        raise PermissionError("already redeemed")
    return {"redeemed": True, "claimed_at_ms": int(time.time() * 1000)}
