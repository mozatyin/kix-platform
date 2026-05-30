"""Quick poll widget — Wave F obvious-win #4.

Inspired by BRAME's "one-question intro before game" engagement pattern.
A brand author creates a poll with N options; users vote once; results
are returned in real time.

Redis schema:
    poll:{pid}:meta         HASH   {question, brand_id, created_at_ms,
                                    options_json, max_votes_per_user}
    poll:{pid}:votes:{opt}  STRING int   per-option counter
    poll:{pid}:voters       SET    user_id (one vote per user)

NEW file.
"""

from __future__ import annotations

import json
import time
from uuid import uuid4


def _k_meta(pid: str) -> str:
    return f"poll:{pid}:meta"


def _k_votes(pid: str, opt_id: str) -> str:
    return f"poll:{pid}:votes:{opt_id}"


def _k_voters(pid: str) -> str:
    return f"poll:{pid}:voters"


async def create_poll(
    r,
    brand_id: str,
    question: str,
    options: list[str],
) -> dict:
    """Create poll and return {poll_id, options:[{id,label}], ...}."""
    if not options or len(options) < 2:
        raise ValueError("poll needs at least 2 options")
    if len(options) > 8:
        raise ValueError("poll supports at most 8 options")
    pid = uuid4().hex[:12]
    option_records = [
        {"id": f"opt{i}", "label": label} for i, label in enumerate(options)
    ]
    meta = {
        "question": question,
        "brand_id": brand_id,
        "created_at_ms": int(time.time() * 1000),
        "options": option_records,
    }
    await r.hset(_k_meta(pid), mapping={
        "question": question,
        "brand_id": brand_id,
        "created_at_ms": str(meta["created_at_ms"]),
        "options_json": json.dumps(option_records),
    })
    return {"poll_id": pid, **meta}


async def get_poll(r, poll_id: str) -> dict | None:
    raw = await r.hgetall(_k_meta(poll_id))
    if not raw:
        return None
    # Normalize bytes/str
    norm: dict[str, str] = {}
    for k, v in raw.items():
        k = k.decode() if isinstance(k, bytes) else k
        v = v.decode() if isinstance(v, bytes) else v
        norm[k] = v
    try:
        options = json.loads(norm.get("options_json", "[]"))
    except (json.JSONDecodeError, TypeError):
        options = []
    return {
        "poll_id": poll_id,
        "question": norm.get("question", ""),
        "brand_id": norm.get("brand_id", ""),
        "created_at_ms": int(norm.get("created_at_ms", "0") or 0),
        "options": options,
    }


async def vote(r, poll_id: str, user_id: str, option_id: str) -> dict:
    """Cast a vote. Returns {accepted, totals}."""
    meta = await get_poll(r, poll_id)
    if meta is None:
        raise ValueError("poll not found")
    valid_ids = {o["id"] for o in meta["options"]}
    if option_id not in valid_ids:
        raise ValueError("invalid option_id")

    added = await r.sadd(_k_voters(poll_id), user_id)
    accepted = bool(added)
    if accepted:
        await r.incr(_k_votes(poll_id, option_id))

    totals = await _totals(r, poll_id, meta["options"])
    return {"accepted": accepted, "totals": totals}


async def results(r, poll_id: str) -> dict | None:
    meta = await get_poll(r, poll_id)
    if meta is None:
        return None
    totals = await _totals(r, poll_id, meta["options"])
    return {**meta, "totals": totals, "total_voters": sum(totals.values())}


async def _totals(r, poll_id: str, options: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for o in options:
        raw = await r.get(_k_votes(poll_id, o["id"]))
        try:
            out[o["id"]] = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            out[o["id"]] = 0
    return out
