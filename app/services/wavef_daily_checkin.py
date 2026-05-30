"""Daily check-in service — Wave F obvious-win #2.

Inspired by BI WORLDWIDE / Bunchball Nitro daily-active-driver mechanic.

Simple boolean-per-day per-brand, with optional voucher issue on success.
Distinct from full streak (which tracks current/longest); check-in is a
zero-overhead daily ping and can run alongside streak.

Redis schema:
    checkin:{bid}:{uid}:{YYYY-MM-DD}   STRING "1"  (NX-set, TTL 48h)
    checkin:{bid}:{uid}:total          STRING int counter
    checkin:{bid}:{uid}:last           STRING YYYY-MM-DD

NEW file.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


_SGT = timezone(timedelta(hours=8))


def _sgt_today() -> str:
    return datetime.now(_SGT).strftime("%Y-%m-%d")


def _k_today(brand_id: str, user_id: str, day: str) -> str:
    return f"checkin:{brand_id}:{user_id}:{day}"


def _k_total(brand_id: str, user_id: str) -> str:
    return f"checkin:{brand_id}:{user_id}:total"


def _k_last(brand_id: str, user_id: str) -> str:
    return f"checkin:{brand_id}:{user_id}:last"


async def check_in(r, brand_id: str, user_id: str, day: str | None = None) -> dict:
    """Idempotent daily check-in.

    Returns:
        {
            "checked_in_today": True if this call did the check (False if duplicate),
            "day": "YYYY-MM-DD",
            "total_checkins": int,
            "reward_eligible": True if this is the first today,
        }
    """
    day = day or _sgt_today()
    today_key = _k_today(brand_id, user_id, day)
    # NX-set with 48h TTL — survives clock skew across timezone boundary
    set_ok = await r.set(today_key, "1", nx=True, ex=48 * 3600)
    if set_ok:
        # First time today; bump totals and last seen
        pipe = r.pipeline(transaction=True)
        pipe.incr(_k_total(brand_id, user_id))
        pipe.set(_k_last(brand_id, user_id), day)
        res = await pipe.execute()
        total = int(res[0])
        return {
            "checked_in_today": True,
            "day": day,
            "total_checkins": total,
            "reward_eligible": True,
        }
    # Already checked in today
    total_raw = await r.get(_k_total(brand_id, user_id))
    try:
        total = int(total_raw) if total_raw is not None else 0
    except (TypeError, ValueError):
        total = 0
    return {
        "checked_in_today": False,
        "day": day,
        "total_checkins": total,
        "reward_eligible": False,
    }


async def status(r, brand_id: str, user_id: str, day: str | None = None) -> dict:
    """Read-only check-in status."""
    day = day or _sgt_today()
    has = await r.exists(_k_today(brand_id, user_id, day))
    total_raw = await r.get(_k_total(brand_id, user_id))
    last = await r.get(_k_last(brand_id, user_id))
    try:
        total = int(total_raw) if total_raw is not None else 0
    except (TypeError, ValueError):
        total = 0
    return {
        "checked_in_today": bool(has),
        "day": day,
        "total_checkins": total,
        "last_check_in": last if isinstance(last, str) else (last.decode() if last else None),
    }
