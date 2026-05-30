"""country_slots: First-100-free-per-country mechanic (v4 P13 + pricing.html).

Backs the public commitment: each ISO country grants its first 100 merchants
0% take-rate forever (founding merchant slot). The pricing page promises this;
the slot table makes it real.

Schema:
  country_code  CHAR(2)     ISO 3166-1 alpha-2 (SG, ID, US, TZ, KH, ...)
  slot_number   INT         1..100 within country
  brand_id      VARCHAR(64) merchant that claimed the slot (NULL until taken)
  claimed_at    TIMESTAMPTZ when claim happened
  released_at   TIMESTAMPTZ if brand churns, slot is recycled (optional)
  PRIMARY KEY (country_code, slot_number)

Plus a denormalised counter view:
  country_slot_summary (country_code, slots_total, slots_claimed, slots_remaining)
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0010_country_slots"
down_revision = "0009_whatsapp_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS country_slots (
            country_code CHAR(2) NOT NULL,
            slot_number  INT     NOT NULL,
            brand_id     VARCHAR(64),
            claimed_at   TIMESTAMPTZ,
            released_at  TIMESTAMPTZ,
            PRIMARY KEY (country_code, slot_number),
            CHECK (slot_number BETWEEN 1 AND 100)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_country_slots_brand
        ON country_slots(brand_id)
        WHERE brand_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_country_slots_claimed_at
        ON country_slots(country_code, claimed_at)
        WHERE claimed_at IS NOT NULL
        """
    )
    # Seed 100 empty slots per country for the 9 launch jurisdictions +
    # the broader "explore" list. New countries auto-seed on first probe.
    for cc in (
        "SG", "ID", "TH", "VN", "PH", "MY", "HK", "TW", "CN",
        "US", "GB", "AU", "NZ", "IN", "PK", "BD", "LK",
        "JP", "KR", "AE", "SA", "EG",
        "BR", "MX", "AR", "CL",
        "DE", "FR", "ES", "IT", "NL", "BE", "PL",
        "TZ", "KH", "MM", "LA",
    ):
        op.execute(
            f"""
            INSERT INTO country_slots (country_code, slot_number)
            SELECT '{cc}', s
            FROM generate_series(1, 100) AS s
            ON CONFLICT (country_code, slot_number) DO NOTHING
            """
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_country_slots_claimed_at")
    op.execute("DROP INDEX IF EXISTS idx_country_slots_brand")
    op.execute("DROP TABLE IF EXISTS country_slots")
