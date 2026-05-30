# TriSoul Integration — Deployment Guide

Wave C of the KIX Gamification Bible delivery. Adds adaptive routing
based on a per-user TriSoul attention vector across push, auction, and
recipe generation. All hooks are **additive**, **feature-flag gated**,
and **default OFF** so this can ship to production without disturbing
existing behaviour.

## What TriSoul provides

For each user, TriSoul maintains an attention/preference embedding
across N dimensions (default 5: `competitive`, `social`, `casual`,
`premium`, `novelty`). Each dimension is a float in `[0, 1]` representing
how strongly the user attends to that quality.

Brand-side embeddings live alongside (same key shape) and the **affinity**
between a user and a brand is the rescaled dot product, clipped to `[0, 1]`.
A default-vs-default pair returns exactly `0.5` (neutral) so cold-start
users do not perturb existing rankings.

## Where it influences routing

| Surface | Hook | Bound | Default |
|---|---|---|---|
| `push_engine._evaluate_candidates` | `composite *= push_boost(affinity)` | ±20% | identity (flag off) |
| `auction.run_auction` rank step | `rank *= auction_boost(affinity)` | ±10% | identity (flag off) |
| `recipe_generator.from_description` | TriSoul re-orders library candidates with 30% selection probability | qualitative | legacy path (flag off) |

The boost multipliers are constrained by construction:

```python
push_boost   = 1.0 + 0.20 * (affinity - 0.5) * 2.0  # ∈ [0.8, 1.2]
auction_boost = 1.0 + 0.10 * (affinity - 0.5) * 2.0  # ∈ [0.9, 1.1]
```

This means TriSoul **cannot** invert an auction by itself (a 10% bump
won't beat a 2× bid difference) and cannot starve campaigns of impressions.

## Enabling in production

### Global enable

```bash
# Single env var; reload not required for new processes.
export TRISOUL_ENABLED=1
```

Restart your API workers (or wait for the rolling restart).

### Incremental rollout (recommended)

Keep `TRISOUL_ENABLED=0` globally and turn the flag on per user via Redis
through the API. This lets you start with internal users and ramp up:

```bash
# Enable for a single user
curl -X POST https://api.letskix.com/api/v1/trisoul/{user_id}/flag \
     -H 'content-type: application/json' \
     -d '{"enabled": true}'

# Check status
curl https://api.letskix.com/api/v1/trisoul/{user_id}/flag
```

Per-user override is read on every routing decision and always beats
the global setting.

### Model version

`TRISOUL_MODEL_VERSION` (default `v1`) is reported on `/api/v1/trisoul/health`.
To hot-swap without restart:

1. Update the env var on the running process (or its supervisor).
2. `POST /api/v1/trisoul/reload`.

## Monitoring

The health endpoint exposes runtime counters:

```bash
curl https://api.letskix.com/api/v1/trisoul/health
```

Returns:

```json
{
  "status": "ok",
  "vendor_loaded": false,
  "vendor_error": "No module named 'trisoul'",
  "model_version": "v1",
  "feature_count": 5,
  "global_flag": false,
  "lookups": {"cached": 1234, "miss": 78},
  "score_histogram": {"b_5": 41, "b_6": 23, ...}
}
```

### Key metrics to watch

| Metric | Healthy signal |
|---|---|
| `lookups.cached / (cached + miss)` | > 0.6 — most lookups served from the 60-second cache |
| `score_histogram` | Not concentrated in `b_4`/`b_5` — that would mean every user is neutral (cold-start broken or features not updating) |
| `vendor_loaded` | `true` once the vendored model ships; `false` in dev / CI is expected |

### Audit trail

Each TriSoul-influenced decision appends one row to the
`trisoul:audit` Redis list (capped at 10 000). Rows include
`{ts, user_id, route, brand_id, version, affinity_hint}` — **never the
raw feature vector**, per the privacy constraint.

## A/B testing setup

Recommended approach:

1. Pick an A/B segment key (e.g., `kid % 100 < N`).
2. From the experiment service, POST to `/api/v1/trisoul/{kid}/flag`
   with `enabled=true` for the treatment cohort and `enabled=false`
   for the control cohort.
3. Track downstream conversion / engagement metrics over a 7–14 day
   window. Cohort assignment is sticky because the Redis key is
   per-user.

## Rollback procedure

### Soft rollback (kill the feature for all users)

```bash
export TRISOUL_ENABLED=0
# No restart needed for processes that re-read env per request; safe
# default: restart workers.
```

Per-user overrides still take effect, so to fully neutralise:

```bash
redis-cli --scan --pattern 'trisoul:enabled:*' | xargs redis-cli del
```

### Hard rollback (remove the routing hooks)

If something more fundamental goes wrong, the hooks themselves are
single-line additive call sites in:

- `app/routers/push_engine.py` (composite_score line)
- `app/routers/auction.py` (rank line)
- `app/routers/recipe_generator.py` (from_description library hit path)

Removing them restores the original scoring exactly. The integration
module (`app/routers/trisoul_integration.py`) can stay mounted (its
endpoints are independently useful for telemetry) without influencing
any routing decision.

## Cold-start behaviour

A user with no entries in `trisoul:user:{uid}` returns
`DEFAULT_FEATURES` (every dimension at `0.5`). Combined with a default
brand embedding this yields affinity `0.5`, which makes both
`push_boost` and `auction_boost` equal to `1.0` — i.e. **a brand-new
user sees the exact same ranking as the legacy path**, even when the
flag is on. Personalisation only kicks in after we've accumulated
enough events to move the vector off neutral.

## Privacy

- The audit log never persists the feature vector itself, only the
  route + brand + scalar affinity hint (rounded to 2 dp).
- `GET /api/v1/trisoul/{user_id}` returns the user's own features
  (callable from authenticated user contexts only — gate at the
  reverse proxy / auth layer).
- TriSoul features are stored under per-user Redis keys and are
  subject to the same GDPR `/right-to-deletion` lifecycle as other
  per-user state.
