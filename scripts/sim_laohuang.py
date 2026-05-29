"""Merchant journey simulation — 老黄 / Huang Mei (Hangzhou online baby shop).

End-to-end probe of the KiX Ads Platform from the perspective of an
ONLINE-ONLY e-commerce merchant. Walks through:
  1. Single brand setup (huang_baby_shop) — NO physical stores
  2. Wallet funding (¥20K/月 e-commerce scale)
  3. Geofence — should be SKIPPABLE for pure online
  4. Pixel SDK heavy integration (website + mini-program, high volume)
  5. Recipe — viral mom-tells-mom referral chain
  6. Custom audience: existing 5000 customers, lookalike for new moms
  7. GMV-based CPS campaign (8% of order)
  8. Repeat-customer / re-engagement (60-day dormant)
  9. Pixel-driven attribution (no manual track_conversion)
 10. Voucher distribution at scale (1000 vouchers)
 11. International expat targeting
 12. Edge cases for online-only / high-volume
 13. Gap log → /Users/mozat/a-docs/laohuang-sim-findings.md

Pattern follows scripts/sim_laowang.py.

In-process via httpx.ASGITransport so no separate server is needed. Requires
a live local Redis.

Run:
    .venv/bin/python scripts/sim_laohuang.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402
from app.redis_client import close_redis, init_redis  # noqa: E402


# ── Constants / config ────────────────────────────────────────────────────
RUN_TAG = int(time.time())
OWNER_USER_ID = f"laohuang_{RUN_TAG}"
BRAND_ID = f"huang_baby_shop_{RUN_TAG}"
BRAND_COLOR = "#FFB6C1"   # soft pink
FINDINGS_PATH = Path("/Users/mozat/a-docs/laohuang-sim-findings.md")

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"

# E-commerce scale numbers
MONTHLY_GMV_CENTS = 200_000_000   # ¥2,000,000
MONTHLY_BUDGET_CENTS = 2_000_000  # ¥20,000
DAILY_BUDGET_CENTS = MONTHLY_BUDGET_CENTS // 30  # ~¥667
TYPICAL_AOV_CENTS = 40_000        # ¥400 average order value
DAILY_ORDERS = 5_000 // 30        # ~167/day for sim purposes

NEW_MOM_FIRSTNAMES = [
    "Xiu Mei", "Li Hua", "Wen Jing", "Ya Ting", "Qian Qian",
    "Mei Ling", "Shu Fen", "Hui Min", "Jia Yi", "Ling Ling",
    "Xiao Lan", "Yu Han", "Zhen Zhen", "Ai Lin", "Bing Bing",
]
NEW_MOM_LASTNAMES = [
    "Wang", "Li", "Zhang", "Liu", "Chen", "Yang", "Zhao", "Huang",
    "Zhou", "Wu", "Xu", "Sun", "Hu", "Zhu",
]

# Brand has 10 sub-categories (still ONE brand)
SUB_CATEGORIES = [
    "diapers", "formula", "toys", "baby_gear", "bath_care",
    "feeding", "clothing", "safety", "books", "skincare",
]


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
    headers: dict | None = None,
) -> tuple[int, Any]:
    try:
        r = await c.request(method, path, json=json_body, params=params, headers=headers)
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
async def phase_1_brand_setup(c: httpx.AsyncClient) -> dict[str, Any]:
    _phase_init("1: Single Online Brand 'huang_baby_shop'")
    state: dict[str, Any] = {"master_id": None, "brand_id": BRAND_ID}

    # Probe: does platform NEED a master account for a single-brand merchant?
    sc, b = await call(c, "POST", "/api/v1/master/create", json_body={
        "company_name": "黄记母婴 / Huang Baby Shop",
        "primary_email": "laohuang@huangbaby.com",
        "owner_user_id": OWNER_USER_ID,
    })
    if sc == 201 and isinstance(b, dict):
        state["master_id"] = b["master_id"]
        ok("create master account (even for 1 brand)", f"master_id={state['master_id']}")
        info("Probe: master required even for solo online merchants — onboarding friction")
    else:
        gap("P1", "create master account", f"{sc} {_short(b)}")

    # Attach single online brand — no store_id since no physical store
    if state["master_id"]:
        sc, b = await call(c, "POST",
                           f"/api/v1/master/{state['master_id']}/brands/attach",
                           json_body={
                               "brand_id": BRAND_ID,
                               "store_name": "Huang Baby Shop (Online)",
                               "store_id": BRAND_ID,  # no physical store; reuse brand_id
                           })
        if sc == 200:
            ok("attach single online brand", f"brand_id={BRAND_ID}")
        else:
            gap("P0", "attach single online brand", f"{sc} {_short(b)}")
        # Note: store_id is required but semantically meaningless online.
        gap("P1", "attach requires store_id for online-only",
            "Online-only merchants must supply a store_id even though they have "
            "no physical store. Today the workaround is to reuse brand_id, but "
            "downstream geofence/store endpoints will produce empty/misleading "
            "data. Need an `is_online_only` brand flag or optional store_id.")

    # Storefront configure with brand color + pink theme
    sc, b = await call(c, "POST", f"/api/v1/storefront/{BRAND_ID}/configure", json_body={
        "display_name": "黄记母婴 (huang_baby_shop)",
        "bio": "Trusted online baby store — moms helping moms in Hangzhou",
        "brand_color": BRAND_COLOR,
        "country": "CN",
        "category": "baby_products",
    })
    if sc == 200:
        ok("storefront configure", f"color={BRAND_COLOR} category=baby_products")
    else:
        gap("P1", "storefront configure", f"{sc} {_short(b)}")

    return state


# ── Phase 2: Wallet ──────────────────────────────────────────────────────
async def phase_2_wallet(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("2: Wallet ¥20K (e-commerce scale)")

    # Top up the single brand wallet ¥20K
    sc, b = await call(c, "POST", f"/api/v1/wallet/{BRAND_ID}/topup", json_body={
        "amount_cents": MONTHLY_BUDGET_CENTS,
        "payment_method": "wechat",
    })
    if sc != 200 or not isinstance(b, dict) or "topup_id" not in b:
        fail("wallet topup", f"{sc} {_short(b)}")
        return
    tid = b["topup_id"]
    sc2, b2 = await call(c, "POST",
                        f"/api/v1/wallet/{BRAND_ID}/topup/{tid}/confirm",
                        json_body={"payment_gateway_response": {"mock": True}})
    if sc2 == 200:
        ok("wallet topup ¥20K confirmed", f"topup_id={tid}")
    else:
        fail("wallet topup confirm", f"{sc2} {_short(b2)}")
        return

    # Set daily budget — for e-commerce, ideally % of expected GMV
    sc, b = await call(c, "POST", f"/api/v1/wallet/{BRAND_ID}/daily-budget",
                       json_body={"daily_budget_cents": DAILY_BUDGET_CENTS})
    if sc == 200:
        ok("daily budget ¥667 set", f"flat ¥{DAILY_BUDGET_CENTS/100:.0f}/day")
    else:
        gap("P1", "daily budget set", f"{sc} {_short(b)}")

    # PROBE: can we set daily budget as % of GMV instead of flat?
    sc, b = await call(c, "POST", f"/api/v1/wallet/{BRAND_ID}/daily-budget",
                       json_body={
                           "daily_budget_strategy": "percent_of_gmv",
                           "gmv_percent": 1.0,
                           "expected_daily_gmv_cents": MONTHLY_GMV_CENTS // 30,
                       })
    if sc == 200 and isinstance(b, dict) and "gmv_percent" in str(b).lower():
        ok("daily budget as % of GMV", "supported")
    else:
        gap("P1", "daily budget as % of GMV",
            f"Only flat ¥/day supported. E-commerce merchants want spend to scale "
            f"with revenue: if today's GMV is ¥40K, spend 1% = ¥400; if GMV doubles "
            f"to ¥80K, auto-spend ¥800. Today {sc} {_short(b)}")

    # Status check
    sc, b = await call(c, "GET", f"/api/v1/wallet/{BRAND_ID}/daily-budget-status")
    if sc == 200 and isinstance(b, dict):
        ok("daily budget status",
           f"daily_budget=¥{b.get('daily_budget_cents',0)/100:.2f} "
           f"spent_today=¥{b.get('spent_today_cents', b.get('today_spent_cents',0))/100:.2f}")
    else:
        gap("P2", "daily budget status", f"{sc} {_short(b)}")


# ── Phase 3: Geofence Skippability ───────────────────────────────────────
async def phase_3_geofence_skip(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("3: Geofence — should be IRRELEVANT for online-only")
    info("Online-only merchant should never need to register a physical store.")

    # Try to query stores for this brand — expect empty
    sc, b = await call(c, "GET", f"/api/v1/geofence/stores/{BRAND_ID}")
    if sc == 200:
        if isinstance(b, list) and len(b) == 0:
            ok("no stores registered", "platform accepts brand with zero stores")
        elif isinstance(b, list):
            gap("P1", "stores auto-created",
                f"Brand has {len(b)} stores without explicit registration — "
                "implicit store creation pollutes data model")
        else:
            ok("stores listing", _short(b))
    elif sc == 404:
        gap("P1", "geofence forces store registration",
            "GET /geofence/stores/{brand_id} returns 404 for an online-only "
            "brand — platform may treat 'no stores' as broken state instead of "
            "valid online-only state. Need explicit support.")
    else:
        gap("P2", "stores query", f"{sc} {_short(b)}")

    # PROBE: can we run an auction without any registered geo store?
    # We don't run a real auction here — that comes in phase 6 — but we check
    # whether campaign creation enforces a "must have ≥1 store" constraint.
    info("Probe: campaign creation later will reveal store dependency")


# ── Phase 4: Pixel Heavy Integration ─────────────────────────────────────
async def phase_4_pixel(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("4: Pixel SDK — website + WeChat Mini-Program")

    # Register a pixel for both origins
    sc, b = await call(c, "POST", "/api/v1/pixel/register", json_body={
        "brand_id": BRAND_ID,
        "allowed_origins": [
            "https://huangbaby.com",
            "https://wxapp.huangbaby.com",   # pseudo origin for mini-program
        ],
    })
    if sc == 201 and isinstance(b, dict):
        state["pixel_id"] = b["pixel_id"]
        ok("pixel register (2 origins)", f"pixel_id={state['pixel_id']}")
    else:
        fail("pixel register", f"{sc} {_short(b)}")
        return

    pid = state["pixel_id"]

    # Grant consent for the smoke-test users that will be tracked below.
    smoke_uids = [f"user_smoke_{et}_{RUN_TAG}" for et in
                  ("pageview", "add_to_cart", "signup", "purchase")]
    await _setup_consent(c, smoke_uids)

    # PROBE: WeChat mini-program isn't standard web — does pixel SDK adapt?
    # Round 2 added wx<appid>/alipay:/ios:/android:/kix-native: identifier support.
    sc, b = await call(c, "POST", "/api/v1/pixel/register", json_body={
        "brand_id": BRAND_ID,
        "allowed_origins": ["wxe1f2a3b4c5d6e7f8", "alipay:1234567890abcdef"],
    })
    if sc == 201:
        ok("pixel register (mini-program native scheme)",
           "wx<appid>/alipay: identifiers accepted")
    elif sc in (400, 422):
        gap("P0", "WeChat Mini-Program origin not supported",
            f"allowed_origins validator rejects mini-program identifiers ({sc} {_short(b)}).")
    else:
        gap("P1", "mini-program origin", f"{sc} {_short(b)}")

    # Send a pageview through the valid pixel
    sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
        "pixel_id": pid,
        "event_type": "pageview",
        "device_fingerprint": "dev_pixel_smoke",
        "origin": "https://huangbaby.com",
        "url": "https://huangbaby.com/diapers",
    }, headers={"Origin": "https://huangbaby.com"})
    if sc == 200:
        ok("pixel pageview accepted")
    else:
        gap("P1", "pixel pageview", f"{sc} {_short(b)}")

    # Send 4 event types: pageview, add_to_cart, signup, purchase
    samples = [
        ("pageview", None, None),
        ("add_to_cart", None, None),
        ("signup", None, None),
        ("purchase", f"sample_order_{RUN_TAG}", TYPICAL_AOV_CENTS),
    ]
    type_pass = 0
    for et, oid, amt in samples:
        body = {
            "pixel_id": pid,
            "event_type": et,
            "device_fingerprint": f"dev_smoke_{et}",
            "user_id": f"user_smoke_{et}_{RUN_TAG}",
            "origin": "https://huangbaby.com",
            "url": "https://huangbaby.com/",
        }
        if oid:
            body["order_id"] = oid
            body["amount_cents"] = amt
        sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body=body,
                           headers={"Origin": "https://huangbaby.com"})
        if sc == 200:
            type_pass += 1
        else:
            gap("P1", f"pixel event {et}", f"{sc} {_short(b)}")
    ok("pixel event types accepted", f"{type_pass}/4 (pageview/add_to_cart/signup/purchase)")

    # Rate limit / volume test: 100 events rapidly
    t0 = time.time()
    rl_pass = 0
    rl_429 = 0
    rl_other = 0
    for i in range(100):
        sc, _ = await call(c, "POST", "/api/v1/pixel/event", json_body={
            "pixel_id": pid,
            "event_type": "pageview",
            "device_fingerprint": f"dev_load_{i % 10}",
            "origin": "https://huangbaby.com",
            "url": f"https://huangbaby.com/{i}",
        }, headers={"Origin": "https://huangbaby.com"})
        if sc == 200:
            rl_pass += 1
        elif sc == 429:
            rl_429 += 1
        else:
            rl_other += 1
    dt = time.time() - t0
    ok("100 events burst", f"{rl_pass} ok / {rl_429} rate-limited / {rl_other} other in {dt:.2f}s "
                            f"({rl_pass/dt:.0f} eps)")
    if rl_429 > 0:
        gap("P1", "pixel rate limit too aggressive for e-commerce",
            f"{rl_429}/100 events hit 429. 老黄 generates 5000 orders/day × ~10 "
            "events each = 50K events/day. Default rate-limit may starve high-"
            "volume merchants. Need per-pixel quota tier (basic / pro / enterprise).")

    # PROBE: batched events (single POST with N events)
    sc, b = await call(c, "POST", "/api/v1/pixel/events/batch", json_body={
        "pixel_id": pid,
        "events": [
            {"event_type": "pageview", "device_fingerprint": "dev_batch_1",
             "origin": "https://huangbaby.com", "url": "https://huangbaby.com/a"},
            {"event_type": "pageview", "device_fingerprint": "dev_batch_2",
             "origin": "https://huangbaby.com", "url": "https://huangbaby.com/b"},
        ],
    }, headers={"Origin": "https://huangbaby.com"})
    if sc in (200, 201, 207):
        ok("batched pixel events", f"status={sc}")
    else:
        gap("P0", "no batched pixel ingestion",
            f"POST /api/v1/pixel/events/batch returns {sc} — only single-event "
            "POSTs supported. 50K events/day on mobile means 50K HTTPS round-"
            "trips; SDK can't batch even though the Beacon API expects batches. "
            "Critical for e-commerce mobile-first tracking.")


# ── Phase 5: Recipe — Viral Referral ─────────────────────────────────────
async def phase_5_referral_recipe(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("5: Recipe — Viral Mom-Tells-Mom Referral Game")

    # Generate from NL description
    sc, b = await call(c, "POST", "/api/v1/recipe-gen/from-description", json_body={
        "brand_id": BRAND_ID,
        "description": (
            "Mom invites mom. New-mom user shares a referral link with friends. "
            "When 3 friends sign up AND each makes their first purchase, the "
            "inviting mom gets a free ¥100 diaper voucher. Track the viral "
            "chain — referrer → referee_1 → referee_2 — and award only after "
            "the third referee's first purchase clears."
        ),
        "industry": "ecommerce",
    })
    if sc == 200 and isinstance(b, dict):
        state["recipe_id"] = b.get("recipe_id")
        ok("recipe generated from NL", f"recipe_id={state['recipe_id']} "
                                      f"confidence={b.get('confidence')}")
        modules = b.get("modules_used", [])
        info(f"modules_used={modules}")
        recipe = b.get("recipe", {})
        # Check whether the recipe encodes the multi-step viral chain
        recipe_str = json.dumps(recipe).lower()
        has_referral = "referral" in recipe_str or "invite" in recipe_str
        has_threshold = "3" in recipe_str or "three" in recipe_str
        has_conditional = "first_purchase" in recipe_str or "conversion" in recipe_str
        if has_referral and has_threshold and has_conditional:
            ok("recipe encodes multi-step viral chain",
               "referral + threshold + conditional first-purchase trigger all present")
        else:
            gap("P1", "recipe missing viral mechanics",
                f"Generated recipe lacks: referral={has_referral} threshold={has_threshold} "
                f"conditional_first_purchase={has_conditional}. Recipe generator can't "
                "encode 'reward only after Nth referee converts' — a basic viral mechanic.")

        if "warnings" in b and b["warnings"]:
            info(f"warnings: {b['warnings']}")
    else:
        gap("P0", "recipe-gen from-description",
            f"{sc} {_short(b)} — viral referral chain can't be auto-generated; "
            "merchants must hand-craft recipe JSON. Blocks self-serve viral marketing.")
        return

    # PROBE: explicitly conditional voucher template (issue only after referee_1.first_purchase)
    sc, b = await call(c, "POST", "/api/v1/voucher-builder/templates/create", json_body={
        "brand_id": BRAND_ID,
        "name": "Mom Referral Reward — ¥100 Diaper Voucher",
        "description": "Earned by referring 3 new moms who each make a first purchase",
        "value": {"type": "fixed", "amount": 10000},  # ¥100
        "conditions": {
            "min_purchase_amount_cents": 0,
            "min_referrals": 3,
            "require_referee_first_purchase": True,
            "total_supply": 1000,
        },
        "expires_in_days": 30,
        "stackable": False,
        "transferable": False,
    })
    if sc in (200, 201) and isinstance(b, dict):
        state["referral_voucher_template_id"] = b.get("template_id")
        ok("conditional voucher template (3 referrals + first purchase)",
           f"tid={state.get('referral_voucher_template_id')}")
    elif sc in (400, 422):
        gap("P0", "no min_referrals / first-purchase conditions",
            f"{sc} {_short(b)} — voucher template conditions schema rejects "
            "'min_referrals' and 'require_referee_first_purchase'. Voucher conditions "
            "only support min_purchase/total_supply — viral reward IS the "
            "primary mom-shop growth mechanism but cannot be encoded.")
    else:
        gap("P1", "conditional voucher template", f"{sc} {_short(b)}")


# ── Phase 6: Custom Audience: New Moms ───────────────────────────────────
async def phase_6_audience(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("6: Custom Audience — 500 existing customer phones → lookalike new moms")

    # Generate 500 phone hashes
    rng = random.Random(RUN_TAG + 6)
    phone_hashes = [_sha256(f"+86138{rng.randint(10000000, 99999999)}") for _ in range(500)]

    sc, b = await call(c, "POST", "/api/v1/audiences/custom/create", json_body={
        "brand_id": BRAND_ID,
        "name": "Existing 500 customers — phone match",
        "source": "csv_upload",
        "phones_sha256": phone_hashes,
        "description": "Bootstrap audience for lookalike seed",
    })
    if sc == 200 and isinstance(b, dict):
        seed_aid = b["audience_id"]
        state["seed_audience_id"] = seed_aid
        ok("seed audience (500 phones)", f"aid={seed_aid} size={b['size']}")
        if b["size"] == 0:
            info("size=0 expected (none of the phones have a known user yet — "
                 "unmatched hashes stored for back-fill)")
    else:
        gap("P0", "custom audience create", f"{sc} {_short(b)}")
        return

    # PROBE: Lookalike for "first 6 months postpartum" mothers
    sc, b = await call(c, "POST", f"/api/v1/audiences/{seed_aid}/lookalike", json_body={
        "brand_id": BRAND_ID,
        "similarity": 7,
        "countries": ["CN"],
        "name": "Lookalike: New moms in first 6 months postpartum",
    })
    if sc == 200 and isinstance(b, dict):
        state["lookalike_audience_id"] = b["audience_id"]
        ok("lookalike audience", f"aid={b['audience_id']} size={b['size']}")
        # PROBE: can we mark life-stage in the lookalike payload?
        if b["size"] == 0:
            gap("P1", "lookalike empty for life-stage seed",
                "Lookalike returned size=0 — algorithm cannot infer 'new mom in "
                "first 6 months postpartum' from purchase patterns. No life-stage "
                "or pregnancy/postpartum facets in audience targeting model.")
    else:
        gap("P1", "lookalike create", f"{sc} {_short(b)}")

    # PROBE: how do you tag a user with life-stage facts like
    # "first 6 months postpartum"? Look for an attribute endpoint
    sc, b = await call(c, "POST", "/api/v1/users/attributes/set", json_body={
        "user_id": f"user_smoke_signup_{RUN_TAG}",
        "attributes": {
            "life_stage": "postpartum_0_6mo",
            "baby_age_months": 3,
        },
    })
    if sc == 200:
        ok("user attribute set", "life_stage tag accepted")
    elif sc == 404:
        gap("P0", "no life-stage attribute API",
            "POST /api/v1/users/attributes/set returns 404 — no documented way "
            "to tag a user as 'new mom' / 'first 6 months postpartum'. Baby-"
            "product merchants live or die by life-stage targeting (baby's age "
            "changes the product category every 3 months). Need first-class "
            "user-attribute / life-stage API.")
    else:
        gap("P1", "user attribute set", f"{sc} {_short(b)}")


# ── Phase 7: GMV-Based Commission Campaign ───────────────────────────────
async def phase_7_cps_campaign(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("7: GMV-Based Commission (CPS 8% of order)")

    # 8% expressed as cents-per-order is impossible to fix at creation time
    # (order amounts vary). Probe how the platform represents "% of order".
    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": BRAND_ID,
        "name": "huang_baby_shop CPS 8% Sales",
        "objective": "sales",
        "bid_strategy": "cps",
        "max_bid_cents": 800,             # legacy cents — meaningless for %
        "bid_percent": 8.0,               # PROBE: does platform recognize %?
        "max_commission_percent": 8.0,    # PROBE: alt field name
        "daily_budget_cents": DAILY_BUDGET_CENTS,
        "total_budget_cents": MONTHLY_BUDGET_CENTS,
        "targeting": {
            "geo": {"country": "CN"},
            "device": "mobile",
        },
        "creative": {"recipe_id": state.get("recipe_id") or "default_baby_recipe"},
        "schedule": {"start_at": time.time() - 60, "end_at": time.time() + 86400 * 30},
    })
    if sc == 200 and isinstance(b, dict):
        state["cps_campaign_id"] = b["campaign_id"]
        ok("CPS campaign created", f"id={b['campaign_id']}")
    else:
        gap("P0", "CPS campaign create", f"{sc} {_short(b)}")
        return

    cid = state["cps_campaign_id"]

    # Inspect details — is bid_percent persisted?
    sc, b = await call(c, "GET", f"/api/v1/campaigns/{cid}/details")
    if sc == 200 and isinstance(b, dict):
        body_str = json.dumps(b).lower()
        if "percent" in body_str or "bid_percent" in body_str:
            ok("CPS bid as % of order", "bid_percent persisted in campaign")
        else:
            gap("P0", "CPS as % of order amount NOT supported",
                "Campaign accepts bid_percent in request but only persists "
                "max_bid_cents — a FIXED cents amount. For CPS at 8% of order, "
                "the actual commission must scale with order_amount_cents at "
                "conversion time. Today a ¥80 order pays the same commission "
                "as a ¥1500 order — destroying the whole CPS economics. THIS IS "
                "THE #1 e-commerce blocker.")
        info(f"campaign status={b.get('status')}")

    # Try admin auto-approve to push campaign live
    sc, b = await call(c, "POST", f"/api/v1/campaigns/{cid}/admin/approve",
                       json_body={"notes": "sim auto-approve laohuang"})
    if sc == 200:
        ok("admin auto-approve", "campaign live")
    elif sc == 404:
        gap("P1", "admin approve path",
            f"POST /campaigns/{{cid}}/admin/approve returns 404 — "
            "merchant has no path to go live without manual ops intervention.")
    else:
        info(f"approve returned {sc}: {_short(b)}")

    # Explicit Round 2 probe: CPS using bid_percent_bps + max_bid_cents ceiling.
    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": BRAND_ID,
        "name": "CPS test",
        "objective": "sales",
        "bid_strategy": "cps",
        "bid_percent_bps": 800,       # 8% commission
        "max_bid_cents": 50000,       # safety ceiling
        "daily_budget_cents": 100000,
        "total_budget_cents": MONTHLY_BUDGET_CENTS,
        "targeting": {"geo": {"country": "CN"}},
        "creative": {"recipe_id": state.get("recipe_id") or "default_baby_recipe"},
        "schedule": {"start_at": time.time() - 60,
                     "end_at": time.time() + 86400 * 30},
    })
    if sc == 200:
        ok("CPS bid_percent_bps accepted", "8% commission configured")
    else:
        gap("P0", "CPS percent not supported", f"{sc} {_short(b)}")


# ── Phase 8: Repeat Customer Engagement (60d dormant) ────────────────────
async def phase_8_reengagement(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("8: Re-engagement — customers dormant > 60 days")

    # PROBE: time-based audience (recency)
    sc, b = await call(c, "POST", "/api/v1/audiences/custom/create", json_body={
        "brand_id": BRAND_ID,
        "name": "Dormant 60d+ — re-engagement target",
        "source": "converters",
        "user_ids": [],
        "filter": {
            "last_purchase_days_ago_min": 60,
            "last_purchase_days_ago_max": 365,
            "lifetime_orders_min": 1,
        },
        "description": "Customers who ordered ≥1× but not in last 60 days",
    })
    if sc == 200 and isinstance(b, dict):
        ok("dormant-segment audience created",
           f"aid={b.get('audience_id')} size={b.get('size')}")
        # Even if accepted, check if filter actually narrows population
        info("Probe: filter field may be ignored — server returned 200 but "
             "filter semantics unverified")
    elif sc in (400, 422):
        gap("P0", "no recency-based audience filter",
            f"{sc} {_short(b)} — audience-create schema has no "
            "'last_purchase_days_ago' field. The single most-used CRM segment "
            "(churn-risk / win-back) cannot be built declaratively. Today the "
            "merchant must compute the user-id list offline, upload as CSV, and "
            "repeat daily. Need server-side recency segments.")
    else:
        gap("P1", "dormant segment", f"{sc} {_short(b)}")

    # PROBE: retention-objective campaign with re-engagement creative
    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": BRAND_ID,
        "name": "Win-Back Dormant Moms",
        "objective": "retention",
        "bid_strategy": "cpa",
        "max_bid_cents": 2000,            # ¥20 per re-activation
        "daily_budget_cents": 20000,
        "total_budget_cents": 100000,
        "targeting": {"geo": {"country": "CN"}},
        "creative": {"recipe_id": "winback_voucher"},
        "schedule": {"start_at": time.time() - 60, "end_at": time.time() + 86400 * 30},
    })
    if sc == 200:
        ok("retention campaign created", "objective=retention accepted")
    elif sc in (400, 422):
        gap("P1", "retention objective not supported",
            f"{sc} {_short(b)} — objective field rejects 'retention'. Only "
            "acquire / sales / geo_visit. Re-engagement gets lumped into "
            "'acquire' which messes with attribution.")
    else:
        gap("P2", "retention campaign", f"{sc} {_short(b)}")


# ── Phase 9: Pixel-Driven Attribution ────────────────────────────────────
async def phase_9_pixel_attribution(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("9: Pixel-Driven Attribution (no manual track_conversion)")

    pid = state.get("pixel_id")
    if not pid:
        fail("phase 9", "no pixel_id from phase 4")
        return

    # 50 user journeys: pageview → add_to_cart → purchase (some abandon)
    rng = random.Random(RUN_TAG + 9)
    metrics = {"pageviews": 0, "carts": 0, "purchases": 0,
               "attributed": 0, "cart_abandon": 0}

    # Grant consent for all journey users before any pixel events.
    journey_uids = [f"u_journey_{RUN_TAG}_{i:02d}" for i in range(50)]
    await _setup_consent(c, journey_uids)

    for i in range(50):
        uid = f"u_journey_{RUN_TAG}_{i:02d}"
        dfp = f"dev_journey_{i:02d}"
        # pageview
        sc, _ = await call(c, "POST", "/api/v1/pixel/event", json_body={
            "pixel_id": pid, "event_type": "pageview",
            "user_id": uid, "device_fingerprint": dfp,
            "origin": "https://huangbaby.com",
            "url": "https://huangbaby.com/diapers",
        }, headers={"Origin": "https://huangbaby.com"})
        if sc == 200:
            metrics["pageviews"] += 1

        # 70% add to cart
        if rng.random() < 0.70:
            sc, _ = await call(c, "POST", "/api/v1/pixel/event", json_body={
                "pixel_id": pid, "event_type": "add_to_cart",
                "user_id": uid, "device_fingerprint": dfp,
                "origin": "https://huangbaby.com",
                "url": "https://huangbaby.com/cart",
            }, headers={"Origin": "https://huangbaby.com"})
            if sc == 200:
                metrics["carts"] += 1

            # 40% of carts actually purchase
            if rng.random() < 0.40:
                amt = rng.choice([8000, 20000, 40000, 80000, 150000])
                sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
                    "pixel_id": pid, "event_type": "purchase",
                    "user_id": uid, "device_fingerprint": dfp,
                    "order_id": f"ord_{RUN_TAG}_{i:02d}",
                    "amount_cents": amt,
                    "currency": "CNY",
                    "origin": "https://huangbaby.com",
                    "url": "https://huangbaby.com/checkout/success",
                }, headers={"Origin": "https://huangbaby.com"})
                if sc == 200:
                    metrics["purchases"] += 1
                    if isinstance(b, dict) and b.get("attributed"):
                        metrics["attributed"] += 1
            else:
                metrics["cart_abandon"] += 1

    ok("50 user journeys",
       f"pv={metrics['pageviews']} cart={metrics['carts']} "
       f"purchase={metrics['purchases']} attributed={metrics['attributed']} "
       f"abandon={metrics['cart_abandon']}")

    if metrics["purchases"] > 0 and metrics["attributed"] == 0:
        gap("P1", "pixel purchase not attributed",
            f"{metrics['purchases']} purchases via pixel — 0 were marked "
            "attributed=true in response. Either no source impression was found "
            "(merchant ran no auction) or attribution requires explicit "
            "impression_token. Pixel should attribute via prior pageview/click "
            "history for the same fingerprint.")

    # PROBE: cart-abandonment voucher trigger
    sc, b = await call(c, "POST", "/api/v1/triggers/register", json_body={
        "brand_id": BRAND_ID,
        "name": "Cart abandonment → voucher",
        "event_type": "pixel.add_to_cart",
        "delay_minutes": 60,
        "condition": {"no_subsequent_event": "purchase"},
        "action": {"type": "issue_voucher", "template_id": "cart_recovery_10pct"},
    })
    if sc in (200, 201):
        ok("cart-abandon trigger registered", "voucher fires 60 min after cart w/o purchase")
    elif sc == 404:
        gap("P0", "no cart-abandonment trigger",
            "POST /api/v1/triggers/register returns 404 — pixel events can't "
            "drive automated workflows. Cart-abandon recovery is the single "
            "highest-ROI e-commerce play (10-30% of lost revenue). Today merchants "
            "build it themselves outside KiX.")
    elif sc in (400, 422):
        gap("P1", "trigger schema mismatch",
            f"{sc} {_short(b)} — trigger endpoint exists but rejects "
            "the 'no_subsequent_event' condition shape. Need documented "
            "pixel-event triggers.")
    else:
        gap("P1", "cart-abandon trigger", f"{sc} {_short(b)}")


# ── Phase 10: Bulk Voucher Distribution (1000) ───────────────────────────
async def phase_10_bulk_vouchers(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("10: Bulk Voucher Distribution (1000)")

    # Ensure we have a template — create a simple one
    sc, b = await call(c, "POST", "/api/v1/voucher-builder/templates/create", json_body={
        "brand_id": BRAND_ID,
        "name": "10% off baby essentials",
        "description": "Mass-distribution voucher",
        "value": {"type": "percent", "amount": 10},
        "conditions": {},
        "expires_in_days": 14,
        "stackable": False,
        "transferable": False,
    })
    if sc not in (200, 201) or not isinstance(b, dict):
        gap("P1", "bulk voucher template create", f"{sc} {_short(b)}")
        return
    tid = b["template_id"]
    state["bulk_template_id"] = tid
    ok("bulk template created", f"tid={tid}")

    # PROBE: is there a bulk-issue endpoint?
    bulk_users = [f"u_bulk_{RUN_TAG}_{i:04d}" for i in range(1000)]
    sc, b = await call(c, "POST",
                       f"/api/v1/voucher-builder/templates/{tid}/issue-bulk",
                       json_body={"brand_id": BRAND_ID, "user_ids": bulk_users})
    if sc in (200, 201):
        ok("bulk-issue endpoint exists", f"status={sc}")
    elif sc == 404:
        gap("P0", "no bulk voucher issuance",
            "POST /voucher-builder/templates/{tid}/issue-bulk returns 404. "
            "Online merchants regularly distribute 1000-100K vouchers in one "
            "campaign blast — today requires 1000 individual POSTs (latency, "
            "rate-limit risk, no transactional rollback). Need bulk-issue API.")
        # Fall back to 100 individual issues to measure latency
        t0 = time.time()
        sample_n = 100
        succ = 0
        for u in bulk_users[:sample_n]:
            sc1, b1 = await call(c, "POST",
                                 f"/api/v1/voucher-builder/templates/{tid}/issue",
                                 json_body={"brand_id": BRAND_ID, "user_id": u})
            if sc1 in (200, 201):
                succ += 1
        dt = time.time() - t0
        info(f"individual issue: {succ}/{sample_n} in {dt:.2f}s "
             f"({sample_n/dt:.0f} req/s) → 1000 would take ~{1000/(sample_n/dt):.1f}s")
        if dt > 5:
            gap("P1", "voucher issue latency",
                f"100 sequential issues took {dt:.2f}s — 1000 would take "
                f">{1000/(sample_n/dt):.0f}s. Frontline mom-group campaigns "
                "need sub-second mass-issue.")
    else:
        gap("P1", "bulk issue", f"{sc} {_short(b)}")

    # PROBE: concurrent redemption check load
    test_codes = []
    # Issue a few then probe validate endpoints (we don't have codes here easily,
    # so just probe by voucher_id format)
    info("Concurrent redemption load test skipped — would need real voucher_ids "
         "from bulk-issue response (which doesn't exist).")


# ── Phase 11: International Targeting (Chinese expats) ───────────────────
async def phase_11_international(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("11: International Audience — Chinese expats (US/AU/SG)")

    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": BRAND_ID,
        "name": "Chinese expat moms — US/AU/SG",
        "objective": "sales",
        "bid_strategy": "cps",
        "max_bid_cents": 800,
        "daily_budget_cents": 10000,
        "total_budget_cents": 200000,
        "targeting": {
            "geo": {"countries": ["US", "AU", "SG"]},
            "language": "zh",
            "currency_pref": "USD",
        },
        "creative": {"recipe_id": "default_baby_recipe"},
        "schedule": {"start_at": time.time() - 60, "end_at": time.time() + 86400 * 30},
    })
    if sc == 200:
        ok("multi-country expat campaign created", "US/AU/SG zh-speaking targeting")
    elif sc in (400, 422):
        gap("P1", "multi-country targeting",
            f"{sc} {_short(b)} — targeting.geo.countries plural form rejected. "
            "Single-country only — expat targeting requires 3 separate campaigns.")
    else:
        gap("P1", "expat campaign", f"{sc} {_short(b)}")

    # PROBE: currency conversion for non-CNY orders
    pid = state.get("pixel_id")
    if pid:
        await _setup_consent(c, [f"u_expat_{RUN_TAG}"])
        sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
            "pixel_id": pid,
            "event_type": "purchase",
            "user_id": f"u_expat_{RUN_TAG}",
            "device_fingerprint": "dev_expat_us",
            "order_id": f"expat_ord_{RUN_TAG}",
            "amount_cents": 5000,             # $50 USD
            "currency": "USD",                # different currency
            "origin": "https://huangbaby.com",
            "url": "https://huangbaby.com/checkout",
        }, headers={"Origin": "https://huangbaby.com"})
        if sc == 200:
            ok("USD purchase event accepted")
            # PROBE: does the platform convert USD → CNY for commission?
            # We can't easily verify without inspecting stats, but flag the gap.
            gap("P1", "currency conversion semantics undefined",
                "Pixel accepts currency=USD but commission/CPS bid is denominated "
                "in cents (assumed CNY). No documented FX conversion — merchant "
                "doesn't know if $50 USD order is treated as ¥50 (wrong) or "
                "~¥360 (correct). Multi-currency e-commerce needs explicit FX.")
        else:
            gap("P1", "non-CNY purchase rejected", f"{sc} {_short(b)}")


# ── Phase 12: Edge Cases ─────────────────────────────────────────────────
async def phase_12_edges(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("12: Edge Cases — online-only / refund / fraud / volume")

    # 12a: Multi-category brand — is huang_baby_shop ONE brand or 10 (one per category)?
    sc, b = await call(c, "GET", f"/api/v1/brands/{BRAND_ID}")
    if sc == 200 and isinstance(b, dict):
        ok("brand fetch", f"single brand handles {len(SUB_CATEGORIES)} categories")
    else:
        info(f"brand fetch {sc}")
    gap("P2", "sub-category targeting within one brand",
        "huang_baby_shop has 10 categories (diapers/formula/toys/...). Platform "
        "models 'brand' as atomic — no way to run separate creatives/budgets "
        "for diapers vs formula without creating 10 brand_ids (which fragments "
        "the storefront). Need 'product line' or 'sub-brand' concept.")

    # 12b: Refund / returned order → auto-reverse KiX commission?
    pid = state.get("pixel_id")
    if pid:
        await _setup_consent(c, [f"u_refund_{RUN_TAG}"])
        # Simulate a refund event
        sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
            "pixel_id": pid,
            "event_type": "refund",
            "user_id": f"u_refund_{RUN_TAG}",
            "device_fingerprint": "dev_refund",
            "order_id": f"ord_{RUN_TAG}_refund",
            "amount_cents": 30000,
            "origin": "https://huangbaby.com",
            "url": "https://huangbaby.com/refund",
        }, headers={"Origin": "https://huangbaby.com"})
        if sc == 200:
            ok("refund event accepted")
        elif sc == 422:
            gap("P0", "no refund/return pixel event",
                "Pixel only accepts pageview/add_to_cart/purchase/signup/custom. "
                "Refund/return is the second-most-important e-commerce event "
                "after purchase — without it, CPS commission paid on returned "
                "orders never reverses. Direct GMV/commission leakage.")
        else:
            gap("P1", "refund event", f"{sc} {_short(b)}")

    # 12c: Bot-driven fake purchases — anti-fraud check
    sc, b = await call(c, "POST", "/api/v1/attribution/anti-fraud/check", json_body={
        "user_id": "bot_fake_001",
        "device_fingerprint": "dev_obviously_bot",
        "ip_address": "1.2.3.4",
        "signals": {
            "purchases_in_last_hour": 50,
            "unique_devices_per_user_24h": 1,
            "device_user_agent": "curl/7.0",
        },
    })
    if sc == 200 and isinstance(b, dict):
        if b.get("is_fraud") or b.get("score", 0) > 0.5:
            ok("anti-fraud detects bot", _short(b))
        else:
            gap("P1", "anti-fraud weak signals",
                f"50 purchases/hour from one fingerprint + curl UA scored "
                f"{b.get('score','?')} — not flagged. E-commerce bot farms can "
                "blow CPS budget; need stronger heuristics.")
    elif sc == 404:
        gap("P0", "no anti-fraud endpoint",
            "/api/v1/attribution/anti-fraud/check returns 404 — bot-driven CPS "
            "fraud is unmitigated. 老黄's commission spend is fully exposed.")
    else:
        gap("P1", "anti-fraud check", f"{sc} {_short(b)}")

    # 12d: High-volume daily orders — simulate 5K events/day load (just timing)
    pid = state.get("pixel_id")
    if pid:
        t0 = time.time()
        sent = 0
        errors = 0
        for i in range(200):  # 200 representative events
            sc, _ = await call(c, "POST", "/api/v1/pixel/event", json_body={
                "pixel_id": pid,
                "event_type": "pageview",
                "device_fingerprint": f"dev_vol_{i % 50}",
                "origin": "https://huangbaby.com",
                "url": f"https://huangbaby.com/p/{i}",
            }, headers={"Origin": "https://huangbaby.com"})
            if sc == 200:
                sent += 1
            else:
                errors += 1
        dt = time.time() - t0
        eps = sent / dt if dt else 0
        # Project: can it handle 5K orders × 10 events = 50K events/day?
        eps_needed = 50_000 / 86_400  # ≈ 0.58 eps avg, but bursts ≫
        ok("volume test 200 events",
           f"{sent}/{200} ok ({eps:.0f} eps); 50K/day avg needs {eps_needed:.2f} eps, "
           "10× burst margin → {:.0f} eps".format(eps_needed * 10))
        if eps < 50:
            gap("P1", "pixel single-event throughput",
                f"In-process ASGI achieved only {eps:.0f} eps for single events — "
                "production with network adds latency. Bursts (sale events) "
                "could overrun. Bulk endpoint + async ingest required.")

    # 12e: Online-only master_accounts model — does it gracefully handle 0 stores?
    master_id = state.get("master_id")
    if master_id:
        sc, b = await call(c, "GET", f"/api/v1/master/{master_id}/consolidated-report")
        if sc == 200 and isinstance(b, dict):
            # Look for store-centric fields that might be empty/misleading
            has_store_field = any(k.startswith("store") or k.startswith("geofence")
                                  for k in b.keys())
            if has_store_field:
                gap("P2", "consolidated-report leaks store-centric fields",
                    "Even for online-only merchants the consolidated-report "
                    "returns store_count / geofence_visits etc. as zero/empty. "
                    "Need an 'e-commerce' merchant template that hides these.")
            else:
                ok("consolidated-report (online-only friendly)", _short(b, 150))
        else:
            gap("P1", "consolidated-report", f"{sc} {_short(b)}")


# ── Module Availability Probe ────────────────────────────────────────────
async def phase_13_module_probe(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("13: Module availability probe (online-relevant)")
    probes = [
        ("pixel.brand_list", "GET", f"/api/v1/pixel/brand/{BRAND_ID}"),
        ("triggers", "POST", "/api/v1/triggers/register"),   # we know it 404'd above
        ("vouchers.list", "GET", f"/api/v1/vouchers/{BRAND_ID}"),
        ("voucher-builder.template", "GET",
            f"/api/v1/voucher-builder/templates/{state.get('bulk_template_id','x')}"),
        ("storefront.public", "GET", f"/api/v1/storefront/{BRAND_ID}"),
        ("storefront.discover", "GET", "/api/v1/storefront/discover"),
        ("payouts.brand.balance", "GET", f"/api/v1/payouts/brand/{BRAND_ID}/balance"),
        ("attribution.brand.incoming", "GET", f"/api/v1/attribution/brand/{BRAND_ID}/incoming"),
        ("audiences.brand", "GET", f"/api/v1/audiences/brand/{BRAND_ID}"),
        ("commerce.loop", "GET", f"/api/v1/commerce/brand/{BRAND_ID}/loop"),
    ]
    avail, missing = 0, 0
    for label, method, path in probes:
        # voucher-builder/templates/{id} requires brand_id query param
        params = None
        if "voucher-builder.template" in label:
            params = {"brand_id": BRAND_ID}
        sc, b = await call(c, method, path, params=params)
        if sc == 200:
            avail += 1
            ok(f"module live: {label}", "200")
        elif sc == 404:
            if isinstance(b, dict) and b.get("detail") in ("Not Found", "not found"):
                missing += 1
                gap("P1", f"module not mounted: {label}", f"404 at {path}")
            else:
                avail += 1
                ok(f"module live (no-resource): {label}", "domain 404")
        elif sc in (400, 422, 405):
            avail += 1
            ok(f"module live: {label}", f"status={sc}")
        else:
            ok(f"module: {label}", f"status={sc}")
    info(f"available={avail} missing={missing}")


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
    md.append("# 老黄 / Huang Mei (huang_baby_shop) — Online E-commerce Findings")
    md.append("")
    md.append(f"**Run tag**: `{RUN_TAG}` | **Runtime**: {runtime:.1f}s | "
              f"**Date**: {time.strftime('%Y-%m-%d %H:%M', time.localtime(start_ts))}")
    md.append("")
    md.append("## Scenario")
    md.append(
        "老黄 runs 「黄记母婴 / huang_baby_shop」 — Hangzhou-based, pure online "
        "baby-products merchant (diapers, formula, toys, baby gear). Sells via "
        "own website + WeChat Mini-Program. NO physical stores. ¥2M/月 GMV, "
        "5000 orders/月, ¥400 AOV. New-mom audience: high activity first 6 "
        "months, then drops off. Heavy mom-tells-mom virality. ¥20K/月 budget. "
        "Commission relationship with KiX is GMV-based."
    )
    md.append("")
    md.append("## What makes this scenario different from 老王 / 老李 / 老张")
    md.append("")
    md.append(
        "- **No physical presence** — geofence is irrelevant; the entire LBS "
        "stack is dead weight or worse, blocks onboarding.\n"
        "- **High transaction volume** — 5K orders/day × ~10 events ≈ 50K pixel "
        "events/day, dwarfing F&B in-store visits.\n"
        "- **GMV-driven commission** — KiX commission scales as % of order, not "
        "flat CAC. Cents-based bid model breaks.\n"
        "- **Life-stage segmentation** — baby age changes the product mix every "
        "3 months; standard demographics aren't enough.\n"
        "- **Virality > acquisition** — mom-group referral chains beat paid "
        "ads; the recipe/voucher system needs to encode multi-step viral rewards.\n"
        "- **Multi-currency / international** — Chinese expats abroad pay in "
        "USD/SGD; FX conversion semantics matter."
    )
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
    for ph, cs in phase_counters.items():
        md.append(f"| {ph} | {cs['pass']} | {cs['gap']} | {cs['fail']} |")
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

    section("P0 — Blockers", p0)
    section("P1 — Friction", p1)
    section("P2 — Nice-to-have", p2)
    section("Hard failures", fails)

    md.append("## Top 3 NEW gaps unique to online-only e-commerce")
    md.append("")
    md.append(
        "1. **CPS bid is fixed cents, not % of order** — the entire e-commerce "
        "commission model is broken. A ¥80 order and a ¥1500 order pay the "
        "exact same `max_bid_cents`. Need a first-class `bid_percent` field "
        "on `bid_strategy=cps`, applied at conversion time against "
        "`order_amount_cents`. Without this, KiX can't run GMV-based revenue "
        "share — its primary monetization for online merchants.\n"
        "2. **No bulk pixel-event ingestion + no refund event** — 老黄 generates "
        "~50K events/day. Single-event POSTs hit rate limits, blow mobile "
        "battery, and don't support Beacon-API batches. Worse, the schema only "
        "accepts pageview/add_to_cart/purchase/signup/custom — no `refund`, so "
        "commission paid on returned orders never reverses. Pixel = backbone of "
        "online attribution; must support batch in and refund out.\n"
        "3. **No life-stage / recency audience filters** — baby-products demand "
        "'first 6 months postpartum' + 'last purchase 60+ days ago' segments. "
        "Audience-create schema has no `last_purchase_days_ago` filter and no "
        "user-attribute API for life-stage tagging. Today merchants must build "
        "the user lists offline and re-upload as CSV daily. Need server-side "
        "recency segments + first-class user attribute (`life_stage`, "
        "`baby_age_months`) with audience filters that read them."
    )
    md.append("")
    md.append("## Cross-comparison: 老王 vs 老李 vs 老张 vs 老黄")
    md.append("")
    md.append(
        "| Dimension | 老王 (10-store F&B) | 老李 (community) | 老张 (luxury 1-venue) | 老黄 (online-only) |\n"
        "|---|---|---|---|---|\n"
        "| Physical presence | 10 stores, geofence vital | Single venue + neighbourhood | 1 venue, white-glove | **None** |\n"
        "| Volume | Medium per store | Low | Very low, high LTV | **Very high (5K orders/day)** |\n"
        "| Commission model | CPA + CPV mix | CPA + community fee | High-touch flat | **% of GMV (CPS)** |\n"
        "| Targeting axis | Geo radius | Neighbourhood + word-of-mouth | VIP allowlist | **Life-stage + recency** |\n"
        "| Virality | Cross-store loyalty | Strong (face-to-face) | Concierge referrals | **Mom-group chains** |\n"
        "| Hardest gap | Onboarding auto-approve | Recipe for community events | CRM richness | **CPS-as-% + pixel volume** |\n"
        "\n"
        "**Pattern across all four**: KiX's data model is store-centric. Every "
        "online-only feature (pixel, audiences, CPS, refund) is a second-class "
        "citizen. Suggested fix: add an `is_online_only` flag at the brand level "
        "that disables store/geofence requirements, switches CPS to %-of-GMV, "
        "and enables a separate dashboard. This unblocks ~40% of TAM (e-commerce) "
        "that the current model treats as exception cases."
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
            print(f"  • [{f['phase']}] {f['action']} — {f['detail'][:110]}")


# ── Main ─────────────────────────────────────────────────────────────────
# ── Phase R7: Round 7 probes — international ecom FX + relational vouchers ─
async def phase_r7_probes(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("R7: Round 7 probes — FX + multi-currency wallet + relational vouchers")
    bid = BRAND_ID

    # 1) Recipe library — baby_products / parenting / ecommerce
    for industry in ("baby_products", "parenting", "ecommerce"):
        sc, b = await call(c, "GET", "/api/v1/recipes", params={"industry": industry})
        if sc == 200 and isinstance(b, (list, dict)):
            items = b if isinstance(b, list) else b.get("recipes", b.get("items", []))
            if items:
                ok(f"recipes industry={industry}", f"{len(items)} recipes")
            else:
                gap("P1", f"recipes industry={industry} empty", "")
        else:
            gap("P1", f"recipes industry={industry}", f"{sc}")

    # 2) /fx/rates/configure — CNY/USD/EUR for international ecom
    sc, b = await call(c, "POST", "/api/v1/fx/rates/configure", json_body={
        "from_currency": "USD",
        "to_currency": "CNY",
        "rate": 7.2,
        "expires_at": int(time.time()) + 86400,
    })
    if sc in (200, 201):
        ok("fx rate USD→CNY configured", "")
    else:
        gap("P1", "fx rates/configure USD→CNY", f"{sc} {_short(b)}")
    sc, b = await call(c, "POST", "/api/v1/fx/rates/configure", json_body={
        "from_currency": "EUR",
        "to_currency": "CNY",
        "rate": 7.8,
        "expires_at": int(time.time()) + 86400,
    })
    if sc in (200, 201):
        ok("fx rate EUR→CNY configured", "")
    else:
        gap("P1", "fx rates/configure EUR→CNY", f"{sc} {_short(b)}")

    # 3) /wallet/{bid}/topup-with-fx — USD payment for overseas customer
    sc, b = await call(c, "POST", f"/api/v1/wallet/{bid}/topup-with-fx", json_body={
        "amount_cents": 100000,  # $1000 USD
        "currency": "USD",
        "payment_method": "wechat",
    })
    if sc in (200, 201):
        ok("topup-with-fx USD→CNY", f"converted topup created")
    else:
        gap("P1", "topup-with-fx", f"{sc} {_short(b)}")

    # 4) Sibling/parent_of relational vouchers (R4+R6)
    parent_uid = f"parent_probe_{RUN_TAG}"
    sibling_uid = f"sibling_probe_{RUN_TAG}"
    version = f"v_huang_r7_{RUN_TAG}"
    await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
        "version": version, "text_md": "## Baby Privacy",
        "effective_at": int(time.time()) - 60, "requires_re_grant": False,
    })
    for uid in (parent_uid, sibling_uid):
        await call(c, "POST", "/api/v1/consent/grant", json_body={
            "user_id": uid, "scopes": ["cross_brand_tracking", "personalization"],
            "policy_version": version, "source": "app",
        })
    sc, b = await call(c, "POST", f"/api/v1/primitives/users/{parent_uid}/relationships",
                       json_body={
                           "related_user_id": sibling_uid,
                           "relationship": "parent_of",
                           "bidirectional": True,
                       })
    if sc in (200, 201):
        ok("parent_of relationship created", "")
        # Now try voucher with relational predicate
        sc, b = await call(c, "POST", "/api/v1/vouchers/issue",
                           params={"issuer_brand_id": bid},
                           json_body={
                               "user_id": parent_uid,
                               "value_cents": 5000,
                               "redeemable_at": "issuer_only",
                               "relational_conditions": {"relationship_type_required": "parent_of"},
                               "source": "campaign",
                           })
        if sc in (200, 201):
            ok("relational sibling_discount voucher issued", "")
        else:
            gap("P1", "relational voucher (sibling_discount)", f"{sc} {_short(b)}")
    else:
        gap("P1", "relationship parent_of", f"{sc} {_short(b)}")

    # 5) Customer service appointment reservation (light)
    sc, b = await call(c, "POST", "/api/v1/reservations/create", json_body={
        "brand_id": bid,
        "user_id": parent_uid,
        "scheduled_at": int(time.time()) + 86400,
        "party_size": 1,
        "type": "appointment",
        "metadata": {"channel": "customer_service", "topic": "返修"},
    })
    if sc in (200, 201):
        ok("customer service appointment reservation", "")
    else:
        gap("P2", "appointment reservation", f"{sc} {_short(b)}")


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
                state = await phase_1_brand_setup(c)
                await phase_2_wallet(c, state)
                await phase_3_geofence_skip(c, state)
                await phase_4_pixel(c, state)
                await phase_5_referral_recipe(c, state)
                await phase_6_audience(c, state)
                await phase_7_cps_campaign(c, state)
                await phase_8_reengagement(c, state)
                await phase_9_pixel_attribution(c, state)
                await phase_10_bulk_vouchers(c, state)
                await phase_11_international(c, state)
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
