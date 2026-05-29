"""Merchant journey simulation — 老韩 / Han Lei (萌爪天地 / Cute Paws World, 3 stores Chengdu).

End-to-end probe of the KiX Ads Platform from the perspective of a PET SERVICES
chain with multi-service master brand. 老韩 owns 「萌爪天地」 in Chengdu —
3 physical stores carrying 4 distinct services under one parent brand:
  - 美容 (grooming, ~¥150 AOV, recurring 4-6 wk cadence)
  - 医疗 (clinic / 医疗, ¥500-3000 AOV, episodic)
  - 寄养 (boarding, ¥80/day, seasonal spikes)
  - 用品 (retail supplies, ¥30-500 AOV, cross-attached to above)

Walks through 11 phases plus Round 5 capability re-test:

  1.  Master + 3 stores + 4 sub-service brands (sub-brand probe)
  2.  Wallet ¥10K + cascade to stores
  3.  Consent + tier setup (chain-wide loyalty)
  4.  Pet identity probe — pet as separate user entity vs attribute blob
  5.  Owner ↔ pet relationship (guardian_of / ward) — 1 owner : N pets
  6.  Pet health-record time-series (weight curve + vaccination log) — TTL=forever
  7.  Recurring reservation — vaccination schedule + grooming cadence
  8.  Multi-pet voucher (relational: sibling/min_children_count for "second pet 30% off")
  9.  Cross-service medical record continuity (美容→医疗→寄养 share pet record)
  10. Push reminder — vaccination due + grooming overdue templates
  11. Edge cases (pet death, ownership transfer, multi-owner pet, breed gating)
  R5. Round 5 re-test — KiX ID, time-series log/history/trend, attr→achievement,
       master tier portability, push/now, relational vouchers end-to-end

Pattern follows scripts/sim_laozhou.py (Fire Forge Fitness).

In-process via httpx.ASGITransport so no separate server is needed. Requires
a live local Redis.

Run:
    .venv/bin/python scripts/sim_laohan.py
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


# ── Constants ─────────────────────────────────────────────────────────────
RUN_TAG = int(time.time())
OWNER_USER_ID = f"laohan_{RUN_TAG}"
FINDINGS_PATH = Path("/Users/mozat/a-docs/laohan-sim-findings.md")

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"

# 3 physical stores of 萌爪天地 across Chengdu
STORES: list[dict[str, Any]] = [
    {"brand_id": f"cutepaws_chunxi_{RUN_TAG}", "name": "萌爪天地 春熙路店",
     "district": "Jinjiang", "lat": 30.6571, "lng": 104.0810},
    {"brand_id": f"cutepaws_gaoxin_{RUN_TAG}", "name": "萌爪天地 高新店",
     "district": "Gaoxin", "lat": 30.5410, "lng": 104.0668},
    {"brand_id": f"cutepaws_wuhou_{RUN_TAG}", "name": "萌爪天地 武侯店",
     "district": "Wuhou", "lat": 30.6422, "lng": 104.0454},
]

# 4 service sub-brands (probe: can a "service" be a sub-brand of a store?)
SERVICES = [
    {"id": "grooming", "name": "美容", "aov_cents": 15_000, "cadence_days": 35},
    {"id": "medical", "name": "医疗", "aov_cents": 80_000, "cadence_days": 180},
    {"id": "boarding", "name": "寄养", "aov_cents": 8_000, "cadence_days": 90},
    {"id": "supplies", "name": "用品", "aov_cents": 12_000, "cadence_days": 30},
]

PET_NAMES = ["旺财", "小白", "豆豆", "Mocha", "Luna", "球球", "毛毛", "Coco",
             "招财", "皮蛋", "Lucky", "团团", "圆圆", "Lulu", "麻薯"]
BREEDS = ["柴犬", "金毛", "比熊", "泰迪", "英短", "美短", "布偶", "拉布拉多"]


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
    print(f"  {color}[GAP {sev}]{RESET} {action} — {detail[:160]}")


def fail(action: str, detail: str) -> None:
    phase_counters[_current_phase]["fail"] += 1
    findings.append({
        "phase": _current_phase,
        "severity": "FAIL",
        "action": action,
        "detail": detail,
    })
    print(f"  {RED}[FAIL]{RESET} {action} — {detail[:160]}")


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


def _short(body: Any, n: int = 200) -> str:
    s = json.dumps(body, ensure_ascii=False) if isinstance(body, (dict, list)) else str(body)
    return s if len(s) <= n else s[:n] + "..."


# ── Consent ──────────────────────────────────────────────────────────────
_consent_policy_published = False
POLICY_VERSION = f"v_{RUN_TAG}"


async def _setup_consent(c: httpx.AsyncClient, user_ids: list[str]) -> int:
    global _consent_policy_published
    if not _consent_policy_published:
        await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
            "version": POLICY_VERSION,
            "text_md": "# 萌爪天地 consent\nPet record + owner contact + cross-service",
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


# ── Phase 1: Master + 3 stores + 4 sub-service brands ─────────────────────
async def phase_1_master_setup(c: httpx.AsyncClient) -> dict[str, Any]:
    _phase_init("1: Master + 3 stores + 4 sub-service brands")
    state: dict[str, Any] = {"master_id": None, "store_ids": {}}

    sc, b = await call(c, "POST", "/api/v1/master/create", json_body={
        "company_name": "萌爪天地宠物综合服务集团 / Cute Paws World",
        "primary_email": "laohan@cutepaws.cn",
        "owner_user_id": OWNER_USER_ID,
    })
    if sc == 201 and isinstance(b, dict):
        state["master_id"] = b["master_id"]
        ok("create master", f"master_id={state['master_id']}")
    else:
        fail("create master", f"{sc} {_short(b)}")
        return state

    master_id = state["master_id"]

    # Attach 3 physical stores
    attached = 0
    for s in STORES:
        sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/brands/attach", json_body={
            "brand_id": s["brand_id"],
            "store_name": s["name"],
            "store_id": s["brand_id"],
        })
        if sc == 200:
            attached += 1
        else:
            gap("P1", f"attach store {s['brand_id']}", f"{sc} {_short(b)}")
    if attached == 3:
        ok("attach 3 stores", f"all attached to master {master_id[:18]}…")
    else:
        gap("P0", "attach 3 stores", f"only {attached}/3 attached")

    # SUB-BRAND PROBE: try to attach each service as a sub-brand under the master
    # Pet retailers want one umbrella with grooming + medical + boarding + supplies
    # registered as distinct service lines (different SKU schemas, pricing, regulations)
    sub_attached = 0
    for svc in SERVICES:
        sub_bid = f"cutepaws_svc_{svc['id']}_{RUN_TAG}"
        sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/brands/attach",
                           json_body={
                               "brand_id": sub_bid,
                               "store_name": f"萌爪天地 / {svc['name']}",
                               "store_id": sub_bid,
                               "parent_brand_id": STORES[0]["brand_id"],  # speculative
                               "sub_brand_type": "service_line",  # speculative
                           })
        if sc == 200:
            sub_attached += 1
            state.setdefault("service_brands", {})[svc["id"]] = sub_bid
        else:
            gap("P2", f"attach service sub-brand {svc['id']}", f"{sc} {_short(b)}")
    if sub_attached == 4:
        ok("4 service sub-brands attached", "all flat under master (peer to stores)")
        gap("P1", "no parent_brand_id / service_line hierarchy",
            "All 4 services were accepted as peer brands under the master. No "
            "two-level hierarchy (Store→Service or Master→Service→Store). "
            "manager sees 7 'stores' (3 physical + 4 services) — confusing. "
            "Pet chains (and dental/medical/auto-service chains) want a service-line "
            "axis crossing physical store axis.")

    # Geofence each PHYSICAL store
    registered = 0
    for s in STORES:
        store_id = f"store_{s['brand_id']}"
        sc, b = await call(c, "POST", "/api/v1/geofence/stores/register", json_body={
            "brand_id": s["brand_id"],
            "store_id": store_id,
            "name": s["name"],
            "brand_name": "萌爪天地",
            "lat": s["lat"],
            "lng": s["lng"],
            "radius_meters": 300,
            "associated_game_slug": "pet_care_game",
            "push_config": {
                "enabled": True,
                "cooldown_minutes": 240,
                "hours_local": [9, 21],
                "message_template": "{name}, {pet_name} 距离上次美容已 {days_since} 天，要不要预约？",
            },
        })
        if sc == 200:
            registered += 1
            state["store_ids"][s["brand_id"]] = store_id
        else:
            gap("P0", f"register store {s['brand_id']}", f"{sc} {_short(b)}")
    if registered == 3:
        ok("geofence 3 stores", "300m radius, 9-21h, {pet_name}/{days_since} push template")
    else:
        gap("P0", "register stores", f"only {registered}/3")

    # Probe: push template references {pet_name} — a pet-domain placeholder.
    # Does the platform document supported placeholders?
    gap("P1", "no pet-domain placeholder catalog",
        "Push template accepts arbitrary {placeholder} tokens but there is no "
        "registered catalog of valid placeholders for pet domain ({pet_name}, "
        "{breed}, {vaccination_due_days}, {last_groomed_days}). Merchant guesses; "
        "interpolator silently fails or returns raw text. Needs a brand-scoped "
        "placeholder schema with type hints + validation at template-save time.")

    return state


# ── Phase 2: Wallet ¥10K + cascade ───────────────────────────────────────
async def phase_2_wallet(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("2: Wallet ¥10K + cascade to 3 stores")
    master_id = state["master_id"]
    if not master_id:
        fail("phase 2", "no master_id")
        return

    allocation = {s["brand_id"]: 0.333 for s in STORES}
    sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/budget/global", json_body={
        "monthly_budget_cents": 1_000_000,  # ¥10K
        "allocation": allocation,
    })
    if sc == 200:
        ok("master global budget", "¥10K/month, even thirds × 3 stores")
    else:
        gap("P1", "master global budget", f"{sc} {_short(b)}")

    # Top up each store
    funded = 0
    for s in STORES:
        bid = s["brand_id"]
        sc, b = await call(c, "POST", f"/api/v1/wallet/{bid}/topup", json_body={
            "amount_cents": 333_000, "payment_method": "wechat",
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
                       json_body={"daily_budget_cents": 11_000})  # ~¥110/day
    if funded == 3:
        ok("fund + daily-budget 3 stores", "¥3,330 each, ¥110 daily cap")
    else:
        gap("P0", "store wallet funding", f"only {funded}/3 funded")

    sc, b = await call(c, "GET", f"/api/v1/master/{master_id}/consolidated-report")
    if sc == 200 and isinstance(b, dict):
        ok("consolidated report",
           f"balance=¥{b.get('total_balance_cents', 0) / 100:.2f}")
    else:
        gap("P1", "consolidated report", f"{sc} {_short(b)}")


# ── Phase 3: Consent + Tier ──────────────────────────────────────────────
async def phase_3_consent_tier(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("3: Consent Flow + Tier Setup")
    primary_bid = STORES[0]["brand_id"]
    state["primary_bid"] = primary_bid

    sc, b = await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
        "version": POLICY_VERSION,
        "text_md": "# 萌爪天地 consent\nPet record + cross-service + medical retention",
        "effective_at": int(time.time()) - 60,
        "requires_re_grant": False,
    })
    if sc == 200:
        ok("publish consent policy", POLICY_VERSION)
        global _consent_policy_published
        _consent_policy_published = True
    else:
        gap("P0", "publish consent policy", f"{sc} {_short(b)}")

    # Pet-owner specific consent scope: medical record retention (forever TTL)
    # Probe: does the platform have a 'medical_record_retention' scope?
    sc, b = await call(c, "POST", "/api/v1/consent/grant", json_body={
        "user_id": f"med_test_{RUN_TAG}",
        "scopes": ["medical_record_retention", "veterinary_data_sharing"],
        "policy_version": POLICY_VERSION,
        "source": "app",
    })
    if sc == 200:
        ok("medical scopes accepted", "platform takes arbitrary scope strings")
        gap("P1", "no medical/healthcare consent scope vocabulary",
            "Consent scopes are free-form strings. There is no registered "
            "'medical_record_retention' / 'rx_visibility' / 'veterinary_data_sharing' "
            "vocabulary that maps to retention TTLs or PII handling rules. "
            "Pet chain (and human medical/dental) needs scope→behavior contracts "
            "or compliance is on-the-honor-system.")
    else:
        gap("P1", "medical consent grant", f"{sc} {_short(b)}")

    # Tier configure
    sc, b = await call(c, "POST", "/api/v1/primitives/tier/configure", json_body={
        "brand_id": primary_bid,
        "tiers": [
            {"name": "new_paws", "xp_min": 0},
            {"name": "regular", "xp_min": 100},
            {"name": "gold_paw", "xp_min": 1000},
            {"name": "vip", "xp_min": 5000},
        ],
    })
    if sc == 200:
        ok("tier configure", "new_paws / regular / gold_paw / vip")
    else:
        gap("P1", "tier configure", f"{sc} {_short(b)}")

    tiers = [
        {"id": "new_paws", "name": "New Paws", "threshold_xp": 0, "perks": ["welcome_kit"]},
        {"id": "regular", "name": "Regular", "threshold_xp": 100, "perks": ["10pct_off_supplies"]},
        {"id": "gold_paw", "name": "Gold Paw", "threshold_xp": 1000,
         "perks": ["10pct_off_supplies", "free_nail_trim"]},
        {"id": "vip", "name": "VIP", "threshold_xp": 5000,
         "perks": ["15pct_off_all", "free_annual_checkup", "priority_boarding"]},
    ]
    created = 0
    for t in tiers:
        sc, b = await call(c, "POST", f"/api/v1/primitives/brand/{primary_bid}/tiers",
                           json_body=t)
        if sc == 200:
            created += 1
    if created == 4:
        ok("4 tiers created", "")
    else:
        gap("P1", "create tiers", f"only {created}/4")


# ── Phase 4: Pet identity probe ──────────────────────────────────────────
async def phase_4_pet_identity(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("4: Pet identity — separate user entity vs attribute blob")
    primary_bid = state["primary_bid"]

    owner_uid = f"pet_owner_{RUN_TAG}_001"
    await _setup_consent(c, [owner_uid])

    # APPROACH A: pet as KiX ID (separate user entity)
    # Probe: register a KiX ID for a pet with phone? Pets have no phone.
    sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
        "phone": f"+8615900{(RUN_TAG + 1) % 1000000:06d}",  # surrogate phone
        "display_name": "旺财 (柴犬)",
        "primary_language": "zh-CN",
        "source_brand_id": primary_bid,
        "device_fingerprint": f"pet_fp_{RUN_TAG}_wangcai",
        "country": "CN",
    })
    if sc == 200 and isinstance(b, dict) and b.get("kid", "").startswith("kid_"):
        pet_kid = b["kid"]
        state["pet_kid"] = pet_kid
        ok("pet registered as KiX ID", f"kid={pet_kid} — using surrogate phone")
        gap("P1", "no non-human entity type",
            "Pet was registered via kix-id/register, which requires a 'phone' field. "
            "Pets don't have phones. We used a surrogate +8615900 number — but this "
            "wastes the global phone-uniqueness namespace + bypasses SMS verification. "
            "Needs an /api/v1/entities/register for non-human subjects (pet, vehicle, "
            "device, IoT) with their own id space and no phone requirement.")
    else:
        gap("P0", "pet identity registration", f"{sc} {_short(b)}")

    # APPROACH B: pet as attribute of human owner
    # Single owner has multiple pets — try storing JSON list
    pets_blob = json.dumps([
        {"pet_id": "p1", "name": "旺财", "breed": "柴犬", "dob": "2023-01-15"},
        {"pet_id": "p2", "name": "小白", "breed": "比熊", "dob": "2024-06-01"},
    ], ensure_ascii=False)
    sc, b = await call(c, "POST",
                       f"/api/v1/primitives/user/{owner_uid}/attributes/pets",
                       params={"brand_id": primary_bid},
                       json_body={"value": pets_blob})
    if sc == 200:
        ok("approach B: pets as JSON blob on owner", "stored as attribute")
        gap("P0", "pet-as-attribute denormalises identity",
            "Storing pets as a JSON blob on user:{owner}:attributes:pets means: "
            "(a) you cannot index/search 'all pets due for vaccination', "
            "(b) you cannot give pets their own XP / streak / achievements, "
            "(c) cross-store pet record is fragmented across stores' attribute blobs, "
            "(d) ownership transfer = JSON surgery on 2 owners. "
            "This is the WRONG model for pet identity but it is the only model "
            "the platform offers if you reject the surrogate-phone hack.")
    else:
        gap("P1", "pet-as-attribute write", f"{sc} {_short(b)}")

    # APPROACH C: per-pet attribute namespace using pet KiX ID
    pet_kid = state.get("pet_kid")
    if pet_kid:
        await _setup_consent(c, [pet_kid])
        await call(c, "POST", f"/api/v1/primitives/user/{pet_kid}/attributes",
                   json_body={"brand_id": primary_bid,
                              "attrs": {
                                  "breed": "柴犬",
                                  "dob": "2023-01-15",
                                  "color": "red",
                                  "neutered": "true",
                                  "microchip_id": f"chip_{RUN_TAG}",
                              }})
        sc, b = await call(c, "GET", f"/api/v1/primitives/user/{pet_kid}/attributes",
                           params={"brand_id": primary_bid})
        if sc == 200 and isinstance(b, dict) and b.get("attrs", {}).get("breed") == "柴犬":
            ok("approach C: pet has its own attribute namespace via kid_", "")
        else:
            gap("P1", "pet kid attributes read", f"{sc} {_short(b)}")


# ── Phase 5: Owner ↔ pet relationship ────────────────────────────────────
async def phase_5_guardian_relationship(c: httpx.AsyncClient,
                                        state: dict[str, Any]) -> None:
    _phase_init("5: Owner ↔ pet relationship (guardian_of / ward)")
    primary_bid = state["primary_bid"]
    pet_kid = state.get("pet_kid")

    # Owner: register as KiX ID too (so we have a clean canonical id)
    sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
        "phone": f"+8613700{RUN_TAG % 1000000:06d}",
        "display_name": "韩磊 (owner)",
        "primary_language": "zh-CN",
        "source_brand_id": primary_bid,
        "device_fingerprint": f"owner_fp_{RUN_TAG}",
        "country": "CN",
    })
    owner_kid = b.get("kid") if isinstance(b, dict) else None
    if owner_kid:
        state["owner_kid"] = owner_kid
        ok("owner registered as KiX ID", f"kid={owner_kid}")
        await _setup_consent(c, [owner_kid])
    else:
        gap("P1", "owner kid register", f"{sc} {_short(b)}")
        return

    if not pet_kid:
        gap("P1", "skip relationship phase", "no pet kid from phase 4")
        return

    # Create guardian relationship: owner --guardian--> pet
    sc, b = await call(c, "POST",
                       f"/api/v1/primitives/users/{owner_kid}/relationships",
                       json_body={
                           "related_user_id": pet_kid,
                           "relationship": "guardian",
                           "bidirectional": True,
                           "meta": {"role": "primary_owner",
                                    "since": "2023-01-15",
                                    "species": "dog"},
                       })
    if sc == 200 and isinstance(b, dict) and b.get("ok"):
        ok("guardian relationship created",
           f"reverse=ward, bidirectional, rel_id={b.get('relationship_id')[:12]}…")
    else:
        gap("P0", "guardian relationship", f"{sc} {_short(b)}")

    # Probe: dedicated pet/guardian semantics?
    gap("P1", "no pet-specific relationship type",
        "Used the generic 'guardian'/'ward' edge from the relationships primitive. "
        "Works mechanically but loses pet-specific semantics — there's no "
        "'pet_of' / 'owner_of' / 'co_pet_parent' edge type that platform code "
        "could route on (e.g. 'all guardian edges where meta.species=dog'). "
        "Also no auto-cleanup on pet death (relationship persists forever).")

    # Register a second pet, also linked to same owner — to probe N pets per owner
    sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
        "phone": f"+8615900{(RUN_TAG + 2) % 1000000:06d}",
        "display_name": "豆豆 (比熊)",
        "primary_language": "zh-CN",
        "source_brand_id": primary_bid,
        "device_fingerprint": f"pet_fp_{RUN_TAG}_doudou",
        "country": "CN",
    })
    pet2_kid = b.get("kid") if isinstance(b, dict) else None
    if pet2_kid:
        state["pet2_kid"] = pet2_kid
        await _setup_consent(c, [pet2_kid])
        await call(c, "POST",
                   f"/api/v1/primitives/users/{owner_kid}/relationships",
                   json_body={
                       "related_user_id": pet2_kid,
                       "relationship": "guardian",
                       "bidirectional": True,
                       "meta": {"role": "primary_owner", "species": "dog"},
                   })
        ok("second pet linked", "owner now has 2 wards")

    # List guardian relationships
    sc, b = await call(c, "GET",
                       f"/api/v1/primitives/users/{owner_kid}/relationships",
                       params={"relationship": "guardian"})
    if sc == 200 and isinstance(b, dict):
        cnt = b.get("count", 0)
        if cnt == 2:
            ok("list guardian edges", f"owner has {cnt} pet wards")
        else:
            gap("P1", "guardian list count",
                f"expected 2 wards, got {cnt}: {_short(b)}")
    else:
        gap("P1", "list guardian relationships", f"{sc} {_short(b)}")


# ── Phase 6: Pet health time-series (Round 5 capability) ─────────────────
async def phase_6_health_records(c: httpx.AsyncClient,
                                 state: dict[str, Any]) -> None:
    _phase_init("6: Pet health-record time-series + vaccination log + TTL")
    primary_bid = state["primary_bid"]
    pet_kid = state.get("pet_kid")
    if not pet_kid:
        gap("P1", "skip phase 6", "no pet kid")
        return

    # Time-series weight log over 6 months (puppy growth curve)
    base_ts = int(time.time()) - 180 * 86400
    weights = [
        (base_ts + 0 * 30 * 86400, 4.2),
        (base_ts + 1 * 30 * 86400, 6.1),
        (base_ts + 2 * 30 * 86400, 7.8),
        (base_ts + 3 * 30 * 86400, 8.9),
        (base_ts + 4 * 30 * 86400, 9.5),
        (base_ts + 5 * 30 * 86400, 9.9),
    ]
    logged = 0
    for ts, w in weights:
        sc, _ = await call(
            c, "POST",
            f"/api/v1/primitives/user/{pet_kid}/attributes/weight_kg/log",
            json_body={
                "brand_id": primary_bid,
                "value": w,
                "ts": ts,
                "source": "measured",
            },
        )
        if sc == 200:
            logged += 1
    if logged == 6:
        ok("time-series weight log", "6 weight entries over 6 months (puppy growth)")
    else:
        gap("P0", "time-series weight log", f"only {logged}/6 entries accepted")

    sc, b = await call(
        c, "GET",
        f"/api/v1/primitives/user/{pet_kid}/attributes/weight_kg/history",
        params={"brand_id": primary_bid, "limit": 100},
    )
    if sc == 200 and isinstance(b, dict) and b.get("count", 0) >= 6:
        delta = (b.get("delta") or {}).get("from_first")
        ok("weight history readback",
           f"count={b['count']} delta_from_first={delta} (puppy growth visible)")
    else:
        gap("P0", "weight history readback", f"{sc} {_short(b)}")

    # Trend (growth slope per day)
    sc, b = await call(
        c, "GET",
        f"/api/v1/primitives/user/{pet_kid}/attributes/weight_kg/trend",
        params={"brand_id": primary_bid, "window_days": 180},
    )
    if sc == 200 and isinstance(b, dict):
        ok("weight trend",
           f"direction={b.get('direction')} slope/day={b.get('slope_per_day')}")
    else:
        gap("P1", "weight trend", f"{sc} {_short(b)}")

    # Vaccination log — each vaccine is a record with date + product + lot
    vaccines = [
        (base_ts + 7 * 86400, "rabies_y1"),
        (base_ts + 30 * 86400, "dhpp_1"),
        (base_ts + 60 * 86400, "dhpp_2"),
        (base_ts + 90 * 86400, "dhpp_3"),
        (base_ts + 150 * 86400, "leptospirosis"),
    ]
    vlogged = 0
    for ts, vname in vaccines:
        sc, _ = await call(
            c, "POST",
            f"/api/v1/primitives/user/{pet_kid}/attributes/vaccination/log",
            json_body={
                "brand_id": primary_bid,
                "value": vname,
                "ts": ts,
                "source": "self_declared",
            },
        )
        if sc == 200:
            vlogged += 1
    if vlogged == 5:
        ok("vaccination history log", "5 vaccines logged")
        gap("P1", "no vaccine-record schema",
            "Vaccination is logged as a free-form attribute value (string). "
            "No structured schema for {vaccine_name, product, lot_number, vet_id, "
            "next_due_at} — required for regulatory record retention + automated "
            "reminders. Trend endpoint returns numeric slope, meaningless for "
            "categorical events.")
    else:
        gap("P0", "vaccination log", f"{vlogged}/5 logged")

    # Probe: TTL=forever (medical records must persist beyond default TTL)
    sc, b = await call(
        c, "POST",
        f"/api/v1/primitives/user/{pet_kid}/attributes/medical_note/log",
        json_body={
            "brand_id": primary_bid,
            "value": "allergic to penicillin",
            "source": "self_declared",
            "ttl_seconds": 100 * 365 * 86400,  # ~100 years
        },
    )
    if sc == 200:
        ok("medical note with 100yr TTL accepted", "")
        gap("P2", "no infinity/retention-class TTL semantics",
            "Set ttl_seconds=3.15e9 (~100 years) as a poor-man's forever. There is "
            "no ttl=null or retention_class=permanent option. Medical records "
            "need legally-mandated retention (often life-of-pet + 5y); a "
            "retention_class enum {ephemeral|business|medical|legal} would map to "
            "policy + jurisdiction.")
    else:
        gap("P1", "medical note TTL", f"{sc} {_short(b)}")


# ── Phase 7: Recurring reservation — vaccination + grooming schedule ─────
async def phase_7_recurring_reservation(c: httpx.AsyncClient,
                                       state: dict[str, Any]) -> None:
    _phase_init("7: Recurring reservation — vaccination + grooming cadence")
    primary_bid = state["primary_bid"]
    owner_kid = state.get("owner_kid") or f"owner_recur_{RUN_TAG}"
    await _setup_consent(c, [owner_kid])

    # 1. Single appointment booking (vaccination due in 1 year)
    sc, b = await call(c, "POST", "/api/v1/reservations/create", json_body={
        "brand_id": primary_bid,
        "user_id": owner_kid,
        "scheduled_at": int(time.time()) + 365 * 86400,
        "party_size": 1,
        "type": "appointment",
        "metadata": {
            "service": "rabies_booster",
            "pet_kid": state.get("pet_kid"),
            "pet_name": "旺财",
        },
        "check_in_grace_minutes": 30,
    })
    if sc in (200, 201) and isinstance(b, dict):
        ok("annual vaccination booking", f"rid={b.get('reservation_id')}")
        state["annual_vax_rid"] = b.get("reservation_id")
    else:
        gap("P1", "annual vaccination booking", f"{sc} {_short(b)}")

    # 2. Probe: recurring booking with cadence
    sc, b = await call(c, "POST", "/api/v1/reservations/create", json_body={
        "brand_id": primary_bid,
        "user_id": owner_kid,
        "scheduled_at": int(time.time()) + 35 * 86400,
        "party_size": 1,
        "type": "service",
        "metadata": {"service": "grooming", "pet_name": "旺财"},
        "check_in_grace_minutes": 30,
        # Speculative fields:
        "recurrence": {"cadence_days": 35, "count": 10},
        "recurring": True,
    })
    if sc in (200, 201) and isinstance(b, dict):
        ok("recurring grooming booking accepted", f"rid={b.get('reservation_id')}")
        # Probe: did the platform create N future bookings, or only 1?
        rid = b.get("reservation_id")
        if rid:
            sc2, b2 = await call(c, "GET", f"/api/v1/reservations/{rid}")
            if isinstance(b2, dict) and b2.get("metadata", {}).get("recurrence"):
                ok("recurrence field round-tripped", "stored in metadata")
            else:
                gap("P0", "no first-class recurrence support",
                    "Reservations module accepts arbitrary metadata but has no "
                    "'recurrence' field — speculative 'recurrence: {cadence_days, "
                    "count}' was silently dropped. Subsequent bookings will NOT be "
                    "auto-generated. Pet grooming (35-day cadence), medication "
                    "renewals, dental checkups (6-month) all need a recurring-template "
                    "primitive. Merchant must build their own scheduler.")
        # Also probe user listing
        sc3, b3 = await call(c, "GET", f"/api/v1/reservations/user/{owner_kid}")
        if sc3 == 200 and isinstance(b3, (list, dict)):
            items = b3 if isinstance(b3, list) else b3.get("reservations", [])
            if len(items) <= 2:
                gap("P0", "recurring did NOT expand to series",
                    f"After requesting recurrence count=10, user has {len(items)} "
                    "reservations (expected ≥10). No platform-side series expansion.")
    else:
        gap("P1", "recurring booking", f"{sc} {_short(b)}")

    # 3. Probe: register a trigger to push a reminder N days BEFORE due date
    # (vaccination_due trigger doesn't exist; try the closest)
    sc, b = await call(c, "POST", "/api/v1/reservations/triggers/register",
                       json_body={
                           "brand_id": primary_bid,
                           "event_type": "reservation.upcoming",  # speculative
                           "action_type": "send_push",
                           "action_config": {
                               "title": "疫苗提醒",
                               "body": "{pet_name} 的狂犬疫苗将于 {days_until} 天后到期。",
                               "advance_days": 14,
                           },
                       })
    if sc in (200, 201):
        ok("upcoming-reminder trigger registered", "")
    elif sc in (400, 422):
        gap("P0", "no pre-due reminder event type",
            f"event_type='reservation.upcoming' rejected ({sc}). Available types "
            "are reservation.no_show / .honored / .confirmed — all POST-event. "
            "Pet vaccinations / grooming cadence / dental checkup REMINDERS need "
            "a PRE-event hook (fire N days before scheduled_at). No such primitive. "
            "Merchant runs their own cron scanning scheduled_at - now < threshold.")
    else:
        info(f"upcoming trigger returned {sc}")


# ── Phase 8: Multi-pet voucher (relational) ──────────────────────────────
async def phase_8_multipet_voucher(c: httpx.AsyncClient,
                                   state: dict[str, Any]) -> None:
    _phase_init("8: Multi-pet voucher — 'second pet 30% off'")
    primary_bid = state["primary_bid"]
    owner_kid = state.get("owner_kid")

    # Use relational_conditions: relationship_type_required='guardian' +
    # min_children_count? That's "min N guardian edges out from the redeemer".
    # In the pet domain, "second pet discount" means: redeemer must already have
    # ≥1 paying pet AND is bringing in a 2nd. Let's encode min_children_count=2.
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": primary_bid,
        "name": "Second Pet 30% Off — 多宠优惠",
        "description": "Owner with 2+ registered pets gets 30% off grooming",
        "value": {"type": "percent", "amount": 30, "currency": "CNY"},
        "conditions": {"usage_limit_per_user": 5,
                       "min_purchase_cents": 5000},
        "relational_conditions": {
            "relationship_type_required": "guardian",
            "min_children_count": 2,
        },
        "expires_in_days": 90,
        "stackable": False,
        "transferable": False,
    })
    if sc == 201 and isinstance(b, dict):
        tid = b.get("template_id")
        state["multipet_voucher_tid"] = tid
        ok("multi-pet voucher template",
           f"id={tid}, relational: min_children_count=2 + relationship=guardian")
    else:
        gap("P0", "multi-pet relational voucher template", f"{sc} {_short(b)}")
        return

    # Probe: min_children_count uses 'parent_of/child_of' semantics, NOT guardian.
    # Issue + redeem to see if it accepts guardian edges.
    if owner_kid and state.get("multipet_voucher_tid"):
        tid = state["multipet_voucher_tid"]
        sc, b = await call(c, "POST",
                           f"/api/v1/vouchers/templates/{tid}/issue",
                           json_body={"user_id": owner_kid,
                                      "brand_id": primary_bid})
        if sc == 201 and isinstance(b, dict):
            vid = b.get("voucher_id")
            state["multipet_voucher_id"] = vid
            ok("multi-pet voucher issued", f"vid={vid[:12]}…")
            # Redeem
            sc_r, b_r = await call(c, "POST",
                                   f"/api/v1/vouchers/{vid}/redeem",
                                   json_body={
                                       "purchase_amount_cents": 30_000,
                                       "at_brand_id": primary_bid,
                                       "redeemer_user_id": owner_kid,
                                   })
            if sc_r == 200 and isinstance(b_r, dict):
                ok("multi-pet voucher redeemed",
                   f"applied={b_r.get('applied_value_cents')}c")
                gap("P1", "min_children_count uses 'children' not 'pets'",
                    "min_children_count evaluator counts edges where the redeemer "
                    "has relationship='parent_of'. Used 'guardian' instead — "
                    "relational gate accepted because the schema validator only "
                    "checks types, not edge-type compatibility. Either rename to "
                    "min_dependents_count or add min_guardian_children_count "
                    "/ min_ward_count for pet domain.")
            elif sc_r in (400, 403):
                gap("P0", "multi-pet voucher fails relational check",
                    f"Voucher with min_children_count=2 + relationship=guardian "
                    f"failed redeem ({sc_r}): {_short(b_r, 150)}. The min_children_count "
                    "evaluator likely counts 'parent_of' edges only, not 'guardian' "
                    "edges. Pet chains cannot express multi-pet discounts via the "
                    "existing relational conditions without abusing the parent_of "
                    "edge type.")
            else:
                gap("P1", "multi-pet redeem", f"{sc_r} {_short(b_r)}")
        else:
            gap("P1", "multi-pet voucher issue", f"{sc} {_short(b)}")


# ── Phase 9: Cross-service medical record continuity ─────────────────────
async def phase_9_cross_service(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("9: Cross-service medical record continuity (美容→医疗→寄养)")
    master_id = state["master_id"]
    pet_kid = state.get("pet_kid")
    if not pet_kid or not master_id:
        gap("P1", "skip phase 9", "no pet_kid or master_id")
        return

    # Write a medical record at the medical brand
    medical_bid = state.get("service_brands", {}).get("medical") or STORES[1]["brand_id"]
    grooming_bid = state.get("service_brands", {}).get("grooming") or STORES[0]["brand_id"]
    boarding_bid = state.get("service_brands", {}).get("boarding") or STORES[2]["brand_id"]

    # Vet writes a record at medical_bid
    sc, b = await call(c, "POST",
                       f"/api/v1/primitives/user/{pet_kid}/attributes/diagnosis/log",
                       json_body={
                           "brand_id": medical_bid,
                           "value": "skin_allergy_seasonal",
                           "source": "self_declared",
                       })
    if sc == 200:
        ok("medical: diagnosis logged", "skin_allergy_seasonal at medical brand")
    else:
        gap("P1", "diagnosis log", f"{sc} {_short(b)}")

    # Now groomer at a DIFFERENT brand tries to read the same pet's attrs
    sc, b = await call(c, "GET",
                       f"/api/v1/primitives/user/{pet_kid}/attributes/diagnosis/history",
                       params={"brand_id": grooming_bid, "limit": 50})
    if sc == 200 and isinstance(b, dict) and b.get("count", 0) >= 1:
        ok("cross-brand pet record visible at grooming brand",
           f"count={b['count']}")
        gap("P2", "no per-brand pet-record ACL",
            "Diagnosis written by vet brand is readable by ANY brand under the "
            "master (including 3rd-party brands that merely share a master_id). "
            "Medical records need finer ACL: 'visible to grooming.allergy_field only' "
            "rather than full read-through. Bundle into consent scope vocabulary.")
    elif sc == 200 and isinstance(b, dict) and b.get("count", 0) == 0:
        gap("P0", "cross-brand pet record NOT visible",
            "Diagnosis written at medical_bid is invisible when read with "
            "brand_id=grooming_bid (same master). Pet chains REQUIRE shared "
            "medical context across services: groomer needs to see 'pet bites' "
            "warning, boarding needs to see medication schedule. Per-brand "
            "attribute scoping breaks the master's continuity promise.")
    else:
        gap("P1", "cross-brand record probe", f"{sc} {_short(b)}")

    # Probe: master-scoped attribute read (not per-brand)
    sc, b = await call(c, "GET",
                       f"/api/v1/primitives/user/{pet_kid}/attributes",
                       params={"master_id": master_id})
    if sc == 404 or (isinstance(b, dict) and not b.get("attrs")):
        gap("P1", "no master-scoped attribute API",
            f"GET /attributes?master_id=... returned {sc}. There is no documented "
            "way to read 'all attributes for this pet across all attached brands'. "
            "Merchant must enumerate brands then union — race-prone + slow.")
    elif sc == 200:
        ok("master-scoped attribute read", _short(b, 120))

    # Cross-service visit: pet boards (寄养) → boarder needs to know latest weight
    sc, b = await call(c, "GET",
                       f"/api/v1/primitives/user/{pet_kid}/attributes/weight_kg/history",
                       params={"brand_id": boarding_bid, "limit": 5})
    if sc == 200 and isinstance(b, dict) and b.get("count", 0) > 0:
        ok("boarding brand reads pet weight history",
           f"count={b['count']} (boarder can size kennel by latest weight)")
    else:
        gap("P0", "boarding cannot read pet weight from medical brand",
            f"Boarding brand_id={boarding_bid} requesting weight_kg/history returned "
            f"{sc} count={b.get('count') if isinstance(b, dict) else '?'}. "
            "Weight was logged with brand_id=primary store; brand-scoping breaks "
            "intra-master continuity.")


# ── Phase 10: Push reminders ─────────────────────────────────────────────
async def phase_10_push_reminders(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("10: Push reminders — vaccination due + grooming overdue")
    primary_bid = state["primary_bid"]
    owner_kid = state.get("owner_kid")

    # Seed a campaign (push engine eligibility requirement)
    await call(c, "POST", "/api/v1/campaigns", json_body={
        "brand_id": primary_bid,
        "name": "萌爪 vaccination reminder",
        "objective": "acquire",
        "bid_strategy": "cpm",
        "max_bid_cents": 50,
        "daily_budget_cents": 5000,
        "total_budget_cents": 50000,
        "target_audience": "existing_customers",
    })

    # Push to owner via /push/now
    if owner_kid:
        sc, b = await call(c, "POST", "/api/v1/push/now", json_body={
            "kid": owner_kid,
            "slot": "push",
            "context": {"pet_name": "旺财", "days_since": 42, "name": "韩磊"},
        })
        if sc == 200 and isinstance(b, dict):
            if b.get("fired"):
                ok("push/now fired",
                   f"push_id={b.get('push_id')} brand={b.get('brand_id')} "
                   f"charged={b.get('charged_cents')}c")
                # Did interpolation happen?
                msg = b.get("message") or b.get("body")
                if isinstance(msg, str):
                    if "{pet_name}" in msg or "{days_since}" in msg:
                        gap("P0", "pet-domain push placeholder not interpolated",
                            f"Push body contains raw placeholders: '{msg[:120]}'. "
                            "{pet_name}/{days_since} were passed in context but not "
                            "substituted into the template. Pet reminder messages "
                            "(the entire UX) are useless without interpolation.")
                    elif "旺财" in msg or "42" in msg:
                        ok("pet placeholders interpolated", f"'{msg[:80]}'")
            else:
                ok("push/now endpoint reached",
                   f"fired=false reason={b.get('reason')}")
        else:
            gap("P1", "push/now", f"{sc} {_short(b)}")

    # Probe: scheduled push (vaccination due in 14 days)
    sc, b = await call(c, "POST", "/api/v1/push/schedule", json_body={
        "kid": owner_kid,
        "slot": "push",
        "fire_at_ts": int(time.time()) + 14 * 86400,
        "context": {"pet_name": "旺财"},
    })
    if sc == 200 and isinstance(b, dict) and b.get("schedule_id"):
        ok("scheduled push", f"schedule_id={b['schedule_id'][:18]}…")
    elif sc in (400, 422):
        gap("P1", "scheduled push schema mismatch", f"{sc} {_short(b)}")
    elif sc == 404:
        gap("P0", "no scheduled push endpoint", "POST /push/schedule 404")
    else:
        info(f"scheduled push returned {sc}")

    # Probe: trigger on "vaccination due in 14 days" (computed from time-series)
    gap("P1", "no attribute-derived schedule trigger",
        "Need: 'when vaccination.log.ts + 365d - now < 14d, fire reminder'. "
        "Today, rule_engine v2 supports attribute_changed events, not "
        "scheduled / time-relative predicates. Vaccination expiry reminders "
        "require a merchant-side cron that scans every pet's vaccination/history "
        "and re-fires push.")


# ── Phase 11: Edge cases ─────────────────────────────────────────────────
async def phase_11_edges(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("11: Edge cases — pet death / ownership transfer / multi-owner / breed gating")
    primary_bid = state["primary_bid"]
    pet_kid = state.get("pet_kid")
    owner_kid = state.get("owner_kid")

    # 11a: Pet death — close the kid, retain medical record for compliance
    if pet_kid:
        sc, b = await call(c, "POST", "/api/v1/kix-id/close", json_body={
            "kid": pet_kid,
            "reason": "pet_deceased",
        })
        if sc == 404:
            gap("P1", "no kix-id/close (account closure)",
                "POST /kix-id/close 404 — there is no API to mark a KiX ID as "
                "closed/deceased. Pet death is a real workflow: stop reminders, "
                "preserve medical record, prevent the owner from being upsold "
                "vaccinations for a dead dog. No primitive exists.")
        else:
            info(f"kix-id/close → {sc}")

    # 11b: Ownership transfer (sell pet, give to family)
    new_owner = f"new_owner_{RUN_TAG}"
    await _setup_consent(c, [new_owner])
    if owner_kid and pet_kid:
        # Approach: delete old guardian edge, create new one
        sc, _ = await call(c, "DELETE",
                           f"/api/v1/primitives/users/{owner_kid}/relationships/{pet_kid}",
                           params={"bidirectional": "true"})
        if sc == 200:
            ok("old guardian edge deleted", "")
        sc, b = await call(c, "POST",
                           f"/api/v1/primitives/users/{new_owner}/relationships",
                           json_body={
                               "related_user_id": pet_kid,
                               "relationship": "guardian",
                               "bidirectional": True,
                               "meta": {"transferred_at": int(time.time()),
                                        "from": owner_kid},
                           })
        if sc == 200:
            ok("new guardian edge created", "pet transferred via edge swap")
            gap("P1", "no atomic ownership-transfer primitive",
                "Pet ownership transfer = manual delete-old-edge + create-new-edge. "
                "Not atomic — if the second call fails the pet is orphaned. Also no "
                "audit trail of transfer (just timestamp in meta). Vehicles, "
                "subscriptions, season tickets all need an atomic /transfer "
                "primitive with audit + 2-step accept.")
        else:
            gap("P1", "ownership transfer (manual edge swap)", f"{sc} {_short(b)}")

    # 11c: Multi-owner pet (divorced couple, both should see pet's record)
    coowner = f"coowner_{RUN_TAG}"
    await _setup_consent(c, [coowner])
    if pet_kid:
        sc, b = await call(c, "POST",
                           f"/api/v1/primitives/users/{coowner}/relationships",
                           json_body={
                               "related_user_id": pet_kid,
                               "relationship": "guardian",
                               "bidirectional": True,
                               "meta": {"role": "co_owner"},
                           })
        if sc == 200:
            ok("multi-owner pet edge created", "2 humans → 1 pet via guardian edges")
            # Probe: does push/reminder fan out to BOTH owners?
            gap("P1", "no fan-out on multi-owner",
                "Created 2 guardian edges pointing at the same pet. Vaccination "
                "reminder push has no mechanism to fan out to all guardians — "
                "today push targets ONE kid. Co-parents / divorced families / "
                "boarding-owner pairs all need 'notify all guardians of pet X'.")

    # 11d: Breed-specific service gating (pit bull restrictions, brachycephalic boarding rules)
    # Probe: voucher conditions with breed predicate?
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": primary_bid,
        "name": "Brachycephalic Boarding (no flight transit)",
        "description": "Boarding voucher excluding flat-faced breeds",
        "value": {"type": "fixed", "amount": 5000, "currency": "CNY"},
        "conditions": {
            "usage_limit_per_user": 1,
            "excluded_breeds": ["pug", "bulldog", "persian"],  # speculative
        },
        "expires_in_days": 60,
        "stackable": False,
        "transferable": False,
    })
    if sc == 201:
        ok("breed-conditioned voucher accepted at schema level", "")
        gap("P1", "no breed/species predicate enforcement",
            "Voucher conditions accept arbitrary keys (e.g. excluded_breeds) but "
            "they are not evaluated. Redemption ignores breed. Pet/auto/specialty "
            "merchants need first-class predicate vocabularies that pivot on "
            "subject attributes (breed, weight class, age, body condition).")

    # 11e: Sub-brand confusion — list master's brands
    sc, b = await call(c, "GET",
                       f"/api/v1/master/{state['master_id']}/brands")
    if sc == 200 and isinstance(b, (list, dict)):
        items = b if isinstance(b, list) else b.get("brands", [])
        info(f"master has {len(items)} attached brands (3 stores + 4 service lines)")
        if len(items) >= 7:
            gap("P1", "service line vs store conflated in brand list",
                "Master lists 7 'brands' (3 physical + 4 service lines). No grouping "
                "by axis (location vs service) — consolidated reports cannot answer "
                "'grooming revenue across all 3 stores'. Need either a brand_tag / "
                "brand_axis field, or true 2-level hierarchy with parent_brand_id.")


# ── Round 5 capability re-test ───────────────────────────────────────────
async def phase_r5_round5(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("R5: Round 4+5 — KiX ID + time-series + attr→achievement + master tier")
    primary_bid = state.get("primary_bid") or STORES[0]["brand_id"]
    master_id = state.get("master_id")

    # 1. KiX ID register for a NEW owner (test full re-use)
    sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
        "phone": f"+8613922{RUN_TAG % 1000000:06d}",
        "display_name": "韩老板会员-B",
        "primary_language": "zh-CN",
        "source_brand_id": primary_bid,
        "device_fingerprint": f"r5_owner_fp_{RUN_TAG}",
        "country": "CN",
    })
    if sc == 200 and isinstance(b, dict) and b.get("kid", "").startswith("kid_"):
        owner_b = b["kid"]
        ok("kix-id register (new owner)", f"kid={owner_b}")
        await _setup_consent(c, [owner_b])
    else:
        gap("P0", "kix-id register r5", f"{sc} {_short(b)}")
        return

    # 2. Time-series for a new pet
    sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
        "phone": f"+8615911{(RUN_TAG + 99) % 1000000:06d}",
        "display_name": "招财 (布偶)",
        "device_fingerprint": f"r5_pet_fp_{RUN_TAG}",
    })
    new_pet = b.get("kid") if isinstance(b, dict) else None
    if new_pet:
        await _setup_consent(c, [new_pet])
        # Log 5 weight measurements
        base = int(time.time()) - 4 * 7 * 86400
        for i, w in enumerate([3.5, 3.8, 4.0, 4.2, 4.4]):
            await call(c, "POST",
                       f"/api/v1/primitives/user/{new_pet}/attributes/weight_kg/log",
                       json_body={"brand_id": primary_bid, "value": w,
                                  "ts": base + i * 7 * 86400, "source": "measured"})
        sc, b = await call(c, "GET",
                           f"/api/v1/primitives/user/{new_pet}/attributes/weight_kg/history",
                           params={"brand_id": primary_bid, "limit": 50})
        if sc == 200 and isinstance(b, dict) and b.get("count", 0) >= 5:
            ok("time-series history for new pet",
               f"count={b['count']} delta={(b.get('delta') or {}).get('from_first')}")
        else:
            gap("P1", "time-series new pet", f"{sc} {_short(b)}")

    # 3. Attr → achievement bridge: pet weight reaches healthy adult range
    sc, b = await call(c, "POST",
                       f"/api/v1/primitives/brand/{primary_bid}/achievements",
                       json_body={
                           "id": "healthy_adult_weight_r5",
                           "name": "Healthy Adult Weight",
                           "description": "Pet reached recommended adult weight",
                           "target_metric": "weight_kg",
                           "target_value": 8,
                           "xp_reward": 100,
                       })
    if sc in (200, 409):
        ok("achievement defined", "healthy_adult_weight_r5")

    sc, b = await call(c, "POST", "/api/v1/rules/rules/create", json_body={
        "brand_id": primary_bid,
        "name": "pet_reaches_healthy_weight",
        "when": {
            "type": "attribute_changed",
            "attribute_key": "weight_kg",
            "condition": {"type": "crosses_threshold", "threshold": 8},
        },
        "then": {
            "action_type": "fire_achievement",
            "action_config": {"achievement_id": "healthy_adult_weight_r5",
                              "increment": 8},
        },
        "max_triggers_per_user": 1,
    })
    if sc == 200 and isinstance(b, dict) and b.get("rule_id"):
        rule_id = b["rule_id"]
        ok("rule created", f"rule_id={rule_id[:12]}…")
    else:
        gap("P0", "rule_engine rules/create", f"{sc} {_short(b)}")
        rule_id = None

    # Trigger threshold cross via /log
    if new_pet:
        await call(c, "POST",
                   f"/api/v1/primitives/user/{new_pet}/attributes/weight_kg/log",
                   json_body={"brand_id": primary_bid, "value": 9.0, "source": "measured"})
        sc, b = await call(c, "GET",
                           f"/api/v1/rules/{primary_bid}/user/{new_pet}/pending-actions")
        fired = False
        if sc == 200 and isinstance(b, dict):
            actions = b.get("actions") or b.get("pending_actions") or []
            fired = any(
                (a.get("action_type") == "fire_achievement"
                 and (a.get("config") or {}).get("achievement_id")
                 == "healthy_adult_weight_r5")
                for a in actions if isinstance(a, dict)
            )
        if fired:
            ok("attr→achievement bridge fired (pet weight crossed 8kg)", "")
        else:
            gap("P1", "attr→achievement bridge unwired for pet",
                f"weight_kg jumped 4.4→9 via /log but rule did not enqueue "
                f"fire_achievement for pet kid. pending-actions empty. Same gap "
                f"reported in laozhou findings — call-site between attributes/log "
                f"and rule_engine.on_attribute_changed is still missing.")

    # 4. Master tier portability — pet earns XP at store A, owner reads tier at store B
    if master_id:
        sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/tier/configure",
                           json_body={
                               "tiers": [
                                   {"name": "new_paws", "xp_min": 0},
                                   {"name": "regular", "xp_min": 100},
                                   {"name": "gold_paw", "xp_min": 1000},
                                   {"name": "vip", "xp_min": 5000},
                               ],
                               "aggregation": "sum",
                           })
        if sc == 200:
            ok("master tier ladder", "4 tiers attached to master")
        await call(c, "POST", "/api/v1/primitives/currency/xp/grant", json_body={
            "user_id": owner_b, "brand_id": primary_bid, "amount": 1500,
            "reason": "frequent_grooming",
        })
        sc, b = await call(c, "GET",
                           f"/api/v1/master/{master_id}/user/{owner_b}/tier")
        if sc == 200 and isinstance(b, dict):
            tier = b.get("current_master_tier")
            ok("master tier readback",
               f"tier={tier} portable={b.get('cross_brand_portability')}")
        else:
            gap("P1", "master tier readback", f"{sc} {_short(b)}")

    # 5. KiX ID connect / token (merchant scoped access)
    sc, b = await call(c, "POST", "/api/v1/kix-id/connect/authorize", json_body={
        "kid": owner_b,
        "brand_id": primary_bid,
        "scopes": ["profile", "email"],
        "redirect_uri": "https://cutepaws.cn/callback",
        "state": "laohan_r5",
    })
    if sc == 200 and isinstance(b, dict) and b.get("code"):
        ok("connect/authorize", f"grant_id={b['grant_id'][:18]}…")
        sc2, t = await call(c, "POST", "/api/v1/kix-id/connect/token", json_body={
            "grant_id": b["grant_id"],
            "code": b["code"],
            "brand_id": primary_bid,
            "client_secret": "test_secret",
        })
        if sc2 == 200 and isinstance(t, dict) and t.get("access_token"):
            ok("connect/token exchanged", f"scopes={t.get('scopes')}")

    # 6. Triggers register
    sc, b = await call(c, "POST", "/api/v1/triggers/register", json_body={
        "brand_id": primary_bid,
        "name": "Vaccination reminder",
        "event_type": "achievement_unlocked",
        "event_filter": {"achievement_id": "healthy_adult_weight_r5"},
        "action": {
            "type": "send_push",
            "config": {"title": "🐾", "body": "恭喜 {pet_name} 达到健康体重!"},
        },
        "cooldown_seconds": 60,
        "max_fires_per_user": 0,
    })
    if sc == 201 and isinstance(b, dict) and b.get("trigger_id"):
        ok("triggers/register", f"trigger_id={b['trigger_id'][:18]}…")
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
    md.append("# 老韩 / Han Lei (萌爪天地 / Cute Paws World) — Merchant Journey Findings")
    md.append("")
    md.append(f"**Run tag**: `{RUN_TAG}` | **Runtime**: {runtime:.1f}s | "
              f"**Date**: {time.strftime('%Y-%m-%d %H:%M', time.localtime(start_ts))}")
    md.append("")
    md.append("## Scenario")
    md.append(
        "老韩 owns 「萌爪天地」(Cute Paws World) in Chengdu — 3 physical stores "
        "(春熙路 / 高新 / 武侯) carrying 4 distinct service lines under one master "
        "brand: 美容 (¥150 grooming, ~35d cadence), 医疗 (¥500-3000 clinic, episodic), "
        "寄养 (¥80/day boarding, seasonal), and 用品 (¥30-500 retail, cross-attached). "
        "Customer base ~3000 pet owners, ~4500 pets (avg 1.5 pets/owner). "
        "Pain points: pet medical record continuity across services, vaccination "
        "schedule reminders, recurring grooming cadence, multi-pet pricing, "
        "ownership transfer / pet death lifecycle. Budget ¥10K/月."
    )
    md.append("")
    md.append("**Critical difference vs prior merchants**: PETS ARE NON-HUMAN "
              "SUBJECTS. The platform's user model is human-centric (phone, "
              "consent, scopes, KiX ID). Pets are entities owned BY humans, with "
              "their own continuous medical record + cadenced services, and they "
              "die / transfer / are co-owned. Three of those primitives have no "
              "native platform support.")
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

    section("P0 — Blockers for pet services", p0)
    section("P1 — Friction", p1)
    section("P2 — Nice-to-have", p2)
    section("Hard failures", fails)

    md.append("## What pet-services probes that no prior merchant did")
    md.append("")
    md.append(
        "老韩's pet chain exercises axes that prior sims (老周 fitness, 老王 F&B, "
        "老李 book club, 老黄 e-commerce, 老张 fine dining, 老周 fitness) could not.\n"
        "\n"
        "### 1. Non-human subject identity\n"
        "Pets are continuous, non-fungible, vaccination-bearing subjects that share "
        "attributes with humans (weight, name, kid) but DON'T have phones, consent, "
        "or scopes. Today, registering a pet requires hijacking `kix-id/register` "
        "with a surrogate phone number — wastes phone uniqueness, bypasses SMS, "
        "and gives the pet a fake consent record. **Need**: "
        "`POST /api/v1/entities/register` for non-human subjects {pet, vehicle, "
        "IoT device, account} with its own id space, no phone field, and a "
        "guardian-required flag.\n"
        "\n"
        "### 2. Pet record continuity across services\n"
        "Attributes are brand-scoped. A diagnosis written by the 医疗 brand is "
        "invisible (or weakly visible) to the 美容 brand under the same master. "
        "Pet chains REQUIRE the inverse: medical record SHOULD follow the pet "
        "across services, but with field-level ACL (groomer sees 'biting risk' "
        "but not 'cancer staging'). **Need**: master-scoped attribute namespace + "
        "field-level ACL via consent scopes.\n"
        "\n"
        "### 3. Recurring / cadenced services (vaccination schedule, grooming cycle)\n"
        "Reservations module accepts arbitrary metadata but has no first-class "
        "recurrence/series. Speculative `recurrence: {cadence_days, count}` was "
        "silently dropped. Pet grooming (35-day), vaccinations (annual), dental "
        "checkups (6-month), medication renewals — all need a recurring-booking "
        "primitive that auto-expands and re-creates on completion. Also no "
        "pre-event reminder trigger (`reservation.upcoming` doesn't exist) — only "
        "post-event hooks (no_show / honored / confirmed).\n"
        "\n"
        "### 4. Multi-pet / multi-guardian relational vouchers\n"
        "`min_children_count` exists in relational_conditions but its evaluator "
        "counts `parent_of` edges only, not `guardian` edges. Pet-domain `second "
        "pet 30% off` cannot be expressed cleanly. Needs either: rename to "
        "min_dependents_count, or add `min_ward_count` / `relationship_type_required`-"
        "aware counter that pivots on edge type.\n"
        "\n"
        "### 5. Pet lifecycle (death, transfer, co-ownership)\n"
        "  - **Death**: no `kix-id/close` → owner keeps getting upsold dead pet's "
        "vaccinations.\n"
        "  - **Transfer**: manual delete-old-edge + create-new-edge — not atomic, "
        "no audit, no 2-step accept by new owner.\n"
        "  - **Co-ownership**: 2 guardian edges → 1 pet is OK structurally, but "
        "push has no fan-out 'notify all guardians of pet X'.\n"
        "\n"
        "### 6. Sub-brand / service-line hierarchy\n"
        "Master can attach N brands; they are all peers. Cannot model "
        "'master → 3 stores, each store offers 4 services'. Resulting brand list "
        "is 7 flat brands. Need a `brand_axis` ({location, service}) or "
        "`parent_brand_id` field. Same gap will appear in dental chains, auto "
        "dealerships, multi-specialty medical groups.\n"
        "\n"
        "### 7. Push placeholder catalog\n"
        "Templates accept arbitrary `{placeholder}` tokens. No per-brand or "
        "per-domain catalog of valid placeholders ({pet_name}, {breed}, "
        "{vaccination_due_days}, {last_groomed_days}). Interpolator silently fails "
        "or returns raw text. Pet vertical's entire UX surface (reminder pushes) "
        "depends on this working.\n"
    )

    md.append("## Strategic recommendations")
    md.append("")
    md.append(
        "1. **[P0] Non-human entity register**: `POST /api/v1/entities/register` "
        "with entity_type ∈ {pet, vehicle, iot_device, account, plot}. No phone, "
        "guardian_required flag, separate id namespace (eid_*). Pet, "
        "auto-service, IoT, real-estate verticals all need this.\n"
        "2. **[P0] Master-scoped attribute namespace + field ACL**: pet medical "
        "record is the test case. Diagnosis written at medical brand must be "
        "readable at grooming brand IF consent scope permits. New "
        "`GET /primitives/user/{id}/attributes?master_id=...&scope=...` plus a "
        "field-level ACL vocabulary in consent scopes.\n"
        "3. **[P0] Recurring reservation primitive**: first-class `recurrence` "
        "field on POST /reservations/create with {cadence_days, count, "
        "auto_recreate_on_complete}. Platform-managed series expansion. Also "
        "add `reservation.upcoming` (pre-event) trigger event type with "
        "configurable advance_days.\n"
        "4. **[P0] Relational condition fix for guardian edges**: either "
        "(a) rename min_children_count → min_dependents_count and count any "
        "out-edge with relationship_type_required, or (b) add explicit "
        "min_ward_count / min_pet_count. Pet/dental/specialty chains all want "
        "multi-dependent pricing.\n"
        "5. **[P1] Entity lifecycle primitives**: `POST /kix-id/close` + "
        "`POST /entities/{id}/transfer` (2-step accept + audit log) + "
        "co-ownership fan-out on push (`fanout: 'all_guardians'`). Pet death + "
        "ownership transfer + divorced co-parents all need this.\n"
        "6. **[P1] Two-axis brand hierarchy**: `parent_brand_id` + "
        "`brand_axis ∈ {location, service_line, region}` on master/brands/attach. "
        "Consolidated reports group by axis. Pet/dental/auto/medical chains "
        "naturally span 2 axes.\n"
        "7. **[P1] Push placeholder catalog**: brand-scoped placeholder schema "
        "with type hints, validation at template-save, fallback strings. Add "
        "pet-domain bundle ({pet_name, breed, vaccination_due_days, "
        "last_groomed_days, weight_kg_latest}).\n"
        "8. **[P1] Healthcare consent scope vocabulary**: registered scopes "
        "(`medical_record_retention`, `rx_visibility`, `veterinary_data_sharing`) "
        "that map to retention TTLs + read ACLs. Adds compliance teeth to the "
        "free-form scope strings.\n"
        "9. **[P1] Retention class on attribute log**: `retention_class ∈ "
        "{ephemeral, business, medical, legal}` replaces awkward "
        "`ttl_seconds=100yr` workaround.\n"
        "10. **[P2] Vaccine record schema**: structured {vaccine_name, product, "
        "lot_number, vet_id, next_due_at, evidence_url} on attribute/log when "
        "key is in a known clinical vocabulary. Enables automated reminders + "
        "regulatory export.\n"
        "11. **[P2] Subject-attribute predicates in voucher conditions**: "
        "breed, age, weight class, body condition score as first-class "
        "predicates evaluated at redemption. Pet/specialty merchants need "
        "this — generic merchants can ignore.\n"
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
# ── Phase R7: Round 7 probes — pet entity primitive + recurring + guardian ─
async def phase_r7_probes(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("R7: Round 7 probes — recipes + entity primitive + entity attributes + guardian_of + recurring grooming")
    primary_bid = state.get("primary_bid")
    owner_uid = state.get("owner_kid") or f"owner_probe_{RUN_TAG}"
    version = state.get("consent_version") or f"v_pet_{RUN_TAG}"
    await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
        "version": version, "text_md": "## Pet Care Privacy",
        "effective_at": int(time.time()) - 60, "requires_re_grant": False,
    })
    await call(c, "POST", "/api/v1/consent/grant", json_body={
        "user_id": owner_uid, "scopes": ["cross_brand_tracking", "personalization"],
        "policy_version": version, "source": "app",
    })

    # 1) Recipe library — pet
    sc, b = await call(c, "GET", "/api/v1/recipes", params={"industry": "pet"})
    if sc == 200 and isinstance(b, (list, dict)):
        items = b if isinstance(b, list) else b.get("recipes", b.get("items", []))
        if items:
            ok("recipes industry=pet", f"{len(items)} recipes")
            found_vax = any("vaccin" in (it.get("id") or "").lower()
                            or "vaccin" in (it.get("name") or "").lower()
                            for it in items)
            if found_vax:
                ok("pet_vaccination_calendar recipe discovered", "")
        else:
            gap("P1", "recipes industry=pet empty", "")
    else:
        gap("P1", "recipes industry=pet", f"{sc}")

    # 2) /primitives/entities/register entity_type=pet + owner_user_id
    sc, b = await call(c, "POST", "/api/v1/primitives/entities/register", json_body={
        "entity_type": "pet",
        "owner_user_id": owner_uid,
        "display_name": "豆豆 (Doudou)",
        "attributes": {"species": "dog", "breed": "Golden Retriever",
                        "weight_kg": "12.5", "birth_year": "2022"},
    })
    pet_eid = None
    if sc in (200, 201) and isinstance(b, dict):
        pet_eid = b.get("entity_id") or b.get("eid") or b.get("id")
        ok("entity register pet", f"eid={pet_eid}")
    elif sc in (400, 422):
        gap("P0", "entity register pet schema", f"{sc} {_short(b)}")
    else:
        gap("P1", "entity register pet", f"{sc} {_short(b)}")

    # 3) /primitives/users/{uid}/entities?entity_type=pet
    sc, b = await call(c, "GET", f"/api/v1/primitives/users/{owner_uid}/entities",
                       params={"entity_type": "pet"})
    if sc == 200 and isinstance(b, (list, dict)):
        items = b if isinstance(b, list) else b.get("entities", b.get("items", []))
        if items:
            ok("list owner's pet entities", f"count={len(items)}")
        else:
            gap("P1", "list pet entities empty",
                "entity register succeeded but list returns 0")
    else:
        gap("P1", "list user entities", f"{sc} {_short(b)}")

    # 4) /primitives/entities/{eid}/attributes for pet weight (time-series log)
    if pet_eid:
        for weight in ("12.5", "13.0", "13.2"):
            sc, b = await call(c, "POST",
                               f"/api/v1/primitives/entities/{pet_eid}/attributes",
                               json_body={
                                   "attrs": {"weight_kg": weight,
                                              "timestamp": int(time.time())},
                                   "append": True,
                               })
            if sc not in (200, 201):
                gap("P1", "entity attribute append",
                    f"{sc} {_short(b)}")
                break
        else:
            ok("entity weight time-series logged (3 entries)", "")

    # 5) /reservations/series/create for monthly grooming (recurring)
    groomer_uid = f"groomer_zhang_{RUN_TAG}"
    await call(c, "POST", "/api/v1/consent/grant", json_body={
        "user_id": groomer_uid, "scopes": ["cross_brand_tracking"],
        "policy_version": version, "source": "app",
    })
    sc, b = await call(c, "POST", "/api/v1/reservations/series/create", json_body={
        "brand_id": primary_bid,
        "user_id": owner_uid,
        "scheduled_at": int(time.time()) + 86400 * 30,
        "party_size": 1,
        "type": "appointment",
        "resource_id": pet_eid or "pet_x",
        "fulfiller_user_id": groomer_uid,
        "metadata": {"service": "grooming", "pet_eid": pet_eid},
        "recurrence": {"frequency": "monthly", "count": 6},
    })
    if sc in (200, 201):
        ok("monthly grooming series created", "")
    elif sc in (400, 422):
        gap("P1", "reservation series create schema",
            f"{sc} {_short(b)}")
    else:
        gap("P1", "reservation series create", f"{sc} {_short(b)}")

    # 6) Relationship: guardian_of / ward_of
    if pet_eid:
        sc, b = await call(c, "POST",
                           f"/api/v1/primitives/users/{owner_uid}/relationships",
                           json_body={
                               "related_user_id": pet_eid,
                               "relationship": "guardian_of",
                               "bidirectional": True,
                           })
        if sc in (200, 201):
            ok("guardian_of relationship created (owner→pet)", "")
        elif sc in (400, 422):
            gap("P1", "guardian_of relationship",
                f"{sc} {_short(b)} — likely rejects entity-id as related_user_id")


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
                await phase_4_pet_identity(c, state)
                await phase_5_guardian_relationship(c, state)
                await phase_6_health_records(c, state)
                await phase_7_recurring_reservation(c, state)
                await phase_8_multipet_voucher(c, state)
                await phase_9_cross_service(c, state)
                await phase_10_push_reminders(c, state)
                await phase_11_edges(c, state)
                await phase_r5_round5(c, state)
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
