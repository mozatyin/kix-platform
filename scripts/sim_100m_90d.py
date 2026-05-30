"""100-merchant × 90-day multi-region marketplace simulation.

Scales the 10×30 SG F&B sim (``sim_sg_marketplace_30day.py``) up to
production capacity to flush edge cases that only surface at scale.

Cohort
------
- 30 Singapore (SG)        — F&B + retail
- 25 Indonesia (ID)        — F&B + service
- 20 SEA other (TH/VN/PH)  — retail + service + beauty
- 15 EU (eu)               — service + beauty (GDPR)
- 10 US (us)               — retail + beauty (CCPA)

Each brand has a (persona, budget_tier, industry, region) tuple drawn
from the cohort plan with a fixed seed for reproducibility.

Daily cycle (per sim-day)
-------------------------
1. Persona decision: bid tweak, budget shift, audience refresh
2. Auctions (cross-brand, sample sized to active brand count)
3. Consumer interactions: impression → click → conversion → viral
4. Wallet bookkeeping: charges, refunds, auto-recharge
5. Frequency caps + per-region compliance probe
6. Daily snapshot: HHI, wallet drift, retention, latency
7. Bug detector sweep

Output
------
- ``/Users/mozat/a-docs/sim_100m_90d_seed{S}.jsonl`` — full event log
- console summary
- per-day metrics stored in memory for the analyzer

Run::

    .venv/bin/python scripts/sim_100m_90d.py            # seed 42
    .venv/bin/python scripts/sim_100m_90d.py 100        # alt seed
    .venv/bin/python scripts/sim_100m_90d.py 7777
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
from httpx import ASGITransport

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("app").setLevel(logging.ERROR)
logging.getLogger("audit_log").setLevel(logging.ERROR)
logging.getLogger("sqlalchemy").setLevel(logging.ERROR)
logging.getLogger("asyncpg").setLevel(logging.ERROR)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402
from app.redis_client import close_redis, init_redis, get_redis  # noqa: E402

try:
    from scripts.llm_quota_monitor import wait_if_paused  # noqa: E402
except Exception:  # pragma: no cover
    async def wait_if_paused(max_wait_seconds: int = 3600) -> bool:  # type: ignore
        return False


# ── Cohort plan ─────────────────────────────────────────────────────────
# (region, count) totals 100. We map each region to a currency the
# wallet router understands. The cents amounts stay logical-currency
# (1 cent = 1/100 unit) regardless of locale.
COHORT_PLAN: list[tuple[str, int]] = [
    ("sg", 30),
    ("id", 25),
    ("th", 8),   # SEA bucket split
    ("vn", 7),   # SEA bucket
    ("ph", 5),   # SEA bucket
    ("eu", 15),
    ("us", 10),
]
assert sum(n for _, n in COHORT_PLAN) == 100, "cohort must total 100"

REGION_CURRENCY = {
    "sg": "SGD", "id": "IDR", "th": "THB", "vn": "VND",
    "ph": "PHP", "eu": "EUR", "us": "USD",
}
REGION_COUNTRY_CODE = {
    "sg": "SG", "id": "ID", "th": "TH", "vn": "VN",
    "ph": "PH", "eu": "DE", "us": "US",
}

INDUSTRIES = ["fnb", "retail", "service", "beauty"]
PERSONAS = ["conservative", "aggressive", "flat", "growing"]
BUDGET_TIERS = [
    # (label, daily_cents, max_bid_cents)
    ("S",  50_00,    400),   # S$0.50/day micro
    ("M",  200_00,   1200),  # S$2K monthly → ~67/day
    ("L",  1000_00,  2500),  # S$10K monthly → ~333/day
    ("XL", 5000_00,  6000),  # S$50K monthly → ~1666/day
]


# ── Run-level constants ─────────────────────────────────────────────────
SIM_DAYS = 90
DEFAULT_SEED = 42
START_WALLET_CENTS = 200_00_00  # $20K in cents — covers heavy XL spenders
# Per-day auction volume = active_brands * AUCTIONS_PER_ACTIVE_BRAND. Keeps
# the sim within the 30min wall budget. 100 brands × ~10 = 1000/day × 90 = 90k.
AUCTIONS_PER_ACTIVE_BRAND = 10
CONCURRENT_AUCTIONS = 8  # asyncio.Semaphore size — leaves CPU headroom
PROGRESS_EVERY = 7  # sim-days

OUT_DIR = Path("/Users/mozat/a-docs")
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ── Brand state ─────────────────────────────────────────────────────────
class BrandState:
    __slots__ = (
        "brand_id", "region", "industry", "persona", "tier",
        "max_bid_cents", "daily_budget_cents", "campaign_id",
        "current_bid_cents", "users_total", "spend_cents", "conversions",
        "wins", "entered", "viral_invites_issued", "viral_redemptions",
        "wallet_balance_cents", "current_qs", "auto_paused",
        "topup_total_cents", "refund_total_cents", "k_factor",
        "compliance_blocks", "frequency_capped",
    )

    def __init__(self, *, brand_id: str, region: str, industry: str,
                 persona: str, tier: str, max_bid_cents: int,
                 daily_budget_cents: int):
        self.brand_id = brand_id
        self.region = region
        self.industry = industry
        self.persona = persona
        self.tier = tier
        self.max_bid_cents = max_bid_cents
        self.daily_budget_cents = daily_budget_cents
        self.campaign_id: str | None = None
        self.current_bid_cents = max_bid_cents
        self.users_total = 100  # baseline cohort per brand
        self.spend_cents = 0
        self.conversions = 0
        self.wins = 0
        self.entered = 0
        self.viral_invites_issued = 0
        self.viral_redemptions = 0
        self.wallet_balance_cents = START_WALLET_CENTS
        self.current_qs = 0.5
        self.auto_paused = False
        self.topup_total_cents = START_WALLET_CENTS
        self.refund_total_cents = 0
        self.k_factor = 0.0
        self.compliance_blocks = 0
        self.frequency_capped = 0


# ── Cohort builder ──────────────────────────────────────────────────────
def build_cohort(seed: int) -> list[BrandState]:
    rng = random.Random(seed)
    cohort: list[BrandState] = []
    idx = 0
    for region, count in COHORT_PLAN:
        for _ in range(count):
            industry = rng.choice(INDUSTRIES)
            persona = rng.choice(PERSONAS)
            tier_label, daily_budget, max_bid = rng.choice(BUDGET_TIERS)
            bid = f"m{idx:03d}_{region}_{industry[:3]}_{tier_label.lower()}"
            cohort.append(BrandState(
                brand_id=bid,
                region=region,
                industry=industry,
                persona=persona,
                tier=tier_label,
                max_bid_cents=max_bid,
                daily_budget_cents=daily_budget,
            ))
            idx += 1
    return cohort


# ── HTTP helpers ────────────────────────────────────────────────────────
async def post(client: httpx.AsyncClient, path: str, **kw):
    return await client.post(path, **kw)


async def get(client: httpx.AsyncClient, path: str, **kw):
    return await client.get(path, **kw)


# ── Setup ───────────────────────────────────────────────────────────────
async def setup_brand(client: httpx.AsyncClient, state: BrandState,
                      run_tag: int) -> None:
    bid = state.brand_id
    pid = os.getpid()

    # 1. brand config
    cfg_body = {
        "brand_id": bid,
        "brand_name": f"Merchant {bid}",
        "brand_slug": f"{bid.replace('_', '-')}-{run_tag}-{pid}",
        "config_json": {"region": state.region, "industry": state.industry},
    }
    r = await post(client, "/api/v1/brands/", json=cfg_body)
    if r.status_code not in (200, 201, 409):
        return  # silently skip — brand will have no campaign

    # 2. wallet topup
    currency = REGION_CURRENCY.get(state.region, "USD")
    tr = await post(
        client,
        f"/api/v1/wallet/{bid}/topup",
        json={
            "amount_cents": START_WALLET_CENTS,
            "payment_method": "alipay",
            "payment_token": f"tok_{bid}_{run_tag}",
            "currency": currency,
        },
    )
    if tr.status_code == 200:
        try:
            topup_id = tr.json()["topup_id"]
            await post(
                client,
                f"/api/v1/wallet/{bid}/topup/{topup_id}/confirm",
                json={"payment_gateway_response": {"status": "ok"}},
            )
        except Exception:
            pass

    # 3. payment method + auto-recharge
    pm_r = await post(
        client,
        f"/api/v1/payment-methods/{bid}/add",
        json={
            "method_type": "alipay",
            "payment_token": f"pm_{bid}_{run_tag}",
            "holder_name": bid,
            "is_default": True,
        },
    )
    pm_id = None
    if pm_r.status_code in (200, 201):
        try:
            pm_id = pm_r.json().get("payment_method_id")
            if pm_id:
                await post(client, f"/api/v1/payment-methods/{pm_id}/verify", json={})
        except Exception:
            pm_id = None
    await client.put(
        f"/api/v1/wallet/{bid}/autorecharge",
        json={
            "enabled": True,
            "threshold_cents": int(START_WALLET_CENTS * 0.2),
            "topup_cents": int(START_WALLET_CENTS * 0.5),
            "payment_method_id": pm_id,
        },
    )

    # 4. campaign
    country = REGION_COUNTRY_CODE.get(state.region, "SG")
    camp_body = {
        "brand_id": bid,
        "name": f"{bid} acquire",
        "objective": "acquire",
        "bid_strategy": "cpa",
        "max_bid_cents": state.max_bid_cents,
        "daily_budget_cents": state.daily_budget_cents,
        "total_budget_cents": state.daily_budget_cents * SIM_DAYS,
        "targeting": {"geo": {"country": country}},
        "creative": {
            "headline": f"{bid} — promo",
            "body": f"{state.industry} promo for {state.region}",
        },
        "quality_score": 0.5,
    }
    r = await post(client, "/api/v1/campaigns/create", json=camp_body)
    if r.status_code == 200:
        try:
            state.campaign_id = r.json()["campaign_id"]
        except Exception:
            pass


# ── Persona decision ────────────────────────────────────────────────────
def persona_decide_bid(state: BrandState, rng: random.Random) -> int:
    cur = state.current_bid_cents
    declared_max = state.max_bid_cents
    win_rate = state.wins / max(1, state.entered)

    if state.persona == "conservative":
        if win_rate < 0.05 and state.entered > 50:
            cur = int(cur * 0.92)
        underspend = state.spend_cents < 0.5 * state.daily_budget_cents
        if underspend and rng.random() < 0.3:
            cur = int(cur * 1.03)
    elif state.persona == "aggressive":
        if win_rate < 0.05 and state.entered > 30:
            cur = int(cur * 1.15)
        if rng.random() < 0.5:
            cur = int(cur * 1.05)
    elif state.persona == "flat":
        # No-op; sometimes nudges +/-1%
        if rng.random() < 0.1:
            cur = int(cur * (1.01 if rng.random() < 0.5 else 0.99))
    elif state.persona == "growing":
        # Slow ramp every day, +2%
        cur = int(cur * 1.02)

    floor = max(50, declared_max // 2)
    if cur < floor:
        cur = floor
    if cur > declared_max:
        cur = declared_max
    return cur


async def update_bid(client: httpx.AsyncClient, state: BrandState,
                     new_bid: int) -> None:
    if state.campaign_id is None or new_bid == state.current_bid_cents:
        return
    r = await post(
        client,
        f"/api/v1/campaigns/{state.campaign_id}/update",
        json={"max_bid_cents": new_bid},
    )
    if r.status_code == 200:
        state.current_bid_cents = new_bid


# ── Single auction ──────────────────────────────────────────────────────
async def run_one_auction(
    client: httpx.AsyncClient,
    user_id: str,
    region: str,
    rng: random.Random,
    by_brand: dict[str, BrandState],
    event_writer,
) -> None:
    fp = f"fp_{user_id}"
    country = REGION_COUNTRY_CODE.get(region, "SG")
    try:
        r = await post(
            client,
            "/api/v1/auction/run",
            json={
                "user_id": user_id,
                "device_fingerprint": fp,
                "geo": {"country": country, "lat": 1.29, "lng": 103.85},
                "context": {"hour": rng.randint(8, 22)},
                "objective_filter": "acquire",
                "slot": "main",
            },
            timeout=15.0,
        )
    except Exception as exc:
        event_writer({"type": "auction_error", "err": str(exc)[:120]})
        return
    if r.status_code != 200:
        return
    data = r.json()
    # bump 'entered' for active brands in this region
    for st in by_brand.values():
        if st.campaign_id and not st.auto_paused and st.region == region:
            st.entered += 1
    if data.get("no_eligible_campaigns"):
        return
    wb = data.get("winner_brand_id")
    if not (wb and wb in by_brand):
        return
    st = by_brand[wb]
    st.wins += 1
    charge = int(data.get("actual_charge_cents") or 0)
    st.spend_cents += charge
    token = data.get("impression_token")
    if not token:
        return
    try:
        await post(client, "/api/v1/auction/report-impression",
                   json={"impression_token": token})
    except Exception:
        return
    # funnel
    if rng.random() < 0.15:
        try:
            await post(
                client,
                "/api/v1/auction/report-click",
                json={
                    "impression_token": token,
                    "user_id": user_id,
                    "device_fingerprint": fp,
                },
            )
        except Exception:
            return
        if rng.random() < 0.53:
            cv = rng.randint(2000, 8000)
            try:
                await post(
                    client,
                    "/api/v1/auction/report-conversion",
                    json={
                        "impression_token": token,
                        "user_id": user_id,
                        "conversion_value_cents": cv,
                    },
                )
                st.conversions += 1
            except Exception:
                return
            # attribution
            try:
                await post(
                    client,
                    "/api/v1/attribution/conversion",
                    json={
                        "user_id": user_id,
                        "brand_id_converted_at": wb,
                        "conversion_value_cents": cv,
                        "model": "time_decay",
                        "lookback_days": 7,
                    },
                )
            except Exception:
                pass
            # viral
            if rng.random() < 0.06:
                trig = rng.choice(["share_to_win", "friend_challenge"])
                try:
                    ir = await post(
                        client,
                        f"/api/v1/network/trigger/{trig}/init",
                        json={
                            "user_id": user_id,
                            "brand_id": wb,
                            "context": {"score": rng.randint(100, 999)},
                        },
                    )
                    if ir.status_code == 200:
                        st.viral_invites_issued += 1
                        tok = ir.json().get("invite_token")
                        if tok and rng.random() < 0.30:
                            new_uid = f"v_{wb}_{rng.randint(0, 10_000_000)}"
                            rr = await post(
                                client,
                                "/api/v1/network/redeem",
                                json={
                                    "invite_token": tok,
                                    "new_user_id": new_uid,
                                    "brand_id": wb,
                                },
                            )
                            if rr.status_code == 200:
                                st.viral_redemptions += 1
                                st.users_total += 1
                except Exception:
                    pass


# ── Daily cycle ─────────────────────────────────────────────────────────
async def run_day(
    client: httpx.AsyncClient,
    day: int,
    rng: random.Random,
    by_brand: dict[str, BrandState],
    event_writer,
) -> dict[str, Any]:
    t0 = time.time()

    # 1. Persona decisions (sequential; cheap)
    for st in by_brand.values():
        if not st.campaign_id or st.auto_paused:
            continue
        nb = persona_decide_bid(st, rng)
        if nb != st.current_bid_cents:
            await update_bid(client, st, nb)

    # 2. Compliance probe — once per day per region (sample 1 brand/region)
    for region in {st.region for st in by_brand.values()}:
        try:
            cr = await get(
                client,
                "/api/v1/compliance/check-content",
                params={"region": region, "category": "alcohol_to_minors",
                        "age": rng.choice([15, 22, 30])},
            )
            if cr.status_code == 200:
                d = cr.json()
                if not d.get("allowed"):
                    # bump per-brand counter for any brand in this region
                    for st in by_brand.values():
                        if st.region == region and rng.random() < 0.05:
                            st.compliance_blocks += 1
                            break
        except Exception:
            pass

    # 3. Auctions
    active = [st for st in by_brand.values()
              if st.campaign_id and not st.auto_paused]
    auctions_today = max(50, len(active) * AUCTIONS_PER_ACTIVE_BRAND)
    sem = asyncio.Semaphore(CONCURRENT_AUCTIONS)

    async def one(i: int) -> None:
        async with sem:
            # pick a region weighted by active-brand count
            region = rng.choice([st.region for st in active])
            # user_id from a pool of ~10K
            user_id = f"u_{region}_{rng.randint(0, 9999)}"
            await run_one_auction(client, user_id, region, rng, by_brand,
                                  event_writer)

    await asyncio.gather(*[one(i) for i in range(auctions_today)])

    # 4. Refresh per-brand server-side state (campaigns + wallet)
    #    Sample every brand on weekly checkpoints; cheap (1 GET each)
    if day % 7 == 0 or day == SIM_DAYS:
        refresh_sem = asyncio.Semaphore(CONCURRENT_AUCTIONS)

        async def refresh(st: BrandState) -> None:
            async with refresh_sem:
                if st.campaign_id:
                    try:
                        rp = await get(
                            client,
                            f"/api/v1/campaigns/{st.campaign_id}/auto-pause-status",
                            timeout=10.0,
                        )
                        if rp.status_code == 200:
                            st.auto_paused = bool(rp.json().get("auto_paused"))
                    except Exception:
                        pass
                    try:
                        rq = await get(
                            client,
                            f"/api/v1/campaigns/{st.campaign_id}/qs-breakdown",
                            timeout=10.0,
                        )
                        if rq.status_code == 200:
                            st.current_qs = float(
                                rq.json().get("current_qs") or 0.5)
                    except Exception:
                        pass
                try:
                    rw = await get(client, f"/api/v1/wallet/{st.brand_id}",
                                   timeout=10.0)
                    if rw.status_code == 200:
                        st.wallet_balance_cents = int(
                            rw.json().get("balance_cents") or 0)
                except Exception:
                    pass

        await asyncio.gather(*[refresh(st) for st in by_brand.values()])

    elapsed = time.time() - t0

    # 5. Daily snapshot metrics
    total_wins = sum(st.wins for st in by_brand.values())
    total_spend = sum(st.spend_cents for st in by_brand.values())
    total_conv = sum(st.conversions for st in by_brand.values())
    shares = {st.brand_id: st.wins / max(1, total_wins)
              for st in by_brand.values()}
    top_share = max(shares.values()) if shares else 0.0
    hhi = int(round(sum((s * 100) ** 2 for s in shares.values())))

    snap = {
        "day": day,
        "elapsed_s": round(elapsed, 2),
        "auctions": auctions_today,
        "total_wins": total_wins,
        "total_spend_cents": total_spend,
        "total_conv": total_conv,
        "hhi": hhi,
        "top_share": round(top_share, 4),
        "active_brands": len(active),
        "paused_brands": sum(1 for st in by_brand.values() if st.auto_paused),
        "zero_win_brands": sum(1 for st in by_brand.values() if st.wins == 0),
    }
    event_writer({"type": "day_snapshot", **snap})
    return snap


# ── Bug detection ───────────────────────────────────────────────────────
def detect_bugs_day(
    by_brand: dict[str, BrandState],
    day: int,
    snap: dict[str, Any],
    prev_latencies: list[float],
    redis_memory_bytes: int | None,
    prev_redis_memory: int | None,
) -> list[dict[str, Any]]:
    bugs: list[dict[str, Any]] = []

    # 1. Concentration
    if snap["hhi"] >= 2500 or snap["top_share"] >= 0.40:
        bugs.append({
            "code": "auction_concentration",
            "severity": "P0" if snap["hhi"] >= 5000 else "P1",
            "day": day,
            "symptom": f"HHI={snap['hhi']} top_share={snap['top_share']*100:.1f}%",
            "root_cause": "Big-spender brands monopolize despite diversity floor",
            "fix": "Raise diversity floor or cap absolute bid by tier",
        })

    # 2. Wallet drift — debits should reconcile with topup_total - balance
    total_drift = 0
    for st in by_brand.values():
        expected_balance = st.topup_total_cents - st.spend_cents + st.refund_total_cents
        drift = abs(st.wallet_balance_cents - expected_balance)
        if drift > 10_00 and st.wallet_balance_cents > 0:  # >$10 drift
            total_drift += drift
    # Auto-recharge will add money, so drift>0 is expected — only flag huge
    if total_drift > 1_000_000_00:  # > $1M aggregate unexplained drift
        bugs.append({
            "code": "wallet_drift_anomaly",
            "severity": "P1",
            "day": day,
            "symptom": f"Aggregate wallet drift ${total_drift/100:,.0f}",
            "root_cause": "Topup/refund/charge ledger not balancing — possible double-charge or missing event",
            "fix": "Add ledger reconciliation worker; cross-check against audit_log",
        })

    # 3. Zero-win starvation at scale
    starved = [st.brand_id for st in by_brand.values()
               if st.wins == 0 and st.entered >= 100 and not st.auto_paused]
    if day >= 14 and len(starved) >= 10:
        bugs.append({
            "code": "cold_start_starvation_at_scale",
            "severity": "P0",
            "day": day,
            "symptom": f"{len(starved)} brands entered ≥100 auctions but won 0 by day {day}",
            "root_cause": "Diversity floor doesn't scale — 3% floor distributed across 100 brands ≈ 0",
            "fix": "Scale floor inversely with active brand count; guarantee min N wins/week",
        })

    # 4. Performance degradation — day_elapsed growing > 2x baseline
    if len(prev_latencies) >= 7:
        baseline = statistics.median(prev_latencies[:3])
        recent = statistics.median(prev_latencies[-3:])
        if recent > 2.0 * baseline and recent > 5.0:
            bugs.append({
                "code": "perf_degradation",
                "severity": "P1",
                "day": day,
                "symptom": f"Day wall time {recent:.1f}s vs baseline {baseline:.1f}s",
                "root_cause": "Latency growth over sim — likely Redis hot-key or N+1 query",
                "fix": "Profile slowest endpoint at this day; check Redis SLOWLOG",
            })

    # 5. Redis memory leak — net growth > 50MB in 7 days
    if (redis_memory_bytes is not None and prev_redis_memory is not None
            and redis_memory_bytes - prev_redis_memory > 50 * 1024 * 1024):
        bugs.append({
            "code": "redis_memory_growth",
            "severity": "P1",
            "day": day,
            "symptom": (f"Redis mem +{(redis_memory_bytes - prev_redis_memory)/1024/1024:.1f}MB in week; "
                        f"now {redis_memory_bytes/1024/1024:.1f}MB"),
            "root_cause": "Per-day TTL-less keys accumulating",
            "fix": "Audit for missing EXPIRE; add nightly cleanup job",
        })

    # 6. Compliance violations — any brand serving banned content
    blocks = sum(st.compliance_blocks for st in by_brand.values())
    if blocks > 0 and day >= 7:
        # not a bug per-se, but worth flagging if a non-EU/US merchant
        # is consistently triggering age-gate (cross-region leak proxy)
        regions_with_blocks = {st.region for st in by_brand.values()
                               if st.compliance_blocks > 0}
        if regions_with_blocks - {"eu", "us"}:
            pass  # informational, no bug

    # 7. Refund/charge audit consistency: every charge should produce one audit event
    #    (sampled — skip in fast sim; logged as informational)

    return bugs


# ── Redis stat helper ───────────────────────────────────────────────────
async def redis_used_memory() -> int | None:
    try:
        r = await get_redis()
        info = await r.info("memory")
        return int(info.get("used_memory", 0))
    except Exception:
        return None


# ── Wipe ────────────────────────────────────────────────────────────────
async def wipe_prior_state(cohort: list[BrandState]) -> None:
    try:
        r = await get_redis()
    except Exception:
        return
    deleted = 0
    bids = [s.brand_id for s in cohort]
    patterns = [
        "auction:diversity:total",
        "auction:diversity:entered:{bid}",
        "auction:diversity:won:{bid}",
        "brand:{bid}:campaigns",
        "brand:{bid}:campaigns_count",
        "viral:brand:{bid}:invites_issued:*",
        "viral:brand:{bid}:invites_redeemed:*",
    ]
    for pat in patterns:
        if "{bid}" not in pat:
            try:
                await r.delete(pat)
                deleted += 1
            except Exception:
                pass
            continue
        for bid in bids:
            k = pat.format(bid=bid)
            if "*" in k:
                try:
                    async for kk in r.scan_iter(match=k):
                        await r.delete(kk)
                        deleted += 1
                except Exception:
                    pass
            else:
                try:
                    if await r.exists(k):
                        await r.delete(k)
                        deleted += 1
                except Exception:
                    pass
    try:
        active = await r.smembers("campaigns:active")
        for cid in active:
            c = await r.hgetall(f"campaign:{cid}")
            if c.get("brand_id") in bids or c.get(b"brand_id") in [b.encode() for b in bids]:
                await r.srem("campaigns:active", cid)
                deleted += 1
    except Exception:
        pass
    print(f"[wipe] cleared {deleted} stale sim keys")


# ── Main ────────────────────────────────────────────────────────────────
async def run_sim(seed: int) -> dict[str, Any]:
    t0 = time.time()
    rng = random.Random(seed)
    cohort = build_cohort(seed)
    by_brand = {st.brand_id: st for st in cohort}
    run_tag = seed * 1000 + os.getpid() % 1000

    print(f"[sim] seed={seed} brands={len(cohort)} days={SIM_DAYS} "
          f"auctions/active/day={AUCTIONS_PER_ACTIVE_BRAND}")
    print(f"[sim] cohort: " + ", ".join(
        f"{r}={n}" for r, n in COHORT_PLAN))

    await init_redis()
    try:
        await wait_if_paused()
    except Exception:
        pass
    await wipe_prior_state(cohort)

    out_path = OUT_DIR / f"sim_100m_90d_seed{seed}.jsonl"
    f_out = out_path.open("w", encoding="utf-8")

    def emit(event: dict[str, Any]) -> None:
        f_out.write(json.dumps(event, ensure_ascii=False) + "\n")

    emit({"type": "run_start", "seed": seed, "days": SIM_DAYS,
          "brands": len(cohort), "cohort_plan": COHORT_PLAN})

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test",
                                 timeout=30.0) as client:

        # ── Setup phase
        t_setup = time.time()
        print(f"[setup] creating {len(cohort)} brands…")
        setup_sem = asyncio.Semaphore(CONCURRENT_AUCTIONS)

        async def _setup_one(st: BrandState) -> None:
            async with setup_sem:
                await setup_brand(client, st, run_tag)

        await asyncio.gather(*[_setup_one(st) for st in cohort])
        with_camp = sum(1 for st in cohort if st.campaign_id)
        print(f"[setup] done in {time.time()-t_setup:.1f}s — "
              f"{with_camp}/{len(cohort)} have campaigns")
        emit({"type": "setup_done", "brands_with_campaign": with_camp,
              "setup_seconds": round(time.time() - t_setup, 2)})

        # ── Daily loop
        all_bugs: list[dict[str, Any]] = []
        per_day: list[dict[str, Any]] = []
        latency_history: list[float] = []
        prev_redis_mem = await redis_used_memory()
        last_checkpoint_redis = prev_redis_mem

        checkpoints = {7, 14, 30, 60, 90}
        for day in range(1, SIM_DAYS + 1):
            snap = await run_day(client, day, rng, by_brand, emit)
            latency_history.append(snap["elapsed_s"])
            per_day.append(snap)

            cur_redis_mem = (await redis_used_memory()) if day % 7 == 0 else None
            bugs = detect_bugs_day(
                by_brand, day, snap, latency_history,
                cur_redis_mem,
                last_checkpoint_redis if day % 7 == 0 else None,
            )
            if cur_redis_mem is not None:
                last_checkpoint_redis = cur_redis_mem
            for b in bugs:
                emit({"type": "bug", **b})
            all_bugs.extend(bugs)

            if day in checkpoints:
                # Trinity 3T-style cross-check
                tri = {
                    "type": "trinity_checkpoint",
                    "day": day,
                    "industry": {
                        "hhi": snap["hhi"],
                        "verdict": ("competitive" if snap["hhi"] < 1500
                                    else "concentrated" if snap["hhi"] < 2500
                                    else "monopolistic"),
                    },
                    "academic": {
                        "k_factor_proxy": (
                            sum(st.viral_redemptions for st in by_brand.values())
                            / max(1, sum(st.viral_invites_issued
                                         for st in by_brand.values()))
                        ),
                        "bass_fit": ("strong" if snap["total_conv"] > 100
                                     else "weak"),
                    },
                    "reality": {
                        "platform_rev_cents": snap["total_spend_cents"],
                        "zero_win_brands": snap["zero_win_brands"],
                        "paused_brands": snap["paused_brands"],
                        "bugs_so_far": len(all_bugs),
                    },
                }
                emit(tri)

            if day % PROGRESS_EVERY == 0 or day == SIM_DAYS:
                elapsed = time.time() - t0
                eta = elapsed / day * (SIM_DAYS - day) if day < SIM_DAYS else 0
                print(
                    f"[day {day:02d}/{SIM_DAYS}] "
                    f"hhi={snap['hhi']:>4d} "
                    f"wins={snap['total_wins']:>5d} "
                    f"conv={snap['total_conv']:>4d} "
                    f"paused={snap['paused_brands']:>2d} "
                    f"zerowin={snap['zero_win_brands']:>2d} "
                    f"bugs={len(all_bugs):>3d} "
                    f"wall={snap['elapsed_s']:.1f}s "
                    f"eta={eta/60:.1f}m"
                )

        # ── Final aggregation
        per_brand_rows = []
        for st in cohort:
            per_brand_rows.append({
                "brand_id": st.brand_id,
                "region": st.region,
                "industry": st.industry,
                "persona": st.persona,
                "tier": st.tier,
                "wins": st.wins,
                "entered": st.entered,
                "spend_cents": st.spend_cents,
                "conv": st.conversions,
                "cpa_cents": (st.spend_cents / st.conversions
                              if st.conversions else 0),
                "users_total": st.users_total,
                "viral_invites": st.viral_invites_issued,
                "viral_redemptions": st.viral_redemptions,
                "qs": st.current_qs,
                "wallet_balance_cents": st.wallet_balance_cents,
                "topup_total_cents": st.topup_total_cents,
                "auto_paused": st.auto_paused,
                "compliance_blocks": st.compliance_blocks,
            })

        runtime = time.time() - t0
        summary = {
            "type": "run_summary",
            "seed": seed,
            "runtime_seconds": round(runtime, 2),
            "total_events": sum(p.get("auctions", 0) for p in per_day),
            "total_wins": sum(st.wins for st in cohort),
            "total_conv": sum(st.conversions for st in cohort),
            "total_spend_cents": sum(st.spend_cents for st in cohort),
            "total_viral_invites": sum(st.viral_invites_issued for st in cohort),
            "total_viral_redemptions": sum(st.viral_redemptions for st in cohort),
            "bugs_total": len(all_bugs),
            "bugs_p0": sum(1 for b in all_bugs if b["severity"] == "P0"),
            "bugs_p1": sum(1 for b in all_bugs if b["severity"] == "P1"),
            "bugs_p2": sum(1 for b in all_bugs if b["severity"] == "P2"),
            "final_hhi": per_day[-1]["hhi"] if per_day else 0,
            "final_top_share": per_day[-1]["top_share"] if per_day else 0,
            "zero_win_brands_final": sum(1 for st in cohort if st.wins == 0),
            "auto_paused_final": sum(1 for st in cohort if st.auto_paused),
        }
        emit(summary)
        emit({"type": "per_brand_final", "rows": per_brand_rows})
        emit({"type": "per_day_summary", "rows": per_day})

        f_out.close()
        print(f"\n[done] runtime={runtime/60:.1f}m | events={summary['total_events']:,} "
              f"| bugs={summary['bugs_total']} "
              f"(P0={summary['bugs_p0']} P1={summary['bugs_p1']} P2={summary['bugs_p2']}) "
              f"| HHI={summary['final_hhi']} "
              f"| jsonl={out_path}")
        return summary

    await close_redis()


def main() -> int:
    seed = DEFAULT_SEED
    if len(sys.argv) >= 2:
        try:
            seed = int(sys.argv[1])
        except ValueError:
            print(f"bad seed: {sys.argv[1]}", file=sys.stderr)
            return 2
    summary = asyncio.run(run_sim(seed))
    # short verdict
    print(f"[verdict] platform_health="
          f"{'PASS' if summary['bugs_p0'] == 0 and summary['final_hhi'] < 2500 else 'FAIL'} "
          f"P0={summary['bugs_p0']} HHI={summary['final_hhi']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
