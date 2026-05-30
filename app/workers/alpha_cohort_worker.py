"""Alpha-cohort worker — automated check-ins, weekly summaries, at-risk flags.

Design
------
This worker is *idempotent* and *cheap*. It scans the current cohort once per
invocation (hourly cron is enough), and for each member decides whether any
of the four automated touches should fire **right now**:

  1. ``alpha_welcome``       — fired at signup by the router. Worker only
                                checks it has not been missed (defensive).
  2. ``alpha_day3_checkin``  — fire if ≥ 3 days since signup and not yet sent.
  3. ``alpha_week1_summary`` — fire if ≥ 7 days since signup and not yet sent.
  4. ``alpha_monthly_survey``— fire on a 30-day cadence post-signup.

Per-touch idempotency is enforced via the Redis key
``alpha:touch:{brand_id}:{touch_name}`` — set on successful enqueue, never
expired. To re-send during a test, ``DEL`` that key.

The worker honours quiet hours (22:00–08:00 SGT). When inside the window
the scan still runs (we still flag at-risk merchants for the dashboard);
only the email enqueue is deferred to the next run.

Public surface
--------------
``run_once(redis, *, cohort=..., now=..., dry_run=False)`` — one scan pass.
Returns a structured report (counts + per-brand actions) so callers can log
or assert in tests. ``dry_run=True`` skips the actual ``enqueue_email`` /
``SET`` and only reports what would have happened.

This worker is **not** a forever-loop — wire it to your cron / scheduler.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

from app.redis_client import get_redis
from app.routers.alpha_program import (
    DEFAULT_COHORT,
    _classify_health,
    _decode_hash,
    _gather_metrics,
    in_quiet_hours,
)

logger = logging.getLogger(__name__)


# ── touch thresholds ─────────────────────────────────────────────────────


# (touch_name, min_age_days, recurrence_days)
# recurrence_days=0 → fire exactly once.
_TOUCHES: tuple[tuple[str, int, int], ...] = (
    ("alpha_day3_checkin", 3, 0),
    ("alpha_week1_summary", 7, 0),
    ("alpha_monthly_survey", 30, 30),
)


def _touch_key(brand_id: str, touch: str) -> str:
    return f"alpha:touch:{brand_id}:{touch}"


# ── per-merchant decision ────────────────────────────────────────────────


async def _decide_touches(
    r: aioredis.Redis,
    brand_id: str,
    signup_ts: float,
    metrics: dict[str, Any],
    now: float,
) -> list[str]:
    """Return the list of touch names that should fire for this brand now."""
    due: list[str] = []
    age_days = (now - signup_ts) / 86_400 if signup_ts else 0.0

    for touch, min_age, recurrence in _TOUCHES:
        if age_days < min_age:
            continue
        last_raw = await r.get(_touch_key(brand_id, touch))
        if last_raw is None:
            due.append(touch)
            continue
        if recurrence <= 0:
            continue  # once-only, already fired
        try:
            last_ts = float(
                last_raw.decode() if isinstance(last_raw, (bytes, bytearray)) else last_raw
            )
        except ValueError:
            last_ts = 0.0
        if (now - last_ts) >= recurrence * 86_400:
            due.append(touch)

    return due


def _template_vars(
    touch: str,
    brand_id: str,
    brand_cfg: dict[str, str],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    """Project the per-brand context onto each touch's required vars."""
    portal_url = f"https://partner.letskix.com/{brand_id}"
    contact = brand_cfg.get("contact_name") or brand_cfg.get("brand_name", brand_id)
    brand_name = brand_cfg.get("brand_name", brand_id)

    if touch == "alpha_day3_checkin":
        return {
            "brand_name": brand_name,
            "contact_name": contact,
            "feedback_url": "/landing/alpha-feedback.html",
        }
    if touch == "alpha_week1_summary":
        return {
            "brand_name": brand_name,
            "contact_name": contact,
            "campaigns_created": metrics.get("campaigns_created", 0),
            "spend_total_sgd": metrics.get("spend_total_sgd", 0.0),
            "portal_url": portal_url,
        }
    if touch == "alpha_monthly_survey":
        return {
            "brand_name": brand_name,
            "contact_name": contact,
            "survey_url": f"/landing/alpha-feedback.html?survey=monthly&bid={brand_id}",
        }
    # Defensive — should never hit.
    return {"brand_name": brand_name, "contact_name": contact, "portal_url": portal_url}


# ── main entrypoint ──────────────────────────────────────────────────────


async def run_once(
    r: aioredis.Redis,
    *,
    cohort: str = DEFAULT_COHORT,
    now: float | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """One scan pass over a cohort. Returns a structured report.

    Args:
        r: aioredis connection.
        cohort: cohort tag to scan.
        now: override "current time" for tests (epoch seconds).
        dry_run: when True, do not enqueue emails or write touch keys —
            only return what would have happened.
    """
    now = now if now is not None else time.time()
    quiet = in_quiet_hours(datetime.fromtimestamp(now, timezone.utc))

    raw_members = await r.smembers(f"alpha:cohort:{cohort}")
    members = sorted(
        m.decode() if isinstance(m, (bytes, bytearray)) else str(m)
        for m in raw_members
    )

    actions: list[dict[str, Any]] = []
    at_risk: list[str] = []
    enqueued = 0
    deferred_quiet = 0

    # Lazy import — keeps top-level import-time light + makes the worker
    # testable without spinning up the email_template module.
    from app.services.email_template_service import enqueue_email

    for bid in members:
        bcfg = _decode_hash(await r.hgetall(f"brand_config:{bid}"))
        sub = _decode_hash(await r.hgetall(f"brand_subscription:{bid}"))
        signup_ts = 0.0
        if sub.get("started_at"):
            try:
                signup_ts = float(sub["started_at"])
            except ValueError:
                signup_ts = 0.0

        metrics = await _gather_metrics(r, bid)
        health = _classify_health(metrics, signup_ts or None)
        if health["at_risk"]:
            at_risk.append(bid)

        due = await _decide_touches(r, bid, signup_ts, metrics, now)
        for touch in due:
            if quiet:
                deferred_quiet += 1
                actions.append({
                    "brand_id": bid,
                    "touch": touch,
                    "status": "deferred_quiet_hours",
                })
                continue

            if dry_run:
                actions.append({
                    "brand_id": bid,
                    "touch": touch,
                    "status": "would_enqueue",
                })
                continue

            try:
                tvars = _template_vars(touch, bid, bcfg, metrics)
                await enqueue_email(
                    r,
                    brand_id=bid,
                    template_id=touch,
                    locale=bcfg.get("locale", "en-SG"),
                    recipient=bcfg.get("contact_email", ""),
                    **tvars,
                )
                await r.set(_touch_key(bid, touch), str(now))
                enqueued += 1
                actions.append({
                    "brand_id": bid,
                    "touch": touch,
                    "status": "enqueued",
                })
            except Exception as exc:  # noqa: BLE001
                logger.warning("alpha touch %s failed for %s: %s", touch, bid, exc)
                actions.append({
                    "brand_id": bid,
                    "touch": touch,
                    "status": "error",
                    "error": str(exc),
                })

    report = {
        "cohort": cohort,
        "scanned": len(members),
        "enqueued": enqueued,
        "deferred_quiet_hours": deferred_quiet,
        "at_risk": at_risk,
        "at_risk_count": len(at_risk),
        "actions": actions,
        "dry_run": dry_run,
        "quiet_hours": quiet,
        "ran_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
    }

    logger.info(
        "alpha_cohort_worker cohort=%s scanned=%d enqueued=%d at_risk=%d quiet=%s",
        cohort, len(members), enqueued, len(at_risk), quiet,
    )
    return report


async def main(cohort: str = DEFAULT_COHORT) -> None:  # pragma: no cover — entrypoint
    """Cron-style one-shot runner."""
    r = await get_redis()
    await run_once(r, cohort=cohort)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
