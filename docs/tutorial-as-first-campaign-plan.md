# Tutorial-as-First-Campaign (Wave I.F — plan, not yet built)

**Why this matters (Aminah sim, Wave I.A-2):**
> "Not ready lah. Too complicated and expensive feeling. Maybe later if got
>  simpler version or someone can teach me step-by-step."

First-time merchants don't bounce because the product is bad. They bounce
because the product is empty. The cure: when they sign up, they immediately
get a pre-filled, ready-to-launch campaign that THEIR business could actually
ship — and a 3-step guided tour through it.

This replaces today's empty portal-first-load with a tour-of-an-actual-campaign.

---

## Design

### Step 1 — Country + vertical question (10 seconds)
"What kind of business are you running?"
[F&B] [Retail] [Beauty] [Fitness] [Services] [Other]

"What country?"
[SG] [MY] [ID] [TH] [HK] [VN] [Other]

That's the entire signup gating. Email/phone collected at Step 3 only.

### Step 2 — Pre-filled demo campaign appears (15 seconds)
Based on (vertical, country), portal renders a fully-configured campaign:
- Game: pre-picked from library (top performer in that vertical/country)
- Budget: S$200 / RM 700 / pre-set
- CPA target: pre-set per vertical benchmark
- Geofence: 200m around their "shop address" (we'll skip if no address yet)
- Voucher: "S$2 off coffee" / "RM 5 off teh" / vertical-appropriate
- TikTok Pixel: shown as "[Add later — works fine without]"

User sees ALL of this auto-filled. Mouse-overs explain each field.
Buttons: [Launch this for real] [Tweak first] [Just watching]

### Step 3 — Lock-in moment (only if user clicks "Launch for real")
Email + phone collected here. PDPA consent shown in user's language.
WhatsApp opt-in for ops check-ins (default ON, easily skipped).
Top-up button: S$50 / S$200 / S$500 (or local currency).

If user clicks "Just watching":
- Campaign stays in /sandbox forever (cron-cleaned after 7 days)
- Email collected with promise: "I'll send you the data even though you didn't launch"
- This catches the lurker who wants to think about it

### Step 4 — Background tour (rolls during step 2)
Bottom-right corner widget walks user through:
1. "Look — this is your wallet" (highlights wallet)
2. "Customers play here" (shows play.html in iframe)
3. "Vouchers redeem at counter" (shows redeem flow)
4. "Reports come Mondays" (shows analytics)

Each item is 8 seconds, auto-advances. Click to skip.

---

## Why this works

- Removes the "empty portal" problem: no merchant ever sees an empty dashboard
- Removes the "what should I build first" decision: we already picked
- Removes the "how does it look" mystery: they see THEIR (mock) game live
- Lock-in moment is delayed to step 3, AFTER they've seen the value

## Build plan (effort estimate: 4-6 days)

| Task | Effort | Owner |
|------|--------|-------|
| Wireframe + copy in Figma | 0.5 day | Founder |
| Backend: /api/v1/onboarding/prefill endpoint | 1 day | Backend |
| Frontend: portal flow rewrite | 2 days | Frontend |
| Game pre-fill lookup table (vertical × country) | 0.5 day | Library |
| Email + PDPA consent flow | 0.5 day | Backend |
| Sandbox campaign auto-cleanup cron | 0.3 day | Backend |
| QA across 3 verticals × 2 countries | 0.5 day | QA |
| **Total** | **5.3 days** | |

## Success metrics (after launch)

- Signup-to-launch conversion: from ~14% (today, estimated) → target 35%
- Time-to-first-launch: from ~3.2 days median → target <1 hour
- Aminah-archetype satisfaction: re-run sim with first_time_merchant
  persona after launch, target verdict ≥ "I'll try" (vs current "too complicated")

## Not in scope (this plan)

- Mobile signup app (web only for now)
- BM/CN/TH guided tour text (EN + SG-EN only for first ship; others Q4)
- Voice-guided onboarding (someday)
- Integration with TikTok-Pixel-already-configured (manual paste for now)
