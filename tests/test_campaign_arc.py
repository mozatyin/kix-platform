"""Tests for the multi-week Campaign Arc engine.

Covers:
  - Each of the four arc templates (monopoly / advent / bracket / sweepstakes).
  - HTTP CRUD + today + play + progress + leaderboard + claim.
  - Worker (`campaign_arc_scheduler`) sweep + drop emission idempotency
    + engagement decay detection.

The tests reuse the standard ``client`` + ``clean_redis`` fixtures from
``tests/conftest.py`` (autouse strict isolation flushes Redis between
tests).
"""

from __future__ import annotations

import json
import time

import pytest

from app.services.campaign_arc import (
    DAY_SECONDS,
    CampaignArc,
    build_daily_drops,
    leaderboard as service_leaderboard,
    load_arc,
)
from app.workers.campaign_arc_scheduler import (
    DROP_EVENT_STREAM,
    detect_engagement_decay,
    discover_active_arc_ids,
    emit_today_drop,
    run_sweep,
)


# ── Templates / pure unit tests ────────────────────────────────────────


def test_template_monopoly_collect_n_compiles():
    drops = build_daily_drops(
        "monopoly_collect_n",
        duration_days=10,
        piece_set=["A", "B", "C"],
        rare_piece_id="C",
    )
    assert len(drops) == 10
    assert all(d["type"] == "piece_drop" for d in drops)
    # Rare piece should be flagged.
    rares = [d for d in drops if d.get("rare")]
    assert rares, "expected at least one rare-flagged drop"
    assert all(d.get("drop_probability", 1) < 1 for d in rares)


def test_template_advent_calendar_each_day_has_door():
    drops = build_daily_drops(
        "advent_calendar",
        duration_days=24,
        daily_rewards=[{"type": "voucher", "value_cents": 500 + i} for i in range(24)],
    )
    assert len(drops) == 24
    door_ids = {d["door_id"] for d in drops}
    assert len(door_ids) == 24  # each door unique


def test_template_tournament_bracket_rounds_decrease():
    drops = build_daily_drops(
        "tournament_bracket", duration_days=8, bracket_size=8
    )
    survivors = [d["survivors"] for d in drops]
    # Monotonically non-increasing over the arc.
    for a, b in zip(survivors, survivors[1:]):
        assert b <= a


def test_template_sweepstakes_finale_flag():
    drops = build_daily_drops("sweepstakes_entries", duration_days=5)
    assert drops[-1]["finale"] is True
    assert all(d["finale"] is False for d in drops[:-1])


def test_invalid_arc_type_raises():
    with pytest.raises(ValueError):
        build_daily_drops("not_a_real_template", duration_days=3)


# ── HTTP API ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_get_arc(client):
    resp = await client.post(
        "/api/v1/campaign-arcs/create",
        json={
            "brand_id": "brand_mcdonalds",
            "name": "McD Monopoly 2026",
            "arc_type": "monopoly_collect_n",
            "duration_days": 42,
            "config": {
                "piece_set": [f"p{i}" for i in range(20)],
                "rare_piece_id": "p19",
                "collect_n": 4,
            },
            "prize_pool": {
                "grand": {
                    "id": "grand",
                    "title": "$1M cash",
                    "quantity": 1,
                    "required_pieces": ["p0", "p1", "p2", "p3"],
                },
                "tier_2": {"id": "tier_2", "title": "Free fries"},
            },
        },
    )
    assert resp.status_code == 200, resp.text
    arc_id = resp.json()["arc_id"]

    detail = await client.get(f"/api/v1/campaign-arcs/{arc_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["arc_type"] == "monopoly_collect_n"
    assert len(body["daily_drops"]) == 42
    assert body["status"] in {"active", "scheduled"}


@pytest.mark.asyncio
async def test_today_endpoint_returns_in_play(client):
    resp = await client.post(
        "/api/v1/campaign-arcs/create",
        json={
            "brand_id": "b1",
            "name": "advent",
            "arc_type": "advent_calendar",
            "duration_days": 12,
        },
    )
    arc_id = resp.json()["arc_id"]
    today = await client.get(f"/api/v1/campaign-arcs/{arc_id}/today")
    assert today.status_code == 200
    payload = today.json()
    assert payload["status"] == "in_play"
    assert payload["day_index"] == 0
    assert "drop" in payload


@pytest.mark.asyncio
async def test_play_and_progress_monopoly(client, clean_redis):
    resp = await client.post(
        "/api/v1/campaign-arcs/create",
        json={
            "brand_id": "b1",
            "name": "mono",
            "arc_type": "monopoly_collect_n",
            "duration_days": 5,
            "config": {"piece_set": ["a", "b", "c"], "collect_n": 3},
        },
    )
    arc_id = resp.json()["arc_id"]

    # Play once — should award one piece.
    play = await client.post(
        f"/api/v1/campaign-arcs/{arc_id}/play",
        json={"user_id": "u1"},
    )
    assert play.status_code == 200, play.text
    body = play.json()
    assert body["ok"] is True
    assert body["awarded"]["piece_id"] in {"a", "b", "c"}

    # Progress endpoint reflects the play.
    prog = await client.get(
        f"/api/v1/campaign-arcs/{arc_id}/progress",
        params={"user_id": "u1"},
    )
    assert prog.status_code == 200
    pbody = prog.json()
    assert pbody["plays"] == 1
    assert pbody["unique_pieces"] >= 1


@pytest.mark.asyncio
async def test_sweepstakes_play_grants_tickets(client):
    resp = await client.post(
        "/api/v1/campaign-arcs/create",
        json={
            "brand_id": "b1",
            "name": "sweep",
            "arc_type": "sweepstakes_entries",
            "duration_days": 7,
        },
    )
    arc_id = resp.json()["arc_id"]
    for _ in range(3):
        await client.post(
            f"/api/v1/campaign-arcs/{arc_id}/play",
            json={"user_id": "u_sweep"},
        )
    prog = await client.get(
        f"/api/v1/campaign-arcs/{arc_id}/progress",
        params={"user_id": "u_sweep"},
    )
    assert prog.json()["tickets"] == 3


@pytest.mark.asyncio
async def test_leaderboard_ranks_users(client):
    resp = await client.post(
        "/api/v1/campaign-arcs/create",
        json={
            "brand_id": "b1",
            "name": "advent",
            "arc_type": "advent_calendar",
            "duration_days": 30,
        },
    )
    arc_id = resp.json()["arc_id"]
    # u1 plays once, u2 plays "twice" — though only one door per day really
    # opens, the play counter increments and the leaderboard records score.
    await client.post(
        f"/api/v1/campaign-arcs/{arc_id}/play", json={"user_id": "u1"}
    )
    await client.post(
        f"/api/v1/campaign-arcs/{arc_id}/play", json={"user_id": "u2"}
    )
    await client.post(
        f"/api/v1/campaign-arcs/{arc_id}/play", json={"user_id": "u2"}
    )
    lb = await client.get(
        f"/api/v1/campaign-arcs/{arc_id}/leaderboard", params={"limit": 10}
    )
    assert lb.status_code == 200
    rows = lb.json()["leaderboard"]
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_claim_succeeds_when_eligible(client, clean_redis):
    # Build a monopoly arc where the prize requires holding piece "a".
    resp = await client.post(
        "/api/v1/campaign-arcs/create",
        json={
            "brand_id": "b1",
            "name": "claimtest",
            "arc_type": "monopoly_collect_n",
            "duration_days": 3,
            "config": {"piece_set": ["a"], "collect_n": 1},
            "prize_pool": {
                "grand": {
                    "id": "grand",
                    "title": "$100",
                    "required_pieces": ["a"],
                    "quantity": 5,
                },
            },
        },
    )
    arc_id = resp.json()["arc_id"]
    await client.post(
        f"/api/v1/campaign-arcs/{arc_id}/play", json={"user_id": "u1"}
    )
    claim = await client.post(
        f"/api/v1/campaign-arcs/{arc_id}/claim",
        json={"user_id": "u1", "prize_id": "grand"},
    )
    assert claim.status_code == 200, claim.text
    assert claim.json()["ok"] is True


@pytest.mark.asyncio
async def test_claim_rejected_when_missing_pieces(client):
    resp = await client.post(
        "/api/v1/campaign-arcs/create",
        json={
            "brand_id": "b1",
            "name": "claimreject",
            "arc_type": "monopoly_collect_n",
            "duration_days": 3,
            "config": {"piece_set": ["a", "b"], "collect_n": 2},
            "prize_pool": {
                "grand": {
                    "id": "grand",
                    "title": "$1M",
                    "required_pieces": ["a", "b", "rare"],
                },
            },
        },
    )
    arc_id = resp.json()["arc_id"]
    # User plays but never gets the 'rare' piece.
    await client.post(
        f"/api/v1/campaign-arcs/{arc_id}/play", json={"user_id": "u1"}
    )
    claim = await client.post(
        f"/api/v1/campaign-arcs/{arc_id}/claim",
        json={"user_id": "u1", "prize_id": "grand"},
    )
    assert claim.status_code == 409
    assert claim.json()["detail"]["reason"] == "pieces_missing"


@pytest.mark.asyncio
async def test_claim_rejected_for_excluded_region(client):
    resp = await client.post(
        "/api/v1/campaign-arcs/create",
        json={
            "brand_id": "b1",
            "name": "regiontest",
            "arc_type": "advent_calendar",
            "duration_days": 2,
            "legal_compliance": {
                "min_age": 18,
                "excluded_regions": ["QC", "IT"],
            },
            "prize_pool": {
                "grand": {"id": "grand", "title": "x", "required_doors": 0},
            },
        },
    )
    arc_id = resp.json()["arc_id"]
    await client.post(
        f"/api/v1/campaign-arcs/{arc_id}/play", json={"user_id": "u1"}
    )
    claim = await client.post(
        f"/api/v1/campaign-arcs/{arc_id}/claim",
        json={"user_id": "u1", "prize_id": "grand", "user_region": "QC"},
    )
    assert claim.status_code == 409
    assert claim.json()["detail"]["reason"] == "region_excluded"


@pytest.mark.asyncio
async def test_claim_capacity_capped(client):
    resp = await client.post(
        "/api/v1/campaign-arcs/create",
        json={
            "brand_id": "b1",
            "name": "captest",
            "arc_type": "advent_calendar",
            "duration_days": 2,
            "prize_pool": {
                "grand": {
                    "id": "grand",
                    "title": "limited",
                    "required_doors": 0,
                    "quantity": 1,  # only 1 winner allowed
                },
            },
        },
    )
    arc_id = resp.json()["arc_id"]
    # Two users both play + try to claim the single-slot prize.
    for uid in ("u1", "u2"):
        await client.post(
            f"/api/v1/campaign-arcs/{arc_id}/play", json={"user_id": uid}
        )
    c1 = await client.post(
        f"/api/v1/campaign-arcs/{arc_id}/claim",
        json={"user_id": "u1", "prize_id": "grand"},
    )
    assert c1.status_code == 200
    c2 = await client.post(
        f"/api/v1/campaign-arcs/{arc_id}/claim",
        json={"user_id": "u2", "prize_id": "grand"},
    )
    assert c2.status_code == 409
    assert c2.json()["detail"]["reason"] == "prize_exhausted"


@pytest.mark.asyncio
async def test_double_claim_rejected(client):
    resp = await client.post(
        "/api/v1/campaign-arcs/create",
        json={
            "brand_id": "b1",
            "name": "dbl",
            "arc_type": "advent_calendar",
            "duration_days": 3,
            "prize_pool": {
                "grand": {
                    "id": "grand",
                    "title": "x",
                    "required_doors": 0,
                    "quantity": 10,
                },
            },
        },
    )
    arc_id = resp.json()["arc_id"]
    await client.post(
        f"/api/v1/campaign-arcs/{arc_id}/play", json={"user_id": "u1"}
    )
    c1 = await client.post(
        f"/api/v1/campaign-arcs/{arc_id}/claim",
        json={"user_id": "u1", "prize_id": "grand"},
    )
    assert c1.status_code == 200
    c2 = await client.post(
        f"/api/v1/campaign-arcs/{arc_id}/claim",
        json={"user_id": "u1", "prize_id": "grand"},
    )
    assert c2.status_code == 409
    assert c2.json()["detail"]["reason"] == "already_claimed"


@pytest.mark.asyncio
async def test_list_brand_arcs(client):
    for i in range(3):
        await client.post(
            "/api/v1/campaign-arcs/create",
            json={
                "brand_id": "bX",
                "name": f"arc_{i}",
                "arc_type": "advent_calendar",
                "duration_days": 5,
            },
        )
    listing = await client.get("/api/v1/campaign-arcs/brand/bX")
    assert listing.status_code == 200
    assert listing.json()["count"] == 3


@pytest.mark.asyncio
async def test_worker_emits_drop_event_once(client, clean_redis):
    r = clean_redis
    resp = await client.post(
        "/api/v1/campaign-arcs/create",
        json={
            "brand_id": "b1",
            "name": "cron",
            "arc_type": "advent_calendar",
            "duration_days": 5,
        },
    )
    arc_id = resp.json()["arc_id"]

    # First emit: succeeds, second: skipped (idempotent).
    r1 = await emit_today_drop(r, arc_id)
    assert r1["result"] == "emitted"
    r2 = await emit_today_drop(r, arc_id)
    assert r2["result"] == "skipped_emitted"

    # Stream should have exactly one entry for this drop.
    entries = await r.xrevrange(DROP_EVENT_STREAM, count=10)
    assert len(entries) >= 1
    # Newest entry decoded
    _id, fields = entries[0]
    data = json.loads(fields["data"])
    assert data["arc_id"] == arc_id
    assert data["day_index"] == 0


@pytest.mark.asyncio
async def test_worker_decay_detection(client, clean_redis):
    """A participant whose last_play_at is stale gets flagged."""
    r = clean_redis
    resp = await client.post(
        "/api/v1/campaign-arcs/create",
        json={
            "brand_id": "b1",
            "name": "decay",
            "arc_type": "advent_calendar",
            "duration_days": 30,
        },
    )
    arc_id = resp.json()["arc_id"]
    # Real play to bootstrap the participant set.
    await client.post(
        f"/api/v1/campaign-arcs/{arc_id}/play", json={"user_id": "ulazy"}
    )
    # Backdate last_play_at to 10 days ago.
    stale_ts = time.time() - 10 * DAY_SECONDS
    await r.hset(
        f"arc:{arc_id}:user:ulazy",
        mapping={"last_play_at": str(stale_ts)},
    )

    result = await detect_engagement_decay(r, arc_id, threshold_days=3)
    assert result["decayed"] == 1


@pytest.mark.asyncio
async def test_worker_full_sweep(client, clean_redis):
    """End-to-end: sweep walks every active arc + emits drops once."""
    r = clean_redis
    for i in range(2):
        await client.post(
            "/api/v1/campaign-arcs/create",
            json={
                "brand_id": f"b{i}",
                "name": f"sweep_{i}",
                "arc_type": "advent_calendar",
                "duration_days": 4,
            },
        )
    ids = await discover_active_arc_ids(r)
    assert len(ids) == 2

    summary = await run_sweep(r)
    assert summary["ok"] is True
    assert summary["arcs_scanned"] == 2
    assert summary["drops_emitted"] == 2

    # Second sweep: all skipped (idempotency).
    summary2 = await run_sweep(r)
    assert summary2["drops_emitted"] == 0
    assert summary2["drops_skipped"] == 2


@pytest.mark.asyncio
async def test_arc_can_wrap_existing_campaigns(client):
    """Backward-compat hook: arcs may reference existing campaigns."""
    # Create a vanilla single-session campaign first.
    cresp = await client.post(
        "/api/v1/campaigns/create",
        json={
            "brand_id": "bWrap",
            "name": "wrapper",
            "objective": "engagement",
            "bid_strategy": "max_delivery",
            "max_bid_cents": 100,
            "daily_budget_cents": 1000,
            "total_budget_cents": 10000,
        },
    )
    assert cresp.status_code == 200, cresp.text
    campaign_id = cresp.json()["campaign_id"]

    # Wrap it in a multi-week arc.
    aresp = await client.post(
        "/api/v1/campaign-arcs/create",
        json={
            "brand_id": "bWrap",
            "name": "monopoly_over_campaign",
            "arc_type": "monopoly_collect_n",
            "duration_days": 7,
            "wrapped_campaign_ids": [campaign_id],
            "config": {"piece_set": ["a", "b"], "collect_n": 2},
        },
    )
    assert aresp.status_code == 200
    arc_id = aresp.json()["arc_id"]
    detail = await client.get(f"/api/v1/campaign-arcs/{arc_id}")
    assert campaign_id in detail.json()["wrapped_campaign_ids"]


@pytest.mark.asyncio
async def test_scheduled_arc_today_returns_waiting(client, clean_redis):
    """An arc with future start_at returns status=waiting on /today."""
    future = time.time() + 3 * DAY_SECONDS
    resp = await client.post(
        "/api/v1/campaign-arcs/create",
        json={
            "brand_id": "b1",
            "name": "future",
            "arc_type": "advent_calendar",
            "duration_days": 5,
            "start_at": future,
        },
    )
    arc_id = resp.json()["arc_id"]
    today = await client.get(f"/api/v1/campaign-arcs/{arc_id}/today")
    assert today.status_code == 200
    assert today.json()["status"] == "waiting"
