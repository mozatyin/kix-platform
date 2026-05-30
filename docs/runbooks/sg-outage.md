# Runbook: SG Region Outage

**Symptom:** Singapore region (our primary) is unreachable or degraded.
Health checks fail from external probers; customer traffic returns 5xx or
times out.

**Severity:** SEV1 (customer-facing total outage).

**Estimated MTTR (cold-DR posture):** 2–4 hours.

---

## 0. Page & assemble

1. On-call SRE acknowledges page within 5 minutes.
2. Open `#incident-active` Slack channel, designate IC + TL + Comms.
3. Post status-page banner using template `SP-OUTAGE-1` (see
   `incident-response.md`).

## 1. Diagnose (10 min budget)

Run in order, stop at the first that explains the outage:

1. **Is it us or them?**
   ```bash
   # From a non-SG host (laptop on a cafe network is fine)
   curl -sS -o /dev/null -w "%{http_code}\n" https://api.kix.io/health
   dig +short api.kix.io
   ```
   If DNS resolves but TCP/TLS handshake fails → likely provider/network.
2. **Cloud provider status page.** Check provider's SG region status; if
   they declared an incident, jump to mitigation §2.
3. **K8s control plane.**
   ```bash
   kubectl --context kix-sg get nodes
   kubectl --context kix-sg get pods -A | grep -v Running
   ```
   If control plane unreachable → infra-level outage, mitigation §2.
4. **Ingress.**
   ```bash
   kubectl --context kix-sg logs -n ingress-nginx -l app=ingress-nginx --tail=200
   ```
5. **App health.** `kubectl exec` into a `kix-api` pod and `curl
   localhost:8000/health`. If green inside the pod but red outside →
   ingress/LB issue, see ingress runbook.

## 2. Mitigate — redirect traffic

If SG region itself is dead and ETA > 30 min, execute cold-DR to standby
region:

1. **Spin up standby cluster** (terraform):
   ```bash
   cd deployment/terraform
   terraform workspace select standby
   terraform apply -auto-approve -var "promote_to_primary=true"
   ```
   Expect 15–25 min.
2. **Restore PG** to standby — follow `db-failover.md` §3. Do NOT skip
   the WAL replay step.
3. **Warm Redis** — empty cache is acceptable; app will recompute. See
   `redis-failover.md` §4 for hot-key preload script.
4. **DNS flip:**
   ```bash
   # Lower TTL first (already 60s in production)
   aws route53 change-resource-record-sets \
     --hosted-zone-id "$ZONE_ID" \
     --change-batch file://deployment/dns/standby-promote.json
   ```
5. **Smoke test from external prober:**
   ```bash
   curl -fsS https://api.kix.io/health
   curl -fsS https://api.kix.io/v1/marketplace/featured
   ```

## 3. Recover (when SG comes back)

1. Confirm SG cluster healthy: `kubectl --context kix-sg get nodes`.
2. **Do not failback during peak hours.** Wait for low-traffic window
   (03:00–06:00 SGT).
3. Re-replicate PG: promote SG back to primary only after WAL catches up
   (`SELECT pg_last_wal_replay_lsn();` matches standby).
4. DNS flip back; monitor error rate for 30 min before standing down.

## 4. Post-mortem template

Open `docs/post-mortems/YYYY-MM-DD-sg-outage.md` from
`incident-response.md` §6 template. Required sections:

- Timeline (UTC + SGT)
- Customer impact (RPS dropped, error rate peak, count of affected users)
- Root cause (5-whys)
- What went well
- What went poorly
- Action items with owners + due dates
- Did our RTO/RPO targets hold? If not, why?

Publish internally within 5 business days; sanitize and publish externally
within 10 business days.
