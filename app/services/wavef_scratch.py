"""Scratch-card service — Wave F obvious-win #9.

Inspired by CataBoom. Reveal-3-of-9 mechanic: card has 9 cells; if 3 cells
contain the SAME symbol the card wins; client only ever sees the revealed
grid. Outcome is server-determined at issue time and stored — the
``/reveal`` endpoint merely uncovers the stored result, so the result is
fully server-authoritative.

Redis schema::

    scratch:cfg:{cid}        HASH   {brand_id, win_probability,
                                      symbol_pool_json, win_payload_json,
                                      created_at_ms}
    scratch:card:{kid}       HASH   {config_id, brand_id, outcome_json,
                                      won:0|1, user_id, issued_at_ms,
                                      revealed:0|1, payload_json}

NEW file.
"""

from __future__ import annotations

import json
import random
import time
from typing import Any
from uuid import uuid4


_DEFAULT_POOL = [
    "cherry", "lemon", "bell", "diamond", "seven",
    "star", "bar", "clover", "horseshoe", "crown",
    "anchor", "rose", "moon", "sun",
]


def _k_cfg(cid: str) -> str:
    return f"scratch:cfg:{cid}"


def _k_card(card_id: str) -> str:
    return f"scratch:card:{card_id}"


async def _decode_hash(r, key: str) -> dict[str, str]:
    raw = await r.hgetall(key)
    out: dict[str, str] = {}
    for k, v in raw.items():
        k = k.decode() if isinstance(k, bytes) else k
        v = v.decode() if isinstance(v, bytes) else v
        out[k] = v
    return out


async def create_config(
    r,
    brand_id: str,
    win_probability: float,
    symbol_pool: list[str] | None = None,
    win_payload: dict[str, Any] | None = None,
) -> dict:
    if not brand_id:
        raise ValueError("brand_id required")
    if not (0 < win_probability <= 1):
        raise ValueError("win_probability must be in (0, 1]")
    pool = symbol_pool or _DEFAULT_POOL
    if len(pool) < 6:
        raise ValueError("symbol pool must have >= 6 symbols")
    if len(set(pool)) != len(pool):
        raise ValueError("symbol pool must have unique symbols")

    cid = uuid4().hex[:12]
    await r.hset(
        _k_cfg(cid),
        mapping={
            "brand_id": brand_id,
            "win_probability": str(win_probability),
            "symbol_pool_json": json.dumps(pool),
            "win_payload_json": json.dumps(win_payload or {}),
            "created_at_ms": str(int(time.time() * 1000)),
        },
    )
    return {
        "config_id": cid,
        "brand_id": brand_id,
        "win_probability": win_probability,
        "symbol_pool": pool,
        "win_payload": win_payload or {},
    }


async def get_config(r, cid: str) -> dict | None:
    raw = await _decode_hash(r, _k_cfg(cid))
    if not raw:
        return None
    try:
        pool = json.loads(raw.get("symbol_pool_json", "[]"))
        payload = json.loads(raw.get("win_payload_json", "{}"))
    except (json.JSONDecodeError, TypeError):
        pool = []
        payload = {}
    return {
        "config_id": cid,
        "brand_id": raw.get("brand_id", ""),
        "win_probability": float(raw.get("win_probability", "0") or 0),
        "symbol_pool": pool,
        "win_payload": payload,
    }


def generate_grid(
    rng: random.Random,
    symbol_pool: list[str],
    win: bool,
) -> list[str]:
    """Deterministically build a 9-cell grid.

    If ``win`` is True, exactly one symbol appears 3 times in random
    positions; the remaining 6 cells are 6 distinct other symbols (so no
    accidental second 3-of-a-kind). If ``win`` is False, the grid contains
    only symbols that appear at most twice.
    """
    if len(symbol_pool) < 7:
        raise ValueError("symbol pool must have >= 7 symbols")

    if win:
        winning_sym = rng.choice(symbol_pool)
        rest = [s for s in symbol_pool if s != winning_sym]
        fillers = rng.sample(rest, 6)
        positions = rng.sample(range(9), 3)
        grid: list[str | None] = [None] * 9
        for p in positions:
            grid[p] = winning_sym
        idx = 0
        for i in range(9):
            if grid[i] is None:
                grid[i] = fillers[idx]
                idx += 1
        return [g for g in grid if g is not None]  # type: ignore[return-value]

    # Lose grid: choose 5 distinct symbols, with one of them appearing
    # twice (rest once each → 5 distinct + 1 dup-of-existing). No symbol
    # appears 3 times.  Total cells = 9.
    chosen = rng.sample(symbol_pool, 5)
    dup_count = 4  # cells beyond the 5 distinct; each at most one extra of an existing symbol
    grid_pool = list(chosen)
    extra = []
    for _ in range(dup_count):
        # pick a symbol that currently has < 2 occurrences (avoid 3rd)
        candidates = [
            s for s in chosen if (grid_pool + extra).count(s) < 2
        ]
        if not candidates:
            # Should not happen with chosen=5 and dup_count=4
            # but fall back to a fresh distinct symbol from pool.
            fresh = [s for s in symbol_pool if s not in grid_pool + extra]
            if not fresh:
                raise ValueError("cannot build losing grid")
            extra.append(rng.choice(fresh))
        else:
            extra.append(rng.choice(candidates))
    grid = grid_pool + extra
    rng.shuffle(grid)

    # Defensive: assert no 3-of-a-kind in lose grids.
    for s in set(grid):
        if grid.count(s) >= 3:
            # Re-shuffle by swapping one of the dupes with a fresh symbol
            fresh = [t for t in symbol_pool if t not in grid]
            if fresh:
                for i, cell in enumerate(grid):
                    if cell == s:
                        grid[i] = rng.choice(fresh)
                        break
    return grid


def _has_three_of_a_kind(grid: list[str]) -> tuple[bool, str | None]:
    for s in set(grid):
        if grid.count(s) >= 3:
            return True, s
    return False, None


async def issue_card(
    r,
    config_id: str,
    user_id: str,
    seed: int | None = None,
) -> dict:
    cfg = await get_config(r, config_id)
    if cfg is None:
        raise ValueError("config not found")

    rng = random.Random(seed) if seed is not None else random.SystemRandom()
    won = rng.random() < cfg["win_probability"]
    grid = generate_grid(rng, cfg["symbol_pool"], win=won)
    payload = cfg["win_payload"] if won else {}

    card_id = uuid4().hex[:16]
    await r.hset(
        _k_card(card_id),
        mapping={
            "config_id": config_id,
            "brand_id": cfg["brand_id"],
            "outcome_json": json.dumps(grid),
            "won": "1" if won else "0",
            "user_id": user_id,
            "issued_at_ms": str(int(time.time() * 1000)),
            "revealed": "0",
            "payload_json": json.dumps(payload),
        },
    )
    await r.expire(_k_card(card_id), 30 * 24 * 3600)
    return {
        "card_id": card_id,
        "config_id": config_id,
        "grid_masked": ["?"] * 9,
        "user_id": user_id,
    }


async def reveal_card(r, card_id: str, user_id: str) -> dict:
    raw = await _decode_hash(r, _k_card(card_id))
    if not raw:
        raise ValueError("card not found")
    if raw.get("user_id") != user_id:
        raise ValueError("not your card")

    try:
        grid = json.loads(raw.get("outcome_json", "[]"))
        payload = json.loads(raw.get("payload_json", "{}"))
    except (json.JSONDecodeError, TypeError):
        grid = []
        payload = {}

    won = raw.get("won") == "1"
    if raw.get("revealed") != "1":
        await r.hset(_k_card(card_id), "revealed", "1")

    return {
        "card_id": card_id,
        "grid": grid,
        "won": won,
        "payload": payload,
    }
