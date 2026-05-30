# Monitoring Dashboard Spec

This document specifies the dashboards we need for operating KiX, the
alert thresholds, and recommended tooling. It is intentionally
tool-agnostic; concrete queries should be added per implementation.

---

## 1. Recommended tooling

| Layer | Recommendation | Why |
|-------|----------------|-----|
| Metrics | **Grafana + Prometheus** (open source) or **Datadog** (managed) | Mature, ubiquitous; pick managed if we don't have an SRE to babysit Prom. |
| Logs | **Loki** (with Grafana) or **Datadog Logs** | Stay in one ecosystem with metrics. |
| Traces | **Honeycomb** or **Tempo** | High-cardinality tracing for the LLM/AI tier where p99 matters. |
| Synthetics / uptime | **Checkly** or **Pingdom** | External probers — critical for outage detection. |
| Paging | **PagerDuty** or **Opsgenie** | Rotation + escalation. |

Default recommendation if starting fresh: **Datadog** (metrics + logs +
APM in one place) + **Checkly** (external) + **PagerDuty**. Trade
flexibility for operational simplicity until we have a dedicated SRE.

---

## 2. Top-level dashboard ("Overview")

One screen, scannable in 10 seconds. Tiles:

| Tile | Metric | Unit | Healthy range |
|------|--------|------|---------------|
| RPS (total) | `sum(rate(http_requests_total[1m]))` | req/s | varies; alert on **delta**, not absolute |
| RPS by region | same, grouped by `region` label | req/s | each region within ±20% of baseline |
| Error rate | `sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m]))` | % | < 0.5% |
| Latency p50 / p95 / p99 | histogram | ms | p99 < 800ms for API, < 2s for marketplace |
| Active users (5min) | distinct user IDs | count | trended |
| Background queue depth | per-queue depth | items | < 1000 stable; alert on growth |
| DB connections in use / max | gauge | count | < 80% of max |
| Cache hit rate | Redis hits/(hits+misses) | % | > 85% |
| LLM quota consumed | from `llm_quota_monitor.py` | % | < 90% (else `wait_if_paused`) |
| Stripe success rate | succeeded/attempted | % | > 99% |

Time range default: 1 hour. Auto-refresh: 30s.

---

## 3. Per-service dashboards

One per major service. Each should answer "is this service healthy?"
without clicking through.

### 3.1 `kix-api` (FastAPI)
- Requests by endpoint (top 20).
- Errors by endpoint + status code.
- Latency histogram per endpoint.
- Saturation: CPU + memory + goroutines/threads.
- Recent deploys (annotated overlay).

### 3.2 Workers
- Jobs/sec by queue.
- Job duration histogram.
- Retry rate.
- Dead-letter queue depth (any non-zero = page).

### 3.3 PostgreSQL
- Connections (active/idle/waiting).
- Replication lag (bytes + seconds) per replica.
- Query latency p95/p99.
- Lock waits.
- Disk usage (% of allocated).
- WAL shipping lag.

### 3.4 Redis
- Ops/sec by type (GET/SET/DEL/MULTI).
- Hit rate.
- Memory used / max.
- Evictions/sec (non-zero on hot keys is a smell).
- Connected clients.
- Sentinel quorum status.

### 3.5 Payments / Stripe
- Charges attempted/succeeded/failed.
- Latency to Stripe API.
- Webhook receive lag (now() - event.created).
- `stripe_outbox` queue depth.
- Reconciliation exceptions (daily).

### 3.6 LLM / Anthropic
- Tokens consumed per minute (in/out).
- Quota % used (from `scripts/llm_quota_monitor.py`).
- Latency p95.
- Error rate by model + error class.
- Pause events (when `wait_if_paused()` actually paused).

---

## 4. Alert thresholds & routing

Three tiers: **page** (wake someone up), **email** (look in business
hours), **ignore** (graph only).

| Signal | Threshold | Tier | Notes |
|--------|-----------|------|-------|
| 5xx rate > 1% for 5 min | sustained | **page** | Customer impact. |
| 5xx rate > 5% any 1 min | spike | **page** | Catch fast outages. |
| API p99 > 2s for 10 min | sustained | **page** | |
| Any region RPS drops > 50% vs 1h baseline | sustained 5 min | **page** | Regional outage signal. |
| DB connections > 90% of max | 5 min | **page** | Imminent saturation. |
| DB replication lag > 60s | 5 min | **page** | Failover risk. |
| Redis Sentinel quorum lost | any | **page** | Failover in progress. |
| Backup missing > 24h | any | **page** | DR posture compromised. |
| Stripe success rate < 95% | 10 min | **page** | Revenue impact. |
| Worker DLQ > 0 | any | **page** | Always investigate. |
| LLM quota > 90% | any | **page** (informational) | Auto-pause should kick in; page if it didn't. |
| Disk > 80% on any node | 1 h | **email** | Plan to expand. |
| Cache hit rate < 70% | 1 h | **email** | Investigate hot-key or eviction. |
| Deploy frequency dropped (no deploys for 7 days on master) | weekly | **email** | Process smell. |
| Cert expiring < 30 days | daily | **email** | Auto-renew should handle; verify. |
| p50 latency drifted +20% week-over-week | weekly | **email** | Slow regression. |
| Single endpoint with sudden 4xx spike | 5 min | **email** | Often a client bug. |
| Anything else interesting | n/a | **ignore** (dashboard only) | |

### Routing
- **Page** → PagerDuty → on-call SRE → escalate after 5 min ack timeout.
- **Email** → `oncall@kix.io` mailing list, triaged in daily standup.
- **Ignore** → metric exists, no notification.

### Alert hygiene
- Every page must have a runbook link in its description. No exceptions.
- Page that fires more than 3x/week without finding a real issue is
  **tuned or deleted** within 2 weeks. Alert fatigue kills response.
- New alerts go to **email** first for 2 weeks; promote to page only
  after we've seen them fire on real issues.

---

## 5. SLOs (informational)

We don't yet have formal SLOs published to customers. Internal
targets we operate to:

| Service | SLI | SLO (target) | Window |
|---------|-----|--------------|--------|
| `kix-api` availability | success rate of `/health` from external prober | 99.9% | 30 days |
| `kix-api` latency | p99 < 1s | 99% of requests | 30 days |
| Marketplace search | p95 < 800ms | 99% of requests | 30 days |
| Payments | charge attempt → terminal state < 30s | 99.5% | 30 days |

Error budget burn-rate alerts (page if we'd consume the monthly budget
in <6 hours) are recommended once we have 30 days of clean data.
