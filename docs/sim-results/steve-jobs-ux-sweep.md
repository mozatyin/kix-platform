# Steve Jobs UX Teardown - KiX full website sweep

**Date:** 2026-05-31 11:47

**Method:** Claude Sonnet 4.5 role-playing Steve Jobs critic. Real rendered DOM via playwright (sequential render, parallel critique).
**Pages swept:** 14 (index, pricing, enterprise, sg-case-studies, my-case-studies, fnb, tiktok, pos, trinity-artifacts, calculator, new-customer, portal, storefront, play)

**Severity legend:** P0 = drives users away . P1 = cheap-looking / breaks flow . P2 = polish miss

---

## index

[`http://localhost:8765/landing/index.html`](http://localhost:8765/landing/index.html)

[P0 . COPY] "Free SaaS. CPA from S$3 / RM 11" - mixing currencies mid-sentence is confusing, pick one or say "from S$3" and let regional users see their currency elsewhere

[P0 . HIERARCHY] Hero section buries the lede - "Pay only for verified new customers" should be the H1, not "The Gamification Marketing Platform" which means nothing

[P0 . COPY] "5 SG F&B pilots live" - calling them "pilots" screams beta/unproven, say "5 Singapore F&B brands" or just drop it

[P0 . INTERACTION] "Get started" button appears 3 times before fold with zero differentiation - no idea if clicking takes me to signup, demo request, or docs

[P1 . COPY] "Three forces are pushing CAC up and retention down" then lists percentages with no source, timeframe, or geography - feels made up

[P1 . SPACING] Pricing section "FREE FOREVER $0/month" - the $0 is redundant and the slash-month implies recurring billing you just said doesn't exist

[P1 . HIERARCHY] "34 verticals supported" with emoji grid - this is filler, either show real logos or cut it to 6 icons max

[P1 . COPY] "Nanyang Tea: 91% win rate...delivered 41 new customers" - 41 customers is embarrassingly small, either aggregate the number or focus on the win rate only

[P1 . TRUST] Customer testimonials have no photos, no job titles, no last names - "Nanyang Tea F&B · 10 stores" could be your cousin's coffee cart

[P1 . COPY] "contractual new-customer definition" as inline link mid-stats-block - either make it a footnote or own it as a trust section, this feels defensive

[P1 . TYPE] "footer.refund", "footer.aup", "footer.cookies" - you shipped with i18n keys visible in production footer

[P1 . MOBILE] Language picker "EN-SG | English (SG) | 简体中文 (新加坡)..." in footer is 9 options with no grouping - will be a horizontal scroll nightmare on mobile

[P2 . COPY] "Built with Trinity Protocol · 525 routes · 5 iteration rounds" in footer - internal jargon that means nothing to a merchant, cut it

[P2 . SPACING] Compliance section "🇸🇬 PDPA Singapore" emoji flags with two-line descriptions creates uneven vertical rhythm, align or use icons

[P2 . ACCESSIBILITY] Emoji as semantic icons (🎮🎯🔗📊) with no aria-label or alt - screen readers will say nothing or "game controller"

---

## pricing

[`http://localhost:8765/landing/pricing.html`](http://localhost:8765/landing/pricing.html)

[P0 . COPY] "Free SaaS. Pay only for results." — contradictory headline. If it's free, why am I paying? Should be "No upfront cost. Pay only for results." or similar.

[P0 . HIERARCHY] Pricing table buries the lede — "FREE FOREVER" column has zero differentiation from "PAY FOR ACQUISITION" visually. Free tier should be a hero card or the paid tier should be clearly secondary.

[P0 . COPY] "S$3 – S$30" CPA range is absurdly wide. A 10x spread tells me nothing. Either show typical rates by vertical or don't show ranges at all.

[P0 . TRUST] "First 100 merchants free, every country" — no country counter, no urgency proof. Claims scarcity but shows zero evidence. Add "23 slots left in Singapore" or kill the claim.

[P1 . COPY] "All gamification capabilities" repeated twice in free tier — lazy. First instance should say what capabilities ARE (spin-the-wheel, scratch cards, etc.).

[P1 . HIERARCHY] "Questions you're probably asking" section has identical visual weight to pricing tiers above. FAQ should be visually subordinate — smaller type, lighter background, or collapsed accordions.

[P1 . COPY] "Auction-based bidding with flexible settlement options" — jargon soup. Merchants don't know what this means. Say "You set your bid, higher bids get more customers" or similar.

[P1 . SPACING] Pricing cards have no breathing room — text crammed edge-to-edge. Needs 24-32px internal padding minimum.

[P1 . INTERACTION] "CPA / CPS / CPM / CPV / CPE" — five pricing models with zero explanation of when to use which. Either cut to two models or add one-line "best for X" under each.

[P1 . COPY] "Your campaigns stay live until the budget runs out, then auto-pause" — sounds like I might accidentally spend money I didn't mean to. Should emphasize control: "Set a budget cap, pause anytime."

[P2 . TYPE] "S$3 – S$30" uses en-dash, "5%-15%" uses hyphen. Pick one dash style and stick to it.

[P2 . COPY] "Founding merchants get permanent 0% commission" — you already said "never pay any CPA/CPS take rate" one sentence prior. Redundant.

[P2 . ACCESSIBILITY] "See SG case studies →" and "MY operator archetypes & projections →" links have no visual distinction from body text except the arrow. Underline or color them.

[P2 . MOBILE] Footer "Verify independently: X · GitHub · HN · Reddit · ACRA" — five links with middots will wrap awkwardly on mobile. Stack or use proper spacing.

---

## enterprise

[`http://localhost:8765/landing/enterprise.html`](http://localhost:8765/landing/enterprise.html)

[P0 . HIERARCHY] "FOR MULTI-OUTLET BRANDS · QSR · LOYALTY PROGRAMS" eyebrow is same visual weight as body text - enterprise buyer can't tell if this is the right page in 0.5 seconds

[P0 . COPY] "You've been burned by loyalty SaaS before" headline is presumptuous - assumes pain that may not exist, alienates buyers who haven't been burned

[P0 . TRUST] "5 SG F&B brands running KiX live" - zero logos, zero names, zero proof. Enterprise buyers will assume you're lying or the brands are embarrassingly small

[P0 . HIERARCHY] The 6-column comparison table ("WHAT YOU'VE BEEN BURNED BY" vs "HOW KIX IS STRUCTURED") is unreadable - text is tiny, rows blur together, no visual separation

[P1 . COPY] "📛 We've heard the Playable / Eber / Salesforce-Loyalty horror stories" - emoji + naming competitors reads desperate, not confident

[P1 . SPACING] Zero whitespace between "What enterprise evaluators actually need" headline and the 8-card grid - cards slam into headline

[P1 . INTERACTION] "Request reference match →" button promises a reference but card copy admits "We don't have one yet" - why is this a CTA if you can't deliver?

[P1 . TYPE] Entire page uses same font size for body text - no typographic hierarchy between critical info and supporting copy

[P1 . COPY] "Sarah Chen + Sandeep Kumar archetype interviews 2026-05" - meaningless jargon that screams "we read a playbook"

[P1 . COLOR] All CTAs are same blue - no visual priority between "Request eval call" (primary) and "See policy draft →" (tertiary)

[P1 . SPACING] Pricing table has zero padding on mobile breakpoints - text touches cell borders

[P1 . COPY] "What a pilot looks like (Sarah Chen archetype — 3-café chain)" - repeating the archetype name is cringe, just say "3-café chain pilot example"

[P2 . COPY] "Stop reading. Get on a call." - trying too hard to sound Jobsian, comes off as pushy

[P2 . SPACING] Footer has 6 lines of micro-copy crammed together - "© 2026 KiX · letskix.com · Terms· Privacy" needs breathing room

[P2 . ACCESSIBILITY] "📜📈🏆🔒🔌🌏💼📊" emoji icons in 8-card grid have no alt text and convey zero meaning to screen readers

---

## sg-case-studies

[`http://localhost:8765/landing/sg-case-studies.html`](http://localhost:8765/landing/sg-case-studies.html)

[P0 . HIERARCHY] "SINGAPORE · F&B CASE STUDIES" title fights with "KiX" logo - unclear if this is a KiX page or a separate site until you read 3 paragraphs in

[P0 . COPY] "interviewed 2026-05-22" - you're claiming a future date (it's 2024), kills all credibility instantly

[P0 . TRUST] "Mozat Pte Ltd · since 2001" at bottom contradicts alpha startup narrative - are you 23 years old or in alpha? Pick one story

[P1 . HIERARCHY] Five case studies with identical card layouts blur together - no visual weight difference between $0 founding slot and $2.4K paying customer

[P1 . COPY] "Real merchants. Real numbers. Real honesty." - saying "real" three times screams fake, just show the numbers

[P1 . SPACING] "90-DAY COHORT DATA" section has table then another table then bullet list with zero breathing room - feels like three designers fought

[P1 . TYPE] Mixing "S$7.20" and "S$2.4K" and "S$890" notation - pick dollars or thousands notation and stick to it

[P1 . INTERACTION] "Apply for SG pilot →" button appears twice (top right nav + bottom CTA) with different styling - which one is real?

[P1 . COPY] "Sarah-Chen-archetype proof block" - you left developer placeholder text in production

[P1 . COLOR] All metrics use same visual weight (black text, no color coding) - can't scan for good vs bad numbers at a glance

[P2 . COPY] "⚠ projected" emoji in table feels apologetic - either own the projection methodology or don't show it

[P2 . SPACING] Footer has 3 lines of dense text + 6 links with no grouping - looks like you ran out of space

[P2 . HIERARCHY] "Honesty disclaimer" paragraph is same size/weight as merchant quotes - should be smaller/lighter

[P2 . MOBILE] Tables with 5+ columns ("Window / Heng Heng / SG café baseline / Delta") will be unreadable on mobile - no responsive strategy visible

---

## my-case-studies

[`http://localhost:8765/landing/my-case-studies.html`](http://localhost:8765/landing/my-case-studies.html)

[P0 . COPY] "meta.title" as page title - literally shows a translation key instead of actual text
[P0 . COPY] Every nav link shows raw keys: "nav.home", "nav.pricing", "nav.sg_cases" - entire i18n system is broken
[P0 . COPY] "hero.title", "hero.sub", "HERO.PILL" all raw keys - hero is unreadable
[P0 . COPY] "hero.disclaimer.title hero.disclaimer.body" - disclaimer renders as concatenated keys with no spacing
[P0 . COPY] "cta.title", "cta.sub", "cta.btn" - CTA section completely broken, no one knows what action to take
[P0 . COPY] "footer.copy footer.terms· footer.privacy· footer.contact" - footer links are translation keys
[P0 . INTERACTION] Language switcher lists 12 languages but page is stuck showing keys - implies switching does nothing
[P1 . HIERARCHY] Case study cards have no visual separation - white text on white, no borders, no cards, just floating sections
[P1 . SPACING] "RM 47→RM 18" stat has arrow touching numbers - needs hair space
[P1 . TYPE] Stat labels like "CAC TARGET (TIKTOK→KIX)" mix caps, parens, arrows inconsistently - pick one pattern
[P1 . COPY] "Kopi Senandung-archetype" - why expose "-archetype" suffix to users? Internal naming leaked
[P1 . COPY] "Ahmad bin Hashim-archetype (composite of 3 MY chain CEOs interviewed 2026-04 to 2026-05)" - dates are in the future, breaks trust
[P1 . COPY] Every case ends with "-archetype" - "Penang Roti Canai-archetype", "KL Café-archetype" - remove this internal label
[P1 . HIERARCHY] "PROJECTED" vs "EARLY ACCESS" pills have no visual distinction - same weight, same position
[P1 . SPACING] Stats grid (4 columns) has no vertical rhythm - "RM 47→RM 18" sits directly on "CAC TARGET" with no breathing room
[P1 . COPY] "Founding-100-MY slot claimed: 0% take rate forever" - buried in setup notes, should be a hero stat if it's the hook
[P1 . TRUST] "composite of 3 MY chain CEOs interviewed 2026-04 to 2026-05" - attribution undermines the quote's authenticity, pick one or the other
[P2 . SPACING] "footer.terms· footer.privacy· footer.contact" uses middle-dot but "X· GitHub· HN· Reddit· ACRA" inconsistent spacing around dots
[P2 . TYPE] "100 OUTLETS (NATIONAL)" mixes caps label with title-case descriptor - commit to one
[P2 . COPY] "BM-first UI" - "BM" undefined on first use, spell out "Bahasa Malaysia" once
[P2 . ACCESSIBILITY] No skip-to-content link, no landmark roles visible in DOM text
[P2 . MOBILE] 4-column stat grids will crush on mobile - no indication of responsive behavior

---

## fnb

[`http://localhost:8765/landing/verticals/fnb.html`](http://localhost:8765/landing/verticals/fnb.html)

[P0 . COPY] "Free for the first 100 F&B merchants in your country" — which country? User has no idea if slots are gone or available, creates FOMO paralysis instead of urgency.

[P0 . HIERARCHY] Hero headline "Turn every coffee, every kopi, every bubble tea into a marketing channel" is vague marketing speak — doesn't tell me WHAT KiX is. Bury the lede (gamification) until line 2.

[P0 . COPY] "75% of one-time customers within 30 days" stat has no source, no asterisk, no credibility anchor — reads like made-up SaaS landing page filler.

[P1 . COPY] "Start free — 5 min" vs "11 min AVG SETUP TIME" — you're lying in the CTA or the stat block. Pick one number.

[P1 . HIERARCHY] Four-stat grid (S$5.80 / 28% / 3.2x / 11 min) has no labels visible in the body text you provided — just floating numbers. Unreadable.

[P1 . COPY] "See 5 SG cases" button — why 5? Why not 3 or 10? Arbitrary number kills trust. Just say "See case studies" or "SG examples."

[P1 . SPACING] Six game cards with emoji + title + description + CPA — no visual separation between cards in the DOM text. Reads like a wall of emoji soup.

[P1 . COPY] "CPA S$4.20 · café avg" — CPA means cost-per-acquisition to marketers, but F&B operators don't speak that language. Say "S$4.20 per new customer" or you lose the audience.

[P1 . TRUST] "© 2026 KiX" — it's 2025. Typo or time traveler? Kills credibility either way.

[P1 . COPY] "0% take rate forever" — no F&B operator believes 'forever' promises from a SaaS. Say "0% commission" or explain the business model or you sound like a scam.

[P2 . COPY] "Halal-aware library" in footer — what does 'aware' mean? Either you're halal-certified or you're not. Weasel word.

[P2 . HIERARCHY] Navigation has "Portal" with no context — portal for whom? Merchants? Players? Ambiguous.

[P2 . COPY] "Pick from 22 F&B archetypes" but you only show 6 game cards — where are the other 16? Feels incomplete.

[P2 . MOBILE] Language picker lists 11 languages in footer — guaranteed unreadable dropdown on mobile, and no F&B owner is toggling between Arabic and Hebrew.

---

## tiktok

[`http://localhost:8765/landing/integrations/tiktok-pixel.html`](http://localhost:8765/landing/integrations/tiktok-pixel.html)

[P0 . HIERARCHY] "Short answer below. Long answer: how events flow..." is confusing meta-commentary that makes user parse instructions instead of just reading content - delete it entirely

[P0 . COPY] "what your TikTok Ads Manager team will actually see" assumes enterprise org structure most SMBs don't have - say "what you'll see in TikTok Ads Manager"

[P0 . INTERACTION] Code block shows comments as if they're instructions ("# In your TikTok Ads Manager...") but formatting makes it look like code to paste - break actual instructions OUT of the code fence

[P1 . COPY] "Does KiX integrate with my TikTok Pixel?" headline is a question the page title already answered - cut straight to "Native TikTok Pixel + Events API integration"

[P1 . SPACING] Events table has no vertical breathing room between rows - add 12-16px row padding so scanning doesn't feel cramped

[P1 . HIERARCHY] "Setup — what you actually do" uses em-dash which renders inconsistently cross-browser and looks like a typo - use colon or drop the separator

[P1 . COPY] "TikTok-savvy operators" is trying too hard to sound insider - say "common questions" or just "FAQ"

[P1 . TYPE] Code fence font size looks smaller than 13px making tokens hard to verify visually - bump to 14px minimum

[P1 . TRUST] "We've validated this with TikTok Ads Manager v3.4+" cites a version number no one can verify and will age badly - remove version or say "current TikTok Ads Manager"

[P1 . COPY] "Match rate in our SG pilots: 78–84%" is oddly specific for a range and "pilots" sounds unfinished - say "Singapore merchants see 78-84% match rates"

[P2 . SPACING] Footer has "Terms· Privacy· Contact" with mid-dots but inconsistent spacing around them - pick spaces or no spaces consistently

[P2 . ACCESSIBILITY] Emoji in flow diagram (📱⚡🎯) have no alt text or aria-label - screen readers will skip or read unicode names

[P2 . COPY] "© 2026 KiX" is either a typo or trying to look futuristic but just looks wrong - use 2024 or 2025

[P2 . MOBILE] "Open portal →" button likely too close to footer links on mobile - add 32px bottom margin minimum

---

## pos

[`http://localhost:8765/landing/integrations/pos-integrations.html`](http://localhost:8765/landing/integrations/pos-integrations.html)

[P0 . HIERARCHY] "POS · INTEGRATION GUIDE" header is visually weak - looks like breadcrumb text, not a page title. Needs 2-3x size + weight.

[P0 . COPY] "Q3 2026" for StoreHub is buried in table - if it's not live, don't list it as "Status: LIVE" equivalent. Move to "Coming soon" section or mark CLEARLY as unavailable.

[P0 . INTERACTION] "Open portal →" button appears 3 times (header, body CTA, footer) but zero indication what happens when you click - does it require login? Create account? Show demo? Add one-line subtext.

[P1 . COPY] "Will POS integration be a 3-month IT nightmare?" - weak hook. You're selling to operators who already know it's a nightmare. Cut the question, lead with "Most merchants ship in 15 minutes."

[P1 . HIERARCHY] Two-column layout for "ZERO-IT PATH" vs "DEEP-SYNC PATH" would make the fork instant to scan. Current stacked blocks require reading to understand the choice.

[P1 . SPACING] Provider matrix table has cramped "Setup time" column - "1 day" / "2 days" feels arbitrary without context. Add "(with IT)" or "(self-serve)" qualifier.

[P1 . COPY] "5 SG / 3 MY archetypes" is jargon soup - what's an archetype? Say "8 Singapore chains, 3 Malaysia chains" if that's what you mean.

[P1 . TYPE] Body text is too small for a B2B decision-maker skimming on mobile. Bump base size from ~14px to 16-17px.

[P1 . COLOR] "LIVE" status badges in green would make table scannable in 2 seconds. Currently all-text, requires reading every cell.

[P1 . TRUST] "Founder reachable on WhatsApp" - no WhatsApp link/number. Either add the actual contact or delete the claim.

[P2 . COPY] "Wallet charges and CPA tracking work without ever touching your POS" - "wallet charges" is unclear. Say "You pay per acquisition" or whatever the actual mechanic is.

[P2 . SPACING] FAQ section has no visual separation between Q&A pairs - add hairline or extra margin between questions.

[P2 . COPY] "Mozat Pte Ltd" in footer with no explanation - is KiX a Mozat product? Rebrand? Clarify relationship in one line.

[P2 . MOBILE] Language picker dropdown in header lists 11 languages - will be unusable on mobile. Collapse to icon + modal.

[P2 . ACCESSIBILITY] Flow diagram uses emoji (🎮🛒📱📊) as the only visual indicator - add text labels for screen readers.

---

## trinity-artifacts

[`http://localhost:8765/landing/trinity-artifacts.html`](http://localhost:8765/landing/trinity-artifacts.html)

[P0 . HIERARCHY] "TRINITY 3T · SIM ARTIFACTS" headline is jargon soup - a first-time visitor has zero idea what Trinity, 3T, or sim artifacts mean. Should be "How We Test Every Page Against Real User Personas" or similar plain English.

[P0 . COPY] "Real users simulated. Real verdicts captured." - contradictory. Either they're real users OR simulated. Pick one. "Real personas, simulated sessions, unfiltered verdicts" would work.

[P0 . TRUST] The entire premise (AI simulating users) sounds like marketing theater, but there's no "Why this matters" or "What we learned" section. Just showing transcripts without synthesis makes this feel like process porn, not customer value.

[P0 . HIERARCHY] Sarah Chen's quote is buried in body text with no visual distinction - it's the strongest social proof on the page but looks like filler. Needs pull-quote treatment, larger type, different background.

[P1 . COPY] "Six rounds. Four personas. Twenty-plus transcripts." - so what? No context for why a visitor should care. Add "Every page change is stress-tested before you see it" or similar benefit.

[P1 . SPACING] The "How a Trinity 3T sim round works" three-step breakdown has identical visual weight for each step - no hierarchy. Step ③ Compare is the payoff but looks identical to ① Render.

[P1 . COPY] "Playwright loads the actual landing page in headless Chromium with the persona's locale + timezone" - way too technical for a landing page. Cut to "We test with real browsers in your customer's language and timezone."

[P1 . HIERARCHY] The four persona cards have massive walls of text in "REMAINING FRICTION" sections that make the page feel like a bug tracker, not a customer-facing artifact. Either hide behind accordions or cut 70%.

[P1 . COLOR] The percentage badges "70% CLOSED" / "80% CLOSED" / "50% CLOSED" have no color coding - 50% and 80% look identical. Use red/yellow/green or don't show percentages at all.

[P1 . COPY] "R1 (RAW HTML, DEEPSEEK)" and similar version labels are internal jargon. Customers don't care about your commit strategy. Show dates: "March 2026 → May 2026" progression instead.

[P1 . INTERACTION] "Full transcripts linked" appears in body copy but there are no visible links to full transcripts anywhere in the persona cards.

[P1 . COPY] "HEAD 951f0a1" - git commit hash on a customer-facing page is engineer navel-gazing. Cut it or hide in a tooltip.

[P1 . TYPE] The code block `python -m scripts.sim_users_v2 --persona ahmad_kopi_chain...` is for developers, not the "real café owner" personas you just spent 2000 words describing. Wrong audience or wrong page.

[P2 . SPACING] The "See the enterprise page →" CTA appears twice (header nav and bottom of page) but has zero visual prominence - same size as body text, no button treatment.

[P2 . COPY] "The sim infrastructure is open" - "open" is vague. Open-source? Open for inspection? Open to run yourself? Clarify or cut.

[P2 . ACCESSIBILITY] The persona verdict quotes use colored text (green for positive shifts) but no other indicator - fails for colorblind users.

---

## calculator

[`http://localhost:8765/landing/calculator.html`](http://localhost:8765/landing/calculator.html)

[P0 . INTERACTION] Calculator inputs have no visible values or placeholders - user has no idea what numbers to enter or what's pre-filled
[P0 . HIERARCHY] "S$600K COST @ TODAY'S TIKTOK CAC" appears before user enters any data - looks broken, undermines trust in tool
[P0 . COPY] "Pilot recommended at 5-10 to validate before scaling" appears as help text but user hasn't entered outlet count yet - cart before horse
[P0 . EMPTY] No default state messaging when calculator loads - just empty fields and giant numbers that make no sense yet

[P1 . COPY] "How does KiX compare to your current TikTok / Meta ad spend?" - you don't compare to spend, you compare to results/efficiency
[P1 . HIERARCHY] "KIX PROJECTION (90-DAY)" section header has same weight as input labels - should dominate
[P1 . COPY] "Projected CPA decay (day 0 → day 90)" - "decay" sounds bad, you mean "improvement" or "reduction"
[P1 . SPACING] Zero visual separation between input section and projection section - they bleed together
[P1 . TYPE] "S$600K" and "S$77K" use same size/weight - the S$523K savings should be 3x bigger, that's the hero number
[P1 . COPY] "+1687% projected ROAS lift (0.32x today → 5.70x projected)" - burying the lede in parentheses with confusing math
[P1 . INTERACTION] "auto-filled from benchmark" and "auto-switches by country" - nothing visible shows this is happening
[P1 . TRUST] "© 2026 KiX" - it's 2024 or 2025, looks like a template mistake

[P2 . COPY] "Affects benchmark CPA + return-rate assumptions" - jargon soup, say "Changes your projected results"
[P2 . SPACING] Assumptions disclaimer is a wall of text with no breathing room between bullets
[P2 . COPY] "founding 100 vs standard" in take-rate label - unexplained insider term
[P2 . ACCESSIBILITY] No visible focus states mentioned, no indication fields are interactive beyond label text
[P2 . TYPE] "sg-case-studies" and "enterprise eval call" buttons in header have identical styling - no CTA hierarchy

---

## new-customer

[`http://localhost:8765/landing/legal/new-customer-definition.html`](http://localhost:8765/landing/legal/new-customer-definition.html)

[P0 . TRUST] "effective 2026-05-31" — it's 2025. Either this is a typo that makes you look incompetent or you're showing a template before it's live. Fix the year.

[P0 . HIERARCHY] Wall of text with no visual breaks. 8 numbered sections with dense paragraphs = instant bounce. Add section cards, background tints, or collapse/expand to make this scannable.

[P0 . COPY] "Playable / Eber pattern" — name-dropping competitors in legal docs is petty and confusing. Say "some loyalty platforms charge for repeat customers" without the callout.

[P1 . SPACING] Zero whitespace between "KiX Enterprise Pricing Legal Center" nav and "CONTRACTUAL DEFINITION" headline. Feels like a rendering bug.

[P1 . TYPE] "CONTRACTUAL DEFINITION" all-caps eyebrow + "What counts as..." headline = two competing H1s. Pick one hierarchy.

[P1 . HIERARCHY] "v1.0 · effective 2026-05-31 · bound into MSA Schedule A" buried in body text. This is critical metadata — put it in a metadata card or sidebar, not inline.

[P1 . COPY] "Why this page exists: Enterprise evaluators we've spoken to in 2026..." — you're explaining why the page exists IN the page. Cut this or move to a collapsed "Background" section. Prospects don't care about your user research process.

[P1 . INTERACTION] "Submit an edge case" button appears twice (once mid-page, once footer). Redundant and makes the first one feel like a mistake.

[P1 . COLOR] All body text is black on white with zero color accents except links. The ✗ bullets in §2 are the only visual relief. Add status colors (green for "counts", red for "doesn't count") or section color-coding.

[P1 . SPACING] §3 "Worked example" has same text size/weight as the bullets above it. Needs italic, indent, or background tint to signal it's illustrative.

[P1 . TYPE] Monospace "POST /api/v1/brand/{id}/customers" inline with body text breaks reading rhythm. Needs code styling (background pill, smaller size).

[P1 . HIERARCHY] §5 fraud table has no visual separation from surrounding paragraphs. Needs border, background, or at minimum more top/bottom margin.

[P1 . COPY] "git log landing/legal/new-customer-definition.html" — showing internal file paths in customer-facing legal docs is sloppy. Say "version history available on request" or link to a changelog page.

[P2 . ACCESSIBILITY] No skip-to-content link. This is a long doc; keyboard users are stuck tabbing through the entire nav.

[P2 . COPY] "Got a 14th edge case we should add?" — why 14th? There aren't 13 cases listed above. Confusing and feels like a copy-paste error.

[P2 . SPACING] Footer "© 2026 KiX · letskix.com · v1.0 · Terms · Privacy..." is a run-on sentence of links with middots. Break into logical groups with actual spacing.

[P2 . MOBILE] Language picker in header shows 10+ languages in a horizontal list. On mobile this will wrap into chaos or overflow. Needs a dropdown.

---

## portal

[`http://localhost:8765/landing/portal.html`](http://localhost:8765/landing/portal.html)

[P0 . HIERARCHY] Multiple complete sign-in/sign-up forms visible simultaneously - user sees "Sign in", "Create merchant account", and "Send reset link" all at once instead of one flow
[P0 . INTERACTION] "Google | Microsoft | Apple" buttons render as plain text with pipes, not actual OAuth buttons - completely broken
[P0 . COPY] "New to KiX? Sign up as merchant" - awkward phrasing, should be "New to KiX? Create merchant account" or just "Sign up"
[P0 . HIERARCHY] Navigation elements bleeding through: "Wallet: $1,234.56", "Last 7 days", "Create campaign" visible on sign-in page - suggests logged-in state leaking into logged-out view
[P1 . SPACING] Password reset form has "← Back to sign in" AND "← Back to KiX home" - two back links is confusing, pick one
[P1 . COPY] "Ads Manager · Sign in to your merchant account" - the middot is trying too hard to be Apple, just use a dash or new line
[P1 . HIERARCHY] "EN-SG" language selector buried at bottom, but also "EN / 中文" appears in button list - inconsistent placement and formatting
[P1 . TRUST] No "Terms" or "Privacy Policy" links anywhere on sign-in page - looks amateur for a payment platform
[P1 . COPY] "Min 8 characters" placeholder in password field - should be helper text below field, not placeholder that disappears
[P1 . COPY] "e.g. Toast Box · Tampines" example for business name uses middot again - just use a comma or dash like normal people
[P2 . TYPE] "6-digit code" button text suggests 2FA flow but appears in main button list - context unclear
[P2 . ACCESSIBILITY] No visible labels on "Email" and "Password" fields if they're using placeholder-only pattern
[P2 . SPACING] Country dropdown shows 5 options but no visual indication it's scrollable or if there are more countries

---

## storefront

[`http://localhost:8765/landing/storefront.html`](http://localhost:8765/landing/storefront.html)

[P0 . COPY] "Search stores, cuisines, brands…" placeholder appears twice in nav - confusing and looks like a bug
[P0 . HIERARCHY] "STORES ON KIX" headline buried below fold after giant nav - users land on page with no clear entry point
[P0 . COPY] "storefront.cat.fnb" and "storefront.cat.beauty" are raw database slugs showing to users - embarrassing
[P0 . INTERACTION] "Search by store name, category, or location" input has no visible search button or enter hint - dead end
[P0 . EMPTY] "12 stores" for entire Singapore platform screams ghost town - hide count or explain it's filtered/beta

[P1 . SPACING] Filter pills "All | Food & Drink | Retail..." have no visual grouping or container - float awkwardly
[P1 . HIERARCHY] "Featured" badge appears 3 times but looks identical to regular stores except tiny yellow tag - weak differentiation
[P1 . COPY] "Browse thousands of merchant storefronts" when showing 12 - blatant lie kills trust immediately
[P1 . TYPE] Store card text hierarchy is flat - rating, reviews, distance all same visual weight as store name
[P1 . SPACING] Footer "Browse by category" section has 6 categories but only 4 are filterable above - inconsistent
[P1 . INTERACTION] Category filter buttons have no selected state shown - can't tell what's active
[P1 . COPY] "Play games. Earn rewards." in hero but zero indication which stores have games - empty promise

[P2 . SPACING] "⌘K" keyboard shortcut shown but search is already visible - wasted space
[P2 . COLOR] "Featured" yellow tag too subtle against white cards - needs stronger contrast
[P2 . ACCESSIBILITY] Star ratings shown as "4.9" with no aria-label or visual stars - screen reader unfriendly

---

## play

[`http://localhost:8765/landing/play.html`](http://localhost:8765/landing/play.html)

[P0 . COPY] "Sign up to launch your own brand | Try nasi | bubble tea | café | K" in header is word salad - reads like broken navigation, not a CTA
[P0 . HIERARCHY] "60/100" giant number has zero context until you read tiny text below - users will stare confused at a random score
[P0 . INTERACTION] "ENERGY 0/100 —" shows zero energy but doesn't explain how to get energy or why I'm blocked - dead end
[P0 . EMPTY] "Loading…" for "Daily streak" and "Top players today" never resolves - looks broken, not like intentional demo state
[P0 . COPY] "Step 1 of 3 — Tap the ☕ to play" but the coffee emoji is tiny and buried in a card - not obviously tappable
[P1 . HIERARCHY] "🎮 DEMO MODE" banner at top fights with "DEMO · THIS IS WHAT A REAL CUSTOMER SEES" card at bottom - pick one demo indicator
[P1 . COPY] "Kopi Senandung Demo · DEMO" repeats DEMO twice in same element - redundant and amateurish
[P1 . SPACING] Filters sidebar (CATEGORY / TIME TO PLAY) has no visual separation from game grid - bleeds together
[P1 . INTERACTION] "Clear filters" button present when no filters are active - shouldn't show in default state
[P1 . TRUST] "Real customers play for 60 seconds. Tap to skip ahead in the demo." - admitting the demo is fake undermines the whole experience
[P1 . COPY] "Playing as a customer of Kopi Senandung Demo" - awkward phrasing, should be "You're playing as a customer of..."
[P2 . TYPE] "K KiX" logo has weird line break making it two lines instead of inline
[P2 . SPACING] Footer links (Help | Terms | Privacy) crammed with no breathing room
[P2 . ACCESSIBILITY] Language picker "EN-SG" in footer with no label - not obvious it's a language selector

---
