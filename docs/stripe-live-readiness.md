# Stripe Live Readiness Checklist

This document is the operator's runbook for graduating the KiX Platform
from Stripe **mock** mode → **test** mode → **live** mode. The platform
is designed so the *only* change between modes is which
`STRIPE_SECRET_KEY` value the pod is booted with — no code changes
required.

## 1. Configure `STRIPE_SECRET_KEY`

| Mode | Prefix         | Behaviour                                              |
| ---- | -------------- | ------------------------------------------------------ |
| mock | (unset)        | Deterministic fake responses. Used in CI + local dev.  |
| mock | `sk_test_stub` | Same as unset. Sentinel for "explicitly mock please".  |
| test | `sk_test_*`    | Real API, Stripe test mode (test cards, no real money).|
| live | `sk_live_*`    | Real API, real cards, real money. Audit logged.        |

Set the env var via your secret manager (AWS SSM, Vault, GCP Secret
Manager, k8s Secret, etc.). **Never check live keys into git.**

Also set:

- `STRIPE_WEBHOOK_SECRET` — `whsec_*`, paired with the webhook endpoint
  you register in step 2.

The startup log line `[stripe_live] mode=live` confirms the pod booted
with the right configuration. If you expected `live` and see `mock`, the
env var didn't reach the process.

## 2. Register the webhook endpoint in Stripe Dashboard

1. Stripe Dashboard → **Developers → Webhooks → Add endpoint**.
2. URL: `https://api.your-domain.com/api/v1/webhooks/stripe`.
3. Subscribe to (minimum):
   - `payment_intent.succeeded`
   - `payment_intent.payment_failed`
   - `charge.refunded`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.payment_succeeded`
   - `invoice.payment_failed`
4. Copy the signing secret (`whsec_…`) into `STRIPE_WEBHOOK_SECRET`.

## 3. Run the manual smoke test

Before flipping a region to `sk_live_*`, run the smoke test against the
target environment with a **test key first**:

```bash
export STRIPE_SECRET_KEY=sk_test_xxx
python -m scripts.stripe_smoke_test --currency=SGD --amount-cents=100
```

Expected: `RESULT: PASS` with a `pi_status=succeeded` and a successful
refund. The script is idempotent — every PaymentIntent it creates is
auto-refunded at the end (unless `--no-refund`).

To exercise the live key (charges and refunds a real S$1):

```bash
export STRIPE_SECRET_KEY=sk_live_xxx
python -m scripts.stripe_smoke_test --currency=SGD --amount-cents=100 --confirm-live
```

The `--confirm-live` flag is mandatory; without it the script refuses to
charge a real card.

To also verify the local webhook side fired (requires the platform
running on `localhost:8000`):

```bash
python -m scripts.stripe_smoke_test --check-webhook
```

This polls `GET /api/v1/health/stripe` and asserts `last_charge_ts`
updates within 15s of the charge.

## 4. Verify the health endpoint

After boot, hit:

```bash
curl https://api.your-domain.com/api/v1/health/stripe
```

Expected shape:

```json
{
  "mode": "live",
  "ready": true,
  "last_charge_ts": 1717012345.6,
  "errors_last_24h": 0,
  "account_id": "acct_...",
  "available_balance": [{"amount": 12345, "currency": "sgd"}],
  "default_currency": "sgd"
}
```

`ready: false` with an `error` field means the configured key cannot
reach Stripe — usually a permissions or rotation issue. Investigate
before serving traffic.

## 5. Watch the metrics

The `stripe_charges_total{result="success|failed"}` Prometheus counter
is incremented on every checkout-session create attempt. Dashboards /
alerts should fire when:

- `rate(stripe_charges_total{result="failed"}[5m]) > 0` for >5 min, or
- `errors_last_24h` on the health endpoint crosses your SLO threshold.

## 6. Common failure modes

| Symptom                                                | Diagnosis                                                                 | Fix                                                                                              |
| ------------------------------------------------------ | ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Boot log says `mode=mock` but you set `sk_live_*`      | env var didn't reach the process; container misconfigured                  | Verify with `printenv STRIPE_SECRET_KEY` inside the running pod                                  |
| Health endpoint `ready: false` `authentication_failed` | Key was revoked, rotated, or you're hitting the wrong account              | Rotate via Stripe Dashboard → Developers → API Keys; redeploy with the new secret                |
| Smoke test passes but no webhook fires                 | Stripe → your edge connectivity blocked, or webhook secret mismatch        | Stripe Dashboard → Webhooks → click endpoint → **Send test webhook**; check 200/400 in the log   |
| `card_declined` on test mode                           | Used a real card number in test mode                                       | Use `tok_visa` / `4242 4242 4242 4242`; live cards 4xxx with real bank are rejected in test mode |
| Webhook 400s with `invalid_signature`                  | `STRIPE_WEBHOOK_SECRET` mismatches the secret shown in the Stripe Dashboard| Copy-paste again from Dashboard → Webhooks → reveal secret                                       |
| `process_webhook` returns wallet-credit on duplicate   | Idempotency key collision (should never happen — covered by test #5)       | File a P0; check `stripe_webhook:seen:<event_id>` keys in Redis                                  |

## 7. Going-live promotion sequence

1. Run smoke test against `sk_test_*` on the target environment — PASS.
2. Run smoke test against `sk_live_*` from a controlled jump host —
   PASS. Verify the S$1 refund landed in Stripe Dashboard within 1 min.
3. Promote `STRIPE_SECRET_KEY` to `sk_live_*` in the env secret store.
4. Roll the deployment.
5. Verify `[stripe_live] mode=live` in the boot logs.
6. Hit `/api/v1/health/stripe` — confirm `ready: true`.
7. Watch dashboards for ≥30 min for the first organic charge.

If any step fails, roll back to the previous `sk_test_*` value and
investigate. The platform's design guarantees a clean degradation path:
test/live both follow the same code path, and mock mode always works as
the last-resort fallback.
