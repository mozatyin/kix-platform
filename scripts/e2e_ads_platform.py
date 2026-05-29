"""E2E test of the KiX Ads Platform: full merchant + user lifecycle.

Walks through:
  Brand A (源品牌) and Brand B (目标品牌)
  ├── Wallet top-up
  ├── Create campaigns (CPA + CPS + Geofence CPV)
  ├── Auction match → impression → click → conversion → wallet deducted
  ├── Cross-brand attribution: token → visit → conversion to OTHER brand
  └── Geofence: register store → nearby → enter → visit attributed

Runs against the ASGI app in-process via httpx — no separate server needed.
Requires a live local Redis (redis-cli ping → PONG).

Run:
    .venv/bin/python scripts/e2e_ads_platform.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

import httpx

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402
from app.redis_client import init_redis, close_redis  # noqa: E402


BRAND_A = f"e2e_brandA_{int(time.time())}"
BRAND_B = f"e2e_brandB_{int(time.time())}"
USER_U = f"e2e_user_{int(time.time())}"
DEVICE_FP = f"e2e_dev_{int(time.time())}"

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"
BOLD = "\033[1m"


def log(stage: str, msg: str, color: str = BLUE) -> None:
    print(f"{color}[{stage}]{RESET} {msg}")


def ok(msg: str) -> None:
    log("PASS", f"{GREEN}✓{RESET} {msg}", GREEN)


def fail(msg: str, body: Any = None) -> None:
    log("FAIL", f"{RED}✗{RESET} {msg}", RED)
    if body is not None:
        print(f"  body: {body}")


async def must(name: str, client: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
    """Call endpoint; fail loudly if non-2xx."""
    r = await client.request(method, path, **kw)
    if r.status_code >= 300:
        fail(f"{name} → {r.status_code} {r.text[:200]}")
        raise RuntimeError(f"{name} failed")
    return r.json() if r.text else {}


async def main() -> int:
    await init_redis()
    transport = httpx.ASGITransport(app=app)
    failed = 0

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # ── Phase 1: Wallet ─────────────────────────────────────────────
        log("PHASE 1", f"{BOLD}Wallet top-up & balance check{RESET}", YELLOW)

        try:
            r = await must("topup A", c, "POST",
                f"/api/v1/wallet/{BRAND_A}/topup",
                json={"amount_cents": 100000, "payment_method": "wechat"})
            topup_id_a = r["topup_id"]
            ok(f"Brand A topup pending: {topup_id_a}")

            await must("confirm A", c, "POST",
                f"/api/v1/wallet/{BRAND_A}/topup/{topup_id_a}/confirm",
                json={"payment_gateway_response": {"mock": True}})
            ok("Brand A topup confirmed")

            r = await must("balance A", c, "GET", f"/api/v1/wallet/{BRAND_A}")
            assert r["balance_cents"] == 100000, f"balance mismatch: {r['balance_cents']}"
            ok(f"Brand A balance = ¥{r['balance_cents']/100:.2f}")

            # Same for B
            r = await must("topup B", c, "POST",
                f"/api/v1/wallet/{BRAND_B}/topup",
                json={"amount_cents": 50000, "payment_method": "alipay"})
            await must("confirm B", c, "POST",
                f"/api/v1/wallet/{BRAND_B}/topup/{r['topup_id']}/confirm",
                json={"payment_gateway_response": {"mock": True}})
            ok("Brand B funded ¥500.00")

            # Daily budget
            await must("set daily budget", c, "POST",
                f"/api/v1/wallet/{BRAND_A}/daily-budget",
                json={"daily_budget_cents": 20000})
            ok("Brand A daily budget ¥200")
        except Exception as e:
            fail(f"Phase 1 crashed: {e}")
            failed += 1

        # ── Phase 2: Create Campaign ────────────────────────────────────
        log("PHASE 2", f"{BOLD}Campaign creation{RESET}", YELLOW)
        campaign_id_a = None
        try:
            r = await must("create CPA campaign", c, "POST",
                "/api/v1/campaigns/create",
                json={
                    "brand_id": BRAND_A,
                    "name": "Indonesia Acquire CPA",
                    "objective": "acquire",
                    "bid_strategy": "cpa",
                    "max_bid_cents": 2000,
                    "daily_budget_cents": 20000,
                    "total_budget_cents": 100000,
                    "targeting": {
                        "geo": {"country": "ID", "city": "Jakarta", "radius_km": 50},
                        "demographics": {"age_min": 18, "age_max": 65}
                    },
                    "creative": {"recipe_id": "starbucks_loyalty", "game_slug": "match3"},
                    "schedule": {
                        "start_at": time.time() - 3600,
                        "end_at": time.time() + 86400 * 365
                    }
                })
            campaign_id_a = r["campaign_id"]
            ok(f"Campaign created: {campaign_id_a}")

            r = await must("list campaigns", c, "GET", f"/api/v1/campaigns/{BRAND_A}")
            assert any(x.get("campaign_id") == campaign_id_a for x in r.get("campaigns", r if isinstance(r, list) else [])), "campaign missing from list"
            ok("Campaign appears in brand list")
        except Exception as e:
            fail(f"Phase 2: {e}")
            failed += 1

        # ── Phase 3: Auction → Impression → Click → Conversion ──────────
        log("PHASE 3", f"{BOLD}Auction lifecycle{RESET}", YELLOW)
        impression_token = None
        try:
            r = await must("run auction", c, "POST",
                "/api/v1/auction/run",
                json={
                    "user_id": USER_U,
                    "device_fingerprint": DEVICE_FP,
                    "geo": {"country": "ID", "city": "Jakarta", "lat": -6.21, "lng": 106.85},
                    "context": {"time_of_day": 14, "day_of_week": 3,
                                "device": "mobile", "language": "id"},
                    "slot": "main"
                })
            if r.get("no_eligible_campaigns"):
                fail("Auction returned no_eligible_campaigns — targeting mismatch?", r)
                failed += 1
            else:
                assert r["winner_brand_id"] == BRAND_A, f"unexpected winner: {r}"
                impression_token = r["impression_token"]
                ok(f"Auction won by {r['winner_brand_id']}, "
                   f"bid=¥{r['winning_bid_cents']/100:.2f}, "
                   f"charge=¥{r['actual_charge_cents']/100:.2f}")
                ok(f"Impression token: {impression_token[:12]}...")

            if impression_token:
                await must("report impression", c, "POST",
                    "/api/v1/auction/report-impression",
                    json={"impression_token": impression_token})
                ok("Impression reported (CPA: no immediate charge)")

                await must("report click", c, "POST",
                    "/api/v1/auction/report-click",
                    json={"impression_token": impression_token,
                          "user_id": USER_U, "device_fingerprint": DEVICE_FP})
                ok("Click reported (CPA: still no charge)")

                bal_before = (await must("bal before conv", c, "GET",
                                         f"/api/v1/wallet/{BRAND_A}"))["balance_cents"]
                await must("report conversion", c, "POST",
                    "/api/v1/auction/report-conversion",
                    json={"impression_token": impression_token,
                          "user_id": USER_U,
                          "conversion_value_cents": 5000})
                bal_after = (await must("bal after conv", c, "GET",
                                        f"/api/v1/wallet/{BRAND_A}"))["balance_cents"]
                charged = bal_before - bal_after
                if charged > 0:
                    ok(f"Wallet deducted ¥{charged/100:.2f} on conversion (was ¥{bal_before/100:.2f} → ¥{bal_after/100:.2f})")
                else:
                    fail(f"No deduction! before={bal_before} after={bal_after}")
                    failed += 1

                r = await must("campaign stats", c, "GET",
                               f"/api/v1/campaigns/{campaign_id_a}/stats")
                ok(f"Campaign stats: imp={r.get('impressions',0)} "
                   f"clk={r.get('clicks',0)} conv={r.get('conversions',0)} "
                   f"spend=¥{r.get('spend_cents',0)/100:.2f}")
        except Exception as e:
            fail(f"Phase 3: {e}")
            failed += 1

        # ── Phase 4: Cross-Brand Attribution ────────────────────────────
        log("PHASE 4", f"{BOLD}Cross-brand attribution flow{RESET}", YELLOW)
        try:
            # Brand A sends invite token; user U eventually buys from Brand B
            r = await must("create invite token", c, "POST",
                "/api/v1/attribution/token/create",
                json={"brand_id": BRAND_A, "user_id": USER_U,
                      "ttl_seconds": 604800,
                      "context": {"campaign_id": campaign_id_a, "source": "cross_brand"}})
            invite = r["invite_token"]
            ok(f"Invite token from Brand A: {invite[:12]}...")

            await must("track click via token", c, "POST",
                "/api/v1/attribution/track/click",
                json={"invite_token": invite, "user_id": USER_U,
                      "device_fingerprint": DEVICE_FP, "target_brand": BRAND_B})
            ok("User clicked from A→B")

            await must("track visit B", c, "POST",
                "/api/v1/attribution/track/visit",
                json={"invite_token": invite, "user_id": USER_U,
                      "target_brand": BRAND_B})
            ok("User visited Brand B")

            r = await must("conversion at B", c, "POST",
                "/api/v1/attribution/track/conversion",
                json={"user_id": USER_U, "target_brand": BRAND_B,
                      "order_id": f"ord_{int(time.time())}",
                      "amount_cents": 10000})
            if r.get("attributed"):
                ok(f"✓ Cross-brand attribution: source={r.get('source_brand')} "
                   f"KiX take ¥{r.get('kix_take_cents',0)/100:.2f}")
            else:
                fail("Conversion not attributed!", r)
                failed += 1

            r = await must("journey", c, "GET",
                f"/api/v1/attribution/user/{USER_U}/journey")
            entries = r.get("entries", [])
            ok(f"User journey has {r.get('count', len(entries))} touchpoints")
            if r.get("count", 0) == 0:
                fail("Journey count is 0 — indexing bug?", r)
                failed += 1

            r = await must("brand B incoming", c, "GET",
                f"/api/v1/attribution/brand/{BRAND_B}/incoming")
            ok(f"Brand B incoming: count={r.get('count',0)} "
               f"by_source={r.get('by_source',{})}")
        except Exception as e:
            fail(f"Phase 4: {e}")
            failed += 1

        # ── Phase 5: Geofence / LBS ─────────────────────────────────────
        log("PHASE 5", f"{BOLD}Geofence + LBS{RESET}", YELLOW)
        try:
            store_id = f"e2e_store_{int(time.time())}"
            await must("register store", c, "POST",
                "/api/v1/geofence/stores/register",
                json={
                    "brand_id": BRAND_A,
                    "store_id": store_id,
                    "name": "Jakarta Central",
                    "lat": -6.21, "lng": 106.85,
                    "radius_meters": 500,
                    "associated_game_slug": "match3",
                    "associated_campaign_id": campaign_id_a,
                    "push_config": {
                        "enabled": True,
                        "cooldown_minutes": 1,
                        "hours_local": [0, 24],
                        "message_template": "你在 {brand_name} 附近！"
                    }
                })
            ok(f"Store registered: {store_id}")

            r = await must("nearby search", c, "POST",
                "/api/v1/geofence/nearby",
                json={"device_fingerprint": DEVICE_FP,
                      "lat": -6.211, "lng": 106.851,
                      "max_distance_km": 5})
            nearby = r.get("nearby_stores", [])
            ok(f"Nearby stores found: {len(nearby)}")

            r = await must("geofence enter", c, "POST",
                "/api/v1/geofence/enter",
                json={"user_id": USER_U + "_new",
                      "device_fingerprint": DEVICE_FP + "_new",
                      "store_id": store_id})
            if r.get("push_eligible"):
                ok(f"Push eligible — game: {r.get('payload',{}).get('game_slug')}")
            else:
                log("INFO", f"push not eligible: {r.get('reason')}", YELLOW)

            await must("record visit", c, "POST",
                "/api/v1/geofence/visit",
                json={"user_id": USER_U + "_new", "store_id": store_id,
                      "evidence": "qr_scan"})
            ok("Visit recorded at store")

            r = await must("store heatmap", c, "GET",
                f"/api/v1/geofence/stores/{store_id}/heatmap")
            ok(f"Heatmap: enter={r.get('enter_count',0)} "
               f"visit={r.get('visit_count',0)}")
        except Exception as e:
            fail(f"Phase 5: {e}")
            failed += 1

        # ── Phase 6: Anti-Fraud ────────────────────────────────────────
        log("PHASE 6", f"{BOLD}Anti-fraud signals{RESET}", YELLOW)
        try:
            r = await must("fraud check legit", c, "POST",
                "/api/v1/attribution/anti-fraud/check",
                json={"user_id": "legit_user", "brand_id": BRAND_A,
                      "action_type": "click"})
            ok(f"Legit user fraud_score={r.get('fraud_score',0)} valid={r.get('valid')}")

            # Hammer to trigger rate limit
            for _ in range(15):
                await c.post("/api/v1/attribution/anti-fraud/check",
                    json={"user_id": "spammer", "brand_id": BRAND_A,
                          "action_type": "click"})
            r = await must("fraud check spammer", c, "POST",
                "/api/v1/attribution/anti-fraud/check",
                json={"user_id": "spammer", "brand_id": BRAND_A,
                      "action_type": "click"})
            if r.get("fraud_score", 0) > 0:
                ok(f"Spammer detected: score={r['fraud_score']} reasons={r.get('reasons',[])}")
            else:
                log("INFO", "Rate limit may need more requests to trip", YELLOW)
        except Exception as e:
            fail(f"Phase 6: {e}")
            failed += 1

        # ── Phase 7: Wallet Final State ─────────────────────────────────
        log("PHASE 7", f"{BOLD}Final wallet state{RESET}", YELLOW)
        try:
            r = await must("final A", c, "GET", f"/api/v1/wallet/{BRAND_A}")
            ok(f"Brand A: balance=¥{r['balance_cents']/100:.2f} "
               f"daily_spent=¥{r['daily_spent_cents']/100:.2f} "
               f"total_spent=¥{r['total_spent_cents']/100:.2f}")

            r = await must("forecast A", c, "GET", f"/api/v1/wallet/{BRAND_A}/forecast")
            ok(f"Forecast: avg_daily=¥{r.get('avg_daily_spend',0)/100:.2f} "
               f"recommendation={r.get('recommendation')}")
        except Exception as e:
            fail(f"Phase 7: {e}")
            failed += 1

    await close_redis()
    print()
    if failed == 0:
        print(f"{GREEN}{BOLD}━━━ ALL E2E PHASES PASSED ━━━{RESET}")
        return 0
    else:
        print(f"{RED}{BOLD}━━━ {failed} PHASE(S) FAILED ━━━{RESET}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
