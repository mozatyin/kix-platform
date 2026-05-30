"""Durable audit log table — PIPL §51 / GDPR Article 30 evidence chain

Why move audit out of Redis?
----------------------------
Pre-R5 the platform recorded every privileged action into a Redis LIST
(``audit:*``) capped at 1000 entries. That worked as a debug breadcrumb
but is unfit for compliance:

* Eviction is silent — once the cap is hit, oldest events drop with no
  alarm; the evidence chain breaks invisibly.
* Redis is volatile — a single ``FLUSHDB`` or instance loss erases the
  audit trail; PIPL §51 and GDPR Article 30 both require durable proof.
* Range / actor / brand queries are O(N) over a LIST.
* No retention policy hooks — every region has a different mandated
  retention horizon (sg=7y, cn=3y, eu=variable, …) and a LIST can't
  express that.

The new ``audit_log`` table is the durable spine: every privileged
mutation appends a row, retention is enforced per jurisdiction, and
admin / regulator queries hit indexed columns instead of scanning.

Schema notes
------------
* ``event_id`` is application-minted (ulid/uuid). The ``UNIQUE``
  constraint lets the migration script (and dual-write hooks) be
  idempotent — re-ingesting the same event is a no-op.
* ``payload`` is JSONB so callers can attach structured context without
  the table needing a schema bump per call site. Callers MUST scrub PII
  before persisting (service layer enforces this).
* ``retention_until`` is nullable so legacy / pre-jurisdictioned rows
  don't accidentally get purged when the cron runs. The partial index
  keeps the purge scan O(expired) not O(table).
* ``jurisdiction`` is the routing key for compliance retention policy.
  ``app/services/audit_log_service.apply_retention_policy`` reads
  ``app/compliance_regional/*`` to compute ``retention_until``.
* INET column type stores the actor IP for forensic traceability;
  callers that want to honour data-minimisation can pass ``None``.

Idempotency
-----------
Every DDL statement is guarded with ``IF NOT EXISTS`` so re-running this
migration after a partial failure is safe. The ``UNIQUE(event_id)``
constraint takes care of the data-level idempotency.

Revision ID: 0007_audit_log
Revises: 0006_i18n_collation
Create Date: 2026-05-30 00:00:00.000000+00:00
"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "0007_audit_log"
down_revision = "0006_i18n_collation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── audit_log table ───────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id              BIGSERIAL PRIMARY KEY,
            event_id        VARCHAR(64)  UNIQUE NOT NULL,
            actor_id        VARCHAR(64)  NOT NULL,
            actor_type      VARCHAR(32)  NOT NULL,
            action          VARCHAR(64)  NOT NULL,
            target_type     VARCHAR(64),
            target_id       VARCHAR(128),
            brand_id        VARCHAR(64),
            ip_address      INET,
            user_agent      TEXT,
            request_id      VARCHAR(64),
            payload         JSONB,
            result          VARCHAR(32),
            jurisdiction    VARCHAR(8),
            ts              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            retention_until TIMESTAMPTZ
        )
        """
    )

    # ── Indexes (all DESC on ts so the common "latest first" admin
    # query and CSV export hit the index directly) ───────────────────
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_actor "
        "ON audit_log(actor_id, ts DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_brand "
        "ON audit_log(brand_id, ts DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_action "
        "ON audit_log(action, ts DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_jurisdiction "
        "ON audit_log(jurisdiction, ts DESC)"
    )
    # Partial index: only rows with retention_until set occupy slot ─
    # the purge cron's WHERE retention_until < NOW() walks only those.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_retention "
        "ON audit_log(retention_until) "
        "WHERE retention_until IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_audit_retention")
    op.execute("DROP INDEX IF EXISTS idx_audit_jurisdiction")
    op.execute("DROP INDEX IF EXISTS idx_audit_action")
    op.execute("DROP INDEX IF EXISTS idx_audit_brand")
    op.execute("DROP INDEX IF EXISTS idx_audit_actor")
    op.execute("DROP TABLE IF EXISTS audit_log")
