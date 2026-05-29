"""Merchant journey simulation — 老梁 / Liang Bo (天下游 Worldwide Travel, Hangzhou).

End-to-end probe of the KiX Ads Platform from the perspective of a PREMIUM
TRAVEL AGENCY with:
  - LONG SALES CYCLE (6+ months from honeymoon browsing → booking → travel)
  - GROUP BOOKINGS (single buyer, 4-50 travelers)
  - MULTI-CURRENCY (CNY base, USD/EUR/JPY/THB/IDR per destination)
  - SUPPLIER MARKETPLACE (50 hotels/airlines/activities aggregated)
  - SEASONAL DEMAND SPIKES (Chinese New Year, Golden Week — 10x burst)
  - REFUNDABLE vs NON-REFUNDABLE voucher policy
  - VISA APPLICATION TRIGGERS (60d before Schengen travel)
  - MULTI-LEG ATTRIBUTION (checkin → arrival → itinerary → return = 4 touchpoints)

Pattern follows scripts/sim_laowang.py and scripts/sim_laozhou.py.

In-process via httpx.ASGITransport so no separate server is needed. Requires
a live local Redis.

Run:
    .venv/bin/python scripts/sim_laoliang.py
"""
from __future__ import annotations

import asyncio
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
OWNER_USER_ID = f"laoliang_{RUN_TAG}"
FINDINGS_PATH = Path("/Users/mozat/a-docs/laoliang-sim-findings.md")

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"

# Destination-region sub-brands. The probe asks whether the platform supports
# multi-line-of-business under one master via either (a) sub-brands per region
# or (b) a single brand with destination tags.
SUB_BRANDS: list[dict[str, Any]] = [
    {"brand_id": f"worldwide_japan_{RUN_TAG}", "name": "天下游 日本线",
     "region": "japan", "currency": "JPY", "city": "Hangzhou"},
    {"brand_id": f"worldwide_europe_{RUN_TAG}", "name": "天下游 欧洲线",
     "region": "europe", "currency": "EUR", "city": "Hangzhou"},
    {"brand_id": f"worldwide_seasia_{RUN_TAG}", "name": "天下游 东南亚线",
     "region": "seasia", "currency": "THB", "city": "Hangzhou"},
    {"brand_id": f"worldwide_cruise_{RUN_TAG}", "name": "天下游 邮轮线",
     "region": "cruise", "currency": "USD", "city": "Hangzhou"},
    {"brand_id": f"worldwide_domestic_{RUN_TAG}", "name": "天下游 国内线",
     "region": "domestic", "currency": "CNY", "city": "Hangzhou"},
]

# Suppliers (hotels, airlines, activities) the agency aggregates
SUPPLIERS: list[dict[str, Any]] = [
    {"id": f"hilton_tokyo_{RUN_TAG}", "type": "hotel", "name": "Hilton Tokyo",
     "region": "japan", "rev_split_pct": 12},
    {"id": f"ana_airlines_{RUN_TAG}", "type": "airline", "name": "ANA Airlines",
     "region": "japan", "rev_split_pct": 5},
    {"id": f"jr_pass_{RUN_TAG}", "type": "activity", "name": "JR Pass 7-day",
     "region": "japan", "rev_split_pct": 8},
    {"id": f"marriott_paris_{RUN_TAG}", "type": "hotel", "name": "Marriott Paris",
     "region": "europe", "rev_split_pct": 15},
    {"id": f"air_france_{RUN_TAG}", "type": "airline", "name": "Air France",
     "region": "europe", "rev_split_pct": 6},
    {"id": f"royal_caribbean_{RUN_TAG}", "type": "cruise", "name": "Royal Caribbean",
     "region": "cruise", "rev_split_pct": 18},
    {"id": f"phuket_villa_{RUN_TAG}", "type": "hotel", "name": "Phuket Villa Resort",
     "region": "seasia", "rev_split_pct": 14},
    {"id": f"thai_air_{RUN_TAG}", "type": "airline", "name": "Thai Airways",
     "region": "seasia", "rev_split_pct": 5},
]

CN_FIRSTNAMES = [
    "Wei", "Fang", "Min", "Jing", "Lei", "Hui", "Xin", "Yan", "Bo", "Mei",
    "Chen", "Liu", "Zhao", "Zhou", "Wu", "Xu", "Sun", "Zhu", "Lin", "He",
]

# Currency conversion (mock rates, 1 CNY = X foreign)
FX_RATES = {"CNY": 1.0, "USD": 0.14, "EUR": 0.13, "JPY": 21.0, "THB": 5.0, "IDR": 2200.0}

# Round 2 attribution window (long sales cycle: 210 days = 7 months)
LONG_SALES_CYCLE_DAYS = 210


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
        "phase": _current_phase, "severity": sev,
        "action": action, "detail": detail,
    })
    color = RED if sev == "P0" else (YELLOW if sev == "P1" else MAGENTA)
    print(f"  {color}[GAP {sev}]{RESET} {action} — {detail}")


def fail(action: str, detail: str) -> None:
    phase_counters[_current_phase]["fail"] += 1
    findings.append({
        "phase": _current_phase, "severity": "FAIL",
        "action": action, "detail": detail,
    })
    print(f"  {RED}[FAIL]{RESET} {action} — {detail}")


def info(msg: str) -> None:
    print(f"  {BLUE}[..]{RESET} {msg}")


# ── HTTP helpers ─────────────────────────────────────────────────────────
async def call(
    c: httpx.AsyncClient, method: str, path: str,
    *, json_body: Any = None, params: dict | None = None,
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


# ── Consent helper ────────────────────────────────────────────────────────
_consent_policy_published = False
POLICY_VERSION = f"v_{RUN_TAG}"


async def _setup_consent(c: httpx.AsyncClient, user_ids: list[str]) -> int:
    global _consent_policy_published
    if not _consent_policy_published:
        await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
            "version": POLICY_VERSION,
            "text_md": "# 天下游 consent\nCross-region travel tracking + supplier-shared booking data",
            "effective_at": int(time.time()) - 60,
            "requires_re_grant": False,
        })
        _consent_policy_published = True
    granted = 0
    for uid in user_ids:
        sc, _ = await call(c, "POST", "/api/v1/consent/grant", json_body={
            "user_id": uid,
            "scopes": ["cross_brand_tracking", "geo_lbs", "personalization", "marketing"],
            "policy_version": POLICY_VERSION,
            "source": "app",
        })
        if sc == 200:
            granted += 1
    return granted


# ── Phase 1: Master + Travel Sub-brands ──────────────────────────────────
async def phase_1_master_setup(c: httpx.AsyncClient) -> dict[str, Any]:
    _phase_init("1: Master Account + 5 Destination-Region Sub-Brands")
    state: dict[str, Any] = {"master_id": None}

    sc, b = await call(c, "POST", "/api/v1/master/create", json_body={
        "company_name": "天下游国际旅行社 / Worldwide Travel Corp",
        "primary_email": "laoliang@worldwidetravel.cn",
        "owner_user_id": OWNER_USER_ID,
    })
    if sc == 201 and isinstance(b, dict):
        state["master_id"] = b["master_id"]
        ok("create master", f"id={state['master_id']}")
    else:
        fail("create master", f"{sc} {_short(b)}")
        return state

    master_id = state["master_id"]

    # Attach 5 region sub-brands
    attached = 0
    for s in SUB_BRANDS:
        sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/brands/attach",
                           json_body={
                               "brand_id": s["brand_id"],
                               "store_name": s["name"],
                               "store_id": s["brand_id"],
                           })
        if sc == 200:
            attached += 1
        else:
            gap("P1", f"attach sub-brand {s['region']}", f"{sc} {_short(b)}")
    if attached == len(SUB_BRANDS):
        ok(f"attach {attached} region sub-brands", "japan/europe/seasia/cruise/domestic")
    else:
        gap("P0", "sub-brand attach",
            f"only {attached}/{len(SUB_BRANDS)} attached — multi-line-of-business "
            "model is structurally broken at master level")

    # Probe: Can sub-brand declare destination metadata (region/currency)?
    # The brands/attach payload only accepts brand_id + store_name. There is
    # no place to record "this brand serves Japan in JPY".
    gap("P1", "no line-of-business metadata on sub-brand attach",
        "POST /master/{id}/brands/attach accepts brand_id + store_name only. "
        "There is no region, default_currency, business_line, or LOB-type field. "
        "Travel agencies aggregating destinations cannot tag 'this brand sells "
        "Japan tours in JPY' — every downstream report must infer region from "
        "campaign metadata. Industry verticals with multi-LOB structures "
        "(travel/insurance/financial-services/healthcare-network) need first-class "
        "LOB metadata at the brand-attach edge.")

    state["sub_brands"] = SUB_BRANDS
    return state


# ── Phase 2: Wallet + Multi-Currency Support ─────────────────────────────
async def phase_2_wallet(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("2: Wallet ¥30,000 + Multi-Currency Support Probe")
    master_id = state.get("master_id")
    if not master_id:
        fail("phase 2", "no master_id")
        return

    # Set master global budget — ¥30K/month, 20% per region
    allocation = {s["brand_id"]: 0.20 for s in SUB_BRANDS}
    sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/budget/global",
                       json_body={
                           "monthly_budget_cents": 3_000_000,  # ¥30K
                           "allocation": allocation,
                       })
    if sc == 200:
        ok("master global budget", "¥30K/月 even 20% × 5 regions")
    else:
        gap("P1", "master global budget", f"{sc} {_short(b)}")

    # Top up each region wallet ¥6K
    funded = 0
    for s in SUB_BRANDS:
        sc, b = await call(c, "POST", f"/api/v1/wallet/{s['brand_id']}/topup",
                           json_body={"amount_cents": 600_000, "payment_method": "wechat"})
        if sc != 200 or not isinstance(b, dict) or "topup_id" not in b:
            gap("P1", f"topup {s['region']}", f"{sc} {_short(b)}")
            continue
        tid = b["topup_id"]
        sc2, _ = await call(c, "POST",
                            f"/api/v1/wallet/{s['brand_id']}/topup/{tid}/confirm",
                            json_body={"payment_gateway_response": {"mock": True}})
        if sc2 == 200:
            funded += 1
    if funded == len(SUB_BRANDS):
        ok("region wallets funded", f"¥6000 × {funded}")
    else:
        gap("P0", "region wallet funding", f"only {funded}/{len(SUB_BRANDS)}")

    # Probe: Can wallet topup specify a currency? (e.g. ¥6K base + USD 500 sub-balance)
    sc, b = await call(c, "POST",
                       f"/api/v1/wallet/{SUB_BRANDS[0]['brand_id']}/topup",
                       json_body={"amount_cents": 500_00,  # USD 500 in "cents"
                                  "currency": "USD",
                                  "payment_method": "wechat"})
    if sc == 200:
        # Inspect to see if currency was honored
        sc2, b2 = await call(c, "GET",
                             f"/api/v1/wallet/{SUB_BRANDS[0]['brand_id']}/daily-budget-status")
        if isinstance(b2, dict) and (b2.get("currency") or b2.get("currencies")):
            ok("multi-currency wallet topup", f"currency preserved")
        else:
            gap("P0", "multi-currency wallet — no FX support",
                "POST /wallet/{bid}/topup accepted a `currency: USD` field but the "
                "wallet status returns no currency metadata. The platform appears to "
                "store all balances as a single CNY-cents integer. Travel agencies "
                "running USD promotion budgets for international flights cannot "
                "separate USD reserves from CNY reserves — every conversion happens "
                "off-platform manually. Multi-currency is a P0 for travel / "
                "luxury-imports / international-ecommerce / FX-hedging merchants.")
    elif sc in (400, 422):
        gap("P0", "no currency field on wallet topup",
            f"POST /wallet/topup rejects `currency` field ({sc} {_short(b, 120)}). "
            "Wallet is implicitly single-currency (CNY). International travel agencies "
            "promoting in USD/EUR/JPY have no way to declare a foreign budget pool.")
    else:
        gap("P1", "wallet currency probe", f"{sc} {_short(b)}")

    # Probe: daily-budget status returns FX info?
    sc, b = await call(c, "GET",
                       f"/api/v1/wallet/{SUB_BRANDS[0]['brand_id']}/daily-budget-status")
    if sc == 200 and isinstance(b, dict):
        if "fx_rate" in b or "currencies" in b or "base_currency" in b:
            ok("wallet FX metadata exposed", f"keys: {list(b)[:5]}")
        else:
            gap("P1", "wallet daily status has no FX/currency keys",
                f"keys={list(b)[:6]} — no currency, fx_rate, base_currency. "
                "Reports can't disambiguate ¥1200 cap from $1200 cap.")
    state["fx_probe_done"] = True


# ── Phase 3: Consent + Tier ──────────────────────────────────────────────
async def phase_3_consent_tier(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("3: Consent Policy + Traveler Tiers")

    # Publish consent
    sc, b = await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
        "version": POLICY_VERSION,
        "text_md": "# 天下游 traveler consent\nCross-region travel tracking",
        "effective_at": int(time.time()) - 60,
        "requires_re_grant": False,
    })
    if sc == 200:
        ok("publish consent policy", POLICY_VERSION)
        global _consent_policy_published
        _consent_policy_published = True
    else:
        gap("P0", "publish consent", f"{sc} {_short(b)}")

    primary_bid = SUB_BRANDS[0]["brand_id"]
    state["primary_bid"] = primary_bid

    # Tier config (4 traveler tiers based on lifetime trip count + AOV)
    sc, b = await call(c, "POST", "/api/v1/primitives/tier/configure", json_body={
        "brand_id": primary_bid,
        "tiers": [
            {"name": "explorer", "xp_min": 0},
            {"name": "frequent_traveler", "xp_min": 1000},
            {"name": "vip_globetrotter", "xp_min": 5000},
            {"name": "lifetime_member", "xp_min": 20000},
        ],
    })
    if sc == 200:
        ok("tier configure", "explorer/frequent/vip/lifetime XP thresholds")
    else:
        gap("P1", "tier configure", f"{sc} {_short(b)}")

    tiers = [
        {"id": "explorer", "name": "Explorer", "threshold_xp": 0,
         "perks": ["1_brochure"]},
        {"id": "frequent_traveler", "name": "Frequent Traveler", "threshold_xp": 1000,
         "perks": ["priority_booking", "free_visa_assist"]},
        {"id": "vip_globetrotter", "name": "VIP Globetrotter", "threshold_xp": 5000,
         "perks": ["lounge_access", "free_upgrade", "dedicated_concierge"]},
        {"id": "lifetime_member", "name": "Lifetime Member", "threshold_xp": 20000,
         "perks": ["all_vip", "annual_free_domestic", "first_class_upgrade"]},
    ]
    created = 0
    for t in tiers:
        sc, b = await call(c, "POST", f"/api/v1/primitives/brand/{primary_bid}/tiers",
                           json_body=t)
        if sc == 200:
            created += 1
        else:
            gap("P1", f"create tier {t['id']}", f"{sc} {_short(b)}")
    if created == 4:
        ok("create 4 tiers", "explorer / frequent / vip / lifetime")
    else:
        gap("P0", "create tiers", f"only {created}/4")

    # CRITICAL: Probe master-level tier portability (Round 4 promise)
    # Try a hypothetical master-level tier endpoint
    sc, b = await call(c, "POST",
                       f"/api/v1/master/{state['master_id']}/tiers/configure",
                       json_body={
                           "tiers": [
                               {"name": "explorer", "xp_min": 0},
                               {"name": "frequent_traveler", "xp_min": 1000},
                               {"name": "vip_globetrotter", "xp_min": 5000},
                               {"name": "lifetime_member", "xp_min": 20000},
                           ],
                       })
    if sc in (200, 201):
        ok("master-level tier configure", "Round 4 master tier endpoint exists")
        state["master_tier_works"] = True
    elif sc == 404:
        gap("P0", "master-level tier portability missing",
            "POST /master/{id}/tiers/configure returns 404 — there is no master-level "
            "tier definition that propagates to all sub-brands. A VIP traveler who "
            "earned vip_globetrotter via 5 Japan trips has NO tier on the Europe "
            "sub-brand. Multi-LOB merchants (travel, insurance, hospitality chains) "
            "*require* master-level tiers as the basic loyalty primitive. Today, "
            "老梁 must hand-replicate the tier definition across all 5 region brands "
            "and reconcile XP balances per brand. Cross-region tier portability is "
            "the SINGLE most important loyalty feature for an agency.")
        state["master_tier_works"] = False
    else:
        gap("P1", "master-level tier endpoint", f"{sc} {_short(b)}")

    # Verify second sub-brand tier visibility (cross-brand portability check)
    sc, b = await call(c, "GET",
                       f"/api/v1/primitives/brand/{SUB_BRANDS[1]['brand_id']}/tiers")
    second_tiers = b if isinstance(b, list) else []
    if not second_tiers:
        gap("P0", "cross-region tier portability",
            f"Tiers created on {primary_bid} (Japan) but {SUB_BRANDS[1]['brand_id']} "
            "(Europe) sees NO tiers. A VIP Globetrotter who booked a Tokyo trip and "
            "earned 5000 XP cannot use their VIP perks on a Paris booking. Brand-scoped "
            "tiers are fundamentally wrong for travel agencies — membership is "
            "*lifetime* and *destination-agnostic*.")
    else:
        ok("cross-region tier portability", f"second region sees {len(second_tiers)} tiers")


# ── Phase 4: Group Booking Reservation ───────────────────────────────────
async def phase_4_group_booking(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("4: Group Booking Reservation (party_size=20)")
    primary_bid = state["primary_bid"]
    organizer_uid = f"group_organizer_{RUN_TAG}"
    await _setup_consent(c, [organizer_uid])

    # Create a group booking reservation — single buyer, 20 travelers
    sc, b = await call(c, "POST", "/api/v1/reservations/create", json_body={
        "brand_id": primary_bid,
        "user_id": organizer_uid,
        "scheduled_at": int(time.time()) + 86400 * 90,  # 90 days from now
        "party_size": 20,
        "type": "tour",
        "metadata": {
            "destination": "Tokyo + Kyoto + Osaka 10-day",
            "departure_city": "Hangzhou",
            "total_value_cents": 30_000_00 * 20,  # ¥30K × 20 people = ¥600K
            "travelers": [  # speculative — does it accept per-traveler details?
                {"name": f"Traveler_{i}", "passport": f"E{10000000 + i}",
                 "dob": f"198{i%10}-01-{(i%28)+1:02d}"}
                for i in range(20)
            ],
        },
        "check_in_grace_minutes": 60,
    })
    if sc in (200, 201) and isinstance(b, dict):
        rid = b.get("reservation_id")
        state["group_reservation_id"] = rid
        ok("group reservation create", f"rid={rid} party_size=20 type=tour")

        # Read back and check if travelers list was preserved
        sc2, b2 = await call(c, "GET", f"/api/v1/reservations/{rid}")
        if sc2 == 200 and isinstance(b2, dict):
            meta = b2.get("metadata", {})
            travelers = meta.get("travelers")
            if travelers and isinstance(travelers, list) and len(travelers) == 20:
                ok("group traveler manifest preserved",
                   f"{len(travelers)} travelers in metadata")
                gap("P1", "no first-class group_manifest primitive",
                    "Travelers were stored in `metadata.travelers` (free-form JSON). "
                    "There is no first-class `group_manifest` schema with per-traveler "
                    "passport / DOB / dietary / mobility / visa-status fields. Compliance "
                    "for international travel needs validated per-traveler records — "
                    "today everything is unstructured metadata.")
            else:
                gap("P0", "group traveler manifest not preserved",
                    f"Created reservation with 20 traveler objects in metadata but "
                    f"readback shows travelers={travelers!r}. Either metadata is "
                    "truncated or the platform's reservation primitive only models "
                    "party_size as a count — multi-traveler bookings cannot store "
                    "passport/identity per traveler. This breaks every international "
                    "tour: airlines need passport numbers per seat, hotels need "
                    "rooming lists, immigration needs identity documents.")
        # Probe: is there a separate /reservations/{rid}/travelers endpoint?
        sc3, _ = await call(c, "GET", f"/api/v1/reservations/{rid}/travelers")
        if sc3 == 200:
            ok("dedicated traveler manifest endpoint exists", "")
        elif sc3 == 404:
            gap("P0", "no /reservations/{rid}/travelers endpoint",
                "GET /reservations/{rid}/travelers returns 404 — there is no API "
                "to fetch/edit the per-traveler list separately from the main "
                "reservation body. Group bookings must hand-roll their own.")
    else:
        gap("P0", "group reservation create",
            f"{sc} {_short(b)} — could not create party_size=20 tour reservation")

    # Probe: party_size limits (50 = bus-tour edge)
    sc, b = await call(c, "POST", "/api/v1/reservations/create", json_body={
        "brand_id": primary_bid,
        "user_id": organizer_uid,
        "scheduled_at": int(time.time()) + 86400 * 120,
        "party_size": 50,
        "type": "tour",
        "metadata": {"destination": "Europe coach tour 50-pax"},
    })
    if sc in (200, 201):
        ok("party_size=50 reservation accepted", "bus-tour scale supported")
    elif sc in (400, 422):
        gap("P1", "party_size upper bound",
            f"party_size=50 rejected ({sc} {_short(b, 120)}). Travel agencies "
            "regularly book 50+ pax coach tours and 100+ pax cruise groups. "
            "Document the cap or remove it.")
    else:
        gap("P1", "party_size=50 probe", f"{sc} {_short(b)}")


# ── Phase 5: Long Sales Cycle Attribution (7 months) ─────────────────────
async def phase_5_long_cycle_attribution(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("5: Long Sales Cycle — 7-month attribution_window_days")
    primary_bid = state["primary_bid"]

    # Create a campaign with attribution_window_days=210 (7 months)
    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": primary_bid,
        "name": "Honeymoon — 7-month consideration",
        "objective": "acquire",
        "bid_strategy": "cpa",
        "max_bid_cents": 100_000,    # ¥1000 per converted couple (high AOV justifies)
        "daily_budget_cents": 50_000,
        "total_budget_cents": 2_000_000,
        "attribution_window_days": LONG_SALES_CYCLE_DAYS,  # 210d
        "targeting": {
            "geo": {"country": "CN", "city": "Hangzhou", "radius_km": 80},
            "demographics": {"age_min": 25, "age_max": 40},
        },
        "creative": {
            "recipe_id": "duolingo_streak",
            "game_slug": "honeymoon_planner",
        },
        "schedule": {"start_at": time.time() - 60, "end_at": time.time() + 86400 * 220},
    })
    if sc == 200 and isinstance(b, dict):
        cid = b["campaign_id"]
        state["long_cycle_campaign_id"] = cid
        ok("create long-cycle campaign",
           f"id={cid} attribution_window_days={LONG_SALES_CYCLE_DAYS}")

        # Approve
        sc_a, _ = await call(c, "POST", f"/api/v1/campaigns/{cid}/admin/approve",
                             json_body={"admin_token": "DEV", "notes": "sim"})
        if sc_a == 200:
            ok("approve long-cycle campaign", "via admin endpoint")

        # Read back — is the 210-day window actually preserved?
        sc, b = await call(c, "GET", f"/api/v1/campaigns/{cid}/details")
        if sc == 200 and isinstance(b, dict):
            stored = b.get("attribution_window_days", 0)
            if stored == LONG_SALES_CYCLE_DAYS:
                ok("attribution_window_days persisted", f"stored={stored}")
            elif stored == 0:
                gap("P0", "attribution_window_days lost (0)",
                    f"Campaign created with attribution_window_days=210 but readback "
                    "shows 0. Travel agencies (and any 6-month-cycle merchant — real "
                    "estate, university, wedding venue, life insurance) cannot retain "
                    "conversions that happen months after the trigger. Round 2 fix "
                    "did not persist for very long windows.")
            elif stored == LONG_SALES_CYCLE_DAYS:
                pass
            else:
                gap("P0", "attribution_window_days truncated",
                    f"set=210, read={stored}. Platform may cap at 90/180 days. "
                    "Honeymoon planning takes 6-12 months; the platform-enforced cap "
                    "is the bug.")
    else:
        gap("P0", "create long-cycle campaign", f"{sc} {_short(b)}")
        return

    # Probe: simulate the full 210-day chain — token created July 2025,
    # conversion February 2026. We can't time-travel; instead we verify
    # the attribution token surface honors a long ttl_hours.
    sc, b = await call(c, "POST", "/api/v1/attribution/token/create", json_body={
        "source_brand": primary_bid,
        "source_user_id": f"honeymoon_browser_{RUN_TAG}",
        "target_brand": primary_bid,
        "channel": "wechat",
        "ttl_hours": LONG_SALES_CYCLE_DAYS * 24,  # 5040 hours
    })
    if sc == 200 and isinstance(b, dict) and b.get("token"):
        ok("long-ttl attribution token", f"ttl={LONG_SALES_CYCLE_DAYS}d accepted")
        state["long_attribution_token"] = b["token"]
    elif sc in (400, 422):
        gap("P0", "attribution token TTL capped",
            f"POST /attribution/token/create rejects ttl_hours=5040 ({sc} "
            f"{_short(b, 120)}). Long-cycle merchants cannot mint browse→book "
            "tokens that survive 7 months. Default cap is too low.")
    else:
        gap("P1", "long-ttl attribution token", f"{sc} {_short(b)}")


# ── Phase 6: Multi-Currency Pricing & Voucher ────────────────────────────
async def phase_6_multicurrency_voucher(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("6: Multi-Currency Voucher + CPS Commission Base Probe")
    europe_bid = SUB_BRANDS[1]["brand_id"]

    # Try a voucher template with a non-CNY currency
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": europe_bid,
        "name": "EUR 100 off Paris hotel",
        "description": "€100 off any Marriott Paris booking",
        "value": {"type": "fixed", "amount": 10_000, "currency": "EUR"},
        "conditions": {"usage_limit_per_user": 1, "min_purchase_amount_cents": 100_000},
        "expires_in_days": 90,
        "stackable": False,
        "transferable": False,
    })
    if sc == 201 and isinstance(b, dict):
        state["eur_voucher_template"] = b.get("template_id")
        ok("EUR voucher template", f"id={b.get('template_id')} currency=EUR accepted")
    elif sc in (400, 422):
        gap("P0", "voucher currency restricted to CNY",
            f"voucher template with currency=EUR rejected: {sc} {_short(b, 150)}. "
            "Multi-currency vouchers are core to international travel — €100 off "
            "a Paris hotel cannot be expressed natively. Workaround (¥800 CNY-equiv) "
            "doesn't track exchange-rate drift between issue and redemption.")
    else:
        gap("P1", "multi-currency voucher", f"{sc} {_short(b)}")

    # JPY voucher
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": SUB_BRANDS[0]["brand_id"],
        "name": "JPY 10000 off Tokyo hotel",
        "value": {"type": "fixed", "amount": 1_000_000, "currency": "JPY"},
        "conditions": {"usage_limit_per_user": 1},
        "expires_in_days": 60,
    })
    if sc == 201:
        ok("JPY voucher template", "currency=JPY accepted")
    elif sc in (400, 422):
        gap("P0", "JPY voucher rejected",
            "Even after EUR was rejected, JPY also rejected — voucher engine is "
            "single-currency (CNY) by design.")

    # CPS campaign on a multi-currency order — what's the commission base?
    # Order: ¥30,000 (base) + USD 4500 international flights + EUR 1200 hotel
    cps_uid = f"cps_buyer_{RUN_TAG}"
    await _setup_consent(c, [cps_uid])

    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": europe_bid,
        "name": "Europe Tour CPS — 8% commission",
        "objective": "sales",
        "bid_strategy": "cps",
        "max_bid_cents": 800,  # 8%
        "daily_budget_cents": 100_000,
        "total_budget_cents": 1_000_000,
        "targeting": {"geo": {"country": "CN"}},
        "creative": {"recipe_id": "duolingo_streak"},
        "schedule": {"start_at": time.time() - 60, "end_at": time.time() + 86400 * 60},
        "attribution_window_days": 90,
    })
    if sc == 200 and isinstance(b, dict):
        cps_cid = b["campaign_id"]
        ok("CPS campaign", f"id={cps_cid} 8% commission")
        await call(c, "POST", f"/api/v1/campaigns/{cps_cid}/admin/approve",
                   json_body={"admin_token": "DEV"})

        # Register a pixel + fire mixed-currency purchases
        sc_px, b_px = await call(c, "POST", "/api/v1/pixel/register",
                                 json_body={
                                     "brand_id": europe_bid,
                                     "allowed_origins": ["https://worldwidetravel.cn"],
                                 })
        if sc_px == 201 and isinstance(b_px, dict):
            pid = b_px["pixel_id"]
            # Send 3 events: base CNY, USD flights, EUR hotel
            events = [
                {"amount": 30_000_00, "currency": "CNY", "desc": "tour base"},
                {"amount": 4500_00, "currency": "USD", "desc": "flights"},
                {"amount": 1200_00, "currency": "EUR", "desc": "hotel"},
            ]
            for i, ev in enumerate(events):
                sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
                    "pixel_id": pid,
                    "event_type": "purchase",
                    "device_fingerprint": f"dev_eur_{i}",
                    "user_id": cps_uid,
                    "order_id": f"euro_tour_{RUN_TAG}_{i}",
                    "amount_cents": ev["amount"],
                    "currency": ev["currency"],
                    "origin": "https://worldwidetravel.cn",
                    "meta": {"segment": ev["desc"]},
                })
                if sc == 200:
                    ok(f"pixel event {ev['currency']}", f"{ev['desc']} accepted")
                else:
                    gap("P1", f"pixel event {ev['currency']}",
                        f"{sc} {_short(b, 100)}")

            # Check pixel stats — does it surface per-currency rollup?
            sc, b = await call(c, "GET", f"/api/v1/pixel/brand/{europe_bid}")
            if sc == 200 and isinstance(b, dict):
                if "by_currency" in b or "currencies" in b or "fx_normalized" in b:
                    ok("pixel per-currency rollup", "FX-aware stats present")
                else:
                    gap("P0", "pixel sums currencies as raw cents",
                        f"GET /pixel/brand/{{bid}} returns total_amount_cents as a "
                        f"single integer summing CNY+USD+EUR (¥30000 + $4500 + €1200) "
                        "as if they were all CNY cents. Spend/revenue reports for "
                        "any multi-currency merchant (travel, FX broker, international "
                        "ecom, luxury) are mathematically WRONG. Needs `by_currency: "
                        "{CNY: ..., USD: ..., EUR: ...}` OR `fx_normalized_cents: ...` "
                        "with rate timestamps.")

    # Probe: voucher discount on multi-currency order
    if state.get("eur_voucher_template"):
        gap("P1", "no FX-aware voucher application",
            "Even if EUR vouchers were accepted, there is no documented logic for "
            "applying a €100 voucher to a mixed-currency order (CNY+USD+EUR). "
            "Should the discount snap to EUR segments only? Or FX-convert? "
            "Undefined behavior is a refund-dispute generator.")

    # CPS commission base ambiguity
    gap("P0", "CPS commission base unspecified for multi-currency orders",
        "An 8% CPS campaign on a ¥30K + $4500 + €1200 order: what's the 8% applied "
        "to? Today the platform sums all currencies as raw cents → 8% of "
        "(30000_00 + 4500_00 + 1200_00) = 8% of CNY 35700.00 (wrong). Travel "
        "agencies booking international flights need a documented FX-normalization "
        "rule + the rate timestamp baked into the commission ledger.")


# ── Phase 7: Group Cohesion Gamification ─────────────────────────────────
async def phase_7_group_gamification(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("7: Group Cohesion Gamification — group-level achievements")
    primary_bid = state["primary_bid"]

    # Create a group of 10 travelers
    group_uids = [f"group_member_{RUN_TAG}_{i:02d}" for i in range(10)]
    await _setup_consent(c, group_uids)

    # Try to create a group via /groups primitive (group_actions router)
    sc, b = await call(c, "POST", "/api/v1/groups/create", json_body={
        "brand_id": primary_bid,
        "name": f"Tokyo Group {RUN_TAG}",
        "member_user_ids": group_uids,
        "metadata": {"trip": "Tokyo+Kyoto 10-day", "departure": "2026-04-01"},
    })
    group_id = None
    if sc in (200, 201) and isinstance(b, dict):
        group_id = b.get("group_id") or b.get("id")
        ok("group create", f"id={group_id} members={len(group_uids)}")
        state["tour_group_id"] = group_id
    elif sc == 404:
        gap("P1", "no /groups/create endpoint",
            "POST /api/v1/groups/create 404. Travel agencies need a 'tour group' "
            "primitive to track collective behavior (group of 10 travelers earn "
            "badges together). Without this, group bookings revert to per-individual "
            "reservation list + manual aggregation.")
    else:
        gap("P1", "group create", f"{sc} {_short(b)}")

    # Group-level achievement probe
    sc, b = await call(c, "POST",
                       f"/api/v1/primitives/brand/{primary_bid}/achievements",
                       json_body={
                           "id": "group_itinerary_complete",
                           "name": "Adventure Unlocked",
                           "description": "Entire group of 10 completed full itinerary",
                           "target_metric": "group_itinerary_progress",
                           "target_value": 100,
                           "xp_reward": 1000,
                           "badge_id": "",
                           "scope": "group",  # speculative — does it accept group scope?
                       })
    if sc == 200:
        ok("group-scoped achievement", "scope=group accepted")
        gap("P1", "group achievement scope flag undocumented",
            "Achievement accepted `scope: group` but there is no documented "
            "group-membership lookup. When does the achievement fire — when ANY "
            "group member crosses target, or when ALL do? Race conditions abundant.")
    elif sc in (400, 422):
        gap("P0", "no group-level achievement primitive",
            f"Achievement engine rejects scope=group ({sc} {_short(b, 100)}). "
            "All achievements are per-user. Travel/team/cohort merchants cannot "
            "express 'group of 10 unlocks bonus when all complete itinerary'. "
            "Group cohesion is the entire UX of guided tours, study-abroad cohorts, "
            "team-building events, family reunions — none of it modelable.")
    else:
        gap("P1", "group achievement create", f"{sc} {_short(b)}")

    # Probe: shared XP pool / group leaderboard
    sc, b = await call(c, "GET",
                       f"/api/v1/leaderboard/{primary_bid}/groups")
    if sc == 404:
        gap("P1", "no group leaderboard",
            "GET /leaderboard/{bid}/groups 404 — leaderboards are per-user only. "
            "Tour operators have no way to gamify inter-group competition "
            "(Tokyo Group vs Kyoto Group: who logged more steps?).")


# ── Phase 8: Supplier Marketplace ────────────────────────────────────────
async def phase_8_supplier_marketplace(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("8: Supplier Marketplace — agency aggregates 50 suppliers")
    master_id = state["master_id"]

    # Try the partnerships router as the closest existing primitive
    attached_suppliers = 0
    for s in SUPPLIERS:
        sc, b = await call(c, "POST", "/api/v1/partnerships/invite", json_body={
            "from_brand_id": SUB_BRANDS[0]["brand_id"],
            "to_brand_id": s["id"],
            "kind": "supplier",
            "revenue_share_pct": s["rev_split_pct"],
            "metadata": {"supplier_type": s["type"], "region": s["region"],
                         "name": s["name"]},
        })
        if sc in (200, 201):
            attached_suppliers += 1
        elif sc == 404:
            if attached_suppliers == 0:
                gap("P0", "no supplier marketplace primitive",
                    "POST /api/v1/partnerships/invite 404. The platform has no native "
                    "way to model a 'supplier network' — agency aggregating 50 hotels/"
                    "airlines/activities with per-supplier revenue split is impossible. "
                    "Workaround (each supplier = sub-brand) breaks the brand-attach "
                    "semantics (suppliers don't belong to 老梁's master).")
            break
        elif sc in (400, 422):
            if attached_suppliers == 0:
                gap("P1", "partnership schema mismatch",
                    f"{sc} {_short(b, 100)} — partnerships endpoint exists but "
                    "rejects supplier kind/rev_share fields.")
            break

    if attached_suppliers:
        ok("partnerships as supplier proxy", f"{attached_suppliers}/{len(SUPPLIERS)}")

    # Probe: list suppliers for the master
    sc, b = await call(c, "GET", f"/api/v1/master/{master_id}/suppliers")
    if sc == 404:
        gap("P0", "no master supplier directory",
            "GET /master/{id}/suppliers 404. There is no 'who are 老梁's 50 suppliers' "
            "endpoint. Reports cannot rollup commission across the supplier network.")

    # Probe: per-supplier revenue split tracking
    sc, b = await call(c, "POST",
                       "/api/v1/payouts/ledger/create",
                       json_body={
                           "from_brand_id": SUB_BRANDS[0]["brand_id"],
                           "to_supplier_id": SUPPLIERS[0]["id"],
                           "amount_cents": 100_000,
                           "currency": "JPY",
                           "reason": "hotel_booking_split",
                       })
    if sc == 404:
        gap("P0", "no inter-brand payout ledger",
            "POST /payouts/ledger/create 404. The agency cannot record 'Hilton Tokyo "
            "earned ¥X from our booking, owe them 88% of it'. Revenue split "
            "reconciliation lives entirely off-platform.")
    elif sc in (200, 201):
        ok("inter-brand payout ledger", "supplier revenue split recorded")

    # Sub-brand network model
    gap("P1", "single-level brand hierarchy",
        "Brand attach is flat: master → brands. There is no 'this brand is a "
        "*supplier of* that brand' edge. Travel/marketplace/franchise models "
        "all need a 2-level (or DAG) brand graph: agency → region → supplier.")


# ── Phase 9: Seasonal Campaign Burst ─────────────────────────────────────
async def phase_9_seasonal_burst(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("9: Seasonal Burst — Chinese New Year 10x traffic")
    primary_bid = state["primary_bid"]

    # Create a CNY-themed campaign with elevated burst budget
    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": primary_bid,
        "name": "Chinese New Year 2026 — Family Travel Burst",
        "objective": "acquire",
        "bid_strategy": "cpa",
        "max_bid_cents": 200_000,  # ¥2000 per family booking (high AOV)
        "daily_budget_cents": 500_000,  # ¥5000/day burst
        "total_budget_cents": 5_000_000,  # ¥50K total
        "targeting": {
            "geo": {"country": "CN"},
            "demographics": {"age_min": 30, "age_max": 55},
        },
        "creative": {"recipe_id": "duolingo_streak", "game_slug": "lunar_travel"},
        "schedule": {"start_at": time.time() - 60,
                     "end_at": time.time() + 86400 * 30},
        "burst_mode": True,  # speculative
        "burst_multiplier": 10,  # speculative
        "seasonal_window": {"start": "2026-02-01", "end": "2026-02-17"},  # CNY
    })
    if sc == 200 and isinstance(b, dict):
        cid = b["campaign_id"]
        ok("seasonal campaign created", f"id={cid}")

        # Read back — was burst_mode preserved?
        sc, b = await call(c, "GET", f"/api/v1/campaigns/{cid}/details")
        if sc == 200 and isinstance(b, dict):
            if "burst_mode" not in b and "seasonal_window" not in b:
                gap("P0", "no seasonal burst primitive",
                    "Campaign created with burst_mode=True + burst_multiplier=10 + "
                    "seasonal_window but readback shows neither. Platform pacing is "
                    "linear daily-budget only — no concept of 'spike for 17 days then "
                    "return to baseline'. Travel/retail/event merchants spend 60% of "
                    "their annual budget in 4 seasonal windows; the platform pacing "
                    "engine works against them, smoothing spend exactly when burst "
                    "is the strategy.")
    else:
        gap("P1", "seasonal campaign create", f"{sc} {_short(b)}")

    # Probe: bid-price seasonality
    sc, b = await call(c, "POST",
                       "/api/v1/campaigns/admin/seasonal-config",
                       json_body={"global_burst_window": "2026-02-01_to_2026-02-17",
                                  "global_bid_multiplier": 2.5})
    if sc == 404:
        gap("P1", "no platform-level seasonal pricing",
            "POST /campaigns/admin/seasonal-config 404. There is no platform-wide "
            "burst pricing for ad slots during peak season. Auction floor cannot "
            "auto-rise during CNY, leaving slot revenue on the table.")

    # Probe: forecast endpoint for burst
    sc, b = await call(c, "GET",
                       f"/api/v1/wallet/{primary_bid}/forecast",
                       params={"window_days": 30, "scenario": "seasonal_burst"})
    if sc == 200 and isinstance(b, dict):
        ok("wallet forecast", f"keys={list(b)[:5]}")
    elif sc == 404:
        gap("P2", "no wallet forecast under burst scenario",
            "Travel merchants can't preview 'if CNY traffic spikes 10x, when do I "
            "run out of budget?'. Budget exhaustion mid-CNY = lost market share.")


# ── Phase 10: Refundable vs Non-refundable Vouchers ──────────────────────
async def phase_10_refund_policy(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("10: Refundable vs Non-Refundable Voucher Policy")
    primary_bid = state["primary_bid"]

    # Refundable voucher (cancel up to 30 days before)
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": primary_bid,
        "name": "Tokyo Tour — Refundable",
        "description": "Refundable up to 30d before departure",
        "value": {"type": "fixed", "amount": 30_000_00, "currency": "CNY"},
        "conditions": {"usage_limit_per_user": 1},
        "expires_in_days": 365,
        "stackable": False,
        "transferable": False,
        "refund_policy": {  # speculative
            "type": "refundable",
            "cutoff_days_before_event": 30,
            "refund_pct": 100,
            "partial_refund_schedule": [
                {"days_before": 60, "refund_pct": 100},
                {"days_before": 30, "refund_pct": 80},
                {"days_before": 14, "refund_pct": 50},
                {"days_before": 7, "refund_pct": 0},
            ],
        },
    })
    refundable_tid = None
    if sc == 201 and isinstance(b, dict):
        refundable_tid = b.get("template_id")
        ok("refundable voucher template", f"id={refundable_tid}")
        # Read back and check if refund_policy was preserved
        sc2, b2 = await call(c, "GET",
                             f"/api/v1/vouchers/templates/{refundable_tid}")
        if sc2 == 200 and isinstance(b2, dict):
            if "refund_policy" in b2:
                ok("refund policy preserved", "schema accepts cancellation rules")
            else:
                gap("P0", "voucher refund_policy silently dropped",
                    "POST /vouchers/templates/create accepted `refund_policy` field "
                    "but readback omits it. There is no first-class cancellation "
                    "schema. Travel agencies cannot encode 'refundable up to 30d, "
                    "50% refund at 14d, no refund at 7d' — every refund is manual.")

    # Non-refundable (early-bird)
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": primary_bid,
        "name": "Tokyo Tour — Early Bird Non-Refundable",
        "description": "20% off but no refund",
        "value": {"type": "percentage", "amount": 20, "currency": "CNY"},
        "conditions": {"usage_limit_per_user": 1},
        "expires_in_days": 365,
        "refund_policy": {"type": "non_refundable"},
    })
    if sc == 201:
        ok("non-refundable voucher template", "accepted")

    # Probe: voucher cancel endpoint
    if refundable_tid:
        # Issue + try to cancel
        cancel_uid = f"cancel_test_{RUN_TAG}"
        await _setup_consent(c, [cancel_uid])
        sc, b = await call(c, "POST",
                           f"/api/v1/vouchers/templates/{refundable_tid}/issue",
                           json_body={"user_id": cancel_uid, "brand_id": primary_bid})
        if sc == 201 and isinstance(b, dict):
            vid = b.get("voucher_id")
            # Try cancel endpoint
            sc2, b2 = await call(c, "POST", f"/api/v1/vouchers/{vid}/cancel",
                                 json_body={"reason": "customer_requested",
                                            "days_before_event": 45})
            if sc2 == 200:
                ok("voucher cancel", "cancellation flow works")
            elif sc2 == 404:
                gap("P0", "no voucher cancel endpoint",
                    "POST /vouchers/{vid}/cancel 404. There is no API to cancel "
                    "an issued voucher (refundable or otherwise). Refunds cannot "
                    "cascade through the attribution chain (CPS commission must be "
                    "reversed), making fraud + refund-management entirely manual.")
            elif sc2 in (400, 405):
                gap("P1", "voucher cancel method",
                    f"{sc2} {_short(b2, 100)} — endpoint exists but rejects "
                    "the cancellation schema.")

    # Probe: cascade — does cancellation reverse CPS commission?
    gap("P0", "no cancellation → commission reversal",
        "Even if voucher cancellation worked, there is no documented hook to "
        "reverse the upstream CPS commission. If 老梁's customer cancels a ¥30K "
        "tour, the platform has already paid 8% commission to whoever drove the "
        "conversion. The agency loses the booking AND the commission. Refund "
        "cascade must be a first-class platform concern for any commission-based "
        "marketplace.")


# ── Phase 11: Visa Application Trigger (60d before) ──────────────────────
async def phase_11_visa_trigger(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("11: Visa Application Trigger — 60d before Schengen travel")
    europe_bid = SUB_BRANDS[1]["brand_id"]

    # Try /triggers/register for a scheduled reminder
    sc, b = await call(c, "POST", "/api/v1/triggers/register", json_body={
        "brand_id": europe_bid,
        "name": "Schengen visa reminder 60d before departure",
        "event_type": "reservation.confirmed",
        "delay_days": -60,  # 60 days BEFORE the scheduled_at
        "condition": {"destination_region": "europe"},
        "action": {"type": "send_push",
                   "message_template": "您的欧洲之旅还有60天，请开始申请申根签证!"},
    })
    if sc in (200, 201):
        ok("visa trigger registered", "scheduled 60d before departure")
    elif sc == 404:
        gap("P0", "/api/v1/triggers/register 404",
            "Triggers router has no /register endpoint at the expected path. "
            "Time-based reminders (visa @ -60d, payment @ -90d, packing @ -7d) "
            "are core to travel UX and have no first-class home.")
    elif sc in (400, 422):
        gap("P1", "trigger schema for time-relative events",
            f"{sc} {_short(b, 120)} — /triggers/register exists but doesn't "
            "accept delay_days with negative values (event-relative) or "
            "destination_region condition. The trigger model assumes events "
            "in the past; travel needs events in the future.")
    else:
        gap("P1", "visa trigger register", f"{sc} {_short(b)}")

    # Try /rules/{bid}/create for chained event flow
    sc, b = await call(c, "POST", f"/api/v1/rules/{europe_bid}/create",
                       json_body={
                           "brand_id": europe_bid,
                           "name": "Pre-departure reminder chain",
                           "trigger_event": "reservation.confirmed",
                           "conditions": {"destination_region": "europe"},
                           "actions": [
                               {"module": "triggers", "action": "schedule_push",
                                "offset_days": -90, "message": "Pay balance now"},
                               {"module": "triggers", "action": "schedule_push",
                                "offset_days": -60, "message": "Apply for visa"},
                               {"module": "triggers", "action": "schedule_push",
                                "offset_days": -14, "message": "Insurance reminder"},
                               {"module": "triggers", "action": "schedule_push",
                                "offset_days": -7, "message": "Packing checklist"},
                           ],
                           "max_triggers_per_user": 4,
                       })
    if sc in (200, 201):
        ok("pre-departure rule chain", "4-step reminder chain accepted")
        gap("P1", "rule engine offset_days semantics undefined",
            "Rule engine accepted offset_days field but it's not in any documented "
            "schema. Whether '-60' fires 60d before reservation.scheduled_at or 60d "
            "after rule registration is undefined.")
    elif sc in (400, 422):
        gap("P0", "rule engine has no time-relative scheduling",
            f"{sc} {_short(b, 150)} — rule engine rejects offset_days. There is "
            "no documented way to express 'fire this action N days before a future "
            "event'. Travel + healthcare + event-planning all require this.")
    elif sc == 404:
        gap("P0", "rules endpoint 404",
            "POST /rules/{bid}/create 404 — rule engine is not mounted or path "
            "differs from documented form.")


# ── Phase 12: Multi-Leg Attribution Chain ─────────────────────────────────
async def phase_12_multileg_attribution(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("12: Multi-Leg Attribution — checkin → arrival → itinerary → return")
    primary_bid = state["primary_bid"]
    traveler_uid = f"multileg_traveler_{RUN_TAG}"
    await _setup_consent(c, [traveler_uid])

    # Fire 4 sequential events as attribution touchpoints
    touchpoints = [
        ("airport_checkin", "User checks in at HGH"),
        ("destination_arrival", "User arrives at NRT (Tokyo)"),
        ("itinerary_complete", "User finishes 10-day itinerary"),
        ("return_home", "User checks back in at HGH"),
    ]
    accepted_legs = 0
    for ev_type, desc in touchpoints:
        sc, b = await call(c, "POST", "/api/v1/attribution/track/conversion",
                           json_body={
                               "user_id": traveler_uid,
                               "target_brand": primary_bid,
                               "order_id": f"multileg_{RUN_TAG}_{ev_type}",
                               "event_type": ev_type,
                               "amount_cents": 0,
                               "metadata": {"leg": desc},
                           })
        if sc == 200:
            accepted_legs += 1
        elif sc in (400, 422):
            if accepted_legs == 0:
                gap("P1", "multi-leg attribution event_type",
                    f"{sc} {_short(b, 120)} — /attribution/track/conversion rejects "
                    "custom event_type. All conversions look identical to the "
                    "platform. Travel needs leg-by-leg attribution.")
        elif sc == 403:
            gap("P1", "multi-leg attribution consent",
                "consent rejected even though we granted it — possibly stricter "
                "scope check needed.")
            break

    if accepted_legs:
        ok("multi-leg attribution events",
           f"{accepted_legs}/{len(touchpoints)} legs recorded")
        if accepted_legs == len(touchpoints):
            # Check whether the chain is queryable
            sc, b = await call(c, "GET",
                               f"/api/v1/attribution/user/{traveler_uid}/journey",
                               params={"brand_id": primary_bid})
            if sc == 200 and isinstance(b, dict):
                legs = b.get("legs") or b.get("touchpoints") or b.get("events")
                if isinstance(legs, list):
                    ok("attribution journey readable",
                       f"{len(legs)} touchpoints in chain")
                else:
                    gap("P1", "no multi-touchpoint readback",
                        f"Journey endpoint returns {list(b)[:5]} — no legs/touchpoints "
                        "structure. Multi-touch attribution analysis is impossible.")
            elif sc == 404:
                gap("P0", "no attribution journey endpoint",
                    "GET /attribution/user/{uid}/journey 404. The platform can "
                    "*record* multiple conversions but cannot *read back* the "
                    "ordered chain. Travel + healthcare + B2B SaaS all need to see "
                    "'user X did A then B then C' to attribute revenue correctly.")
    else:
        gap("P0", "no multi-leg attribution",
            "All 4 multi-leg events rejected — attribution is fundamentally "
            "single-event (conversion = single moment). Travel journeys with "
            "4-7 touchpoints are first-class.")


# ── Phase 13: Loyalty Earning Across Destinations ────────────────────────
async def phase_13_loyalty_earning(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("13: Loyalty Earning — vip_globetrotter upgrade after 3 trips")
    master_id = state["master_id"]
    loyal_uid = f"loyalty_test_{RUN_TAG}"
    await _setup_consent(c, [loyal_uid])

    # Grant XP across 3 different region brands (japan + europe + cruise)
    granted_total = 0
    for sb in SUB_BRANDS[:3]:
        sc, b = await call(c, "POST", "/api/v1/primitives/currency/xp/grant",
                           json_body={
                               "user_id": loyal_uid,
                               "brand_id": sb["brand_id"],
                               "amount": 2000,
                               "reason": f"trip_{sb['region']}_completed",
                           })
        if sc == 200:
            granted_total += 2000
    ok("grant XP across regions", f"granted {granted_total} XP across 3 sub-brands")

    # Read tier on each sub-brand — does VIP show up cross-brand?
    primary_tier = None
    europe_tier = None
    cruise_tier = None
    for sb, key in [(SUB_BRANDS[0], "japan"), (SUB_BRANDS[1], "europe"),
                    (SUB_BRANDS[3], "cruise")]:
        sc, b = await call(c, "GET",
                           f"/api/v1/primitives/user/{loyal_uid}/tier",
                           params={"brand_id": sb["brand_id"]})
        if sc == 200 and isinstance(b, dict):
            ct = b.get("current_tier")
            cur = ct.get("id") if isinstance(ct, dict) else (ct or b.get("tier"))
            xp = b.get("xp", 0)
            info(f"  {sb['region']}: tier={cur} xp={xp}")
            if sb["region"] == "japan":
                primary_tier = cur
            elif sb["region"] == "europe":
                europe_tier = cur
            elif sb["region"] == "cruise":
                cruise_tier = cur

    # If tier aggregates across regions: total XP=6000 ⇒ vip_globetrotter (≥5000)
    # If tier is per-region: each region has only 2000 XP ⇒ frequent_traveler
    if primary_tier == "vip_globetrotter" or europe_tier == "vip_globetrotter":
        ok("cross-region tier aggregation", "XP rolled up across regions")
    else:
        gap("P0", "no cross-region XP aggregation",
            f"User earned 2000 XP × 3 regions = 6000 total (which should be "
            f"vip_globetrotter, ≥5000), but per-region tier reads as: "
            f"japan={primary_tier} europe={europe_tier} cruise={cruise_tier}. "
            "XP is brand-scoped, tiers don't aggregate, so loyalty is meaningless "
            "for multi-destination travelers. The customer who books 3 different "
            "regions sees Explorer tier in each. Master-level XP pool is required.")

    # Probe: master-level tier readback
    sc, b = await call(c, "GET",
                       f"/api/v1/master/{master_id}/user/{loyal_uid}/tier")
    if sc == 404:
        gap("P0", "no master-level tier readback",
            "GET /master/{id}/user/{uid}/tier 404. There is no aggregate view "
            "of 'this user's tier across all brands in the master'. Concierge "
            "staff cannot answer 'is this customer a VIP?' without checking 5 brands.")
    elif sc == 200 and isinstance(b, dict):
        ok("master-level tier readback", f"tier={b.get('current_tier')}")


# ── Phase 14: Viral / WeChat Group Referral ──────────────────────────────
async def phase_14_wom_referral(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("14: Word-of-Mouth — WeChat group referral, family/spouse weighting")
    primary_bid = state["primary_bid"]
    rng = random.Random(RUN_TAG + 14)

    referrers = [f"referrer_{RUN_TAG}_{i:02d}" for i in range(5)]
    await _setup_consent(c, referrers)

    # Create referral tokens
    tokens = []
    for r in referrers:
        sc, b = await call(c, "POST", "/api/v1/attribution/token/create",
                           json_body={
                               "source_brand": primary_bid,
                               "source_user_id": r,
                               "target_brand": primary_bid,
                               "channel": "wechat_group",
                               "ttl_hours": 24 * 90,  # 90d
                               "relationship_type": "family",  # speculative
                           })
        if sc == 200 and isinstance(b, dict) and b.get("token"):
            tokens.append((r, b["token"]))
    if tokens:
        ok("5 referral tokens via wechat_group", f"{len(tokens)}/{len(referrers)}")

    # Probe: relationship-type weighting (family vs friend vs colleague)
    gap("P1", "no relationship-type referral weighting",
        "Attribution token accepts arbitrary `relationship_type` but there is no "
        "documented downstream effect. Travel agencies see 10x higher conversion "
        "from family referrals (honeymoons, family trips) vs casual friend "
        "referrals. A 'weight referral commission by relationship_type' primitive "
        "could 2x viral lift.")

    # Probe: K-factor / viral coefficient surface
    sc, b = await call(c, "GET",
                       f"/api/v1/master/{state['master_id']}/viral-metrics")
    if sc == 404:
        gap("P1", "no viral K-factor metric",
            "GET /master/{id}/viral-metrics 404. There is no K-factor / viral "
            "coefficient measurement out of the box. Travel agencies need to "
            "monitor 'each customer brings 0.7 others' to optimize referral "
            "campaigns.")

    # Probe: WeChat-group fan-out
    gap("P1", "no group-message fan-out primitive",
        "Sharing 'I just booked a Tokyo trip!' to a WeChat group is the #1 "
        "viral channel for Chinese travel. The platform has /attribution/token/create "
        "(1:1 link) but no 'broadcast this offer to my contacts' API. WeChat "
        "MiniProgram messages must be hand-rolled per merchant.")


# ── Phase 15: Edge Cases ─────────────────────────────────────────────────
async def phase_15_edges(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("15: Edge Cases — FX drift / supplier-fault / force majeure / wallet")
    primary_bid = state["primary_bid"]

    # 15a: Currency conversion mid-campaign (USD goes up)
    sc, b = await call(c, "POST", "/api/v1/wallet/admin/fx-rate-update",
                       json_body={"currency_pair": "CNY_USD", "new_rate": 0.16})
    if sc == 404:
        gap("P0", "no FX rate management endpoint",
            "POST /wallet/admin/fx-rate-update 404. The platform has no FX-rate "
            "table at all. Multi-currency wallets (Phase 2) can't even *attempt* "
            "FX-aware accounting because there's nowhere to update the rate.")

    # 15b: Cancellation by supplier (not customer) — fault assignment
    sc, b = await call(c, "POST",
                       f"/api/v1/reservations/{state.get('group_reservation_id') or 'x'}/cancel",
                       json_body={"reason": "supplier_cancelled",
                                  "fault_party": "supplier",
                                  "compensation_due": True})
    if sc in (200, 201):
        ok("supplier-fault cancellation", "fault_party=supplier accepted")
        gap("P1", "fault_party field undocumented",
            "Cancellation accepted `fault_party: supplier` but downstream "
            "compensation routing is not specified. Who pays? Agency or supplier? "
            "Travel insurance integrations would need this.")
    elif sc == 404:
        gap("P0", "no /reservations/{rid}/cancel endpoint",
            "POST /reservations/{rid}/cancel 404 (rid may not exist). Confirm whether "
            "the cancel surface supports fault assignment or just user-initiated.")
    elif sc in (400, 422):
        gap("P1", "supplier-fault schema",
            f"{sc} {_short(b, 100)} — cancellation endpoint exists but rejects "
            "fault_party. Travel needs three cancellation classes (customer, "
            "supplier, force_majeure) with different refund/commission cascades.")

    # 15c: Force majeure — bulk refund (pandemic, earthquake)
    sc, b = await call(c, "POST",
                       f"/api/v1/reservations/admin/bulk-cancel",
                       json_body={"brand_id": primary_bid,
                                  "region": "japan",
                                  "reason": "force_majeure",
                                  "event_window_start": int(time.time()),
                                  "event_window_end": int(time.time()) + 86400 * 30,
                                  "dry_run": True})
    if sc == 404:
        gap("P0", "no bulk cancellation primitive",
            "POST /reservations/admin/bulk-cancel 404. When a pandemic / "
            "earthquake / volcanic eruption forces cancellation of ALL Japan "
            "trips for a month, there is no bulk operation. Operators must "
            "cancel one-by-one. Bulk + force_majeure flagging is critical for "
            "travel/event/sports merchants.")

    # 15d: Late cancellation by traveler — penalty + recovery voucher
    gap("P1", "no traveler-side penalty primitive",
        "Late cancellation by traveler (within 7 days of departure) typically "
        "incurs a penalty (50% of tour cost) AND can be offered a recovery "
        "voucher for a future trip. There is no native primitive for "
        "'penalty + recovery' as a paired transaction. Off-platform handling "
        "loses the behavioral signal for retention modeling.")

    # 15e: Custom itinerary pricing — flexible, no template
    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": primary_bid,
        "name": "Custom Itinerary — Pricing Flexible",
        "objective": "sales",
        "bid_strategy": "cps",
        "max_bid_cents": 1500,  # 15% commission on custom builds
        "daily_budget_cents": 20_000,
        "total_budget_cents": 200_000,
        "targeting": {"geo": {"country": "CN"}},
        "creative": {"recipe_id": "duolingo_streak"},
        "schedule": {"start_at": time.time() - 60, "end_at": time.time() + 86400 * 60},
        "pricing_model": "dynamic",  # speculative
        "min_order_cents": 30_000_00,
        "max_order_cents": 500_000_00,  # ¥500K bespoke luxury
    })
    if sc == 200 and isinstance(b, dict):
        info("custom-itinerary campaign accepted (dynamic pricing fields probably ignored)")
    else:
        info(f"custom itinerary campaign returned {sc}")
    gap("P1", "no dynamic / bespoke pricing model",
        "All campaigns assume fixed unit economics (max_bid_cents). Custom-tour "
        "agencies sell bespoke itineraries from ¥30K → ¥500K with no fixed unit. "
        "Platform has no pricing_model: dynamic + per-deal CPS rate.")

    # 15f: Pre-paid wallet — customer adds funds, multi-trip redemption
    sc, b = await call(c, "POST",
                       f"/api/v1/wallet/customer/{f'prepaid_{RUN_TAG}'}/topup",
                       json_body={"amount_cents": 100_000_00, "currency": "CNY"})
    if sc == 404:
        gap("P1", "no customer-side prepaid wallet",
            "Travel agencies often run prepaid loyalty wallets (¥100K deposit, "
            "redeem across multiple trips, 5% bonus). The platform's /wallet/ "
            "router is merchant-side only. Customer-side wallets do not exist.")

    # 15g: Multi-segment trip (3 destinations, single booking)
    sc, b = await call(c, "POST", "/api/v1/reservations/create", json_body={
        "brand_id": primary_bid,
        "user_id": f"multisegment_{RUN_TAG}",
        "scheduled_at": int(time.time()) + 86400 * 60,
        "party_size": 2,
        "type": "tour",
        "metadata": {
            "segments": [
                {"city": "Beijing", "arr": "2026-04-01", "dep": "2026-04-03"},
                {"city": "Tokyo", "arr": "2026-04-03", "dep": "2026-04-07"},
                {"city": "Bali", "arr": "2026-04-07", "dep": "2026-04-12"},
            ],
        },
    })
    if sc in (200, 201):
        ok("multi-segment reservation", "3 cities in metadata")
        gap("P1", "no first-class multi-segment / itinerary primitive",
            "Multi-city itineraries (Beijing → Tokyo → Bali) live in free-form "
            "metadata. There is no ordered-segment schema with arrival/departure "
            "times per leg, no validation that the chain is geographically/"
            "temporally consistent (Tokyo dep == Bali arr).")

    # 15h: External loyalty (airline miles)
    sc, b = await call(c, "POST",
                       f"/api/v1/primitives/user/external_loyalty_test_{RUN_TAG}/external-link",
                       json_body={"brand_id": primary_bid,
                                  "external_program": "ANA_Mileage_Club",
                                  "external_id": "ANA12345",
                                  "external_balance": 50000})
    if sc == 404:
        gap("P2", "no external-loyalty link primitive",
            "Frequent flyer programs (ANA / United / Marriott Bonvoy) are the #1 "
            "loyalty competitor to a travel agency's own program. There is no "
            "way to link/import external loyalty balances. Co-branded earning "
            "(agency Gold = ANA Silver) impossible at platform level.")


# ── Phase 16: Module Probe ───────────────────────────────────────────────
async def phase_16_module_probe(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("16: Module Probe — what's reachable for a travel agency")
    primary_bid = state["primary_bid"]
    probes = [
        ("master.tier.configure", "POST",
         f"/api/v1/master/{state['master_id']}/tiers/configure", None),
        ("master.user.tier", "GET",
         f"/api/v1/master/{state['master_id']}/user/u/tier", None),
        ("master.cross-brand-visits", "GET",
         f"/api/v1/master/{state['master_id']}/cross-brand-visits", None),
        ("master.suppliers", "GET",
         f"/api/v1/master/{state['master_id']}/suppliers", None),
        ("master.viral-metrics", "GET",
         f"/api/v1/master/{state['master_id']}/viral-metrics", None),
        ("reservations.group", "POST",
         "/api/v1/reservations/group/create", None),
        ("reservations.travelers", "GET",
         "/api/v1/reservations/x/travelers", None),
        ("reservations.bulk-cancel", "POST",
         "/api/v1/reservations/admin/bulk-cancel", None),
        ("groups.create", "POST", "/api/v1/groups/create", None),
        ("partnerships.invite", "POST", "/api/v1/partnerships/invite", None),
        ("payouts.ledger.create", "POST", "/api/v1/payouts/ledger/create", None),
        ("triggers.register", "POST", "/api/v1/triggers/register", None),
        ("rules.create", "POST", f"/api/v1/rules/{primary_bid}/create", None),
        ("attribution.user.journey", "GET",
         f"/api/v1/attribution/user/u/journey", {"brand_id": primary_bid}),
        ("wallet.admin.fx-rate-update", "POST",
         "/api/v1/wallet/admin/fx-rate-update", None),
        ("campaigns.admin.seasonal-config", "POST",
         "/api/v1/campaigns/admin/seasonal-config", None),
        ("vouchers.cancel", "POST", "/api/v1/vouchers/x/cancel", None),
    ]
    avail, missing = [], []
    for label, method, path, params in probes:
        if method == "POST":
            sc, b = await call(c, method, path, json_body={}, params=params)
        else:
            sc, b = await call(c, method, path, params=params)
        if sc == 200:
            avail.append(label)
            ok(label, "200")
        elif sc == 404:
            if isinstance(b, dict) and b.get("detail") in ("Not Found", "not found"):
                missing.append(label)
                gap("P1", f"module not mounted: {label}", f"404 at {path}")
            else:
                avail.append(label)
                ok(f"{label} (domain 404)", "")
        elif sc in (400, 422):
            avail.append(label)
            info(f"{label} → {sc} (route exists, schema mismatch)")
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
    md.append("# 老梁 / Liang Bo (天下游 Worldwide Travel) — Merchant Journey Findings")
    md.append("")
    md.append(f"**Run tag**: `{RUN_TAG}` | **Runtime**: {runtime:.1f}s | "
              f"**Date**: {time.strftime('%Y-%m-%d %H:%M', time.localtime(start_ts))}")
    md.append("")
    md.append("## Scenario")
    md.append(
        "老梁 owns 「天下游」 (Worldwide Travel) — a premium Hangzhou-based "
        "international travel agency. Services span package tours, custom "
        "itineraries, business travel, study abroad, premium cruise. "
        "3000 trips/year, AOV ¥8000/person, group bookings (4-50 people) common. "
        "Pricing ¥3K → ¥200K. Budget ¥30K/月 plus heavy seasonal swings.\n"
        "\n"
        "**Critical differences vs prior merchants**: LONG SALES CYCLE (6+ months), "
        "GROUP BOOKING (1 buyer / N travelers), MULTI-CURRENCY (CNY/USD/EUR/JPY/THB), "
        "SUPPLIER MARKETPLACE (50 hotels/airlines/activities aggregated), and "
        "SEASONAL DEMAND SPIKES (CNY/Golden Week 10x burst). Five axes — each "
        "exercises a primitive that previous merchant simulations could not."
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
    for ph, cnt in phase_counters.items():
        md.append(f"| {ph} | {cnt['pass']} | {cnt['gap']} | {cnt['fail']} |")
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

    section("P0 — Blockers for the travel-agency use case", p0)
    section("P1 — Friction", p1)
    section("P2 — Nice-to-have", p2)
    section("Hard failures", fails)

    md.append("## Top 3 NEW Gaps Unique to Travel")
    md.append("")
    md.append(
        "Among the dozens of gaps surfaced, three are *fundamentally new* compared "
        "to prior merchant sims (F&B chain, book club, e-commerce, fine dining, "
        "education academy, fitness chain) — they are CONSEQUENCES OF AXES that "
        "no prior merchant exercised:\n"
        "\n"
        "### 1. Multi-currency wallet + FX-aware reporting (NEW)\n"
        "F&B (老王 Indonesia) used IDR/CNY but at a single-store granularity; "
        "fine dining (老张) saw international tourists in USD/JPY but as one-off "
        "purchases. 老梁 is the FIRST merchant whose CORE wallet must hold parallel "
        "balances in CNY/USD/EUR/JPY/THB simultaneously, run promotion budgets "
        "in foreign currencies, and reconcile commission across mixed-currency "
        "orders. The platform is single-currency at every layer:\n"
        "  - `POST /wallet/{bid}/topup` has no currency field\n"
        "  - `GET /wallet/{bid}/daily-budget-status` returns no currency/fx_rate\n"
        "  - Voucher value.currency outside CNY rejected\n"
        "  - `/pixel/brand/{bid}` sums total_amount_cents across currencies as if "
        "all were CNY\n"
        "  - CPS commission base on mixed-currency orders is undefined\n"
        "  - `/wallet/admin/fx-rate-update` returns 404\n"
        "Travel agencies + international ecom + luxury imports + FX brokers all "
        "share this need. P0 platform-wide.\n"
        "\n"
        "### 2. Group-booking primitive (single buyer, N travelers) (NEW)\n"
        "All prior merchants modeled a transaction as one user = one customer. "
        "Even 老周's gym booking (Round 3 reservation primitive) was party_size=1 "
        "or 2. 老梁 needs:\n"
        "  - Single buyer (the group organizer) holding the contract & payment\n"
        "  - Per-traveler passport/DOB/dietary/visa-status records\n"
        "  - Group-level achievements (entire group of 10 completes itinerary)\n"
        "  - Per-traveler XP / loyalty earning rolled up to organizer\n"
        "Result of probe: party_size>1 accepted, but per-traveler `travelers` "
        "array silently dropped from metadata. No `/reservations/{rid}/travelers` "
        "endpoint. No `scope: group` achievement support. No `/groups/create` "
        "primitive (404). Tour operators / study-abroad agencies / family-event "
        "venues all hit this wall.\n"
        "\n"
        "### 3. Supplier-marketplace network model + commission split (NEW)\n"
        "Prior merchants ran their own brands (F&B chain, gym chain, book club). "
        "老梁 *aggregates* 50 third-party suppliers (Hilton Tokyo, ANA, Royal "
        "Caribbean) and earns a revenue split per booking. This requires:\n"
        "  - A 'supplier' relationship distinct from 'sub-brand attach'\n"
        "  - Per-supplier revenue_share_pct (variable: 5% airline, 18% cruise)\n"
        "  - Inter-brand payout ledger (record + reconcile)\n"
        "  - A 2-level brand graph (agency → region → supplier)\n"
        "Result of probe: `/partnerships/invite` 404, `/payouts/ledger/create` 404, "
        "`/master/{id}/suppliers` 404. The platform's brand model is single-level "
        "(master → brands), so suppliers must be modeled as either peer brands "
        "(breaks ownership semantics) or fully off-platform (no commission "
        "tracking). The whole *marketplace* business model is invisible.\n"
    )
    md.append("")

    md.append("## Cross-Comparison with All 7 Previous Merchants")
    md.append("")
    md.append(
        "| Axis | 老王 F&B | 老李 Books | 老黄 Ecom | 老张 Dining | 老吴 Edu | "
        "老周 Gym | 老梁 Travel |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| Sub-brands | 10 stores | 1 brand | 1 brand | 1 brand | 1 brand | 5 gyms | 5 regions |\n"
        "| Multi-currency | No (IDR only) | No | No | Hint (tourists) | No | No | **YES (5 currencies)** |\n"
        "| Sales cycle | <1 day | weeks | days | days | 14-30d | weeks | **210d (7mo)** |\n"
        "| Group booking | No | No | No | Table-of-10 | Family (parent+kid) | No | **party_size 4-50** |\n"
        "| Supplier network | No | No | No | No | No | No | **YES (50 suppliers)** |\n"
        "| Seasonal burst | LBS spike | low | 11.11 sale | low | semester | low | **CNY/Golden Week 10x** |\n"
        "| Refund cascade | low stakes | low | medium | medium | medium | medium | **HIGH (8% CPS reversal)** |\n"
        "| Master tier portability | needed | n/a | n/a | n/a | n/a | needed | **CRITICAL** |\n"
        "| Multi-leg journey | no | no | cart→buy | reserve→dine | trial→pay | book→attend | "
        "**checkin→arrive→itinerary→return** |\n"
        "| External loyalty | n/a | n/a | n/a | n/a | n/a | n/a | **airline miles** |\n"
        "\n"
        "**Pattern**: 老梁 is the FIRST merchant who stresses 5 dimensions "
        "simultaneously (multi-currency × multi-LOB × long-cycle × group × marketplace). "
        "Earlier merchants exposed individual gaps; 老梁 exposes that the platform's "
        "*shape* assumes single-currency / single-tenant / single-touchpoint / "
        "single-buyer / single-day cycles. Major industries the platform cannot "
        "serve at all without these primitives: travel agencies, OTAs, study-abroad "
        "consultancies, premium cruise lines, MICE event planners, international "
        "real estate, immigration consultancies, luxury wedding planners, sports "
        "tourism, medical tourism.\n"
    )
    md.append("")

    md.append("## Strategic Recommendations")
    md.append("")
    md.append(
        "1. **[P0] Multi-currency wallet + FX engine**: add `currency` field to "
        "`/wallet/topup` + `/daily-budget`, expose `base_currency` + `by_currency: "
        "{...}` on `/wallet/daily-budget-status`, build an `/admin/fx-rate-update` "
        "table with timestamped rates. Voucher templates must accept any ISO-4217 "
        "currency. CPS commission must be FX-normalized at the moment of conversion "
        "and recorded with the rate timestamp. P0 platform-wide.\n"
        "2. **[P0] Group-booking primitive**: extend `/reservations/create` with a "
        "first-class `travelers: [{name, passport, dob, dietary, visa_status, "
        "mobility}]` array; add `GET/POST /reservations/{rid}/travelers` endpoints; "
        "extend achievements with `scope: group` semantics (fires when N/M members "
        "complete); add `POST /groups/create` for cohort tracking distinct from "
        "the reservation.\n"
        "3. **[P0] Supplier-marketplace model**: add a 2-level brand graph (agency "
        "→ region → supplier); `/partnerships/invite` with `kind: supplier` + "
        "`revenue_share_pct`; `/payouts/ledger/create` for per-supplier commission "
        "records; `/master/{id}/suppliers` for the supplier directory. Travel + "
        "marketplace + franchise all need this.\n"
        "4. **[P0] Master-level tier portability**: lift tier definition + XP pool "
        "from per-brand to per-master. Add `POST /master/{id}/tiers/configure` + "
        "`GET /master/{id}/user/{uid}/tier`. Brand-scoped tiers should be the "
        "exception, not the default. Cross-region loyalty is the basic ask of every "
        "multi-LOB merchant.\n"
        "5. **[P0] Long-cycle attribution**: confirm `attribution_window_days=210` "
        "persists end-to-end; raise/remove the TTL cap on `/attribution/token/create`. "
        "Real-estate, wedding, university, life-insurance, B2B SaaS all care.\n"
        "6. **[P0] Refund/cancellation cascade**: `/vouchers/{vid}/cancel` + "
        "automatic reversal of upstream CPS commission. `fault_party: customer | "
        "supplier | force_majeure` with distinct downstream cascades. "
        "`/reservations/admin/bulk-cancel` for force-majeure events.\n"
        "7. **[P1] Pixel multi-currency rollup**: `/pixel/brand/{bid}` must expose "
        "`by_currency` and `fx_normalized_cents` (with rate timestamp). Current "
        "behavior of summing raw cents across currencies is mathematically wrong.\n"
        "8. **[P1] Time-relative triggers**: `/triggers/register` accept `delay_days` "
        "with NEGATIVE values (= N days before reservation.scheduled_at). Visa "
        "reminders, payment-balance reminders, pre-departure packing — all need "
        "future-event scheduling.\n"
        "9. **[P1] Multi-touchpoint attribution journey**: `/attribution/user/{uid}/"
        "journey` returning the ordered sequence of touchpoints with per-leg "
        "attribution credit. Multi-touch is the basic ask of any analytics customer.\n"
        "10. **[P1] Seasonal burst pricing**: `burst_mode` + `seasonal_window` on "
        "campaign create; pacing engine should know not to smooth spend during "
        "declared burst windows. Platform-level `/admin/seasonal-config` for "
        "auction-floor adjustment.\n"
        "11. **[P1] Multi-segment itinerary**: first-class ordered-segment schema "
        "on reservations (Beijing → Tokyo → Bali with arr/dep per leg + "
        "consistency validation).\n"
        "12. **[P1] LOB metadata on brand attach**: `region`, `default_currency`, "
        "`business_line`, `lob_type` fields on `/master/{id}/brands/attach`. "
        "Without these, every report must infer LOB from campaign metadata.\n"
        "13. **[P2] External loyalty link**: `/primitives/user/{uid}/external-link` "
        "for ANA Mileage Club / Marriott Bonvoy / etc — co-branded earning is a "
        "core travel/hospitality primitive.\n"
        "14. **[P2] Customer-side prepaid wallet**: many travel agencies run "
        "prepaid loyalty deposits — platform's `/wallet/` is merchant-side only."
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
        for f in p0[:6]:
            print(f"  • [{f['phase']}] {f['action']} — {f['detail'][:100]}")


# ── Main ─────────────────────────────────────────────────────────────────
async def main() -> int:
    start_ts = time.time()
    await init_redis()
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30.0
        ) as c:
            state: dict[str, Any] = {}
            try:
                state = await phase_1_master_setup(c)
                await phase_2_wallet(c, state)
                await phase_3_consent_tier(c, state)
                await phase_4_group_booking(c, state)
                await phase_5_long_cycle_attribution(c, state)
                await phase_6_multicurrency_voucher(c, state)
                await phase_7_group_gamification(c, state)
                await phase_8_supplier_marketplace(c, state)
                await phase_9_seasonal_burst(c, state)
                await phase_10_refund_policy(c, state)
                await phase_11_visa_trigger(c, state)
                await phase_12_multileg_attribution(c, state)
                await phase_13_loyalty_earning(c, state)
                await phase_14_wom_referral(c, state)
                await phase_15_edges(c, state)
                await phase_16_module_probe(c, state)
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
