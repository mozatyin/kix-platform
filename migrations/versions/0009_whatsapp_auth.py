"""Additive auth_method column on user_profiles — Wave E item 6.

Wave E item 6 introduces WhatsApp OTP as a first-class login path
alongside the legacy email/device-sig route. The router itself
(``app/routers/whatsapp_auth.py``) doesn't strictly need a column —
phone-verified users are stored with a deterministic ``device_sig``
prefix (``wa:<sha256-prefix>``) so the existing
``UNIQUE(brand_id, device_sig)`` constraint already lets us dedupe.

This column is the *signal* layer: dashboards / fraud / cohort
analytics need to know which method a row was created with, and
existing users may eventually have BOTH methods linked. Treating it as
an additive nullable column lets:

* legacy rows (NULL → "device_sig" default) keep working unchanged;
* new WhatsApp rows record ``auth_method = 'whatsapp'`` at insert time;
* a future link-existing flow flip a row from ``device_sig`` to
  ``hybrid`` without breaking historical analytics.

The router tolerates this column being absent (``setattr`` guard) so
the migration can ship asynchronously from the router; once this
migration applies, every new WhatsApp-verified row carries the tag.

Revision ID: 0009_whatsapp_auth
Revises: 0008_prizes
"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "0009_whatsapp_auth"
down_revision = "0008_prizes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE user_profiles
        ADD COLUMN IF NOT EXISTS auth_method VARCHAR(32)
        """
    )
    # Backfill existing rows with the legacy method label so a NULL
    # value in the column always means "unknown / pre-migration ghost"
    # rather than ambiguity with the active device-sig users.
    op.execute(
        "UPDATE user_profiles SET auth_method = 'device_sig' "
        "WHERE auth_method IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_profiles_auth_method "
        "ON user_profiles(auth_method)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_user_profiles_auth_method")
    op.execute("ALTER TABLE user_profiles DROP COLUMN IF EXISTS auth_method")
