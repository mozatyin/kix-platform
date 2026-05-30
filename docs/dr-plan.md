# KiX Disaster Recovery Plan

**Owner:** SRE / Tech Lead
**Last reviewed:** 2026-05-30
**Status:** v1 — to be exercised quarterly

This document is the master DR plan for the KiX platform. It governs how we
recover from regional outages, infrastructure failures, third-party provider
outages, and data loss events. Runbooks for specific scenarios live under
`docs/runbooks/`.

---

## 1. Objectives

| Class       | RTO (Recovery Time) | RPO (Data Loss)  |
|-------------|---------------------|------------------|
| Stateless (web/API, workers) | **1 hour** | 0 (re-derivable) |
| Stateful (PG, Redis, S3)     | **4 hours** | **1 hour** |
| Customer-visible read paths  | 15 minutes (cached) | n/a |
| Payment/Stripe writes        | 2 hours (queued)    | 0 (no charge lost) |

If any objective above cannot be met for a given component, mark it RED in
section 4 and open a remediation ticket.

---

## 2. Reality Check (2026-05-30)

KiX **claims** multi-region (CN / ID / SG) via `docker-compose.*.yml`, but in
production we currently operate **one Kubernetes cluster**. This plan is
written to that reality. Multi-region active/active is on the roadmap; until
then, "failover" means standing up the standby region from backups + DNS
flip, which is a **cold-DR** posture with a 4-hour RTO.

---

## 3. Roles

| Role | Responsibility | Who (rotation) |
|------|----------------|----------------|
| **Incident Commander (IC)** | Owns the incident end-to-end. Calls SEVs, declares all-clear. | On-call SRE primary |
| **Tech Lead (TL)** | Diagnoses + executes the fix. Reads runbooks aloud. | Service owner on rotation |
| **Communications (Comms)** | Customer-facing updates, status page, internal Slack | Support lead on call |
| **Scribe** | Timeline + decisions log in incident channel | Anyone available |

One person can hold multiple roles for SEV3. SEV1 requires distinct IC + TL.

---

## 4. Component Catalog

### 4.1 PostgreSQL (primary OLTP)
- **Hosted:** managed PG 15 on cloud provider (single region SG).
- **Backups:** continuous WAL archiving to S3 + nightly base backup at 02:00 SGT.
- **Retention:** 30 days PITR, 90 days base backup.
- **Restore procedure:** `docs/runbooks/db-failover.md`.
- **Last tested:** NOT YET — schedule first drill within 30 days.
- **RTO/RPO:** 4h / 5min (WAL ships every 60s).

### 4.2 Redis
- **Hosted:** managed Redis 7 with Sentinel, 3 nodes.
- **Backups:** RDB snapshot every 6h to S3. AOF on.
- **Restore procedure:** `docs/runbooks/redis-failover.md`.
- **Last tested:** NOT YET.
- **RTO/RPO:** 30min / 1h. **Cache is rebuildable**; AOF protects sessions.

### 4.3 Stripe (payments)
- **Provider SLO:** 99.99%.
- **Our mitigation:** charge intents are persisted in PG with state machine
  before calling Stripe; Stripe webhook retries handled idempotently.
- **Outage runbook:** `docs/runbooks/stripe-outage.md`.
- **RTO/RPO:** depends on Stripe; we queue and replay.

### 4.4 Anthropic API (LLM)
- **Provider SLO:** best-effort.
- **Mitigation:** `wait_if_paused()` quota guard, retry with exponential
  backoff. LLM features degrade to template fallbacks.
- **Outage runbook:** treat as non-critical, see `incident-response.md`.

### 4.5 S3 (object storage)
- **Backups:** cross-region replication enabled to `ap-southeast-1` mirror.
- **Restore procedure:** `aws s3 sync` from mirror bucket.
- **RTO/RPO:** 1h / 15min.

### 4.6 Application tier (FastAPI, workers)
- **Stateless.** Recovered by re-deploying the image to standby region or
  re-creating the cluster. No data to restore.

---

## 5. Backup Verification

Backups that have not been restored are **not backups**. We verify weekly:

- `scripts/backup_verify.sh` runs nightly via cron at 03:30 SGT.
- It pulls the most recent base backup from S3, restores to a throwaway
  PG instance, runs `pg_amcheck` + schema checksum, and emails ops on
  failure.
- Missing backup for >24h **pages on-call**.

---

## 6. Communication Plan

| Trigger | Channel | Audience | SLA |
|---------|---------|----------|-----|
| SEV1 declared | Status page banner + email blast | All customers | 15 min |
| SEV2 declared | Status page banner | Affected segment | 30 min |
| SEV3 declared | Internal Slack only | Eng | n/a |
| Resolution    | Status page update + post-mortem link | All customers | 24h post-incident |
| Post-mortem published | Blog + email | Affected customers | within 5 business days |

Pre-approved customer templates: `docs/runbooks/incident-response.md` §5.

---

## 7. Drill Schedule

| Drill | Frequency | Owner |
|-------|-----------|-------|
| PG restore-from-backup | Quarterly | DB owner |
| Redis failover | Quarterly | Cache owner |
| SG region cold-start in standby | Semi-annual | SRE |
| Tabletop incident (SEV1) | Quarterly | IC rotation |
| `scripts/failover_drill.py` (dev) | Weekly via CI | SRE |

Outcomes (MTTR observed, what broke, what we learned) tracked in
`docs/dr-drill-log.md` (created at first drill).

---

## 8. Out of Scope

- True active/active multi-region (planned, not in scope here).
- Byzantine failure / nation-state actor (security plan, separate doc).
- Data exfiltration response (see `SECURITY.md`).
