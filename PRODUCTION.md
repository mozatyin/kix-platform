# KiX Production Deployment Guide

This document captures everything needed to take KiX Platform from a fresh
Linux box to a production-grade deployment serving real merchants.

## Prerequisites

- Linux server (Ubuntu 22.04+ recommended; Debian 12 also fine)
- Python 3.12+
- Redis 7+ with persistence (AOF `appendonly yes` + nightly RDB snapshot)
- PostgreSQL 15+
- nginx (TLS termination + static asset cache)
- SSL certificate (Let's Encrypt via certbot)
- DNS for: `partner.letskix.com` / `api.letskix.com` / `letskix.com`

## Environment Variables

The app reads config from environment (`app/config.py`). Keep secrets out
of git — use `EnvironmentFile=` in systemd or a secret manager.

```bash
# ── Required ──────────────────────────────────────────────────────────
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
ANTHROPIC_API_KEY=sk-ant-...
DATABASE_URL=postgres://kix:***@localhost:5432/kix
REDIS_URL=redis://localhost:6379/0
JWT_SECRET=<openssl rand -base64 48>
ADMIN_TOKEN=<openssl rand -base64 32>

# ── Optional ──────────────────────────────────────────────────────────
ELTM_BASE_URL=http://localhost:8001
KIX_PUBLIC_URL=https://api.letskix.com
KIX_CONSENT_ENFORCEMENT=strict   # or `permissive` for staging
LOG_LEVEL=INFO
```

## Deployment Steps

### 1. Server bootstrap

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv \
    redis-server postgresql-15 nginx certbot python3-certbot-nginx
sudo systemctl enable --now redis-server postgresql nginx
```

### 2. Database

```bash
sudo -u postgres createuser kix --pwprompt
sudo -u postgres createdb kix -O kix
# Apply schema migrations (see migrations/ directory)
psql -U kix -d kix -f migrations/init.sql
```

### 3. Application install

```bash
sudo useradd -m -s /bin/bash kix
sudo -u kix git clone https://github.com/mozatyin/kix-platform.git /opt/kix-platform
cd /opt/kix-platform
sudo -u kix python3.12 -m venv .venv
sudo -u kix .venv/bin/pip install -e ".[dev]"
sudo -u kix cp .env.example .env  # then edit secrets
```

### 4. systemd unit — API

`/etc/systemd/system/kix-api.service`:

```ini
[Unit]
Description=KiX Platform API
After=network.target redis-server.service postgresql.service

[Service]
User=kix
Group=kix
WorkingDirectory=/opt/kix-platform
Environment="PATH=/opt/kix-platform/.venv/bin"
EnvironmentFile=/opt/kix-platform/.env
ExecStart=/opt/kix-platform/.venv/bin/uvicorn app.main:app \
    --host 127.0.0.1 --port 8000 --workers 4 --proxy-headers
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kix-api
```

### 5. Background workers

KiX ships several workers under `workers/`. Each runs as its own systemd
unit (or systemd `*.timer` if cron-style).

| Worker | Cadence | Purpose |
|---|---|---|
| `billing_cron.py` | hourly | Auto-renew subscriptions, settle PG charges |
| `push_worker.py` | always-on | Drains the smart-push delivery queue |
| `moderation_worker.py` (TBD) | always-on | Consumes the content-review queue |

Example timer for `billing_cron.py`:

```ini
# /etc/systemd/system/kix-billing.service
[Unit]
Description=KiX Billing Cron (one-shot)

[Service]
Type=oneshot
User=kix
WorkingDirectory=/opt/kix-platform
EnvironmentFile=/opt/kix-platform/.env
ExecStart=/opt/kix-platform/.venv/bin/python -m workers.billing_cron
```

```ini
# /etc/systemd/system/kix-billing.timer
[Unit]
Description=Run kix-billing every hour

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now kix-billing.timer
```

### 6. nginx + TLS

```nginx
server {
    listen 443 ssl http2;
    server_name api.letskix.com;

    ssl_certificate     /etc/letsencrypt/live/api.letskix.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.letskix.com/privkey.pem;

    client_max_body_size 10M;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}

server {
    listen 80;
    server_name api.letskix.com;
    return 301 https://$host$request_uri;
}
```

```bash
sudo certbot --nginx -d api.letskix.com -d partner.letskix.com -d letskix.com
sudo nginx -t && sudo systemctl reload nginx
```

### 7. Monitoring & observability

- **Metrics**: Prometheus scrape on the API + node_exporter on each host;
  dashboards in Grafana.
- **Errors**: Sentry SDK wired into `app/main.py` (set `SENTRY_DSN`).
- **Logs**: ship to ELK / Datadog / CloudWatch via `journald` →
  `filebeat` or `vector`.
- **Uptime**: external probe hitting `GET /api/v1/attribution/health`
  every 60s.

### 8. Backups

- **Redis**: AOF on (`appendonly yes`, `appendfsync everysec`) + daily RDB
  snapshot rsynced to S3.
- **Postgres**: `pg_dump` nightly to S3, 30-day retention. Test restores
  monthly.

## Health Checks

```bash
# API liveness
curl https://api.letskix.com/api/v1/attribution/health

# Redis
redis-cli ping

# Worker status
systemctl status kix-api kix-billing.timer kix-push
```

## Smoke Tests After Deploy

```bash
# 1. OpenAPI schema generation works (regression guard)
.venv/bin/python -c "import app.main; app.main.app.openapi()" \
    && echo "OPENAPI OK"

# 2. Run the unit test suite
.venv/bin/pytest tests/ -v

# 3. End-to-end: register kid → topup wallet → run auction → report click
#    (see scripts/smoke_e2e.sh)
```

## Rollback Procedure

```bash
cd /opt/kix-platform
sudo -u kix git fetch --tags
sudo -u kix git checkout <last_good_tag>
sudo -u kix .venv/bin/pip install -e ".[dev]"
sudo systemctl restart kix-api
# verify
curl -fsS https://api.letskix.com/api/v1/attribution/health
```

## Geofence PG Setup

The geofence module dual-writes to Redis (`GEOADD geofence:stores`) and a
PostGIS-backed `geofences` table. Reads currently come from Redis, with PG
serving as the future scale path once the 30-day soak verifies parity.

If the PG table is missing the API logs:

```
WARN  schema_health: missing PG table 'geofences' — run `alembic upgrade head`
```

This is **non-fatal** — the Redis-only path keeps serving traffic — but
the backfill script will fail and PG-side spatial queries (`ST_DWithin`)
won't work until the table exists.

### Apply the migration

```bash
cd /opt/kix-platform
sudo -u kix .venv/bin/alembic upgrade head
```

This creates the `geofences` table with the `location geography(POINT,
4326)` column, the `ix_geofence_location_gist` GiST index, and the brand-
scoped secondary indexes.

### Backfill from Redis (dual-write soak)

```bash
sudo -u kix .venv/bin/python -m scripts.migrate_geofence_to_postgis --dry-run
sudo -u kix .venv/bin/python -m scripts.migrate_geofence_to_postgis
sudo -u kix .venv/bin/python -m scripts.migrate_geofence_to_postgis --verify
```

The migrate script is **idempotent**: it bootstraps the `geofences` table
itself (every DDL guarded with `IF NOT EXISTS`) so a fresh dev env can run
the backfill without first running `alembic upgrade head`. In production
still prefer alembic — the script's self-heal is a dev convenience, not
the canonical schema source.

### Verify the GiST index

```bash
psql -U kix -d kix -c "\d+ geofences"
# expect: ix_geofence_location_gist gist (location)
```

## Common Issues

| Symptom | Cause | Fix |
|---|---|---|
| `502 Bad Gateway` | uvicorn down or Redis unreachable | `systemctl status kix-api`; `redis-cli ping` |
| Payments failing silently | bad `STRIPE_SECRET_KEY` or webhook URL | verify env, hit `/v1/charges` in Stripe dashboard, check webhook deliveries |
| Push not delivered | FCM/APNS creds missing | check `push_worker` logs, verify provider keys |
| OpenAPI 500 on `/docs` | forward-ref `Request` in a route | use `Request` (module-level import) or `include_in_schema=False` |
| `Event loop is closed` in tests | session-scoped Redis fixture bound to wrong loop | confirm `asyncio_default_fixture_loop_scope = "session"` in pyproject.toml |

## Security Hardening Checklist

- [ ] All secrets in `EnvironmentFile`, never committed
- [ ] Redis bound to `127.0.0.1` only (or VPC with `requirepass`)
- [ ] Postgres bound to `127.0.0.1` only
- [ ] nginx has rate-limiting + WAF rules for `/api/v1/*`
- [ ] TLS A+ rating on https://www.ssllabs.com/ssltest/
- [ ] `ADMIN_TOKEN` rotated quarterly
- [ ] PII fields are hashed (phone/email → SHA-256) — see `kix_id._hash_identifier`
- [ ] GDPR/PIPL consent enforcement enabled (`KIX_CONSENT_ENFORCEMENT=strict`)

## Capacity Planning Starting Points

- API: 4 uvicorn workers per 4-core box; ~500 RPS comfortable headroom
- Redis: 8 GB RAM handles ~10M active brand/user keys
- Postgres: nightly VACUUM; partition `attribution_events` by month once
  monthly volume > 10M rows

## Internationalisation (i18n)

The platform ships with a Project Fluent runtime under `app/i18n/`.

- **Locale registry** — `app.i18n.SUPPORTED_LOCALES` (currently
  `en-SG`, `zh-Hans-SG`, `en-US`, `zh-Hans-CN`, plus Phase 2 SEA:
  `id-ID` (Indonesian), `ms-MY` (Malay), `th-TH` (Thai), `vi-VN`
  (Vietnamese)). New regions `id`, `my`, `th`, `vn` registered in
  `app/region.py` with currencies IDR / MYR / THB / VND.
- **Per-request resolution** — `LanguageMiddleware` reads
  `?lang=` → JWT user pref → `Accept-Language` → region default →
  `en-SG`. Result is exposed as `request.state.locale`, on the
  `current_locale` ContextVar (`app.i18n.context`), and as the
  `Content-Language` response header.
- **Translation API** — `from app.i18n import t; t("welcome-message",
  name="Alice")`. Missing keys log `i18n.missing_translation` and
  return the key verbatim — never raise.
- **Catalogs** — Fluent `.ftl` files at
  `app/i18n/catalogs/<locale>/main.ftl`. Per-locale onboarding
  checklist lives in `docs/i18n-adding-a-locale.md`.
- **Region fallback chain** — see `language_fallback_chain` per
  region in `app/region.py`; surfaced via
  `region.get_supported_locales_for_region()`.
- **Monitoring** — alert on the `i18n.missing_translation` log line;
  it indicates a key shipped without a translation in some locale.

## Stripe Integration

Stripe is the production payment provider for wallet top-ups, payment-
method onboarding and subscription billing. The integration lives in
`app/services/stripe_live.py` (single entry point) and three routers
that call it:

- `app/routers/wallet.py` — `POST /api/v1/wallet/{bid}/topup/checkout`
  creates a Stripe Checkout session; the legacy `/topup` + `/topup/{id}/
  confirm` pair stays for callers that still embed their own gateway
  state machine, including the `?mock=true` fast-path for dashboard QA.
- `app/routers/payment_methods.py` — `POST /{bid}/add-setup-intent`
  returns a SetupIntent `client_secret` for client-side Stripe Elements.
  `DELETE /{pm_id}` and `PUT /{pm_id}/set-default` also push state to
  the customer at Stripe.
- `app/routers/stripe_webhook.py` — `POST /api/v1/webhooks/stripe`
  receives + verifies events. Idempotent via a two-phase Redis claim
  (`processing` → `completed`).

### Mode auto-detection

`app.services.stripe_live.get_mode()` returns one of:

- `live` — `STRIPE_SECRET_KEY` starts with `sk_live_`. Real money.
- `test` — `STRIPE_SECRET_KEY` starts with `sk_test_`. Test cards only.
- `mock` — key unset or set to `sk_test_stub`. Never touches the network.
  All tests, CI and local dev use this mode by default.

### Setting `STRIPE_SECRET_KEY` in production

Add to the systemd `EnvironmentFile` (never commit secrets):

```bash
STRIPE_SECRET_KEY=sk_live_***
STRIPE_WEBHOOK_SECRET=whsec_***
```

Then `systemctl restart kix-api`.

### Configuring the webhook endpoint

In the Stripe Dashboard → Developers → Webhooks → "Add endpoint":

- URL: `https://<your-domain>/api/v1/webhooks/stripe`
- Events to send:
  - `payment_intent.succeeded`
  - `payment_intent.payment_failed`
  - `setup_intent.succeeded`
  - `customer.subscription.updated`
  - `customer.subscription.deleted`
  - `invoice.payment_succeeded`
  - `invoice.payment_failed`
  - `charge.refunded`

Copy the **Signing secret** (`whsec_...`) into `STRIPE_WEBHOOK_SECRET`.

### Test mode before going live

1. Use `sk_test_...` key + matching `whsec_...` from the test-mode
   dashboard.
2. Drive a full flow:
   `POST /api/v1/wallet/<bid>/topup/checkout` → follow the returned
   `checkout_url` → pay with `4242 4242 4242 4242`.
3. Confirm the webhook fired and the wallet was credited.
4. Swap to `sk_live_...` only after the test flow round-trips clean.

### Troubleshooting Stripe-specific errors

| Symptom | Likely cause | Fix |
|---|---|---|
| `stripe_checkout_failed` 502 on /topup/checkout | bad/expired API key | rotate `STRIPE_SECRET_KEY`; check Stripe dashboard logs |
| Webhook returns 401 `invalid_signature` | webhook secret mismatch | re-copy `whsec_...` from dashboard, redeploy |
| Webhook returns 503 `webhook_not_configured` | `STRIPE_WEBHOOK_SECRET` empty | set the env var; restart |
| Wallet never credited despite payment success | metadata missing `brand_id` | confirm `/topup/checkout` was used (sets metadata) — direct charges via dashboard need manual reconciliation |
| Stuck "pending" topup | webhook never delivered | check Stripe dashboard → Webhooks → Recent deliveries; verify endpoint reachable |

---

Last reviewed: 2026-05-30
