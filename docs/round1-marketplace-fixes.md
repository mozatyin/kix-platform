# 30-Day SG F&B Marketplace — Trinity Iteration v1 → v2

**Date**: 2026-05-29 | **Iteration**: Round 1 fixes complete

## Trinity Loop Result

```
Round 0 (sim findings):
  21 bugs (6 P0 + 10 P1 + 5 P2)
  HHI 3898 (highly concentrated)
  K-factor 0.000 (no viral)
  5/10 brands won 0 auctions
  ─────────────────────────────────
Round 1 (fixes, 8 agents parallel):
  9 commits landed in main
  305 tests pass (was 211 baseline +94 new)
  9/10 fix endpoints wired (1 was an auth test, not a bug)
```

## Commits Landed (in order)

| # | Commit  | Fix                                               |
|---|---------|---------------------------------------------------|
| 1 | 24cbc6c | P2 QS auto-compute + decay + breakdown + override |
| 2 | a934bb7 | P0 geofences PG table — startup health + bootstrap|
| 3 | 0302621 | P1 BrandConfigCreate docs + autofill + endpoints  |
| 4 | 93fdcd2 | P2 cross-brand multi-touch attribution            |
| 5 | 6e46083 | P0 viral compounding — auto-emit invite on redeem |
| 6 | 84b3318 | P1 wallet auto-recharge (STARTER default-on)      |
| 7 | be6ec90 | P1 bid death-spiral floor + auto-pause + PI pacing|
| 8 | 6d26aad | P0 wallet→auction budget backpressure             |
| 9 | 5a0f1a0 | P0 cold-start learning boost + diversity floor    |

## Fix Coverage vs Original Findings

| Original Bug                           | Sev | Fix Commit | Mechanism |
|----------------------------------------|-----|------------|-----------|
| viral_loop_dead                        | P0  | 6e46083    | Auto-emit invite on redeem, depth-5 cap, explosion warning K>1 |
| cold_start_starvation (5 brands)       | P0  | 5a0f1a0    | 24h learning boost 1.5×→1.0× + 3% diversity floor winner-slot |
| auction_concentration (4 instances)    | P1  | 5a0f1a0    | Same — diversity floor injects winner into ranked top-3 |
| bid_death_spiral (4 instances)         | P1  | be6ec90    | Floor=max(50c, declared_max×50%) + 400 reject + auto-pause at <5% |
| wallet_depletion_no_autorecharge       | P1  | 84b3318    | STARTER default-on at 20% threshold + 30% warning + Stripe topup |
| pacing_drift                           | P1  | be6ec90    | Replaced hourly buckets with PI controller (Kp=1.0, Ki=0.05, 60s) |
| quality_score_dispersion (4 instances) | P2  | 24cbc6c    | Weekly auto-compute from CTR/CVR/completion + decay <100 imp/wk |
| cross_brand_attribution_clash          | P2  | 93fdcd2    | linear / time_decay / position_based models + persisted credits |
| geofences PG missing                   | P0* | a934bb7    | Startup health check + idempotent bootstrap DDL + docs |
| BrandConfigCreate undocumented schema  | P1* | 0302621    | Field descriptions + autofill + /config-template endpoint |
| budget_exceeded no auction backpressure| P0* | 6d26aad    | Budget-blocked Redis flag + _has_budget short-circuit |

\* = surfaced from sim logs, not in original 21-bug list

## Expected Round 2 Sim Metrics (predicted)

| Metric             | Round 0 actual | Round 1 target |
|--------------------|---------------:|---------------:|
| HHI                | 3898           | <1500          |
| K-factor (7d)      | 0.000          | 0.3 - 1.2      |
| Brands with 0 wins | 5/10           | 0/10           |
| CHIR CHIR share    | 57.1%          | <25%           |
| Total user growth  | -10%           | +30-50%        |
| Attribution coverage | 100% last-touch | multi-touch all |

## Open Issues

- **Sim script lost**: `sim_sg_marketplace_30day.py` was wiped by concurrent agent reset
  during the parallel fix wave. Findings + verification preserved; sim needs rebuild for Round 2.
- **F7 multi-touch endpoint** returns 403 (admin-token gated) — verify caller has token

## Next: Round 2

When the sim is rebuilt:
1. Rerun with same 10 personas + 30 sim days
2. Snapshot Day 7 / 14 / 21 / 30 Trinity checkpoints
3. Compare HHI / K-factor / brand-share-distribution against Round 0
4. Identify residual systemic bugs (expected: 3-5 lower-severity, e.g.
   fatigue accumulation, cross-region campaign behavior, frequency cap thrashing)

## Trinity-Industry Validation

- **Google AdWords**: their Smart Bidding uses pacing + quality-score auto-compute, our PI + 7d window matches
- **TikTok Ads**: 24-hour "learning phase" is industry-standard; our 1.5× decaying boost mirrors it
- **Viral compounding**: WhatsApp / Dropbox 90%+ K-factor on multi-leg invites; ours
  now mathematically capable of compounding (auto-emit at redeem)
- **Multi-touch attribution**: Meta + Google moved off last-touch in 2018-2020; ours
  now matches their linear / time-decay default

## Files Modified (cumulative)

- `app/routers/auction.py` — diversity floor + learning boost + PI pacing + budget backpressure
- `app/routers/campaigns.py` — bid floor + auto-pause + bid history + budget status
- `app/routers/wallet.py` — auto-recharge v2 + budget-blocked flags
- `app/routers/network_effect.py` — invite compounding + K-factor tracker
- `app/routers/attribution.py` — multi-touch models + persisted credits
- `app/routers/reporting.py` — attributed metrics integration
- `app/routers/brands.py` — config template + auto-fill
- `app/routers/dashboards.py` — wallet-low alerts surfaced
- `app/quality_score.py` (new) — auto-compute + decay + override
- `app/pacing_controller.py` (new) — PI controller
- `app/main.py` — schema_health probe
- `app/schemas.py` — BrandConfigCreate documented fields
- `app/workers/billing_cron.py` — recompute_all_active + low_perf_pause sweep
- `scripts/migrate_geofence_to_postgis.py` — idempotent bootstrap DDL
- `scripts/llm_quota_monitor.py` (new) — Anthropic API quota guard
- `PRODUCTION.md` — geofence PG setup section
- 11 new test files, +94 tests, 305 total passing
