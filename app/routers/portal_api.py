"""Enterprise Portal convenience meta-endpoints.

This router is a **read-only composition layer** on top of the existing
campaigns / audiences / dashboards / reporting / wallet routers. It
exists so the new enterprise portal UI can render its core views with
the minimum number of round-trips, with locale-aware formatting baked
in, without us having to mutate every existing production router.

Endpoints
---------
- ``GET /api/v1/portal/dashboard/{brand_id}`` — single payload feeding
  KPI cards + spend sparkline + top campaigns + active alerts + wallet.
- ``GET /api/v1/portal/campaigns/{brand_id}`` — paginated, sortable,
  filterable campaign list with facets.
- ``GET /api/v1/portal/audiences/{brand_id}`` — locale-formatted audience
  list (wraps the audiences router).
- ``GET /api/v1/portal/search`` — global search across campaigns /
  audiences / report templates / help docs.
- ``GET /api/v1/portal/notifications/{brand_id}`` — feed with
  ``since=<unix_ts>`` cursor, backed by a per-brand Redis LIST.
- ``GET /api/v1/portal/date-range-compare`` — current vs prior period
  delta for a single metric.
- ``GET /api/v1/portal/export.csv`` — CSV streaming export.

Design notes
------------
- We do **not** call other routers as HTTP — we import their helpers
  (``_load_and_refresh``, ``_read_stats``, ``_compute_today_metrics``)
  so a single asyncio loop and Redis pool are reused.
- Every monetary value is returned as
  ``{value_cents, currency, formatted_display}`` so the UI never has
  to know about CLDR.
- Every status string is shipped with a paired ``i18n_key`` so the UI
  can swap label text without server changes.
- Notifications are persisted at
  ``portal:notifications:{brand_id}`` (LIST of JSON entries, capped at
  500 — see :func:`record_notification`).
- All endpoints are tagged ``portal`` so OpenAPI groups them together.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api_standards import error_response, list_response, not_found
from app.i18n import t as i18n_t
from app.i18n.context import get_current_locale
from app.i18n.formatting import format_currency, format_date, format_datetime

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ────────────────────────────────────────────────────────────

NOTIFICATION_LIST_KEY = "portal:notifications:{brand_id}"
NOTIFICATION_MAX_LEN = 500
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

# Sortable campaign columns. We expose only computed values so callers
# can't sort by a Redis HSET internal we'd later rename.
_SORTABLE_CAMPAIGN_FIELDS: frozenset[str] = frozenset(
    {
        "created_at",
        "updated_at",
        "spend",
        "spend_cents",
        "impressions",
        "clicks",
        "conversions",
        "ctr",
        "cvr",
        "roas",
        "cpa_actual",
        "name",
        "status",
    }
)

# Status → i18n key mapping. Generic enough that the UI can dispatch
# off the key without knowing the natural-language label.
_CAMPAIGN_STATUS_I18N: dict[str, str] = {
    "active": "status.active",
    "paused": "status.paused",
    "draft": "status.draft",
    "pending_review": "status.pending_review",
    "rejected": "status.rejected",
    "ended": "status.ended",
    "deleted": "status.deleted",
}

_AUDIENCE_STATUS_I18N: dict[str, str] = {
    "ready": "status.ready",
    "building": "status.building",
    "failed": "status.failed",
}


# ── Time helpers ─────────────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _yesterday_str() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def _day_offset(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d"
    )


# ── Locale-formatting helpers ─────────────────────────────────────────────

def _money(amount_cents: int, currency: str) -> dict[str, Any]:
    """Tri-shape money: cents (storage) + currency + locale-formatted display."""
    locale = get_current_locale()
    try:
        formatted = format_currency(int(amount_cents or 0), currency, locale)
    except Exception as exc:  # pragma: no cover — Babel never raises in practice
        logger.warning("money_format_failed cur=%s err=%s", currency, exc)
        formatted = f"{currency} {amount_cents / 100:.2f}"
    return {
        "value_cents": int(amount_cents or 0),
        "currency": currency,
        "formatted_display": formatted,
    }


def _ts(epoch: float | int | None) -> dict[str, Any] | None:
    """Dual-shape timestamp: ISO 8601 + locale-formatted display.

    Returns ``None`` when ``epoch`` is missing / 0, so the UI can hide
    the field entirely instead of rendering ``"1970-01-01"``.
    """
    if not epoch:
        return None
    try:
        dt = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None
    locale = get_current_locale()
    return {
        "epoch_seconds": int(float(epoch)),
        "iso8601": dt.isoformat(),
        "formatted_display": format_datetime(dt, locale=locale),
    }


def _date_label(day: str) -> dict[str, Any]:
    """``YYYY-MM-DD`` → ISO + locale-formatted display."""
    try:
        dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return {"iso8601": day, "formatted_display": day}
    return {
        "iso8601": day,
        "formatted_display": format_date(dt, locale=get_current_locale()),
    }


def _status_envelope(state: str, i18n_table: dict[str, str]) -> dict[str, str]:
    """Pair a status string with its i18n key + translated label."""
    key = i18n_table.get(state, f"status.{state}")
    return {
        "value": state,
        "display_label_i18n_key": key,
        "display_label": i18n_t(key, locale=get_current_locale()),
    }


# ── Wallet helpers (no router import; re-resolve keys directly) ──────────

DEFAULT_CURRENCY_FALLBACK = "CNY"


async def _read_wallet_balance(
    r: aioredis.Redis, brand_id: str
) -> dict[str, Any]:
    """Pull balance + currency without importing wallet router internals.

    Mirrors wallet.py's `_k_balance` / `_k_currency` shapes so a future
    refactor of either side is caught by the portal contract tests.
    """
    balance = 0
    try:
        balance = int(await r.get(f"wallet:{brand_id}:balance") or 0)
    except (TypeError, ValueError):
        balance = 0
    cur = await r.get(f"wallet:{brand_id}:currency")
    currency = (cur or DEFAULT_CURRENCY_FALLBACK).upper()
    return {"balance": _money(balance, currency), "currency": currency}


# ── Campaigns: paginated + filterable + sortable list ────────────────────


def _serialise_campaign_row(
    raw: dict[str, str], stats: dict[str, Any], currency: str
) -> dict[str, Any]:
    """Locale-aware row for the Campaigns table.

    Money fields use the brand wallet's currency. ``stats`` carries the
    derived CTR / CVR / ROAS / CPA which we surface unchanged.
    """
    return {
        "campaign_id": raw.get("campaign_id"),
        "brand_id": raw.get("brand_id"),
        "name": raw.get("name"),
        "objective": raw.get("objective"),
        "status": _status_envelope(
            raw.get("status", "active"), _CAMPAIGN_STATUS_I18N
        ),
        "daily_budget": _money(
            int(raw.get("daily_budget_cents", 0) or 0), currency
        ),
        "total_budget": _money(
            int(raw.get("total_budget_cents", 0) or 0), currency
        ),
        "max_bid": _money(int(raw.get("max_bid_cents", 0) or 0), currency),
        "spend": _money(int(stats.get("spend_cents", 0) or 0), currency),
        "revenue": _money(int(stats.get("revenue_cents", 0) or 0), currency),
        "metrics": {
            "impressions": int(stats.get("impressions", 0) or 0),
            "clicks": int(stats.get("clicks", 0) or 0),
            "conversions": int(stats.get("conversions", 0) or 0),
            "ctr": float(stats.get("ctr", 0.0) or 0.0),
            "cvr": float(stats.get("cvr", 0.0) or 0.0),
            "roas": float(stats.get("roas", 0.0) or 0.0),
            "cpa_actual": float(stats.get("cpa_actual", 0.0) or 0.0),
        },
        "created_at": _ts(float(raw.get("created_at", 0) or 0)),
        "updated_at": _ts(float(raw.get("updated_at", 0) or 0)),
        "quality_score": float(raw.get("quality_score", 0.5) or 0.5),
    }


def _row_sort_key(row: dict[str, Any], field: str) -> Any:
    """Resolve dotted-field sort key for a serialised campaign row."""
    if field in {"created_at", "updated_at"}:
        ts = row.get(field) or {}
        return ts.get("epoch_seconds", 0) if isinstance(ts, dict) else 0
    if field in {"spend", "spend_cents"}:
        sp = row.get("spend") or {}
        return sp.get("value_cents", 0) if isinstance(sp, dict) else 0
    if field in {"impressions", "clicks", "conversions", "ctr", "cvr",
                 "roas", "cpa_actual"}:
        return (row.get("metrics") or {}).get(field, 0)
    if field == "name":
        return (row.get("name") or "").lower()
    if field == "status":
        st = row.get("status") or {}
        return st.get("value", "") if isinstance(st, dict) else ""
    return row.get(field, 0)


@router.get(
    "/dashboard/{brand_id}",
    tags=["portal"],
    summary="Single-shot portal dashboard payload",
    responses={
        200: {
            "description": "Composite KPI + spend timeseries + top campaigns",
            "content": {
                "application/json": {
                    "example": {
                        "brand_id": "b_demo",
                        "date": "2026-05-30",
                        "locale": "en-SG",
                        "kpis": {
                            "spend": {
                                "today": {
                                    "value_cents": 12345,
                                    "currency": "SGD",
                                    "formatted_display": "S$123.45",
                                },
                                "delta_vs_yesterday_cents": 200,
                            },
                            "conversions": {"today": 12, "delta": 3},
                            "cpa": {"today": 1029, "delta": -50},
                            "roas": {"today": 2.4, "delta": 0.1},
                        },
                        "spend_timeseries": [],
                        "top_campaigns": [],
                        "alerts": [],
                        "wallet": {
                            "balance": {
                                "value_cents": 500000,
                                "currency": "SGD",
                                "formatted_display": "S$5,000.00",
                            }
                        },
                    }
                }
            },
        }
    },
)
async def portal_dashboard(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Composite payload used by the portal landing screen.

    Bundles KPI cards (today + delta vs yesterday), a 14-day spend
    sparkline, the top 5 campaigns by spend, active alerts, and wallet
    balance — locale-formatted.
    """
    # Defer to existing dashboards router internals (single shared loop).
    from app.routers.dashboards import (
        _compute_today_metrics,
        _conversion_value_cents,
    )
    from app.routers.campaigns import (
        BRAND_CAMPAIGNS_KEY,
        _load_and_refresh,
        _read_stats,
    )

    locale = get_current_locale()
    today = _today_str()
    yesterday = _yesterday_str()

    today_metrics = await _compute_today_metrics(r, brand_id, today)
    ytd_metrics = await _compute_today_metrics(r, brand_id, yesterday)

    wallet = await _read_wallet_balance(r, brand_id)
    currency = wallet["currency"]

    # ── KPI cards ─────────────────────────────────────────────────────
    # Spend / conversions / cpa / roas. CPA / ROAS derived from brand
    # cumulative stats hash maintained by auction.py.
    brand_stats = await r.hgetall(f"brand:{brand_id}:stats") or {}
    spend_total = int(brand_stats.get("spend_cents", 0) or 0)
    convs_total = int(brand_stats.get("conversions", 0) or 0)
    revenue_total = int(brand_stats.get("revenue_cents", 0) or 0)
    cpa_today_cents = (
        spend_total // convs_total if convs_total > 0 else 0
    )
    roas_today = (revenue_total / spend_total) if spend_total > 0 else 0.0

    # Today's spend specifically (sum of campaign daily_spent_cents today).
    today_spend_cents = 0
    cids = await r.smembers(BRAND_CAMPAIGNS_KEY.format(bid=brand_id))
    for cid in cids:
        try:
            today_spend_cents += int(
                await r.get(f"campaign:{cid}:spend_daily:{today}") or 0
            )
        except (TypeError, ValueError):
            continue

    yesterday_spend_cents = 0
    for cid in cids:
        try:
            yesterday_spend_cents += int(
                await r.get(f"campaign:{cid}:spend_daily:{yesterday}") or 0
            )
        except (TypeError, ValueError):
            continue

    kpis = {
        "spend": {
            "today": _money(today_spend_cents, currency),
            "yesterday": _money(yesterday_spend_cents, currency),
            "delta_vs_yesterday_cents": today_spend_cents - yesterday_spend_cents,
        },
        "conversions": {
            "today": today_metrics["vouchers_redeemed"],
            "yesterday": ytd_metrics["vouchers_redeemed"],
            "delta": today_metrics["vouchers_redeemed"]
            - ytd_metrics["vouchers_redeemed"],
        },
        "cpa": {
            "today": _money(cpa_today_cents, currency),
            "delta_cents": 0,  # placeholder until we materialise a daily CPA
        },
        "roas": {
            "today": round(roas_today, 4),
            "delta": 0.0,
        },
        "new_users": {
            "today": today_metrics["new_users"],
            "yesterday": ytd_metrics["new_users"],
            "delta": today_metrics["new_users"] - ytd_metrics["new_users"],
        },
    }

    # ── Spend timeseries (14d) ────────────────────────────────────────
    spend_timeseries: list[dict[str, Any]] = []
    for i in range(13, -1, -1):
        day = _day_offset(i)
        day_total = 0
        for cid in cids:
            try:
                day_total += int(
                    await r.get(f"campaign:{cid}:spend_daily:{day}") or 0
                )
            except (TypeError, ValueError):
                continue
        spend_timeseries.append({
            "date": _date_label(day),
            "spend": _money(day_total, currency),
        })

    # ── Top campaigns (top 5 by today's spend) ─────────────────────────
    top_rows: list[dict[str, Any]] = []
    for cid in cids:
        try:
            raw = await _load_and_refresh(r, cid)
        except HTTPException:
            continue
        stats = await _read_stats(r, cid)
        top_rows.append(_serialise_campaign_row(raw, stats, currency))
    top_rows.sort(key=lambda x: x["spend"]["value_cents"], reverse=True)
    top_campaigns = top_rows[:5]

    # ── Alerts (wallet_low, autorecharge_failed, budget_exhausted) ────
    alerts: list[dict[str, Any]] = []
    wallet_low = await r.get(f"notification:brand:{brand_id}:wallet_low")
    if wallet_low == "1":
        alerts.append({
            "kind": "wallet_low",
            "severity": "warning",
            "i18n_key": "alert.wallet_low",
            "message": i18n_t("alert.wallet_low", locale=locale),
        })
    autoreload_fail = await r.get(
        f"notification:brand:{brand_id}:autorecharge_failed"
    )
    if autoreload_fail:
        alerts.append({
            "kind": "autorecharge_failed",
            "severity": "error",
            "i18n_key": "alert.autorecharge_failed",
            "message": i18n_t(
                "alert.autorecharge_failed", locale=locale
            ),
            "at": _ts(float(autoreload_fail)) if autoreload_fail else None,
        })

    # Per-campaign budget_exhausted flags (set by wallet.charge path).
    for cid in list(cids)[:50]:  # cap scan to keep this <200ms
        exhausted = await r.get(
            f"notification:brand:{brand_id}:budget_exhausted:{cid}"
        )
        if exhausted == "1":
            alerts.append({
                "kind": "campaign_budget_exhausted",
                "severity": "warning",
                "campaign_id": cid,
                "i18n_key": "alert.budget_exhausted",
                "message": i18n_t(
                    "alert.budget_exhausted", locale=locale
                ),
            })

    return {
        "brand_id": brand_id,
        "date": _date_label(today),
        "locale": locale,
        "currency": currency,
        "kpis": kpis,
        "spend_timeseries": spend_timeseries,
        "top_campaigns": top_campaigns,
        "alerts": alerts,
        "wallet": wallet,
        "today_metrics": today_metrics,
        "generated_at": _ts(_now()),
    }


@router.get(
    "/campaigns/{brand_id}",
    tags=["portal"],
    summary="Paginated / filterable / sortable campaign list",
)
async def portal_campaigns(
    brand_id: str,
    status_filter: str | None = Query(None, alias="status"),
    objective: str | None = Query(None),
    sort: str = Query(
        "created_at.desc",
        description="Sort spec ``<field>.<asc|desc>`` (e.g. ``spend.desc``).",
    ),
    page: int = Query(1, ge=1),
    size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    search: str | None = Query(None),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Portal-side wrapper around :func:`list_brand_campaigns`.

    Adds query-string filtering, sorting, pagination, search-by-name, and
    facet counts derived from the unfiltered set (so the UI can show
    `Active (12) · Paused (3)` chips).
    """
    from app.routers.campaigns import (
        BRAND_CAMPAIGNS_KEY,
        _load_and_refresh,
        _read_stats,
    )

    wallet = await _read_wallet_balance(r, brand_id)
    currency = wallet["currency"]

    cids = await r.smembers(BRAND_CAMPAIGNS_KEY.format(bid=brand_id))
    all_rows: list[dict[str, Any]] = []
    for cid in cids:
        try:
            raw = await _load_and_refresh(r, cid)
        except HTTPException:
            continue
        stats = await _read_stats(r, cid)
        all_rows.append(_serialise_campaign_row(raw, stats, currency))

    # ── Facets (computed on the unfiltered set) ───────────────────────
    status_facets: dict[str, int] = {}
    objective_facets: dict[str, int] = {}
    for row in all_rows:
        st = (row["status"] or {}).get("value", "unknown")
        status_facets[st] = status_facets.get(st, 0) + 1
        obj = row.get("objective") or "unknown"
        objective_facets[obj] = objective_facets.get(obj, 0) + 1

    # ── Filter ────────────────────────────────────────────────────────
    filtered = all_rows
    if status_filter:
        filtered = [
            row for row in filtered
            if (row["status"] or {}).get("value") == status_filter
        ]
    if objective:
        filtered = [
            row for row in filtered if row.get("objective") == objective
        ]
    if search:
        needle = search.lower()
        filtered = [
            row for row in filtered
            if needle in (row.get("name") or "").lower()
        ]

    # ── Sort ──────────────────────────────────────────────────────────
    sort_parts = (sort or "created_at.desc").split(".")
    field = sort_parts[0]
    direction = sort_parts[1] if len(sort_parts) > 1 else "desc"
    if field not in _SORTABLE_CAMPAIGN_FIELDS:
        raise error_response(
            422,
            "invalid_sort_field",
            f"sort field must be one of {sorted(_SORTABLE_CAMPAIGN_FIELDS)}",
            requested_field=field,
        )
    filtered.sort(
        key=lambda row: _row_sort_key(row, field),
        reverse=(direction.lower() == "desc"),
    )

    # ── Paginate ──────────────────────────────────────────────────────
    offset = (page - 1) * size
    page_rows = filtered[offset:offset + size]
    envelope = list_response(
        items=page_rows, total=len(filtered), limit=size, offset=offset
    )

    return {
        **envelope,
        "brand_id": brand_id,
        "page": page,
        "size": size,
        "sort": sort,
        "filters": {
            "status": status_filter,
            "objective": objective,
            "search": search,
        },
        "facets": {
            "status": status_facets,
            "objective": objective_facets,
        },
        "currency": currency,
    }


# ── Audiences ────────────────────────────────────────────────────────────


@router.get(
    "/audiences/{brand_id}",
    tags=["portal"],
    summary="Locale-formatted audiences list for the portal",
)
async def portal_audiences(
    brand_id: str,
    page: int = Query(1, ge=1),
    size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    search: str | None = Query(None),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Wraps :func:`list_brand_audiences` with i18n-friendly fields."""
    from app.routers.audiences import (
        AUDIENCE_KEY,
        AUDIENCE_MEMBERS_KEY,
        BRAND_AUDIENCES_KEY,
        STATUS_READY,
    )

    aids = await r.smembers(BRAND_AUDIENCES_KEY.format(bid=brand_id))
    rows: list[dict[str, Any]] = []
    for aid in aids:
        raw = await r.hgetall(AUDIENCE_KEY.format(aid=aid))
        if not raw:
            continue
        try:
            sz = int(await r.scard(AUDIENCE_MEMBERS_KEY.format(aid=aid)) or 0)
        except Exception:
            sz = 0
        rows.append({
            "audience_id": aid,
            "name": raw.get("name"),
            "source": raw.get("source"),
            "is_lookalike": raw.get("is_lookalike") == "true",
            "similarity": int(raw.get("similarity", 0) or 0),
            "size": sz,
            "status": _status_envelope(
                raw.get("status", STATUS_READY), _AUDIENCE_STATUS_I18N
            ),
            "created_at": _ts(float(raw.get("created_at", 0) or 0)),
            "last_updated": _ts(float(raw.get("last_updated", 0) or 0)),
        })

    if search:
        needle = search.lower()
        rows = [row for row in rows if needle in (row.get("name") or "").lower()]

    rows.sort(
        key=lambda row: (row.get("created_at") or {}).get("epoch_seconds", 0),
        reverse=True,
    )

    offset = (page - 1) * size
    page_rows = rows[offset:offset + size]
    envelope = list_response(
        items=page_rows, total=len(rows), limit=size, offset=offset
    )
    return {
        **envelope,
        "brand_id": brand_id,
        "page": page,
        "size": size,
    }


# ── Search ───────────────────────────────────────────────────────────────


@router.get(
    "/search",
    tags=["portal"],
    summary="Global portal search across campaigns / audiences / help",
)
async def portal_search(
    q: str = Query(..., min_length=1, max_length=200),
    brand_id: str = Query(...),
    limit: int = Query(20, ge=1, le=100),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Ranked, bounded, multi-entity search.

    Returns up to ``limit`` results across campaigns, audiences, report
    templates, and help docs. Score is a simple substring-rank — full
    inverted-index search is a follow-up if portal traffic justifies it.
    """
    from app.routers.audiences import (
        AUDIENCE_KEY,
        BRAND_AUDIENCES_KEY,
    )
    from app.routers.campaigns import BRAND_CAMPAIGNS_KEY, CAMPAIGN_KEY

    needle = q.lower()
    results: list[dict[str, Any]] = []

    # Campaigns
    cids = await r.smembers(BRAND_CAMPAIGNS_KEY.format(bid=brand_id))
    for cid in cids:
        raw = await r.hgetall(CAMPAIGN_KEY.format(cid=cid))
        if not raw:
            continue
        name = (raw.get("name") or "").lower()
        if needle in name or needle in cid.lower():
            results.append({
                "kind": "campaign",
                "id": cid,
                "title": raw.get("name") or cid,
                "subtitle": raw.get("objective", ""),
                "url": f"/portal/campaigns/{cid}",
                "score": 1.0 if name.startswith(needle) else 0.5,
            })

    # Audiences
    aids = await r.smembers(BRAND_AUDIENCES_KEY.format(bid=brand_id))
    for aid in aids:
        raw = await r.hgetall(AUDIENCE_KEY.format(aid=aid))
        if not raw:
            continue
        name = (raw.get("name") or "").lower()
        if needle in name or needle in aid.lower():
            results.append({
                "kind": "audience",
                "id": aid,
                "title": raw.get("name") or aid,
                "subtitle": raw.get("source") or "",
                "url": f"/portal/audiences/{aid}",
                "score": 1.0 if name.startswith(needle) else 0.5,
            })

    # Static report templates (placeholder — reporting router has
    # /templates that we could cross-reference once portal exposes them).
    _HELP_DOCS: list[dict[str, str]] = [
        {
            "title": "Getting started with campaigns",
            "subtitle": "How to set a daily budget",
            "url": "/landing/api-docs/#campaigns",
        },
        {
            "title": "Audiences & Lookalike",
            "subtitle": "Building a 1–10 similarity audience",
            "url": "/landing/api-docs/#audiences",
        },
        {
            "title": "Wallet auto-recharge",
            "subtitle": "Trigger, threshold, and cap",
            "url": "/landing/api-docs/#wallet",
        },
    ]
    for doc in _HELP_DOCS:
        if needle in doc["title"].lower() or needle in doc["subtitle"].lower():
            results.append({
                "kind": "help",
                "id": doc["url"],
                "title": doc["title"],
                "subtitle": doc["subtitle"],
                "url": doc["url"],
                "score": 0.3,
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    sliced = results[:limit]

    return {
        "query": q,
        "brand_id": brand_id,
        "items": sliced,
        "count": len(sliced),
        "total": len(results),
        "has_more": len(results) > limit,
    }


# ── Notifications feed ───────────────────────────────────────────────────


def _notif_key(brand_id: str) -> str:
    return NOTIFICATION_LIST_KEY.format(brand_id=brand_id)


async def record_notification(
    r: aioredis.Redis,
    brand_id: str,
    kind: str,
    *,
    severity: str = "info",
    title: str | None = None,
    body: str | None = None,
    i18n_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Append a notification to the brand's portal feed.

    Designed to be called from existing routers (wallet, campaigns,
    disputes) as a side-effect of state changes. Idempotent only by
    timestamp — callers should embed a dedupe key in ``metadata`` if
    re-fire is undesirable.
    """
    ts = _now()
    notif_id = f"ntf_{int(ts * 1000):x}"
    entry = {
        "id": notif_id,
        "ts": ts,
        "kind": kind,
        "severity": severity,
        "title": title or kind,
        "body": body or "",
        "i18n_key": i18n_key or f"notif.{kind}",
        "metadata": metadata or {},
        "read": False,
    }
    key = _notif_key(brand_id)
    pipe = r.pipeline()
    pipe.lpush(key, json.dumps(entry))
    pipe.ltrim(key, 0, NOTIFICATION_MAX_LEN - 1)
    await pipe.execute()
    return notif_id


@router.get(
    "/notifications/{brand_id}",
    tags=["portal"],
    summary="Notification feed (LIST-backed) with since-cursor",
)
async def portal_notifications(
    brand_id: str,
    since: float | None = Query(
        None,
        description="Return only notifications strictly newer than this Unix timestamp.",
    ),
    limit: int = Query(50, ge=1, le=NOTIFICATION_MAX_LEN),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return notifications most-recent first."""
    raws = await r.lrange(_notif_key(brand_id), 0, limit * 2 - 1)
    items: list[dict[str, Any]] = []
    for raw in raws:
        try:
            entry = json.loads(raw)
        except (TypeError, ValueError):
            continue
        ts = float(entry.get("ts", 0) or 0)
        if since is not None and ts <= float(since):
            continue
        # Attach localised display fields without mutating storage shape.
        entry["created_at"] = _ts(ts)
        if entry.get("i18n_key"):
            entry["display_label"] = i18n_t(
                entry["i18n_key"], locale=get_current_locale()
            )
        items.append(entry)
        if len(items) >= limit:
            break

    next_cursor = items[0]["ts"] if items else (since or 0)
    return {
        "brand_id": brand_id,
        "items": items,
        "count": len(items),
        "since": since,
        "next_cursor": next_cursor,
        "has_more": len(raws) >= limit * 2 and len(items) >= limit,
    }


@router.post(
    "/notifications/{brand_id}/test",
    tags=["portal"],
    summary="Test-only: synthesise a notification into the brand feed",
    include_in_schema=False,
)
async def portal_notifications_test(
    brand_id: str,
    kind: str = Query("test"),
    title: str = Query("Test notification"),
    severity: str = Query("info"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Test fixture so the suite can verify since-cursor semantics.

    Not part of the public contract (``include_in_schema=False``). Real
    callers should use :func:`record_notification` directly.
    """
    notif_id = await record_notification(
        r, brand_id, kind, severity=severity, title=title
    )
    return {"ok": True, "id": notif_id}


# ── Date-range comparison ────────────────────────────────────────────────


_VALID_COMPARE_METRICS: frozenset[str] = frozenset(
    {"spend", "conversions", "scans", "new_users", "revenue"}
)
_VALID_COMPARE_RANGES: dict[str, int] = {
    "last7d": 7,
    "last14d": 14,
    "last30d": 30,
    "last90d": 90,
}


async def _metric_for_range(
    r: aioredis.Redis,
    brand_id: str,
    metric: str,
    days: int,
    end_offset: int = 0,
) -> int:
    """Sum *metric* over a contiguous day window.

    ``end_offset=0`` → window ends today. ``end_offset=N`` shifts the
    window N days into the past (so end_offset=days yields the "prior"
    period for direct comparison).
    """
    total = 0
    for i in range(days):
        day = _day_offset(end_offset + i)
        if metric == "spend":
            # Sum campaign-level daily spend across the brand.
            from app.routers.campaigns import BRAND_CAMPAIGNS_KEY
            cids = await r.smembers(
                BRAND_CAMPAIGNS_KEY.format(bid=brand_id)
            )
            for cid in cids:
                try:
                    total += int(
                        await r.get(f"campaign:{cid}:spend_daily:{day}") or 0
                    )
                except (TypeError, ValueError):
                    continue
        elif metric == "conversions":
            from app.routers.dashboards import _zset_count_for_day
            total += await _zset_count_for_day(
                r, f"brand:{brand_id}:redeemed_vouchers", day
            )
        elif metric == "scans":
            from app.routers.dashboards import _safe_scard
            total += await _safe_scard(r, f"brand:{brand_id}:qr_scans:{day}")
        elif metric == "new_users":
            from app.routers.dashboards import _safe_scard
            total += await _safe_scard(
                r, f"brand:{brand_id}:users_acquired:{day}"
            )
        elif metric == "revenue":
            from app.routers.dashboards import _conversion_value_cents
            total += await _conversion_value_cents(r, brand_id, day)
    return total


@router.get(
    "/date-range-compare",
    tags=["portal"],
    summary="Compare a metric across current vs prior period",
)
async def portal_date_range_compare(
    metric: str = Query("spend"),
    range: str = Query("last7d", alias="range"),
    brand_id: str = Query(...),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return ``current`` and ``previous`` totals + delta + delta_pct."""
    if metric not in _VALID_COMPARE_METRICS:
        raise error_response(
            422,
            "invalid_metric",
            f"metric must be one of {sorted(_VALID_COMPARE_METRICS)}",
            requested=metric,
        )
    if range not in _VALID_COMPARE_RANGES:
        raise error_response(
            422,
            "invalid_range",
            f"range must be one of {sorted(_VALID_COMPARE_RANGES)}",
            requested=range,
        )

    days = _VALID_COMPARE_RANGES[range]
    current = await _metric_for_range(r, brand_id, metric, days, end_offset=0)
    previous = await _metric_for_range(
        r, brand_id, metric, days, end_offset=days
    )
    delta = current - previous
    delta_pct = (delta / previous) if previous > 0 else None

    is_money = metric in {"spend", "revenue"}
    if is_money:
        wallet = await _read_wallet_balance(r, brand_id)
        currency = wallet["currency"]
        current_view = _money(current, currency)
        previous_view = _money(previous, currency)
    else:
        current_view = {"value": current}
        previous_view = {"value": previous}

    return {
        "brand_id": brand_id,
        "metric": metric,
        "range": range,
        "current": current_view,
        "previous": previous_view,
        "delta": delta,
        "delta_pct": delta_pct,
        "improved": (
            delta >= 0 if metric != "cpa_actual" else delta <= 0
        ),
    }


# ── CSV export ───────────────────────────────────────────────────────────


_EXPORTABLE_TYPES: frozenset[str] = frozenset({"campaigns", "audiences"})


@router.get(
    "/export.csv",
    tags=["portal"],
    summary="CSV export of campaigns or audiences",
)
async def portal_export_csv(
    type: str = Query("campaigns"),
    brand_id: str = Query(...),
    r: aioredis.Redis = Depends(get_redis),
) -> Response:
    """Stream a CSV — money rendered in the brand's wallet currency."""
    if type not in _EXPORTABLE_TYPES:
        raise error_response(
            422,
            "invalid_export_type",
            f"type must be one of {sorted(_EXPORTABLE_TYPES)}",
            requested=type,
        )

    buf = io.StringIO()
    writer = csv.writer(buf)

    if type == "campaigns":
        from app.routers.campaigns import (
            BRAND_CAMPAIGNS_KEY,
            _load_and_refresh,
            _read_stats,
        )
        wallet = await _read_wallet_balance(r, brand_id)
        currency = wallet["currency"]
        writer.writerow([
            "campaign_id", "name", "objective", "status",
            "daily_budget_cents", "total_budget_cents",
            "spend_cents", "revenue_cents",
            "impressions", "clicks", "conversions",
            "ctr", "cvr", "roas", "currency",
            "created_at_iso",
        ])
        cids = await r.smembers(BRAND_CAMPAIGNS_KEY.format(bid=brand_id))
        for cid in cids:
            try:
                raw = await _load_and_refresh(r, cid)
            except HTTPException:
                continue
            stats = await _read_stats(r, cid)
            created = float(raw.get("created_at", 0) or 0)
            created_iso = (
                datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
                if created else ""
            )
            writer.writerow([
                raw.get("campaign_id"),
                raw.get("name", ""),
                raw.get("objective", ""),
                raw.get("status", ""),
                int(raw.get("daily_budget_cents", 0) or 0),
                int(raw.get("total_budget_cents", 0) or 0),
                int(stats.get("spend_cents", 0) or 0),
                int(stats.get("revenue_cents", 0) or 0),
                int(stats.get("impressions", 0) or 0),
                int(stats.get("clicks", 0) or 0),
                int(stats.get("conversions", 0) or 0),
                stats.get("ctr", 0.0),
                stats.get("cvr", 0.0),
                stats.get("roas", 0.0),
                currency,
                created_iso,
            ])

    elif type == "audiences":
        from app.routers.audiences import (
            AUDIENCE_KEY,
            AUDIENCE_MEMBERS_KEY,
            BRAND_AUDIENCES_KEY,
        )
        writer.writerow([
            "audience_id", "name", "source", "is_lookalike",
            "similarity", "size", "status", "created_at_iso",
        ])
        aids = await r.smembers(BRAND_AUDIENCES_KEY.format(bid=brand_id))
        for aid in aids:
            raw = await r.hgetall(AUDIENCE_KEY.format(aid=aid))
            if not raw:
                continue
            try:
                sz = int(
                    await r.scard(AUDIENCE_MEMBERS_KEY.format(aid=aid)) or 0
                )
            except Exception:
                sz = 0
            created = float(raw.get("created_at", 0) or 0)
            created_iso = (
                datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
                if created else ""
            )
            writer.writerow([
                aid,
                raw.get("name", ""),
                raw.get("source", ""),
                raw.get("is_lookalike", "false"),
                int(raw.get("similarity", 0) or 0),
                sz,
                raw.get("status", ""),
                created_iso,
            ])

    payload = buf.getvalue()
    filename = f"{type}-{brand_id}-{_today_str()}.csv"
    return Response(
        content=payload,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


__all__ = [
    "router",
    "record_notification",
]
