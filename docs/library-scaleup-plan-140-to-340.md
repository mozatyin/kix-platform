# Library Scale-up Plan: 140 → 340 Games (Wave I.D)

**Status:** Plan only — no code in this commit. First 10 stubs land in Wave I.D-2.
**Updated:** 2026-05-31
**Owner:** Founder + ELTM pipeline

---

## Why scale 200 more games

Current 140-game library covers F&B + light retail. Per Ahmad sim
(Wave I.A-2), MY 100-outlet enterprise asked for vertical-specific
archetypes. Per consumer sim, "feels like a toy" suggests too-shallow
variety. 200 more = depth across 8 verticals (25 per vertical).

## Current 140 break-down (by vertical, estimate)

| Vertical | Current games | Target | Gap |
|----------|---------------|--------|-----|
| F&B (kopi / café / bubble tea / hawker) | 64 | 90 | +26 |
| Retail / fashion / electronics | 28 | 55 | +27 |
| Beauty / salon / spa | 12 | 45 | +33 |
| Fitness / gym / studio | 9 | 38 | +29 |
| Services (laundry, pet care, repair) | 11 | 30 | +19 |
| Hospitality (hotel, BNB, travel) | 7 | 25 | +18 |
| Education / tuition / coaching | 5 | 25 | +20 |
| Health / clinic / pharmacy | 4 | 32 | +28 |
| **TOTAL** | **140** | **340** | **+200** |

## Generation strategy — 3 phases

### Phase 1 (Week 1–2): Recipe-templating (50 games)
Take the 50 highest-performing games and create 2 parametric variations
each. Same game logic, different theme/skin/copy. Production: ELTM
pipeline runs the same wireframe with vertical-specific tokens (color +
asset slots + brand fonts).

- Output: 50 new games (mostly visual reskinning)
- Cost: ~$3 (GPT image gen) + 4 hours dev time per 10 games

### Phase 2 (Week 3–6): Vertical-specific net-new (100 games)
For each vertical with biggest gap, write 12–14 brand-new game archetypes
informed by:
1. Vertical operator interviews (5 per vertical = 40 total)
2. Successful gamified-marketing precedents in that vertical (industry
   research)
3. KiX game-grammar primitives (existing in code-soul)

Build them in 4-game weekly batches per vertical (parallel).

- Output: 100 net-new games across 8 verticals
- Cost: ~$200 (ELTM gen + Sonnet review per game)

### Phase 3 (Week 7–8): Long-tail discovery (50 games)
Stretch goals: games we don't yet know we need. Sources:
- KiX user-app suggest box (collect ideas from real players)
- Competitor library scrape + adapt (Playable, BRAME, etc.)
- Founder's "things that should exist" list

- Output: 50 exploratory games (some may fail; pruning expected)
- Cost: ~$100

## First 10 priority gaps to fill (Week 1)

Picked by: biggest revenue impact × shortest dev time.

| # | Game | Vertical | Why prioritized |
|---|------|----------|------------------|
| 1 | Latte-art Match v2 (BM/MY localized) | F&B café | MY ramp |
| 2 | Spice-level Roulette | F&B halal | Halal F&B undeserved |
| 3 | Bubble Tea Mixer v3 | F&B bubble tea | Top-performing template |
| 4 | Style Match (apparel) | Retail fashion | Retail vertical opening |
| 5 | Mystery Box Daily | Retail convenience | Daily-engagement gap |
| 6 | Color Match Challenge | Beauty nail | Beauty vertical opening |
| 7 | Spa Stress Quiz | Beauty spa | Service-based discovery |
| 8 | Attendance Streak v2 | Fitness | Highest retention impact |
| 9 | Class-Find Voucher | Fitness studio | Off-peak fill problem |
| 10 | 90-Day Challenge | Fitness | Long-tail retention play |

## Pipeline (ELTM-driven)

```
manifest (specs) → ELTM brick.gen_foundation → code-soul render →
property-oracle verify → 6-dim eval → PDCA repair → assembly → ship to library
```

Each game must pass:
- 6/6 dimensions ≥ 3.0 (S / L / B / Logic / Fidelity / Usability)
- Real-mobile playable check (Playwright on iPhone 14 viewport)
- Halal-aware filter (no gambling mechanics, no pig imagery, no alcohol primary)
- BM + EN UI default (other locales per market launch order)

## Risks

1. **Quality dilution** — 200 new games might lower avg library quality.
   Mitigation: 6-dim eval gate; rejected games go to /attic not /library.
2. **OpenRouter spend** — full 200-game gen ~$200 in LLM cost.
   Acceptable but flag in monthly review.
3. **No real merchant validation** until we have MY/HK pilots running.
   Mitigation: dogfood with SG cohort first (Phase 1+2 games go to
   SG alpha for 1 week each before promotion).

## Success metrics

- [ ] 50 Phase-1 games shipped by 2026-06-14
- [ ] 100 Phase-2 games shipped by 2026-07-12
- [ ] 50 Phase-3 games shipped by 2026-07-26
- [ ] Library size = 340 by 2026-07-30
- [ ] Vertical coverage = ≥25 games per vertical (8 verticals)
- [ ] No regression in library avg quality score (6-dim mean ≥ baseline)

## Next: I.D-2 commit

- First 10 stubs (specs only) in `data/library/specs/staging/`
- ELTM gen runs unattended via cron
- Daily progress report to founder
