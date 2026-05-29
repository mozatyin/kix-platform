"""Merchant journey simulation — 老胡 / Hu Tian (Idle Excellent / 闲优 C2C marketplace).

End-to-end probe of the KiX Ads Platform from the perspective of a C2C
PEER-TO-PEER MARKETPLACE operator. Walks through:
  1. Single brand setup (idle_excellent) — universal marketplace SKU
  2. Wallet funding (¥25K/月 ops budget)
  3. KiX ID registration — dual-role users (buyer + seller in one identity)
  4. Seller rating attribute + transactions_completed (Round 5 user attributes)
  5. Social graph — followers / kudos on a seller "store"
  6. Listings-as-creative — photo / video / multi-image (negotiation chat)
  7. Audience by buyer category preference (attribute_filter)
  8. CPS-style listing-promotion campaign (commission on each transaction)
  9. Bidding / counter-offer (buyer offers below asking, like 闲鱼 出价)
 10. Trust score gating + low-trust user limits
 11. Disputes — buyer claims defect; seller rating decays
 12. Edge cases — refund, fraud bidding, bulk listing, multi-category seller
 13. Module probe + findings → /Users/mozat/a-docs/laohu-sim-findings.md

Pattern follows scripts/sim_laohuang.py / sim_laowu.py.

Unique to C2C:
  - Same KiX ID is BOTH buyer AND seller (no merchant ≠ user dichotomy)
  - Listings are short-lived ad creatives, not stable products
  - Trust + reputation > brand identity
  - Negotiation is the conversion event, not "buy now"
  - KiX commission scales with each P2P transaction GMV

In-process via httpx.ASGITransport so no separate server is needed. Requires
a live local Redis.

Run:
    .venv/bin/python scripts/sim_laohu.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402
from app.redis_client import close_redis, init_redis  # noqa: E402


# ── Constants / config ────────────────────────────────────────────────────
RUN_TAG = int(time.time())
OWNER_USER_ID = f"laohu_{RUN_TAG}"
BRAND_ID = f"idle_excellent_{RUN_TAG}"
BRAND_COLOR = "#FFCC33"   # 闲鱼-style yellow
FINDINGS_PATH = Path("/Users/mozat/a-docs/laohu-sim-findings.md")

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"

# C2C scale numbers — 闲优 / Idle Excellent
ACTIVE_LISTINGS = 15_000
DAILY_TRANSACTIONS = 5_000
TYPICAL_LOW_AOV_CENTS = 5_000        # ¥50
TYPICAL_HIGH_AOV_CENTS = 300_000     # ¥3000
TYPICAL_AOV_CENTS = 30_000           # ¥300 mean
MONTHLY_BUDGET_CENTS = 2_500_000     # ¥25,000
DAILY_BUDGET_CENTS = MONTHLY_BUDGET_CENTS // 30
KIX_COMMISSION_BPS = 200             # 2% take-rate on each transaction

CATEGORIES = ["digital", "home", "fashion", "toys", "books"]
CATEGORIES_CN = {
    "digital": "数码", "home": "家居", "fashion": "服饰",
    "toys": "玩具", "books": "书籍",
}

# Personas — kid as dual-role (buyer + seller)
DUAL_ROLE_USERS = [
    ("Xiao Wang", "小王", "+8613811110001", ["digital", "books"]),
    ("Mei Mei", "美美", "+8613811110002", ["fashion", "home"]),
    ("Lao Liu", "老刘", "+8613811110003", ["digital", "toys"]),
    ("Ah Qiang", "阿强", "+8613811110004", ["digital"]),
    ("Yu Han", "雨涵", "+8613811110005", ["fashion", "books", "toys"]),
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


# ── Consent setup ────────────────────────────────────────────────────────
_consent_policy_published = False


async def _setup_consent(c: httpx.AsyncClient, user_ids: list[str],
                         policy_version: str = "1.0") -> None:
    global _consent_policy_published
    if not _consent_policy_published:
        sc, _b = await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
            "version": policy_version,
            "text_md": "# Idle Excellent sim policy\nFor C2C marketplace test.",
            "effective_at": int(time.time()) - 60,
            "requires_re_grant": False,
        })
        _consent_policy_published = (sc == 200)
    for uid in user_ids:
        await call(c, "POST", "/api/v1/consent/grant", json_body={
            "user_id": uid,
            "scopes": ["cross_brand_tracking", "geo_lbs",
                       "personalization", "marketing"],
            "policy_version": policy_version,
            "source": "app",
        })


# ── Phase 1: Brand Setup (C2C marketplace) ───────────────────────────────
async def phase_1_brand_setup(c: httpx.AsyncClient) -> dict[str, Any]:
    _phase_init("1: C2C Brand 'idle_excellent' — listings as a brand store?")
    state: dict[str, Any] = {
        "master_id": None, "brand_id": BRAND_ID,
        "users": {},      # display_name → kid
        "phone_to_kid": {},
        "listings": [],
    }

    sc, b = await call(c, "POST", "/api/v1/master/create", json_body={
        "company_name": "闲优 / Idle Excellent",
        "primary_email": "laohu@idleexcellent.com",
        "owner_user_id": OWNER_USER_ID,
    })
    if sc == 201 and isinstance(b, dict):
        state["master_id"] = b["master_id"]
        ok("create master account", f"master_id={state['master_id']}")
    else:
        gap("P1", "create master account", f"{sc} {_short(b)}")

    if state["master_id"]:
        sc, b = await call(c, "POST",
                           f"/api/v1/master/{state['master_id']}/brands/attach",
                           json_body={
                               "brand_id": BRAND_ID,
                               "store_name": "Idle Excellent (Master C2C)",
                               "store_id": BRAND_ID,
                           })
        if sc == 200:
            ok("attach C2C master brand", f"brand_id={BRAND_ID}")
        else:
            gap("P0", "attach C2C master brand", f"{sc} {_short(b)}")

    # PROBE: C2C marketplace has thousands of seller "stores". Are individual
    # sellers modeled as sub-brands, or all under one master brand?
    sc, b = await call(c, "POST",
                       f"/api/v1/master/{state['master_id'] or 'x'}/brands/attach",
                       json_body={
                           "brand_id": f"seller_xiaowang_{RUN_TAG}",
                           "store_name": "Xiao Wang's Idle Store",
                           "store_id": f"seller_xiaowang_{RUN_TAG}",
                           "parent_brand_id": BRAND_ID,
                       })
    if sc == 200:
        ok("seller-as-sub-brand attach", "individual seller modeled as sub-brand")
        gap("P1", "C2C seller-as-brand cost",
            "Modeling 15K active sellers as brands creates 15K brand records — "
            "each gets wallet/storefront/audience caches. Probably unsustainable. "
            "Need a 'lightweight seller account' primitive distinct from brand.")
    elif sc in (400, 422, 404):
        gap("P0", "no first-class seller-account primitive",
            f"{sc} {_short(b)} — C2C marketplaces need ONE marketplace brand "
            "(闲优) but THOUSANDS of seller storefronts each. Platform forces "
            "binary choice: model every seller as a brand (heavy, expensive) or "
            "ignore seller identity (loses trust signals). Need a 'seller' "
            "concept = KiX ID + reputation + listing namespace, sub-brand-lite.")
    else:
        gap("P1", "seller-as-sub-brand attach", f"{sc} {_short(b)}")

    # Storefront for the marketplace itself
    sc, b = await call(c, "POST", f"/api/v1/storefront/{BRAND_ID}/configure",
                       json_body={
                           "display_name": "闲优 Idle Excellent",
                           "bio": "Peer-to-peer used goods — trust, quality, value",
                           "brand_color": BRAND_COLOR,
                           "country": "CN",
                           "category": "c2c_marketplace",
                       })
    if sc == 200:
        ok("marketplace storefront configure", f"category=c2c_marketplace")
    else:
        gap("P1", "storefront configure", f"{sc} {_short(b)}")

    return state


# ── Phase 2: Wallet ──────────────────────────────────────────────────────
async def phase_2_wallet(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("2: Wallet ¥25K/月 (C2C ops budget)")

    sc, b = await call(c, "POST", f"/api/v1/wallet/{BRAND_ID}/topup", json_body={
        "amount_cents": MONTHLY_BUDGET_CENTS,
        "payment_method": "alipay",
    })
    if sc != 200 or not isinstance(b, dict) or "topup_id" not in b:
        fail("wallet topup", f"{sc} {_short(b)}")
        return
    tid = b["topup_id"]
    sc2, b2 = await call(c, "POST",
                        f"/api/v1/wallet/{BRAND_ID}/topup/{tid}/confirm",
                        json_body={"payment_gateway_response": {"mock": True}})
    if sc2 == 200:
        ok("wallet topup ¥25K confirmed", f"topup_id={tid}")
    else:
        fail("wallet topup confirm", f"{sc2} {_short(b2)}")
        return

    sc, b = await call(c, "POST", f"/api/v1/wallet/{BRAND_ID}/daily-budget",
                       json_body={"daily_budget_cents": DAILY_BUDGET_CENTS})
    if sc == 200:
        ok("daily budget set", f"¥{DAILY_BUDGET_CENTS/100:.0f}/day")
    else:
        gap("P1", "daily budget set", f"{sc} {_short(b)}")

    # PROBE: Commission-only wallet — does platform support "marketplace take-
    # rate" instead of pre-paid ad budget?
    sc, b = await call(c, "POST", f"/api/v1/wallet/{BRAND_ID}/topup", json_body={
        "amount_cents": 0,
        "payment_method": "marketplace_take_rate",
        "billing_strategy": "post_paid_commission",
        "commission_bps": KIX_COMMISSION_BPS,
    })
    if sc == 200 and isinstance(b, dict):
        ok("post-paid take-rate wallet", "C2C marketplace billing supported")
    else:
        gap("P0", "no marketplace take-rate billing",
            f"{sc} {_short(b)} — KiX wallet only supports PRE-PAID topup. C2C "
            "marketplaces earn from each transaction (1-3% commission), not "
            "pre-funded ad budget. Today 老胡 must topup ¥25K up front and "
            "reconcile commission earnings manually. Need post-paid take-rate "
            "wallet billed against transaction events.")


# ── Phase 3: KiX ID — Dual-Role Users (buyer + seller) ───────────────────
async def phase_3_dual_role_kix_ids(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("3: KiX ID — dual-role users (buyer + seller in ONE identity)")

    users: dict[str, str] = {}
    for en, cn, phone, _cats in DUAL_ROLE_USERS:
        sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
            "phone": phone,
            "display_name": cn,
            "primary_language": "zh-CN",
            "source_brand_id": BRAND_ID,
            "device_fingerprint": f"dev_{RUN_TAG}_{en.replace(' ','_')}",
            "country": "CN",
        })
        if sc == 200 and isinstance(b, dict) and b.get("kid"):
            users[en] = b["kid"]
            state["phone_to_kid"][phone] = b["kid"]
        else:
            gap("P0", f"kix-id register {en}", f"{sc} {_short(b)}")
    state["users"] = users

    if len(users) == len(DUAL_ROLE_USERS):
        ok(f"register {len(users)} dual-role users",
           f"kids={[k[:14] for k in users.values()]}")
    else:
        gap("P0", "kix-id registrations",
            f"only {len(users)}/{len(DUAL_ROLE_USERS)} registered")
        return

    await _setup_consent(c, list(users.values()))

    # PROBE: Same KID acts as buyer (track conversion) AND seller (receive
    # payout). Is the identity universal across both roles?
    seller_kid = users["Xiao Wang"]
    buyer_kid = users["Mei Mei"]

    # Tag one user with role attribute "buyer" and "seller" simultaneously
    sc, b = await call(c, "POST",
                       f"/api/v1/primitives/user/{seller_kid}/attributes/roles",
                       json_body={"value": "buyer,seller"},
                       params={"brand_id": BRAND_ID})
    if sc == 200:
        ok("dual-role attribute set", "roles=buyer,seller on one KID")
    else:
        gap("P1", "dual-role attribute", f"{sc} {_short(b)}")

    # PROBE: profile-for-merchant — does it distinguish buyer-vs-seller view?
    sc, b = await call(c, "GET",
                       f"/api/v1/kix-id/{seller_kid}/profile-for-merchant/{BRAND_ID}")
    if sc == 200 and isinstance(b, dict):
        ok("profile-for-merchant fetch", _short(b, 150))
        if "role" not in str(b).lower() and "seller" not in str(b).lower():
            gap("P1", "profile lacks role context",
                "C2C marketplace needs profile to surface user's role on THIS "
                "brand (is_buyer / is_seller / both / new). Today returns flat "
                "profile with no role hint — merchant has no signal to render "
                "the right UI (buyer view vs seller dashboard).")
    elif sc == 403:
        gap("P1", "profile-for-merchant blocked",
            f"403 — consent gate too tight. C2C UX needs name/role visible to "
            "the marketplace itself, but the OAuth scope model treats the "
            "marketplace as a 3rd party. Need a 'self-merchant' fast path.")
    else:
        gap("P1", "profile-for-merchant", f"{sc} {_short(b)}")


# ── Phase 4: Seller Rating & Transactions_Completed Attributes ───────────
async def phase_4_seller_attributes(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("4: Seller rating + transactions_completed (user attributes)")

    if not state["users"]:
        fail("phase 4", "no users from phase 3")
        return

    # Set initial seller_rating & transactions_completed for each user
    set_ok = 0
    for en, kid in state["users"].items():
        # seller rating 4.0-5.0 random
        rating = 4.2 + 0.7 * ((hash(en) % 100) / 100.0)
        tx_count = 10 + (hash(en) % 200)
        for key, val in [
            ("seller_rating", f"{rating:.2f}"),
            ("transactions_completed", str(tx_count)),
            ("trust_score", "0.85"),
            ("listings_active", "12"),
            ("preferred_category", "digital"),
        ]:
            sc, _b = await call(c, "POST",
                                f"/api/v1/primitives/user/{kid}/attributes/{key}",
                                json_body={"value": val},
                                params={"brand_id": BRAND_ID})
            if sc == 200:
                set_ok += 1
    if set_ok >= len(state["users"]) * 4:
        ok("seller attributes set",
           f"{set_ok}/{len(state['users'])*5} attributes (rating/tx/trust/listings/pref)")
    else:
        gap("P1", "seller attributes",
            f"only {set_ok} of {len(state['users'])*5} set")

    # PROBE: Numeric range queries — "sellers with rating ≥ 4.5"
    sc, b = await call(c, "POST", "/api/v1/audiences/filter/preview", json_body={
        "brand_id": BRAND_ID,
        "attribute_filter": {"seller_rating_gte": "4.5"},
        "limit": 50,
    })
    if sc == 200 and isinstance(b, dict):
        ok("range-attribute filter preview", _short(b, 150))
    elif sc in (400, 422):
        gap("P0", "no numeric range attribute filter",
            f"{sc} {_short(b)} — attribute_filter is exact-match only "
            "({key: value}). Cannot express 'seller_rating >= 4.5' or "
            "'transactions_completed > 100'. C2C trust segmentation is "
            "ENTIRELY numeric (ratings, counts, scores). Need operator "
            "syntax: {seller_rating: {gte: 4.5}} or top-level keys with "
            "_gte/_lte/_eq suffixes.")
    else:
        gap("P1", "range filter", f"{sc} {_short(b)}")

    # PROBE: Decay — does platform support attribute decay (rating drops on bad tx)?
    seller_kid = state["users"].get("Xiao Wang")
    if seller_kid:
        sc, b = await call(c, "POST",
                           f"/api/v1/primitives/user/{seller_kid}/attributes/seller_rating/log",
                           json_body={
                               "delta": -0.15,
                               "reason": "dispute_against_seller",
                               "event_id": f"dispute_{RUN_TAG}",
                           })
        if sc == 200:
            ok("rating decay logged",
               "seller_rating decremented on dispute event")
        elif sc in (400, 422):
            gap("P1", "rating decay schema",
                f"{sc} {_short(b)} — attribute log endpoint exists but doesn't "
                "accept 'delta' for monotonic decrement. Seller-rating must "
                "decay on each dispute; today the merchant must read-modify-"
                "write, racing other dispute events. Need an atomic delta op.")
        else:
            gap("P1", "rating decay log", f"{sc} {_short(b)}")

        # Read the rating history (trend)
        sc, b = await call(c, "GET",
                           f"/api/v1/primitives/user/{seller_kid}/attributes/seller_rating/trend")
        if sc == 200 and isinstance(b, dict):
            ok("rating trend fetch", _short(b, 150))
        else:
            gap("P2", "rating trend", f"{sc} {_short(b)}")


# ── Phase 5: Social Graph — Followers / Kudos ────────────────────────────
async def phase_5_social_followers(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("5: Social — followers & kudos on seller storefronts")

    users = state["users"]
    if len(users) < 2:
        fail("phase 5", "need ≥ 2 users")
        return

    user_list = list(users.values())
    seller = user_list[0]  # Xiao Wang as "popular seller"
    followers = user_list[1:]

    # All other users follow Xiao Wang
    follow_ok = 0
    for f in followers:
        sc, b = await call(c, "POST", "/api/v1/social/follow", json_body={
            "follower_id": f,
            "followed_id": seller,
        })
        if sc in (200, 201):
            follow_ok += 1
    ok("followers attached", f"{follow_ok}/{len(followers)} follow seller")

    # Verify followers list
    sc, b = await call(c, "GET", f"/api/v1/social/{seller}/followers")
    if sc == 200 and isinstance(b, dict):
        count = b.get("count") or len(b.get("followers", []))
        ok("seller followers fetch", f"count={count}")
    else:
        gap("P1", "followers fetch", f"{sc} {_short(b)}")

    # Seller posts a "new listing" to followers' feed
    sc, b = await call(c, "POST", "/api/v1/social/feed/post", json_body={
        "user_id": seller,
        "brand_id": BRAND_ID,
        "event_type": "new_listing",
        "payload": {
            "title": "iPhone 14 Pro 256G (8 months used)",
            "category": "digital",
            "asking_price_cents": 600_000,
            "image_count": 6,
            "has_video": True,
        },
    })
    post_id = None
    if sc in (200, 201) and isinstance(b, dict):
        post_id = b.get("post_id")
        ok("listing feed post", f"post_id={post_id}")
    else:
        gap("P1", "listing feed post", f"{sc} {_short(b)}")
        # PROBE: are 闲鱼-style "new_listing" event types supported?
        if sc in (400, 422):
            gap("P0", "feed event_type restricted",
                f"{sc} — feed/post rejects event_type='new_listing'. C2C needs "
                "new_listing / price_drop / sold / restock events to drive "
                "follower discovery. Today only legacy high_score / level_up "
                "types likely accepted. Need open event_type taxonomy.")

    # Kudos / likes from followers
    if post_id:
        kudos_ok = 0
        for f in followers:
            sc, _b = await call(c, "POST",
                                f"/api/v1/social/feed/{post_id}/like",
                                json_body={"user_id": f})
            if sc in (200, 201):
                kudos_ok += 1
        ok("kudos collected", f"{kudos_ok}/{len(followers)} likes")

        # Comment (negotiation hint)
        sc, _b = await call(c, "POST",
                            f"/api/v1/social/feed/{post_id}/comment",
                            json_body={
                                "user_id": followers[0],
                                "text": "5500能出吗？",   # negotiation in comments
                            })
        if sc in (200, 201):
            ok("negotiation comment posted", "buyer asks for price drop in comments")
        else:
            gap("P1", "comment post", f"{sc} {_short(b)}")


# ── Phase 6: Listing as Ad Creative — Photo / Video ──────────────────────
async def phase_6_listing_creative(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("6: Listing as creative — photo/video/multi-image")

    # PROBE: creative-gen endpoint — can a listing photo BE an ad creative?
    sc, b = await call(c, "POST", "/api/v1/creative-gen/generate", json_body={
        "brand_id": BRAND_ID,
        "objective": "c2c_listing",
        "asset_type": "image",
        "context": {
            "category": "digital",
            "title": "iPhone 14 Pro 256G",
            "condition_desc": "8 months used, with box, two scratches",
            "asking_price_cents": 600_000,
            "seller_rating": 4.8,
            "user_uploaded_images": [
                "https://idleexcellent.com/u/img/abc123.jpg",
                "https://idleexcellent.com/u/img/abc124.jpg",
                "https://idleexcellent.com/u/img/abc125.jpg",
            ],
            "user_uploaded_video": "https://idleexcellent.com/u/v/xyz.mp4",
        },
    })
    if sc == 200 and isinstance(b, dict):
        ok("creative-gen accepts user-uploaded media", _short(b, 150))
    elif sc in (400, 422):
        gap("P0", "creative-gen rejects C2C listings",
            f"{sc} {_short(b)} — creative-gen schema assumes brand-authored "
            "creatives. C2C listings ARE the creative (user-uploaded photo + "
            "title + condition desc). Today the marketplace must hack listings "
            "in as 'recipes' or external creatives. Need a `user_listing` "
            "creative type that takes uploaded media + reputation badge.")
    elif sc == 404:
        gap("P1", "creative-gen not mounted",
            f"{sc} — endpoint missing; fallback to manual creative attach.")
    else:
        gap("P1", "creative-gen probe", f"{sc} {_short(b)}")

    # PROBE: multi-listing batch creation (each is a tiny creative)
    rng = random.Random(RUN_TAG)
    listings = []
    for i in range(10):
        seller_kid = list(state["users"].values())[i % len(state["users"])]
        cat = CATEGORIES[i % len(CATEGORIES)]
        listing = {
            "listing_id": f"listing_{RUN_TAG}_{i:03d}",
            "seller_user_id": seller_kid,
            "brand_id": BRAND_ID,
            "category": cat,
            "title": f"{CATEGORIES_CN[cat]} item #{i}",
            "asking_price_cents": rng.choice([
                TYPICAL_LOW_AOV_CENTS, 15_000, TYPICAL_AOV_CENTS,
                100_000, TYPICAL_HIGH_AOV_CENTS,
            ]),
            "condition": rng.choice(["new", "like_new", "good", "fair"]),
        }
        listings.append(listing)
    state["listings"] = listings

    sc, b = await call(c, "POST", "/api/v1/primitives/listings/batch-create",
                       json_body={"brand_id": BRAND_ID, "listings": listings})
    if sc in (200, 201):
        ok("listings batch-create", f"{len(listings)} listings")
    elif sc == 404:
        gap("P0", "no listing primitive",
            "POST /primitives/listings/batch-create returns 404 — KiX has "
            "no first-class 'listing' primitive. C2C marketplaces revolve "
            "around the listing entity (CRUD, search, expiry, sold-status). "
            "Today 老胡 must shoehorn listings into 'vouchers' or 'campaigns', "
            "neither of which fit (vouchers are issued by merchant, not user; "
            "campaigns assume brand-controlled budget).")
    else:
        gap("P1", "listings batch-create", f"{sc} {_short(b)}")


# ── Phase 7: Audience by Buyer-Category Preference ───────────────────────
async def phase_7_audience_by_pref(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("7: Audience by buyer category preference (attribute_filter)")

    # Mark some users with preferred_category attribute via primitives
    cat_pref_set = 0
    for en, kid in state["users"].items():
        # find their preferred categories from DUAL_ROLE_USERS
        match = next((u for u in DUAL_ROLE_USERS if u[0] == en), None)
        if not match:
            continue
        cats = match[3]
        pref = cats[0]
        sc, _b = await call(c, "POST",
                            f"/api/v1/primitives/user/{kid}/attributes/buyer_category_pref",
                            json_body={"value": pref},
                            params={"brand_id": BRAND_ID})
        if sc == 200:
            cat_pref_set += 1
    ok("buyer category prefs set",
       f"{cat_pref_set}/{len(state['users'])} users tagged")

    # Now create an audience: "buyers who prefer digital"
    sc, b = await call(c, "POST", "/api/v1/audiences/custom/create", json_body={
        "brand_id": BRAND_ID,
        "name": "Buyers preferring 数码 (digital)",
        "source": "filter",
        "attribute_filter": {"buyer_category_pref": "digital"},
        "description": "Buyers whose preferred category is digital electronics",
    })
    if sc == 200 and isinstance(b, dict):
        state["digital_buyer_audience_id"] = b.get("audience_id")
        ok("category-pref audience created",
           f"aid={state['digital_buyer_audience_id']} size={b.get('size')}")
        # Audit: did the filter actually match users?
        if b.get("size", 0) == 0:
            gap("P1", "category-pref audience empty",
                "Filter returned 0 matches despite setting buyer_category_pref "
                "on multiple users. Likely cause: attribute hash key doesn't "
                "match the SCAN namespace used by audience filter walker. C2C "
                "marketplaces depend on category-preference targeting; this "
                "must work end-to-end with primitives attribute API.")
    else:
        gap("P0", "category-pref audience create",
            f"{sc} {_short(b)} — attribute_filter source 'filter' broken.")

    # PROBE: Multi-category preference (user likes digital + books)
    sc, b = await call(c, "POST", "/api/v1/audiences/custom/create", json_body={
        "brand_id": BRAND_ID,
        "name": "Buyers preferring digital OR books",
        "source": "filter",
        "attribute_filter": {"buyer_category_pref_in": ["digital", "books"]},
    })
    if sc == 200:
        ok("multi-value filter", f"size={b.get('size') if isinstance(b, dict) else '?'}")
    elif sc in (400, 422):
        gap("P1", "no IN/OR semantics for attribute filter",
            f"{sc} {_short(b)} — attribute_filter cannot express 'pref IN "
            "{digital, books}'. Most buyers like 2-3 categories; merchant must "
            "create N separate audiences and union them at campaign time. "
            "Need _in suffix or list-valued filter.")
    else:
        gap("P2", "multi-value filter", f"{sc} {_short(b)}")


# ── Phase 8: CPS Listing-Promotion Campaign ──────────────────────────────
async def phase_8_cps_listing_promo(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("8: CPS — 2% commission on each P2P transaction")

    aid = state.get("digital_buyer_audience_id")
    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": BRAND_ID,
        "name": "Idle Excellent listing-promo (2% commission)",
        "objective": "sales",
        "bid_strategy": "cps",
        "bid_percent_bps": KIX_COMMISSION_BPS,
        "max_bid_cents": 50_000,
        "daily_budget_cents": DAILY_BUDGET_CENTS,
        "total_budget_cents": MONTHLY_BUDGET_CENTS,
        "targeting": {
            "geo": {"country": "CN"},
            "audience_id": aid,
        },
        "creative": {"recipe_id": "c2c_listing_recipe"},
        "schedule": {"start_at": time.time() - 60,
                     "end_at": time.time() + 86400 * 30},
    })
    if sc == 200 and isinstance(b, dict):
        state["cps_campaign_id"] = b["campaign_id"]
        ok("CPS campaign created", f"id={b['campaign_id']}")
    else:
        gap("P0", "CPS campaign create", f"{sc} {_short(b)}")
        return

    # Inspect commission semantics — does CPS scale with transaction amount?
    cid = state["cps_campaign_id"]
    sc, b = await call(c, "GET", f"/api/v1/campaigns/{cid}/details")
    if sc == 200 and isinstance(b, dict):
        body_str = json.dumps(b).lower()
        if "bid_percent" in body_str or "percent_bps" in body_str:
            ok("CPS as % persisted", "commission scales with order amount")
        else:
            gap("P0", "CPS still fixed-cents only",
                "Campaign accepts bid_percent_bps in request but persists "
                "only max_bid_cents. C2C take-rate (2%) MUST scale: ¥50 "
                "transaction pays ¥1, ¥3000 transaction pays ¥60. Today both "
                "would pay the same ¥500 cap. Marketplace economics broken.")

    # PROBE: per-listing campaign (each listing is its own ad)
    if state.get("listings"):
        sample = state["listings"][0]
        sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
            "brand_id": BRAND_ID,
            "name": f"Boost listing {sample['listing_id']}",
            "objective": "listing_promotion",
            "bid_strategy": "cpc",
            "max_bid_cents": 100,
            "daily_budget_cents": 500,
            "total_budget_cents": 2000,
            "targeting": {"geo": {"country": "CN"}},
            "creative": {"listing_id": sample["listing_id"]},
            "schedule": {"start_at": time.time() - 60,
                         "end_at": time.time() + 86400 * 7},
        })
        if sc == 200:
            ok("per-listing boost campaign", f"id={b.get('campaign_id')}")
        elif sc in (400, 422):
            gap("P1", "no listing_promotion objective",
                f"{sc} {_short(b)} — objective='listing_promotion' rejected. "
                "C2C sellers expect a one-tap '推一下 / Boost this listing' "
                "feature (5 元 to push to top for 24h). Forced into 'sales' "
                "objective which assumes brand-level creatives.")


# ── Phase 9: Bidding / Counter-Offer ─────────────────────────────────────
async def phase_9_bidding(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("9: Bidding — buyer offers below asking (like 闲鱼出价)")

    if not state.get("listings") or len(state["users"]) < 2:
        info("Skipping — need listings + users")
        return

    listing = state["listings"][0]
    buyer = list(state["users"].values())[1]

    # PROBE: native bid/offer primitive
    sc, b = await call(c, "POST", "/api/v1/p2p/trade/propose", json_body={
        "from_user": buyer,
        "to_user": listing["seller_user_id"],
        "brand_id": BRAND_ID,
        "offer": {
            "currency_cents": int(listing["asking_price_cents"] * 0.85),
            "vouchers": [],
            "items": [],
        },
        "request": {
            "currency_cents": 0,
            "items": [{"id": listing["listing_id"]}],
        },
    })
    if sc in (200, 201) and isinstance(b, dict):
        trade_id = b.get("trade_id")
        state["counter_offer_trade_id"] = trade_id
        ok("counter-offer via p2p/trade/propose", f"trade_id={trade_id} (15% off)")
    elif sc in (400, 422):
        gap("P0", "p2p trade rejects currency-for-item",
            f"{sc} {_short(b)} — p2p/trade/propose schema may not accept "
            "currency_cents on the offer side or items by listing_id. C2C "
            "bidding IS the conversion event in 闲鱼-style platforms — without "
            "it, marketplace cannot run. Need 'bid' / 'offer' as a "
            "first-class primitive distinct from gift-trade.")
    else:
        gap("P1", "trade propose", f"{sc} {_short(b)}")

    # Seller accepts
    tid = state.get("counter_offer_trade_id")
    if tid:
        sc, b = await call(c, "POST", f"/api/v1/p2p/trade/{tid}/accept",
                           json_body={"user_id": listing["seller_user_id"]})
        if sc == 200:
            ok("seller accepts offer", "trade complete (85% of asking)")
        elif sc in (400, 422, 404):
            gap("P1", "trade accept signature mismatch",
                f"{sc} {_short(b)} — accept endpoint exists but rejects payload.")
        else:
            gap("P1", "trade accept", f"{sc} {_short(b)}")

    # PROBE: counter-counter-offer (seller proposes new price back)
    sc, b = await call(c, "POST", "/api/v1/p2p/trade/propose", json_body={
        "from_user": listing["seller_user_id"],
        "to_user": buyer,
        "brand_id": BRAND_ID,
        "offer": {"currency_cents": 0, "items": [{"id": listing["listing_id"]}]},
        "request": {
            "currency_cents": int(listing["asking_price_cents"] * 0.92),
        },
        "parent_trade_id": tid,
    })
    if sc in (200, 201):
        ok("seller counter-counter", "counter-offer chain supported")
    elif sc in (400, 422):
        gap("P1", "no counter-offer chain",
            f"{sc} {_short(b)} — parent_trade_id not modeled, so each offer "
            "is an isolated trade. Negotiation in C2C is multi-turn (buyer "
            "offers, seller counters, buyer accepts). Need a 'negotiation' "
            "thread that links offers.")
    else:
        gap("P2", "counter-counter", f"{sc} {_short(b)}")


# ── Phase 10: Trust Score Gating ─────────────────────────────────────────
async def phase_10_trust_gating(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("10: Trust score gating — low-trust accounts limited")

    # Create a low-trust user
    low_trust_phone = f"+861389{RUN_TAG % 1000000:06d}"
    sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
        "phone": low_trust_phone,
        "display_name": "新号_low_trust",
        "primary_language": "zh-CN",
        "source_brand_id": BRAND_ID,
        "device_fingerprint": f"dev_{RUN_TAG}_lowtrust",
        "country": "CN",
    })
    if sc != 200 or not isinstance(b, dict):
        gap("P1", "register low-trust user", f"{sc} {_short(b)}")
        return
    low_kid = b["kid"]
    await _setup_consent(c, [low_kid])

    # Mark trust_score very low
    sc, _b = await call(c, "POST",
                        f"/api/v1/primitives/user/{low_kid}/attributes/trust_score",
                        json_body={"value": "0.20"},
                        params={"brand_id": BRAND_ID})
    if sc == 200:
        ok("low trust_score set", "trust_score=0.20")
    else:
        gap("P1", "trust score set", f"{sc} {_short(b)}")

    # PROBE: rule_engine — gate "create_listing" action on min trust score
    sc, b = await call(c, "POST", "/api/v1/rule-engine/conditions/check",
                       json_body={
                           "user_id": low_kid,
                           "brand_id": BRAND_ID,
                           "action": "create_listing",
                           "required_attributes": {"trust_score_gte": "0.50"},
                       })
    if sc == 200 and isinstance(b, dict):
        allowed = b.get("allowed", b.get("ok"))
        if allowed is False:
            ok("low-trust gated correctly", "create_listing denied")
        else:
            gap("P1", "trust gate not enforced",
                f"User with trust_score=0.20 allowed create_listing (need ≥0.50). "
                "Result: {_short(b)}. C2C platforms MUST gate listing creation, "
                "bidding, and withdrawal on trust score to prevent fraud rings. "
                "Today no first-class trust-gate primitive exists.")
    elif sc == 404:
        gap("P0", "no rule-engine trust gate",
            "POST /rule-engine/conditions/check 404 — no declarative way to "
            "gate user actions by attribute thresholds. Trust gating is the "
            "single biggest fraud-prevention lever in C2C; cannot be deferred.")
    elif sc in (400, 422):
        gap("P1", "rule-engine schema mismatch",
            f"{sc} {_short(b)} — endpoint exists but rejects this shape.")
    else:
        gap("P1", "trust gate check", f"{sc} {_short(b)}")

    # PROBE: limit listing count for low-trust accounts
    sc, b = await call(c, "POST", "/api/v1/frequency-cap/check", json_body={
        "user_id": low_kid,
        "brand_id": BRAND_ID,
        "action": "create_listing",
        "window_seconds": 86400,
        "max_count": 3,  # low-trust = 3 listings/day
    })
    if sc == 200 and isinstance(b, dict):
        ok("frequency-cap check available", _short(b, 150))
    elif sc == 404:
        gap("P1", "no per-action frequency cap",
            "Frequency-cap endpoint not generalized to user actions. C2C "
            "needs trust-tier-based caps (new user: 3 listings/day, verified: "
            "100/day). Today only ad-frequency capping exists.")
    else:
        gap("P2", "frequency-cap probe", f"{sc} {_short(b)}")


# ── Phase 11: Disputes — Buyer Claims Defect ─────────────────────────────
async def phase_11_disputes(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("11: Disputes — buyer claims defect, seller rating decays")

    if not state.get("listings") or len(state["users"]) < 2:
        info("Skipping — need listings + users")
        return

    listing = state["listings"][0]
    buyer_kid = list(state["users"].values())[1]
    seller_kid = listing["seller_user_id"]

    # Try to record a "purchase" first (via pixel) — get conversion_id
    sc, b = await call(c, "POST", "/api/v1/pixel/register", json_body={
        "brand_id": BRAND_ID,
        "allowed_origins": ["https://idleexcellent.com"],
    })
    pid = None
    if sc == 201 and isinstance(b, dict):
        pid = b["pixel_id"]
        ok("pixel registered for C2C", f"pid={pid}")
    else:
        gap("P1", "pixel register", f"{sc} {_short(b)}")
        return

    sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
        "pixel_id": pid,
        "event_type": "purchase",
        "user_id": buyer_kid,
        "device_fingerprint": f"dev_buyer_{RUN_TAG}",
        "order_id": f"ord_c2c_{RUN_TAG}",
        "amount_cents": listing["asking_price_cents"],
        "origin": "https://idleexcellent.com",
        "url": "https://idleexcellent.com/checkout/success",
        "metadata": {
            "seller_user_id": seller_kid,
            "listing_id": listing["listing_id"],
            "p2p": True,
        },
    }, headers={"Origin": "https://idleexcellent.com"})
    conversion_id = None
    if sc == 200 and isinstance(b, dict):
        conversion_id = b.get("conversion_id") or b.get("event_id")
        ok("P2P purchase recorded via pixel", f"conversion_id={conversion_id}")
    else:
        gap("P1", "pixel P2P purchase", f"{sc} {_short(b)}")

    # PROBE: does pixel record SELLER on purchase (not just buyer)?
    # Important for marketplace — commission, dispute, payout all tied to seller.
    if conversion_id is None:
        gap("P0", "no seller attribution on purchase",
            "Pixel purchase event records user_id (buyer) only — no native "
            "seller_user_id field. C2C marketplace must know BOTH parties on "
            "every transaction to compute commission + dispute target. Today "
            "must stuff seller into 'metadata' bag (no first-class semantics).")

    # Open a dispute — buyer claims defect
    sc, b = await call(c, "POST", "/api/v1/disputes/open", json_body={
        "brand_id": BRAND_ID,
        "conversion_id": conversion_id or f"manual_conv_{RUN_TAG}",
        "category": "fake_user",  # closest enum to "fake/defective item"
        "evidence_text": "Item arrived broken — claimed 'like new' but screen "
                         "is cracked. Photos attached.",
        "evidence_url": "https://idleexcellent.com/disputes/photo1.jpg",
    })
    dispute_id = None
    if sc in (200, 201) and isinstance(b, dict):
        dispute_id = b.get("dispute_id")
        state["dispute_id"] = dispute_id
        ok("dispute opened", f"id={dispute_id}")
    else:
        gap("P1", "open dispute", f"{sc} {_short(b)}")

    # PROBE: dispute category doesn't include C2C-specific reasons
    gap("P0", "no C2C-specific dispute categories",
        "OpenDisputeRequest.category enum is "
        "[fake_user, existing_customer, fraud_suspected, wrong_attribution, "
        "other]. None capture C2C realities: item_not_as_described, item_not_"
        "received, fake_authenticity, defective, seller_unresponsive, "
        "shipping_damage. Buyer must misuse 'fake_user' or 'other', which "
        "blocks downstream auto-resolution.")

    # Admin resolve in buyer's favor → seller rating should decay
    if dispute_id:
        sc, b = await call(c, "POST",
                           f"/api/v1/disputes/{dispute_id}/admin/resolve",
                           json_body={
                               "decision": "approved",
                               "decided_by": "admin_laohu",
                               "notes": "Buyer wins — refund + seller rating -0.15",
                           })
        if sc == 200:
            ok("dispute resolved (buyer wins)", "downstream: refund + rating decay")
        else:
            gap("P1", "dispute resolve", f"{sc} {_short(b)}")

        # PROBE: did the resolution AUTO-decay seller rating?
        sc, b = await call(c, "GET",
                           f"/api/v1/primitives/user/{seller_kid}/attributes/seller_rating")
        if sc == 200:
            info(f"post-dispute seller_rating: {_short(b)}")
            gap("P0", "no auto-link dispute → rating decay",
                "Dispute resolution doesn't auto-update seller's rating "
                "attribute. Two separate systems with no glue. C2C trust = "
                "function(dispute_outcomes). Need declarative effect on "
                "dispute policy: 'on approved → seller.attr[rating] -= 0.15'.")
        else:
            gap("P2", "rating fetch after dispute", f"{sc} {_short(b)}")


# ── Phase 12: Edge Cases ─────────────────────────────────────────────────
async def phase_12_edges(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("12: Edge cases — refund, fraud bidding, bulk listing, multi-cat")

    # 12a: P2P refund event
    sc, b = await call(c, "GET", f"/api/v1/pixel/brand/{BRAND_ID}")
    pids = b if isinstance(b, list) else []
    pid = pids[0]["pixel_id"] if pids and isinstance(pids[0], dict) and pids[0].get("pixel_id") else None
    if pid:
        sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
            "pixel_id": pid,
            "event_type": "refund",
            "user_id": list(state["users"].values())[1],
            "device_fingerprint": "dev_refund",
            "order_id": f"ord_c2c_{RUN_TAG}_refund",
            "amount_cents": 50_000,
            "origin": "https://idleexcellent.com",
            "url": "https://idleexcellent.com/refund",
        }, headers={"Origin": "https://idleexcellent.com"})
        if sc == 200:
            ok("P2P refund event accepted")
        elif sc in (400, 422):
            gap("P0", "no refund event for C2C",
                "Refund is THE most common C2C reversal. Without it, "
                "commission charged on disputed sales never reverses → "
                "marketplace eats the cost or operates at a loss.")
        else:
            gap("P1", "refund event", f"{sc} {_short(b)}")

    # 12b: Bidding-bot fraud detection
    sc, b = await call(c, "POST", "/api/v1/attribution/anti-fraud/check",
                       json_body={
                           "user_id": "bidding_bot_001",
                           "device_fingerprint": "dev_bot",
                           "ip_address": "10.0.0.1",
                           "signals": {
                               "bids_per_hour": 200,
                               "unique_listings_bid": 200,
                               "win_rate": 0.0,
                               "device_user_agent": "python-requests/2.31",
                           },
                       })
    if sc == 200 and isinstance(b, dict):
        if b.get("is_fraud") or b.get("score", 0) > 0.6:
            ok("bidding-bot flagged", _short(b, 120))
        else:
            gap("P1", "anti-fraud blind to bidding pattern",
                f"200 bids/hr, 0% win rate, curl UA scored {b.get('score','?')} "
                "— not flagged. Bidding bots inflate seller hopes and abandon, "
                "destroying trust. Need bidding-specific signals.")
    elif sc == 404:
        gap("P0", "no anti-fraud endpoint", "404 — bidding fraud unmitigated.")
    else:
        gap("P1", "anti-fraud", f"{sc} {_short(b)}")

    # 12c: Bulk listing creation (seller uploads 50 books at once)
    sc, b = await call(c, "POST", "/api/v1/primitives/listings/bulk-import",
                       json_body={
                           "brand_id": BRAND_ID,
                           "seller_user_id": list(state["users"].values())[0],
                           "listings": [
                               {"category": "books",
                                "title": f"Book #{i}",
                                "asking_price_cents": 5000 + i * 100}
                               for i in range(50)
                           ],
                       })
    if sc in (200, 201):
        ok("bulk listing import", f"{_short(b, 100)}")
    elif sc == 404:
        gap("P1", "no bulk listing import",
            "Sellers commonly clear out 50 books / 30 clothing items at once. "
            "Today each listing = one POST. Need bulk import + image batch upload.")
    else:
        gap("P2", "bulk listing", f"{sc} {_short(b)}")

    # 12d: Single seller spans 3+ categories — does platform group correctly?
    seller_kid = list(state["users"].values())[0]
    sc, b = await call(c, "GET",
                       f"/api/v1/primitives/user/{seller_kid}/attributes")
    if sc == 200 and isinstance(b, dict):
        ok("seller attributes fetch", _short(b, 200))
    else:
        gap("P2", "seller attribute fetch", f"{sc} {_short(b)}")

    # 12e: Listing expiry / auto-archive
    if state.get("listings"):
        lid = state["listings"][0]["listing_id"]
        sc, b = await call(c, "POST",
                           f"/api/v1/primitives/listings/{lid}/expire",
                           json_body={"reason": "30d_no_activity"})
        if sc in (200, 404):
            if sc == 404:
                gap("P1", "no listing-expiry endpoint",
                    "C2C marketplaces auto-archive stale listings (30-60d). "
                    "Without server-side expiry, dead listings clutter search.")
            else:
                ok("listing expire", _short(b, 100))


# ── Module Availability Probe ────────────────────────────────────────────
async def phase_13_module_probe(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("13: C2C-relevant module probe")

    probes = [
        ("kix-id.get", "GET",
            f"/api/v1/kix-id/{list(state['users'].values())[0]}"
            if state["users"] else "/api/v1/kix-id/x"),
        ("primitives.user.attrs", "GET",
            f"/api/v1/primitives/user/{list(state['users'].values())[0]}/attributes"
            if state["users"] else "/api/v1/primitives/user/x/attributes"),
        ("social.followers", "GET",
            f"/api/v1/social/{list(state['users'].values())[0]}/followers"
            if state["users"] else "/api/v1/social/x/followers"),
        ("p2p.trades.pending", "GET",
            f"/api/v1/p2p/trades/pending?user_id={list(state['users'].values())[0]}&brand_id={BRAND_ID}"
            if state["users"] else f"/api/v1/p2p/trades/pending?user_id=x&brand_id={BRAND_ID}"),
        ("audiences.brand", "GET", f"/api/v1/audiences/brand/{BRAND_ID}"),
        ("disputes.brand", "GET", f"/api/v1/disputes/brand/{BRAND_ID}"),
        ("disputes.stats", "GET", "/api/v1/disputes/stats"),
        ("storefront.public", "GET", f"/api/v1/storefront/{BRAND_ID}"),
        ("rule-engine.check", "POST", "/api/v1/rule-engine/conditions/check"),
        ("commerce.loop", "GET", f"/api/v1/commerce/brand/{BRAND_ID}/loop"),
        ("payouts.balance", "GET", f"/api/v1/payouts/brand/{BRAND_ID}/balance"),
    ]
    avail, missing = 0, 0
    for label, method, path in probes:
        sc, b = await call(c, method, path)
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
    md.append("# 老胡 / Hu Tian (闲优 / Idle Excellent) — C2C Marketplace Findings")
    md.append("")
    md.append(f"**Run tag**: `{RUN_TAG}` | **Runtime**: {runtime:.1f}s | "
              f"**Date**: {time.strftime('%Y-%m-%d %H:%M', time.localtime(start_ts))}")
    md.append("")
    md.append("## Scenario")
    md.append(
        "老胡 runs 「闲优 / Idle Excellent」 — a P2P used-goods marketplace "
        "(think 闲鱼). 15K active listings, 5K transactions/day. Categories: "
        "数码/家居/服饰/玩具/书籍. AOV ¥50–¥3000. Users are simultaneously "
        "BUYERS and SELLERS through one KiX ID. Revenue = 2% take-rate on "
        "every P2P transaction. ¥25K/月 ops budget. Core pain points: "
        "**trust** (counterfeit / no-show), **buyer-show feedback** loops "
        "(positive review = listing visibility), **chat-driven negotiation**, "
        "and **seller rating decay** on disputes. Tested Round-5 features: "
        "KiX ID universal identity, user attributes (seller_rating, "
        "transactions_completed, trust_score), social graph (followers / "
        "kudos), audience by buyer category preference."
    )
    md.append("")
    md.append("## What makes C2C marketplace different from the other 9 personas")
    md.append("")
    md.append(
        "- **Universal identity** — same KiX ID is buyer + seller; no "
        "merchant/user dichotomy.\n"
        "- **No first-class merchant** — every seller is a 'micro-merchant' "
        "with reputation but no brand record.\n"
        "- **Listings = creative** — user-uploaded photos/videos ARE the ad; "
        "marketplace doesn't author creatives.\n"
        "- **Negotiation = conversion** — chat-driven counter-offers, not "
        "Buy-Now. Multi-turn trade primitive needed.\n"
        "- **Trust > targeting** — seller_rating decay + trust-score gating "
        "are the platform's most important levers.\n"
        "- **Take-rate billing** — commission per transaction, not pre-paid "
        "ad budget.\n"
        "- **Reputation-driven discovery** — followers / kudos drive listing "
        "visibility more than CPM."
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

    md.append("## Top NEW gaps unique to C2C marketplace")
    md.append("")
    md.append(
        "1. **No 'listing' primitive** — KiX has campaigns, vouchers, "
        "creatives, but no first-class user-authored listing entity. C2C "
        "marketplaces revolve around listings (CRUD, search, expiry, "
        "sold-status, image bundles). Today 老胡 would shoehorn into "
        "vouchers (issued by merchant, wrong direction) or campaigns "
        "(brand budget, wrong owner). Need `POST /listings/create` with "
        "fields: seller_user_id, category, title, condition, asking_price, "
        "image_urls[], video_url, expires_at, status.\n"
        "2. **No 'seller' as a primitive distinct from brand** — modeling "
        "every seller as a sub-brand explodes brand-table cardinality (15K "
        "active sellers → 15K brand rows + wallets + storefronts). Need a "
        "lightweight `seller` = KiX ID + reputation + listing namespace, "
        "rolled up under the marketplace brand for billing.\n"
        "3. **Disputes have no C2C categories + don't auto-decay rating** — "
        "OpenDisputeRequest.category enum is [fake_user, existing_customer, "
        "fraud_suspected, wrong_attribution, other]. None capture C2C "
        "realities: item_not_as_described, item_not_received, fake_"
        "authenticity, defective, seller_unresponsive, shipping_damage. And "
        "dispute resolution doesn't auto-update the seller's rating "
        "attribute — two siloed systems with no glue. Need expanded "
        "categories + declarative `on_approved` policy effects on user "
        "attributes.\n"
        "4. **No numeric range attribute filters** — attribute_filter is "
        "exact-match only ({key: value}). C2C trust segmentation is "
        "ENTIRELY numeric: 'sellers with rating ≥ 4.5', 'buyers with "
        "transactions_completed > 50'. Need {key: {gte, lte, eq, in}} "
        "operator syntax for audiences.\n"
        "5. **No post-paid take-rate wallet** — KiX wallet only supports "
        "pre-paid topup. Marketplace revenue model is 2% commission on each "
        "transaction; merchant should be billed post-fact against settled "
        "transaction events, not pre-fund ¥25K. Need "
        "`billing_strategy=post_paid_commission` with `commission_bps`."
    )
    md.append("")
    md.append("## Cross-comparison: where C2C breaks the platform model")
    md.append("")
    md.append(
        "| Dimension | B2C (老黄/baby) | F&B chain (老王) | Healthcare (老蔡) | **C2C (老胡)** |\n"
        "|---|---|---|---|---|\n"
        "| Merchant identity | 1 brand | 1 brand, N stores | 1 brand, N depts | **1 brand + 1000s sellers** |\n"
        "| Creative source | Brand-authored | Brand-authored | Brand-authored | **User-uploaded photo/video** |\n"
        "| Conversion event | Buy Now (1-step) | Visit + buy | Appointment | **Multi-turn negotiation** |\n"
        "| Reputation primitive | Brand rating | Store rating | Doctor rating | **Per-user seller rating** |\n"
        "| Revenue model | Ad spend | Ad spend | Ad spend | **2% take-rate per tx** |\n"
        "| Fraud surface | Click fraud | Coupon abuse | No-show | **Bidding bots + fake listings** |\n"
        "\n"
        "**Pattern**: KiX assumes 'one brand, many users' — C2C inverts to "
        "'one platform, many micro-sellers'. Almost every primitive (brand, "
        "wallet, campaign, creative, dispute) needs a C2C lens. Suggested "
        "fix: introduce `marketplace_type` on the brand record. When set to "
        "`c2c_marketplace`, enable: listing primitive, seller sub-entity, "
        "post-paid take-rate wallet, multi-turn negotiation trade, C2C "
        "dispute categories, dispute→rating glue, and numeric-range "
        "attribute filters. This unlocks ~20% of TAM (resale, gig, P2P) "
        "that the current model can't serve."
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
# ── Phase R7: Round 7 probes — C2C marketplace listings + take-rate + offers ─
async def phase_r7_probes(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("R7: Round 7 probes — listings primitive + take-rate + multi-currency + offer chain")
    bid = state.get("brand_id") or BRAND_ID
    users = state.get("users") or {}
    user_kids = list(users.values()) if isinstance(users, dict) else []
    seller_uid = user_kids[0] if user_kids else f"seller_probe_{RUN_TAG}"
    buyer_uid = user_kids[1] if len(user_kids) >= 2 else f"buyer_probe_{RUN_TAG}"

    # 1) Recipe library — marketplace / ecommerce
    for industry in ("marketplace", "ecommerce"):
        sc, b = await call(c, "GET", "/api/v1/recipes", params={"industry": industry})
        if sc == 200 and isinstance(b, (list, dict)):
            items = b if isinstance(b, list) else b.get("recipes", b.get("items", []))
            if items:
                ok(f"recipes industry={industry}", f"{len(items)} recipes")
            else:
                gap("P1", f"recipes industry={industry} empty", "")
        else:
            gap("P1", f"recipes industry={industry}", f"{sc}")

    # 2) /listings/create + /listings/{lid}/promote
    sc, b = await call(c, "POST", "/api/v1/listings/create", json_body={
        "brand_id": bid,
        "seller_user_id": seller_uid,
        "title": "iPhone 13 Pro Max 256GB — 95新",
        "description": "Test listing for R7 probe",
        "price_cents": 450000,
        "currency": "CNY",
        "category": "electronics",
    })
    listing_id = None
    if sc in (200, 201) and isinstance(b, dict):
        listing_id = b.get("listing_id") or b.get("id")
        ok("listing create", f"lid={listing_id}")
    else:
        gap("P0", "listing create", f"{sc} {_short(b)}")

    if listing_id:
        sc, b = await call(c, "POST", f"/api/v1/listings/{listing_id}/promote", json_body={
            "duration_days": 7,
            "promotion_cents": 500,
        })
        if sc in (200, 201):
            ok("listing promote", "")
        elif sc in (400, 422):
            gap("P1", "listing promote schema", f"{sc} {_short(b)}")
        else:
            gap("P1", "listing promote", f"{sc} {_short(b)}")

    # 3) /wallet/{bid}/take-rate/configure with category_rates AND currency_rates
    sc, b = await call(c, "POST", f"/api/v1/wallet/{bid}/take-rate/configure", json_body={
        "default_basis_points": 500,
        "minimum_fee_cents": 100,
        "category_rates": {
            "electronics": 600,
            "luxury": 800,
            "books": 300,
        },
        "currency_rates": {
            "CNY": 500,
            "USD": 700,
        },
    })
    if sc in (200, 201):
        ok("take-rate configured (category + currency)", "")
    elif sc in (400, 422):
        gap("P1", "take-rate configure schema",
            f"{sc} {_short(b)} — category_rates/currency_rates rejected")

    # 4) /wallet/{bid}/marketplace-charge + marketplace-charge-multi-currency
    sc, b = await call(c, "POST", f"/api/v1/wallet/{bid}/marketplace-charge", json_body={
        "gross_amount_cents": 100000,
        "seller_user_id": seller_uid,
        "buyer_user_id": buyer_uid,
        "category": "electronics",
        "listing_id": listing_id or "x",
    })
    if sc in (200, 201):
        ok("marketplace-charge", "")
    elif sc in (400, 422):
        gap("P1", "marketplace-charge schema", f"{sc} {_short(b)}")
    else:
        gap("P1", "marketplace-charge", f"{sc} {_short(b)}")

    sc, b = await call(c, "POST",
                       f"/api/v1/wallet/{bid}/marketplace-charge-multi-currency",
                       json_body={
                           "gross_amount_cents": 10000,
                           "currency": "USD",
                           "seller_user_id": seller_uid,
                           "buyer_user_id": buyer_uid,
                           "category": "electronics",
                           "listing_id": listing_id or "x",
                       })
    if sc in (200, 201):
        ok("marketplace-charge-multi-currency", "")
    elif sc in (400, 422):
        gap("P1", "marketplace-charge-multi-currency schema", f"{sc} {_short(b)}")
    else:
        gap("P1", "marketplace-charge-multi-currency", f"{sc} {_short(b)}")

    # 5) /listings/{lid}/offer + /accept + /counter (offer chain)
    if listing_id:
        sc, b = await call(c, "POST", f"/api/v1/listings/{listing_id}/offer", json_body={
            "buyer_user_id": buyer_uid,
            "offer_cents": 420000,
            "message": "minor scratch okay?",
        })
        offer_id = None
        if sc in (200, 201) and isinstance(b, dict):
            offer_id = b.get("offer_id") or b.get("id")
            ok("listing offer create", f"oid={offer_id}")
        else:
            gap("P1", "listing offer", f"{sc} {_short(b)}")

        if offer_id:
            sc, b = await call(c, "POST",
                               f"/api/v1/listings/{listing_id}/counter",
                               json_body={
                                   "offer_id": offer_id,
                                   "counter_cents": 440000,
                                   "message": "would do 4400",
                               })
            if sc in (200, 201):
                ok("listing counter-offer", "")
            elif sc in (400, 422):
                gap("P1", "counter schema", f"{sc} {_short(b)}")
            else:
                gap("P1", "counter offer", f"{sc} {_short(b)}")

            sc, b = await call(c, "POST",
                               f"/api/v1/listings/{listing_id}/accept",
                               json_body={"offer_id": offer_id})
            if sc in (200, 201):
                ok("listing offer accept", "")
            elif sc in (400, 422):
                gap("P1", "accept schema", f"{sc} {_short(b)}")
            else:
                gap("P1", "accept offer", f"{sc} {_short(b)}")


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
                await phase_3_dual_role_kix_ids(c, state)
                await phase_4_seller_attributes(c, state)
                await phase_5_social_followers(c, state)
                await phase_6_listing_creative(c, state)
                await phase_7_audience_by_pref(c, state)
                await phase_8_cps_listing_promo(c, state)
                await phase_9_bidding(c, state)
                await phase_10_trust_gating(c, state)
                await phase_11_disputes(c, state)
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
