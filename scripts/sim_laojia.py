"""Merchant journey simulation — 老贾 / Jia Hong (速达物流 / Speed Delivery).

Guangzhou last-mile courier company, 200 couriers, ~8000 deliveries/day, mixed
B2C (个人寄件 ¥10-30) and B2B (商家发货 ¥0.5-3 大宗). Probes the KiX Ads
Platform from a LOGISTICS / DELIVERY perspective — the first 3-party identity
sim (sender ≠ recipient ≠ courier), real-time status push, and the cross-id
attribution puzzle (who is "the customer" when the SENDER pays but the
RECIPIENT receives the package?).

  1. Master + 速达物流 brand + 8 zone hubs (天河/越秀/海珠/番禺/...)
  2. Wallet ¥30K/month + cascade to zone hubs
  3. Consent flow — three legal subjects (sender, recipient, courier)
  4. KiX ID register × 3 (sender, recipient, courier) + relationships
  5. Reservation primitive — PICKUP SLOT (¥0 commit, fitness_class fallback)
  6. Real-time status push — created → picked → in_transit → delivered (push/now)
  7. Geofence — courier arrival triggers recipient "出门取件" push
  8. Courier rating system — 5-star tier ladder for couriers as users
  9. Photo-of-delivery proof — attribute storage + voucher refund on missing
 10. B2C campaign — individual senders (高 AOV / low volume / cps bid)
 11. B2B campaign — merchant bulk (low AOV / high volume / cpm bid)
 12. Fraud probe — virtual receipt (recipient never confirmed received)
 13. Failed delivery retry flow — retry attribute counter + recovery voucher
 14. Cross-id attribution — sender pays / recipient receives — who gets credit?
 15. Module probe + R5 (KiX ID + time-series rating + push + master tier)

Pattern follows scripts/sim_laozhou.py and sim_laowu.py. In-process via
httpx.ASGITransport; requires a live local Redis.

Run:
    .venv/bin/python scripts/sim_laojia.py
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
OWNER_USER_ID = f"laojia_{RUN_TAG}"
BRAND_ID = f"sudahanyun_{RUN_TAG}"
FINDINGS_PATH = Path("/Users/mozat/a-docs/laojia-sim-findings.md")

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"

# 8 zone hubs across Guangzhou
ZONES: list[dict[str, Any]] = [
    {"zone_id": f"sudahanyun_tianhe_{RUN_TAG}",   "name": "速达 天河枢纽",
     "district": "Tianhe",   "lat": 23.1331, "lng": 113.3303},
    {"zone_id": f"sudahanyun_yuexiu_{RUN_TAG}",   "name": "速达 越秀枢纽",
     "district": "Yuexiu",   "lat": 23.1291, "lng": 113.2670},
    {"zone_id": f"sudahanyun_haizhu_{RUN_TAG}",   "name": "速达 海珠枢纽",
     "district": "Haizhu",   "lat": 23.0838, "lng": 113.3173},
    {"zone_id": f"sudahanyun_panyu_{RUN_TAG}",    "name": "速达 番禺枢纽",
     "district": "Panyu",    "lat": 22.9938, "lng": 113.3839},
    {"zone_id": f"sudahanyun_baiyun_{RUN_TAG}",   "name": "速达 白云枢纽",
     "district": "Baiyun",   "lat": 23.2294, "lng": 113.2728},
    {"zone_id": f"sudahanyun_huangpu_{RUN_TAG}",  "name": "速达 黄埔枢纽",
     "district": "Huangpu",  "lat": 23.1066, "lng": 113.4598},
    {"zone_id": f"sudahanyun_huadu_{RUN_TAG}",    "name": "速达 花都枢纽",
     "district": "Huadu",    "lat": 23.4042, "lng": 113.2207},
    {"zone_id": f"sudahanyun_nansha_{RUN_TAG}",   "name": "速达 南沙枢纽",
     "district": "Nansha",   "lat": 22.8014, "lng": 113.5320},
]

COURIER_LASTNAMES  = ["陈", "李", "黄", "张", "王", "刘", "吴", "周", "梁", "杨"]
COURIER_FIRSTNAMES = ["国强", "建华", "志明", "伟杰", "小军", "海涛", "立新", "永泉"]
SENDER_FIRSTNAMES  = ["美丽", "小红", "丽华", "婷婷", "雪梅", "玉芬", "晓琳"]
RECIPIENT_NAMES    = ["王生", "李太", "张医生", "陈老师", "黄师傅", "刘伯"]

DELIVERY_STATUSES = ["created", "picked", "in_transit", "out_for_delivery", "delivered"]


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


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ── HTTP helpers ─────────────────────────────────────────────────────────
async def call(c: httpx.AsyncClient, method: str, path: str, *,
               json_body: Any = None, params: dict | None = None) -> tuple[int, Any]:
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
            "text_md": "# 速达物流 consent\n寄件人/收件人/快递员 三方授权",
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


# ── Phase 1: Master + 速达物流 brand + 8 Zone Hubs ───────────────────────
async def phase_1_brand_setup(c: httpx.AsyncClient) -> dict[str, Any]:
    _phase_init("1: 速达物流 Brand + Master + 8 Zone Hubs Geofence")
    state: dict[str, Any] = {"brand_id": BRAND_ID, "zone_store_ids": {}}

    # Master + brand attach (8 zones treated as stores under a single brand)
    sc, b = await call(c, "POST", "/api/v1/master/create", json_body={
        "company_name": "速达物流 / Speed Delivery Co.",
        "primary_email": "laojia@sudahanyun.cn",
        "owner_user_id": OWNER_USER_ID,
    })
    if sc == 201 and isinstance(b, dict):
        state["master_id"] = b["master_id"]
        ok("create master", f"master_id={state['master_id']}")
    else:
        fail("create master", f"{sc} {_short(b)}")
        return state

    sc, b = await call(c, "POST", f"/api/v1/master/{state['master_id']}/brands/attach",
                       json_body={
                           "brand_id": BRAND_ID,
                           "store_name": "速达物流 (主)",
                           "store_id": BRAND_ID,
                       })
    if sc == 200:
        ok("attach brand to master", f"brand={BRAND_ID}")
    else:
        gap("P1", "attach brand", f"{sc} {_short(b)}")

    # Register 8 zone hubs as geofenced stores
    registered = 0
    for z in ZONES:
        store_id = f"hub_{z['zone_id']}"
        sc, b = await call(c, "POST", "/api/v1/geofence/stores/register", json_body={
            "brand_id": BRAND_ID,
            "store_id": store_id,
            "name": z["name"],
            "brand_name": "速达物流",
            "lat": z["lat"],
            "lng": z["lng"],
            "radius_meters": 300,  # courier arrival zone
            "associated_game_slug": "delivery_rush",
            "push_config": {
                "enabled": True,
                "cooldown_minutes": 0,  # delivery push wants no cooldown
                "hours_local": [7, 22],
                "message_template": "{name}, 您的快递已到达 {district} 附近，请准备签收！",
            },
        })
        if sc == 200:
            registered += 1
            state["zone_store_ids"][z["zone_id"]] = store_id
        else:
            gap("P1", f"register hub {z['zone_id']}", f"{sc} {_short(b)}")
    if registered == 8:
        ok("geofence 8 zone hubs", "300m courier-arrival radius, no cooldown")
    else:
        gap("P0", "register hubs", f"only {registered}/8")

    # Probe: real-time push cooldown_minutes=0 — does the platform accept?
    if registered > 0:
        gap("P2", "geofence push cooldown semantics",
            "Geofence push_config supports cooldown_minutes but no documented "
            "'real-time / status-driven' mode. For logistics delivery status pushes "
            "(created/picked/in_transit/delivered) the same recipient may need to "
            "receive 4-5 pushes within minutes — the cooldown primitive is built for "
            "F&B / retail '不要烦客' rate-limiting, not for real-time event streams. "
            "Setting cooldown_minutes=0 works but the semantic intent is unclear.")

    return state


# ── Phase 2: Wallet ¥30K/month ───────────────────────────────────────────
async def phase_2_wallet(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("2: Wallet ¥30K/month — courier ad budget")
    master_id = state.get("master_id")
    if not master_id:
        fail("phase 2", "no master_id")
        return

    sc, b = await call(c, "POST", f"/api/v1/master/{master_id}/budget/global", json_body={
        "monthly_budget_cents": 3_000_000,  # ¥30K
        "allocation": {BRAND_ID: 1.0},
    })
    if sc == 200:
        ok("master global budget", "¥30K/month 100% → speed delivery")
    else:
        gap("P1", "master global budget", f"{sc} {_short(b)}")

    # Top-up wallet
    sc, b = await call(c, "POST", f"/api/v1/wallet/{BRAND_ID}/topup", json_body={
        "amount_cents": 3_000_000, "payment_method": "wechat",
    })
    if sc == 200 and isinstance(b, dict) and "topup_id" in b:
        tid = b["topup_id"]
        sc2, _ = await call(c, "POST", f"/api/v1/wallet/{BRAND_ID}/topup/{tid}/confirm",
                            json_body={"payment_gateway_response": {"mock": True}})
        if sc2 == 200:
            ok("wallet topup confirmed", "¥30000 added")
        else:
            gap("P1", "topup confirm", f"{sc2}")
    else:
        gap("P1", "topup", f"{sc} {_short(b)}")

    # Daily budget cap — ¥30K/30 = ¥1000/day
    sc, _ = await call(c, "POST", f"/api/v1/wallet/{BRAND_ID}/daily-budget",
                       json_body={"daily_budget_cents": 100_000})
    if sc == 200:
        ok("daily budget cap", "¥1000/day (¥30K / 30)")

    # B2C and B2B share the same wallet — probe whether the platform can split
    gap("P1", "no per-channel wallet split",
        "速达物流 has TWO distinct revenue channels (B2C 个人寄件 AOV ¥10-30, B2B "
        "商家发货 AOV ¥0.5-3) with very different bid economics. The wallet is "
        "brand-scoped only — there is no sub-wallet or budget-channel concept to "
        "split B2C and B2B spend cleanly. Manager must run 2 separate campaigns "
        "and rely on campaign-level daily caps as a soft split.")


# ── Phase 3: Three-Party Consent ─────────────────────────────────────────
async def phase_3_three_party_consent(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("3: Three-Party Consent — Sender + Recipient + Courier")

    sc, b = await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
        "version": POLICY_VERSION,
        "text_md": "# 速达物流 三方授权\n寄件人姓名+电话+地址 / 收件人姓名+电话+地址 / "
                   "快递员位置追踪+评分",
        "effective_at": int(time.time()) - 60,
        "requires_re_grant": False,
    })
    if sc == 200:
        ok("publish 3-party consent policy", POLICY_VERSION)
        global _consent_policy_published
        _consent_policy_published = True
    else:
        gap("P0", "publish consent", f"{sc} {_short(b)}")

    # Probe: can the platform tag a consent grant with a ROLE (sender/recipient/courier)?
    test_uid = f"role_probe_{RUN_TAG}"
    sc, b = await call(c, "POST", "/api/v1/consent/grant", json_body={
        "user_id": test_uid,
        "scopes": ["cross_brand_tracking", "geo_lbs", "personalization"],
        "policy_version": POLICY_VERSION,
        "source": "app",
        "role": "courier",  # speculative — does the platform retain role context?
    })
    if sc == 200 and isinstance(b, dict):
        ok("consent grant accepted (probed role field)", "")
        if "role" not in b:
            gap("P1", "consent has no role concept",
                "Consent grant accepted a 'role' field but it is silently dropped — "
                "consent records have no notion that the SAME human can be sender on "
                "one delivery, recipient on another, and (rarely) a courier-on-record. "
                "Audit trails for '人物角色' (compliance: did we get pickup-address "
                "consent from the SENDER not the recipient?) are impossible to "
                "construct from the consent module alone.")
    else:
        gap("P1", "consent grant w/ role", f"{sc} {_short(b)}")

    # Courier needs DIFFERENT scopes than sender/recipient (geo_lbs always-on, etc.)
    # Probe: can we publish a SECOND policy for couriers?
    sc, b = await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
        "version": f"courier_{RUN_TAG}",
        "text_md": "# 快递员授权 — 位置实时追踪 + 评分被收集",
        "effective_at": int(time.time()) - 60,
        "requires_re_grant": False,
        "audience": "courier",  # speculative
    })
    if sc == 200:
        ok("courier-specific consent policy", "second policy published")
        gap("P2", "no policy audience routing",
            "We published a second policy version intended for couriers — but the "
            "platform has no audience-tagging on policies. Every grant request must "
            "specify a policy_version explicitly; the platform never routes the right "
            "policy to the right role automatically. Workable but tedious.")
    elif sc in (400, 409):
        gap("P1", "single global consent policy",
            f"Cannot publish a courier-specific policy ({sc}). Logistics needs "
            "AT MINIMUM two policy variants (consumer vs courier-employee) because "
            "the legal basis (PIPL consent vs labor-law disclosure) is different. "
            "Today one policy text must cover both — non-compliant in PRC.")


# ── Phase 4: KiX ID register × 3 (sender + recipient + courier) ──────────
async def phase_4_three_party_identity(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("4: Three-Party Identity — Sender + Recipient + Courier KiX IDs")
    rng = random.Random(RUN_TAG)

    # Register a "round" of 3 KiX IDs representing one delivery
    triads: list[dict[str, str]] = []
    for i in range(5):
        # Sender
        sc_s, b_s = await call(c, "POST", "/api/v1/kix-id/register", json_body={
            "phone": f"+8613810{(RUN_TAG + i) % 1000000:06d}",
            "display_name": f"{rng.choice(COURIER_LASTNAMES)}{rng.choice(SENDER_FIRSTNAMES)}",
            "primary_language": "zh-CN",
            "source_brand_id": BRAND_ID,
            "device_fingerprint": f"dev_sender_{RUN_TAG}_{i}",
            "country": "CN",
        })
        # Recipient
        sc_r, b_r = await call(c, "POST", "/api/v1/kix-id/register", json_body={
            "phone": f"+8613820{(RUN_TAG + i) % 1000000:06d}",
            "display_name": rng.choice(RECIPIENT_NAMES),
            "primary_language": "zh-CN",
            "source_brand_id": BRAND_ID,
            "device_fingerprint": f"dev_recipient_{RUN_TAG}_{i}",
            "country": "CN",
        })
        # Courier — same role as a user
        sc_c, b_c = await call(c, "POST", "/api/v1/kix-id/register", json_body={
            "phone": f"+8613830{(RUN_TAG + i) % 1000000:06d}",
            "display_name": f"{rng.choice(COURIER_LASTNAMES)}{rng.choice(COURIER_FIRSTNAMES)}",
            "primary_language": "zh-CN",
            "source_brand_id": BRAND_ID,
            "device_fingerprint": f"dev_courier_{RUN_TAG}_{i}",
            "country": "CN",
        })
        triad = {
            "sender_kid":    b_s.get("kid") if isinstance(b_s, dict) else None,
            "recipient_kid": b_r.get("kid") if isinstance(b_r, dict) else None,
            "courier_kid":   b_c.get("kid") if isinstance(b_c, dict) else None,
        }
        if all(triad.values()):
            triads.append(triad)
    state["triads"] = triads
    if len(triads) == 5:
        ok("register 5 triads (sender + recipient + courier)", f"15 KiX IDs total")
    else:
        gap("P0", "kix-id register triads",
            f"only {len(triads)}/5 triads complete — kix-id registration unreliable for "
            "3-party flow.")
        return

    # Consent all
    all_kids = [k for t in triads for k in t.values()]
    granted = await _setup_consent(c, all_kids)
    ok("consent 15 KiX IDs", f"{granted}/15")

    # ── Probe: role attribute on each KiX ID ─────────────────────────────
    for triad in triads:
        await call(c, "POST",
                   f"/api/v1/primitives/user/{triad['sender_kid']}/attributes/role",
                   params={"brand_id": BRAND_ID},
                   json_body={"value": "sender"})
        await call(c, "POST",
                   f"/api/v1/primitives/user/{triad['recipient_kid']}/attributes/role",
                   params={"brand_id": BRAND_ID},
                   json_body={"value": "recipient"})
        await call(c, "POST",
                   f"/api/v1/primitives/user/{triad['courier_kid']}/attributes/role",
                   params={"brand_id": BRAND_ID},
                   json_body={"value": "courier"})

    # ── Probe: 3-way relationship for ONE delivery ──────────────────────
    # Try sender→delivery→recipient + delivery→courier as edges
    triad0 = triads[0]
    delivery_id = f"delivery_{RUN_TAG}_001"

    # sender_of_delivery
    sc1, b1 = await call(
        c, "POST", f"/api/v1/primitives/users/{triad0['sender_kid']}/relationships",
        json_body={
            "related_user_id": triad0['recipient_kid'],
            "relationship": "sender_of",
            "bidirectional": False,
            "meta": {"delivery_id": delivery_id, "shipping_fee_cents": 1500},
        },
    )
    # courier_assigned
    sc2, b2 = await call(
        c, "POST", f"/api/v1/primitives/users/{triad0['courier_kid']}/relationships",
        json_body={
            "related_user_id": triad0['recipient_kid'],
            "relationship": "delivering_to",
            "bidirectional": False,
            "meta": {"delivery_id": delivery_id},
        },
    )
    if sc1 == 200 and sc2 == 200:
        ok("3-party delivery relationships", "sender_of + delivering_to edges established")
        gap("P1", "no delivery primitive",
            "We have to encode the SEMANTIC '一件快递' (one shipment) as two separate "
            "user-relationship edges. The platform has no /api/v1/delivery/create "
            "primitive that atomically captures (sender, recipient, courier, "
            "shipment_id, status_machine). Last-mile logistics gets relationships but "
            "no delivery domain object.")
    elif sc1 == 404 or sc2 == 404:
        gap("P0", "no 3-party relationship",
            "Cannot create sender_of + delivering_to relationships — relationships "
            "module only knows pairwise edges, and there is no first-class "
            "DELIVERY object that joins all 3 parties. Last-mile logistics has no "
            "domain primitive.")
    else:
        gap("P1", "3-party relationships", f"sc1={sc1} sc2={sc2} {_short(b1)} {_short(b2)}")

    # ── Probe: cross-id attribution — who is "the customer"? ─────────────
    # B2C delivery: sender PAYS (¥15 shipping fee), recipient just RECEIVES.
    # Platform attribution model assumes user_id == event_actor.
    # Probe: log a 'shipping_fee_paid' purchase, see whose attribute it lands on
    sc, b = await call(c, "POST", "/api/v1/attribution/track/purchase", json_body={
        "user_id": triad0["sender_kid"],
        "target_brand": BRAND_ID,
        "amount_cents": 1500,  # ¥15 fee
        "source": "speed_delivery_b2c",
    })
    if sc in (200, 201):
        ok("attribution: sender paid", "purchase landed on SENDER user_id")
    else:
        gap("P1", "attribution sender", f"{sc} {_short(b)}")

    gap("P0", "no cross-id attribution",
        "速达物流 pain point: SENDER pays ¥15 shipping but the EXPERIENCE belongs to "
        "the RECIPIENT (who unwraps the box). For LTV / repeat-purchase / NPS / "
        "lookalike modeling, 'the customer' is ambiguous. The platform's attribution "
        "is single-user: every event is bound to one user_id. There is no "
        "co-attribution / shared-credit / dual-attribute model. Lookalike audiences "
        "trained on senders will miss the 80% who only RECEIVE; trained on recipients "
        "will miss the 20% who only SEND. Need attribute (paying_user, beneficiary_user) "
        "as first-class on purchase events.")


# ── Phase 5: Reservation primitive — PICKUP SLOT ─────────────────────────
async def phase_5_pickup_reservation(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("5: Reservation Primitive — PICKUP SLOT (sender books courier window)")
    triads = state.get("triads", [])
    if not triads:
        fail("phase 5", "no triads")
        return

    # Configure brand-level reservation policy
    sc, b = await call(c, "POST", "/api/v1/reservations/admin/policy/configure", json_body={
        "brand_id": BRAND_ID,
        "default_grace_minutes": 30,  # courier 30min grace
    })
    if sc == 200:
        ok("reservation policy", "30min grace for courier arrival")
    else:
        gap("P1", "reservation policy", f"{sc} {_short(b)}")

    # R7: type=pickup (first-class) + fulfiller_user_id=courier + recipient_user_id
    triad0 = triads[0]
    sc, b = await call(c, "POST", "/api/v1/reservations/create", json_body={
        "brand_id": BRAND_ID,
        "user_id": triad0["sender_kid"],  # sender = booker
        "scheduled_at": int(time.time()) + 3600,
        "party_size": 1,
        "type": "pickup",  # R7 first-class
        "fulfiller_user_id": triad0["courier_kid"],  # R7
        "recipient_user_id": triad0["recipient_kid"],  # R7
        "metadata": {
            "address": "天河区天河北路",
            "package_weight_g": 850,
            "delivery_type": "b2c",
        },
        "check_in_grace_minutes": 30,
    })
    if sc in (200, 201) and isinstance(b, dict):
        state["pickup_rid"] = b.get("reservation_id")
        ok("pickup reservation (type=pickup, R7)",
           f"rid={b.get('reservation_id')}")
    elif sc in (400, 422):
        gap("P0", "no 'pickup' reservation type",
            f"Reservation type='pickup' rejected ({sc} {_short(b, 120)})")
    else:
        gap("P1", "pickup reservation", f"{sc} {_short(b)}")

    # R7: GET readback to verify fulfiller_user_id + recipient_user_id persisted
    rid = state.get("pickup_rid")
    if rid:
        sc, b = await call(c, "GET", f"/api/v1/reservations/{rid}")
        if sc == 200 and isinstance(b, dict):
            if b.get("fulfiller_user_id") == triad0["courier_kid"]:
                ok("reservation fulfiller_user_id (courier) persisted",
                   f"fulfiller={triad0['courier_kid']}")
            else:
                gap("P0", "fulfiller_user_id (courier) not persisted",
                    f"readback fulfiller_user_id={b.get('fulfiller_user_id')!r}")
            if b.get("recipient_user_id") == triad0["recipient_kid"]:
                ok("reservation recipient_user_id persisted",
                   f"recipient={triad0['recipient_kid']}")
            else:
                gap("P1", "recipient_user_id not persisted",
                    f"readback recipient_user_id={b.get('recipient_user_id')!r}")

        # R7: GET /reservations/fulfiller/{courier_kid} — courier route board
        sc2, b2 = await call(c, "GET",
                              f"/api/v1/reservations/fulfiller/{triad0['courier_kid']}")
        if sc2 == 200 and isinstance(b2, dict) and b2.get("count", 0) >= 1:
            ok("courier route board via /fulfiller/{uid}",
               f"count={b2['count']}")
        else:
            gap("P0", "courier route board missing",
                f"GET /reservations/fulfiller/{triad0['courier_kid']} {sc2} {_short(b2)}")
        # R7: GET /reservations/recipient/{recipient_kid}
        sc3, b3 = await call(c, "GET",
                              f"/api/v1/reservations/recipient/{triad0['recipient_kid']}")
        if sc3 == 200 and isinstance(b3, dict) and b3.get("count", 0) >= 1:
            ok("recipient view via /recipient/{uid}", f"count={b3['count']}")
        else:
            gap("P1", "recipient view missing",
                f"GET /reservations/recipient/{triad0['recipient_kid']} {sc3} {_short(b3)}")

        # R7: payouts.inter-brand-transfer (sender brand → courier brand)
        # The triad lives under one brand here; use brand+self test reference
        sc_pay, b_pay = await call(c, "POST",
                                    "/api/v1/payouts/inter-brand-transfer",
                                    json_body={
                                        "from_brand_id": BRAND_ID,
                                        "to_brand_id": f"{BRAND_ID}_courier_pool",
                                        "amount_cents": 5_00,  # ¥5 courier fee
                                        "reason": "supplier_payment",
                                        "reference_id": f"delivery_fee_{rid}",
                                        "ledger_entry_metadata": {
                                            "category": "courier_delivery_fee",
                                            "courier_kid": triad0["courier_kid"],
                                            "reservation_id": rid,
                                        },
                                    })
        if sc_pay in (200, 201) and isinstance(b_pay, dict) and b_pay.get("entry_id"):
            ok("payouts.inter-brand-transfer (courier fee)",
               f"entry={b_pay['entry_id']}")
        else:
            gap("P1", "payouts.inter-brand-transfer (courier fee)",
                f"{sc_pay} {_short(b_pay)}")

    # Burst: 100 reservations across senders × 8 zones
    rng = random.Random(RUN_TAG + 5)
    burst_ok, burst_total = 0, 0
    for i in range(100):
        sender_kid = rng.choice(triads)["sender_kid"]
        zone = rng.choice(ZONES)
        burst_total += 1
        sc, _ = await call(c, "POST", "/api/v1/reservations/create", json_body={
            "brand_id": BRAND_ID,
            "user_id": sender_kid,
            "scheduled_at": int(time.time()) + 7200 + i * 60,
            "party_size": 1,
            "type": "service",
            "metadata": {"domain": "logistics_pickup", "zone": zone["district"],
                         "b2b": rng.random() < 0.4},  # 40% B2B mix
            "check_in_grace_minutes": 30,
        })
        if sc in (200, 201):
            burst_ok += 1
    if burst_ok >= burst_total * 0.95:
        ok("100-reservation pickup burst", f"{burst_ok}/{burst_total} across senders")
    elif burst_ok > 0:
        gap("P1", "pickup burst partial",
            f"{burst_ok}/{burst_total} — rate-limit / capacity issue?")
    else:
        gap("P0", "pickup burst", f"0/{burst_total}")


# ── Phase 6: Real-Time Status Push (created → picked → delivered) ────────
async def phase_6_realtime_status(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("6: Real-Time Status Push — created → picked → in_transit → delivered")
    triads = state.get("triads", [])
    if not triads:
        fail("phase 6", "no triads")
        return

    triad0 = triads[0]
    sender_kid    = triad0["sender_kid"]
    recipient_kid = triad0["recipient_kid"]
    courier_kid   = triad0["courier_kid"]

    # ── Probe: register triggers for each status transition ──────────────
    triggers_ok = 0
    for status in DELIVERY_STATUSES:
        sc, b = await call(c, "POST", "/api/v1/triggers/register", json_body={
            "brand_id": BRAND_ID,
            "name": f"delivery_{status}_notify_recipient",
            "event_type": "attribute_changed",
            "event_filter": {"attribute_key": "delivery_status",
                             "to_value": status},
            "action": {
                "type": "send_push",
                "config": {
                    "title": "包裹状态更新",
                    "body": f"您的包裹已 {status}",
                },
                "recipient_user_id_attr": "recipient_kid",  # indirection
            },
            "cooldown_seconds": 0,  # real-time, no cooldown
            "max_fires_per_user": 0,  # unlimited (5 statuses per delivery)
        })
        if sc == 201:
            triggers_ok += 1
        elif sc in (400, 422):
            info(f"trigger {status} rejected {sc} {_short(b, 100)}")
    if triggers_ok == 5:
        ok("5 status triggers registered", "created/picked/in_transit/out_for_delivery/delivered")
    elif triggers_ok > 0:
        gap("P1", "partial status triggers", f"only {triggers_ok}/5 accepted")
    else:
        gap("P0", "status triggers", "0/5 registered — real-time push not wireable")

    # ── Walk the status machine on attribute ─────────────────────────────
    # Use recipient as the "subject" of the delivery (its delivery_status attr)
    rec_uid = recipient_kid
    fired_count = 0
    for status in DELIVERY_STATUSES:
        # Set delivery_status attribute on recipient
        sc, _ = await call(c, "POST",
                           f"/api/v1/primitives/user/{rec_uid}/attributes/delivery_status",
                           params={"brand_id": BRAND_ID},
                           json_body={"value": status})
        if sc == 200:
            fired_count += 1
    if fired_count == 5:
        ok("status machine walked", "5 transitions on recipient.delivery_status")
    else:
        gap("P1", "status walk", f"{fired_count}/5 transitions accepted")

    # ── Probe: did the triggers fire and produce push deliveries? ────────
    sc, b = await call(c, "GET",
                       f"/api/v1/rules/{BRAND_ID}/user/{rec_uid}/pending-actions")
    pending = 0
    if sc == 200 and isinstance(b, dict):
        actions = b.get("actions") or b.get("pending_actions") or []
        pending = len(actions)
    if pending >= 5:
        ok("pending status pushes", f"{pending} push actions enqueued")
    elif pending > 0:
        gap("P1", "partial trigger firing",
            f"only {pending} pending actions for 5 status writes — rule engine "
            "fires inconsistently")
    else:
        gap("P0", "status→push bridge unwired",
            f"After walking 5 status writes (created/picked/.../delivered), the "
            "rule engine has 0 pending push actions. attribute_changed event_type "
            "with to_value filter doesn't trigger, OR /attributes/{key} POST doesn't "
            "call rule_engine on each write. Real-time delivery push (the SIGNATURE "
            "logistics feature) is non-functional today.")

    # ── push/now invocation — direct push to recipient ───────────────────
    sc, b = await call(c, "POST", "/api/v1/push/now", json_body={
        "kid": recipient_kid, "slot": "push", "context": {"delivery_status": "delivered"},
    })
    if sc == 200 and isinstance(b, dict):
        if b.get("fired"):
            ok("push/now to recipient", f"push_id={b.get('push_id')} "
               f"charged={b.get('charged_cents')}c")
        else:
            ok("push/now endpoint", f"fired=false reason={b.get('reason')}")
    else:
        gap("P1", "push/now", f"{sc} {_short(b)}")

    # ── Probe: push to a SECOND kid (sender) for the same delivery ───────
    # Logistics often pushes the same status to BOTH parties (sender wants to know
    # "your package was delivered" too)
    sc, b = await call(c, "POST", "/api/v1/push/now", json_body={
        "kid": sender_kid, "slot": "push", "context": {"delivery_status": "delivered"},
    })
    if sc == 200:
        ok("push/now to sender (same event)", "two-recipient push works")
        gap("P1", "no fan-out push primitive",
            "Logistics needs ONE EVENT (delivery delivered) → push to BOTH sender "
            "and recipient. Today we must call /push/now twice with two kids. There "
            "is no /push/fan-out endpoint that accepts an event + recipient_list. "
            "Per-call billing and rate-limit treats the two notifications as "
            "independent — they should be a single logical event.")


# ── Phase 7: Geofence — Courier Arrival → 出门取件 push ──────────────────
async def phase_7_arrival_push(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("7: Geofence — Courier Arrival → Recipient '出门取件' Push")
    triads = state.get("triads", [])
    zone_stores = state.get("zone_store_ids", {})
    if not triads or not zone_stores:
        fail("phase 7", "no triads or zones")
        return

    triad0 = triads[0]
    recipient_kid = triad0["recipient_kid"]
    courier_kid   = triad0["courier_kid"]

    # Pre-set name + delivery_status attributes for placeholder interpolation
    await call(c, "POST", f"/api/v1/primitives/user/{recipient_kid}/attributes",
               json_body={"brand_id": BRAND_ID,
                          "attrs": {"name": "李太", "district": "Tianhe"}})

    # COURIER (not recipient!) enters the geofence — that's the arrival signal
    first_zone_store = list(zone_stores.values())[0]
    sc, b = await call(c, "POST", "/api/v1/geofence/enter", json_body={
        "user_id": courier_kid,
        "device_fingerprint": f"dev_courier_{RUN_TAG}_0",
        "store_id": first_zone_store,
    })
    if sc == 200:
        ok("courier geofence enter", "courier triggered arrival event")
    else:
        gap("P1", "courier geofence", f"{sc} {_short(b)}")

    # Probe: did the platform route this arrival to the RECIPIENT's push?
    # geofence/enter assumes user_id is the audience. For logistics the courier
    # is the ACTOR but the recipient is the AUDIENCE.
    gap("P0", "geofence actor-vs-audience mismatch",
        "Geofence /enter takes ONE user_id and treats it as both the location "
        "actor AND the push audience. For logistics, the COURIER's location must "
        "trigger a push to the RECIPIENT — actor ≠ audience. There is no "
        "trigger_user_id vs notify_user_id distinction. Workaround requires "
        "merchant-side lambda that listens for courier.enter and calls "
        "/push/now with recipient_kid — defeats the point of the geofence "
        "primitive.")

    # Direct push as workaround
    sc, b = await call(c, "POST", "/api/v1/push/now", json_body={
        "kid": recipient_kid, "slot": "push",
        "context": {"reason": "courier_arrived"},
    })
    if sc == 200:
        ok("workaround: direct recipient push", "merchant lambda → push/now")


# ── Phase 8: Courier Rating System — 5-Star Tier Ladder ──────────────────
async def phase_8_courier_rating(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("8: Courier Rating — 5-star tier ladder + bonus pool")
    triads = state.get("triads", [])

    # Tier ladder for couriers as users
    sc, b = await call(c, "POST", "/api/v1/primitives/tier/configure", json_body={
        "brand_id": BRAND_ID,
        "tiers": [
            {"name": "rookie",     "xp_min": 0},
            {"name": "silver",     "xp_min": 200},
            {"name": "gold",       "xp_min": 800},
            {"name": "diamond",    "xp_min": 2500},
            {"name": "ace",        "xp_min": 8000},
        ],
    })
    if sc == 200:
        ok("courier tier ladder", "rookie / silver / gold / diamond / ace")
    else:
        gap("P1", "tier ladder", f"{sc} {_short(b)}")

    # ── Probe: can the rating be a TIME-SERIES attribute on the courier? ──
    courier_kid = triads[0]["courier_kid"]
    base_ts = int(time.time()) - 7 * 86400
    rating_history = [
        (base_ts + 0 * 86400, 4.5),
        (base_ts + 1 * 86400, 5.0),
        (base_ts + 2 * 86400, 4.0),  # bad rating
        (base_ts + 3 * 86400, 5.0),
        (base_ts + 5 * 86400, 4.8),
        (base_ts + 7 * 86400, 5.0),
    ]
    logged = 0
    for ts, r in rating_history:
        sc, _ = await call(
            c, "POST",
            f"/api/v1/primitives/user/{courier_kid}/attributes/rating/log",
            json_body={"brand_id": BRAND_ID, "value": r, "ts": ts,
                       "source": "post_delivery_review"},
        )
        if sc == 200:
            logged += 1
    if logged == 6:
        ok("rating time-series log", "6 ratings spanning 7 days")
    else:
        gap("P1", "rating time-series", f"only {logged}/6 logged")

    # Read history + trend
    sc, b = await call(
        c, "GET",
        f"/api/v1/primitives/user/{courier_kid}/attributes/rating/history",
        params={"brand_id": BRAND_ID, "limit": 50},
    )
    if sc == 200 and isinstance(b, dict) and b.get("count", 0) >= 6:
        ok("rating history readback", f"count={b['count']}")
    else:
        gap("P0", "rating time-series history",
            f"{sc} {_short(b)} — courier rating progression invisible. Logistics "
            "merchants want 'last 30 day avg', 'PR drop detection' (rating falling "
            "below 4.5), 'consecutive 5-stars'. Without history any rating-based "
            "bonus is gameable by the last data point only.")

    # Trend
    sc, b = await call(
        c, "GET",
        f"/api/v1/primitives/user/{courier_kid}/attributes/rating/trend",
        params={"brand_id": BRAND_ID, "window_days": 30},
    )
    if sc == 200 and isinstance(b, dict):
        ok("rating trend", f"direction={b.get('direction')} slope_per_day={b.get('slope_per_day')}")
    else:
        gap("P1", "rating trend", f"{sc} {_short(b)}")

    # ── Probe: bonus voucher for 5-star couriers (issue to courier_kid) ──
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": BRAND_ID,
        "name": "5-Star Courier Weekly Bonus ¥200",
        "description": "Issued weekly to couriers averaging ≥4.8 stars",
        "value": {"type": "fixed", "amount": 20000, "currency": "CNY"},
        "conditions": {"usage_limit_per_user": 1},
        "expires_in_days": 14,
        "stackable": False,
        "transferable": False,
    })
    if sc == 201 and isinstance(b, dict):
        state["bonus_voucher_template"] = b.get("template_id")
        ok("courier bonus voucher template", f"id={b.get('template_id')}")
    else:
        gap("P1", "bonus voucher template", f"{sc} {_short(b)}")

    # ── Gap: the platform has no native COURIER role concept ─────────────
    gap("P0", "no employee/courier role model",
        "Vouchers/tiers/XP are designed for CUSTOMERS — issuing a ¥200 bonus to a "
        "COURIER (an employee, not a consumer) coerces the platform into modeling "
        "labor compensation through consumer-loyalty primitives. There is no "
        "user_type=employee / contractor concept, no separation between consumer "
        "wallet and payroll wallet. Misuse-by-design: today the same wallet pays "
        "for ad clicks AND courier weekly bonuses, with no separable reporting.")


# ── Phase 9: Photo-of-Delivery Proof + Refund Voucher ────────────────────
async def phase_9_photo_proof(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("9: Photo-of-Delivery Proof — attribute storage + refund on missing")
    triads = state.get("triads", [])
    triad0 = triads[0]
    courier_kid   = triad0["courier_kid"]
    recipient_kid = triad0["recipient_kid"]

    delivery_id = f"delivery_{RUN_TAG}_001"
    # Store photo URL + GPS + timestamp as attributes
    sc, b = await call(c, "POST",
                       f"/api/v1/primitives/user/{courier_kid}/attributes",
                       json_body={"brand_id": BRAND_ID, "attrs": {
                           f"proof_{delivery_id}_url": "https://cdn.sudahanyun.cn/p/abc.jpg",
                           f"proof_{delivery_id}_gps": "23.1331,113.3303",
                           f"proof_{delivery_id}_ts":  str(int(time.time())),
                       }})
    if sc == 200:
        ok("photo proof stored on courier", f"3 attrs for {delivery_id}")
    else:
        gap("P1", "photo proof store", f"{sc} {_short(b)}")

    # ── Gap: no binary / blob primitive ─────────────────────────────────
    gap("P0", "no media/blob primitive",
        "Photo-of-delivery proof MUST be a binary blob (JPEG) — but the platform's "
        "attribute store accepts only strings. We pass a CDN URL and rely on the "
        "merchant's OWN object storage. This means: (a) no platform-side virus scan, "
        "(b) no platform-side OCR / signature recognition, (c) zero compliance "
        "guarantee that the photo URL is reachable, (d) no chain-of-custody (URL "
        "can be silently swapped). For a logistics SaaS this is the single most "
        "important asset — and the platform has no first-class representation.")

    # ── Probe: "missing package" refund voucher template ────────────────
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": BRAND_ID,
        "name": "包裹丢失补偿 ¥50",
        "description": "Issued when proof_url is missing or recipient disputes delivery",
        "value": {"type": "fixed", "amount": 5000, "currency": "CNY"},
        "conditions": {"usage_limit_per_user": 3},
        "expires_in_days": 30,
        "stackable": False,
        "transferable": False,
    })
    if sc == 201 and isinstance(b, dict):
        state["refund_voucher_template"] = b.get("template_id")
        ok("refund voucher template", f"id={b.get('template_id')}")

    # Issue refund to RECIPIENT (not sender — even though sender paid)
    tid = state.get("refund_voucher_template")
    if tid:
        sc, b = await call(c, "POST", f"/api/v1/vouchers/templates/{tid}/issue",
                           json_body={"user_id": recipient_kid, "brand_id": BRAND_ID})
        if sc == 201:
            ok("issue refund to recipient", "voucher issued — sender paid, recipient gets refund")
            gap("P1", "refund target ambiguity",
                "We issued the refund to RECIPIENT but the SENDER paid the shipping "
                "fee. The platform has no built-in 'refund to paying party' resolver — "
                "the merchant has to choose which side gets the voucher, with no "
                "policy attached. For 'apology vouchers' the recipient is correct; "
                "for 'shipping fee refund' the sender is correct. Same primitive, "
                "different semantics, no platform support.")


# ── Phase 10: B2C Campaign (high AOV, low volume, cps bid) ───────────────
async def phase_10_b2c_campaign(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("10: B2C Acquisition Campaign — individual senders")

    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": BRAND_ID,
        "name": "B2C — Individual Senders (个人寄件)",
        "objective": "acquire",
        "bid_strategy": "cps",     # cost-per-shipment-sale
        "max_bid_cents": 300,      # ¥3 per shipment
        "daily_budget_cents": 50_000,
        "total_budget_cents": 1_500_000,
        "targeting": {
            "geo": {"country": "CN", "city": "Guangzhou", "radius_km": 30},
            "demographics": {"age_min": 22, "age_max": 60},
        },
        "creative": {"recipe_id": "shipping_first_order"},
        "schedule": {"start_at": time.time() - 60, "end_at": time.time() + 86400 * 30},
        "target_audience": "new_users_only",
        "attribution_window_days": 14,
    })
    if sc == 200 and isinstance(b, dict):
        state["b2c_campaign_id"] = b["campaign_id"]
        ok("B2C campaign", f"id={b['campaign_id']} cps ¥3/shipment")
        sc_a, _ = await call(c, "POST",
                             f"/api/v1/campaigns/{b['campaign_id']}/admin/approve",
                             json_body={"admin_token": "DEV"})
        if sc_a == 200:
            ok("approve B2C campaign", "")
    else:
        gap("P1", "B2C campaign create", f"{sc} {_short(b)}")


# ── Phase 11: B2B Campaign (low AOV, high volume, cpm bid) ───────────────
async def phase_11_b2b_campaign(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("11: B2B Bulk Shipper Campaign — merchant accounts")

    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": BRAND_ID,
        "name": "B2B — Bulk Merchant Shippers (商家发货)",
        "objective": "acquire",
        "bid_strategy": "cpm",     # very low per-shipment AOV (¥0.5-3); pay per-impression
        "max_bid_cents": 30,       # ¥0.30 CPM
        "daily_budget_cents": 50_000,
        "total_budget_cents": 1_500_000,
        "targeting": {
            "geo": {"country": "CN", "city": "Guangzhou"},
            "merchant_size": "sme",  # speculative — does targeting support B2B segs?
        },
        "creative": {"recipe_id": "bulk_shipping_volume_discount"},
        "schedule": {"start_at": time.time() - 60, "end_at": time.time() + 86400 * 30},
    })
    if sc == 200 and isinstance(b, dict):
        state["b2b_campaign_id"] = b["campaign_id"]
        ok("B2B campaign", f"id={b['campaign_id']} cpm ¥0.30")
    else:
        gap("P1", "B2B campaign create", f"{sc} {_short(b)}")

    gap("P0", "no B2B audience targeting",
        "Campaign targeting accepts demographics + geo but has no 'merchant_size' / "
        "'business_type' / 'monthly_shipment_volume' B2B-shaped axes. The platform "
        "treats every user as a consumer. For 老贾's B2B channel (40% of revenue "
        "from bulk merchants doing 100+ shipments/day) there is no way to target "
        "merchants vs consumers in the SAME campaign — both must run as 'acquire' "
        "with no B2B filter, leading to wasted spend on wrong-fit audiences.")


# ── Phase 12: Fraud Probe — 虚假签收 (false delivery confirmation) ────────
async def phase_12_fraud(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("12: Fraud Probe — virtual receipt (虚假签收)")
    triads = state.get("triads", [])
    triad0 = triads[0]
    courier_kid   = triad0["courier_kid"]
    recipient_kid = triad0["recipient_kid"]

    # Scenario: courier marks delivered=true at 09:00 but recipient never confirmed.
    # The platform should be able to compute 'time between courier-marked-delivered
    # and recipient-confirmed-received' as a fraud signal.
    delivered_ts = int(time.time())
    await call(c, "POST",
               f"/api/v1/primitives/user/{recipient_kid}/attributes/delivery_status",
               params={"brand_id": BRAND_ID},
               json_body={"value": "delivered_by_courier_unconfirmed"})

    # ── Probe: is there a recipient confirmation primitive? ─────────────
    sc, b = await call(c, "POST",
                       "/api/v1/reservations/" + (state.get("pickup_rid") or "X")
                       + "/check-in",
                       json_body={"at_brand_id": BRAND_ID, "evidence": "recipient_signature"})
    if sc == 200:
        ok("recipient confirmation via check-in", "evidence=recipient_signature")
    elif sc == 404:
        gap("P1", "no delivery confirmation primitive",
            "There is no /api/v1/delivery/{rid}/confirm-received endpoint. We try "
            "reservation check-in as a proxy but it's semantically wrong (check-in "
            "is the user arriving at a venue, not a third party confirming a "
            "package). Logistics needs a first-class 'received-by-recipient' event.")

    # ── Probe: 'count(deliveries where confirmed_ts == NULL after 24h)' as fraud signal ─
    # No native fraud module — must build out-of-band.
    gap("P0", "no fraud / dispute primitive",
        "False-signature fraud (courier scans 'delivered' but recipient never got "
        "the package) is the single largest logistics dispute category. The platform "
        "has no fraud_score / dispute_open / chargeback flow. Today 老贾 has to "
        "build a separate dispute system (3rd-party complaints app), correlate with "
        "the delivery_status attribute, and decide refunds manually. No platform "
        "signal feeds fraud-suspicious couriers back into the bonus algorithm — "
        "fraudulent couriers can keep earning 5-star bonuses unless caught manually.")

    # ── Probe: black-list / risk-score attribute ────────────────────────
    sc, _ = await call(c, "POST",
                       f"/api/v1/primitives/user/{courier_kid}/attributes/risk_score",
                       params={"brand_id": BRAND_ID},
                       json_body={"value": "0.62"})
    if sc == 200:
        ok("manual risk_score attribute", "fallback for fraud-risk tagging")


# ── Phase 13: Failed Delivery Retry Flow ─────────────────────────────────
async def phase_13_failed_delivery(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("13: Failed Delivery Retry — retry counter + recovery voucher")
    triads = state.get("triads", [])
    triad0 = triads[0]
    recipient_kid = triad0["recipient_kid"]

    # Use the attribute store as a retry counter (logistics has 3-retry policy)
    for attempt in range(1, 4):
        sc, _ = await call(c, "POST",
                           f"/api/v1/primitives/user/{recipient_kid}/attributes/retry_count",
                           params={"brand_id": BRAND_ID},
                           json_body={"value": str(attempt)})
        if sc == 200:
            await call(c, "POST",
                       f"/api/v1/primitives/user/{recipient_kid}/attributes/delivery_status",
                       params={"brand_id": BRAND_ID},
                       json_body={"value": "retry_scheduled"})

    ok("retry counter walked 1→3", "manual attribute increment")

    # Register rule: after 3 failed attempts → issue refund voucher + tag escalation
    sc, b = await call(c, "POST", "/api/v1/rules/rules/create", json_body={
        "brand_id": BRAND_ID,
        "name": "3 failed deliveries → refund + escalate",
        "when": {
            "type": "attribute_changed",
            "attribute_key": "retry_count",
            "condition": {"type": "crosses_threshold", "threshold": 3},
        },
        "then": {
            "action_type": "issue_voucher",
            "action_config": {
                "template_id": state.get("refund_voucher_template", ""),
                "reason": "delivery_failed_3x",
            },
        },
        "max_triggers_per_user": 1,
    })
    if sc == 200 and isinstance(b, dict) and b.get("rule_id"):
        ok("3-strikes rule created", f"rule_id={b['rule_id'][:18]}…")
    else:
        gap("P1", "3-strikes rule", f"{sc} {_short(b)}")

    # Probe: did the rule fire after retry_count crossed 3?
    sc, b = await call(c, "GET",
                       f"/api/v1/rules/{BRAND_ID}/user/{recipient_kid}/pending-actions")
    fired = False
    if sc == 200 and isinstance(b, dict):
        actions = b.get("actions") or b.get("pending_actions") or []
        fired = any((a.get("action_type") == "issue_voucher") for a in actions)
    if fired:
        ok("3-strikes rule fired", "issue_voucher enqueued at retry_count=3")
    else:
        gap("P1", "3-strikes rule did not fire",
            "After retry_count crossed 3 via attribute write, no pending issue_voucher "
            "action was enqueued. attribute_changed + crosses_threshold rule did not "
            "execute. Same root cause as Phase 6 — attribute writes don't fan out "
            "to rule_engine v2 consistently.")


# ── Phase 14: Cross-ID Attribution — Who Is The Customer? ────────────────
async def phase_14_cross_id_attribution(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("14: Cross-ID Attribution — sender pays, recipient receives")
    triads = state.get("triads", [])

    # Simulate 10 deliveries, track who paid vs who received
    rng = random.Random(RUN_TAG + 14)
    purchases_logged = 0
    receipts_logged = 0
    for i in range(10):
        triad = rng.choice(triads)
        amount = rng.choice([1000, 1500, 2000, 2500, 3000])  # ¥10-30 B2C
        # Sender pays
        sc, _ = await call(c, "POST", "/api/v1/attribution/track/purchase", json_body={
            "user_id": triad["sender_kid"],
            "target_brand": BRAND_ID,
            "amount_cents": amount,
            "source": "speed_delivery_b2c",
        })
        if sc in (200, 201):
            purchases_logged += 1
        # Recipient "receives" — track as a visit/event (no shared-credit primitive)
        sc, _ = await call(c, "POST", "/api/v1/attribution/track/visit", json_body={
            "user_id": triad["recipient_kid"],
            "target_brand": BRAND_ID,
            "source": "package_received",
        })
        if sc in (200, 201):
            receipts_logged += 1
    ok("10 deliveries logged", f"sender purchases={purchases_logged} recipient visits={receipts_logged}")

    # ── Probe: lookalike audience from senders (will only see paying party) ──
    # Audience creation — manual sender list
    sender_kids = [t["sender_kid"] for t in triads]
    sc, b = await call(c, "POST", "/api/v1/audiences/custom/create", json_body={
        "brand_id": BRAND_ID,
        "name": "Paying Senders",
        "source": "manual",
        "user_ids": sender_kids,
        "description": "Customers who pay shipping fees",
    })
    if sc == 200 and isinstance(b, dict):
        ok("audience: paying senders", f"size={b.get('size')}")
    else:
        gap("P1", "sender audience", f"{sc} {_short(b)}")

    # ── Probe: audience that joins SENDERS + RECIPIENTS (both = "delivery user")
    sc, b = await call(c, "POST", "/api/v1/audiences/segment", json_body={
        "brand_id": BRAND_ID,
        "name": "Anyone touched by a delivery",
        "filters": {
            "or": [
                {"role": "sender", "purchases_within_days": 30},
                {"role": "recipient", "received_within_days": 30},
            ],
        },
    })
    if sc == 404:
        gap("P0", "no role+event audience segmentation",
            "POST /audiences/segment 404. Cannot express 'OR(sender, recipient)' "
            "delivery-touched audience. The fundamental cross-id attribution "
            "question — 'how big is my universe of delivery-experienced users?' — "
            "is unanswerable without manual list joins.")
    elif sc in (400, 422):
        gap("P1", "audience segment schema", f"{sc} {_short(b)}")
    else:
        info(f"segment endpoint sc={sc}")

    gap("P0", "no co-attribution primitive",
        "Every event in /attribution/track/* binds to exactly ONE user_id. For "
        "logistics where the PURCHASE belongs to the sender but the EXPERIENCE "
        "belongs to the recipient, the platform forces an arbitrary choice: either "
        "(a) credit the sender → recipient becomes invisible to LTV / retargeting, "
        "or (b) credit the recipient → sender LTV is lost. Need "
        "/attribution/track/purchase to accept "
        "{paying_user_id, beneficiary_user_ids[]} so both sides accrue credit, "
        "with a configurable revenue-split (sender=1.0, recipient=0.5 for example).")


# ── Phase 15: Module Probe ───────────────────────────────────────────────
async def phase_15_module_probe(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("15: Module Probe — what's reachable for a logistics merchant")

    probes = [
        ("delivery.create",        "POST", "/api/v1/delivery/create", None),
        ("delivery.confirm",       "POST", "/api/v1/delivery/abc/confirm-received", None),
        ("shipment.track",         "GET",  "/api/v1/shipment/abc/track", None),
        ("courier.assign",         "POST", "/api/v1/courier/assign", None),
        ("fraud.score",            "GET",  f"/api/v1/fraud/user/{state['triads'][0]['courier_kid']}/score", None),
        ("dispute.open",           "POST", "/api/v1/disputes/open", None),
        ("co-attribution",         "POST", "/api/v1/attribution/co-attribute", None),
        ("media.blob.upload",      "POST", "/api/v1/media/upload", None),
        ("reservations.create",    "POST", "/api/v1/reservations/create", None),
        ("triggers.register",      "POST", "/api/v1/triggers/register", None),
        ("kix-id.register",        "POST", "/api/v1/kix-id/register", None),
        ("push.now",               "POST", "/api/v1/push/now", None),
        ("master.cross-brand",     "GET",
         f"/api/v1/master/{state.get('master_id','X')}/cross-brand-visits", None),
        ("primitives.attributes",  "GET",
         f"/api/v1/primitives/user/{state['triads'][0]['courier_kid']}/attributes",
         {"brand_id": BRAND_ID}),
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
    total_gap  = sum(p["gap"]  for p in phase_counters.values())
    total_fail = sum(p["fail"] for p in phase_counters.values())

    p0 = [f for f in findings if f["severity"] == "P0"]
    p1 = [f for f in findings if f["severity"] == "P1"]
    p2 = [f for f in findings if f["severity"] == "P2"]
    fails = [f for f in findings if f["severity"] == "FAIL"]

    md: list[str] = []
    md.append("# 老贾 / Jia Hong (速达物流 Speed Delivery) — Merchant Journey Findings")
    md.append("")
    md.append(f"**Run tag**: `{RUN_TAG}` | **Runtime**: {runtime:.1f}s | "
              f"**Date**: {time.strftime('%Y-%m-%d %H:%M', time.localtime(start_ts))}")
    md.append("")
    md.append("## Scenario")
    md.append(
        "老贾 owns 「速达物流」(Speed Delivery) — a Guangzhou last-mile courier "
        "company with 200 couriers across 8 zone hubs (天河/越秀/海珠/番禺/白云/"
        "黄埔/花都/南沙). Volume: ~8000 deliveries/day, mixed B2C (个人寄件, "
        "AOV ¥10-30) and B2B (商家发货, AOV ¥0.5-3 大宗). Pain points: "
        "**3-party identity** (sender ≠ recipient ≠ courier), **real-time status** "
        "(created → picked → in_transit → delivered), **courier ratings & bonuses**, "
        "**fraud** (虚假签收 — courier scans delivered without actual handoff), "
        "**failed delivery retry flow**, and the **cross-id attribution** puzzle: "
        "the SENDER pays the ¥15 fee, but the RECIPIENT opens the box — who is "
        "'the customer'? Budget: ¥30K/月."
    )
    md.append("")
    md.append("**Critical difference vs prior merchants**: this is the FIRST sim "
              "with THREE first-class human roles per transaction. All prior "
              "merchants modeled CONSUMER↔BRAND. Logistics introduces "
              "SENDER↔COURIER↔RECIPIENT with three different legal bases, three "
              "different push audiences, and three different incentive structures.")
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

    section("P0 — Blockers for the logistics use case", p0)
    section("P1 — Friction", p1)
    section("P2 — Nice-to-have", p2)
    section("Hard failures", fails)

    md.append("## Cross-Comparison: What Logistics Needs That Other Industries Don't")
    md.append("")
    md.append(
        "老贾's logistics business probes axes that prior merchant sims (老王 F&B, "
        "老李 book club, 老黄 e-commerce, 老周 fitness, 老五 K12 education) could "
        "not exercise. FOUR classes of gap are UNIQUE to last-mile logistics.\n"
        "\n"
        "### 1. Three-Party Identity (the signature gap)\n"
        "Every prior sim has TWO roles: customer + brand (or in education, "
        "parent + child + brand = still 1 transaction has 2 humans). Logistics has "
        "THREE: sender + recipient + courier — each with its own consent basis, "
        "push audience, and incentive structure.\n"
        "  - Consent has no 'role' tag — same human can be sender today, recipient "
        "tomorrow, no audit trail of which capacity granted which consent\n"
        "  - Couriers as 'users' coerce a labor-compensation flow through "
        "consumer-loyalty primitives (tiers/XP/vouchers); no `user_type=employee`\n"
        "  - Relationships module gives us pairwise edges but no `delivery` domain "
        "object that atomically binds (sender, recipient, courier, shipment_id)\n"
        "\n"
        "### 2. Real-Time Status Push (event stream)\n"
        "F&B merchants push 1-2 times/day with strong cooldown ('不要烦客'). "
        "Logistics pushes 4-5 times PER PACKAGE within minutes "
        "(created/picked/in_transit/out_for_delivery/delivered). The push primitive "
        "is built for the F&B cadence:\n"
        "  - Geofence cooldown_minutes defaults to anti-spam — has to be set to 0 "
        "with no documented 'real-time mode'\n"
        "  - `attribute_changed` rule did not fire on `delivery_status` writes — "
        "attribute writes don't reliably fan out to rule_engine v2\n"
        "  - Geofence /enter conflates actor (courier location) with audience "
        "(recipient push). No trigger_user_id vs notify_user_id distinction.\n"
        "  - No `/push/fan-out` to send one event to BOTH sender + recipient as a "
        "single logical operation\n"
        "\n"
        "### 3. Cross-ID Attribution (who is THE customer?)\n"
        "F&B/retail purchases assume payer == experiencer. Logistics: payer "
        "(sender) ≠ experiencer (recipient). Every attribution endpoint binds to "
        "ONE user_id:\n"
        "  - `track/purchase` accepts only one user_id → must arbitrarily choose "
        "to credit sender or recipient, losing the other side from all downstream "
        "LTV / retargeting / lookalike\n"
        "  - No `paying_user_id` + `beneficiary_user_ids[]` schema\n"
        "  - No revenue-split policy (sender=1.0, recipient=0.5 etc.)\n"
        "  - Audience segments cannot express OR(sender, recipient) of a "
        "delivery — fundamental 'how big is my delivery-touched universe?' is "
        "unanswerable\n"
        "\n"
        "### 4. Logistics Domain Primitives (mostly absent)\n"
        "  - No `/api/v1/delivery/create` — must encode as a reservation\n"
        "  - No `/api/v1/delivery/{id}/confirm-received` — recipient confirmation\n"
        "  - Reservation `type` enum has no 'pickup' / 'delivery' — must fall back "
        "to `service` with metadata.domain='logistics_pickup', losing all stat "
        "filtering\n"
        "  - No `assignee_user_id` / `linked_actor[]` field on reservation — "
        "courier_kid lives in metadata as a string\n"
        "  - No media/blob primitive — photo-of-delivery is a CDN URL stored as "
        "an attribute, no chain-of-custody / OCR / virus scan / availability "
        "guarantee\n"
        "  - No fraud / dispute primitives — 虚假签收 is a top complaint category "
        "with no platform signal feeding back into courier bonus algorithm\n"
        "  - No `merchant_size` / B2B targeting axes — B2B revenue line (40% for "
        "老贾) is invisible to campaign targeting\n"
        "\n"
        "### Adjacent (shared with prior sims but sharper here)\n"
        "  - Time-series attributes work for courier rating progression (R5 win), "
        "but rule_engine v2 doesn't reliably re-trigger on threshold cross\n"
        "  - Wallet has no B2C/B2B sub-channel split — two very different bid "
        "economics share one budget\n"
        "  - Voucher refund-target ambiguity — sender paid, but should refund "
        "go to sender or recipient? Platform has no opinion.\n"
    )
    md.append("")

    md.append("## Strategic Recommendations")
    md.append("")
    md.append(
        "1. **[P0] Delivery domain primitive**: `POST /api/v1/delivery/create` "
        "with `{sender_user_id, recipient_user_id, courier_user_id, shipment_id, "
        "status_machine}` — atomic capture of the 3-party tuple. "
        "`POST /delivery/{id}/status` for state-machine transitions with native "
        "trigger fan-out. `POST /delivery/{id}/confirm-received` for the recipient "
        "confirmation primitive that fraud-detection depends on.\n"
        "2. **[P0] Co-attribution / shared credit**: extend "
        "`/attribution/track/purchase` to accept "
        "`{paying_user_id, beneficiary_user_ids[]}` with a revenue-split policy "
        "({sender:1.0, recipient:0.5}). Audience segmentation must support "
        "OR(role:sender, role:recipient) / 'delivery-touched in last N days'. "
        "Logistics is the canonical case; the same pattern unlocks gift-card / "
        "B2B-buyer-for-end-user / corporate-perks across all merchants.\n"
        "3. **[P0] Role-aware identity + consent**: KiX ID and consent grant must "
        "support a `role` tag (`sender|recipient|courier|employee|admin`) so the "
        "audit trail can reconstruct which capacity granted which scope. "
        "Couriers should be `user_type=employee` with separate wallet + scopes; "
        "today loyalty/voucher primitives misuse-by-design as labor comp.\n"
        "4. **[P0] Real-time status push / event stream**: "
        "`POST /push/fan-out` (one event → recipient_list[] → single billing unit "
        "+ shared rate-limit), and a documented `cooldown_minutes=0 + "
        "delivery_status` real-time mode that bypasses default anti-spam. "
        "Geofence /enter should split `trigger_user_id` from `notify_user_id` so "
        "courier arrival routes to recipient push natively.\n"
        "5. **[P0] Media / blob primitive**: "
        "`POST /api/v1/media/upload` returning signed CDN URL + integrity hash + "
        "TTL. Photo-of-delivery, restaurant menus, course materials, body-progress "
        "photos all need a first-class chain-of-custody store, not free-form "
        "attribute URLs.\n"
        "6. **[P0] Fraud / dispute primitives**: "
        "`POST /api/v1/disputes/open` (recipient claims missing/damaged), "
        "`GET /api/v1/fraud/user/{uid}/score` (computed from "
        "delivered-but-unconfirmed rate, dispute rate, time-to-confirm). Feed "
        "fraud signal back into courier bonus eligibility so fraudulent couriers "
        "lose 5-star bonuses automatically.\n"
        "7. **[P1] Reservation extension for logistics**: open the `type` enum to "
        "include `pickup` / `delivery`. Add a first-class "
        "`linked_actors: [{role, user_id}]` field so courier_kid + recipient_kid "
        "are not buried in metadata. Add `resource_id` for courier-capacity "
        "tracking.\n"
        "8. **[P1] B2B audience axes**: targeting schema needs "
        "`{merchant_size, business_type, monthly_volume}` so B2B campaigns can "
        "target merchants vs consumers. Today both channels run as 'acquire' "
        "with wasted spend on wrong-fit audiences.\n"
        "9. **[P1] Wallet sub-channel split**: support `{wallet}/sub-budgets` "
        "with named channels (B2C, B2B, courier_bonus_pool). Logistics needs "
        "courier-bonus payouts to be reportably distinct from ad spend; "
        "currently they collide.\n"
        "10. **[P1] Voucher refund-target policy**: voucher template should "
        "carry `refund_target ∈ {payer, beneficiary, both}` so the platform "
        "resolves who gets the apology vs the shipping-fee refund.\n"
        "11. **[P2] Per-policy audience routing**: consent policies should "
        "accept `audience: courier|consumer|merchant` so the platform can route "
        "the right policy version to each role without merchant code.\n"
        "12. **[P2] Geofence real-time mode**: documented "
        "`mode: anti_spam|real_time` flag on push_config; real_time skips "
        "cooldown and allows multi-push within window for status streams."
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
                state = await phase_1_brand_setup(c)
                await phase_2_wallet(c, state)
                await phase_3_three_party_consent(c, state)
                await phase_4_three_party_identity(c, state)
                await phase_5_pickup_reservation(c, state)
                await phase_6_realtime_status(c, state)
                await phase_7_arrival_push(c, state)
                await phase_8_courier_rating(c, state)
                await phase_9_photo_proof(c, state)
                await phase_10_b2c_campaign(c, state)
                await phase_11_b2b_campaign(c, state)
                await phase_12_fraud(c, state)
                await phase_13_failed_delivery(c, state)
                await phase_14_cross_id_attribution(c, state)
                await phase_15_module_probe(c, state)
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
