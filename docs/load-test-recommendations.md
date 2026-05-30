# KiX Load-Test Recommendations

Companion to the empirical report at `/Users/mozat/a-docs/load-test-report.md`.
That report shows *what* the load tests measured. This doc lists *what to fix*
when each pathology shows up.

The recommendations are organized by bottleneck class. Each item lists:

- **Symptom** — how it shows up in the locust CSVs / host metrics
- **Likely cause** — concrete file/table/key, not abstract advice
- **Proposed fix**
- **Estimated capacity gain** — order-of-magnitude only, validate by re-running
  the same profile after the change

## How to use this doc

1. Run `load_tests/run_tests.sh` (or just the `breaking` profile)
2. Read `/Users/mozat/a-docs/load-test-report.md`
3. For each entry in the report's "Top 10 slowest" or "highest error rate"
   tables, look up the matching section below
4. Apply the highest-leverage fix first (typically a missing index or a
   synchronous webhook handler)
5. Re-run the same profile, confirm the gain matches the estimate within a
   factor of 2

---

## A. Database (Postgres)

### A1. Missing index on `campaigns(brand_id, status, updated_at DESC)`

- **Symptom**: `GET /campaigns/[brand_id]` p95 > 400 ms even at baseline load;
  `pg_stat_activity` shows `Seq Scan on campaigns`.
- **Cause**: list endpoint scans the full campaigns table per merchant.
- **Fix**: `CREATE INDEX CONCURRENTLY idx_campaigns_brand_status_updated ON
  campaigns(brand_id, status, updated_at DESC);` then rewrite the query to
  match.
- **Gain**: 3–8× on the list endpoint, drops dashboard p95 alongside it.

### A2. Hot-row contention on `wallets.balance`

- **Symptom**: `GET /wallet/[brand_id]` p95 climbs with merchant count; PG
  `pg_locks` shows `RowExclusiveLock` on `wallets`.
- **Cause**: every charge/topup does `SELECT … FOR UPDATE` on the same row.
- **Fix**: keep authoritative balance in Redis as an `INCRBY` counter,
  reconcile to PG in a worker every N seconds, fall back to PG only on
  reconcile or dispute.
- **Gain**: 10×+ on hot brands; eliminates lock-pile-up at stress level.

### A3. Connection-pool exhaustion under thundering-herd

- **Symptom**: `HERD POST /campaigns/create` shows errors > 0.5%, log says
  `QueuePool limit … reached`.
- **Cause**: `pool_size=20` (or similar) configured in `app/database.py`;
  every concurrent campaign-create grabs a connection.
- **Fix**: front PG with **PgBouncer in transaction mode**, set app pool to
  ~5 per worker, total upstream pool ~200. Add a per-brand rate limit on
  campaign creation (e.g. 5/sec).
- **Gain**: removes the cliff; capacity scales linearly with PgBouncer pool.

### A4. Audit-log write amplification

- **Symptom**: any POST endpoint has p99 ≫ p95 (long tail), audit-log table
  growing fast.
- **Cause**: synchronous insert into `audit_log` on the request path.
- **Fix**: use FastAPI `BackgroundTasks` or push to a Redis stream consumed by
  a worker. Keep only the actor + request ID inline.
- **Gain**: cuts p99 by ~30% on write endpoints.

---

## B. Redis

### B1. Hot-key on shared campaign counter

- **Symptom**: `HOT POST /auction/report-engagement` p95 climbs with consumer
  count even though every consumer hits the same campaign; Redis `CPU` on the
  shard holding that key approaches 100%.
- **Cause**: single key like `engagement:cmp_<id>:count` is INCR'd by every
  consumer.
- **Fix**: shard the counter across N=16 keys
  (`engagement:cmp_<id>:shard_<n>`), pick shard by `kix_id` hash, sum on read.
  Refresh the read aggregate every 1–5 s into a single derived key.
- **Gain**: linear in N (16× with N=16).

### B2. Unbounded Redis memory growth

- **Symptom**: `host_*.csv` `redis_used_mb` grows monotonically through the
  stress run.
- **Cause**: keys missing TTL (commonly impression/click tokens or session
  caches).
- **Fix**: enforce `EXPIRE` on every `SET`; add a Redis-side `maxmemory-policy
  allkeys-lru` as a safety net.
- **Gain**: prevents OOM crash at sustained stress level.

### B3. Pipelined commands on shared connection

- **Symptom**: Redis-bound endpoints all degrade together (not just one).
- **Cause**: single shared connection blocks under contention.
- **Fix**: use `redis.asyncio.ConnectionPool(max_connections=50)` per worker
  in `app/redis_client.py`; reuse pipelines per request only.
- **Gain**: 2–3× on Redis-heavy endpoints under load.

---

## C. Application layer (FastAPI / workers)

### C1. Synchronous Stripe webhook processing

- **Symptom**: `WEBHOOK POST /deposits/webhook/stripe` p95 > 500 ms or err > 0.5%
  during the webhook-flood scenario.
- **Cause**: handler verifies signature, looks up brand, updates wallet, fires
  notifications — all inline before returning 200.
- **Fix**: validate signature only, push event onto a Redis stream, return
  200 immediately. Process the stream from `app/workers/`.
- **Gain**: 10× throughput; eliminates Stripe retry storms.

### C2. Pacing controller row-locks

- **Symptom**: `PACING POST /auction/report-impression` p95 climbs with brand
  count.
- **Cause**: `app/pacing_controller.py` likely takes a row-level lock on the
  campaign per spend event to decrement budget.
- **Fix**: lock-free **token bucket in Redis** (`HINCRBYFLOAT` on a
  per-campaign hash). Reconcile back to PG every N seconds in a worker.
- **Gain**: removes the bottleneck entirely; pacing becomes O(1) per event.

### C3. Synchronous fraud check on every engagement

- **Symptom**: `POST /auction/report-engagement` p95 > 250 ms.
- **Cause**: each engagement runs a multi-table fraud lookup before ACK.
- **Fix**: fire-and-forget into a worker; return ACK immediately. Use the
  fraud worker's decision asynchronously to invalidate or clawback the
  engagement reward.
- **Gain**: ~3× on engagement throughput.

### C4. Dashboard rollups computed per request

- **Symptom**: `GET /dashboards/brand/[id]` is the slowest endpoint, scales
  with campaign count per brand.
- **Cause**: SQL aggregates over all of a brand's campaign stats on the
  request path.
- **Fix**: precompute rollups in a worker on a 30–60 s cadence into Redis;
  serve with stale-while-revalidate. The dashboard tolerates 1-minute lag.
- **Gain**: 5–10×; brings p95 well under 200 ms.

---

## D. Auction subsystem

### D1. Linear candidate scan in `/auction/run`

- **Symptom**: `STORM POST /auction/run` p95 ≫ 500 ms even at moderate QPS.
- **Cause**: candidate set scanned per audience match in Python.
- **Fix**: pre-shard candidates by `(region, audience_segment)` into Redis
  ZSETs scored by bid; top-K is a `ZRANGEBYSCORE` (O(log N + K)). Re-shard
  in a worker every N seconds.
- **Gain**: 4–6× on auction throughput; sub-50 ms p95 achievable.

### D2. Reserve-price lookup on every bid

- **Symptom**: small but consistent p50 floor on `/auction/run` (~20 ms).
- **Cause**: per-bid PG lookup for current reserve price.
- **Fix**: cache reserve price in Redis with a 60 s TTL; invalidate on admin
  update.
- **Gain**: 5–10 ms off p50.

---

## E. Capacity headroom & 10K-merchant target

The marketing claim is "10K+ merchants". Treat that as a **floor** for
breaking-point measurements, not the design point:

- **Baseline**: 100 merchants + 1 000 consumers must run at p95 ≤ 200 ms
  across all endpoints.
- **Stress**: 1 000 merchants + 10 000 consumers must hold for 15 minutes
  with no SLO violation and no Redis growth slope.
- **Breaking**: must exceed **20 000 concurrent merchants** (~220 000 total
  users at 1:10 split) before any SLO breach. That gives 2× headroom over
  the claim.

If the `breaking.py` profile trips the detector below 20 000 merchants,
treat the relevant top-of-list bottleneck above as **P0**.

---

## F. Operational follow-ups

- Wire `load_tests/run_tests.sh` into the perf-environment CI so every release
  candidate gets at least the baseline profile.
- Snapshot `host_*.csv` from each run and alert when CPU > 80 %, Redis growth
  > 1 MB/min, or PG `active` connections > 80 % of pool.
- Re-run after every database migration: indexes are the most common cause of
  silent regression.
