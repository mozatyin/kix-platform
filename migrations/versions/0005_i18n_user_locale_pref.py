"""User-profile i18n columns — locale / region / country / timezone

Adds the four locale-resolution columns referenced by
:mod:`app.i18n.middleware` so the request-scoped ``LanguageContext`` can
fall back from ``Accept-Language`` → ``user.locale_pref`` →
``region.primary_locale``.

Columns
-------
* ``locale_pref``    — BCP 47 tag, e.g. ``en-SG`` / ``zh-Hans-SG``.
                      Nullable; ``NULL`` means "no explicit user choice,
                      resolve from region / Accept-Language".
* ``region``         — internal region code (``sg``, ``cn``, ``us`` …),
                      indexed because pricing / compliance filters scan
                      by region.
* ``country_code``   — ISO 3166-1 alpha-2; persisted separately from
                      ``region`` because one region may serve multiple
                      countries (e.g. ``sea`` ⊇ ``SG``, ``MY``, …).
* ``timezone``       — IANA tz name (``Asia/Singapore``). Used for
                      streak rollover, daily check-in, push windows.

All ALTERs use ``ADD COLUMN IF NOT EXISTS`` so this migration is safe
to re-run on environments where some columns were applied manually.
Existing rows keep the default ``NULL`` value, so no row is broken by
the change.

Revision ID: 0005_i18n_user_locale_pref
Revises: 0004_i18n_brand_translations
Create Date: 2026-05-30 00:00:00.000000+00:00
"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "0005_i18n_user_locale_pref"
down_revision = "0004_i18n_brand_translations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL ≥9.6 supports ``ADD COLUMN IF NOT EXISTS``; we lean on
    # it for idempotency rather than introspecting ``information_schema``.
    op.execute(
        """
        ALTER TABLE user_profiles
            ADD COLUMN IF NOT EXISTS locale_pref  VARCHAR(16),
            ADD COLUMN IF NOT EXISTS region       VARCHAR(8),
            ADD COLUMN IF NOT EXISTS country_code VARCHAR(2),
            ADD COLUMN IF NOT EXISTS timezone     VARCHAR(64)
        """
    )

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_profiles_locale "
        "ON user_profiles(locale_pref)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_profiles_region "
        "ON user_profiles(region)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_user_profiles_region")
    op.execute("DROP INDEX IF EXISTS idx_user_profiles_locale")
    # ``DROP COLUMN IF EXISTS`` is also Postgres-native.
    op.execute(
        """
        ALTER TABLE user_profiles
            DROP COLUMN IF EXISTS timezone,
            DROP COLUMN IF EXISTS country_code,
            DROP COLUMN IF EXISTS region,
            DROP COLUMN IF EXISTS locale_pref
        """
    )
