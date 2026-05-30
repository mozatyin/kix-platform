"""SQLAlchemy 2.0 ORM models for KiX Platform R5."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


# ── 1. Brand Configuration ───────────────────────────────────────────────
class BrandConfig(Base):
    __tablename__ = "brand_configs"

    brand_id: Mapped[str] = mapped_column(String, primary_key=True)
    brand_name: Mapped[str] = mapped_column(String, nullable=False)
    brand_slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    config_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ── 2. User Profile ──────────────────────────────────────────────────────
class UserProfile(Base):
    __tablename__ = "user_profiles"
    __table_args__ = (
        UniqueConstraint("brand_id", "device_sig", name="uq_brand_device"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    brand_id: Mapped[str] = mapped_column(
        String, ForeignKey("brand_configs.brand_id"), nullable=False
    )
    device_sig: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    day1_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Wave E item 6 — which login path created this row. Nullable so a
    # pre-migration row read by the ORM doesn't blow up; new rows set it
    # at insert time. See migrations/versions/0009_whatsapp_auth.py.
    auth_method: Mapped[str | None] = mapped_column(
        String, nullable=True, default=None
    )


# ── 3. Voucher Pool ──────────────────────────────────────────────────────
class VoucherPool(Base):
    __tablename__ = "voucher_pool"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brand_id: Mapped[str] = mapped_column(String, nullable=False)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    tier: Mapped[str] = mapped_column(String, nullable=False)  # bronze/silver/gold
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(
        String, default="available"
    )  # available/assigned/redeemed/expired
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    redeemed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ── 4. Energy Snapshot ────────────────────────────────────────────────────
class EnergySnapshot(Base):
    __tablename__ = "energy_snapshots"

    brand_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True
    )
    balance: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now()
    )


# ── 5. Energy Transaction ────────────────────────────────────────────────
class EnergyTransaction(Base):
    __tablename__ = "energy_transactions"
    __table_args__ = (
        Index("ix_energy_tx_brand_user_created", "brand_id", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    brand_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    operation: Mapped[str] = mapped_column(
        String, nullable=False
    )  # initial/reserve/confirm/refund/qr_grant/regen/welcome_back/streak_milestone
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    metadata_json: Mapped[dict | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ── 6. Score Archive ─────────────────────────────────────────────────────
class ScoreArchive(Base):
    __tablename__ = "score_archive"
    __table_args__ = (
        Index("ix_score_brand_game_season", "brand_id", "game_id", "season_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    brand_id: Mapped[str] = mapped_column(String, nullable=False)
    game_id: Mapped[str] = mapped_column(String, nullable=False)
    season_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ── 7. Brand Location ────────────────────────────────────────────────────
class BrandLocation(Base):
    __tablename__ = "brand_locations"

    location_id: Mapped[str] = mapped_column(String, primary_key=True)
    brand_id: Mapped[str] = mapped_column(
        String, ForeignKey("brand_configs.brand_id"), nullable=False
    )
    location_name: Mapped[str] = mapped_column(String, nullable=False)
    address: Mapped[str | None] = mapped_column(String, nullable=True)
    latitude: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 7), nullable=True
    )
    longitude: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 7), nullable=True
    )
    status: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
