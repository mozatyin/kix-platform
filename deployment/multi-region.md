# KiX Multi-Region Deployment Guide

KiX is a global gamification platform. Each region is an **independent stack** for the MVP — no cross-region writes — chosen for compliance, latency, and operational simplicity.

---

## 1. Region Map

| Region Code | Name              | Cloud / Provider     | Compliance      | Primary Currency | Default Languages         |
|-------------|-------------------|----------------------|-----------------|------------------|---------------------------|
| `cn`        | China-North       | Aliyun cn-beijing    | PIPL, CSL, DSL  | CNY              | zh-CN                     |
| `id`        | Indonesia-Jakarta | AWS ap-southeast-3   | UU 27/2022 (PDP)| IDR              | id-ID, en-US              |
| `sg`        | Singapore         | AWS ap-southeast-1   | PDPA            | SGD              | en-US, zh-CN, id-ID, ms-MY|
| `us`        | US-West           | AWS us-west-2        | CCPA, COPPA     | USD              | en-US, es-US              |
| `eu`        | EU-Frankfurt      | AWS eu-central-1     | GDPR            | EUR              | en-GB, de-DE, fr-FR       |

`cn` and `eu` have **data residency required** — user PII must not leave the region.
`id`, `sg`, `us` may share aggregate (non-PII) telemetry through the SG control plane.

---

## 2. Per-Region Infrastructure

Each region runs an isolated stack:

```
                ┌────────────────────────┐
                │  nginx ingress (SSL)   │  ← Cloudflare GeoDNS
                └───────────┬────────────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
        uvicorn-1     uvicorn-2 … uvicorn-N    (HPA: 4..32 pods)
              └─────────────┬─────────────┘
                            │
         ┌──────────────────┼─────────────────────┐
         ▼                  ▼                     ▼
   Redis Cluster        PG Primary           Object Storage
   3 shards × RF 1      + 2 read replicas    (game assets, media)
                        + WAL → S3 backup
```

### Compute

| Component        | CN (launch)   | ID         | SG         | US/EU (P2)  |
|------------------|---------------|------------|------------|-------------|
| uvicorn workers  | 4 pods × 4 wk | 2 × 4 wk   | 2 × 4 wk   | 2 × 4 wk    |
| billing cron     | 1 pod         | 1 pod      | 1 pod      | 1 pod       |
| push worker      | 2 pods        | 1 pod      | 1 pod      | 1 pod       |
| centrifugo       | 2 pods        | 1 pod      | 1 pod      | 1 pod       |

### Data plane

| Component | CN              | ID/SG          | US/EU         |
|-----------|-----------------|----------------|---------------|
| Redis     | 3-node cluster  | 3-node cluster | 3-node cluster|
| Postgres  | primary + 2 RO  | primary + 1 RO | primary + 1 RO|
| Backups   | OSS hourly      | S3 hourly      | S3 hourly     |

---

## 3. DNS Routing Strategy

Cloudflare GeoDNS or AWS Route 53 geolocation routing. Anycast nameserver returns A-records based on the resolver's IP geolocation.

```
partner.letskix.com   →  GeoDNS pool
api.letskix.com       →  GeoDNS pool

China CIDRs           →  cn.partner.letskix.com   →  CN ingress
Indonesia CIDRs       →  id.partner.letskix.com   →  ID ingress
SE-Asia (default)     →  sg.partner.letskix.com   →  SG ingress
Europe                →  eu.partner.letskix.com   →  EU ingress
North America (fallback) → us.partner.letskix.com →  US ingress
```

See `dns-routing.md` for the full routing table and TTL guidance.

---

## 4. Cross-Region Data Sync

**MVP (now):** no cross-region sync. Each region owns its users. Travellers who register in two regions get two `kid_*` ids — accepted edge case.

**Phase 2:** event-driven, eventually consistent.

```
   ┌──────────┐    Redis Streams (per-region)    ┌──────────┐
   │  CN API  │──────────────────────────────────►│ Kafka MM │
   └──────────┘                                   └─────┬────┘
                                                        │
                       Topics: kid.created, wallet.tx,  │
                       brand.updated, attribution.event │
                                                        ▼
                                            ┌─────────────────────┐
                                            │ Global control plane│
                                            │ (SG) — read-only    │
                                            │ analytics + admin   │
                                            └─────────────────────┘
```

Optional Phase 3: switch to globally-distributed SQL (CockroachDB / Yugabyte) when partner cross-region needs justify the cost.

---

## 5. Failover Procedure

### 5.1 Single-pod failure

Handled by Kubernetes — HPA replaces the pod within ~30s. No operator action.

### 5.2 Region-wide outage

1. **Detect:** Cloudflare health-check on `/api/v1/health/region` fails for ≥3 polls (90s).
2. **Reroute:** GeoDNS health-check removes the down region from the pool; traffic shifts to nearest healthy region (`cn → sg`, `id → sg`, `eu → us`, `us → sg`).
3. **Degrade:** Users from the failed region see a "Service unavailable in your region" banner if redirected to a non-residency-compliant region.
4. **Recover:** When `/api/v1/health/region` returns 200 for 3 consecutive polls, GeoDNS re-adds the region.

### 5.3 Postgres primary failure

1. Patroni promotes the most up-to-date replica.
2. Application reconnects via DNS alias (`pg-primary.cn.internal`).
3. Run `alembic upgrade head` is **not** required — schema is replicated.

### 5.4 Redis cluster failure

1. Redis Sentinel promotes replicas to primaries.
2. Read traffic continues; writes are blocked for ≤10s during election.
3. uvicorn workers use exponential-backoff retry on `RedisError`.

---

## 6. Deployment Order

For a fresh region bring-up:

```bash
# 1. Provision infra (Terraform — see infra/ repo)
terraform apply -target=module.region_cn

# 2. Apply Postgres schema
DATABASE_URL=$CN_DB_URL alembic upgrade head

# 3. Apply Kubernetes manifests
kubectl apply -k deployment/k8s/overlays/cn

# 4. Verify
kubectl -n kix-cn get pods
curl https://cn.api.letskix.com/api/v1/health/region

# 5. Add to GeoDNS pool
./scripts/cloudflare-add-pool-member.sh cn
```

---

## 7. Region-aware Application Code

The `app/region.py` module is the single source of truth for region behavior. All currency, compliance, language, and payment-method decisions must call `get_region_config()`.

```python
from app.region import get_region_config, CURRENT_REGION

cfg = get_region_config()
if "CNY" not in cfg["supported_currencies"]:
    raise ValueError("CNY top-up not allowed in this region")
```

Container env per region:

```bash
KIX_REGION=cn         # cn | id | sg | us | eu
REDIS_URL_CN=...
DATABASE_URL_CN=...
```

---

## 8. Observability

Each region exposes:

- `GET /health` — liveness
- `GET /ready` — readiness (Redis + seed loaded)
- `GET /api/v1/health/pg` — Postgres + replica lag
- `GET /api/v1/health/region` — region-aware compliance/currency probe
- `GET /metrics` — Prometheus pool gauges

A Grafana dashboard per region plus a global meta-dashboard aggregating health endpoints across regions.
