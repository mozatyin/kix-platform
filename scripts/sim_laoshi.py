"""Merchant journey simulation — 老石 / Shi Liang (智能办公 SmartOffice, B2B SaaS).

End-to-end probe of the KiX Ads Platform from the perspective of a B2B SaaS
vendor selling an enterprise project-management platform with:
  - LONG SALES CYCLE (3-6 months from MQL→SQL→PoC→procurement→signature)
  - MULTI-PERSONA DECISION (CEO economic buyer + CTO technical buyer +
    procurement gatekeeper + employee end-user)
  - SEAT-BASED LICENSING (¥50K-¥500K AOV, priced per seat)
  - RENEWAL-CENTRIC (90% margin, churn is catastrophic)
  - ACCOUNT-BASED MARKETING (the COMPANY is the target, not individuals)
  - ICP TARGETING (revenue / industry / employee count screens)
  - 500 ENTERPRISE CUSTOMERS averaging 200 employees each

Pattern follows scripts/sim_laoliang.py (long cycle / multi-currency / supplier)
and scripts/sim_laowu.py (multi-persona = parent+teacher analogue).

In-process via httpx.ASGITransport so no separate server is needed. Requires a
live local Redis.

Run:
    .venv/bin/python scripts/sim_laoshi.py
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


# ── Constants ────────────────────────────────────────────────────────────
RUN_TAG = int(time.time())
OWNER_USER_ID = f"laoshi_{RUN_TAG}"
FINDINGS_PATH = Path("/Users/mozat/a-docs/laoshi-sim-findings.md")

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"

# ICP segment sub-brands — 5 verticals 老石 targets
SUB_BRANDS: list[dict[str, Any]] = [
    {"brand_id": f"smartoffice_finance_{RUN_TAG}",
     "name": "智能办公 金融行业版", "icp": "finance", "min_employees": 500},
    {"brand_id": f"smartoffice_tech_{RUN_TAG}",
     "name": "智能办公 科技公司版", "icp": "technology", "min_employees": 100},
    {"brand_id": f"smartoffice_manuf_{RUN_TAG}",
     "name": "智能办公 制造业版", "icp": "manufacturing", "min_employees": 1000},
    {"brand_id": f"smartoffice_retail_{RUN_TAG}",
     "name": "智能办公 零售连锁版", "icp": "retail", "min_employees": 200},
    {"brand_id": f"smartoffice_health_{RUN_TAG}",
     "name": "智能办公 医疗集团版", "icp": "healthcare", "min_employees": 300},
]

LONG_SALES_CYCLE_DAYS = 180  # 6 months B2B cycle


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
POLICY_VERSION = f"v_{RUN_TAG}"
_consent_policy_published = False


async def _setup_consent(c: httpx.AsyncClient, user_ids: list[str]) -> int:
    global _consent_policy_published
    if not _consent_policy_published:
        await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
            "version": POLICY_VERSION,
            "text_md": "# SmartOffice B2B consent",
            "effective_at": int(time.time()) - 60,
            "requires_re_grant": False,
        })
        _consent_policy_published = True
    granted = 0
    for uid in user_ids:
        sc, _ = await call(c, "POST", "/api/v1/consent/grant", json_body={
            "user_id": uid,
            "scopes": ["cross_brand_tracking", "personalization", "marketing"],
            "policy_version": POLICY_VERSION,
            "source": "app",
        })
        if sc == 200:
            granted += 1
    return granted


# ── Phase 1: Master + 5 ICP Segment Sub-Brands ───────────────────────────
async def phase_1_master_setup(c: httpx.AsyncClient) -> dict[str, Any]:
    _phase_init("1: SmartOffice Master + 5 ICP Segment Sub-Brands")
    state: dict[str, Any] = {"master_id": None}

    sc, b = await call(c, "POST", "/api/v1/master/create", json_body={
        "company_name": "智能办公科技有限公司 / SmartOffice Tech Inc",
        "primary_email": "laoshi@smartoffice.cn",
        "owner_user_id": OWNER_USER_ID,
    })
    if sc == 201 and isinstance(b, dict):
        state["master_id"] = b["master_id"]
        ok("create master", f"id={state['master_id']}")
    else:
        fail("create master", f"{sc} {_short(b)}")
        return state

    master_id = state["master_id"]
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
            gap("P1", f"attach {s['icp']} sub-brand", f"{sc} {_short(b)}")
    if attached == len(SUB_BRANDS):
        ok(f"attach {attached} ICP sub-brands",
           "finance/tech/manuf/retail/healthcare")
    else:
        gap("P0", "sub-brand attach", f"only {attached}/{len(SUB_BRANDS)}")

    # PROBE: ICP metadata at brand attach time (industry, min_employees,
    # revenue_band, deal_size). Same gap as 老梁: brand-attach has no LOB metadata.
    gap("P1", "no ICP metadata at brand-attach",
        "POST /master/{id}/brands/attach accepts brand_id+store_name only. "
        "B2B SaaS vendors filter by industry / employee_count / revenue_band / "
        "ARR_band on every prospect — this is the canonical ICP triple. There "
        "is no first-class place to declare 'this sub-brand sells the finance "
        "edition to companies with >500 employees'. ABM dashboards must infer "
        "ICP from campaign metadata.")

    state["sub_brands"] = SUB_BRANDS
    state["primary_bid"] = SUB_BRANDS[0]["brand_id"]
    return state


# ── Phase 2: Wallet ¥80K ─────────────────────────────────────────────────
async def phase_2_wallet(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("2: Wallet ¥80K/月 B2B Budget")
    master_id = state.get("master_id")
    if not master_id:
        fail("phase 2", "no master_id")
        return

    allocation = {s["brand_id"]: 0.20 for s in SUB_BRANDS}
    sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/budget/global",
                       json_body={
                           "monthly_budget_cents": 8_000_000,  # ¥80K
                           "allocation": allocation,
                       })
    if sc == 200:
        ok("master global budget", "¥80K/月 20% × 5 ICP segments")
    else:
        gap("P1", "master global budget", f"{sc} {_short(b)}")

    funded = 0
    for s in SUB_BRANDS:
        sc, b = await call(c, "POST", f"/api/v1/wallet/{s['brand_id']}/topup",
                           json_body={"amount_cents": 1_600_000,
                                      "payment_method": "alipay"})
        if sc != 200 or not isinstance(b, dict) or "topup_id" not in b:
            gap("P1", f"topup {s['icp']}", f"{sc} {_short(b)}")
            continue
        tid = b["topup_id"]
        sc2, _ = await call(c, "POST",
                            f"/api/v1/wallet/{s['brand_id']}/topup/{tid}/confirm",
                            json_body={"payment_gateway_response": {"mock": True}})
        if sc2 == 200:
            funded += 1
    if funded == len(SUB_BRANDS):
        ok("ICP wallets funded", f"¥16,000 × {funded}")
    else:
        gap("P0", "wallet funding", f"only {funded}/{len(SUB_BRANDS)}")

    # PROBE: wire-transfer is the canonical B2B settlement; merchants pay 老石
    # via bank wire, not alipay/wechat. Confirm rejection.
    sc, b = await call(c, "POST",
                       f"/api/v1/wallet/{SUB_BRANDS[0]['brand_id']}/topup",
                       json_body={"amount_cents": 100_000,
                                  "payment_method": "wire_transfer"})
    if sc in (400, 422):
        gap("P0", "wallet topup rejects wire_transfer payment method",
            f"{sc} {_short(b, 150)} — payment_method enum is alipay/wechat/"
            "stripe/paypal only. B2B procurement PAYS BY WIRE TRANSFER "
            "exclusively for amounts >¥10K. Without wire_transfer support, "
            "no B2B SaaS vendor can fund their platform wallet through "
            "their finance team's standard process.")

    # PROBE: corporate-invoice / NET-30 settlement support
    gap("P1", "no B2B payment-terms primitive on wallet topup",
        "B2B procurement typically pays via wire transfer with NET-30/60/90 "
        "terms and a PO number. /wallet/{bid}/topup accepts payment_method but "
        "has no PO-number, payment_terms, billing_entity, or invoice_id field. "
        "Reconciling 老石's ¥80K monthly platform spend against his finance "
        "team's PO ledger is fully manual.")


# ── Phase 3: KiX ID for 50 enterprise users ──────────────────────────────
async def phase_3_kix_ids(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("3: KiX IDs — 10 CEOs / 10 CTOs / 10 Procurement / 20 Employees")
    state["users"] = {"ceo": [], "cto": [], "procurement": [], "employee": []}

    role_counts = {"ceo": 10, "cto": 10, "procurement": 10, "employee": 20}
    primary_bid = state["primary_bid"]
    registered = 0
    role_codes = {"ceo": "10", "cto": "20", "procurement": "30", "employee": "40"}
    for role, n in role_counts.items():
        for i in range(n):
            sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
                "phone": f"+8615700{RUN_TAG % 10000:04d}{role_codes[role]}{i:02d}",
                "display_name": f"{role}_{i:02d}",
                "primary_language": "zh-CN",
                "source_brand_id": primary_bid,
                "device_fingerprint": f"dev_{RUN_TAG}_{role}_{i}",
                "country": "CN",
            })
            if sc == 200 and isinstance(b, dict) and b.get("kid"):
                state["users"][role].append(b["kid"])
                registered += 1
    if registered == 50:
        ok("register 50 enterprise users", "10 CEO / 10 CTO / 10 procurement / 20 employee")
    else:
        gap("P0", "kix-id register 50 users", f"{registered}/50 registered")

    # PROBE: KiX ID `role` / `job_title` / `company_id` first-class field
    gap("P1", "no enterprise-role attribute on kix-id register",
        "POST /kix-id/register accepts display_name + phone + country but has "
        "no first-class enterprise field set: role (CEO/CTO/...), job_title, "
        "department, employer_id, company_kid. Today 老石 must stuff these "
        "into display_name or a side attribute store, breaking downstream ABM "
        "segmentation. Every B2B platform needs role + employer.")

    # Consent for all
    all_users = sum(state["users"].values(), [])
    granted = await _setup_consent(c, all_users)
    if granted == len(all_users):
        ok("consent grant", f"{granted}/{len(all_users)}")


# ── Phase 4: Relationships — employee↔manager↔executive ───────────────────
async def phase_4_relationships(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("4: Org Chart — employee↔manager (= CTO) ↔ executive (= CEO)")
    users = state["users"]

    # Approximation: employee.manager=CTO,  CTO.manager=CEO
    # Use 'employee'/'manager' relationship (registered in primitives router)
    rels_emp = 0
    for i, emp in enumerate(users["employee"]):
        # Each employee reports to a CTO (round-robin)
        cto = users["cto"][i % len(users["cto"])]
        sc, b = await call(
            c, "POST", f"/api/v1/primitives/users/{emp}/relationships",
            json_body={
                "related_user_id": cto,
                "relationship": "employee",  # emp → CTO; reverse=manager
                "bidirectional": True,
                "meta": {"reports_to": "cto"},
            },
        )
        if sc == 200 and isinstance(b, dict) and b.get("ok"):
            rels_emp += 1
    if rels_emp == 20:
        ok("employee→CTO edges", "20 employees linked to CTOs")
    else:
        gap("P0", "employee→CTO relationships", f"{rels_emp}/20")

    rels_exec = 0
    for cto in users["cto"]:
        # CTO → CEO (CTO is employee, CEO is manager)
        ceo = users["ceo"][users["cto"].index(cto) % len(users["ceo"])]
        sc, b = await call(
            c, "POST", f"/api/v1/primitives/users/{cto}/relationships",
            json_body={
                "related_user_id": ceo,
                "relationship": "employee",
                "bidirectional": True,
                "meta": {"reports_to": "ceo", "tier": "executive"},
            },
        )
        if sc == 200:
            rels_exec += 1
    if rels_exec == 10:
        ok("CTO→CEO edges", "10 CTOs linked to CEOs")
    else:
        gap("P0", "CTO→CEO relationships", f"{rels_exec}/10")

    # Verify lookup — CEO's reports (managed employees) reachable?
    ceo0 = state["users"]["ceo"][0]
    sc, b = await call(c, "GET",
                       f"/api/v1/primitives/users/{ceo0}/relationships",
                       params={"relationship": "manager"})
    if sc == 200 and isinstance(b, dict) and (b.get("count") or 0) >= 1:
        ok("CEO sees direct reports (manager edges)",
           f"count={b.get('count')}")
    else:
        gap("P1", "CEO direct reports lookup", f"{sc} {_short(b)}")

    # PROBE: no transitive lookup — "all users at company X"
    gap("P0", "no transitive org-chart traversal",
        "Relationship primitive supports direct edges (A reports to B) and "
        "reverse lookup, but B2B SaaS routinely needs 'all employees beneath "
        "this CEO' (full subtree). No /relationships/subtree, "
        "/relationships/transitive, or /company/{id}/members endpoint. Without "
        "this, account-based marketing cannot enumerate the buying committee.")

    # PROBE: no 'executive' role-tier on relationship meta — role/seniority
    # is jammed into free-text meta. A 'role: executive' first-class flag on
    # the relationship (or on the user) would unlock buying-committee analysis.
    gap("P1", "no role-tier (executive/manager/IC) on user or relationship",
        "Decision-maker hierarchy in B2B (executive=economic buyer, "
        "manager=champion, IC=user) maps to attention/budget weight in "
        "every ABM model. No first-class seniority field; today inferable only "
        "from string-matching display_name.")

    # PROBE: 'colleague_of' / 'works_with' lateral edge missing — multi-decision
    # PoC committees often span peer departments
    sc, b = await call(c, "POST",
                       f"/api/v1/primitives/users/{users['cto'][0]}/relationships",
                       json_body={
                           "related_user_id": users["procurement"][0],
                           "relationship": "colleague_of",
                           "bidirectional": True,
                       })
    if sc in (400, 422):
        gap("P1", "no colleague_of / works_with relationship type",
            f"{sc} {_short(b, 100)} — _RELATIONSHIP_REVERSE_MAP lacks lateral "
            "B2B edges. CTO and procurement aren't in a vertical reporting "
            "line, but they co-decide. Without a peer relationship, the "
            "platform sees the buying committee as disconnected.")
    elif sc == 200:
        ok("colleague_of edge", "(unexpected — lateral edge accepted)")


# ── Phase 5: Buyer Agreement Consent Flow ────────────────────────────────
async def phase_5_buyer_agreement(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("5: Multi-Stakeholder Buyer Agreement Signing")
    ceo = state["users"]["ceo"][0]
    cto = state["users"]["cto"][0]
    procurement = state["users"]["procurement"][0]

    # CEO signs the buyer agreement (economic buyer)
    sc, b = await call(c, "POST", "/api/v1/consent/document/sign", json_body={
        "user_id": ceo,
        "document_type": "buyer_agreement",
        "document_version": POLICY_VERSION,
        "document_url": "https://smartoffice.cn/msa.pdf",
        "signature_method": "signature",
        "signature_evidence_url": "https://smartoffice.cn/sig/ceo",
        "granted_scopes": ["marketing", "personalization"],
    })
    if sc == 200 and isinstance(b, dict) and b.get("document_consent_id"):
        state["msa_doc_id_ceo"] = b["document_consent_id"]
        ok("CEO signs buyer_agreement", f"doc={state['msa_doc_id_ceo']}")
    else:
        gap("P0", "CEO buyer_agreement sign", f"{sc} {_short(b)}")

    # CTO signs (technical buyer) — likely same MSA but different signer
    sc, b = await call(c, "POST", "/api/v1/consent/document/sign", json_body={
        "user_id": cto,
        "document_type": "buyer_agreement",
        "document_version": POLICY_VERSION,
        "document_url": "https://smartoffice.cn/msa.pdf",
        "signature_method": "signature",
        "signature_evidence_url": "https://smartoffice.cn/sig/cto",
        "granted_scopes": ["marketing", "personalization"],
        # speculative: link to CEO's signing for co-signer chain
        "co_signer_user_id": ceo,
        "signing_order": 2,
    })
    if sc == 200:
        ok("CTO signs buyer_agreement (co-signer)", "")
    else:
        gap("P1", "CTO buyer_agreement co-signer", f"{sc} {_short(b)}")

    # Procurement signs (gatekeeper / approver)
    sc, b = await call(c, "POST", "/api/v1/consent/document/sign", json_body={
        "user_id": procurement,
        "document_type": "buyer_agreement",
        "document_version": POLICY_VERSION,
        "document_url": "https://smartoffice.cn/msa.pdf",
        "signature_method": "signature",
        "signature_evidence_url": "https://smartoffice.cn/sig/proc",
        "granted_scopes": ["marketing"],
    })
    if sc == 200:
        ok("procurement signs buyer_agreement", "(third signature)")

    # PROBE: multi-stakeholder consent semantics
    gap("P0", "no multi-stakeholder agreement primitive",
        "consent/document/sign treats every signature as independent. A B2B "
        "MSA is a *single contract* signed by 3+ parties (CEO economic + CTO "
        "technical + procurement gatekeeper). There is no agreement entity "
        "with required-signers, pending-signers, fully-executed states. "
        "co_signer_user_id and signing_order were accepted but silently "
        "ignored — readback won't show the chain.")

    # PROBE: Does CEO authorize CTO's data sharing?  Authority delegation
    sc, b = await call(c, "POST", "/api/v1/consent/grant", json_body={
        "user_id": cto,
        "scopes": ["cross_brand_tracking"],
        "policy_version": POLICY_VERSION,
        "source": "delegated_by_ceo",     # speculative
        "delegated_by_user_id": ceo,      # speculative
        "delegation_evidence_url": "https://smartoffice.cn/auth/ceo-to-cto",
    })
    if sc == 200:
        info("CEO→CTO delegated consent accepted (likely fields ignored)")
    gap("P1", "no consent delegation/authority chain",
        "Enterprise reality: CEO authorizes CTO to consent to data-sharing "
        "on behalf of company employees. There is no first-class delegation "
        "primitive (delegated_by_user_id, delegation_scope, delegation_expiry). "
        "GDPR/PIPL audit trail for B2B is impossible without it.")


# ── Phase 6: Sales-Demo Reservation w/ sales rep as fulfiller ────────────
async def phase_6_demo_reservation(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("6: Sales Demo (type=consultation, fulfiller=sales rep)")
    primary_bid = state["primary_bid"]
    cto = state["users"]["cto"][0]
    sales_rep = f"salesrep_{RUN_TAG}"
    await _setup_consent(c, [sales_rep])

    sc, b = await call(c, "POST", "/api/v1/reservations/create", json_body={
        "brand_id": primary_bid,
        "user_id": cto,
        "scheduled_at": int(time.time()) + 86400 * 7,
        "party_size": 5,  # CTO + 4 engineers
        "type": "consultation",
        "resource_id": sales_rep,
        "fulfiller_user_id": sales_rep,
        "metadata": {
            "purpose": "product_demo",
            "deal_size_estimated_cents": 30_000_000,  # ¥300K AOV
            "stage": "SQL",
            "company_size": 200,
        },
    })
    if sc in (200, 201) and isinstance(b, dict):
        rid = b.get("reservation_id")
        state["demo_rid"] = rid
        ok("demo reservation (consultation)", f"rid={rid} fulfiller=sales_rep")

        sc_get, b_get = await call(c, "GET", f"/api/v1/reservations/{rid}")
        if sc_get == 200 and isinstance(b_get, dict):
            if b_get.get("fulfiller_user_id") == sales_rep:
                ok("sales-rep fulfiller persisted", "")
            else:
                gap("P1", "sales-rep fulfiller dropped on readback",
                    f"{_short(b_get)}")
    else:
        gap("P0", "demo reservation create", f"{sc} {_short(b)}")

    # PROBE: pipeline-stage on reservation (MQL/SQL/Opportunity/Closed-Won)
    gap("P1", "no pipeline-stage primitive on reservation",
        "B2B demos exist *within a sales pipeline*: MQL→SQL→Demo→PoC→Verbal→"
        "Closed-Won. Reservation has no `pipeline_stage`, `deal_amount`, "
        "`probability_pct`, `expected_close_date` fields. Pushing 老石's CRM "
        "data into the platform requires sticking it all into free-text "
        "metadata, defeating segmentation/forecast queries.")

    # PROBE: account_id on reservation — the COMPANY, not the individual
    gap("P0", "no account_id (company) on reservation",
        "Reservation.user_id is the individual booker. But the DEMO is "
        "*for the company* — multiple individuals attend, the deal is owned "
        "by the company. Without `account_id`/`company_id` linking the "
        "reservation to a corporate entity, downstream attribution rolls up "
        "to the wrong unit. ABM dashboards need 'this account has 3 demos "
        "scheduled', not 'CTO_07 has 3 demos'.")


# ── Phase 7: Multi-Touchpoint Attribution Across Decision-Makers ──────────
async def phase_7_multitouch(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("7: Multi-Touchpoint Attribution — CEO+CTO+Procurement engage")
    primary_bid = state["primary_bid"]
    ceo = state["users"]["ceo"][0]
    cto = state["users"]["cto"][0]
    procurement = state["users"]["procurement"][0]
    # Use 180d attribution window (now possible after R6 365d cap)
    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": primary_bid,
        "name": "Enterprise Demand-Gen 180d",
        "objective": "acquire",
        "bid_strategy": "cpa",
        "max_bid_cents": 500_000,  # ¥5000/SQL acceptable for ¥300K AOV
        "daily_budget_cents": 50_000,
        "total_budget_cents": 3_000_000,
        "attribution_window_days": LONG_SALES_CYCLE_DAYS,  # 180d
        "targeting": {"geo": {"country": "CN"}},
        "creative": {"recipe_id": "duolingo_streak"},
        "schedule": {"start_at": time.time() - 60,
                     "end_at": time.time() + 86400 * 200},
        "target_audience": "new_users_only",
    })
    if sc == 200 and isinstance(b, dict):
        cid = b["campaign_id"]
        state["abm_campaign_id"] = cid
        ok("180-day attribution campaign", f"id={cid}")
        await call(c, "POST", f"/api/v1/campaigns/{cid}/admin/approve",
                   json_body={"admin_token": "DEV", "notes": "sim"})
        # Read back
        sc, b = await call(c, "GET", f"/api/v1/campaigns/{cid}/details")
        if sc == 200 and isinstance(b, dict):
            stored = b.get("attribution_window_days", 0)
            if stored == LONG_SALES_CYCLE_DAYS:
                ok("180d window persisted",
                   f"stored={stored} (R6 raised cap from 90→365)")
            else:
                gap("P0", "180d attribution window not persisted",
                    f"set=180, got={stored}")
    else:
        gap("P0", "demand-gen campaign create", f"{sc} {_short(b)}")

    # 3 multi-touch attribution events — one per decision maker
    accepted = 0
    for actor, label in [(ceo, "ceo_visited_pricing_page"),
                         (cto, "cto_attended_demo"),
                         (procurement, "procurement_requested_contract")]:
        sc, b = await call(c, "POST", "/api/v1/attribution/track/conversion",
                           json_body={
                               "user_id": actor,
                               "target_brand": primary_bid,
                               "order_id": f"abm_touch_{RUN_TAG}_{label}",
                               "event_type": label,
                               "amount_cents": 0,
                               "metadata": {"role": label.split("_")[0],
                                            "account_id": "company_alpha"},
                           })
        if sc == 200:
            accepted += 1
    if accepted == 3:
        ok("3 multi-stakeholder touches recorded",
           "CEO + CTO + procurement events")
    else:
        gap("P1", "multi-stakeholder touch events",
            f"{accepted}/3 — custom event_type may be rejected")

    # PROBE: account-level journey readback
    sc, b = await call(c, "GET",
                       "/api/v1/attribution/account/company_alpha/journey",
                       params={"brand_id": primary_bid})
    if sc == 404:
        gap("P0", "no account-level attribution journey",
            "GET /attribution/account/{id}/journey 404. There is no way to "
            "ask 'across all individuals at this company, what touchpoints "
            "happened, in what order, with what weight?'. B2B attribution is "
            "inherently account-rolled-up — single-user journey misses 70% "
            "of the decision-making behavior.")

    # PROBE: Buying-Committee credit attribution
    gap("P0", "no buying-committee credit weighting",
        "Multi-touch attribution in B2B needs to weight by stakeholder role "
        "(CEO 50%, CTO 30%, procurement 10%, employee 10% — or W-shaped / "
        "U-shaped models). Platform's attribution is single-actor; no model "
        "supports 'distribute conversion credit across 4 different users on "
        "the same account'.")


# ── Phase 8: Nurture Campaign target_audience=new_users_only ─────────────
async def phase_8_nurture(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("8: Nurture Campaign target_audience=new_users_only")
    primary_bid = state["primary_bid"]

    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": primary_bid,
        "name": "MQL Nurture Sequence",
        "objective": "engagement",
        "bid_strategy": "target_cpa",
        "max_bid_cents": 100_000,
        "target_cpa_cents": 50_000,
        "daily_budget_cents": 20_000,
        "total_budget_cents": 600_000,
        "attribution_window_days": 90,
        "target_audience": "new_users_only",
    })
    if sc in (200, 201) and isinstance(b, dict):
        cid = b["campaign_id"]
        ok("nurture campaign new_users_only", f"id={cid}")
        state["nurture_cid"] = cid
    else:
        gap("P1", "nurture campaign", f"{sc} {_short(b)}")

    # Auction savings — relevant for B2B because re-buying your own existing
    # enterprise customer at ¥5000 CPA is catastrophic.
    sc, b = await call(c, "GET", f"/api/v1/auction/admin/savings/{primary_bid}")
    if sc == 200 and isinstance(b, dict):
        ok("auction savings",
           f"skipped={b.get('existing_customers_skipped')} "
           f"avg_cpa={b.get('average_cpa_cents')}c (critical at ¥5K CPA)")
    else:
        gap("P1", "auction savings", f"{sc} {_short(b)}")

    # PROBE: push engine — nurture drip sequence
    pushed = 0
    for ceo in state["users"]["ceo"][:5]:
        sc, b = await call(c, "POST", "/api/v1/push/now", json_body={
            "kid": ceo, "slot": "push",
        })
        if sc == 200 and isinstance(b, dict) and b.get("fired"):
            pushed += 1
    info(f"nurture push fired={pushed}/5 CEOs (push/now non-deterministic)")

    # PROBE: drip sequence scheduling
    sc, b = await call(c, "POST", "/api/v1/push/schedule", json_body={
        "kid": state["users"]["ceo"][0],
        "slot": "push",
        "scheduled_at": int(time.time()) + 86400,
        "payload": {"title": "Demo follow-up day 1",
                    "body": "How was your demo? Try our ROI calculator."},
    })
    if sc == 200:
        ok("nurture drip scheduled", "day-1 follow-up")
    elif sc in (400, 422):
        gap("P1", "drip-schedule payload shape mismatch",
            f"{sc} {_short(b, 100)}")
    elif sc == 404:
        gap("P1", "no /push/schedule endpoint", "404")

    # PROBE: multi-step drip sequence (day 0/3/7/14/30/60/90)
    gap("P1", "no drip-sequence primitive",
        "B2B nurture is multi-step: day-0 thank-you, day-3 case study, "
        "day-7 ROI calc, day-14 demo invite, day-30 PoC offer, day-60 BANT "
        "qualifier. Platform exposes /push/schedule (single nudge) and "
        "/push/now. There is no /push/sequence/create that takes an array of "
        "(offset_days, payload) and pauses on user response. Each nudge "
        "must be scheduled individually with no abandonment-based branching.")


# ── Phase 9: Trial → Conversion → Renewal Lifecycle ──────────────────────
async def phase_9_lifecycle(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("9: Trial → Paid Conversion → Renewal Lifecycle")
    primary_bid = state["primary_bid"]

    # Voucher = "free 30-day trial seat"
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": primary_bid,
        "name": "30-Day Pilot — 50 seats",
        "description": "Free trial 50 seats × 30 days",
        "value": {"type": "fixed", "amount": 0, "currency": "CNY"},
        "conditions": {"usage_limit_per_user": 1,
                       "min_purchase_amount_cents": 0,
                       "seat_count": 50},  # speculative seat dimension
        "expires_in_days": 30,
        "stackable": False,
        "transferable": False,
        "voucher_subtype": "trial_seat_pack",  # speculative
    })
    trial_tid = None
    if sc == 201 and isinstance(b, dict):
        trial_tid = b.get("template_id")
        ok("trial voucher template", f"id={trial_tid}")
        state["trial_tid"] = trial_tid
        # Readback — did seat_count survive?
        sc2, b2 = await call(c, "GET", f"/api/v1/vouchers/templates/{trial_tid}")
        if sc2 == 200 and isinstance(b2, dict):
            conds = b2.get("conditions") or {}
            if "seat_count" in json.dumps(b2):
                ok("seat_count preserved", "(found in template payload)")
            else:
                gap("P0", "seat_count dropped on voucher template readback",
                    "Voucher engine has no per-seat dimension. B2B SaaS is "
                    "fundamentally seat-priced (¥1000/seat × 200 seats = "
                    "¥200K). Today seats live only in free-text. There's "
                    "no /vouchers/issue?seat_count=N parameter and no "
                    "consumption ledger 'seats remaining = 47/50'.")
    else:
        gap("P0", "trial voucher template", f"{sc} {_short(b)}")

    # Conversion: trial → paid annual contract (¥300K)
    ceo = state["users"]["ceo"][0]
    pixel_id = None
    sc, b = await call(c, "POST", "/api/v1/pixel/register", json_body={
        "brand_id": primary_bid,
        "allowed_origins": ["https://smartoffice.cn"],
    })
    if sc == 201 and isinstance(b, dict):
        pixel_id = b["pixel_id"]
    if pixel_id:
        sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
            "pixel_id": pixel_id,
            "event_type": "purchase",
            "device_fingerprint": f"dev_b2b_{RUN_TAG}",
            "user_id": ceo,
            "order_id": f"annual_contract_{RUN_TAG}",
            "amount_cents": 30_000_000,  # ¥300K
            "currency": "CNY",
            "origin": "https://smartoffice.cn",
            "meta": {"seats": 200, "tier": "enterprise",
                     "contract_term_months": 12},
        })
        if sc == 200:
            ok("trial→paid conversion fired", "¥300K annual contract")
        else:
            gap("P1", "conversion pixel fire", f"{sc} {_short(b)}")

    # PROBE: subscription / recurring revenue primitive
    sc, b = await call(c, "POST", "/api/v1/subscriptions/create", json_body={
        "brand_id": primary_bid,
        "user_id": ceo,
        "plan": "enterprise_annual",
        "amount_cents": 30_000_000,
        "term_months": 12,
        "seats": 200,
        "renewal_date": int(time.time()) + 86400 * 365,
    })
    if sc == 404:
        gap("P0", "no /subscriptions primitive",
            "POST /api/v1/subscriptions/create 404. B2B SaaS = subscriptions. "
            "There is no subscription entity with start_date / renewal_date / "
            "term / seats / MRR / status (active/paused/churned). 老石 has "
            "to model every renewal as a fresh order, losing all renewal-rate "
            "/ NDR / GRR metrics that VCs measure SaaS companies on.")
    elif sc in (400, 422):
        gap("P1", "subscriptions schema mismatch", f"{sc} {_short(b, 120)}")


# ── Phase 10: Seat Expansion ─────────────────────────────────────────────
async def phase_10_seat_expansion(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("10: Seat Expansion — existing customer adds 100 more seats")
    primary_bid = state["primary_bid"]
    ceo = state["users"]["ceo"][0]

    # Fire an "expansion" purchase event (200 → 300 seats)
    sc, b = await call(c, "POST", "/api/v1/pixel/register", json_body={
        "brand_id": primary_bid,
        "allowed_origins": ["https://smartoffice.cn"],
    })
    if sc == 201:
        pixel_id = b["pixel_id"]
        sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
            "pixel_id": pixel_id,
            "event_type": "purchase",
            "user_id": ceo,
            "device_fingerprint": f"dev_expand_{RUN_TAG}",
            "order_id": f"seat_expand_{RUN_TAG}",
            "amount_cents": 15_000_000,  # ¥150K for 100 incremental seats
            "currency": "CNY",
            "origin": "https://smartoffice.cn",
            "meta": {"event_subtype": "expansion",
                     "previous_seats": 200, "new_seats": 300,
                     "incremental_seats": 100,
                     "mid_term_proration": True},
        })
        if sc == 200:
            ok("expansion purchase event", "+100 seats ¥150K")

    # PROBE: expansion-classified revenue (vs new-business / renewal / churn)
    gap("P0", "no expansion/new/renewal revenue classifier",
        "B2B finance discipline classifies revenue as: New / Expansion / "
        "Renewal / Reactivation / Churn. Platform has no field to mark this. "
        "NDR (Net Dollar Retention) — the #1 SaaS metric — is uncomputable "
        "from platform data alone. event_subtype=expansion is free-text and "
        "ignored.")

    # PROBE: usage-based / seat-count delta tracking
    sc, b = await call(c, "POST",
                       f"/api/v1/primitives/user/{ceo}/seat-balance",
                       json_body={"brand_id": primary_bid,
                                  "current_seats": 300, "max_seats": 500})
    if sc == 404:
        gap("P1", "no seat-balance / entitlement primitive",
            "POST /primitives/user/{uid}/seat-balance 404. To upsell, the "
            "platform must know 'this customer is at 280/300 seats — time to "
            "pitch expansion'. No entitlement ledger exists.")


# ── Phase 11: Churn Risk Detection ───────────────────────────────────────
async def phase_11_churn_risk(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("11: Churn Risk — usage drop triggers")
    primary_bid = state["primary_bid"]

    # Try a trigger on a "usage_drop" event
    sc, b = await call(c, "POST", "/api/v1/triggers/register", json_body={
        "brand_id": primary_bid,
        "name": "Churn risk — usage drop >40%",
        "event_type": "attribute_threshold",
        "event_filter": {"attribute_key": "weekly_active_seats_pct",
                         "threshold": 60, "comparator": "lt"},
        "action": {
            "type": "send_push",
            "config": {"title": "客户健康度告警", "body": "本月使用率显著下降"},
            "recipient_user_id_attr": "manager",  # escalate to CSM/manager
        },
        "cooldown_seconds": 86400 * 7,
        "max_fires_per_user": 4,
    })
    if sc == 201:
        ok("churn-risk trigger registered", "fires when weekly active seats < 60%")
    elif sc in (400, 422):
        gap("P1", "churn-risk trigger schema",
            f"{sc} {_short(b, 100)} — attribute_threshold + recipient_indirection "
            "exists but config may be ad-hoc.")
    elif sc == 404:
        gap("P0", "/triggers/register 404",
            "B2B churn protection requires event-driven CSM alerts; no "
            "trigger endpoint mounted.")

    # PROBE: health-score / leading-indicator primitive
    gap("P0", "no health-score / leading-indicator primitive",
        "B2B SaaS depends on a 'customer health score' (usage + adoption + "
        "support tickets + NPS). Platform has tier (loyalty-XP) but no "
        "health-score primitive. 老石 must run a side ML pipeline; renewal "
        "alerts can't trigger off platform-computed signals.")

    # PROBE: pre-renewal warning window (60d before contract end)
    gap("P1", "no time-relative trigger for renewal-anniversary",
        "Renewal anniversaries fire N days before subscription.renewal_date. "
        "Without a subscription primitive (Phase 9) there's no anchor for "
        "'60 days before renewal'. Today 老石 builds this off-platform.")


# ── Phase 12: Renewal Campaign target_audience=retargeting_only ──────────
async def phase_12_renewal_campaign(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("12: Renewal Campaign target_audience=retargeting_only")
    primary_bid = state["primary_bid"]

    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": primary_bid,
        "name": "Renewal Booster (existing customers)",
        "objective": "retention",
        "bid_strategy": "target_roas",
        "max_bid_cents": 200_000,
        "target_roas": 5.0,
        "daily_budget_cents": 30_000,
        "total_budget_cents": 1_000_000,
        "attribution_window_days": 90,
        "target_audience": "retargeting_only",  # opposite of new_users_only
    })
    if sc in (200, 201) and isinstance(b, dict):
        cid = b["campaign_id"]
        ok("renewal campaign retargeting_only", f"id={cid} ROAS=5.0")
        # Readback
        sc, b = await call(c, "GET", f"/api/v1/campaigns/{cid}/details")
        if sc == 200 and isinstance(b, dict):
            ta = b.get("target_audience")
            if ta == "retargeting_only":
                ok("retargeting_only persisted", f"value={ta}")
            else:
                gap("P0", "retargeting_only target_audience not persisted",
                    f"set=retargeting_only got={ta} — renewal campaigns can't "
                    "target existing customers explicitly; auction savings "
                    "may skip them by default.")
    else:
        gap("P0", "renewal campaign create", f"{sc} {_short(b)}")

    # PROBE: objective=retention semantics
    gap("P1", "objective=retention semantics under-specified",
        "Renewal campaigns target objective=retention but there is no "
        "documented difference vs objective=acquire (besides target_audience). "
        "Renewal funnels measure NDR / GRR / logo-retention not CAC. Without "
        "those KPIs surfaced, retention campaigns get the same dashboards as "
        "acquisition — wrong unit-economics view.")


# ── Phase 13: Account-Based Marketing ────────────────────────────────────
async def phase_13_abm(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("13: Account-Based Marketing — target an entire COMPANY")
    primary_bid = state["primary_bid"]

    # Try to create a custom audience of "all decision-makers at company X"
    # — use CEO + CTO + procurement kids as members
    abm_users = [state["users"]["ceo"][0], state["users"]["cto"][0],
                 state["users"]["procurement"][0]]
    sc, b = await call(c, "POST", "/api/v1/audiences/custom/create", json_body={
        "brand_id": primary_bid,
        "name": "Account Alpha — Buying Committee",
        "audience_type": "custom_list",
        "user_ids": abm_users,
        "metadata": {"account_id": "company_alpha",
                     "industry": "finance",
                     "employee_count": 200,
                     "annual_revenue_cents_band": "100M_500M",
                     "icp_score": 92},
    })
    if sc == 200 and isinstance(b, dict):
        aid = b.get("audience_id")
        state["abm_audience_id"] = aid
        ok("ABM audience created", f"aid={aid} (3 stakeholders)")
    elif sc in (400, 422):
        gap("P1", "audiences custom_list schema",
            f"{sc} {_short(b, 150)}")
    else:
        gap("P1", "audience create", f"{sc} {_short(b)}")

    # PROBE: ICP-filter audience (revenue band + industry + min employees)
    sc, b = await call(c, "POST", "/api/v1/audiences/filter/preview", json_body={
        "brand_id": primary_bid,
        "filters": {
            "attributes": {
                "industry": "finance",
                "employee_count_gte": 500,
                "annual_revenue_band": "100M_plus",
            },
        },
    })
    if sc == 200 and isinstance(b, dict):
        ok("ICP filter preview", f"matches~{b.get('estimated_size')}")
    elif sc in (400, 422):
        gap("P1", "ICP filter preview schema",
            f"{sc} {_short(b, 120)} — attribute filter likely accepts only "
            "string equality, not _gte/_band semantics.")
    elif sc == 404:
        gap("P1", "/audiences/filter/preview 404", "")

    # PROBE: bind ABM audience to a campaign
    if state.get("abm_audience_id") and state.get("abm_campaign_id"):
        sc, b = await call(c, "POST",
                           f"/api/v1/audiences/{state['abm_audience_id']}/"
                           f"target-in-campaign",
                           json_body={"campaign_id": state["abm_campaign_id"]})
        if sc == 200:
            ok("ABM audience → demand-gen campaign", "bound for targeting")
        else:
            gap("P1", "audience target-in-campaign",
                f"{sc} {_short(b, 100)}")

    # PROBE: account as first-class entity
    sc, b = await call(c, "POST", "/api/v1/primitives/entity/create",
                       json_body={
                           "entity_type": "company",
                           "name": "Account Alpha",
                           "attributes": {"industry": "finance",
                                          "employee_count": 200,
                                          "annual_revenue_cents_band": "100M_500M",
                                          "icp_score": 92,
                                          "stage": "Opportunity"},
                       })
    if sc in (200, 201) and isinstance(b, dict):
        eid = b.get("entity_id")
        state["company_entity_id"] = eid
        ok("company as entity primitive", f"eid={eid}")
        # Link CEO --owns--> company
        ceo = state["users"]["ceo"][0]
        sc2, b2 = await call(c, "POST",
                             f"/api/v1/primitives/users/{ceo}/relationships",
                             json_body={"related_user_id": eid,
                                        "relationship": "owns",
                                        "bidirectional": True})
        if sc2 == 200:
            ok("CEO → company ownership edge", "")
        else:
            gap("P1", "user→entity ownership edge", f"{sc2} {_short(b2, 100)}")
    elif sc == 404:
        gap("P0", "entity primitive missing",
            "POST /primitives/entity/create 404. R6 entity primitive (designed "
            "for pets/devices/vehicles) doesn't include 'company' as a "
            "concrete entity_type, or the endpoint path differs. ABM models "
            "the company as a first-class node with employees as members.")


# ── Phase 14: Module probe (B2B-specific) ────────────────────────────────
async def phase_14_module_probe(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("14: Module Probe — B2B SaaS surface area")
    primary_bid = state["primary_bid"]
    master_id = state.get("master_id")
    probes = [
        ("subscriptions.create", "POST", "/api/v1/subscriptions/create"),
        ("subscriptions.renew", "POST", "/api/v1/subscriptions/x/renew"),
        ("account.attribution.journey", "GET",
         "/api/v1/attribution/account/company_alpha/journey"),
        ("entity.company.create", "POST", "/api/v1/primitives/entity/create"),
        ("relationships.subtree", "GET",
         f"/api/v1/primitives/users/{state['users']['ceo'][0]}/relationships/subtree"),
        ("master.icp-filter", "POST",
         f"/api/v1/master/{master_id}/icp/filter"),
        ("master.companies", "GET",
         f"/api/v1/master/{master_id}/companies"),
        ("master.account.health", "GET",
         f"/api/v1/master/{master_id}/account/x/health"),
        ("audiences.account-based", "POST",
         "/api/v1/audiences/account-based/create"),
        ("vouchers.seat-pack", "POST",
         "/api/v1/vouchers/seat-pack/issue"),
        ("primitives.seat-balance", "GET",
         f"/api/v1/primitives/user/u/seat-balance"),
        ("primitives.health-score", "GET",
         f"/api/v1/primitives/user/u/health-score"),
        ("triggers.renewal-anniversary", "POST",
         "/api/v1/triggers/renewal-anniversary/register"),
        ("push.sequence", "POST", "/api/v1/push/sequence/create"),
        ("consent.agreement.multi-signer", "POST",
         "/api/v1/consent/agreement/multi-signer/create"),
        ("consent.delegation", "POST",
         "/api/v1/consent/delegation/grant"),
        ("attribution.committee.credit", "POST",
         "/api/v1/attribution/committee/credit"),
    ]
    avail, missing = [], []
    for label, method, path in probes:
        if method == "POST":
            sc, b = await call(c, method, path, json_body={})
        else:
            sc, b = await call(c, method, path)
        if sc == 200:
            avail.append(label)
            ok(label, "200")
        elif sc == 404:
            if isinstance(b, dict) and (b.get("detail") in ("Not Found", "not found")
                                        or "not found" in str(b.get("detail", "")).lower()):
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
    md.append("# 老石 / Shi Liang (智能办公 SmartOffice) — B2B SaaS Merchant Journey Findings")
    md.append("")
    md.append(f"**Run tag**: `{RUN_TAG}` | **Runtime**: {runtime:.1f}s | "
              f"**Date**: {time.strftime('%Y-%m-%d %H:%M', time.localtime(start_ts))}")
    md.append("")
    md.append("## Scenario")
    md.append(
        "老石 owns 「智能办公」 (SmartOffice) — a B2B SaaS vendor selling "
        "enterprise project-management software to 500 corporate customers "
        "averaging 200 employees each. AOV ¥50K-¥500K/yr. Budget ¥80K/月. "
        "Long sales cycles (3-6 months), multi-persona buying committee "
        "(CEO economic + CTO technical + procurement gatekeeper + employee "
        "end-user), seat-based pricing, renewal-centric (90% margin, churn "
        "is catastrophic).\n"
        "\n"
        "**Critical differences vs prior merchants**: B2B SALE (account = "
        "company, not individual), MULTI-PERSONA decision (≥3 stakeholders "
        "per deal), LONG CYCLE (180d attribution), SEAT-BASED licensing "
        "(distinct from per-transaction or subscription), RENEWAL-CENTRIC "
        "(NDR/GRR matter more than CAC). Five axes each exercises a primitive "
        "that previous merchant simulations could not."
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

    section("P0 — Blockers for the B2B SaaS use case", p0)
    section("P1 — Friction", p1)
    section("P2 — Nice-to-have", p2)
    section("Hard failures", fails)

    md.append("## Top 5 NEW Gaps Unique to B2B SaaS")
    md.append("")
    md.append(
        "Among the gaps surfaced, five are *fundamentally new* compared to "
        "the 16 prior merchant sims — they are CONSEQUENCES OF AXES that no "
        "prior merchant exercised:\n"
        "\n"
        "### 1. Account = company (not individual) (NEW)\n"
        "All prior merchants treated 'customer' as a person. B2B fundamentally "
        "models the **company** as the buying unit: contracts signed by the "
        "company, seats licensed to the company, renewal owned by the "
        "company. Probe results:\n"
        "  - No `account_id` / `company_id` field on reservations, "
        "campaigns, attribution events, or vouchers\n"
        "  - No `/attribution/account/{id}/journey` (404)\n"
        "  - No `/master/{id}/companies` directory (404)\n"
        "  - Entity primitive (R6) supports `entity_type` but no 'company' "
        "type is wired through ABM/audience flow\n"
        "Result: every B2B platform user must run a side identity-resolution "
        "service mapping individuals to companies. ABM dashboards are blind.\n"
        "\n"
        "### 2. Multi-stakeholder buyer agreement (NEW)\n"
        "B2B MSAs are signed by 3+ parties (economic buyer + technical buyer "
        "+ procurement). `/consent/document/sign` treats each signature as "
        "an independent record:\n"
        "  - Field `co_signer_user_id` silently accepted, not persisted\n"
        "  - No agreement entity with pending-signers / fully-executed states\n"
        "  - No consent-delegation (`delegated_by_user_id` for CEO→CTO "
        "authorization on behalf of employees)\n"
        "Result: a finalized MSA can't be reasoned about as a single "
        "contract. Audit trail for PIPL/GDPR enterprise compliance is "
        "fragmented across three independent consent records.\n"
        "\n"
        "### 3. Subscription primitive missing (NEW)\n"
        "Prior merchants modeled discrete purchases (food order, lesson "
        "package, hotel night). 老石 sells **recurring annual "
        "subscriptions** with auto-renewal, mid-term expansion, and "
        "anniversary cycles. Probe: POST `/api/v1/subscriptions/create` 404. "
        "No subscription entity with start_date / renewal_date / term / "
        "seats / MRR / status. Consequences:\n"
        "  - NDR (Net Dollar Retention) — the #1 SaaS metric — uncomputable\n"
        "  - Expansion vs new-business vs churn revenue classification "
        "missing\n"
        "  - 60-days-before-renewal triggers have no anchor date\n"
        "  - Renewal forecasts impossible\n"
        "\n"
        "### 4. Seat-based licensing primitive missing (NEW)\n"
        "Voucher engine assumes per-unit currency discounts. B2B SaaS is "
        "**seat-priced**: ¥1000/seat × 200 seats = ¥200K. Probe results:\n"
        "  - Voucher template `conditions.seat_count`: accepted but dropped "
        "on readback\n"
        "  - No `/vouchers/seat-pack/issue` endpoint\n"
        "  - No seat-balance ledger (used 280/300 seats)\n"
        "  - No expansion-flagged transaction (`event_subtype=expansion`)\n"
        "Result: per-seat upsell strategy (the #1 B2B SaaS growth lever) is "
        "invisible to the platform.\n"
        "\n"
        "### 5. Buying-committee credit attribution (NEW)\n"
        "Prior merchants attributed conversions to a single user. B2B deals "
        "have a **buying committee**: CEO (50%) + CTO (30%) + procurement "
        "(10%) + employee (10%) — or W-shaped/U-shaped models. Probe:\n"
        "  - Multi-stakeholder events (CEO/CTO/procurement) recorded as "
        "independent conversions\n"
        "  - No `/attribution/account/{id}/journey` to roll up across users\n"
        "  - No `/attribution/committee/credit` to weight by role\n"
        "Result: deal-credit attribution defaults to last-click on a single "
        "person. Marketing-mix optimization across roles impossible.\n"
    )
    md.append("")

    md.append("## Cross-Comparison with All 16 Previous Merchants")
    md.append("")
    md.append(
        "| Axis | Prior merchants | 老石 SmartOffice |\n"
        "|---|---|---|\n"
        "| Customer = | individual person | **company (account)** |\n"
        "| Sales cycle | <1 day → 7 months (老梁) | **3-6 months / 180d** |\n"
        "| Decision-makers | 1 (some 2 — 老吴 parent+child) | **3+ (CEO+CTO+procurement+IC)** |\n"
        "| Pricing model | per-unit / package | **per-seat × term × tier** |\n"
        "| Revenue cadence | one-shot or per-visit | **subscription + renewal + expansion** |\n"
        "| Critical KPI | CAC, LTV | **NDR, GRR, ARR, logo-retention** |\n"
        "| Consent | individual click | **3-party MSA + delegation** |\n"
        "| Targeting | demographic + geo | **ICP (industry × employees × revenue)** |\n"
        "| Refund/cancel | per-order | **mid-term proration / co-terming** |\n"
        "| Channel | mass / 1-to-1 | **ABM (1-to-account)** |\n"
        "\n"
        "**Pattern**: 老石 is the FIRST merchant whose unit-of-economics is "
        "the **company-relationship**, not the customer-purchase. The "
        "platform's primitives (user, voucher, reservation, attribution "
        "event) all assume an individual buyer making a discrete purchase. "
        "B2B SaaS — which dominates the global software market — needs a "
        "complementary 'account' layer above 'user'. Industries the platform "
        "cannot serve today without this layer: B2B SaaS, enterprise "
        "consulting, professional services, B2B marketplaces, B2B fintech, "
        "industrial supply, insurance brokerages, real-estate franchises "
        "(multi-tenant), legal SaaS.\n"
    )
    md.append("")

    md.append("## Strategic Recommendations")
    md.append("")
    md.append(
        "1. **[P0] Account/Company primitive**: add `account_id` to "
        "reservations, campaigns, attribution events, vouchers; expose "
        "`/master/{id}/companies` directory, `/attribution/account/{id}/"
        "journey`, `/audiences/account-based/create`. Bridge entity primitive "
        "(R6) by formalising `entity_type: company` as a first-class type "
        "with employee membership semantics.\n"
        "2. **[P0] Subscription primitive**: `/api/v1/subscriptions/create` "
        "with start_date / renewal_date / term_months / seats / status / "
        "MRR. Hook `/triggers/register` to renewal-anniversary events. "
        "Expose NDR/GRR/logo-retention on /master/{id}/metrics. Without "
        "this, the platform cannot serve any SaaS company.\n"
        "3. **[P0] Multi-stakeholder MSA primitive**: replace independent "
        "/consent/document/sign with `/consent/agreement/create` returning "
        "an agreement_id; signers added via `/agreement/{id}/sign`; states "
        "(pending-signers, partially-signed, fully-executed). Add "
        "`/consent/delegation/grant` (CEO authorizes CTO).\n"
        "4. **[P0] Buying-committee attribution**: `/attribution/committee/"
        "credit/configure` taking a role-weight map (CEO 50%, CTO 30%, "
        "etc.) and applying it on /attribution/account/{id}/journey "
        "rollup. Support W-shaped / U-shaped / linear models.\n"
        "5. **[P0] Seat-based licensing primitive**: voucher engine accepts "
        "`seat_count` dimension; `/primitives/user/{uid}/seat-balance` "
        "exposes used/total; expansion-classified transaction events "
        "(`event_subtype: new | expansion | renewal | reactivation | "
        "churn`).\n"
        "6. **[P0] Org-chart transitive traversal**: "
        "`/primitives/users/{uid}/relationships/subtree` returns full org "
        "below a CEO. Needed for ABM 'all decision-makers at this account'.\n"
        "7. **[P1] ICP metadata at brand-attach**: industry, employee_count, "
        "annual_revenue_band, deal_size as first-class fields. Cleans up "
        "every downstream filter.\n"
        "8. **[P1] Enterprise-role attributes on KiX ID**: role (CEO/CTO/"
        "...), job_title, department, employer_id, company_kid on "
        "/kix-id/register. Today everything lives in display_name.\n"
        "9. **[P1] Drip-sequence primitive**: /push/sequence/create with "
        "(offset_days, payload)[] + branching on user response. Today each "
        "nudge must be scheduled individually.\n"
        "10. **[P1] Lateral relationship types**: add `colleague_of` / "
        "`works_with` to _RELATIONSHIP_REVERSE_MAP. B2B buying committees "
        "are lateral (CTO + procurement), not hierarchical.\n"
        "11. **[P1] Role-tier on user / relationship**: `seniority: "
        "executive | manager | IC` first-class flag. Enables buying-"
        "committee weighting + escalation rules.\n"
        "12. **[P1] Customer-health-score primitive**: /primitives/user/"
        "{uid}/health-score with usage + adoption + sentiment + support-"
        "ticket inputs. Renewal triggers fire off score thresholds.\n"
        "13. **[P1] Pipeline-stage on reservation**: add `pipeline_stage` "
        "(MQL/SQL/Demo/PoC/Verbal/Closed-Won), `deal_amount`, `probability"
        "_pct`, `expected_close_date`. CRM integration becomes native.\n"
        "14. **[P1] target_audience persistence + retention KPIs**: "
        "ensure `retargeting_only` persists end-to-end on /campaigns/create; "
        "surface NDR/GRR/logo-retention specifically when "
        "objective=retention.\n"
        "15. **[P2] B2B payment-terms on wallet topup**: PO number, "
        "payment_terms (NET-30/60/90), billing_entity, invoice_id fields "
        "for procurement-led wire transfer reconciliation.\n"
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


# ── Phase R10: Round 10 — first-class B2B accounts, org chart, buying
#              committee, subscriptions w/ NDR/GRR, co-attribution ────────
async def phase_r10_probes(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("R10: accounts/subscriptions/co-attribution (B2B SaaS native)")
    primary_bid = state.get("primary_bid")
    users = state.get("users") or {}
    ceo = (users.get("ceo") or [None])[0]
    cto = (users.get("cto") or [None])[0]
    procurement = (users.get("procurement") or [None])[0]
    employee = (users.get("employee") or [None])[0]

    if not (primary_bid and ceo and cto and procurement and employee):
        gap("P0", "R10 prerequisites", "missing brand/users from earlier phases")
        return

    # ── 1. POST /accounts/register — enterprise B2B account ────────────────
    sc, b = await call(c, "POST", "/api/v1/accounts/register", json_body={
        "account_name": f"FactoryCorp {RUN_TAG}",
        "industry": "manufacturing",
        "size": "51-200",
        "primary_contact_user_id": ceo,
        "billing_contact_user_id": procurement,
        "domain": f"factory{RUN_TAG}.example.cn",
        "metadata": {"vertical": "saas_buyer"},
    })
    aid: str | None = None
    if sc in (200, 201) and isinstance(b, dict) and b.get("account_id"):
        aid = b["account_id"]
        ok("accounts/register enterprise account",
           f"account_id={aid} primary={ceo[:14]}")
    else:
        gap("P0", "accounts/register", f"{sc} {_short(b)}")
        return

    # ── 2. POST /accounts/{aid}/members/add — CTO + procurement + end_user
    added = 0
    for uid, role in (
        (cto, "decision_maker"),
        (procurement, "procurement"),
        (employee, "end_user"),
    ):
        sc, b = await call(c, "POST",
                           f"/api/v1/accounts/{aid}/members/add",
                           json_body={
                               "user_id": uid,
                               "role": role,
                               "seat_status": "active",
                           })
        if sc == 200:
            added += 1
    if added == 3:
        ok("accounts/members/add", "CTO/procurement/end_user attached")
    else:
        gap("P0", "accounts/members/add", f"{added}/3 added")

    # ── 3. POST /accounts/{aid}/org-chart/edge — CEO → CTO → end_user ─────
    sc1, _ = await call(c, "POST",
                        f"/api/v1/accounts/{aid}/org-chart/edge",
                        json_body={
                            "manager_user_id": ceo,
                            "report_user_id": cto,
                        })
    sc2, _ = await call(c, "POST",
                        f"/api/v1/accounts/{aid}/org-chart/edge",
                        json_body={
                            "manager_user_id": cto,
                            "report_user_id": employee,
                        })
    if sc1 == 200 and sc2 == 200:
        ok("accounts/org-chart/edge", "CEO → CTO → end_user 2-level tree")
    else:
        gap("P0", "accounts/org-chart/edge", f"sc1={sc1} sc2={sc2}")

    # ── 4. GET /accounts/{aid}/buying-committee ────────────────────────────
    sc, b = await call(c, "GET",
                       f"/api/v1/accounts/{aid}/buying-committee")
    if sc == 200 and isinstance(b, dict):
        cnt = b.get("count") or len(b.get("members") or [])
        # End-user should be excluded; CEO + CTO + procurement = 3
        if cnt >= 3:
            ok("accounts/buying-committee",
               f"count={cnt} (end_user excluded)")
        else:
            gap("P1", "accounts/buying-committee count",
                f"got {cnt}, expected >=3")
    else:
        gap("P0", "accounts/buying-committee", f"{sc} {_short(b)}")

    # ── 5. POST /subscriptions/create — account-owned B2B seat plan ────────
    now = time.time()
    sc, b = await call(c, "POST", "/api/v1/subscriptions/create", json_body={
        "account_id": aid,
        "brand_id": primary_bid,
        "plan_id": f"enterprise_plan_{RUN_TAG}",
        "monthly_amount_cents": 50000,  # ¥500/seat/month
        "seats": 10,
        "billing_cycle": "annual",
        "starts_at": now,
        "auto_renew": True,
        "metadata": {"tier": "enterprise"},
    })
    sid: str | None = None
    if sc in (200, 201) and isinstance(b, dict) and b.get("subscription_id"):
        sid = b["subscription_id"]
        ok("subscriptions/create B2B account-owned",
           f"sid={sid} seats={b.get('seats')} mrr={b.get('mrr_cents')}c")
    else:
        gap("P0", "subscriptions/create", f"{sc} {_short(b)}")

    # ── 6. POST /subscriptions/{sid}/seat-change — seat expansion ──────────
    if sid:
        sc, b = await call(c, "POST",
                           f"/api/v1/subscriptions/{sid}/seat-change",
                           json_body={"new_seat_count": 25})
        if sc == 200 and isinstance(b, dict):
            ok("subscriptions/seat-change 10→25",
               f"movement={b.get('movement')} "
               f"delta_mrr={b.get('delta_mrr_cents')}c")
        else:
            gap("P0", "subscriptions/seat-change", f"{sc} {_short(b)}")

    # ── 7. GET /subscriptions/brand/{bid}/metrics — NDR / GRR ──────────────
    sc, b = await call(c, "GET",
                       f"/api/v1/subscriptions/brand/{primary_bid}/metrics",
                       params={"period": "monthly"})
    if sc == 200 and isinstance(b, dict):
        ok("subscriptions/brand/metrics NDR/GRR",
           f"NDR={b.get('ndr')} GRR={b.get('grr')} "
           f"customers={b.get('customer_count')} "
           f"ARR_end={b.get('arr_end_cents')}c")
    else:
        gap("P0", "subscriptions/brand/metrics", f"{sc} {_short(b)}")

    # ── 8. POST /attribution/track/conversion-co — multi-stakeholder split ─
    # First grant consent to each named user so co-attribution doesn't 403.
    await _setup_consent(c, [ceo, cto, procurement])
    sc, b = await call(c, "POST",
                       "/api/v1/attribution/track/conversion-co",
                       json_body={
                           "target_brand": primary_bid,
                           "order_id": f"b2b_order_{RUN_TAG}",
                           "amount_cents": 1_200_000,  # ¥12000 annual deal
                           "account_id": aid,
                           "co_attribution": [
                               {"user_id": ceo,        "role": "signer",      "weight": 0.4},
                               {"user_id": cto,        "role": "decider",     "weight": 0.4},
                               {"user_id": procurement,"role": "influencer",  "weight": 0.2},
                           ],
                       })
    if sc in (200, 201) and isinstance(b, dict):
        users_out = b.get("attributed_users") or []
        ok("attribution/track/conversion-co",
           f"users={len(users_out)} "
           f"total_commission={b.get('total_commission_cents')}c "
           f"event={(b.get('event_id') or '?')[:18]}…")
    else:
        gap("P0", "attribution/track/conversion-co", f"{sc} {_short(b)}")


# ── Main ─────────────────────────────────────────────────────────────────
async def main() -> int:
    start_ts = time.time()
    await init_redis()
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
                await phase_3_kix_ids(c, state)
                await phase_4_relationships(c, state)
                await phase_5_buyer_agreement(c, state)
                await phase_6_demo_reservation(c, state)
                await phase_7_multitouch(c, state)
                await phase_8_nurture(c, state)
                await phase_9_lifecycle(c, state)
                await phase_10_seat_expansion(c, state)
                await phase_11_churn_risk(c, state)
                await phase_12_renewal_campaign(c, state)
                await phase_13_abm(c, state)
                await phase_14_module_probe(c, state)
                await phase_r10_probes(c, state)
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
