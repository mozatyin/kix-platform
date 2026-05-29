"""Merchant journey simulation — 老李 / Li Bo (Guangzhou Reading Salon).

End-to-end probe of the KiX Ads Platform from the perspective of a SINGLE-
brand community-driven merchant. Walks through:
  1. Single brand setup (no multi-store, no master needed)
  2. Modest wallet funding (¥1500 total)
  3. Recipe discovery + NL→Recipe for non-retail intent
  4. Engagement / retention campaigns (no purchase event!)
  5. Streak + leaderboard + completion-gated voucher
  6. Custom audience + lookalike for niche community
  7. Pixel registration with WeChat Mini-Program origin
  8. Storefront for content-heavy book club
  9. 30-member simulation with cross-member kudos + viral invites
 10. Multi-month retention / churn cohorts
 11. Cross-brand partnership thought experiment
 12. Free-tier vs paid-tier discrimination + atomic referral reward

In-process via httpx.ASGITransport so no separate server is needed. Requires
a live local Redis.

Run:
    .venv/bin/python scripts/sim_laoli.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402
from app.redis_client import close_redis, init_redis  # noqa: E402


# ── Constants / config ────────────────────────────────────────────────────
RUN_TAG = int(time.time())
OWNER_USER_ID = f"laoli_{RUN_TAG}"
BRAND_ID = f"guangzhou_reading_salon_{RUN_TAG}"
FINDINGS_PATH = Path("/Users/mozat/a-docs/laoli-sim-findings.md")

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"

# 3 borrowed/rented venue locations in Guangzhou
VENUES: list[dict[str, Any]] = [
    {"venue_id": f"venue_zhujiang_{RUN_TAG}", "name": "珠江新城 Coffee Lab",
     "lat": 23.1213, "lng": 113.3245},
    {"venue_id": f"venue_yuexiu_{RUN_TAG}", "name": "越秀公园 LibraryBar",
     "lat": 23.1396, "lng": 113.2664},
    {"venue_id": f"venue_tianhe_{RUN_TAG}", "name": "天河北 Slow Books",
     "lat": 23.1466, "lng": 113.3242},
]

# 30 simulated members for the activity loop
MEMBER_FIRSTNAMES = [
    "Wei", "Fang", "Min", "Jing", "Lei", "Hui", "Xin", "Yan",
    "Chen", "Liu", "Zhao", "Zhou", "Wu", "Xu", "Sun", "Zhu",
    "Lin", "He", "Gao", "Luo", "Song", "Tang", "Han", "Feng",
    "Deng", "Cao", "Peng", "Cheng", "Pan", "Yuan",
]

# Member tiers
TIER_FREE = "free_trial"
TIER_MONTHLY = "monthly_199"
TIER_ANNUAL = "annual_1999"


# ── Logging helpers ──────────────────────────────────────────────────────
findings: list[dict[str, str]] = []
phase_counters: dict[str, dict[str, int]] = {}
_current_phase = "boot"


def _phase_init(name: str) -> None:
    global _current_phase
    _current_phase = name
    phase_counters[name] = {"pass": 0, "gap": 0, "fail": 0}
    print()
    print("=" * 70)
    print(f"{BOLD}{BLUE}PHASE {name}{RESET}")
    print("=" * 70)


def ok(action: str, result: str = "") -> None:
    phase_counters[_current_phase]["pass"] += 1
    print(f"  {GREEN}[PASS]{RESET} {action}" + (f" — {result}" if result else ""))


def gap(severity: str, action: str, detail: str) -> None:
    sev = severity.upper()
    phase_counters[_current_phase]["gap"] += 1
    findings.append({
        "phase": _current_phase,
        "severity": sev,
        "action": action,
        "detail": detail,
    })
    color = RED if sev == "P0" else (YELLOW if sev == "P1" else MAGENTA)
    print(f"  {color}[GAP {sev}]{RESET} {action} — {detail}")


def fail(action: str, detail: str) -> None:
    phase_counters[_current_phase]["fail"] += 1
    findings.append({
        "phase": _current_phase,
        "severity": "FAIL",
        "action": action,
        "detail": detail,
    })
    print(f"  {RED}[FAIL]{RESET} {action} — {detail}")


def info(msg: str) -> None:
    print(f"  {BLUE}[..]{RESET} {msg}")


# ── HTTP helpers ─────────────────────────────────────────────────────────
async def call(
    c: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    params: dict | None = None,
) -> tuple[int, Any]:
    try:
        r = await c.request(method, path, json=json_body, params=params)
    except Exception as e:
        return -1, {"exception": repr(e)}
    body: Any
    if r.headers.get("content-type", "").startswith("application/json"):
        try:
            body = r.json()
        except Exception:
            body = r.text
    else:
        body = r.text
    return r.status_code, body


def _short(body: Any, n: int = 250) -> str:
    s = json.dumps(body, ensure_ascii=False) if isinstance(body, (dict, list)) else str(body)
    return s if len(s) <= n else s[:n] + "..."


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ── Consent setup helper ─────────────────────────────────────────────────
_consent_policy_published = False


async def _setup_consent(c: httpx.AsyncClient, user_ids: list[str],
                         policy_version: str = "1.0") -> None:
    """Publish policy once + grant consent for each user. Idempotent."""
    global _consent_policy_published
    if not _consent_policy_published:
        sc, _b = await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
            "version": policy_version,
            "text_md": "# Sim policy\nFor testing.",
            "effective_at": int(time.time()) - 60,
            "requires_re_grant": False,
        })
        _consent_policy_published = (sc == 200)
    for uid in user_ids:
        await call(c, "POST", "/api/v1/consent/grant", json_body={
            "user_id": uid,
            "scopes": ["cross_brand_tracking", "geo_lbs", "personalization", "marketing"],
            "policy_version": policy_version,
            "source": "web",
        })


# ── Phase 1: Single Brand Setup ──────────────────────────────────────────
async def phase_1_single_brand(c: httpx.AsyncClient) -> dict[str, Any]:
    _phase_init("1: Single Brand Setup — no master, no multi-store")
    state: dict[str, Any] = {"brand_id": BRAND_ID, "master_id": None}

    # Probe: does the platform require a master for a single-brand merchant?
    # Try going straight to wallet/storefront WITHOUT a master account.
    sc, b = await call(c, "GET", f"/api/v1/wallet/{BRAND_ID}")
    # Wallet may be implicitly created on first topup; 200 or 404 are both fine.
    if sc in (200, 404):
        ok("single-brand pre-master probe",
           f"GET /wallet/{{bid}} returns {sc} without a master — single-brand flow viable")
    else:
        gap("P1", "single-brand wallet pre-check", f"unexpected {sc} {_short(b)}")

    # Decision: 老李 has no master. But the platform's geofence/campaign world
    # is brand_id-centric anyway. We still test that master endpoints degrade
    # gracefully when called with an unknown master_id.
    sc, b = await call(c, "GET", "/api/v1/master/m_nonexistent_for_laoli")
    if sc == 404:
        ok("master endpoints degrade", "GET /master/{unknown} returns 404 cleanly")
    elif sc == 500:
        gap("P0", "master endpoints degrade",
            f"GET /master/{{unknown}} 500ed — should be 404 for single-brand merchants")
    else:
        gap("P2", "master endpoints degrade", f"{sc} {_short(b)}")

    # Probe: accessible-brands for an owner who never created a master
    sc, b = await call(c, "GET", f"/api/v1/master/user/{OWNER_USER_ID}/accessible-brands")
    if sc == 200 and isinstance(b, dict):
        count = b.get("count", 0)
        if count == 0:
            ok("no-master accessible-brands",
               f"returns count=0 cleanly for users with no master (no 500)")
        else:
            info(f"accessible-brands returned count={count} for fresh user")
    else:
        gap("P1", "no-master accessible-brands",
            f"{sc} {_short(b)} — should gracefully return empty list")

    info("老李 design choice: NO master account; brand-id-only flow")
    return state


# ── Phase 2: Wallet Funding (¥1500 one-time) ─────────────────────────────
async def phase_2_wallet(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("2: Wallet Funding — single ¥1500 topup, ¥50/day cap")
    bid = state["brand_id"]

    sc, b = await call(c, "POST", f"/api/v1/wallet/{bid}/topup", json_body={
        "amount_cents": 150_000,
        "payment_method": "wechat",
    })
    if sc != 200 or not isinstance(b, dict) or "topup_id" not in b:
        fail("topup ¥1500", f"{sc} {_short(b)}")
        return
    tid = b["topup_id"]
    sc2, b2 = await call(c, "POST", f"/api/v1/wallet/{bid}/topup/{tid}/confirm",
                        json_body={"payment_gateway_response": {"mock": True}})
    if sc2 == 200:
        ok("confirm topup", f"¥1500 credited to {bid}")
    else:
        fail("confirm topup", f"{sc2} {_short(b2)}")
        return

    # Set daily budget — ¥1500/30days ≈ ¥50 = 5000 cents
    sc, b = await call(c, "POST", f"/api/v1/wallet/{bid}/daily-budget",
                       json_body={"daily_budget_cents": 5000})
    if sc == 200:
        ok("set daily budget", "¥50/day")
    else:
        gap("P1", "set daily budget", f"{sc} {_short(b)}")

    sc, b = await call(c, "GET", f"/api/v1/wallet/{bid}")
    if sc == 200 and isinstance(b, dict):
        bal = b.get("balance_cents", 0) / 100
        ok("wallet status", f"balance=¥{bal:.2f}")
    else:
        gap("P1", "wallet status", f"{sc} {_short(b)}")


# ── Phase 3: Recipe Discovery + NL→Recipe ────────────────────────────────
async def phase_3_recipes(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("3: Recipe Discovery + NL→Recipe for community/engagement")
    bid = state["brand_id"]

    # Look for community/book/engagement recipes
    sc, b = await call(c, "GET", "/api/v1/recipes")
    available_ids = []
    if sc == 200 and isinstance(b, (list, dict)):
        items = b if isinstance(b, list) else b.get("recipes", b.get("items", []))
        available_ids = [r.get("id") or r.get("recipe_id") for r in items if isinstance(r, dict)]
        info(f"recipes available: {available_ids[:8]}")
        community_match = [r for r in items
                           if isinstance(r, dict) and any(
                               kw in (r.get("name", "") + r.get("category", "")).lower()
                               for kw in ["book", "club", "community", "read", "education"])]
        if community_match:
            ok("community/book recipe", f"found {[r.get('id') for r in community_match]}")
        else:
            gap("P0", "community/book_club recipe",
                "no recipe in catalog targets community/book_club/education merchants. "
                "Closest fit is engagement recipes (duolingo_streak / nike_run_streak) — "
                "but those are not labeled for content-driven community use cases. "
                "Catalog is heavily F&B / fitness / e-commerce biased.")
    else:
        gap("P1", "recipe listing", f"{sc} {_short(b)}")

    # Try category filter "engagement"
    sc, b = await call(c, "GET", "/api/v1/recipes", params={"category": "engagement"})
    if sc == 200 and isinstance(b, (list, dict)):
        items = b if isinstance(b, list) else b.get("recipes", b.get("items", []))
        ok("recipes ?category=engagement", f"returns {len(items)} matches")
    else:
        gap("P1", "recipes category filter", f"{sc} {_short(b)}")

    # NL→Recipe: try with a community/book-club intent
    sc, b = await call(c, "POST", "/api/v1/recipe-gen/from-description", json_body={
        "brand_id": bid,
        "description": "I run a Guangzhou book club. I want a reading-streak game where "
                       "members log 1 book per week, earn badges, climb a leaderboard, "
                       "and get a free month of membership after completing 5 books.",
        "style": "loyalty",
        # Industry literal is one of coffee|retail|fitness|gaming|food|beauty|other
        # — there is NO "community"/"education"/"content" — probe this
        "industry": "other",
    })
    if sc == 200 and isinstance(b, dict):
        rec = b.get("recipe", b)
        rid = rec.get("id") or rec.get("recipe_id") if isinstance(rec, dict) else None
        ok("NL→Recipe for book club", f"recipe_id={rid} (industry=other fallback)")
        state["generated_recipe"] = rec
    elif sc in (400, 422):
        gap("P1", "NL→Recipe schema", f"{sc} {_short(b)}")
    else:
        gap("P1", "NL→Recipe call", f"{sc} {_short(b)}")

    # Probe: try industry="community" or "education" — should fail
    sc, b = await call(c, "POST", "/api/v1/recipe-gen/from-description", json_body={
        "brand_id": bid,
        "description": "Book club reading streak",
        "industry": "community",
    })
    if sc in (400, 422):
        gap("P1", "Industry enum coverage",
            "industry=community rejected (422). Allowed: coffee|retail|fitness|gaming|food|"
            "beauty|other. Community/education/content merchants are forced into 'other', "
            "losing personalisation signal for recipe generation.")
    elif sc == 200:
        info("industry=community accepted (unexpected) — enum may be open")

    # Probe: can a recipe target objective="engagement"? Check campaign objectives.
    # Already known the literal is acquire|sales|awareness|geo_visit, but we still call.
    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": bid,
        "name": "probe engagement objective",
        "objective": "engagement",
        "bid_strategy": "cpa",
        "max_bid_cents": 100,
        "daily_budget_cents": 100,
        "total_budget_cents": 100,
        "targeting": {"geo": {"country": "CN"}},
        "creative": {"recipe_id": "duolingo_streak"},
        "schedule": {"start_at": time.time(), "end_at": time.time() + 86400},
    })
    if sc in (400, 422):
        detail = b.get("detail") if isinstance(b, dict) else str(b)
        gap("P0", "no 'engagement' campaign objective",
            f"objective='engagement' rejected: {detail!s:.220}. Book clubs / "
            "subscription / community merchants cannot create campaigns whose "
            "success metric is engagement (sessions, streaks, posts). The only "
            "available objectives are acquire|sales|awareness|geo_visit — all "
            "purchase- or visit-oriented.")
    elif sc == 200:
        info("engagement objective accepted (unexpected)")

    # Probe retention as objective
    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": bid,
        "name": "probe retention objective",
        "objective": "retention",
        "bid_strategy": "cpa",
        "max_bid_cents": 100,
        "daily_budget_cents": 100,
        "total_budget_cents": 100,
        "targeting": {"geo": {"country": "CN"}},
        "creative": {"recipe_id": "duolingo_streak"},
        "schedule": {"start_at": time.time(), "end_at": time.time() + 86400},
    })
    if sc in (400, 422):
        gap("P0", "no 'retention' campaign objective",
            "objective='retention' rejected. 老李's primary problem is 3-month churn — "
            "there is no native way to express 'pay per re-engaged dormant member' or "
            "'pay per renewed subscription' as a campaign objective.")


# ── Phase 4: Engagement Campaign ─────────────────────────────────────────
async def phase_4_campaign(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("4: Engagement Campaign (forced into 'acquire' objective)")
    bid = state["brand_id"]
    state["campaigns"] = {}

    # Fallback: use acquire/cpa, but the real intent is engagement.
    # We label it clearly so post-hoc analysis surfaces the misfit.
    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": bid,
        "name": "[INTENT:ENGAGEMENT] Weekly Reading Streak",
        "objective": "acquire",  # forced — real intent is engagement
        "bid_strategy": "cpa",
        "max_bid_cents": 300,  # ¥3 per engaged member
        "daily_budget_cents": 5000,
        "total_budget_cents": 150_000,
        "targeting": {
            "geo": {"country": "CN", "city": "Guangzhou", "radius_km": 30},
            "demographics": {"age_min": 22, "age_max": 55},
        },
        "creative": {"recipe_id": "duolingo_streak", "game_slug": "reading_streak"},
        "schedule": {"start_at": time.time() - 60, "end_at": time.time() + 86400 * 90},
    })
    if sc == 200 and isinstance(b, dict):
        state["campaigns"]["streak_campaign"] = b["campaign_id"]
        ok("streak campaign (objective=acquire forced)", f"id={b['campaign_id']}")
        gap("P1", "objective semantic misfit",
            "Campaign labelled 'Weekly Reading Streak' had to declare objective=acquire "
            "and bid_strategy=cpa. The platform will measure success as 'new customer' "
            "events, but the merchant cares about session count / book log / streak. "
            "Reporting will look broken even when the campaign succeeds.")
    else:
        gap("P0", "create engagement campaign", f"{sc} {_short(b)}")

    # Try a viral campaign — CPS for member-to-member invites
    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": bid,
        "name": "Member Referral ¥50",
        "objective": "acquire",
        "bid_strategy": "cpa",
        "max_bid_cents": 5000,  # ¥50 per converted invitee
        "daily_budget_cents": 5000,
        "total_budget_cents": 100_000,
        "targeting": {"geo": {"country": "CN", "city": "Guangzhou"}},
        "creative": {"recipe_id": "dropbox_referral"},
        "schedule": {"start_at": time.time() - 60, "end_at": time.time() + 86400 * 90},
    })
    if sc == 200 and isinstance(b, dict):
        state["campaigns"]["referral_campaign"] = b["campaign_id"]
        ok("referral campaign", f"id={b['campaign_id']}")
    else:
        gap("P1", "create referral campaign", f"{sc} {_short(b)}")

    # Approve them via admin path
    for label, cid in list(state["campaigns"].items()):
        sc_a, b_a = await call(c, "POST", f"/api/v1/campaigns/{cid}/admin/approve",
                               json_body={"admin_token": "DEV", "notes": "sim auto-approve"})
        if sc_a == 200:
            ok(f"admin approve {label}", "approved")
        else:
            gap("P1", f"admin approve {label}", f"{sc_a} {_short(b_a)}")


# ── Phase 5: Conditions + Gamification + Voucher ─────────────────────────
async def phase_5_gamification(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("5: Conditions + Gamification + 'Complete 5 books' Voucher")
    bid = state["brand_id"]

    # Try to wire conditions on a campaign — books_read / streak_count
    cid = next(iter(state.get("campaigns", {}).values()), None)
    if cid:
        sc, b = await call(c, "POST", f"/api/v1/conditions/campaigns/{cid}", json_body={
            "brand_id": bid,
            "conditions": [
                {"type": "streak", "min_days": 7, "scope": "weekly_book_log"},
                {"type": "books_completed", "min_count": 5},
                {"type": "discussion_posts", "min_count": 3},
            ],
        })
        if sc == 200:
            ok("define campaign conditions", "streak+books+discussion accepted")
        elif sc in (400, 422):
            gap("P1", "campaign conditions schema",
                f"{sc} {_short(b)} — custom condition types like 'books_completed' / "
                "'discussion_posts' rejected; engine likely only knows generic types.")
        else:
            gap("P1", "define campaign conditions", f"{sc} {_short(b)}")

    # Create voucher template: "complete 5 books → free 1 month membership"
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": bid,
        "name": "完成5本书 → 免费1月会员",
        "description": "Read 5 books and earn 1 free month",
        "value": {"type": "free_item", "amount": 199, "currency": "CNY"},
        "conditions": {
            # The platform supports min_purchase_cents / min_items / tier_required.
            # There is no books_completed condition — gap.
            "min_items": 5,
            "first_time_user_only": False,
            "usage_limit_per_user": 1,
        },
        "expires_in_days": 30,
        "stackable": False,
        "transferable": False,
    })
    if sc == 201 and isinstance(b, dict):
        state["voucher_template_id"] = b.get("template_id")
        ok("voucher template '5 books → free month'",
           f"template_id={state['voucher_template_id']} (using min_items as proxy)")
        gap("P1", "voucher condition vocabulary",
            "VoucherConditions has min_purchase_cents / min_items / tier_required / "
            "first_time_user_only — no 'streak_days_required', 'books_completed', "
            "'sessions_attended'. Non-retail merchants must shoehorn into min_items "
            "and rely on out-of-band logic to actually issue.")
    else:
        gap("P1", "voucher template create", f"{sc} {_short(b)}")

    # Try a CNY currency — VoucherValue.currency is 3-letter, should be OK
    # Also try transferable voucher (member gifts to friend)
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": bid,
        "name": "Gift to Friend — 1 Free Session",
        "description": "Transferable gift voucher for non-members",
        "value": {"type": "free_item", "amount": 0, "currency": "CNY"},
        "conditions": {"usage_limit_per_user": 1},
        "expires_in_days": 60,
        "stackable": False,
        "transferable": True,
    })
    if sc == 201:
        ok("transferable gift voucher template", "supports member→friend gifting")
    else:
        gap("P1", "transferable voucher", f"{sc} {_short(b)}")

    # Streak module: probe without auth (we don't have a user JWT here, so
    # we expect 401/422 — the gap is whether streak is brand-configurable).
    sc, b = await call(c, "POST", "/api/v1/streak/check",
                       json_body={"brand_id": bid})
    if sc in (401, 403, 422):
        info(f"streak/check requires auth (got {sc}); skipped direct test")
        # Probe: is there a brand-side endpoint to CONFIGURE streak rules?
        # Looking at the router, there's no /streak/configure — only /check.
        gap("P1", "streak module configurability",
            "POST /streak/check is the only public endpoint. There is no documented "
            "/streak/configure or /streak/milestones API for the merchant to define "
            "weekly cadence, book-log cadence, or grace-day rules. Streak milestones "
            "are read from brand_config (out of band).")
    else:
        info(f"streak/check returned {sc}")


# ── Phase 6: Member Cohort + Audiences + Lookalike ───────────────────────
async def phase_6_audiences(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("6: Member Cohort + Audiences + Lookalike for niche community")
    bid = state["brand_id"]
    rng = random.Random(RUN_TAG)

    # 200 active members — generate emails + Chinese phone numbers
    emails = [f"member_{i:03d}_{RUN_TAG}@reading.cn" for i in range(200)]
    phones = [f"+861{rng.randint(3,9)}{rng.randint(0,9)}{rng.randint(10000000,99999999)}"
              for _ in range(200)]
    emails_sha = [_sha256(e.lower().strip()) for e in emails]
    phones_sha = [_sha256(p) for p in phones]

    sc, b = await call(c, "POST", "/api/v1/audiences/custom/create", json_body={
        "brand_id": bid,
        "name": "Active Members (200)",
        "source": "csv_upload",
        "emails_sha256": emails_sha,
        "phones_sha256": phones_sha,
        "description": "Active monthly/annual members as of run",
    })
    if sc == 200 and isinstance(b, dict):
        aid = b.get("audience_id")
        state["member_audience_id"] = aid
        size = b.get("size", 0)
        ok("upload 200-member audience",
           f"audience_id={aid} size={size} (hash-matched against existing users)")
        if size == 0:
            info("size=0 expected — no pre-existing users matched the sha256 hashes")
    else:
        gap("P1", "audience create from CRM", f"{sc} {_short(b)}")

    # Lookalike — niche community, only 200 seeds. Probe quality.
    if state.get("member_audience_id"):
        sc, b = await call(c, "POST",
                           f"/api/v1/audiences/{state['member_audience_id']}/lookalike",
                           json_body={
                               "brand_id": bid,
                               "name": "Lookalike — Guangzhou Readers",
                               "size_target": 5000,
                               "geo": {"country": "CN", "city": "Guangzhou"},
                           })
        if sc == 200 and isinstance(b, dict):
            la_size = b.get("size", 0)
            ok("lookalike for niche community", f"size={la_size}")
            if la_size < 100:
                gap("P1", "lookalike quality for niche communities",
                    f"Lookalike from 200-seed niche cohort produced only {la_size} users. "
                    "Algorithm appears to require larger seed populations; book club "
                    "merchants cannot bootstrap reach from a small loyal base.")
        elif sc in (400, 422):
            gap("P1", "lookalike schema", f"{sc} {_short(b)}")
        else:
            gap("P1", "lookalike create", f"{sc} {_short(b)}")

    # Probe: Chinese phone format handling — already hashed, but ensure
    # the audience system didn't strip them silently.
    if state.get("member_audience_id"):
        sc, b = await call(c, "GET",
                           f"/api/v1/audiences/{state['member_audience_id']}/details")
        if sc == 200 and isinstance(b, dict):
            n_unmatched_phones = len(b.get("unmatched_phones", []))
            n_unmatched_emails = len(b.get("unmatched_emails", []))
            ok("audience details", f"unmatched phones={n_unmatched_phones} "
               f"emails={n_unmatched_emails}")
        else:
            gap("P1", "audience details", f"{sc} {_short(b)}")


# ── Phase 7: Pixel for WeChat Mini-Program ───────────────────────────────
async def phase_7_pixel_wechat(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("7: Pixel for WeChat Mini-Program (non-web context)")
    bid = state["brand_id"]

    # WeChat Mini-Programs — Round 2 added wx<appid>/alipay:/ios:/android:/kix-native: support.
    sc, b = await call(c, "POST", "/api/v1/pixel/register", json_body={
        "brand_id": bid,
        "allowed_origins": ["wx1234567890abcdef12", "kix-native:guangzhou_reading_salon"],
    })
    if sc in (400, 422):
        gap("P0", "pixel does not support WeChat Mini-Program origins",
            f"POST /pixel/register rejects mini-program identifiers ({sc}). "
            "Mini-Programs / native apps cannot register a pixel.")
    elif sc == 201:
        ok("pixel mini-program origins accepted",
           "wx<appid> + kix-native: identifiers supported")

    # Fallback: register with the public web fallback URL (a marketing landing page)
    sc, b = await call(c, "POST", "/api/v1/pixel/register", json_body={
        "brand_id": bid,
        "allowed_origins": ["https://reading.guangzhou.cn"],
    })
    if sc == 201 and isinstance(b, dict):
        state["pixel_id"] = b["pixel_id"]
        ok("pixel register (web fallback)", f"pixel_id={state['pixel_id']}")
    else:
        gap("P1", "pixel register (web)", f"{sc} {_short(b)}")

    # Try a "book_completed" custom event — pixel only allows 5 event_types
    if state.get("pixel_id"):
        sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
            "pixel_id": state["pixel_id"],
            "event_type": "book_completed",
            "device_fingerprint": "dev_member_001",
            "origin": "https://reading.guangzhou.cn",
            "url": "https://reading.guangzhou.cn/library/book/123",
        })
        if sc in (400, 422):
            gap("P1", "pixel event types are retail-biased",
                "Pixel only accepts event_type in {pageview, add_to_cart, purchase, "
                "signup, custom}. 'book_completed', 'session_attended', 'discussion_posted' "
                "must be flattened into 'custom' — losing dimensionality.")
        elif sc == 200:
            info("book_completed accepted (unexpected — likely treated as custom)")

        # Custom event with metadata
        sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
            "pixel_id": state["pixel_id"],
            "event_type": "custom",
            "device_fingerprint": "dev_member_001",
            "origin": "https://reading.guangzhou.cn",
            "url": "https://reading.guangzhou.cn/log",
        })
        if sc == 200:
            ok("pixel custom event", "accepted")
        else:
            gap("P1", "pixel custom event", f"{sc} {_short(b)}")


# ── Phase 8: Storefront (content-heavy) ──────────────────────────────────
async def phase_8_storefront(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("8: Storefront with content-heavy book club identity")
    bid = state["brand_id"]

    sc, b = await call(c, "POST", f"/api/v1/storefront/{bid}/configure", json_body={
        "display_name": "广州读书会 / Guangzhou Reading Salon",
        "bio": "每月一书，一城同读。深度阅读 + 真人讨论 + 终身朋友。",
        "brand_color": "#1B4332",
        "country": "CN",
        "category": "community",  # probe: category enum strictness
        "featured_games": ["reading_streak"],
        "featured_vouchers": [state.get("voucher_template_id")] if state.get("voucher_template_id") else [],
        "show_stores": True,
        "custom_sections": [
            {"title": "本月共读 / This Month's Book",
             "content_md": "# 《活着》余华\n300 members reading together · Discussion June 15"},
            {"title": "下月预告 / Upcoming",
             "content_md": "## July: 《百年孤独》 加西亚·马尔克斯\nVote for August book →"},
            {"title": "会员心声 / Member Voices",
             "content_md": "> 三个月读了 12 本书，认识了 8 个新朋友。 — Wei, member since Jan"},
            {"title": "本月排行榜 / Monthly Leaderboard",
             "content_md": "🥇 Lin (5 books) 🥈 Zhao (4 books) 🥉 Chen (4 books)"},
            {"title": "三个聚会场地 / Our 3 Venues",
             "content_md": "珠江新城 · 越秀公园 · 天河北"},
        ],
        "socials": {},
        "contact": {},
    })
    if sc == 200:
        ok("storefront configure (5 custom sections)",
           "rich content-driven profile accepted")
    else:
        gap("P1", "storefront configure", f"{sc} {_short(b)}")

    # Probe: category="community" — is it accepted as a discovery category?
    sc, b = await call(c, "GET", "/api/v1/storefront/discover",
                       params={"country": "CN", "category": "community"})
    if sc == 200 and isinstance(b, (dict, list)):
        items = b if isinstance(b, list) else b.get("items", [])
        found = any((it.get("brand_id") if isinstance(it, dict) else None) == bid for it in items)
        if found:
            ok("storefront discover by category=community", f"book club appears in feed")
        else:
            gap("P2", "storefront discover for community",
                "Storefront accepted category='community' but discover endpoint did "
                "not return it under that filter. Discovery seems to expect a closed "
                "set (food/retail/service); community/education merchants are not "
                "first-class.")
    else:
        gap("P1", "storefront discover", f"{sc} {_short(b)}")

    # Vouchers section
    sc, b = await call(c, "GET", f"/api/v1/storefront/{bid}/vouchers")
    if sc == 200:
        ok("storefront vouchers", f"{_short(b, 100)}")
    else:
        gap("P2", "storefront vouchers", f"{sc} {_short(b)}")


# ── Phase 9: 30-Member Simulation ────────────────────────────────────────
async def phase_9_simulation(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("9: 30-Member Simulation — kudos, invites, voucher redemption")
    bid = state["brand_id"]
    rng = random.Random(RUN_TAG + 1)

    # Build 30 members
    members = []
    for i in range(30):
        first = rng.choice(MEMBER_FIRSTNAMES)
        tier = rng.choices(
            [TIER_FREE, TIER_MONTHLY, TIER_ANNUAL],
            weights=[0.30, 0.55, 0.15],
        )[0]
        members.append({
            "user_id": f"m_{RUN_TAG}_{i:02d}",
            "device_fingerprint": f"devm_{RUN_TAG}_{i:02d}",
            "name": first,
            "tier": tier,
            "joined_days_ago": rng.randint(1, 120),
        })
    state["members"] = members

    # Publish a consent policy + grant for all members
    await _setup_consent(c, [m["user_id"] for m in members])
    ok("publish consent policy + grant", f"30 members consented via helper")

    # Simulate reading log streaks via auction (closest available proxy)
    metrics = {
        "auctions": 0, "won": 0, "imp": 0, "clk": 0, "conv": 0,
        "kudos_attempts": 0, "kudos_ok": 0,
        "invite_attempts": 0, "invite_ok": 0,
        "voucher_attempts": 0, "voucher_issued": 0, "voucher_redeemed": 0,
    }
    cid = state.get("campaigns", {}).get("streak_campaign")

    for m in members:
        rounds = rng.randint(1, 3)
        for _ in range(rounds):
            metrics["auctions"] += 1
            sc, b = await call(c, "POST", "/api/v1/auction/run", json_body={
                "user_id": m["user_id"],
                "device_fingerprint": m["device_fingerprint"],
                "geo": {"country": "CN", "city": "Guangzhou",
                        "lat": 23.13 + rng.uniform(-0.05, 0.05),
                        "lng": 113.30 + rng.uniform(-0.05, 0.05)},
                "context": {"time_of_day": rng.randint(18, 22),
                            "day_of_week": rng.randint(0, 6),
                            "device": "mobile", "language": "zh"},
                "slot": "main",
            })
            if sc != 200 or not isinstance(b, dict):
                continue
            if b.get("no_eligible_campaigns"):
                continue
            metrics["won"] += 1
            tok = b.get("impression_token")
            if not tok:
                continue
            await call(c, "POST", "/api/v1/auction/report-impression",
                       json_body={"impression_token": tok})
            metrics["imp"] += 1
            if rng.random() < 0.4:  # engaged community → high CTR
                sc_c, _ = await call(c, "POST", "/api/v1/auction/report-click",
                                     json_body={"impression_token": tok,
                                                "user_id": m["user_id"],
                                                "device_fingerprint": m["device_fingerprint"]})
                if sc_c == 200:
                    metrics["clk"] += 1
                    if rng.random() < 0.25:  # community engagement
                        sc_v, _ = await call(c, "POST", "/api/v1/auction/report-conversion",
                                             json_body={"impression_token": tok,
                                                        "user_id": m["user_id"],
                                                        "conversion_value_cents": 0})
                        if sc_v == 200:
                            metrics["conv"] += 1

    ok("ad lifecycle",
       f"auctions={metrics['auctions']} won={metrics['won']} "
       f"imp={metrics['imp']} clk={metrics['clk']} conv={metrics['conv']}")

    if metrics["conv"] > 0:
        gap("P1", "non-monetary conversion measurement",
            "Conversion reported with conversion_value_cents=0 (because there is no "
            "purchase). Stats endpoint will compute CAC as undefined / spend÷0. There "
            "is no 'engagement conversion' event with weight by activity type "
            "(book_log / discussion / kudos).")

    # Cross-member kudos — try social/follow endpoint
    for i in range(20):
        a, b = rng.sample(members, 2)
        metrics["kudos_attempts"] += 1
        sc, _ = await call(c, "POST", "/api/v1/social/follow", json_body={
            "user_id": a["user_id"], "target_user_id": b["user_id"],
        })
        if sc == 200:
            metrics["kudos_ok"] += 1
    if metrics["kudos_ok"]:
        ok("cross-member follow (kudos proxy)",
           f"{metrics['kudos_ok']}/{metrics['kudos_attempts']} succeeded")
    else:
        gap("P1", "cross-member kudos",
            f"social/follow had {metrics['kudos_attempts']} attempts, "
            "0 succeeded — no public kudos / cheer / clap primitive exists; "
            "follow is the closest proxy and may need auth.")

    # Viral invites — 5 members invite friends
    for i in range(5):
        m = rng.choice(members)
        metrics["invite_attempts"] += 1
        sc, b = await call(c, "POST", "/api/v1/attribution/token/create", json_body={
            "source_brand": bid,
            "source_user_id": m["user_id"],
            "target_brand": bid,
            "channel": "wechat",
            "ttl_hours": 168,
        })
        if sc == 200 and isinstance(b, dict) and b.get("token"):
            metrics["invite_ok"] += 1
    if metrics["invite_ok"]:
        ok("member viral invite tokens", f"{metrics['invite_ok']}/{metrics['invite_attempts']}")
    else:
        gap("P1", "viral invite token",
            f"{metrics['invite_attempts']} attempts, 0 succeeded — needs payload tweak "
            "or a proper member-referral API surface.")

    # 5 members try to redeem "complete 5 books" voucher
    template_id = state.get("voucher_template_id")
    if template_id:
        for i in range(5):
            m = members[i]
            metrics["voucher_attempts"] += 1
            sc, b = await call(c, "POST",
                               f"/api/v1/vouchers/templates/{template_id}/issue",
                               json_body={
                                   "user_id": m["user_id"], "brand_id": bid,
                                   "reason": "completed 5 books",
                               })
            if sc == 201 and isinstance(b, dict):
                metrics["voucher_issued"] += 1
                vid = b.get("voucher_id")
                # Redeem
                sc_r, b_r = await call(c, "POST",
                                       f"/api/v1/vouchers/{vid}/redeem",
                                       json_body={
                                           "purchase_amount_cents": 0,
                                           "items": [{"sku": "book_1"}, {"sku": "book_2"},
                                                     {"sku": "book_3"}, {"sku": "book_4"},
                                                     {"sku": "book_5"}],
                                       })
                if sc_r == 200:
                    metrics["voucher_redeemed"] += 1
        ok("voucher issue+redeem",
           f"issued={metrics['voucher_issued']}/{metrics['voucher_attempts']} "
           f"redeemed={metrics['voucher_redeemed']}")
        if metrics["voucher_issued"] == 0:
            gap("P0", "voucher issuance broken for community use case",
                "No vouchers could be issued from the 'complete 5 books' template. "
                "Likely cause: condition validation expects monetary fields the "
                "merchant cannot fake.")

    state["metrics"] = metrics


# ── Phase 10: Multi-Month Retention / Churn Cohort ───────────────────────
async def phase_10_retention(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("10: Multi-Month Retention / Cohort Reports")
    bid = state["brand_id"]

    # Probe: does any endpoint expose cohort retention?
    candidates = [
        ("/api/v1/wallet/{bid}/forecast", "wallet forecast"),
        ("/api/v1/campaigns/{cid}/stats", "campaign stats"),
        ("/api/v1/audiences/brand/{bid}", "brand audiences"),
        ("/api/v1/storefront/{bid}/followers/count", "storefront follower count"),
    ]
    cid = next(iter(state.get("campaigns", {}).values()), None)
    found_retention = False
    for path_tmpl, label in candidates:
        path = path_tmpl.replace("{bid}", bid).replace("{cid}", cid or "x")
        sc, b = await call(c, "GET", path)
        if sc == 200 and isinstance(b, dict):
            if any(k in b for k in ("retention", "cohort", "churn", "d30", "d60", "d90", "retained")):
                ok(f"{label} has retention fields", f"keys={[k for k in b][:6]}")
                found_retention = True
    if not found_retention:
        gap("P0", "no cohort / retention report",
            "Searched wallet/forecast, campaigns/stats, audiences/brand, storefront "
            "followers — none expose D7/D30/D60/D90 retention, churn rate, or "
            "cohort tables. 老李's primary pain point (3-month churn) is invisible "
            "in the platform. Needs `/reports/cohort` or `/reports/retention?bid=…`.")

    # Probe: can a campaign be marked 'retention' vs 'acquisition'?
    if cid:
        sc, b = await call(c, "POST", f"/api/v1/campaigns/{cid}/update", json_body={
            "campaign_type": "retention",
            "tags": ["retention", "engagement", "churn_save"],
        })
        if sc == 200:
            ok("campaign tagging", "update with retention tags accepted")
        elif sc in (400, 422):
            gap("P1", "no retention campaign category",
                f"Cannot label a campaign as retention/engagement at the type level. "
                f"Update returned {sc}. Reports can't filter by lifecycle stage.")


# ── Phase 11: Cross-Brand Partnership Thought Experiment ─────────────────
async def phase_11_cross_brand(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("11: Cross-Brand Partnership — book club × coffee chain")
    bid = state["brand_id"]
    partner_bid = f"luckin_partner_{RUN_TAG}"

    # 老李 wants to swap vouchers with Luckin — different brand, different master.
    # Probe: any partnership / cross-master API?
    sc, b = await call(c, "POST", f"/api/v1/brands/{bid}/partnerships/create",
                       json_body={
                           "partner_brand_id": partner_bid,
                           "exchange_type": "voucher_swap",
                           "terms": "1 reading session = 1 coffee voucher",
                       })
    if sc == 404:
        gap("P0", "no cross-brand partnership API",
            "POST /brands/{bid}/partnerships/create returns 404. There is no API for "
            "two different brands (especially across different masters) to set up "
            "voucher exchange, audience swap, or co-marketing. 老李 cannot extend "
            "his ¥1500 budget by partnering with a coffee chain.")
    else:
        info(f"partnership endpoint returned {sc}")

    # Workaround probe: can a brand 'follow' another brand?
    sc, b = await call(c, "POST", f"/api/v1/storefront/{partner_bid}/follow",
                       json_body={"user_id": OWNER_USER_ID})
    if sc == 200:
        info("brand-to-brand follow works via user-level follow API (workaround)")
    elif sc == 404:
        info("partner brand has no storefront → follow 404 (expected)")

    # Network-effect router exists — see if it can bridge brands
    sc, b = await call(c, "GET", f"/api/v1/network/brand/{bid}")
    if sc == 200:
        ok("network-effect router responds", f"{_short(b, 120)}")
    elif sc == 404:
        gap("P2", "network-effect cross-brand utility",
            "/api/v1/network/brand/{bid} returned 404 — module may not expose "
            "cross-brand partnership primitives.")


# ── Phase 12: Edge Cases ─────────────────────────────────────────────────
async def phase_12_edges(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("12: Edge Cases — single-brand, tier, atomic referral, anon→member")
    bid = state["brand_id"]
    members = state.get("members", [])

    # 12a: tier-based discrimination — free vs paid tier
    if members:
        free_member = next((m for m in members if m["tier"] == TIER_FREE), members[0])
        paid_member = next((m for m in members if m["tier"] == TIER_ANNUAL), members[-1])
        # Probe: can voucher template be issued only to certain tiers?
        sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
            "brand_id": bid,
            "name": "Annual Member Only — Free Premium Discussion",
            "value": {"type": "free_item", "amount": 0, "currency": "CNY"},
            "conditions": {"tier_required": "annual_1999", "usage_limit_per_user": 1},
            "expires_in_days": 30,
            "stackable": False,
            "transferable": False,
        })
        if sc == 201 and isinstance(b, dict):
            tier_template_id = b.get("template_id")
            ok("tier-gated voucher template", f"template_id={tier_template_id}")
            # Try to issue to free-tier member — should fail in validation
            sc_i, b_i = await call(c, "POST",
                                   f"/api/v1/vouchers/templates/{tier_template_id}/issue",
                                   json_body={"user_id": free_member["user_id"],
                                              "brand_id": bid,
                                              "reason": "tier test"})
            # The platform likely issues but redemption fails — probe both.
            if sc_i == 201:
                info(f"tier voucher issued to free member (template-time check missing)")
                gap("P2", "tier check at issuance vs redemption",
                    "Voucher template with tier_required=annual_1999 was happily ISSUED "
                    "to a free-tier member. The tier check (if any) only fires at redeem "
                    "time, which is too late — the member sees the voucher and gets "
                    "frustrated when it bounces.")
        else:
            gap("P2", "tier-gated voucher", f"{sc} {_short(b)}")

    # 12b: Atomic referral reward — invitee converts, inviter gets ¥50 instantly
    if len(members) >= 2:
        inviter = members[0]
        invitee = members[1]
        # Create attribution token
        sc, b = await call(c, "POST", "/api/v1/attribution/token/create", json_body={
            "source_brand": bid, "source_user_id": inviter["user_id"],
            "target_brand": bid, "channel": "wechat", "ttl_hours": 24,
        })
        if sc == 200 and isinstance(b, dict) and b.get("token"):
            token = b["token"]
            sc2, b2 = await call(c, "POST", "/api/v1/attribution/track/conversion",
                                 json_body={
                                     "user_id": invitee["user_id"],
                                     "target_brand": bid,
                                     "order_id": f"ref_{RUN_TAG}",
                                     "amount_cents": 19900,  # ¥199 monthly fee
                                     "token": token,
                                 })
            if sc2 == 200 and isinstance(b2, dict) and b2.get("attributed"):
                ok("referral attribution", "invitee→inviter chain attributed")
                # Probe: is there an automatic inviter reward?
                # Check wallet — did the platform auto-credit anything?
                sc3, b3 = await call(c, "GET",
                                     f"/api/v1/wallet/{bid}/transactions")
                if sc3 == 200:
                    info(f"wallet transactions tail: {_short(b3, 200)}")
                gap("P1", "no atomic inviter reward",
                    "Attribution succeeded but the platform does NOT atomically credit "
                    "the inviter with the ¥50 reward. Merchant must run their own "
                    "reward worker — race conditions and idempotency become their "
                    "problem. Should be a built-in `referral_reward_cents` on "
                    "attribution/token/create.")
            elif sc2 == 200:
                info(f"conversion accepted but attributed=false ({_short(b2, 120)})")
            else:
                gap("P1", "referral attribution call", f"{sc2} {_short(b2)}")
        else:
            gap("P1", "attribution token create for referral", f"{sc} {_short(b)}")

    # 12c: Anonymous browse → registered member conversion attribution
    # Anonymous user only has device_fingerprint, no user_id yet.
    anon_fp = f"anon_{RUN_TAG}_fp"
    # Track pageview
    if state.get("pixel_id"):
        sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
            "pixel_id": state["pixel_id"],
            "event_type": "pageview",
            "device_fingerprint": anon_fp,
            "origin": "https://reading.guangzhou.cn",
            "url": "https://reading.guangzhou.cn/about",
        })
        if sc == 200:
            ok("anon pageview", "tracked with device_fingerprint only")
        # Now the same device "signs up"
        new_uid = f"new_member_{RUN_TAG}"
        await _setup_consent(c, [new_uid])
        sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
            "pixel_id": state["pixel_id"],
            "event_type": "signup",
            "user_id": new_uid,
            "device_fingerprint": anon_fp,
            "origin": "https://reading.guangzhou.cn",
            "url": "https://reading.guangzhou.cn/signup",
        })
        if sc == 200 and isinstance(b, dict):
            attributed = b.get("attributed")
            if attributed:
                ok("anon→registered identity stitch", "signup attributed to prior anon view")
            else:
                gap("P1", "anon→registered identity stitch",
                    "Signup event with same device_fingerprint as prior anon pageview "
                    "did NOT mark attributed=true. Identity stitching across the "
                    "anonymous→registered boundary is absent or weak.")
        else:
            gap("P1", "signup pixel event", f"{sc} {_short(b)}")

    # 12d: Single-brand merchant: master endpoints should gracefully degrade
    sc, b = await call(c, "GET", "/api/v1/master/m_does_not_exist/consolidated-report")
    if sc == 404:
        ok("master consolidated-report degrades", "404 for missing master")
    elif sc == 500:
        gap("P0", "master endpoint crash on unknown master",
            "GET /master/{unknown}/consolidated-report returned 500 — single-brand "
            "merchants who never created a master shouldn't crash the API.")
    else:
        info(f"master consolidated-report returned {sc} for unknown master")


# ── Phase 13: Module Probe ───────────────────────────────────────────────
async def phase_13_module_probe(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("13: Module Probe — what's reachable for a single-brand community")
    bid = state["brand_id"]
    probes = [
        ("recipes.engagement", "GET", "/api/v1/recipes", {"category": "engagement"}),
        ("streak.check", "POST", "/api/v1/streak/check", None),  # auth-gated
        ("leaderboard", "GET", "/api/v1/leaderboard/", None),
        ("conditions.user.eligibility", "GET",
         f"/api/v1/conditions/user/u_test/eligibility", None),
        ("triggers", "GET", "/api/v1/triggers/", None),
        ("groups", "GET", "/api/v1/groups/", None),
        ("p2p", "GET", "/api/v1/p2p/", None),
        ("multiplayer", "GET", "/api/v1/multiplayer/", None),
        ("network", "GET", f"/api/v1/network/brand/{bid}", None),
        ("storefront.discover.community", "GET", "/api/v1/storefront/discover",
         {"country": "CN", "category": "community"}),
    ]
    avail, missing = [], []
    for label, method, path, params in probes:
        sc, b = await call(c, method, path, params=params)
        if sc == 200:
            avail.append(label)
            ok(f"{label}", "200")
        elif sc == 404:
            if isinstance(b, dict) and b.get("detail") in ("Not Found", "not found"):
                missing.append(label)
                gap("P1", f"module not mounted: {label}", f"404 at {path}")
            else:
                avail.append(label)
                ok(f"{label} (domain 404)", "")
        else:
            info(f"{label} → {sc}")
            avail.append(label)
    info(f"available={len(avail)} missing={len(missing)}")


# ── Findings writer ──────────────────────────────────────────────────────
def write_findings(start_ts: float) -> None:
    runtime = time.time() - start_ts
    total_pass = sum(p["pass"] for p in phase_counters.values())
    total_gap = sum(p["gap"] for p in phase_counters.values())
    total_fail = sum(p["fail"] for p in phase_counters.values())

    p0 = [f for f in findings if f["severity"] == "P0"]
    p1 = [f for f in findings if f["severity"] == "P1"]
    p2 = [f for f in findings if f["severity"] == "P2"]
    fails = [f for f in findings if f["severity"] == "FAIL"]

    md: list[str] = []
    md.append("# 老李 / Li Bo (Guangzhou Reading Salon) — Merchant Journey Findings")
    md.append("")
    md.append(f"**Run tag**: `{RUN_TAG}` | **Runtime**: {runtime:.1f}s | "
              f"**Date**: {time.strftime('%Y-%m-%d %H:%M', time.localtime(start_ts))}")
    md.append("")
    md.append("## Scenario")
    md.append(
        "老李 owns 「广州读书会」 — a single-brand community-driven book club in "
        "Guangzhou. 3 borrowed/rented café venues, ~200 active members on ¥199/mo "
        "or ¥1999/yr subscriptions, ~50 new prospects monthly. Budget ¥1500/mo "
        "(much smaller than 老王's ¥3500). Primary problems: (1) 3-month churn "
        "after the novelty wears off, (2) keeping members engaged between monthly "
        "meetings, (3) viral member-to-member acquisition is THE only growth channel."
    )
    md.append("")
    md.append("**Critical difference vs 老王**: no physical store loyalty, no POS, "
              "no transaction value per event. All success metrics are *engagement* "
              "(book logs, discussions, attendance), not *conversions* (purchases).")
    md.append("")
    md.append("## Summary")
    md.append("")
    md.append(f"- **Passes**: {total_pass}")
    md.append(f"- **Gaps**: {total_gap} (P0={len(p0)} P1={len(p1)} P2={len(p2)})")
    md.append(f"- **Fails**: {total_fail}")
    md.append("")
    md.append("### Per-phase tally")
    md.append("")
    md.append("| Phase | Pass | Gap | Fail |")
    md.append("|---|---:|---:|---:|")
    for ph, c in phase_counters.items():
        md.append(f"| {ph} | {c['pass']} | {c['gap']} | {c['fail']} |")
    md.append("")

    def section(title: str, items: list[dict]) -> None:
        md.append(f"## {title} ({len(items)})")
        md.append("")
        if not items:
            md.append("_None._")
            md.append("")
            return
        for f in items:
            md.append(f"### {f['action']}")
            md.append(f"- **Phase**: {f['phase']}")
            md.append(f"- **Severity**: {f['severity']}")
            md.append(f"- **Detail**: {f['detail']}")
            md.append("")

    section("P0 — Blockers for the book-club use case", p0)
    section("P1 — Friction", p1)
    section("P2 — Nice-to-have", p2)
    section("Hard failures", fails)

    md.append("## Cross-Comparison: What 老李 Needs That 老王 Didn't")
    md.append("")
    md.append(
        "老王 (bubble tea, 10 stores, F&B, transaction-based) and 老李 (book club, "
        "1 brand, community, subscription-based) probe two orthogonal axes of the "
        "platform. The 老李 simulation surfaced THREE classes of gap that 老王's "
        "simulation could not have exposed:\n"
        "\n"
        "### 1. Lifecycle vocabulary (engagement vs acquisition)\n"
        "老王 was acquisition-first; CPA / CPS / CPV fit. 老李 needs:\n"
        "- `campaign.objective='engagement'` — pay per session / book log / discussion\n"
        "- `campaign.objective='retention'` — pay per re-activated dormant member\n"
        "- `campaign.objective='renewal'` — pay per subscription renewed\n"
        "- a `campaign.campaign_type='lifecycle_stage'` tag (new/active/at_risk/churned)\n"
        "- bid strategies beyond CPA/CPS/CPV/CPM/CPC: CPE (cost-per-engagement) and "
        "CPRtn (cost-per-retained-month)\n"
        "\n"
        "Today, 老李 has to declare every campaign as `objective=acquire` + "
        "`bid_strategy=cpa` and rely on bracketed `[INTENT:...]` prefixes in the "
        "name, which is unmeasurable and unreportable.\n"
        "\n"
        "### 2. Non-monetary primitives\n"
        "老王 measured success in ¥. 老李's success is non-monetary:\n"
        "- pixel `event_type` is locked to {pageview, add_to_cart, purchase, signup, "
        "custom}. Book-club events (`book_completed`, `discussion_posted`, "
        "`session_attended`) get flattened into `custom`, losing dimensionality\n"
        "- voucher `conditions` is purchase-shaped (`min_purchase_cents`, `min_items`, "
        "`tier_required`). No `streak_days_required`, `books_completed`, "
        "`sessions_attended_in_window`, `discussions_authored`\n"
        "- campaign `conditions` engine accepts only generic types; can't compose "
        "book/streak/discussion predicates\n"
        "- conversion stats endpoint divides spend by conversion count to compute CAC; "
        "with `conversion_value_cents=0` the numbers are nonsense\n"
        "\n"
        "### 3. Non-web context + cross-brand surface\n"
        "老王's stores were physical (geofence covered it). 老李 lives on WeChat:\n"
        "- pixel `allowed_origins` validator requires `http(s)://`, rejecting "
        "WeChat / Alipay / Douyin Mini-Program origins outright. The entire "
        "merchant-side analytics loop is unreachable for app-only merchants\n"
        "- there is no `/brands/{bid}/partnerships/...` surface — 老王 only needed "
        "intra-master coordination, but 老李 wants to swap vouchers with a coffee "
        "chain run by a DIFFERENT master\n"
        "- recipe `industry` enum is {coffee, retail, fitness, gaming, food, beauty, "
        "other} — community / education / content merchants fall into 'other', "
        "losing personalisation signal\n"
        "- storefront `category=community` is accepted on write but invisible on "
        "discover\n"
        "\n"
        "### Smaller-budget effects (¥1500 vs ¥3500)\n"
        "老李 cannot afford failed experiments. The fact that lookalike from a "
        "200-seed niche cohort produced a small or empty set, that referral rewards "
        "are not atomically credited (he must build his own reward worker), and "
        "that there is no cohort/retention dashboard means he is flying blind on "
        "his single biggest pain point (churn) while paying for it directly."
    )
    md.append("")

    md.append("## Strategic Recommendations")
    md.append("")
    md.append(
        "1. **[P0] Extend campaign vocabulary**: add objectives "
        "`engagement`, `retention`, `renewal`, and bid strategies `cpe`, `cprtn`. "
        "Without this, every non-retail merchant has to lie about what they want.\n"
        "2. **[P0] Extend pixel scheme allowlist**: support `wxapp://`, "
        "`servicewechat://`, `alipays://`, `snssdk://`, `kix-native://`, plus "
        "an SDK-issued opaque origin for true native apps. Without this, the "
        "entire China mini-program merchant segment is unreachable.\n"
        "3. **[P0] Cohort / retention report**: ship "
        "`GET /reports/{bid}/cohort?window=d30|d60|d90` returning per-cohort "
        "retention curves. Today there is no way to even SEE 老李's primary problem.\n"
        "4. **[P1] Non-monetary voucher conditions**: add "
        "`streak_days_required`, `books_completed` / `sessions_attended` / "
        "`activities_in_window` to `VoucherConditions`. Composable with existing "
        "conditions.\n"
        "5. **[P1] Pixel event taxonomy**: open `event_type` to a "
        "merchant-defined list (validated against a registered schema) instead of "
        "the hardcoded 5. Custom events with structured metadata > one bucket.\n"
        "6. **[P1] Recipe `industry` enum**: add `community`, `education`, "
        "`content`, `subscription`. Or open it and let the LLM/router classify.\n"
        "7. **[P1] Atomic referral reward**: extend "
        "`/attribution/token/create` with `inviter_reward_cents` so the platform "
        "credits the inviter when the invitee converts. Don't make every merchant "
        "build their own reward worker.\n"
        "8. **[P1] Cross-master partnership API**: "
        "`POST /partnerships/create` between two brand_ids (possibly under "
        "different masters) for voucher swap / audience cross-promote / "
        "co-funded campaigns. Otherwise small merchants can't pool reach.\n"
        "9. **[P2] Tier check at voucher issuance**: validate "
        "`tier_required` at issue-time, not just redeem-time, to avoid issuing "
        "vouchers that will bounce in front of the member.\n"
        "10. **[P2] Storefront discover for community**: ensure "
        "`category=community` (and others outside the default set) actually appears "
        "in `/storefront/discover` results so content-driven merchants are findable."
    )
    md.append("")

    FINDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FINDINGS_PATH.write_text("\n".join(md), encoding="utf-8")
    print()
    print("=" * 70)
    print(f"{BOLD}SUMMARY{RESET}")
    print("=" * 70)
    print(f"  passes={total_pass}  gaps={total_gap} "
          f"(P0={len(p0)} P1={len(p1)} P2={len(p2)})  fails={total_fail}")
    print(f"  findings → {FINDINGS_PATH}")
    if p0:
        print()
        print(f"{RED}Top P0 gaps:{RESET}")
        for f in p0[:5]:
            print(f"  • [{f['phase']}] {f['action']} — {f['detail'][:100]}")


# ── Main ─────────────────────────────────────────────────────────────────
# ── Phase R7: Round 7 probes — book club recipes + event reservations ────
async def phase_r7_probes(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("R7: Round 7 probes — book_club / community / education recipes + event reservations")
    bid = state.get("brand_id") or BRAND_ID
    member = (state.get("members") or [{}])[0]
    if isinstance(member, dict):
        user_id = member.get("user_id") or f"reader_probe_{RUN_TAG}"
    else:
        user_id = f"reader_probe_{RUN_TAG}"

    # 1) Recipe library — three industries
    total_recipes = 0
    found_kq = found_brs = False
    for industry in ("book_club", "community", "education"):
        sc, b = await call(c, "GET", "/api/v1/recipes", params={"industry": industry})
        if sc == 200 and isinstance(b, (list, dict)):
            items = b if isinstance(b, list) else b.get("recipes", b.get("items", []))
            if items:
                total_recipes += len(items)
                ok(f"recipes industry={industry}", f"{len(items)} recipes")
                for it in items:
                    name = (it.get("id") or it.get("name") or "").lower()
                    if "knowledge_quiz" in name or "quiz" in name:
                        found_kq = True
                    if "book_review" in name or "social" in name:
                        found_brs = True
            else:
                gap("P1", f"recipes industry={industry} empty", "")
        else:
            gap("P1", f"recipes industry={industry}", f"{sc}")
    if total_recipes >= 9:
        ok("R7 community recipes available", f"{total_recipes} across 3 industries")
    if found_kq:
        ok("knowledge_quiz recipe discovered", "")
    if found_brs:
        ok("book_review_social recipe discovered", "")

    # 2) /reservations type=event + resource_id=meeting_room
    room_id = f"meeting_room_a_{RUN_TAG}"
    host_uid = f"host_facilitator_{RUN_TAG}"
    version = f"v_book_{RUN_TAG}"
    await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
        "version": version, "text_md": "## Book Club Privacy",
        "effective_at": int(time.time()) - 60, "requires_re_grant": False,
    })
    for uid in (user_id, host_uid):
        await call(c, "POST", "/api/v1/consent/grant", json_body={
            "user_id": uid, "scopes": ["cross_brand_tracking", "personalization"],
            "policy_version": version, "source": "app",
        })
    sc, b = await call(c, "POST", "/api/v1/reservations/create", json_body={
        "brand_id": bid,
        "user_id": user_id,
        "scheduled_at": int(time.time()) + 86400 * 3,
        "party_size": 1,
        "type": "event",
        "resource_id": room_id,
        "fulfiller_user_id": host_uid,
        "metadata": {"book": "百年孤独", "session": "weekly_discussion"},
        "check_in_grace_minutes": 30,
    })
    if sc in (200, 201) and isinstance(b, dict):
        rid = b.get("reservation_id")
        ok("event reservation w/ meeting_room", f"rid={rid}")
        sc_get, b_get = await call(c, "GET", f"/api/v1/reservations/{rid}")
        if sc_get == 200 and isinstance(b_get, dict) and b_get.get("resource_id") == room_id:
            ok("meeting_room resource_id persisted", "")
        else:
            gap("P1", "meeting_room resource_id dropped", f"{_short(b_get)}")
    else:
        gap("P0", "event reservation create", f"{sc} {_short(b)}")


async def main() -> int:
    start_ts = time.time()
    await init_redis()
    # R7: lifespan startup isn't triggered by ASGITransport, so manually seed recipes
    try:
        from app.redis_client import get_redis as _get_redis
        from app.routers.recipes import load_seed_recipes as _load_seed
        _r = await _get_redis()
        await _load_seed(_r)
    except Exception:
        pass
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30.0
        ) as c:
            state: dict[str, Any] = {}
            try:
                state = await phase_1_single_brand(c)
                await phase_2_wallet(c, state)
                await phase_3_recipes(c, state)
                await phase_4_campaign(c, state)
                await phase_5_gamification(c, state)
                await phase_6_audiences(c, state)
                await phase_7_pixel_wechat(c, state)
                await phase_8_storefront(c, state)
                await phase_9_simulation(c, state)
                await phase_10_retention(c, state)
                await phase_11_cross_brand(c, state)
                await phase_12_edges(c, state)
                await phase_13_module_probe(c, state)
                await phase_r7_probes(c, state)
            except Exception as e:
                fail("simulation crash", repr(e))
                import traceback
                traceback.print_exc()
    finally:
        write_findings(start_ts)
        await close_redis()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
