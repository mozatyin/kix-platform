"""Tests — Wave F quick-poll widget."""

from __future__ import annotations

import pytest

from app.main import app
from app.deps import get_current_user
from app.services import wavef_poll as svc


def _override_user(user_id: str = "u-poll-1"):
    async def _fake():
        return {
            "sub": user_id,
            "brand_id": "b1",
            "device_sig": "dev",
            "session_id": "s",
            "is_day1": False,
            "exp": 0,
        }
    return _fake


# ── Service ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_service_create_and_vote(clean_redis):
    r = clean_redis
    poll = await svc.create_poll(r, "b1", "Best topping?", ["Cheese", "Pepperoni"])
    assert poll["poll_id"]
    assert len(poll["options"]) == 2

    res1 = await svc.vote(r, poll["poll_id"], "u1", "opt0")
    assert res1["accepted"] is True
    assert res1["totals"]["opt0"] == 1

    # Same user cannot double-vote.
    res2 = await svc.vote(r, poll["poll_id"], "u1", "opt1")
    assert res2["accepted"] is False
    # Counts unchanged.
    assert res2["totals"]["opt0"] == 1
    assert res2["totals"]["opt1"] == 0


@pytest.mark.asyncio
async def test_service_rejects_invalid_option(clean_redis):
    r = clean_redis
    poll = await svc.create_poll(r, "b1", "X?", ["A", "B"])
    with pytest.raises(ValueError):
        await svc.vote(r, poll["poll_id"], "u1", "opt99")


@pytest.mark.asyncio
async def test_service_rejects_too_few_options(clean_redis):
    r = clean_redis
    with pytest.raises(ValueError):
        await svc.create_poll(r, "b1", "Y?", ["only"])


@pytest.mark.asyncio
async def test_service_results_aggregates(clean_redis):
    r = clean_redis
    poll = await svc.create_poll(r, "b1", "Choose", ["A", "B", "C"])
    for u, opt in [("u1", "opt0"), ("u2", "opt0"), ("u3", "opt1")]:
        await svc.vote(r, poll["poll_id"], u, opt)
    res = await svc.results(r, poll["poll_id"])
    assert res["totals"] == {"opt0": 2, "opt1": 1, "opt2": 0}
    assert res["total_voters"] == 3


# ── Router ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_full_flow(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user("creator")
    try:
        r = await client.post(
            "/api/v1/wavef/poll/",
            json={"brand_id": "b1", "question": "Best?", "options": ["A", "B"]},
        )
        assert r.status_code == 200, r.text
        pid = r.json()["poll_id"]

        # vote
        app.dependency_overrides[get_current_user] = _override_user("voter-1")
        v = await client.post(
            f"/api/v1/wavef/poll/{pid}/vote",
            json={"option_id": "opt0"},
        )
        assert v.status_code == 200
        assert v.json()["accepted"] is True

        # results
        res = await client.get(f"/api/v1/wavef/poll/{pid}/results")
        assert res.status_code == 200
        body = res.json()
        assert body["totals"]["opt0"] == 1
        assert body["total_voters"] == 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_404_on_unknown_poll(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        res = await client.get("/api/v1/wavef/poll/does-not-exist/results")
        assert res.status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)
