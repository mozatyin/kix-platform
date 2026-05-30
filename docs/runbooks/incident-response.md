# Runbook: Incident Response

This is the meta-runbook: how to run an incident regardless of cause.
Specific scenarios are in sibling runbooks (`sg-outage.md`,
`db-failover.md`, `redis-failover.md`, `stripe-outage.md`).

---

## 1. Severity Classification

| SEV | Definition | Examples | Response |
|-----|------------|----------|----------|
| **SEV1** | Customer-facing total outage OR data loss OR security breach. | Site down. Payments broken. PII exposed. | Page on-call immediately. IC + TL + Comms within 15 min. Status page banner. Hourly customer updates. |
| **SEV2** | Significant degradation but workaround exists. Some customers/regions affected. | One region down. Cache down (slow but functional). Stripe degraded. | Page on-call. IC + TL within 30 min. Status page banner if customer-noticeable. |
| **SEV3** | Internal degradation, no customer impact. | One worker pool crashlooping but others handling load. Non-critical job failing. | Slack `#oncall`. Fix during business hours. |

When in doubt, **call it one level higher**. Downgrading mid-incident is
cheap; upgrading late is expensive.

---

## 2. On-call rotation

- **Primary:** SRE rotation (weekly, Mon 10:00 SGT handoff).
- **Secondary:** Service-owner rotation per domain (payments, ads,
  marketplace).
- **Manager escalation:** Engineering manager, then CTO.
- Schedule lives in PagerDuty (or whatever we end up using); source of
  truth is `deployment/oncall-schedule.yaml`.

Response SLAs from page:
- Ack within **5 min** (page repeats every 5 min for 30 min, then
  escalates to secondary).
- First mitigation action started within **15 min**.

---

## 3. War-room procedure

When SEV1 or SEV2 declared:

1. **Create** Slack channel `#inc-YYYYMMDD-<slug>` (e.g.
   `#inc-20260530-sg-outage`).
2. **Assign roles** in the channel topic:
   `IC: @alice | TL: @bob | Comms: @carol | Scribe: @dave`.
3. **Open** a Google Doc / shared notes for the timeline (Scribe
   maintains).
4. **Status update cadence:**
   - SEV1: every 30 min internally, every 60 min to customers.
   - SEV2: every 60 min internally, on milestone changes to customers.
5. **No silent work.** Every action goes in the channel before it's
   taken (so the Scribe captures it and others can object).
   - Format: `@alice: I am going to [action] in [system]. Objections in
     2 min.`
6. **One brain at the wheel.** TL executes; everyone else queues
   suggestions in thread. IC arbitrates conflicts.
7. **Declare all-clear** only after:
   - Metrics back to baseline for 15 min.
   - Customer-impacting symptoms verified gone via external probe.
   - Post-mortem owner assigned and ticket filed.

---

## 4. Tooling cheat sheet

```bash
# Top-down quick check
kubectl --context kix-sg get nodes
kubectl --context kix-sg get pods -A | grep -v -E 'Running|Completed'
kubectl --context kix-sg top pods -A --sort-by=memory | head -20

# Recent deploys (was this caused by a deploy?)
kubectl --context kix-sg rollout history deploy -n kix
git -C /Users/mozat/kix-platform log --oneline -20

# Metrics
open "https://grafana.kix.io/d/overview"

# Logs (last 5 min, errors)
kubectl --context kix-sg logs -n kix -l app=kix-api --since=5m | grep -E 'ERROR|CRITICAL'
```

---

## 5. Customer comms templates

### SP-OUTAGE-1 (initial)
> We are currently experiencing an outage affecting [scope]. Our team is
> actively investigating. We'll provide an update within [60 minutes].
> We apologise for the disruption.

### SP-OUTAGE-2 (update)
> Update on the ongoing incident: we have identified [cause] and are
> [action]. Estimated time to recovery: [ETA or "still investigating"].
> Next update at [time].

### SP-OUTAGE-3 (resolved)
> The incident has been resolved. All services are operating normally.
> A full post-mortem will be published within 5 business days. Thank
> you for your patience.

### STRIPE-OUT-1
> Payment processing is currently delayed due to an issue with our
> payment provider. Your order is reserved and your card will be
> charged automatically once service is restored. No action needed.

### DATA-LOSS-1 (only with PR/legal sign-off)
> We have identified that data in the window [start]–[end] may have
> been [lost/corrupted/exposed]. Affected customers will be contacted
> directly. We are taking the following actions: [list]. We are deeply
> sorry. [Contact info for questions.]

---

## 6. Post-mortem timeline

| When | What |
|------|------|
| T+0 (all-clear) | IC files PM ticket, assigns owner (usually TL). |
| T+24h | Draft timeline shared with team. |
| T+3 days | Root cause + action items reviewed by service owners. |
| T+5 business days | Internal PM published; external version drafted if customer-impacting. |
| T+10 business days | External PM published (if applicable). |
| T+30 days | Action item review: which closed, which slipped, why. |

### Template

```markdown
# Post-mortem: <slug>

**Date:** YYYY-MM-DD
**Severity:** SEV1/2/3
**Duration:** start UTC – end UTC (Xh Ym)
**Authors:** @ic, @tl

## Summary
One paragraph.

## Impact
- Customers affected: N (or % of MAU)
- Revenue impact: $X (estimate)
- Data loss: yes/no — if yes, scope
- SLO consumed: X% of monthly error budget

## Timeline (UTC)
- HH:MM — Event
- HH:MM — Event

## Root cause
5-whys. Be specific. "A bug" is not a root cause.

## What went well
Bullet list.

## What went poorly
Bullet list. Be honest.

## Action items
| # | Action | Owner | Due | Type |
|---|--------|-------|-----|------|
| 1 | ... | @owner | YYYY-MM-DD | Prevent / Detect / Mitigate |

## Lessons
Bullet list — what we *learned*, not what we'll *do*.
```

### Blameless rule

We attribute incidents to **systems**, not people. If a human pushed a
bad config, the question is "why did our system let that config reach
prod?" — not "why did Alice push it?". Action items target the system.
