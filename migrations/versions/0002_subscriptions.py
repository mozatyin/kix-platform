"""Subscription state — durable PostgreSQL tables

Adds two tables that absorb the Redis ``brand:{bid}:subscription`` HASH
and ``brand:{bid}:subscription:history`` LIST so billing state survives
Redis restarts. Includes an index on ``next_charge_at`` so the billing
cron can replace its full ``SCAN brand:*:subscription`` with an indexed
range query — O(N) → O(log N).

Revision ID: 0002_subscriptions
Revises: a1b2c3d4e5f6
Create Date: 2026-05-29 00:00:00.000000+00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "0002_subscriptions"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brand_subscriptions",
        sa.Column("brand_id", sa.String(length=128), primary_key=True),
        sa.Column(
            "tier",
            sa.String(length=32),
            nullable=False,
            server_default="free",
        ),
        sa.Column(
            "billing",
            sa.String(length=16),
            nullable=False,
            server_default="monthly",
        ),
        sa.Column("started_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("next_charge_at", sa.BigInteger(), nullable=False),
        sa.Column(
            "auto_renew",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("payment_method_id", sa.String(length=64), nullable=True),
        sa.Column(
            "first_year_free",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "cancel_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("pending_tier", sa.String(length=32), nullable=True),
        sa.Column("pending_effective_at", sa.BigInteger(), nullable=True),
        sa.Column("renew_to_tier", sa.String(length=32), nullable=True),
        sa.Column(
            "dunning_state",
            sa.String(length=32),
            nullable=False,
            server_default="none",
        ),
        sa.Column(
            "dunning_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("dunning_grace_until", sa.BigInteger(), nullable=True),
        sa.Column("dunning_reason", sa.String(length=128), nullable=True),
        sa.Column("last_charged_at", sa.BigInteger(), nullable=True),
        sa.Column(
            "last_charge_amount_cents", sa.Integer(), nullable=True
        ),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_brand_subscriptions_brand_id",
        "brand_subscriptions",
        ["brand_id"],
    )
    op.create_index(
        "ix_brand_subscriptions_tier",
        "brand_subscriptions",
        ["tier"],
    )
    op.create_index(
        "ix_brand_subscriptions_next_charge_at",
        "brand_subscriptions",
        ["next_charge_at"],
    )
    op.create_index(
        "ix_brand_sub_due",
        "brand_subscriptions",
        ["next_charge_at", "tier", "auto_renew"],
    )

    op.create_table(
        "subscription_history",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "brand_id",
            sa.String(length=128),
            sa.ForeignKey("brand_subscriptions.brand_id"),
            nullable=False,
        ),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("from_tier", sa.String(length=32), nullable=True),
        sa.Column("to_tier", sa.String(length=32), nullable=True),
        sa.Column("charge_amount_cents", sa.Integer(), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("ts", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_subscription_history_brand_id",
        "subscription_history",
        ["brand_id"],
    )
    op.create_index(
        "ix_subscription_history_ts",
        "subscription_history",
        ["ts"],
    )
    op.create_index(
        "ix_sub_history_brand_ts",
        "subscription_history",
        ["brand_id", "ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_sub_history_brand_ts", table_name="subscription_history")
    op.drop_index(
        "ix_subscription_history_ts", table_name="subscription_history"
    )
    op.drop_index(
        "ix_subscription_history_brand_id",
        table_name="subscription_history",
    )
    op.drop_table("subscription_history")
    op.drop_index("ix_brand_sub_due", table_name="brand_subscriptions")
    op.drop_index(
        "ix_brand_subscriptions_next_charge_at",
        table_name="brand_subscriptions",
    )
    op.drop_index(
        "ix_brand_subscriptions_tier", table_name="brand_subscriptions"
    )
    op.drop_index(
        "ix_brand_subscriptions_brand_id", table_name="brand_subscriptions"
    )
    op.drop_table("brand_subscriptions")
