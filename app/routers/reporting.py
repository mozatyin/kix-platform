"""Multi-Dimensional Reporting Engine — TikTok/Google Ads Manager parity.

While ``dashboards.py`` ships a fast TODAY view (5-10 KPIs), serious
merchants want the slice-and-dice they get in TikTok Ads Manager or
Google Ads: pick *any* combination of N dimensions × M metrics, drag in
filters, sort + paginate. This module implements that surface.

Design
------
At write time (impression / click / conversion / engagement /
voucher event / scan / ...) we INCR a fixed set of pre-aggregated
counter HASHes keyed by (brand, dimension-tuple, value-tuple). Each
counter HASH stores every metric for that exact slice. At read time
we look up the requested slice directly — no scatter-gather, no key
scanning on the hot path.

We refuse to materialise every C(20, k) combo (combinatorial
explosion). Instead, the writer maintains the ~25 most useful slices
that cover the dashboards merchants actually build. When the query
asks for a slice we don't pre-compute, we return a structured
"unsupported_combo" error listing the combos that *are* available so
ops can add the missing one. Every refusal is recorded against
``reports:unsupported_demand`` so the team can prioritise.

Storage layout
--------------
::

  reports:brand:{bid}:cube:{combo_name}:{value_tuple}
      HASH:
        impressions, unique_impressions, reach,
        clicks, engagements, conversions, view_through_conversions,
        spend_cents, revenue_cents,
        quality_score_sum, quality_score_n,
        completion_sum, completion_n,
        vouchers_issued, vouchers_redeemed,
        trial_starts, subscriptions, renewals, churns,
        new_users, returning_users, store_visits,
        ...

  reports:brand:{bid}:cube:{combo_name}:index:{date}
      SET of ``value_tuple`` strings — lets us enumerate rows for a
      given combo on a given day without a KEYS scan.

  reports:brand:{bid}:unique:{combo_name}:{value_tuple}:users
      SET of user_id  (uniques / reach — 35-day TTL). LEGACY: kept
      under dual-write during the 30-day HLL transition; will be
      retired once read traffic is fully served by HLL.

  reports:brand:{bid}:unique:{combo_name}:{value_tuple}:dfp
      SET of device_fingerprint (unique_impressions counter). LEGACY.

  reports:brand:{bid}:hll:{combo_name}:{value_tuple}:users
      HyperLogLog of user_id (reach). Fixed ~12 KB regardless of
      cardinality, ~0.81% standard error. PFADD on write,
      PFCOUNT on read, PFMERGE for cross-slice aggregation.

  reports:brand:{bid}:hll:{combo_name}:{value_tuple}:dfp
      HyperLogLog of device_fingerprint (unique_impressions).

All keys carry a 35-day TTL.

Combos materialised (write side)
--------------------------------
1.  date
2.  hour
3.  day_of_week
4.  campaign
5.  ad
6.  placement
7.  device
8.  country
9.  audience
10. date × campaign
11. date × ad
12. date × placement
13. date × device
14. date × country
15. date × os
16. date × language
17. date × audience
18. date × campaign × device
19. date × campaign × country
20. date × campaign × placement
21. campaign × ad
22. hour × day_of_week                (heatmap)
23. date × source_brand
24. date × age_bucket
25. date × gender

Query API (read side)
---------------------
::

   POST /api/v1/reporting/query
     {
       brand_id, dimensions: [...], metrics: [...],
       filters: { date: {from, to}, device: [...], ... },
       order_by: "<metric> <asc|desc>",
       limit: 100,
       offset: 0,
     }

   GET /api/v1/reporting/dimensions
   GET /api/v1/reporting/metrics
   GET /api/v1/reporting/combos               -- list materialised combos
   GET /api/v1/reporting/templates/{template_name}/{brand_id}
   GET /api/v1/reporting/cohort-retention/{brand_id}?period=weekly&weeks_back=12
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from uuid import uuid4

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Public catalogue ─────────────────────────────────────────────────────

DIMENSIONS: tuple[str, ...] = (
    "date",
    "hour",
    "day_of_week",
    "brand_id",
    "campaign_id",
    "adgroup_id",
    "ad_id",
    "country",
    "city",
    "region",
    "device",
    "os",
    "browser",
    "placement",
    "audience_id",
    "language",
    "age_bucket",
    "gender",
    "tier",
    "source_brand",
)

# Metric catalogue. Numeric counter metrics are stored verbatim;
# *derived* metrics (CTR / CVR / CPM / CPC / CPA / ROAS) are computed
# from the raw counters at query time.
COUNTER_METRICS: tuple[str, ...] = (
    "impressions",
    "clicks",
    "engagements",
    "conversions",
    "view_through_conversions",
    "spend_cents",
    "revenue_cents",
    "vouchers_issued",
    "vouchers_redeemed",
    "trial_starts",
    "subscriptions",
    "renewals",
    "churns",
    "new_users",
    "returning_users",
    "store_visits",
    "attributed_value_cents",
    "attributed_conversions",
)

# Composite metrics (post-aggregation).
DERIVED_METRICS: tuple[str, ...] = (
    "click_through_rate",
    "engagement_rate",
    "conversion_rate",
    "cpm",
    "cpc",
    "cpa",
    "roas",
    "quality_score_avg",
    "frequency",
    "completion_rate",
    "voucher_redemption_rate",
    "unique_impressions",
    "reach",
)

METRICS: tuple[str, ...] = COUNTER_METRICS + DERIVED_METRICS


# ── Combo registry ───────────────────────────────────────────────────────
#
# A *combo* names a tuple of dimensions for which we materialise counters
# at write time. Order matters — it is the storage tuple order used by the
# value_tuple key.

@dataclass(frozen=True)
class Combo:
    name: str
    dims: tuple[str, ...]


_COMBOS: tuple[Combo, ...] = (
    Combo("date", ("date",)),
    Combo("hour", ("hour",)),
    Combo("dow", ("day_of_week",)),
    Combo("campaign", ("campaign_id",)),
    Combo("ad", ("ad_id",)),
    Combo("placement", ("placement",)),
    Combo("device", ("device",)),
    Combo("country", ("country",)),
    Combo("audience", ("audience_id",)),
    Combo("date_campaign", ("date", "campaign_id")),
    Combo("date_ad", ("date", "ad_id")),
    Combo("date_placement", ("date", "placement")),
    Combo("date_device", ("date", "device")),
    Combo("date_country", ("date", "country")),
    Combo("date_os", ("date", "os")),
    Combo("date_language", ("date", "language")),
    Combo("date_audience", ("date", "audience_id")),
    Combo("date_campaign_device", ("date", "campaign_id", "device")),
    Combo("date_campaign_country", ("date", "campaign_id", "country")),
    Combo("date_campaign_placement", ("date", "campaign_id", "placement")),
    Combo("campaign_ad", ("campaign_id", "ad_id")),
    Combo("hour_dow", ("hour", "day_of_week")),
    Combo("date_source_brand", ("date", "source_brand")),
    Combo("date_age", ("date", "age_bucket")),
    Combo("date_gender", ("date", "gender")),
)

COMBO_BY_DIMS: dict[tuple[str, ...], Combo] = {
    tuple(sorted(c.dims)): c for c in _COMBOS
}
COMBO_BY_NAME: dict[str, Combo] = {c.name: c for c in _COMBOS}


def _lookup_combo(dims: Iterable[str]) -> Combo | None:
    return COMBO_BY_DIMS.get(tuple(sorted(dims)))


# ── Key shaping ──────────────────────────────────────────────────────────

# 35 days = ~5 weeks of daily counters. Enough headroom for month-over-
# month merchant dashboards without unbounded growth.
CUBE_TTL_SECONDS = 35 * 86400
UNIQUE_TTL_SECONDS = 35 * 86400


def _cube_key(brand_id: str, combo: Combo, value_tuple: tuple[str, ...]) -> str:
    vt = "|".join(value_tuple)
    return f"reports:brand:{brand_id}:cube:{combo.name}:{vt}"


def _index_key(brand_id: str, combo: Combo, date: str) -> str:
    """Index set of value_tuples seen on ``date`` for ``combo``.

    For combos that include ``date`` we use the row's date. For combos
    without date we use today's date — the index lets us list rows
    written today, which is enough for daily-paged dashboards and
    keeps the universe bounded.
    """
    return f"reports:brand:{brand_id}:cube:{combo.name}:index:{date}"


def _unique_users_key(
    brand_id: str, combo: Combo, value_tuple: tuple[str, ...]
) -> str:
    vt = "|".join(value_tuple)
    return f"reports:brand:{brand_id}:unique:{combo.name}:{vt}:users"


def _unique_dfp_key(
    brand_id: str, combo: Combo, value_tuple: tuple[str, ...]
) -> str:
    vt = "|".join(value_tuple)
    return f"reports:brand:{brand_id}:unique:{combo.name}:{vt}:dfp"


def _hll_users_key(
    brand_id: str, combo: Combo, value_tuple: tuple[str, ...]
) -> str:
    """HyperLogLog reach key (PFADD user_id)."""
    vt = "|".join(value_tuple)
    return f"reports:brand:{brand_id}:hll:{combo.name}:{vt}:users"


def _hll_dfp_key(
    brand_id: str, combo: Combo, value_tuple: tuple[str, ...]
) -> str:
    """HyperLogLog unique_impressions key (PFADD device_fingerprint)."""
    vt = "|".join(value_tuple)
    return f"reports:brand:{brand_id}:hll:{combo.name}:{vt}:dfp"


# ── Time helpers ─────────────────────────────────────────────────────────


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _today_str() -> str:
    return _now_utc().strftime("%Y-%m-%d")


def _date_range(date_from: str, date_to: str) -> list[str]:
    """Return inclusive YYYY-MM-DD list. Caps at 366 days to bound work."""
    try:
        d1 = datetime.strptime(date_from, "%Y-%m-%d").date()
        d2 = datetime.strptime(date_to, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"bad date: {exc}") from exc
    if d2 < d1:
        d1, d2 = d2, d1
    days = (d2 - d1).days + 1
    if days > 366:
        raise HTTPException(
            status_code=422, detail="date range exceeds 366 days"
        )
    return [(d1 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


def _age_bucket(age: int | None) -> str:
    if age is None or age < 0:
        return "unknown"
    if age < 18:
        return "u18"
    if age < 25:
        return "18-24"
    if age < 35:
        return "25-34"
    if age < 45:
        return "35-44"
    if age < 55:
        return "45-54"
    if age < 65:
        return "55-64"
    return "65+"


# ── Writer (called from auction/attribution/etc.) ────────────────────────


def _norm_dim_value(v: Any) -> str:
    """Stable string serialisation of a dimension value."""
    if v is None or v == "":
        return "unknown"
    return str(v)


async def _record_unsupported(
    r: aioredis.Redis, dims: tuple[str, ...]
) -> None:
    """Log unsupported combo demand so we can prioritise adding combos."""
    try:
        await r.hincrby(
            "reports:unsupported_demand", "|".join(sorted(dims)), 1
        )
    except Exception:
        pass


async def write_event(
    r: aioredis.Redis,
    *,
    brand_id: str,
    dim_values: dict[str, Any],
    metric_deltas: dict[str, int],
    user_id: str | None = None,
    device_fingerprint: str | None = None,
) -> None:
    """Increment every materialised combo for this event.

    ``dim_values`` should contain as many of ``DIMENSIONS`` as the
    caller knows — missing dimensions degrade to "unknown" so the
    counter for that slice still moves (TikTok behaviour: "Unknown"
    bucket rather than dropping the event).

    ``metric_deltas`` maps a counter metric to its integer delta
    (always non-negative; this is an INCR, not an arbitrary set).

    Never raises — reporting must not break the hot ad path.
    """
    if not brand_id:
        return
    try:
        # Default dims we always know.
        now = _now_utc()
        dims_full: dict[str, str] = {
            "date": _norm_dim_value(
                dim_values.get("date") or now.strftime("%Y-%m-%d")
            ),
            "hour": _norm_dim_value(
                dim_values.get("hour")
                if dim_values.get("hour") is not None
                else now.hour
            ),
            "day_of_week": _norm_dim_value(
                dim_values.get("day_of_week")
                if dim_values.get("day_of_week") is not None
                else now.weekday()
            ),
            "brand_id": brand_id,
        }
        for d in DIMENSIONS:
            if d in dims_full:
                continue
            dims_full[d] = _norm_dim_value(dim_values.get(d))

        # Sanitise deltas — only known counter metrics, only ints.
        clean: dict[str, int] = {}
        for k, v in metric_deltas.items():
            if k not in COUNTER_METRICS:
                continue
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv == 0:
                continue
            clean[k] = iv
        if not clean:
            # No counters to bump — but uniques may still be relevant.
            pass

        pipe = r.pipeline(transaction=False)
        # Iterate all materialised combos.
        for combo in _COMBOS:
            value_tuple = tuple(dims_full[d] for d in combo.dims)
            ck = _cube_key(brand_id, combo, value_tuple)
            for metric, delta in clean.items():
                pipe.hincrby(ck, metric, delta)
            pipe.expire(ck, CUBE_TTL_SECONDS)

            # Maintain index for enumeration. Use today's date if combo
            # doesn't include date; row will live in today's index.
            idx_date = (
                dims_full["date"]
                if "date" in combo.dims
                else dims_full["date"]
            )
            ix = _index_key(brand_id, combo, idx_date)
            pipe.sadd(ix, "|".join(value_tuple))
            pipe.expire(ix, CUBE_TTL_SECONDS)

            # Uniques (impression-driven). Only fold the user into the
            # uniques set when there's actually a user_id and the event
            # represents a viewing event (impressions / engagements /
            # clicks / conversions).
            #
            # DUAL-WRITE during HLL migration: both the legacy SET
            # (SCARD-counted) and the new HLL (PFCOUNT-counted) receive
            # the id. Once read traffic is fully served by HLL across
            # the 30-day transition the SET writes can be dropped.
            if (
                user_id
                and clean.get("impressions", 0)
                + clean.get("clicks", 0)
                + clean.get("conversions", 0)
                + clean.get("engagements", 0)
                > 0
            ):
                uk = _unique_users_key(brand_id, combo, value_tuple)
                pipe.sadd(uk, user_id)  # legacy SET — TODO: retire after 30d
                pipe.expire(uk, UNIQUE_TTL_SECONDS)
                huk = _hll_users_key(brand_id, combo, value_tuple)
                pipe.pfadd(huk, user_id)
                pipe.expire(huk, UNIQUE_TTL_SECONDS)
            if device_fingerprint and clean.get("impressions", 0) > 0:
                dk = _unique_dfp_key(brand_id, combo, value_tuple)
                pipe.sadd(dk, device_fingerprint)  # legacy SET
                pipe.expire(dk, UNIQUE_TTL_SECONDS)
                hdk = _hll_dfp_key(brand_id, combo, value_tuple)
                pipe.pfadd(hdk, device_fingerprint)
                pipe.expire(hdk, UNIQUE_TTL_SECONDS)

        # Quality score running totals: stored as (sum, n) on the
        # canonical (date) and (campaign_id) cubes only — these are the
        # views where merchants display "average QS" today.
        qs = metric_deltas.get("quality_score_inst")
        if qs is not None:
            try:
                qsi = float(qs)
            except (TypeError, ValueError):
                qsi = None
            if qsi is not None and 0.0 <= qsi <= 1.0:
                for cname in ("date", "campaign", "date_campaign"):
                    combo = COMBO_BY_NAME[cname]
                    value_tuple = tuple(dims_full[d] for d in combo.dims)
                    ck = _cube_key(brand_id, combo, value_tuple)
                    pipe.hincrbyfloat(ck, "quality_score_sum", qsi)
                    pipe.hincrby(ck, "quality_score_n", 1)

        completion = metric_deltas.get("completion_inst")
        if completion is not None:
            try:
                ci = float(completion)
            except (TypeError, ValueError):
                ci = None
            if ci is not None and 0.0 <= ci <= 1.0:
                for cname in ("date", "campaign", "date_campaign"):
                    combo = COMBO_BY_NAME[cname]
                    value_tuple = tuple(dims_full[d] for d in combo.dims)
                    ck = _cube_key(brand_id, combo, value_tuple)
                    pipe.hincrbyfloat(ck, "completion_sum", ci)
                    pipe.hincrby(ck, "completion_n", 1)

        await pipe.execute()
    except Exception as exc:  # pragma: no cover — never break hot path
        logger.warning("reporting.write_event failed: %s", exc)


# ── Aggregated reads ─────────────────────────────────────────────────────


async def _read_cube_row(
    r: aioredis.Redis,
    brand_id: str,
    combo: Combo,
    value_tuple: tuple[str, ...],
) -> dict[str, Any]:
    """Return raw counter HASH + uniques cardinality for one slice."""
    ck = _cube_key(brand_id, combo, value_tuple)
    raw = await r.hgetall(ck) or {}

    out: dict[str, Any] = {}
    for k, v in raw.items():
        try:
            if "." in v:
                out[k] = float(v)
            else:
                out[k] = int(v)
        except (TypeError, ValueError):
            out[k] = v

    # Uniques: prefer HyperLogLog (PFCOUNT, ~12 KB / key, ~0.81% error);
    # fall back to legacy SET SCARD for slices not yet migrated. The
    # fallback only triggers when the HLL is genuinely empty (PFCOUNT
    # returns 0), so well-populated migrated keys never pay the second
    # round-trip on the hot read path.
    uk = _unique_users_key(brand_id, combo, value_tuple)
    dk = _unique_dfp_key(brand_id, combo, value_tuple)
    huk = _hll_users_key(brand_id, combo, value_tuple)
    hdk = _hll_dfp_key(brand_id, combo, value_tuple)
    try:
        reach = await r.pfcount(huk)
        reach = int(reach or 0)
        if reach == 0:
            try:
                reach = int(await r.scard(uk) or 0)
            except Exception:
                reach = 0
        out["reach"] = reach
    except Exception:
        try:
            out["reach"] = int(await r.scard(uk) or 0)
        except Exception:
            out["reach"] = 0
    try:
        uniq_imp = await r.pfcount(hdk)
        uniq_imp = int(uniq_imp or 0)
        if uniq_imp == 0:
            try:
                uniq_imp = int(await r.scard(dk) or 0)
            except Exception:
                uniq_imp = 0
        out["unique_impressions"] = uniq_imp
    except Exception:
        try:
            out["unique_impressions"] = int(await r.scard(dk) or 0)
        except Exception:
            out["unique_impressions"] = 0
    return out


# ── HLL aggregation across dimensions ────────────────────────────────────


async def aggregate_unique_via_hll(
    r: aioredis.Redis,
    brand_id: str,
    combo: Combo,
    value_tuples: Iterable[tuple[str, ...]],
    kind: str = "users",
) -> int:
    """Merge multiple HLL slices into a deduplicated cardinality.

    Use when callers need a total reach/unique-impressions across many
    slices (e.g. total reach across all countries on a given date).
    PFMERGE is associative + idempotent, so the result correctly
    de-duplicates ids that appear in multiple slices — something SCARD
    + sum cannot do.

    ``kind`` is either ``"users"`` (→ reach) or ``"dfp"`` (→
    unique_impressions). Returns the aggregated cardinality estimate.
    """
    if kind == "users":
        keyfn = _hll_users_key
        legacy_keyfn = _unique_users_key
    elif kind == "dfp":
        keyfn = _hll_dfp_key
        legacy_keyfn = _unique_dfp_key
    else:
        raise ValueError(f"kind must be 'users' or 'dfp', got {kind!r}")

    vts = list(value_tuples)
    if not vts:
        return 0

    hll_keys = [keyfn(brand_id, combo, vt) for vt in vts]
    temp_key = f"_hll_agg:{uuid4().hex[:12]}"
    try:
        try:
            await r.pfmerge(temp_key, *hll_keys)
            count = int(await r.pfcount(temp_key) or 0)
        finally:
            try:
                await r.delete(temp_key)
            except Exception:
                pass
        if count > 0:
            return count
        # All HLLs empty — try a legacy SUNIONSTORE across SET fallbacks
        # so callers don't see a hard zero during migration.
        legacy_keys = [legacy_keyfn(brand_id, combo, vt) for vt in vts]
        tmp_set = f"_set_agg:{uuid4().hex[:12]}"
        try:
            await r.sunionstore(tmp_set, *legacy_keys)
            return int(await r.scard(tmp_set) or 0)
        except Exception:
            return 0
        finally:
            try:
                await r.delete(tmp_set)
            except Exception:
                pass
    except Exception as exc:
        logger.debug("aggregate_unique_via_hll failed: %s", exc)
        return 0


def _apply_derived_metrics(row: dict[str, Any]) -> dict[str, Any]:
    """Compute CTR/CVR/CPM/CPC/CPA/ROAS/etc. from counter snapshot."""
    impr = int(row.get("impressions", 0) or 0)
    clicks = int(row.get("clicks", 0) or 0)
    engagements = int(row.get("engagements", 0) or 0)
    conversions = int(row.get("conversions", 0) or 0)
    spend = int(row.get("spend_cents", 0) or 0)
    revenue = int(row.get("revenue_cents", 0) or 0)
    reach = int(row.get("reach", 0) or 0)
    vissued = int(row.get("vouchers_issued", 0) or 0)
    vredeemed = int(row.get("vouchers_redeemed", 0) or 0)

    if impr > 0:
        row["click_through_rate"] = clicks / impr
        row["engagement_rate"] = engagements / impr
        row["conversion_rate"] = (
            conversions / impr if impr else 0.0
        )
        row["cpm"] = (spend / impr) * 1000.0
    else:
        row["click_through_rate"] = 0.0
        row["engagement_rate"] = 0.0
        row["conversion_rate"] = 0.0
        row["cpm"] = 0.0
    if clicks > 0:
        row["cpc"] = spend / clicks
    else:
        row["cpc"] = 0.0
    if conversions > 0:
        row["cpa"] = spend / conversions
    else:
        row["cpa"] = 0.0
    if spend > 0:
        row["roas"] = revenue / spend
    else:
        row["roas"] = 0.0
    if reach > 0:
        row["frequency"] = impr / reach
    else:
        row["frequency"] = 0.0
    qs_n = int(row.get("quality_score_n", 0) or 0)
    qs_sum = float(row.get("quality_score_sum", 0) or 0)
    row["quality_score_avg"] = qs_sum / qs_n if qs_n > 0 else 0.0
    cmp_n = int(row.get("completion_n", 0) or 0)
    cmp_sum = float(row.get("completion_sum", 0) or 0)
    row["completion_rate"] = cmp_sum / cmp_n if cmp_n > 0 else 0.0
    row["voucher_redemption_rate"] = (
        vredeemed / vissued if vissued > 0 else 0.0
    )
    return row


# ── Filter handling ──────────────────────────────────────────────────────

class DateFilter(BaseModel):
    from_: str = Field(alias="from")
    to: str

    model_config = {"populate_by_name": True}


def _row_passes_filters(
    value_tuple: tuple[str, ...],
    dims: tuple[str, ...],
    filters: dict[str, Any] | None,
) -> bool:
    if not filters:
        return True
    for i, d in enumerate(dims):
        if d == "date":
            continue  # handled by date_range up front
        f = filters.get(d)
        if f is None:
            continue
        vt_val = value_tuple[i]
        if isinstance(f, list):
            if vt_val not in {str(x) for x in f}:
                return False
        elif isinstance(f, str):
            if vt_val != f:
                return False
        elif isinstance(f, dict):
            # range filters like {gte: x, lte: y} — interpret numerically.
            try:
                num = float(vt_val)
            except (TypeError, ValueError):
                continue
            if "gte" in f and num < float(f["gte"]):
                return False
            if "lte" in f and num > float(f["lte"]):
                return False
    return True


# ── Pydantic models for query ────────────────────────────────────────────


class ReportQuery(BaseModel):
    brand_id: str
    dimensions: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    order_by: str | None = None
    limit: int = Field(default=100, ge=1, le=10000)
    offset: int = Field(default=0, ge=0)

    @field_validator("dimensions")
    @classmethod
    def _check_dims(cls, v: list[str]) -> list[str]:
        bad = [d for d in v if d not in DIMENSIONS]
        if bad:
            raise ValueError(f"unknown dimensions: {bad}")
        if len(set(v)) != len(v):
            raise ValueError("duplicate dimensions")
        return v

    @field_validator("metrics")
    @classmethod
    def _check_metrics(cls, v: list[str]) -> list[str]:
        bad = [m for m in v if m not in METRICS]
        if bad:
            raise ValueError(f"unknown metrics: {bad}")
        return v


class ReportRow(BaseModel):
    # Free-form: dimension values + metric values.
    pass


class ReportResponse(BaseModel):
    rows: list[dict[str, Any]]
    total_rows: int
    query_time_ms: int
    combo_used: str
    available_combos: list[str] | None = None


# ── Endpoints: catalogue ─────────────────────────────────────────────────


@router.get("/dimensions")
async def list_dimensions() -> dict[str, Any]:
    """All dimensions the engine understands."""
    return {"dimensions": list(DIMENSIONS), "count": len(DIMENSIONS)}


@router.get("/metrics")
async def list_metrics() -> dict[str, Any]:
    """Counter + derived metrics."""
    return {
        "counter_metrics": list(COUNTER_METRICS),
        "derived_metrics": list(DERIVED_METRICS),
        "all": list(METRICS),
    }


@router.get("/combos")
async def list_combos() -> dict[str, Any]:
    """Materialised dimension combinations available to /query."""
    return {
        "combos": [
            {"name": c.name, "dimensions": list(c.dims)} for c in _COMBOS
        ],
        "count": len(_COMBOS),
    }


@router.get("/unsupported-demand")
async def unsupported_demand(
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Top requested combinations we haven't materialised yet.

    Surfaces what to add next: every /query that hit an
    unsupported combo logged itself here. Ops watches this hash to
    prioritise the materialiser.
    """
    raw = await r.hgetall("reports:unsupported_demand") or {}
    items = sorted(
        (
            {"dimensions": k.split("|"), "request_count": int(v)}
            for k, v in raw.items()
        ),
        key=lambda x: -x["request_count"],
    )
    return {"top_unsupported": items[:50], "total_distinct": len(items)}


# ── Admin: bulk SET → HLL migration ──────────────────────────────────────


class HLLMigrateRequest(BaseModel):
    brand_id: str | None = None
    scan_count: int = Field(default=500, ge=50, le=5000)
    max_keys: int = Field(default=100_000, ge=1, le=10_000_000)
    delete_legacy: bool = False


@router.post("/admin/migrate-to-hll")
async def admin_migrate_to_hll(
    body: HLLMigrateRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Bulk-migrate legacy SET unique-counters into HyperLogLog keys.

    Walks ``reports:brand:{bid}:unique:*:{users,dfp}`` via SCAN (no
    KEYS), and for each SET it SSCANs the members and PFADDs them into
    the matching ``reports:brand:{bid}:hll:*`` key. Idempotent — running
    twice produces the same cardinality estimate.

    When ``delete_legacy=true`` the SET key is dropped after a
    successful PFADD pass. Leave it false during the 30-day transition
    so the read path's SCARD fallback still works while traffic ramps
    onto HLL.

    Returns a per-brand-and-kind summary so ops can confirm coverage
    before flipping the switch in the writer.
    """
    pattern = (
        f"reports:brand:{body.brand_id}:unique:*"
        if body.brand_id
        else "reports:brand:*:unique:*"
    )
    sets_migrated = 0
    members_added = 0
    legacy_deleted = 0
    errors = 0
    keys_seen = 0

    cursor = 0
    started = time.time()
    try:
        while True:
            cursor, batch = await r.scan(
                cursor=cursor, match=pattern, count=body.scan_count
            )
            for key in batch:
                keys_seen += 1
                if keys_seen > body.max_keys:
                    cursor = 0
                    break
                key_s = (
                    key.decode() if isinstance(key, (bytes, bytearray)) else key
                )
                # Only consider :users and :dfp leaves — skip anything else.
                if key_s.endswith(":users"):
                    suffix = ":users"
                elif key_s.endswith(":dfp"):
                    suffix = ":dfp"
                else:
                    continue
                # Map legacy key → HLL key by swapping the segment marker.
                #   reports:brand:{bid}:unique:{combo}:{vt}:users
                # → reports:brand:{bid}:hll:{combo}:{vt}:users
                hll_key = key_s.replace(":unique:", ":hll:", 1)
                try:
                    sub_cursor = 0
                    pipe = r.pipeline(transaction=False)
                    pending = 0
                    while True:
                        sub_cursor, members = await r.sscan(
                            key_s, cursor=sub_cursor, count=body.scan_count
                        )
                        if members:
                            decoded = [
                                m.decode()
                                if isinstance(m, (bytes, bytearray))
                                else m
                                for m in members
                            ]
                            pipe.pfadd(hll_key, *decoded)
                            pending += len(decoded)
                            members_added += len(decoded)
                        if sub_cursor == 0:
                            break
                    pipe.expire(hll_key, UNIQUE_TTL_SECONDS)
                    await pipe.execute()
                    sets_migrated += 1
                    if body.delete_legacy and pending > 0:
                        try:
                            await r.delete(key_s)
                            legacy_deleted += 1
                        except Exception:
                            errors += 1
                except Exception as exc:
                    errors += 1
                    logger.warning(
                        "migrate-to-hll key=%s failed: %s", key_s, exc
                    )
            if cursor == 0 or keys_seen > body.max_keys:
                break
    except Exception as exc:
        logger.exception("migrate-to-hll scan failed: %s", exc)
        raise HTTPException(
            status_code=500, detail=f"scan failed: {exc}"
        ) from exc

    return {
        "ok": True,
        "brand_id": body.brand_id,
        "pattern": pattern,
        "keys_seen": keys_seen,
        "sets_migrated": sets_migrated,
        "members_added": members_added,
        "legacy_deleted": legacy_deleted,
        "errors": errors,
        "elapsed_ms": int((time.time() - started) * 1000),
        "truncated": keys_seen > body.max_keys,
    }


# ── Endpoint: query ──────────────────────────────────────────────────────


@router.post("/query", response_model=ReportResponse)
async def query(
    body: ReportQuery,
    r: aioredis.Redis = Depends(get_redis),
) -> ReportResponse:
    """Slice-and-dice query over the pre-aggregated cubes.

    Behaviour mirrors Ads Manager: pick dims, pick metrics, filter,
    sort, paginate. Date range capped at 366 days. Returns
    422 + ``available_combos`` when the requested dim set isn't
    materialised.
    """
    start = time.time()

    if not body.brand_id:
        raise HTTPException(status_code=422, detail="brand_id required")

    combo = _lookup_combo(body.dimensions) if body.dimensions else None
    if combo is None:
        # Empty dimensions = "no breakdown" — use the `date` combo and
        # aggregate to a single total row.
        if not body.dimensions:
            combo = COMBO_BY_NAME["date"]
        else:
            await _record_unsupported(r, tuple(body.dimensions))
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "dimension_combination_not_supported",
                    "requested": list(body.dimensions),
                    "available_combos": [
                        {"name": c.name, "dimensions": list(c.dims)}
                        for c in _COMBOS
                    ],
                },
            )

    # Date range resolution.
    df = body.filters.get("date") if body.filters else None
    if df and isinstance(df, dict) and "from" in df and "to" in df:
        days = _date_range(df["from"], df["to"])
    else:
        # Default = today only.
        days = [_today_str()]

    # Decide which day-indexes to walk. For combos that include 'date'
    # the index date == row's date. For combos without 'date' the row
    # gets re-touched each day, so the union of indexes across days
    # gives us all relevant value tuples.
    rows: list[dict[str, Any]] = []

    if "date" in combo.dims:
        date_pos = combo.dims.index("date")
        for day in days:
            ix = _index_key(body.brand_id, combo, day)
            try:
                members = await r.smembers(ix)
            except Exception:
                members = set()
            for vt_str in members:
                value_tuple = tuple(vt_str.split("|"))
                if not _row_passes_filters(
                    value_tuple, combo.dims, body.filters
                ):
                    continue
                if value_tuple[date_pos] != day:
                    # Defensive: index may have been populated with
                    # rows whose row-date differs from the index date
                    # (combo writer guards this, but a stale index entry
                    # shouldn't poison the result).
                    continue
                raw = await _read_cube_row(
                    r, body.brand_id, combo, value_tuple
                )
                row: dict[str, Any] = {
                    d: value_tuple[i] for i, d in enumerate(combo.dims)
                }
                row.update(raw)
                _apply_derived_metrics(row)
                rows.append(row)
    else:
        # Combos without date — read once, ignore date filter beyond
        # surfacing whether *anything* exists today.
        seen: set[tuple[str, ...]] = set()
        for day in days:
            ix = _index_key(body.brand_id, combo, day)
            try:
                members = await r.smembers(ix)
            except Exception:
                members = set()
            for vt_str in members:
                value_tuple = tuple(vt_str.split("|"))
                if value_tuple in seen:
                    continue
                seen.add(value_tuple)
                if not _row_passes_filters(
                    value_tuple, combo.dims, body.filters
                ):
                    continue
                raw = await _read_cube_row(
                    r, body.brand_id, combo, value_tuple
                )
                row = {
                    d: value_tuple[i] for i, d in enumerate(combo.dims)
                }
                row.update(raw)
                _apply_derived_metrics(row)
                rows.append(row)

    # If body.metrics specified, project to those columns + dims.
    projected: list[dict[str, Any]]
    if body.metrics:
        projected = []
        for row in rows:
            base = {d: row.get(d) for d in combo.dims}
            for m in body.metrics:
                base[m] = row.get(m, 0)
            projected.append(base)
    else:
        projected = rows

    # Order by.
    if body.order_by:
        parts = body.order_by.strip().split()
        key = parts[0]
        desc = len(parts) > 1 and parts[1].lower() == "desc"
        try:
            projected.sort(
                key=lambda x: (x.get(key) is None, x.get(key) or 0),
                reverse=desc,
            )
        except TypeError:
            projected.sort(
                key=lambda x: str(x.get(key) or ""), reverse=desc
            )

    total = len(projected)
    paged = projected[body.offset : body.offset + body.limit]

    elapsed_ms = int((time.time() - start) * 1000)
    return ReportResponse(
        rows=paged,
        total_rows=total,
        query_time_ms=elapsed_ms,
        combo_used=combo.name,
    )


# ── Templates ────────────────────────────────────────────────────────────

_TEMPLATES: dict[str, dict[str, Any]] = {
    "campaign_performance": {
        "dimensions": ["date", "campaign_id"],
        "metrics": [
            "impressions",
            "clicks",
            "conversions",
            "spend_cents",
            "revenue_cents",
            "click_through_rate",
            "conversion_rate",
            "cpa",
            "roas",
        ],
        "order_by": "spend_cents desc",
    },
    "geo_breakdown": {
        "dimensions": ["date", "country"],
        "metrics": [
            "impressions",
            "clicks",
            "conversions",
            "spend_cents",
            "click_through_rate",
            "conversion_rate",
        ],
        "order_by": "impressions desc",
    },
    "device_breakdown": {
        "dimensions": ["date", "device"],
        "metrics": [
            "impressions",
            "clicks",
            "conversions",
            "click_through_rate",
            "conversion_rate",
        ],
        "order_by": "impressions desc",
    },
    "hourly_heatmap": {
        "dimensions": ["hour", "day_of_week"],
        "metrics": [
            "impressions",
            "engagements",
            "engagement_rate",
        ],
        "order_by": "engagements desc",
    },
    "audience_compare": {
        "dimensions": ["date", "audience_id"],
        "metrics": [
            "impressions",
            "clicks",
            "conversions",
            "spend_cents",
            "click_through_rate",
            "conversion_rate",
        ],
        "order_by": "spend_cents desc",
    },
    "placement_compare": {
        "dimensions": ["date", "placement"],
        "metrics": [
            "impressions",
            "engagements",
            "conversions",
            "spend_cents",
            "conversion_rate",
            "cpa",
        ],
        "order_by": "impressions desc",
    },
    "os_breakdown": {
        "dimensions": ["date", "os"],
        "metrics": [
            "impressions",
            "clicks",
            "conversions",
            "click_through_rate",
            "conversion_rate",
        ],
        "order_by": "impressions desc",
    },
    "cross_brand_attribution": {
        "dimensions": ["date", "source_brand"],
        "metrics": [
            "conversions",
            "revenue_cents",
            "view_through_conversions",
        ],
        "order_by": "revenue_cents desc",
    },
}


@router.get("/templates")
async def list_templates() -> dict[str, Any]:
    return {
        "templates": [
            {
                "name": name,
                "dimensions": tpl["dimensions"],
                "metrics": tpl["metrics"],
                "order_by": tpl.get("order_by"),
            }
            for name, tpl in _TEMPLATES.items()
        ]
    }


@router.get(
    "/templates/{template_name}/{brand_id}", response_model=ReportResponse
)
async def run_template(
    template_name: str,
    brand_id: str,
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=10000),
    offset: int = Query(default=0, ge=0),
    r: aioredis.Redis = Depends(get_redis),
) -> ReportResponse:
    """Run a predefined report.

    Optional date_from/date_to default to the trailing 30 days.
    """
    tpl = _TEMPLATES.get(template_name)
    if not tpl:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "unknown_template",
                "available": list(_TEMPLATES.keys()),
            },
        )
    if not date_to:
        date_to = _today_str()
    if not date_from:
        date_from = (
            _now_utc() - timedelta(days=29)
        ).strftime("%Y-%m-%d")
    qb = ReportQuery(
        brand_id=brand_id,
        dimensions=tpl["dimensions"],
        metrics=tpl["metrics"],
        filters={"date": {"from": date_from, "to": date_to}},
        order_by=tpl.get("order_by"),
        limit=limit,
        offset=offset,
    )
    return await query(qb, r)


# ── Cohort retention ─────────────────────────────────────────────────────


def _iso_week_key(d: datetime) -> str:
    iso = d.isocalendar()
    return f"{iso[0]:04d}-W{iso[1]:02d}"


def _month_key(d: datetime) -> str:
    return d.strftime("%Y-%m")


def _period_key(d: datetime, period: str) -> str:
    if period == "monthly":
        return _month_key(d)
    return _iso_week_key(d)


def _periods_back(period: str, n: int) -> list[str]:
    now = _now_utc()
    out: list[str] = []
    if period == "monthly":
        # Walk back month-by-month.
        cur = now.replace(day=1)
        for _ in range(n):
            out.append(_month_key(cur))
            # Step to previous month.
            prev = cur - timedelta(days=1)
            cur = prev.replace(day=1)
    else:
        cur = now
        for _ in range(n):
            out.append(_iso_week_key(cur))
            cur = cur - timedelta(days=7)
    out.reverse()
    return out


@router.get("/cohort-retention/{brand_id}")
async def cohort_retention(
    brand_id: str,
    period: str = Query(default="weekly", pattern="^(weekly|monthly)$"),
    weeks_back: int = Query(default=12, ge=1, le=52),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Cohort retention matrix.

    Cohort = ``brand:{bid}:cohort:{period_key}`` SET of user_ids whose
    *first_brand_touch* fell in that period. We compute retention by
    intersecting each cohort with that period's *active* set
    (``brand:{bid}:active:{period_key}``) for subsequent periods.

    Sources:
      - ``brand:{bid}:cohort:{period_key}`` — populated by the writer
        (see ``mark_cohort_touch``).
      - ``brand:{bid}:active:{period_key}`` — populated by every event.

    Cohorts older than ``weeks_back`` periods are excluded.
    """
    if brand_id == "":
        raise HTTPException(status_code=422, detail="brand_id required")

    periods = _periods_back(period, weeks_back)
    matrix: list[dict[str, Any]] = []
    for cohort_idx, cohort_period in enumerate(periods):
        cohort_key = f"brand:{brand_id}:cohort:{cohort_period}"
        try:
            cohort_size = await r.scard(cohort_key)
        except Exception:
            cohort_size = 0
        row: dict[str, Any] = {
            "cohort": cohort_period,
            "cohort_size": int(cohort_size or 0),
            "retention": [],
        }
        for offset in range(0, len(periods) - cohort_idx):
            active_period = periods[cohort_idx + offset]
            active_key = f"brand:{brand_id}:active:{active_period}"
            try:
                # SINTERCARD if available, fallback to a temp key.
                retained = await r.sintercard(2, [cohort_key, active_key])
            except Exception:
                # Fallback for older Redis that lacks SINTERCARD.
                try:
                    inter = await r.sinter(cohort_key, active_key)
                    retained = len(inter or [])
                except Exception:
                    retained = 0
            row["retention"].append(
                {
                    "period_offset": offset,
                    "period": active_period,
                    "retained_users": int(retained or 0),
                    "retention_rate": (
                        (int(retained or 0) / int(cohort_size))
                        if cohort_size
                        else 0.0
                    ),
                }
            )
        matrix.append(row)

    return {
        "brand_id": brand_id,
        "period": period,
        "periods": periods,
        "matrix": matrix,
    }


async def mark_cohort_touch(
    r: aioredis.Redis, brand_id: str, user_id: str
) -> None:
    """Stamp the user into the weekly+monthly cohort and active sets.

    First write wins for the cohort assignment (idempotent via SADD).
    Active sets get the user every event so retention can be computed.
    """
    if not brand_id or not user_id:
        return
    try:
        now = _now_utc()
        w = _iso_week_key(now)
        m = _month_key(now)

        first_key = f"brand:{brand_id}:first_touch_period"
        # Has the user already been bucketed into a cohort for this brand?
        existing = await r.hget(first_key, user_id)
        if not existing:
            await r.hset(first_key, user_id, f"{w}|{m}")
            await r.sadd(f"brand:{brand_id}:cohort:{w}", user_id)
            await r.sadd(f"brand:{brand_id}:cohort:{m}", user_id)
            # 12 months retention.
            await r.expire(
                f"brand:{brand_id}:cohort:{w}", 86400 * 7 * 26
            )
            await r.expire(
                f"brand:{brand_id}:cohort:{m}", 86400 * 31 * 13
            )
            await r.expire(first_key, 86400 * 31 * 13)

        # Active sets — touched every event.
        await r.sadd(f"brand:{brand_id}:active:{w}", user_id)
        await r.sadd(f"brand:{brand_id}:active:{m}", user_id)
        await r.expire(
            f"brand:{brand_id}:active:{w}", 86400 * 7 * 26
        )
        await r.expire(
            f"brand:{brand_id}:active:{m}", 86400 * 31 * 13
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("mark_cohort_touch failed: %s", exc)


# ── Convenience writer wrappers ──────────────────────────────────────────
#
# Other routers can either call ``write_event`` directly with the full
# dim dict or use these tight helpers tailored to the common events.


async def record_impression(
    r: aioredis.Redis,
    *,
    brand_id: str,
    campaign_id: str | None,
    ad_id: str | None,
    placement: str | None,
    country: str | None,
    city: str | None,
    device: str | None,
    os_name: str | None,
    language: str | None,
    audience_id: str | None,
    source_brand: str | None,
    user_id: str | None,
    device_fingerprint: str | None,
    quality_score: float | None = None,
    age_bucket: str | None = None,
    gender: str | None = None,
) -> None:
    await write_event(
        r,
        brand_id=brand_id,
        dim_values={
            "campaign_id": campaign_id,
            "ad_id": ad_id,
            "placement": placement,
            "country": country,
            "city": city,
            "device": device,
            "os": os_name,
            "language": language,
            "audience_id": audience_id,
            "source_brand": source_brand,
            "age_bucket": age_bucket,
            "gender": gender,
        },
        metric_deltas={
            "impressions": 1,
            "quality_score_inst": quality_score if quality_score else 0,
        },
        user_id=user_id,
        device_fingerprint=device_fingerprint,
    )
    if user_id:
        await mark_cohort_touch(r, brand_id, user_id)


async def record_click(
    r: aioredis.Redis,
    *,
    brand_id: str,
    campaign_id: str | None,
    ad_id: str | None,
    placement: str | None,
    country: str | None,
    device: str | None,
    os_name: str | None,
    language: str | None,
    source_brand: str | None,
    user_id: str | None,
    device_fingerprint: str | None,
) -> None:
    await write_event(
        r,
        brand_id=brand_id,
        dim_values={
            "campaign_id": campaign_id,
            "ad_id": ad_id,
            "placement": placement,
            "country": country,
            "device": device,
            "os": os_name,
            "language": language,
            "source_brand": source_brand,
        },
        metric_deltas={"clicks": 1},
        user_id=user_id,
        device_fingerprint=device_fingerprint,
    )


async def record_conversion(
    r: aioredis.Redis,
    *,
    brand_id: str,
    campaign_id: str | None,
    ad_id: str | None,
    placement: str | None,
    country: str | None,
    device: str | None,
    source_brand: str | None,
    user_id: str | None,
    value_cents: int,
    view_through: bool = False,
) -> None:
    deltas: dict[str, int] = {
        "conversions": 1,
        "revenue_cents": int(max(0, value_cents or 0)),
    }
    if view_through:
        deltas["view_through_conversions"] = 1
    await write_event(
        r,
        brand_id=brand_id,
        dim_values={
            "campaign_id": campaign_id,
            "ad_id": ad_id,
            "placement": placement,
            "country": country,
            "device": device,
            "source_brand": source_brand,
        },
        metric_deltas=deltas,
        user_id=user_id,
        device_fingerprint=None,
    )
    if user_id:
        await mark_cohort_touch(r, brand_id, user_id)


async def record_attributed_value(
    r: aioredis.Redis,
    *,
    brand_id: str,
    campaign_id: str | None,
    source_brand: str | None,
    user_id: str | None,
    attributed_value_cents: float,
    weight: float,
) -> None:
    """Multi-touch attributed value -- separate from record_conversion.

    Rounded to int cents for the cube (cumulative drift is sub-cent).
    """
    val_int = int(max(0, round(attributed_value_cents)))
    deltas: dict[str, int] = {"attributed_conversions": 1}
    if val_int > 0:
        deltas["attributed_value_cents"] = val_int
    await write_event(
        r,
        brand_id=brand_id,
        dim_values={
            "campaign_id": campaign_id,
            "source_brand": source_brand,
        },
        metric_deltas=deltas,
        user_id=user_id,
        device_fingerprint=None,
    )


async def record_engagement(
    r: aioredis.Redis,
    *,
    brand_id: str,
    campaign_id: str | None,
    placement: str | None,
    country: str | None,
    device: str | None,
    user_id: str | None,
    completion: float | None = None,
) -> None:
    deltas: dict[str, int] = {"engagements": 1}
    if completion is not None:
        deltas["completion_inst"] = completion  # type: ignore[assignment]
    await write_event(
        r,
        brand_id=brand_id,
        dim_values={
            "campaign_id": campaign_id,
            "placement": placement,
            "country": country,
            "device": device,
        },
        metric_deltas=deltas,
        user_id=user_id,
        device_fingerprint=None,
    )


async def record_spend(
    r: aioredis.Redis,
    *,
    brand_id: str,
    campaign_id: str | None,
    placement: str | None,
    country: str | None,
    device: str | None,
    spend_cents: int,
) -> None:
    if spend_cents <= 0 or not brand_id:
        return
    await write_event(
        r,
        brand_id=brand_id,
        dim_values={
            "campaign_id": campaign_id,
            "placement": placement,
            "country": country,
            "device": device,
        },
        metric_deltas={"spend_cents": int(spend_cents)},
    )


async def record_voucher_event(
    r: aioredis.Redis,
    *,
    brand_id: str,
    campaign_id: str | None,
    user_id: str | None,
    issued: int = 0,
    redeemed: int = 0,
) -> None:
    deltas: dict[str, int] = {}
    if issued > 0:
        deltas["vouchers_issued"] = issued
    if redeemed > 0:
        deltas["vouchers_redeemed"] = redeemed
    if not deltas:
        return
    await write_event(
        r,
        brand_id=brand_id,
        dim_values={"campaign_id": campaign_id},
        metric_deltas=deltas,
        user_id=user_id,
        device_fingerprint=None,
    )


async def record_subscription_event(
    r: aioredis.Redis,
    *,
    brand_id: str,
    user_id: str | None = None,
    trials: int = 0,
    subs: int = 0,
    renewals: int = 0,
    churns: int = 0,
) -> None:
    deltas: dict[str, int] = {}
    if trials:
        deltas["trial_starts"] = trials
    if subs:
        deltas["subscriptions"] = subs
    if renewals:
        deltas["renewals"] = renewals
    if churns:
        deltas["churns"] = churns
    if not deltas:
        return
    await write_event(
        r,
        brand_id=brand_id,
        dim_values={},
        metric_deltas=deltas,
        user_id=user_id,
    )


async def record_user_event(
    r: aioredis.Redis,
    *,
    brand_id: str,
    user_id: str | None,
    new_user: bool = False,
    returning: bool = False,
    store_visit: bool = False,
) -> None:
    deltas: dict[str, int] = {}
    if new_user:
        deltas["new_users"] = 1
    if returning:
        deltas["returning_users"] = 1
    if store_visit:
        deltas["store_visits"] = 1
    if not deltas:
        return
    await write_event(
        r,
        brand_id=brand_id,
        dim_values={},
        metric_deltas=deltas,
        user_id=user_id,
    )
    if user_id:
        await mark_cohort_touch(r, brand_id, user_id)
