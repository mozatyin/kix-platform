# Trinity 3T Iteration Handbook

> **Status**: institutionalised. Manual 5-round 小王 cycle has been
> turned into a callable engine (`app.services.trinity_engine`) with a
> REST surface (`app.routers.trinity_admin`) and a CLI
> (`scripts/trinity_iterate.py`).

---

## 1. What is Trinity 3T?

The **Trinity Protocol** triangulates any audit through three sources of
truth so that no single lens dominates:

| Pillar | Question | Source |
|--------|----------|--------|
| **Industry** | What do the best-in-class tools actually do? | Google Ads, TikTok Ads Manager, Stripe Dashboard, Duolingo, etc. |
| **Academic** | What does literature say about good UX / pedagogy / engagement? | Nielsen heuristics, Jakob's Law, Octalysis, etc. |
| **Reality** | What does the repo actually contain right now? | `grep`, file reads, schema introspection. |

"3T" stands for the **three** triangulation pillars; each pillar is a
**T**ruth source. A finding without at least two-pillar support is a
hunch, not a complaint.

The engine wraps the protocol in an iteration loop so the audit
**converges** rather than producing a one-shot wishlist:

1. **Persona walk** — the auditor *adopts* a stakeholder's lens (small
   shop owner, marketing agency planner, end consumer, KiX admin,
   investor). Each round runs through that lens.
2. **Industry comparison** — diff against the persona's named baselines.
3. **Academic check** — Nielsen / domain heuristics.
4. **Reality dump** — grep the artifact and adjacent code.
5. **Synthesise** complaints into the canonical schema.
6. **Categorise** P0 / P1 / P2.
7. **Verdict** — persona scores 0-10 and returns a headline.

Rounds keep firing until:

- ≤3 *new* P0/P1 complaints land in **two consecutive** rounds, **or**
- the persona verdict score reaches `target_quality`, **or**
- `max_rounds` is exhausted.

---

## 2. When to use it

| Trigger | Recommended persona | Target |
|---------|---------------------|--------|
| New landing page / portal view | `shop-owner` or `consumer` | 7/10 |
| New admin tool / ops console | `admin` | 7/10 |
| New ad-platform feature | `marketing-agency` | 8/10 |
| Pre-fundraise diligence pass | `investor` | 8/10 |
| Major redesign of any surface | the surface's primary persona | 7/10 |
| Compliance review | `admin` + (jurisdiction-specific custom) | 9/10 |

**Use it before sending the surface to the next stakeholder**, not as a
QA gate after merge. Trinity is a *thinking* tool, not a regression
suite.

---

## 3. How to define a new persona

A persona is a `@dataclass(frozen=True)` with five required fields:

```python
from app.services.trinity_engine import Persona, PERSONA_REGISTRY

def RestaurantOwnerPersona() -> Persona:
    return Persona(
        slug="restaurant-owner",
        label="Restaurant Owner (F&B)",
        description="Singapore F&B operator, 1-3 outlets",
        industry_baselines=("Chope", "Eatigo", "Foodpanda Merchant"),
        focus=("reservations", "table-turn", "menu", "promo"),
        red_flags=(
            "no reservation",       # P0 — appears first
            "no menu",
            "no promo",             # P1
            "no analytics",
            "no integration",       # P2
        ),
        verdict_phrase_good="signup yes",
        verdict_phrase_bad="not for me",
    )

PERSONA_REGISTRY["restaurant-owner"] = RestaurantOwnerPersona
```

Persona authoring rules:

- **Order `red_flags` by importance** — the first two map to P0, next
  two to P1, the rest to P2.
- Use **plain-noun tokens** so the deterministic walker can grep them
  (e.g. `"no menu"` becomes a check for the token `menu` in the body).
- Cap `industry_baselines` at 4 — more is noise.
- Keep `description` short — it goes into the prompt and the
  leaderboard row.

---

## 4. How to run an iteration cycle

### CLI (preferred for ad-hoc audits)

```bash
python -m scripts.trinity_iterate \
    --persona shop-owner \
    --artifact landing/portal.html \
    --max-rounds 5 \
    --target 7
```

Artifacts land at `/Users/mozat/a-docs/trinity-runs/{iteration_id}/`:

- `round-1.json` … `round-N.json` — per-round complaint dumps
- `summary.json` — final verdict

### REST (for automation / cohort tools)

```bash
curl -X POST http://localhost:8000/api/v1/trinity/iterate \
    -H "X-Admin-Token: $KIX_ADMIN_TOKEN" \
    -d '{
        "persona": "shop-owner",
        "artifact_path": "landing/portal.html",
        "target_quality": 7,
        "max_rounds": 5,
        "auto_run": true
    }'
```

Then poll:

```bash
curl http://localhost:8000/api/v1/trinity/iteration/{iid} \
    -H "X-Admin-Token: $KIX_ADMIN_TOKEN"
```

### Python API (for embedding in other tools)

```python
from app.services.trinity_engine import TrinityIteration, Severity

it = await TrinityIteration.create(
    persona="shop-owner",
    artifact_path="landing/portal.html",
    target_quality=7,
)
while not await it.has_converged():
    result = await it.round()
    print(result.verdict_headline, result.p0_count())

verdict = await it.final_verdict()
prompts = await it.dispatch_autofix(severities=(Severity.P0,))
```

---

## 5. The complaint schema

Every finding is a `Complaint` with this shape:

```json
{
  "severity": "P0",
  "category": "pricing",
  "persona_concern": "Small Business Shop Owner cares about pricing",
  "expected": "artifact addresses pricing",
  "got": "no mention of price in landing/portal.html",
  "fix_estimate_hours": 2,
  "fingerprint": "a1b2c3d4e5f60718",
  "occurrences": 3,
  "first_seen_round": 1,
  "last_seen_round": 4
}
```

Stable fingerprint = `sha256(category|persona_concern|expected|got)[:16]`.
Identical complaints across rounds **dedupe** rather than bloat the
list, with `occurrences` tracking persistence.

**Severity escalation**: a P2 that survives 3 rounds becomes P1; a
persistent P1 becomes P0. Long-tail nits stop being nits if nobody
fixes them.

---

## 6. Auto-dispatch fixes

`dispatch_autofix(severities=(Severity.P0,))` returns one fix-prompt per
complaint (or invokes a caller-supplied `dispatch_fn` for real agent
runtime integration). The default prompts look like:

```
Fix this specific complaint on landing/portal.html.
Severity: P0
Category: pricing
Persona concern: Small Business Shop Owner cares about pricing
Expected: artifact addresses pricing
Got: no mention of price in landing/portal.html
Estimated effort: 2h
Produce a minimal patch; do not gold-plate.
```

The engine is intentionally decoupled from any specific agent system —
plug Claude Code, Cursor, or a queue worker behind `dispatch_fn`.

---

## 7. Case study — 小王 5 rounds → S$5K verdict

The motivating run (manual, pre-engine) audited
`landing/portal.html` from the `shop-owner` lens. Five rounds gave us:

| Round | New P0 | New P1 | Score | Headline |
|-------|--------|--------|-------|----------|
| 1 | 6 | 4 | 2/10 | "not paying S$5K" |
| 2 | 2 | 3 | 4/10 | "not paying S$5K" |
| 3 | 1 | 2 | 6/10 | "not paying S$5K" |
| 4 | 0 | 1 | 7/10 | "S$5K yes" |
| 5 | 0 | 0 | 8/10 | "S$5K yes" — **converged** |

What we learned and baked into the engine:

- **Pricing first** — it's the persona's #1 concern; surface it as P0.
- **Trust before features** — refund + FAQ + chat outweighed feature
  depth.
- **Repetition signals priority** — a P1 raised in rounds 1-3 deserves
  P0 treatment, hence severity escalation.
- **Convergence is non-monotone** — score dipped in round 2 (deeper
  walk found more issues) before climbing. The "≤3 new P0/P1 for two
  rounds" rule absorbs that wobble.

---

## 8. Quality-target setting

| Target | Meaning | When to use |
|--------|---------|-------------|
| **5** | "It works" — no blocker for first internal demo | Pre-α prototype |
| **7** | "I'd sign up" — persona is willing to pay | Pre-launch gate |
| **8** | "I'd recommend" — persona becomes an advocate | Pre-PR pitch |
| **9** | "Industry-leading" — beats named baselines on at least one axis | Pre-fundraise diligence |
| **10** | Reserved; no human persona scores 10/10 honestly. | — |

Setting `target_quality` too high wastes rounds. Setting it too low
ships dross. **7 is the right answer for almost every product surface.**

---

## 9. Engine surface reference

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/trinity/personas` | GET | List registered personas (no auth) |
| `/api/v1/trinity/iterate` | POST | Start a new iteration |
| `/api/v1/trinity/iteration/{id}` | GET | Status + complaints |
| `/api/v1/trinity/iteration/{id}/round/{n}` | GET | Single round result |
| `/api/v1/trinity/iteration/{id}/round` | POST | Run next round |
| `/api/v1/trinity/iteration/{id}/auto-fix` | POST | Dispatch fix-agents |
| `/api/v1/trinity/leaderboard` | GET | All recent iterations |

All admin endpoints accept `KIX_ADMIN_TOKEN` via query string or
`X-Admin-Token` header.

Engine module: `app.services.trinity_engine`
CLI: `python -m scripts.trinity_iterate`
Test suite: `tests/test_trinity_engine.py` (22 tests)

---

## 10. Anti-patterns

- **Don't run Trinity against itself.** Auditing the engine through
  the engine is recursive cuteness with no signal.
- **Don't paste the complaint list into Slack and call it done.**
  Convergence requires *fixing* and re-running, not just reading.
- **Don't pick a persona that doesn't actually consume the surface.**
  An `investor` persona on a consumer-facing voucher claim screen
  produces noise.
- **Don't disable convergence.** If you keep finding new P0s past
  round 5, the artifact has a structural problem that one more round
  won't fix — escalate, don't iterate.
