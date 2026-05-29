"""Audiences — Custom and Lookalike user audiences for ad targeting.

A merchant can build *Custom* audiences from first-party data (CRM upload,
website visitors, converters, in-app users) and then derive *Lookalike*
audiences that algorithmically expand reach to "users similar to your best
customers". Audiences are then attached to campaigns as either **inclusion**
(target only these people) or **exclusion** (suppress — e.g. don't show
acquisition ads to existing customers).

Industry parallels: Meta Custom Audiences, Google Customer Match, TikTok
Audience Manager.

Identifiers
-----------
Three address types are supported. Email and phone are stored as SHA-256
hashes (industry standard for PII safety); merchants are expected to hash
client-side before upload. ``user_ids`` are KiX-native and resolved
directly.

  * ``user_ids``        — KiX user IDs (direct membership)
  * ``emails_sha256``   — hex sha256 of normalised email
  * ``phones_sha256``   — hex sha256 of E.164 phone

A reverse identity index (``identity:email:{hash}`` → user_id) maps hashes
back to KiX users at upload time. Unmatched hashes are kept on a pending
set; if/when a user later signs up and registers that hash, a background
backfill (out of scope here) can claim them.

Redis Schema
------------
  audience:{aid}                       HASH  full state
  audience:{aid}:members               SET   of KiX user_ids
  audience:{aid}:pending_emails        SET   sha256 not yet matched
  audience:{aid}:pending_phones        SET   sha256 not yet matched
  audience:{aid}:growth                LIST  JSON points {ts,size} (cap 30)
  brand:{bid}:audiences                SET   audience_ids
  user:{uid}:audiences                 SET   reverse index for auction
  audience:{aid}:campaign_include      SET   campaign_ids
  audience:{aid}:campaign_exclude      SET   campaign_ids
  campaign:{cid}:include_audiences     SET   audience_ids
  campaign:{cid}:exclude_audiences     SET   audience_ids
  identity:email:{sha256}              STR   user_id (read-only resolver)
  identity:phone:{sha256}              STR   user_id (read-only resolver)

Lookalike Algorithm (MVP cohort match)
--------------------------------------
1. Walk seed members; build a centroid (country histogram, mean age,
   interest-tag bag-of-tags, gender histogram).
2. SCAN a sample of platform users; score each by feature overlap.
3. Take top-N where N ≈ base_size × similarity_factor (1=tightest →
   small, 10=loosest → broad). Mark ``is_lookalike=true`` and write
   ``lookalike_seed=<aid>`` so the lineage is auditable.

This is intentionally simple — production would replace it with an
embedding-space kNN index. The API surface is stable so the swap is
internal.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterable, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis
from app.routers.campaigns import _safe_json_loads

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ────────────────────────────────────────────────────────────

AUDIENCE_KEY = "audience:{aid}"
AUDIENCE_MEMBERS_KEY = "audience:{aid}:members"
AUDIENCE_PENDING_EMAILS_KEY = "audience:{aid}:pending_emails"
AUDIENCE_PENDING_PHONES_KEY = "audience:{aid}:pending_phones"
AUDIENCE_GROWTH_KEY = "audience:{aid}:growth"
BRAND_AUDIENCES_KEY = "brand:{bid}:audiences"
USER_AUDIENCES_KEY = "user:{uid}:audiences"

AUD_INCLUDE_CAMPAIGNS_KEY = "audience:{aid}:campaign_include"
AUD_EXCLUDE_CAMPAIGNS_KEY = "audience:{aid}:campaign_exclude"
CAMPAIGN_INCLUDE_AUDS_KEY = "campaign:{cid}:include_audiences"
CAMPAIGN_EXCLUDE_AUDS_KEY = "campaign:{cid}:exclude_audiences"

IDENTITY_EMAIL_KEY = "identity:email:{hash}"
IDENTITY_PHONE_KEY = "identity:phone:{hash}"

VALID_SOURCES = {
    "csv_upload",
    "website_visitors",
    "app_users",
    "converters",
    "manual",
    "filter",
}

# Event-type → ordered list of attribution stages that qualify. Used by
# recency filters. "any" matches every stage on the journey.
RECENCY_EVENT_STAGES = {
    "purchase": {"conversion"},
    "visit": {"visit", "click", "impression"},
    "any": None,  # sentinel — accept any stage
}

# Hard cap on user-set SCAN for filter-built audiences to avoid pathological
# walks on large platforms. Tune up once we move to a proper index.
FILTER_MAX_SCAN = 5000

STATUS_READY = "ready"
STATUS_BUILDING = "building"

# Lookalike: maps slider value 1..10 → size multiplier of seed and an
# upper bound. Tighter slider = stricter score threshold.
LOOKALIKE_SIZE_MULT = {
    1: 2.0,   # tightest cohort, still 2× seed
    2: 3.0,
    3: 5.0,
    4: 7.0,
    5: 10.0,
    6: 15.0,
    7: 22.0,
    8: 32.0,
    9: 45.0,
    10: 60.0,
}
# Minimum similarity score required to be included at each slider value.
LOOKALIKE_SCORE_FLOOR = {
    1: 0.85,
    2: 0.75,
    3: 0.65,
    4: 0.55,
    5: 0.45,
    6: 0.35,
    7: 0.25,
    8: 0.15,
    9: 0.08,
    10: 0.0,
}

# Hard cap on platform users scanned during lookalike construction.
LOOKALIKE_MAX_SCAN = 5000
GROWTH_HISTORY_CAP = 30


# ── Pydantic Models ──────────────────────────────────────────────────────


class CustomAudienceCreate(BaseModel):
    brand_id: str
    name: str
    source: Literal[
        "csv_upload",
        "website_visitors",
        "app_users",
        "converters",
        "manual",
        "filter",
    ]
    user_ids: list[str] = Field(default_factory=list)
    emails_sha256: list[str] = Field(default_factory=list)
    phones_sha256: list[str] = Field(default_factory=list)
    description: str | None = None
    # ── Filter-built audiences ──────────────────────────────────────────
    # Used when source="filter". Each filter is independent; a user must
    # satisfy ALL provided filters (AND) to be included.
    #
    # recency_filter:
    #   {min_days_since: int?, max_days_since: int?,
    #    event_type: "purchase"|"visit"|"any"}
    #   → match users whose most-recent event of that type falls inside
    #     [min_days_since, max_days_since] days ago.
    # lifecycle_filter:
    #   {stages: ["new_mom_0_3mo", ...]}
    #   → match users whose user:{uid}:lifecycle_stage.stage is in the set.
    # attribute_filter:
    #   {attr_key: expected_value, ...} (values stringified)
    #   → match users whose attribute hash has those k/v pairs.
    recency_filter: dict | None = None
    lifecycle_filter: dict | None = None
    attribute_filter: dict | None = None


class FilterPreview(BaseModel):
    brand_id: str
    recency_filter: dict | None = None
    lifecycle_filter: dict | None = None
    attribute_filter: dict | None = None
    limit: int = Field(default=100, ge=1, le=FILTER_MAX_SCAN)


class AudienceAppend(BaseModel):
    user_ids: list[str] = Field(default_factory=list)
    emails_sha256: list[str] = Field(default_factory=list)
    phones_sha256: list[str] = Field(default_factory=list)


class LookalikeCreate(BaseModel):
    brand_id: str
    similarity: int = Field(ge=1, le=10)
    countries: list[str] = Field(default_factory=list)
    name: str | None = None


class CampaignLink(BaseModel):
    campaign_id: str


class MembershipCheck(BaseModel):
    user_id: str
    audience_id: str


class AudienceCreateResponse(BaseModel):
    audience_id: str
    size: int
    status: str


# ── Key helpers ──────────────────────────────────────────────────────────


def _now() -> float:
    return time.time()


def _ak(aid: str) -> str:
    return AUDIENCE_KEY.format(aid=aid)


def _mk(aid: str) -> str:
    return AUDIENCE_MEMBERS_KEY.format(aid=aid)


def _pek(aid: str) -> str:
    return AUDIENCE_PENDING_EMAILS_KEY.format(aid=aid)


def _ppk(aid: str) -> str:
    return AUDIENCE_PENDING_PHONES_KEY.format(aid=aid)


def _bak(bid: str) -> str:
    return BRAND_AUDIENCES_KEY.format(bid=bid)


def _uak(uid: str) -> str:
    return USER_AUDIENCES_KEY.format(uid=uid)


def _aud_inc_camps(aid: str) -> str:
    return AUD_INCLUDE_CAMPAIGNS_KEY.format(aid=aid)


def _aud_exc_camps(aid: str) -> str:
    return AUD_EXCLUDE_CAMPAIGNS_KEY.format(aid=aid)


def _camp_inc_auds(cid: str) -> str:
    return CAMPAIGN_INCLUDE_AUDS_KEY.format(cid=cid)


def _camp_exc_auds(cid: str) -> str:
    return CAMPAIGN_EXCLUDE_AUDS_KEY.format(cid=cid)


def _gk(aid: str) -> str:
    return AUDIENCE_GROWTH_KEY.format(aid=aid)


# ── Identity resolution ──────────────────────────────────────────────────


async def _resolve_email_hashes(
    r: aioredis.Redis, hashes: Iterable[str]
) -> tuple[list[str], list[str]]:
    """Returns (matched_user_ids, unmatched_hashes)."""
    matched: list[str] = []
    unmatched: list[str] = []
    for h in hashes:
        if not h:
            continue
        uid = await r.get(IDENTITY_EMAIL_KEY.format(hash=h))
        if uid:
            matched.append(uid)
        else:
            unmatched.append(h)
    return matched, unmatched


async def _resolve_phone_hashes(
    r: aioredis.Redis, hashes: Iterable[str]
) -> tuple[list[str], list[str]]:
    matched: list[str] = []
    unmatched: list[str] = []
    for h in hashes:
        if not h:
            continue
        uid = await r.get(IDENTITY_PHONE_KEY.format(hash=h))
        if uid:
            matched.append(uid)
        else:
            unmatched.append(h)
    return matched, unmatched


async def _add_members(
    r: aioredis.Redis,
    aid: str,
    user_ids: Iterable[str],
) -> int:
    """Adds members + maintains reverse index. Returns count newly added."""
    uids = [u for u in user_ids if u]
    if not uids:
        return 0
    pipe = r.pipeline()
    pipe.sadd(_mk(aid), *uids)
    for uid in uids:
        pipe.sadd(_uak(uid), aid)
    res = await pipe.execute()
    return int(res[0]) if res else 0


async def _record_growth(r: aioredis.Redis, aid: str, size: int) -> None:
    """Append a tiny growth point (ts, size); cap LIST length."""
    point = json.dumps({"ts": _now(), "size": size})
    pipe = r.pipeline()
    pipe.rpush(_gk(aid), point)
    pipe.ltrim(_gk(aid), -GROWTH_HISTORY_CAP, -1)
    await pipe.execute()


# ── Filter evaluation (recency / lifecycle / attribute) ─────────────────


def _serialize_attr_value(v: Any) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v)


async def _matches_recency(
    r: aioredis.Redis, user_id: str, rf: dict
) -> bool:
    """Return True if the user has an event of the requested type within
    the [min_days_since, max_days_since] window (either bound optional)."""
    event_type = (rf.get("event_type") or "any").lower()
    accepted_stages = RECENCY_EVENT_STAGES.get(event_type)
    min_days = rf.get("min_days_since")
    max_days = rf.get("max_days_since")

    # Walk the journey (newest-first), find first qualifying event.
    journey_key = f"user:{user_id}:attr_journey"
    event_ids = await r.lrange(journey_key, 0, 200)
    if not event_ids:
        return False

    now = time.time()
    matched_age_days: float | None = None
    for eid in event_ids:
        ev = await r.hgetall(f"attr:{eid}")
        if not ev:
            continue
        if accepted_stages is not None and ev.get("stage") not in accepted_stages:
            continue
        try:
            ts = float(ev.get("timestamp", 0) or 0)
        except (TypeError, ValueError):
            continue
        if ts <= 0:
            continue
        age_days = (now - ts) / 86400.0
        matched_age_days = age_days
        break

    if matched_age_days is None:
        return False
    if min_days is not None and matched_age_days < float(min_days):
        return False
    if max_days is not None and matched_age_days > float(max_days):
        return False
    return True


async def _matches_lifecycle(
    r: aioredis.Redis, user_id: str, lf: dict
) -> bool:
    wanted = set(lf.get("stages") or [])
    if not wanted:
        return True  # vacuous filter — accept all
    raw = await r.hgetall(f"user:{user_id}:lifecycle_stage")
    if not raw:
        return False
    return raw.get("stage") in wanted


async def _matches_attributes(
    r: aioredis.Redis,
    user_id: str,
    af: dict,
    brand_id: str | None,
) -> bool:
    """Match against global attrs first, then brand-scoped overrides."""
    if not af:
        return True
    global_key = f"user:{user_id}:attributes"
    scoped_key = (
        f"user:{user_id}:attributes:{brand_id}" if brand_id else None
    )
    glob = await r.hgetall(global_key) or {}
    scoped = await r.hgetall(scoped_key) if scoped_key else {}
    for k, v in af.items():
        expected = _serialize_attr_value(v)
        actual = scoped.get(k) if scoped else None
        if actual is None:
            actual = glob.get(k)
        if actual != expected:
            return False
    return True


async def _evaluate_filters_for_user(
    r: aioredis.Redis,
    user_id: str,
    brand_id: str | None,
    recency_filter: dict | None,
    lifecycle_filter: dict | None,
    attribute_filter: dict | None,
) -> bool:
    if recency_filter and not await _matches_recency(r, user_id, recency_filter):
        return False
    if lifecycle_filter and not await _matches_lifecycle(r, user_id, lifecycle_filter):
        return False
    if attribute_filter and not await _matches_attributes(
        r, user_id, attribute_filter, brand_id
    ):
        return False
    return True


async def _scan_users_for_filters(
    r: aioredis.Redis,
    brand_id: str | None,
    recency_filter: dict | None,
    lifecycle_filter: dict | None,
    attribute_filter: dict | None,
    limit: int,
) -> tuple[list[str], int]:
    """SCAN ``user:*`` profile keys, return up to ``limit`` matching uids.

    Returns (matched_uids, total_scanned). Scans are capped at
    ``min(limit, FILTER_MAX_SCAN)`` candidates so a caller asking for 50
    matches doesn't pay the cost of scanning 5000 keys.
    """
    # Bug 7 fix: respect the caller's ``limit`` as a strict upper bound on
    # both matched results AND total keys scanned. Previously the scan
    # would keep going up to FILTER_MAX_SCAN regardless of how few results
    # the caller wanted, causing latency spikes for small audiences.
    effective_scan_cap = min(max(int(limit), 1), FILTER_MAX_SCAN)
    out: list[str] = []
    cursor = 0
    scanned = 0
    while scanned < effective_scan_cap and len(out) < limit:
        cursor, batch = await r.scan(cursor=cursor, match="user:*", count=200)
        for k in batch:
            # Only top-level user profile keys (user:{uid}), not sub-keys
            # like user:{uid}:currency:... which contain extra colons.
            if k.count(":") != 1:
                continue
            scanned += 1
            uid = k.split(":", 1)[1]
            ok = await _evaluate_filters_for_user(
                r,
                uid,
                brand_id,
                recency_filter,
                lifecycle_filter,
                attribute_filter,
            )
            if ok:
                out.append(uid)
                if len(out) >= limit:
                    break
            if scanned >= effective_scan_cap:
                break
        if cursor == 0:
            break
    return out, scanned


# ── Endpoints: Custom Audience ───────────────────────────────────────────


@router.post("/custom/create", response_model=AudienceCreateResponse)
async def create_custom_audience(
    body: CustomAudienceCreate,
    r: aioredis.Redis = Depends(get_redis),
) -> AudienceCreateResponse:
    """Create a custom audience from CRM-style input."""
    if body.source not in VALID_SOURCES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"source must be one of {sorted(VALID_SOURCES)}",
        )

    # ── Tier-quota gate (audiences) ────────────────────────────────────
    # Block creation when the brand has hit the audiences quota for its
    # current subscription tier. Fail-open if the subscription module is
    # unavailable.
    try:
        from app.routers.brand_subscriptions import check_quota
        allowed, info = await check_quota(body.brand_id, "audiences", r)
        if not allowed:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "tier_limit_reached",
                    "message": (
                        f"Your {info['tier']} tier allows "
                        f"{info['limit']} custom audiences. Upgrade to "
                        f"{info['upgrade_required_to']} for more."
                    ),
                    "tier": info["tier"],
                    "current": info["current"],
                    "limit": info["limit"],
                    "upgrade_required_to": info["upgrade_required_to"],
                },
            )
    except HTTPException:
        raise
    except (ImportError, ValueError):
        # Module not available or unknown resource — fail-open.
        pass

    aid = f"aud_{uuid4().hex[:16]}"

    matched_email_uids, unmatched_emails = await _resolve_email_hashes(
        r, body.emails_sha256
    )
    matched_phone_uids, unmatched_phones = await _resolve_phone_hashes(
        r, body.phones_sha256
    )
    all_uids: set[str] = set()
    all_uids.update(body.user_ids)
    all_uids.update(matched_email_uids)
    all_uids.update(matched_phone_uids)

    # source=filter: materialize membership by scanning matching users now.
    # Filter spec is also persisted so auction-time matching can re-evaluate
    # dynamically (a user becomes a member as soon as they satisfy filters).
    filter_spec: dict[str, Any] = {}
    if body.source == "filter":
        if not any([body.recency_filter, body.lifecycle_filter, body.attribute_filter]):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="source=filter requires at least one of "
                "recency_filter, lifecycle_filter, attribute_filter",
            )
        matched, _scanned = await _scan_users_for_filters(
            r,
            brand_id=body.brand_id,
            recency_filter=body.recency_filter,
            lifecycle_filter=body.lifecycle_filter,
            attribute_filter=body.attribute_filter,
            limit=FILTER_MAX_SCAN,
        )
        all_uids.update(matched)
        filter_spec = {
            "recency_filter": body.recency_filter,
            "lifecycle_filter": body.lifecycle_filter,
            "attribute_filter": body.attribute_filter,
        }

    payload: dict[str, str] = {
        "audience_id": aid,
        "brand_id": body.brand_id,
        "name": body.name,
        "source": body.source,
        "description": body.description or "",
        "is_lookalike": "false",
        "lookalike_seed": "",
        "similarity": "0",
        "created_at": str(_now()),
        "last_updated": str(_now()),
        "status": STATUS_READY,
        "filter_spec": json.dumps(filter_spec) if filter_spec else "",
    }

    pipe = r.pipeline()
    pipe.hset(_ak(aid), mapping=payload)
    pipe.sadd(_bak(body.brand_id), aid)
    if unmatched_emails:
        pipe.sadd(_pek(aid), *unmatched_emails)
    if unmatched_phones:
        pipe.sadd(_ppk(aid), *unmatched_phones)
    # Tier-quota counter — INCR audiences_count for this brand.
    pipe.incr(f"brand:{body.brand_id}:audiences_count")
    await pipe.execute()

    added = await _add_members(r, aid, all_uids)
    size = await r.scard(_mk(aid))
    await r.hset(_ak(aid), mapping={"size": str(size)})
    await _record_growth(r, aid, int(size))

    logger.info(
        "audience created aid=%s brand=%s source=%s size=%d (matched=%d, "
        "unmatched_email=%d, unmatched_phone=%d)",
        aid, body.brand_id, body.source, added,
        len(matched_email_uids) + len(matched_phone_uids),
        len(unmatched_emails), len(unmatched_phones),
    )

    return AudienceCreateResponse(
        audience_id=aid, size=int(size), status=STATUS_READY
    )


@router.post("/filter/preview")
async def filter_preview(
    body: FilterPreview,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Estimate audience size for a filter spec without persisting.

    Useful before saving — merchants see a live count + sample as they
    tweak the filter. Scans up to ``limit`` users; ``estimated_size`` is
    the count actually matched within that scan window (a lower bound for
    very large platforms).
    """
    if not any(
        [body.recency_filter, body.lifecycle_filter, body.attribute_filter]
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="at least one filter (recency/lifecycle/attribute) required",
        )
    matched, scanned = await _scan_users_for_filters(
        r,
        brand_id=body.brand_id,
        recency_filter=body.recency_filter,
        lifecycle_filter=body.lifecycle_filter,
        attribute_filter=body.attribute_filter,
        limit=body.limit,
    )
    return {
        "brand_id": body.brand_id,
        "estimated_size": len(matched),
        "scanned": scanned,
        "scan_cap": FILTER_MAX_SCAN,
        "sample_user_ids": matched[: min(len(matched), body.limit)],
    }


@router.post("/{audience_id}/append")
async def append_audience(
    audience_id: str,
    body: AudienceAppend,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Append more members to an existing audience."""
    if not await r.exists(_ak(audience_id)):
        raise HTTPException(status_code=404, detail="audience not found")

    matched_email_uids, unmatched_emails = await _resolve_email_hashes(
        r, body.emails_sha256
    )
    matched_phone_uids, unmatched_phones = await _resolve_phone_hashes(
        r, body.phones_sha256
    )
    all_uids: set[str] = set()
    all_uids.update(body.user_ids)
    all_uids.update(matched_email_uids)
    all_uids.update(matched_phone_uids)

    added = await _add_members(r, audience_id, all_uids)

    pipe = r.pipeline()
    if unmatched_emails:
        pipe.sadd(_pek(audience_id), *unmatched_emails)
    if unmatched_phones:
        pipe.sadd(_ppk(audience_id), *unmatched_phones)
    await pipe.execute()

    size = await r.scard(_mk(audience_id))
    await r.hset(
        _ak(audience_id),
        mapping={"size": str(size), "last_updated": str(_now())},
    )
    await _record_growth(r, audience_id, int(size))

    return {
        "ok": True,
        "audience_id": audience_id,
        "added": added,
        "size": int(size),
        "unmatched_emails": len(unmatched_emails),
        "unmatched_phones": len(unmatched_phones),
    }


@router.get("/brand/{brand_id}")
async def list_brand_audiences(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """List audiences owned by a brand."""
    aids = await r.smembers(_bak(brand_id))
    out: list[dict[str, Any]] = []
    for aid in aids:
        raw = await r.hgetall(_ak(aid))
        if not raw:
            # Orphan reference — clean up lazily.
            await r.srem(_bak(brand_id), aid)
            continue
        size = await r.scard(_mk(aid))
        out.append({
            "audience_id": aid,
            "name": raw.get("name"),
            "source": raw.get("source"),
            "is_lookalike": raw.get("is_lookalike") == "true",
            "lookalike_seed": raw.get("lookalike_seed") or None,
            "similarity": int(raw.get("similarity", 0) or 0),
            "size": int(size),
            "status": raw.get("status", STATUS_READY),
            "created_at": float(raw.get("created_at", 0.0)),
            "last_updated": float(raw.get("last_updated", 0.0)),
        })
    out.sort(key=lambda a: a.get("created_at", 0.0), reverse=True)
    return {"brand_id": brand_id, "audiences": out, "count": len(out)}


@router.get("/{audience_id}/details")
async def audience_details(
    audience_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Full info, member count, growth chart placeholder."""
    raw = await r.hgetall(_ak(audience_id))
    if not raw:
        raise HTTPException(status_code=404, detail="audience not found")

    size = await r.scard(_mk(audience_id))
    pending_emails = await r.scard(_pek(audience_id))
    pending_phones = await r.scard(_ppk(audience_id))
    include_campaigns = list(await r.smembers(_aud_inc_camps(audience_id)))
    exclude_campaigns = list(await r.smembers(_aud_exc_camps(audience_id)))

    growth_raw = await r.lrange(_gk(audience_id), 0, -1)
    growth: list[dict[str, Any]] = []
    for pt in growth_raw:
        parsed = _safe_json_loads(pt, None)
        if isinstance(parsed, dict):
            growth.append(parsed)

    return {
        "audience_id": audience_id,
        "brand_id": raw.get("brand_id"),
        "name": raw.get("name"),
        "source": raw.get("source"),
        "description": raw.get("description") or "",
        "is_lookalike": raw.get("is_lookalike") == "true",
        "lookalike_seed": raw.get("lookalike_seed") or None,
        "similarity": int(raw.get("similarity", 0) or 0),
        "size": int(size),
        "pending_unmatched": {
            "emails": int(pending_emails),
            "phones": int(pending_phones),
        },
        "status": raw.get("status", STATUS_READY),
        "created_at": float(raw.get("created_at", 0.0)),
        "last_updated": float(raw.get("last_updated", 0.0)),
        "linked_campaigns": {
            "include": include_campaigns,
            "exclude": exclude_campaigns,
        },
        "growth": growth,
    }


@router.delete("/{audience_id}")
async def delete_audience(
    audience_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Remove an audience and detach it from every link."""
    raw = await r.hgetall(_ak(audience_id))
    if not raw:
        raise HTTPException(status_code=404, detail="audience not found")
    brand_id = raw.get("brand_id", "")

    # Clean reverse user → audience index (best-effort, SET may be large).
    members = await r.smembers(_mk(audience_id))
    include_campaigns = await r.smembers(_aud_inc_camps(audience_id))
    exclude_campaigns = await r.smembers(_aud_exc_camps(audience_id))

    pipe = r.pipeline()
    for uid in members:
        pipe.srem(_uak(uid), audience_id)
    for cid in include_campaigns:
        pipe.srem(_camp_inc_auds(cid), audience_id)
    for cid in exclude_campaigns:
        pipe.srem(_camp_exc_auds(cid), audience_id)
    pipe.delete(
        _ak(audience_id),
        _mk(audience_id),
        _pek(audience_id),
        _ppk(audience_id),
        _gk(audience_id),
        _aud_inc_camps(audience_id),
        _aud_exc_camps(audience_id),
    )
    if brand_id:
        pipe.srem(_bak(brand_id), audience_id)
        # Tier-quota counter — DECR audiences_count.
        pipe.decr(f"brand:{brand_id}:audiences_count")
    await pipe.execute()

    # Clamp the counter at 0 (defensive against double-delete drift).
    if brand_id:
        try:
            cur = await r.get(f"brand:{brand_id}:audiences_count")
            if cur is not None and int(cur) < 0:
                await r.set(f"brand:{brand_id}:audiences_count", 0)
        except (TypeError, ValueError):
            pass

    return {"ok": True, "deleted": audience_id}


# ── Lookalike construction ───────────────────────────────────────────────


def _feature_similarity(
    centroid: dict[str, Any],
    profile: dict[str, Any],
) -> float:
    """Return a [0..1] similarity score of profile vs seed centroid.

    Components (each contributes to final score):
      * country match against centroid country distribution
      * age band proximity (|Δ| ≤ 5 → full; ≤ 10 → half; else zero)
      * interest tag jaccard
      * gender histogram match
    """
    score = 0.0
    weight = 0.0

    # Country.
    countries: dict[str, int] = centroid.get("country_hist") or {}
    if countries:
        total = sum(countries.values()) or 1
        c = profile.get("country")
        share = (countries.get(c, 0) / total) if c else 0.0
        score += share * 0.4
        weight += 0.4

    # Age proximity.
    mean_age = centroid.get("mean_age")
    if mean_age:
        try:
            age = int(profile.get("age", 0))
        except (TypeError, ValueError):
            age = 0
        if age > 0:
            delta = abs(age - mean_age)
            if delta <= 5:
                age_score = 1.0
            elif delta <= 10:
                age_score = 0.5
            elif delta <= 20:
                age_score = 0.2
            else:
                age_score = 0.0
            score += age_score * 0.2
            weight += 0.2

    # Interests jaccard.
    seed_tags: set[str] = set(centroid.get("interest_tags") or [])
    if seed_tags:
        raw_int = profile.get("interests", "")
        if isinstance(raw_int, str) and raw_int.startswith("["):
            have = set(_safe_json_loads(raw_int, []))
        elif isinstance(raw_int, str):
            have = {s.strip() for s in raw_int.split(",") if s.strip()}
        else:
            have = set()
        if have:
            inter = len(seed_tags & have)
            union = len(seed_tags | have)
            jacc = inter / union if union else 0.0
            score += jacc * 0.3
            weight += 0.3

    # Gender.
    gender_hist: dict[str, int] = centroid.get("gender_hist") or {}
    if gender_hist:
        total = sum(gender_hist.values()) or 1
        g = profile.get("gender")
        share = (gender_hist.get(g, 0) / total) if g else 0.0
        score += share * 0.1
        weight += 0.1

    return (score / weight) if weight > 0 else 0.0


async def _build_centroid(
    r: aioredis.Redis,
    seed_aid: str,
    sample_cap: int = 500,
) -> tuple[dict[str, Any], int]:
    """Build a feature centroid from a sample of seed members.

    Returns (centroid_dict, sampled_count). If the seed is very large,
    we sample ``sample_cap`` random members via SRANDMEMBER.
    """
    seed_size = await r.scard(_mk(seed_aid))
    if seed_size == 0:
        return {}, 0

    if seed_size > sample_cap:
        members = await r.srandmember(_mk(seed_aid), sample_cap)
    else:
        members = list(await r.smembers(_mk(seed_aid)))

    country_hist: dict[str, int] = {}
    gender_hist: dict[str, int] = {}
    interest_counts: dict[str, int] = {}
    ages: list[int] = []
    sampled = 0

    for uid in members:
        prof = await r.hgetall(f"user:{uid}")
        if not prof:
            continue
        sampled += 1
        c = prof.get("country")
        if c:
            country_hist[c] = country_hist.get(c, 0) + 1
        g = prof.get("gender")
        if g:
            gender_hist[g] = gender_hist.get(g, 0) + 1
        try:
            age = int(prof.get("age", 0))
            if age > 0:
                ages.append(age)
        except (TypeError, ValueError):
            pass
        raw_int = prof.get("interests", "")
        if raw_int.startswith("["):
            tags = _safe_json_loads(raw_int, [])
        else:
            tags = [s.strip() for s in raw_int.split(",") if s.strip()]
        for t in tags:
            interest_counts[t] = interest_counts.get(t, 0) + 1

    # Top-K interest tags as the seed signature.
    top_tags = sorted(
        interest_counts.items(), key=lambda kv: kv[1], reverse=True
    )[:15]

    centroid = {
        "country_hist": country_hist,
        "gender_hist": gender_hist,
        "mean_age": (sum(ages) / len(ages)) if ages else None,
        "interest_tags": [t for t, _ in top_tags],
    }
    return centroid, sampled


async def _scan_candidates(
    r: aioredis.Redis,
    countries_filter: list[str],
    exclude_members: set[str],
    cap: int,
) -> list[str]:
    """SCAN ``user:*`` profile keys; return up to ``cap`` candidate uids."""
    out: list[str] = []
    cursor = 0
    visited = 0
    cf = set(countries_filter or [])
    while visited < cap * 4 and len(out) < cap:
        cursor, batch = await r.scan(cursor=cursor, match="user:*", count=200)
        for k in batch:
            visited += 1
            if k.count(":") != 1:
                continue
            uid = k.split(":", 1)[1]
            if uid in exclude_members:
                continue
            if cf:
                # Cheap pre-filter without an extra HGETALL: read just country.
                c = await r.hget(k, "country")
                if c not in cf:
                    continue
            out.append(uid)
            if len(out) >= cap:
                break
        if cursor == 0:
            break
    return out


@router.post("/{audience_id}/lookalike", response_model=AudienceCreateResponse)
async def create_lookalike(
    audience_id: str,
    body: LookalikeCreate,
    r: aioredis.Redis = Depends(get_redis),
) -> AudienceCreateResponse:
    """Build a lookalike audience seeded by ``audience_id``."""
    seed = await r.hgetall(_ak(audience_id))
    if not seed:
        raise HTTPException(status_code=404, detail="seed audience not found")
    if seed.get("is_lookalike") == "true":
        raise HTTPException(
            status_code=409,
            detail="cannot build lookalike from another lookalike",
        )

    # ── Tier-quota gate (audiences) ────────────────────────────────────
    # Lookalikes are a new persisted audience, so the same quota applies.
    try:
        from app.routers.brand_subscriptions import check_quota
        allowed, info = await check_quota(body.brand_id, "audiences", r)
        if not allowed:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "tier_limit_reached",
                    "message": (
                        f"Your {info['tier']} tier allows "
                        f"{info['limit']} custom audiences. Upgrade to "
                        f"{info['upgrade_required_to']} for more."
                    ),
                    "tier": info["tier"],
                    "current": info["current"],
                    "limit": info["limit"],
                    "upgrade_required_to": info["upgrade_required_to"],
                },
            )
    except HTTPException:
        raise
    except (ImportError, ValueError):
        pass

    seed_size = await r.scard(_mk(audience_id))
    if seed_size == 0:
        raise HTTPException(
            status_code=422,
            detail="seed audience is empty; cannot build a lookalike",
        )

    centroid, sampled = await _build_centroid(r, audience_id)
    if sampled == 0:
        raise HTTPException(
            status_code=422,
            detail="seed members have no resolvable profiles",
        )

    seed_members: set[str] = set(await r.smembers(_mk(audience_id)))

    # Target size of the lookalike, capped on the platform sample cap.
    mult = LOOKALIKE_SIZE_MULT.get(body.similarity, 5.0)
    target_size = min(int(seed_size * mult), LOOKALIKE_MAX_SCAN)
    score_floor = LOOKALIKE_SCORE_FLOOR.get(body.similarity, 0.4)

    candidates = await _scan_candidates(
        r,
        countries_filter=body.countries,
        exclude_members=seed_members,
        cap=LOOKALIKE_MAX_SCAN,
    )

    scored: list[tuple[float, str]] = []
    for uid in candidates:
        prof = await r.hgetall(f"user:{uid}")
        if not prof:
            continue
        s = _feature_similarity(centroid, prof)
        if s >= score_floor:
            scored.append((s, uid))

    scored.sort(key=lambda kv: kv[0], reverse=True)
    chosen = scored[:target_size]

    # Aggregate similarity score = mean of selected (defensive against 0).
    aggregate = (
        sum(s for s, _ in chosen) / len(chosen) if chosen else 0.0
    )

    new_aid = f"aud_{uuid4().hex[:16]}"
    payload = {
        "audience_id": new_aid,
        "brand_id": body.brand_id,
        "name": body.name or f"Lookalike of {seed.get('name', audience_id)}",
        "source": "lookalike",
        "description": (
            f"Lookalike of {audience_id} @ similarity={body.similarity}"
        ),
        "is_lookalike": "true",
        "lookalike_seed": audience_id,
        "similarity": str(body.similarity),
        "similarity_score": f"{aggregate:.4f}",
        "countries": json.dumps(body.countries or []),
        "created_at": str(_now()),
        "last_updated": str(_now()),
        "status": STATUS_READY,
    }

    pipe = r.pipeline()
    pipe.hset(_ak(new_aid), mapping=payload)
    pipe.sadd(_bak(body.brand_id), new_aid)
    # Tier-quota counter — INCR audiences_count for this brand.
    pipe.incr(f"brand:{body.brand_id}:audiences_count")
    await pipe.execute()

    if chosen:
        await _add_members(r, new_aid, [uid for _, uid in chosen])

    final_size = await r.scard(_mk(new_aid))
    await r.hset(_ak(new_aid), mapping={"size": str(final_size)})
    await _record_growth(r, new_aid, int(final_size))

    logger.info(
        "lookalike built new=%s seed=%s sim=%d size=%d/%d score=%.3f",
        new_aid, audience_id, body.similarity,
        int(final_size), target_size, aggregate,
    )

    return AudienceCreateResponse(
        audience_id=new_aid,
        size=int(final_size),
        status=STATUS_READY,
    )


# ── Campaign linkage ─────────────────────────────────────────────────────


async def _ensure_audience_and_campaign(
    r: aioredis.Redis, aid: str, cid: str
) -> None:
    if not await r.exists(_ak(aid)):
        raise HTTPException(status_code=404, detail="audience not found")
    if not await r.exists(f"campaign:{cid}"):
        raise HTTPException(status_code=404, detail="campaign not found")


@router.post("/{audience_id}/exclude-in-campaign")
async def link_exclude(
    audience_id: str,
    body: CampaignLink,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Attach this audience as an *exclusion* for the campaign."""
    await _ensure_audience_and_campaign(r, audience_id, body.campaign_id)
    pipe = r.pipeline()
    pipe.sadd(_aud_exc_camps(audience_id), body.campaign_id)
    pipe.sadd(_camp_exc_auds(body.campaign_id), audience_id)
    # Remove from include if both were set — exclusion wins by intent.
    pipe.srem(_aud_inc_camps(audience_id), body.campaign_id)
    pipe.srem(_camp_inc_auds(body.campaign_id), audience_id)
    await pipe.execute()
    return {
        "ok": True,
        "audience_id": audience_id,
        "campaign_id": body.campaign_id,
        "link": "exclude",
    }


@router.post("/{audience_id}/target-in-campaign")
async def link_include(
    audience_id: str,
    body: CampaignLink,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Attach this audience as an *inclusion* for the campaign."""
    await _ensure_audience_and_campaign(r, audience_id, body.campaign_id)
    pipe = r.pipeline()
    pipe.sadd(_aud_inc_camps(audience_id), body.campaign_id)
    pipe.sadd(_camp_inc_auds(body.campaign_id), audience_id)
    await pipe.execute()
    return {
        "ok": True,
        "audience_id": audience_id,
        "campaign_id": body.campaign_id,
        "link": "include",
    }


# ── Membership lookups ───────────────────────────────────────────────────


@router.post("/check")
async def check_membership(
    body: MembershipCheck,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Is ``user_id`` in ``audience_id``?"""
    member = await r.sismember(_mk(body.audience_id), body.user_id)
    return {
        "user_id": body.user_id,
        "audience_id": body.audience_id,
        "member": bool(member),
    }


@router.get("/user/{user_id}/memberships")
async def user_memberships(
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """All audiences the user belongs to — used by the auction at bid time."""
    aids = list(await r.smembers(_uak(user_id)))
    details: list[dict[str, Any]] = []
    for aid in aids:
        raw = await r.hgetall(_ak(aid))
        if not raw:
            await r.srem(_uak(user_id), aid)
            continue
        details.append({
            "audience_id": aid,
            "brand_id": raw.get("brand_id"),
            "name": raw.get("name"),
            "is_lookalike": raw.get("is_lookalike") == "true",
        })
    return {
        "user_id": user_id,
        "audience_ids": [d["audience_id"] for d in details],
        "audiences": details,
        "count": len(details),
    }


# ── Public helpers consumed by auction.py ────────────────────────────────


async def get_user_audience_memberships(
    user_id: str, r: aioredis.Redis
) -> set[str]:
    """Returns the set of ``audience_ids`` the user belongs to.

    Cheap (one SMEMBERS). The auction calls this once per bid to filter
    campaigns by include/exclude linkage.
    """
    if not user_id:
        return set()
    members = await r.smembers(_uak(user_id))
    return set(members or [])


async def _audience_dynamic_match(
    r: aioredis.Redis, audience_id: str, user_id: str
) -> bool:
    """For ``source=filter`` audiences, re-evaluate the persisted filter
    spec against the user's current state. Returns False for non-filter
    audiences (caller should fall back to SET membership for those).
    """
    raw = await r.hgetall(_ak(audience_id))
    if not raw or raw.get("source") != "filter":
        return False
    spec_raw = raw.get("filter_spec")
    if not spec_raw:
        return False
    spec = _safe_json_loads(spec_raw, None)
    if not isinstance(spec, dict):
        return False
    return await _evaluate_filters_for_user(
        r,
        user_id=user_id,
        brand_id=raw.get("brand_id") or None,
        recency_filter=spec.get("recency_filter"),
        lifecycle_filter=spec.get("lifecycle_filter"),
        attribute_filter=spec.get("attribute_filter"),
    )


async def campaign_audience_matches(
    campaign_id: str, user_id: str, r: aioredis.Redis
) -> bool:
    """True iff the user passes this campaign's audience gates.

    Rules:
      * If the campaign has *include* audiences, the user must be a
        member of at least one.
      * The user must NOT be a member of any *exclude* audience.
      * Campaigns with no audience linkage are unaffected (pass-through).

    Filter-based audiences are re-evaluated dynamically against the user's
    current attribute / lifecycle / journey state (so a user joins/leaves
    automatically as their state changes — no rescan needed).
    """
    include_set = await r.smembers(_camp_inc_auds(campaign_id)) or set()
    exclude_set = await r.smembers(_camp_exc_auds(campaign_id)) or set()

    if not include_set and not exclude_set:
        return True

    user_auds = await get_user_audience_memberships(user_id, r)

    # Exclusion check (static membership first, then dynamic filter).
    if exclude_set:
        if user_auds & set(exclude_set):
            return False
        for aid in exclude_set:
            if aid in user_auds:
                continue
            if await _audience_dynamic_match(r, aid, user_id):
                return False

    if include_set:
        if user_auds & set(include_set):
            return True
        for aid in include_set:
            if aid in user_auds:
                return True
            if await _audience_dynamic_match(r, aid, user_id):
                return True
        return False
    return True
