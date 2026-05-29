"""Cold-start learning boost + diversity floor tests.

Covers the two-part P0 fix for cold-start auction starvation:

  Part A — campaigns < LEARNING_PHASE_HOURS old get a decaying rank boost.
  Part B — brands below DIVERSITY_FLOOR_PCT share of trailing auctions
           are force-promoted into the top-K of the ranking, regardless
           of bid x QS.
"""

from __future__ import annotations

import json
import time

import pytest

from app.routers import auction as auction_mod
from app.routers.auction import (
    DIVERSITY_ENTERED_KEY,
    DIVERSITY_TOTAL_KEY,
    DIVERSITY_WON_KEY,
    DIVERSITY_WINDOW,
    LEARNING_BOOST_MAX,
    LEARNING_PHASE_HOURS,
    _apply_diversity_floor,
    _learning_boost,
)
from app.routers.campaigns import (
    _ck,
    _index_campaign_active,
)


def _make_campaign(
    cid: str,
    *,
    brand: str,
    created_at: float | None = None,
    max_bid_cents: int = 100,
    quality_score: float = 0.5,
) -> dict[str, str]:
    c: dict[str, str] = {
        "campaign_id": cid,
        "brand_id": brand,
        "objective": "acquire",
        "status": "active",
        "targeting": json.dumps({}),
        "quality_score": str(quality_score),
        "max_bid_cents": str(max_bid_cents),
        "daily_budget_cents": "1000000",
        "total_budget_cents": "100000000",
        "bid_strategy": "cpm",
        "target_audience": "all",
    }
    if created_at is not None:
        c["created_at"] = str(created_at)
    return c


async def _seed(r, campaigns: list[dict[str, str]]) -> None:
    pipe = r.pipeline()
    for c in campaigns:
        pipe.hset(_ck(c["campaign_id"]), mapping=c)
    await pipe.execute()
    for c in campaigns:
        await _index_campaign_active(r, c["campaign_id"], c)


# ── Part A: Learning boost (pure function tests) ─────────────────────────


def test_learning_boost_new_campaign_gets_boost():
    """A campaign created right now gets the full (1 + LEARNING_BOOST_MAX) boost."""
    now = time.time()
    c = {"created_at": str(now)}
    boost = _learning_boost(c, now=now)
    assert boost == pytest.approx(1.0 + LEARNING_BOOST_MAX, rel=1e-6)


def test_learning_boost_decays_linearly():
    """Halfway through the learning phase the boost is half the max."""
    now = time.time()
    half = now - (LEARNING_PHASE_HOURS / 2) * 3600
    c = {"created_at": str(half)}
    boost = _learning_boost(c, now=now)
    assert boost == pytest.approx(1.0 + LEARNING_BOOST_MAX * 0.5, rel=1e-6)


def test_learning_boost_old_campaign_no_boost():
    """A campaign older than LEARNING_PHASE_HOURS gets no boost."""
    now = time.time()
    old = now - (LEARNING_PHASE_HOURS + 1) * 3600
    c = {"created_at": str(old)}
    assert _learning_boost(c, now=now) == 1.0


def test_learning_boost_missing_created_at_no_boost():
    """A campaign with no created_at field is treated as old."""
    assert _learning_boost({}, now=time.time()) == 1.0
    assert _learning_boost({"created_at": "0"}, now=time.time()) == 1.0
    assert _learning_boost({"created_at": "garbage"}, now=time.time()) == 1.0


# ── Part B: Diversity floor (pure function test) ─────────────────────────


def test_diversity_floor_promotes_starved_brand():
    """A starved brand's candidate is promoted to position 0 (winner slot)."""
    ranked = [
        (1000.0, 100, 0.5, 1.0, {"brand_id": "brand_A", "campaign_id": "c1"}),
        (900.0, 90, 0.5, 1.0, {"brand_id": "brand_A", "campaign_id": "c2"}),
        (800.0, 80, 0.5, 1.0, {"brand_id": "brand_B", "campaign_id": "c3"}),
        (700.0, 70, 0.5, 1.0, {"brand_id": "brand_B", "campaign_id": "c4"}),
        (10.0, 1, 0.5, 1.0, {"brand_id": "brand_X", "campaign_id": "c5"}),
    ]
    new_ranked = _apply_diversity_floor(ranked, starved_brand_ids={"brand_X"})
    # Winner slot is now brand_X (was rank 5, force-promoted).
    assert new_ranked[0][4]["brand_id"] == "brand_X"
    # Total candidate count preserved.
    assert len(new_ranked) == len(ranked)


def test_diversity_floor_no_starved_brands_is_noop():
    """When no brand is starved, ordering is preserved."""
    ranked = [
        (1000.0, 100, 0.5, 1.0, {"brand_id": "brand_A", "campaign_id": "c1"}),
        (900.0, 90, 0.5, 1.0, {"brand_id": "brand_B", "campaign_id": "c2"}),
        (800.0, 80, 0.5, 1.0, {"brand_id": "brand_C", "campaign_id": "c3"}),
    ]
    new_ranked = _apply_diversity_floor(ranked, starved_brand_ids=set())
    assert [row[4]["campaign_id"] for row in new_ranked] == ["c1", "c2", "c3"]


# ── Integration: end-to-end through /auction/run ─────────────────────────


@pytest.mark.asyncio
async def test_new_campaign_learning_boost_via_auction(client, clean_redis):
    """Test 1 — A fresh campaign (<24h) outranks an incumbent thanks to boost."""
    r = clean_redis
    now = time.time()
    incumbent = _make_campaign(
        "c_old", brand="brand_old", created_at=now - 30 * 3600,
        max_bid_cents=100, quality_score=0.5,
    )
    # Newcomer: bid 80, brand new -> 1.5x boost -> 80 * 1.5 = 120 > 100.
    newcomer = _make_campaign(
        "c_new", brand="brand_new", created_at=now,
        max_bid_cents=80, quality_score=0.5,
    )
    await _seed(r, [incumbent, newcomer])

    res = await client.post(
        "/api/v1/auction/run",
        json={"device_fingerprint": "fp_learning", "slot": "main"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["winner_campaign_id"] == "c_new"
    assert body["winner_brand_id"] == "brand_new"


@pytest.mark.asyncio
async def test_old_campaign_no_boost_via_auction(client, clean_redis):
    """Test 2 — Two old campaigns: highest bid wins, no boost interference."""
    r = clean_redis
    now = time.time()
    old_a = _make_campaign(
        "c_old_a", brand="brand_a", created_at=now - 48 * 3600,
        max_bid_cents=100, quality_score=0.5,
    )
    old_b = _make_campaign(
        "c_old_b", brand="brand_b", created_at=now - 48 * 3600,
        max_bid_cents=80, quality_score=0.5,
    )
    await _seed(r, [old_a, old_b])

    res = await client.post(
        "/api/v1/auction/run",
        json={"device_fingerprint": "fp_old", "slot": "main"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["winner_campaign_id"] == "c_old_a"


@pytest.mark.asyncio
async def test_starved_brand_floor_injection(client, clean_redis):
    """Test 3 — After DIVERSITY_WINDOW prior auctions where a starved
    brand won 0, the next auction promotes that brand's candidate to
    the winner slot via the diversity floor."""
    r = clean_redis
    now = time.time()
    # All old (no learning boost). brand_dom dominates by bid×qs; brand_X
    # is starved and would never win on rank alone.
    dom = _make_campaign(
        "c_dom", brand="brand_dom", created_at=now - 48 * 3600,
        max_bid_cents=10000, quality_score=0.9,
    )
    starved_x = _make_campaign(
        "c_x", brand="brand_X", created_at=now - 48 * 3600,
        max_bid_cents=1, quality_score=0.1,
    )
    await _seed(r, [dom, starved_x])

    # Pre-seed DIVERSITY_WINDOW prior auctions: dom won them all.
    # brand_X entered every prior auction but never won (starved).
    pipe = r.pipeline()
    pipe.set(DIVERSITY_TOTAL_KEY, DIVERSITY_WINDOW)
    for i in range(DIVERSITY_WINDOW):
        pipe.zadd(
            DIVERSITY_ENTERED_KEY.format(brand_id="brand_dom"),
            {f"e_dom_{i}": now - DIVERSITY_WINDOW + i},
        )
        pipe.zadd(
            DIVERSITY_WON_KEY.format(brand_id="brand_dom"),
            {f"w_dom_{i}": now - DIVERSITY_WINDOW + i},
        )
        pipe.zadd(
            DIVERSITY_ENTERED_KEY.format(brand_id="brand_X"),
            {f"e_X_{i}": now - DIVERSITY_WINDOW + i},
        )
    await pipe.execute()

    res = await client.post(
        "/api/v1/auction/run",
        json={"device_fingerprint": "fp_starve", "slot": "main"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["eligible_count"] == 2
    # Floor promotes brand_X (starved) to winner slot even though its
    # rank is much lower than brand_dom.
    assert body["winner_brand_id"] == "brand_X"


@pytest.mark.asyncio
async def test_well_served_brand_no_injection(client, clean_redis):
    """Test 4 — Brand with > floor% share doesn't get force-injected."""
    r = clean_redis
    now = time.time()
    high = _make_campaign(
        "c_high", brand="brand_high", created_at=now - 48 * 3600,
        max_bid_cents=1000, quality_score=0.9,
    )
    low = _make_campaign(
        "c_low", brand="brand_low", created_at=now - 48 * 3600,
        max_bid_cents=10, quality_score=0.5,
    )
    await _seed(r, [high, low])

    # Pre-seed DIVERSITY_WINDOW auctions where each brand won 60+ (above
    # the 3% = 30 floor).
    pipe = r.pipeline()
    pipe.set(DIVERSITY_TOTAL_KEY, DIVERSITY_WINDOW)
    for i in range(DIVERSITY_WINDOW):
        pipe.zadd(
            DIVERSITY_ENTERED_KEY.format(brand_id="brand_high"),
            {f"e_h_{i}": now - DIVERSITY_WINDOW + i},
        )
        pipe.zadd(
            DIVERSITY_ENTERED_KEY.format(brand_id="brand_low"),
            {f"e_l_{i}": now - DIVERSITY_WINDOW + i},
        )
    for i in range(60):
        pipe.zadd(
            DIVERSITY_WON_KEY.format(brand_id="brand_high"),
            {f"w_h_{i}": now - 60 + i},
        )
        pipe.zadd(
            DIVERSITY_WON_KEY.format(brand_id="brand_low"),
            {f"w_l_{i}": now - 60 + i},
        )
    await pipe.execute()

    res = await client.post(
        "/api/v1/auction/run",
        json={"device_fingerprint": "fp_normal", "slot": "main"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # Highest rank wins normally — no floor injection.
    assert body["winner_campaign_id"] == "c_high"


@pytest.mark.asyncio
async def test_diversity_report_endpoint(client, clean_redis):
    """Test 5 — /diversity-report/{brand_id} returns trailing share metrics."""
    r = clean_redis
    now = time.time()
    pipe = r.pipeline()
    pipe.set(DIVERSITY_TOTAL_KEY, 500)
    for i in range(200):
        pipe.zadd(
            DIVERSITY_ENTERED_KEY.format(brand_id="brand_report"),
            {f"e_{i}": now - 200 + i},
        )
    for i in range(15):
        pipe.zadd(
            DIVERSITY_WON_KEY.format(brand_id="brand_report"),
            {f"w_{i}": now - 15 + i},
        )
    await pipe.execute()

    res = await client.get("/api/v1/auction/diversity-report/brand_report")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["brand_id"] == "brand_report"
    assert body["window_size"] == DIVERSITY_WINDOW
    assert body["trailing_total_auctions"] == 500
    assert body["entered"] == 200
    assert body["won"] == 15
    assert body["won_share"] == pytest.approx(15 / 500, rel=1e-3)
    assert body["floor_pct"] == auction_mod.DIVERSITY_FLOOR_PCT
    # below_floor only triggers once trailing >= window; here 500 < 1000.
    assert body["below_floor"] is False
