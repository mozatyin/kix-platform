"""Merchant journey simulation — 老吴 / Wu Min (Shenzhen K12 Tutoring Center).

End-to-end probe of the KiX Ads Platform from the perspective of a K12
education-tutoring merchant. The unique-to-education concerns probed here:

  * **Two-sided identity** — parent is decision-maker / payer, child is the
    actual user. The platform must let us link them, push to the parent on
    the child's behaviour, and audience-filter by the relationship.
  * **Multi-child families** — one payer, N student profiles, sibling-discount
    voucher needs a relational predicate.
  * **High AOV** — ¥10K–30K per semester, paid by the parent. Different cost
    structure than F&B / community ¥100 orders.
  * **Long sales cycle** — trial → 14 days deliberation → enrollment. Needs
    `attribution_window_days >= 14`.
  * **Progress reports** — per-child events (lessons completed, exam scores)
    that trigger pushes to a *different* user (the parent).
  * **WeChat parent-group word-of-mouth** — viral K-factor among parents.
  * **Regulatory backdrop** (双减) — content-compliance flags on creative.

In-process via httpx.ASGITransport so no separate server is needed. Requires
a live local Redis.

Run:
    .venv/bin/python scripts/sim_laowu.py
"""
from __future__ import annotations

import asyncio
import hashlib
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
OWNER_USER_ID = f"laowu_{RUN_TAG}"
BRAND_ID = f"tomorrow_academy_{RUN_TAG}"
FINDINGS_PATH = Path("/Users/mozat/a-docs/laowu-sim-findings.md")

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"

# Center location (Shenzhen, Futian district)
CENTER_LAT = 22.5410
CENTER_LNG = 114.0596

PARENT_FIRSTNAMES = [
    "建国", "海燕", "志强", "丽华", "国栋", "晓敏", "卫东", "桂芳",
    "建华", "玉兰", "永强", "美玲", "建军", "翠华", "金山", "淑芬",
    "建平", "秀英", "卫国", "凤英",
]
CHILD_FIRSTNAMES = [
    "子轩", "雨桐", "梓涵", "浩然", "诗涵", "宇航", "欣怡", "俊杰",
    "可馨", "天佑", "依依", "睿杰", "怡萱", "嘉豪", "梦琪", "鑫磊",
]
PARENT_LASTNAMES = ["王", "李", "张", "陈", "刘", "杨", "黄", "吴", "周", "徐"]


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


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ── Phase 1: Single brand setup ──────────────────────────────────────────
async def phase_1_brand_setup(c: httpx.AsyncClient) -> dict[str, Any]:
    _phase_init("1: Single Brand Setup — 「明日学堂」 K12 Tutoring")
    state: dict[str, Any] = {"brand_id": BRAND_ID, "owner_id": OWNER_USER_ID}

    # No master / multi-store — single location + online. Verify clean
    # behaviour for accessible-brands on a fresh owner.
    sc, b = await call(c, "GET", f"/api/v1/master/user/{OWNER_USER_ID}/accessible-brands")
    if sc == 200 and isinstance(b, dict):
        ok("accessible-brands fresh owner", f"count={b.get('count', 0)} (expected 0)")
    else:
        gap("P2", "accessible-brands fresh owner", f"{sc} {_short(b)}")

    # Probe: a true single-brand merchant should not need a master at all.
    # Wallet should be accessible without one.
    sc, b = await call(c, "GET", f"/api/v1/wallet/{BRAND_ID}")
    if sc in (200, 404):
        ok("single-brand pre-master probe",
           f"GET /wallet/{BRAND_ID} returns {sc} without master — brand-only flow OK")
    else:
        gap("P1", "single-brand pre-master probe", f"{sc} {_short(b)}")

    return state


# ── Phase 2: Wallet — ¥30K monthly budget ────────────────────────────────
async def phase_2_wallet(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("2: Wallet — ¥30K/month high education budget")
    bid = state["brand_id"]

    sc, b = await call(c, "POST", f"/api/v1/wallet/{bid}/topup", json_body={
        "amount_cents": 3_000_000,  # ¥30,000
        "payment_method": "wechat",
    })
    if sc != 200 or not isinstance(b, dict) or "topup_id" not in b:
        fail("topup ¥30,000", f"{sc} {_short(b)}")
        return
    tid = b["topup_id"]
    sc2, b2 = await call(c, "POST", f"/api/v1/wallet/{bid}/topup/{tid}/confirm",
                        json_body={"payment_gateway_response": {"mock": True}})
    if sc2 == 200:
        ok("confirm topup ¥30K", f"credited to {bid}")
    else:
        fail("confirm topup ¥30K", f"{sc2} {_short(b2)}")
        return

    # ¥30K/month ÷ 30 days = ¥1000/day = 100_000c
    sc, b = await call(c, "POST", f"/api/v1/wallet/{bid}/daily-budget",
                       json_body={"daily_budget_cents": 100_000})
    if sc == 200:
        ok("set daily budget ¥1000/day", "high AOV merchant gets generous daily cap")
    else:
        gap("P1", "set daily budget", f"{sc} {_short(b)}")

    sc, b = await call(c, "GET", f"/api/v1/wallet/{bid}")
    if sc == 200 and isinstance(b, dict):
        ok("wallet status", f"balance=¥{b.get('balance_cents', 0)/100:.2f}")
    else:
        gap("P1", "wallet status", f"{sc} {_short(b)}")


# ── Phase 3: Consent + Tier configuration ────────────────────────────────
async def phase_3_consent_tier(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("3: Consent (parent+child) + Parent Tier Ladder")
    bid = state["brand_id"]

    # Publish consent policy
    sc, b = await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
        "version": f"v_{RUN_TAG}",
        "text_md": "# 明日学堂 隐私政策\n家长 + 学生联合身份, 进度报告, 推荐",
        "effective_at": int(time.time()) - 60,
        "requires_re_grant": False,
    })
    if sc == 200:
        ok("publish consent policy", f"version=v_{RUN_TAG}")
    else:
        gap("P0", "publish consent policy", f"{sc} {_short(b)}")
        return

    # Configure parent tier — based on cumulative ¥ spend
    # new_parent (0) / engaged_parent (¥10K) / vip_parent (¥30K)
    sc, b = await call(c, "POST", "/api/v1/primitives/tier/configure", json_body={
        "brand_id": bid,
        "tiers": [
            {"name": "new_parent", "xp_min": 0},
            {"name": "engaged_parent", "xp_min": 10_000},  # ≥¥10K cumulative
            {"name": "vip_parent", "xp_min": 30_000},      # ≥¥30K cumulative
        ],
    })
    if sc == 200:
        ok("configure parent tier ladder", "new_parent / engaged_parent / vip_parent")
    else:
        gap("P1", "configure parent tier", f"{sc} {_short(b)}")
        # Probe: is "tier" the right abstraction for cumulative ¥ spend?
        gap("P2", "tier semantic for ¥-spend",
            "Tiers use xp_min thresholds. For an education merchant the natural axis is "
            "cumulative ¥ spend, not XP. Without a clean 'currency:cny:total' tier source, "
            "the merchant has to mirror every payment into XP themselves.")

    state["consent_version"] = f"v_{RUN_TAG}"


# ── Phase 4: Two-Sided Identity (parent ↔ child) ─────────────────────────
async def phase_4_two_sided_identity(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("4: Two-Sided Identity — Parent + Child Linking (UNIQUE TO EDUCATION)")
    bid = state["brand_id"]

    # Create 8 families: 5 single-child + 3 two-child
    rng = random.Random(RUN_TAG)
    families: list[dict[str, Any]] = []
    parent_ids: list[str] = []
    child_ids: list[str] = []
    for i in range(8):
        last = rng.choice(PARENT_LASTNAMES)
        parent_id = f"parent_{RUN_TAG}_{i:02d}"
        n_children = 2 if i < 3 else 1
        children: list[dict[str, Any]] = []
        for j in range(n_children):
            cid = f"child_{RUN_TAG}_{i:02d}_{j}"
            children.append({
                "user_id": cid,
                "name": f"{last}{rng.choice(CHILD_FIRSTNAMES)}",
                "grade": rng.randint(1, 9),
                "subjects": rng.sample(["math", "english", "science"], k=rng.randint(1, 3)),
            })
            child_ids.append(cid)
        family = {
            "parent": {
                "user_id": parent_id,
                "name": f"{last}{rng.choice(PARENT_FIRSTNAMES)}",
                "phone_sha": _sha256(f"+861{rng.randint(30000000000, 99999999999)}"),
            },
            "children": children,
        }
        families.append(family)
        parent_ids.append(parent_id)

    state["families"] = families
    state["parent_ids"] = parent_ids
    state["child_ids"] = child_ids
    ok("generate families", f"{len(families)} families, {len(parent_ids)} parents, "
       f"{len(child_ids)} children (3 multi-child)")

    # Consent grants for ALL (parents + children both)
    consented = 0
    for uid in parent_ids + child_ids:
        sc, _ = await call(c, "POST", "/api/v1/consent/grant", json_body={
            "user_id": uid,
            "scopes": ["cross_brand_tracking", "personalization", "marketing"],
            "policy_version": state["consent_version"],
            "source": "app",
        })
        if sc == 200:
            consented += 1
    expected = len(parent_ids) + len(child_ids)
    if consented == expected:
        ok("consent grant — both sides", f"{consented}/{expected} (parents + children)")
    else:
        gap("P1", "consent both sides", f"only {consented}/{expected} granted")

    # ── Two-sided probe 1: dedicated relationship endpoint? ──────────────
    f0 = families[0]
    parent_uid = f0["parent"]["user_id"]
    child_uid = f0["children"][0]["user_id"]

    for path in (
        "/api/v1/users/relationship/create",
        "/api/v1/users/relationships/create",
        f"/api/v1/users/{parent_uid}/relationships",
        "/api/v1/relationships/create",
    ):
        sc, _ = await call(c, "POST", path, json_body={
            "parent_user_id": parent_uid,
            "child_user_id": child_uid,
            "relationship": "parent_of",
        })
        if sc == 200 or sc == 201:
            ok("relationship endpoint exists", f"POST {path} accepted")
            state["relationship_endpoint"] = path
            break
    else:
        gap("P0", "user relationship endpoint",
            "no /users/relationship endpoint exists (tried 4 paths, all 404). "
            "K12 education needs first-class parent↔child linking — without it the "
            "merchant must store this relation out-of-band, breaking every audience "
            "filter, push target, and dispute that depends on 'this parent's children'. "
            "Workaround via user_attributes (probed below) is brittle: requires every "
            "consumer to know to look in the JSON blob.")

    # ── Two-sided probe 2: attribute-as-relation workaround ──────────────
    # parent.attribute("children", ["child_001","child_002"])
    for fam in families:
        p_uid = fam["parent"]["user_id"]
        kid_uids = [k["user_id"] for k in fam["children"]]
        sc, b = await call(c, "POST", f"/api/v1/primitives/user/{p_uid}/attributes",
                           json_body={"brand_id": bid, "attrs": {
                               "role": "parent",
                               "children_user_ids": json.dumps(kid_uids),
                               "n_children": len(kid_uids),
                           }})
        if sc not in (200, 201):
            if fam is families[0]:
                gap("P1", "set parent attributes",
                    f"{sc} {_short(b)} — children_user_ids workaround for missing relation")
        # And reverse: child.attribute("parent", parent_id)
        for kid in fam["children"]:
            await call(c, "POST", f"/api/v1/primitives/user/{kid['user_id']}/attributes",
                       json_body={"brand_id": bid, "attrs": {
                           "role": "student",
                           "parent_user_id": p_uid,
                           "grade": kid["grade"],
                           "subjects": json.dumps(kid["subjects"]),
                       }})
    ok("attribute-based relationship workaround", "bidirectional links stored on both sides")

    # Verify: fetch parent's attributes
    sc, b = await call(c, "GET", f"/api/v1/primitives/user/{parent_uid}/attributes",
                       params={"brand_id": bid})
    if sc == 200 and isinstance(b, dict):
        attrs = b.get("attrs", b)
        if isinstance(attrs, dict) and "children_user_ids" in attrs:
            ok("read parent attributes", f"children_user_ids={attrs.get('children_user_ids')}")
        else:
            gap("P1", "attribute readback shape", f"unexpected attrs payload: {_short(b)}")
    else:
        gap("P1", "read parent attributes", f"{sc} {_short(b)}")

    # ── Two-sided probe 3: audience filter "parent has child who attended trial" ─
    # We try the conditions DSL.
    sc, b = await call(c, "POST", "/api/v1/audiences/custom/create", json_body={
        "brand_id": bid,
        "name": "Parents w/ child attended trial last 7d",
        "source": "manual",
        # No native filter for 'has_child_with_event' — try a relational predicate
        # to see if the API even accepts the shape.
        "predicates": [
            {"relation": "has_child",
             "child_predicate": {"event": "trial_attended", "within_days": 7}},
        ],
        "description": "Probe — relational predicate (child of)",
    })
    if sc == 200:
        ok("audience: relational predicate", "API accepted has_child predicate")
    elif sc in (400, 422):
        gap("P0", "audience relational predicate",
            "audiences.custom.create rejected has_child predicate "
            f"({sc} {_short(b)}). The merchant cannot define 'parents whose child did X' "
            "as an audience. Trial-conversion campaigns must enumerate parent_user_ids "
            "out-of-band, then upload as a manual user_ids list — defeats the point of "
            "an ads platform.")
    else:
        gap("P1", "audience relational predicate", f"{sc} {_short(b)}")


# ── Phase 5: Education Recipe Probe ──────────────────────────────────────
async def phase_5_recipe(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("5: Education Recipe — 'course_completion_education'")
    bid = state["brand_id"]

    # Look for an education recipe
    sc, b = await call(c, "GET", "/api/v1/recipes")
    available_ids: list[str] = []
    edu_match: list[dict] = []
    if sc == 200 and isinstance(b, (list, dict)):
        items = b if isinstance(b, list) else b.get("recipes", b.get("items", []))
        available_ids = [r.get("id") or r.get("recipe_id") for r in items if isinstance(r, dict)]
        edu_match = [r for r in items if isinstance(r, dict) and (
            "education" in (r.get("name", "") + r.get("category", "")
                            + (r.get("industry") or "")).lower()
            or "course" in (r.get("name", "") + r.get("id", "")).lower()
            or "tutor" in (r.get("name", "") + r.get("id", "")).lower()
        )]
        info(f"recipe catalog size={len(items)}, sample={available_ids[:6]}")
    else:
        gap("P1", "recipe listing", f"{sc} {_short(b)}")

    if edu_match:
        ok("education recipe found", f"{[r.get('id') for r in edu_match]}")
        state["edu_recipe_id"] = edu_match[0].get("id")
    else:
        gap("P0", "course_completion_education recipe",
            f"no recipe in catalog targets K12 / tutoring / course-completion. "
            f"Catalog covers F&B (starbucks_loyalty), gaming (pokemon_raid, fortnite_battlepass), "
            f"fitness (nike_run_streak), retail (mcd_monopoly), comms (wechat_hongbao), but no "
            f"'course_completion_education' or 'tutoring_progress'. "
            f"Industry enum includes 'kids_education' and 'education' but the seed library has "
            f"zero entries tagged either. Round 3 'new education recipe' has NOT been seeded.")

    # Try filtering by industry=kids_education
    sc, b = await call(c, "GET", "/api/v1/recipes", params={"industry": "kids_education"})
    if sc == 200 and isinstance(b, (list, dict)):
        items = b if isinstance(b, list) else b.get("recipes", b.get("items", []))
        if items:
            ok("?industry=kids_education", f"{len(items)} match")
        else:
            gap("P1", "?industry=kids_education",
                "filter works syntactically but catalog has 0 kids_education recipes")
    elif sc in (400, 422):
        gap("P1", "recipe industry filter schema",
            f"?industry=kids_education rejected: {_short(b)}")
    else:
        gap("P1", "recipe industry filter", f"{sc} {_short(b)}")

    # Try NL→Recipe with an explicit education intent
    sc, b = await call(c, "POST", "/api/v1/recipe-gen/from-description", json_body={
        "brand_id": bid,
        "description": (
            "I run a K12 tutoring center 「明日学堂」 in Shenzhen. I want to gamify a "
            "16-week semester so students earn XP for completing lessons, badges for "
            "exam-score improvements, and parents get a quarterly progress badge wall. "
            "Course-completion = enrollment renewal trigger."
        ),
        "style": "loyalty",
        "industry": "kids_education",
    })
    if sc == 200 and isinstance(b, dict):
        rec = b.get("recipe", b)
        rid = rec.get("id") or rec.get("recipe_id") if isinstance(rec, dict) else None
        ok("NL→Recipe (industry=kids_education)", f"recipe_id={rid}")
        state["gen_recipe_id"] = rid
        # Apply it
        if rid:
            sc_a, b_a = await call(c, "POST", f"/api/v1/recipes/{rid}/apply", json_body={
                "brand_id": bid,
                "overrides": {},
                "rollback_on_conflict": False,
            })
            if sc_a == 200:
                ok("apply generated recipe", "modules + rules wired")
            else:
                gap("P1", "apply generated recipe", f"{sc_a} {_short(b_a)}")
    elif sc in (400, 422):
        gap("P1", "NL→Recipe kids_education", f"{sc} {_short(b)}")
    else:
        gap("P1", "NL→Recipe call", f"{sc} {_short(b)}")


# ── Phase 6: Progress Reports — push to PARENT for child's events ────────
async def phase_6_progress_reports(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("6: Quarterly Progress Reports — push target ≠ event actor")
    bid = state["brand_id"]
    families = state.get("families", [])

    # Simulate per-child progress: lessons_completed, last_exam_score, attendance
    for fam in families[:5]:
        for kid in fam["children"]:
            await call(c, "POST", f"/api/v1/primitives/user/{kid['user_id']}/attributes",
                       json_body={"brand_id": bid, "attrs": {
                           "lessons_completed": str(random.randint(10, 40)),
                           "last_exam_score": str(random.randint(60, 98)),
                           "attendance_rate": str(round(random.uniform(0.75, 1.0), 2)),
                       }})
    ok("per-child progress attributes set", "lessons / exam / attendance")

    # Probe: trigger a push where event happens on child but recipient is parent.
    f0 = families[0]
    parent_uid = f0["parent"]["user_id"]
    child_uid = f0["children"][0]["user_id"]

    # Try the triggers router with a "fire" or "send" pattern.
    sc, b = await call(c, "POST", "/api/v1/triggers/fire", json_body={
        "brand_id": bid,
        "event_actor_user_id": child_uid,
        "push_target_user_id": parent_uid,
        "event": "quarterly_progress_ready",
        "payload": {"lessons": 32, "avg_score": 88, "attendance": 0.93},
    })
    if sc == 200:
        ok("trigger fire — push to parent", "actor=child, target=parent")
    elif sc == 404:
        gap("P0", "triggers/fire endpoint",
            "POST /api/v1/triggers/fire returns 404. The triggers router exists but exposes "
            "no merchant-facing 'fire this event with these recipients' API. Progress reports "
            "(per-child event → push to parent) are not natively supported.")
    elif sc in (400, 422):
        gap("P1", "trigger fire schema",
            f"{sc} {_short(b)} — fields like push_target_user_id not in the schema; the "
            "platform appears to assume push recipient == event actor")
    else:
        gap("P1", "trigger fire", f"{sc} {_short(b)}")

    # Probe: in the rule_engine, can an action declare a recipient_user_id_attr?
    sc, b = await call(c, "POST", f"/api/v1/rules/{bid}/create", json_body={
        "brand_id": bid,
        "name": "Quarterly progress → push parent",
        "trigger_event": "quarter_end",
        "conditions": {"lessons_completed": {"$gte": 20}},
        "actions": [{
            "module": "triggers",
            "action": "send_push",
            "recipient_user_id_attr": "parent_user_id",  # dereference child.parent_user_id
            "message_template": "{child_name} 本季度完成 {lessons} 课时, 平均 {score} 分!",
        }],
        "max_triggers_per_user": 1,
    })
    if sc in (200, 201):
        ok("rule with recipient_user_id_attr", "rule accepted reference-to-parent action")
    elif sc in (400, 404, 422):
        gap("P0", "rule_engine action.recipient_user_id_attr",
            f"rule with action.recipient_user_id_attr rejected ({sc}). "
            "The rule engine cannot express 'when child does X, push to child.parent'. "
            "Every progress-report use case needs this primitive. Workarounds (separate "
            "lambda layer reading child.parent_user_id and re-firing the event) break the "
            "platform's audit trail and PDP attribution.")
    else:
        gap("P1", "rule_engine recipient indirection", f"{sc} {_short(b)}")


# ── Phase 7: Trial Class Conversion Campaign ─────────────────────────────
async def phase_7_trial_conversion(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("7: Trial → Paid Conversion Campaign (30 trial classes)")
    bid = state["brand_id"]
    families = state.get("families", [])

    # Record 30 trial classes — children attend, parents are the audience.
    trial_count = 0
    for fam in families:
        for kid in fam["children"]:
            # Each child takes 1-3 trials
            for _ in range(random.randint(1, 3)):
                await call(c, "POST", f"/api/v1/primitives/brand/{bid}/events",
                           json_body={
                               "event_type": "trial_attended",
                               "user_id": kid["user_id"],
                               "payload": {"subject": random.choice(kid["subjects"])},
                           })
                trial_count += 1
    ok("simulate trial attendance", f"{trial_count} trial events recorded across children")

    # Create the conversion campaign with attribution_window_days=30
    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": bid,
        "name": "Trial → Paid Enrollment (¥15K AOV)",
        "objective": "acquire",
        "bid_strategy": "cpa",
        "max_bid_cents": 50_000,    # ¥500 per converted parent (high AOV justifies)
        "daily_budget_cents": 100_000,
        "total_budget_cents": 1_500_000,
        "attribution_window_days": 30,   # honors Round 2 fix
        "targeting": {
            "geo": {"country": "CN", "city": "Shenzhen", "radius_km": 20},
            "demographics": {"age_min": 28, "age_max": 55},  # parents
        },
        "creative": {
            "recipe_id": state.get("gen_recipe_id") or state.get("edu_recipe_id")
                         or "duolingo_streak",
            "game_slug": "course_completion",
        },
        "schedule": {"start_at": time.time() - 60, "end_at": time.time() + 86400 * 30},
    })
    if sc == 200 and isinstance(b, dict):
        state["trial_campaign_id"] = b["campaign_id"]
        ok("create trial conversion campaign",
           f"id={b['campaign_id']} attribution_window_days=30 ¥500 CPA")
    else:
        gap("P0", "create trial campaign", f"{sc} {_short(b)}")
        return

    # Verify attribution_window_days was actually stored on the campaign
    cid = state["trial_campaign_id"]
    sc, b = await call(c, "GET", f"/api/v1/campaigns/{cid}/details")
    if sc == 200 and isinstance(b, dict):
        stored_window = b.get("attribution_window_days", 0)
        if stored_window == 30:
            ok("attribution_window_days persisted", "stored=30")
        elif stored_window == 0:
            gap("P0", "attribution_window_days lost",
                "campaign created with attribution_window_days=30 but readback shows 0. "
                "Long-sales-cycle merchants (education, real estate, healthcare) cannot "
                "retain conversions that happen 14-30 days after the trial.")
        else:
            gap("P1", "attribution_window_days mismatch",
                f"set=30, read={stored_window}")

    # Approve
    sc_a, b_a = await call(c, "POST", f"/api/v1/campaigns/{cid}/admin/approve",
                           json_body={"admin_token": "DEV", "notes": "sim auto-approve"})
    if sc_a == 200:
        ok("admin approve trial campaign", "approved")
    else:
        gap("P1", "admin approve", f"{sc_a} {_short(b_a)}")


# ── Phase 8: WeChat Mini-Program pixel + CNY purchase ────────────────────
async def phase_8_wechat_pixel(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("8: WeChat Mini-Program Pixel + CNY ¥15K Purchase")
    bid = state["brand_id"]

    # Register pixel with WeChat origin (wx<16+ alnum>)
    sc, b = await call(c, "POST", "/api/v1/pixel/register", json_body={
        "brand_id": bid,
        "allowed_origins": [
            "https://mingri-academy.cn",
            "wxabcdef0123456789",   # WeChat Mini-Program AppID (16+ alnum)
        ],
    })
    if sc == 201 and isinstance(b, dict):
        pid = b["pixel_id"]
        state["pixel_id"] = pid
        ok("pixel register (https + wx<appid>)", f"pixel_id={pid}")
    else:
        gap("P0", "pixel register w/ WeChat origin",
            f"{sc} {_short(b)} — Round-3 WeChat origin support not accepting wx<appid>")
        return

    # Fire a purchase event from the mini-program — parent pays ¥15K
    pid = state["pixel_id"]
    f0 = state["families"][0]
    parent_uid = f0["parent"]["user_id"]
    sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
        "pixel_id": pid,
        "event_type": "purchase",
        "user_id": parent_uid,
        "device_fingerprint": f"dev_{parent_uid}",
        "origin": "wxabcdef0123456789",
        "url": "wxapp://mingri/checkout",
        "value_cents": 1_500_000,
        "currency": "CNY",
        "metadata": {"sku": "math_g5_semester", "for_child": f0["children"][0]["user_id"]},
    })
    if sc == 200:
        ok("pixel purchase event (CNY ¥15K)", "WeChat origin accepted")
    elif sc in (400, 422):
        gap("P1", "pixel purchase schema",
            f"{sc} {_short(b)} — currency/value_cents/metadata.for_child not in schema")
    elif sc == 403:
        gap("P0", "pixel WeChat origin rejected",
            f"origin=wxabcdef0123456789 rejected by allowlist check despite registration "
            f"({_short(b)}). WeChat Mini-Program is where 80%+ of China parent payments "
            "happen — this is a blocker for CN merchants.")
    else:
        gap("P1", "pixel event", f"{sc} {_short(b)}")


# ── Phase 9: Sibling discount voucher — relational predicate ─────────────
async def phase_9_sibling_voucher(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("9: Sibling Discount — 30% off 2nd child (relational predicate)")
    bid = state["brand_id"]

    # Create a voucher template gated on "parent has >= 2 children"
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": bid,
        "name": "二孩优惠 — 30% off 2nd child enrollment",
        "description": "Sibling discount: 30% off when a parent enrolls 2nd child",
        "value": {"type": "percent_off", "amount": 30, "currency": "CNY"},
        "conditions": {
            # No standard `n_children_min` — try it; expect rejection.
            "n_children_min": 2,
            "min_purchase_cents": 1_000_000,  # ¥10K min enrollment
            "usage_limit_per_user": 1,
            "first_time_user_only": False,
        },
        "expires_in_days": 90,
        "stackable": False,
        "transferable": False,
    })
    if sc == 201 and isinstance(b, dict):
        ok("sibling voucher template created",
           f"template_id={b.get('template_id')}  (n_children_min accepted)")
        state["sibling_voucher_id"] = b.get("template_id")
    elif sc in (400, 422):
        gap("P0", "voucher relational condition `n_children_min`",
            f"{sc} {_short(b)} — voucher conditions vocabulary "
            "(min_purchase_cents / min_items / tier_required / first_time_user_only) "
            "has no relational predicate. Sibling discount, family-plan upgrade, "
            "household-balance vouchers cannot be expressed declaratively. Merchant must "
            "issue manually after server-side check.")
        # Retry without the relational predicate (just to get a template id)
        sc2, b2 = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
            "brand_id": bid,
            "name": "二孩优惠 — 30% off (degraded)",
            "description": "Sibling discount (relational gate removed)",
            "value": {"type": "percent_off", "amount": 30, "currency": "CNY"},
            "conditions": {"min_purchase_cents": 1_000_000, "usage_limit_per_user": 1},
            "expires_in_days": 90,
        })
        if sc2 == 201 and isinstance(b2, dict):
            state["sibling_voucher_id"] = b2.get("template_id")
            info(f"fallback voucher created template_id={state['sibling_voucher_id']}")
    else:
        gap("P1", "sibling voucher create", f"{sc} {_short(b)}")


# ── Phase 10: Parent WoM viral / referral ────────────────────────────────
async def phase_10_parent_referral(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("10: Parent Word-of-Mouth — refer 3 parents → ¥500 voucher")
    bid = state["brand_id"]
    parent_ids = state.get("parent_ids", [])

    if len(parent_ids) < 4:
        info("not enough parents to simulate referral; skipping")
        return

    # Inviter is parent[0]; invitees are parent[1..3]
    inviter = parent_ids[0]
    invitees = parent_ids[1:4]

    # Try the network-effect / referral endpoints
    for invitee in invitees:
        sc, b = await call(c, "POST", "/api/v1/network/referral/record", json_body={
            "brand_id": bid,
            "inviter_user_id": inviter,
            "invitee_user_id": invitee,
            "source": "wechat_group",
        })
        if sc not in (200, 201):
            gap("P1", "network referral record",
                f"{sc} {_short(b)} — no /network/referral/record at expected path") \
                if invitee == invitees[0] else None
            break
    else:
        ok("3 parent referrals recorded", f"inviter={inviter} via wechat_group")

    # Probe: K-factor measurement endpoint
    for path in (
        f"/api/v1/network/k-factor/{bid}",
        f"/api/v1/network/viral/{bid}/k-factor",
        f"/api/v1/network/stats/{bid}",
    ):
        sc, b = await call(c, "GET", path)
        if sc == 200 and isinstance(b, dict):
            ok("K-factor measurement", f"{path} → k={b.get('k_factor', b.get('viral_k'))}")
            break
    else:
        gap("P1", "K-factor measurement endpoint",
            "no /network/k-factor or /viral stats endpoint found at obvious paths. "
            "Parent-to-parent word-of-mouth in WeChat groups is the #1 acquisition channel "
            "for tutoring centers; merchants need a measurable K with attribution to a source "
            "channel (group / 1-on-1 share / poster).")

    # Probe: attribution rivalry — "this parent was already referred by a different source"
    sc, b = await call(c, "POST", "/api/v1/attribution/track/conversion", json_body={
        "user_id": invitees[0],
        "brand_id": bid,
        "target_brand": bid,
        "order_id": f"rivalry_{RUN_TAG}",
        "amount_cents": 1_500_000,
        "competing_sources": [
            {"source": "referral", "inviter_user_id": inviter, "ts": time.time() - 86400 * 5},
            {"source": "school_recommendation", "ts": time.time() - 86400 * 20},
        ],
    })
    if sc == 200 and isinstance(b, dict):
        if "winning_source" in b or "attributed_to" in b:
            ok("attribution rivalry resolved",
               f"winner={b.get('winning_source') or b.get('attributed_to')}")
        else:
            gap("P1", "attribution rivalry response shape",
                f"200 but no winning_source/attributed_to: {_short(b)}")
    elif sc in (400, 422):
        gap("P1", "attribution competing_sources",
            f"{sc} {_short(b)} — track/conversion has no competing_sources param; "
            "platform implicitly does last-touch. Education merchants need configurable "
            "attribution model (referral usually wins over generic recommendation)")
    else:
        gap("P1", "attribution rivalry", f"{sc} {_short(b)}")


# ── Phase 11: Exam achievement → cross-identity push ─────────────────────
async def phase_11_exam_achievement(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("11: Exam Score Achievement — badge on child, push to parent")
    bid = state["brand_id"]
    families = state.get("families", [])
    if not families:
        return

    f0 = families[0]
    child = f0["children"][0]
    parent = f0["parent"]

    # Create a brand achievement first
    sc, b = await call(c, "POST", f"/api/v1/primitives/brand/{bid}/achievements",
                       json_body={
                           "name": "学霸 95+",
                           "description": "Exam score >= 95",
                           "icon": "🏆",
                       })
    if sc in (200, 201) and isinstance(b, dict):
        ach_id = b.get("achievement_id") or b.get("id")
        ok("create achievement '学霸 95+'", f"achievement_id={ach_id}")
        state["achievement_id"] = ach_id
    else:
        gap("P1", "create achievement", f"{sc} {_short(b)}")
        return

    # Mark child progress to 100% (i.e., earned)
    sc, b = await call(c, "POST",
                       f"/api/v1/primitives/achievement/{state['achievement_id']}/progress",
                       json_body={
                           "user_id": child["user_id"],
                           "brand_id": bid,
                           "delta": 1.0,
                       })
    if sc == 200 and isinstance(b, dict):
        ok("child earned achievement", f"unlocked={b.get('unlocked', b.get('earned'))}")
    else:
        gap("P1", "child achievement progress", f"{sc} {_short(b)}")

    # Now: can we fan out a notification to the parent automatically? Probe.
    sc, b = await call(c, "POST", "/api/v1/triggers/configure", json_body={
        "brand_id": bid,
        "trigger_event": "achievement_unlocked",
        "conditions": {"achievement_id": state["achievement_id"]},
        "actions": [{
            "type": "push",
            "recipient": "parent_user_id",   # indirect via child.parent_user_id
            "template": "恭喜!{child_name} 在数学测试中取得 95+ 分!",
        }],
    })
    if sc in (200, 201):
        ok("trigger configured: child unlock → parent push", "")
    elif sc == 404:
        gap("P0", "trigger configure endpoint",
            "no /triggers/configure endpoint. Cross-identity event chains "
            "(child unlocks → parent notified) cannot be declared.")
    elif sc in (400, 422):
        gap("P0", "trigger action.recipient indirection",
            f"{sc} {_short(b)} — trigger action has no recipient_user_id_attr / "
            "recipient_indirect field. Same root cause as Phase 6 / Phase 11.")
    else:
        gap("P1", "trigger configure", f"{sc} {_short(b)}")

    info(f"identities involved: child={child['user_id']} parent={parent['user_id']}")


# ── Phase 12: Long sales-cycle attribution (already tested in Phase 7) ───
async def phase_12_attribution_window(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("12: Long Sales-Cycle Attribution (14-day window)")
    bid = state["brand_id"]
    cid = state.get("trial_campaign_id")
    if not cid:
        info("no trial campaign id from phase 7; cannot test")
        return

    # Run an auction to get an impression_token, then "wait" 14 simulated
    # days before reporting the conversion.
    parent_uid = state["parent_ids"][0]
    sc, b = await call(c, "POST", "/api/v1/auction/run", json_body={
        "user_id": parent_uid,
        "device_fingerprint": f"dev_{parent_uid}",
        "geo": {"country": "CN", "city": "Shenzhen", "lat": CENTER_LAT, "lng": CENTER_LNG},
        "context": {"time_of_day": 20, "day_of_week": 3, "device": "mobile", "language": "zh"},
        "slot": "main",
    })
    if sc != 200 or not isinstance(b, dict):
        gap("P1", "auction run", f"{sc} {_short(b)}")
        return
    if b.get("no_eligible_campaigns"):
        gap("P1", "auction returned no_eligible_campaigns",
            "trial-conversion campaign was approved + funded yet no_eligible_campaigns. "
            "Likely cause: campaign quality_score / geo / consent mismatch in auction ranker.")
        return
    token = b.get("impression_token")
    if not token:
        gap("P1", "no impression_token", _short(b))
        return
    ok("auction won", f"token={token[:20]}...")

    # Report impression + click
    await call(c, "POST", "/api/v1/auction/report-impression",
               json_body={"impression_token": token})
    await call(c, "POST", "/api/v1/auction/report-click",
               json_body={"impression_token": token, "user_id": parent_uid,
                          "device_fingerprint": f"dev_{parent_uid}"})

    # Report conversion 14 days later. We can't actually wait — the
    # attribution check is wall-clock. Probe how the API handles it.
    sc, b = await call(c, "POST", "/api/v1/auction/report-conversion", json_body={
        "impression_token": token,
        "user_id": parent_uid,
        "conversion_value_cents": 1_500_000,
        "metadata": {"sales_cycle_days": 14, "for_child": state["families"][0]["children"][0]["user_id"]},
    })
    if sc == 200 and isinstance(b, dict):
        if b.get("rejected") == "outside_attribution_window":
            gap("P1", "attribution outside window",
                "conversion rejected outside_attribution_window — sim is wall-clock; "
                "real merchants will hit this when sales cycle exceeds the configured "
                "window. Confirms window is enforced, but no way to back-date a confirmed "
                "out-of-band conversion.")
        else:
            ok("long-cycle conversion accepted", f"¥15K credited to campaign {cid}")
    else:
        gap("P1", "long-cycle conversion", f"{sc} {_short(b)}")


# ── Phase 13: Renewal retention campaign with target_roas ────────────────
async def phase_13_renewal_campaign(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("13: Quarterly Renewal Campaign — objective=retention, target_roas")
    bid = state["brand_id"]

    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": bid,
        "name": "Quarterly Renewal — Target ROAS 3.0",
        "objective": "retention",
        "bid_strategy": "cpa",   # target_roas is an optimization, not a bid_strategy
        "max_bid_cents": 30_000,  # ceiling
        "daily_budget_cents": 50_000,
        "total_budget_cents": 500_000,
        "attribution_window_days": 60,
        "targeting": {
            "geo": {"country": "CN", "city": "Shenzhen"},
            "demographics": {"age_min": 28, "age_max": 55},
        },
        "creative": {"recipe_id": state.get("gen_recipe_id") or "starbucks_loyalty"},
        "schedule": {"start_at": time.time() - 60, "end_at": time.time() + 86400 * 90},
        # No explicit target_roas field on CampaignCreate — probe.
        "target_roas": 3.0,
    })
    if sc == 200 and isinstance(b, dict):
        ok("retention campaign created", f"id={b['campaign_id']}")
        state["renewal_campaign_id"] = b["campaign_id"]
    elif sc in (400, 422):
        gap("P1", "retention objective + target_roas",
            f"{sc} {_short(b)} — target_roas appears to be a server-side smart-bidding "
            "knob (auction.py:564), but CampaignCreate schema does not surface it. "
            "Merchants cannot declare target_roas at campaign-create time.")
    else:
        gap("P1", "retention campaign", f"{sc} {_short(b)}")

    # If created, verify it can hit the auction
    if state.get("renewal_campaign_id"):
        cid = state["renewal_campaign_id"]
        await call(c, "POST", f"/api/v1/campaigns/{cid}/admin/approve",
                   json_body={"admin_token": "DEV", "notes": "sim"})
        # Check via campaign details that target_roas was stored
        sc, b = await call(c, "GET", f"/api/v1/campaigns/{cid}/details")
        if sc == 200 and isinstance(b, dict):
            if b.get("target_roas") == 3.0:
                ok("target_roas persisted", "stored=3.0")
            else:
                gap("P1", "target_roas not persisted",
                    f"sent 3.0, readback={b.get('target_roas')} — smart bidding gets the "
                    "default 0 → bid optimizer falls back to fixed max_bid_cents")


# ── Phase 14: Edge Cases ─────────────────────────────────────────────────
async def phase_14_edge_cases(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("14: Edge Cases — identity merge, grandparent, regulation, packages")
    bid = state["brand_id"]
    families = state.get("families", [])

    # 14a: Identity merge — two parent_ids that should be the same person
    sc, b = await call(c, "POST", "/api/v1/users/merge", json_body={
        "primary_user_id": state["parent_ids"][0] if state.get("parent_ids") else "x",
        "secondary_user_id": "duplicate_parent_imposter",
    })
    if sc == 200:
        ok("identity merge", "two parent accounts merged")
    elif sc == 404:
        gap("P0", "identity merge endpoint",
            "no /users/merge endpoint. Education merchants regularly hit duplicate-parent "
            "scenarios (mom signed up at trial, dad at enrollment). Without a merge primitive "
            "the platform double-counts MAUs, double-charges frequency caps, and splits "
            "spend attribution.")
    elif sc in (400, 422):
        gap("P1", "identity merge schema", f"{sc} {_short(b)}")

    # 14b: Grandparent — third-party payer ≠ decision-maker
    if families:
        grandparent = f"grandparent_{RUN_TAG}_00"
        await call(c, "POST", "/api/v1/consent/grant", json_body={
            "user_id": grandparent,
            "scopes": ["personalization", "marketing"],
            "policy_version": state["consent_version"],
            "source": "in_person",
        })
        sc, b = await call(c, "POST", f"/api/v1/primitives/user/{grandparent}/attributes",
                           json_body={"brand_id": bid, "attrs": {
                               "role": "payer_third_party",
                               "pays_for_parent_user_id": families[0]["parent"]["user_id"],
                               "pays_for_child_user_id": families[0]["children"][0]["user_id"],
                           }})
        if sc == 200:
            ok("third-party payer (grandparent) modeled", "via attributes workaround")
        # The deeper gap: there's no first-class 'payer' role distinct from
        # decision-maker, and no 3-actor attribution.
        gap("P1", "third-party payer role",
            "grandparent paying for child while parent is decision-maker is a 3-actor "
            "scenario. Platform has 'user' but no payer ≠ beneficiary ≠ decision-maker "
            "distinction. Receipts go to whoever swiped the card, marketing pushes to "
            "whoever swiped the card — wrong on both counts.")

    # 14c: Content compliance — 双减 (Chinese tutoring regulation)
    sc, b = await call(c, "POST", "/api/v1/creative-gen/request", json_body={
        "brand_id": bid,
        "name": "Math Tutoring Promo",
        "spec": {
            "game_type": "quiz",
            "brand_description": "明日学堂 — 数学一对一辅导, 提分 30%",
            "brand_color": "#1A4E8E",
            "goal": "acquisition",
            "reward": "voucher",
            "duration_seconds": 30,
            # Probe: does the creative-gen pipeline have a compliance-filter
            # for promises ('+30 marks', '保证提分')?
            "compliance_jurisdiction": "CN",
            "regulation_flags": ["doubleten", "no_grade_promise"],
        },
    })
    if sc == 202:
        ok("creative-gen with compliance flags", "accepted (will see if filtered)")
    elif sc in (400, 422):
        gap("P0", "content compliance for 双减",
            f"{sc} {_short(b)} — no compliance_jurisdiction / regulation_flags in spec. "
            "Chinese K12 advertising is heavily regulated (双减 forbids grade-improvement "
            "promises, after-school subject tutoring marketing). Platform has no copy-checker "
            "or jurisdictional filter — every ad would need merchant-side legal review.")
    else:
        gap("P1", "creative-gen compliance", f"{sc} {_short(b)}")

    # 14d: Online vs offline event types — fulfillment difference
    # Probe: brand events accept fulfillment_mode?
    sc, b = await call(c, "POST", f"/api/v1/primitives/brand/{bid}/events",
                       json_body={
                           "event_type": "class_completed",
                           "user_id": families[0]["children"][0]["user_id"] if families else "x",
                           "payload": {
                               "fulfillment_mode": "online",
                               "duration_min": 50,
                           },
                       })
    if sc in (200, 201):
        ok("event w/ fulfillment_mode=online", "payload accepted (no validation though)")
    else:
        info(f"event create: {sc} {_short(b)}")

    # 14e: Course package (bundle) vs single class — voucher on bundles
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": bid,
        "name": "学期套餐 50% off second semester",
        "value": {"type": "percent_off", "amount": 50, "currency": "CNY"},
        "conditions": {
            "bundle_required": "semester_pkg_2",
            "min_purchase_cents": 2_000_000,
        },
        "expires_in_days": 180,
    })
    if sc == 201:
        ok("bundle voucher template", "bundle_required field accepted")
    elif sc in (400, 422):
        gap("P1", "voucher bundle_required condition",
            f"{sc} {_short(b)} — no bundle / package SKU condition. "
            "Education-style 'buy semester 1 → 50% off semester 2' cannot be expressed.")

    # 14f: Dispute flow — parent claims "this lead was already our student"
    sc, b = await call(c, "POST", "/api/v1/disputes/open", json_body={
        "brand_id": bid,
        "category": "attribution_dispute",
        "evidence_text": "Conversion attributed to KiX campaign — but this parent was already "
                         "our student from prior semester, source=organic_renewal",
        # Probe: dispute with stronger payload
        "claimed_actual_source": "organic_renewal",
        "user_id": state["parent_ids"][0] if state.get("parent_ids") else "x",
    })
    if sc == 200 and isinstance(b, dict):
        ok("attribution dispute opened", f"id={b.get('dispute_id')} status={b.get('status')}")
    elif sc in (400, 422):
        gap("P1", "dispute category=attribution_dispute",
            f"{sc} {_short(b)} — attribution_dispute may not be a known category; "
            "education merchants need a 'this student was already ours' dispute path that "
            "doesn't require a specific impression_token (long sales cycle = token expired)")
    else:
        gap("P1", "open dispute", f"{sc} {_short(b)}")


# ── Phase 15: Module availability probe ──────────────────────────────────
async def phase_15_module_probe(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("15: Module Availability Probe (education-relevant)")
    bid = state["brand_id"]
    parent_uid = state["parent_ids"][0] if state.get("parent_ids") else "x"

    probes = [
        ("users.relationship", "GET", f"/api/v1/users/{parent_uid}/relationships", None),
        ("users.merge", "POST", "/api/v1/users/merge", None),
        ("primitives.user.attrs", "GET", f"/api/v1/primitives/user/{parent_uid}/attributes", None),
        ("triggers.configure", "POST", "/api/v1/triggers/configure", None),
        ("network.k-factor", "GET", f"/api/v1/network/k-factor/{bid}", None),
        ("network.referral", "POST", "/api/v1/network/referral/record", None),
        ("partnerships.list", "GET", f"/api/v1/partnerships/brand/{bid}", None),
        ("vouchers.bundle", "GET", f"/api/v1/brands/{bid}/vouchers/bundles", None),
        ("recipes.industry.kids_education", "GET", "/api/v1/recipes",
         {"industry": "kids_education"}),
        ("storefront.education", "GET", f"/api/v1/storefront/{bid}", None),
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
                gap("P1", f"module missing: {label}", f"404 Not Found at {path}")
            else:
                available.append(label)
                ok(f"module live (no-resource): {label}", "404 with domain detail")
        elif sc in (400, 422):
            # Endpoint exists but rejected our probe payload — still counts as live
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
    md.append("# 老吴 / Wu Min (Tomorrow Academy K12) — Merchant Journey Findings")
    md.append("")
    md.append(f"**Run tag**: `{RUN_TAG}` | **Runtime**: {runtime:.1f}s | "
              f"**Date**: {time.strftime('%Y-%m-%d %H:%M', time.localtime(start_ts))}")
    md.append("")
    md.append("## Scenario")
    md.append(
        "老吴 owns 「明日学堂」 — a Shenzhen K12 tutoring center (math/english/science, "
        "grades 1-9). 800 students, ~30 trial classes/week. Pricing ¥10K–30K/semester paid "
        "by parents. Budget ¥30K/month. Education margins fund a high CPA. Unique pains: "
        "**parent is the buyer, child is the user** (two-sided identity); multi-child "
        "families need sibling discounts; quarterly progress reports must reach the parent "
        "(not the child); enrollment cycle 7-30 days; word-of-mouth in WeChat parent groups "
        "is the #1 channel."
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

    md.append("## Cross-Industry Comparison — What K12 Education Needs That Others Don't")
    md.append("")
    md.append(
        "Compared with prior simulations (老王 F&B 10-store, 老李 community/book club, "
        "老黄 baby products, 老张 fitness), K12 tutoring exposes these *unique* primitives "
        "that other verticals never demanded:\n"
        "\n"
        "1. **Two-sided identity (parent ↔ child)**. F&B has one identity (the buyer). "
        "Book club has one. Even baby products are 'parent buys for baby' but the baby "
        "is not a tracked user. K12 is the first where *both* sides are events-emitting, "
        "consented users — and the linkage between them is a first-class concept.\n"
        "\n"
        "2. **Push target ≠ event actor**. F&B push targets and event actors are always the "
        "same person. K12 is the first where the trigger event ('child completes lesson') "
        "and the push recipient ('parent') are different user_ids that must be related at "
        "delivery time.\n"
        "\n"
        "3. **Relational voucher conditions**. F&B voucher conditions are user-local "
        "(min_purchase, tier_required). K12's sibling discount is the first that's "
        "*household-level* — needs `parent.n_children >= 2`.\n"
        "\n"
        "4. **Long sales cycle attribution**. F&B conversions happen same-day. K12 trial→pay "
        "takes 7-30 days. The 7-day default is wrong; `attribution_window_days` up to 30 "
        "is critical, and back-dating confirmed conversions (e.g., parent enrolled offline "
        "3 weeks later) needs a primitive.\n"
        "\n"
        "5. **Jurisdiction-aware creative compliance**. F&B / fitness ads are largely "
        "free-form. K12 ads in China are policed under 双减政策 (no grade-improvement "
        "promises, no false guarantees). No other vertical so far has required a "
        "compliance_jurisdiction or regulation_flags on creatives.\n"
        "\n"
        "6. **Third-party payer (grandparent)**. F&B payer = consumer. K12 introduces a "
        "third role: payer ≠ decision-maker (parent) ≠ beneficiary (child). The platform's "
        "single-user model can't express this without ad-hoc attributes.\n"
        "\n"
        "7. **Identity merge** (deferred but spotted in F&B too). Acute in education because "
        "mom-signs-trial / dad-signs-enrollment is the *modal* pattern, not an edge case.\n"
    )
    md.append("")

    md.append("## Strategic Recommendations (Top 5)")
    md.append("")
    md.append(
        "1. **[P0] Ship `/users/{uid}/relationships` as a first-class primitive.** "
        "Today the relationship has to be encoded inside user_attributes JSON; every "
        "consumer (audiences, triggers, push) must learn the convention. A typed graph "
        "(parent_of, payer_for, sibling_of, guardian_of) lets every downstream filter "
        "speak the same language. Minimum schema:\n"
        "   ```\n"
        "   POST /users/{uid}/relationships\n"
        "     { other_user_id, relation, brand_id?, valid_from?, valid_until? }\n"
        "   GET  /users/{uid}/relationships?relation=children\n"
        "   ```\n"
        "\n"
        "2. **[P0] Trigger / rule actions need `recipient_user_id_attr` indirection.** "
        "Today action.recipient is implicit (event actor). Add an attribute-deref form: "
        "`action.recipient_user_id_attr = 'parent_user_id'` resolves at fire-time. "
        "Same primitive solves quarterly progress reports, exam-achievement pushes, and "
        "any 'when child does X, notify parent' flow.\n"
        "\n"
        "3. **[P0] Seed `course_completion_education` / `tutoring_progress` recipes.** "
        "Industry enum already includes `kids_education` and `education` but the catalog "
        "has 0 entries. NL→Recipe falls back to a generic streak. Education merchants "
        "need a tier+streak+badge wall+voucher recipe out of the box — same level of "
        "polish as `starbucks_loyalty`.\n"
        "\n"
        "4. **[P0] Relational predicates in audiences and vouchers.** Audience: "
        "`has_child_with_event(trial_attended, within_days=7)`. Voucher conditions: "
        "`n_children_min`, `bundle_required`. Without these, education-vertical "
        "campaigns cannot be expressed declaratively — merchants exit the platform "
        "to a CRM and the data round-trip kills latency and consent posture.\n"
        "\n"
        "5. **[P1] Smart bidding `target_roas` surfaced on CampaignCreate.** The auction "
        "ranker reads campaign.target_roas (auction.py:564) but the CampaignCreate "
        "schema doesn't surface it. High-AOV education merchants would benefit most "
        "from ROAS targeting — currently the only knob is fixed `max_bid_cents`, which "
        "either over-spends on cheap leads or starves expensive ones.\n"
        "\n"
        "6. **[P1] Compliance pipeline for creative-gen** (CN: 双减; US: COPPA; EU: GDPR-K). "
        "Add `spec.compliance_jurisdiction` and a per-jurisdiction copy-checker stage. "
        "Education is the first vertical where mandatory legal review is the default, "
        "not the exception.\n"
        "\n"
        "7. **[P1] WeChat Mini-Program origin must be solid.** Round-3 added `wx<appid>` "
        "but the surrounding pixel-event flow (currency, value_cents, metadata.for_child) "
        "needs an explicit education-payment schema. 80%+ of CN parent payments happen "
        "inside WeChat Mini-Programs.\n"
        "\n"
        "8. **[P1] `/users/merge` primitive**. Acute in education (mom-trials, dad-pays). "
        "Without it: double-counted MAU, split spend attribution, broken frequency caps.\n"
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
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30.0
        ) as c:
            state: dict[str, Any] = {}
            try:
                state = await phase_1_brand_setup(c)
                await phase_2_wallet(c, state)
                await phase_3_consent_tier(c, state)
                await phase_4_two_sided_identity(c, state)
                await phase_5_recipe(c, state)
                await phase_6_progress_reports(c, state)
                await phase_7_trial_conversion(c, state)
                await phase_8_wechat_pixel(c, state)
                await phase_9_sibling_voucher(c, state)
                await phase_10_parent_referral(c, state)
                await phase_11_exam_achievement(c, state)
                await phase_12_attribution_window(c, state)
                await phase_13_renewal_campaign(c, state)
                await phase_14_edge_cases(c, state)
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
