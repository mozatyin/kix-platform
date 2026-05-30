# Runbook: Stripe Outage

**Symptom:** Stripe API returns 5xx, times out, or webhook deliveries
have stopped. Payments are not completing.

**Severity:** SEV2 (revenue impact but not customer data loss).

---

## 1. Confirm it's Stripe, not us

```bash
# Check Stripe status
curl -fsS https://status.stripe.com/api/v2/status.json | jq .

# Check our outbound from a worker
kubectl exec -n kix deploy/worker-payments -- \
  curl -sS -o /dev/null -w "%{http_code} %{time_total}s\n" \
  https://api.stripe.com/v1/charges
```

If Stripe is healthy but we're failing, we have a credential / network
issue on our side — see §6.

## 2. Queue charges (degrade write path)

The payments worker already implements a write-ahead pattern: every
charge is persisted to `payment_intents` table with state `pending`
**before** calling Stripe. During Stripe outage:

```bash
# Tell the worker to short-circuit Stripe calls and just queue
kubectl set env deploy/worker-payments STRIPE_OUTAGE_MODE=true
```

Behavior with the flag on:
- New `payment_intents` rows are written and ack'd to the user with
  message "We received your payment request and will charge your card
  shortly."
- Worker stops attempting Stripe calls; queue depth grows in
  `stripe_outbox` table.
- User-facing pages render "Payment processing — usually <1 minute" instead
  of "Charged successfully".

## 3. Customer comms

Use template `STRIPE-OUT-1` (in `incident-response.md` §5):

> We're aware that payment processing is currently delayed due to an
> issue with our payment provider. Your order is reserved and your
> card will be charged automatically once service is restored. No
> action needed on your part.

Post to status page. Do NOT promise a specific time unless Stripe
publishes an ETA.

## 4. Drain the queue when Stripe recovers

1. Verify Stripe is healthy: status page green AND a manual test charge
   succeeds:
   ```bash
   kubectl exec -n kix deploy/worker-payments -- \
     python -m app.scripts.test_stripe_charge --amount 50 --dry-run
   ```
2. Unset the outage flag:
   ```bash
   kubectl set env deploy/worker-payments STRIPE_OUTAGE_MODE-
   ```
3. The worker reads `stripe_outbox` in FIFO order with idempotency keys
   (so retries don't double-charge). Monitor:
   ```bash
   psql "$DATABASE_URL" -c \
     "SELECT count(*), min(created_at) FROM stripe_outbox WHERE status='pending';"
   ```
   Expect queue to drain at ~50 charges/sec. Page Tech Lead if depth
   > 10k and growing.

## 5. Webhook receive failure

If webhooks stopped arriving but our API is up, Stripe may be unable
to reach our endpoint. Backfill via poll:

```bash
# Pull events from the last 4 hours
python scripts/stripe_backfill.py --since "$(date -u -v -4H +%FT%TZ)"
```

The script calls `GET /v1/events` and dispatches each event through
the same handler as the webhook (idempotent by event ID).

## 6. Manual reconciliation

After a long outage, run end-of-day reconciliation:

```bash
python scripts/stripe_reconcile.py --date "$(date -u +%F)"
```

This compares:
- `payment_intents` rows in our DB with status `succeeded`.
- Stripe `/v1/charges` for the same window.

Discrepancies are logged to `reconciliation_exceptions` table and
emailed to finance. **Each row must be investigated within 24h.**

Common discrepancies:
- Our DB says `succeeded`, Stripe has no record → likely a webhook we
  processed but Stripe rolled back. Refund the customer record.
- Stripe says `succeeded`, our DB says `pending` → backfill missed.
  Run §5 again for the specific event ID.

## 7. Our-side failure (credentials / IP allowlist)

If §1 showed Stripe healthy but we're failing:

1. Rotate keys? Check `kubectl get secret -n kix stripe-keys -o yaml`
   and compare with Stripe dashboard.
2. IP allowlist? We don't use one by default; if someone enabled
   restrictions, the outbound NAT IP may have changed after a node
   pool refresh.
3. TLS? `openssl s_client -connect api.stripe.com:443 -servername
   api.stripe.com </dev/null` — if cert verify fails our CA bundle
   needs updating (`update-ca-certificates` in the worker image).
