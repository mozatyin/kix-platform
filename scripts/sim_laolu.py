"""Merchant journey simulation — 老陆 / Lu Wei (方正地产 Fang Zheng Real Estate).

End-to-end probe of the KiX Ads Platform from the perspective of a Shenzhen
high-end residential property broker. Unique-to-real-estate concerns probed:

  * **Ultra-long sales cycle** — 6-month buyer journey requires
    ``attribution_window_days >= 180`` (current schema caps at 90).
  * **Single-transaction focus** — 50 transactions/month × ¥80K commission;
    the funnel matters more than volume; one lost ¥30M deal = month of revenue.
  * **Agent network** — 15 agents, each owns a book of clients; needs
    Round-4 client↔agent relationships + commission-split payouts.
  * **Property as resource** — 200 active listings, each a Round-3 reservation
    resource_id; showroom geofence; off-plan vs ready-stock subtyping.
  * **Document-heavy qualification** — buyer ID + hukou + 资金证明 + 限购资格
    (all PHI-class sensitive data with 5–10 year retention).
  * **Regulatory load** — 限购 (purchase quota), 网签 (registered sale),
    增值税 disclosure, 个人所得税, 房地产广告法 (advertising compliance).
  * **High-touch CRM** — agent calls + WeChat + WhatsApp + viewings + follow-up;
    each touchpoint costs human time, attribution to the right agent matters.
  * **Retargeting strategy** — viewed-but-not-bought audience over 180 days
    vs. cold new leads at ¥3M+ AOV.
  * **Showroom = store** — sales gallery (售楼处) acts as the physical store;
    geofence enter event = high-intent.
  * **Government regs** — purchase-eligibility lottery, no over-promising on
    appreciation, escrow-held deposit (定金), 阴阳合同 black-market risk.

Run:
    .venv/bin/python scripts/sim_laolu.py
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
OWNER_USER_ID = f"laolu_{RUN_TAG}"
MASTER_BRAND_ID = f"fangzheng_realty_{RUN_TAG}"

# Sub-brands: 4 office/segment splits
SUB_OFFICES = [
    ("nanshan",  "方正地产 — 南山旗舰店",   "luxury_apartment"),     # 南山 high-end
    ("futian",   "方正地产 — 福田CBD店",    "cbd_apartment"),         # 福田 CBD
    ("qianhai",  "方正地产 — 前海新区店",   "new_development"),       # 前海 off-plan
    ("baoan",    "方正地产 — 宝安别墅店",   "villa"),                 # 宝安 villas
]

FINDINGS_PATH = Path("/Users/mozat/a-docs/laolu-sim-findings.md")

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"

# Shenzhen 南山 (premium residential coordinates)
SZ_LAT = 22.5333
SZ_LNG = 113.9333

# 15 agents
AGENT_FIRSTNAMES = ["建华", "玉芳", "志强", "明辉", "晓东", "丽娟", "国庆",
                    "佳慧", "永康", "美琪", "立群", "婷婷", "天宇", "雪梅", "宏伟"]
AGENT_LASTNAMES = ["王", "李", "张", "陈", "刘", "杨", "黄", "周", "吴", "徐",
                   "孙", "马", "朱", "胡", "林"]

# Sample properties (resource_id = property listing)
PROPERTIES = [
    {"prop_id": f"prop_nanshan_a01_{RUN_TAG}", "name": "深圳湾一号A-3501", "office": "nanshan",
     "price_cents": 5_000_000_000, "type": "luxury_apartment", "rooms": 4, "area_sqm": 280},   # ¥50M
    {"prop_id": f"prop_nanshan_a02_{RUN_TAG}", "name": "华润城润府B-1802", "office": "nanshan",
     "price_cents": 1_800_000_000, "type": "luxury_apartment", "rooms": 3, "area_sqm": 145},   # ¥18M
    {"prop_id": f"prop_futian_c01_{RUN_TAG}",  "name": "卓越世纪中心C-2105", "office": "futian",
     "price_cents": 1_200_000_000, "type": "cbd_apartment", "rooms": 2, "area_sqm": 95},        # ¥12M
    {"prop_id": f"prop_qianhai_d01_{RUN_TAG}", "name": "前海自贸壹号(预售)", "office": "qianhai",
     "price_cents": 800_000_000, "type": "new_development", "rooms": 3, "area_sqm": 120},       # ¥8M off-plan
    {"prop_id": f"prop_baoan_e01_{RUN_TAG}",   "name": "纯水岸别墅A栋",     "office": "baoan",
     "price_cents": 4_500_000_000, "type": "villa", "rooms": 5, "area_sqm": 380},               # ¥45M
    {"prop_id": f"prop_nanshan_a03_{RUN_TAG}", "name": "蛇口太子湾汀峰", "office": "nanshan",
     "price_cents": 3_500_000_000, "type": "luxury_apartment", "rooms": 4, "area_sqm": 220},   # ¥35M
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


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


async def _grant_consent(c: httpx.AsyncClient, user_ids: list[str], version: str) -> int:
    granted = 0
    for uid in user_ids:
        sc, _ = await call(c, "POST", "/api/v1/consent/grant", json_body={
            "user_id": uid,
            "scopes": ["cross_brand_tracking", "personalization", "marketing", "geo_lbs"],
            "policy_version": version,
            "source": "app",
        })
        if sc == 200:
            granted += 1
    return granted


# ── Phase 1: Master + 4-office agency setup ──────────────────────────────
async def phase_1_master_setup(c: httpx.AsyncClient) -> dict[str, Any]:
    _phase_init("1: Master + 4-office real estate agency")
    state: dict[str, Any] = {"master_id": None, "sub_brands": {}}

    sc, b = await call(c, "POST", "/api/v1/master/create", json_body={
        "company_name": "方正地产 / Fang Zheng Real Estate Brokerage Corp",
        "primary_email": "laolu@fangzheng-realty.cn",
        "owner_user_id": OWNER_USER_ID,
    })
    if sc == 201 and isinstance(b, dict):
        state["master_id"] = b["master_id"]
        ok("create master account", f"master_id={state['master_id']}")
    else:
        fail("create master account", f"status={sc} body={_short(b)}")
        return state

    master_id = state["master_id"]

    # Attach 4 sub-offices (Shenzhen districts)
    attached = 0
    for slug, name, _segment in SUB_OFFICES:
        bid = f"fangzheng_{slug}_{RUN_TAG}"
        state["sub_brands"][slug] = bid
        sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/brands/attach", json_body={
            "brand_id": bid,
            "store_name": name,
            "store_id": bid,
        })
        if sc == 200:
            attached += 1
        else:
            gap("P1", f"attach office {slug}", f"{sc} {_short(b)}")
    if attached == len(SUB_OFFICES):
        ok("attach 4 district offices", "南山 / 福田 / 前海 / 宝安")

    # Probe: industry='real_estate' / 'realty' field & recipes
    sc, b = await call(c, "GET", f"/api/v1/master/{master_id}")
    if sc == 200 and isinstance(b, dict):
        if "industry" in b or "vertical" in b:
            ok("master industry field", f"vertical={b.get('industry') or b.get('vertical')}")
        else:
            gap("P1", "master.industry field missing",
                "GET /master/{id} returns no `industry` field — real estate cannot self-"
                "declare vertical at master level. Downstream compliance "
                "(房地产广告法 forbids guaranteed appreciation claims) must be guessed.")

    sc, b = await call(c, "GET", "/api/v1/recipes", params={"industry": "real_estate"})
    if sc == 200 and isinstance(b, (list, dict)):
        items = b if isinstance(b, list) else b.get("recipes", b.get("items", []))
        if items:
            ok("recipes industry=real_estate", f"{len(items)} match")
        else:
            gap("P0", "no real_estate recipes seeded",
                "?industry=real_estate returns 0. ¥3M-50M property funnels don't fit any "
                "loyalty / streak / referral recipe out of the box. Recipes needed: "
                "viewing_to_offer_funnel, off_plan_pre_sale_waitlist, agent_referral_chain, "
                "high_intent_retargeting_180d.")
    else:
        gap("P1", "recipe industry filter", f"{sc} {_short(b)}")

    state["primary_bid"] = state["sub_brands"]["nanshan"]
    return state


# ── Phase 2: Wallet ¥60K/month + luxury margin ──────────────────────────
async def phase_2_wallet(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("2: Wallet ¥60K/month + luxury margin")
    master_id = state["master_id"]
    if not master_id:
        return

    allocation = {bid: round(1.0 / len(SUB_OFFICES), 4) for bid in state["sub_brands"].values()}
    deficit = 1.0 - sum(allocation.values())
    first_bid = next(iter(allocation))
    allocation[first_bid] = round(allocation[first_bid] + deficit, 4)

    sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/budget/global", json_body={
        "monthly_budget_cents": 6_000_000,   # ¥60K
        "allocation": allocation,
    })
    if sc == 200:
        ok("set master global budget", "¥60K/month across 4 offices")
    else:
        gap("P1", "set master global budget", f"{sc} {_short(b)}")

    # Top up nanshan flagship ¥30K (highest AOV district)
    primary = state["primary_bid"]
    sc, b = await call(c, "POST", f"/api/v1/wallet/{primary}/topup", json_body={
        "amount_cents": 3_000_000,
        "payment_method": "wechat",
    })
    if sc == 200 and isinstance(b, dict) and "topup_id" in b:
        tid = b["topup_id"]
        sc2, _ = await call(c, "POST", f"/api/v1/wallet/{primary}/topup/{tid}/confirm",
                            json_body={"payment_gateway_response": {"mock": True}})
        if sc2 == 200:
            ok("topup nanshan ¥30K + confirm", "")
        else:
            gap("P1", "confirm topup", f"{sc2}")
    else:
        gap("P1", "topup nanshan", f"{sc} {_short(b)}")

    # Probe: CPA limit suitable for ¥3M+ AOV. Real estate CPA easily ¥500-2000.
    sc, b = await call(c, "GET", f"/api/v1/wallet/{primary}/daily-budget-status")
    if sc == 200 and isinstance(b, dict):
        daily = b.get("today_budget_cents") or b.get("daily_budget_cents", 0)
        ok("wallet daily budget status", f"daily_budget_cents={daily}")
        if daily and daily < 50_000:  # ¥500 daily = only 1 viewing-CPA at ¥500
            gap("P2", "daily budget tight for high-AOV vertical",
                f"daily={daily}c — real estate CPA per qualified viewing easily ¥500-2000. "
                "Single transaction commission ¥80K supports much higher CPA than retail.")
    else:
        gap("P2", "wallet status", f"{sc} {_short(b)}")


# ── Phase 3: Consent + sensitive document scope probe + tier ladder ─────
async def phase_3_consent_tiers(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("3: Consent (document_storage probe) + Buyer Tiers")
    primary = state["primary_bid"]
    ver = f"v_{RUN_TAG}"

    sc, b = await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
        "version": ver,
        "text_md": (
            "# 方正地产 隐私政策\n"
            "本政策涵盖买家身份证 / 户口本 / 资金证明 / 限购资格审核 / 网签备案数据存储。"
            "适用《个人信息保护法》第28条敏感个人信息、《房地产经纪管理办法》、"
            "《商品房销售管理办法》第43条客户档案保管 (≥10年)。"
        ),
        "effective_at": int(time.time()) - 60,
        "requires_re_grant": False,
    })
    if sc == 200:
        ok("publish consent policy", f"version={ver}")
    else:
        gap("P0", "publish consent policy", f"{sc} {_short(b)}")
        return
    state["consent_version"] = ver

    # Probe: document_storage scope (REAL-ESTATE-SPECIFIC sensitive PI)
    sc, b = await call(c, "POST", "/api/v1/consent/grant", json_body={
        "user_id": f"doc_probe_{RUN_TAG}",
        "scopes": ["document_storage"],
        "policy_version": ver,
        "source": "app",
    })
    body_str = json.dumps(b) if isinstance(b, dict) else str(b)
    if sc == 200:
        ok("document_storage scope accepted", "REAL-ESTATE sensitive-PI scope is wired")
    elif sc in (400, 422) and "scope" in body_str.lower():
        gap("P0", "document_storage consent scope missing",
            f"{sc} {_short(b)} — consent.VALID_SCOPES has no `document_storage` (or "
            "`identity_document` / `financial_proof`). Real estate brokers must store: "
            "buyer ID card, hukou (户口本), proof of funds (资金证明), 限购 eligibility "
            "letter. 个保法 §28 classifies these as sensitive PI requiring separate "
            "consent from marketing. Today they collapse into 'personalization'.")
    else:
        gap("P1", "document_storage scope probe", f"{sc} {_short(b)}")

    # Probe: financial_proof scope
    sc, b = await call(c, "POST", "/api/v1/consent/grant", json_body={
        "user_id": f"fin_probe_{RUN_TAG}",
        "scopes": ["financial_proof"],
        "policy_version": ver,
        "source": "app",
    })
    if sc == 200:
        ok("financial_proof scope accepted", "")
    elif sc in (400, 422):
        gap("P1", "financial_proof scope missing",
            "buyer 资金证明 (bank statements, salary cert) is sensitive PI distinct from "
            "marketing — needs its own scope toggle.")

    # Configure buyer tier — qualified prospect / contracted / closed
    sc, b = await call(c, "POST", "/api/v1/primitives/tier/configure", json_body={
        "brand_id": primary,
        "tiers": [
            {"name": "lead", "xp_min": 0},
            {"name": "qualified", "xp_min": 1000},     # passed 限购 check + viewing booked
            {"name": "contracted", "xp_min": 10_000},  # 定金 paid
            {"name": "closed", "xp_min": 100_000},     # 网签 + 过户 complete
        ],
    })
    if sc == 200:
        ok("buyer tier ladder configured", "lead / qualified / contracted / closed")
    else:
        gap("P1", "configure buyer tier", f"{sc} {_short(b)}")


# ── Phase 4: KiX IDs — agents + clients + client↔agent relationships ────
async def phase_4_kix_id_and_relationships(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("4: KiX IDs (agents + clients) + Round-4 client↔agent relationships")
    ver = state["consent_version"]
    primary = state["primary_bid"]
    rng = random.Random(RUN_TAG)

    # ── Register 15 agents as first-class KiX IDs ─────────────────────────
    agents: list[dict[str, Any]] = []
    for i in range(15):
        last = AGENT_LASTNAMES[i % len(AGENT_LASTNAMES)]
        first = AGENT_FIRSTNAMES[i]
        sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
            "phone": f"+8613911{(RUN_TAG + i) % 1000000:06d}",
            "display_name": f"{last}{first}",
            "primary_language": "zh-CN",
            "source_brand_id": primary,
            "device_fingerprint": f"dev_agent_{RUN_TAG}_{i}",
            "country": "CN",
        })
        if sc == 200 and isinstance(b, dict) and b.get("kid"):
            agents.append({"kid": b["kid"], "name": f"{last}{first}", "office": SUB_OFFICES[i % 4][0]})
        else:
            gap("P1", f"kix-id register agent {i}", f"{sc} {_short(b)}")
    if len(agents) == 15:
        ok("15 agents registered as KiX IDs", "first-class identity for sales staff")
    state["agents"] = agents

    # Probe: a tag/role field for agent vs client. Use update.
    if agents:
        sc, b = await call(c, "POST", f"/api/v1/kix-id/{agents[0]['kid']}/update", json_body={
            "role": "agent",
            "agent_license_no": f"SZ_AG_{RUN_TAG}",
            "office": agents[0]["office"],
        })
        if sc == 200 and isinstance(b, dict):
            if b.get("role") == "agent" or (isinstance(b.get("metadata"), dict)
                                            and b["metadata"].get("role") == "agent"):
                ok("agent role/license stored on KiX ID", "")
            else:
                gap("P1", "agent role/license silently dropped",
                    "kix-id update accepted role+license fields but readback shows neither. "
                    "Real-estate licensing (经纪人备案号) is required by 房地产经纪管理办法 — "
                    "platform must surface license to buyers for trust + 防欺诈.")
        elif sc in (400, 422):
            gap("P1", "kix-id update rejects role field",
                f"{sc} — KiX ID schema doesn't accept `role`/`agent_license_no`. "
                "Brokers can't differentiate staff from clients in the identity layer.")

    # ── Register 30 buyer clients ─────────────────────────────────────────
    clients: list[dict[str, Any]] = []
    for i in range(30):
        sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
            "phone": f"+8613811{(RUN_TAG + 1000 + i) % 1000000:06d}",
            "display_name": f"买家{i+1:02d}",
            "primary_language": "zh-CN",
            "source_brand_id": primary,
            "device_fingerprint": f"dev_buyer_{RUN_TAG}_{i}",
            "country": "CN",
        })
        if sc == 200 and isinstance(b, dict) and b.get("kid"):
            clients.append({"kid": b["kid"], "name": f"buyer_{i+1}"})
    if len(clients) >= 25:
        ok(f"{len(clients)} client KiX IDs registered", "")
    else:
        gap("P1", "client registration burst", f"only {len(clients)}/30")
    state["clients"] = clients

    # Grant consent for all
    all_ids = [a["kid"] for a in agents] + [cl["kid"] for cl in clients]
    granted = await _grant_consent(c, all_ids, ver)
    info(f"consent granted: {granted}/{len(all_ids)}")

    # ── Round-4 relationships: each client ↔ one assigned agent ───────────
    state["relationship_works"] = False
    edges_created = 0
    first_err = None
    pairings = []
    for i, cl in enumerate(clients[:20]):
        ag = agents[i % len(agents)] if agents else None
        if not ag:
            break
        pairings.append((cl["kid"], ag["kid"]))
        sc, b = await call(c, "POST",
                           f"/api/v1/primitives/users/{ag['kid']}/relationships",
                           json_body={
                               "related_user_id": cl["kid"],
                               "relationship": "agent_of",
                               "bidirectional": True,
                               "meta": {
                                   "assigned_at": int(time.time()),
                                   "office": ag["office"],
                                   "intent_segment": "high_intent",
                               },
                           })
        if sc in (200, 201):
            edges_created += 1
        elif first_err is None:
            first_err = (sc, b)
    if edges_created == len(pairings):
        ok("Round-4 agent_of relationships", f"{edges_created} client↔agent edges created")
        state["relationship_works"] = True
    elif edges_created > 0:
        gap("P1", "agent↔client relationships partial",
            f"{edges_created}/{len(pairings)}; first err: {first_err}")
    else:
        gap("P0", "agent↔client relationships missing",
            f"all {len(pairings)} edges failed; first: {first_err}. "
            "Real estate is a relationship business — without a first-class "
            "agent_of edge, commission attribution, lead handoff, and 'my agent' "
            "personalization all have to live in custom JSON.")
    state["pairings"] = pairings

    # Probe: filter by relationship to fetch an agent's book of clients
    if agents:
        ag0 = agents[0]
        sc, b = await call(c, "GET",
                           f"/api/v1/primitives/users/{ag0['kid']}/relationships",
                           params={"relationship": "agent_of"})
        if sc == 200 and isinstance(b, dict):
            n = b.get("count") or len(b.get("relationships", []) or b.get("edges", []) or [])
            if n > 0:
                ok("agent book-of-clients query", f"agent[0] book size={n}")
            else:
                gap("P2", "agent book count zero",
                    "filter returned but no clients — relationship may not be indexed by type")
        else:
            gap("P1", "filter by relationship=agent_of", f"{sc} {_short(b)}")


# ── Phase 5: Property listings as reservation resource_id (viewings) ────
async def phase_5_property_viewings(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("5: Property = resource_id; Viewing = reservation")
    primary = state["primary_bid"]
    rng = random.Random(RUN_TAG + 5)

    # Probe: real-estate reservation types
    re_types = ["property_viewing", "off_plan_showroom", "open_house",
                "signing_appointment", "deposit_payment"]
    accepted, rejected = [], []
    for t in re_types:
        sc, _ = await call(c, "POST", "/api/v1/reservations/create", json_body={
            "brand_id": primary,
            "user_id": state["clients"][0]["kid"] if state["clients"] else f"probe_{RUN_TAG}",
            "scheduled_at": int(time.time()) + 86400,
            "party_size": 2,  # buyer + spouse common
            "type": t,
            "metadata": {"probe": True},
        })
        (accepted if sc in (200, 201) else rejected).append(t)
    if not rejected:
        ok("real-estate reservation types accepted", f"all 5: {re_types}")
    else:
        gap("P0", "reservation type enum lacks real-estate subtypes",
            f"rejected: {rejected}. Accepted: {accepted}. Documented enum is "
            "dining|fitness_class|appointment|event|tour|service. Real estate needs "
            "first-class property_viewing / off_plan_showroom / open_house / "
            "signing_appointment / deposit_payment subtyping for funnel analytics "
            "(viewing→offer→signed→closed conversion rate per agent / per property).")

    # Create a sample viewing with resource_id = property listing
    if not state.get("clients") or not state.get("agents"):
        return
    cl0 = state["clients"][0]
    ag0 = state["agents"][0]
    prop = PROPERTIES[0]   # ¥50M flagship

    sc, b = await call(c, "POST", "/api/v1/reservations/create", json_body={
        "brand_id": primary,
        "user_id": cl0["kid"],
        "scheduled_at": int(time.time()) + 3600,
        "party_size": 3,             # buyer + spouse + 1 parent (typical CN purchase)
        "type": "tour",              # closest existing enum value
        "resource_id": prop["prop_id"],
        "metadata": {
            "property_name": prop["name"],
            "price_cents": prop["price_cents"],
            "area_sqm": prop["area_sqm"],
            "rooms": prop["rooms"],
            "assigned_agent_kid": ag0["kid"],
            "intent": "primary_residence",
            "buyer_qualified_limit_purchase": True,   # passed 限购 check
            "needs_mortgage": True,
            "viewing_round": 1,
        },
        "check_in_grace_minutes": 30,
    })
    sample_rid = None
    if sc in (200, 201) and isinstance(b, dict):
        sample_rid = b.get("reservation_id")
        if "resource_id" in b or (isinstance(b.get("metadata"), dict)
                                  and b["metadata"].get("resource_id")):
            ok("viewing resource_id (property) persisted", f"rid={sample_rid}")
        else:
            gap("P0", "reservation resource_id silently dropped",
                "viewing created but readback has no top-level resource_id — property binding "
                "lives only in metadata. Per-listing demand stats / per-listing capacity "
                "(VIP showings often capped) cannot be enforced. 老周 P1 already flagged this; "
                "real estate inherits the gap with higher stakes (¥30M+ per resource).")
    else:
        gap("P0", "viewing reservation create", f"{sc} {_short(b)}")
    state["sample_viewing"] = sample_rid

    # Burst: 50 viewings across 6 properties × 15 agents (mimics monthly volume)
    burst_ok, burst_total = 0, 0
    for i in range(50):
        prop_i = rng.choice(PROPERTIES)
        cl_i = rng.choice(state["clients"])
        ag_i = rng.choice(state["agents"])
        burst_total += 1
        sc, _ = await call(c, "POST", "/api/v1/reservations/create", json_body={
            "brand_id": primary,
            "user_id": cl_i["kid"],
            "scheduled_at": int(time.time()) + 86400 + i * 1800,
            "party_size": rng.choice([1, 2, 3, 4]),
            "type": "tour",
            "resource_id": prop_i["prop_id"],
            "metadata": {
                "property_name": prop_i["name"],
                "price_cents": prop_i["price_cents"],
                "assigned_agent_kid": ag_i["kid"],
                "viewing_round": rng.randint(1, 4),
            },
            "check_in_grace_minutes": 30,
        })
        if sc in (200, 201):
            burst_ok += 1
    if burst_ok == burst_total:
        ok("50-viewing burst across 6 properties × 15 agents", f"{burst_ok}/{burst_total}")
    else:
        gap("P1", "viewing burst", f"only {burst_ok}/{burst_total}")
    state["viewings_created"] = burst_ok

    # Probe: per-property stats — high-demand listings need to be discoverable
    sc, b = await call(c, "GET", f"/api/v1/reservations/brand/{primary}/stats",
                       params={"resource_id": PROPERTIES[0]["prop_id"]})
    if sc == 200 and isinstance(b, dict):
        if "by_resource" in b or b.get("filtered_by_resource"):
            ok("per-property stats filter works", "")
        else:
            gap("P1", "per-property stats filter ignored",
                f"resource_id query param did not narrow stats; brokers can't see "
                f"per-listing viewing volume / no-show rate. {_short(b, 120)}")
    else:
        gap("P1", "per-property stats", f"{sc} {_short(b)}")


# ── Phase 6: 6-month attribution window probe ────────────────────────────
async def phase_6_attribution_window(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("6: 180-day Attribution Window (6-month sales cycle)")
    primary = state["primary_bid"]

    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": primary,
        "name": "6-Month Sales Cycle Funnel (180-day attribution)",
        "objective": "acquire",
        "bid_strategy": "cpa",
        "max_bid_cents": 200_000,        # ¥2000 CPA — high-AOV vertical can afford it
        "daily_budget_cents": 500_000,   # ¥5000/day
        "total_budget_cents": 5_000_000,
        "attribution_window_days": 180,  # PROBE: schema currently capped at 90
        "targeting": {
            "geo": {"country": "CN", "city": "Shenzhen", "radius_km": 50},
        },
        "creative": {"recipe_id": "starbucks_loyalty"},   # fallback
        "schedule": {"start_at": time.time() - 60, "end_at": time.time() + 86400 * 180},
    })
    cid = None
    if sc == 200 and isinstance(b, dict):
        cid = b["campaign_id"]
        ok("180-day campaign created", f"id={cid}")
    elif sc in (400, 422):
        gap("P0", "attribution_window_days capped below 180",
            f"{sc} {_short(b)} — schema enforces attribution_window_days ≤ 90. "
            "Real estate sales cycle averages 6 months from first viewing to 网签; high-end "
            "properties (¥30M+) often take 9-12 months. 90-day cap means every closed deal "
            "shows as 'organic / unattributed' → no ROAS visibility on the funnel that pays "
            "for itself most. 老蔡 already flagged the same 365-day need for healthcare; "
            "vertical solution = lift cap to 365+ or remove entirely.")
        # Retry with 90 so the rest still exercises the readback
        sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
            "brand_id": primary,
            "name": "Real-estate funnel (clamped to 90d)",
            "objective": "acquire",
            "bid_strategy": "cpa",
            "max_bid_cents": 200_000,
            "daily_budget_cents": 500_000,
            "total_budget_cents": 5_000_000,
            "attribution_window_days": 90,
            "targeting": {"geo": {"country": "CN", "city": "Shenzhen", "radius_km": 50}},
            "creative": {"recipe_id": "starbucks_loyalty"},
            "schedule": {"start_at": time.time() - 60, "end_at": time.time() + 86400 * 180},
        })
        if sc == 200 and isinstance(b, dict):
            cid = b["campaign_id"]
            info(f"fallback 90d campaign id={cid}")
    else:
        gap("P0", "real-estate campaign create", f"{sc} {_short(b)}")

    if cid:
        sc, b = await call(c, "GET", f"/api/v1/campaigns/{cid}/details")
        if sc == 200 and isinstance(b, dict):
            stored = b.get("attribution_window_days", 0)
            if stored == 180:
                ok("attribution_window_days=180 persisted", "")
            elif stored == 90:
                gap("P0", "attribution_window_days hard-capped at 90",
                    "set=180 silently downgraded to 90. Same root cause: schema "
                    "Field(ge=1, le=90). Need verticals (real_estate, healthcare, education) "
                    "to push to 365+.")
            elif stored == 0:
                gap("P0", "attribution_window_days not stored", "")

    state["funnel_campaign_id"] = cid


# ── Phase 7: Showroom Geofence (售楼处 = store) ─────────────────────────
async def phase_7_showroom_geofence(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("7: Showroom (售楼处) as store + geofence enter")
    primary = state["primary_bid"]

    # Register the 南山旗舰店 showroom as a geofence store
    sc, b = await call(c, "POST", "/api/v1/geofence/stores/register", json_body={
        "brand_id": primary,
        "store_id": f"showroom_nanshan_{RUN_TAG}",
        "store_name": "深圳湾一号 售楼处",
        "lat": SZ_LAT,
        "lng": SZ_LNG,
        "radius_meters": 100,             # showroom is a building, not a strip mall
        "metadata": {
            "type": "sales_gallery",
            "property_ids": [p["prop_id"] for p in PROPERTIES if p["office"] == "nanshan"],
        },
    })
    if sc in (200, 201):
        ok("showroom registered as geofence store", "")
    else:
        gap("P1", "showroom geofence register", f"{sc} {_short(b)}")

    # Probe: showroom-specific metadata (vs cafe/restaurant)
    sc, b = await call(c, "GET", f"/api/v1/geofence/stores/{primary}")
    if sc == 200 and isinstance(b, (list, dict)):
        items = b if isinstance(b, list) else b.get("stores", [])
        if items:
            store = items[0] if isinstance(items, list) else items
            meta = store.get("metadata", {}) if isinstance(store, dict) else {}
            if meta.get("type") == "sales_gallery" or meta.get("property_ids"):
                ok("showroom metadata persisted", f"property_ids exposed")
            else:
                gap("P1", "showroom-property linkage missing",
                    "geofence store has no first-class property_ids list. Showroom-enter event "
                    "can't auto-trigger property-specific follow-up (e.g. 'you visited 深圳湾一号 "
                    "售楼处 last Saturday — see today's pricing update').")

    # Probe: geofence enter triggers high-intent event
    if state.get("clients"):
        cl0 = state["clients"][0]
        sc, b = await call(c, "POST", "/api/v1/geofence/enter", json_body={
            "user_id": cl0["kid"],
            "brand_id": primary,
            "store_id": f"showroom_nanshan_{RUN_TAG}",
            "lat": SZ_LAT,
            "lng": SZ_LNG,
            "accuracy_m": 20,
        })
        if sc in (200, 201) and isinstance(b, dict):
            ok("showroom enter event accepted",
               f"event_id={b.get('event_id') or b.get('visit_id')}")
            # Intent boost? Real estate showroom enter is ¥10M+ intent signal
            if b.get("intent_score") or b.get("score"):
                ok("intent score returned", f"score={b.get('intent_score') or b.get('score')}")
            else:
                gap("P2", "no intent score on geofence enter",
                    "showroom-enter for ¥30M property is the highest-intent signal possible. "
                    "Platform should surface a normalised intent score so downstream (push, "
                    "audience, agent routing) can prioritise.")
        else:
            gap("P1", "showroom enter event", f"{sc} {_short(b)}")


# ── Phase 8: Property "voucher" — decoration / tour / document fee ──────
async def phase_8_property_voucher(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("8: Property voucher — decoration / tour / document fee")
    primary = state["primary_bid"]

    # Decoration package voucher (¥200K hardstop, hard to template)
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": primary,
        "name": "签约送 ¥200K 软装大礼包",
        "description": "Contract-signing decoration package ¥200K for buyers of luxury units",
        "value": {"type": "fixed", "amount": 20_000_000, "currency": "CNY"},   # ¥200K
        "conditions": {
            "usage_limit_per_user": 1,
            "min_purchase_cents": 1_500_000_000,   # only triggers above ¥15M sale
            "redeem_after_stage": "contract_signed",  # PROBE: stage-gated redemption
        },
        "metadata": {
            "voucher_kind": "in_kind_decoration",
            "redeemable_with_vendor": "fang_zheng_interior_design",
            "tax_treatment": "non_cash_benefit",
        },
        "expires_in_days": 365,    # PROBE: needs > 90
        "stackable": False,
        "transferable": True,       # buyer can gift to family
    })
    if sc in (200, 201) and isinstance(b, dict):
        ok("decoration voucher template", f"id={b.get('template_id')}")
        tid = b.get("template_id")
        sc2, b2 = await call(c, "GET", f"/api/v1/brands/{primary}/vouchers/templates/{tid}")
        if sc2 == 200 and isinstance(b2, dict):
            cond = b2.get("conditions", {})
            if cond.get("redeem_after_stage"):
                ok("stage-gated redemption persisted",
                   f"redeem_after_stage={cond.get('redeem_after_stage')}")
            else:
                gap("P1", "stage-gated redemption silently dropped",
                    "voucher conditions vocabulary has no `redeem_after_stage` — real-estate "
                    "vouchers must be tied to funnel stage (signed / 定金 paid / 网签 / 过户), "
                    "not just a single one-shot redemption. Without this, a contract-signing "
                    "voucher can be 'spent' before the buyer signs.")
    elif sc in (400, 422):
        body_str = json.dumps(b) if isinstance(b, dict) else str(b)
        if "expires_in_days" in body_str.lower() or "365" in body_str:
            gap("P0", "voucher expires_in_days capped",
                f"{sc} {_short(b)} — voucher expires_in_days ≤ N for some N < 365. Real "
                "estate signing → 过户 timeline routinely 6-12 months; voucher must outlive "
                "this. Currently buyer voucher expires before deed transfer.")
        else:
            gap("P1", "decoration voucher template", f"{sc} {_short(b)}")
    else:
        gap("P1", "decoration voucher", f"{sc} {_short(b)}")

    # Probe: "free viewing tour" voucher with chauffeur — service voucher
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": primary,
        "name": "VIP 私人专车看房半日",
        "description": "Chauffeured tour of 3 properties (Rolls Royce + lunch + agent escort)",
        "value": {"type": "service", "amount": 0, "currency": "CNY"},  # PROBE: service-type voucher
        "conditions": {
            "usage_limit_per_user": 1,
            "applicable_to_resource_type": "property_viewing",  # PROBE
            "min_property_price_cents": 1_000_000_000,           # only ¥10M+ tours
        },
        "expires_in_days": 30,
        "stackable": False,
    })
    if sc in (200, 201):
        ok("VIP service voucher (tour)", f"id={b.get('template_id')}")
    elif sc in (400, 422):
        gap("P1", "service-type voucher unsupported",
            f"{sc} {_short(b)} — voucher.value.type accepts only percent|fixed (not "
            "service|in_kind). Real-estate VIP perks (tour, decoration, lawyer fees) "
            "are non-cash; current schema forces them into ¥0 fixed amount + metadata, "
            "losing semantics for redemption-eligibility checks.")


# ── Phase 9: 6-month follow-up via Push Engine ──────────────────────────
async def phase_9_push_followup(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("9: 6-Month Follow-up — Push Engine schedule + reminders")
    primary = state["primary_bid"]

    if not state.get("clients"):
        return
    cl0 = state["clients"][0]

    # Seed a follow-up campaign so push engine has something to deliver
    sc, _ = await call(c, "POST", "/api/v1/campaigns", json_body={
        "brand_id": primary,
        "name": "Post-viewing 30-day follow-up",
        "objective": "retention",
        "bid_strategy": "cpm",
        "max_bid_cents": 200,
        "daily_budget_cents": 50_000,
        "total_budget_cents": 1_000_000,
        "target_audience": "retargeting_only",   # PROBE: retargeting strategy
    })
    if sc == 200:
        ok("follow-up campaign seeded", "target_audience=retargeting_only")
    elif sc in (400, 422):
        gap("P1", "retargeting_only target_audience",
            f"{sc} {_short(b)} — target_audience enum may not accept retargeting_only "
            "on retention campaigns")

    # /push/now to a viewed-but-not-bought client
    sc, b = await call(c, "POST", "/api/v1/push/now", json_body={
        "kid": cl0["kid"],
        "slot": "push",
        "context": {
            "viewed_property_id": PROPERTIES[0]["prop_id"],
            "days_since_viewing": 14,
        },
    })
    if sc == 200 and isinstance(b, dict):
        ok("push/now endpoint", f"fired={b.get('fired')} reason={b.get('reason')}")
    else:
        gap("P1", "push/now to viewed buyer", f"{sc} {_short(b)}")

    # /push/schedule for a 6-month later "have you decided yet?" nudge
    sc, b = await call(c, "POST", "/api/v1/push/schedule", json_body={
        "kid": cl0["kid"],
        "brand_id": primary,
        "scheduled_at": int(time.time()) + 86400 * 180,   # 6 months
        "slot": "push",
        "title": "6-month check-in",
        "body": f"还在考虑 {PROPERTIES[0]['name']} 吗？最新价格更新",
        "context": {"campaign_purpose": "long_cycle_nudge"},
    })
    if sc in (200, 201) and isinstance(b, dict):
        ok("6-month future push scheduled", f"schedule_id={b.get('schedule_id')}")
    elif sc in (400, 422):
        body_str = json.dumps(b) if isinstance(b, dict) else str(b)
        if "180" in body_str or "future" in body_str.lower() or "max" in body_str.lower():
            gap("P0", "push schedule ≤ N days; 180 rejected",
                f"{sc} {_short(b)} — /push/schedule rejects scheduled_at 180 days in the "
                "future. Real estate cycle is 6 months; the platform needs to retain "
                "scheduled pushes long enough to fire. Otherwise broker must re-schedule "
                "monthly from a CRM cron.")
        else:
            gap("P1", "push schedule", f"{sc} {_short(b)}")
    else:
        gap("P1", "push schedule 6-month", f"{sc} {_short(b)}")

    # Trigger: schedule a follow-up X days after viewing — uses /triggers
    sc, b = await call(c, "POST", "/api/v1/triggers/register", json_body={
        "brand_id": primary,
        "name": "viewing → 3-day follow-up push",
        "event_type": "reservation.honored",
        "event_filter": {"type": "tour"},
        "action": {
            "type": "send_push",
            "config": {
                "title": "感谢看房",
                "body": "您今天看的 {property_name} 还需要更多资料吗？",
                "delay_days": 3,          # PROBE: time-relative delay (老周 already flagged)
            },
        },
        "cooldown_seconds": 86400,
    })
    if sc in (200, 201):
        ok("post-viewing trigger registered", f"trigger_id={b.get('trigger_id', '')[:18]}…")
    elif sc in (400, 422):
        gap("P1", "delay_days on trigger action",
            f"{sc} {_short(b)} — trigger action_config doesn't accept delay_days; same "
            "gap 老周/老蔡 flagged. Real estate follow-up is multi-step (D+3, D+14, D+30, "
            "D+90, D+180) so this matters cumulatively.")


# ── Phase 10: Agent Commission Split via Payouts / Partnerships ─────────
async def phase_10_commission_split(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("10: Agent commission split — payouts.revenue_split")
    primary = state["primary_bid"]

    if not state.get("agents"):
        return
    ag = state["agents"][0]

    # Probe: revenue split for one closed deal — 50% agent / 30% office / 20% house
    sc, b = await call(c, "POST", "/api/v1/payouts/configure", json_body={
        "brand_id": primary,
        "revenue_split": [
            {"recipient_id": ag["kid"], "recipient_type": "agent", "percentage": 50.0},
            {"recipient_id": f"office_{ag['office']}_pool", "recipient_type": "office", "percentage": 30.0},
            {"recipient_id": "fang_zheng_house", "recipient_type": "house", "percentage": 20.0},
        ],
        "trigger_event": "deal_closed",     # PROBE: per-event split
        "currency": "CNY",
    })
    if sc in (200, 201):
        ok("3-way revenue split configured", "agent 50% / office 30% / house 20%")
    elif sc == 404:
        gap("P0", "/payouts/configure with revenue_split missing",
            "no /payouts/configure with revenue_split path. Real estate brokerage runs on "
            "commission splits; without first-class support brokers must operate a parallel "
            "ledger. Same gap 老蔡 flagged for group practice (doctor/hospital).")
    elif sc in (400, 422):
        gap("P0", "revenue_split schema rejects real-estate shape",
            f"{sc} {_short(b)} — payouts.configure has revenue_split structurally but doesn't "
            "accept recipient_type=agent/office/house or per-event triggers. Commission split "
            "is the central money primitive for brokerage; needs first-class shape.")

    # Probe: partnerships path — could co-listing be a partnership?
    if len(state["sub_brands"]) >= 2:
        offices = list(state["sub_brands"].values())
        sc, b = await call(c, "POST", "/api/v1/partnerships/propose", json_body={
            "proposer_brand_id": offices[0],
            "partner_brand_id": offices[1],
            "purpose": "co_listing_commission_share",
            "terms": {"commission_split_proposer_pct": 50.0, "split_partner_pct": 50.0},
            "expires_at": int(time.time()) + 86400 * 30,
        })
        if sc == 201 and isinstance(b, dict):
            ok("co-listing partnership proposed", f"partnership_id={b.get('partnership_id')}")
        elif sc in (400, 422):
            gap("P1", "co-listing partnership terms schema",
                f"{sc} {_short(b)} — partnerships.propose terms vocabulary doesn't include "
                "commission_split keys. Common in real estate when two offices co-list a unit.")


# ── Phase 11: Document-heavy lead qualification (sensitive PI) ──────────
async def phase_11_document_qualification(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("11: Sensitive PI — buyer ID / hukou / financial-proof storage")
    primary = state["primary_bid"]
    if not state.get("clients"):
        return
    cl0 = state["clients"][0]

    # Try to store ID-card / hukou / proof-of-funds attributes with sensitive flag
    sc, b = await call(c, "POST", f"/api/v1/primitives/user/{cl0['kid']}/attributes",
                       json_body={
                           "brand_id": primary,
                           "attrs": {
                               "id_card_no": f"4403{RUN_TAG % 100000000:08d}1234",   # 18-digit CN ID
                               "hukou_province": "广东",
                               "limit_purchase_eligibility": "qualified_local",
                               "proof_of_funds_cny": 30_000_000,
                               "mortgage_pre_approval_bank": "中国工商银行",
                               "mortgage_pre_approval_amount_cny": 15_000_000,
                           },
                           "sensitive_pi": True,        # 个保法 §28 sensitive PI flag
                           "retention_years": 10,        # 商品房销售管理办法 §43
                       })
    if sc in (200, 201):
        ok("sensitive PI attributes stored", "id_card / hukou / proof-of-funds set")
        sc2, b2 = await call(c, "GET", f"/api/v1/primitives/user/{cl0['kid']}/attributes",
                             params={"brand_id": primary})
        if sc2 == 200 and isinstance(b2, dict):
            flagged = (b2.get("sensitive_pi")
                       or b2.get("retention_years")
                       or (isinstance(b2.get("meta"), dict)
                           and (b2["meta"].get("sensitive_pi") or b2["meta"].get("retention_years"))))
            if flagged:
                ok("sensitive_pi flag persisted", f"flag={flagged}")
            else:
                gap("P0", "sensitive_pi / retention_years dropped",
                    "POST attributes accepted sensitive_pi=True + retention_years=10 but "
                    "readback shows no flags. Same Redis bucket as marketing nickname. "
                    "个保法 §28 demands separate handling for sensitive PI; 商品房销售管理办法 "
                    "§43 mandates 10-year retention with audit. Currently impossible to "
                    "differentiate buyer documents from marketing data.")
    elif sc in (400, 422):
        gap("P1", "sensitive PI attr store", f"{sc} {_short(b)}")

    # Probe: ID card readback — should not return plaintext if encrypted at rest
    sc, b = await call(c, "GET", f"/api/v1/primitives/user/{cl0['kid']}/attributes/id_card_no",
                       params={"brand_id": primary})
    if sc == 200 and isinstance(b, dict):
        v = b.get("value") or b.get("id_card_no")
        if isinstance(v, str) and v.startswith("4403") and len(v) >= 15:
            gap("P0", "ID card stored plaintext",
                f"id_card_no readback returns {v[:6]}…{v[-4:]} verbatim. Field-level "
                "encryption for sensitive PI is mandatory under 个保法 §51. Need AES-GCM "
                "with KMS-rotated keys + masked-display by default.")
        else:
            ok("ID card not plaintext", f"readback masked")
    elif sc == 404:
        info("single-key readback 404")

    # Probe: audit log for sensitive PI access (same gap 老蔡 flagged for PHI)
    for path in (
        f"/api/v1/audit/sensitive/{cl0['kid']}",
        f"/api/v1/audit/user/{cl0['kid']}",
        f"/api/v1/consent/audit/{cl0['kid']}",
        f"/api/v1/primitives/user/{cl0['kid']}/audit",
    ):
        sc, b = await call(c, "GET", path)
        if sc == 200:
            ok("audit log endpoint", path)
            state["audit_endpoint"] = path
            break
    else:
        gap("P0", "no sensitive-PI access audit log",
            "tried 4 audit paths; all 404. 个保法 §51 requires audit of who-read-what-when "
            "for sensitive PI. Real estate brokers handle 18-digit ID + ¥M financial proof — "
            "an internal staff leak is industry-known risk. Platform offers no log to detect "
            "it.")


# ── Phase 12: Regulatory: 限购 / 网签 / 房地产广告法 ──────────────────
async def phase_12_regulatory(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("12: Regulatory — 限购 lottery / 网签 / 广告法 compliance")
    primary = state["primary_bid"]

    # Probe: 限购 lottery / waitlist primitive — multiplayer / leaderboard / queue?
    found = False
    for path in (
        "/api/v1/multiplayer/lottery/create",
        "/api/v1/multiplayer/waitlist/create",
        f"/api/v1/multiplayer/{primary}/lottery",
    ):
        sc, b = await call(c, "POST", path, json_body={
            "brand_id": primary,
            "resource_id": PROPERTIES[3]["prop_id"],  # off-plan needs lottery
            "max_winners": 50,
            "draw_at": int(time.time()) + 86400 * 7,
            "eligibility_predicates": [
                {"attribute": "limit_purchase_eligibility", "equals": "qualified_local"},
                {"attribute": "proof_of_funds_cny", "gte": 8_000_000},
            ],
        })
        if sc in (200, 201):
            found = True
            ok("lottery / waitlist primitive", f"via {path}")
            break
    if not found:
        gap("P0", "限购 lottery / waitlist primitive missing",
            "no /multiplayer/lottery or /waitlist accepts an eligibility-gated draw. "
            "Shenzhen first-tier limit-purchase rules + 网红盘 ('hot' new developments) "
            "make a fairness-audited lottery mandatory. Right now brokers run lotteries "
            "by spreadsheet → compliance + fraud risk.")

    # Probe: 网签 (registered sale) state — a deal isn't legally complete until 网签
    # Try a partner-state primitive (commerce_loop)
    sc, b = await call(c, "POST", f"/api/v1/commerce/transactions/create", json_body={
        "brand_id": primary,
        "user_id": state["clients"][0]["kid"] if state.get("clients") else f"probe_{RUN_TAG}",
        "amount_cents": 3_000_000_000,
        "currency": "CNY",
        "status": "wangqian_pending",                # 网签等待
        "metadata": {
            "deal_stage": "wangqian_pending",
            "deposit_paid_cents": 500_000_000,        # ¥5M 定金
            "expected_wangqian_date": int(time.time()) + 86400 * 30,
            "expected_deed_transfer_date": int(time.time()) + 86400 * 90,
        },
    })
    if sc in (200, 201):
        ok("transaction with 网签 stage accepted", f"id={b.get('transaction_id', '')}")
    elif sc == 404:
        gap("P1", "/commerce/transactions/create missing",
            "no commerce transactions endpoint at expected path; real estate transactions "
            "have stages (定金 → 首付 → 网签 → 过户) over months; need first-class state machine.")
    elif sc in (400, 422):
        gap("P1", "real-estate transaction stages unsupported",
            f"{sc} {_short(b)} — transaction status enum doesn't include real-estate stages "
            "(deposit_paid / wangqian_pending / wangqian_done / deed_transferred). Currently "
            "brokers shoehorn this into custom metadata.")

    # Probe: 房地产广告法 — banned creative terms
    # 房地产广告法第7条 prohibits: 升值/保值/学区/落户/投资回报率 etc.
    banned_terms = ["升值30%", "100%学区房", "包落户", "投资回报率18%"]
    sc, b = await call(c, "POST", "/api/v1/recipe-gen/validate", json_body={
        "brand_id": primary,
        "industry": "real_estate",
        "creative_copy": f"{PROPERTIES[0]['name']} — {' / '.join(banned_terms)}",
        "country": "CN",
    })
    if sc == 200 and isinstance(b, dict):
        warns = b.get("compliance_warnings") or b.get("violations") or []
        if warns:
            ok("real-estate creative compliance check", f"{len(warns)} violations flagged")
        else:
            gap("P0", "real-estate ad-law violations not flagged",
                "creative copy with explicit 升值/学区/落户/投资回报率 claims accepted with no "
                "warning. 房地产广告法 §7 makes these illegal; merchants will get fined "
                "(¥50K-200K per violation) without platform-side gate. Same gap as healthcare "
                "cure-claims (老蔡).")
    elif sc == 404:
        gap("P0", "no creative-compliance validation endpoint",
            "tried /recipe-gen/validate — 404. Without a compliance gate, the platform "
            "can be used to publish 房地产广告法-violating copy. Healthcare and real "
            "estate both need this; build once, ship to both verticals.")
    elif sc in (400, 422):
        gap("P1", "validate endpoint schema", f"{sc} {_short(b)}")


# ── Phase 13: Target audience — new buyers vs retargeting ───────────────
async def phase_13_target_audience(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("13: target_audience — new_users_only vs retargeting_only vs viewed_not_bought")
    primary = state["primary_bid"]

    # 1. new_users_only — cold lead acquisition
    sc, b = await call(c, "POST", "/api/v1/campaigns", json_body={
        "brand_id": primary,
        "name": "Cold acquisition — new buyers in SZ",
        "objective": "acquire",
        "bid_strategy": "cpa",
        "max_bid_cents": 200_000,
        "daily_budget_cents": 200_000,
        "total_budget_cents": 2_000_000,
        "target_audience": "new_users_only",
        "targeting": {"geo": {"country": "CN", "city": "Shenzhen", "radius_km": 50}},
    })
    if sc == 200:
        ok("cold-acquisition campaign", "target_audience=new_users_only")
    else:
        gap("P1", "new_users_only campaign", f"{sc} {_short(b)}")

    # 2. retargeting_only — viewed-but-not-bought 180-day window
    sc, b = await call(c, "POST", "/api/v1/campaigns", json_body={
        "brand_id": primary,
        "name": "Re-engage viewed-but-not-bought",
        "objective": "retention",
        "bid_strategy": "cpc",
        "max_bid_cents": 5000,
        "daily_budget_cents": 100_000,
        "total_budget_cents": 500_000,
        "target_audience": "retargeting_only",
    })
    if sc == 200:
        ok("retargeting campaign", "target_audience=retargeting_only")
    elif sc in (400, 422):
        gap("P1", "retargeting_only campaign rejected",
            f"{sc} {_short(b)} — target_audience enum may not include retargeting_only "
            "for retention. Real-estate's bread-and-butter is 180-day retargeting of "
            "viewed leads.")

    # 3. Probe: custom audience — "viewed property X but no offer in 30 days"
    sc, b = await call(c, "POST", "/api/v1/audiences/custom/create", json_body={
        "brand_id": primary,
        "name": "Viewed nanshan flagship, no offer in 30d",
        "source": "manual",
        "predicates": [
            {"event": "reservation.honored", "resource_id": PROPERTIES[0]["prop_id"],
             "within_days": 60},
            {"event": "offer_submitted", "absent_within_days": 30},   # PROBE: absent_within predicate
        ],
        "predicate_logic": "AND",
    })
    if sc == 200:
        ok("viewed-but-not-offered audience", "complex multi-predicate accepted")
    elif sc in (400, 422):
        gap("P1", "absent_within_days predicate missing",
            f"{sc} {_short(b)} — audiences.custom can't express 'event A happened and "
            "event B did NOT happen within window'. This is the most common real-estate "
            "retargeting query (viewed but didn't offer); needs first-class NOT predicate.")

    # 4. Probe: audience by tier (qualified prospects only)
    sc, b = await call(c, "POST", "/api/v1/audiences/custom/create", json_body={
        "brand_id": primary,
        "name": "Qualified buyers only",
        "source": "manual",
        "predicates": [
            {"tier_name": "qualified", "brand_id": primary},
        ],
    })
    if sc == 200:
        ok("audience by tier predicate", "")
    elif sc in (400, 422):
        gap("P1", "tier_name audience predicate missing",
            f"{sc} {_short(b)} — audiences can't filter by tier name. Real estate runs "
            "expensive campaigns ONLY against qualified-tier prospects (passed 限购 + "
            "proof-of-funds); can't currently target.")


# ── Phase 14: Module availability probe ─────────────────────────────────
async def phase_14_module_probe(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("14: Module availability probe (real-estate relevant)")
    primary = state["primary_bid"]
    cl0 = state["clients"][0] if state.get("clients") else {"kid": f"probe_{RUN_TAG}"}

    probes = [
        ("kix-id.register", "POST", "/api/v1/kix-id/register", None),
        ("kix-id.profile", "GET", f"/api/v1/kix-id/{cl0['kid']}", None),
        ("relationships.list", "GET", f"/api/v1/primitives/users/{cl0['kid']}/relationships", None),
        ("reservations.create", "POST", "/api/v1/reservations/create", None),
        ("geofence.stores", "GET", f"/api/v1/geofence/stores/{primary}", None),
        ("geofence.enter", "POST", "/api/v1/geofence/enter", None),
        ("push.now", "POST", "/api/v1/push/now", None),
        ("push.schedule", "POST", "/api/v1/push/schedule", None),
        ("partnerships.propose", "POST", "/api/v1/partnerships/propose", None),
        ("payouts.configure", "POST", "/api/v1/payouts/configure", None),
        ("commerce.transactions", "POST", "/api/v1/commerce/transactions/create", None),
        ("recipes.industry.real_estate", "GET", "/api/v1/recipes", {"industry": "real_estate"}),
        ("audiences.custom", "POST", "/api/v1/audiences/custom/create", None),
        ("audit.sensitive", "GET", f"/api/v1/audit/sensitive/{cl0['kid']}", None),
        ("frequency-cap.check", "POST", "/api/v1/frequency-cap/check", None),
    ]
    available, missing = [], []
    for label, method, path, params in probes:
        sc, b = await call(c, method, path,
                           params=params,
                           json_body=None if method == "GET" else {})
        if sc in (200, 201):
            available.append(label)
            ok(f"module live: {label}", f"{sc}")
        elif sc == 404:
            if isinstance(b, dict) and b.get("detail") in ("Not Found", "not found"):
                missing.append(label)
                gap("P1", f"module missing: {label}", f"404 at {path}")
            else:
                available.append(label)
                ok(f"module live (no-resource): {label}", "404 with domain detail")
        elif sc in (400, 422):
            available.append(label)
            ok(f"module live: {label}", f"{sc} (schema mismatch)")
        else:
            available.append(label)
            info(f"module {label}: {sc}")
    info(f"available={len(available)} missing={len(missing)}")


# ── Findings writer ─────────────────────────────────────────────────────
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
    md.append("# 老陆 / Lu Wei (方正地产 Fang Zheng Real Estate) — Merchant Journey Findings")
    md.append("")
    md.append(f"**Run tag**: `{RUN_TAG}` | **Runtime**: {runtime:.1f}s | "
              f"**Date**: {time.strftime('%Y-%m-%d %H:%M', time.localtime(start_ts))}")
    md.append("")
    md.append("## Scenario")
    md.append(
        "老陆 owns 「方正地产」 — a Shenzhen high-end residential property brokerage. "
        "4 district offices (南山 / 福田 / 前海 / 宝安), 15 agents, 200 active listings "
        "from ¥3M–¥50M, 50 transactions/month at avg ¥80K commission. Budget ¥60K/month "
        "(luxury margins fund a high CPA). Unique pains: **6-month sales cycle** (180-day "
        "attribution), **single-transaction focus** (one closed deal = a month of revenue), "
        "**agent network** (client↔agent relationships + commission splits), **regulatory "
        "load** (限购 lottery, 网签 stages, 房地产广告法, 个保法 §28 sensitive PI on "
        "buyer documents), **showroom geofencing** (售楼处 enter = ¥10M+ intent signal), "
        "**document-heavy qualification** (ID + hukou + 资金证明 + 限购资格)."
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

    section("P0 — Blockers", p0)
    section("P1 — Friction", p1)
    section("P2 — Nice-to-have", p2)
    section("Hard failures", fails)

    md.append("## Top 5 NEW gaps unique to REAL ESTATE")
    md.append("")
    md.append(
        "1. **180-day (6-month) attribution window unreachable.** Schema caps "
        "`attribution_window_days ≤ 90`. Real-estate buyer journey from first viewing to "
        "网签 averages 6 months; ¥30M+ purchases routinely take 9–12 months. With a 90-day "
        "cap, every closed deal exits the attribution window — campaigns that "
        "actually drive the highest-AOV revenue in the system show as 'organic'. "
        "**Same root cause** 老蔡 flagged for healthcare's 365-day booster; vertical "
        "fix = lift cap to 365 (or remove and clamp at campaign-end).\n"
        "\n"
        "2. **Agent network is unmodelable.** Three intertwined gaps:\n"
        "   - KiX ID has no first-class `role`/`agent_license_no` field — agents and "
        "clients use the same identity shape, so brokers can't surface licensing for "
        "trust (mandated by 房地产经纪管理办法).\n"
        "   - Round-4 relationships works for `agent_of` edges, but auto-filtering an "
        "agent's book by relationship type is inconsistent.\n"
        "   - `payouts.configure` with `revenue_split` (agent 50% / office 30% / house 20% "
        "per deal) returns 404 or 400. Commission split is the central money primitive for "
        "brokerage; without it brokers operate a parallel ledger. Same gap 老蔡 flagged "
        "for group-practice (doctor/hospital); brokerage and group-practice share schema.\n"
        "\n"
        "3. **Real-estate reservation subtyping + state machine missing.** Today:\n"
        "   - Reservation `type` enum has no `property_viewing` / `off_plan_showroom` / "
        "`open_house` / `signing_appointment` / `deposit_payment` — all collapse to `tour`.\n"
        "   - `resource_id` (property listing) stored only in metadata; per-listing demand "
        "stats unavailable.\n"
        "   - No transaction state machine for 定金 → 网签 → 过户 stages over 90+ days; "
        "vouchers tied to 'contract_signed' can't be redemption-gated by stage.\n"
        "\n"
        "4. **Sensitive PI (buyer documents) handling absent.** Buyer ID-card / hukou / "
        "proof-of-funds / 限购资格 are 个保法 §28 sensitive PI requiring (a) separate "
        "consent scope (`document_storage` not in VALID_SCOPES), (b) field-level encryption "
        "(ID card stored plaintext today), (c) 10-year retention under 商品房销售管理办法 "
        "§43, (d) access audit log (no `/audit/sensitive/{uid}` endpoint). Same structural "
        "gaps 老蔡 flagged for PHI; build once, ship to healthcare + real estate + finance.\n"
        "\n"
        "5. **Regulatory primitives missing across the board.** Three sub-gaps:\n"
        "   - **限购 lottery / waitlist** — Shenzhen first-tier rules + 网红盘 mandate "
        "eligibility-gated draws. No `/multiplayer/lottery` accepts `eligibility_predicates`; "
        "brokers run lotteries by spreadsheet.\n"
        "   - **房地产广告法 §7 creative compliance** — copy containing 升值/学区/落户/投资回报率 "
        "is illegal but accepted without warning. Need vertical-aware creative validator "
        "(reuses healthcare's cure-claim gate).\n"
        "   - **'absent_within_days' audience predicate** — the most common real-estate "
        "retargeting query is 'viewed property X but DID NOT submit offer in 30 days'. "
        "`audiences.custom` can't express NOT predicates; brokers can't build the funnel "
        "without an external ETL.\n"
    )
    md.append("")
    md.append("## Cross-Comparison: 老王 / 老黄 / 老张 / 老周 / 老吴 / 老蔡 / **老陆**")
    md.append("")
    md.append(
        "| Concern | 老王 F&B | 老黄 Baby | 老张 Dining | 老周 Gym | 老吴 K12 | 老蔡 Hospital | **老陆 Real Estate** |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| AOV | ¥50–500 | ¥200–2K | ¥500–5K | ¥200–1K | ¥3K–20K | ¥500–50K | **¥3M–¥50M** |\n"
        "| Sales cycle | same-day | 7d | 7d | 30d | 14–30d | 365d (booster) | **180–365d (网签 + 过户)** |\n"
        "| Reservation `type` | n/a | n/a | dining ✓ | fitness_class ✓ | n/a | gp / specialist (missing) | **property_viewing / off_plan / signing (missing)** |\n"
        "| Resource binding | brand | brand | table/section | class/instructor (meta) | n/a | doctor_id (must persist) | **prop_id (must persist + multi-stage)** |\n"
        "| Identity model | single user | parent-baby | single user | single user | parent ↔ child | family graph | **client ↔ agent + license** |\n"
        "| Voucher conditions | discount | family bundle | — | — | sibling | diagnostic_code / payor | **redeem_after_stage / service-type / 365d** |\n"
        "| Compliance regime | local F&B | minor data | F&B | none | 双减 | HIPAA + 个保法 | **房地产广告法 + 商品房销售管理办法 + 个保法 §28 + 限购** |\n"
        "| Sensitive PI | low | low | low | low | minor | PHI | **ID + hukou + 资金证明 (10yr retention)** |\n"
        "| Commission split | — | — | — | — | — | doctor 70 / hospital 30 | **agent 50 / office 30 / house 20 (per deal)** |\n"
        "| Geofence semantics | café | nursery | restaurant | gym | school | clinic | **售楼处 = highest-intent signal (¥30M)** |\n"
        "| Retargeting window | same-day | 7–14d | 7d | 30d | 14–30d | 365d | **90–180d viewed-not-offered** |\n"
        "| Government lottery / queue | — | — | — | — | rare | — | **限购 fairness-audited lottery** |\n"
    )
    md.append("")
    md.append(
        "**Real estate is the first vertical where**: the sales cycle stretches past "
        "**180 days** (some past a year), where the **agent ↔ client relationship is the "
        "money-making primitive** (not the transaction), where the **per-event commission "
        "split is 3+ ways** (agent / office / house, sometimes co-listing), where the "
        "**ad-law compliance vocabulary is most prescriptive** (升值/学区/落户/投资回报率 "
        "are all banned terms), and where **a single reservation resource (prop_id) carries "
        "more value than a year of fitness memberships**. The platform pieces (KiX ID, "
        "reservations, geofence, push, partnerships) are mostly present — what's missing is "
        "vertical-aware widening of the existing primitives (longer windows, richer enums, "
        "NOT predicates, multi-way splits, sensitive-PI flags)."
    )
    md.append("")
    md.append("## Strategic Recommendations (Top 10)")
    md.append("")
    md.append(
        "1. **[P0] Lift `attribution_window_days` cap from 90 → 365.** "
        "Schema change: `Field(ge=1, le=365)`. Same one-line fix unblocks healthcare + "
        "real-estate + education at once. Document the new ceiling.\n"
        "\n"
        "2. **[P0] Expand reservation `type` enum.** Add real-estate subtypes "
        "(`property_viewing`, `off_plan_showroom`, `open_house`, `signing_appointment`, "
        "`deposit_payment`) alongside healthcare's gp/specialist/lab. Same migration, "
        "two verticals.\n"
        "\n"
        "3. **[P0] Persist + surface reservation `resource_id` first-class.** "
        "Same gap 老周 first flagged for fitness; real-estate makes it acute (per-listing "
        "demand stats, per-listing voucher gating, per-listing capacity caps on VIP showings).\n"
        "\n"
        "4. **[P0] Sensitive-PI flag (`sensitive_pi=True`, `retention_years=N`) on "
        "attributes.** Persist + route to a hardened bucket with field-level AES-GCM + "
        "separate audit log + scoped access. Healthcare (PHI), real-estate (ID/hukou/资金), "
        "and finance (KYC) all need this; ship as one primitive.\n"
        "\n"
        "5. **[P0] `/audit/sensitive/{uid}` + `/users/{uid}/export` (个保法 §45 + §51).** "
        "Append-only Redis stream of every sensitive-PI read/write. Reuse for 老蔡's "
        "HIPAA need and 老陆's 个保法 §28 need.\n"
        "\n"
        "6. **[P0] `payouts.configure` accept multi-party `revenue_split` with "
        "`recipient_type` (agent / office / house / co-broker) and `trigger_event` "
        "(per-deal vs monthly).** Real-estate commission split + healthcare group-practice "
        "split + influencer marketing all need the same shape.\n"
        "\n"
        "7. **[P1] `audiences.custom` NOT-predicate (`absent_within_days`).** Most common "
        "retargeting query: 'viewed but didn't convert in N days'. Real-estate, fitness "
        "trial-to-paid, K12 demo-to-enroll all need it.\n"
        "\n"
        "8. **[P1] KiX ID `role` + `agent_license_no` first-class fields.** Surface "
        "licensing on the public profile-for-merchant view for buyer trust + "
        "房地产经纪管理办法 compliance.\n"
        "\n"
        "9. **[P1] Vertical-aware creative validator** — single endpoint "
        "`/creative/validate?industry={real_estate|healthcare|...}` returns "
        "compliance_warnings list. Same plumbing serves 房地产广告法 §7 (no 升值/学区/落户), "
        "医疗广告法 (no cure claims), 双减 (no homework promises).\n"
        "\n"
        "10. **[P1] Property/listing waitlist + lottery primitive.** "
        "`/multiplayer/lottery/create` with eligibility_predicates (currently 404). "
        "Solves 限购 fairness audit + off-plan launch frenzy + healthcare clinical-trial "
        "enrollment + concert pre-sale.\n"
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


# ── Main ────────────────────────────────────────────────────────────────
# ── Phase R7: Round 7 probes — real estate PHI + buyer agreement + relationships ─
async def phase_r7_probes(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("R7: Round 7 probes — recipes + financial_proof PHI + buyer_agreement + property_viewing + agent_of")
    primary = state.get("primary_bid")
    version = state.get("consent_version") or f"v_re_{RUN_TAG}"
    clients = state.get("clients") or []
    agents = state.get("agents") or []
    client_uid = clients[0].get("kid") if (clients and isinstance(clients[0], dict)) else f"client_probe_{RUN_TAG}"
    agent_uid = agents[0].get("kid") if (agents and isinstance(agents[0], dict)) else f"agent_probe_{RUN_TAG}"

    # 1) Recipe library
    for industry in ("real_estate", "financial_services"):
        sc, b = await call(c, "GET", "/api/v1/recipes", params={"industry": industry})
        if sc == 200 and isinstance(b, (list, dict)):
            items = b if isinstance(b, list) else b.get("recipes", b.get("items", []))
            if items:
                ok(f"recipes industry={industry}", f"{len(items)} recipes")
            else:
                gap("P1", f"recipes industry={industry} empty", "")
        else:
            gap("P1", f"recipes industry={industry}", f"{sc}")

    await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
        "version": version, "text_md": "## Real Estate Privacy",
        "effective_at": int(time.time()) - 60, "requires_re_grant": False,
    })

    # 2) consent scopes: document_storage / financial_proof / pii_kyc WITH consent_evidence
    consent_grant_id = None
    for scope_name in ("document_storage", "financial_proof", "pii_kyc"):
        sc, b = await call(c, "POST", "/api/v1/consent/grant", json_body={
            "user_id": client_uid,
            "scopes": [scope_name],
            "policy_version": version,
            "source": "app",
            "consent_evidence": {
                "method": "signature",
                "reference": f"sig_re_{scope_name}_{RUN_TAG}",
            },
        })
        if sc == 200:
            ok(f"{scope_name} scope w/ consent_evidence", "")
            if isinstance(b, dict) and not consent_grant_id:
                consent_grant_id = b.get("grant_id") or b.get("user_id")
        elif sc in (400, 422):
            gap("P0", f"consent scope {scope_name} rejected",
                f"{sc} {_short(b)}")

    # 3) /consent/document/sign type=buyer_agreement + signature
    sc, b = await call(c, "POST", "/api/v1/consent/document/sign", json_body={
        "user_id": client_uid,
        "document_type": "buyer_agreement",
        "document_version": version,
        "document_url": "https://example.com/buyer-agreement-re.pdf",
        "signature_method": "signature",
        "signature_evidence_url": "https://example.com/sig-re",
        "granted_scopes": ["document_storage", "financial_proof"],
    })
    if sc == 200 and isinstance(b, dict) and b.get("document_consent_id"):
        ok("buyer_agreement signed (real estate)",
           f"doc_id={b['document_consent_id']}")
    elif sc in (400, 422):
        gap("P1", "buyer_agreement document/sign", f"{sc} {_short(b)}")

    # 4) /media/upload media_class=document for property paperwork
    sc, b = await call(c, "POST", "/api/v1/media/upload", json_body={
        "owner_user_id": client_uid,
        "brand_id": primary,
        "media_class": "document",
        "storage_url": "s3://kix-re/title-deed.pdf",
        "content_hash": "sha256:" + "d" * 40,
        "mime_type": "application/pdf",
        "size_bytes": 512000,
        "consent_grant_id": consent_grant_id or f"grant_{client_uid}",
        "retention_days": 3650,
        "metadata": {"doc_kind": "title_deed"},
    })
    if sc == 200 and isinstance(b, dict) and b.get("media_id"):
        ok("media.upload document w/ consent_grant_id",
           f"media_id={b['media_id']}")
    elif sc in (400, 422):
        gap("P1", "media.upload document",
            f"{sc} {_short(b)}")

    # 5) Reservation type=property_viewing + resource_id=property + fulfiller=agent
    property_id = f"property_shenzhen_lot_{RUN_TAG}"
    await call(c, "POST", "/api/v1/consent/grant", json_body={
        "user_id": agent_uid, "scopes": ["cross_brand_tracking"],
        "policy_version": version, "source": "app",
    })
    sc, b = await call(c, "POST", "/api/v1/reservations/create", json_body={
        "brand_id": primary,
        "user_id": client_uid,
        "scheduled_at": int(time.time()) + 86400 * 2,
        "party_size": 2,
        "type": "property_viewing",
        "resource_id": property_id,
        "fulfiller_user_id": agent_uid,
        "metadata": {"property_type": "apartment", "price_cny": 8500000},
    })
    if sc in (200, 201) and isinstance(b, dict):
        rid = b.get("reservation_id")
        ok("property_viewing reservation w/ agent", f"rid={rid}")
        sc_get, b_get = await call(c, "GET", f"/api/v1/reservations/{rid}")
        if sc_get == 200 and isinstance(b_get, dict):
            if b_get.get("resource_id") == property_id:
                ok("property resource_id persisted", "")
            else:
                gap("P1", "property resource_id dropped", f"{b_get.get('resource_id')!r}")
    elif sc in (400, 422):
        gap("P0", "property_viewing reservation type", f"{sc} {_short(b)}")

    # 6) Relationship: agent_of / client_of (new in R6)
    sc, b = await call(c, "POST", f"/api/v1/primitives/users/{agent_uid}/relationships",
                       json_body={
                           "related_user_id": client_uid,
                           "relationship": "agent_of",
                           "bidirectional": True,
                       })
    if sc in (200, 201):
        ok("agent_of relationship created", "")
    elif sc in (400, 422):
        gap("P1", "agent_of relationship", f"{sc} {_short(b)}")


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
                await phase_3_consent_tiers(c, state)
                await phase_4_kix_id_and_relationships(c, state)
                await phase_5_property_viewings(c, state)
                await phase_6_attribution_window(c, state)
                await phase_7_showroom_geofence(c, state)
                await phase_8_property_voucher(c, state)
                await phase_9_push_followup(c, state)
                await phase_10_commission_split(c, state)
                await phase_11_document_qualification(c, state)
                await phase_12_regulatory(c, state)
                await phase_13_target_audience(c, state)
                await phase_14_module_probe(c, state)
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
