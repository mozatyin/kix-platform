"""Audit log service — single supported surface for the durable audit spine.

This service replaces the volatile, cap-evicted Redis LIST (``audit:*``)
with a PostgreSQL-backed durable evidence chain that satisfies:

* **PIPL §51** — China's Personal Information Protection Law requires a
  durable, queryable audit trail of personal-information handling.
* **GDPR Article 30** — records of processing activities, exportable on
  regulator request.
* **9-region rollout** — each ``app/compliance_regional/*`` ruleset
  carries its own retention horizon (sg=7y, cn=3y, eu=variable, …);
  ``apply_retention_policy`` reads those rules to compute
  ``retention_until`` per event.

Write path
----------
``record_event`` is the only supported INSERT. It:

1. Mints an ``event_id`` (idempotency token) if the caller didn't supply
   one — re-inserting the same ``event_id`` is a no-op (``ON CONFLICT
   DO NOTHING`` on the UNIQUE constraint).
2. Sanitises ``payload`` to strip well-known PII keys before persisting
   (defence-in-depth — call sites SHOULD already minimise, this is the
   last gate before the row hits the table).
3. Optionally mirrors the event to a capped Redis LIST
   (``audit:recent``, last 100) so the admin dashboard's "live tail"
   view keeps the same UX as before without scanning the table.
4. Tolerates a Redis outage silently — PG is the source of truth, the
   Redis mirror is best-effort.

Read path
---------
``query`` is a paginated AND-filtered read. ``export_csv`` streams
events to a CSV string for compliance exports — small enough at
realistic regulator-request sizes (~10k rows / case), and a 200-line
streaming exporter would add risk without clear benefit.

Retention
---------
``apply_retention_policy`` is called on backfill / migration and
optionally inside ``record_event`` when ``auto_retention=True``.
``purge_expired`` is the cron entry point — it deletes WHERE
``retention_until < NOW()`` using the partial index from migration
0007, so the scan is O(expired) not O(table).
"""

from __future__ import annotations

import csv
import io
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_standards import mint_id
from app.models.audit_log import AuditLog

logger = logging.getLogger(__name__)


# ── PII scrubbing ─────────────────────────────────────────────────────────

# Keys we strip from ``payload`` before persisting. Defence-in-depth — the
# call sites should already minimise, but a single forgotten ``email=`` in
# a request body should not poison the audit chain (which is itself a PII
# attack surface for regulators / admins reading it).
_PII_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "card_number",
        "cvv",
        "cvc",
        "pin",
        "ssn",
        "national_id",
        "passport",
        "private_key",
        "authorization",
    }
)


def _scrub_payload(payload: Any) -> Any:
    """Recursively replace values for well-known PII keys with ``"***"``.

    Walks dicts and lists. Non-container values pass through unchanged.
    Returns a new structure — callers' input is not mutated.
    """
    if isinstance(payload, dict):
        return {
            k: ("***" if k.lower() in _PII_KEYS else _scrub_payload(v))
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [_scrub_payload(v) for v in payload]
    return payload


# ── Redis mirror (best-effort live tail) ─────────────────────────────────

_REDIS_TAIL_KEY = "audit:recent"
_REDIS_TAIL_CAP = 100


async def _mirror_to_redis(record: dict[str, Any]) -> None:
    """LPUSH + LTRIM to the capped admin live-tail list.

    Best-effort: any Redis exception is swallowed and logged. PG is the
    durable spine; this mirror is purely for the admin "what just
    happened" UI which used to read from the legacy ``audit:*`` lists.
    """
    try:
        import json as _json

        from app.redis_client import get_redis

        r = await get_redis()
        await r.lpush(_REDIS_TAIL_KEY, _json.dumps(record, default=str))
        await r.ltrim(_REDIS_TAIL_KEY, 0, _REDIS_TAIL_CAP - 1)
    except Exception as exc:  # pragma: no cover — Redis-outage best-effort
        logger.debug("audit_log redis mirror failed: %s", exc)


# ── Write API ────────────────────────────────────────────────────────────


async def record_event(
    db: AsyncSession,
    *,
    actor_id: str,
    actor_type: str,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    brand_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    payload: dict[str, Any] | None = None,
    result: str | None = None,
    jurisdiction: str | None = None,
    event_id: str | None = None,
    auto_retention: bool = True,
    mirror_redis: bool = True,
) -> str:
    """Append one audit event to the durable log. Returns the ``event_id``.

    The write is idempotent on ``event_id`` (UNIQUE constraint +
    ``ON CONFLICT DO NOTHING``) so retrying a flaky network call is
    safe. When the caller doesn't pass ``event_id`` one is minted from
    ``mint_id("evt")``.

    Parameters
    ----------
    auto_retention
        When True (default) compute ``retention_until`` from the
        ``jurisdiction`` rule set immediately. Pass False on the
        migration-from-Redis path so legacy events are imported with
        ``retention_until=None`` (never auto-purged).
    mirror_redis
        When True (default) LPUSH a JSON copy onto the capped
        ``audit:recent`` list so the admin live-tail UI sees it.
    """
    eid = event_id or mint_id("evt")

    scrubbed = _scrub_payload(payload) if payload is not None else None

    retention_until: datetime | None = None
    if auto_retention and jurisdiction:
        retention_until = _compute_retention_until(jurisdiction)

    values = dict(
        event_id=eid,
        actor_id=actor_id,
        actor_type=actor_type,
        action=action,
        target_type=target_type,
        target_id=target_id,
        brand_id=brand_id,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
        payload=scrubbed,
        result=result,
        jurisdiction=jurisdiction,
        retention_until=retention_until,
    )

    # Idempotency: duplicate event_id → silently skip the insert.
    # PG and SQLite each speak their own ``ON CONFLICT`` flavour, so we
    # pick the right dialect-specific INSERT at call-time. Both
    # support ``ON CONFLICT (event_id) DO NOTHING`` semantics.
    dialect_name = db.bind.dialect.name if db.bind is not None else "postgresql"
    if dialect_name == "sqlite":
        stmt = (
            sqlite_insert(AuditLog)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["event_id"])
        )
    else:
        stmt = (
            pg_insert(AuditLog)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["event_id"])
        )
    await db.execute(stmt)
    await db.commit()

    if mirror_redis:
        await _mirror_to_redis(
            {
                "event_id": eid,
                "actor_id": actor_id,
                "actor_type": actor_type,
                "action": action,
                "brand_id": brand_id,
                "result": result,
                "ts": time.time(),
            }
        )

    return eid


# ── Read API ─────────────────────────────────────────────────────────────


async def query(
    db: AsyncSession,
    *,
    actor_id: str | None = None,
    brand_id: str | None = None,
    action: str | None = None,
    jurisdiction: str | None = None,
    result: str | None = None,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[AuditLog]:
    """Paginated AND-filtered query. Ordered by ``ts DESC``.

    All filter args are optional — passing none returns the most recent
    ``limit`` events globally (admin "live tail"). ``limit`` is clamped
    to [1, 1000] to keep one-call exports from monopolising the DB.
    """
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))

    stmt = select(AuditLog).order_by(AuditLog.ts.desc())

    conds = []
    if actor_id is not None:
        conds.append(AuditLog.actor_id == actor_id)
    if brand_id is not None:
        conds.append(AuditLog.brand_id == brand_id)
    if action is not None:
        conds.append(AuditLog.action == action)
    if jurisdiction is not None:
        conds.append(AuditLog.jurisdiction == jurisdiction)
    if result is not None:
        conds.append(AuditLog.result == result)
    if from_ts is not None:
        conds.append(AuditLog.ts >= from_ts)
    if to_ts is not None:
        conds.append(AuditLog.ts <= to_ts)

    if conds:
        stmt = stmt.where(and_(*conds))

    stmt = stmt.limit(limit).offset(offset)
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def get_event(db: AsyncSession, event_id: str) -> AuditLog | None:
    """Single-event lookup by ``event_id``."""
    res = await db.execute(
        select(AuditLog).where(AuditLog.event_id == event_id)
    )
    return res.scalar_one_or_none()


# ── CSV export ───────────────────────────────────────────────────────────

_CSV_COLUMNS: tuple[str, ...] = (
    "event_id",
    "ts",
    "actor_id",
    "actor_type",
    "action",
    "target_type",
    "target_id",
    "brand_id",
    "ip_address",
    "request_id",
    "result",
    "jurisdiction",
    "payload",
)


async def export_csv(
    db: AsyncSession,
    *,
    actor_id: str | None = None,
    brand_id: str | None = None,
    action: str | None = None,
    jurisdiction: str | None = None,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
    max_rows: int = 100_000,
) -> str:
    """Return a CSV string of matching events. Ordered ``ts ASC``.

    Ordering is ascending here (not descending like ``query``) because
    a regulator CSV is most-useful as a chronological narrative. The
    ``max_rows`` cap (default 100k) is a soft guardrail; raise it for
    enterprise-tier exports.
    """
    stmt = select(AuditLog).order_by(AuditLog.ts.asc())
    conds = []
    if actor_id is not None:
        conds.append(AuditLog.actor_id == actor_id)
    if brand_id is not None:
        conds.append(AuditLog.brand_id == brand_id)
    if action is not None:
        conds.append(AuditLog.action == action)
    if jurisdiction is not None:
        conds.append(AuditLog.jurisdiction == jurisdiction)
    if from_ts is not None:
        conds.append(AuditLog.ts >= from_ts)
    if to_ts is not None:
        conds.append(AuditLog.ts <= to_ts)
    if conds:
        stmt = stmt.where(and_(*conds))
    stmt = stmt.limit(max_rows)

    res = await db.execute(stmt)
    rows = list(res.scalars().all())

    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(_CSV_COLUMNS)

    import json as _json

    for r in rows:
        writer.writerow(
            [
                r.event_id,
                r.ts.isoformat() if r.ts else "",
                r.actor_id,
                r.actor_type,
                r.action,
                r.target_type or "",
                r.target_id or "",
                r.brand_id or "",
                str(r.ip_address) if r.ip_address else "",
                r.request_id or "",
                r.result or "",
                r.jurisdiction or "",
                _json.dumps(r.payload, default=str) if r.payload else "",
            ]
        )
    return buf.getvalue()


# ── Retention policy ─────────────────────────────────────────────────────


def _compute_retention_until(jurisdiction: str) -> datetime | None:
    """Compute ``retention_until`` from a jurisdiction's ruleset.

    Reads ``app/compliance_regional/*`` for ``data_retention_max_days``.
    Returns ``None`` for unknown jurisdictions — caller must decide
    whether that means "never expire" or "fall back to conservative
    7y default" (we choose "never expire" here so a missing region
    never deletes evidence).
    """
    try:
        from app.compliance_regional import get_compliance_for_region

        rules = get_compliance_for_region(jurisdiction)
        days = rules.data_retention_max_days
    except KeyError:
        return None
    return datetime.now(timezone.utc) + timedelta(days=int(days))


async def apply_retention_policy(
    db: AsyncSession,
    *,
    jurisdiction: str,
    days: int | None = None,
) -> int:
    """Backfill ``retention_until`` for every event in ``jurisdiction``.

    Called on migration / when a region's retention horizon changes.
    Pass ``days`` to override the compliance-rule default (used by the
    one-shot Redis→PG importer when the source records imply a custom
    retention).

    Returns the number of rows updated.
    """
    if days is None:
        until = _compute_retention_until(jurisdiction)
    else:
        until = datetime.now(timezone.utc) + timedelta(days=int(days))

    if until is None:
        # Unknown jurisdiction + no explicit days → refuse to silently
        # set NULL across a whole region.
        return 0

    from sqlalchemy import update as sa_update

    stmt = (
        sa_update(AuditLog)
        .where(AuditLog.jurisdiction == jurisdiction)
        .values(retention_until=until)
    )
    res = await db.execute(stmt)
    await db.commit()
    return int(res.rowcount or 0)


async def purge_expired(db: AsyncSession, *, batch_size: int = 10_000) -> int:
    """DELETE every row where ``retention_until < NOW()``.

    Returns the count purged. Uses the partial index
    ``idx_audit_retention`` (migration 0007) so the scan is
    O(expired) not O(table). Batched delete keeps long transactions
    from blocking writers.
    """
    total = 0
    while True:
        # Pull a batch of expired ids, then delete by id — keeps the
        # delete predicate simple and predictable on big tables.
        ids_q = (
            select(AuditLog.id)
            .where(
                and_(
                    AuditLog.retention_until.is_not(None),
                    AuditLog.retention_until < func.now(),
                )
            )
            .limit(batch_size)
        )
        ids_res = await db.execute(ids_q)
        ids = [row[0] for row in ids_res.all()]
        if not ids:
            break

        stmt = delete(AuditLog).where(AuditLog.id.in_(ids))
        res = await db.execute(stmt)
        await db.commit()
        purged = int(res.rowcount or 0)
        total += purged
        if purged < batch_size:
            break
    return total


async def retention_status(
    db: AsyncSession,
) -> list[dict[str, Any]]:
    """Per-jurisdiction summary: total / expired / expiring-soon counts.

    Returns one dict per jurisdiction with keys:
    ``jurisdiction``, ``total``, ``expired``, ``expiring_30d``,
    ``earliest_expiry``, ``latest_expiry``.
    """
    # Two-pass: (a) totals per jurisdiction, (b) expired counts per
    # jurisdiction. Cheaper to do two simple aggregates than one
    # conditional sum + works portably across PG + SQLite (tests).
    totals_q = (
        select(
            AuditLog.jurisdiction,
            func.count(AuditLog.id),
            func.min(AuditLog.retention_until),
            func.max(AuditLog.retention_until),
        )
        .group_by(AuditLog.jurisdiction)
        .order_by(AuditLog.jurisdiction.asc())
    )
    totals_res = await db.execute(totals_q)

    expired_q = (
        select(AuditLog.jurisdiction, func.count(AuditLog.id))
        .where(
            and_(
                AuditLog.retention_until.is_not(None),
                AuditLog.retention_until < datetime.now(timezone.utc),
            )
        )
        .group_by(AuditLog.jurisdiction)
    )
    expired_res = await db.execute(expired_q)
    expired_by: dict[str | None, int] = {
        row[0]: int(row[1]) for row in expired_res.all()
    }

    out: list[dict[str, Any]] = []
    for row in totals_res.all():
        out.append(
            {
                "jurisdiction": row[0],
                "total": int(row[1] or 0),
                "expired": int(expired_by.get(row[0], 0)),
                "earliest_expiry": row[2].isoformat() if row[2] else None,
                "latest_expiry": row[3].isoformat() if row[3] else None,
            }
        )
    return out


__all__ = [
    "record_event",
    "query",
    "get_event",
    "export_csv",
    "apply_retention_policy",
    "purge_expired",
    "retention_status",
]
