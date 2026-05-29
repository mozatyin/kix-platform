"""Brand translations sidecar table — i18n storage

Implements the sidecar pattern from ``i18n-trinity-strategy.md`` §4.1.
Rather than denormalising a ``locale`` column onto every content table,
all merchant-controlled translated fields (brand name, voucher title,
recipe description, etc) live in a single ``brand_translations`` table
keyed by ``(brand_id, field_name, locale)``.

Why a sidecar?
--------------
* Most content fields stay monolingual (the merchant's primary locale
  IS the source of truth). Only ~5 fields per brand need translation.
* Adding a ``locale`` column to every table would multiply the schema
  surface area and break every existing query.
* Sidecar lookups are a single PK-hit; the cost is one extra round-trip
  per translated field, mitigated by per-request batch loaders.

Translation review pipeline
---------------------------
``auto_translated`` is set ``TRUE`` when the LLM translates a field on
behalf of the merchant; ``reviewed`` flips to ``TRUE`` only after a
human (the merchant themselves OR an admin) approves the translation.
The partial index ``idx_brand_translations_review_queue`` keeps the
admin review queue scan O(queue-size) rather than O(translations).

Revision ID: 0004_i18n_brand_translations
Revises: 0003_geofences
Create Date: 2026-05-30 00:00:00.000000+00:00
"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "0004_i18n_brand_translations"
down_revision = "0003_geofences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``IF NOT EXISTS`` everywhere so this migration is idempotent —
    # operators may re-run it after partial failures without conflicts.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS brand_translations (
            brand_id        VARCHAR(64)  NOT NULL,
            field_name      VARCHAR(64)  NOT NULL,
            locale          VARCHAR(16)  NOT NULL,
            value           TEXT         NOT NULL,
            auto_translated BOOLEAN      NOT NULL DEFAULT FALSE,
            reviewed        BOOLEAN      NOT NULL DEFAULT FALSE,
            reviewer_id     VARCHAR(64),
            created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            PRIMARY KEY (brand_id, field_name, locale)
        )
        """
    )

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brand_translations_brand "
        "ON brand_translations(brand_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brand_translations_locale "
        "ON brand_translations(locale)"
    )
    # Partial index: only rows in the review backlog occupy index space.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brand_translations_review_queue "
        "ON brand_translations(reviewed, auto_translated) "
        "WHERE reviewed = FALSE AND auto_translated = TRUE"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_brand_translations_review_queue")
    op.execute("DROP INDEX IF EXISTS idx_brand_translations_locale")
    op.execute("DROP INDEX IF EXISTS idx_brand_translations_brand")
    op.execute("DROP TABLE IF EXISTS brand_translations")
