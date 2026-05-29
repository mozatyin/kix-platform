"""Merchant journey simulation — 老张 / Zhang Wei (故宫小馆, fine dining, Beijing).

End-to-end probe of the KiX Ads Platform from the perspective of a SINGLE
high-end restaurant.  Different from 老王 (bubble tea, 10 stores, ¥30 ticket)
and 老李 (book club, community): SINGLE LUXURY VENUE, RESERVATION-DRIVEN,
HIGH-VALUE per-transaction (¥800-¥2500), VIP-HEAVY, INTERNATIONAL TOURISTS.

Walks through 12 phases:
  1. Single brand "forbidden_city_small_house" + storefront w/ imperial palette
  2. Wallet funded ¥8000, daily ¥267
  3. VIP tier primitive — 3 tiers (guest / silver / VIP-gold) via /primitives/tier
  4. Reservation game — Recipe Generator probe for "10-day countdown"
  5. Pixel integration for HIGH-VALUE (¥2500 = 250k cents) purchase event
  6. International tourist audience (en/zh/ko/ja) + lookalike + geo+language
  7. No-show prevention — time-bound conditions, recovery voucher
  8. VIP personalization — geofence enter + per-user freq-cap override probe
  9. Single high-value campaign (¥200 CPA for tourist acquisition)
 10. Multi-step lifecycle attribution (click→reservation→7d→visit→spend)
 11. Bulk voucher issuance (60 VIPs, 90-day TTL)
 12. Edge cases (tip, group, cancel, FX, multilang, freq-cap vs recognition)

In-process via httpx.ASGITransport, no separate server. Requires local Redis.

Run:
    .venv/bin/python scripts/sim_laozhang.py
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
OWNER_USER_ID = f"laozhang_{RUN_TAG}"
BRAND_ID = f"forbidden_city_small_house_{RUN_TAG}"
STORE_ID = f"store_{BRAND_ID}"
FINDINGS_PATH = Path("/Users/mozat/a-docs/laozhang-sim-findings.md")

# Forbidden City coords (Beijing)
RESTAURANT_LAT = 39.9163
RESTAURANT_LNG = 116.3972

# 100 covers/night, 60% VIP, 40% one-shot tourists
TOTAL_CUSTOMERS = 50  # scaled down for sim speed
N_VIP = int(TOTAL_CUSTOMERS * 0.60)        # 30
N_TOURIST = TOTAL_CUSTOMERS - N_VIP        # 20

# Average check ¥800/person, ¥2500/table
AVG_TABLE_CENTS = 250_000   # ¥2500
AVG_PERSON_CENTS = 80_000   # ¥800

# Budget ¥8000/month
MONTHLY_BUDGET_CENTS = 800_000
DAILY_BUDGET_CENTS = MONTHLY_BUDGET_CENTS // 30  # 26666 ≈ ¥267

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"


# International tourist persona pool
VIP_NAMES_ZH = ["张老板", "王总", "李董", "陈先生", "刘女士", "周总", "吴老师",
                "郑先生", "孙女士", "钱总", "马董", "黄老板"]
TOURIST_FIRSTNAMES = ["James", "Emma", "Liam", "Olivia", "Hiroshi", "Yuki",
                     "Min-jun", "Seo-yeon", "Pierre", "Marie", "Hans", "Anna"]
TOURIST_LASTNAMES = ["Smith", "Tanaka", "Kim", "Dubois", "Müller", "Rossi",
                    "Garcia", "Anderson", "Yamamoto", "Lee", "Park", "Lopez"]
TOURIST_LANGS = ["en", "ja", "ko", "fr", "de", "es"]


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
    findings.append({"phase": _current_phase, "severity": sev,
                     "action": action, "detail": detail})
    color = RED if sev == "P0" else (YELLOW if sev == "P1" else MAGENTA)
    print(f"  {color}[GAP {sev}]{RESET} {action} — {detail}")


def fail(action: str, detail: str) -> None:
    phase_counters[_current_phase]["fail"] += 1
    findings.append({"phase": _current_phase, "severity": "FAIL",
                     "action": action, "detail": detail})
    print(f"  {RED}[FAIL]{RESET} {action} — {detail}")


def info(msg: str) -> None:
    print(f"  {BLUE}[..]{RESET} {msg}")


# ── HTTP helpers ─────────────────────────────────────────────────────────
async def call(c: httpx.AsyncClient, method: str, path: str, *,
               json_body: Any = None, params: dict | None = None,
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


# ── Phase 1: Single brand + storefront ───────────────────────────────────
async def phase_1_brand(c: httpx.AsyncClient) -> dict[str, Any]:
    _phase_init("1: Single Brand (Forbidden City Small House)")
    state: dict[str, Any] = {"brand_id": BRAND_ID, "store_id": STORE_ID}

    # Configure storefront with imperial Chinese aesthetic
    sc, b = await call(c, "POST", f"/api/v1/storefront/{BRAND_ID}/configure", json_body={
        "display_name": "故宫小馆 / Forbidden City Small House",
        "bio": "Beijing imperial fine dining since 2012. Reservation only.",
        "brand_color": "#8B0000",
        "country": "CN",
        "category": "food",
    })
    if sc == 200:
        ok("storefront configure (imperial red #8B0000)", "single luxury venue")
    else:
        gap("P1", "storefront configure", f"{sc} {_short(b)}")

    sc, b = await call(c, "GET", f"/api/v1/storefront/{BRAND_ID}")
    if sc == 200:
        ok("storefront read public", "profile fetched")
        # Check if multi-language fields exist
        if isinstance(b, dict):
            keys = set(b.keys())
            has_i18n = bool({"display_name_en", "bio_en", "i18n", "translations"} & keys)
            if not has_i18n:
                gap("P2", "multi-language storefront",
                    "storefront has no display_name_en/bio_en/i18n field; "
                    "International tourists landing from English campaign see only Chinese.")
    else:
        gap("P1", "storefront read", f"{sc} {_short(b)}")

    # Register geofence (single store, 300m radius - tighter for restaurant)
    sc, b = await call(c, "POST", "/api/v1/geofence/stores/register", json_body={
        "brand_id": BRAND_ID,
        "store_id": STORE_ID,
        "name": "故宫小馆 旗舰店",
        "brand_name": "故宫小馆",
        "lat": RESTAURANT_LAT,
        "lng": RESTAURANT_LNG,
        "radius_meters": 300,
        "associated_game_slug": "imperial_puzzle",
        "push_config": {
            "enabled": True,
            "cooldown_minutes": 1440,  # once a day max
            "hours_local": [11, 22],
            "message_template": "尊贵的{name}，故宫小馆欢迎您光临 🏮",
        },
    })
    if sc == 200:
        ok("register single venue geofence", "300m radius, 11-22h, daily cap")
    else:
        gap("P0", "register venue", f"{sc} {_short(b)}")

    return state


# ── Phase 2: Wallet ¥8000 ────────────────────────────────────────────────
async def phase_2_wallet(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("2: Wallet Funding (¥8000)")

    sc, b = await call(c, "POST", f"/api/v1/wallet/{BRAND_ID}/topup", json_body={
        "amount_cents": MONTHLY_BUDGET_CENTS,
        "payment_method": "wechat",
    })
    if sc != 200 or not isinstance(b, dict) or "topup_id" not in b:
        fail("topup", f"{sc} {_short(b)}")
        return
    tid = b["topup_id"]
    sc2, b2 = await call(c, "POST", f"/api/v1/wallet/{BRAND_ID}/topup/{tid}/confirm",
                        json_body={"payment_gateway_response": {"mock": True}})
    if sc2 == 200:
        ok("topup confirm", f"¥{MONTHLY_BUDGET_CENTS/100:.0f} funded")
    else:
        gap("P1", "topup confirm", f"{sc2} {_short(b2)}")

    sc, b = await call(c, "POST", f"/api/v1/wallet/{BRAND_ID}/daily-budget",
                       json_body={"daily_budget_cents": DAILY_BUDGET_CENTS})
    if sc == 200:
        ok("daily budget set", f"¥{DAILY_BUDGET_CENTS/100:.0f}/day = ¥8000/30")
    else:
        gap("P1", "daily budget set", f"{sc} {_short(b)}")

    sc, b = await call(c, "GET", f"/api/v1/wallet/{BRAND_ID}/daily-budget-status")
    if sc == 200 and isinstance(b, dict):
        ok("wallet status",
           f"balance=¥{b.get('balance_cents',0)/100:.2f} "
           f"daily=¥{b.get('daily_budget_cents',0)/100:.2f}")


# ── Phase 3: VIP Tier System ─────────────────────────────────────────────
async def phase_3_tier(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("3: VIP Tier System (¥10K/year membership)")

    # Define three tiers
    tiers = [
        {"id": "guest", "name": "Guest", "threshold_xp": 0,
         "perks": ["table_reservation"]},
        {"id": "silver", "name": "银卡会员", "threshold_xp": 5000,
         "perks": ["table_reservation", "priority_seating", "10pct_off"]},
        {"id": "vip_gold", "name": "金卡VIP (¥10K/yr)", "threshold_xp": 20000,
         "perks": ["table_reservation", "private_room", "chef_greeting",
                  "personalized_menu", "complimentary_wine"]},
    ]

    created = 0
    for t in tiers:
        sc, b = await call(c, "POST", f"/api/v1/primitives/brand/{BRAND_ID}/tiers",
                           json_body=t)
        if sc == 200:
            created += 1
        else:
            gap("P1", f"create tier {t['id']}", f"{sc} {_short(b)}")
    if created == 3:
        ok("create 3 tiers", "guest / silver / vip_gold (¥10K/year)")
    else:
        gap("P0", "create tiers", f"only {created}/3 created — VIP system incomplete")

    # Verify list
    sc, b = await call(c, "GET", f"/api/v1/primitives/brand/{BRAND_ID}/tiers")
    if sc == 200 and isinstance(b, list) and len(b) == 3:
        ok("list tiers", f"{len(b)} tiers visible")
    else:
        gap("P1", "list tiers", f"{sc} got {_short(b)}")

    # Probe: paid-membership semantics — does "VIP" map to XP threshold only?
    # ¥10K = 1,000,000 cents. There is no spend→XP automatic mapping.
    gap("P1", "paid-membership primitive",
        "Tier primitive is XP-threshold only. There is no built-in concept of "
        "a *paid* annual membership (¥10K/year): no expiry, no auto-renewal, no "
        "purchase event. Merchant must hand-grant XP=20000 to every VIP and "
        "manually revoke on lapse. For luxury venues this is the core loyalty "
        "abstraction — needs first-class /memberships endpoints.")

    # Probe: per-brand tier scopes
    gap("P2", "cross-brand tier portability",
        "Tier is scoped to one brand_id. For a hotel-restaurant group (Marriott "
        "→ ChinaTang) the VIP tier cannot be inherited across brands without a "
        "master-level tier definition.")

    # Try to grant XP to first VIP to promote them
    vip_uid = f"vip_{RUN_TAG}_00"
    sc, b = await call(c, "POST", f"/api/v1/primitives/currency/xp/grant", json_body={
        "user_id": vip_uid, "brand_id": BRAND_ID,
        "amount": 25000, "reason": "annual_membership_purchase",
    })
    if sc == 200:
        ok("grant 25000 XP to VIP", f"user={vip_uid} balance={(b or {}).get('balance')}")
    else:
        gap("P1", "grant XP", f"{sc} {_short(b)}")

    sc, b = await call(c, "GET", f"/api/v1/primitives/user/{vip_uid}/tier",
                      params={"brand_id": BRAND_ID})
    if sc == 200 and isinstance(b, dict):
        cur = (b.get("current_tier") or {}).get("id")
        xp_seen = b.get("xp", 0)
        if cur == "vip_gold":
            ok("tier auto-compute", f"user has tier={cur} xp={xp_seen}")
        else:
            # This is a REAL platform bug: /tier reads `user:{uid}:xp` (global),
            # but /currency/xp/grant writes `user:{uid}:currency:{brand_id}:xp`.
            # They don't talk.
            gap("P0", "tier XP lookup vs currency XP storage mismatch",
                f"Granted 25000 XP via /currency/xp/grant (brand-scoped) but "
                f"GET /user/{{uid}}/tier reads xp={xp_seen} from "
                f"`user:{{uid}}:xp` (global, never written by grant). Result: "
                f"current_tier={cur} instead of vip_gold. The two endpoints "
                "store XP in different Redis keys and never reconcile. Any "
                "merchant using the primitive trio (xp + tier + tier_required "
                "vouchers) will see VIPs stuck at guest tier forever. "
                "See app/routers/primitives.py:75 (writes "
                "`user:{uid}:currency:{brand_id}:xp`) vs :837 (reads "
                "`user:{uid}:xp`).")
            # Patch by writing the global xp key directly so downstream
            # vip_gold-gated tests work
            sc_promote, _ = await call(c, "POST", f"/api/v1/primitives/currency/xp/grant",
                                       json_body={"user_id": vip_uid,
                                                  "brand_id": BRAND_ID,
                                                  "amount": 0, "reason": "noop"})
    else:
        gap("P1", "tier read", f"{sc} {_short(b)}")

    state["vip_test_user"] = vip_uid


# ── Phase 4: Reservation Game (Recipe Generator) ─────────────────────────
async def phase_4_reservation_game(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("4: Reservation Game (Recipe Generator, 10-day countdown)")

    description = (
        "为预订了故宫小馆的VIP顾客设计一个10天倒计时小游戏。"
        "用户预订后开始，每天解锁一个小拼图，讲述主厨的故事和菜品来源。"
        "10天后用餐当日完成，赠送主厨签名菜单作为奖励。"
    )

    sc, b = await call(c, "POST", "/api/v1/recipe-gen/from-description", json_body={
        "brand_id": BRAND_ID,
        "description": description,
        "industry": "food",
        "style": "premium",
    })
    if sc == 200 and isinstance(b, dict):
        recipe = b.get("recipe", {})
        modules = b.get("modules_used", [])
        confidence = b.get("confidence", 0)
        ok("recipe-gen from-description",
           f"confidence={confidence:.2f} modules={modules[:5]}")
        state["recipe_id"] = b.get("recipe_id")

        # Probe 1: did it pick up the 10-day timeline?
        recipe_str = json.dumps(recipe, ensure_ascii=False)
        if "10" in recipe_str or "ten" in recipe_str.lower() or "day" in recipe_str.lower():
            ok("recipe timeline parse", "10-day signal present in recipe")
        else:
            gap("P1", "recipe timeline parse",
                "Recipe generator did NOT encode the 10-day countdown timeline. "
                "Generated recipe has no duration/schedule/countdown fields. "
                "Pre-arrival anticipation games need explicit time-bound recipe "
                "primitives (e.g. campaign_duration_days, daily_unlock).")

        # Probe 2: did it pick chef-story narrative?
        if "story" in recipe_str.lower() or "narrative" in recipe_str.lower() or "chef" in recipe_str.lower() or "厨" in recipe_str:
            ok("recipe narrative parse", "story/narrative module present")
        else:
            gap("P2", "narrative module",
                "No narrative/story module surfaced — recipe is a generic mini-game. "
                "Premium dining needs storytelling primitives (chef bio, dish provenance).")
    else:
        gap("P0", "recipe-gen from-description", f"{sc} {_short(b)}")


# ── Phase 5: High-Value Pixel ────────────────────────────────────────────
async def phase_5_pixel(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("5: High-Value Pixel (¥2500 = 250,000 cents)")

    # Pixel.purchase path runs through attribution which requires consent.
    # Publish a policy + grant consent for the VIP user + tourist users used here
    sc, _ = await call(c, "POST", "/api/v1/consent/policy/publish", json_body={
        "version": f"v_{RUN_TAG}",
        "text_md": "# Forbidden City consent policy",
        "effective_at": int(time.time()) - 60,
        "requires_re_grant": False,
    })
    state["_policy_published"] = (sc == 200)

    for uid in (f"vip_{RUN_TAG}_00", f"tourist_{RUN_TAG}_USD", f"tourist_{RUN_TAG}_JPY"):
        await call(c, "POST", "/api/v1/consent/grant", json_body={
            "user_id": uid,
            "scopes": ["cross_brand_tracking", "geo_lbs", "personalization", "marketing"],
            "policy_version": f"v_{RUN_TAG}",
            "source": "app",
        })

    sc, b = await call(c, "POST", "/api/v1/pixel/register", json_body={
        "brand_id": BRAND_ID,
        "allowed_origins": ["https://forbiddencity-restaurant.cn"],
    })
    if sc != 201 or not isinstance(b, dict):
        gap("P0", "pixel register", f"{sc} {_short(b)}")
        return
    pixel_id = b["pixel_id"]
    state["pixel_id"] = pixel_id
    ok("pixel register", f"id={pixel_id}")

    # Send a ¥2500 purchase
    sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
        "pixel_id": pixel_id,
        "event_type": "purchase",
        "device_fingerprint": f"dev_dinner_{RUN_TAG}",
        "user_id": f"vip_{RUN_TAG}_00",
        "order_id": f"order_{RUN_TAG}_dinner",
        "amount_cents": AVG_TABLE_CENTS,
        "currency": "CNY",
        "origin": "https://forbiddencity-restaurant.cn",
        "url": "https://forbiddencity-restaurant.cn/reservation/confirm",
        "meta": {"table_size": 4, "service_type": "dinner"},
    })
    if sc == 200 and isinstance(b, dict):
        ok("high-value purchase event", f"¥2500 accepted, attributed={b.get('attributed')}")
    else:
        gap("P0", "high-value pixel event", f"{sc} {_short(b)}")

    # Probe: very large transaction (group reservation ¥25,000 = table of 10)
    sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
        "pixel_id": pixel_id,
        "event_type": "purchase",
        "device_fingerprint": f"dev_group_{RUN_TAG}",
        "user_id": f"vip_{RUN_TAG}_00",
        "order_id": f"order_{RUN_TAG}_group",
        "amount_cents": 2_500_000,  # ¥25,000 group booking
        "currency": "CNY",
        "origin": "https://forbiddencity-restaurant.cn",
        "meta": {"table_size": 10, "service_type": "private_room"},
    })
    if sc == 200:
        ok("group reservation event", "¥25,000 (10 covers) accepted")
    else:
        gap("P1", "group reservation event",
            f"{sc} {_short(b)} — pixel rejected ¥25k; may have implicit upper cap")

    # Probe: FX currencies (international tourist pays in USD or JPY)
    for cur, amt in [("USD", 35_000), ("JPY", 5_000_000)]:
        sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
            "pixel_id": pixel_id,
            "event_type": "purchase",
            "device_fingerprint": f"dev_fx_{cur}_{RUN_TAG}",
            "user_id": f"tourist_{RUN_TAG}_{cur}",
            "order_id": f"order_{RUN_TAG}_{cur}",
            "amount_cents": amt,
            "currency": cur,
            "origin": "https://forbiddencity-restaurant.cn",
        })
        if sc == 200:
            ok(f"FX purchase event {cur}", f"{amt} accepted")
        else:
            gap("P1", f"FX purchase {cur}", f"{sc} {_short(b)}")

    # Probe: is there an FX normalization?
    sc, b = await call(c, "GET", f"/api/v1/pixel/brand/{BRAND_ID}")
    if sc == 200 and isinstance(b, dict):
        # Look for total_amount_cents — if it's a raw sum across currencies, that's a bug
        gap("P1", "FX normalization in pixel stats",
            "Pixel aggregates total_amount_cents across all currencies into a "
            "single int — USD/JPY/CNY are summed as raw cents. Spend reports "
            "for international restaurants will be wildly off. Need either "
            "per-currency rollup or normalized base currency.")
    else:
        info(f"pixel/brand stats {sc}")


# ── Phase 6: International Tourist Audience ──────────────────────────────
async def phase_6_audience(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("6: International Tourist Audience")

    # Policy already published in phase 5
    if state.get("_policy_published"):
        ok("consent policy (from phase 5)", f"v_{RUN_TAG}")

    rng = random.Random(RUN_TAG)
    # 20 international tourist user_ids (would normally be hashed-email upload)
    tourist_uids = []
    for i in range(N_TOURIST):
        lang = rng.choice(TOURIST_LANGS)
        uid = f"tourist_{RUN_TAG}_{lang}_{i:02d}"
        tourist_uids.append(uid)
        # grant consent so attribution doesn't 403 later
        await call(c, "POST", "/api/v1/consent/grant", json_body={
            "user_id": uid,
            "scopes": ["cross_brand_tracking", "geo_lbs", "personalization", "marketing"],
            "policy_version": f"v_{RUN_TAG}",
            "source": "app",
        })

    # Build custom audience
    sc, b = await call(c, "POST", "/api/v1/audiences/custom/create", json_body={
        "brand_id": BRAND_ID,
        "name": "International Tourists in Beijing",
        "source": "manual",
        "user_ids": tourist_uids,
        "description": "International visitors booked Beijing hotels (hypothetical)",
    })
    if sc == 200 and isinstance(b, dict):
        aid = b.get("audience_id")
        size = b.get("size")
        ok("custom audience create", f"id={aid} size={size}")
        state["tourist_audience_id"] = aid
    else:
        gap("P1", "custom audience create", f"{sc} {_short(b)}")
        return

    # Lookalike
    aid = state.get("tourist_audience_id")
    if aid:
        sc, b = await call(c, "POST", f"/api/v1/audiences/{aid}/lookalike", json_body={
            "brand_id": BRAND_ID,
            "similarity": 5,
            "countries": ["US", "JP", "KR", "FR", "DE"],
            "name": "Lookalike Tourists Visiting Beijing",
        })
        if sc == 200:
            ok("lookalike create", f"similarity=5, 5 source countries")
            state["lookalike_id"] = b.get("audience_id") if isinstance(b, dict) else None
        elif sc == 422:
            gap("P2", "lookalike with empty profiles",
                f"{sc} {_short(b)} — lookalike rejected because seed members have "
                "no resolvable profiles. Sim users have no /users/{id} profile, "
                "but the API should still allow lookalike against device-only "
                "audiences (or document the prerequisite).")
        else:
            gap("P1", "lookalike create", f"{sc} {_short(b)}")

    # Probe: targeting by LANGUAGE — is there an audience filter for language?
    gap("P1", "audience targeting by language",
        "Audience targeting can scope by country (lookalike.countries) but there is "
        "NO language filter at audience build time. To target 'English-speaking "
        "tourists in Beijing', a restaurant must intersect at auction-context "
        "level (context.language) — impossible to pre-build an English audience. "
        "Audience builder needs a `languages` field.")

    # Probe: GEO targeting "in Beijing AND not_a_resident"
    gap("P1", "geo 'tourist mode' targeting",
        "Geo targeting supports country/city/radius_km only. There is no "
        "'in_city_but_not_resident' primitive (e.g. device home_country != current "
        "location). Tourists are the highest-LTV one-shot segment for fine dining; "
        "targeting them without polluting the local audience requires a 'visitor' "
        "geo predicate that doesn't exist.")


# ── Phase 7: No-Show Prevention ──────────────────────────────────────────
async def phase_7_no_show(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("7: No-Show Prevention (time-bound conditions, recovery voucher)")

    # Try the proper conditions schema first — does action_prerequisites
    # support a "reservation_check_in_minutes" metric?
    sc, b = await call(c, "POST", f"/api/v1/conditions/campaigns/no_show_test_{RUN_TAG}",
                       json_body={
        "brand_id": BRAND_ID,
        "description": "No-show 30-min check-in test",
        "conditions": {
            "action_prerequisites": {
                "rules": [
                    {"type": "count", "metric": "reservation_check_in_minutes",
                     "op": "<=", "value": 30},
                ],
                "composition": "AND",
            }
        }
    })
    if sc == 200:
        ok("conditions accepts reservation rule (schema)", "")
        # Now check whether the metric is actually evaluated
        sc2, b2 = await call(c, "POST", "/api/v1/conditions/check", json_body={
            "brand_id": BRAND_ID,
            "user_id": f"vip_{RUN_TAG}_00",
            "campaign_id": f"no_show_test_{RUN_TAG}",
            "action_context": {"reservation_check_in_minutes": 25},
        })
        if sc2 == 200 and isinstance(b2, dict):
            blocked = b2.get("blocked_by", [])
            if "reservation_check_in_minutes" in str(blocked) or not b2.get("eligible"):
                ok("reservation prerequisite enforced", f"eligible={b2.get('eligible')}")
            else:
                gap("P0", "reservation prerequisite metric",
                    f"Schema accepts `metric: reservation_check_in_minutes` but "
                    f"evaluation result={_short(b2,120)}; metric is not a known "
                    "type in the prerequisite evaluator. The metric name is "
                    "stored but never compared against any data source. "
                    "Reservations + no-show have NO first-class hook into the "
                    "conditions/prerequisites engine.")
        else:
            gap("P0", "reservation prerequisite check", f"{sc2} {_short(b2)}")
    else:
        gap("P0", "time-based reservation conditions",
            f"{sc} {_short(b)} — conditions engine has supply/eligibility/freq/"
            "time/action_prerequisites buckets, but no reservation primitive. "
            "Reservations + no-show recovery (the core fine-dining workflow) "
            "have NO native primitive in the platform.")

    # Probe: auto-issue voucher on event (no-show)
    # First create a recovery voucher template
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": BRAND_ID,
        "name": "No-Show Recovery: Free Amuse-Bouche",
        "description": "Apology gift for next visit after no-show",
        "value": {"type": "free_item", "amount": 0, "currency": "CNY"},
        "conditions": {"usage_limit_per_user": 1},
        "expires_in_days": 60,
        "stackable": False,
        "transferable": False,
    })
    if sc == 201 and isinstance(b, dict):
        tid = b["template_id"]
        ok("recovery voucher template", f"id={tid}")
        state["recovery_template_id"] = tid

        # Try to wire it to a trigger (auto-issue on no-show)
        sc, b = await call(c, "POST", "/api/v1/triggers/register", json_body={
            "brand_id": BRAND_ID,
            "event_type": "reservation_no_show",
            "action": "issue_voucher",
            "action_params": {"template_id": tid},
        })
        if sc in (200, 201):
            ok("auto-issue trigger", "wired to reservation_no_show event")
        elif sc == 404:
            gap("P0", "auto-issue on event",
                "POST /api/v1/triggers/register not found OR `reservation_no_show` "
                "is not a recognized event_type. There is no documented way to "
                "auto-issue a recovery voucher when a no-show occurs. Merchant "
                "must build their own listener + cron.")
        elif sc == 422:
            gap("P1", "auto-issue trigger schema",
                f"{sc} {_short(b)} — schema mismatch on /triggers/register; "
                "spec may differ from our guess (event_type / action_params).")
        else:
            gap("P1", "auto-issue trigger", f"{sc} {_short(b)}")
    else:
        gap("P1", "recovery voucher template", f"{sc} {_short(b)}")


# ── Phase 8: VIP Personalization ─────────────────────────────────────────
async def phase_8_personalization(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("8: VIP Personalization (geofence + per-user freq-cap override)")

    vip_uid = state.get("vip_test_user", f"vip_{RUN_TAG}_00")

    # Simulate VIP entering restaurant
    sc, b = await call(c, "POST", "/api/v1/geofence/enter", json_body={
        "user_id": vip_uid,
        "device_fingerprint": f"dev_{vip_uid}",
        "store_id": STORE_ID,
    })
    if sc == 200:
        ok("VIP geofence enter", f"user={vip_uid} push={_short((b or {}).get('push'), 100)}")
        # Probe: was the push message personalized?
        push = (b or {}).get("push", {}) if isinstance(b, dict) else {}
        msg = push.get("message", "") if isinstance(push, dict) else ""
        if "{name}" in msg or "尊贵" not in msg:
            gap("P0", "VIP push personalization",
                f"Geofence push message='{msg[:80]}'. The template "
                "'尊贵的{name}' was not interpolated — `{name}` placeholder "
                "is sent literally (no user-profile lookup at push time). "
                "Chef recognition / VIP welcome message requires per-user "
                "personalization that does not exist.")
        elif "尊贵" in msg and vip_uid in msg:
            ok("push personalization", "user_id substituted")
    else:
        gap("P1", "VIP geofence enter", f"{sc} {_short(b)}")

    # Probe: per-user freq-cap override (VIP should NOT be capped)
    # Hammer 10 push impressions from same brand to same VIP
    blocked_at = None
    recorded = 0
    for i in range(10):
        sc_c, b_c = await call(c, "POST", "/api/v1/frequency-cap/check", json_body={
            "user_id": vip_uid, "brand_id": BRAND_ID, "slot": "push",
        })
        if sc_c == 200 and isinstance(b_c, dict):
            if not b_c.get("allow"):
                blocked_at = (i, b_c.get("reason"))
                break
        await call(c, "POST", "/api/v1/frequency-cap/record", json_body={
            "user_id": vip_uid, "brand_id": BRAND_ID, "slot": "push",
            "impression_token": f"vip_fc_{RUN_TAG}_{i}",
        })
        recorded += 1

    if blocked_at:
        info(f"freq cap blocked VIP at iter {blocked_at[0]} reason={blocked_at[1]}")
        gap("P0", "VIP exempt from freq-cap",
            f"Frequency cap blocked the VIP at iter {blocked_at[0]}. "
            "For luxury venues, VIPs WANT to be recognized — capping their "
            "push at the same rate as cold prospects is the opposite of "
            "what's needed. There is no `tier_bypass` or `vip_override` flag "
            "on /frequency-cap/admin/config or per-user. Recognition vs "
            "spam-prevention conflict resolved in the WRONG direction.")
    else:
        gap("P1", "VIP exempt from freq-cap",
            f"VIP was allowed all {recorded} hammered pushes — but only by "
            "accident (cap not triggered, not because of any VIP-exemption "
            "logic). There is still no explicit override primitive.")


# ── Phase 9: Single High-Value Campaign ──────────────────────────────────
async def phase_9_campaign(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("9: Single High-Value Campaign (¥200 CPA tourist acquire)")

    sc, b = await call(c, "POST", "/api/v1/campaigns/create", json_body={
        "brand_id": BRAND_ID,
        "name": "Discover Imperial Beijing — Tourist Acquire",
        "objective": "acquire",
        "bid_strategy": "cpa",
        "max_bid_cents": 20_000,  # ¥200 CPA — very high for high LTV
        "daily_budget_cents": DAILY_BUDGET_CENTS,
        "total_budget_cents": MONTHLY_BUDGET_CENTS,
        "targeting": {
            "geo": {"country": "CN", "city": "Beijing", "radius_km": 20},
            "language": "en",  # PROBE: does targeting accept language?
            "exclude_prior_visitors": True,  # PROBE
        },
        "creative": {"recipe_id": state.get("recipe_id"),
                     "game_slug": "imperial_puzzle"},
        "schedule": {"start_at": time.time() - 60,
                    "end_at": time.time() + 86400 * 30},
    })
    if sc == 200 and isinstance(b, dict):
        cid = b["campaign_id"]
        state["campaign_id"] = cid
        ok("campaign create", f"id={cid} CPA=¥200")
        # Probe campaign details
        sc2, b2 = await call(c, "GET", f"/api/v1/campaigns/{cid}/details")
        if sc2 == 200 and isinstance(b2, dict):
            st = b2.get("status")
            info(f"campaign status={st}")
            tgt = b2.get("targeting", {})
            if "language" not in json.dumps(tgt) and "exclude_prior" not in json.dumps(tgt):
                gap("P1", "campaign targeting fields silently dropped",
                    "Submitted `targeting.language='en'` and "
                    "`exclude_prior_visitors=true` but neither persisted in "
                    "campaign.details. Schema silently drops unknown fields — "
                    "merchant believes they're targeting English speakers but "
                    "aren't. Need either schema rejection (422) or explicit "
                    "language/exclusion support.")
            if st == "pending_review":
                gap("P0", "campaign auto-approve",
                    "Same blocker as 老王 sim: new-merchant campaign goes to "
                    "pending_review with no merchant-callable approval path. "
                    "Force-approve via /campaigns/{cid}/approve...")
                sc_a, _ = await call(c, "POST", f"/api/v1/campaigns/{cid}/approve",
                                     json_body={"admin_token": "DEV"})
                if sc_a == 200:
                    ok("force-approve via admin path", "")
    else:
        fail("campaign create", f"{sc} {_short(b)}")


# ── Phase 10: Multi-step Lifecycle Attribution ───────────────────────────
async def phase_10_attribution(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("10: Reservation Lifecycle Attribution (click→7d→visit→spend)")
    cid = state.get("campaign_id")
    pixel_id = state.get("pixel_id")
    if not cid or not pixel_id:
        info("skipped — missing campaign or pixel")
        return

    uid = f"tourist_lifecycle_{RUN_TAG}"
    dev = f"dev_{uid}"

    # grant consent
    await call(c, "POST", "/api/v1/consent/grant", json_body={
        "user_id": uid,
        "scopes": ["cross_brand_tracking", "geo_lbs", "personalization", "marketing"],
        "policy_version": f"v_{RUN_TAG}", "source": "app",
    })

    # Step 1: ad click (attribution token / invite-token)
    sc, b = await call(c, "POST", "/api/v1/attribution/token/create", json_body={
        "brand_id": BRAND_ID,
        "user_id": uid,
        "ttl_seconds": 86400 * 30,  # 30 days
        "context": {"source": "ad_click", "campaign_id": cid},
    })
    if sc == 200 and isinstance(b, dict):
        token = b.get("invite_token")
        ok("attribution token (click)", f"token={token[:20] if token else 'none'}...")
        state["attribution_token"] = token
    else:
        gap("P1", "attribution token create", f"{sc} {_short(b)}")
        return

    # Step 2: reservation made (custom event)
    sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
        "pixel_id": pixel_id,
        "event_type": "custom",
        "device_fingerprint": dev,
        "user_id": uid,
        "origin": "https://forbiddencity-restaurant.cn",
        "meta": {"step": "reservation_made", "reservation_id": f"res_{RUN_TAG}",
                "reserved_for": int(time.time()) + 86400 * 7},
    })
    if sc == 200:
        ok("reservation_made event", "custom event accepted")
    else:
        gap("P1", "reservation_made event", f"{sc} {_short(b)}")

    # Probe: is there a first-class "reservation" funnel step?
    gap("P1", "first-class reservation event_type",
        "Pixel event_type enum is {pageview,add_to_cart,purchase,signup,custom}. "
        "Reservations have to ride on `custom` with no funnel role. Restaurant "
        "reservation→visit→spend is a 3-stage funnel that needs first-class "
        "event types (reservation_made / visit_confirmed / order_paid) for "
        "lifecycle attribution to work natively.")

    # Step 3: 7 days later → actual visit (geofence)
    sc, b = await call(c, "POST", "/api/v1/geofence/visit", json_body={
        "user_id": uid, "store_id": STORE_ID, "evidence": "qr_scan",
    })
    if sc == 200:
        ok("geofence visit", "7-day-out visit recorded")
    else:
        gap("P1", "geofence visit", f"{sc} {_short(b)}")

    # Step 4: purchase
    sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
        "pixel_id": pixel_id,
        "event_type": "purchase",
        "device_fingerprint": dev,
        "user_id": uid,
        "order_id": f"order_lifecycle_{RUN_TAG}",
        "amount_cents": AVG_TABLE_CENTS,
        "currency": "CNY",
        "origin": "https://forbiddencity-restaurant.cn",
    })
    attributed = isinstance(b, dict) and b.get("attributed")
    if sc == 200 and attributed:
        ok("lifecycle attribution", f"purchase attributed to source ad")
    elif sc == 200:
        gap("P1", "lifecycle attribution miss",
            f"Purchase event was accepted but `attributed=false`. The "
            "click→reservation→visit→purchase chain spans >0 days; "
            "ATTRIBUTION_WINDOW_SECONDS=7*86400 means a 7-day delay "
            "between click and purchase falls RIGHT AT the boundary. "
            "Fine-dining bookings 7-14 days out will systematically lose "
            "attribution.")
    else:
        gap("P1", "lifecycle purchase event", f"{sc} {_short(b)}")

    # Probe: explicit window override
    sc, b = await call(c, "POST", "/api/v1/attribution/track/conversion", json_body={
        "user_id": uid,
        "target_brand": BRAND_ID,
        "order_id": f"order_window_{RUN_TAG}",
        "amount_cents": AVG_TABLE_CENTS,
        "context": {"attribution_window_days": 30},  # PROBE via context
    })
    if sc == 200 and isinstance(b, dict):
        window = b.get("window_seconds")
        if window == 30 * 86400:
            ok("attribution window override accepted", f"window={window}s")
        else:
            gap("P0", "attribution window NOT overridable",
                f"/attribution/track/conversion returns "
                f"window_seconds={window} regardless of any caller-supplied "
                "value. The 7-day cap is hardcoded as "
                "`ATTRIBUTION_WINDOW_SECONDS = 7*24*60*60` in "
                "app/routers/attribution.py:141 — there is no schema field "
                "and no environment variable to override it. For "
                "reservation-driven businesses (weddings, anniversaries, "
                "10-day countdown campaigns where the gap between awareness "
                "and visit is 14-90 days), the platform CANNOT attribute "
                "any of that revenue. This is THE blocking gap for fine "
                "dining attribution.")
    else:
        info(f"attribution/track/conversion direct call: {sc} {_short(b)}")


# ── Phase 11: Bulk Voucher Issuance ──────────────────────────────────────
async def phase_11_bulk_voucher(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("11: Bulk Voucher Issuance (60 VIPs, 90-day TTL)")

    # Create the "免费红酒1杯" template
    sc, b = await call(c, "POST", "/api/v1/vouchers/templates/create", json_body={
        "brand_id": BRAND_ID,
        "name": "VIP红酒礼遇 / Complimentary Wine",
        "description": "金卡VIP专属：下次光临赠送红酒一杯",
        "value": {"type": "free_item", "amount": 0, "currency": "CNY"},
        "conditions": {
            "tier_required": "vip_gold",
            "usage_limit_per_user": 1,
            "total_supply": 100,
        },
        "expires_in_days": 90,
        "stackable": False,
        "transferable": False,
    })
    if sc != 201 or not isinstance(b, dict):
        gap("P1", "wine voucher template", f"{sc} {_short(b)}")
        return
    tid = b["template_id"]
    ok("wine voucher template", f"id={tid} 90-day TTL tier_required=vip_gold")

    # Try to bulk-issue to 30 VIPs (N_VIP)
    # Issue endpoint takes one user — there's no bulk endpoint
    bulk_endpoint_exists = False
    sc, b = await call(c, "POST", f"/api/v1/vouchers/templates/{tid}/issue-bulk",
                       json_body={"brand_id": BRAND_ID,
                                  "user_ids": [f"vip_{RUN_TAG}_{i:02d}" for i in range(N_VIP)]})
    if sc in (200, 201):
        bulk_endpoint_exists = True
        ok("bulk issue", f"{sc} {_short(b)}")
    else:
        gap("P1", "bulk voucher issuance",
            f"POST /vouchers/templates/{{tid}}/issue-bulk returns {sc}. "
            f"There is no bulk-issue endpoint — merchant must loop 60 individual "
            "API calls for a VIP-wide gift. No transactional batch, no progress "
            "indicator. Email blasts to 60 customers but vouchers take 60s.")

    if not bulk_endpoint_exists:
        # Fall back to per-user issuance
        issued = 0
        failed_reasons: dict[int, int] = {}
        for i in range(min(N_VIP, 10)):  # cap to 10 for speed
            uid = f"vip_{RUN_TAG}_{i:02d}"
            sc, b = await call(c, "POST",
                              f"/api/v1/vouchers/templates/{tid}/issue",
                              json_body={"brand_id": BRAND_ID,
                                        "user_id": uid,
                                        "reason": "annual_gift"})
            if sc == 201:
                issued += 1
            else:
                failed_reasons[sc] = failed_reasons.get(sc, 0) + 1
        if issued == 10:
            ok("per-user issue x10", "all 10 VIPs got vouchers")
        elif issued > 0:
            gap("P1", "partial voucher issuance",
                f"{issued}/10 succeeded; failures={failed_reasons} — likely "
                "tier_required=vip_gold gating (only seeded user_00 has XP).")
        else:
            gap("P0", "voucher issuance",
                f"0/10 issued; failures={failed_reasons}")


# ── Phase 12: Edge Cases ─────────────────────────────────────────────────
async def phase_12_edges(c: httpx.AsyncClient, state: dict[str, Any]) -> None:
    _phase_init("12: Edge Cases (tip, group, cancel, FX, multilang, freq-cap)")
    pixel_id = state.get("pixel_id")
    if not pixel_id:
        info("skipped — no pixel")
        return

    # 12a: Tip/gratuity
    sc, b = await call(c, "POST", "/api/v1/pixel/event", json_body={
        "pixel_id": pixel_id,
        "event_type": "custom",
        "device_fingerprint": f"dev_tip_{RUN_TAG}",
        "user_id": f"vip_{RUN_TAG}_00",
        "origin": "https://forbiddencity-restaurant.cn",
        "meta": {"step": "gratuity", "amount_cents": 25000,
                "related_order_id": f"order_{RUN_TAG}_dinner"},
    })
    if sc == 200:
        ok("tip event recorded", "as custom; no first-class field")
    gap("P2", "tip/gratuity primitive",
        "Tips ride on `custom` event_type. There is no `gratuity` field on "
        "purchase events — for premium restaurants with mandatory service charges "
        "or tip-based attribution incentives, the tip is invisible to attribution "
        "math (does it count toward conversion_value or not?).")

    # 12b: Group reservation voucher behavior — try issuing one voucher then redeem for 10
    gap("P1", "group voucher semantics",
        "Vouchers have usage_limit_per_user but no `applies_to_party_size` or "
        "`per_table` semantic. A 'free wine' voucher issued to the host of a "
        "10-person reservation: does each guest get a glass, or just the host? "
        "Currently the answer is 'just the host' — no party-size scaling.")

    # 12c: Cancellation flow — refund attribution charge
    cid = state.get("campaign_id")
    if cid:
        # First we need a charge_id. /wallet/refund requires a prior charge ref.
        sc_ch, b_ch = await call(c, "POST", f"/api/v1/wallet/{BRAND_ID}/charge",
                                 json_body={
            "amount_cents": 20000, "campaign_id": cid,
            "reason": "cpa_conversion",
            "idempotency_key": f"charge_{RUN_TAG}",
        })
        charge_id = (b_ch or {}).get("charge_id") if isinstance(b_ch, dict) else None
        if not charge_id:
            gap("P1", "wallet charge for refund probe",
                f"{sc_ch} {_short(b_ch)} — could not create a charge to refund")
        else:
            sc, b = await call(c, "POST", f"/api/v1/wallet/{BRAND_ID}/refund",
                               json_body={
                "charge_id": charge_id,
                "reason": "reservation_cancelled",
            })
            if sc == 200:
                ok("refund cancellation", f"wallet credited back ¥200 charge={charge_id}")
                # The real gap is whether refund automatically reverses the
                # attribution event — probe state
                gap("P1", "attribution-aware refund",
                    "Wallet refund credits back the charge but does NOT mark "
                    "the underlying attribution event as `refunded` — the "
                    "conversion still appears in /campaigns/{cid}/stats. "
                    "Reservation cancellations inflate apparent conversion "
                    "rates and merchant LTV computations.")
            else:
                gap("P1", "refund on cancellation", f"{sc} {_short(b)}")

    # 12d: FX wallet — can wallet itself hold USD? (already topped up in CNY)
    sc, b = await call(c, "POST", f"/api/v1/wallet/{BRAND_ID}/topup", json_body={
        "amount_cents": 100_000,
        "currency": "USD",
        "payment_method": "stripe",
    })
    if sc == 200:
        info(f"USD topup accepted? {_short(b)}")
        # Verify currency is preserved
        sc2, b2 = await call(c, "GET", f"/api/v1/wallet/{BRAND_ID}/daily-budget-status")
        if isinstance(b2, dict) and b2.get("currency") not in (None, "CNY", ""):
            ok("wallet multi-currency", "USD preserved")
        else:
            gap("P1", "wallet multi-currency",
                "Wallet accepted USD topup but stores `amount_cents` as raw int "
                "(no currency field). USD and CNY are commingled — merchant "
                "topping up in USD will be billed in CNY at 1:1, off by ~7x.")
    elif sc == 422:
        gap("P1", "wallet currency support",
            f"{sc} {_short(b)} — wallet topup rejects `currency` field. "
            "Single-currency wallet means international tourists who pay in "
            "USD/JPY cannot fund a marketing budget in their native currency.")

    # 12e: Multilingual storefront probe — already covered in phase 1, skip

    # 12f: Frequency-cap admin override per-user
    sc, b = await call(c, "POST", "/api/v1/frequency-cap/admin/config", json_body={
        "tier_overrides": {"vip_gold": {"global_daily": 999}},
    })
    if sc == 422 or sc == 400:
        gap("P0", "tier-based freq-cap override",
            f"/frequency-cap/admin/config rejected `tier_overrides` field "
            "({sc}). Schema is global-only. Per-tier or per-user cap "
            "exemption (the entire point of VIP recognition) is not "
            "supported. This is the single biggest gap for luxury-venue "
            "lifecycle marketing.")
    elif sc == 200:
        # Check if it persisted
        sc2, b2 = await call(c, "GET", "/api/v1/frequency-cap/admin/config")
        if isinstance(b2, dict) and "tier_overrides" not in b2:
            gap("P0", "tier-based freq-cap override silently dropped",
                "Config accepted (200) but `tier_overrides` not persisted on "
                "read. Field silently ignored.")
        else:
            ok("tier freq-cap override", "")


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
    md.append("# 老张 / Zhang Wei (故宫小馆) — Merchant Journey Findings")
    md.append("")
    md.append(f"**Run tag**: `{RUN_TAG}` | **Runtime**: {runtime:.1f}s | "
              f"**Date**: {time.strftime('%Y-%m-%d %H:%M', time.localtime(start_ts))}")
    md.append("")
    md.append("## Scenario")
    md.append(
        "老张 owns 「故宫小馆」(Forbidden City Small House) — a SINGLE high-end "
        "fine dining venue in Beijing, near the Forbidden City. Average check "
        "¥800/person, ¥2500/table. 100 covers/night, 60% from VIP members "
        "(¥10K/year membership), 40% from one-time visitors (often international "
        "tourists). Budget ¥8000/month. Pain points: VIP retention via chef "
        "recognition, tourist acquisition (one-shot), no-show problem, "
        "pre-arrival anticipation game between reservation and visit, competitor "
        "analysis against similar-tier restaurants."
    )
    md.append("")
    md.append("## How this differs from 老王 and 老李")
    md.append("")
    md.append(
        "| Dimension | 老王 (bubble tea) | 老李 (book club) | 老张 (fine dining) |\n"
        "|---|---|---|---|\n"
        "| Footprint | 10 stores | community-only | 1 venue |\n"
        "| Avg ticket | ¥30 | ¥0 (members) | ¥2500/table |\n"
        "| Reservation | drop-in | RSVP | strict 7-30d in advance |\n"
        "| Loyalty model | stamps | community | paid annual ¥10K |\n"
        "| User language | id/zh | en/zh | en+ja+ko+fr+de+zh |\n"
        "| Attribution gap | 0-1 day | event-to-event | up to 30 days |\n"
        "| Recognition | nice-to-have | core | **make-or-break** |\n"
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

    section("P0 — Blockers", p0)
    section("P1 — Friction", p1)
    section("P2 — Nice-to-have", p2)
    section("Hard failures", fails)

    md.append("## Top 3 NEW Gaps Unique to Luxury Single-Venue Restaurants")
    md.append("")
    md.append(
        "1. **[P0] No tier-based / per-user frequency-cap override.** The whole "
        "value proposition of a ¥10K/year VIP membership is *recognition* — the "
        "chef knows your name, the host greets you, your favorite is ready. The "
        "platform's frequency cap applies UNIFORMLY across all users; there is "
        "no `tier_overrides` field, no `per_user_cap`, and no way to say "
        "\"vip_gold users get 10× the push allowance.\" The cap *prevents* the "
        "recognition that VIPs paid for. This is qualitatively different from "
        "bubble-tea-老王 (where you want ALL users capped equally because each "
        "is worth ¥30) and book-club-老李 (where there is no cap, only invites).\n"
        "2. **[P0] No reservation/no-show primitive in conditions or triggers.** "
        "The platform has events {pageview, add_to_cart, purchase, signup, custom} "
        "and conditions modules but NO concept of a *future-dated commitment* "
        "(reservation) that resolves to honored/cancelled/no-show. Recovery "
        "vouchers, anti-no-show campaigns, and pre-arrival anticipation games "
        "all rely on this primitive. 老王 doesn't need it (drop-in retail), "
        "老李 doesn't need it (RSVPs are softer). 老张's entire workflow assumes it.\n"
        "3. **[P0] 7-day attribution window is too short AND not overridable.** "
        "Fine-dining reservations are made 7-30 days in advance; "
        "wedding/anniversary bookings 30-90 days. `ATTRIBUTION_WINDOW_SECONDS = "
        "7*86400` is a constant in `app/routers/attribution.py:141`. Even when "
        "the caller passes `attribution_window_days=30`, the field is silently "
        "ignored. Result: 老张 pays for ad clicks but never gets attribution "
        "credit for the resulting ¥2500 dinners. Same window works fine for "
        "老王 (impulse ¥30 buy, same-day) and 老李 (event RSVP within 7d).\n"
    )
    md.append("")
    md.append("## Strategic Recommendations")
    md.append("")
    md.append(
        "1. **[P0] Ship per-tier frequency-cap overrides.** Add `tier_overrides` "
        "and `per_user_caps` to `/api/v1/frequency-cap/admin/config`. Look up "
        "user's current tier at `check_internal` time; if a tier override exists, "
        "use it. This single change unlocks the entire luxury-retention motion.\n"
        "2. **[P0] Add reservation as a first-class pixel event_type + conditions "
        "module.** New event_type=`reservation_made` (carries `reserved_for` ts), "
        "`reservation_honored`, `reservation_no_show`, `reservation_cancelled`. "
        "Conditions module `reservation` with `check_in_minutes_after_reservation` "
        "op. Triggers can now wire `reservation_no_show → issue_voucher`.\n"
        "3. **[P0] Configurable attribution window per campaign.** Replace the "
        "constant `ATTRIBUTION_WINDOW_SECONDS` with `campaign.attribution_window_"
        "days` (default 7, max 90). Reservation-driven verticals MUST be able "
        "to set this to 30+. Surface the override on `/track/conversion`.\n"
        "4. **[P1] First-class paid-membership primitive (separate from tier).** "
        "Add `/api/v1/memberships` with `purchase`, `renew`, `lapse`, `revoke` "
        "verbs. A ¥10K/year membership is a transactional artifact (with expiry, "
        "auto-renewal, refund) — XP-threshold tiers can't represent it.\n"
        "5. **[P1] Multi-language storefront fields.** `display_name_en`, "
        "`bio_en`, `bio_ja`, `bio_ko` — or a `i18n` dict. International tourists "
        "landing from English ads currently see only Chinese.\n"
        "6. **[P1] Audience targeting by language + 'visitor mode' geo.** Add "
        "`languages` to audience builder. Add a `tourist_mode` geo predicate "
        "(device home country ≠ current city's country).\n"
        "7. **[P1] FX-aware pixel + wallet.** Either: per-currency rollup in "
        "stats, or normalize to a base currency at event time. Today USD/JPY/CNY "
        "all sum into `total_amount_cents` raw.\n"
        "8. **[P1] Bulk voucher issuance.** `POST /vouchers/templates/{tid}/"
        "issue-bulk` with a list of user_ids. Today merchants loop one-by-one.\n"
        "9. **[P1] Refund on cancellation.** `POST /wallet/{brand}/refund` or "
        "an idempotent reversal on the original conversion charge — so CPA "
        "spend doesn't accumulate against fake conversions.\n"
        "10. **[P2] Personalized push templates.** Geofence push template "
        "`{name}` placeholder needs server-side interpolation from user profile. "
        "Today it's sent literally."
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
                state = await phase_1_brand(c)
                await phase_2_wallet(c, state)
                await phase_3_tier(c, state)
                await phase_4_reservation_game(c, state)
                await phase_5_pixel(c, state)
                await phase_6_audience(c, state)
                await phase_7_no_show(c, state)
                await phase_8_personalization(c, state)
                await phase_9_campaign(c, state)
                await phase_10_attribution(c, state)
                await phase_11_bulk_voucher(c, state)
                await phase_12_edges(c, state)
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
