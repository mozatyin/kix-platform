"""Reservations router tests — create, check-in, cancel, double check-in guard."""

from __future__ import annotations

import time

import pytest


def _future_ts(seconds: int = 3600) -> int:
    return int(time.time()) + seconds


@pytest.mark.asyncio
async def test_reservation_create_basic(client, clean_redis):
    res = await client.post(
        "/api/v1/reservations/create",
        json={
            "brand_id": "b_res_1",
            "user_id": "u_res_1",
            "scheduled_at": _future_ts(7200),
            "party_size": 2,
            "type": "dining",
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["reservation_id"].startswith("res_")
    assert body["status"] == "confirmed"


@pytest.mark.asyncio
async def test_reservation_create_past_time_rejected(client, clean_redis):
    """bug-bait: scheduled_at in the past must be 400."""
    res = await client.post(
        "/api/v1/reservations/create",
        json={
            "brand_id": "b_res_2",
            "user_id": "u_res_2",
            "scheduled_at": int(time.time()) - 3600,  # one hour ago
            "party_size": 1,
            "type": "appointment",
        },
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_reservation_create_party_size_validation(client, clean_redis):
    """bug-bait: party_size < 1 is 422 (ge=1)."""
    res = await client.post(
        "/api/v1/reservations/create",
        json={
            "brand_id": "b_res_3",
            "user_id": "u_res_3",
            "scheduled_at": _future_ts(7200),
            "party_size": 0,
            "type": "dining",
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_reservation_check_in_too_early(client, clean_redis):
    """Check-in well before scheduled_at returns 409 (outside grace window)."""
    create = await client.post(
        "/api/v1/reservations/create",
        json={
            "brand_id": "b_res_4",
            "user_id": "u_res_4",
            # 4h in the future, default grace is 15 min — too early to check in.
            "scheduled_at": _future_ts(4 * 3600),
            "party_size": 1,
            "type": "dining",
        },
    )
    assert create.status_code == 201
    rid = create.json()["reservation_id"]

    res = await client.post(
        f"/api/v1/reservations/{rid}/check-in",
        json={"evidence": "manual"},
    )
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_reservation_check_in_unknown_id(client, clean_redis):
    """bug-bait: check-in on unknown reservation_id must be 404."""
    res = await client.post(
        "/api/v1/reservations/res_does_not_exist/check-in",
        json={"evidence": "manual"},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_reservation_cancel_succeeds(client, clean_redis):
    create = await client.post(
        "/api/v1/reservations/create",
        json={
            "brand_id": "b_res_5",
            "user_id": "u_res_5",
            "scheduled_at": _future_ts(7200),
            "party_size": 2,
            "type": "appointment",
        },
    )
    rid = create.json()["reservation_id"]

    res = await client.post(
        f"/api/v1/reservations/{rid}/cancel",
        json={"by": "user", "reason": "plans changed"},
    )
    assert res.status_code == 200
    # Status should now be a cancelled_* variant
    get = await client.get(f"/api/v1/reservations/{rid}")
    assert get.status_code == 200
    assert get.json()["status"].startswith("cancelled")


@pytest.mark.asyncio
async def test_reservation_double_cancel_is_409(client, clean_redis):
    """bug-bait: cancelling a cancelled reservation should not silently succeed."""
    create = await client.post(
        "/api/v1/reservations/create",
        json={
            "brand_id": "b_res_6",
            "user_id": "u_res_6",
            "scheduled_at": _future_ts(7200),
            "party_size": 1,
            "type": "appointment",
        },
    )
    rid = create.json()["reservation_id"]

    first = await client.post(
        f"/api/v1/reservations/{rid}/cancel",
        json={"by": "user"},
    )
    assert first.status_code == 200

    # Second cancel — status is already cancelled_*, must conflict, not crash.
    second = await client.post(
        f"/api/v1/reservations/{rid}/cancel",
        json={"by": "user"},
    )
    # 409 (already cancelled) is the expected guard.
    assert second.status_code in {409, 400}


@pytest.mark.asyncio
async def test_reservation_get_unknown_is_404(client, clean_redis):
    res = await client.get("/api/v1/reservations/res_unknown_xxx")
    assert res.status_code == 404
