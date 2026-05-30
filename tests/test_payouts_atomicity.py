"""Atomicity, idempotency, and saga tests for the v2 cross-brand
commission-transfer endpoint (gap analysis R5 P0 fix).

These tests target the failure modes that the legacy
`/inter-brand-transfer` shape could not surface cleanly:

* partial credit / phantom debit on mid-pipeline failure,
* duplicate transfers under network retries,
* concurrent A↔B transfers,
* cross-currency commissions (SGD → CNY),
* self-transfer / negative-amount rejection,
* audit log emission on success + failure,
* reversal flow,
* performance under 100-way concurrency.

The test fixture flushes Redis between tests (see `conftest.py`), so
state from one test never leaks into another.
"""

from __future__ import annotations

import asyncio
import time

import pytest


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


COMMISSION_URL = "/api/v1/payouts/commission-transfer"
LEGACY_URL = "/api/v1/payouts/inter-brand-transfer"
AUDIT_KEY = "payouts:audit:inter_brand"


async def _topup(client, brand_id: str, amount_cents: int, *, currency: str | None = None) -> None:
    """Top up a brand wallet and confirm so the wallet balance key exists.

    Optionally lock the wallet's base currency on first topup — used by
    the cross-currency saga test.
    """
    body: dict = {"amount_cents": amount_cents, "payment_method": "stripe"}
    if currency:
        body["currency"] = currency
    res = await client.post(f"/api/v1/wallet/{brand_id}/topup", json=body)
    assert res.status_code == 200, res.text
    topup_id = res.json()["topup_id"]
    res = await client.post(
        f"/api/v1/wallet/{brand_id}/topup/{topup_id}/confirm",
        json={"payment_gateway_response": {}},
    )
    assert res.status_code == 200, res.text


async def _balance(client, brand_id: str) -> int:
    res = await client.get(f"/api/v1/wallet/{brand_id}")
    assert res.status_code == 200, res.text
    return int(res.json()["balance_cents"])


# ──────────────────────────────────────────────────────────────────────────
# 1. Happy path — both legs move atomically
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_atomic_happy_path(client, clean_redis):
    await _topup(client, "atom_a", 10_000)
    await _topup(client, "atom_b", 1)

    body = {
        "from_brand_id": "atom_a",
        "to_brand_id": "atom_b",
        "amount_cents": 2_500,
        "reason": "affiliate_commission",
        "idempotency_key": "idem_happy_1",
    }
    res = await client.post(COMMISSION_URL, json=body)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["amount_cents"] == 2_500
    assert data["debited_amount_cents"] == 2_500
    assert data["credited_amount_cents"] == 2_500
    assert data["fx_applied"] is False
    assert data["idempotent"] is False
    assert data["idempotency_key"] == "idem_happy_1"
    assert data["entry_id"].startswith("le_")

    assert await _balance(client, "atom_a") == 7_500
    assert await _balance(client, "atom_b") == 2_501


# ──────────────────────────────────────────────────────────────────────────
# 2. Insufficient source — destination must not be credited
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_insufficient_source_rejected_no_credit(client, clean_redis):
    await _topup(client, "atom_broke", 100)
    await _topup(client, "atom_rich", 1)

    res = await client.post(
        COMMISSION_URL,
        json={
            "from_brand_id": "atom_broke",
            "to_brand_id": "atom_rich",
            "amount_cents": 5_000,
            "reason": "supplier_payment",
            "idempotency_key": "idem_broke_1",
        },
    )
    assert res.status_code == 402, res.text
    assert res.json()["detail"]["error"] == "insufficient_funds"

    # Critical invariant: B unchanged, A unchanged.
    assert await _balance(client, "atom_broke") == 100
    assert await _balance(client, "atom_rich") == 1


# ──────────────────────────────────────────────────────────────────────────
# 3. Idempotency — same key twice → single transfer
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_idempotency_same_key_single_transfer(client, clean_redis):
    await _topup(client, "idm_src", 10_000)
    await _topup(client, "idm_dst", 1)

    body = {
        "from_brand_id": "idm_src",
        "to_brand_id": "idm_dst",
        "amount_cents": 750,
        "reason": "revenue_share",
        "idempotency_key": "idem_dup_key",
    }
    res1 = await client.post(COMMISSION_URL, json=body)
    res2 = await client.post(COMMISSION_URL, json=body)
    res3 = await client.post(COMMISSION_URL, json=body)
    assert res1.status_code == 200
    assert res2.status_code == 200
    assert res3.status_code == 200

    eid = res1.json()["entry_id"]
    assert res2.json()["entry_id"] == eid
    assert res3.json()["entry_id"] == eid
    assert res2.json()["idempotent"] is True
    assert res3.json()["idempotent"] is True

    # Balance only moved once.
    assert await _balance(client, "idm_src") == 9_250
    assert await _balance(client, "idm_dst") == 751


# ──────────────────────────────────────────────────────────────────────────
# 4. Concurrent A→B and B→A — both succeed (or retry to success)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_bidirectional_transfers(client, clean_redis):
    await _topup(client, "conc_a", 100_000)
    await _topup(client, "conc_b", 100_000)

    async def _send(from_b: str, to_b: str, amt: int, idem: str):
        return await client.post(
            COMMISSION_URL,
            json={
                "from_brand_id": from_b,
                "to_brand_id": to_b,
                "amount_cents": amt,
                "reason": "supplier_payment",
                "idempotency_key": idem,
            },
        )

    results = await asyncio.gather(
        _send("conc_a", "conc_b", 1_000, "ab_1"),
        _send("conc_b", "conc_a", 700, "ba_1"),
        _send("conc_a", "conc_b", 200, "ab_2"),
        _send("conc_b", "conc_a", 50, "ba_2"),
    )
    # Optimistic retry should make every leg eventually succeed (we have
    # ample funds on both sides and MAX_WATCH_RETRIES=8 head-room).
    for r in results:
        assert r.status_code == 200, r.text

    a_out = 1_000 + 200
    a_in = 700 + 50
    b_out = 700 + 50
    b_in = 1_000 + 200
    assert await _balance(client, "conc_a") == 100_000 - a_out + a_in
    assert await _balance(client, "conc_b") == 100_000 - b_out + b_in


# ──────────────────────────────────────────────────────────────────────────
# 5. Crash simulation — abort mid-pipeline, balances remain consistent
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_crash_simulation_consistency(client, clean_redis, monkeypatch):
    """Force the MULTI execute to raise on the very first attempt and
    verify the ledger never sees a partial application.

    The WATCH/MULTI primitive in Redis is the safety net: when execute
    fails Redis discards every queued command, so we should see *no*
    debit, *no* credit, and *no* ledger row — and a 503 returned to the
    caller (matching the implementation's contract).
    """
    await _topup(client, "crash_src", 10_000)
    await _topup(client, "crash_dst", 1)

    from app.routers import payouts as payouts_mod

    real_pipeline = payouts_mod.aioredis.Redis.pipeline

    poisoned = {"used": False}

    class _PoisonedPipe:
        def __init__(self, inner):
            self._inner = inner

        async def __aenter__(self):
            self._pipe = await self._inner.__aenter__()
            return self

        async def __aexit__(self, *a):
            return await self._inner.__aexit__(*a)

        def __getattr__(self, name):
            return getattr(self._pipe, name)

        async def execute(self):
            if not poisoned["used"]:
                poisoned["used"] = True
                raise RuntimeError("simulated_redis_crash")
            return await self._pipe.execute()

    def _patched(self, *args, **kwargs):
        inner = real_pipeline(self, *args, **kwargs)
        return _PoisonedPipe(inner)

    monkeypatch.setattr(payouts_mod.aioredis.Redis, "pipeline", _patched)

    res = await client.post(
        COMMISSION_URL,
        json={
            "from_brand_id": "crash_src",
            "to_brand_id": "crash_dst",
            "amount_cents": 1_000,
            "reason": "supplier_payment",
            "idempotency_key": "idem_crash_1",
        },
    )
    # Either 503 (crashed) or 200 (already-retried internally). What
    # matters is consistency: balances either both moved or neither did.
    assert res.status_code in (200, 503)
    src_bal = await _balance(client, "crash_src")
    dst_bal = await _balance(client, "crash_dst")
    # If the first attempt was poisoned and execute() threw, the impl
    # raises 503 immediately — no balance moves.
    if res.status_code == 503:
        assert src_bal == 10_000
        assert dst_bal == 1
    else:
        assert src_bal == 9_000
        assert dst_bal == 1_001


# ──────────────────────────────────────────────────────────────────────────
# 6. Cross-currency saga — SGD → CNY with FX
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_currency_fx_saga(client, clean_redis):
    # Lock currencies on first topup.
    await _topup(client, "fx_sgd", 10_000, currency="SGD")
    await _topup(client, "fx_cny", 1, currency="CNY")

    # Without allow_fx → 409.
    res = await client.post(
        COMMISSION_URL,
        json={
            "from_brand_id": "fx_sgd",
            "to_brand_id": "fx_cny",
            "amount_cents": 500,
            "reason": "revenue_share",
            "idempotency_key": "idem_fx_reject",
        },
    )
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["error"] == "currency_mismatch"
    # No balance moves.
    assert await _balance(client, "fx_sgd") == 10_000
    assert await _balance(client, "fx_cny") == 1

    # With allow_fx → conversion applied (stub FX uses identity for
    # same-decimal pairs SGD↔CNY).
    res = await client.post(
        COMMISSION_URL,
        json={
            "from_brand_id": "fx_sgd",
            "to_brand_id": "fx_cny",
            "amount_cents": 500,
            "reason": "revenue_share",
            "idempotency_key": "idem_fx_apply",
            "allow_fx": True,
        },
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["fx_applied"] is True
    assert data["debited_currency"] == "SGD"
    assert data["credited_currency"] == "CNY"
    assert data["debited_amount_cents"] == 500
    assert data["credited_amount_cents"] > 0
    assert await _balance(client, "fx_sgd") == 9_500
    assert await _balance(client, "fx_cny") == 1 + data["credited_amount_cents"]


# ──────────────────────────────────────────────────────────────────────────
# 7. Negative amount — rejected at the Pydantic boundary
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_negative_amount_rejected(client, clean_redis):
    await _topup(client, "neg_x", 1_000)
    await _topup(client, "neg_y", 1)
    res = await client.post(
        COMMISSION_URL,
        json={
            "from_brand_id": "neg_x",
            "to_brand_id": "neg_y",
            "amount_cents": -100,
            "reason": "supplier_payment",
            "idempotency_key": "idem_neg",
        },
    )
    # Field validator `gt=0` → 422.
    assert res.status_code == 422, res.text


# ──────────────────────────────────────────────────────────────────────────
# 8. Self-transfer rejected
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_self_transfer_rejected(client, clean_redis):
    await _topup(client, "self_solo", 5_000)
    res = await client.post(
        COMMISSION_URL,
        json={
            "from_brand_id": "self_solo",
            "to_brand_id": "self_solo",
            "amount_cents": 100,
            "reason": "other",
            "idempotency_key": "idem_self",
        },
    )
    assert res.status_code == 400, res.text
    assert res.json()["detail"]["error"] == "self_transfer_not_allowed"
    assert await _balance(client, "self_solo") == 5_000


# ──────────────────────────────────────────────────────────────────────────
# 9. Audit log emitted on success + failure
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_log_emitted_success_and_failure(client, clean_redis):
    import json

    await _topup(client, "aud_a", 1_000)
    await _topup(client, "aud_b", 1)

    # Success.
    res = await client.post(
        COMMISSION_URL,
        json={
            "from_brand_id": "aud_a",
            "to_brand_id": "aud_b",
            "amount_cents": 200,
            "reason": "supplier_payment",
            "idempotency_key": "idem_aud_ok",
        },
    )
    assert res.status_code == 200

    # Failure (insufficient).
    res = await client.post(
        COMMISSION_URL,
        json={
            "from_brand_id": "aud_a",
            "to_brand_id": "aud_b",
            "amount_cents": 999_999,
            "reason": "supplier_payment",
            "idempotency_key": "idem_aud_bad",
        },
    )
    assert res.status_code == 402

    entries_raw = await clean_redis.lrange(AUDIT_KEY, 0, -1)
    entries = [json.loads(x) for x in entries_raw]
    outcomes = {e["outcome"] for e in entries}
    assert "success" in outcomes
    assert "rejected_insufficient_funds" in outcomes


# ──────────────────────────────────────────────────────────────────────────
# 10. Reversal flow — second transfer in the opposite direction nets to zero
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reversal_flow_round_trip(client, clean_redis):
    await _topup(client, "rev_a", 10_000)
    await _topup(client, "rev_b", 1)

    # Forward transfer.
    res = await client.post(
        COMMISSION_URL,
        json={
            "from_brand_id": "rev_a",
            "to_brand_id": "rev_b",
            "amount_cents": 1_500,
            "reason": "affiliate_commission",
            "idempotency_key": "idem_rev_fwd",
            "reference_id": "order_42_commission",
        },
    )
    assert res.status_code == 200, res.text

    # Reversal — opposite direction, dedicated reason + idem key.
    res = await client.post(
        COMMISSION_URL,
        json={
            "from_brand_id": "rev_b",
            "to_brand_id": "rev_a",
            "amount_cents": 1_500,
            "reason": "commission_reversal",
            "idempotency_key": "idem_rev_back",
            "reference_id": "order_42_commission_reversal",
            "ledger_entry_metadata": {"original_ref": "order_42_commission"},
        },
    )
    assert res.status_code == 200, res.text

    # Net zero (modulo the seed cent on rev_b).
    assert await _balance(client, "rev_a") == 10_000
    assert await _balance(client, "rev_b") == 1


# ──────────────────────────────────────────────────────────────────────────
# 11. Currency-mismatch surface — explicit error shape & no balance move
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_currency_mismatch_error_handling(client, clean_redis):
    await _topup(client, "cm_usd", 5_000, currency="USD")
    await _topup(client, "cm_jpy", 1, currency="JPY")

    res = await client.post(
        COMMISSION_URL,
        json={
            "from_brand_id": "cm_usd",
            "to_brand_id": "cm_jpy",
            "amount_cents": 500,
            "reason": "supplier_payment",
            "idempotency_key": "idem_cm_1",
        },
    )
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["error"] == "currency_mismatch"
    assert detail["from_currency"] == "USD"
    assert detail["to_currency"] == "JPY"
    # No balance moves on rejection.
    assert await _balance(client, "cm_usd") == 5_000
    assert await _balance(client, "cm_jpy") == 1


# ──────────────────────────────────────────────────────────────────────────
# 12. Performance — 100 concurrent transfers complete within 5s
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_performance_100_concurrent_transfers(client, clean_redis):
    """Stress: 100 independent A_i → B_i transfers run concurrently.

    Each transfer uses a distinct brand pair so there is no contention
    in the ideal case, but the test still verifies the impl scales
    without locking up.
    """
    N = 100
    # Pre-seed wallets.
    await asyncio.gather(*[_topup(client, f"perf_a_{i}", 10_000) for i in range(N)])
    await asyncio.gather(*[_topup(client, f"perf_b_{i}", 1) for i in range(N)])

    async def _send(i: int):
        return await client.post(
            COMMISSION_URL,
            json={
                "from_brand_id": f"perf_a_{i}",
                "to_brand_id": f"perf_b_{i}",
                "amount_cents": 100,
                "reason": "supplier_payment",
                "idempotency_key": f"idem_perf_{i}",
            },
        )

    t0 = time.perf_counter()
    results = await asyncio.gather(*[_send(i) for i in range(N)])
    elapsed = time.perf_counter() - t0

    ok = sum(1 for r in results if r.status_code == 200)
    assert ok == N, f"only {ok}/{N} succeeded"
    assert elapsed < 5.0, f"100 transfers took {elapsed:.2f}s, > 5s budget"


# ──────────────────────────────────────────────────────────────────────────
# Bonus: legacy endpoint still works (backward compat smoke)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_legacy_endpoint_still_works(client, clean_redis):
    """The deprecated /inter-brand-transfer endpoint keeps its response
    shape so existing callers (disputes / commission claw-back / pixel)
    are not broken by the v2 rollout.
    """
    await _topup(client, "legacy_a", 5_000)
    await _topup(client, "legacy_b", 1)

    res = await client.post(
        LEGACY_URL,
        json={
            "from_brand_id": "legacy_a",
            "to_brand_id": "legacy_b",
            "amount_cents": 500,
            "reason": "supplier_payment",
            "reference_id": "legacy_ref_1",
        },
    )
    assert res.status_code == 200, res.text
    data = res.json()
    # Legacy shape must NOT leak v2-only fields.
    assert set(data.keys()) == {
        "entry_id", "from_brand_id", "to_brand_id", "amount_cents",
        "currency", "reason", "reference_id", "ts", "metadata", "idempotent",
    }
    assert await _balance(client, "legacy_a") == 4_500
    assert await _balance(client, "legacy_b") == 501
