"""Merchant journey simulation — 老郑 / Zheng Hao (北京金融科技 — 智信金科).

End-to-end probe of the KiX Ads Platform from the perspective of a Beijing
FINTECH WEALTH-MANAGEMENT MERCHANT. Unique-to-fintech concerns probed:

  * **KYC PII SENSITIVE** — phone-verified KiX ID is the only legal on-ramp;
    身份证 / 银行卡 / 风险问卷 form PII that must NEVER mix with marketing.
  * **RISK GRADIENT** — every product carries a risk tier (R1 货币基金 → R5
    私募); users carry a tolerance score (C1 保守 → C5 激进). Propensity must
    NEVER push high-risk product to low-tolerance user (适当性管理).
  * **CROSS-PRODUCT** — own 货币基金 / 理财 / 保险 / 小额贷款; cross-sell relies
    on "holds A → unlock B" relational voucher predicates.
  * **REGULATORY** — 银保监会 / 证监会 / 一行两会 demand: 双录 (audio+video
    record), 风险揭示, 适当性匹配, 反洗钱 (AML), 大额可疑交易上报, 个保法
    敏感个人信息分类, 广告法 § 25 (金融广告 must show 风险提示 + 业绩不预示).
  * **ANTI-FRAUD** — far heavier than retail; account takeover, 羊毛党, 信用卡盗刷,
    多头借贷 detection.
  * **LIFECYCLE** — new_investor → mature → pre_retirement → post_retirement;
    each stage triggers DIFFERENT product mix.
  * **SUB-BRAND P&L** — 货币基金 / 理财 / 保险 / 贷款 each must reconcile
    revenue split independently (合规分账 required for 持牌 entities).

In-process via httpx.ASGITransport. Requires a live local Redis.

Run:
    .venv/bin/python scripts/sim_laozheng.py
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
OWNER_USER_ID = f"laozheng_{RUN_TAG}"
MASTER_BRAND_ID = f"zhixin_jinke_{RUN_TAG}"

SUB_PRODUCTS = [
    ("mmf", "智信货币基金 / Smart Trust Money Market Fund", "R1"),
    ("wealth", "智信理财 / Smart Trust Wealth Management", "R3"),
    ("insurance", "智信保险 / Smart Trust Insurance", "R2"),
    ("loan", "智信小贷 / Smart Trust Micro-Loan", "R4"),
]

FINDINGS_PATH = Path("/Users/mozat/a-docs/laozheng-sim-findings.md")

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"

# Beijing 国贸 — fintech district
BEIJING_LAT = 39.9088
BEIJING_LNG = 116.4575

# Risk tolerance ladder (C1 conservative → C5 aggressive) per 适当性管理办法
RISK_TOLERANCE_LADDER = ["C1", "C2", "C3", "C4", "C5"]
# Product risk ladder (R1 lowest → R5 highest)
PRODUCT_RISK_LADDER = ["R1", "R2", "R3", "R4", "R5"]

INCOME_TIERS = ["mass", "affluent", "hnw", "uhnw"]   # 大众 / 富裕 / 高净值 / 超高净值
LIFECYCLE_STAGES = [
    "new_investor", "active_investor", "mature_investor",
    "pre_retirement", "post_retirement",
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


# ── Phase 1: Master + 4 product sub-brands ───────────────────────────────
async def phase_1_master_setup(c: httpx.AsyncClient) -> dict[str, Any]:
    _phase_init("1: Master + 4 Fintech Sub-Brands (基金/理财/保险/贷款)")
    state: dict[str, Any] = {"master_id": None, "sub_brands": {}, "product_risk": {}}

    sc, b = await call(c, "POST", "/api/v1/master/create", json_body={
        "company_name": "智信金科 Corp / Smart Trust Fintech",
        "primary_email": "laozheng@zhixin-jinke.cn",
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
    for slug, name, risk in SUB_PRODUCTS:
        bid = f"zhixin_{slug}_{RUN_TAG}"
        state["sub_brands"][slug] = bid
        state["product_risk"][bid] = risk
        sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/brands/attach", json_body={
            "brand_id": bid,
            "store_name": name,
            "store_id": bid,
        })
        if sc == 200:
            attached += 1
        else:
            gap("P1", f"attach product {slug}", f"{sc} {_short(b)}")
    if attached == len(SUB_PRODUCTS):
        ok("attach 4 product lines", "货币基金 / 理财 / 保险 / 贷款")

    # Probe: master.industry / vertical = fintech?
    sc, b = await call(c, "GET", f"/api/v1/master/{master_id}")
    if sc == 200 and isinstance(b, dict):
        if "industry" in b or "vertical" in b:
            ok("master industry field", f"vertical={b.get('industry') or b.get('vertical')}")
        else:
            gap("P0", "master.industry field missing",
                "fintech merchants cannot self-declare vertical at the master level. Every "
                "downstream regulatory / creative / risk filter must guess. Worse than other "
                "verticals because 金融广告 has hard 广告法 § 25 rules (风险提示 mandatory, "
                "业绩不预示, 私募 cannot mass-advertise) — guessing is non-compliant.")

    # Probe: industry='fintech' / 'finance' recipe support
    sc, b = await call(c, "GET", "/api/v1/recipes", params={"industry": "fintech"})
    if sc == 200 and isinstance(b, (list, dict)):
        items = b if isinstance(b, list) else b.get("recipes", b.get("items", []))
        if items:
            ok("recipes industry=fintech", f"{len(items)} match")
        else:
            gap("P0", "no fintech recipes seeded",
                "?industry=fintech returns 0. Fintech merchants get a starbucks_loyalty "
                "fallback — wrong copy (illegal under 广告法 § 25), wrong mechanic (no risk "
                "tier gating), wrong compliance (no 业绩不预示 disclaimer auto-injection). "
                "Need: mmf_yield_compare, wealth_risk_tier_match, insurance_age_bracket, "
                "micro_loan_credit_score recipes — each with 风险提示 boilerplate.")
    else:
        gap("P1", "recipe industry filter", f"{sc} {_short(b)}")

    # Probe: licensing/regulatory metadata on master
    sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/compliance/configure",
                       json_body={
                           "regulator": "银保监会",
                           "license_number": f"金管局-基金代销-{RUN_TAG}",
                           "license_expiry": int(time.time()) + 86400 * 365,
                           "advertising_restrictions": ["no_guaranteed_return", "risk_warning_required"],
                       })
    if sc in (200, 201):
        ok("master compliance metadata configured", "")
    elif sc == 404:
        gap("P0", "no master compliance/license endpoint",
            "POST /master/{id}/compliance/configure 404. Fintech merchants must register "
            "their 持牌 license number (基金销售/保险代销/小贷牌照), expiry, and regulator. "
            "Platform-side absence means: (a) no expiry-driven creative freeze, "
            "(b) no per-product-license type filtering, (c) regulator audit cannot trace "
            "which licensed entity ran which campaign. Required by 银保监会 § XII (持牌经营).")

    return state


# ── Phase 2: Wallet ¥100K + sub-brand P&L split ──────────────────────────
async def phase_2_wallet(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("2: Wallet ¥100K/月 + Sub-Brand P&L (合规分账)")
    master_id = state["master_id"]
    if not master_id:
        return

    # Allocation skewed toward 理财 (largest revenue contributor) and 保险 (high margin)
    allocation = {
        state["sub_brands"]["mmf"]: 0.15,
        state["sub_brands"]["wealth"]: 0.40,
        state["sub_brands"]["insurance"]: 0.30,
        state["sub_brands"]["loan"]: 0.15,
    }
    sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/budget/global", json_body={
        "monthly_budget_cents": 10_000_000,  # ¥100K
        "allocation": allocation,
    })
    if sc == 200:
        ok("set master global budget", "¥100K/月 split: 15% 基金 / 40% 理财 / 30% 保险 / 15% 贷款")
    else:
        gap("P1", "set master global budget", f"{sc} {_short(b)}")

    # Top up 理财 directly
    wealth_bid = state["sub_brands"]["wealth"]
    sc, b = await call(c, "POST", f"/api/v1/wallet/{wealth_bid}/topup", json_body={
        "amount_cents": 4_000_000,
        "payment_method": "wechat",
    })
    if sc == 200 and isinstance(b, dict) and "topup_id" in b:
        tid = b["topup_id"]
        sc2, _ = await call(c, "POST", f"/api/v1/wallet/{wealth_bid}/topup/{tid}/confirm",
                            json_body={"payment_gateway_response": {"mock": True}})
        if sc2 == 200:
            ok("topup 理财 ¥40K + confirm", "")
        else:
            gap("P1", "confirm topup 理财", f"{sc2}")
    else:
        gap("P1", "topup 理财", f"{sc} {_short(b)}")

    # Probe: sub-brand independent P&L report (持牌分账)
    sc, b = await call(c, "GET", f"/api/v1/master/{master_id}/revenue/by-brand",
                       params={"period": "month"})
    if sc == 200 and isinstance(b, (list, dict)):
        ok("per-sub-brand revenue report exists", _short(b, 120))
    elif sc == 404:
        gap("P0", "no per-sub-brand revenue split report",
            "GET /master/{id}/revenue/by-brand 404. Each licensed fintech sub-entity "
            "(基金销售 vs 保险代销 vs 小贷) must report revenue SEPARATELY to its respective "
            "regulator (基金=证监会, 保险=银保监会, 贷款=金管局). Without this endpoint, "
            "持牌分账 (compliant revenue split per license) is impossible — auditors will "
            "reject mixed P&L.")
    else:
        gap("P1", "revenue by-brand", f"{sc} {_short(b)}")

    # Probe: per-product attribution that respects 持牌 boundary
    sc, b = await call(c, "GET", f"/api/v1/wallet/{wealth_bid}/daily-budget-status")
    if sc == 200 and isinstance(b, dict):
        ok("理财 wallet status", f"daily_budget_cents={b.get('today_budget_cents') or b.get('daily_budget_cents', 0)}")
    else:
        gap("P2", "理财 wallet status", f"{sc} {_short(b)}")


# ── Phase 3: Phone-Verified KiX ID — Real KYC On-Ramp ────────────────────
async def phase_3_kix_id_kyc(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("3: Phone-Verified KiX ID (KYC Backbone — 实名制)")
    primary_bid = state["sub_brands"]["wealth"]
    state["primary_bid"] = primary_bid

    # Register KiX ID with a phone (legal hook for 实名制)
    rng = random.Random(RUN_TAG + 3)
    investor_phone = f"+8613{rng.randint(100000000, 999999999)}"
    sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
        "phone": investor_phone,
        "device_fingerprint": f"dev_zheng_{RUN_TAG}_0001",
        "display_name": "陈建国",
        "primary_language": "zh-CN",
        "source_brand_id": primary_bid,
        "country": "CN",
    })
    if sc in (200, 201) and isinstance(b, dict) and "kid" in b:
        state["primary_kid"] = b["kid"]
        ok("KiX ID register w/ phone", f"kid={b['kid']} is_new={b.get('is_new')}")
    else:
        gap("P0", "KiX ID register w/ phone",
            f"{sc} {_short(b)} — phone-based KiX ID register is the KYC on-ramp; "
            "registration failing blocks every downstream test.")
        return

    # Probe: identity-link with verification_token (OTP confirmation) — proves phone is verified
    sc, b = await call(c, "POST", f"/api/v1/kix-id/{state['primary_kid']}/identity-link",
                       json_body={
                           "phone": investor_phone,
                           "verification_token": f"OTP_{RUN_TAG}",   # OTP proof
                       })
    if sc in (200, 201):
        ok("phone identity-link with verification_token", "OTP proof accepted")
        state["phone_verified"] = True
    elif sc in (400, 422):
        gap("P0", "phone verification_token rejected",
            f"{sc} {_short(b)} — KiX ID identity-link rejects OTP-style verification_token. "
            "Fintech needs a verifiable proof of phone ownership before any investment action "
            "(反洗钱法 § 16, 个保法 § 28 sensitive PI). Without verified phone, KYC is fake.")
    elif sc == 404:
        gap("P0", "no identity-link endpoint to attach verified phone",
            "404 on /kix-id/{kid}/identity-link. Cannot upgrade an anonymous KiX ID to a "
            "phone-verified KYC identity. Critical regression: fintech onboarding cannot "
            "differentiate between guest browsing and 实名 投资者.")
    else:
        gap("P1", "phone identity-link", f"{sc} {_short(b)}")

    # Probe: lookup-by-phone (regulator anti-money-laundering trace path)
    sc, b = await call(c, "POST", "/api/v1/kix-id/lookup", json_body={"phone": investor_phone})
    if sc == 200 and isinstance(b, dict) and b.get("found") and b.get("kid") == state["primary_kid"]:
        ok("phone → kid reverse lookup", "AML trace path works")
    elif sc == 200 and not b.get("found"):
        gap("P0", "phone reverse-lookup returns not found",
            "lookup right after register returns found=False. AML 反洗钱法 § 30 (大额可疑交易 "
            "上报) requires fast phone→identity lookup for regulator queries. Currently broken.")
    else:
        gap("P1", "phone reverse-lookup", f"{sc} {_short(b)}")

    # Probe: identity-link with NO verification_token (should reject under 实名制)
    fake_kid = state["primary_kid"]
    fake_phone = f"+8613{rng.randint(100000000, 999999999)}"
    sc, b = await call(c, "POST", f"/api/v1/kix-id/{fake_kid}/identity-link",
                       json_body={"phone": fake_phone})   # NO OTP token
    if sc in (400, 422):
        ok("unverified phone link rejected", f"{sc} — 实名制 enforced")
    elif sc in (200, 201):
        gap("P0", "phone link succeeds without OTP",
            "identity-link accepted phone WITHOUT verification_token. Anyone could attach "
            "any phone to any kid → impersonation, AML evasion, regulator audit fail. "
            "Must hard-require verification_token.")
    else:
        info(f"link-no-token sc={sc}")


# ── Phase 4: Consent — `pii_kyc` scope probe + 适当性 consent ────────────
async def phase_4_consent(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("4: Consent — pii_kyc scope + 适当性管理 双录 consent")
    primary_bid = state["primary_bid"]

    sc, b = await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
        "version": f"v_{RUN_TAG}",
        "text_md": (
            "# 智信金科 隐私政策\n"
            "涵盖 PII (身份证/银行卡/手机/地址) 存储、跨产品共享、风险评估、双录 (audio+video) "
            "保留与回溯。等同 个保法 + 反洗钱法 + 适当性管理办法 + 一行两会 监管要求。"
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

    # R7: pii_kyc — REGULATED, needs consent_evidence
    pii_probe_uid = f"pii_probe_{RUN_TAG}"
    sc, b = await call(c, "POST", "/api/v1/consent/grant", json_body={
        "user_id": pii_probe_uid,
        "scopes": ["pii_kyc"],
        "policy_version": f"v_{RUN_TAG}",
        "source": "app",
        "consent_evidence": {
            "method": "signature",
            "reference": f"sig_kyc_{RUN_TAG}",
        },
    })
    body_str = json.dumps(b) if isinstance(b, dict) else str(b)
    if sc == 200:
        ok("pii_kyc scope accepted w/ consent_evidence", "FINTECH 个保法 §28 wired")
        state["pii_probe_uid"] = pii_probe_uid
    elif sc in (400, 422) and "scope" in body_str.lower():
        gap("P0", "pii_kyc consent scope missing", f"{sc} {_short(b)}")
    else:
        gap("P1", "pii_kyc scope probe", f"{sc} {_short(b)}")

    # R7: financial_data scope
    sc, b = await call(c, "POST", "/api/v1/consent/grant", json_body={
        "user_id": f"fin_probe_{RUN_TAG}",
        "scopes": ["financial_data"],
        "policy_version": f"v_{RUN_TAG}",
        "source": "app",
    })
    if sc == 200:
        ok("financial_data scope accepted", "")
    elif sc not in (400, 422):
        gap("P2", "financial_data scope probe", f"{sc} {_short(b)}")

    # R7: 双录 audio_video_recording REGULATED scope — needs consent_evidence
    shuanglu_uid = f"shuanglu_probe_{RUN_TAG}"
    sc, b = await call(c, "POST", "/api/v1/consent/grant", json_body={
        "user_id": shuanglu_uid,
        "scopes": ["audio_video_recording"],
        "policy_version": f"v_{RUN_TAG}",
        "source": "app",
        "consent_evidence": {
            "method": "video",
            "reference": f"REC_{RUN_TAG}_av180s",
        },
    })
    if sc == 200:
        ok("双录 audio_video_recording scope accepted w/ evidence",
           "适当性管理办法 §28 双录 wired")
        state["shuanglu_uid"] = shuanglu_uid
    elif sc in (400, 422):
        gap("P0", "双录 (audio+video) consent scope missing", f"{sc} {_short(b)}")

    # R7: /consent/document/sign type="双录" with signature_method=video_recording
    sc, b = await call(c, "POST", "/api/v1/consent/document/sign", json_body={
        "user_id": pii_probe_uid,
        "document_type": "双录",
        "document_version": f"v_{RUN_TAG}",
        "document_url": "https://example.com/双录.pdf",
        "signature_method": "video_recording",
        "signature_evidence_url": f"s3://kix-fintech/rec_{RUN_TAG}.mp4",
        "granted_scopes": ["audio_video_recording", "financial_data"],
    })
    if sc == 200 and isinstance(b, dict) and b.get("document_consent_id"):
        ok("双录 /document/sign", f"dcons={b['document_consent_id']}")
        state["shuanglu_doc_id"] = b["document_consent_id"]
    else:
        gap("P0", "双录 /document/sign", f"{sc} {_short(b)}")

    # R7: media.upload for KYC document (身份证 scan)
    sc, b = await call(c, "POST", "/api/v1/media/upload", json_body={
        "owner_user_id": pii_probe_uid,
        "brand_id": primary_bid,
        "media_class": "document",  # 身份证 = document class
        "storage_url": f"s3://kix-fintech/kyc_idcard_{RUN_TAG}.jpg",
        "content_hash": "sha256:" + "k" * 40,
        "mime_type": "image/jpeg",
        "size_bytes": 204800,
        "retention_days": 1825,  # 5y KYC retention
        "metadata": {"document_kind": "id_card_front"},
    })
    if sc == 200 and isinstance(b, dict) and b.get("media_id"):
        ok("media.upload KYC document", f"media_id={b['media_id']}")
        state["kyc_media_id"] = b["media_id"]
    else:
        gap("P0", "media.upload KYC document", f"{sc} {_short(b)}")

    # Probe: investor tier ladder (mass / affluent / hnw / uhnw)
    sc, b = await call(c, "POST", "/api/v1/primitives/tier/configure", json_body={
        "brand_id": primary_bid,
        "tiers": [
            {"name": "mass", "xp_min": 0},
            {"name": "affluent", "xp_min": 1_000_000},      # ≥¥10K AUM
            {"name": "hnw", "xp_min": 10_000_000},          # ≥¥100K AUM
            {"name": "uhnw", "xp_min": 100_000_000},        # ≥¥1M AUM
        ],
    })
    if sc == 200:
        ok("investor tier ladder configured", "mass / affluent / hnw / uhnw (AUM-based)")
    else:
        gap("P1", "configure investor tier", f"{sc} {_short(b)}")


async def _setup_consent(c: httpx.AsyncClient, user_ids: list[str], version: str) -> int:
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


# ── Phase 5: Risk Profile User Attributes (适当性匹配 score) ─────────────
async def phase_5_risk_profile(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("5: Risk Profile User Attributes (C1-C5 适当性匹配)")
    bid = state["primary_bid"]
    ver = state["consent_version"]

    # Generate 50 investors with diverse profiles
    rng = random.Random(RUN_TAG + 5)
    investors = []
    for i in range(50):
        uid = f"investor_{i:03d}_{RUN_TAG}"
        investors.append(uid)
    state["investors"] = investors
    state["consented_investors"] = await _setup_consent(c, investors, ver)
    ok("consent 50 investors", f"{state['consented_investors']}/50 granted")

    # Persist risk profile attributes
    primary_uid = investors[0]
    sc, b = await call(c, "POST", f"/api/v1/primitives/user/{primary_uid}/attributes",
                       json_body={
                           "brand_id": bid,
                           "attrs": {
                               "risk_tolerance": "C2",        # 适当性 score
                               "income_tier": "affluent",
                               "investment_goal": "wealth_preservation",
                               "investment_horizon_years": 5,
                               "investable_assets_cny": 500_000,
                               "id_number_hash": _sha("310101199001011234")[:16],   # never plaintext
                               "bank_card_hash": _sha("6228480001234567890")[:16],
                               "occupation": "engineer",
                           },
                           "pii": True,         # probe: PII flag recognised?
                           "sensitivity": "high",
                       })
    if sc in (200, 201):
        ok("risk profile attributes stored", "C2 conservative, affluent, ¥500K investable")
        # Probe: pii flag readback
        sc2, b2 = await call(c, "GET", f"/api/v1/primitives/user/{primary_uid}/attributes",
                             params={"brand_id": bid})
        if sc2 == 200 and isinstance(b2, dict):
            flagged = b2.get("pii") or b2.get("sensitivity") or (
                isinstance(b2.get("meta"), dict) and b2["meta"].get("pii"))
            if flagged:
                ok("PII compliance flag persisted", f"flag={flagged}")
            else:
                gap("P0", "PII compliance flag silently dropped",
                    "POST /primitives/user/{uid}/attributes accepted pii=True + sensitivity=high "
                    "but readback shows no flag. Fintech cannot mark attributes as 敏感个人信息. "
                    "Same Redis bucket as marketing nickname. 个保法 § 28 violation — 敏感 PI "
                    "must be differentially protected.")
    else:
        gap("P1", "risk profile attribute store", f"{sc} {_short(b)}")

    # Probe: id_number / bank_card stored plaintext (should NEVER happen — but check)
    sc, b = await call(c, "GET",
                       f"/api/v1/primitives/user/{primary_uid}/attributes/id_number_hash")
    if sc == 200 and isinstance(b, dict):
        v = b.get("value") or b.get("id_number_hash") or ""
        if str(v).isdigit() and len(str(v)) == 18:
            gap("P0", "PII stored plaintext (身份证号)",
                "id_number_hash readback returns 18-digit plaintext ID. No field-level "
                "encryption / tokenisation. 个保法 § 51 + 网络安全法 § 21 require encryption "
                "for 敏感个人信息. Direct legal exposure.")
        else:
            ok("ID number not plaintext", "stored as hash/token")
    elif sc == 404:
        info("single-key readback 404 — attribute scope differs")

    # Bulk assign random risk profiles to investors for cohort simulation
    rng = random.Random(RUN_TAG + 50)
    profile_bulk_ok = 0
    for uid in investors[1:30]:
        tolerance = rng.choice(RISK_TOLERANCE_LADDER)
        income = rng.choice(INCOME_TIERS)
        lifecycle = rng.choice(LIFECYCLE_STAGES)
        sc, _ = await call(c, "POST", f"/api/v1/primitives/user/{uid}/attributes",
                           json_body={
                               "brand_id": bid,
                               "attrs": {
                                   "risk_tolerance": tolerance,
                                   "income_tier": income,
                                   "lifecycle_stage": lifecycle,
                                   "investable_assets_cny": rng.randint(10_000, 5_000_000),
                                   "investment_horizon_years": rng.randint(1, 30),
                               },
                           })
        if sc in (200, 201):
            profile_bulk_ok += 1
    if profile_bulk_ok >= 25:
        ok("bulk investor profiles", f"{profile_bulk_ok}/29 stored")
    else:
        gap("P1", "bulk investor profiles", f"only {profile_bulk_ok}/29")

    # Probe: lifecycle-stage primitive (Round-5 lifecycle filter)
    sc, b = await call(c, "POST",
                       f"/api/v1/primitives/user/{primary_uid}/attributes/lifecycle-stage",
                       json_body={
                           "brand_id": bid,
                           "stage": "pre_retirement",
                           "evidence": {"age": 55, "computed_at": int(time.time())},
                       })
    if sc in (200, 201):
        ok("lifecycle-stage = pre_retirement set", "Round-5 lifecycle primitive works")
        state["lifecycle_works"] = True
    elif sc in (400, 422):
        gap("P1", "lifecycle-stage Round-5 primitive partial",
            f"{sc} {_short(b)} — lifecycle-stage POST rejects pre_retirement. Fintech needs "
            "new_investor / active / mature / pre_retirement / post_retirement; if stage enum "
            "is fixed (signup/active/churned), regulatory product mix triggers (pre-retirement "
            "→ annuity-only) cannot fire.")
    else:
        gap("P1", "lifecycle-stage primitive", f"{sc} {_short(b)}")


# ── Phase 6: Suitability Propensity Gate (适当性匹配 enforcement) ────────
async def phase_6_suitability(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("6: Suitability Propensity Gate (限制 C1 ↛ R5)")
    bid = state["primary_bid"]
    primary_uid = state["investors"][0]   # C2 conservative

    # Probe: propensity prediction with risk-suitability awareness
    sc, b = await call(c, "POST", "/api/v1/propensity/predict", json_body={
        "user_id": primary_uid,
        "brand_id": bid,
        "product_risk_tier": "R5",        # 私募 high-risk
        "context": {"product_id": "private_equity_fund_xyz"},
    })
    if sc == 200 and isinstance(b, dict):
        score = b.get("score") or b.get("propensity_score") or 0
        suit = b.get("suitability_blocked") or b.get("suitability_warning")
        if suit or score == 0:
            ok("propensity blocks/zeros C2→R5 push", f"suitability gate honored: {_short(b, 120)}")
        else:
            gap("P0", "propensity ignores 适当性 mismatch",
                f"{_short(b)} — propensity returned score={score} for C2 user × R5 product. "
                "适当性管理办法 § 17 PROHIBITS marketing 中高风险 products to 低风险承受能力 "
                "investors. Platform must hard-zero propensity (or return suitability_blocked) "
                "when product_risk_tier > user.risk_tolerance. Currently no gate.")
    elif sc == 404:
        gap("P0", "propensity endpoint missing or no risk-aware shape",
            f"404 on /propensity/predict with product_risk_tier. Fintech cannot use the "
            "platform recommender without suitability injection.")
    elif sc in (400, 422):
        gap("P0", "propensity has no product_risk_tier parameter",
            f"{sc} {_short(b)} — propensity API doesn't accept product_risk_tier nor inspect "
            "user.risk_tolerance. Any retail recipe will inadvertently push 私募 to grandmas. "
            "This is the most expensive 监管 violation in fintech (sanctioned 私募 misselling "
            "got firms fined ¥10M+ in 2023).")
    else:
        info(f"propensity sc={sc}")

    # Probe: audience filter with risk-tolerance + lifecycle (适当性 cohort)
    sc, b = await call(c, "POST", "/api/v1/audiences/custom/create", json_body={
        "brand_id": bid,
        "name": "Conservative pre-retirement investors (C1-C2 + pre_retirement)",
        "source": "manual",
        "predicates": [
            {"attribute": "risk_tolerance", "in": ["C1", "C2"]},
            {"attribute": "lifecycle_stage", "eq": "pre_retirement"},
        ],
        "predicate_logic": "AND",
    })
    if sc == 200:
        ok("risk×lifecycle cohort audience created", "适当性 audience builds")
    elif sc in (400, 422):
        gap("P1", "audience predicate vocabulary lacks risk/lifecycle attributes",
            f"{sc} {_short(b)} — audiences.custom.create rejects {{attribute, in/eq}} shape "
            "for user_attributes. Fintech cannot build 适当性 audiences declaratively → every "
            "campaign hand-rolls a query → drift between audience definition and actual targets.")
    else:
        gap("P1", "risk×lifecycle audience", f"{sc} {_short(b)}")


# ── Phase 7: Cross-Product Bundle Voucher (Round-4 relational) ───────────
async def phase_7_cross_product_voucher(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("7: Cross-Product Bundle Voucher (持有 货币基金 → 解锁 理财)")
    wealth_bid = state["sub_brands"]["wealth"]

    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": wealth_bid,
        "name": "Cross-Product Reward — 持有 货币基金 ≥30天 解锁 理财 1%加息",
        "description": "Yield boost for existing money-market fund holders crossing to wealth mgmt",
        "value": {"type": "percent_bps", "amount": 100, "currency": "CNY"},  # +1.00% APR
        "conditions": {
            # Round-4-style relational predicate
            "requires_holding": {
                "brand_id": state["sub_brands"]["mmf"],
                "product_type": "money_market_fund",
                "min_holding_cents": 1_000_000,   # ≥¥10K in MMF
                "min_days_held": 30,
            },
            "min_purchase_cents": 5_000_000,     # ≥¥50K wealth product
            "usage_limit_per_user": 1,
        },
        "expires_in_days": 90,
        "stackable": False,
        "transferable": False,
    })
    if sc in (200, 201) and isinstance(b, dict):
        tid = b.get("template_id")
        ok("cross-product voucher (requires_holding) accepted", f"template_id={tid}")
        # Probe readback
        sc2, b2 = await call(c, "GET", f"/api/v1/brands/{wealth_bid}/vouchers/templates/{tid}")
        if sc2 == 200 and isinstance(b2, dict):
            conds = b2.get("conditions", {})
            if isinstance(conds, dict) and conds.get("requires_holding"):
                ok("requires_holding predicate persisted", "")
            else:
                gap("P0", "requires_holding condition silently dropped",
                    "template created but readback has no `conditions.requires_holding`. "
                    "Cross-sell is the fintech business model — without relational predicates "
                    "linking holdings across sub-brands, every 'hold A unlock B' offer requires "
                    "merchant-side ETL. 50K-user multi-product playbook collapses to manual lists.")
        elif sc2 == 404:
            gap("P2", "voucher template readback path", "no GET /brands/{bid}/vouchers/templates/{tid}")
    elif sc in (400, 422):
        gap("P0", "requires_holding voucher condition unsupported",
            f"{sc} {_short(b)} — voucher conditions vocabulary "
            "(min_purchase_cents / usage_limit) has no `requires_holding` predicate referring "
            "to a sibling sub-brand's product. Fintech cross-sell ENTIRE BUSINESS MODEL "
            "depends on this. Without it, 智信金科 cannot run its core offer.")

    # Probe: stack of requires_holding + risk-tolerance ≥ Cn
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": state["sub_brands"]["insurance"],
        "name": "保险加保 — 理财 ≥¥100K + risk_tolerance ≥ C3 → 重疾险 8 折",
        "description": "Critical illness 20%-off for active 理财 holders with C3+ risk profile",
        "value": {"type": "percent", "amount": 20, "currency": "CNY"},
        "conditions": {
            "requires_holding": {
                "brand_id": state["sub_brands"]["wealth"],
                "min_holding_cents": 10_000_000,
            },
            "user_attribute_in": {"risk_tolerance": ["C3", "C4", "C5"]},
            "lifecycle_stage_in": ["mature_investor", "pre_retirement"],
            "min_purchase_cents": 200_000,
        },
        "expires_in_days": 60,
    })
    if sc in (200, 201):
        ok("compound holding × attribute × lifecycle voucher condition", "")
    elif sc in (400, 422):
        gap("P1", "compound voucher conditions partial/missing",
            f"{sc} {_short(b)} — voucher conditions cannot stack requires_holding + "
            "user_attribute_in + lifecycle_stage_in. Each axis individually unsupported even "
            "if any one passes. Cross-product × risk × lifecycle is the typical fintech "
            "bundle — every bundle becomes a code change.")


# ── Phase 8: Master-Level Investor Tier (Round-4 cross-brand) ────────────
async def phase_8_master_tier(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("8: Master-Level Investor Tier (cross-product AUM aggregation)")
    master_id = state["master_id"]
    primary_uid = state["investors"][0]

    # Probe: master-scoped tier (cross-product AUM)
    sc, b = await call(c, "POST", "/api/v1/primitives/tier/configure", json_body={
        "master_id": master_id,
        "tiers": [
            {"name": "starter", "xp_min": 0},
            {"name": "silver", "xp_min": 5_000_000},      # ¥50K AUM cross-product
            {"name": "gold", "xp_min": 50_000_000},       # ¥500K AUM
            {"name": "private_bank", "xp_min": 600_000_000},  # ¥6M AUM (CN 私行 threshold)
        ],
    })
    if sc == 200:
        ok("master-scoped investor tier configured", "starter/silver/gold/private_bank")
        state["master_tier_works"] = True
    elif sc in (400, 422):
        gap("P0", "master-scoped tier rejected",
            f"{sc} {_short(b)} — tier/configure only accepts brand_id. Fintech needs AUM "
            "aggregation across 货币基金 + 理财 + 保险 (loans NEGATIVE-weight). Per-brand "
            "tier means an investor with ¥100K split across 4 products shows as 4 separate "
            "low-tier accounts. Private banking onboarding (¥6M+) cannot fire.")

    # Probe: per-user tier readback (cross-product view)
    sc, b = await call(c, "GET", f"/api/v1/primitives/user/{primary_uid}/tier",
                       params={"master_id": master_id})
    if sc == 200 and isinstance(b, dict):
        if b.get("master_id") or b.get("tier_master_id"):
            ok("master tier on user", f"tier={b.get('current_tier')}")
        else:
            gap("P1", "master tier readback ambiguous",
                f"endpoint returned, but no master_id field on response: {_short(b, 120)}")
    elif sc == 404:
        gap("P1", "master tier on user path",
            "GET /primitives/user/{uid}/tier?master_id=… 404. Cross-product AUM tier "
            "(holds MMF + wealth + insurance → gold) cannot be queried.")
    else:
        gap("P1", "master tier query", f"{sc} {_short(b)}")

    # R7: scope=global tier ladder via /master/{mid}/tier/configure
    sc, b = await call(c, "POST",
                       f"/api/v1/master/{master_id}/tier/configure",
                       json_body={
                           "tiers": [
                               {"name": "starter_global", "xp_min": 0},
                               {"name": "gold_global", "xp_min": 50_000_000},
                               {"name": "private_bank_global", "xp_min": 600_000_000},
                           ],
                           "aggregation": "sum",
                           "scope": "global",
                       })
    if sc == 200 and isinstance(b, dict) and b.get("scope") == "global":
        ok("master tier scope=global configured",
           f"fanned_to={len(b.get('fanned_to', []))}")
        state["global_tier_works"] = True
    else:
        gap("P0", "master tier scope=global", f"{sc} {_short(b)}")

    # R7: tier-portability — full map across brand/region/master/global
    sc, b = await call(c, "GET",
                       f"/api/v1/master/{master_id}/user/{primary_uid}/tier-portability")
    if sc == 200 and isinstance(b, dict):
        if "portability_map" in b and "brand_tiers" in b:
            ok("tier-portability map", f"global_tier={b.get('global_tier')} "
               f"brands={len(b.get('brand_tiers', {}))} "
               f"map_entries={len(b.get('portability_map', {}))}")
        else:
            gap("P1", "tier-portability shape", f"missing keys: {list(b.keys())}")
    elif sc == 404:
        gap("P0", "tier-portability endpoint missing",
            "GET /master/{mid}/user/{uid}/tier-portability 404")
    else:
        gap("P1", "tier-portability", f"{sc} {_short(b)}")

    # R7: tier-by-scope?scope=global
    sc, b = await call(c, "GET",
                       f"/api/v1/master/{master_id}/user/{primary_uid}/tier-by-scope",
                       params={"scope": "global"})
    if sc == 200 and isinstance(b, dict):
        ok("tier-by-scope scope=global", f"tier={b.get('tier')}")
    else:
        gap("P1", "tier-by-scope scope=global", f"{sc} {_short(b)}")


# ── Phase 9: Regulatory Creative-Gen Compliance Flag ─────────────────────
async def phase_9_regulatory_creative(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("9: Regulatory Compliance Flag on Creative-Gen (广告法 § 25)")
    bid = state["primary_bid"]

    # Try to generate a creative WITHOUT a 风险提示 — should be rejected or auto-injected
    sc, b = await call(c, "POST", "/api/v1/creatives/generate", json_body={
        "brand_id": bid,
        "recipe_id": "starbucks_loyalty",   # fallback recipe (no fintech recipe yet)
        "context": {
            "product_name": "智信稳赢90天理财",
            "headline": "保证收益 4.5%！稳赚不赔！",   # ILLEGAL claims under 广告法 § 25
            "cta": "立即购买",
        },
    })
    if sc == 200 and isinstance(b, dict):
        warnings = b.get("compliance_warnings") or b.get("violations") or []
        if warnings:
            ok("creative-gen flagged illegal language", f"warnings={_short(warnings, 120)}")
        else:
            gap("P0", "creative-gen does NOT detect '保证收益/稳赚不赔'",
                f"创意生成 accepted '保证收益' + '稳赚不赔' headlines verbatim — both BANNED by "
                "广告法 § 25(1)(2): no guarantees of return, no past-performance promises. "
                "Platform must auto-detect banned phrases (regex/list) AND auto-append 风险提示 "
                "footer for any fintech creative. 监管 fines start at ¥200K + license suspension.")
    elif sc == 404:
        gap("P1", "creative-gen endpoint missing",
            "no /creatives/generate path; cannot verify compliance auto-injection.")
    elif sc in (400, 422):
        # Schema rejection is also OK — at least it didn't accept the illegal copy
        info(f"creative-gen schema rejection: {_short(b, 120)}")

    # Probe: compliance scan endpoint (paste copy → get violations)
    sc, b = await call(c, "POST", "/api/v1/compliance/scan", json_body={
        "industry": "fintech",
        "country": "CN",
        "text": "保证收益 4.5%，稳赚不赔，立即购买，私募基金，无风险！",
    })
    if sc == 200 and isinstance(b, dict):
        ok("compliance scan returned", f"{_short(b, 200)}")
    elif sc == 404:
        gap("P0", "no /compliance/scan endpoint",
            "no path to scan a copy block for banned phrases. Every merchant must self-police "
            "广告法 § 25. Platform should ship a standard banned-phrase list (保证/承诺/必赚/"
            "稳赚/零风险/无风险/最高/排名/业绩第一) + auto-append 风险提示 boilerplate. "
            "Same primitive serves healthcare (no cure promise), education (no 升学率 "
            "guarantee). Single API, many verticals.")

    # Probe: 风险提示 (risk disclaimer) auto-injection setting
    sc, b = await call(c, "POST", f"/api/v1/master/{state['master_id']}/compliance/disclaimer-template",
                       json_body={
                           "industry": "fintech",
                           "disclaimer_zh": "市场有风险，投资需谨慎；过往业绩不预示其未来表现。",
                           "auto_inject": True,
                           "min_size_px": 14,
                       })
    if sc in (200, 201):
        ok("风险提示 auto-inject template configured", "")
    elif sc == 404:
        gap("P1", "no disclaimer-template endpoint",
            "fintech merchants cannot pre-register 风险提示 to auto-append. Every creative "
            "must manually include the right boilerplate — drift = 监管 violation.")


# ── Phase 10: Anti-Fraud / 反洗钱 (AML) Heavier Than Retail ──────────────
async def phase_10_antifraud_aml(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("10: Anti-Fraud + AML 反洗钱 (账户接管 / 羊毛党 / 大额可疑交易)")
    bid = state["primary_bid"]
    primary_uid = state["investors"][0]

    # Probe: device-fingerprint multi-account binding alert
    sc, b = await call(c, "POST", "/api/v1/fraud/check", json_body={
        "user_id": primary_uid,
        "brand_id": bid,
        "device_fingerprint": f"dev_zheng_{RUN_TAG}_0001",   # already bound to another kid
        "action": "investment_purchase",
        "amount_cents": 5_000_000,
        "context": {
            "ip": "203.0.113.42",
            "geo": "Beijing",
        },
    })
    if sc == 200 and isinstance(b, dict):
        risk = b.get("risk_score") or b.get("risk_level") or "unknown"
        rules = b.get("rules_triggered") or b.get("signals") or []
        ok("fraud/check endpoint returned", f"risk={risk} rules={_short(rules, 120)}")
    elif sc == 404:
        gap("P0", "no /fraud/check endpoint",
            "fintech anti-fraud is structurally absent. /fraud/check 404. Cannot enforce: "
            "(a) device-fingerprint multi-account binding (羊毛党), "
            "(b) velocity check (>5 投资 in 1 hour), "
            "(c) geo anomaly (Beijing user purchases from Estonia IP), "
            "(d) high-amount cooling-period (≥¥50K need 24h cool-off per 适当性). "
            "All four are baseline fintech expectations.")

    # Probe: large-amount / suspicious transaction reporting hook (反洗钱法 § 30)
    sc, b = await call(c, "POST", "/api/v1/aml/report", json_body={
        "user_id": primary_uid,
        "brand_id": bid,
        "transaction_type": "investment_purchase",
        "amount_cents": 50_000_000,   # ¥500K — 大额 threshold ≥ ¥200K
        "currency": "CNY",
        "context": {"source_of_funds": "salary"},
    })
    if sc in (200, 201):
        ok("/aml/report endpoint accepts 大额 report", "")
    elif sc == 404:
        gap("P0", "no /aml/report endpoint",
            "反洗钱法 § 30 requires 大额可疑交易上报 within 5 business days. 持牌 fintech must "
            "be able to programmatically file these. Without endpoint = manual filing = "
            "license risk. Should support 大额 (≥¥200K) + 可疑 (structuring, smurfing) + "
            "PEP (政治公众人物) flags.")

    # Probe: velocity rule registration
    sc, b = await call(c, "POST", "/api/v1/fraud/rules/register", json_body={
        "brand_id": bid,
        "rule_type": "velocity",
        "config": {
            "window_seconds": 3600,
            "max_events": 5,
            "event_type": "investment_purchase",
        },
        "action": "challenge_with_otp",
    })
    if sc in (200, 201):
        ok("velocity fraud rule registered", "")
    elif sc == 404:
        gap("P1", "no /fraud/rules/register",
            "cannot programmatically install velocity / pattern rules. Fintech needs at "
            "minimum: velocity, geo-anomaly, device-anomaly, amount-anomaly. Generic primitive "
            "would help retail too (refund-fraud).")


# ── Phase 11: Lifecycle Audience Filter (Round-5 new/mature/pre-retirement)─
async def phase_11_lifecycle_audience(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("11: Lifecycle Audience Filter (Round-5)")
    bid = state["primary_bid"]

    # New investor cohort (signup < 30 days, never invested)
    sc, b = await call(c, "POST", "/api/v1/audiences/custom/create", json_body={
        "brand_id": bid,
        "name": "New investor onboarding cohort (signed up < 30d, 0 investments)",
        "source": "manual",
        "predicates": [
            {"attribute": "lifecycle_stage", "eq": "new_investor"},
            {"event": "investment_purchase", "count_eq": 0},
        ],
        "predicate_logic": "AND",
    })
    if sc == 200:
        ok("new_investor cohort audience accepted", "")
    elif sc in (400, 422):
        gap("P1", "new_investor lifecycle predicate",
            f"{sc} {_short(b)} — audience predicate cannot filter by lifecycle_stage. "
            "Onboarding campaigns (welcome MMF gift, risk questionnaire reminder) cannot "
            "target the right cohort.")
    else:
        gap("P1", "new_investor cohort", f"{sc} {_short(b)}")

    # Pre-retirement cohort (age 55-65, mature investor, conservative pivot)
    sc, b = await call(c, "POST", "/api/v1/audiences/custom/create", json_body={
        "brand_id": bid,
        "name": "Pre-retirement annuity-eligible (lifecycle=pre_retirement)",
        "source": "manual",
        "predicates": [
            {"attribute": "lifecycle_stage", "eq": "pre_retirement"},
            {"attribute": "risk_tolerance", "in": ["C1", "C2", "C3"]},
            {"attribute": "investable_assets_cny", "gte": 500_000},
        ],
        "predicate_logic": "AND",
    })
    if sc == 200:
        ok("pre_retirement cohort accepted", "")
    elif sc in (400, 422):
        gap("P0", "pre_retirement annuity cohort cannot be expressed",
            f"{sc} {_short(b)} — audience predicate has neither lifecycle filter nor "
            "numeric attribute comparators (gte / lte). 退休理财 is the SINGLE most profitable "
            "fintech segment (¥800B CN market 2024). Cannot be targeted declaratively.")

    # Post-retirement (income-replacement, withdrawal phase)
    sc, b = await call(c, "POST", "/api/v1/audiences/custom/create", json_body={
        "brand_id": bid,
        "name": "Post-retirement income product cohort",
        "source": "manual",
        "predicates": [
            {"attribute": "lifecycle_stage", "eq": "post_retirement"},
            {"event": "withdrawal", "count_gte": 1, "within_days": 90},
        ],
        "predicate_logic": "AND",
    })
    if sc == 200:
        ok("post_retirement cohort accepted", "")
    elif sc in (400, 422):
        gap("P2", "post_retirement withdrawal cohort",
            f"{sc} {_short(b)} — withdrawal event predicate may not exist")


# ── Phase 12: PII Audit Trail (个保法 § 51) ──────────────────────────────
async def phase_12_pii_audit(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("12: PII Audit Trail + Right-to-Erasure (个保法 § 45 + § 47)")
    primary_uid = state["investors"][0]

    # Probe: audit log endpoint for PII access
    for path in (
        f"/api/v1/consent/audit/{primary_uid}",
        f"/api/v1/audit/user/{primary_uid}",
        f"/api/v1/audit/pii/{primary_uid}",
        f"/api/v1/audit/kyc/{primary_uid}",
        f"/api/v1/primitives/user/{primary_uid}/audit",
    ):
        sc, b = await call(c, "GET", path)
        if sc == 200 and isinstance(b, (list, dict)):
            ok("PII audit log endpoint found", f"{path}")
            state["audit_endpoint"] = path
            break
    else:
        gap("P0", "no PII access audit log",
            "5 audit paths all 404. 个保法 § 51 mandates audit trail for 敏感个人信息 access. "
            "反洗钱法 § 21 requires identity-verification record keeping ≥ 5 years. Currently "
            "impossible to satisfy regulator audit on 'who accessed 客户身份证号 at what time'.")

    # Probe: data export (个保法 § 45 right to access)
    for path in (
        f"/api/v1/users/{primary_uid}/export",
        f"/api/v1/consent/export/{primary_uid}",
        "/api/v1/consent/data/export",
    ):
        sc, b = await call(c, "POST", path,
                           json_body={"user_id": primary_uid, "format": "json", "reason": "data_subject_access"})
        if sc in (200, 202):
            ok("data export endpoint found", f"{path} → {sc}")
            state["export_endpoint"] = path
            break
    else:
        gap("P0", "no data export endpoint",
            "个保法 § 45 requires data-subject access (download all data held). Fintech "
            "amplifies — investor can demand full investment history + KYC docs in portable "
            "form. No endpoint found.")

    # Probe: right-to-erasure with legal-hold (反洗钱法 retains data ≥ 5y)
    sc, b = await call(c, "POST", "/api/v1/consent/data/delete", json_body={
        "user_id": primary_uid,
        "reason": "data_subject_request",
        "retain_legally_required_categories": ["aml_record", "kyc_record", "tax_record"],
    })
    if sc == 200:
        ok("erasure with legal-hold categories accepted", "")
    elif sc in (400, 422):
        gap("P1", "erasure ignores retain_legally_required_categories",
            f"{sc} {_short(b)} — fintech CANNOT wipe AML records even on user request "
            "(反洗钱法 mandates ≥5y retention; 税法 ≥10y). Platform must support legal-hold "
            "flag at category level.")
    elif sc == 404:
        gap("P0", "no /consent/data/delete path",
            "right-to-erasure missing entirely. 个保法 § 47 violation. Audit fail.")


# ── Phase 13: Suitability Frequency Cap (don't over-push) ────────────────
async def phase_13_frequency_cap(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("13: Frequency Cap with Suitability Priority")
    bid = state["primary_bid"]
    primary_uid = state["investors"][0]

    # Probe: frequency cap should respect 适当性 + 监管 priority axis
    sc, b = await call(c, "POST", "/api/v1/frequency-cap/check", json_body={
        "user_id": primary_uid,
        "brand_id": bid,
        "slot": "push",
        "priority": "regulatory",     # probe — 监管必发 (e.g. 强制信披)
        "category": "investor_disclosure",
    })
    if sc == 200 and isinstance(b, dict):
        if b.get("override_reason") or b.get("regulatory_override"):
            ok("regulatory frequency-cap override", "强制信披 bypasses cap")
        else:
            gap("P0", "no regulatory frequency-cap override",
                f"frequency-cap ignored priority=regulatory ({_short(b)}). 信息披露管理办法 "
                "requires fund managers to push 净值变动 / 季度报告 / 重大事项 — these "
                "MUST bypass standard frequency caps. Without override, 持牌 entities can't "
                "meet disclosure SLAs.")

    # Probe: cool-off period between purchase prompts
    sc, b = await call(c, "POST", "/api/v1/frequency-cap/configure", json_body={
        "brand_id": bid,
        "category": "investment_promotion",
        "cool_off_after_purchase_hours": 24,   # 适当性 24h reflection
    })
    if sc in (200, 201):
        ok("适当性 24h cool-off configured", "")
    elif sc == 404:
        gap("P1", "no per-category cool-off frequency rules",
            "fintech 适当性管理 ≥¥50K purchases need 24h cool-off before next prompt. "
            "Generic frequency cap doesn't differentiate by category.")


# ── Phase 14: Pixel Event + WeChat Mini-Program Fintech-aware ────────────
async def phase_14_pixel_event(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("14: Pixel Event + Fintech Event Types (WeChat MP)")
    bid = state["primary_bid"]
    primary_uid = state["investors"][0]

    sc, b = await call(c, "POST", "/api/v1/pixel/register", json_body={
        "brand_id": bid,
        "allowed_origins": [
            "https://zhixin-jinke.cn",
            "wxzhixin0123456789abcdef",   # WeChat MP appid
        ],
    })
    pid = None
    if sc == 201 and isinstance(b, dict):
        pid = b["pixel_id"]
        ok("pixel register (https + wx)", f"pixel_id={pid}")
    else:
        gap("P1", "pixel register WeChat origin", f"{sc} {_short(b)}")
        return

    # Probe: fintech event_type — investment_purchase with PII flag
    sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
        "pixel_id": pid,
        "event_type": "investment_purchase",
        "user_id": primary_uid,
        "device_fingerprint": "dev_wx_test",
        "origin": "wxzhixin0123456789abcdef",
        "url": "wxapp://zhixin/buy",
        "value_cents": 5_000_000,
        "currency": "CNY",
        "metadata": {
            "product_id": "mmf_xinxin_001",
            "product_risk_tier": "R1",
            "id_number_hash": _sha("3101011990")[:16],   # PII (hashed!)
            "bank_card_last4": "5678",
            "is_pii": True,
        },
    })
    if sc == 200:
        if isinstance(b, dict) and (b.get("pii_flagged") or b.get("compliance_warnings")):
            ok("PII in pixel event flagged", f"warnings={b.get('compliance_warnings')}")
        else:
            gap("P0", "PII in pixel event not flagged",
                "pixel accepted id_number_hash + is_pii=True without compliance flag. "
                "Fintech events flow through same pipeline as e-commerce pageviews → PII "
                "leakage to analytics warehouses. 个保法 § 28 violation; 反洗钱 audit fails. "
                "Pixel needs `event_classification=pii` path that diverts to a hardened store.")
    elif sc in (400, 422):
        gap("P1", "investment_purchase event_type unknown",
            f"{sc} {_short(b)} — pixel event_type enum lacks fintech verticals. Add: "
            "investment_purchase / redemption / kyc_completed / risk_assessment_completed / "
            "loan_application_submitted / insurance_quote_requested.")
    else:
        gap("P1", "pixel event", f"{sc} {_short(b)}")

    # Probe: webhook on suspicious transaction
    sc, b = await call(c, "POST", "/api/v1/pixel/webhooks/register", json_body={
        "pixel_id": pid,
        "event_type": "investment_purchase",
        "filter": {"value_cents_gte": 20_000_000},   # ≥¥200K 大额
        "target_url": "https://zhixin-jinke.cn/aml/hooks/large-amount",
    })
    if sc in (200, 201):
        ok("AML 大额 webhook registered", "")
    elif sc == 404:
        gap("P1", "no /pixel/webhooks/register",
            "fintech needs to forward 大额 events to in-house AML system in real time. "
            "Polling pixel.events for ≥¥200K transactions isn't a viable SLA path.")


# ── Phase 15: Module availability probe (fintech-relevant) ───────────────
async def phase_15_module_probe(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("15: Module Availability Probe (fintech-relevant)")
    bid = state["primary_bid"]
    primary_uid = state["investors"][0]

    probes = [
        ("kix_id.register", "POST", "/api/v1/kix-id/register", None),
        ("kix_id.identity_link", "POST", f"/api/v1/kix-id/{state.get('primary_kid','x')}/identity-link", None),
        ("kix_id.lookup", "POST", "/api/v1/kix-id/lookup", None),
        ("consent.policy.current", "GET", "/api/v1/consent/policy/current", None),
        ("consent.audit", "GET", f"/api/v1/consent/audit/{primary_uid}", None),
        ("consent.data.delete", "POST", "/api/v1/consent/data/delete", None),
        ("consent.data.export", "POST", "/api/v1/consent/data/export", None),
        ("user.lifecycle_stage", "POST", f"/api/v1/primitives/user/{primary_uid}/attributes/lifecycle-stage", None),
        ("propensity.predict", "POST", "/api/v1/propensity/predict", None),
        ("compliance.scan", "POST", "/api/v1/compliance/scan", None),
        ("fraud.check", "POST", "/api/v1/fraud/check", None),
        ("aml.report", "POST", "/api/v1/aml/report", None),
        ("recipes.industry.fintech", "GET", "/api/v1/recipes", {"industry": "fintech"}),
        ("master.revenue_by_brand", "GET", f"/api/v1/master/{state['master_id']}/revenue/by-brand", None),
        ("audit.pii", "GET", f"/api/v1/audit/pii/{primary_uid}", None),
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
    md.append("# 老郑 / Zheng Hao (北京金融科技 — 智信金科) — Merchant Journey Findings")
    md.append("")
    md.append(f"**Run tag**: `{RUN_TAG}` | **Runtime**: {runtime:.1f}s | "
              f"**Date**: {time.strftime('%Y-%m-%d %H:%M', time.localtime(start_ts))}")
    md.append("")
    md.append("## Scenario")
    md.append(
        "老郑 owns 「智信金科」 — a Beijing fintech operator with FOUR licensed product lines "
        "(货币基金 / 理财 / 保险 / 小额贷款), 50K active users, ¥100K/month marketing budget. "
        "Service is a multi-product cross-sell engine: get user in via R1 货币基金 → upsell "
        "R3 理财 → cross-sell 保险 → on-demand 贷款. Unique pains: **KYC PII** (身份证/银行卡/"
        "phone) under 个保法 § 28 敏感个人信息, **风险等级 R1-R5 gradient** with mandatory "
        "适当性匹配 (C1 user cannot see R5 product), **regulatory** advertising 广告法 § 25 "
        "(no 保证收益/稳赚不赔), **AML 反洗钱法** 大额可疑交易上报, **持牌分账** revenue split "
        "per license, and **lifecycle stages** (new → pre-retirement → post-retirement) driving "
        "different product mix."
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

    md.append("## Top 3 NEW gaps unique to FINTECH")
    md.append("")
    md.append(
        "1. **Suitability gating (适当性管理) is structurally absent.** Three intertwined gaps:\n"
        "   - `/propensity/predict` accepts no `product_risk_tier` parameter and does not "
        "inspect `user.risk_tolerance` — C1 grandmas can be ranked highest for R5 私募.\n"
        "   - Audience predicates don't accept attribute filters like "
        "`risk_tolerance in [C1,C2]` — cannot build 适当性 cohorts declaratively.\n"
        "   - Voucher conditions have no `user_attribute_in` or `lifecycle_stage_in` — "
        "cannot restrict who can redeem high-risk product offers.\n"
        "   适当性管理办法 § 17 PROHIBITS marketing 中高风险 products to low-tolerance "
        "investors. 2023 sanctions hit several fintechs with ¥10M+ fines for this exact "
        "category of mis-selling. Healthcare also needed risk gating (don't push to "
        "contraindicated patients); fintech raises the stakes — every product purchase, "
        "every voucher redemption, every push must pass a suitability check.\n"
        "\n"
        "2. **PII / KYC compliance is structurally absent.** Three intertwined gaps:\n"
        "   - `consent.VALID_SCOPES` does not include `pii_kyc` / `financial_data` / "
        "`audio_video_recording` (双录). Investors cannot grant separate consent for 身份证 vs "
        "marketing — 个保法 § 28 violation.\n"
        "   - User attributes accept `pii=True` and `sensitivity=high` flags but drop them "
        "silently — no per-attribute classification, no encryption differentiation. 身份证号 "
        "sits in the same Redis bucket as nickname.\n"
        "   - No `/audit/pii/{uid}` endpoint surfaces who-read-what-when. 个保法 § 51 + "
        "反洗钱法 § 21 cannot be satisfied.\n"
        "   Healthcare (老蔡) identified the parallel for PHI; fintech adds 双录 (audio+video) "
        "as a hard legal prerequisite that has no scope today. Without these, the platform "
        "cannot legally hold 客户身份证 / 银行卡 / 风险问卷 — meaning fintech merchants run a "
        "shadow KYC system, leaving the platform a thin presentation layer.\n"
        "\n"
        "3. **Regulatory advertising guardrails missing.** Three sub-gaps:\n"
        "   - `/creatives/generate` accepts '保证收益' + '稳赚不赔' headlines verbatim — both "
        "BANNED by 广告法 § 25. No banned-phrase auto-detection, no 风险提示 auto-injection.\n"
        "   - No `/compliance/scan` endpoint to lint copy against industry-specific banned "
        "phrases. Same primitive serves healthcare (no cure claims), education (no 升学率 "
        "guarantees), now fintech (no return guarantees) — one API serves all.\n"
        "   - No `master.compliance.configure` for 持牌 license + 风险提示 disclaimer auto-"
        "append. Fintech runs without a license registry today.\n"
        "   Fintech merchants compete with WeChat Pay / Ant Group ads — those have built-in "
        "compliance auto-injection. KiX cannot match this until the platform owns a banned-"
        "phrase list + disclaimer template registry.\n"
    )
    md.append("")
    md.append("## Cross-Comparison: 老蔡 (Hospital) / 老吴 (K12) / 老郑 (Fintech)")
    md.append("")
    md.append(
        "| Concern | 老蔡 Hospital | 老吴 K12 | **老郑 Fintech** |\n"
        "|---|---|---|---|\n"
        "| Primary identity model | family graph (payer/spouse/kids/elder) | parent ↔ child | **phone-verified KYC kid (实名制)** |\n"
        "| Sensitive scope needed | phi_storage (missing) | parent + minor | **pii_kyc + 双录 audio_video_recording (both missing)** |\n"
        "| Risk gradient | clinical severity | none | **R1-R5 product × C1-C5 user (no gate)** |\n"
        "| Cross-product / cross-brand | family bundle | sibling discount | **requires_holding voucher predicate (untested)** |\n"
        "| Regulator pressure | HIPAA + 个保法 + 病历档案 | 双减 | **银保监会 + 证监会 + 反洗钱法 + 广告法 § 25 + 适当性管理办法** |\n"
        "| Banned advertising phrases | no cure promise | no 升学率 guarantee | **no 保证收益/稳赚不赔/无风险/必赚** |\n"
        "| Audit log requirement | PHI access trail | minor data | **PII access + 反洗钱 5y + 双录 10y retention** |\n"
        "| Lifecycle stages | n/a | grade level | **new/active/mature/pre-retirement/post-retirement** |\n"
        "| Anti-fraud baseline | dispute claims | refund fraud | **velocity + device-binding + geo-anomaly + 大额可疑** |\n"
        "| Frequency-cap override | emergency push | none | **regulatory disclosure (强制信披)** |\n"
        "| Revenue split | doctor 70/hospital 30 | n/a | **per-license sub-brand (基金/保险/贷款 separate regulators)** |\n"
    )
    md.append("")
    md.append(
        "**Fintech is the first vertical where**: every interaction is mediated by a "
        "regulator-defined gate (适当性 + 实名 + 风险提示), the user-attribute schema must "
        "expose a risk tolerance score as a first-class dimension, the master account must "
        "expose a per-sub-brand revenue split per regulator, advertising copy must pass an "
        "industry-specific banned-phrase scan + auto-inject a disclaimer, and the audit log "
        "is a hard legal artifact, not a nice-to-have. Round-4 master/sub-brand + Round-5 "
        "lifecycle solve some axes; risk gating + regulatory copy gates + per-license split "
        "remain uncovered."
    )
    md.append("")
    md.append("## Strategic Recommendations (Top 10)")
    md.append("")
    md.append(
        "1. **[P0] Add `pii_kyc` (+ `financial_data`, `audio_video_recording`) to "
        "`consent.VALID_SCOPES`.** One-line constant change; enforces decorator returns 403 "
        "when reading PII without scope. Adds 双录 as a first-class consent artifact with "
        "recording_id + retain_years.\n"
        "\n"
        "2. **[P0] Make `pii` / `sensitivity` first-class user-attribute flags.** "
        "Persist + route to encrypted bucket + emit separate audit-log entry on access. "
        "Identical primitive to healthcare `phi=True`; reuse, don't fork.\n"
        "\n"
        "3. **[P0] Suitability gate on `/propensity/predict` and voucher conditions.** "
        "Accept `product_risk_tier` parameter; hard-zero (or return suitability_blocked) "
        "when product_risk_tier > user.risk_tolerance. Voucher conditions need "
        "`user_attribute_in: {risk_tolerance: [C3,C4,C5]}` predicate. Single platform "
        "feature unlocks 适当性管理 across every fintech endpoint.\n"
        "\n"
        "4. **[P0] `/compliance/scan` endpoint.** Generic banned-phrase + disclaimer linter, "
        "industry-parameterized. Serves fintech (保证/稳赚/无风险), healthcare (cure/diagnose), "
        "education (升学率/必上). Master account configures `disclaimer_template` per industry "
        "with `auto_inject=true` so creative-gen always appends 风险提示.\n"
        "\n"
        "5. **[P0] Master compliance + license registry.** "
        "`POST /master/{id}/compliance/configure` with regulator / license_number / expiry / "
        "advertising_restrictions. Drives auto-freeze on expiry, regulator-traceable per-"
        "license attribution, and 持牌分账.\n"
        "\n"
        "6. **[P0] Per-sub-brand revenue / P&L report.** "
        "`GET /master/{id}/revenue/by-brand` returns isolated income / spend / attribution per "
        "sub-brand. 持牌 entities (基金=证监会, 保险=银保监会, 贷款=金管局) need SEPARATE "
        "reconciliations. Same primitive serves any multi-license operator.\n"
        "\n"
        "7. **[P1] Voucher cross-product `requires_holding` predicate.** "
        "Allow voucher conditions to reference holdings in a sibling sub-brand "
        "(min_holding_cents, min_days_held). Single change unlocks the entire fintech cross-"
        "sell business model; healthcare family-package needed the same shape.\n"
        "\n"
        "8. **[P1] `/fraud/check` + `/fraud/rules/register` + `/aml/report`.** "
        "Three minimum endpoints for fintech anti-fraud + 反洗钱. Generic primitive serves "
        "retail (refund fraud), travel (chargeback), and fintech (account takeover, structuring).\n"
        "\n"
        "9. **[P1] Audience predicates: attribute filters + lifecycle filter.** "
        "Accept `{attribute, in/eq/gte/lte}` shape on user attributes (risk_tolerance, "
        "investable_assets_cny, lifecycle_stage). 退休理财 alone is ¥800B CN market; cannot "
        "be targeted today.\n"
        "\n"
        "10. **[P1] Frequency-cap `priority: regulatory` override + per-category cool-off.** "
        "强制信披 must bypass cap; 适当性 24h cool-off after ≥¥50K purchase. Healthcare needed "
        "the same shape (emergency); add `regulatory` as another priority axis.\n"
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


# ── Phase R10: Round 10 — consumer wallet, enterprise B2B accounts,
#              and compliance rollups for the fintech journey ────────────
async def phase_r10_probes(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("R10: user_wallet + enterprise accounts + compliance rollups")
    master_id = state.get("master_id")
    primary_bid = state.get("primary_bid") or next(
        iter(state.get("sub_brands", {}).values()), None
    )
    investors = state.get("investors") or []
    primary_kid = (
        state.get("primary_kid")
        or (investors[0] if investors else None)
    )

    # ── 1. POST /user-wallet/{kid}/create — consumer money-in for retail ──
    if primary_kid:
        sc, b = await call(c, "POST",
                           f"/api/v1/user-wallet/{primary_kid}/create",
                           json_body={"currency": "CNY"})
        if sc in (200, 201) and isinstance(b, dict):
            ok("user-wallet/create",
               f"balance={b.get('balance_cents')} currency={b.get('currency')}")
        else:
            gap("P0", "user-wallet/create", f"{sc} {_short(b)}")

        # ── 2. POST /user-wallet/{kid}/topup — money-in ────────────────────
        sc, b = await call(c, "POST",
                           f"/api/v1/user-wallet/{primary_kid}/topup",
                           json_body={
                               "amount_cents": 1_000_000,  # ¥10000
                               "source": "bank_transfer",
                               "reference_id": f"laozheng_topup_{RUN_TAG}",
                               "note": "fintech onboarding deposit",
                           })
        if sc == 200 and isinstance(b, dict):
            ok("user-wallet/topup ¥10000",
               f"new_balance={b.get('new_balance_cents')}c tx={b.get('tx_id')}")
        else:
            gap("P0", "user-wallet/topup consumer money in", f"{sc} {_short(b)}")

    # ── 3. POST /accounts/register — enterprise B2B treasury client ────────
    enterprise_primary = f"enterprise_lead_{RUN_TAG}"
    sc, b = await call(c, "POST", "/api/v1/accounts/register", json_body={
        "account_name": f"Acme Treasury Co {RUN_TAG}",
        "industry": "manufacturing",
        "size": "201-1000",
        "primary_contact_user_id": enterprise_primary,
        "domain": f"acme{RUN_TAG}.example.cn",
        "metadata": {"vertical": "fintech_enterprise"},
    })
    enterprise_aid: str | None = None
    if sc in (200, 201) and isinstance(b, dict) and b.get("account_id"):
        enterprise_aid = b["account_id"]
        ok("accounts/register enterprise B2B",
           f"account_id={enterprise_aid} size=201-1000")
    else:
        gap("P0", "accounts/register enterprise B2B", f"{sc} {_short(b)}")

    # ── 4. GET /master/{mid}/compliance/audit ──────────────────────────────
    if master_id:
        sc, b = await call(c, "GET",
                           f"/api/v1/master/{master_id}/compliance/audit")
        if sc == 200 and isinstance(b, dict):
            ok("master compliance/audit",
               f"keys={list(b.keys())[:6]} "
               f"events={len(b.get('events') or b.get('audit_events') or [])}")
        else:
            gap("P0", "master compliance/audit", f"{sc} {_short(b)}")

        # ── 5. GET /master/{mid}/compliance/dashboard ──────────────────────
        sc, b = await call(c, "GET",
                           f"/api/v1/master/{master_id}/compliance/dashboard")
        if sc == 200 and isinstance(b, dict):
            ok("master compliance/dashboard",
               f"status={b.get('overall_status') or b.get('status')} "
               f"brands={len(b.get('by_brand') or [])}")
        else:
            gap("P0", "master compliance/dashboard", f"{sc} {_short(b)}")

        # Bonus: consolidated rollup is the natural fintech treasury view.
        sc, b = await call(c, "GET",
                           f"/api/v1/master/{master_id}/reports/consolidated")
        if sc == 200 and isinstance(b, dict):
            ok("master reports/consolidated (fintech rollup)",
               f"brands={b.get('brand_count') or len(b.get('by_brand') or [])}")
        else:
            gap("P1", "master reports/consolidated", f"{sc} {_short(b)}")


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
                await phase_3_kix_id_kyc(c, state)
                await phase_4_consent(c, state)
                await phase_5_risk_profile(c, state)
                await phase_6_suitability(c, state)
                await phase_7_cross_product_voucher(c, state)
                await phase_8_master_tier(c, state)
                await phase_9_regulatory_creative(c, state)
                await phase_10_antifraud_aml(c, state)
                await phase_11_lifecycle_audience(c, state)
                await phase_12_pii_audit(c, state)
                await phase_13_frequency_cap(c, state)
                await phase_14_pixel_event(c, state)
                await phase_15_module_probe(c, state)
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
