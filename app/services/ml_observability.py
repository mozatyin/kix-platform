"""ML & Network-Effect production observability.

This service is the operational telemetry layer that lets us *verify*
Bible claims in real traffic:

* Smart Bidding — LightGBM models are claimed to drive auction CTR /
  CVR predictions. We need accuracy, precision, recall, AUC, calibration
  and drift against rolling baselines.
* Network Effect — invite-tree K-factor must stay in the productive
  band (target K > 0.3, alert if K > 1.5 per existing explosion fix).
* Attribution — every ML prediction is reference-able by an
  ``audit_event_id`` so a post-incident review can reconstruct the full
  decision chain.

Design constraints
------------------
* **< 1 ms overhead per tracked event** — we use Redis sorted sets
  (``ZADD`` with score=timestamp) keyed by date so the hot path never
  blocks on PG, and we never await an audit-log write on the critical
  path (the caller passes in an already-minted ``audit_event_id``).
* **No new heavy dependencies** — z-score anomaly detection is hand-rolled
  with stdlib ``statistics``; metric aggregation uses pure-Python loops
  capped at the rolling window size.
* **Brand-scoped tenancy** — viral keys all include ``brand_id`` in the
  key template, never cross-aggregated by default.

Key schema
----------
``ml:predictions:{model_name}:{YYYYMMDD}``
    Sorted set of JSON prediction records, score = epoch seconds. Each
    member is a JSON blob ``{ts, features_hash, prediction, actual,
    audit_event_id}``. Used by ``compute_ml_metrics``.

``ml:baseline:{model_name}:{metric}``
    HASH of ``{date -> value}`` storing the rolling per-day metric so
    anomaly detection can z-score against the trailing 14 d baseline.

``viral:events:{brand_id}:{YYYYMMDD}``
    Sorted set of viral events, score = epoch seconds. Members are JSON
    ``{ts, event_type, user_id, inviter_id?}``.

``viral:inviters:{brand_id}:{YYYYMMDD}``
    Set of inviter user ids (for K-factor denominator).

``viral:redemptions:{brand_id}:{YYYYMMDD}``
    Counter of redemptions (K-factor numerator).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

PREDICTION_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days
VIRAL_TTL_SECONDS = 60 * 24 * 60 * 60  # 60 days — covers 30 d K-factor window
BASELINE_TTL_SECONDS = 90 * 24 * 60 * 60  # 90 days

# Anomaly severity thresholds (absolute z-score against trailing baseline).
ANOMALY_Z_WARN = 2.0
ANOMALY_Z_CRIT = 3.5

# Viral event taxonomy. Free-form strings are accepted (forward compat)
# but these are the canonical names compute_kfactor_realtime() looks for.
EVT_INVITE_ISSUED = "invite_issued"
EVT_INVITE_REDEEMED = "invite_redeemed"
EVT_FRIEND_JOINED = "friend_joined"
EVT_SHARE_CLICKED = "share_clicked"

# K-factor explosion threshold — per existing inheritance-depth fix in
# routers/network_effect.py. Above this, alert ops; viral loop is in
# runaway territory and the depth cap should already be biting.
KFACTOR_EXPLOSION_THRESHOLD = 1.5
KFACTOR_PRODUCTIVE_FLOOR = 0.3


# ── Internal helpers ──────────────────────────────────────────────────────


def _today_str(ts: float | None = None) -> str:
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")


def _date_range(from_ts: float, to_ts: float) -> list[str]:
    """Inclusive list of YYYYMMDD bucket keys covering [from_ts, to_ts]."""
    if to_ts < from_ts:
        from_ts, to_ts = to_ts, from_ts
    start = datetime.fromtimestamp(from_ts, tz=timezone.utc).date()
    end = datetime.fromtimestamp(to_ts, tz=timezone.utc).date()
    out: list[str] = []
    d = start
    while d <= end:
        out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out


def _features_hash(features: Mapping[str, Any]) -> str:
    """Stable, short fingerprint of a feature dict — for grouping repeat
    queries, not for security. SHA-256 truncated to 16 hex chars."""
    try:
        canonical = json.dumps(features, sort_keys=True, default=str)
    except Exception:
        canonical = repr(features)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _pred_key(model_name: str, day: str) -> str:
    return f"ml:predictions:{model_name}:{day}"


def _baseline_key(model_name: str, metric: str) -> str:
    return f"ml:baseline:{model_name}:{metric}"


def _viral_events_key(brand_id: str, day: str) -> str:
    return f"viral:events:{brand_id}:{day}"


def _viral_inviters_key(brand_id: str, day: str) -> str:
    return f"viral:inviters:{brand_id}:{day}"


def _viral_redemptions_key(brand_id: str, day: str) -> str:
    return f"viral:redemptions:{brand_id}:{day}"


# ── ML prediction tracking ────────────────────────────────────────────────


async def track_ml_prediction(
    model_name: str,
    features: Mapping[str, Any],
    prediction: float,
    actual: bool | float | None = None,
    *,
    audit_event_id: str | None = None,
    ts: float | None = None,
) -> str:
    """Persist a single Smart-Bidding (or other) ML prediction.

    Returns a short ``prediction_id`` you can later use to attach an
    ``actual`` outcome via :func:`attach_actual_outcome`. The hot path
    is a single ZADD — < 1 ms in steady state.

    Parameters
    ----------
    model_name
        e.g. ``"smart_bidding_ctr"``. Models are namespaced by name —
        callers should keep the name stable across deploys.
    features
        Input feature dict at prediction time. Hashed only (never stored
        raw) so PII can't leak into the telemetry surface.
    prediction
        Model output, typically a probability in [0, 1].
    actual
        Optional ground-truth label — pass ``True``/``False`` once the
        auction outcome is known, or call :func:`attach_actual_outcome`
        later.
    audit_event_id
        If the caller minted an audit-log row for this decision, pass
        the event_id here so post-incident review can reconstruct the
        full chain.
    """
    r = await get_redis()
    now = ts if ts is not None else time.time()
    day = _today_str(now)
    fh = _features_hash(features)
    pred_id = f"{int(now * 1000)}:{fh}"

    record = {
        "id": pred_id,
        "ts": now,
        "features_hash": fh,
        "prediction": float(prediction),
        "actual": None if actual is None else (1.0 if bool(actual) else 0.0)
        if isinstance(actual, bool)
        else float(actual),
        "audit_event_id": audit_event_id,
    }
    key = _pred_key(model_name, day)
    pipe = r.pipeline()
    pipe.zadd(key, {json.dumps(record): now})
    pipe.expire(key, PREDICTION_TTL_SECONDS)
    await pipe.execute()
    return pred_id


async def attach_actual_outcome(
    model_name: str,
    prediction_id: str,
    actual: bool | float,
    *,
    ts: float | None = None,
) -> bool:
    """Late-binding update for a previously-tracked prediction.

    Auction outcomes arrive seconds to minutes after the bid. We scan
    today + yesterday for the matching ``prediction_id`` (the id encodes
    the millisecond timestamp so the search space is tiny).
    """
    r = await get_redis()
    now = ts if ts is not None else time.time()
    # Search today and yesterday — covers any reasonable feedback lag.
    days = [_today_str(now), _today_str(now - 86400)]
    for day in days:
        key = _pred_key(model_name, day)
        members = await r.zrange(key, 0, -1, withscores=True)
        for raw, score in members:
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            if rec.get("id") != prediction_id:
                continue
            rec["actual"] = 1.0 if (isinstance(actual, bool) and actual) else (
                0.0 if isinstance(actual, bool) else float(actual)
            )
            rec["actual_ts"] = now
            pipe = r.pipeline()
            pipe.zrem(key, raw)
            pipe.zadd(key, {json.dumps(rec): score})
            await pipe.execute()
            return True
    return False


async def _load_predictions(
    model_name: str, from_ts: float, to_ts: float
) -> list[dict[str, Any]]:
    r = await get_redis()
    days = _date_range(from_ts, to_ts)
    out: list[dict[str, Any]] = []
    for day in days:
        key = _pred_key(model_name, day)
        members = await r.zrangebyscore(key, from_ts, to_ts)
        for raw in members:
            try:
                out.append(json.loads(raw))
            except Exception:
                continue
    return out


def _classification_metrics(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Compute accuracy/precision/recall/AUC/calibration for binary preds.

    Records without ``actual`` are excluded. AUC uses the trapezoidal
    rule over the sorted-by-prediction ROC curve. Calibration is the
    mean absolute gap between predicted probability and observed rate
    inside 10 equal-width bins (lower is better).
    """
    rows = [
        (float(r["prediction"]), float(r["actual"]))
        for r in records
        if r.get("actual") is not None and r.get("prediction") is not None
    ]
    n_total = len(rows)
    if n_total == 0:
        return {
            "n_with_label": 0,
            "accuracy": None,
            "precision": None,
            "recall": None,
            "auc": None,
            "calibration_mae": None,
        }

    tp = fp = tn = fn = 0
    for p, y in rows:
        pred_class = 1 if p >= 0.5 else 0
        if pred_class == 1 and y >= 0.5:
            tp += 1
        elif pred_class == 1:
            fp += 1
        elif y >= 0.5:
            fn += 1
        else:
            tn += 1

    accuracy = (tp + tn) / n_total if n_total else None
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None

    # AUC via Mann-Whitney U on label-stratified scores.
    pos = [p for p, y in rows if y >= 0.5]
    neg = [p for p, y in rows if y < 0.5]
    if pos and neg:
        wins = 0.0
        for ps in pos:
            for ns in neg:
                if ps > ns:
                    wins += 1.0
                elif ps == ns:
                    wins += 0.5
        auc = wins / (len(pos) * len(neg))
    else:
        auc = None

    # Calibration MAE over 10 bins.
    bins: list[tuple[list[float], list[float]]] = [([], []) for _ in range(10)]
    for p, y in rows:
        idx = min(int(p * 10), 9)
        bins[idx][0].append(p)
        bins[idx][1].append(y)
    gaps: list[float] = []
    for preds, ys in bins:
        if not preds:
            continue
        gaps.append(abs(statistics.fmean(preds) - statistics.fmean(ys)))
    calibration_mae = statistics.fmean(gaps) if gaps else None

    return {
        "n_with_label": n_total,
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "auc": auc,
        "calibration_mae": calibration_mae,
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
    }


def _drift_score(
    current: Iterable[float], baseline: Iterable[float]
) -> float | None:
    """Population Stability Index-style drift score, capped to [0, 1].

    Simple mean-shift / std-shift composite. ``None`` if either sample
    is too small to compare meaningfully.
    """
    cur = [float(x) for x in current]
    base = [float(x) for x in baseline]
    if len(cur) < 5 or len(base) < 5:
        return None
    cur_mean, base_mean = statistics.fmean(cur), statistics.fmean(base)
    cur_std = statistics.pstdev(cur) or 1e-9
    base_std = statistics.pstdev(base) or 1e-9
    mean_shift = abs(cur_mean - base_mean) / max(base_std, 1e-9)
    std_ratio = abs(math.log(cur_std / base_std))
    score = (mean_shift + std_ratio) / 4.0  # rough scaling
    return min(1.0, score)


async def compute_ml_metrics(
    model_name: str, from_ts: float, to_ts: float
) -> dict[str, Any]:
    """Aggregate accuracy / precision / recall / AUC / calibration / drift.

    Drift is computed by comparing the prediction distribution in the
    requested window against the prior window of equal length.
    """
    records = await _load_predictions(model_name, from_ts, to_ts)
    metrics = _classification_metrics(records)
    metrics["n_total"] = len(records)
    metrics["from_ts"] = from_ts
    metrics["to_ts"] = to_ts
    metrics["model_name"] = model_name

    # Drift: this window vs equal-length prior window.
    span = to_ts - from_ts
    if span > 0:
        prior = await _load_predictions(model_name, from_ts - span, from_ts)
        cur_preds = [r["prediction"] for r in records if r.get("prediction") is not None]
        prior_preds = [r["prediction"] for r in prior if r.get("prediction") is not None]
        metrics["drift_score"] = _drift_score(cur_preds, prior_preds)
    else:
        metrics["drift_score"] = None

    return metrics


# ── Viral / K-factor tracking ─────────────────────────────────────────────


async def track_viral_event(
    brand_id: str,
    event_type: str,
    user_id: str,
    *,
    inviter_id: str | None = None,
    ts: float | None = None,
) -> None:
    """Record one viral event for the rolling K-factor calculation.

    ``event_type`` is free-form but the canonical taxonomy is:
    ``invite_issued`` (denominator), ``invite_redeemed`` (numerator),
    ``friend_joined``, ``share_clicked``.
    """
    r = await get_redis()
    now = ts if ts is not None else time.time()
    day = _today_str(now)

    payload = {
        "ts": now,
        "event_type": event_type,
        "user_id": user_id,
        "inviter_id": inviter_id,
    }
    events_key = _viral_events_key(brand_id, day)
    pipe = r.pipeline()
    pipe.zadd(events_key, {json.dumps(payload): now})
    pipe.expire(events_key, VIRAL_TTL_SECONDS)

    if event_type == EVT_INVITE_ISSUED:
        inv_key = _viral_inviters_key(brand_id, day)
        pipe.sadd(inv_key, user_id)
        pipe.expire(inv_key, VIRAL_TTL_SECONDS)
    elif event_type == EVT_INVITE_REDEEMED:
        red_key = _viral_redemptions_key(brand_id, day)
        pipe.incr(red_key)
        pipe.expire(red_key, VIRAL_TTL_SECONDS)

    await pipe.execute()


async def compute_kfactor_realtime(
    brand_id: str, window_days: int = 7
) -> float:
    """Rolling viral coefficient = redemptions / unique_inviters.

    Returns ``0.0`` when there are no inviters in the window (avoids
    division-by-zero and keeps dashboards numeric).
    """
    r = await get_redis()
    now = time.time()
    inviter_keys: list[str] = []
    redemption_total = 0
    for offset in range(window_days):
        ts = now - offset * 86400
        day = _today_str(ts)
        inviter_keys.append(_viral_inviters_key(brand_id, day))
        red_raw = await r.get(_viral_redemptions_key(brand_id, day))
        if red_raw is not None:
            try:
                redemption_total += int(red_raw)
            except (TypeError, ValueError):
                continue

    if not inviter_keys:
        return 0.0
    # SUNION on the inviter sets is O(N) in total members — fine at our
    # scale (one user per inviter per day).
    unique_inviters = await r.sunion(*inviter_keys)
    n_inviters = len(unique_inviters) if unique_inviters else 0
    if n_inviters == 0:
        return 0.0
    return redemption_total / n_inviters


async def kfactor_trailing(
    brand_id: str, *, windows: tuple[int, ...] = (1, 7, 30)
) -> dict[str, float]:
    """Convenience: returns ``{"1d": ..., "7d": ..., "30d": ...}``."""
    out: dict[str, float] = {}
    for w in windows:
        out[f"{w}d"] = await compute_kfactor_realtime(brand_id, window_days=w)
    return out


# ── Anomaly detection ─────────────────────────────────────────────────────


async def _record_baseline(
    model_name: str, metric: str, value: float, *, ts: float | None = None
) -> None:
    r = await get_redis()
    now = ts if ts is not None else time.time()
    day = _today_str(now)
    key = _baseline_key(model_name, metric)
    await r.hset(key, day, str(value))
    await r.expire(key, BASELINE_TTL_SECONDS)


async def _load_baseline(
    model_name: str, metric: str, *, days: int = 14
) -> list[float]:
    r = await get_redis()
    key = _baseline_key(model_name, metric)
    raw = await r.hgetall(key)
    # raw is {date_bytes: value_bytes} or {date_str: value_str} depending
    # on the redis client's decode_responses setting.
    items: list[tuple[str, float]] = []
    for k, v in raw.items():
        ks = k.decode() if isinstance(k, bytes) else k
        vs = v.decode() if isinstance(v, bytes) else v
        try:
            items.append((ks, float(vs)))
        except ValueError:
            continue
    items.sort(key=lambda kv: kv[0])
    return [v for _, v in items[-days:]]


async def detect_anomaly(
    metric_name: str,
    value: float,
    *,
    baseline_window_days: int = 14,
    model_name: str = "_global",
    record: bool = True,
) -> dict[str, Any]:
    """Simple z-score anomaly check against a trailing baseline.

    Returns ``{"is_anomaly": bool, "severity": "ok"|"warn"|"critical",
    "z": float|None, "baseline_n": int}``. When fewer than 3 baseline
    samples are present we return ``severity="ok"`` and ``is_anomaly=False``
    — the system is in cold-start, not anomalous.
    """
    baseline = await _load_baseline(
        model_name, metric_name, days=baseline_window_days
    )
    if record:
        await _record_baseline(model_name, metric_name, value)

    if len(baseline) < 3:
        return {
            "metric": metric_name,
            "value": value,
            "is_anomaly": False,
            "severity": "ok",
            "z": None,
            "baseline_n": len(baseline),
            "reason": "insufficient_baseline",
        }

    mean = statistics.fmean(baseline)
    std = statistics.pstdev(baseline) or 1e-9
    z = (value - mean) / std
    abs_z = abs(z)
    if abs_z >= ANOMALY_Z_CRIT:
        severity = "critical"
    elif abs_z >= ANOMALY_Z_WARN:
        severity = "warn"
    else:
        severity = "ok"
    return {
        "metric": metric_name,
        "value": value,
        "is_anomaly": severity != "ok",
        "severity": severity,
        "z": z,
        "baseline_n": len(baseline),
        "baseline_mean": mean,
        "baseline_std": std,
    }


async def recent_anomalies(
    *, model_name: str = "_global", limit: int = 50
) -> list[dict[str, Any]]:
    """Replay the trailing baseline and surface days that breached the
    z-score warning band. Pure read — no side effects."""
    r = await get_redis()
    out: list[dict[str, Any]] = []
    # Scan for baseline keys under the model namespace.
    pattern = f"ml:baseline:{model_name}:*"
    cursor = 0
    metrics_seen: list[str] = []
    while True:
        cursor, keys = await r.scan(cursor=cursor, match=pattern, count=100)
        for k in keys:
            ks = k.decode() if isinstance(k, bytes) else k
            metrics_seen.append(ks.split(":")[-1])
        if cursor == 0:
            break

    for metric in metrics_seen:
        series = await _load_baseline(model_name, metric, days=30)
        if len(series) < 4:
            continue
        # z-score the latest sample vs the rest.
        head, tail = series[:-1], series[-1]
        mean = statistics.fmean(head)
        std = statistics.pstdev(head) or 1e-9
        z = (tail - mean) / std
        if abs(z) >= ANOMALY_Z_WARN:
            out.append(
                {
                    "metric": metric,
                    "model_name": model_name,
                    "value": tail,
                    "z": z,
                    "severity": "critical" if abs(z) >= ANOMALY_Z_CRIT else "warn",
                }
            )
    return out[:limit]


# ── Aggregate dashboard payload ───────────────────────────────────────────


async def dashboard_snapshot(
    *,
    brand_ids: list[str] | None = None,
    model_names: list[str] | None = None,
) -> dict[str, Any]:
    """Aggregate everything an ops dashboard needs in one round trip.

    Safe to call from a 30 s polling loop — every component is bounded
    and returns ``None`` rather than raising on missing data.
    """
    now = time.time()
    week_ago = now - 7 * 86400

    model_names = model_names or ["smart_bidding_ctr", "smart_bidding_cvr"]
    models_block: dict[str, Any] = {}
    for m in model_names:
        try:
            models_block[m] = await compute_ml_metrics(m, week_ago, now)
        except Exception as exc:  # pragma: no cover
            logger.warning("compute_ml_metrics(%s) failed: %s", m, exc)
            models_block[m] = {"error": str(exc)}

    brand_ids = brand_ids or []
    viral_block: dict[str, Any] = {}
    for b in brand_ids:
        try:
            viral_block[b] = {
                "kfactor": await kfactor_trailing(b),
                "explosion_warning": (
                    await compute_kfactor_realtime(b, window_days=1)
                )
                > KFACTOR_EXPLOSION_THRESHOLD,
            }
        except Exception as exc:  # pragma: no cover
            logger.warning("kfactor(%s) failed: %s", b, exc)
            viral_block[b] = {"error": str(exc)}

    return {
        "generated_at": now,
        "models": models_block,
        "viral": viral_block,
        "thresholds": {
            "kfactor_explosion": KFACTOR_EXPLOSION_THRESHOLD,
            "kfactor_productive_floor": KFACTOR_PRODUCTIVE_FLOOR,
            "anomaly_z_warn": ANOMALY_Z_WARN,
            "anomaly_z_critical": ANOMALY_Z_CRIT,
        },
    }


# ── Daily metric recomputation job ────────────────────────────────────────


async def run_daily_metric_job(
    model_names: Iterable[str],
    *,
    ts: float | None = None,
) -> dict[str, Any]:
    """Idempotent daily roll-up — recomputes yesterday's metrics and
    pushes them into the baseline hashes. Safe to re-run any number of
    times for the same day (we overwrite, we don't accumulate).
    """
    now = ts if ts is not None else time.time()
    end = now - (now % 86400)  # midnight UTC today
    start = end - 86400  # midnight UTC yesterday
    out: dict[str, Any] = {"date": _today_str(start), "models": {}}
    for model_name in model_names:
        metrics = await compute_ml_metrics(model_name, start, end)
        for metric_key in ("accuracy", "auc", "calibration_mae", "drift_score"):
            v = metrics.get(metric_key)
            if v is None:
                continue
            await _record_baseline(model_name, metric_key, float(v), ts=start)
        out["models"][model_name] = metrics
    return out


# ── Audit chain reconstruction ────────────────────────────────────────────


async def reconstruct_decision_chain(
    model_name: str, prediction_id: str
) -> dict[str, Any] | None:
    """Look up one prediction by id and return the record plus the
    referenced audit_event_id (caller resolves the audit row itself).
    Returns ``None`` if the prediction isn't found in the last 30 days."""
    r = await get_redis()
    now = time.time()
    for offset in range(31):
        day = _today_str(now - offset * 86400)
        key = _pred_key(model_name, day)
        members = await r.zrange(key, 0, -1)
        for raw in members:
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            if rec.get("id") == prediction_id:
                return {
                    "model_name": model_name,
                    "prediction": rec,
                    "audit_event_id": rec.get("audit_event_id"),
                    "located_in_day": day,
                }
    return None


# ── Infra health probe (cheap) ────────────────────────────────────────────


async def observability_health() -> dict[str, Any]:
    """Return a small dict describing whether the telemetry infra is up.

    Probes Redis with a ``PING`` and reports the count of prediction keys
    and viral keys currently present. Designed to be < 5 ms even on
    large keyspaces (uses SCAN with a tight cap).
    """
    started = time.perf_counter()
    status: dict[str, Any] = {
        "redis": "unknown",
        "prediction_keys": 0,
        "viral_keys": 0,
    }
    try:
        r = await get_redis()
        pong = await r.ping()
        status["redis"] = "ok" if pong else "error"
        # Bounded SCAN — stop after 500 keys to keep the probe cheap.
        for pattern, field in (
            ("ml:predictions:*", "prediction_keys"),
            ("viral:events:*", "viral_keys"),
        ):
            cursor = 0
            count = 0
            iters = 0
            while iters < 5:
                cursor, keys = await r.scan(
                    cursor=cursor, match=pattern, count=100
                )
                count += len(keys)
                iters += 1
                if cursor == 0:
                    break
            status[field] = count
    except Exception as exc:
        status["redis"] = "error"
        status["error"] = str(exc)
    status["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return status
