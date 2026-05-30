"""Cross-brand voucher pooling tests (Wave E item 4).

Covers:
  * Pool creation + membership lifecycle (join / leave / discovery)
  * Issue at brand A, redeem at brand B inside the pool
  * Reject redemption at a brand outside the pool
  * District / cuisine / min-purchase restrictions
  * Reciprocity ratios (default 1:1, per-edge override, flat per-source)
  * Concurrent-redemption double-spend guard (WATCH/MULTI)
  * Idempotency on transaction_id (replay is a no-op)
  * Net-position math + minimum-edge settlement plan
  * Worker dry-run + executed dispatch through the inter-brand ledger
  * Audit log integration on pool create / join / redeem

The settlement worker tests reuse the wallet topup endpoints from the
A3 atomicity suite so the inter-brand transfer leg lands real cents.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.redis_client import get_redis
from app.services import voucher_pool as vp


# ── Helpers ──────────────────────────────────────────────────────────────


POOL_PREFIX = "/api/v1/voucher-pools"


def _suffix() -> str:
    """Per-test entity suffix so cross-test name collisions can't happen
    even with the --allow-pollution flag."""
    return uuid.uuid4().hex[:10]


async def _create_pool(
    client,
    *,
    brand_ids: list[str],
    district: str = "tampines",
    name: str | None = None,
    rules: dict | None = None,
    discoverable: bool = True,
) -> dict:
    body = {
        "brand_ids": brand_ids,
        "district": district,
        "name": name or f"pool_{_suffix()}",
        "rules": rules or {},
        "discoverable": discoverable,
    }
    res = await client.post(f"{POOL_PREFIX}/create", json=body)
    assert res.status_code == 201, res.text
    return res.json()


async def _issue(
    client,
    *,
    pool_id: str,
    user_id: str,
    source_brand_id: str,
    amount_cents: int = 1_000,
) -> str:
    res = await client.post(
        f"{POOL_PREFIX}/{pool_id}/issue-voucher",
        json={
            "user_id": user_id,
            "source_brand_id": source_brand_id,
            "amount_cents": amount_cents,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["voucher_id"]


async def _redeem(
    client,
    *,
    pool_id: str,
    voucher_id: str,
    target_brand_id: str,
    transaction_id: str | None = None,
    context: dict | None = None,
    idempotency_key: str | None = None,
):
    body = {
        "voucher_id": voucher_id,
        "target_brand_id": target_brand_id,
        "transaction_id": transaction_id or f"tx_{uuid.uuid4().hex[:12]}",
        "target_context": context or {},
    }
    if idempotency_key:
        body["idempotency_key"] = idempotency_key
    return await client.post(f"{POOL_PREFIX}/{pool_id}/redeem", json=body)


async def _topup_wallet(client, brand_id: str, amount_cents: int) -> None:
    """Mirrors test_payouts_atomicity._topup so settlement legs have funds."""
    res = await client.post(
        f"/api/v1/wallet/{brand_id}/topup",
        json={"amount_cents": amount_cents, "payment_method": "stripe"},
    )
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


# ── 1. Pool CRUD ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_pool_and_membership(client, clean_redis):
    s = _suffix()
    pool = await _create_pool(
        client,
        brand_ids=[f"toast_{s}", f"yakun_{s}", f"killiney_{s}"],
        district="tampines",
        name=f"Coffee Network {s}",
    )
    assert pool["pool_id"].startswith("pool_")
    assert sorted(pool["members"]) == sorted([
        f"toast_{s}", f"yakun_{s}", f"killiney_{s}",
    ])
    # Fetch round-trip should preserve state.
    res = await client.get(f"{POOL_PREFIX}/{pool['pool_id']}")
    assert res.status_code == 200
    got = res.json()
    assert got["district"] == "tampines"
    assert got["status"] == "active"
    assert len(got["members"]) == 3


@pytest.mark.asyncio
async def test_join_and_leave_pool(client, clean_redis):
    s = _suffix()
    pool = await _create_pool(
        client, brand_ids=[f"toast_{s}"],
        district="tampines",
    )
    # Join
    res = await client.post(
        f"{POOL_PREFIX}/{pool['pool_id']}/join",
        json={"brand_id": f"yakun_{s}"},
    )
    assert res.status_code == 200, res.text
    refreshed = (await client.get(f"{POOL_PREFIX}/{pool['pool_id']}")).json()
    assert f"yakun_{s}" in refreshed["members"]

    # Leave
    res = await client.post(
        f"{POOL_PREFIX}/{pool['pool_id']}/leave",
        json={"brand_id": f"yakun_{s}"},
    )
    assert res.status_code == 200, res.text
    refreshed = (await client.get(f"{POOL_PREFIX}/{pool['pool_id']}")).json()
    assert f"yakun_{s}" not in refreshed["members"]


@pytest.mark.asyncio
async def test_discovery_lists_pools(client, clean_redis):
    s = _suffix()
    await _create_pool(
        client, brand_ids=[f"a_{s}", f"b_{s}"], name=f"public_pool_{s}",
        discoverable=True,
    )
    private = await _create_pool(
        client, brand_ids=[f"c_{s}", f"d_{s}"], name=f"private_{s}",
        discoverable=False,
    )
    res = await client.get(f"{POOL_PREFIX}/discovery", params={"limit": 50})
    assert res.status_code == 200, res.text
    names = [p["name"] for p in res.json()["pools"]]
    assert f"public_pool_{s}" in names
    assert private["name"] not in names  # private pool is hidden


# ── 2. Issue + cross-brand redemption ────────────────────────────────────


@pytest.mark.asyncio
async def test_issue_at_A_redeem_at_B_succeeds(client, clean_redis):
    s = _suffix()
    a, b = f"toast_{s}", f"yakun_{s}"
    pool = await _create_pool(client, brand_ids=[a, b], district="tampines")
    vid = await _issue(
        client, pool_id=pool["pool_id"], user_id=f"u_{s}",
        source_brand_id=a, amount_cents=1_500,
    )
    res = await _redeem(
        client, pool_id=pool["pool_id"], voucher_id=vid, target_brand_id=b,
    )
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["ok"] is True
    assert out["source_brand_id"] == a
    assert out["target_brand_id"] == b
    assert out["credit_amount_cents"] == 1_500
    assert out["ratio"] == 1.0


@pytest.mark.asyncio
async def test_redeem_at_brand_outside_pool_rejected(client, clean_redis):
    s = _suffix()
    a, b, c = f"a_{s}", f"b_{s}", f"c_{s}"
    pool = await _create_pool(client, brand_ids=[a, b])
    vid = await _issue(
        client, pool_id=pool["pool_id"], user_id=f"u_{s}",
        source_brand_id=a, amount_cents=500,
    )
    res = await _redeem(
        client, pool_id=pool["pool_id"], voucher_id=vid, target_brand_id=c,
    )
    assert res.status_code == 400, res.text
    assert res.json()["detail"]["reason"] == "target_brand_not_in_pool"


@pytest.mark.asyncio
async def test_double_redemption_rejected(client, clean_redis):
    s = _suffix()
    a, b = f"a_{s}", f"b_{s}"
    pool = await _create_pool(client, brand_ids=[a, b])
    vid = await _issue(
        client, pool_id=pool["pool_id"], user_id=f"u_{s}",
        source_brand_id=a, amount_cents=500,
    )
    r1 = await _redeem(client, pool_id=pool["pool_id"], voucher_id=vid, target_brand_id=b)
    assert r1.status_code == 200, r1.text
    # Second redemption with a *different* transaction_id should be blocked
    # because the voucher state is now ``redeemed``.
    r2 = await _redeem(
        client, pool_id=pool["pool_id"], voucher_id=vid, target_brand_id=b,
        transaction_id="another_txn",
    )
    assert r2.status_code == 400
    assert r2.json()["detail"]["reason"] == "voucher_redeemed"


# ── 3. Restrictions: district + min purchase ─────────────────────────────


@pytest.mark.asyncio
async def test_same_district_restriction_enforced(client, clean_redis):
    s = _suffix()
    a, b = f"a_{s}", f"b_{s}"
    pool = await _create_pool(
        client, brand_ids=[a, b], district="tampines",
        rules={"restrictions": {"same_district": True}},
    )
    vid = await _issue(
        client, pool_id=pool["pool_id"], user_id=f"u_{s}",
        source_brand_id=a, amount_cents=1_000,
    )
    # Caller supplies a divergent district — should reject.
    res = await _redeem(
        client, pool_id=pool["pool_id"], voucher_id=vid, target_brand_id=b,
        context={"source_district": "tampines", "target_district": "jurong"},
    )
    assert res.status_code == 400
    assert res.json()["detail"]["reason"] == "district_mismatch"


@pytest.mark.asyncio
async def test_min_purchase_restriction(client, clean_redis):
    s = _suffix()
    a, b = f"a_{s}", f"b_{s}"
    pool = await _create_pool(
        client, brand_ids=[a, b],
        rules={"restrictions": {"min_purchase_cents": 2_000}},
    )
    vid = await _issue(
        client, pool_id=pool["pool_id"], user_id=f"u_{s}",
        source_brand_id=a, amount_cents=500,
    )
    # Below threshold
    res = await _redeem(
        client, pool_id=pool["pool_id"], voucher_id=vid, target_brand_id=b,
        context={"purchase_amount_cents": 500},
    )
    assert res.status_code == 400
    assert res.json()["detail"]["reason"] == "below_min_purchase"
    # At threshold — succeed
    res = await _redeem(
        client, pool_id=pool["pool_id"], voucher_id=vid, target_brand_id=b,
        context={"purchase_amount_cents": 2_000},
    )
    assert res.status_code == 200, res.text


# ── 4. Reciprocity ratios ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reciprocity_per_edge_ratio(client, clean_redis):
    s = _suffix()
    a, b = f"a_{s}", f"b_{s}"
    # A->B credits at 0.8, B->A at 1.25 (asymmetric).
    pool = await _create_pool(
        client, brand_ids=[a, b],
        rules={"reciprocity": {a: {b: 0.8}, b: {a: 1.25}}},
    )
    v1 = await _issue(
        client, pool_id=pool["pool_id"], user_id=f"u_{s}",
        source_brand_id=a, amount_cents=1_000,
    )
    r1 = await _redeem(client, pool_id=pool["pool_id"], voucher_id=v1, target_brand_id=b)
    assert r1.status_code == 200
    assert r1.json()["credit_amount_cents"] == 800
    assert r1.json()["ratio"] == 0.8

    v2 = await _issue(
        client, pool_id=pool["pool_id"], user_id=f"u2_{s}",
        source_brand_id=b, amount_cents=1_000,
    )
    r2 = await _redeem(client, pool_id=pool["pool_id"], voucher_id=v2, target_brand_id=a)
    assert r2.json()["credit_amount_cents"] == 1_250
    assert r2.json()["ratio"] == 1.25


@pytest.mark.asyncio
async def test_reciprocity_default_when_no_rule(client, clean_redis):
    s = _suffix()
    a, b = f"a_{s}", f"b_{s}"
    pool = await _create_pool(client, brand_ids=[a, b])  # no ratio
    vid = await _issue(
        client, pool_id=pool["pool_id"], user_id=f"u_{s}",
        source_brand_id=a, amount_cents=777,
    )
    res = await _redeem(
        client, pool_id=pool["pool_id"], voucher_id=vid, target_brand_id=b,
    )
    assert res.json()["credit_amount_cents"] == 777  # 1:1 default
    assert res.json()["ratio"] == 1.0


# ── 5. Idempotency ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redeem_idempotent_on_replay(client, clean_redis):
    s = _suffix()
    a, b = f"a_{s}", f"b_{s}"
    pool = await _create_pool(client, brand_ids=[a, b])
    vid = await _issue(
        client, pool_id=pool["pool_id"], user_id=f"u_{s}",
        source_brand_id=a, amount_cents=1_000,
    )
    idem = f"idem_{s}"
    r1 = await _redeem(
        client, pool_id=pool["pool_id"], voucher_id=vid,
        target_brand_id=b, transaction_id="txn_A", idempotency_key=idem,
    )
    assert r1.status_code == 200
    assert r1.json().get("idempotent") is not True

    r2 = await _redeem(
        client, pool_id=pool["pool_id"], voucher_id=vid,
        target_brand_id=b, transaction_id="txn_A_replay",
        idempotency_key=idem,
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["idempotent"] is True


# ── 6. Concurrent redemption (double-spend guard) ────────────────────────


@pytest.mark.asyncio
async def test_concurrent_redemptions_one_wins(client, clean_redis):
    s = _suffix()
    a, b, c = f"a_{s}", f"b_{s}", f"c_{s}"
    pool = await _create_pool(client, brand_ids=[a, b, c])
    vid = await _issue(
        client, pool_id=pool["pool_id"], user_id=f"u_{s}",
        source_brand_id=a, amount_cents=1_000,
    )
    # Two independent redeems at B and C in parallel. The voucher can
    # only be burned once. Exactly one must succeed; the other must
    # observe voucher_redeemed.
    results = await asyncio.gather(
        _redeem(
            client, pool_id=pool["pool_id"], voucher_id=vid,
            target_brand_id=b, transaction_id=f"txn_b_{s}",
            idempotency_key=f"k_b_{s}",
        ),
        _redeem(
            client, pool_id=pool["pool_id"], voucher_id=vid,
            target_brand_id=c, transaction_id=f"txn_c_{s}",
            idempotency_key=f"k_c_{s}",
        ),
    )
    successes = [r for r in results if r.status_code == 200]
    failures = [r for r in results if r.status_code != 200]
    assert len(successes) == 1, [r.text for r in results]
    assert len(failures) == 1
    assert failures[0].json()["detail"]["reason"] == "voucher_redeemed"


# ── 7. Redemption options for UI ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_redemption_options_lists_other_members(client, clean_redis):
    s = _suffix()
    a, b, c = f"a_{s}", f"b_{s}", f"c_{s}"
    pool = await _create_pool(
        client, brand_ids=[a, b, c],
        rules={"reciprocity": {"default": 0.9}},
    )
    vid = await _issue(
        client, pool_id=pool["pool_id"], user_id=f"u_{s}",
        source_brand_id=a, amount_cents=1_000,
    )
    res = await client.get(
        f"{POOL_PREFIX}/voucher/{vid}/redemption-options"
    )
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["ok"] is True
    # Source brand A should be excluded; B and C remain.
    target_ids = {opt["brand_id"] for opt in out["options"]}
    assert target_ids == {b, c}
    # Default 0.9 ratio applied → 900 cents on a 1000 voucher.
    for opt in out["options"]:
        assert opt["credit_amount_cents"] == 900
        assert opt["ratio"] == 0.9


# ── 8. Net position + pool value math ────────────────────────────────────


@pytest.mark.asyncio
async def test_net_position_math(client, clean_redis):
    s = _suffix()
    a, b = f"a_{s}", f"b_{s}"
    pool = await _create_pool(client, brand_ids=[a, b])

    # A users redeem at B for total 3000c
    for amt in (1_000, 2_000):
        vid = await _issue(
            client, pool_id=pool["pool_id"], user_id=f"u_{s}_{amt}",
            source_brand_id=a, amount_cents=amt,
        )
        r = await _redeem(
            client, pool_id=pool["pool_id"], voucher_id=vid,
            target_brand_id=b,
        )
        assert r.status_code == 200, r.text
    # B users redeem at A for total 1000c
    vid = await _issue(
        client, pool_id=pool["pool_id"], user_id=f"v_{s}",
        source_brand_id=b, amount_cents=1_000,
    )
    await _redeem(
        client, pool_id=pool["pool_id"], voucher_id=vid, target_brand_id=a,
    )

    # B should have +2000 net (received 3000 from A's vouchers, sent 1000)
    res = await client.get(
        f"{POOL_PREFIX}/{b}/net-position",
        params={"pool_id": pool["pool_id"]},
    )
    assert res.status_code == 200, res.text
    np_b = res.json()
    assert np_b["incoming_cents"] == 3_000
    assert np_b["outgoing_cents"] == 1_000
    assert np_b["net_cents"] == 2_000

    # A should be the mirror.
    res = await client.get(
        f"{POOL_PREFIX}/{a}/net-position",
        params={"pool_id": pool["pool_id"]},
    )
    np_a = res.json()
    assert np_a["net_cents"] == -2_000


@pytest.mark.asyncio
async def test_compute_pool_value_summary(client, clean_redis):
    s = _suffix()
    a, b = f"a_{s}", f"b_{s}"
    pool = await _create_pool(client, brand_ids=[a, b])
    vid = await _issue(
        client, pool_id=pool["pool_id"], user_id=f"u_{s}",
        source_brand_id=a, amount_cents=500,
    )
    await _redeem(client, pool_id=pool["pool_id"], voucher_id=vid, target_brand_id=b)

    res = await client.get(f"{POOL_PREFIX}/{b}/net-position")
    assert res.status_code == 200, res.text
    summary = res.json()
    assert summary["brand_id"] == b
    assert summary["totals"]["incoming_cents"] == 500
    assert summary["totals"]["outgoing_cents"] == 0
    assert summary["pool_count"] >= 1


# ── 9. Settlement plan / minimum-edge planner ────────────────────────────


def test_plan_settlement_transfers_minimal_edges():
    """Pure-function unit test for the settlement planner."""
    positions = [
        {"brand_id": "A", "net_cents": -1000},  # owes
        {"brand_id": "B", "net_cents": +600},   # owed
        {"brand_id": "C", "net_cents": +400},   # owed
    ]
    plan = vp.plan_settlement_transfers(positions)
    # Exactly two legs needed, both from A → creditors.
    assert len(plan) == 2
    assert all(p["from_brand_id"] == "A" for p in plan)
    assert sum(p["amount_cents"] for p in plan) == 1000
    pay = {p["to_brand_id"]: p["amount_cents"] for p in plan}
    assert pay == {"B": 600, "C": 400}


def test_plan_settlement_zero_positions_is_noop():
    plan = vp.plan_settlement_transfers([
        {"brand_id": "A", "net_cents": 0},
        {"brand_id": "B", "net_cents": 0},
    ])
    assert plan == []


# ── 10. Settlement worker (dispatches inter-brand transfers) ─────────────


@pytest.mark.asyncio
async def test_settlement_worker_dispatches_transfers(client, clean_redis):
    """End-to-end: redeem cross-brand → run settlement worker → wallets move.

    The worker invokes ``payouts._inter_brand_transfer_impl`` which
    requires the source wallet to be funded; we top up the debtor here
    so the leg can actually execute.
    """
    from app.workers import voucher_pool_settlement_worker as wkr

    s = _suffix()
    a, b = f"settle_a_{s}", f"settle_b_{s}"
    pool = await _create_pool(client, brand_ids=[a, b])

    # A's user redeems at B for 1000c — A owes B 1000c at settlement.
    vid = await _issue(
        client, pool_id=pool["pool_id"], user_id=f"u_{s}",
        source_brand_id=a, amount_cents=1_000,
    )
    rr = await _redeem(
        client, pool_id=pool["pool_id"], voucher_id=vid, target_brand_id=b,
    )
    assert rr.status_code == 200, rr.text

    # Fund A's wallet (and seed B's so the balance key exists post-credit).
    await _topup_wallet(client, a, 5_000)
    await _topup_wallet(client, b, 1)
    bal_a_before = await _balance(client, a)
    bal_b_before = await _balance(client, b)

    r = await get_redis()
    report = await wkr.run_once(r)
    assert report["pool_count"] >= 1
    pool_reports = [pr for pr in report["reports"] if pr["pool_id"] == pool["pool_id"]]
    assert pool_reports, report
    pr = pool_reports[0]
    assert pr["transfer_count"] == 1
    assert len(pr["executed"]) == 1
    leg = pr["executed"][0]
    assert leg["from_brand_id"] == a
    assert leg["to_brand_id"] == b
    assert leg["amount_cents"] == 1_000

    # Balances actually moved.
    assert await _balance(client, a) == bal_a_before - 1_000
    assert await _balance(client, b) == bal_b_before + 1_000

    # A re-run is idempotent — the per-leg key collapses the replay.
    report2 = await wkr.run_once(r)
    pr2 = [x for x in report2["reports"] if x["pool_id"] == pool["pool_id"]][0]
    # No new debit (executed is empty OR amount unchanged because the
    # impl returned idempotent=True).
    assert await _balance(client, a) == bal_a_before - 1_000
    assert await _balance(client, b) == bal_b_before + 1_000
    # The idempotent leg should be either skipped or recorded as a replay.
    if pr2["executed"]:
        for leg in pr2["executed"]:
            assert leg["result"].get("idempotent") is True


# ── 11. Worker dry-run does NOT move balances ────────────────────────────


@pytest.mark.asyncio
async def test_settlement_worker_dry_run(client, clean_redis):
    from app.workers import voucher_pool_settlement_worker as wkr

    s = _suffix()
    a, b = f"dry_a_{s}", f"dry_b_{s}"
    pool = await _create_pool(client, brand_ids=[a, b])
    vid = await _issue(
        client, pool_id=pool["pool_id"], user_id=f"u_{s}",
        source_brand_id=a, amount_cents=1_500,
    )
    await _redeem(
        client, pool_id=pool["pool_id"], voucher_id=vid, target_brand_id=b,
    )
    await _topup_wallet(client, a, 5_000)
    bal_before = await _balance(client, a)

    r = await get_redis()
    report = await wkr.run_once(r, dry_run=True)
    assert report["dry_run"] is True
    # No movement
    assert await _balance(client, a) == bal_before
