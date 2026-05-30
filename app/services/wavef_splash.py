"""Pre-game brand splash — Wave F spec #15.

A tiny config endpoint that returns the splash screen parameters for a
campaign — logo, tagline, brand colour, duration. The client renders
the actual splash and uses ``localStorage`` to throttle to
``show_max_per_day``.

Redis schema
------------
::

    campaign:{cid}:splash    HASH {enabled, logo_url, tagline, duration_ms,
                                   brand_primary, show_max_per_day}

NEW file.
"""

from __future__ import annotations

import re

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _k_splash(cid: str) -> str:
    return f"campaign:{cid}:splash"


def _validate(
    *,
    duration_ms: int,
    show_max_per_day: int,
    brand_primary: str,
) -> None:
    if not (500 <= duration_ms <= 10000):
        raise ValueError("duration_ms must be between 500 and 10000")
    if not (1 <= show_max_per_day <= 50):
        raise ValueError("show_max_per_day must be between 1 and 50")
    if brand_primary and not _HEX_RE.match(brand_primary):
        raise ValueError("brand_primary must be a hex color like #RRGGBB")


async def set_config(
    r,
    *,
    campaign_id: str,
    logo_url: str,
    tagline: str = "",
    duration_ms: int = 3000,
    brand_primary: str = "#1F6FEB",
    show_max_per_day: int = 1,
    enabled: bool = True,
) -> dict:
    _validate(
        duration_ms=duration_ms,
        show_max_per_day=show_max_per_day,
        brand_primary=brand_primary,
    )
    if not logo_url and enabled:
        raise ValueError("logo_url is required when splash is enabled")
    await r.hset(
        _k_splash(campaign_id),
        mapping={
            "enabled": "1" if enabled else "0",
            "logo_url": logo_url,
            "tagline": tagline,
            "duration_ms": str(int(duration_ms)),
            "brand_primary": brand_primary,
            "show_max_per_day": str(int(show_max_per_day)),
        },
    )
    return await get_config(r, campaign_id)


async def disable(r, campaign_id: str) -> dict:
    """Mark splash as disabled without deleting the row."""
    await r.hset(_k_splash(campaign_id), mapping={"enabled": "0"})
    return await get_config(r, campaign_id)


async def get_config(r, campaign_id: str) -> dict | None:
    raw = await r.hgetall(_k_splash(campaign_id))
    if not raw:
        return None
    norm: dict[str, str] = {}
    for k, v in raw.items():
        norm[k.decode() if isinstance(k, bytes) else k] = (
            v.decode() if isinstance(v, bytes) else v
        )
    enabled = norm.get("enabled", "0") == "1"
    try:
        duration_ms = int(norm.get("duration_ms", "3000") or 3000)
    except (TypeError, ValueError):
        duration_ms = 3000
    try:
        show_max_per_day = int(norm.get("show_max_per_day", "1") or 1)
    except (TypeError, ValueError):
        show_max_per_day = 1
    return {
        "campaign_id": campaign_id,
        "enabled": enabled,
        "logo_url": norm.get("logo_url", ""),
        "tagline": norm.get("tagline", ""),
        "duration_ms": duration_ms,
        "brand_primary": norm.get("brand_primary", "#1F6FEB"),
        "show_max_per_day": show_max_per_day,
    }
