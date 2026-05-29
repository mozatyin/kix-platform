"""Tests for the P1 bid death-spiral fix.

sg-marketplace 30-day sim findings:
  - toast_box bid -78% by day 14
  - killiney -90% by day 21
  - papparich -95% by day 29
Root cause: react_to_loss=decrease_bid was unbounded.

This suite verifies:
  1. /update rejects max_bid_cents below the floor (max(50¢, 50% of declared))
  2. /update accepts max_bid_cents at/above the floor
  3. run_low_performance_pause_sweep pauses a campaign with
     win_rate=0%, auctions_entered>=300 over the trailing window
  4. The same sweep leaves alone a campaign with win_rate=10%
  5. /bid-history returns an ordered list of bid changes
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from app.redis_client import get_redis
from app.routers.campaigns import (
    AUTO_PAUSE_MIN_IMPRESSIONS,
    CAMPAIGN_DECLARED_MAX_BID_KEY,
    _auctions_entered_daily_key,
    _wins_daily_key,
    record_auction_participation,
    run_low_performance_pause_sweep,
)


# ── helpers ──────────────────────────────────────────────────────────────


async def _make_campaign(
    client,
    *,
    brand_id: str = "brand_bidfloor",
    max_bid_cents: int = 1000,
    daily_budget_cents: int = 100_000,
    total_budget_cents: int = 1_000_000,
) -> str:
    """POST /api/v1/campaigns/create returning the campaign_id."""
    res = await client.post(
        "/api/v1/campaigns/create",
        json={
            "brand_id": brand_id,
            "name": "bid floor test",
            "objective": "acquire",
            "bid_strategy": "cpm",
            "max_bid_cents": max_bid_cents,
            "daily_budget_cents": daily_budget_cents,
            "total_budget_cents": total_budget_cents,
            "targeting": {"geo": {"country": "SG"}},
        },
    )
    assert res.status_code == 200, res.text
    return res.json()["campaign_id"]


# ── Test 1: bid below floor → 400 with helpful message ───────────────────


@pytest.mark.asyncio
async def test_bid_below_floor_rejected(client, clean_redis):
    """Bid below max(50¢, 50% of declared) → 400 with structured detail."""
    cid = await _make_campaign(client, max_bid_cents=1000)
    # Declared = 1000, floor = max(50, 500) = 500.
    res = await client.post(
        f"/api/v1/campaigns/{cid}/update",
        json={"max_bid_cents": 100},  # well below 500
    )
    assert res.status_code == 400, res.text
    detail = res.json()["detail"]
    assert detail["error"] == "bid_below_floor"
    assert detail["floor_cents"] == 500
    assert detail["declared_max_bid_cents"] == 1000
    assert detail["submitted_bid_cents"] == 100
    # bid_floor_hits counter incremented.
    r = clean_redis
    hits = await r.get(f"campaign:{cid}:bid_floor_hits")
    assert hits is not None and int(hits) == 1


# ── Test 2: bid above floor → accepted ───────────────────────────────────


@pytest.mark.asyncio
async def test_bid_above_floor_accepted(client, clean_redis):
    """Bid at/above floor → accepted; bid_history records the change."""
    cid = await _make_campaign(client, max_bid_cents=1000)
    # Floor = 500. Try 600 — above floor.
    res = await client.post(
        f"/api/v1/campaigns/{cid}/update",
        json={"max_bid_cents": 600},
    )
    assert res.status_code == 200, res.text

    # /bid-history should now show initial + the accepted update.
    hist = await client.get(f"/api/v1/campaigns/{cid}/bid-history")
    assert hist.status_code == 200
    body = hist.json()
    assert body["count"] >= 2
    reasons = [e["reason"] for e in body["entries"]]
    assert "initial_bid" in reasons
    assert "manual_update" in reasons


# ── Test 3: 0% win-rate + 300+ impressions → auto-paused ─────────────────


@pytest.mark.asyncio
async def test_low_win_rate_auto_paused(client, clean_redis):
    """Sweep auto-pauses a campaign with win_rate=0% & enough evidence."""
    r = clean_redis
    cid = await _make_campaign(client, brand_id="brand_lowperf")

    # Distribute 0 wins / >=300 entries across the trailing 3 days so the
    # window covers them regardless of clock boundary.
    now = time.time()
    for day_offset in range(3):
        date = datetime.fromtimestamp(
            now - day_offset * 86400, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        await r.set(_auctions_entered_daily_key(cid, date), 120)
        # 0 wins → win_rate = 0%

    counters = await run_low_performance_pause_sweep(r)
    assert counters["paused"] >= 1

    # Campaign is now paused.
    raw = await r.hgetall(f"campaign:{cid}")
    assert raw["status"] == "paused"
    assert raw.get("pause_reason") == "low_performance"

    # /auto-pause-status endpoint reflects the state.
    res = await client.get(f"/api/v1/campaigns/{cid}/auto-pause-status")
    assert res.status_code == 200
    body = res.json()
    assert body["paused"] is True
    assert body["reason"] == "low_performance"
    assert body["suggested_action"] in {
        "raise_bid_to_floor",
        "improve_quality_score",
        "narrow_audience",
        "abandon",
    }

    # Notification queued for the brand.
    notes = await r.lrange("notification:brand:brand_lowperf:campaign_paused", 0, -1)
    assert notes, "expected at least one pause notification"
    payload = json.loads(notes[0])
    assert payload["campaign_id"] == cid
    assert payload["reason"] == "low_performance"


# ── Test 4: 10% win-rate → not paused ────────────────────────────────────


@pytest.mark.asyncio
async def test_healthy_win_rate_not_paused(client, clean_redis):
    """Sweep leaves alone a campaign whose win_rate is above the threshold."""
    r = clean_redis
    cid = await _make_campaign(client, brand_id="brand_healthy")

    now = time.time()
    for day_offset in range(3):
        date = datetime.fromtimestamp(
            now - day_offset * 86400, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        await r.set(_auctions_entered_daily_key(cid, date), 200)
        # 20 wins out of 200 = 10% win rate (above 5% threshold).
        await r.set(_wins_daily_key(cid, date), 20)

    await run_low_performance_pause_sweep(r)

    raw = await r.hgetall(f"campaign:{cid}")
    # Still active (or whatever non-paused state derive_status gives).
    assert raw["status"] != "paused"

    res = await client.get(f"/api/v1/campaigns/{cid}/auto-pause-status")
    body = res.json()
    assert body["paused"] is False
    # 600 entered total / 60 wins = 0.10
    assert body["win_rate"] == pytest.approx(0.10, abs=0.001)


# ── Test 5: bid-history endpoint returns ordered list ────────────────────


@pytest.mark.asyncio
async def test_bid_history_endpoint_ordered(client, clean_redis):
    """After multiple accepted updates, /bid-history returns oldest→newest."""
    cid = await _make_campaign(client, max_bid_cents=2000)
    # Floor = 1000. Walk three accepted updates above the floor.
    for new_bid in (1500, 1600, 1700):
        res = await client.post(
            f"/api/v1/campaigns/{cid}/update",
            json={"max_bid_cents": new_bid},
        )
        assert res.status_code == 200, res.text

    res = await client.get(f"/api/v1/campaigns/{cid}/bid-history")
    body = res.json()
    assert body["count"] >= 4  # initial + 3 updates
    ts_series = [e["ts"] for e in body["entries"]]
    assert ts_series == sorted(ts_series), (
        "bid-history entries must be ordered oldest→newest"
    )
    bid_series = [e["bid_cents"] for e in body["entries"]]
    # First entry is the initial bid; later three contain the updates.
    assert bid_series[0] == 2000
    assert 1500 in bid_series
    assert 1600 in bid_series
    assert 1700 in bid_series

    # Floor metadata.
    assert body["declared_max_bid_cents"] == 2000
    assert body["current_bid_floor_cents"] == 1000


# ── Bonus sanity: record_auction_participation increments daily counters ─


@pytest.mark.asyncio
async def test_record_auction_participation_counters(clean_redis):
    """record_auction_participation increments both daily + lifetime keys."""
    r = clean_redis
    cid = "camp_track1"
    await record_auction_participation(r, cid, won=False)
    await record_auction_participation(r, cid, won=True)
    await record_auction_participation(r, cid, won=False)

    entered = await r.get(_auctions_entered_daily_key(cid))
    wins = await r.get(_wins_daily_key(cid))
    assert int(entered) == 3
    assert int(wins) == 1


# ── Bonus: declared_max_bid is frozen (PATCH can't ratchet floor down) ───


@pytest.mark.asyncio
async def test_declared_max_bid_frozen(client, clean_redis):
    """Floor is based on the *declared* max — not the most recent bid.

    Without this guarantee, react_to_loss=decrease_bid could PATCH the bid
    down step-by-step (each PATCH halves the floor) until bid → 0.
    """
    r = clean_redis
    cid = await _make_campaign(client, max_bid_cents=10_000)
    # Floor at creation = 5000.
    res = await client.post(
        f"/api/v1/campaigns/{cid}/update",
        json={"max_bid_cents": 5000},
    )
    assert res.status_code == 200

    # Declared still 10k (frozen).
    declared = await r.get(
        CAMPAIGN_DECLARED_MAX_BID_KEY.format(cid=cid)
    )
    assert int(declared) == 10_000

    # Attempting to ratchet down further must still hit the *original*
    # floor of 5000 (not 2500).
    res = await client.post(
        f"/api/v1/campaigns/{cid}/update",
        json={"max_bid_cents": 2500},
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert detail["floor_cents"] == 5000
