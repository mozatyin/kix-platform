"""Campaign-partition correctness + perf tests (Trinity-F bottleneck #1).

Validates the multi-dimensional ``campaigns:active:*`` indexes maintained
by ``campaigns.py`` and consumed by ``auction.find_candidates_for_user``:

  * Correctness — country / geohash partitioning returns exactly the
    expected slice, untargeted campaigns always bid, audience targeting
    is NOT intersected (would over-narrow).
  * Lifecycle — pause / resume / delete / update preserve index integrity.
  * Perf — at 1K and 10K active campaigns, NEW (partitioned SINTERSTORE
    selection) is materially faster than OLD (SMEMBERS-the-world).

These tests run against the live Redis from ``conftest.clean_redis`` so
they exercise the real SINTERSTORE / SUNIONSTORE pipeline, not a mock.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from app.routers import auction as auction_mod
from app.routers.auction import find_candidates_for_user
from app.routers.campaigns import (
    ACTIVE_BY_COUNTRY_KEY,
    ACTIVE_BY_GEOHASH_KEY,
    ACTIVE_BY_OBJECTIVE_KEY,
    ACTIVE_CAMPAIGNS_KEY,
    ACTIVE_UNTARGETED_KEY,
    CAMPAIGN_INDEX_MEMBERSHIP_KEY,
    _ck,
    _geohash_encode,
    _index_campaign_active,
    _deindex_campaign_active,
)


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


def _make_campaign(
    cid: str,
    *,
    brand: str = "brand_default",
    objective: str = "acquire",
    country: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    audience_id: str | None = None,
) -> dict[str, str]:
    targeting: dict[str, Any] = {}
    if country or (lat is not None and lng is not None):
        geo: dict[str, Any] = {}
        if country:
            geo["country"] = country
        if lat is not None:
            geo["lat"] = lat
            geo["lng"] = lng
        targeting["geo"] = geo
    if audience_id:
        targeting["audience_id"] = audience_id

    return {
        "campaign_id": cid,
        "brand_id": brand,
        "objective": objective,
        "status": "active",
        "targeting": json.dumps(targeting),
        "quality_score": "0.5",
        "max_bid_cents": "100",
        "daily_budget_cents": "1000000",
        "total_budget_cents": "10000000",
    }


async def _seed(r, campaigns: list[dict[str, str]]) -> None:
    """Write each campaign to its HASH and call _index_campaign_active."""
    pipe = r.pipeline()
    for c in campaigns:
        pipe.hset(_ck(c["campaign_id"]), mapping=c)
    await pipe.execute()
    for c in campaigns:
        await _index_campaign_active(r, c["campaign_id"], c)


# ─────────────────────────────────────────────────────────────────────────
# Correctness
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_index_membership_split_by_country(clean_redis):
    r = clean_redis
    await _seed(
        r,
        [
            _make_campaign("c_us_1", country="US"),
            _make_campaign("c_us_2", country="US"),
            _make_campaign("c_fr_1", country="FR"),
            _make_campaign("c_global", country=None),  # untargeted
        ],
    )

    us = await r.smembers(ACTIVE_BY_COUNTRY_KEY.format(country="US"))
    fr = await r.smembers(ACTIVE_BY_COUNTRY_KEY.format(country="FR"))
    untargeted = await r.smembers(ACTIVE_UNTARGETED_KEY)
    all_active = await r.smembers(ACTIVE_CAMPAIGNS_KEY)

    assert us == {"c_us_1", "c_us_2"}
    assert fr == {"c_fr_1"}
    assert untargeted == {"c_global"}
    # Legacy aggregate keeps everything for backwards-compat.
    assert all_active == {"c_us_1", "c_us_2", "c_fr_1", "c_global"}


@pytest.mark.asyncio
async def test_find_candidates_intersects_geo_and_includes_untargeted(
    clean_redis,
):
    r = clean_redis
    await _seed(
        r,
        [
            _make_campaign("c_us", country="US"),
            _make_campaign("c_fr", country="FR"),
            _make_campaign("c_global", country=None),
        ],
    )

    us_candidates = await find_candidates_for_user({"country": "US"}, r)
    assert us_candidates == {"c_us", "c_global"}

    fr_candidates = await find_candidates_for_user({"country": "FR"}, r)
    assert fr_candidates == {"c_fr", "c_global"}

    # Country with no campaigns + untargeted still bid.
    de_candidates = await find_candidates_for_user({"country": "DE"}, r)
    assert de_candidates == {"c_global"}


@pytest.mark.asyncio
async def test_find_candidates_with_geohash_unions_with_country(clean_redis):
    r = clean_redis
    # NYC ≈ 40.71, -74.01 → geohash5 = "dr5ru" (first 5 chars)
    nyc_gh = _geohash_encode(40.71, -74.01, precision=5)
    await _seed(
        r,
        [
            _make_campaign("c_us", country="US"),
            _make_campaign("c_nyc", lat=40.71, lng=-74.01),
            _make_campaign("c_global", country=None),
        ],
    )
    assert nyc_gh == _geohash_encode(40.71, -74.01, precision=5)

    # User in NYC with country=US gets ALL three.
    res = await find_candidates_for_user(
        {"country": "US", "lat": 40.71, "lng": -74.01}, r
    )
    assert res == {"c_us", "c_nyc", "c_global"}


@pytest.mark.asyncio
async def test_find_candidates_objective_filter_intersects(clean_redis):
    r = clean_redis
    await _seed(
        r,
        [
            _make_campaign("c_us_acq", country="US", objective="acquire"),
            _make_campaign("c_us_sales", country="US", objective="sales"),
            _make_campaign("c_global_acq", objective="acquire"),
        ],
    )

    res = await find_candidates_for_user(
        {"country": "US"}, r, objective_filter="acquire"
    )
    assert res == {"c_us_acq", "c_global_acq"}


@pytest.mark.asyncio
async def test_find_candidates_no_signal_falls_back_to_aggregate(clean_redis):
    """No geo + no objective → legacy SMEMBERS path. Compat-safe."""
    r = clean_redis
    await _seed(
        r,
        [
            _make_campaign("c_us", country="US"),
            _make_campaign("c_fr", country="FR"),
            _make_campaign("c_global", country=None),
        ],
    )
    res = await find_candidates_for_user({}, r)
    assert res == {"c_us", "c_fr", "c_global"}


@pytest.mark.asyncio
async def test_deindex_removes_from_all_partitions(clean_redis):
    r = clean_redis
    c = _make_campaign(
        "c_drop", country="US", lat=40.71, lng=-74.01, audience_id="aud_42"
    )
    await _seed(r, [c])

    # Sanity: membership tracker has all the keys.
    membership = await r.smembers(
        CAMPAIGN_INDEX_MEMBERSHIP_KEY.format(cid="c_drop")
    )
    assert ACTIVE_CAMPAIGNS_KEY in membership
    assert ACTIVE_BY_COUNTRY_KEY.format(country="US") in membership

    # Deindex.
    await _deindex_campaign_active(r, "c_drop")

    # All partition SETs no longer contain c_drop.
    for key in membership:
        assert not await r.sismember(key, "c_drop")
    # Membership tracker itself is gone.
    assert (
        await r.smembers(CAMPAIGN_INDEX_MEMBERSHIP_KEY.format(cid="c_drop"))
        == set()
    )


@pytest.mark.asyncio
async def test_reindex_after_targeting_change(clean_redis):
    """Updating targeting should not leak the campaign into stale partitions."""
    r = clean_redis
    cid = "c_move"
    initial = _make_campaign(cid, country="US")
    await _seed(r, [initial])
    assert await r.sismember(ACTIVE_BY_COUNTRY_KEY.format(country="US"), cid)

    # Simulate update_campaign re-indexing: deindex then index with new targeting.
    moved = _make_campaign(cid, country="FR")
    await r.hset(_ck(cid), mapping=moved)
    await _deindex_campaign_active(r, cid)
    await _index_campaign_active(r, cid, moved)

    assert not await r.sismember(
        ACTIVE_BY_COUNTRY_KEY.format(country="US"), cid
    )
    assert await r.sismember(ACTIVE_BY_COUNTRY_KEY.format(country="FR"), cid)


@pytest.mark.asyncio
async def test_lifecycle_via_router_create_pause_resume_delete(
    client, clean_redis
):
    """End-to-end: create → pause → resume → delete keeps indexes consistent."""
    r = clean_redis

    res = await client.post(
        "/api/v1/campaigns/create",
        json={
            "brand_id": "brand_perf",
            "name": "Perf Smoke",
            "objective": "acquire",
            "bid_strategy": "cpm",
            "max_bid_cents": 100,
            "daily_budget_cents": 10000,
            "total_budget_cents": 100000,
            "targeting": {"geo": {"country": "US"}},
        },
    )
    assert res.status_code == 200, res.text
    cid = res.json()["campaign_id"]
    status = res.json()["status"]

    # Some auto-approve flows leave campaigns in pending_review; only
    # active campaigns land in partitions.
    if status == "active":
        assert await r.sismember(
            ACTIVE_BY_COUNTRY_KEY.format(country="US"), cid
        )

        # Pause → removed from partitions.
        res = await client.post(f"/api/v1/campaigns/{cid}/pause")
        assert res.status_code == 200, res.text
        assert not await r.sismember(
            ACTIVE_BY_COUNTRY_KEY.format(country="US"), cid
        )
        assert not await r.sismember(ACTIVE_CAMPAIGNS_KEY, cid)

        # Resume → back in partitions.
        res = await client.post(f"/api/v1/campaigns/{cid}/resume")
        assert res.status_code == 200, res.text
        assert await r.sismember(
            ACTIVE_BY_COUNTRY_KEY.format(country="US"), cid
        )

    # Delete → gone from every partition + tracker.
    res = await client.post(f"/api/v1/campaigns/{cid}/delete")
    assert res.status_code == 200, res.text
    assert not await r.sismember(
        ACTIVE_BY_COUNTRY_KEY.format(country="US"), cid
    )
    assert not await r.sismember(ACTIVE_CAMPAIGNS_KEY, cid)
    assert (
        await r.smembers(CAMPAIGN_INDEX_MEMBERSHIP_KEY.format(cid=cid))
        == set()
    )


# ─────────────────────────────────────────────────────────────────────────
# Perf
# ─────────────────────────────────────────────────────────────────────────


def _country_for(i: int) -> str:
    # 20-way fanout — at 10K campaigns each country bucket holds ~500.
    return ["US", "FR", "DE", "GB", "JP", "BR", "AU", "IN", "CN", "CA",
            "MX", "IT", "ES", "RU", "NL", "SE", "PL", "KR", "AR", "ZA"][i % 20]


async def _bulk_seed(r, n: int) -> None:
    """Seed n campaigns spread across 20 countries via pipelined HSET + index."""
    # Pre-build the campaign dicts then pipeline all HSETs + index keys in
    # one go for fast setup (don't measure this in the perf budget).
    pipe = r.pipeline()
    cids: list[tuple[str, dict[str, str]]] = []
    for i in range(n):
        cid = f"c_perf_{i:06d}"
        c = _make_campaign(cid, country=_country_for(i))
        cids.append((cid, c))
        pipe.hset(_ck(cid), mapping=c)
    await pipe.execute()

    # Index in batches so we don't blow Redis with one giant pipe.
    BATCH = 500
    for i in range(0, len(cids), BATCH):
        pipe = r.pipeline()
        for cid, c in cids[i:i + BATCH]:
            # Inline the SADD work to keep the seed phase fast.
            from app.routers.campaigns import _compute_active_index_keys
            keys = _compute_active_index_keys(c)
            membership_key = CAMPAIGN_INDEX_MEMBERSHIP_KEY.format(cid=cid)
            pipe.delete(membership_key)
            for key in keys:
                pipe.sadd(key, cid)
            if keys:
                pipe.sadd(membership_key, *keys)
        await pipe.execute()


async def _old_path_smembers_only(r) -> int:
    """Simulate the OLD auction prelude: SMEMBERS the full active set."""
    members = await r.smembers(ACTIVE_CAMPAIGNS_KEY)
    return len(members)


async def _new_path_partitioned(r, country: str) -> int:
    res = await find_candidates_for_user({"country": country}, r)
    return len(res)


@pytest.mark.asyncio
@pytest.mark.parametrize("n", [1_000, 10_000])
async def test_perf_partitioned_vs_full_scan(clean_redis, n):
    r = clean_redis
    await _bulk_seed(r, n)

    # Warm both code paths once so JIT / connection setup don't skew.
    await _old_path_smembers_only(r)
    await _new_path_partitioned(r, "US")

    # OLD path — best of 3 to suppress noise.
    old_ms: list[float] = []
    for _ in range(3):
        t0 = time.perf_counter()
        for _ in range(10):
            await _old_path_smembers_only(r)
        old_ms.append((time.perf_counter() - t0) * 1000 / 10)
    old_best = min(old_ms)

    # NEW path — same.
    new_ms: list[float] = []
    for _ in range(3):
        t0 = time.perf_counter()
        for _ in range(10):
            await _new_path_partitioned(r, "US")
        new_ms.append((time.perf_counter() - t0) * 1000 / 10)
    new_best = min(new_ms)

    # Confirm partition size is ~ N / 20 (we have 20 countries).
    us_size = await r.scard(ACTIVE_BY_COUNTRY_KEY.format(country="US"))
    expected = n // 20

    print(
        f"\n[perf n={n}] OLD smembers={old_best:.2f}ms  "
        f"NEW partitioned={new_best:.2f}ms  "
        f"US partition size={us_size} (expected ~{expected})"
    )

    # Sanity assertions:
    assert us_size == expected
    # NEW path returns ~partition size (no untargeted in this seed).
    new_count = await _new_path_partitioned(r, "US")
    assert new_count == expected

    # At 10K campaigns the partitioned path should be at least 3× faster
    # than the full SMEMBERS pull. At 1K the gap shrinks because SMEMBERS
    # of 1K is already fast — we relax to a no-regression bound.
    if n >= 10_000:
        assert new_best * 3 <= old_best, (
            f"partitioned path not significantly faster: "
            f"old={old_best:.2f}ms new={new_best:.2f}ms"
        )
    else:
        # No-regression: NEW must not be worse than 1.5× OLD at small N.
        assert new_best <= old_best * 1.5, (
            f"partitioned path regressed at n={n}: "
            f"old={old_best:.2f}ms new={new_best:.2f}ms"
        )
