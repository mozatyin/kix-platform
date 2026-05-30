"""Memory-match game service — Wave F obvious-win #10.

Inspired by CataBoom. Classic Concentration: NxN grid of paired tiles,
user flips two at a time; matches stay revealed; full clear = win + voucher.

Anti-cheat: server holds the full deck. Client only sees what it has
flipped. Each /flip is server-recorded; ``complete`` requires server's
match set to be full + client-claimed flip count to match server.

Redis schema::

    memory:s:{sid}    HASH   {brand_id, user_id, difficulty, grid_size,
                              deck_json, matched_json, flip_count,
                              pending_flip_pos, pending_flip_symbol,
                              started_at_ms, completed_at_ms, won}

NEW file.
"""

from __future__ import annotations

import json
import random
import time
from uuid import uuid4


# difficulty → grid_size (NxN; total tiles must be even)
_DIFFICULTY = {1: 4, 2: 6, 3: 8}   # 4x4=16, 6x6=36, 8x8=64
SESSION_TTL_SEC = 30 * 60


def _k_session(sid: str) -> str:
    return f"memory:s:{sid}"


async def _decode_hash(r, key: str) -> dict[str, str]:
    raw = await r.hgetall(key)
    out: dict[str, str] = {}
    for k, v in raw.items():
        k = k.decode() if isinstance(k, bytes) else k
        v = v.decode() if isinstance(v, bytes) else v
        out[k] = v
    return out


def gen_deck(grid_size: int, rng: random.Random) -> list[int]:
    """Return a shuffled list of length ``grid_size*grid_size`` with each
    symbol appearing exactly twice (i.e. all pairs)."""
    n = grid_size * grid_size
    if n % 2 != 0:
        raise ValueError("grid_size produces odd cell count")
    symbols = list(range(n // 2)) * 2
    rng.shuffle(symbols)
    return symbols


async def create_session(
    r,
    brand_id: str,
    user_id: str,
    difficulty: int = 1,
    seed: int | None = None,
) -> dict:
    if not brand_id or not user_id:
        raise ValueError("brand_id and user_id required")
    if difficulty not in _DIFFICULTY:
        raise ValueError("difficulty must be 1, 2 or 3")
    grid_size = _DIFFICULTY[difficulty]
    rng = random.Random(seed) if seed is not None else random.SystemRandom()
    deck = gen_deck(grid_size, rng)
    sid = uuid4().hex[:12]
    await r.hset(
        _k_session(sid),
        mapping={
            "brand_id": brand_id,
            "user_id": user_id,
            "difficulty": str(difficulty),
            "grid_size": str(grid_size),
            "deck_json": json.dumps(deck),
            "matched_json": json.dumps([]),
            "flip_count": "0",
            "pending_flip_pos": "-1",
            "pending_flip_symbol": "-1",
            "started_at_ms": str(int(time.time() * 1000)),
            "won": "0",
        },
    )
    await r.expire(_k_session(sid), SESSION_TTL_SEC)
    return {
        "session_id": sid,
        "brand_id": brand_id,
        "user_id": user_id,
        "difficulty": difficulty,
        "grid_size": grid_size,
        "deck_layout_masked": ["?"] * len(deck),
    }


async def _load_session(r, sid: str) -> dict:
    raw = await _decode_hash(r, _k_session(sid))
    if not raw:
        raise ValueError("session not found")
    try:
        deck = json.loads(raw.get("deck_json", "[]"))
        matched = set(json.loads(raw.get("matched_json", "[]")))
    except (json.JSONDecodeError, TypeError):
        deck = []
        matched = set()
    return {
        "raw": raw,
        "deck": deck,
        "matched": matched,
        "flip_count": int(raw.get("flip_count", "0") or 0),
        "pending_pos": int(raw.get("pending_flip_pos", "-1") or -1),
        "pending_symbol": int(raw.get("pending_flip_symbol", "-1") or -1),
        "won": raw.get("won") == "1",
        "grid_size": int(raw.get("grid_size", "0") or 0),
        "user_id": raw.get("user_id", ""),
    }


async def flip(r, sid: str, user_id: str, position: int) -> dict:
    """Flip a tile. Each pair of flips is a "round"; on the 2nd flip we
    decide match / no-match. Already-matched positions cannot be flipped.
    Flipping the same pending position twice in a row is rejected.
    """
    state = await _load_session(r, sid)
    if state["user_id"] != user_id:
        raise ValueError("not your session")
    if state["won"]:
        raise ValueError("session already complete")
    deck = state["deck"]
    if position < 0 or position >= len(deck):
        raise ValueError("position out of range")
    if position in state["matched"]:
        raise ValueError("tile already matched")
    if position == state["pending_pos"]:
        raise ValueError("cannot flip same tile twice")

    new_flip_count = state["flip_count"] + 1
    symbol = deck[position]
    second_flip_result: str | None = None
    matched_now = False
    new_pending_pos = state["pending_pos"]
    new_pending_symbol = state["pending_symbol"]

    if state["pending_pos"] == -1:
        # First flip in a pair.
        new_pending_pos = position
        new_pending_symbol = symbol
    else:
        # Second flip in a pair.
        if symbol == state["pending_symbol"]:
            state["matched"].add(state["pending_pos"])
            state["matched"].add(position)
            second_flip_result = "match"
            matched_now = True
        else:
            second_flip_result = "no_match"
        new_pending_pos = -1
        new_pending_symbol = -1

    await r.hset(
        _k_session(sid),
        mapping={
            "flip_count": str(new_flip_count),
            "matched_json": json.dumps(sorted(state["matched"])),
            "pending_flip_pos": str(new_pending_pos),
            "pending_flip_symbol": str(new_pending_symbol),
        },
    )
    await r.expire(_k_session(sid), SESSION_TTL_SEC)

    all_matched = len(state["matched"]) == len(deck)
    return {
        "session_id": sid,
        "position": position,
        "tile_face": symbol,
        "second_flip_result": second_flip_result,
        "matched": matched_now,
        "flip_count": new_flip_count,
        "matched_positions": sorted(state["matched"]),
        "all_matched": all_matched,
    }


async def complete(
    r,
    sid: str,
    user_id: str,
    flips: int,
    time_ms: int,
) -> dict:
    state = await _load_session(r, sid)
    if state["user_id"] != user_id:
        raise ValueError("not your session")
    if state["won"]:
        return {
            "session_id": sid,
            "won": True,
            "already_completed": True,
            "flip_count": state["flip_count"],
            "score": _score(state["grid_size"], state["flip_count"]),
        }
    if len(state["matched"]) != len(state["deck"]):
        raise ValueError("session not yet solved")
    if flips != state["flip_count"]:
        raise ValueError("client flip count mismatch")
    if time_ms < 0:
        raise ValueError("time_ms must be >= 0")

    score = _score(state["grid_size"], flips)
    completed_ms = int(time.time() * 1000)
    await r.hset(
        _k_session(sid),
        mapping={
            "won": "1",
            "completed_at_ms": str(completed_ms),
        },
    )
    return {
        "session_id": sid,
        "won": True,
        "already_completed": False,
        "flip_count": flips,
        "time_ms": time_ms,
        "score": score,
    }


def _score(grid_size: int, flips: int) -> int:
    """Fewer flips = higher score. Minimum perfect flips = N pairs * 2."""
    if grid_size <= 0 or flips <= 0:
        return 0
    pairs = (grid_size * grid_size) // 2
    perfect = pairs * 2
    # 1000 base; lose 10 per "extra" flip beyond perfect; floor 0.
    extra = max(0, flips - perfect)
    return max(0, 1000 - extra * 10)
