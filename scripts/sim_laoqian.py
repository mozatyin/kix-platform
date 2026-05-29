"""Merchant journey simulation — 老钱 / Qian Yu (明亮发型 Bright Hair, 8 salons Hangzhou).

End-to-end probe of the KiX Ads Platform from the perspective of a Hangzhou
HAIR SALON chain with STYLIST MARKETPLACE + 4-6 WEEK CYCLES + PHOTO HISTORY +
COMMISSION SPLIT at its core. Walks through:

  1. Master + 8 salon branches (西湖/拱墅/上城/下城/江干/滨江/萧山/余杭)
  2. Wallet ¥15K + cascade to 8 salons
  3. Consent flow + tier setup (stylist marketplace)
  4. Reservation primitive — resource_id = stylist_id (老周/老蔡 gap)
  5. Recipe — hair_recurring_cut + buddy referral
  6. Customer-stylist attachment ("my regular stylist") + style preference
  7. Audience — new_users_only + lapsed (>5 weeks no visit)
  8. Renewal-cycle campaign (4-week haircut, 8-week color)
  9. Geofence personalization + push engine (interpolate {stylist_name})
 10. Photo before/after attribute storage + time-series
 11. Stylist commission split + payout split
 12. Cross-salon visits (stylist follow, customer follows stylist)

Pattern follows scripts/sim_laozhou.py (fitness — similar booking + cycle).

In-process via httpx.ASGITransport so no separate server is needed. Requires
a live local Redis.

Run:
    .venv/bin/python scripts/sim_laoqian.py
"""
from __future__ import annotations

import asyncio
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
OWNER_USER_ID = f"laoqian_{RUN_TAG}"
FINDINGS_PATH = Path("/Users/mozat/a-docs/laoqian-sim-findings.md")

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"

# 8 Bright Hair salons across Hangzhou
SALONS: list[dict[str, Any]] = [
    {"brand_id": f"brighthair_xihu_{RUN_TAG}",    "name": "明亮发型 西湖店",
     "district": "Xihu",    "lat": 30.2741, "lng": 120.1551},
    {"brand_id": f"brighthair_gongshu_{RUN_TAG}", "name": "明亮发型 拱墅店",
     "district": "Gongshu", "lat": 30.3193, "lng": 120.1419},
    {"brand_id": f"brighthair_shangcheng_{RUN_TAG}", "name": "明亮发型 上城店",
     "district": "Shangcheng", "lat": 30.2425, "lng": 120.1693},
    {"brand_id": f"brighthair_xiacheng_{RUN_TAG}", "name": "明亮发型 下城店",
     "district": "Xiacheng",  "lat": 30.2812, "lng": 120.1730},
    {"brand_id": f"brighthair_jianggan_{RUN_TAG}", "name": "明亮发型 江干店",
     "district": "Jianggan",  "lat": 30.2566, "lng": 120.2050},
    {"brand_id": f"brighthair_binjiang_{RUN_TAG}", "name": "明亮发型 滨江店",
     "district": "Binjiang",  "lat": 30.2080, "lng": 120.2106},
    {"brand_id": f"brighthair_xiaoshan_{RUN_TAG}", "name": "明亮发型 萧山店",
     "district": "Xiaoshan",  "lat": 30.1840, "lng": 120.2645},
    {"brand_id": f"brighthair_yuhang_{RUN_TAG}",   "name": "明亮发型 余杭店",
     "district": "Yuhang",    "lat": 30.4188, "lng": 120.3026},
]

# 60 stylists: 8 stars + 24 senior + 28 junior (distributed across salons)
STYLIST_LEVELS = [
    ("star",   8, 60_000, 0.60),   # ticket ¥600+, commission 60%
    ("senior", 24, 30_000, 0.50),  # ticket ¥300+, commission 50%
    ("junior", 28, 15_000, 0.40),  # ticket ¥150+, commission 40%
]

SERVICE_TYPES = ["cut", "color", "perm", "treatment", "cut_and_color"]
SERVICE_PRICES = {
    "cut": 15000,           # ¥150
    "color": 50000,         # ¥500
    "perm": 60000,          # ¥600
    "treatment": 30000,     # ¥300
    "cut_and_color": 80000, # ¥800
}

# Style preferences (Round 5: time-series attribute)
STYLE_PREFS = ["bob", "layered", "pixie", "long_wavy", "blowout", "balayage", "highlights"]


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


# ── Consent helper ────────────────────────────────────────────────────────
_consent_policy_published = False
POLICY_VERSION = f"v_{RUN_TAG}"


async def _setup_consent(c: httpx.AsyncClient, user_ids: list[str]) -> int:
    global _consent_policy_published
    if not _consent_policy_published:
        await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
            "version": POLICY_VERSION,
            "text_md": "# Bright Hair consent\nStylist + photo + style history",
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


# ── Stylist generation ───────────────────────────────────────────────────
def _gen_stylists() -> list[dict[str, Any]]:
    stylists = []
    sid = 0
    for level, count, base_fee, commission in STYLIST_LEVELS:
        for i in range(count):
            sid += 1
            home_salon = SALONS[sid % len(SALONS)]["brand_id"]
            stylists.append({
                "stylist_id": f"stylist_{level}_{sid:03d}_{RUN_TAG}",
                "name": f"Stylist-{level[0].upper()}{sid:03d}",
                "level": level,
                "base_fee_cents": base_fee,
                "commission_rate": commission,
                "home_salon": home_salon,
            })
    return stylists


STYLISTS = _gen_stylists()


# ── Phase 1: Master + 8 Salons + Geofence ────────────────────────────────
async def phase_1_master_setup(c: httpx.AsyncClient) -> dict[str, Any]:
    _phase_init("1: Master + 8 Salons + Geofence")
    state: dict[str, Any] = {"master_id": None, "salon_store_ids": {}}

    sc, b = await call(c, "POST", "/api/v1/master/create", json_body={
        "company_name": "明亮发型集团 / Bright Hair Salons Corp",
        "primary_email": "laoqian@brighthair.cn",
        "owner_user_id": OWNER_USER_ID,
    })
    if sc == 201 and isinstance(b, dict):
        state["master_id"] = b["master_id"]
        ok("create master account", f"master_id={state['master_id']}")
    else:
        fail("create master account", f"{sc} {_short(b)}")
        return state

    master_id = state["master_id"]

    # Attach 8 salons
    attached = 0
    for s in SALONS:
        sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/brands/attach", json_body={
            "brand_id": s["brand_id"],
            "store_name": s["name"],
            "store_id": s["brand_id"],
        })
        if sc == 200:
            attached += 1
        else:
            gap("P1", f"attach salon {s['brand_id']}", f"{sc} {_short(b)}")
    if attached == 8:
        ok("attach 8 salons", f"all 8 attached to master {master_id}")
    else:
        gap("P0", "attach 8 salons", f"only {attached}/8 attached")

    # Geofence each salon (300m radius — urban storefronts)
    registered = 0
    for s in SALONS:
        store_id = f"store_{s['brand_id']}"
        sc, b = await call(c, "POST", "/api/v1/geofence/stores/register", json_body={
            "brand_id": s["brand_id"],
            "store_id": store_id,
            "name": s["name"],
            "brand_name": "明亮发型",
            "lat": s["lat"],
            "lng": s["lng"],
            "radius_meters": 300,
            "associated_game_slug": "hair_style_quiz",
            "push_config": {
                "enabled": True,
                "cooldown_minutes": 180,
                "hours_local": [10, 21],
                "message_template": "{name}, 距离上次剪发已{weeks_since_visit}周，{stylist_name} 已为您预留时间！",
            },
        })
        if sc == 200:
            registered += 1
            state["salon_store_ids"][s["brand_id"]] = store_id
        else:
            gap("P0", f"register salon {s['brand_id']}", f"{sc} {_short(b)}")
    if registered == 8:
        ok("geofence 8 salons", "300m radius, 10-21h, {weeks_since_visit}+{stylist_name} template")
    else:
        gap("P0", "register salons", f"only {registered}/8")

    state["primary_bid"] = SALONS[0]["brand_id"]
    return state


# ── Phase 2: Wallet ¥15K + cascade ───────────────────────────────────────
async def phase_2_wallet(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("2: Wallet ¥15K + cascade to 8 salons")
    master_id = state["master_id"]
    if not master_id:
        fail("phase 2", "no master_id")
        return

    # 12.5% × 8 salons = 100%
    allocation = {s["brand_id"]: 0.125 for s in SALONS}
    sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/budget/global", json_body={
        "monthly_budget_cents": 1_500_000,  # ¥15000
        "allocation": allocation,
    })
    if sc == 200:
        ok("set master global budget", "¥15000/月, even 12.5% × 8 salons")
    else:
        gap("P1", "set master global budget", f"{sc} {_short(b)}")

    # Cascade check (老周 P1: cascade doesn't auto-push daily caps)
    sample_bid = SALONS[0]["brand_id"]
    sc, b = await call(c, "GET", f"/api/v1/wallet/{sample_bid}/daily-budget-status")
    cascaded = ((b or {}).get("today_budget_cents") or (b or {}).get("daily_budget_cents") or 0) \
        if isinstance(b, dict) else 0
    if cascaded > 0:
        ok("budget cascade verified", f"daily_budget_cents={cascaded}")
    else:
        gap("P1", "budget cascade",
            "master budget did not push daily caps to salon wallets — same gap "
            "as 老周 (fitness chain). Still needs manual top-up per branch.")

    # Top up each salon ¥1875 = ¥15000 / 8
    funded = 0
    for s in SALONS:
        bid = s["brand_id"]
        sc, b = await call(c, "POST", f"/api/v1/wallet/{bid}/topup", json_body={
            "amount_cents": 187_500, "payment_method": "wechat",
        })
        if sc != 200 or not isinstance(b, dict) or "topup_id" not in b:
            gap("P1", f"topup {bid}", f"{sc} {_short(b)}")
            continue
        tid = b["topup_id"]
        sc2, _ = await call(c, "POST", f"/api/v1/wallet/{bid}/topup/{tid}/confirm",
                            json_body={"payment_gateway_response": {"mock": True}})
        if sc2 == 200:
            funded += 1
            await call(c, "POST", f"/api/v1/wallet/{bid}/daily-budget",
                       json_body={"daily_budget_cents": 6000})  # ~¥60/day
    if funded == 8:
        ok("fund + daily-budget all 8 salons", "¥1875 each, ¥60 daily cap")
    else:
        gap("P0", "salon wallet funding", f"only {funded}/8 funded")

    sc, b = await call(c, "GET", f"/api/v1/master/{master_id}/consolidated-report")
    if sc == 200 and isinstance(b, dict):
        ok("consolidated report",
           f"balance=¥{b.get('total_balance_cents',0)/100:.2f}")
    else:
        gap("P1", "consolidated report", f"{sc} {_short(b)}")


# ── Phase 3: Consent + Tier + Stylist Roster ─────────────────────────────
async def phase_3_consent_tier(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("3: Consent + Tier Setup + Stylist Roster")

    sc, b = await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
        "version": POLICY_VERSION,
        "text_md": "# Bright Hair consent\nStylist + photo + style history + commission tracking",
        "effective_at": int(time.time()) - 60,
        "requires_re_grant": False,
    })
    if sc == 200:
        ok("publish consent policy", POLICY_VERSION)
        global _consent_policy_published
        _consent_policy_published = True
    else:
        gap("P0", "publish consent policy", f"{sc} {_short(b)}")

    primary_bid = state["primary_bid"]

    # Tier configure — VIP card tiers based on annual spend
    sc, b = await call(c, "POST", "/api/v1/primitives/tier/configure", json_body={
        "brand_id": primary_bid,
        "tiers": [
            {"name": "guest", "xp_min": 0},
            {"name": "regular", "xp_min": 500},     # ~3 visits
            {"name": "silver", "xp_min": 2000},     # ~ ¥2000/yr
            {"name": "gold", "xp_min": 5000},       # ~ ¥5000/yr
            {"name": "diamond", "xp_min": 15000},   # VIP
        ],
    })
    if sc == 200:
        ok("tier configure", "guest/regular/silver/gold/diamond")
    else:
        gap("P1", "tier configure", f"{sc} {_short(b)}")

    # Create per-tier primitives
    tiers = [
        {"id": "guest", "name": "Guest", "threshold_xp": 0, "perks": []},
        {"id": "regular", "name": "Regular", "threshold_xp": 500, "perks": ["10pct_off"]},
        {"id": "silver", "name": "Silver", "threshold_xp": 2000,
         "perks": ["15pct_off", "priority_booking"]},
        {"id": "gold", "name": "Gold", "threshold_xp": 5000,
         "perks": ["20pct_off", "priority_booking", "free_treatment_qtr"]},
        {"id": "diamond", "name": "Diamond", "threshold_xp": 15000,
         "perks": ["25pct_off", "private_room", "free_treatment_month", "stylist_house_call"]},
    ]
    created = 0
    for t in tiers:
        sc, _ = await call(c, "POST", f"/api/v1/primitives/brand/{primary_bid}/tiers",
                           json_body=t)
        if sc == 200:
            created += 1
    if created == 5:
        ok("create 5 tiers", "")
    else:
        gap("P1", "create tiers", f"only {created}/5")

    # Cross-salon tier portability check — same gap as 老周
    sc, b = await call(c, "GET", f"/api/v1/primitives/brand/{SALONS[1]['brand_id']}/tiers")
    second_tiers = b if isinstance(b, list) else []
    if not second_tiers:
        gap("P0", "cross-salon tier portability",
            f"Tiers configured on {primary_bid} but {SALONS[1]['brand_id']} (same master) "
            "has no tiers visible. A diamond customer at 西湖店 walks into 余杭店 as "
            "'guest' — the VIP card model is broken across the chain. Customers EXPECT "
            "their tier to follow them; commission settlement assumes it does. P0 for "
            "chain salons.")
    else:
        ok("cross-salon tier portability", f"second salon sees {len(second_tiers)} tiers")

    # Register the 60-stylist roster as brand attributes / agents
    # R7: POST /primitives/brand/{bid}/resources (no /register suffix)
    sc, b = await call(c, "POST", f"/api/v1/primitives/brand/{primary_bid}/resources",
                       json_body={
                           "resource_id": STYLISTS[0]["stylist_id"],
                           "name": STYLISTS[0]["name"],
                           "type": "stylist",
                           "metadata": {"level": STYLISTS[0]["level"],
                                        "commission_rate": STYLISTS[0]["commission_rate"]},
                       })
    if sc == 404:
        gap("P0", "no stylist/resource primitive",
            "POST /primitives/brand/{bid}/resources 404. Hair salons need a "
            "first-class STAFF/RESOURCE primitive — each stylist has identity, level, "
            "commission rate, calendar, and history. Today the merchant must hand-roll "
            "the roster as free-form metadata strings on reservations. No way to query "
            "'who is Stylist-S001?' through the platform. Same gap as 老蔡 "
            "(specialist marketplace) — sharper here because customers follow stylists "
            "across branches.")
    elif sc in (200, 201):
        ok("stylist resource registered", "first-class resource primitive exists!")
        # R7: bulk-register the rest so downstream phases can reference real resources
        registered = 1
        for stylist in STYLISTS[1:20]:
            sc2, _ = await call(c, "POST",
                                f"/api/v1/primitives/brand/{primary_bid}/resources",
                                json_body={
                                    "resource_id": stylist["stylist_id"],
                                    "name": stylist["name"],
                                    "type": "stylist",
                                    "metadata": {"level": stylist["level"],
                                                 "commission_rate": stylist["commission_rate"]},
                                })
            if sc2 in (200, 201):
                registered += 1
        if registered >= 15:
            ok("stylist roster bulk registered", f"{registered}/20 stylists")
        else:
            gap("P1", "stylist roster partial", f"only {registered}/20 registered")
        # R7: list resources by type=stylist to verify discovery
        sc3, b3 = await call(c, "GET",
                             f"/api/v1/primitives/brand/{primary_bid}/resources",
                             params={"type": "stylist"})
        if sc3 == 200 and isinstance(b3, dict) and b3.get("count", 0) > 0:
            ok("list stylists by type", f"count={b3['count']}")
        else:
            gap("P1", "list stylists by type", f"{sc3} {_short(b3)}")
    else:
        gap("P1", "stylist register", f"{sc} {_short(b)}")

    state["stylists"] = STYLISTS[:20]  # work with 20 for the sim


# ── Phase 4: Reservation w/ resource_id=stylist_id ───────────────────────
async def phase_4_reservations(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("4: Reservation Primitive — resource_id=stylist_id probe")
    primary_bid = state["primary_bid"]
    probe_uid = f"customer_probe_{RUN_TAG}"
    await _setup_consent(c, [probe_uid])

    # Recovery voucher for no-show (12% rate)
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": primary_bid,
        "name": "No-show Recovery: ¥50 off next cut",
        "description": "Make-up after no-show",
        "value": {"type": "fixed", "amount": 5000, "currency": "CNY"},
        "conditions": {"usage_limit_per_user": 1},
        "expires_in_days": 30,
        "stackable": False,
        "transferable": False,
    })
    recovery_tid = None
    if sc == 201 and isinstance(b, dict):
        recovery_tid = b.get("template_id")
        state["recovery_voucher_template"] = recovery_tid
        ok("recovery voucher template", f"id={recovery_tid}")
    else:
        gap("P1", "recovery voucher template", f"{sc} {_short(b)}")

    # Sample reservation with R7: type=stylist + resource_id + fulfiller_user_id
    star_stylist = STYLISTS[0]
    stylist_uid = f"user_{star_stylist['stylist_id']}"
    await _setup_consent(c, [stylist_uid])
    sc, b = await call(c, "POST", "/api/v1/reservations/create", json_body={
        "brand_id": primary_bid,
        "user_id": probe_uid,
        "scheduled_at": int(time.time()) + 3600,
        "party_size": 1,
        "type": "stylist",  # R7: first-class stylist type
        "resource_id": star_stylist["stylist_id"],
        "fulfiller_user_id": stylist_uid,  # R7: stylist as fulfiller
        "metadata": {
            "service": "cut_and_color",
            "stylist_name": star_stylist["name"],
            "stylist_level": star_stylist["level"],
            "estimated_price_cents": SERVICE_PRICES["cut_and_color"],
            "commission_rate": star_stylist["commission_rate"],
        },
        "recovery_voucher_template_id": recovery_tid,
        "check_in_grace_minutes": 15,
    })
    rid_sample = None
    if sc in (200, 201) and isinstance(b, dict):
        rid_sample = b.get("reservation_id")
        # R7: GET readback to verify resource_id + fulfiller_user_id persisted
        sc_get, b_get = await call(c, "GET", f"/api/v1/reservations/{rid_sample}")
        if sc_get == 200 and isinstance(b_get, dict):
            if b_get.get("resource_id") == star_stylist["stylist_id"]:
                ok("reservation resource_id (stylist) persisted top-level",
                   f"rid={rid_sample} resource={b_get['resource_id']}")
            else:
                gap("P0", "reservation resource_id silently dropped",
                    f"GET readback resource_id={b_get.get('resource_id')!r}")
            if b_get.get("fulfiller_user_id") == stylist_uid:
                ok("reservation fulfiller_user_id persisted", f"fulfiller={stylist_uid}")
            else:
                gap("P1", "fulfiller_user_id not persisted",
                    f"GET readback fulfiller_user_id={b_get.get('fulfiller_user_id')!r}")
        # R7: GET /reservations/fulfiller/{uid} — stylist chair calendar
        sc_ff, b_ff = await call(c, "GET",
                                  f"/api/v1/reservations/fulfiller/{stylist_uid}")
        if sc_ff == 200 and isinstance(b_ff, dict) and b_ff.get("count", 0) >= 1:
            ok("stylist chair calendar via /fulfiller/{uid}",
               f"count={b_ff['count']}")
        else:
            gap("P0", "stylist chair calendar missing",
                f"GET /reservations/fulfiller/{stylist_uid} {sc_ff} {_short(b_ff)}")
        # R7: 409 conflict on overlapping booking for same stylist (resource lock)
        sc_dup, b_dup = await call(c, "POST", "/api/v1/reservations/create", json_body={
            "brand_id": primary_bid,
            "user_id": f"customer_dup_{RUN_TAG}",
            "scheduled_at": int(time.time()) + 3600,  # same slot
            "party_size": 1,
            "type": "stylist",
            "resource_id": star_stylist["stylist_id"],
        })
        if sc_dup == 409:
            ok("409 conflict on overlapping stylist booking", "resource lock works")
        elif sc_dup in (200, 201):
            gap("P1", "no overlap conflict on shared stylist",
                "two bookings for same stylist at same time both accepted; "
                "expected 409 conflict on overlapping resource bookings.")
    else:
        gap("P0", "reservation create with stylist", f"{sc} {_short(b)}")
    state["sample_reservation_id"] = rid_sample

    # Check-in probe
    if rid_sample:
        sc, b = await call(c, "POST", f"/api/v1/reservations/{rid_sample}/check-in",
                           json_body={"at_brand_id": primary_bid, "evidence": "qr"})
        if sc == 200:
            ok("reservation check-in", "qr evidence honored")
        elif sc == 409:
            info(f"check-in 409 (guard fired): {_short(b, 100)}")
        else:
            gap("P1", "reservation check-in", f"{sc} {_short(b)}")

    # Policy configure
    sc, b = await call(c, "POST", "/api/v1/reservations/admin/policy/configure",
                       json_body={
                           "brand_id": primary_bid,
                           "default_grace_minutes": 15,
                           "default_recovery_voucher_template_id": recovery_tid,
                       })
    if sc == 200:
        ok("reservation policy configure", "grace=15min, recovery wired")
    else:
        gap("P1", "reservation policy configure", f"{sc} {_short(b)}")

    # Triggers — honored → award_xp, no_show → recovery voucher
    if recovery_tid:
        sc, _ = await call(c, "POST", "/api/v1/reservations/triggers/register", json_body={
            "brand_id": primary_bid,
            "event_type": "reservation.no_show",
            "action_type": "issue_voucher",
            "action_config": {"template_id": recovery_tid, "expires_in_days": 30},
        })
        if sc in (200, 201):
            ok("trigger: no_show → issue_voucher", "")
        else:
            gap("P1", "trigger no_show", f"{sc}")

    sc, _ = await call(c, "POST", "/api/v1/reservations/triggers/register", json_body={
        "brand_id": primary_bid,
        "event_type": "reservation.honored",
        "action_type": "award_xp",
        "action_config": {"amount": 100},
    })
    if sc in (200, 201):
        ok("trigger: honored → award_xp", "+100 XP per visit")
    else:
        gap("P1", "trigger honored", f"{sc}")

    # 100 reservation burst — distribute across 8 salons + 20 stylists
    rng = random.Random(RUN_TAG + 4)
    burst_ok, burst_total = 0, 100
    for i in range(burst_total):
        salon = rng.choice(SALONS)
        stylist = rng.choice(STYLISTS)
        service = rng.choice(SERVICE_TYPES)
        sc, _ = await call(c, "POST", "/api/v1/reservations/create", json_body={
            "brand_id": salon["brand_id"],
            "user_id": f"customer_burst_{RUN_TAG}_{i % 50:02d}",
            "scheduled_at": int(time.time()) + 86400 + i * 1800,
            "party_size": 1,
            "type": "service",
            "resource_id": stylist["stylist_id"],
            "metadata": {
                "service": service,
                "estimated_price_cents": SERVICE_PRICES[service],
                "stylist_level": stylist["level"],
            },
            "check_in_grace_minutes": 15,
        })
        if sc in (200, 201):
            burst_ok += 1
    if burst_ok == burst_total:
        ok("100-reservation burst", f"{burst_ok}/{burst_total} across 8 salons × 20 stylists")
    elif burst_ok > 0:
        gap("P1", "reservation burst", f"only {burst_ok}/{burst_total} succeeded")
    else:
        gap("P0", "reservation burst", f"0/{burst_total} — broken?")

    # Per-stylist stats probe
    sc, b = await call(c, "GET", f"/api/v1/reservations/brand/{primary_bid}/stats",
                       params={"resource_id": STYLISTS[0]["stylist_id"]})
    if sc == 200 and isinstance(b, dict):
        if "by_resource" in b or b.get("filtered_by_resource"):
            ok("per-stylist stats", "resource breakdown returned")
        else:
            gap("P1", "per-stylist stats filter ignored",
                f"resource_id query param did not narrow stats; aggregate only "
                f"({_short(b, 120)}). Salons need per-stylist no-show / honor rate "
                "for commission and performance review.")
    else:
        gap("P1", "per-stylist stats", f"{sc} {_short(b)}")

    # No-show scan
    sc, b = await call(c, "POST", "/api/v1/reservations/scan-no-shows", json_body={
        "admin_token": "DEV", "dry_run": True, "cutoff_seconds": 1800,
    })
    if sc == 200 and isinstance(b, dict):
        ok("no-show scan (dry run)",
           f"scanned={b.get('scanned')} would_mark={b.get('marked_no_show')}")
    elif sc == 403:
        gap("P2", "no-show scan admin_token", "DEV token rejected; needs ADMIN_TOKEN env")
    else:
        gap("P1", "no-show scan", f"{sc} {_short(b)}")


# ── Phase 5: Recipe — hair recurring + buddy ─────────────────────────────
async def phase_5_recipe(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("5: Recipe — hair_recurring_cut + buddy referral")
    primary_bid = state["primary_bid"]

    await call(c, "GET", "/api/v1/recipes/_catalog/reload")
    sc, b = await call(c, "GET", "/api/v1/recipes")
    hair_recipes: list[dict] = []
    hair_recipe_exists = False
    if sc == 200 and isinstance(b, (list, dict)):
        items = b if isinstance(b, list) else b.get("recipes", b.get("items", []))
        for r in items:
            if not isinstance(r, dict):
                continue
            rid = r.get("id") or r.get("recipe_id") or ""
            cat = r.get("category", "") or ""
            name = r.get("name", "") or ""
            if any(kw in (rid + cat + name).lower()
                   for kw in ["hair", "salon", "stylist", "recurring", "cycle"]):
                hair_recipes.append(r)
            if rid in ("hair_recurring_cut", "salon_loyalty", "stylist_attachment"):
                hair_recipe_exists = True
        ok("recipes catalog read",
           f"{len(items)} total, {len(hair_recipes)} hair/salon-relevant")
    else:
        gap("P1", "recipe listing", f"{sc} {_short(b)}")

    if hair_recipe_exists:
        ok("hair recipe", "hair_recurring_cut / salon_loyalty exists")
    else:
        gap("P0", "no native hair/salon recipe",
            f"No recipe with id ∈ {{hair_recurring_cut, salon_loyalty, stylist_attachment}}. "
            f"Closest matches: {[r.get('id') for r in hair_recipes][:5]}. The 4-6 week "
            "haircut cycle + stylist-attachment loop is a primary loyalty pattern (¥150-800 "
            "AOV, 60+ stylist marketplace). Today merchant must reach for "
            "duolingo_streak/nike_run and lose the cadence semantics.")

    # NL → recipe
    sc, b = await call(c, "POST", "/api/v1/recipe-gen/from-description", json_body={
        "brand_id": primary_bid,
        "description": "顾客每4周回店剪发，连续6次解锁¥200折扣。指定发型师可获双倍积分。好友推荐则双方各得¥80折扣。",
        "industry": "beauty",
        "style": "loyalty",
    })
    if sc == 200 and isinstance(b, dict):
        rec = b.get("recipe", {})
        modules = b.get("modules_used", [])
        ok("NL→recipe (hair/loyalty)", f"modules={modules[:5]}")
        rstr = json.dumps(rec, ensure_ascii=False).lower()
        if "streak" in rstr or "recurring" in rstr or "cycle" in rstr:
            ok("recipe captures recurring cadence", "")
        else:
            gap("P1", "NL→recipe cadence parse",
                "'每4周回店' intent did not produce a cadence-aware module. "
                "Beauty/wellness recurring is timer-based, not action-streak-based.")
        if "referral" in rstr or "buddy" in rstr or "好友" in rstr or "邀请" in rstr:
            ok("recipe includes referral", "")
        else:
            gap("P1", "NL→recipe buddy parse",
                "'好友推荐' intent did not produce referral/buddy module")
    else:
        gap("P1", "NL→recipe call", f"{sc} {_short(b)}")

    # Buddy referral voucher template ¥80 mutual
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": primary_bid,
        "name": "Buddy Referral - ¥80 Mutual",
        "description": "Refer a friend → both get ¥80 off next cut",
        "value": {"type": "fixed", "amount": 8000, "currency": "CNY"},
        "conditions": {"usage_limit_per_user": 1},
        "expires_in_days": 90,
        "stackable": False,
        "transferable": False,
    })
    if sc == 201 and isinstance(b, dict):
        state["buddy_voucher_template"] = b.get("template_id")
        ok("buddy voucher template", f"id={b.get('template_id')}")
        gap("P1", "no atomic mutual-reward primitive",
            "Same gap as 老周/老蔡: voucher template is per-user; no 'mutual referral' "
            "primitive that atomically issues ¥80 to BOTH sides on conversion. "
            "Merchant must hand-roll dual issue + dedup — race conditions are theirs.")
    else:
        gap("P1", "buddy voucher", f"{sc} {_short(b)}")


# ── Phase 6: Customer-Stylist Attachment + Style Preference History ──────
async def phase_6_stylist_attachment(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("6: Customer-Stylist Attachment + Style Preference Time-Series")
    primary_bid = state["primary_bid"]
    test_uid = f"regular_customer_{RUN_TAG}"
    await _setup_consent(c, [test_uid])

    # Store "my regular stylist" as attribute
    star_stylist = STYLISTS[0]
    sc, b = await call(c, "POST", f"/api/v1/primitives/user/{test_uid}/attributes",
                       json_body={
                           "brand_id": primary_bid,
                           "attrs": {
                               "regular_stylist_id": star_stylist["stylist_id"],
                               "regular_stylist_name": star_stylist["name"],
                               "preferred_style": "balayage",
                               "last_cut_at": str(int(time.time()) - 21 * 86400),  # 3 weeks ago
                               "last_color_at": str(int(time.time()) - 56 * 86400),  # 8 weeks ago
                               "average_ticket_cents": "65000",
                           },
                       })
    if sc == 200:
        ok("attach customer to regular stylist", "5 attributes set")
    else:
        gap("P0", "set stylist attachment attrs", f"{sc} {_short(b)}")

    # Style preference evolution — time-series log
    style_history = [
        (int(time.time()) - 180 * 86400, "long_wavy"),
        (int(time.time()) - 120 * 86400, "layered"),
        (int(time.time()) - 60 * 86400, "balayage"),
        (int(time.time()) - 30 * 86400, "highlights"),
        (int(time.time()) - 1 * 86400, "balayage"),
    ]
    logged = 0
    for ts, style in style_history:
        sc, _ = await call(c, "POST",
                           f"/api/v1/primitives/user/{test_uid}/attributes/preferred_style/log",
                           json_body={
                               "brand_id": primary_bid,
                               "value": style,
                               "ts": ts,
                               "source": "stylist_record",
                           })
        if sc == 200:
            logged += 1
    if logged == 5:
        ok("style preference history logged", "5 entries over 6 months")
    elif logged > 0:
        gap("P1", "style time-series partial", f"only {logged}/5 entries logged")
    else:
        gap("P0", "style preference time-series",
            f"Cannot log style preference time-series (last call={sc}). Salons need "
            "this to detect 'customer drifting away from current stylist's specialty' "
            "(e.g. balayage stylist's customer keeps requesting pixie cuts → flag for "
            "rebooking with different stylist). String-valued time-series may not be "
            "supported.")

    # Read history
    sc, b = await call(c, "GET",
                       f"/api/v1/primitives/user/{test_uid}/attributes/preferred_style/history",
                       params={"brand_id": primary_bid, "limit": 50})
    if sc == 200 and isinstance(b, dict) and b.get("count", 0) >= 1:
        ok("style preference history readback", f"count={b['count']}")
    else:
        gap("P1", "style history readback",
            f"{sc} {_short(b)} — string-valued time-series readback failed/empty.")

    # Read last_cut_at, compute weeks since
    sc, b = await call(c, "GET",
                       f"/api/v1/primitives/user/{test_uid}/attributes",
                       params={"brand_id": primary_bid})
    if sc == 200 and isinstance(b, dict):
        attrs = b.get("attrs", {})
        last_cut = attrs.get("last_cut_at")
        if last_cut:
            weeks = (int(time.time()) - int(last_cut)) / (7 * 86400)
            ok("readback last_cut_at", f"~{weeks:.1f} weeks since last cut")
        else:
            gap("P1", "last_cut_at missing on readback", _short(b, 120))
    else:
        gap("P1", "attribute readback", f"{sc} {_short(b)}")

    # Probe: lifecycle stage based on visit recency
    # 4-week cycle → at_risk if 6+ weeks no visit, churned if 12+
    gap("P1", "no auto cadence-based lifecycle",
        "Lifecycle stage is free-form text; the platform never auto-computes "
        "'at_risk' (no visit in 6 weeks) or 'churned' (no visit in 12 weeks) for "
        "the hair recurring cycle. 老周 fitness flagged the same gap — for hair "
        "the cadence is even more predictable (4-6 weeks) so the missing automation "
        "is a bigger pain point.")


# ── Phase 7: Audience — new + lapsed cohorts ─────────────────────────────
async def phase_7_audience(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("7: Audience — new_users + lapsed (>5 weeks)")
    primary_bid = state["primary_bid"]
    rng = random.Random(RUN_TAG + 7)

    new_uids = [f"new_{RUN_TAG}_{i:02d}" for i in range(30)]
    lapsed_uids = [f"lapsed_{RUN_TAG}_{i:02d}" for i in range(20)]
    await _setup_consent(c, new_uids + lapsed_uids)

    # Tag last_cut_at — new users have a recent first visit, lapsed have 6-12 weeks old
    for uid in new_uids:
        ts = int(time.time()) - rng.randint(0, 7) * 86400
        await call(c, "POST",
                   f"/api/v1/primitives/user/{uid}/attributes/last_cut_at",
                   params={"brand_id": primary_bid},
                   json_body={"value": str(ts), "ttl_seconds": 90 * 86400})
    for uid in lapsed_uids:
        ts = int(time.time()) - rng.randint(42, 90) * 86400  # 6-12 weeks ago
        await call(c, "POST",
                   f"/api/v1/primitives/user/{uid}/attributes/last_cut_at",
                   params={"brand_id": primary_bid},
                   json_body={"value": str(ts), "ttl_seconds": 90 * 86400})
    ok("tagged 50 users with last_cut_at", "30 new + 20 lapsed")

    # Build custom audience for new (manual list)
    sc, b = await call(c, "POST", "/api/v1/audiences/custom/create", json_body={
        "brand_id": primary_bid,
        "name": "New Customers (last 7 days)",
        "source": "manual",
        "user_ids": new_uids,
        "description": "First-time visitors in last 7 days",
    })
    if sc == 200 and isinstance(b, dict):
        state["new_audience_id"] = b.get("audience_id")
        ok("new-user audience", f"id={state['new_audience_id']} size={b.get('size')}")
    else:
        gap("P1", "new audience create", f"{sc} {_short(b)}")

    # Build lapsed audience for renewal-cycle reactivation
    sc, b = await call(c, "POST", "/api/v1/audiences/custom/create", json_body={
        "brand_id": primary_bid,
        "name": "Lapsed Customers (6-12 weeks no cut)",
        "source": "manual",
        "user_ids": lapsed_uids,
    })
    if sc == 200 and isinstance(b, dict):
        state["lapsed_audience_id"] = b.get("audience_id")
        ok("lapsed audience", f"id={state['lapsed_audience_id']} size={b.get('size')}")
    else:
        gap("P1", "lapsed audience", f"{sc} {_short(b)}")

    # Probe: filter by last_cut_at recency directly
    sc, b = await call(c, "POST", "/api/v1/audiences/segment", json_body={
        "brand_id": primary_bid,
        "name": "Auto-Lapsed (last_cut_at > 35 days)",
        "filters": {"last_cut_at": {"older_than_days": 35}},
    })
    if sc == 404:
        gap("P0", "no attribute-based audience segmentation",
            "POST /api/v1/audiences/segment 404. Same as 老周: no auto-rebuild of "
            "lapsed cohort by attribute predicate. The 4-week cycle is the unit of "
            "salon retention — there must be an automatic 'time since last visit > N' "
            "segment that refreshes daily. Today every audience requires a pre-computed "
            "user_id list.")
    elif sc in (200, 201):
        ok("attribute-based segment", _short(b, 120))
    elif sc in (400, 422):
        gap("P1", "segment schema", f"{sc} {_short(b)}")

    # Lookalike from new converters
    aid = state.get("new_audience_id")
    if aid:
        sc, b = await call(c, "POST", f"/api/v1/audiences/{aid}/lookalike", json_body={
            "brand_id": primary_bid,
            "name": "Lookalike new customers",
            "similarity": 5,
            "size_target": 2000,
            "geo": {"country": "CN", "city": "Hangzhou"},
        })
        if sc == 200:
            ok("lookalike", f"size={b.get('size') if isinstance(b, dict) else '?'}")
        else:
            gap("P1", "lookalike", f"{sc} {_short(b)}")


# ── Phase 8: Renewal-Cycle Campaign w/ 30-day attribution ────────────────
async def phase_8_cycle_campaign(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("8: Renewal-Cycle Campaign — target_audience + attribution_window=30")
    primary_bid = state["primary_bid"]

    # New-user acquisition with 30-day attribution (covers 4-week first cycle)
    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": primary_bid,
        "name": "Hair Acquisition — New Hangzhou customers",
        "objective": "acquire",
        "bid_strategy": "target_cpa",
        "max_bid_cents": 500,
        "target_cpa_cents": 8000,         # ¥80 acceptable CPA per first-cut customer
        "daily_budget_cents": 6000,        # ¥60/day
        "total_budget_cents": 150_000,     # ¥1500 total
        "attribution_window_days": 30,     # 4-week cycle
        "target_audience": "new_users_only",
        "targeting": {"geo": {"country": "CN", "city": "Hangzhou", "radius_km": 20}},
    })
    if sc in (200, 201) and isinstance(b, dict):
        cid = b.get("campaign_id")
        state["acq_campaign_id"] = cid
        ok("acquisition campaign", f"id={cid} window=30d target_cpa=¥80")
        sc_a, _ = await call(c, "POST", f"/api/v1/campaigns/{cid}/admin/approve",
                             json_body={"admin_token": "DEV", "notes": "sim auto"})
        if sc_a == 200:
            ok("acq campaign approved", "")
    else:
        gap("P1", "acquisition campaign", f"{sc} {_short(b)}")

    # Lapsed re-engagement campaign — should be 'retention' / 'reactivation' but...
    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": primary_bid,
        "name": "Re-engage lapsed — 6+ weeks no cut",
        "objective": "retention",
        "bid_strategy": "cpa",
        "max_bid_cents": 800,
        "daily_budget_cents": 4000,
        "total_budget_cents": 50_000,
        "attribution_window_days": 30,
        "targeting": {"geo": {"country": "CN", "city": "Hangzhou"}},
    })
    if sc in (400, 422):
        detail = b.get("detail") if isinstance(b, dict) else str(b)
        gap("P0", "no 'retention'/'reactivation' campaign objective",
            f"objective='retention' rejected: {str(detail)[:200]}. Hair salons (and any "
            "cadence-based service: nails, lashes, massage, dental cleaning) live or "
            "die by reactivating 5-12 week lapsed customers. Today must fall back to "
            "'acquire' with audience workaround — but auction savings then thinks they "
            "are NEW users (target_audience=new_users_only). The two cannot coexist.")
        # Fallback to acquire
        sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
            "brand_id": primary_bid,
            "name": "[INTENT:REACTIVATION] Lapsed 6+ weeks",
            "objective": "acquire",
            "bid_strategy": "cpa",
            "max_bid_cents": 800,
            "daily_budget_cents": 4000,
            "total_budget_cents": 50_000,
            "attribution_window_days": 30,
            "targeting": {"geo": {"country": "CN", "city": "Hangzhou"}},
        })
    if sc in (200, 201) and isinstance(b, dict):
        cid = b.get("campaign_id")
        state["reactivation_campaign_id"] = cid
        ok("reactivation campaign (forced into acquire)", f"id={cid}")

    # Probe: attribution_window_days = 30 (within cap)
    if state.get("acq_campaign_id"):
        sc, b = await call(c, "GET", f"/api/v1/campaigns/{state['acq_campaign_id']}")
        if sc == 200 and isinstance(b, dict):
            stored = b.get("attribution_window_days")
            if stored == 30:
                ok("attribution_window_days=30 persisted", "4-week cycle window honored")
            else:
                gap("P1", "attribution_window mismatch",
                    f"submitted 30, stored {stored}")


# ── Phase 9: Geofence + Push Engine + Interpolation ──────────────────────
async def phase_9_geofence_push(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("9: Geofence Push — {stylist_name} + {weeks_since_visit} interpolation")
    primary_bid = state["primary_bid"]
    primary_store_id = state["salon_store_ids"].get(primary_bid)
    if not primary_store_id:
        fail("phase 9", "no primary store_id")
        return

    test_uid = f"push_test_{RUN_TAG}"
    await _setup_consent(c, [test_uid])
    star_stylist = STYLISTS[0]
    await call(c, "POST", f"/api/v1/primitives/user/{test_uid}/attributes",
               json_body={
                   "brand_id": primary_bid,
                   "attrs": {
                       "name": "美玲",
                       "stylist_name": star_stylist["name"],
                       "weeks_since_visit": "5",
                       "regular_stylist_id": star_stylist["stylist_id"],
                   },
               })

    sc, b = await call(c, "POST", "/api/v1/geofence/enter", json_body={
        "user_id": test_uid,
        "device_fingerprint": f"dev_{test_uid}",
        "store_id": primary_store_id,
    })
    if sc == 200 and isinstance(b, dict):
        payload = b.get("payload") or {}
        push_message = (payload.get("message") if isinstance(payload, dict) else None) \
            or b.get("push_message")
        if push_message and isinstance(push_message, str):
            if "{name}" in push_message or "{stylist_name}" in push_message \
               or "{weeks_since_visit}" in push_message:
                gap("P0", "push template interpolation not applied",
                    f"Geofence push fired with RAW placeholders: '{push_message[:140]}'. "
                    "Customers see literal '{stylist_name}' instead of their stylist's name. "
                    "Round 5 promised end-to-end interpolation against user attribute "
                    "hash — not landing for salon push templates.")
            elif star_stylist["name"] in push_message or "美玲" in push_message:
                ok("push interpolation", f"placeholders replaced: '{push_message[:80]}'")
            else:
                gap("P1", "push interpolation fallback",
                    f"placeholders replaced with generic fallback (not customer "
                    f"attributes): '{push_message[:120]}'. Interpolator not reading "
                    "user attribute hash; falling back to defaults.")
        ok("geofence enter", "200")
    else:
        gap("P1", "geofence enter", f"{sc} {_short(b)}")

    # Push/now engine probe
    sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
        "phone": f"+8613900{RUN_TAG % 1000000:06d}",
        "display_name": "Push Test Customer",
        "device_fingerprint": f"dev_fp_qian_{RUN_TAG}",
    })
    push_kid = b.get("kid") if isinstance(b, dict) else None
    if push_kid:
        await _setup_consent(c, [push_kid])
        sc, b = await call(c, "POST", "/api/v1/push/now", json_body={
            "kid": push_kid, "slot": "push", "context": {},
        })
        if sc == 200 and isinstance(b, dict):
            if b.get("fired"):
                ok("push/now fired",
                   f"brand={b.get('brand_id')} charged={b.get('charged_cents')}c")
            else:
                ok("push/now endpoint", f"fired=false reason={b.get('reason')}")
        else:
            gap("P1", "push/now", f"{sc} {_short(b)}")


# ── Phase 10: Photo Before/After Storage ─────────────────────────────────
async def phase_10_photo_history(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("10: Photo Before/After Storage — attribute or BLOB?")
    primary_bid = state["primary_bid"]
    photo_uid = f"photo_test_{RUN_TAG}"
    await _setup_consent(c, [photo_uid])

    # Try storing photo URL/hash as time-series
    photos = [
        (int(time.time()) - 90 * 86400, "https://cdn.brighthair.cn/photos/u1_before_001.jpg"),
        (int(time.time()) - 60 * 86400, "https://cdn.brighthair.cn/photos/u1_after_001.jpg"),
        (int(time.time()) - 30 * 86400, "https://cdn.brighthair.cn/photos/u1_after_002.jpg"),
        (int(time.time()) - 1 * 86400, "https://cdn.brighthair.cn/photos/u1_after_003.jpg"),
    ]
    logged = 0
    for ts, url in photos:
        sc, _ = await call(c, "POST",
                           f"/api/v1/primitives/user/{photo_uid}/attributes/photo_history/log",
                           json_body={
                               "brand_id": primary_bid,
                               "value": url,
                               "ts": ts,
                               "source": "stylist_upload",
                           })
        if sc == 200:
            logged += 1
    if logged == 4:
        ok("photo history time-series", "4 photo URLs logged over 90 days")
    elif logged > 0:
        gap("P1", "photo history partial",
            f"only {logged}/4 — string-valued time-series may be limited")
    else:
        gap("P0", "no photo history storage",
            f"Cannot log photo URLs as time-series ({sc}). Hair salons need before/after "
            "photo trail per customer for: (a) consultation reference (stylist sees "
            "history before service), (b) marketing case studies, (c) dispute resolution "
            "('I asked for X, you gave me Y'). Today merchant must build their own "
            "asset CDN + index — outside the platform.")

    # BLOB/asset upload primitive probe
    sc, b = await call(c, "POST", "/api/v1/assets/upload",
                       json_body={
                           "brand_id": primary_bid,
                           "user_id": photo_uid,
                           "kind": "photo",
                           "mime_type": "image/jpeg",
                       })
    if sc == 404:
        gap("P1", "no native asset/photo upload primitive",
            "POST /api/v1/assets/upload 404. No platform-managed photo store; salons "
            "must operate their own S3/OSS bucket + CDN. For a vertical that lives on "
            "visual transformation, this is a meaningful build-your-own gap.")
    elif sc in (200, 201):
        ok("asset upload primitive", "exists")


# ── Phase 11: Stylist Commission Split + Payout Split ────────────────────
async def phase_11_commission_split(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("11: Commission Split — stylist takes 40-60% of ticket")
    primary_bid = state["primary_bid"]
    customer_uid = f"split_customer_{RUN_TAG}"
    await _setup_consent(c, [customer_uid])

    star = STYLISTS[0]  # star: 60% commission
    ticket_cents = SERVICE_PRICES["cut_and_color"]  # ¥800

    # Try a transaction with explicit split
    sc, b = await call(c, "POST", "/api/v1/transactions/record",
                       json_body={
                           "brand_id": primary_bid,
                           "user_id": customer_uid,
                           "amount_cents": ticket_cents,
                           "items": [{"sku": "cut_and_color", "price_cents": ticket_cents}],
                           "metadata": {
                               "stylist_id": star["stylist_id"],
                               "stylist_commission_cents": int(ticket_cents * star["commission_rate"]),
                               "salon_share_cents": int(ticket_cents * (1 - star["commission_rate"])),
                           },
                           "splits": [
                               {"recipient_id": star["stylist_id"], "recipient_type": "stylist",
                                "amount_cents": int(ticket_cents * star["commission_rate"]),
                                "purpose": "commission"},
                               {"recipient_id": primary_bid, "recipient_type": "brand",
                                "amount_cents": int(ticket_cents * (1 - star["commission_rate"])),
                                "purpose": "salon"},
                           ],
                       })
    if sc == 404:
        gap("P0", "no transaction primitive + no payment split",
            "POST /api/v1/transactions/record 404. There is no native commerce / "
            "transaction primitive that records a ticket + splits attribution to "
            "stylist + salon. Hair salons are PAYMENT MARKETPLACES (stylist gets "
            "40-60%, salon takes rent + tools + supplies). Without split primitives, "
            "the platform cannot offer commission reports, cannot run 'top stylist by "
            "earnings' leaderboards, cannot drive payouts. Stripe Connect / Adyen "
            "ForFee shape is the missing piece — and it's needed for ANY marketplace "
            "merchant (gym PT, hospital specialist, lawyer, masseuse, tutor).")
    elif sc in (200, 201):
        ok("transaction.record with splits", "")

    # Achievements / XP attribution per stylist?
    # Stylist who serves customer 10 times → unlocks 'loyal customer' badge for both
    sc, b = await call(c, "POST", f"/api/v1/primitives/brand/{primary_bid}/achievements",
                       json_body={
                           "id": f"stylist_loyal_customer_{RUN_TAG}",
                           "name": "Loyal Customer Bond",
                           "description": "Customer-stylist pair: 10 visits",
                           "target_metric": "visits_with_regular_stylist",
                           "target_value": 10,
                           "xp_reward": 500,
                       })
    if sc == 200:
        ok("pair achievement defined", "")
        gap("P1", "no pair/bond achievement primitive",
            "Achievement is single-user. A 'customer-stylist bond' achievement (both "
            "sides unlock together) cannot be modelled; same gap shape as buddy referral. "
            "Marketplace dynamics need pair/group achievements.")
    else:
        gap("P1", "pair achievement create", f"{sc} {_short(b)}")


# ── Phase 12: Cross-Salon Visits — customer follows stylist ──────────────
async def phase_12_cross_salon(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("12: Cross-Salon Visits — customer follows stylist between branches")
    master_id = state["master_id"]
    rng = random.Random(RUN_TAG + 12)

    follower_uids = [f"follower_{RUN_TAG}_{i:02d}" for i in range(12)]
    await _setup_consent(c, follower_uids)

    visits = 0
    cross_visits = 0
    for uid in follower_uids:
        salons_visited = rng.sample(SALONS, k=rng.randint(2, 3))
        for s in salons_visited:
            store_id = state["salon_store_ids"].get(s["brand_id"])
            if not store_id:
                continue
            sc, _ = await call(c, "POST", "/api/v1/geofence/visit", json_body={
                "user_id": uid,
                "store_id": store_id,
                "evidence": "qr_scan",
            })
            if sc == 200:
                visits += 1
        if len(salons_visited) > 1:
            cross_visits += 1
    ok("cross-salon activity", f"visits={visits} cross_visitors={cross_visits}/12")

    # Cross-brand visit report — same gap shape as 老周
    sc, b = await call(c, "GET", f"/api/v1/master/{master_id}/cross-brand-visits")
    if sc == 404:
        gap("P0", "no cross-salon visit report",
            "GET /master/{id}/cross-brand-visits 404. 8-salon chain has no consolidated "
            "visit-matrix view (how many 西湖 customers also visited 滨江 after their "
            "stylist transferred?). Customer-follows-stylist is the PRIMARY churn risk "
            "for salon chains — stylist leaves, customer follows. Without this report, "
            "老钱 cannot see the migration patterns until revenue drops.")
    elif sc == 200:
        ok("cross-salon visit report", _short(b, 120))

    # Chain-wide voucher with redemption scope
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": SALONS[0]["brand_id"],
        "name": "Chain-Wide Free Treatment",
        "description": "Redeemable at any 明亮发型 salon",
        "value": {"type": "free_item", "amount": 0, "currency": "CNY"},
        "conditions": {"usage_limit_per_user": 1},
        "expires_in_days": 90,
        "stackable": False,
        "transferable": False,
        "redemption_brand_scope": "all_to_all",  # speculative
    })
    if sc == 201 and isinstance(b, dict):
        tid = b.get("template_id")
        ok("chain-wide voucher template", f"id={tid} (scope may be ignored)")
        sc_i, b_i = await call(c, "POST",
                               f"/api/v1/vouchers/templates/{tid}/issue",
                               json_body={"user_id": follower_uids[0],
                                          "brand_id": SALONS[0]["brand_id"]})
        if sc_i == 201 and isinstance(b_i, dict):
            vid = b_i.get("voucher_id")
            sc_r, b_r = await call(c, "POST",
                                   f"/api/v1/vouchers/{vid}/redeem",
                                   json_body={
                                       "purchase_amount_cents": 0,
                                       "at_brand_id": SALONS[3]["brand_id"],
                                       "redeemer_user_id": follower_uids[0],
                                   })
            if sc_r == 200:
                gap("P1", "no voucher network policy enforcement",
                    "voucher issued at 西湖 was redeemed at 下城 — no policy check. "
                    "Salons get accidental cross-redemption without governance "
                    "(same gap shape as 老周).")
            elif sc_r in (400, 403):
                gap("P0", "voucher network policy missing",
                    f"voucher issued at 西湖 cannot be redeemed at 下城 ({sc_r}). "
                    "No 'all_to_all' or 'master_chain' scope. Chain-wide perks "
                    "require N×N issuance.")


# ── Phase R5: Round 5 capabilities (KiX ID + tier portability + bridge) ──
async def phase_r5_round5(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("R5: Round 5 — KiX ID + time-series + master tier + auction + connect")
    primary_bid = state["primary_bid"]
    master_id = state.get("master_id")

    # KiX ID register
    sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
        "phone": f"+8613700{RUN_TAG % 1000000:06d}",
        "display_name": "明亮VIP-A",
        "primary_language": "zh-CN",
        "source_brand_id": primary_bid,
        "device_fingerprint": f"dev_fp_qian_{RUN_TAG}_a",
        "country": "CN",
    })
    if sc == 200 and isinstance(b, dict) and b.get("kid", "").startswith("kid_"):
        kid_a = b["kid"]
        ok("kix-id register", f"kid={kid_a} is_new={b.get('is_new')}")
    else:
        gap("P0", "kix-id register", f"{sc} {_short(b)}")
        return
    await _setup_consent(c, [kid_a])

    # Master tier portability — diamond status follows customer
    if master_id:
        sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/tier/configure",
                           json_body={
                               "tiers": [
                                   {"name": "guest", "xp_min": 0},
                                   {"name": "regular", "xp_min": 500},
                                   {"name": "silver", "xp_min": 2000},
                                   {"name": "gold", "xp_min": 5000},
                                   {"name": "diamond", "xp_min": 15000},
                               ],
                               "aggregation": "sum",
                           })
        if sc == 200:
            ok("master tier ladder", "5 tiers attached to master")
        else:
            gap("P0", "master tier configure", f"{sc} {_short(b)}")

        sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/tier/promotion-rule",
                           json_body={"rule": "sum_xp_then_tier"})
        if sc == 200:
            ok("master promotion rule", "sum_xp_then_tier")

        # Grant XP at 西湖 only, check tier readable from 滨江
        await call(c, "POST", "/api/v1/primitives/currency/xp/grant", json_body={
            "user_id": kid_a, "brand_id": primary_bid, "amount": 6000,
            "reason": "diamond_progress",
        })
        sc, b = await call(c, "GET", f"/api/v1/master/{master_id}/user/{kid_a}/tier")
        if sc == 200 and isinstance(b, dict):
            tier = b.get("current_master_tier")
            portable = bool(b.get("cross_brand_portability"))
            if tier in ("gold", "diamond", "silver") and portable:
                ok("master tier portable",
                   f"user has '{tier}' readable from any salon, xp={b.get('aggregated_xp')}")
            else:
                gap("P1", "master tier portability",
                    f"tier={tier} portable={portable} xp={b.get('aggregated_xp')}")
        else:
            gap("P0", "master user/tier read", f"{sc} {_short(b)}")

    # Connect grant → token
    sc, b = await call(c, "POST", "/api/v1/kix-id/connect/authorize", json_body={
        "kid": kid_a,
        "brand_id": primary_bid,
        "scopes": ["profile", "email"],
        "redirect_uri": "https://brighthair.cn/callback",
        "state": "qian_test",
    })
    if sc == 200 and isinstance(b, dict) and b.get("code"):
        grant_id, code = b["grant_id"], b["code"]
        ok("connect/authorize", f"grant_id={grant_id[:18]}…")
        sc, t = await call(c, "POST", "/api/v1/kix-id/connect/token", json_body={
            "grant_id": grant_id, "code": code,
            "brand_id": primary_bid, "client_secret": "test_secret",
        })
        if sc == 200 and isinstance(t, dict) and t.get("access_token"):
            ok("connect/token exchanged", f"scopes={t.get('scopes')}")
            try:
                r = await c.get(
                    f"/api/v1/kix-id/{kid_a}/profile-for-merchant/{primary_bid}",
                    headers={"Authorization": f"Bearer {t['access_token']}"},
                )
                if r.status_code == 200:
                    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                    ok("profile-for-merchant scope-filtered", f"keys={list(body.keys())}")
                else:
                    gap("P1", "profile-for-merchant", f"{r.status_code} {r.text[:120]}")
            except Exception as e:
                gap("P1", "profile-for-merchant", repr(e))
        else:
            gap("P1", "connect/token exchange", f"{sc} {_short(t)}")
    else:
        gap("P1", "connect/authorize", f"{sc} {_short(b)}")

    # Auction savings — confirm new_users_only is filtering returning customers
    sc, b = await call(c, "GET", f"/api/v1/auction/admin/savings/{primary_bid}")
    if sc == 200 and isinstance(b, dict):
        skipped = b.get("existing_customers_skipped", 0)
        ok("auction savings", f"skipped={skipped} avg_cpa={b.get('average_cpa_cents')}c "
                              f"(new_users_only protects merchant)")
    else:
        gap("P1", "auction savings", f"{sc} {_short(b)}")

    # Triggers — stylist-related event
    sc, b = await call(c, "POST", "/api/v1/triggers/register", json_body={
        "brand_id": primary_bid,
        "name": "Stylist anniversary — comp treatment",
        "event_type": "attribute_threshold",
        "event_filter": {"attribute_key": "weeks_with_stylist", "threshold": 52},
        "action": {
            "type": "send_push",
            "config": {"title": "周年纪念", "body": "{name}, 与 {stylist_name} 合作一年了，免费护理一次"},
        },
        "cooldown_seconds": 86400,
        "max_fires_per_user": 1,
    })
    if sc == 201:
        ok("triggers/register", "stylist anniversary fan-out")
    else:
        gap("P1", "triggers/register", f"{sc} {_short(b)}")


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
    md.append("# 老钱 / Qian Yu (明亮发型 Bright Hair) — Merchant Journey Findings")
    md.append("")
    md.append(f"**Run tag**: `{RUN_TAG}` | **Runtime**: {runtime:.1f}s | "
              f"**Date**: {time.strftime('%Y-%m-%d %H:%M', time.localtime(start_ts))}")
    md.append("")
    md.append("## Scenario")
    md.append(
        "老钱 owns 「明亮发型」(Bright Hair) — 8 hair salons across Hangzhou "
        "(西湖/拱墅/上城/下城/江干/滨江/萧山/余杭). 60-stylist marketplace "
        "(8 star / 24 senior / 28 junior). Customers pick a favorite stylist and "
        "follow them. Service mix: cut (¥150), color (¥500), perm (¥600), "
        "treatment (¥300), cut_and_color (¥800). 4-6 week haircut cycle, "
        "8-week color cycle. Pain points: no-show 12%, customer-stylist "
        "attachment makes customers follow stylists who switch salons, "
        "member-referral viral loop. Budget ¥15000/月."
    )
    md.append("")
    md.append("**Unique-to-salon dimensions**: STYLIST MARKETPLACE "
              "(resource_id=stylist_id), COMMISSION SPLIT (40-60% to stylist), "
              "PHOTO BEFORE/AFTER history, 4-WEEK CADENCE, STYLE PREFERENCE "
              "TIME-SERIES, CHAIN VIP CARD PORTABILITY.")
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

    section("P0 — Blockers for salon chain use case", p0)
    section("P1 — Friction", p1)
    section("P2 — Nice-to-have", p2)
    section("Hard failures", fails)

    md.append("## Cross-Comparison — What Salons Need vs Prior Sims")
    md.append("")
    md.append(
        "老钱's hair salon chain probes a marketplace + cadence model that prior sims "
        "touched but did not stress:\n"
        "\n"
        "### 1. Reservation resource_id (stylist_id)\n"
        "Submitted resource_id at top-level of `/reservations/create`. 老蔡 "
        "(specialist doctors) flagged the same need. Today resource_id is either "
        "silently dropped or shoved into metadata — neither gives indexed per-resource "
        "stats (no-show rate per stylist, capacity per slot, performance review "
        "data). For salons (60 stylists × 8 branches) this is the single most "
        "important reservation field.\n"
        "\n"
        "### 2. Stylist/Resource primitive\n"
        "No `/primitives/brand/{bid}/resources` API. The platform has merchants, "
        "users, brands — but not 'staff/resource' as a first-class entity. Hair "
        "salons need stylist identity + level + commission rate + calendar + "
        "history queriable by ID. Same gap surfaces in any marketplace merchant "
        "(gym PT, hospital specialist, lawyer, masseuse, tutor).\n"
        "\n"
        "### 3. Payment split / commission marketplace\n"
        "No `/transactions/record` with split semantics. Stylists earn 40-60% "
        "commission; salons take rest for rent + tools. Without split primitives, "
        "platform cannot drive commission reports, leaderboards, or payouts. "
        "Stripe Connect / Adyen ForFee shape is the missing piece. P0 for any "
        "marketplace merchant.\n"
        "\n"
        "### 4. 4-week cadence-based audience\n"
        "No segment-by-attribute (`last_cut_at older_than_days:35`). Lapsed-cohort "
        "definition is THE retention lever — 35-day cycle is the unit of salon "
        "revenue. Auto-rebuild lapsed segment must exist or every campaign is a "
        "pre-computed user_id list (same as 老周 fitness, sharper here).\n"
        "\n"
        "### 5. Photo before/after history\n"
        "No `/assets/upload` and string-valued time-series may be limited. Salon "
        "service IS the visual transformation; storing the trail is foundational "
        "to consultation + marketing + dispute resolution. Today merchant runs "
        "their own CDN. Adjacent merchants: nails, lashes, dental, plastic surgery.\n"
        "\n"
        "### 6. Reactivation campaign objective\n"
        "objective='retention'/'reactivation' rejected. 老周 fitness flagged this; "
        "for hair the pain is sharper because the 4-week cadence makes lapsed "
        "scanning a daily operational need. Today must misuse 'acquire' + lose "
        "auction savings semantics (new_users_only exclusion).\n"
        "\n"
        "### 7. Cross-salon tier portability + chain-wide voucher scope\n"
        "Same chain-merchant gaps as 老周 fitness. Diamond customers at 西湖 are "
        "guests at 余杭; chain-wide perks need N × N issuance.\n"
        "\n"
        "### 8. Pair/bond achievements + buddy mutual referral\n"
        "Customer-stylist bond achievement (both sides unlock together) cannot be "
        "modelled. Mutual referral voucher (both sides get ¥80) requires hand-rolled "
        "dual issue — race conditions are merchant's problem.\n"
    )
    md.append("")

    md.append("## Strategic Recommendations")
    md.append("")
    md.append(
        "1. **[P0] First-class `resource_id` field on reservations** — promote "
        "from metadata to top-level indexed field. Drives per-stylist (and per-doctor / "
        "per-trainer / per-instructor) stats + commission attribution + capacity "
        "tracking. Single most-requested gap across 老周 / 老蔡 / 老钱.\n"
        "\n"
        "2. **[P0] Staff/Resource primitive** — "
        "`POST /primitives/brand/{bid}/resources/register` with id, name, type, "
        "level, commission_rate, calendar reference. Enables marketplace merchants "
        "to identify and query staff as first-class entities.\n"
        "\n"
        "3. **[P0] Transaction + payment split primitive** — "
        "`POST /transactions/record` with `splits=[{recipient_id, recipient_type, "
        "amount_cents, purpose}]`. Drive commission reports + 'top stylist by "
        "earnings' leaderboards + payouts. Foundation for any marketplace merchant.\n"
        "\n"
        "4. **[P0] Attribute-based audience segmentation** — "
        "`POST /audiences/segment` with predicates on `last_cut_at:{older_than_days}`, "
        "`lifecycle_stage`, `regular_stylist_id`. Auto-rebuild cohorts daily.\n"
        "\n"
        "5. **[P0] Reactivation campaign objective** — `objective=reactivation` "
        "or `objective=retention` that targets EXISTING lapsed customers (the "
        "opposite of `new_users_only`). Cadence-based businesses (hair, nails, "
        "dental, lashes, massage) need this as a first-class type.\n"
        "\n"
        "6. **[P0] Cross-brand tier portability** — master-level tier definitions "
        "inherited by attached brands. Chain merchants' #1 expectation: 'my Gold "
        "is Gold everywhere'. (Repeat of 老周 P0.)\n"
        "\n"
        "7. **[P0] Voucher network policy** — `redemption_brand_scope` field on "
        "voucher template ({issuer_only, master_chain, all_to_all}). Chain-wide "
        "perks without N × N issuance. (Repeat of 老周 P0.)\n"
        "\n"
        "8. **[P1] Asset/photo upload primitive** — `POST /assets/upload` + "
        "platform-managed CDN. Salons / nails / dental / plastic surgery merchants "
        "have a visual core; today they run their own bucket.\n"
        "\n"
        "9. **[P1] String-valued attribute time-series** — confirm "
        "`/attributes/{key}/log` accepts string values for `preferred_style` / "
        "`photo_history` / `regular_stylist_id`. Today Round 5 time-series mostly "
        "demoed numeric (weight_kg).\n"
        "\n"
        "10. **[P1] Pair/bond achievement** — atomic achievement that unlocks for "
        "both sides of a relationship (customer + stylist; inviter + invitee).\n"
        "\n"
        "11. **[P1] Auto cadence-based lifecycle** — platform auto-computes "
        "`at_risk` (no visit in cadence_days × 1.5) and `churned` (cadence_days × "
        "3) for every cadence-bound business. Configurable per-brand cadence.\n"
        "\n"
        "12. **[P1] Cross-brand visit report** — `GET /master/{id}/cross-brand-visits` "
        "returning N × N matrix + unique multi-store user count. Salon-chain-specific: "
        "detect 'customer followed stylist to new branch' migration. (Repeat of 老周.)\n"
        "\n"
        "13. **[P1] Push template interpolation** — re-verify "
        "`{name}`/`{stylist_name}`/`{weeks_since_visit}` substitution against "
        "user attributes end-to-end (Round 5 promised this).\n"
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
                state = await phase_1_master_setup(c)
                await phase_2_wallet(c, state)
                await phase_3_consent_tier(c, state)
                await phase_4_reservations(c, state)
                await phase_5_recipe(c, state)
                await phase_6_stylist_attachment(c, state)
                await phase_7_audience(c, state)
                await phase_8_cycle_campaign(c, state)
                await phase_9_geofence_push(c, state)
                await phase_10_photo_history(c, state)
                await phase_11_commission_split(c, state)
                await phase_12_cross_salon(c, state)
                await phase_r5_round5(c, state)
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
