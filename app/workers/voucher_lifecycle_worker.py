"""Voucher lifecycle worker — expiration reminders, grace periods, win-back.

~40 % of issued vouchers expire unused (industry standard). Every lost
voucher = lost merchant ROI + lost lock-in moment for the user. This
worker runs hourly to:

  * **Remind** holders that their voucher is about to expire, on a
    cadence tuned to voucher value (small bills get 3 nudges; high-value
    ones get a 4–5-touch sequence and a dedicated WhatsApp reach-out).
  * **Grace-period** vouchers that *just* expired — give $20+ holders an
    extra 24 h, $50+ holders an extra 72 h to come back and redeem
    before the voucher is hard-marked ``expired``.
  * **Win-back** holders who missed even the grace window with a
    "sorry you missed it, here's 50 % credit toward another" offer that
    we track so we can A/B-test the redemption rate.

Smart-timing rules:

  * Singapore quiet hours (22:00–07:00 SGT) — no notifications fire
    during the window; they get pushed to 07:00 the next morning.
  * Frequency cap — we ask :mod:`app.routers.frequency_cap` whether the
    user is currently capped on the ``voucher`` slot before dispatching.
  * A/B variants — every notification kind has 2+ message variants;
    selection is sticky-per-user via a sha-based hash so the same user
    sees the same variant across the whole sequence.

Public surface
--------------
``run_once(redis=None, *, now=None, dry_run=False)``
    One scan pass. Returns a structured report (counts + per-voucher
    actions). ``dry_run=True`` skips writes; ``now`` lets tests pin the
    clock.

This worker is **not** a forever-loop — schedule via cron / k8s CronJob.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────

SGT = ZoneInfo("Asia/Singapore")
QUIET_HOURS_START = 22  # 22:00 SGT
QUIET_HOURS_END = 7  # 07:00 SGT
SCAN_PAGE_SIZE = 500
MAX_USERS_PER_RUN = 5000  # safety cap per hourly run

# Reminder cadence per voucher value tier — values in cents.
# Each entry is (threshold_cents, [reminder_offsets_days], grace_hours).
# A positive ``reminder_offset`` = days BEFORE expiry; a negative = hours
# AFTER expiry (grace-period reminders).
TIER_CADENCE: list[tuple[int, list[int], int]] = [
    # ($100+) — premium: 5 touches + WhatsApp reach-out
    (10_000, [14, 7, 3, 1], 72),
    # ($50+)
    (5_000, [14, 7, 3, 1], 72),
    # ($20+)
    (2_000, [7, 3, 1], 24),
    # ($5+) — minimum tier
    (500, [7, 3, 1], 0),
]
# Below the lowest threshold: no reminders. (Voucher value <$5 isn't
# worth a notification frequency-budget slot.)

# Channel preference resolution order.
DEFAULT_CHANNEL_ORDER = ("push", "whatsapp", "sms", "email")

# A/B variant copy. Each list is the variant pool for that notification
# kind. Trinity ab-testing engine resolves the winner over time.
MESSAGE_VARIANTS: dict[str, list[dict[str, str]]] = {
    "reminder": [
        {
            "title": "Your voucher expires soon",
            "body": "You have ${value} waiting at {brand}. Don't lose it!",
        },
        {
            "title": "${value} on the table",
            "body": "Use your {brand} voucher before {expires_in}.",
        },
    ],
    "grace": [
        {
            "title": "We've extended your voucher",
            "body": (
                "Your ${value} {brand} voucher just expired — we've given "
                "you {grace_hours}h more. Use it now!"
            ),
        },
        {
            "title": "One more chance",
            "body": (
                "Your {brand} voucher expired — but here's a {grace_hours}h "
                "grace window. ${value} is yours if you redeem now."
            ),
        },
    ],
    "winback": [
        {
            "title": "Sorry you missed it",
            "body": (
                "Your ${value} {brand} voucher expired. Here's 50% credit "
                "(${winback_value}) toward another."
            ),
        },
        {
            "title": "Come back for ${winback_value}",
            "body": (
                "We saved you a deal at {brand} — claim 50% credit on the "
                "voucher you missed."
            ),
        },
    ],
}


# ── Redis key helpers (mirror app.routers.vouchers) ──────────────────────

def _k_voucher(vid: str) -> str:
    return f"voucher:{vid}"


def _k_user_vouchers(uid: str) -> str:
    return f"user:{uid}:vouchers"


def _k_reminder_sent(vid: str, slot: str) -> str:
    """One key per (voucher, reminder-slot) — idempotency guard."""
    return f"voucher:{vid}:reminder_sent:{slot}"


def _k_grace_applied(vid: str) -> str:
    return f"voucher:{vid}:grace_applied"


def _k_winback_offered(vid: str) -> str:
    return f"voucher:{vid}:winback_offered"


def _k_user_notifications(uid: str) -> str:
    return f"user:{uid}:notifications"


def _k_user_prefs(uid: str) -> str:
    return f"user:{uid}:notification_prefs"


def _k_brand_stats_expiration(bid: str) -> str:
    return f"brand:{bid}:voucher_expiration_stats"


def _k_winback_offers(uid: str) -> str:
    return f"user:{uid}:voucher_winback_offers"


def _k_voucher_audit(vid: str) -> str:
    return f"voucher:{vid}:lifecycle_audit"


# Active voucher index — populated lazily; in production this would be
# maintained by the issue path. For test isolation we scan
# ``brand:*:issued_vouchers`` on first run if no index exists.
K_ACTIVE_INDEX = "voucher:active_index"


# ── Helpers ───────────────────────────────────────────────────────────────


def _now() -> int:
    return int(time.time())


def _is_quiet_hours(now_ts: float) -> bool:
    """True if ``now_ts`` (UTC seconds) falls inside SGT 22:00–07:00."""
    sgt = datetime.fromtimestamp(now_ts, tz=timezone.utc).astimezone(SGT)
    hour = sgt.hour
    if QUIET_HOURS_START > QUIET_HOURS_END:
        # Wraps midnight: 22..23 or 0..6
        return hour >= QUIET_HOURS_START or hour < QUIET_HOURS_END
    return QUIET_HOURS_START <= hour < QUIET_HOURS_END


def _tier_for_value(value_cents: int) -> tuple[list[int], int] | None:
    """Return (reminder_days, grace_hours) for the highest tier the
    voucher qualifies for. ``None`` if below the smallest tier.
    """
    for threshold, days, grace_h in TIER_CADENCE:
        if value_cents >= threshold:
            return days, grace_h
    return None


def _select_variant(kind: str, user_id: str) -> dict[str, str]:
    """Sticky-per-user variant pick — same user sees same A/B branch."""
    pool = MESSAGE_VARIANTS.get(kind) or []
    if not pool:
        return {"title": kind, "body": ""}
    h = int(hashlib.sha256(f"{kind}:{user_id}".encode()).hexdigest(), 16)
    return pool[h % len(pool)]


def _render(template: dict[str, str], **kwargs: Any) -> dict[str, str]:
    out = {}
    for k, v in template.items():
        try:
            out[k] = v.format(**kwargs)
        except (KeyError, IndexError):
            out[k] = v
    return out


async def _load_user_prefs(r: aioredis.Redis, uid: str) -> dict[str, Any]:
    """Load notification channel preference for a user.

    Default — push first, fall back through whatsapp/sms/email.
    """
    raw = await r.hgetall(_k_user_prefs(uid))
    if not raw:
        return {
            "channels": list(DEFAULT_CHANNEL_ORDER),
            "opted_out": False,
        }
    channels_raw = raw.get("channels")
    channels: list[str]
    if channels_raw:
        try:
            channels = json.loads(channels_raw)
            if not isinstance(channels, list):
                channels = list(DEFAULT_CHANNEL_ORDER)
        except json.JSONDecodeError:
            channels = list(DEFAULT_CHANNEL_ORDER)
    else:
        channels = list(DEFAULT_CHANNEL_ORDER)
    return {
        "channels": channels,
        "opted_out": raw.get("opted_out", "0") == "1",
    }


async def _check_frequency_cap(
    r: aioredis.Redis, *, user_id: str, brand_id: str
) -> bool:
    """Ask the freq-cap router whether we can fire a voucher slot now.

    Fail-OPEN: if the freq-cap module is unavailable or throws, we allow
    the notification — the voucher reminder is too important to drop on
    an infra blip. The cap module itself logs the bypass.
    """
    try:
        from app.routers.frequency_cap import check_internal
        # ``push`` is the closest existing slot — voucher reminders ride
        # the same outbound notification budget as other push traffic.
        allow, _details = await check_internal(
            user_id=user_id, brand_id=brand_id, slot="push", r=r
        )
        return bool(allow)
    except Exception as exc:  # pragma: no cover — never block on cap infra
        logger.debug("frequency_cap unreachable, allowing: %s", exc)
        return True


async def _dispatch_notification(
    r: aioredis.Redis,
    *,
    user_id: str,
    channel: str,
    kind: str,
    payload: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    """Push a notification envelope onto the appropriate transport queue.

    The actual transport workers (FCM push, WhatsApp Business, SMS, SES
    email) consume from these queues — we never call transport SDKs
    directly so the worker stays infra-agnostic.
    """
    envelope = {
        "user_id": user_id,
        "channel": channel,
        "kind": kind,
        "payload": payload,
        "ts": _now(),
    }
    if dry_run:
        return envelope
    try:
        # Per-user notification feed (used by mobile in-app)
        await r.lpush(
            _k_user_notifications(user_id),
            json.dumps(
                {"kind": kind, "ts": _now(), **payload}, default=str
            ),
        )
        await r.ltrim(_k_user_notifications(user_id), 0, 199)
        # Channel-specific transport queue
        await r.rpush(
            f"notify:{channel}:queue",
            json.dumps(envelope, default=str),
        )
        await r.ltrim(f"notify:{channel}:queue", -10_000, -1)
    except aioredis.RedisError as exc:  # pragma: no cover
        logger.warning("notify dispatch failed (kind=%s): %s", kind, exc)
    return envelope


async def _audit(
    r: aioredis.Redis,
    *,
    voucher_id: str,
    event: str,
    meta: dict[str, Any],
    dry_run: bool,
) -> None:
    if dry_run:
        return
    rec = {"event": event, "ts": _now(), **meta}
    try:
        await r.rpush(_k_voucher_audit(voucher_id), json.dumps(rec, default=str))
        await r.ltrim(_k_voucher_audit(voucher_id), -200, -1)
    except aioredis.RedisError as exc:  # pragma: no cover
        logger.debug("audit write failed: %s", exc)


# ── Voucher discovery ─────────────────────────────────────────────────────


async def _scan_active_vouchers(
    r: aioredis.Redis, *, max_items: int = MAX_USERS_PER_RUN,
) -> list[str]:
    """Enumerate vouchers we should consider this run.

    Strategy: SCAN keys ``voucher:{vid}`` directly. Cheap enough for the
    current scale (≤100k active vouchers) and avoids needing a parallel
    index. Bounded by ``max_items`` so a runaway DB doesn't OOM the
    worker.
    """
    vids: list[str] = []
    cursor: int = 0
    while True:
        cursor, keys = await r.scan(
            cursor=cursor, match="voucher:*", count=SCAN_PAGE_SIZE
        )
        for k in keys or []:
            s = k.decode() if isinstance(k, (bytes, bytearray)) else k
            # Only top-level voucher hashes (no sub-keys with extra colons
            # like ``voucher:{vid}:redemption_history``).
            parts = s.split(":")
            if len(parts) != 2:
                continue
            vids.append(parts[1])
            if len(vids) >= max_items:
                return vids
        if cursor == 0:
            break
    return vids


# ── Core: per-voucher decision  ──────────────────────────────────────────


async def _process_voucher(
    r: aioredis.Redis,
    *,
    vid: str,
    now_ts: int,
    quiet: bool,
    dry_run: bool,
    report: dict[str, Any],
) -> None:
    voucher = await r.hgetall(_k_voucher(vid))
    if not voucher:
        return
    status_now = voucher.get("status", "")
    if status_now not in ("issued", "claimed"):
        return  # already redeemed / cancelled / expired-final
    expires_at_raw = voucher.get("expires_at") or ""
    if not expires_at_raw:
        return  # no expiry → no work
    try:
        expires_at = int(expires_at_raw)
    except ValueError:
        return
    try:
        value_cents = int(voucher.get("value_cents", "0") or 0)
    except ValueError:
        value_cents = 0
    holder = voucher.get("holder_user_id", "")
    if not holder or voucher.get("holder_type", "kid") != "kid":
        return  # device_fp vouchers haven't been claimed by a user yet
    brand_id = voucher.get("issuer_brand_id", "")

    tier = _tier_for_value(value_cents)
    if tier is None:
        return  # < $5 — no reminder
    reminder_days, grace_hours = tier

    seconds_to_expiry = expires_at - now_ts

    # ── 1. Pre-expiry reminders ────────────────────────────────────────
    if seconds_to_expiry > 0:
        # Walk slots from smallest (most-imminent, e.g. T-1d) to largest
        # (e.g. T-14d). Fire the most-imminent crossed-but-unfired slot —
        # this way if the worker comes back online after a multi-day gap,
        # it skips ahead to the latest-due reminder instead of replaying
        # the entire historical cadence.
        for days_before in sorted(reminder_days):
            window_start = days_before * 86400
            if seconds_to_expiry > window_start:
                continue  # not yet inside this slot's window
            slot_key = f"T-{days_before}d"
            if await r.exists(_k_reminder_sent(vid, slot_key)):
                continue
            # Quiet hours / frequency cap gating — do not consume the
            # idempotency key so the reminder can fire next hour.
            if quiet:
                report["deferred_quiet_hours"] += 1
                return
            if not await _check_frequency_cap(
                r, user_id=holder, brand_id=brand_id
            ):
                report["deferred_freq_cap"] += 1
                return
            await _fire_reminder(
                r,
                vid=vid,
                voucher=voucher,
                holder=holder,
                brand_id=brand_id,
                value_cents=value_cents,
                slot=slot_key,
                expires_at=expires_at,
                dry_run=dry_run,
                report=report,
                kind="reminder",
            )
            # Also mark every larger-offset slot as sent so a multi-day
            # worker outage doesn't replay the whole cadence next run.
            if not dry_run:
                for older in reminder_days:
                    if older > days_before:
                        await r.set(
                            _k_reminder_sent(vid, f"T-{older}d"),
                            "skipped",
                            ex=180 * 86400,
                        )
            return  # only one reminder per voucher per run
        return

    # ── 2. Just-expired: grace period? ─────────────────────────────────
    seconds_past_expiry = -seconds_to_expiry
    if grace_hours > 0 and seconds_past_expiry <= grace_hours * 3600:
        if not await r.exists(_k_grace_applied(vid)):
            if quiet:
                report["deferred_quiet_hours"] += 1
                return
            await _apply_grace(
                r,
                vid=vid,
                voucher=voucher,
                holder=holder,
                brand_id=brand_id,
                value_cents=value_cents,
                grace_hours=grace_hours,
                old_expires_at=expires_at,
                dry_run=dry_run,
                report=report,
            )
            return
        # Grace already applied — re-check using the new expires_at on
        # next pass once it's been written.

    # ── 3. Post-grace: hard expire + win-back ──────────────────────────
    # Voucher is past expires_at AND past any grace window — mark fully
    # expired and emit a win-back offer (once).
    if not await r.exists(_k_winback_offered(vid)):
        await _expire_and_winback(
            r,
            vid=vid,
            voucher=voucher,
            holder=holder,
            brand_id=brand_id,
            value_cents=value_cents,
            dry_run=dry_run,
            quiet=quiet,
            report=report,
        )


async def _fire_reminder(
    r: aioredis.Redis,
    *,
    vid: str,
    voucher: dict[str, str],
    holder: str,
    brand_id: str,
    value_cents: int,
    slot: str,
    expires_at: int,
    dry_run: bool,
    report: dict[str, Any],
    kind: str,
) -> None:
    prefs = await _load_user_prefs(r, holder)
    if prefs.get("opted_out"):
        report["opted_out"] += 1
        return
    channels = prefs.get("channels") or list(DEFAULT_CHANNEL_ORDER)

    variant = _select_variant(kind, holder)
    rendered = _render(
        variant,
        value=f"{value_cents / 100:.2f}",
        brand=brand_id or "your favourite store",
        expires_in=datetime.fromtimestamp(
            expires_at, tz=timezone.utc
        ).strftime("%Y-%m-%d"),
        grace_hours=24,
    )

    # Premium tier ($100+) → dedicated WhatsApp reach-out as the first
    # channel, regardless of pref order.
    if value_cents >= 10_000 and "whatsapp" in channels:
        channels = ["whatsapp"] + [c for c in channels if c != "whatsapp"]

    fired_channel = channels[0]
    envelope = await _dispatch_notification(
        r,
        user_id=holder,
        channel=fired_channel,
        kind="voucher_expiration_reminder",
        payload={
            "voucher_id": vid,
            "brand_id": brand_id,
            "value_cents": value_cents,
            "expires_at": expires_at,
            "slot": slot,
            "title": rendered.get("title", ""),
            "body": rendered.get("body", ""),
            "variant_pool_size": len(MESSAGE_VARIANTS.get(kind) or []),
        },
        dry_run=dry_run,
    )

    if not dry_run:
        await r.set(_k_reminder_sent(vid, slot), str(_now()), ex=180 * 86400)
        await r.hincrby(
            _k_brand_stats_expiration(brand_id), "reminders_sent", 1
        )

    await _audit(
        r,
        voucher_id=vid,
        event="reminder_sent",
        meta={
            "slot": slot, "channel": fired_channel, "value_cents": value_cents,
            "holder_user_id": holder,
        },
        dry_run=dry_run,
    )

    report["reminders_sent"] += 1
    report["actions"].append({
        "voucher_id": vid,
        "action": "reminder",
        "slot": slot,
        "channel": fired_channel,
        "envelope": envelope,
    })


async def _apply_grace(
    r: aioredis.Redis,
    *,
    vid: str,
    voucher: dict[str, str],
    holder: str,
    brand_id: str,
    value_cents: int,
    grace_hours: int,
    old_expires_at: int,
    dry_run: bool,
    report: dict[str, Any],
) -> None:
    new_expires_at = old_expires_at + grace_hours * 3600

    if not dry_run:
        # Extend the voucher's expiry + mark the grace as applied (one-shot).
        pipe = r.pipeline()
        pipe.hset(
            _k_voucher(vid),
            mapping={
                "expires_at": str(new_expires_at),
                "grace_extended_at": str(_now()),
                "grace_hours": str(grace_hours),
            },
        )
        pipe.set(_k_grace_applied(vid), str(_now()), ex=365 * 86400)
        pipe.hincrby(
            _k_brand_stats_expiration(brand_id), "grace_extensions", 1
        )
        await pipe.execute()

    # Reminder fire on grace window
    variant = _select_variant("grace", holder)
    rendered = _render(
        variant,
        value=f"{value_cents / 100:.2f}",
        brand=brand_id or "your favourite store",
        grace_hours=grace_hours,
    )
    prefs = await _load_user_prefs(r, holder)
    channels = prefs.get("channels") or list(DEFAULT_CHANNEL_ORDER)
    envelope = await _dispatch_notification(
        r,
        user_id=holder,
        channel=channels[0],
        kind="voucher_grace_extended",
        payload={
            "voucher_id": vid,
            "brand_id": brand_id,
            "value_cents": value_cents,
            "grace_hours": grace_hours,
            "new_expires_at": new_expires_at,
            "title": rendered.get("title", ""),
            "body": rendered.get("body", ""),
        },
        dry_run=dry_run,
    )
    await _audit(
        r,
        voucher_id=vid,
        event="grace_extended",
        meta={
            "grace_hours": grace_hours, "old_expires_at": old_expires_at,
            "new_expires_at": new_expires_at, "holder_user_id": holder,
        },
        dry_run=dry_run,
    )
    report["grace_extensions"] += 1
    report["actions"].append({
        "voucher_id": vid,
        "action": "grace_extend",
        "grace_hours": grace_hours,
        "new_expires_at": new_expires_at,
        "envelope": envelope,
    })


async def _expire_and_winback(
    r: aioredis.Redis,
    *,
    vid: str,
    voucher: dict[str, str],
    holder: str,
    brand_id: str,
    value_cents: int,
    dry_run: bool,
    quiet: bool,
    report: dict[str, Any],
) -> None:
    winback_value = value_cents // 2  # 50 % credit
    offer_id = hashlib.sha256(f"winback:{vid}:{_now()}".encode()).hexdigest()[:16]

    if not dry_run:
        # Mark voucher fully expired + record win-back offer
        pipe = r.pipeline()
        pipe.hset(
            _k_voucher(vid),
            mapping={"status": "expired", "expired_at": str(_now())},
        )
        pipe.set(_k_winback_offered(vid), offer_id, ex=180 * 86400)
        pipe.hincrby(_k_brand_stats_expiration(brand_id), "expired", 1)
        pipe.hincrby(
            _k_brand_stats_expiration(brand_id), "winback_offered", 1
        )
        # Per-user list of pending win-back offers
        pipe.lpush(
            _k_winback_offers(holder),
            json.dumps({
                "offer_id": offer_id,
                "original_voucher_id": vid,
                "brand_id": brand_id,
                "original_value_cents": value_cents,
                "winback_value_cents": winback_value,
                "created_at": _now(),
                "claimed": False,
            }, default=str),
        )
        pipe.ltrim(_k_winback_offers(holder), 0, 49)
        await pipe.execute()

    # The notification itself respects quiet hours — but the win-back
    # offer is recorded immediately so the user sees it in-app whenever
    # they next open.
    if not quiet:
        variant = _select_variant("winback", holder)
        rendered = _render(
            variant,
            value=f"{value_cents / 100:.2f}",
            winback_value=f"{winback_value / 100:.2f}",
            brand=brand_id or "your favourite store",
        )
        prefs = await _load_user_prefs(r, holder)
        if not prefs.get("opted_out"):
            channels = prefs.get("channels") or list(DEFAULT_CHANNEL_ORDER)
            await _dispatch_notification(
                r,
                user_id=holder,
                channel=channels[0],
                kind="voucher_winback_offer",
                payload={
                    "voucher_id": vid,
                    "brand_id": brand_id,
                    "original_value_cents": value_cents,
                    "winback_value_cents": winback_value,
                    "offer_id": offer_id,
                    "title": rendered.get("title", ""),
                    "body": rendered.get("body", ""),
                },
                dry_run=dry_run,
            )

    await _audit(
        r,
        voucher_id=vid,
        event="expired_and_winback_offered",
        meta={
            "offer_id": offer_id,
            "winback_value_cents": winback_value,
            "holder_user_id": holder,
        },
        dry_run=dry_run,
    )
    report["expired_finalised"] += 1
    report["winback_offers"] += 1
    report["actions"].append({
        "voucher_id": vid,
        "action": "expire_and_winback",
        "offer_id": offer_id,
        "winback_value_cents": winback_value,
    })


# ── Entrypoint ────────────────────────────────────────────────────────────


async def run_once(
    redis: aioredis.Redis | None = None,
    *,
    now: int | float | None = None,
    dry_run: bool = False,
    max_vouchers: int = MAX_USERS_PER_RUN,
) -> dict[str, Any]:
    """One pass over active vouchers.

    Returns a structured report counting per-action outcomes — fed into
    the admin ``expiration-stats`` endpoint for dashboards.
    """
    r = redis if redis is not None else await get_redis()
    now_ts = int(now) if now is not None else _now()
    quiet = _is_quiet_hours(now_ts)

    report: dict[str, Any] = {
        "scanned": 0,
        "reminders_sent": 0,
        "grace_extensions": 0,
        "expired_finalised": 0,
        "winback_offers": 0,
        "deferred_quiet_hours": 0,
        "deferred_freq_cap": 0,
        "opted_out": 0,
        "quiet_hours_active": quiet,
        "dry_run": dry_run,
        "actions": [],
    }

    vids = await _scan_active_vouchers(r, max_items=max_vouchers)
    for vid in vids:
        report["scanned"] += 1
        try:
            await _process_voucher(
                r,
                vid=vid,
                now_ts=now_ts,
                quiet=quiet,
                dry_run=dry_run,
                report=report,
            )
        except Exception as exc:  # pragma: no cover — per-voucher isolation
            logger.warning("voucher_lifecycle: error processing %s: %s", vid, exc)

    logger.info(
        "voucher_lifecycle_worker scanned=%d reminders=%d grace=%d expired=%d winback=%d "
        "deferred_quiet=%d deferred_cap=%d dry_run=%s",
        report["scanned"], report["reminders_sent"], report["grace_extensions"],
        report["expired_finalised"], report["winback_offers"],
        report["deferred_quiet_hours"], report["deferred_freq_cap"], dry_run,
    )
    return report


if __name__ == "__main__":  # pragma: no cover — cron entrypoint
    asyncio.run(run_once())
