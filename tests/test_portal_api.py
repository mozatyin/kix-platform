"""Portal-API meta-endpoint tests.

Covers shape, pagination, sort+filter, search, locale-aware rendering,
since-cursor notifications, date-range comparison maths, and CSV export.
20 tests total.

All tests run against a clean Redis (``clean_redis`` fixture) and seed
data via the public APIs rather than poking raw keys, so test failures
flag a *contract* break rather than a key-name drift.
"""

from __future__ import annotations

import csv
import io
import time

import pytest


BRAND = "b_portal_test"


# ── Fixtures: seed minimum-viable brand state ────────────────────────────


async def _seed_brand(client, brand_id: str = BRAND, redis=None) -> None:
    """Top up wallet (SGD), and pin tier=enterprise so we don't hit quota.

    The portal tests aren't validating subscription enforcement — they
    just need a brand with enough headroom to create ~10 campaigns and
    audiences for the list/filter assertions. Setting the tier hash
    directly is exactly what the brand_subscriptions admin endpoint
    would do under the hood.
    """
    r = await client.post(
        f"/api/v1/wallet/{brand_id}/topup",
        json={"amount_cents": 1_000_000, "payment_method": "wechat", "currency": "SGD"},
    )
    assert r.status_code == 200, r.text
    topup_id = r.json()["topup_id"]
    r = await client.post(
        f"/api/v1/wallet/{brand_id}/topup/{topup_id}/confirm",
        json={"payment_gateway_response": {"mock": True}},
    )
    assert r.status_code == 200, r.text
    # Pin tier=enterprise so quota gates (-1 limits) never trip in tests.
    if redis is not None:
        await redis.hset(
            f"brand:{brand_id}:subscription",
            mapping={"tier": "enterprise", "started_at": "0"},
        )


async def _create_campaign(
    client, brand_id: str, name: str, max_bid_cents: int = 200, objective: str = "acquire"
) -> str:
    body = {
        "brand_id": brand_id,
        "name": name,
        "objective": objective,
        "bid_strategy": "cpa",
        "max_bid_cents": max_bid_cents,
        "daily_budget_cents": 100_000,
        "total_budget_cents": 1_000_000,
        "target_audience": "new_users_only",
    }
    r = await client.post("/api/v1/campaigns/create", json=body)
    assert r.status_code == 200, r.text
    return r.json()["campaign_id"]


async def _create_audience(
    client, brand_id: str, name: str, members: list[str] | None = None
) -> str:
    r = await client.post(
        "/api/v1/audiences/custom/create",
        json={
            "brand_id": brand_id,
            "name": name,
            "source": "csv_upload",
            "user_ids": members or [f"u_{brand_id}_{i}" for i in range(3)],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["audience_id"]


# ── 1. Dashboard ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dashboard_shape(client, clean_redis):
    await _seed_brand(client, redis=clean_redis)
    r = await client.get(f"/api/v1/portal/dashboard/{BRAND}")
    assert r.status_code == 200, r.text
    body = r.json()
    for k in (
        "brand_id", "date", "locale", "currency",
        "kpis", "spend_timeseries", "top_campaigns",
        "alerts", "wallet", "today_metrics", "generated_at",
    ):
        assert k in body, f"missing key {k}"
    assert body["brand_id"] == BRAND
    # Money shape
    assert "value_cents" in body["wallet"]["balance"]
    assert "currency" in body["wallet"]["balance"]
    assert "formatted_display" in body["wallet"]["balance"]


@pytest.mark.asyncio
async def test_dashboard_kpi_deltas(client, clean_redis):
    await _seed_brand(client, redis=clean_redis)
    r = await client.get(f"/api/v1/portal/dashboard/{BRAND}")
    body = r.json()
    kpis = body["kpis"]
    # All KPI cards present
    for k in ("spend", "conversions", "cpa", "roas", "new_users"):
        assert k in kpis, f"missing KPI {k}"
    # Delta fields present (zero-state OK).
    assert "delta_vs_yesterday_cents" in kpis["spend"]
    assert "delta" in kpis["conversions"]


@pytest.mark.asyncio
async def test_dashboard_spend_timeseries_length(client, clean_redis):
    await _seed_brand(client, redis=clean_redis)
    r = await client.get(f"/api/v1/portal/dashboard/{BRAND}")
    series = r.json()["spend_timeseries"]
    # 14-day sparkline.
    assert len(series) == 14
    assert "date" in series[0]
    assert "spend" in series[0]
    assert "value_cents" in series[0]["spend"]


# ── 2. Campaigns list ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_campaigns_list_pagination(client, clean_redis):
    await _seed_brand(client, redis=clean_redis)
    for i in range(5):
        await _create_campaign(client, BRAND, f"camp_{i}")
    r = await client.get(
        f"/api/v1/portal/campaigns/{BRAND}?page=1&size=2"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert body["total"] == 5
    assert body["has_more"] is True
    r2 = await client.get(
        f"/api/v1/portal/campaigns/{BRAND}?page=3&size=2"
    )
    body2 = r2.json()
    assert body2["count"] == 1
    assert body2["has_more"] is False


@pytest.mark.asyncio
async def test_campaigns_list_sort_by_name(client, clean_redis):
    await _seed_brand(client, redis=clean_redis)
    for n in ("zebra", "apple", "mango"):
        await _create_campaign(client, BRAND, n)
    r = await client.get(
        f"/api/v1/portal/campaigns/{BRAND}?sort=name.asc"
    )
    items = r.json()["items"]
    names = [it["name"] for it in items]
    assert names == sorted(names)


@pytest.mark.asyncio
async def test_campaigns_list_filter_by_status(client, clean_redis):
    await _seed_brand(client, redis=clean_redis)
    cid = await _create_campaign(client, BRAND, "active_one")
    # Pause one to create status diversity.
    pr = await client.post(f"/api/v1/campaigns/{cid}/pause")
    assert pr.status_code == 200, pr.text
    await _create_campaign(client, BRAND, "active_two")

    r = await client.get(
        f"/api/v1/portal/campaigns/{BRAND}?status=paused"
    )
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["status"]["value"] == "paused"


@pytest.mark.asyncio
async def test_campaigns_list_search(client, clean_redis):
    await _seed_brand(client, redis=clean_redis)
    await _create_campaign(client, BRAND, "Black Friday Sale")
    await _create_campaign(client, BRAND, "New Years Promo")
    r = await client.get(
        f"/api/v1/portal/campaigns/{BRAND}?search=friday"
    )
    items = r.json()["items"]
    assert len(items) == 1
    assert "Friday" in items[0]["name"]


@pytest.mark.asyncio
async def test_campaigns_list_facets(client, clean_redis):
    await _seed_brand(client, redis=clean_redis)
    await _create_campaign(client, BRAND, "c1", objective="acquire")
    await _create_campaign(client, BRAND, "c2", objective="acquire")
    await _create_campaign(client, BRAND, "c3", objective="awareness")
    r = await client.get(f"/api/v1/portal/campaigns/{BRAND}")
    facets = r.json()["facets"]
    assert facets["status"].get("active", 0) == 3
    assert facets["objective"]["acquire"] == 2
    assert facets["objective"]["awareness"] == 1


@pytest.mark.asyncio
async def test_campaigns_list_invalid_sort_field(client, clean_redis):
    await _seed_brand(client, redis=clean_redis)
    await _create_campaign(client, BRAND, "x")
    r = await client.get(
        f"/api/v1/portal/campaigns/{BRAND}?sort=secret_field.desc"
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_sort_field"


@pytest.mark.asyncio
async def test_campaigns_list_locale_aware_money(client, clean_redis):
    await _seed_brand(client, redis=clean_redis)
    await _create_campaign(client, BRAND, "money_test")
    # English-Singapore: SGD with S$ prefix.
    r = await client.get(
        f"/api/v1/portal/campaigns/{BRAND}",
        headers={"Accept-Language": "en-SG"},
    )
    items = r.json()["items"]
    assert items
    money = items[0]["daily_budget"]
    assert money["currency"] == "SGD"
    assert "S$" in money["formatted_display"]
    # Chinese-Hans-SG locale must still parse and format something.
    r2 = await client.get(
        f"/api/v1/portal/campaigns/{BRAND}",
        headers={"Accept-Language": "zh-Hans-SG"},
    )
    items2 = r2.json()["items"]
    assert items2[0]["daily_budget"]["formatted_display"]


# ── 3. Audiences list ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audiences_list_shape(client, clean_redis):
    await _seed_brand(client, redis=clean_redis)
    aid = await _create_audience(client, BRAND, "VIPs")
    r = await client.get(f"/api/v1/portal/audiences/{BRAND}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] >= 1
    item = body["items"][0]
    assert item["audience_id"] == aid
    assert item["status"]["display_label_i18n_key"] == "status.ready"
    assert item["size"] == 3


@pytest.mark.asyncio
async def test_audiences_list_search(client, clean_redis):
    await _seed_brand(client, redis=clean_redis)
    await _create_audience(client, BRAND, "VIP customers")
    await _create_audience(client, BRAND, "lapsed users")
    r = await client.get(
        f"/api/v1/portal/audiences/{BRAND}?search=lapsed"
    )
    items = r.json()["items"]
    assert len(items) == 1
    assert "lapsed" in items[0]["name"].lower()


# ── 4. Global search ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_ranks_campaigns_and_audiences(client, clean_redis):
    await _seed_brand(client, redis=clean_redis)
    await _create_campaign(client, BRAND, "Summer Sale")
    await _create_audience(client, BRAND, "Summer Fans")
    r = await client.get(
        f"/api/v1/portal/search?q=summer&brand_id={BRAND}"
    )
    assert r.status_code == 200
    body = r.json()
    kinds = {it["kind"] for it in body["items"]}
    # At minimum a campaign and an audience match — help docs may also hit.
    assert "campaign" in kinds
    assert "audience" in kinds


@pytest.mark.asyncio
async def test_search_empty_query_rejected(client, clean_redis):
    # FastAPI Query(min_length=1) rejects empty strings.
    r = await client.get(
        f"/api/v1/portal/search?q=&brand_id={BRAND}"
    )
    assert r.status_code == 422


# ── 5. Notifications feed ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notifications_feed_empty(client, clean_redis):
    r = await client.get(f"/api/v1/portal/notifications/{BRAND}")
    assert r.status_code == 200
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_notifications_since_cursor(client, clean_redis):
    # Drop in two notifications via the test fixture endpoint.
    r = await client.post(
        f"/api/v1/portal/notifications/{BRAND}/test?kind=k1&title=first"
    )
    assert r.status_code == 200
    # Read all once, capture cursor.
    r_all = await client.get(f"/api/v1/portal/notifications/{BRAND}")
    assert r_all.json()["count"] == 1
    cursor = r_all.json()["next_cursor"]

    # Sleep briefly so the next notification has a strictly newer ts.
    time.sleep(0.01)
    r2 = await client.post(
        f"/api/v1/portal/notifications/{BRAND}/test?kind=k2&title=second"
    )
    assert r2.status_code == 200

    # since=<previous cursor> must skip the first and surface only the
    # newer entry.
    r_since = await client.get(
        f"/api/v1/portal/notifications/{BRAND}?since={cursor}"
    )
    items = r_since.json()["items"]
    assert len(items) == 1
    assert items[0]["title"] == "second"


# ── 6. Date-range comparison ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_date_range_compare_shape(client, clean_redis):
    await _seed_brand(client, redis=clean_redis)
    r = await client.get(
        f"/api/v1/portal/date-range-compare"
        f"?metric=spend&range=last7d&brand_id={BRAND}"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["metric"] == "spend"
    assert body["range"] == "last7d"
    # Money metric → tri-shape.
    assert "value_cents" in body["current"]
    assert "formatted_display" in body["current"]
    assert body["delta"] == 0
    assert body["delta_pct"] is None  # zero-prior baseline


@pytest.mark.asyncio
async def test_date_range_compare_invalid_metric(client, clean_redis):
    r = await client.get(
        f"/api/v1/portal/date-range-compare"
        f"?metric=mystery&range=last7d&brand_id={BRAND}"
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_metric"


@pytest.mark.asyncio
async def test_date_range_compare_non_money_metric(client, clean_redis):
    await _seed_brand(client, redis=clean_redis)
    r = await client.get(
        f"/api/v1/portal/date-range-compare"
        f"?metric=new_users&range=last7d&brand_id={BRAND}"
    )
    assert r.status_code == 200
    body = r.json()
    # Non-money metric → simple `value`.
    assert "value" in body["current"]
    assert "formatted_display" not in body["current"]


# ── 7. CSV export ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_csv_campaigns(client, clean_redis):
    await _seed_brand(client, redis=clean_redis)
    await _create_campaign(client, BRAND, "csv_export_one")
    r = await client.get(
        f"/api/v1/portal/export.csv?type=campaigns&brand_id={BRAND}"
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    reader = csv.reader(io.StringIO(r.text))
    rows = list(reader)
    header = rows[0]
    assert "campaign_id" in header
    assert "spend_cents" in header
    # At least one data row.
    assert len(rows) >= 2


@pytest.mark.asyncio
async def test_export_csv_invalid_type(client, clean_redis):
    r = await client.get(
        f"/api/v1/portal/export.csv?type=unknown&brand_id={BRAND}"
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_export_type"
