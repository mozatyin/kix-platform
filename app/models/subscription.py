"""Brand subscription ORM models.

Migrated from Redis HASH (``brand:{bid}:subscription``) to PostgreSQL so
billing state survives Redis restarts. Source-of-truth for:

* tier / billing cadence / next_charge_at
* dunning state machine (none → grace → downgraded)
* cancel-at-period-end flag
* pending downgrade scheduling
* per-event audit history (SubscriptionHistory)

The billing cron now queries ``next_charge_at`` via an indexed predicate
instead of doing a full ``SCAN brand:*:subscription`` over Redis — O(log
N) instead of O(N) at scale.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models._core import Base


class BrandSubscription(Base):
    """One row per brand. Single source of truth for the tier / billing
    lifecycle. Mirrors the Redis HASH shape so legacy callers can keep
    reading from Redis during the dual-write migration window.
    """

    __tablename__ = "brand_subscriptions"
    __table_args__ = (
        Index(
            "ix_brand_sub_due",
            "next_charge_at",
            "tier",
            "auto_renew",
        ),
    )

    brand_id: Mapped[str] = mapped_column(
        String(128), primary_key=True, index=True
    )
    tier: Mapped[str] = mapped_column(
        String(32), nullable=False, default="free", index=True
    )
    billing: Mapped[str] = mapped_column(
        String(16), nullable=False, default="monthly"
    )
    started_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    next_charge_at: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True
    )
    auto_renew: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    payment_method_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    first_year_free: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    cancel_pending: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    pending_tier: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    pending_effective_at: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    renew_to_tier: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    dunning_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="none"
    )  # none / grace / downgraded
    dunning_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    dunning_grace_until: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    dunning_reason: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    last_charged_at: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    last_charge_amount_cents: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    metadata_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def to_dict(self) -> dict:
        """Render this row as the same dict shape the Redis HASH produced.

        Keeps API contract identical so the portal does not need to know
        the storage layer changed.
        """
        return {
            "brand_id": self.brand_id,
            "tier": self.tier,
            "billing": self.billing,
            "started_at": float(self.started_at) if self.started_at else None,
            "expires_at": float(self.expires_at) if self.expires_at else None,
            "next_charge_at": (
                float(self.next_charge_at) if self.next_charge_at else None
            ),
            "auto_renew": bool(self.auto_renew),
            "payment_method_id": self.payment_method_id or None,
            "first_year_free": bool(self.first_year_free),
            "cancel_pending": bool(self.cancel_pending),
            "pending_tier": self.pending_tier,
            "pending_effective_at": (
                float(self.pending_effective_at)
                if self.pending_effective_at
                else None
            ),
            "renew_to_tier": self.renew_to_tier,
            "dunning_state": self.dunning_state,
            "dunning_attempts": int(self.dunning_attempts or 0),
            "dunning_grace_until": (
                float(self.dunning_grace_until)
                if self.dunning_grace_until
                else None
            ),
            "dunning_reason": self.dunning_reason,
            "last_charged_at": (
                float(self.last_charged_at) if self.last_charged_at else None
            ),
            "last_charge_amount_cents": self.last_charge_amount_cents,
        }


class SubscriptionHistory(Base):
    """Append-only audit log for every tier-affecting event.

    UPGRADE / DOWNGRADE / CANCEL / RENEWAL / DUNNING_START /
    DUNNING_REMINDER / DOWNGRADE_TO_FREE / AUTO_RENEW_CONFIG /
    FREE_TRIAL_3MO / AUTO_RENEW_SUCCESS / AUTO_RENEW_FREE_CYCLE.
    """

    __tablename__ = "subscription_history"
    __table_args__ = (
        Index("ix_sub_history_brand_ts", "brand_id", "ts"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    brand_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("brand_subscriptions.brand_id"),
        index=True,
        nullable=False,
    )
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    from_tier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_tier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    charge_amount_cents: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    metadata_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "brand_id": self.brand_id,
            "event": self.event,
            "from_tier": self.from_tier,
            "to_tier": self.to_tier,
            "charge_amount_cents": self.charge_amount_cents,
            "ts": float(self.ts) if self.ts else None,
            **(self.metadata_json or {}),
        }
