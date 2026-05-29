"""Fraud router tests — incidents, trust score, AML, velocity."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_fraud_incident_report_basic(client, clean_redis):
    res = await client.post(
        "/api/v1/fraud/incident/report",
        json={
            "brand_id": "b_fraud_1",
            "incident_type": "vandalism",
            "actor_user_id": "u_fraud_actor_1",
            "evidence": {
                "description": "broken glass at storefront",
                "severity": "medium",
                "urls": [],
            },
            "reporter_user_id": "u_fraud_reporter_1",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["incident_id"]
    assert body["status"] in {"open", "auto_resolved"}


@pytest.mark.asyncio
async def test_fraud_incident_report_requires_party(client, clean_redis):
    """bug-bait: incident with no actor / target / resource must fail validation."""
    res = await client.post(
        "/api/v1/fraud/incident/report",
        json={
            "brand_id": "b_fraud_2",
            "incident_type": "other",
            "evidence": {"description": "x", "severity": "low"},
            "reporter_user_id": "u_fraud_2",
            # No actor_user_id / target_user_id / related_resource_id.
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_fraud_trust_score_lazy_init_within_bounds(client, clean_redis):
    """Fresh user gets a neutral default score within [0, 100]."""
    res = await client.get("/api/v1/fraud/trust-score/u_fraud_3")
    assert res.status_code == 200
    body = res.json()
    # bug-bait check: trust_score must always be in [0, 100].
    assert 0 <= body["score"] <= 100


@pytest.mark.asyncio
async def test_fraud_trust_adjust_clamps_to_max(client, clean_redis):
    """Adjusting by +100 from a fresh score must clamp to 100, not exceed."""
    res = await client.post(
        "/api/v1/fraud/trust-score/u_fraud_4/adjust",
        json={"delta": 100, "reason": "verified founder"},
    )
    assert res.status_code == 200
    body = res.json()
    # bug-bait check: clamped at 100 even if delta would push over.
    assert body["new_score"] <= 100
    assert body["new_score"] >= 0


@pytest.mark.asyncio
async def test_fraud_trust_adjust_clamps_to_min(client, clean_redis):
    """Adjusting by -100 from default must clamp to 0, never negative."""
    res = await client.post(
        "/api/v1/fraud/trust-score/u_fraud_5/adjust",
        json={"delta": -100, "reason": "abuse"},
    )
    assert res.status_code == 200
    body = res.json()
    # bug-bait check: trust score never goes below 0.
    assert body["new_score"] >= 0


@pytest.mark.asyncio
async def test_fraud_trust_adjust_oversized_delta_rejected(client, clean_redis):
    """bug-bait: delta outside [-100, 100] is rejected at schema layer."""
    res = await client.post(
        "/api/v1/fraud/trust-score/u_fraud_6/adjust",
        json={"delta": 500, "reason": "x"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_fraud_aml_report_large_amount_escalates(client, clean_redis):
    res = await client.post(
        "/api/v1/fraud/aml/report",
        json={
            "user_id": "u_fraud_7",
            "amount_cents": 10_000_000,  # $100,000 — well over large_amount threshold
            "currency": "USD",
            "flag_type": "large_amount",
            "evidence": "single $100k transfer",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["aml_report_id"]
    assert body["escalated"] is True


@pytest.mark.asyncio
async def test_fraud_velocity_check_requires_identifier(client, clean_redis):
    """bug-bait: neither user_id nor device_fp → 422."""
    res = await client.post(
        "/api/v1/fraud/velocity/check",
        json={"action_type": "login"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_fraud_velocity_check_allows_first_hit(client, clean_redis):
    res = await client.post(
        "/api/v1/fraud/velocity/check",
        json={
            "user_id": "u_fraud_8",
            "action_type": "login",
            "window_seconds": 3600,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["count_in_window"] >= 1
    assert body["action"] in {"allow", "throttle", "block"}
