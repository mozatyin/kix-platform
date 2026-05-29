"""Training data pipeline — replay events out of Redis.

The KiX auction emits structured events for every impression, click,
and conversion. This module joins those streams into a labeled
``(features, label)`` matrix suitable for offline LightGBM training.

For QualityScore + Relevance: the binary label is ``1`` if the
impression turned into a conversion within ``conversion_window_hours``,
else ``0``.

For SmartBid: the regression label is ``revenue_cents / impression_count``
for a campaign over the period (i.e. how much was each impression
worth) — that gives the model a "value-per-impression" target which it
can multiply through to a per-impression bid.

Data sources (Redis):

  * ``auction:events:impressions``  STREAM  one entry per served impression
  * ``auction:events:conversions``  STREAM  one entry per conversion
  * ``campaign:hash:{cid}``         HASH    static campaign metadata
  * ``kid:profile:{kid}``           HASH    user profile

When a stream is empty or the keys are missing (typical for a freshly
booted dev instance), the helpers return empty matrices rather than
raising — the trainer will surface a clear "not enough data" error.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.ml.features import features_to_vector, FEATURES

logger = logging.getLogger(__name__)

IMPRESSION_STREAM = "auction:events:impressions"
CONVERSION_STREAM = "auction:events:conversions"


async def _read_stream(
    r: Any, key: str, since_ms: int, max_entries: int = 100_000,
) -> list[dict[str, Any]]:
    """Read XRANGE from ``since_ms`` → now. Tolerant of missing streams."""
    if r is None:
        return []
    try:
        # XRANGE since_ms+ → max-id
        entries = await r.xrange(key, min=f"{since_ms}-0", max="+", count=max_entries)
    except Exception as exc:  # noqa: BLE001
        logger.warning("data: xrange %s failed: %s", key, exc)
        return []
    out: list[dict[str, Any]] = []
    for entry_id, fields in entries:
        try:
            ts_ms = int(str(entry_id).split("-", 1)[0])
        except (TypeError, ValueError):
            ts_ms = since_ms
        row = dict(fields)
        row["_event_ts"] = ts_ms / 1000.0
        out.append(row)
    return out


async def _campaign_hash(r: Any, cid: str) -> dict[str, Any]:
    if not r or not cid:
        return {}
    try:
        return await r.hgetall(f"campaign:hash:{cid}") or {}
    except Exception:
        return {}


async def _user_profile(r: Any, kid: str) -> dict[str, Any]:
    if not r or not kid:
        return {}
    try:
        return await r.hgetall(f"kid:profile:{kid}") or {}
    except Exception:
        return {}


async def build_training_set(
    r: Any,
    period_days: int = 30,
    label: str = "conversion",
    conversion_window_hours: int = 24,
    max_rows: int = 50_000,
) -> tuple[list[list[float]], list[float]]:
    """Return ``(X, y)`` for the requested label.

    ``label`` ∈ ``{"conversion", "click", "revenue"}``:

      * ``conversion`` — binary; trains QualityScore.
      * ``click``      — binary; trains RelevanceScore.
      * ``revenue``    — float cents; trains SmartBid.
    """
    now = time.time()
    since_ms = int((now - period_days * 86400) * 1000)

    impressions = await _read_stream(r, IMPRESSION_STREAM, since_ms, max_rows)
    conversions = await _read_stream(r, CONVERSION_STREAM, since_ms, max_rows)

    if not impressions:
        return [], []

    # Index conversions by (campaign_id, kid) for fast join.
    conv_lookup: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for c in conversions:
        key = (str(c.get("campaign_id", "")), str(c.get("kid", "")))
        conv_lookup.setdefault(key, []).append(c)

    X: list[list[float]] = []
    y: list[float] = []
    window_s = conversion_window_hours * 3600.0

    # Small in-loop cache so we don't HGETALL the same campaign 50k times.
    camp_cache: dict[str, dict[str, Any]] = {}
    user_cache: dict[str, dict[str, Any]] = {}

    for imp in impressions:
        cid = str(imp.get("campaign_id", ""))
        kid = str(imp.get("kid", ""))
        imp_ts = float(imp.get("_event_ts") or now)

        if cid not in camp_cache:
            camp_cache[cid] = await _campaign_hash(r, cid)
        if kid not in user_cache:
            user_cache[kid] = await _user_profile(r, kid) if kid else {}

        campaign = {**camp_cache[cid], "campaign_id": cid}
        user = user_cache[kid]
        context = {
            "now": imp_ts,
            "hour_of_day": int(time.localtime(imp_ts).tm_hour),
            "day_of_week": int(time.localtime(imp_ts).tm_wday),
        }

        # Compute the label.
        matched_convs = [
            c for c in conv_lookup.get((cid, kid), [])
            if 0 <= float(c.get("_event_ts", 0)) - imp_ts <= window_s
        ]
        if label == "conversion":
            y_val = 1.0 if matched_convs else 0.0
        elif label == "click":
            y_val = float(imp.get("clicked", 0) or 0)
            if y_val == 0.0 and matched_convs:
                # A conversion implies a click happened.
                y_val = 1.0
        elif label == "revenue":
            y_val = sum(
                float(c.get("revenue_cents", 0) or 0) for c in matched_convs
            )
        else:
            raise ValueError(f"unknown label: {label!r}")

        X.append(features_to_vector(campaign, user, context))
        y.append(y_val)

        if len(X) >= max_rows:
            break

    return X, y


def feature_names() -> list[str]:
    """Re-export — convenience for the trainer."""
    return list(FEATURES)
