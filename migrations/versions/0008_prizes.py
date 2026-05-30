"""Prize fulfillment — instant-win pools, sweepstakes, and per-jurisdiction
legal evidence (W-9 trigger, GDPR consent timestamp, draw notification).

Why a dedicated tables pair?
----------------------------
KiX already issues *vouchers* (digital codes redeemable at participating
brands). Realtime Media / Merkle ePrize-class brand campaigns require a
different lifecycle: a finite *prize pool* with probabilistic instant-win
roll, optional sweepstakes draw, fulfillment workflow (mail / pickup /
digital), and explicit legal acknowledgment per jurisdiction.

The voucher tables (``voucher_pool`` and Redis ``voucher:*`` hashes) are
deliberately untouched — they remain the spine for digital-code-only
flows. Where a prize *is* a digital voucher, the prize_winners row
references the voucher_id via ``fulfillment_data.voucher_id`` instead of
duplicating state.

Schema notes
------------
* ``win_probability_pct`` is ``DECIMAL(5,2)`` so 0.01 .. 100.00 is
  exactly representable (no float drift on the audit trail).
* ``inventory_claimed`` is updated via ``UPDATE … SET claimed = claimed+1
  WHERE claimed < count``, which is atomic in Postgres and the source of
  truth for "no more prizes available".
* ``sweepstakes_draw_at`` nullable: instant-win and sweepstakes both use
  the same table — discriminator is ``instant_win`` boolean.
* ``fulfillment_data`` JSONB lets each ``prize_type`` (digital / mail /
  pickup / cash) attach its own payload (tracking number, voucher_id,
  pickup location, …) without a schema bump.
* ``contact_info_verified`` gates ``initiate_fulfillment`` — anti-fraud
  requires verified email/phone before any physical or cash payout.
* ``legal_acknowledgment_at`` records when the winner ticked the
  jurisdiction-specific T&C box. For US prizes >$600 the service layer
  additionally flags ``fulfillment_data.w9_required=true``; for EU
  recipients ``fulfillment_data.gdpr_consent_at`` is mandatory.
* ``jurisdiction`` is the routing key for compliance_regional rule
  application — the service layer reads this and refuses payouts when
  the jurisdiction blocks the prize_type (e.g. CN raffles).

Idempotency
-----------
Every DDL is guarded with ``IF NOT EXISTS`` so re-running the migration
after a partial failure is safe. The PK on ``prize_id`` / ``winner_id``
plus app-minted ULIDs give data-level idempotency for the service layer.

Revision ID: 0008_prizes
Revises: 0007_audit_log
Create Date: 2026-05-30 00:00:00.000000+00:00
"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "0008_prizes"
down_revision = "0007_audit_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── prizes (the pool definition) ──────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS prizes (
            prize_id              VARCHAR(64)  PRIMARY KEY,
            brand_id              VARCHAR(64)  NOT NULL,
            campaign_id           VARCHAR(64),
            name                  VARCHAR(255) NOT NULL,
            description           TEXT,
            prize_type            VARCHAR(32)  NOT NULL,
            value_cents           BIGINT,
            inventory_count       INT,
            inventory_claimed     INT          NOT NULL DEFAULT 0,
            win_probability_pct   DECIMAL(5,2),
            instant_win           BOOLEAN      NOT NULL DEFAULT FALSE,
            sweepstakes_draw_at   TIMESTAMPTZ,
            fulfillment_method    VARCHAR(32),
            legal_disclaimer      TEXT,
            jurisdiction          VARCHAR(8),
            created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
        """
    )

    # ── prize_winners (the claim log) ─────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS prize_winners (
            winner_id                VARCHAR(64)  PRIMARY KEY,
            prize_id                 VARCHAR(64)  NOT NULL,
            user_id                  VARCHAR(64)  NOT NULL,
            brand_id                 VARCHAR(64)  NOT NULL,
            won_at                   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            claim_status             VARCHAR(32)  NOT NULL DEFAULT 'pending',
            fulfillment_data         JSONB,
            contact_info_verified    BOOLEAN      NOT NULL DEFAULT FALSE,
            legal_acknowledgment_at  TIMESTAMPTZ,
            jurisdiction             VARCHAR(8),
            claim_deadline           TIMESTAMPTZ,
            claimed_at               TIMESTAMPTZ,
            shipped_at               TIMESTAMPTZ,
            delivered_at             TIMESTAMPTZ,
            expired_at               TIMESTAMPTZ
        )
        """
    )

    # ── Indexes ───────────────────────────────────────────────────────
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_prizes_brand "
        "ON prizes(brand_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_prizes_campaign "
        "ON prizes(campaign_id) WHERE campaign_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_prizes_draw_at "
        "ON prizes(sweepstakes_draw_at) WHERE sweepstakes_draw_at IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_winners_user "
        "ON prize_winners(user_id, won_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_winners_prize "
        "ON prize_winners(prize_id, won_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_winners_status "
        "ON prize_winners(claim_status, won_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_winners_brand "
        "ON prize_winners(brand_id, won_at DESC)"
    )
    # Partial index: only un-expired pending winners participate in the
    # fulfillment-queue scan; the cron's ``WHERE claim_deadline < NOW()``
    # walks only those.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_winners_deadline "
        "ON prize_winners(claim_deadline) "
        "WHERE claim_status = 'pending' AND claim_deadline IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_winners_deadline")
    op.execute("DROP INDEX IF EXISTS idx_winners_brand")
    op.execute("DROP INDEX IF EXISTS idx_winners_status")
    op.execute("DROP INDEX IF EXISTS idx_winners_prize")
    op.execute("DROP INDEX IF EXISTS idx_winners_user")
    op.execute("DROP INDEX IF EXISTS idx_prizes_draw_at")
    op.execute("DROP INDEX IF EXISTS idx_prizes_campaign")
    op.execute("DROP INDEX IF EXISTS idx_prizes_brand")
    op.execute("DROP TABLE IF EXISTS prize_winners")
    op.execute("DROP TABLE IF EXISTS prizes")
