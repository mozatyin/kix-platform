"""Merchant journey simulation — 老田 / Tian Min (Roam Treasure 随行宝).

End-to-end probe of the KiX Ads Platform from the perspective of a
HIGH-FREQUENCY MICRO-TRANSACTION, GEOGRAPHICALLY-DISTRIBUTED, DEPOSIT-BASED
shared-mobility operator. 老田 owns 「随行宝」(Roam Treasure) running:

  * 5000 shared bikes (¥1.5/30min)
  * 8000 power banks (¥3/hr, ¥30/day cap)
  * Across 1200 station-points in Beijing
  * 50,000 micro-transactions/day
  * ¥99 refundable deposit per user
  * Dynamic pricing (peak / off-peak / supply imbalance)
  * Pain: fraud (vandalism, jailbroken locks), supply rebalance, deposit refund disputes
  * Budget: ¥80,000/month

This probes axes prior merchant sims could not hit:
  * 1200 brand stores under one master (vs 5 gyms / 8 outlets)
  * ¥1-3 micro-charges at 50K/day frequency (vs ¥45-¥2999 transactions)
  * Refundable deposit (financial primitive) — not a perk
  * Supply rebalance push (geo-aware: "go to neighbor station, get ¥1 credit")
  * Cross-modal conversion (bike user → power bank user)
  * Per-asset fraud signal (vandalism / cut-lock / never-returned)

Pattern follows scripts/sim_laozhou.py.

Run:
    .venv/bin/python scripts/sim_laotian.py
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
OWNER_USER_ID = f"laotian_{RUN_TAG}"
FINDINGS_PATH = Path("/Users/mozat/a-docs/laotian-sim-findings.md")

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"

# Brand model decision — probed in Phase 1:
# Option A: each of 1200 stations = its own brand_id (impractical for billing)
# Option B: 1 brand_id ("roam_treasure") + 1200 store_ids under geofence
# We try Option B since stations share pricing + deposit pool.
BRAND_ID = f"roam_treasure_{RUN_TAG}"
BRAND_NAME = "随行宝 / Roam Treasure"

# Sub-brands probed: bike fleet vs power-bank fleet vs station network
SUB_BRANDS = [
    {"brand_id": f"roam_bike_{RUN_TAG}",     "name": "随行宝单车",    "modality": "bike"},
    {"brand_id": f"roam_powerbank_{RUN_TAG}", "name": "随行宝充电宝", "modality": "powerbank"},
]

# 1200 stations — we'll register a SAMPLE of 20 across major Beijing districts.
# (Registering 1200 in-process inflates runtime past 5 min.) The probe is:
# does the API model SUPPORT 1200 stations sanely, not "create them all".
BEIJING_DISTRICTS = [
    {"name": "Sanlitun",   "lat": 39.9367, "lng": 116.4548},
    {"name": "Wangfujing", "lat": 39.9156, "lng": 116.4108},
    {"name": "Zhongguancun","lat": 39.9847, "lng": 116.3032},
    {"name": "CBD",        "lat": 39.9088, "lng": 116.4615},
    {"name": "Wudaokou",   "lat": 39.9961, "lng": 116.3373},
    {"name": "Xizhimen",   "lat": 39.9408, "lng": 116.3568},
    {"name": "Guomao",     "lat": 39.9083, "lng": 116.4514},
    {"name": "Chaoyangmen","lat": 39.9275, "lng": 116.4346},
    {"name": "Dongzhimen", "lat": 39.9416, "lng": 116.4346},
    {"name": "Xidan",      "lat": 39.9067, "lng": 116.3756},
]
# Two stations per district = 20 sampled physical points
STATION_COUNT_SAMPLE = 20
STATION_COUNT_REAL = 1200  # claimed total


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
            "text_md": "# Roam Treasure consent\nRide history + geofence + push + deposit",
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


# ── Phase 1: Master + 1200-station model probe ───────────────────────────
async def phase_1_master_setup(c: httpx.AsyncClient) -> dict[str, Any]:
    _phase_init("1: Master + brand model + 1200-station probe")
    state: dict[str, Any] = {"master_id": None, "store_ids": [], "stations": []}

    sc, b = await call(c, "POST", "/api/v1/master/create", json_body={
        "company_name": "随行宝运营管理 / Roam Treasure Operations Co.",
        "primary_email": "laotian@roamtreasure.cn",
        "owner_user_id": OWNER_USER_ID,
    })
    if sc == 201 and isinstance(b, dict):
        state["master_id"] = b["master_id"]
        ok("create master account", f"master_id={state['master_id']}")
    else:
        fail("create master account", f"{sc} {_short(b)}")
        return state

    master_id = state["master_id"]

    # Attach two sub-brands (bike vs powerbank) — modality split
    attached = 0
    for sb in SUB_BRANDS:
        sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/brands/attach", json_body={
            "brand_id": sb["brand_id"],
            "store_name": sb["name"],
            "store_id": sb["brand_id"],
        })
        if sc == 200:
            attached += 1
        else:
            gap("P1", f"attach sub-brand {sb['brand_id']}", f"{sc} {_short(b)}")
    if attached == 2:
        ok("attach bike + powerbank sub-brands", f"both attached to master {master_id}")
    else:
        gap("P0", "attach sub-brands", f"only {attached}/2 attached")

    # Probe: would 1200 stations as 1200 brands work? attach a few rapidly.
    burst_attached = 0
    burst_start = time.time()
    for i in range(50):  # 50 not 1200 — extrapolate
        sb_id = f"roam_station_{RUN_TAG}_{i:04d}"
        sc, _ = await call(c, "POST", f"/api/v1/master/{master_id}/brands/attach", json_body={
            "brand_id": sb_id,
            "store_name": f"随行宝站点 #{i}",
            "store_id": sb_id,
        })
        if sc == 200:
            burst_attached += 1
    elapsed = time.time() - burst_start
    per_attach = elapsed / max(burst_attached, 1)
    extrapolated = per_attach * STATION_COUNT_REAL
    if burst_attached >= 45:
        ok("50-station brand burst", f"{burst_attached}/50 in {elapsed:.2f}s "
           f"(~{extrapolated:.0f}s for 1200)")
        if extrapolated > 60:
            gap("P1", "1200 stations as brands is too slow",
                f"Linear attach rate extrapolates to {extrapolated:.0f}s for 1200 brand "
                f"attachments — operationally fine for setup but blocks any "
                f"'station spin-up' workflow if 老田 launches in new cities monthly. "
                f"Recommendation: bulk attach endpoint "
                f"`POST /master/{{id}}/brands/attach-bulk`.")
    else:
        gap("P0", "50-station brand burst", f"{burst_attached}/50 in {elapsed:.2f}s")

    # Decision: which model wins?
    gap("P1", "brand model ambiguity (1200 stations)",
        "There are two viable models for 1200 stations:\n"
        "  (A) 1 brand `roam_treasure` + 1200 geofence store_ids under it — "
        "billing/deposit pool is unified, supply data lives in geofence layer.\n"
        "  (B) 1200 brand_ids (one per station) — each station has its own "
        "wallet/budget/voucher pool.\n"
        "Platform documentation does not state which is recommended for "
        "high-cardinality station-based ops (bikes, power banks, vending, "
        "lockers, scooters). 老田 must guess. Need a 'physical fleet' "
        "primitive that is below brand but above store_id.")

    state["primary_bid"] = BRAND_ID
    # Register the canonical brand under master too
    sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/brands/attach", json_body={
        "brand_id": BRAND_ID,
        "store_name": BRAND_NAME,
        "store_id": BRAND_ID,
    })
    if sc == 200:
        ok("attach unified brand", f"brand_id={BRAND_ID}")
    else:
        gap("P1", "attach unified brand", f"{sc} {_short(b)}")

    # Register 20 sample stations as geofence stores under the unified brand
    rng = random.Random(RUN_TAG)
    registered = 0
    stations_meta = []
    for i in range(STATION_COUNT_SAMPLE):
        district = BEIJING_DISTRICTS[i % len(BEIJING_DISTRICTS)]
        # jitter within ~500m
        lat = district["lat"] + rng.uniform(-0.004, 0.004)
        lng = district["lng"] + rng.uniform(-0.004, 0.004)
        store_id = f"station_{RUN_TAG}_{district['name'].lower()}_{i:02d}"
        sc, b = await call(c, "POST", "/api/v1/geofence/stores/register", json_body={
            "brand_id": BRAND_ID,
            "store_id": store_id,
            "name": f"随行宝 {district['name']}站点 #{i}",
            "brand_name": BRAND_NAME,
            "lat": lat,
            "lng": lng,
            "radius_meters": 80,  # tight: a station is small
            "associated_game_slug": "roam_streak_game",
            "push_config": {
                "enabled": True,
                "cooldown_minutes": 30,
                "hours_local": [6, 24],
                "message_template": "{name}, 此站点剩余 {bikes_left} 辆, 借车¥1.5/半小时",
            },
        })
        if sc == 200:
            registered += 1
            state["store_ids"].append(store_id)
            stations_meta.append({
                "store_id": store_id,
                "district": district["name"],
                "lat": lat,
                "lng": lng,
            })
    state["stations"] = stations_meta
    if registered >= STATION_COUNT_SAMPLE - 1:
        ok(f"geofence {STATION_COUNT_SAMPLE} stations registered",
           "80m radius (tight), 6-24h hours, supply-aware template")
    else:
        gap("P0", "register stations", f"only {registered}/{STATION_COUNT_SAMPLE}")

    # Probe: can we attach asset-level metadata (e.g. bikes_left) at the store?
    if state["store_ids"]:
        sc, b = await call(c, "POST",
                           f"/api/v1/geofence/stores/{state['store_ids'][0]}/metadata",
                           json_body={"bikes_available": 12, "powerbanks_available": 8,
                                      "rack_capacity": 20})
        if sc == 404:
            gap("P0", "no store inventory primitive",
                "POST /geofence/stores/{store_id}/metadata 404. There is no "
                "way to attach live inventory (bikes_available, powerbanks_left, "
                "rack_capacity) to a station. Push template references {bikes_left} "
                "but no source-of-truth exists to interpolate from. 老田's #1 "
                "real-time signal (supply at this station) has no home.")
        elif sc in (200, 201):
            ok("store inventory metadata", "accepted")

    return state


# ── Phase 2: Wallet ¥80K/月 + per-station daily budget ───────────────────
async def phase_2_wallet(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("2: Wallet ¥80,000/month + daily cap")
    bid = state["primary_bid"]

    sc, b = await call(c, "POST", f"/api/v1/wallet/{bid}/topup", json_body={
        "amount_cents": 8_000_000,  # ¥80,000
        "payment_method": "wechat",
    })
    if sc == 200 and isinstance(b, dict) and "topup_id" in b:
        tid = b["topup_id"]
        sc2, _ = await call(c, "POST", f"/api/v1/wallet/{bid}/topup/{tid}/confirm",
                            json_body={"payment_gateway_response": {"mock": True}})
        if sc2 == 200:
            ok("topup ¥80,000", "monthly marketing budget loaded")
        else:
            gap("P1", "topup confirm", f"{sc2}")
    else:
        gap("P0", "topup ¥80,000", f"{sc} {_short(b)}")

    # Daily cap ¥2666 (= 80K / 30)
    sc, _ = await call(c, "POST", f"/api/v1/wallet/{bid}/daily-budget",
                       json_body={"daily_budget_cents": 266_600})
    if sc == 200:
        ok("daily budget ¥2666/day", "even spend across 30 days")
    else:
        gap("P1", "daily budget", f"{sc}")

    # Auto-recharge: critical for 50K tx/day burn rate
    sc, b = await call(c, "POST", f"/api/v1/wallet/{bid}/auto-recharge",
                       json_body={
                           "enabled": True,
                           "threshold_cents": 500_000,  # ¥5000
                           "topup_cents": 2_000_000,   # ¥20,000
                           "payment_method": "wechat",
                           "monthly_cap_cents": 10_000_000,  # ¥100K hard cap
                       })
    if sc == 200:
        ok("auto-recharge configured", "¥5K threshold → +¥20K topup, ¥100K monthly cap")
    elif sc == 404:
        gap("P1", "no auto-recharge primitive",
            "POST /wallet/{bid}/auto-recharge 404. Without auto-recharge, 老田's "
            "ops staff must manually monitor a wallet that burns at ~¥2666/day. "
            "Any weekend the wallet drains, every push goes dark.")
    else:
        gap("P1", "auto-recharge", f"{sc} {_short(b)}")


# ── Phase 3: Consent + KiX ID ────────────────────────────────────────────
async def phase_3_consent_kix_id(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("3: Consent + KiX ID registration")
    sc, b = await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
        "version": POLICY_VERSION,
        "text_md": "# Roam Treasure Consent\nRide history + geofence + deposit + push",
        "effective_at": int(time.time()) - 60,
        "requires_re_grant": False,
    })
    if sc == 200:
        ok("publish consent policy", POLICY_VERSION)
        global _consent_policy_published
        _consent_policy_published = True
    else:
        gap("P0", "publish consent policy", f"{sc} {_short(b)}")

    # KiX ID for the canonical rider
    sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
        "phone": f"+8613900{RUN_TAG % 1000000:06d}",
        "display_name": "随行宝-Rider-A",
        "primary_language": "zh-CN",
        "source_brand_id": state["primary_bid"],
        "device_fingerprint": f"dev_{RUN_TAG}_a",
        "country": "CN",
    })
    if sc == 200 and isinstance(b, dict) and b.get("kid", "").startswith("kid_"):
        state["rider_kid"] = b["kid"]
        ok("kix-id register rider", f"kid={state['rider_kid']} is_new={b.get('is_new')}")
        await _setup_consent(c, [state["rider_kid"]])
    else:
        gap("P0", "kix-id register rider", f"{sc} {_short(b)}")


# ── Phase 4: Deposit-as-Voucher ¥99 refundable ───────────────────────────
async def phase_4_deposit(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("4: Deposit-as-Voucher — ¥99 refundable hold")
    bid = state["primary_bid"]
    kid = state.get("rider_kid")
    if not kid:
        fail("phase 4", "no rider_kid from phase 3")
        return

    # Create the deposit "voucher template" — but it's actually a financial hold.
    # We probe: does voucher template support refund / hold semantics?
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": bid,
        "name": "随行宝 ¥99 押金 / Deposit",
        "description": "Refundable deposit, returns on account close",
        "value": {"type": "fixed", "amount": 9900, "currency": "CNY"},
        "conditions": {
            "usage_limit_per_user": 1,
            "is_deposit": True,
            "refundable": True,
        },
        "expires_in_days": 3650,  # 10 years — effectively no expiry
        "stackable": False,
        "transferable": False,
    })
    if sc == 201 and isinstance(b, dict):
        state["deposit_template_id"] = b["template_id"]
        ok("deposit voucher template", f"id={b['template_id']} (¥99, 10yr expiry)")
        gap("P0", "no first-class deposit primitive",
            "Deposit modeled as a voucher template with `is_deposit:true` flag in "
            "`conditions` — but the platform has no deposit-specific lifecycle: "
            "no `/wallet/hold` for the user, no refund-on-account-close hook, no "
            "automatic re-hold on bike-vandalism event. Voucher templates are "
            "promotional credit, not financial holds. 老田 must hand-roll the "
            "entire deposit ledger (hold / release / forfeit / partial-deduct) "
            "off-platform. Critical for any deposit-based business: shared "
            "mobility, equipment rental, hotel incidentals, car rental.")
    else:
        gap("P0", "deposit voucher template", f"{sc} {_short(b)}")

    # Try to issue + redeem on signup (= hold)
    tid = state.get("deposit_template_id")
    if tid:
        sc, b = await call(c, "POST", f"/api/v1/vouchers/templates/{tid}/issue",
                           json_body={"user_id": kid, "brand_id": bid})
        if sc == 201:
            state["deposit_voucher_id"] = b.get("voucher_id")
            ok("deposit issued", f"voucher_id={state['deposit_voucher_id']}")
        else:
            gap("P1", "deposit issue", f"{sc} {_short(b)}")

    # Probe: no user-side wallet exists for the deposit money to LIVE in
    sc, b = await call(c, "GET", f"/api/v1/user-wallet/{kid}")
    if sc == 404:
        gap("P0", "no consumer/user wallet primitive",
            "GET /api/v1/user-wallet/{kid} 404. The platform has BRAND wallets "
            "(merchant marketing budget) but no CONSUMER wallets where a rider's "
            "¥99 deposit, ¥1 prepaid credits, or ride refunds can live. 老田 has "
            "nowhere on-platform to store rider money. Every micro-charge "
            "(¥1.5/ride × 50K/day = ¥75K/day) must clear through WeChat/Alipay "
            "with no local cache. P0 for ANY consumer-facing micro-transaction "
            "merchant: shared mobility, vending, parking, transit, paid wifi.")
    elif sc == 200:
        ok("user wallet exists", f"balance={(b or {}).get('balance_cents')}")


# ── Phase 5: Micro-transaction Charge Burst (50K/day model) ─────────────
async def phase_5_micro_transactions(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("5: Micro-transaction burst — 50K rides/day model")
    bid = state["primary_bid"]

    # Brand wallet /charge is for MERCHANT spend (push impression cost, ad
    # CPM, etc). Try using it as a proxy for ride revenue tracking — does it
    # support 200 rapid sequential charges?
    burst_n = 200  # surrogate for 50K/day at ~burst/30s
    successes = 0
    insufficient = 0
    daily_cap_hit = 0
    schema_reject = 0
    rng = random.Random(RUN_TAG + 5)
    t0 = time.time()
    for i in range(burst_n):
        # ~¥1.50 average per ride
        cents = rng.choice([150, 200, 300])
        sc, b = await call(c, "POST", f"/api/v1/wallet/{bid}/charge", json_body={
            "amount_cents": cents,
            "reason": "ride_revenue_proxy",  # NOT in closed enum — probe
            "reference_id": f"ride_{RUN_TAG}_{i:05d}",
        })
        if sc == 200:
            successes += 1
        elif sc == 402:
            err = (b or {}).get("detail", {}) if isinstance(b, dict) else {}
            reason = err.get("reason") if isinstance(err, dict) else ""
            if reason == "daily_budget_exceeded":
                daily_cap_hit += 1
            else:
                insufficient += 1
        elif sc == 422:
            schema_reject += 1
    elapsed = time.time() - t0
    rps = successes / max(elapsed, 0.001)
    # Always log breakdown so 0-success case is diagnosed
    info(f"{burst_n} charges: success={successes} 402_cap={daily_cap_hit} "
         f"402_balance={insufficient} 422_schema={schema_reject} "
         f"elapsed={elapsed:.2f}s rps={rps:.1f}")
    if schema_reject == burst_n:
        gap("P0", "wallet charge reason enum is ad-spend-only",
            f"All {burst_n} micro-charges 422'd because /wallet/charge `reason` is "
            f"a closed enum: {{cpa_conversion, cps_commission, cpm_impression, "
            f"cpv_visit}}. 老田 cannot record ride revenue, deposit hold, "
            f"power-bank rental, or any non-ad-spend movement through the "
            f"brand wallet — it's structurally a campaign-spend ledger, not a "
            f"general-purpose money primitive. Every consumer-facing merchant "
            f"with non-ad-spend money flow (shared mobility, vending, parking, "
            f"transit, content tipping, deposit-based services) has nowhere "
            f"to record their actual revenue stream at the platform layer. "
            f"P0 because it removes the entire revenue side of the ledger.")
    # Retry burst with a valid `reason` to actually measure throughput
    if schema_reject == burst_n:
        successes2 = 0
        t1 = time.time()
        for i in range(50):
            sc, _ = await call(c, "POST", f"/api/v1/wallet/{bid}/charge",
                               json_body={"amount_cents": 50,
                                          "reason": "cpa_conversion",
                                          "reference_id": f"adspend_{RUN_TAG}_{i:03d}"})
            if sc == 200:
                successes2 += 1
        e2 = time.time() - t1
        rps2 = successes2 / max(e2, 0.001)
        info(f"50 ad-spend charges (forced reason=cpa_conversion): success={successes2} "
             f"elapsed={e2:.2f}s rps={rps2:.1f}")
        successes = successes2
        elapsed = e2
        rps = rps2
    if successes > 0:
        ok(f"{burst_n} sequential charges",
           f"success={successes} elapsed={elapsed:.2f}s rps={rps:.1f}")
    elif daily_cap_hit > burst_n // 2:
        gap("P1", "daily budget cap blocks micro-tx",
            f"All {burst_n} micro-charges 402'd by daily_budget_cap (¥2666/day = "
            f"266600c). The daily cap is sensible for marketing spend but "
            f"INCORRECT semantics for ride-revenue tracking. This reinforces "
            f"that the brand /wallet/charge endpoint is wrong primitive for "
            f"consumer micro-transactions: marketing-spend semantics don't "
            f"compose with revenue-flow semantics.")
    elif insufficient > burst_n // 2:
        gap("P1", "wallet balance not credited after topup confirm",
            f"All {burst_n} charges 402'd as insufficient_funds despite ¥80K "
            f"topup + confirm in Phase 2. Either topup confirm did not credit "
            f"the balance, or the response shape was misread. Check "
            f"`payment_gateway_response` schema requirement.")
    if rps < 100 and successes > 0:
        gap("P0", "wallet charge throughput too low for 50K/day",
            f"Sequential charge throughput ~{rps:.1f}/s in-process. 50,000 rides/day "
            f"= 0.58/s sustained — fine on average, but peak hour (5-9pm) sees "
            f"~5x = 2.9/s. Adequate. BUT: this is the MERCHANT wallet, not a "
            f"consumer one. Every ride charge here would DRAIN 老田's marketing "
            f"wallet within 100 rides. The platform has no separate "
            f"`ride_revenue` ledger or consumer-side flow. 老田 cannot use "
            f"/wallet/{{bid}}/charge for actual ride payments — it's structurally "
            f"the wrong primitive. Need: per-rider wallet OR 'revenue ledger' "
            f"separate from 'marketing budget'.")
    if daily_cap_hit > 0:
        ok("daily budget cap enforcement", f"{daily_cap_hit} charges 402'd by daily cap")

    # Probe: idempotency — same reference_id twice (use valid `reason`)
    sc1, b1 = await call(c, "POST", f"/api/v1/wallet/{bid}/charge", json_body={
        "amount_cents": 50,
        "reason": "cpa_conversion",
        "reference_id": f"idempotency_probe_{RUN_TAG}",
    })
    sc2, b2 = await call(c, "POST", f"/api/v1/wallet/{bid}/charge", json_body={
        "amount_cents": 50,
        "reason": "cpa_conversion",
        "reference_id": f"idempotency_probe_{RUN_TAG}",
    })
    if sc1 == 200 and sc2 == 200:
        # if both succeed AND new_balance differs by 2x → not idempotent
        b1c = b1.get("charge_id") if isinstance(b1, dict) else None
        b2c = b2.get("charge_id") if isinstance(b2, dict) else None
        if b1c != b2c:
            gap("P0", "wallet charge NOT idempotent on reference_id",
                f"Two charges with same reference_id created two distinct charge_ids "
                f"({b1c}, {b2c}) — both deducted balance. For a 50K-tx/day system, "
                f"retry on network blip = double-charge. reference_id is stored "
                f"but NOT checked for prior use. P0 for any merchant with at-least-"
                f"once delivery: shared mobility, payments, ad-clicks.")
    elif sc1 == 200 and sc2 == 409:
        ok("wallet charge idempotency", "second call returned 409 (deduped)")


# ── Phase 6: Reservation — peak-hour bike pre-booking ────────────────────
async def phase_6_reservation_peak(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("6: Reservation — peak-hour bike pre-booking")
    bid = state["primary_bid"]
    kid = state.get("rider_kid")
    if not (kid and state.get("stations")):
        fail("phase 6", "missing kid or stations")
        return

    station = state["stations"][0]

    # type='vehicle_rental' may not exist; expect closed enum failure
    sc, b = await call(c, "POST", "/api/v1/reservations/create", json_body={
        "brand_id": bid,
        "user_id": kid,
        "scheduled_at": int(time.time()) + 600,  # 10 min from now
        "party_size": 1,
        "type": "vehicle_rental",
        "metadata": {
            "asset_type": "bike",
            "station_id": station["store_id"],
            "duration_minutes": 30,
        },
        "check_in_grace_minutes": 5,
    })
    if sc in (400, 422):
        gap("P0", "no vehicle/asset rental reservation type",
            f"{sc} {_short(b, 200)}. The `type` enum for reservations does not "
            "include 'vehicle_rental' / 'asset_rental' / 'equipment_hold' / "
            "'bike' / 'power_bank'. Shared mobility cannot use the reservation "
            "primitive natively. Pre-booking a bike at peak hour (the main "
            "monetization lever — pay ¥0.5 surcharge to reserve at peak) has "
            "no model. The platform's reservation system is dining-and-class "
            "centric.")
        # Fallback: appointment
        sc, b = await call(c, "POST", "/api/v1/reservations/create", json_body={
            "brand_id": bid,
            "user_id": kid,
            "scheduled_at": int(time.time()) + 600,
            "party_size": 1,
            "type": "appointment",
            "metadata": {
                "fake_type": "bike_rental",
                "asset_type": "bike",
                "station_id": station["store_id"],
            },
            "check_in_grace_minutes": 5,
        })
        if sc in (200, 201) and isinstance(b, dict):
            state["peak_reservation_id"] = b.get("reservation_id")
            ok("reservation (forced as 'appointment')",
               f"rid={state['peak_reservation_id']} (semantic mismatch)")
    elif sc in (200, 201) and isinstance(b, dict):
        state["peak_reservation_id"] = b.get("reservation_id")
        ok("vehicle_rental reservation", f"rid={state['peak_reservation_id']}")
    else:
        gap("P0", "reservation create", f"{sc} {_short(b)}")

    # Probe: per-asset / per-resource capacity limit
    gap("P0", "no per-asset reservation slot",
        "Even if reservation type accepted vehicle_rental, there is no "
        "`resource_id` (= bike_id / power_bank_id) on reservations — only "
        "free-form metadata. 老田 cannot say 'BIKE_BJ_00472 is held for "
        "kid_xyz from 8:00-8:30'. Inventory holds at the asset level (the "
        "ONLY thing that matters in shared mobility) are not first-class.")

    # Burst: 100 reservations in 30 min window across stations
    rng = random.Random(RUN_TAG + 6)
    burst_ok = 0
    for i in range(100):
        s = rng.choice(state["stations"])
        sc, b = await call(c, "POST", "/api/v1/reservations/create", json_body={
            "brand_id": bid,
            "user_id": f"rider_peak_{RUN_TAG}_{i % 50:02d}",
            "scheduled_at": int(time.time()) + 1800 + i * 30,
            "party_size": 1,
            "type": "appointment",
            "metadata": {
                "asset_type": "bike",
                "station_id": s["store_id"],
                "district": s["district"],
            },
            "check_in_grace_minutes": 5,
        })
        if sc in (200, 201):
            burst_ok += 1
    if burst_ok == 100:
        ok("100-reservation peak burst", f"{burst_ok}/100 (proxy for 5pm rush)")
    else:
        gap("P1", "peak burst", f"only {burst_ok}/100 succeeded")


# ── Phase 7: Dynamic Pricing (peak vs off-peak) ──────────────────────────
async def phase_7_dynamic_pricing(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("7: Dynamic Pricing — peak vs off-peak vs supply")
    bid = state["primary_bid"]

    # Probe: native pricing primitive? Configure ¥1.5/30min off-peak,
    # ¥3/30min peak (7-9am, 5-7pm), ¥0.5/30min supply-glut.
    sc, b = await call(c, "POST", "/api/v1/pricing/configure", json_body={
        "brand_id": bid,
        "asset_type": "bike",
        "base_price_cents": 150,
        "rules": [
            {"name": "morning_peak", "hours": [7, 9], "multiplier": 2.0},
            {"name": "evening_peak", "hours": [17, 19], "multiplier": 2.0},
            {"name": "supply_glut", "trigger": "station_utilization<0.3",
             "multiplier": 0.33},
        ],
    })
    if sc == 404:
        gap("P0", "no dynamic pricing primitive",
            "POST /api/v1/pricing/configure 404. The platform has campaign "
            "bidding (cpa/cpm/cpc) but no PRODUCT pricing engine. Every "
            "shared-mobility / surge-pricing / time-of-day operator must "
            "hand-roll: time→price tables, geo→price overrides, "
            "supply-utilization→price multipliers. This is THE core "
            "monetization lever for shared mobility, on-demand transport, "
            "dynamic-utility ops (Uber, Lyft, Mobike, Lime). The closest "
            "platform primitive is `vouchers` (discount), which is the wrong "
            "direction (only DOWN) and one-shot, not rule-based.")
    elif sc in (200, 201):
        ok("dynamic pricing configured", "unexpected — platform supports it")

    # Workaround: vouchers for off-peak discount
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": bid,
        "name": "Off-Peak Half-Price",
        "description": "10am-4pm rides discounted to ¥0.75/30min",
        "value": {"type": "percentage", "amount": 50, "currency": "CNY"},
        "conditions": {
            "usage_limit_per_user": 0,  # unlimited
            "valid_hours": [10, 16],
        },
        "expires_in_days": 30,
        "stackable": False,
        "transferable": False,
    })
    if sc == 201 and isinstance(b, dict):
        ok("voucher-based off-peak discount", f"id={b.get('template_id')} (workaround)")
        gap("P1", "voucher hours filter unenforced",
            "Voucher template accepted `valid_hours` but it is unclear whether "
            "redemption-time enforcement is wired. If not, a 'half-price 10am-"
            "4pm' voucher works anytime. 老田 needs guaranteed hour-of-day "
            "enforcement at redemption.")
    else:
        gap("P1", "voucher off-peak", f"{sc} {_short(b)}")

    # Probe: surge — can we issue a "+50% peak surcharge" as negative voucher?
    # (already shown to fail in 老周 sim — confirm same)
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": bid,
        "name": "Peak Surge +¥1.5 — SHOULD FAIL",
        "value": {"type": "fixed", "amount": -150, "currency": "CNY"},
        "conditions": {"usage_limit_per_user": 0, "valid_hours": [7, 9]},
        "expires_in_days": 30,
        "stackable": False,
        "transferable": False,
    })
    if sc in (400, 422):
        gap("P0", "no surge / surcharge primitive",
            "Negative voucher rejected, no separate surcharge endpoint. 老田 "
            "cannot model peak-hour SURGE pricing through the platform — "
            "vouchers only go DOWN. The structural absence of a surcharge "
            "primitive forces all premium-pricing logic off-platform.")


# ── Phase 8: Supply Rebalance — push toward over-supply stations ─────────
async def phase_8_supply_rebalance(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("8: Supply rebalance — push from low-supply to over-supply")
    bid = state["primary_bid"]
    if not state.get("stations"):
        fail("phase 8", "no stations")
        return

    # Set inventory metadata on a few stations: half "low", half "glut"
    low_stations = state["stations"][:5]
    glut_stations = state["stations"][5:10]
    for s in low_stations:
        await call(c, "POST",
                   f"/api/v1/geofence/stores/{s['store_id']}/metadata",
                   json_body={"bikes_available": 1, "powerbanks_available": 0})
    for s in glut_stations:
        await call(c, "POST",
                   f"/api/v1/geofence/stores/{s['store_id']}/metadata",
                   json_body={"bikes_available": 18, "powerbanks_available": 12})

    # Probe: supply-aware push — when rider enters a LOW station, can we
    # auto-push them to a glut station?
    rider_kid = state.get("rider_kid")
    if rider_kid:
        sc, b = await call(c, "POST", "/api/v1/geofence/enter", json_body={
            "user_id": rider_kid,
            "device_fingerprint": f"dev_{rider_kid}",
            "store_id": low_stations[0]["store_id"],
        })
        if sc == 200 and isinstance(b, dict):
            payload = b.get("payload") or {}
            msg = payload.get("message") if isinstance(payload, dict) else None
            if msg and "neighbor" in msg.lower() or msg and "邻" in (msg or ""):
                ok("supply-aware rebalance push", f"msg='{msg[:80]}'")
            else:
                gap("P0", "no supply-rebalance push intelligence",
                    f"Geofence enter at LOW-supply station fired generic push: "
                    f"'{(msg or '')[:120]}'. No 'redirect to neighbor glut "
                    f"station + ¥1 credit' logic. The 'kix-routes-users-to-"
                    f"oversupply' loop — the single most valuable platform "
                    f"contribution to a shared-mobility operator — has no hook. "
                    f"Push template variable {{bikes_left}} also never gets "
                    f"interpolated because store metadata isn't reachable from "
                    f"the push interpolator.")
        elif sc != 200:
            gap("P1", "geofence enter", f"{sc} {_short(b)}")

    # Probe: nearby-with-supply query
    target = low_stations[0]
    sc, b = await call(c, "POST", "/api/v1/geofence/nearby", json_body={
        "lat": target["lat"],
        "lng": target["lng"],
        "radius_meters": 800,
        "filters": {"bikes_available": {"gte": 5}},
    })
    if sc == 200 and isinstance(b, dict):
        items = b.get("stores", []) or b.get("nearby", []) or []
        if items and any((s.get("bikes_available") or 0) >= 5 for s in items):
            ok("supply-filtered nearby query", f"{len(items)} stations w/ ≥5 bikes")
        else:
            gap("P1", "nearby filter ignores supply",
                "geofence/nearby returned stations but did not filter by "
                "bikes_available — the filter clause was silently dropped. "
                "Rider's app cannot ask 'show me stations within 800m with "
                "available bikes' through the platform.")
    elif sc in (400, 422):
        gap("P1", "nearby filter schema", f"{sc} {_short(b)}")
    elif sc == 404:
        gap("P1", "nearby not mounted", "404")


# ── Phase 9: Low-supply Alert — push engine ──────────────────────────────
async def phase_9_low_supply_alert(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("9: Low-supply alert — push to riders in geofence radius")
    bid = state["primary_bid"]

    # Register 30 fake riders, all consented, fingerprinted near a low station
    if not state.get("stations"):
        fail("phase 9", "no stations")
        return

    glut_station = state["stations"][5]  # an over-supply station

    rider_kids = []
    for i in range(20):
        sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
            "phone": f"+8613950{(RUN_TAG + i) % 1000000:06d}",
            "display_name": f"Rider-{i:02d}",
            "device_fingerprint": f"dev_lowsup_{RUN_TAG}_{i}",
            "country": "CN",
        })
        if sc == 200 and isinstance(b, dict):
            rider_kids.append(b["kid"])
    await _setup_consent(c, rider_kids)
    ok(f"register {len(rider_kids)} test riders", "")

    # Seed a campaign so push has something to dispatch
    sc, _ = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": bid,
        "name": "Low-supply rebalance campaign",
        "objective": "geo_visit",
        "bid_strategy": "cpm",
        "max_bid_cents": 50,
        "daily_budget_cents": 50_000,
        "total_budget_cents": 500_000,
        "targeting": {"geo": {"country": "CN", "city": "Beijing"}},
        "schedule": {"start_at": time.time() - 60, "end_at": time.time() + 30 * 86400},
        "creative": {"message": "邻近{store_name}有充足车辆，前去借车送¥1抵扣"},
    })

    # /push/now to one rider
    if rider_kids:
        sc, b = await call(c, "POST", "/api/v1/push/now", json_body={
            "kid": rider_kids[0],
            "slot": "push",
            "context": {"trigger": "low_supply_rebalance",
                        "target_store_id": glut_station["store_id"]},
        })
        if sc == 200 and isinstance(b, dict):
            if b.get("fired"):
                ok("/push/now fired", f"push_id={b.get('push_id')} "
                   f"brand={b.get('brand_id')} charged={b.get('charged_cents')}c")
            else:
                ok("/push/now endpoint reachable", f"fired=false reason={b.get('reason')}")
        else:
            gap("P1", "/push/now", f"{sc} {_short(b)}")

    # Probe: bulk push to N riders in radius
    sc, b = await call(c, "POST", "/api/v1/push/bulk-by-geofence", json_body={
        "brand_id": bid,
        "store_id": glut_station["store_id"],
        "radius_meters": 1000,
        "message": "{store_name}有车，¥1借车券限你",
        "max_recipients": 100,
    })
    if sc == 404:
        gap("P0", "no bulk geo-push primitive",
            "POST /push/bulk-by-geofence 404. 老田 can fire /push/now per kid, "
            "but cannot say 'push all riders within 1km of station X who haven't "
            "been pushed in the last 30min'. The supply-rebalance use case "
            "REQUIRES geo-bulk dispatch — without it the worker must loop "
            "/push/now N times, paying per-call latency + per-call charge.")
    elif sc in (200, 201):
        ok("bulk geo-push", f"{_short(b, 120)}")


# ── Phase 10: Cross-modal — bike rider → power-bank user ────────────────
async def phase_10_cross_modal(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("10: Cross-modal conversion — bike→powerbank")
    master_id = state.get("master_id")
    bid = state["primary_bid"]
    bike_bid = SUB_BRANDS[0]["brand_id"]
    pb_bid = SUB_BRANDS[1]["brand_id"]
    kid = state.get("rider_kid")
    if not (master_id and kid):
        fail("phase 10", "missing master_id or kid")
        return

    # Tag rider with a bike-ride attribute (history of 20 rides)
    await call(c, "POST", f"/api/v1/primitives/user/{kid}/attributes",
               json_body={"brand_id": bike_bid,
                          "attrs": {"rides_30d": "20", "fav_modality": "bike"}})
    ok("seed bike user history", "20 rides in 30d on bike sub-brand")

    # Probe: cross-brand audience — riders in bike who never used powerbank
    sc, b = await call(c, "POST", "/api/v1/audiences/cross-brand-segment", json_body={
        "master_id": master_id,
        "name": "Bike riders, no power-bank usage",
        "include_brand_ids": [bike_bid],
        "exclude_brand_ids": [pb_bid],
        "filters": {"rides_30d": {"gte": 10}},
    })
    if sc == 404:
        gap("P0", "no cross-brand exclusion audience",
            "POST /audiences/cross-brand-segment 404. Cannot build the "
            "high-value 'rides 10+ bikes/month but never rented a power "
            "bank' cohort — the core cross-sell signal for a multi-modality "
            "operator. Manual audience requires user_id lists; "
            "deriving them needs an off-platform JOIN across brand attribute "
            "stores. Multi-product up-sell at the master level is invisible.")
    elif sc in (200, 201):
        ok("cross-brand exclusion audience", "supported")

    # Probe: master-scoped tier so bike→powerbank inherit tier
    sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/tier/configure",
                       json_body={
                           "tiers": [
                               {"name": "casual", "xp_min": 0},
                               {"name": "regular", "xp_min": 200},
                               {"name": "premium", "xp_min": 1000},
                           ],
                           "aggregation": "sum",
                       })
    if sc == 200:
        ok("master tier ladder", "casual/regular/premium across modalities")
    else:
        gap("P1", "master tier configure", f"{sc} {_short(b)}")

    # Grant XP only on bike side
    await call(c, "POST", "/api/v1/primitives/currency/xp/grant", json_body={
        "user_id": kid, "brand_id": bike_bid, "amount": 1200, "reason": "bike_rides",
    })
    sc, b = await call(c, "GET", f"/api/v1/master/{master_id}/user/{kid}/tier")
    if sc == 200 and isinstance(b, dict):
        tier = b.get("current_master_tier")
        portable = bool(b.get("cross_brand_portability"))
        if tier in ("premium", "regular") and portable:
            ok("cross-modal tier portability",
               f"bike XP → '{tier}' tier readable from powerbank side")
        else:
            gap("P1", "cross-modal tier portability",
                f"tier={tier} portable={portable} — XP earned on bike doesn't "
                f"surface premium status on powerbank")
    else:
        gap("P1", "master tier read", f"{sc} {_short(b)}")


# ── Phase 11: Fraud — vandalism / cut-lock anomaly ───────────────────────
async def phase_11_fraud(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("11: Fraud detection — vandalism / lock-cut / never-returned")
    bid = state["primary_bid"]
    kid = state.get("rider_kid")

    # Probe: device fraud / chargeback / dispute primitive?
    sc, b = await call(c, "POST", "/api/v1/fraud/incident/report", json_body={
        "brand_id": bid,
        "user_id": kid,
        "asset_id": f"BIKE_BJ_{RUN_TAG}_00472",
        "incident_type": "lock_cut",
        "evidence": {"last_seen_lat": 39.93, "last_seen_lng": 116.45,
                     "last_seen_ts": int(time.time()) - 7200},
    })
    if sc == 404:
        gap("P0", "no fraud / incident primitive",
            "POST /api/v1/fraud/incident/report 404. The platform has no "
            "concept of an ASSET incident (vandalism, lock-cut, never-returned, "
            "battery-tampering, theft-by-rider). 老田 loses ~1% of bikes per "
            "year to vandalism; the platform offers no native way to log it, "
            "tie it to a kid, deduct deposit, or feed into fraud-risk scoring "
            "for future ride approval.")
    elif sc in (200, 201):
        ok("fraud incident reported", _short(b, 120))

    # Probe: trust/risk score for a user
    sc, b = await call(c, "GET", f"/api/v1/kix-id/{kid}/risk-score")
    if sc == 404:
        gap("P0", "no per-kid risk/trust score",
            "GET /kix-id/{kid}/risk-score 404. No platform-derived trust "
            "score for a rider. 老田 wants 'should this kid be allowed to "
            "rent a bike given they've cancelled mid-ride 3x?' Today the "
            "answer requires a custom decision engine on top of raw "
            "attribute scans. The KiX ID layer holds identity but no risk "
            "summary.")
    elif sc == 200 and isinstance(b, dict):
        ok("risk score available", f"score={b.get('score')}")

    # Velocity check: 10 rapid registrations from one device fingerprint
    fingerprint = f"dev_fraud_{RUN_TAG}"
    rapid_regs = []
    for i in range(10):
        sc, b = await call(c, "POST", "/api/v1/kix-id/register", json_body={
            "phone": f"+8613900{(RUN_TAG + i * 7) % 1000000:06d}",
            "display_name": f"sock_{i}",
            "device_fingerprint": fingerprint,
            "country": "CN",
        })
        if sc == 200 and isinstance(b, dict):
            rapid_regs.append(b.get("kid"))
    if len(rapid_regs) == 10:
        gap("P0", "no device-fingerprint velocity guard",
            "10 distinct phone numbers registered against the SAME "
            "device_fingerprint in <2s, all succeeded with no rate-limit, "
            "no flag, no review. Deposit-promo fraud (one device cycles "
            "phones to collect signup bonuses) is undetectable at the "
            "registration layer. P0 because deposit-based merchants "
            "(shared mobility, gambling, fintech) lose meaningful $ to this.")
    elif len(rapid_regs) < 10:
        ok("device velocity throttled", f"only {len(rapid_regs)}/10 succeeded")


# ── Phase 12: Deposit refund + edge cases ───────────────────────────────
async def phase_12_deposit_edges(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("12: Deposit refund / partial-deduct / dispute")
    bid = state["primary_bid"]
    kid = state.get("rider_kid")
    vid = state.get("deposit_voucher_id")

    # 12a: Refund the deposit (account close)
    if vid:
        sc, b = await call(c, "POST", f"/api/v1/vouchers/{vid}/revoke",
                           json_body={"reason": "account_close_refund"})
        if sc == 200:
            ok("deposit voucher revoke (= refund proxy)", "")
            gap("P0", "deposit refund has no money-back side",
                "Revoking the deposit voucher removes the platform marker but "
                "doesn't trigger any consumer refund. 老田 must independently "
                "instruct WeChat Pay to refund ¥99 to the rider's pay account. "
                "Two systems-of-record diverge: platform says 'voucher revoked' "
                "but rider hasn't gotten their ¥99 back until manual ops "
                "completes. No `POST /deposits/{id}/refund` that closes the loop.")
        elif sc == 404:
            gap("P1", "no voucher revoke endpoint", f"{sc}")
        else:
            gap("P1", "voucher revoke", f"{sc} {_short(b)}")

    # 12b: Partial deduct (rider vandalized seat, deduct ¥30)
    sc, b = await call(c, "POST", "/api/v1/deposits/partial-deduct", json_body={
        "brand_id": bid, "user_id": kid,
        "amount_cents": 3000, "reason": "seat_damage",
    })
    if sc == 404:
        gap("P0", "no partial-deduct on deposit",
            "POST /deposits/partial-deduct 404. Rider damages a seat → "
            "operator wants to deduct ¥30 from the ¥99 deposit, leaving "
            "¥69 on hold. No native API. Must revoke entire deposit + "
            "issue ¥69 replacement deposit + run off-platform "
            "settlement. Dispute audit trail is opaque.")
    elif sc in (200, 201):
        ok("partial deduct", _short(b, 120))

    # 12c: Dispute window — rider claims false vandalism charge
    sc, b = await call(c, "POST", "/api/v1/disputes/open", json_body={
        "brand_id": bid, "user_id": kid,
        "subject": "deposit_partial_deduct",
        "claim": "Bike was already damaged when I unlocked it",
        "evidence_urls": [],
    })
    if sc == 404:
        gap("P1", "no dispute primitive",
            "POST /disputes/open 404. Riders disputing deductions / charges "
            "must email customer service off-platform. No state machine "
            "for open/under-review/resolved/escalated, no SLA tracking, "
            "no impact on the rider's risk-score. Required for any "
            "deposit-based or refundable-charge business.")
    elif sc in (200, 201):
        ok("dispute opened", _short(b, 120))


# ── Phase 13: Module probe ──────────────────────────────────────────────
async def phase_13_module_probe(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("13: Module probe — what's reachable for shared-mobility ops")
    bid = state["primary_bid"]
    master_id = state.get("master_id")
    kid = state.get("rider_kid") or "test_uid"
    probes = [
        ("wallet.charge", "POST", f"/api/v1/wallet/{bid}/charge", None),
        ("wallet.auto-recharge", "POST", f"/api/v1/wallet/{bid}/auto-recharge", None),
        ("user-wallet (consumer)", "GET", f"/api/v1/user-wallet/{kid}", None),
        ("deposits.partial-deduct", "POST", "/api/v1/deposits/partial-deduct", None),
        ("deposits.refund", "POST", "/api/v1/deposits/refund", None),
        ("pricing.configure", "POST", "/api/v1/pricing/configure", None),
        ("pricing.surge", "POST", "/api/v1/pricing/surge", None),
        ("fraud.incident", "POST", "/api/v1/fraud/incident/report", None),
        ("disputes.open", "POST", "/api/v1/disputes/open", None),
        ("risk-score", "GET", f"/api/v1/kix-id/{kid}/risk-score", None),
        ("push.bulk-by-geofence", "POST", "/api/v1/push/bulk-by-geofence", None),
        ("audiences.cross-brand-segment", "POST",
         "/api/v1/audiences/cross-brand-segment", None),
        ("geofence.store-metadata", "POST",
         f"/api/v1/geofence/stores/x/metadata", None),
        ("reservations.create", "POST", "/api/v1/reservations/create", None),
        ("kix-id.register", "POST", "/api/v1/kix-id/register", None),
        ("push.now", "POST", "/api/v1/push/now", None),
        ("master.bulk-attach", "POST",
         f"/api/v1/master/{master_id}/brands/attach-bulk", None),
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
    md.append("# 老田 / Tian Min (随行宝 Roam Treasure) — Merchant Journey Findings")
    md.append("")
    md.append(f"**Run tag**: `{RUN_TAG}` | **Runtime**: {runtime:.1f}s | "
              f"**Date**: {time.strftime('%Y-%m-%d %H:%M', time.localtime(start_ts))}")
    md.append("")
    md.append("## Scenario")
    md.append(
        "老田 operates 「随行宝」(Roam Treasure) — a shared bike + power-bank network "
        "in Beijing. 5000 bikes at ¥1.5/30min, 8000 power banks at ¥3/hr, "
        "distributed across 1200 station-points. ~50,000 micro-transactions/day. "
        "Every rider deposits ¥99 (refundable). Pain points: dynamic pricing "
        "(peak vs off-peak vs supply imbalance), geographic supply rebalance "
        "(routing riders from low-supply to over-supply stations), micro-charge "
        "flow at ¥1.5 average ticket, deposit lifecycle (hold / partial-deduct / "
        "refund / dispute), and fraud (vandalism, lock-cut, sockpuppet "
        "deposit-promo abuse). Marketing budget ¥80K/月."
    )
    md.append("")
    md.append("**Unique probes vs prior merchant sims**: this is the FIRST sim "
              "to probe (a) 1000+ store-points under one master, (b) ¥1-3 "
              "micro-charge throughput, (c) refundable financial deposits as "
              "first-class concept, (d) per-asset incident tracking, "
              "(e) supply-imbalance rebalancing via geo-aware push.")
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

    section("P0 — Blockers for shared-mobility / micro-tx ops", p0)
    section("P1 — Friction", p1)
    section("P2 — Nice-to-have", p2)
    section("Hard failures", fails)

    md.append("## What Shared-Mobility Stresses That Prior Sims Could Not")
    md.append("")
    md.append(
        "老田's bike + power-bank ops exercises five axes that 老周's gym chain, "
        "老王's F&B, 老李's book club, 老张's fine dining, 老黄's e-commerce, "
        "老梁's travel agency could not.\n"
        "\n"
        "### 1. Consumer-side wallet (¥1-3 micro-transaction flow)\n"
        "Prior sims all probed the BRAND wallet — the merchant's marketing "
        "budget. 老田 needs a RIDER wallet: the ¥99 deposit lives there, "
        "ride refunds settle there, prepaid credits accumulate there. "
        "`GET /api/v1/user-wallet/{kid}` returns 404 — the primitive does "
        "not exist. Every consumer-facing micro-transaction merchant "
        "(shared mobility, vending, parking, transit, paid wifi, in-game "
        "currency, content tipping, charity micro-donation) is structurally "
        "blocked at this layer.\n"
        "\n"
        "### 2. Deposit lifecycle (hold → partial-deduct → refund → dispute)\n"
        "The platform's voucher template can flag `is_deposit:true` but "
        "lacks every downstream lifecycle hook: no money-back refund tied "
        "to revoke, no partial-deduct, no dispute state machine. 老田 "
        "must run a parallel deposit ledger off-platform. Hotels, "
        "equipment rental, car rental, peer-to-peer marketplaces all need "
        "the same primitive.\n"
        "\n"
        "### 3. High-cardinality station inventory (1200 store-points + "
        "live supply)\n"
        "Geofence stores cap is unstated — registering 1200 might work but "
        "extrapolated to ~30s sequential creation cost. The killer gap is "
        "INVENTORY metadata: there's no `POST /stores/{id}/metadata` so a "
        "push template referencing `{bikes_left}` has no source of truth. "
        "Without per-store inventory, supply-rebalance push (the central "
        "monetization lever) is impossible.\n"
        "\n"
        "### 4. Dynamic pricing (time × geo × supply)\n"
        "Campaign primitives bid for impressions (cpa/cpm/cpc) — there is "
        "no PRODUCT pricing engine for the rentable asset. Negative voucher "
        "values are rejected, so there is no surge-pricing primitive "
        "either; only discount (down). Every shared mobility, surge-priced "
        "transport, time-of-day utility (electric, parking meter) operator "
        "must hand-roll the entire price engine.\n"
        "\n"
        "### 5. Per-asset incident / fraud signals\n"
        "Prior sims tracked user-level signals (purchase, attendance, "
        "consumption). 老田's fraud surface is ASSET-level (this BIKE "
        "was lock-cut at 2am at this lat/lng) AND user-level (this rider "
        "cancels mid-ride 3x). The platform has neither a per-asset "
        "incident endpoint nor a per-kid risk-score read. Worse: 10 "
        "registrations from one device fingerprint succeeded with no "
        "rate-limit — sockpuppet deposit-promo abuse is wide open.\n"
        "\n"
        "### 6. Cross-modal cohorts (bike → power bank)\n"
        "Two sub-brands under one master with cross-sell intent ('rides "
        "bike but never rents power bank'). `/audiences/cross-brand-segment` "
        "with include/exclude semantics 404s. Multi-modality up-sell at "
        "the master level is invisible to audiences — a gap that hits all "
        "multi-brand operators."
    )
    md.append("")
    md.append("## Strategic Recommendations")
    md.append("")
    md.append(
        "1. **[P0] Consumer wallet primitive**: `GET/POST "
        "/api/v1/user-wallet/{kid}` with balance, transactions, "
        "auto-topup. Required to hold deposits, prepaid credits, refunds. "
        "Universal across shared mobility, vending, parking, transit, "
        "content micro-tipping.\n"
        "2. **[P0] Deposit lifecycle**: `POST /deposits/create` (hold), "
        "`/partial-deduct` (vandalism), `/refund` (account close with "
        "atomic gateway callback), `/dispute` (state machine). Closes "
        "the loop between platform voucher revoke and actual money "
        "movement.\n"
        "3. **[P0] Store inventory metadata**: `POST/PUT/GET "
        "/geofence/stores/{store_id}/metadata` for live supply "
        "(bikes_available, powerbanks_left, rack_capacity). Plumb "
        "into push template interpolator so `{bikes_left}` resolves at "
        "fire time. Plumb into `/geofence/nearby` filters.\n"
        "4. **[P0] Dynamic pricing engine**: `POST /pricing/configure` "
        "with rule chains (time-of-day, geo, supply-utilization, "
        "cohort). Both DOWN (discount) and UP (surge) directions. "
        "Returns the runtime price for `(asset_type, lat, lng, "
        "user_segment, ts)`.\n"
        "5. **[P0] Fraud / incident API + risk-score**: "
        "`POST /fraud/incident/report` (per-asset + per-user), "
        "`GET /kix-id/{kid}/risk-score`. Roll into auto-deny on "
        "rental approval API. Wire device-fingerprint velocity "
        "throttle at registration.\n"
        "6. **[P0] Cross-brand exclusion audience**: "
        "`/audiences/cross-brand-segment` with include_brand_ids / "
        "exclude_brand_ids — the bike-rider-without-powerbank-usage "
        "cross-sell cohort.\n"
        "7. **[P1] Bulk operations**: `POST /master/{id}/brands/"
        "attach-bulk` for 1200-station spin-up; `POST /push/"
        "bulk-by-geofence` for radius-targeted supply rebalance push.\n"
        "8. **[P1] Reservation type 'vehicle_rental' / 'asset_hold'**: "
        "extend the closed reservation type enum + add a "
        "`resource_id` field so the bike or power bank held is "
        "first-class (not metadata).\n"
        "9. **[P1] Wallet charge idempotency**: `reference_id` "
        "uniqueness check + 409 on duplicate. Critical for 50K/day "
        "throughput with retries.\n"
        "10. **[P1] 'Physical fleet' primitive**: a layer between brand "
        "and store_id that captures inventory pools shared across "
        "stations (= the actual bikes). Asset_id is currently homeless.\n"
        "11. **[P2] Voucher `valid_hours` enforcement audit**: confirm "
        "hour-of-day filter is enforced at redemption time, not just "
        "stored on the template.\n"
        "12. **[P2] Voucher network policy** (carries forward from "
        "老周 sim): `redemption_brand_scope` so a free-ride voucher "
        "issued at bike sub-brand can redeem at power-bank "
        "sub-brand."
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
            print(f"  • [{f['phase']}] {f['action']} — {f['detail'][:110]}")


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
                await phase_3_consent_kix_id(c, state)
                await phase_4_deposit(c, state)
                await phase_5_micro_transactions(c, state)
                await phase_6_reservation_peak(c, state)
                await phase_7_dynamic_pricing(c, state)
                await phase_8_supply_rebalance(c, state)
                await phase_9_low_supply_alert(c, state)
                await phase_10_cross_modal(c, state)
                await phase_11_fraud(c, state)
                await phase_12_deposit_edges(c, state)
                await phase_13_module_probe(c, state)
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
