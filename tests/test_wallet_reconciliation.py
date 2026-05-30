"""Tests for wallet ledger reconciliation worker."""
import json
import time

import pytest

from app.workers.wallet_reconciliation_worker import (
    ALERT_KEY,
    DRIFT_ALERT_CENTS,
    DRIFT_CRITICAL_CENTS,
    DRIFT_WARN_CENTS,
    compute_ledger_expected,
    get_recent_alerts,
    list_active_brands,
    reconcile_brand,
    run_once,
)
from app.redis_client import get_redis


pytestmark = pytest.mark.asyncio


async def _setup_brand(brand_id, balance, topups=None, charges=None, refunds=None,
                       recharges=None):
    r = await get_redis()
    await r.set(f"wallet:{brand_id}:balance", balance)
    for i, amt in enumerate(topups or []):
        await r.hset(f"topup:t-{brand_id}-{i}", mapping={
            "brand_id": brand_id,
            "amount_cents": amt,
            "status": "confirmed",
        })
    for i, amt in enumerate(charges or []):
        await r.hset(f"charge:c-{brand_id}-{i}", mapping={
            "brand_id": brand_id,
            "amount": amt,
        })
    for i, amt in enumerate(refunds or []):
        await r.hset(f"refund:rf-{brand_id}-{i}", mapping={
            "brand_id": brand_id,
            "amount_cents": amt,
        })
    for amt in recharges or []:
        await r.lpush(
            f"wallet:{brand_id}:autorecharge_log",
            json.dumps({"amount_cents": amt, "status": "succeeded"}),
        )


async def test_compute_ledger_zero_for_empty():
    r = await get_redis()
    result = await compute_ledger_expected(r, "empty-brand")
    assert result["expected"] == 0
    assert result["topup_sum"] == 0
    assert result["doc_count"] == 0


async def test_compute_ledger_sums_topups_charges_refunds():
    await _setup_brand(
        "test-recon-1",
        balance=10000,
        topups=[5000, 5000],
        charges=[2000],
        refunds=[500],
    )
    r = await get_redis()
    result = await compute_ledger_expected(r, "test-recon-1")
    assert result["topup_sum"] == 10000
    assert result["charge_sum"] == 2000
    assert result["refund_sum"] == 500
    assert result["expected"] == 10000 - 2000 + 500  # 8500


async def test_compute_ledger_includes_autorecharges():
    await _setup_brand(
        "test-recon-recharge",
        balance=15000,
        topups=[5000],
        recharges=[3000, 2000],  # 5000 from auto-recharge
    )
    r = await get_redis()
    result = await compute_ledger_expected(r, "test-recon-recharge")
    assert result["recharge_sum"] == 5000
    assert result["expected"] == 10000  # 5000 topup + 5000 recharge


async def test_reconcile_brand_no_drift_returns_ok():
    await _setup_brand(
        "test-clean",
        balance=3000,
        topups=[5000],
        charges=[2000],
    )
    r = await get_redis()
    report = await reconcile_brand(r, "test-clean")
    assert report["drift_cents"] == 0
    assert report["severity"] == "ok"


async def test_reconcile_brand_small_drift_warns():
    await _setup_brand(
        "test-warn",
        balance=3050,  # 50c drift above expected 3000
        topups=[5000],
        charges=[2000],
    )
    r = await get_redis()
    report = await reconcile_brand(r, "test-warn")
    assert report["drift_cents"] == 50
    assert report["severity"] == "ok"  # below DRIFT_WARN_CENTS (1000)


async def test_reconcile_brand_warn_severity():
    await _setup_brand(
        "test-warn-sev",
        balance=5000,
        topups=[3000],  # expect 3000, actual 5000 = 2000c drift
    )
    r = await get_redis()
    report = await reconcile_brand(r, "test-warn-sev")
    assert abs(report["drift_cents"]) == 2000
    assert report["severity"] == "warn"


async def test_reconcile_brand_critical_severity():
    await _setup_brand(
        "test-critical",
        balance=20_000_00,  # actual $20K
        topups=[5_000_00],   # expected $5K
    )
    r = await get_redis()
    report = await reconcile_brand(r, "test-critical")
    assert abs(report["drift_cents"]) == 15_000_00
    assert report["severity"] == "critical"


async def test_alerts_recorded_for_drift():
    r = await get_redis()
    await r.delete(ALERT_KEY)
    await _setup_brand(
        "test-alert",
        balance=100_000_00,
        topups=[5_000_00],
    )
    await reconcile_brand(r, "test-alert")
    alerts = await get_recent_alerts(r, 10)
    assert len(alerts) >= 1
    assert alerts[0]["brand_id"] == "test-alert"
    assert alerts[0]["severity"] == "critical"


async def test_list_active_brands_discovers_via_scan():
    r = await get_redis()
    for bid in ["scan-brand-1", "scan-brand-2", "scan-brand-3"]:
        await r.set(f"wallet:{bid}:balance", 100)
    brands = await list_active_brands(r)
    assert "scan-brand-1" in brands
    assert "scan-brand-2" in brands
    assert "scan-brand-3" in brands


async def test_run_once_aggregates_drift_by_severity():
    r = await get_redis()
    await r.delete(ALERT_KEY)
    # Clean brand
    await _setup_brand("ro-clean", balance=1000, topups=[1000])
    # Warning brand
    await _setup_brand("ro-warn", balance=3000, topups=[1000])
    # Critical brand
    await _setup_brand("ro-critical", balance=20_000_00, topups=[1000])

    summary = await run_once(r)
    assert summary["brands_scanned"] >= 3
    assert summary["by_severity"]["critical"] >= 1
    assert summary["by_severity"]["warn"] >= 1
    assert summary["total_abs_drift_cents"] > 0


async def test_pending_topup_ignored_until_confirmed():
    r = await get_redis()
    bid = "test-pending"
    await r.set(f"wallet:{bid}:balance", 0)
    await r.hset(f"topup:t-pending-{bid}", mapping={
        "brand_id": bid,
        "amount_cents": 5000,
        "status": "pending",  # not yet confirmed
    })
    result = await compute_ledger_expected(r, bid)
    assert result["topup_sum"] == 0  # pending excluded
    assert result["expected"] == 0


async def test_failed_recharge_ignored():
    r = await get_redis()
    bid = "test-failed-rch"
    await r.set(f"wallet:{bid}:balance", 0)
    await r.lpush(
        f"wallet:{bid}:autorecharge_log",
        json.dumps({"amount_cents": 3000, "status": "failed"}),
    )
    result = await compute_ledger_expected(r, bid)
    assert result["recharge_sum"] == 0
