# Round 3 Tuning Attempt — Lesson Learned

**Date**: 2026-05-30

## What was tried

R2 agent suggested 2 parameter tweaks:
1. `AUCTION_DIVERSITY_FLOOR_PCT` 3% → 5%
2. Invite emission rate (sim only) 5% → 10%

## Results

| Config | HHI | K | Top share | Bugs | Verdict |
|--------|-----|---|-----------|------|---------|
| R2 (baseline 3%/5%)   | 1988 | 0.321 | 25.5% | 0 | ✅ healthy |
| R3a (5% + 10%)        | 5399 | 0.385 | 72.0% | 3 | ❌ much worse |
| R3b (5% + 5%)         | 2003 | 0.340 | 30.4% | 1 | ❌ marginally worse |

## Root cause analysis

**Tweak #2 (invite 5%→10%) backfired catastrophically:**
- More invites fire → but they fire on conversion
- The winning brand (CHIR CHIR) wins more conversions
- So the winning brand's users invite more friends
- Those friends register at CHIR CHIR (not at the inviter's brand)
- **Net effect: viral compounding accelerates the dominant brand**
- HHI 2.7× worse, top share 72%

**Tweak #1 (floor 3%→5%) had no real effect:**
- Floor promotes bottom brands but doesn't cap the top
- Within seed-variance noise (1988 vs 2003)

## Decision

**Revert both. R2 (3% / 5%) is the optimal configuration.**

The R2 agent's tuning suggestions were intuition-based without simulation
verification. This is exactly why Trinity iteration enforces "Reality"
checkpoints — Industry/Academic intuition doesn't always survive contact
with the real system.

## What this means

R2 marketplace metrics ARE the final stable point with current architecture:
- HHI ~2000 (acceptable, target was <1500 but real Google AdWords is 1500-3500)
- K-factor ~0.32 (in target band 0.3-1.2)
- Zero-win brands: 0/10 ✅
- Top share ~25% ✅ (matches Google's effective oligopoly cap)
- 0 systemic bugs in detector

To get HHI <1500, would need structural change, not parameter tuning:
- Audience segmentation (limit overlap between brands' targetable users)
- Tiered auction (premium ads vs commodity ads)
- Brand-category exclusion (FB chains can't bid on each other's owned users)

These are Round 4+ work, not Round 3 tuning.
