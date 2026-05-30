"""Tests — Wave F collect-a-set mechanic (spec #12)."""

from __future__ import annotations

import random

import pytest

from app.main import app
from app.deps import get_current_user
from app.services import wavef_sets as svc


def _override_user(uid: str = "u-sets-1"):
    async def _fake():
        return {
            "sub": uid,
            "brand_id": "b1",
            "device_sig": "dev",
            "session_id": "s",
            "is_day1": False,
            "exp": 0,
        }
    return _fake


# ── Service ──────────────────────────────────────────────────────────────


def _pieces(grand_weight: float = 1.0):
    return [
        {"id": "p1", "label": "Boardwalk", "rarity_weight": 1.0},
        {"id": "p2", "label": "Park Place", "rarity_weight": 1.0},
        {"id": "p3", "label": "Mediterranean", "rarity_weight": 1.0},
        {"id": "p4", "label": "Reading RR", "rarity_weight": 1.0},
        {"id": "grand", "label": "Mr. Monopoly", "rarity_weight": grand_weight, "grand": True},
    ]


@pytest.mark.asyncio
async def test_create_and_draw(clean_redis):
    r = clean_redis
    cmp = await svc.create_campaign(
        r, brand_id="b1", name="Monopoly", pieces=_pieces(), target=3,
    )
    assert cmp["campaign_id"]
    rng = random.Random(0)
    res = await svc.draw(r, cmp["campaign_id"], "u1", rng=rng)
    assert res["piece"]["id"] in {p["id"] for p in cmp["pieces"]}
    assert res["distinct"] >= 1


@pytest.mark.asyncio
async def test_redeem_requires_full_set(clean_redis):
    r = clean_redis
    cmp = await svc.create_campaign(
        r, brand_id="b1", name="X", pieces=_pieces(), target=3,
    )
    with pytest.raises(ValueError):
        await svc.redeem(r, cmp["campaign_id"], "u1")


@pytest.mark.asyncio
async def test_full_set_redeems_once(clean_redis):
    r = clean_redis
    cmp = await svc.create_campaign(
        r, brand_id="b1", name="X", pieces=_pieces(), target=3,
    )
    # Inject inventory directly to dodge randomness.
    for pid in ("p1", "p2", "p3"):
        await r.hincrby(f"sets:cmp:{cmp['campaign_id']}:user:u9", pid, 1)
    first = await svc.redeem(r, cmp["campaign_id"], "u9")
    assert first["redeemed"] is True
    with pytest.raises(PermissionError):
        await svc.redeem(r, cmp["campaign_id"], "u9")


@pytest.mark.asyncio
async def test_draw_blocked_after_redeem(clean_redis):
    r = clean_redis
    cmp = await svc.create_campaign(
        r, brand_id="b1", name="X", pieces=_pieces(), target=3,
    )
    for pid in ("p1", "p2", "p3"):
        await r.hincrby(f"sets:cmp:{cmp['campaign_id']}:user:uX", pid, 1)
    await svc.redeem(r, cmp["campaign_id"], "uX")
    with pytest.raises(ValueError):
        await svc.draw(r, cmp["campaign_id"], "uX", rng=random.Random(0))


@pytest.mark.asyncio
async def test_distribution_matches_weights(clean_redis):
    r = clean_redis
    # Two-piece set: weights 1 and 4 → ~20%/80% split (target=2 disables boost).
    pieces = [
        {"id": "a", "label": "A", "rarity_weight": 1.0},
        {"id": "b", "label": "B", "rarity_weight": 4.0},
    ]
    cmp = await svc.create_campaign(
        r, brand_id="b1", name="weighttest", pieces=pieces, target=2,
    )
    rng = random.Random(42)
    counts = {"a": 0, "b": 0}
    # Use a fresh uid each draw so we never complete the set.
    for i in range(2000):
        res = await svc.draw(r, cmp["campaign_id"], f"u{i}", rng=rng)
        counts[res["piece"]["id"]] += 1
    total = counts["a"] + counts["b"]
    share_b = counts["b"] / total
    assert 0.74 <= share_b <= 0.86, (counts, share_b)


@pytest.mark.asyncio
async def test_create_rejects_bad_config(clean_redis):
    r = clean_redis
    with pytest.raises(ValueError):
        await svc.create_campaign(
            r, brand_id="b1", name="X", pieces=_pieces(), target=99,
        )
    with pytest.raises(ValueError):
        await svc.create_campaign(
            r,
            brand_id="b1",
            name="X",
            pieces=[
                {"id": "a", "label": "A", "rarity_weight": 1.0, "grand": True},
                {"id": "b", "label": "B", "rarity_weight": 1.0, "grand": True},
            ],
            target=2,
        )


@pytest.mark.asyncio
async def test_anti_frustration_boost_helps_finisher(clean_redis):
    """When user is one-short and missing grand, boost kicks in."""
    pieces = [
        {"id": "a", "label": "A", "rarity_weight": 1.0},
        {"id": "b", "label": "B", "rarity_weight": 1.0},
        {"id": "grand", "label": "Grand", "rarity_weight": 0.01, "grand": True},
    ]
    inv = {"a": 1, "b": 1}
    base = svc._boosted_weights(pieces, inv, target=3, misses=0)
    boosted = svc._boosted_weights(pieces, inv, target=3, misses=10)
    grand_idx = next(i for i, p in enumerate(pieces) if p["id"] == "grand")
    assert boosted[grand_idx] > base[grand_idx]
    # Cap is 5x.
    capped = svc._boosted_weights(pieces, inv, target=3, misses=1000)
    assert capped[grand_idx] == pytest.approx(pieces[grand_idx]["rarity_weight"] * 5)


# ── Router ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_create_and_inventory(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user("u-router-1")
    try:
        resp = await client.post(
            "/api/v1/wavef/sets/campaigns",
            json={
                "brand_id": "b1",
                "name": "Coffee Pieces",
                "pieces": [
                    {"id": "p1", "label": "Bean", "rarity_weight": 1.0},
                    {"id": "p2", "label": "Cup",  "rarity_weight": 1.0},
                    {"id": "g",  "label": "Gold", "rarity_weight": 0.01, "grand": True},
                ],
                "target": 2,
            },
        )
        assert resp.status_code == 200, resp.text
        cid = resp.json()["campaign_id"]
        d = await client.post(f"/api/v1/wavef/sets/campaigns/{cid}/draw")
        assert d.status_code == 200
        inv = await client.get(
            f"/api/v1/wavef/sets/campaigns/{cid}/inventory/u-router-1"
        )
        assert inv.status_code == 200
        assert inv.json()["distinct"] >= 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_redeem_incomplete_returns_400(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user("u-router-2")
    try:
        resp = await client.post(
            "/api/v1/wavef/sets/campaigns",
            json={
                "brand_id": "b1",
                "name": "X",
                "pieces": [
                    {"id": "p1", "label": "A", "rarity_weight": 1.0},
                    {"id": "p2", "label": "B", "rarity_weight": 1.0},
                    {"id": "p3", "label": "C", "rarity_weight": 1.0},
                ],
                "target": 3,
            },
        )
        cid = resp.json()["campaign_id"]
        r2 = await client.post(f"/api/v1/wavef/sets/campaigns/{cid}/redeem")
        assert r2.status_code == 400
    finally:
        app.dependency_overrides.pop(get_current_user, None)
