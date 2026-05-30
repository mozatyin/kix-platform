# Runbook: PostgreSQL Failover

**Symptom:** Primary PG unreachable, write latency >30s, or replication
lag exploded. App returning 500s on writes.

**Severity:** SEV1 if writes are blocked, SEV2 if read-only mode still
works.

---

## 1. Confirm primary is actually down

Do not promote on a flap. Confirm twice from two locations.

```bash
# From bastion
psql -h pg-primary.kix.internal -U sre -c 'SELECT 1;'

# From a worker pod
kubectl exec -n kix deploy/worker-default -- \
  psql "$DATABASE_URL" -c 'SELECT now();'
```

If both fail and provider console shows the instance unreachable for
>5 min, proceed. If primary is up but lagging, see §6 (lag-only).

## 2. Stop writes (fence)

Prevent split-brain. Put the app in read-only mode:

```bash
kubectl scale -n kix deploy/kix-api --replicas=0
kubectl scale -n kix deploy/worker-default --replicas=0
# Or use feature flag if implemented:
# kubectl set env deploy/kix-api READ_ONLY=true
```

Comms posts banner: "Maintenance in progress — read-only mode."

## 3. Promote the replica

```bash
# Identify the most-caught-up replica
psql -h pg-replica-1.kix.internal -U sre \
  -c "SELECT pg_last_wal_replay_lsn(), pg_is_in_recovery();"

psql -h pg-replica-2.kix.internal -U sre \
  -c "SELECT pg_last_wal_replay_lsn(), pg_is_in_recovery();"
```

Pick the replica with the highest LSN. Promote:

```bash
# Managed PG: use provider CLI
gcloud sql instances promote-replica pg-replica-1 --quiet
# OR (self-managed):
ssh pg-replica-1 'sudo -u postgres pg_ctl promote -D /var/lib/postgresql/15/main'
```

Verify it became primary:

```bash
psql -h pg-replica-1.kix.internal -U sre -c "SELECT pg_is_in_recovery();"
# Expect: f
```

## 4. Update connection string

The app reads `DATABASE_URL` from the K8s secret. Update it:

```bash
kubectl -n kix create secret generic pg-conn \
  --from-literal=DATABASE_URL="postgresql://kix_app:$PG_PASSWORD@pg-replica-1.kix.internal:5432/kix?sslmode=require" \
  --dry-run=client -o yaml | kubectl apply -f -

# Force pod restart so new env is picked up
kubectl rollout restart -n kix deploy/kix-api
kubectl rollout restart -n kix deploy/worker-default
```

Scale back up:

```bash
kubectl scale -n kix deploy/kix-api --replicas=6
kubectl scale -n kix deploy/worker-default --replicas=4
```

## 5. Verify data consistency post-failover

Three checks; all must pass before declaring all-clear.

1. **Row counts on critical tables:**
   ```bash
   psql "$DATABASE_URL" -f scripts/sql/dr_rowcount_check.sql
   ```
   Compare against last hour's `dr_rowcount_baseline` (cron snapshots
   into a `_dr_baseline` table every 15 min).
2. **No orphaned FKs:**
   ```bash
   psql "$DATABASE_URL" -f scripts/sql/fk_integrity_check.sql
   ```
3. **Most-recent write replayed.** Take 5 recent order IDs from
   application logs pre-incident, confirm they exist:
   ```bash
   psql "$DATABASE_URL" -c "SELECT id, status FROM orders WHERE id IN (...);"
   ```

If any check fails, **do not declare all-clear**. Escalate to TL — may
need point-in-time-restore (see §7).

## 6. Lag-only scenario (primary alive, replica behind)

If replication lag is the issue and primary is alive:

- Do NOT promote. Investigate WAL receiver / network on replica.
- App can keep running on primary; reads of stale replicas can be
  switched to primary via `READS_TO_PRIMARY=true` env var.

## 7. Rollback (promotion was wrong)

If we promoted prematurely (primary actually came back):

1. **Stop writes immediately** (re-scale to 0 as in §2).
2. Compare LSN: if original primary is ahead, point app back at it and
   demote the promoted replica back to read-only:
   ```bash
   psql -h pg-replica-1.kix.internal -U sre -c "CHECKPOINT;"
   # Then resync from original primary via pg_basebackup
   ```
3. If the promoted replica accepted writes that are NOT on the original
   primary → we have divergence. **Stop. Page the database SME.** Do
   not auto-merge. Options:
   - Replay writes from logs onto original primary manually.
   - Accept divergence, treat promoted as new primary (revert §4).
4. Document the divergence window in the post-mortem.

## 8. Rebuild the old primary as replica

Once incident is over and we want HA back:

```bash
# On the demoted node
sudo systemctl stop postgresql
sudo -u postgres pg_basebackup -h pg-replica-1.kix.internal \
  -D /var/lib/postgresql/15/main -U replicator -Fp -P -R
sudo systemctl start postgresql
```

Verify it's catching up:
```bash
psql -h pg-replica-1.kix.internal -U sre \
  -c "SELECT client_addr, state, sync_state FROM pg_stat_replication;"
```
