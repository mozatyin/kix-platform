"""R5 schema - 7 tables

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2026-05-23 12:00:00.000000+00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. brand_configs
    op.create_table(
        "brand_configs",
        sa.Column("brand_id", sa.String(), primary_key=True),
        sa.Column("brand_name", sa.String(), nullable=False),
        sa.Column("brand_slug", sa.String(), unique=True, nullable=False),
        sa.Column("config_json", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(), server_default="active"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )

    # 2. user_profiles
    op.create_table(
        "user_profiles",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "brand_id",
            sa.String(),
            sa.ForeignKey("brand_configs.brand_id"),
            nullable=False,
        ),
        sa.Column("device_sig", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("day1_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("brand_id", "device_sig", name="uq_brand_device"),
    )
    op.create_index(
        "idx_user_brand_device", "user_profiles", ["brand_id", "device_sig"]
    )

    # 3. voucher_pool
    op.create_table(
        "voucher_pool",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("brand_id", sa.String(), nullable=False),
        sa.Column("code", sa.String(), unique=True, nullable=False),
        sa.Column("tier", sa.String(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.String(), server_default="available"),
        sa.Column("assigned_to", postgresql.UUID(as_uuid=True)),
        sa.Column("assigned_at", sa.DateTime(timezone=True)),
        sa.Column("redeemed_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )

    # 4. energy_snapshots
    op.create_table(
        "energy_snapshots",
        sa.Column("brand_id", sa.String(), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("balance", sa.Integer(), nullable=False),
        sa.Column(
            "snapshot_at",
            sa.DateTime(timezone=True),
            primary_key=True,
            server_default=sa.func.now(),
        ),
    )

    # 5. energy_transactions
    op.create_table(
        "energy_transactions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("brand_id", sa.String(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True)),
        sa.Column("metadata", postgresql.JSONB()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_energy_tx_user",
        "energy_transactions",
        ["brand_id", "user_id", "created_at"],
    )

    # 6. score_archive
    op.create_table(
        "score_archive",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("brand_id", sa.String(), nullable=False),
        sa.Column("game_id", sa.String(), nullable=False),
        sa.Column("season_id", sa.String(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("rank", sa.Integer()),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_score_season",
        "score_archive",
        ["brand_id", "game_id", "season_id"],
    )

    # 7. brand_locations
    op.create_table(
        "brand_locations",
        sa.Column("location_id", sa.String(), primary_key=True),
        sa.Column(
            "brand_id",
            sa.String(),
            sa.ForeignKey("brand_configs.brand_id"),
            nullable=False,
        ),
        sa.Column("location_name", sa.String(), nullable=False),
        sa.Column("address", sa.Text()),
        sa.Column("latitude", sa.Numeric(10, 7)),
        sa.Column("longitude", sa.Numeric(10, 7)),
        sa.Column("status", sa.String(), server_default="active"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("brand_locations")
    op.drop_table("score_archive")
    op.drop_table("energy_transactions")
    op.drop_table("energy_snapshots")
    op.drop_table("voucher_pool")
    op.drop_table("user_profiles")
    op.drop_table("brand_configs")
