"""Merchant journey simulation — 老沈 / Shen Lan (素颜医美 Bare Beauty Aesthetics).

End-to-end probe of the KiX Ads Platform from the perspective of a Shanghai
mid-tier medical aesthetics chain (4 clinics, 1200 members). Probes things
unique to AESTHETIC MEDICINE that distinguish it from 老蔡's hospital sim:

  * **Medical advertising compliance** — 医疗广告法 forbids cure/diagnosis
    claims, before/after photos are sensitive (require model release + age
    proof), promises of efficacy are illegal. Probe whether creative-gen /
    recipes can flag "medical advertising" rules at all.
  * **Before-after photo storage** — sensitive imagery class. Test if there
    is a media_class=medical_sensitive routing.
  * **Treatment-interval enforcement** — pushing a Botox top-up 2 weeks after
    last injection is medically wrong (interval is 3–4 months). Probe whether
    push frequency cap supports a per-treatment-type cooldown driven by
    user_attributes time-series.
  * **Female-only audience** — 80% female customer base; some products are
    female-exclusive (HIFU, breast, OB). Probe gender-based targeting.
  * **Referral viral via women's groups** — friend's *first treatment payment*
    triggers referrer voucher (delayed conversion, not signup). Probe Round-4
    voucher relational predicates with conversion-event conditions.
  * **Doctor / injector binding** — repeat customers want the same hand;
    test resource_id persistence for doctor_id (carries from 老蔡's gap list).
  * **Out-of-pocket only** — aesthetic medicine is NOT insurance-covered in
    CN. No payor/insurance metadata needed (negative finding vs hospital).
  * **Group-buying / 团购** — popular hyaluronic packages priced via 拼团.
    Probe group-buy primitive.
  * **Long re-treatment cycle** — laser at 6mo, HA at 4–6mo, surgical at 1y+.
    attribution_window_days = 180 minimum.

Uses Round 4 + 5 features explicitly:
  * KiX ID for users (`/kix-id/register`)
  * User attributes time-series with TTL (treatment history)
  * Push engine with consent (marketing scope) → /push/now
  * target_audience=new_users_only auctions
  * Voucher relational predicates (referral chain)
  * attribution_window_days=180 (long re-treatment cycles)

Run:
    .venv/bin/python scripts/sim_laoshen.py
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
OWNER_USER_ID = f"laoshen_{RUN_TAG}"
MASTER_BRAND_ID = f"bare_beauty_{RUN_TAG}"
CLINIC_SLUGS = [
    ("jingan",  "素颜医美 — 静安旗舰店"),
    ("xuhui",   "素颜医美 — 徐汇店"),
    ("pudong",  "素颜医美 — 浦东陆家嘴店"),
    ("hongqiao","素颜医美 — 虹桥店"),
]
FINDINGS_PATH = Path("/Users/mozat/a-docs/laoshen-sim-findings.md")

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"

# Shanghai Jing'an
LAT = 31.2304
LNG = 121.4737

INJECTORS = [
    {"doctor_id": f"dr_zhao_{RUN_TAG}",  "name": "Dr. 赵主任", "specialty": "injectables",   "fee_cents": 80000},   # ¥800 base
    {"doctor_id": f"dr_qian_{RUN_TAG}",  "name": "Dr. 钱医生", "specialty": "laser",         "fee_cents": 50000},
    {"doctor_id": f"dr_sun_{RUN_TAG}",   "name": "Dr. 孙主任", "specialty": "surgical",      "fee_cents": 300000},  # ¥3000 consult
    {"doctor_id": f"dr_li_{RUN_TAG}",    "name": "Dr. 李医生", "specialty": "skin_management","fee_cents": 30000},
]

TREATMENTS = [
    {"sku": "ha_botulinum_50u",   "name": "保妥适 50单位",       "price_cents":  280000, "interval_days": 120, "category": "injectable"},
    {"sku": "ha_juvederm_1cc",    "name": "乔雅登 1cc",          "price_cents":  480000, "interval_days": 180, "category": "injectable"},
    {"sku": "laser_picosure",     "name": "皮秒激光全脸",        "price_cents":  380000, "interval_days": 180, "category": "laser"},
    {"sku": "laser_thermage",     "name": "热玛吉 FLX 600发",   "price_cents": 1800000, "interval_days": 365, "category": "laser"},
    {"sku": "surgical_eyelid",    "name": "双眼皮成型术",        "price_cents": 1500000, "interval_days": 999, "category": "surgical"},
    {"sku": "surgical_rhino",     "name": "鼻综合整形",          "price_cents": 6000000, "interval_days": 999, "category": "surgical"},
    {"sku": "hifu_face",          "name": "Ulthera 超声刀面部",  "price_cents":  900000, "interval_days": 365, "category": "energy"},
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
async def call(c: httpx.AsyncClient, method: str, path: str, *,
               json_body: Any = None, params: dict | None = None,
               headers: dict | None = None) -> tuple[int, Any]:
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


async def _setup_consent(c: httpx.AsyncClient, user_ids: list[str], version: str,
                         scopes: list[str] | None = None) -> int:
    granted = 0
    scopes = scopes or ["cross_brand_tracking", "personalization", "marketing", "geo_lbs"]
    for uid in user_ids:
        sc, _ = await call(c, "POST", "/api/v1/consent/grant", json_body={
            "user_id": uid, "scopes": scopes,
            "policy_version": version, "source": "app",
        })
        if sc == 200:
            granted += 1
    return granted


# ── Phase 1: Master + 4 Clinics + medical-aesthetics industry probe ──────
async def phase_1_master(c: httpx.AsyncClient) -> dict[str, Any]:
    _phase_init("1: Master + 4 Clinics + medical_aesthetics industry probe")
    state: dict[str, Any] = {"master_id": None, "sub_brands": {}}

    sc, b = await call(c, "POST", "/api/v1/master/create", json_body={
        "company_name": "素颜医美 Corp / Bare Beauty Aesthetics",
        "primary_email": "laoshen@bare-beauty.cn",
        "owner_user_id": OWNER_USER_ID,
    })
    if sc == 201 and isinstance(b, dict):
        state["master_id"] = b["master_id"]
        ok("create master account", f"master_id={state['master_id']}")
    else:
        fail("create master account", f"status={sc} body={_short(b)}")
        return state

    master_id = state["master_id"]

    attached = 0
    for slug, name in CLINIC_SLUGS:
        bid = f"bare_beauty_{slug}_{RUN_TAG}"
        state["sub_brands"][slug] = bid
        sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/brands/attach", json_body={
            "brand_id": bid, "store_name": name, "store_id": bid,
        })
        if sc == 200:
            attached += 1
        else:
            gap("P1", f"attach clinic {slug}", f"{sc} {_short(b)}")
    if attached == len(CLINIC_SLUGS):
        ok("attach 4 clinics", "Jing'an / Xuhui / Pudong / Hongqiao")

    # Probe: medical_aesthetics industry recipes
    sc, b = await call(c, "GET", "/api/v1/recipes",
                       params={"industry": "medical_aesthetics"})
    if sc == 200 and isinstance(b, (list, dict)):
        items = b if isinstance(b, list) else b.get("recipes", b.get("items", []))
        if items:
            ok("recipes industry=medical_aesthetics", f"{len(items)} match")
        else:
            gap("P0", "no medical_aesthetics recipes seeded",
                "?industry=medical_aesthetics returns 0 entries. Aesthetic clinics fall back to "
                "starbucks_loyalty — wrong copy, wrong mechanic, and CRITICALLY illegal under "
                "医疗广告法 (cure/efficacy/before-after claims forbidden). Recipe seed must "
                "include compliance-aware copy (no '保证', no '效果', no diagnosis claims).")
    else:
        gap("P1", "recipe industry filter", f"{sc} {_short(b)}")

    # Probe: industry field on master / brand
    sc, b = await call(c, "GET", f"/api/v1/master/{master_id}")
    if sc == 200 and isinstance(b, dict):
        if b.get("industry") in ("medical_aesthetics", "aesthetic_medicine"):
            ok("master industry field", f"vertical={b.get('industry')}")
        else:
            gap("P1", "master.industry not declarative",
                "GET /master/{id} has no `industry`/`vertical` field. Compliance filters cannot "
                "route on industry=medical_aesthetics → 医疗广告法 rule pack not auto-applied.")

    # Probe: medical-advertising compliance rule pack
    sc, b = await call(c, "GET", "/api/v1/compliance/rules",
                       params={"industry": "medical_aesthetics", "jurisdiction": "CN"})
    if sc == 200 and isinstance(b, (list, dict)):
        items = b if isinstance(b, list) else b.get("rules", [])
        if items:
            ok("compliance rules for medical_aesthetics", f"{len(items)} rules")
        else:
            gap("P0", "medical-advertising compliance pack missing",
                "GET /compliance/rules?industry=medical_aesthetics returns no rule set. "
                "医疗广告法 has 30+ banned-phrase classes ('保证安全', '永久', '彻底治愈', "
                "before/after photos without disclaimer). Without a rule pack the creative-gen "
                "produces non-compliant copy that gets the clinic's ad license revoked.")
    elif sc == 404:
        gap("P0", "no /compliance/rules endpoint",
            "GET /compliance/rules 404. The platform has no compliance-rule registry at all. "
            "Medical aesthetics, pharmaceutical, financial all need industry-specific banned-"
            "phrase + mandatory-disclaimer enforcement at creative-gen time.")
    else:
        gap("P1", "compliance rules endpoint", f"{sc} {_short(b)}")

    return state


# ── Phase 2: Wallet ¥40K/month ──────────────────────────────────────────
async def phase_2_wallet(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("2: Wallet ¥40K/month + clinic budget cascade")
    master_id = state["master_id"]
    if not master_id:
        return

    allocation = {bid: round(1.0 / len(CLINIC_SLUGS), 4)
                  for bid in state["sub_brands"].values()}
    deficit = 1.0 - sum(allocation.values())
    first_bid = next(iter(allocation))
    allocation[first_bid] = round(allocation[first_bid] + deficit, 4)

    sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/budget/global", json_body={
        "monthly_budget_cents": 4_000_000,  # ¥40K
        "allocation": allocation,
    })
    if sc == 200:
        ok("set master global budget", "¥40K/month across 4 clinics")
    else:
        gap("P1", "set master global budget", f"{sc} {_short(b)}")

    # Topup the flagship Jing'an clinic
    primary_bid = state["sub_brands"]["jingan"]
    state["primary_bid"] = primary_bid
    sc, b = await call(c, "POST", f"/api/v1/wallet/{primary_bid}/topup", json_body={
        "amount_cents": 2_000_000, "payment_method": "wechat",
    })
    if sc == 200 and isinstance(b, dict) and "topup_id" in b:
        tid = b["topup_id"]
        sc2, _ = await call(c, "POST",
                            f"/api/v1/wallet/{primary_bid}/topup/{tid}/confirm",
                            json_body={"payment_gateway_response": {"mock": True}})
        if sc2 == 200:
            ok("topup Jing'an clinic ¥20K + confirm", "")
        else:
            gap("P1", "confirm topup", f"{sc2}")
    else:
        gap("P1", "topup", f"{sc} {_short(b)}")


# ── Phase 3: Consent + medical/photo scopes + tiers ──────────────────────
async def phase_3_consent(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("3: Consent (medical/photo scopes) + Member Tiers")
    primary_bid = state["primary_bid"]

    sc, b = await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
        "version": f"v_{RUN_TAG}",
        "text_md": (
            "# 素颜医美 隐私政策\n"
            "本政策涵盖医美病历存储、术前术后照片(before/after)、营销推广。"
            "符合医疗广告法+个保法+网络安全法三重合规。"
        ),
        "effective_at": int(time.time()) - 60,
        "requires_re_grant": False,
    })
    if sc == 200:
        ok("publish consent policy", f"version=v_{RUN_TAG}")
    else:
        gap("P0", "publish consent policy", f"{sc} {_short(b)}")
        return
    state["consent_version"] = f"v_{RUN_TAG}"

    # Probe: medical_data scope
    sc, b = await call(c, "POST", "/api/v1/consent/grant", json_body={
        "user_id": f"med_probe_{RUN_TAG}", "scopes": ["medical_data"],
        "policy_version": f"v_{RUN_TAG}", "source": "app",
    })
    if sc == 200:
        ok("medical_data consent scope accepted", "")
    elif sc in (400, 422):
        gap("P0", "medical_data consent scope missing",
            f"{sc} {_short(b)} — consent.VALID_SCOPES rejects medical_data. Aesthetic clinics "
            "cannot collect informed consent for medical-record storage separately from "
            "marketing. 老蔡 already flagged this; medical aesthetics inherits the gap "
            "with extra urgency (before/after photos are a category of PHI).")

    # Probe: before_after_photo scope — UNIQUE TO AESTHETICS
    sc, b = await call(c, "POST", "/api/v1/consent/grant", json_body={
        "user_id": f"photo_probe_{RUN_TAG}", "scopes": ["before_after_photo"],
        "policy_version": f"v_{RUN_TAG}", "source": "app",
    })
    if sc == 200:
        ok("before_after_photo scope accepted", "AESTHETICS-SPECIFIC scope wired")
    elif sc in (400, 422):
        gap("P0", "before_after_photo scope missing",
            f"{sc} {_short(b)} — consent.VALID_SCOPES has no `before_after_photo`. Aesthetic "
            "clinics need explicit consent (with model release + age verification) to store + "
            "use B/A photos. Mixing this consent into `personalization` is a legal trap: "
            "patient revoking marketing consent does NOT revoke photo use, but today the "
            "platform can't distinguish.")

    # Probe: marketing_medical scope (variant for medical-advertising consent)
    sc, b = await call(c, "POST", "/api/v1/consent/grant", json_body={
        "user_id": f"mm_probe_{RUN_TAG}", "scopes": ["marketing_medical"],
        "policy_version": f"v_{RUN_TAG}", "source": "app",
    })
    if sc == 200:
        ok("marketing_medical scope accepted", "")
    elif sc in (400, 422):
        gap("P1", "marketing_medical scope missing",
            "no separate scope for medical-advertising marketing (vs generic marketing). "
            "医疗广告法 requires patients to opt-in to MEDICAL advertising distinctly.")

    # Member tier ladder
    sc, b = await call(c, "POST", "/api/v1/primitives/tier/configure", json_body={
        "brand_id": primary_bid,
        "tiers": [
            {"name": "new",        "xp_min": 0},
            {"name": "regular",    "xp_min": 50_000},    # ≥ ¥500
            {"name": "vip",        "xp_min": 500_000},   # ≥ ¥5K
            {"name": "diamond",    "xp_min": 5_000_000}, # ≥ ¥50K
        ],
    })
    if sc == 200:
        ok("configure tier ladder", "new / regular / vip / diamond")
    else:
        gap("P1", "configure tier", f"{sc} {_short(b)}")


# ── Phase 4: Members + female-only audience ──────────────────────────────
async def phase_4_audience(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("4: 50 Members + Female-only audience targeting")
    primary_bid = state["primary_bid"]
    ver = state["consent_version"]
    rng = random.Random(RUN_TAG + 4)

    # Build 50 members (80% female mirrors the real customer base)
    members: list[str] = []
    for i in range(50):
        uid = f"member_{i:03d}_{RUN_TAG}"
        members.append(uid)
    granted = await _setup_consent(c, members, ver)
    ok(f"50 members + consent", f"{granted}/50 granted")
    state["members"] = members

    # Assign gender attribute (80% female)
    sex_set = 0
    for i, uid in enumerate(members):
        sex = "F" if i < 40 else "M"
        sc, _ = await call(c, "POST",
                           f"/api/v1/primitives/user/{uid}/attributes",
                           json_body={
                               "brand_id": primary_bid,
                               "attrs": {"sex": sex, "age": rng.randint(25, 45)},
                           })
        if sc in (200, 201):
            sex_set += 1
    if sex_set == 50:
        ok("set sex + age attributes", "80% F / 20% M, age 25–45")
    else:
        gap("P1", "set demographic attrs", f"only {sex_set}/50 stored")

    # Female-only custom audience
    sc, b = await call(c, "POST", "/api/v1/audiences/custom/create", json_body={
        "brand_id": primary_bid,
        "name": "Female 25–45 (medical aesthetics core)",
        "source": "manual",
        "predicates": [
            {"attribute": "sex", "op": "eq", "value": "F"},
            {"attribute": "age", "op": "between", "value": [25, 45]},
        ],
        "predicate_logic": "AND",
    })
    if sc == 200 and isinstance(b, dict):
        audience_id = b.get("audience_id") or b.get("id")
        if audience_id:
            ok("female 25–45 audience created", f"audience_id={audience_id}")
            state["female_audience_id"] = audience_id
        else:
            gap("P1", "female audience id missing", f"body={_short(b)}")
    elif sc in (400, 422):
        gap("P0", "gender-attribute audience predicate rejected",
            f"{sc} {_short(b)} — audiences.custom.create cannot filter by sex/gender "
            "attribute. Medical aesthetics is 80% female with female-exclusive treatments "
            "(HIFU breast, OB-aesthetic). Without gender targeting, ads waste 50% of "
            "impressions on the wrong demographic.")

    # Probe: female-exclusive product gate (some procedures legally restricted by sex)
    sc, b = await call(c, "GET", f"/api/v1/audiences/{state.get('female_audience_id', 'none')}/size")
    if sc == 200 and isinstance(b, dict):
        size = b.get("size") or b.get("member_count")
        if size and 35 <= size <= 45:
            ok("female audience size correct", f"size={size} (~40 expected)")
        else:
            gap("P1", "female audience size off",
                f"expected ~40 (80% of 50), got {size}")
    elif sc == 404 and state.get("female_audience_id"):
        gap("P1", "audience size endpoint missing",
            "GET /audiences/{aid}/size 404 — can't verify the audience materialized.")


# ── Phase 5: KiX ID + treatment history time-series (Round 5) ────────────
async def phase_5_kix_id_history(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("5: KiX ID register + treatment history time-series (Round 5)")
    primary_bid = state["primary_bid"]

    # Register a flagship KiX ID member
    sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
        "phone": f"+8613900{RUN_TAG % 1000000:06d}",
        "display_name": "素颜会员A",
        "primary_language": "zh-CN",
        "source_brand_id": primary_bid,
        "device_fingerprint": f"dev_fp_{RUN_TAG}_a",
        "country": "CN",
    })
    if sc == 200 and isinstance(b, dict) and b.get("kid", "").startswith("kid_"):
        kid_a = b["kid"]
        ok("kix-id register flagship member", f"kid={kid_a} is_new={b.get('is_new')}")
        state["kid_a"] = kid_a
    else:
        gap("P0", "kix-id register", f"{sc} {_short(b)}")
        return

    await _setup_consent(c, [kid_a], state["consent_version"])

    # Log treatment history time-series (last 4 botox sessions, every ~4mo)
    base_ts = int(time.time()) - 365 * 86400
    treatment_events = [
        (base_ts + 0,         "ha_botulinum_50u", 280000),
        (base_ts + 120*86400, "ha_botulinum_50u", 280000),
        (base_ts + 240*86400, "laser_picosure",   380000),
        (base_ts + 330*86400, "ha_botulinum_50u", 280000),
    ]
    logged = 0
    for ts, sku, price in treatment_events:
        sc, _ = await call(c, "POST",
                           f"/api/v1/primitives/user/{kid_a}/attributes/last_treatment_sku/log",
                           json_body={
                               "brand_id": primary_bid,
                               "value": sku,
                               "ts": ts,
                               "source": "clinic_pos",
                               "ttl_seconds": 86400 * 730,  # 2-year retention
                               "metadata": {"price_cents": price, "doctor_id": INJECTORS[0]["doctor_id"]},
                           })
        if sc == 200:
            logged += 1
    if logged == 4:
        ok("treatment history time-series", "4 entries, TTL=730d")
    else:
        gap("P1", "treatment history log",
            f"only {logged}/4 logged — time-series /attributes/{{key}}/log may not exist "
            "or doesn't accept ts/ttl_seconds. Aesthetic merchants must store treatment "
            "history with TTL aligned to 病历保存年限.")

    # Read history with delta
    sc, b = await call(c, "GET",
                       f"/api/v1/primitives/user/{kid_a}/attributes/last_treatment_sku/history",
                       params={"brand_id": primary_bid, "limit": 50})
    if sc == 200 and isinstance(b, dict) and b.get("count", 0) >= 1:
        ok("treatment history readback", f"count={b['count']}")
        # Probe: most-recent-by-category filter (needed for interval enforcement)
        sc2, b2 = await call(c, "GET",
                             f"/api/v1/primitives/user/{kid_a}/attributes/last_treatment_sku/history",
                             params={"brand_id": primary_bid,
                                     "metadata_filter": "category=injectable",
                                     "limit": 1})
        if sc2 == 200:
            ok("history filter by metadata.category", "")
        elif sc2 in (400, 422):
            gap("P1", "history metadata filter missing",
                "/attributes/{key}/history rejects metadata_filter param. Interval enforcement "
                "needs 'most recent INJECTABLE for this user' query — without metadata filter, "
                "merchant pulls all history client-side. O(N) per push decision.")
    else:
        gap("P1", "treatment history readback", f"{sc} {_short(b)}")


# ── Phase 6: Before/After photo storage probe ────────────────────────────
async def phase_6_photo_storage(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("6: Before/After Photo Storage — sensitive media class")
    primary_bid = state["primary_bid"]
    kid_a = state.get("kid_a", "no_kid")

    # Probe 1: media upload with sensitivity flag
    paths_tried = []
    media_ok = False
    for path in (
        "/api/v1/media/upload",
        "/api/v1/primitives/media/upload",
        f"/api/v1/brands/{primary_bid}/media",
    ):
        sc, b = await call(c, "POST", path, json_body={
            "brand_id": primary_bid,
            "user_id": kid_a,
            "media_type": "image",
            "media_class": "medical_sensitive",   # before/after class
            "purpose": "before_after_comparison",
            "filename": "ba_botox_2026.jpg",
            "consent_token": f"consent_{kid_a}_v_{RUN_TAG}",
            "treatment_sku": "ha_botulinum_50u",
            "stage": "before",
        })
        paths_tried.append((path, sc))
        if sc in (200, 201):
            media_ok = True
            ok("medical_sensitive media upload accepted", f"via {path}")
            break
    if not media_ok:
        gap("P0", "no medical-sensitive media storage primitive",
            f"tried {len(paths_tried)} paths, none accepted: {paths_tried}. Before/after "
            "photos are the #1 conversion driver in aesthetics. There's no media-upload "
            "endpoint, no media_class classification, no consent-token binding. Clinic "
            "uploads to merchant-side S3 → platform never knows the photo exists → can't "
            "auto-apply 医疗广告法 disclaimers when surfacing in creative.")

    # Probe 2: model-release / age verification
    sc, b = await call(c, "POST",
                       f"/api/v1/primitives/user/{kid_a}/legal-documents",
                       json_body={
                           "brand_id": primary_bid,
                           "doc_type": "model_release",
                           "signed_at": int(time.time()),
                           "scope": "before_after_photo_use_in_marketing",
                           "expires_at": int(time.time()) + 86400 * 365,
                       })
    if sc in (200, 201):
        ok("model-release document stored", "")
    elif sc == 404:
        gap("P0", "no legal-documents primitive",
            "POST /primitives/user/{uid}/legal-documents 404. Aesthetic clinics need a "
            "signed model-release before publishing a patient's photo. Without a stored "
            "doc + expiry, platform can't gate creative rendering on 'do we still have "
            "rights to this image'.")


# ── Phase 7: Treatment-interval enforcement (push cooldown by SKU) ───────
async def phase_7_treatment_interval(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("7: Treatment-interval enforcement — push 2wk after Botox = BAD")
    primary_bid = state["primary_bid"]
    kid_a = state.get("kid_a", "no_kid")

    # Probe: frequency-cap can it be keyed on a per-treatment-cooldown?
    sc, b = await call(c, "POST", "/api/v1/frequency-cap/configure", json_body={
        "brand_id": primary_bid,
        "policy": "treatment_aware",
        "rules": [
            {"treatment_sku": "ha_botulinum_50u", "min_days_since_last": 90,
             "max_days_since_last": 150,
             "if_violated_action": "suppress"},
            {"treatment_sku": "laser_picosure", "min_days_since_last": 150,
             "max_days_since_last": 210, "if_violated_action": "suppress"},
        ],
    })
    if sc in (200, 201):
        ok("treatment-aware frequency cap configured", "")
    elif sc in (400, 422, 404):
        gap("P0", "no treatment-interval-aware push gate",
            f"{sc} {_short(b)} — /frequency-cap/configure can't accept per-SKU min/max "
            "days-since-last-treatment rules. Pushing a Botox top-up reminder 2 weeks "
            "after the last injection is medically unsafe AND damages trust. Need a "
            "first-class push gate that reads user_attributes/{last_treatment_sku}/history "
            "and suppresses below min_interval_days.")

    # Probe: push-eligibility decision endpoint
    sc, b = await call(c, "POST", "/api/v1/push/eligibility/check", json_body={
        "kid": kid_a, "brand_id": primary_bid,
        "treatment_sku": "ha_botulinum_50u",
        "now_ts": int(time.time()),
    })
    if sc == 200 and isinstance(b, dict):
        if "eligible" in b and "reason" in b:
            ok("push eligibility check exists", f"eligible={b.get('eligible')}")
        else:
            gap("P1", "push eligibility response shape", f"body={_short(b)}")
    elif sc == 404:
        gap("P1", "no push-eligibility check endpoint",
            "POST /push/eligibility/check 404. Merchant has to roll its own pre-push gate. "
            "Generic primitive that takes (kid, treatment_sku, last-known-treatment-history) "
            "and returns eligible/why would unblock all interval-aware verticals "
            "(aesthetics, dental, vaccination, dermatology).")


# ── Phase 8: 180-day attribution + new-users-only auction ────────────────
async def phase_8_long_attribution(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("8: attribution_window_days=180 + target_audience=new_users_only")
    primary_bid = state["primary_bid"]

    # Aesthetics re-treatment cycle: 4–6 months for HA, 6mo for laser
    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": primary_bid,
        "name": "HA Re-treatment 6mo (180d attribution)",
        "objective": "retention",
        "bid_strategy": "cpa",
        "max_bid_cents": 30_000,            # CPA up to ¥300 (procedure margin allows)
        "daily_budget_cents": 50_000,
        "total_budget_cents": 1_000_000,
        "attribution_window_days": 180,
        "target_audience": "new_users_only",  # Round-5 — exclude existing customers
        "targeting": {"geo": {"country": "CN", "city": "Shanghai", "radius_km": 25}},
        "creative": {"recipe_id": "starbucks_loyalty"},  # fallback because no medical recipes
        "schedule": {"start_at": time.time() - 60,
                     "end_at": time.time() + 86400 * 180},
    })
    cid = None
    if sc == 200 and isinstance(b, dict):
        cid = b["campaign_id"]
        ok("180d campaign created", f"id={cid}")
        state["campaign_id"] = cid
    elif sc in (400, 422):
        # Check if attribution_window or target_audience is the blocker
        msg = json.dumps(b) if isinstance(b, dict) else str(b)
        if "attribution_window" in msg or "180" in msg:
            gap("P0", "attribution_window_days=180 rejected",
                f"{sc} {_short(b)} — schema caps the window below 180. Aesthetic re-treatment "
                "cycles are 4–6mo (HA), 6mo (laser), 12mo (Thermage/HIFU). Without ≥180d "
                "the merchant cannot attribute a return visit to its acquisition spend.")
        elif "target_audience" in msg or "new_users_only" in msg:
            gap("P0", "target_audience=new_users_only rejected",
                f"{sc} {_short(b)} — Round-5 new_users_only audience filter is rejected. "
                "Acquisition spend ends up overlapping with retention pushes to existing "
                "members → wasted CPA on people who would have come back anyway.")
        else:
            gap("P0", "campaign create with Round-5 params", f"{sc} {_short(b)}")
            return
    else:
        gap("P0", "campaign create", f"{sc} {_short(b)}")
        return

    # Readback verification
    if cid:
        sc, b = await call(c, "GET", f"/api/v1/campaigns/{cid}/details")
        if sc == 200 and isinstance(b, dict):
            stored = b.get("attribution_window_days", 0)
            ta = b.get("target_audience")
            if stored == 180:
                ok("attribution_window_days=180 persisted", "")
            else:
                gap("P0", "attribution_window silent clamp",
                    f"set=180 readback={stored} — silent clamp; aesthetic retention impossible.")
            if ta == "new_users_only":
                ok("target_audience=new_users_only persisted", "")
            else:
                gap("P1", "target_audience persisted?", f"readback={ta}")

    # Auction savings (Round-5)
    sc, b = await call(c, "GET", f"/api/v1/auction/admin/savings/{primary_bid}")
    if sc == 200 and isinstance(b, dict):
        skipped = b.get("existing_customers_skipped", 0)
        ok("auction savings endpoint", f"skipped={skipped} avg_cpa={b.get('average_cpa_cents')}c")
    elif sc == 404:
        gap("P1", "auction savings endpoint missing",
            "GET /auction/admin/savings/{bid} 404. Round-5 promised existing-customer "
            "exclusion measurement; aesthetic merchant can't see how much CPA was saved.")


# ── Phase 9: Referral chain — friend's FIRST treatment payment triggers ──
async def phase_9_referral_chain(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("9: Referral chain — friend's FIRST payment triggers referrer voucher")
    primary_bid = state["primary_bid"]
    kid_a = state.get("kid_a", "no_kid")

    # Probe: voucher template with Round-4 relational predicate
    # Referrer gets ¥200 voucher when ANY referred friend completes their FIRST treatment
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": primary_bid,
        "name": "闺蜜首单返券 ¥200 (referral on friend's 1st treatment)",
        "description": "Referrer earns ¥200 after referred friend pays for first treatment",
        "value": {"type": "fixed", "amount": 20_000, "currency": "CNY"},
        "conditions": {
            "usage_limit_per_user": 5,
            "min_purchase_cents": 100_000,
            # Round-4 relational predicate probe
            "referral_chain": {
                "trigger_event": "treatment_payment_completed",
                "trigger_user_role": "referred",
                "trigger_user_count_gte": 1,
                "referred_user_first_event": True,
                "issue_to": "referrer",
            },
        },
        "expires_in_days": 90,
        "stackable": False,
        "transferable": False,
    })
    if sc in (200, 201):
        ok("referral-chain voucher template accepted",
           "Round-4 relational predicate + conversion-event trigger works")
    elif sc in (400, 422):
        gap("P0", "referral-chain relational voucher missing",
            f"{sc} {_short(b)} — vouchers.templates.create rejects `referral_chain` "
            "condition. Women's-group viral (referrer earns after friend completes first "
            "treatment) is the #1 acquisition channel for aesthetics. Without a built-in "
            "primitive, every merchant rolls a parallel webhook system → fraud risk and "
            "race conditions on double-issuance.")

    # Probe: attribution token with relationship_type (老梁's pattern)
    sc, b = await call(c, "POST", "/api/v1/attribution/token/create", json_body={
        "source_brand": primary_bid,
        "source_user_id": kid_a,
        "target_brand": primary_bid,
        "channel": "wechat_friend",
        "ttl_hours": 24 * 90,
        "relationship_type": "close_female_friend",
        "viral_chain_id": f"chain_{RUN_TAG}",
    })
    if sc == 200 and isinstance(b, dict) and b.get("token"):
        ok("attribution token with viral_chain_id", f"token={b['token'][:18]}…")
        state["referral_token"] = b["token"]
    else:
        gap("P1", "attribution token referral", f"{sc} {_short(b)}")

    # Probe: viral K-factor / women's-group fan-out
    sc, b = await call(c, "GET", f"/api/v1/master/{state['master_id']}/viral-metrics")
    if sc == 404:
        gap("P1", "no viral K-factor surface",
            "GET /master/{id}/viral-metrics 404. Aesthetic clinics measure success by "
            "'每个老客户带来 0.8 个新客户'. No K-factor surface → marketing can't optimize.")
    elif sc == 200:
        ok("viral metrics endpoint", _short(b, 120))


# ── Phase 10: Doctor/injector binding (resource_id persistence) ──────────
async def phase_10_doctor_binding(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("10: Doctor/injector binding — resource_id on reservation")
    primary_bid = state["primary_bid"]
    kid_a = state.get("kid_a", "no_kid")

    # Create reservation with doctor binding
    sc, b = await call(c, "POST", "/api/v1/reservations/create", json_body={
        "brand_id": primary_bid,
        "user_id": kid_a,
        "scheduled_at": int(time.time()) + 86400 * 7,
        "party_size": 1,
        "type": "appointment",
        "resource_id": INJECTORS[0]["doctor_id"],
        "metadata": {
            "treatment_sku": "ha_botulinum_50u",
            "doctor_name": INJECTORS[0]["name"],
            "treatment_category": "injectable",
            "repeat_with_same_injector": True,
        },
        "check_in_grace_minutes": 15,
    })
    rid = None
    if sc in (200, 201) and isinstance(b, dict):
        rid = b.get("reservation_id")
        if "resource_id" in b or (
            isinstance(b.get("metadata"), dict) and b["metadata"].get("resource_id")):
            ok("doctor resource_id persisted on reservation", f"rid={rid}")
        else:
            gap("P0", "doctor resource_id dropped on readback",
                "reservation accepted but readback has no resource_id field. Repeat-injector "
                "binding (women want the same hand every Botox round) cannot be enforced. "
                "Same gap as 老蔡 hospital sim — still unfixed.")
    else:
        gap("P1", "doctor reservation create", f"{sc} {_short(b)}")

    # Probe: per-doctor preference attribute
    sc, b = await call(c, "POST",
                       f"/api/v1/primitives/user/{kid_a}/attributes",
                       json_body={
                           "brand_id": primary_bid,
                           "attrs": {
                               "preferred_doctor_id": INJECTORS[0]["doctor_id"],
                               "preferred_doctor_set_at": int(time.time()),
                           },
                       })
    if sc in (200, 201):
        ok("preferred_doctor_id attribute stored", "")

    # Probe: auto-route to preferred doctor when re-booking
    sc, b = await call(c, "POST", "/api/v1/reservations/recommend-resource", json_body={
        "brand_id": primary_bid,
        "user_id": kid_a,
        "scheduled_at": int(time.time()) + 86400 * 120,  # next Botox cycle
        "treatment_sku": "ha_botulinum_50u",
    })
    if sc == 200 and isinstance(b, dict) and b.get("resource_id"):
        if b["resource_id"] == INJECTORS[0]["doctor_id"]:
            ok("auto-route to preferred doctor", f"resource_id={b['resource_id']}")
        else:
            gap("P1", "auto-route ignored preferred_doctor", f"got={b['resource_id']}")
    elif sc == 404:
        gap("P1", "no /reservations/recommend-resource",
            "POST /reservations/recommend-resource 404. Aesthetics + salons + clinics all "
            "want auto-pick-preferred-staff when re-booking. Today merchant codes this "
            "client-side from preferred_doctor_id attribute.")


# ── Phase 11: Group-buy / 拼团 for popular HA package ─────────────────────
async def phase_11_groupbuy(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("11: Group-buy 拼团 (popular HA package)")
    primary_bid = state["primary_bid"]

    # Probe: group-buy primitive
    sc, b = await call(c, "POST", "/api/v1/groupbuy/create", json_body={
        "brand_id": primary_bid,
        "treatment_sku": "ha_juvederm_1cc",
        "regular_price_cents":  480_000,
        "groupbuy_price_cents": 380_000,
        "min_participants": 3,
        "max_participants": 10,
        "window_hours": 72,
    })
    if sc in (200, 201):
        ok("groupbuy primitive exists", f"id={b.get('groupbuy_id') if isinstance(b, dict) else ''}")
    elif sc == 404:
        gap("P1", "no /groupbuy/create primitive",
            "POST /groupbuy/create 404. 拼团 is a top WeChat conversion mechanic in CN. "
            "Aesthetic merchants run group-buys on injectables 80% of weekends. Without "
            "platform primitive: merchant runs a parallel system, can't share inventory, "
            "no auction-side discount budget integration.")
    elif sc in (400, 422):
        gap("P1", "groupbuy schema", f"{sc} {_short(b)}")

    # Fallback: voucher with min_participants — see if vouchers can express it
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": primary_bid,
        "name": "HA 1cc 拼团价 ¥3800 (3+人)",
        "value": {"type": "fixed", "amount": 100_000, "currency": "CNY"},
        "conditions": {
            "min_purchase_cents": 380_000,
            "min_concurrent_redeemers": 3,        # probe
            "concurrent_window_hours": 72,
            "usage_limit_per_user": 1,
        },
        "expires_in_days": 7,
    })
    if sc in (200, 201):
        ok("groupbuy-as-voucher accepted", "min_concurrent_redeemers persisted?")
    elif sc in (400, 422):
        gap("P1", "voucher min_concurrent_redeemers missing",
            "voucher conditions vocabulary lacks min_concurrent_redeemers / "
            "concurrent_window_hours. Can't even fake 拼团 via voucher template.")


# ── Phase 12: Push engine — consent + marketing scope + medical-ad rules ─
async def phase_12_push_marketing(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("12: Push engine — marketing scope + medical-ad compliance")
    primary_bid = state["primary_bid"]

    # Seed an acquisition campaign so push engine has something to dispatch
    sc, _ = await call(c, "POST", "/api/v1/campaigns", json_body={
        "brand_id": primary_bid,
        "name": "素颜医美 acquisition push test",
        "objective": "acquire",
        "bid_strategy": "cpm",
        "max_bid_cents": 80,
        "daily_budget_cents": 5000,
        "total_budget_cents": 50000,
        "target_audience": "new_users_only",
    })

    # Register a fresh kid (acquisition target)
    sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
        "phone": f"+8613911{RUN_TAG % 1000000:06d}",
        "display_name": "素颜潜客",
        "device_fingerprint": f"dev_fp_{RUN_TAG}_push",
        "country": "CN",
    })
    push_kid = b.get("kid") if isinstance(b, dict) and isinstance(b.get("kid"), str) else None
    if not push_kid:
        gap("P1", "push kid register", f"{sc} {_short(b)}")
        return

    await _setup_consent(c, [push_kid], state["consent_version"],
                         scopes=["marketing", "personalization", "cross_brand_tracking"])

    # Push without marketing-medical scope
    sc, b = await call(c, "POST", "/api/v1/push/now", json_body={
        "kid": push_kid, "slot": "push", "context": {"industry": "medical_aesthetics"},
    })
    if sc == 200 and isinstance(b, dict):
        if b.get("fired"):
            ok("push/now fired (marketing scope)", f"push_id={b.get('push_id')} brand={b.get('brand_id')}")
            # Did the push payload run through medical-advertising linter?
            payload = b.get("payload") or b.get("creative") or {}
            if isinstance(payload, dict) and payload.get("compliance_warnings"):
                ok("push payload medical-ad linted", f"warnings={payload['compliance_warnings']}")
            else:
                gap("P0", "push creative has no medical-ad lint",
                    "/push/now fires creative without running it through 医疗广告法 banned-"
                    "phrase / disclaimer linter. If creative-gen says '永久去皱' the push "
                    "still goes out → ad license revocation risk.")
        else:
            ok("push/now endpoint reachable", f"fired=false reason={b.get('reason')}")
    else:
        gap("P1", "push/now", f"{sc} {_short(b)}")

    # Probe: revoking marketing scope must stop pushes (and audit it)
    sc, b = await call(c, "POST", "/api/v1/consent/revoke", json_body={
        "user_id": push_kid, "scopes": ["marketing"],
        "reason": "user_initiated",
    })
    if sc == 200:
        ok("consent revoke (marketing) accepted", "")
        sc2, b2 = await call(c, "POST", "/api/v1/push/now", json_body={
            "kid": push_kid, "slot": "push",
        })
        if sc2 == 200 and isinstance(b2, dict):
            if b2.get("fired"):
                gap("P0", "push fires after marketing-consent revoked",
                    "after /consent/revoke for marketing scope, /push/now still fired. "
                    "Direct violation of 个保法 §16 (user can withdraw consent at any time).")
            else:
                ok("post-revoke push suppressed", f"reason={b2.get('reason')}")


# ── Phase 13: 13-Phase wrap — Edge cases + module probe ──────────────────
async def phase_13_edges_and_probe(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("13: Edge cases — out-of-pocket only / disclaimer / module probe")
    primary_bid = state["primary_bid"]
    kid_a = state.get("kid_a", "no_kid")

    # 13a: Out-of-pocket only — confirm insurance/payor fields are NOT mistakenly required
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": primary_bid,
        "name": "首单优惠 ¥500 off (self-pay only)",
        "value": {"type": "fixed", "amount": 50000, "currency": "CNY"},
        "conditions": {
            "usage_limit_per_user": 1,
            "min_purchase_cents": 200_000,
            "payor_in": ["self_pay"],   # probe — should accept, no insurance
        },
        "expires_in_days": 60,
    })
    if sc in (200, 201):
        ok("self-pay voucher accepted", "")
    elif sc in (400, 422):
        gap("P2", "payor_in field unsupported",
            "voucher condition payor_in=['self_pay'] rejected. Aesthetics is 100% self-pay; "
            "still worth a declarative tag so reporting can separate self-pay vs (any future) "
            "insurance flows.")

    # 13b: Mandatory disclaimer field on creative
    sc, b = await call(c, "POST", "/api/v1/recipes/generate", json_body={
        "brand_id": primary_bid,
        "industry": "medical_aesthetics",
        "objective": "first_visit_acquisition",
        "intent": "保妥适首单 ¥1980 体验价",
    })
    if sc == 200 and isinstance(b, dict):
        body_str = json.dumps(b, ensure_ascii=False)
        bad_phrases = ["保证", "永久", "无副作用", "绝对", "彻底治愈"]
        hits = [p for p in bad_phrases if p in body_str]
        if hits:
            gap("P0", "creative-gen produced banned medical-ad phrases",
                f"hits={hits} — generator output contains 医疗广告法 banned phrases. "
                "Must be linted/replaced server-side before storage.")
        else:
            ok("creative-gen output clean of obvious banned phrases", "")
        if "风险提示" in body_str or "副作用" in body_str or "禁忌" in body_str:
            ok("creative includes risk disclaimer", "")
        else:
            gap("P1", "creative lacks mandatory risk disclaimer",
                "医疗广告法 §16 requires risk warnings; generated creative has none.")
    elif sc == 404:
        gap("P1", "no /recipes/generate endpoint", "creative-gen not exposed")

    # 13c: Trigger registration — booking → confirm push within consent
    sc, b = await call(c, "POST", "/api/v1/triggers/register", json_body={
        "brand_id": primary_bid,
        "name": "Post-treatment care reminder (D+1)",
        "event_type": "treatment_completed",
        "event_filter": {"treatment_category": "injectable"},
        "action": {
            "type": "send_push",
            "config": {
                "title": "术后24h提醒",
                "body": "今日避免按压注射部位 / 不化妆 / 不饮酒",
                "scope_required": "personalization",  # post-care is service, not marketing
            },
            "delay_seconds": 86400,
        },
        "cooldown_seconds": 0,
        "max_fires_per_user": 0,
    })
    if sc == 201 and isinstance(b, dict) and b.get("trigger_id"):
        ok("post-care trigger registered", f"trigger_id={b['trigger_id'][:18]}…")
    elif sc in (400, 422):
        gap("P1", "trigger schema rejects scope_required",
            f"{sc} {_short(b)} — post-care reminders are service messages "
            "(personalization scope) NOT marketing. Can't disambiguate today.")

    # 13d: Module probe
    probes = [
        ("kix-id.register",          "POST", "/api/v1/kix-id/register", None),
        ("attributes.log",           "POST", f"/api/v1/primitives/user/{kid_a}/attributes/x/log", None),
        ("attributes.history",       "GET",  f"/api/v1/primitives/user/{kid_a}/attributes/last_treatment_sku/history", {"brand_id": primary_bid}),
        ("push.now",                 "POST", "/api/v1/push/now", None),
        ("push.eligibility",         "POST", "/api/v1/push/eligibility/check", None),
        ("auction.savings",          "GET",  f"/api/v1/auction/admin/savings/{primary_bid}", None),
        ("compliance.rules",         "GET",  "/api/v1/compliance/rules", {"industry": "medical_aesthetics"}),
        ("media.upload",             "POST", "/api/v1/media/upload", None),
        ("groupbuy.create",          "POST", "/api/v1/groupbuy/create", None),
        ("legal-documents",          "POST", f"/api/v1/primitives/user/{kid_a}/legal-documents", None),
        ("recommend-resource",       "POST", "/api/v1/reservations/recommend-resource", None),
        ("viral-metrics",            "GET",  f"/api/v1/master/{state['master_id']}/viral-metrics", None),
        ("recipes.industry",         "GET",  "/api/v1/recipes", {"industry": "medical_aesthetics"}),
        ("audiences.gender-filter",  "POST", "/api/v1/audiences/custom/create", None),
    ]
    avail, missing = [], []
    for label, method, path, params in probes:
        sc, b = await call(c, method, path, params=params,
                           json_body=None if method == "GET" else {})
        if sc in (200, 201):
            avail.append(label); ok(f"module live: {label}", f"{sc}")
        elif sc == 404:
            if isinstance(b, dict) and b.get("detail") in ("Not Found", "not found"):
                missing.append(label); gap("P1", f"module missing: {label}", f"404 at {path}")
            else:
                avail.append(label); ok(f"module live (no-resource): {label}", "404 with domain detail")
        elif sc in (400, 422):
            avail.append(label); ok(f"module live: {label}", f"{sc} (schema mismatch)")
        else:
            avail.append(label); info(f"module {label}: {sc}")
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
    md.append("# 老沈 / Shen Lan (素颜医美 Bare Beauty Aesthetics) — Merchant Journey Findings")
    md.append("")
    md.append(f"**Run tag**: `{RUN_TAG}` | **Runtime**: {runtime:.1f}s | "
              f"**Date**: {time.strftime('%Y-%m-%d %H:%M', time.localtime(start_ts))}")
    md.append("")
    md.append("## Scenario")
    md.append(
        "老沈 owns 「素颜医美」 — a Shanghai mid-tier medical aesthetics chain with 4 clinics "
        "(Jing'an / Xuhui / Pudong / Hongqiao), 1200 active members (80% female, 25–45). "
        "Services: HA injectables ¥3K–8K, laser ¥1K–5K, surgical ¥30K–200K. Budget ¥40K/月. "
        "Unique pains: **medical-advertising compliance** (医疗广告法 forbids cure/efficacy "
        "claims), **before/after photo sensitivity** (model release + age proof required), "
        "**long re-treatment cycles** (HA 4–6mo, laser 6mo, energy 12mo), **female-network "
        "viral** (闺蜜推荐 with friend's first-payment trigger), **out-of-pocket only** "
        "(no insurance), **doctor/injector loyalty** (women want the same hand each round), "
        "and **treatment-interval safety** (a Botox top-up 2 weeks early is medically wrong)."
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
            md.append("_None._"); md.append(""); return
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

    md.append("## Top 5 NEW gaps unique to MEDICAL AESTHETICS")
    md.append("")
    md.append(
        "1. **Medical-advertising compliance enforcement is absent.** No "
        "`/compliance/rules?industry=medical_aesthetics`, no banned-phrase linter on "
        "creative-gen output, no mandatory risk-disclaimer injection. 医疗广告法 has 30+ "
        "banned-phrase classes ('保证安全', '永久', '彻底治愈', '无副作用'). Aesthetic clinic "
        "running creative-gen today could publish illegal copy and lose its ad licence. "
        "Pharma + finance verticals will hit the same gap.\n"
        "\n"
        "2. **Before/after photo class missing across the stack.** No media-upload primitive "
        "with `media_class=medical_sensitive`, no `before_after_photo` consent scope, no "
        "`/primitives/user/{uid}/legal-documents` for model-release storage. Aesthetic "
        "clinics' #1 conversion asset is B/A photos but the platform can't legally host them. "
        "Same primitive serves dermatology, plastic surgery, dentistry.\n"
        "\n"
        "3. **Treatment-interval-aware push gate missing.** `/frequency-cap/configure` has "
        "no per-SKU min/max-days-since-last-treatment rules. `/push/eligibility/check` "
        "endpoint doesn't exist. Pushing a Botox top-up 2 weeks after the last injection is "
        "medically wrong + destroys trust. Generic primitive (kid, treatment_sku, history → "
        "eligible/why) unblocks aesthetics, dental, vaccination, dermatology, chronic-care.\n"
        "\n"
        "4. **Referral-chain voucher with conversion-event trigger missing.** "
        "vouchers.templates.create rejects `referral_chain` predicate (trigger_event = "
        "referred friend's first-payment). 闺蜜推荐 is the #1 acquisition channel for "
        "female-network aesthetic clinics; today every merchant rolls a parallel webhook "
        "system → race conditions on double-issuance, fraud risk.\n"
        "\n"
        "5. **Gender-attribute audience predicate.** audiences.custom.create may not accept "
        "`{attribute: sex, op: eq, value: F}`. Medical aesthetics is 80% female with "
        "female-exclusive treatments (HIFU breast, OB-aesthetic). Without gender targeting "
        "the merchant wastes 50% of acquisition impressions. Also blocks future verticals "
        "(maternity, lingerie, men's grooming).\n"
    )
    md.append("")
    md.append("## Cross-Comparison: 老蔡 Hospital vs 老沈 Medical Aesthetics")
    md.append("")
    md.append(
        "| Concern | 老蔡 仁爱国际医院 | **老沈 素颜医美** |\n"
        "|---|---|---|\n"
        "| Customer mix | mixed gender / all ages | **80% female, 25–45** |\n"
        "| Payor | insurance + self-pay | **self-pay only** (no payor metadata needed) |\n"
        "| Compliance pressure | HIPAA + 个保法 + 网络安全法 | **+ 医疗广告法 (banned-phrase enforcement)** |\n"
        "| Photo sensitivity | radiology / specialist | **before/after = primary marketing asset (model release required)** |\n"
        "| Reservation primitive needs | gp/specialist/lab/vaccination/dental | **injectable / laser / surgical / energy** |\n"
        "| Resource binding | doctor_id | **injector_id (preferred-hand loyalty)** |\n"
        "| Attribution window | 365d (annual booster) | **180d (4-6mo HA / 6mo laser)** |\n"
        "| Voucher relational | family package | **referral chain w/ friend's first-payment trigger** |\n"
        "| Push gate uniqueness | emergency override | **treatment-interval cooldown (don't push Botox 2wk early)** |\n"
        "| Acquisition channel | doctor referral / search | **WeChat group / 闺蜜 viral** |\n"
        "| Group-buy 拼团 | nope | **first-class need** |\n"
        "| Mandatory disclaimers | informed-consent forms | **medical-advertising risk disclaimer in every creative** |\n"
    )
    md.append("")
    md.append(
        "**Medical aesthetics is the first vertical where**: the creative-gen pipeline itself "
        "needs an industry-aware compliance linter (医疗广告法), before/after photos are a "
        "first-class media class with model-release legal-docs, treatment-interval safety is "
        "a hard push-gate (medical safety, not just frequency), and viral acquisition runs "
        "through women's WeChat groups (referral chain with conversion-event trigger, not "
        "signup trigger). 老蔡 hospital probed PHI compliance; 老沈 aesthetics adds a parallel "
        "axis: medical-advertising compliance is just as load-bearing and currently as absent."
    )
    md.append("")
    md.append("## Strategic Recommendations (Top 8)")
    md.append("")
    md.append(
        "1. **[P0] Compliance rule registry** `GET /compliance/rules?industry=…&jurisdiction=…`. "
        "Seed `medical_aesthetics`+CN with the 30+ 医疗广告法 banned-phrase classes + mandatory-"
        "disclaimer templates. Hook into creative-gen as a server-side linter that rewrites "
        "or rejects bad copy before storage.\n"
        "\n"
        "2. **[P0] before_after_photo + medical_data consent scopes**. One-line addition to "
        "consent.VALID_SCOPES; large unlock. Plus `marketing_medical` for 医疗广告法-grade "
        "marketing opt-in distinct from generic marketing.\n"
        "\n"
        "3. **[P0] Media upload primitive with media_class.** "
        "`POST /media/upload` accepts `media_class={generic,medical_sensitive,model_face,...}` "
        "with consent_token binding. medical_sensitive routes to a hardened bucket; auto-"
        "applies blurring / disclaimer overlay when surfaced in creative.\n"
        "\n"
        "4. **[P0] Legal-documents primitive.** "
        "`POST /primitives/user/{uid}/legal-documents` with doc_type (model_release | "
        "informed_consent | privacy_acknowledgement), signed_at, scope, expires_at. Gate "
        "creative rendering on a valid unexpired model_release.\n"
        "\n"
        "5. **[P0] Treatment-interval-aware push gate.** "
        "Extend /frequency-cap/configure with per-SKU min/max days-since-last-event rules. "
        "Add `/push/eligibility/check` endpoint that reads user attribute history and returns "
        "(eligible, reason, next_eligible_at). Same primitive serves aesthetics, vaccination, "
        "dental, chronic-care.\n"
        "\n"
        "6. **[P0] Voucher referral_chain predicate.** "
        "Extend voucher conditions vocabulary with `referral_chain: {trigger_event, "
        "trigger_user_role, trigger_user_count_gte, referred_user_first_event, issue_to}`. "
        "Solves women's-group viral (aesthetics), buddy-fitness (gym), and group-booking "
        "(travel) in one primitive.\n"
        "\n"
        "7. **[P1] Gender-attribute audience predicate.** "
        "audiences.custom.create must accept `{attribute, op, value}` predicates against "
        "user_attributes (sex / age / preferred_doctor_id). Unblocks medical aesthetics, "
        "lingerie, maternity, men's grooming.\n"
        "\n"
        "8. **[P1] Group-buy primitive `/groupbuy/create`** with min/max participants, "
        "window_hours, brand wallet integration. 拼团 is a top CN conversion mechanic; "
        "applies to aesthetics, dining, education, travel.\n"
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
                state = await phase_1_master(c)
                await phase_2_wallet(c, state)
                await phase_3_consent(c, state)
                await phase_4_audience(c, state)
                await phase_5_kix_id_history(c, state)
                await phase_6_photo_storage(c, state)
                await phase_7_treatment_interval(c, state)
                await phase_8_long_attribution(c, state)
                await phase_9_referral_chain(c, state)
                await phase_10_doctor_binding(c, state)
                await phase_11_groupbuy(c, state)
                await phase_12_push_marketing(c, state)
                await phase_13_edges_and_probe(c, state)
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
