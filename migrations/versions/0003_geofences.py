"""Geofences — PostGIS-backed spatial table

Migrates geofence storage off the single Redis ``GEOADD geofence:stores``
sorted-set onto a PostGIS R-tree GiST index so spatial queries scale to
10K+ geofences platform-wide. The application keeps writing to Redis as
well during the dual-write window; reads will switch to PG after the
backfill (``scripts/migrate_geofence_to_postgis.py``) is verified.

Revision ID: 0003_geofences
Revises: 0002_subscriptions
Create Date: 2026-05-29 00:00:00.000000+00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "0003_geofences"
down_revision = "0002_subscriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enable PostGIS once. Safe to repeat — IF NOT EXISTS short-circuits.
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    # Create the table with everything except the PostGIS geography
    # columns first; those are added via raw SQL so we don't have to
    # import geoalchemy2 in the migration environment.
    op.create_table(
        "geofences",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("brand_id", sa.String(length=128), nullable=False),
        sa.Column("store_id", sa.String(length=128), nullable=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column(
            "radius_meters",
            sa.Integer(),
            nullable=False,
            server_default="500",
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column(
            "db_created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "db_updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # PostGIS geography columns — added via raw DDL.
    op.execute(
        "ALTER TABLE geofences "
        "ADD COLUMN location geography(POINT, 4326) NOT NULL "
        "DEFAULT ST_SetSRID(ST_MakePoint(0, 0), 4326)::geography"
    )
    # Drop bootstrap default so future inserts must supply a real point.
    op.execute("ALTER TABLE geofences ALTER COLUMN location DROP DEFAULT")
    op.execute(
        "ALTER TABLE geofences "
        "ADD COLUMN polygon geography(POLYGON, 4326) NULL"
    )

    # R-tree GiST spatial index — the whole point of this migration.
    op.execute(
        "CREATE INDEX ix_geofence_location_gist "
        "ON geofences USING GIST(location)"
    )

    # Secondary indexes for brand-scoped filtering.
    op.create_index(
        "ix_geofence_brand_active",
        "geofences",
        ["brand_id", "active"],
    )
    op.create_index(
        "ix_geofences_brand_id",
        "geofences",
        ["brand_id"],
    )
    op.create_index(
        "ix_geofences_store_id",
        "geofences",
        ["store_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_geofences_store_id", table_name="geofences")
    op.drop_index("ix_geofences_brand_id", table_name="geofences")
    op.drop_index("ix_geofence_brand_active", table_name="geofences")
    op.execute("DROP INDEX IF EXISTS ix_geofence_location_gist")
    op.drop_table("geofences")
    # PostGIS extension is intentionally NOT dropped — other tables may
    # depend on it. Operators can drop manually if no longer needed.
