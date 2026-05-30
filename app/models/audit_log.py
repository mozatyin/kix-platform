"""Audit log ORM model — durable evidence chain for PIPL §51 / GDPR Art. 30.

One row per privileged action across the platform. Source-of-truth for:

* Who did what (actor_id, actor_type, action)
* On what (target_type, target_id, brand_id)
* From where (ip_address, user_agent, request_id)
* When (ts) and how (payload, result)
* For how long (jurisdiction → retention_until)

Migrated from the volatile, cap-evicted Redis LIST (``audit:*``) to a
PostgreSQL table so the evidence chain survives Redis restarts and is
range-queryable for regulator export. See migration ``0007_audit_log``.

This model is intentionally a thin reflection of the table — all
business logic (PII scrubbing, retention computation, dual-write to
Redis for the live admin tail) lives in
``app/services/audit_log_service``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models._core import Base

# Column-type variants: on Postgres we want native JSONB + INET (so the
# migration's GIN-friendly type + IP validation work). On any other
# dialect (notably SQLite for tests) we fall back to JSON / TEXT so the
# table can be created without dialect-specific extensions.
_JSONB_VARIANT = JSONB().with_variant(JSON(), "sqlite")
_INET_VARIANT = INET().with_variant(String(45), "sqlite")


class AuditLog(Base):
    """SQLAlchemy mapping for the ``audit_log`` table.

    Columns mirror the migration's DDL one-for-one. ``Index`` entries
    here are documentation; the physical indexes are created in
    migration 0007 with explicit DESC-on-ts ordering which SQLAlchemy
    cannot express portably.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        # Document-only — physical indexes live in migration 0007.
        Index("idx_audit_actor", "actor_id", "ts"),
        Index("idx_audit_brand", "brand_id", "ts"),
        Index("idx_audit_action", "action", "ts"),
        Index("idx_audit_jurisdiction", "jurisdiction", "ts"),
    )

    # BIGSERIAL on Postgres → SQLAlchemy treats BigInteger primary keys
    # as auto-increment. On SQLite (tests) only INTEGER PRIMARY KEY
    # auto-increments, so the column type is variant-swapped at
    # table-creation time. Production DDL goes through Alembic (which
    # is unaffected — only the SQLite test path swaps).
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )

    # Application-minted ULID/UUID — UNIQUE so dual-write + migration
    # script can be idempotent (re-ingesting an event is a no-op).
    event_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )

    # ── Actor (who) ──────────────────────────────────────────────────
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # merchant / admin / system / customer

    # ── Action (what) ────────────────────────────────────────────────
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    brand_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── Forensic context (from where) ────────────────────────────────
    # INET stores IPv4/IPv6 natively; column is nullable so callers that
    # honour data-minimisation can omit it.
    ip_address: Mapped[str | None] = mapped_column(_INET_VARIANT, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── Structured context (how) ─────────────────────────────────────
    # JSONB so call sites can attach arbitrary structured context
    # without bumping the schema. Service layer scrubs PII before
    # persisting so this column is safe to expose to admin readers.
    payload: Mapped[dict[str, Any] | None] = mapped_column(
        _JSONB_VARIANT, nullable=True
    )
    result: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # ── Compliance routing ───────────────────────────────────────────
    # Drives the retention cron + admin export region filtering. Two-
    # letter ISO-style codes ("sg", "cn", "eu", …) keep alignment with
    # ``app/compliance_regional/``.
    jurisdiction: Mapped[str | None] = mapped_column(String(8), nullable=True)

    # ── Timing ───────────────────────────────────────────────────────
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # NULL → never auto-purged. Populated by
    # ``apply_retention_policy`` when the event has a known
    # jurisdiction.
    retention_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def to_dict(self) -> dict[str, Any]:
        """Serialise for JSON / CSV export. ``ts`` becomes ISO-8601."""
        return {
            "id": self.id,
            "event_id": self.event_id,
            "actor_id": self.actor_id,
            "actor_type": self.actor_type,
            "action": self.action,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "brand_id": self.brand_id,
            "ip_address": str(self.ip_address) if self.ip_address else None,
            "user_agent": self.user_agent,
            "request_id": self.request_id,
            "payload": self.payload,
            "result": self.result,
            "jurisdiction": self.jurisdiction,
            "ts": self.ts.isoformat() if self.ts else None,
            "retention_until": (
                self.retention_until.isoformat()
                if self.retention_until
                else None
            ),
        }
