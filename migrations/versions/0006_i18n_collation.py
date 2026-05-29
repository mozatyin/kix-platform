"""ICU collation + GIN trigram index for multilingual sort/search

Why ICU?
--------
The default PG collation (``"default"`` → libc ``en_US.UTF-8``) does
not produce sensible orderings for mixed-script content (Chinese +
Arabic + Latin). ICU's ``und-u-ks-level2`` ("language-undefined,
case-insensitive, accent-sensitive") gives a stable cross-script
collation usable for any catalog sort.

GIN + pg_trgm
-------------
We also add a trigram GIN index on ``brand_translations.value`` so
admin search ("find every translation containing 'voucher'") stays
fast as the catalog grows. The ``pg_trgm`` extension is enabled
idempotently — operators may already have it on shared infra.

Idempotency
-----------
Every statement is guarded with ``IF NOT EXISTS`` so re-running the
migration after a partial failure (or after a DBA pre-created the
collation manually) does not error. ``CREATE COLLATION`` itself
honours ``IF NOT EXISTS`` since PG 9.5.

Revision ID: 0006_i18n_collation
Revises: 0005_i18n_user_locale_pref
Create Date: 2026-05-30 00:00:00.000000+00:00
"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "0006_i18n_collation"
down_revision = "0005_i18n_user_locale_pref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── pg_trgm extension (for GIN trigram index below) ───────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ── ICU collation for multilingual sort ───────────────────────────
    # ``deterministic = false`` is required so case/accent folding can
    # treat distinct byte-sequences as equal (e.g. "Cafe" == "café").
    # ICU may not be available in every PG build (e.g. RDS pre-13); we
    # wrap the CREATE in a DO-block that swallows the "not supported"
    # error so the migration still progresses on libc-only deployments.
    op.execute(
        """
        DO $$
        BEGIN
            CREATE COLLATION IF NOT EXISTS i18n_ci (
                provider = icu,
                locale = 'und-u-ks-level2',
                deterministic = false
            );
        EXCEPTION
            WHEN feature_not_supported THEN
                RAISE NOTICE 'ICU collation unavailable on this build; skipping';
            WHEN undefined_object THEN
                RAISE NOTICE 'ICU provider missing on this build; skipping';
        END
        $$
        """
    )

    # ── GIN trigram index on the translation values ───────────────────
    # Depends on ``brand_translations`` from 0004 + ``pg_trgm`` above.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brand_translations_value_ci "
        "ON brand_translations USING GIN (value gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_brand_translations_value_ci")
    # Collation drop is wrapped because it may not exist on libc-only
    # installs where the upgrade silently skipped it.
    op.execute(
        """
        DO $$
        BEGIN
            DROP COLLATION IF EXISTS i18n_ci;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'i18n_ci collation drop skipped: %', SQLERRM;
        END
        $$
        """
    )
    # ``pg_trgm`` is intentionally NOT dropped — other tables may use it.
