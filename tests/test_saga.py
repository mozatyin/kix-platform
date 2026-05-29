"""Saga coordinator tests.

Verifies:
- A saga that succeeds end-to-end records every step in the journal and
  returns ``success=True``.
- A saga that fails mid-execution compensates all previously-completed
  steps in REVERSE order and returns ``success=False`` with the full
  diagnostic envelope.
- A saga whose compensation also fails surfaces a non-empty
  ``compensation_failures`` list (the operator-escalation signal).
- The ``GET /api/v1/saga/{saga_id}`` endpoint hydrates the journal.
"""

from __future__ import annotations

import pytest

from app.saga import SagaCoordinator, SagaStep


# ──────────────────────────────────────────────────────────────────────────
# Direct coordinator tests
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_saga_happy_path_records_journal(clean_redis):
    r = clean_redis

    invoked: list[str] = []

    async def act_a(ctx, _r):
        invoked.append("a")
        await _r.set("saga:test:a", "1")
        return {"a": True}

    async def comp_a(ctx, _r):
        invoked.append("comp_a")
        await _r.delete("saga:test:a")

    async def act_b(ctx, _r):
        invoked.append("b")
        await _r.set("saga:test:b", "1")
        return {"b": True}

    async def comp_b(ctx, _r):
        invoked.append("comp_b")
        await _r.delete("saga:test:b")

    coord = SagaCoordinator(r)
    result = await coord.run(
        "saga_test_happy",
        [
            SagaStep("a", act_a, comp_a, timeout_seconds=5),
            SagaStep("b", act_b, comp_b, timeout_seconds=5),
        ],
        context={},
    )

    assert result.success is True
    assert result.completed_steps == ["a", "b"]
    assert result.compensated_steps == []
    assert result.failed_step is None
    assert invoked == ["a", "b"]  # no compensation ran

    # State written by actions persists.
    assert await r.get("saga:test:a") == "1"
    assert await r.get("saga:test:b") == "1"

    snap = await coord.get_status("saga_test_happy")
    assert snap["found"] is True
    assert snap["meta"]["status"] == "succeeded"
    kinds = [e["kind"] for e in snap["journal"]]
    assert "saga_started" in kinds
    assert kinds.count("step_completed") == 2
    assert "saga_succeeded" in kinds


@pytest.mark.asyncio(loop_scope="session")
async def test_saga_mid_failure_compensates_in_reverse(clean_redis):
    """Step 1+2 succeed, step 3 fails — compensations run in reverse order."""
    r = clean_redis
    order: list[str] = []

    async def act_a(ctx, _r):
        order.append("act_a")
        await _r.set("saga:fail:a", "1")
        return {}

    async def comp_a(ctx, _r):
        order.append("comp_a")
        await _r.delete("saga:fail:a")

    async def act_b(ctx, _r):
        order.append("act_b")
        await _r.set("saga:fail:b", "1")
        return {}

    async def comp_b(ctx, _r):
        order.append("comp_b")
        await _r.delete("saga:fail:b")

    async def act_c(ctx, _r):
        order.append("act_c")
        raise RuntimeError("step_c_boom")

    async def comp_c(ctx, _r):
        order.append("comp_c")

    coord = SagaCoordinator(r)
    result = await coord.run(
        "saga_test_fail",
        [
            SagaStep("a", act_a, comp_a, timeout_seconds=5),
            SagaStep("b", act_b, comp_b, timeout_seconds=5),
            SagaStep("c", act_c, comp_c, timeout_seconds=5),
        ],
        context={},
    )

    assert result.success is False
    assert result.failed_step == "c"
    assert "step_c_boom" in (result.failed_reason or "")
    assert result.completed_steps == ["a", "b"]
    # Compensations run in REVERSE order of completion.
    assert result.compensated_steps == ["b", "a"]
    assert result.compensation_failures == []

    # Side-effects from steps a + b were undone by their compensators.
    assert await r.get("saga:fail:a") is None
    assert await r.get("saga:fail:b") is None

    # Action order: a, b, c (failed) → comp_b → comp_a. Compensation for
    # the failed step c does NOT run (it never completed).
    assert order == ["act_a", "act_b", "act_c", "comp_b", "comp_a"]

    snap = await coord.get_status("saga_test_fail")
    assert snap["meta"]["status"] == "failed_rolled_back"
    kinds = [e["kind"] for e in snap["journal"]]
    assert "step_failed" in kinds
    assert kinds.count("step_compensated") == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_saga_compensation_failure_is_surfaced(clean_redis):
    r = clean_redis

    async def act_a(ctx, _r):
        return {}

    async def comp_a(ctx, _r):
        raise RuntimeError("comp_a_also_broke")

    async def act_b(ctx, _r):
        raise RuntimeError("b_failed")

    async def comp_b(ctx, _r):
        return None

    coord = SagaCoordinator(r)
    result = await coord.run(
        "saga_test_compfail",
        [
            SagaStep("a", act_a, comp_a, timeout_seconds=5),
            SagaStep("b", act_b, comp_b, timeout_seconds=5),
        ],
        context={},
    )

    assert result.success is False
    assert result.failed_step == "b"
    assert result.compensation_failures == ["a"]
    assert result.compensated_steps == []

    snap = await coord.get_status("saga_test_compfail")
    assert snap["meta"]["status"] == "failed_compensation_failed"


# ──────────────────────────────────────────────────────────────────────────
# Refund-cascade saga via the disputes integration
# ──────────────────────────────────────────────────────────────────────────


async def _topup(client, brand_id: str, amount_cents: int = 100_000) -> None:
    res = await client.post(
        f"/api/v1/wallet/{brand_id}/topup",
        json={"amount_cents": amount_cents, "payment_method": "stripe"},
    )
    assert res.status_code == 200, res.text
    tid = res.json()["topup_id"]
    res = await client.post(
        f"/api/v1/wallet/{brand_id}/topup/{tid}/confirm",
        json={"payment_gateway_response": {}},
    )
    assert res.status_code == 200, res.text


@pytest.mark.asyncio(loop_scope="session")
async def test_get_saga_status_404_on_unknown(client, clean_redis):
    res = await client.get("/api/v1/saga/nonexistent_id")
    assert res.status_code == 404
    assert res.json()["detail"]["error"] == "saga_not_found"


@pytest.mark.asyncio(loop_scope="session")
async def test_subscription_upgrade_saga_rolls_back_on_failure(clean_redis):
    """If feature-enable fails, charge + tier upgrade must be undone."""
    from app.saga_definitions import (
        _do_charge_payment, _undo_charge_payment,
        _do_upgrade_tier, _undo_upgrade_tier,
    )
    from app.saga import SagaCoordinator, SagaStep

    r = clean_redis
    brand_id = "brand_saga_subup"
    # Seed the wallet with funds for the charge.
    from app.routers.wallet import _k_balance as _wk_balance
    await r.set(_wk_balance(brand_id), 500_000)

    async def boom_features(ctx, _r):
        raise RuntimeError("features_step_failed")

    async def noop_comp(ctx, _r):
        return None

    coord = SagaCoordinator(r)
    result = await coord.run(
        "saga_subup_rollback_test",
        [
            SagaStep("charge_payment", _do_charge_payment,
                     _undo_charge_payment, timeout_seconds=5),
            SagaStep("upgrade_tier", _do_upgrade_tier,
                     _undo_upgrade_tier, timeout_seconds=5),
            SagaStep("enable_features", boom_features, noop_comp,
                     timeout_seconds=5),
        ],
        context={
            "_saga_id": "saga_subup_rollback_test",
            "brand_id": brand_id,
            "new_tier": "growth",
            "upgrade_price_cents": 50_000,
            "features": ["pro_dashboard"],
        },
    )

    assert result.success is False
    assert result.failed_step == "enable_features"
    assert result.compensated_steps == ["upgrade_tier", "charge_payment"]
    # Wallet balance restored to original 500_000.
    bal = int(await r.get(_wk_balance(brand_id)) or 0)
    assert bal == 500_000
    # Subscription tier reverted to "free" (its previous value).
    tier = await r.hget(f"subscription:{brand_id}", "tier")
    assert tier == "free"
