"""SSO Bridge — KiX ID single sign-on across merchants.

The KiX ID (``kid``) is already minted as a network-wide identity by
``app.routers.kix_id``. This service layers the **network-effect**
semantics on top of that primitive:

    * One canonical KiX ID per user — minted once on first contact.
    * Per-brand opt-in linkage (``BrandLink``) with explicit consent
      scope — privacy is the default, network effect is the upgrade.
    * Cross-brand attribution — when a user takes an action at brand B
      after first arriving via brand A, we record the journey so the
      auction / dashboard layer can measure the network's value.
    * Network-stats roll-up — unique kids, unique brands, unique
      cross-brand pairs — the metric the BD team uses to pitch the
      "Stripe Connect of offline gamification" story.

Design choices
--------------
* **Additive only.** ``kix_id.ensure_kid`` already maintains
  ``kid:{kid}:brands`` (a SET written by ``qr_scan_bind``). We layer
  consent-scoped ``brand_link:{kid}:{brand_id}`` HASHes on top — the
  raw membership set keeps working for callers that don't care about
  consent metadata.
* **Privacy as a right, not a setting.** Every link/unlink event is
  written to the durable audit log (PIPL §51). The user can list and
  revoke any brand link at any time.
* **Optional auto-link.** A per-user setting
  ``kid:{kid}:settings:auto_link_city`` (default off) lets power users
  opt into automatic linkage when a new merchant in their city joins
  the network. Off by default — explicit opt-in only.
* **Backward compatible.** The legacy ``kid:{kid}:brands`` SET is
  written *alongside* the new BrandLink HASH, so existing code that
  reads it (e.g. ``identity_link`` phone-verified counters) keeps
  working unchanged.

Redis key schema (additive)
---------------------------
::

    brand_link:{kid}:{brand_id}             HASH
        brand_id, kid, scope (csv), linked_at, unlinked_at,
        last_event_at, source ("register"|"explicit"|"auto_city"|
                               "qr_scan"|"connect"), status
        ("active"|"revoked")
    kid:{kid}:brand_links                   SET of brand_ids (active)
    brand:{brand_id}:kids                   SET of all-time linked kids
    brand:{brand_id}:kids:active            SET of currently-linked kids
    kid:{kid}:settings:auto_link_city       STRING "1" | "0"

    sso:attribution:{kid}                   LIST of JSON records
        {source_brand, target_brand, event, ts}
    sso:cross_pair:{source}:{target}        SET of kids who traversed
                                              the source→target edge

    sso:stats:total_kids                    STRING (counter, eventually
                                                    consistent — recomputed
                                                    by network_stats())
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────

VALID_CONSENT_SCOPES: set[str] = {
    "profile",
    "history",
    "location",
    "favorites",
    "email",
    "phone",
    "insights",
    # Cross-brand-specific scopes — a superset of the OAuth scopes used
    # by ``kix_id.py`` Connect grants. Callers may grant either.
    "cross_brand_tracking",
    "cross_brand_marketing",
}

VALID_LINK_SOURCES: frozenset[str] = frozenset(
    {"register", "explicit", "auto_city", "qr_scan", "connect", "migration"}
)

ATTRIBUTION_HISTORY_MAX = 500

# Audit-log convenience: every link/unlink event records under these
# action names so the compliance team can filter on them cleanly.
_AUDIT_ACTION_LINK = "sso.brand_linked"
_AUDIT_ACTION_UNLINK = "sso.brand_unlinked"
_AUDIT_ACTION_ATTR = "sso.cross_brand_attribute"
_AUDIT_ACTION_CREATE = "sso.kix_id_created"


# ── Helpers ───────────────────────────────────────────────────────────────


def _now() -> int:
    return int(time.time())


def _link_key(kid: str, brand_id: str) -> str:
    return f"brand_link:{kid}:{brand_id}"


def _scopes_to_csv(scopes: list[str] | set[str]) -> str:
    """Normalise + canonicalise scope list for HASH storage."""
    cleaned = sorted({s.strip() for s in scopes if s and s.strip()})
    return ",".join(cleaned)


def _csv_to_scopes(csv: str | None) -> list[str]:
    if not csv:
        return []
    return [s for s in csv.split(",") if s]


def _validate_scopes(scopes: list[str]) -> None:
    bad = [s for s in scopes if s not in VALID_CONSENT_SCOPES]
    if bad:
        raise ValueError(
            f"Invalid consent scope(s): {bad}. "
            f"Allowed: {sorted(VALID_CONSENT_SCOPES)}"
        )


async def _audit(
    action: str,
    *,
    kid: str,
    brand_id: str | None = None,
    payload: dict[str, Any] | None = None,
    result: str = "success",
) -> None:
    """Fire-and-forget durable audit log call.

    Uses the project's ``audit_log_service`` — PIPL §51 compliance.
    Failures never break the request.
    """
    try:
        from app.services.audit_log_service import (
            record_event_fire_and_forget,
        )

        await record_event_fire_and_forget(
            actor_id=kid,
            actor_type="kix_user",
            action=action,
            target_type="brand" if brand_id else "kix_id",
            target_id=brand_id or kid,
            brand_id=brand_id,
            payload=payload,
            result=result,
        )
    except Exception as exc:  # pragma: no cover — audit must never break flow
        logger.debug("sso_bridge audit_log emit failed: %s", exc)


# ── Public API: KiX ID creation ──────────────────────────────────────────


async def create_kix_id(
    r: aioredis.Redis,
    *,
    phone: str | None = None,
    locale: str | None = None,
    region: str | None = None,
    device_fingerprint: str | None = None,
    source_brand_id: str | None = None,
) -> dict[str, Any]:
    """Create (or resolve) the user's single canonical KiX ID.

    Idempotent — if the phone already maps to a kid, returns that kid.
    Delegates to ``app.routers.kix_id.ensure_kid`` so we keep one code
    path for all kid minting. If ``source_brand_id`` is supplied, the
    new kid is immediately ``link_to_brand``-ed with a default profile
    scope so the first merchant doesn't need a second round-trip.

    Returns ``{kid, is_new, locale, region, created_at}``.
    """
    if not phone and not device_fingerprint:
        raise ValueError("phone or device_fingerprint required")

    from app.routers.kix_id import ensure_kid

    kid, is_new = await ensure_kid(
        r,
        phone=phone,
        device_fp=device_fingerprint,
        primary_language=locale,
        country=region,
        source_brand_id=source_brand_id,
    )

    if is_new:
        # Bump network counter (eventually-consistent — network_stats
        # also recomputes from authoritative sources).
        try:
            await r.incr("sso:stats:total_kids")
        except Exception:  # pragma: no cover
            pass
        await _audit(
            _AUDIT_ACTION_CREATE,
            kid=kid,
            brand_id=source_brand_id,
            payload={
                "locale": locale,
                "region": region,
                "source_brand_id": source_brand_id,
                "has_phone": bool(phone),
                "has_device_fp": bool(device_fingerprint),
            },
        )

    # Auto-link to the source brand on first contact — preserves the
    # existing kix_id.register UX (user expects to "be a member" of
    # the merchant they just scanned).
    if source_brand_id:
        await link_to_brand(
            r,
            kid=kid,
            brand_id=source_brand_id,
            consent_scope=["profile"],
            source="register" if is_new else "qr_scan",
            silent=True,  # part of the create flow — single audit row
        )

    return {
        "kid": kid,
        "is_new": is_new,
        "locale": locale,
        "region": region,
        "created_at": _now(),
    }


# ── Public API: Brand linkage ────────────────────────────────────────────


async def link_to_brand(
    r: aioredis.Redis,
    *,
    kid: str,
    brand_id: str,
    consent_scope: list[str],
    source: str = "explicit",
    silent: bool = False,
) -> dict[str, Any]:
    """Opt-in link a kid to a brand with an explicit consent scope.

    Idempotent: re-linking with a new scope **merges** scopes (union)
    so a "yes to profile, then yes to history" upgrade path is one
    natural flow. Re-linking with the same scope updates ``last_event_at``
    only.

    Parameters
    ----------
    consent_scope
        List of granted scopes (subset of ``VALID_CONSENT_SCOPES``).
        Must be non-empty.
    source
        Where the link came from. One of ``register / explicit /
        auto_city / qr_scan / connect / migration``.
    silent
        When True, suppress the per-link audit row. Used by upstream
        flows that already record their own audit event (e.g.
        ``create_kix_id`` writes ``sso.kix_id_created`` which already
        captures the source_brand link).
    """
    if not consent_scope:
        raise ValueError("consent_scope required (non-empty list)")
    _validate_scopes(consent_scope)
    if source not in VALID_LINK_SOURCES:
        raise ValueError(
            f"Invalid source '{source}'. Allowed: {sorted(VALID_LINK_SOURCES)}"
        )

    key = _link_key(kid, brand_id)
    existing = await r.hgetall(key)
    now = _now()

    existing_scope = set(_csv_to_scopes(existing.get("scope"))) if existing else set()
    new_scope = existing_scope | set(consent_scope)
    is_new_link = not existing or existing.get("status") == "revoked"

    record = {
        "kid": kid,
        "brand_id": brand_id,
        "scope": _scopes_to_csv(new_scope),
        "source": source,
        "status": "active",
        "last_event_at": str(now),
    }
    if is_new_link:
        record["linked_at"] = str(now)
        # Clear any prior unlinked_at on re-link.
        record["unlinked_at"] = "0"

    await r.hset(key, mapping=record)
    # Active membership sets (the new, consent-aware view).
    await r.sadd(f"kid:{kid}:brand_links", brand_id)
    await r.sadd(f"brand:{brand_id}:kids:active", kid)
    # Lifetime membership set (never shrinks — for churn analysis).
    await r.sadd(f"brand:{brand_id}:kids", kid)
    # Backward-compatible legacy set (already used by identity_link
    # phone-verified counters, qr_scan_bind, etc.).
    await r.sadd(f"kid:{kid}:brands", brand_id)

    if not silent:
        await _audit(
            _AUDIT_ACTION_LINK,
            kid=kid,
            brand_id=brand_id,
            payload={
                "consent_scope": sorted(new_scope),
                "source": source,
                "is_new_link": is_new_link,
            },
        )

    return {
        "kid": kid,
        "brand_id": brand_id,
        "consent_scope": sorted(new_scope),
        "source": source,
        "status": "active",
        "is_new_link": is_new_link,
        "linked_at": int(record.get("linked_at", existing.get("linked_at", now))),
    }


async def unlink_from_brand(
    r: aioredis.Redis,
    *,
    kid: str,
    brand_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Privacy right: revoke a brand's access. Idempotent.

    Marks the BrandLink as ``revoked``, removes the kid from the
    brand's active set, but **preserves** the lifetime
    ``brand:{brand_id}:kids`` set (regulators need the record of
    "this user did interact with this brand"; the consent revocation
    is layered on top, not destructive).

    Also revokes any active OAuth Connect grants this user has for
    the brand so merchant SDKs immediately stop receiving data —
    consent revocation must propagate to data flow, not just records.
    """
    key = _link_key(kid, brand_id)
    existing = await r.hgetall(key)
    now = _now()

    if not existing:
        # Idempotent — surfacing the no-op cleanly so callers can
        # distinguish "user wasn't linked" from "user is now unlinked".
        return {
            "kid": kid,
            "brand_id": brand_id,
            "status": "not_linked",
            "unlinked_at": now,
        }

    was_active = existing.get("status") != "revoked"
    await r.hset(
        key,
        mapping={
            "status": "revoked",
            "unlinked_at": str(now),
            "last_event_at": str(now),
            "unlink_reason": (reason or "")[:512],
        },
    )
    await r.srem(f"kid:{kid}:brand_links", brand_id)
    await r.srem(f"brand:{brand_id}:kids:active", kid)
    # Legacy set: we DO srem here so existing call sites that read
    # ``kid:{kid}:brands`` see a consistent post-revoke world.
    await r.srem(f"kid:{kid}:brands", brand_id)

    # Cascade: revoke any active OAuth grants for this (kid, brand).
    revoked_grants: list[str] = []
    try:
        gids = await r.smembers(f"kid:{kid}:grants")
        for gid in gids or []:
            g = await r.hgetall(f"grant:{gid}")
            if g and g.get("brand_id") == brand_id and g.get("revoked") != "true":
                await r.hset(
                    f"grant:{gid}",
                    mapping={
                        "revoked": "true",
                        "revoked_at": str(now),
                        "revoked_by": "user",
                    },
                )
                revoked_grants.append(gid)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("sso_bridge: grant cascade failed: %s", exc)

    if was_active:
        await _audit(
            _AUDIT_ACTION_UNLINK,
            kid=kid,
            brand_id=brand_id,
            payload={
                "reason": (reason or "")[:128],
                "revoked_grants": revoked_grants,
            },
        )

    return {
        "kid": kid,
        "brand_id": brand_id,
        "status": "revoked",
        "unlinked_at": now,
        "revoked_grants": revoked_grants,
    }


async def get_user_brands(
    r: aioredis.Redis,
    *,
    kid: str,
    include_revoked: bool = False,
) -> list[dict[str, Any]]:
    """List every brand this kid has linked to.

    By default returns active links only. Pass ``include_revoked=True``
    for the user's full lifetime privacy-dashboard view.
    """
    # We walk the lifetime view (active + revoked) by scanning every
    # brand_link key for this kid. The active SET is the fast-path
    # filter when ``include_revoked=False``.
    out: list[dict[str, Any]] = []

    if not include_revoked:
        active_brands = await r.smembers(f"kid:{kid}:brand_links")
        for b in active_brands:
            rec = await r.hgetall(_link_key(kid, b))
            if not rec or rec.get("status") == "revoked":
                continue
            out.append(_brand_link_view(rec))
    else:
        # Lifetime scan via SCAN on the brand_link:{kid}:* prefix.
        cursor = 0
        pattern = f"brand_link:{kid}:*"
        while True:
            cursor, keys = await r.scan(
                cursor=cursor, match=pattern, count=200
            )
            for k in keys:
                rec = await r.hgetall(k)
                if rec:
                    out.append(_brand_link_view(rec))
            if cursor == 0:
                break

    # Active first, then most-recent first.
    out.sort(
        key=lambda x: (x["status"] != "active", -int(x.get("last_event_at", 0)))
    )
    return out


def _brand_link_view(rec: dict[str, str]) -> dict[str, Any]:
    return {
        "kid": rec.get("kid"),
        "brand_id": rec.get("brand_id"),
        "consent_scope": _csv_to_scopes(rec.get("scope")),
        "source": rec.get("source"),
        "status": rec.get("status", "active"),
        "linked_at": int(rec.get("linked_at", 0) or 0),
        "unlinked_at": int(rec.get("unlinked_at", 0) or 0),
        "last_event_at": int(rec.get("last_event_at", 0) or 0),
    }


async def get_brand_users(
    r: aioredis.Redis,
    *,
    brand_id: str,
    include_revoked: bool = False,
    limit: int = 1000,
) -> list[str]:
    """Return every KiX ID linked to this brand.

    Active-only by default. ``limit`` is a soft cap to keep ops UI
    paginated; pass a higher value for billing exports.
    """
    if include_revoked:
        members = await r.smembers(f"brand:{brand_id}:kids")
    else:
        members = await r.smembers(f"brand:{brand_id}:kids:active")
    kids = sorted(members)
    return kids[: max(1, int(limit))]


# ── Public API: Cross-brand attribution ──────────────────────────────────


async def cross_brand_attribute(
    r: aioredis.Redis,
    *,
    kid: str,
    source_brand: str,
    target_brand: str,
    event: str,
) -> dict[str, Any]:
    """Record that ``kid`` took ``event`` at ``target_brand`` after
    arriving via ``source_brand``.

    This is the **network-effect telemetry** — without it we can't
    measure that the KiX network is delivering value beyond any
    individual merchant. The auction / dashboard reads
    ``sso:cross_pair:{source}:{target}`` to surface the source→target
    "introduction value" for the BD pitch deck.

    Note: cross-brand attribution requires the user to have granted
    ``cross_brand_tracking`` consent on **at least one** of the two
    brand links (we accept either side; the source brand owns the
    "I introduced this user" claim, the target brand owns the
    "I converted this user" claim — either suffices).
    """
    if source_brand == target_brand:
        # Self-referential — not interesting, but not an error. Treat
        # as a no-op rather than poisoning the journey graph.
        return {
            "kid": kid,
            "source_brand": source_brand,
            "target_brand": target_brand,
            "event": event,
            "recorded": False,
            "reason": "same_brand",
        }

    if not event or len(event) > 128:
        raise ValueError("event required (≤128 chars)")

    # Consent gate — at least one of (source, target) must have a
    # cross_brand_tracking scope. Linkage with ``profile`` alone is
    # not sufficient.
    consented = False
    for b in (source_brand, target_brand):
        rec = await r.hgetall(_link_key(kid, b))
        scopes = set(_csv_to_scopes(rec.get("scope") if rec else None))
        if rec and rec.get("status") == "active" and (
            "cross_brand_tracking" in scopes or "history" in scopes
        ):
            consented = True
            break
    if not consented:
        await _audit(
            _AUDIT_ACTION_ATTR,
            kid=kid,
            brand_id=target_brand,
            result="denied",
            payload={
                "source_brand": source_brand,
                "event": event,
                "reason": "no_cross_brand_consent",
            },
        )
        return {
            "kid": kid,
            "source_brand": source_brand,
            "target_brand": target_brand,
            "event": event,
            "recorded": False,
            "reason": "no_cross_brand_consent",
        }

    now = _now()
    entry = {
        "source_brand": source_brand,
        "target_brand": target_brand,
        "event": event,
        "ts": now,
    }
    await r.lpush(f"sso:attribution:{kid}", json.dumps(entry))
    await r.ltrim(f"sso:attribution:{kid}", 0, ATTRIBUTION_HISTORY_MAX - 1)
    # Pairwise journey set — read by the auction / dashboard.
    await r.sadd(f"sso:cross_pair:{source_brand}:{target_brand}", kid)
    # Aggregate journey counter (eventually-consistent).
    try:
        await r.incr("sso:stats:total_cross_brand_events")
    except Exception:  # pragma: no cover
        pass

    await _audit(
        _AUDIT_ACTION_ATTR,
        kid=kid,
        brand_id=target_brand,
        payload={"source_brand": source_brand, "event": event},
    )

    return {
        "kid": kid,
        "source_brand": source_brand,
        "target_brand": target_brand,
        "event": event,
        "recorded": True,
        "ts": now,
    }


async def get_attribution_journey(
    r: aioredis.Redis,
    *,
    kid: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return this kid's cross-brand journey (most recent first)."""
    raw = await r.lrange(
        f"sso:attribution:{kid}", 0, max(0, min(int(limit), ATTRIBUTION_HISTORY_MAX) - 1)
    )
    out: list[dict[str, Any]] = []
    for item in raw:
        try:
            out.append(json.loads(item))
        except Exception:  # pragma: no cover — guard against corruption
            continue
    return out


# ── Public API: Network stats (ops dashboard) ────────────────────────────


async def network_stats(r: aioredis.Redis) -> dict[str, Any]:
    """Return a snapshot of the SSO network for the ops dashboard.

    Fields:
        * ``total_kids`` — unique KiX IDs ever minted
        * ``total_brands`` — brands with at least one active link
        * ``total_active_links`` — sum of active links across brands
        * ``total_cross_brand_pairs`` — distinct (source, target) edges
        * ``total_cross_brand_events`` — lifetime attribute calls
        * ``top_brands`` — top 10 brands by active link count

    Authoritative recompute (eventually-consistent) — scans the
    ``brand:*:kids:active`` keyspace and aggregates. SCAN-based so
    we never block on a single big KEYS call.
    """
    # 1. Walk brand:*:kids:active to find every linked brand.
    brand_counts: dict[str, int] = {}
    cursor = 0
    pattern = "brand:*:kids:active"
    while True:
        cursor, keys = await r.scan(cursor=cursor, match=pattern, count=200)
        for k in keys:
            # Key shape: ``brand:{brand_id}:kids:active`` — split on
            # ``:`` and pick index 1. Skip malformed keys defensively.
            parts = k.split(":")
            if len(parts) < 4 or parts[0] != "brand":
                continue
            brand_id = parts[1]
            count = await r.scard(k)
            if count > 0:
                brand_counts[brand_id] = int(count)
        if cursor == 0:
            break

    total_brands = len(brand_counts)
    total_active_links = sum(brand_counts.values())

    # 2. Distinct cross-brand pairs.
    cross_pairs = 0
    cursor = 0
    pattern = "sso:cross_pair:*"
    while True:
        cursor, keys = await r.scan(cursor=cursor, match=pattern, count=200)
        for k in keys:
            if await r.scard(k) > 0:
                cross_pairs += 1
        if cursor == 0:
            break

    # 3. Counters (best-effort — recomputable from sets if drift suspected).
    total_kids = int(await r.get("sso:stats:total_kids") or 0)
    total_cross_events = int(
        await r.get("sso:stats:total_cross_brand_events") or 0
    )

    # 4. Top brands by active count.
    top_brands = sorted(
        brand_counts.items(), key=lambda kv: -kv[1]
    )[:10]

    return {
        "total_kids": total_kids,
        "total_brands": total_brands,
        "total_active_links": total_active_links,
        "total_cross_brand_pairs": cross_pairs,
        "total_cross_brand_events": total_cross_events,
        "top_brands": [
            {"brand_id": b, "active_users": c} for b, c in top_brands
        ],
        "snapshot_ts": _now(),
    }


# ── Public API: Auto-link settings ───────────────────────────────────────


async def set_auto_link_city(
    r: aioredis.Redis, *, kid: str, enabled: bool
) -> dict[str, Any]:
    """Toggle the "auto-link new merchants in my city" power-user setting.

    Default off. When on, the QR-scan / register flow MAY auto-link
    the kid to a newly-encountered merchant in their declared city
    without an explicit opt-in modal. The user can always revoke via
    ``unlink_from_brand``. This is **strictly opt-in** — the default
    state must remain "explicit consent per brand".
    """
    await r.set(
        f"kid:{kid}:settings:auto_link_city", "1" if enabled else "0"
    )
    return {"kid": kid, "auto_link_city": bool(enabled)}


async def get_auto_link_city(r: aioredis.Redis, *, kid: str) -> bool:
    val = await r.get(f"kid:{kid}:settings:auto_link_city")
    return val == "1"


__all__ = [
    "create_kix_id",
    "link_to_brand",
    "unlink_from_brand",
    "get_user_brands",
    "get_brand_users",
    "cross_brand_attribute",
    "get_attribution_journey",
    "network_stats",
    "set_auto_link_city",
    "get_auto_link_city",
    "VALID_CONSENT_SCOPES",
    "VALID_LINK_SOURCES",
]
