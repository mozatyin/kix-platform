#!/usr/bin/env python3
"""One-shot importer: legacy Redis ``audit:*`` LISTs → ``audit_log`` table.

The pre-R5 platform recorded every privileged action to a capped Redis
LIST (``audit:*``, cap=1000). This script drains those lists into the
new durable ``audit_log`` table introduced in migration 0007.

Why one-shot, not streaming?
----------------------------
The Redis lists are the *historical* tail; new writes already land in
PG via ``audit_log_service.record_event``. After this script has run
once on each environment, the Redis lists can be DEL-ed at leisure —
they are no longer read by any code path.

Idempotency
-----------
Each event is inserted with an ``event_id`` derived from the Redis
list-key + the legacy event timestamp + a stable hash of the payload.
Re-running the script is a no-op on already-imported rows because the
``record_event`` write path uses ``ON CONFLICT (event_id) DO NOTHING``.

Legacy shape
------------
We saw three list shapes in production. The parser tries them in turn:

1. ``compliance:pii_audit:user:{uid}`` — JSON dicts, ``{ts, action,
   actor, …}``. Most common.
2. ``compliance:pii_audit:brand:{bid}`` — same shape, brand-scoped.
3. ``payouts:audit:inter_brand`` — JSON dicts with payouts-specific
   keys.

Anything that doesn't parse is logged + skipped (counted in the
"unparsed" tally) so the importer never crashes on a malformed row.

Usage
-----
    python -m scripts.migrate_audit_redis_to_pg            # dry-run
    python -m scripts.migrate_audit_redis_to_pg --apply    # commit
    python -m scripts.migrate_audit_redis_to_pg --apply \
        --pattern 'audit:*'                                # custom scan
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
from typing import Any

logger = logging.getLogger("migrate_audit")


# Default key patterns we scan. Order matters only for the dry-run log.
DEFAULT_PATTERNS: tuple[str, ...] = (
    "compliance:pii_audit:*",
    "payouts:audit:*",
    "audit:*",
)


def _derive_event_id(list_key: str, raw: str) -> str:
    """Deterministic event_id so re-imports are idempotent.

    SHA-256 over (list_key || raw_payload) → 22 hex chars, prefixed
    ``evt_`` to match the platform ID convention.
    """
    h = hashlib.sha256(f"{list_key}\x1f{raw}".encode("utf-8")).hexdigest()
    return f"evt_{h[:22]}"


def _parse_legacy(raw: str) -> dict[str, Any] | None:
    """Parse one legacy list entry into a kwargs dict for record_event.

    Returns None when the entry can't be coerced into the new schema —
    caller logs + increments the ``unparsed`` counter.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    # The three legacy shapes use slightly different field names. Map
    # them all onto record_event kwargs.
    actor_id = (
        data.get("actor_id")
        or data.get("actor")
        or data.get("user_id")
        or data.get("uid")
        or data.get("admin_id")
        or "legacy-unknown"
    )
    actor_type = data.get("actor_type") or (
        "admin" if data.get("admin_id") else "system"
    )
    action = (
        data.get("action") or data.get("event") or data.get("op") or "legacy"
    )
    brand_id = data.get("brand_id") or data.get("bid")
    target_type = data.get("target_type")
    target_id = data.get("target_id") or data.get("target")

    return {
        "actor_id": str(actor_id),
        "actor_type": str(actor_type),
        "action": str(action),
        "brand_id": str(brand_id) if brand_id else None,
        "target_type": str(target_type) if target_type else None,
        "target_id": str(target_id) if target_id else None,
        "jurisdiction": data.get("jurisdiction") or data.get("region"),
        "result": data.get("result") or data.get("status"),
        "payload": {
            "_migrated": True,
            "_source": "redis",
            "raw": data,
        },
    }


async def _scan_keys(redis: Any, pattern: str) -> list[str]:
    """SCAN all keys matching ``pattern``. Returns a list of str keys."""
    keys: list[str] = []
    async for k in redis.scan_iter(match=pattern, count=500):
        if isinstance(k, bytes):
            k = k.decode("utf-8", errors="replace")
        keys.append(k)
    return keys


async def _import_one_list(
    db_session_factory: Any,
    redis: Any,
    list_key: str,
    *,
    apply: bool,
) -> tuple[int, int, int]:
    """Drain one list. Returns (seen, imported, unparsed)."""
    raw_entries = await redis.lrange(list_key, 0, -1)
    seen = imported = unparsed = 0

    # Lazy-import to avoid module-load cost on --dry-run / --help.
    from app.services.audit_log_service import record_event

    for raw in raw_entries:
        seen += 1
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")

        kwargs = _parse_legacy(raw)
        if kwargs is None:
            unparsed += 1
            logger.debug("unparsed entry on %s: %.80s", list_key, raw)
            continue

        event_id = _derive_event_id(list_key, raw)

        if not apply:
            imported += 1  # dry-run accounting
            continue

        async with db_session_factory() as db:
            try:
                await record_event(
                    db,
                    event_id=event_id,
                    auto_retention=False,  # legacy rows = never auto-purged
                    mirror_redis=False,    # avoid double-mirror on import
                    **kwargs,
                )
                imported += 1
            except Exception as exc:  # pragma: no cover — single-row safety
                logger.warning(
                    "import failed for %s (event_id=%s): %s",
                    list_key,
                    event_id,
                    exc,
                )
                unparsed += 1

    return seen, imported, unparsed


async def run(patterns: list[str], *, apply: bool) -> dict[str, Any]:
    """Top-level driver. Returns a summary dict for the CLI."""
    # Lazy-imports: the script must still parse / --help without a live
    # Redis or PG.
    from app.database import async_session_factory
    from app.redis_client import get_redis, init_redis

    await init_redis()
    r = await get_redis()

    summary = {
        "apply": apply,
        "lists": 0,
        "seen": 0,
        "imported": 0,
        "unparsed": 0,
    }

    for pattern in patterns:
        keys = await _scan_keys(r, pattern)
        for k in keys:
            seen, imported, unparsed = await _import_one_list(
                async_session_factory, r, k, apply=apply
            )
            summary["lists"] += 1
            summary["seen"] += seen
            summary["imported"] += imported
            summary["unparsed"] += unparsed
            logger.info(
                "%s: seen=%d imported=%d unparsed=%d",
                k,
                seen,
                imported,
                unparsed,
            )

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually commit to PG. Without this flag the script "
        "dry-runs (counts only).",
    )
    parser.add_argument(
        "--pattern",
        action="append",
        default=None,
        help="Redis key pattern(s) to scan. Repeatable. Defaults to the "
        "three known legacy patterns.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="DEBUG logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    patterns = list(args.pattern) if args.pattern else list(DEFAULT_PATTERNS)

    summary = asyncio.run(run(patterns, apply=args.apply))
    logger.info("summary: %s", summary)
    if not args.apply:
        logger.info("DRY-RUN — re-run with --apply to commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
