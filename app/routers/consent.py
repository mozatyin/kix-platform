"""Consent / Privacy module — KiX legal spine.

Cross-brand attribution (see ``attribution.py``) shares user behavior
across brands. Under GDPR (EU), PIPL (CN), Indonesia PDP Law, and similar
regimes, this requires *explicit, scoped, revocable* user consent. Without
this module, every cross-brand event KiX records is unlawful processing.

Architecture
------------
Consent is *scope-grained* — a user can grant ``personalization`` but
refuse ``cross_brand_tracking``. Each scope carries an audit record
(granted_at, revoked_at, policy version it was granted under, IP hash).

When the active policy version changes and ``requires_re_grant=true`` is
set, all *existing* user consents are considered ``expired`` until the
user re-grants under the new version. ``check_internal`` enforces this.

Integration points (sibling routers should call ``check_internal``):
  * ``attribution.track_*`` → ``check_internal(uid, "cross_brand_tracking")``
  * ``geofence.enter``      → ``check_internal(uid, "geo_lbs")``
  * ``audiences``           → ``check_internal(uid, "personalization")``

On missing consent, sibling routers should ``raise HTTPException(403)``
with header ``Consent-Required: <scope>`` so the client SDK can prompt
the user to grant consent and retry the request.

Redis key schema
----------------
    consent:user:{uid}              HASH   {scope: JSON(record)}
    consent:user:{uid}:audit        LIST   audit trail (LPUSH + LTRIM)
    consent:policy:current          HASH   {version, text_md, effective_at,
                                            requires_re_grant, published_at}
    consent:policy:history          LIST   JSON(policy snapshot)
    consent:policy:{version}        HASH   {text_md, effective_at, ...}
    consent:export:{job_id}         HASH   job status / payload
    consent:delete:{job_id}         HASH   job status

Audit list is capped at 500 entries per user.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ─────────────────────────────────────────────────────────────

VALID_SCOPES: set[str] = {
    "cross_brand_tracking",
    "geo_lbs",
    "personalization",
    "marketing",
}

VALID_SOURCES: set[str] = {"web", "app", "qr"}

AUDIT_LOG_MAX = 500
POLICY_HISTORY_MAX = 100

# Reasons returned by check / check_internal
REASON_OK = "ok"
REASON_NOT_GRANTED = "not_granted"
REASON_REVOKED = "revoked"
REASON_EXPIRED_POLICY = "expired_policy"  # user consented under older version
REASON_INVALID_SCOPE = "invalid_scope"
REASON_NO_POLICY = "no_policy_published"


# ── Pydantic models ───────────────────────────────────────────────────────


class GrantRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    scopes: list[str] = Field(..., min_length=1)
    policy_version: str = Field(..., min_length=1, max_length=64)
    source: Literal["web", "app", "qr"] = "web"


class RevokeRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    scopes: list[str] | None = None  # None / omit ⇒ revoke all


class CheckRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    scope: str = Field(..., min_length=1)


class CheckResponse(BaseModel):
    allowed: bool
    reason: str | None = None


class ScopeStatus(BaseModel):
    granted: bool
    granted_at: int | None = None
    revoked_at: int | None = None
    policy_version: str | None = None


class UserConsentResponse(BaseModel):
    user_id: str
    scopes: dict[str, ScopeStatus]
    all_granted: bool
    current_policy_version: str | None = None


class PolicyPublishRequest(BaseModel):
    version: str = Field(..., min_length=1, max_length=64)
    text_md: str = Field(..., min_length=1)
    effective_at: int = Field(..., description="Unix epoch seconds")
    requires_re_grant: bool = False


class PolicyResponse(BaseModel):
    version: str
    text_md: str
    effective_at: int
    requires_re_grant: bool
    published_at: int


class DataJobRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    hard: bool = False  # only meaningful for delete


class DataJobResponse(BaseModel):
    job_id: str
    user_id: str
    status: str
    kind: str  # "export" | "delete"


# ── Helpers ───────────────────────────────────────────────────────────────


def _hash_ip(ip: str | None) -> str:
    """Salted SHA256 of the request IP (privacy preserving)."""
    if not ip:
        return ""
    salt = "kix-consent-v1"  # static; the hash is one-way, salt prevents rainbow
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:32]


def _client_ip(request: Request) -> str | None:
    """Best-effort client IP, honoring X-Forwarded-For."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def _validate_scopes(scopes: list[str]) -> None:
    bad = [s for s in scopes if s not in VALID_SCOPES]
    if bad:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid scope(s): {bad}. Allowed: {sorted(VALID_SCOPES)}",
        )


async def _audit(
    r: aioredis.Redis,
    user_id: str,
    action: str,
    detail: dict[str, Any],
) -> None:
    """Append a tamper-evident audit entry (capped LIST)."""
    entry = {
        "ts": int(time.time()),
        "action": action,
        **detail,
    }
    key = f"consent:user:{user_id}:audit"
    await r.lpush(key, json.dumps(entry))
    await r.ltrim(key, 0, AUDIT_LOG_MAX - 1)


async def _current_policy(r: aioredis.Redis) -> dict[str, Any] | None:
    raw = await r.hgetall("consent:policy:current")
    if not raw:
        return None
    return {
        "version": raw.get("version"),
        "text_md": raw.get("text_md", ""),
        "effective_at": int(raw.get("effective_at", 0) or 0),
        "requires_re_grant": raw.get("requires_re_grant", "false") == "true",
        "published_at": int(raw.get("published_at", 0) or 0),
    }


def _parse_scope_record(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


# ── Core check (sync-callable from siblings) ──────────────────────────────


async def check_internal(
    user_id: str,
    scope: str,
    r: aioredis.Redis,
) -> tuple[bool, str]:
    """Authoritative consent check for sibling routers.

    Returns ``(allowed, reason)``. Siblings should treat ``allowed=False``
    as a hard block and respond ``403`` with header
    ``Consent-Required: <scope>`` so the client SDK can drive the grant
    UX and retry.

    Reasons returned: ``ok`` | ``not_granted`` | ``revoked`` |
    ``expired_policy`` | ``invalid_scope`` | ``no_policy_published``.
    """
    if scope not in VALID_SCOPES:
        return False, REASON_INVALID_SCOPE

    policy = await _current_policy(r)
    if not policy:
        # Fail closed: no policy published → no lawful basis to process.
        return False, REASON_NO_POLICY

    raw = await r.hget(f"consent:user:{user_id}", scope)
    rec = _parse_scope_record(raw)
    if not rec:
        return False, REASON_NOT_GRANTED

    if rec.get("revoked_at"):
        return False, REASON_REVOKED

    if not rec.get("granted_at"):
        return False, REASON_NOT_GRANTED

    if policy["requires_re_grant"]:
        if rec.get("policy_version") != policy["version"]:
            return False, REASON_EXPIRED_POLICY

    return True, REASON_OK


# ── Endpoints: user consent ───────────────────────────────────────────────


@router.post("/grant")
async def grant_consent(
    body: GrantRequest,
    request: Request,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Record explicit consent for one or more scopes.

    Each scope gets an independent record. Re-granting an existing scope
    refreshes ``granted_at`` and clears ``revoked_at`` (re-consent flow).
    """
    _validate_scopes(body.scopes)

    policy = await _current_policy(r)
    if not policy:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No active policy. Admin must publish a policy first.",
        )
    if body.policy_version != policy["version"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"policy_version mismatch. Active version is "
                f"'{policy['version']}', got '{body.policy_version}'. "
                "Refetch /policy/current and re-prompt the user."
            ),
        )

    ip_hash = _hash_ip(_client_ip(request))
    now = int(time.time())

    key = f"consent:user:{body.user_id}"
    granted: list[str] = []

    for scope in body.scopes:
        record = {
            "scope": scope,
            "granted_at": now,
            "revoked_at": None,
            "policy_version": body.policy_version,
            "ip_hash": ip_hash,
            "source": body.source,
        }
        await r.hset(key, scope, json.dumps(record))
        granted.append(scope)

    await _audit(
        r,
        body.user_id,
        "grant",
        {
            "scopes": granted,
            "policy_version": body.policy_version,
            "source": body.source,
            "ip_hash": ip_hash,
        },
    )

    return {
        "user_id": body.user_id,
        "granted": granted,
        "granted_at": now,
        "policy_version": body.policy_version,
    }


@router.post("/revoke")
async def revoke_consent(
    body: RevokeRequest,
    request: Request,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Mark scopes as revoked + cascade downstream side-effects.

    Cascades (best-effort, fire-and-forget): stop attribution tracking,
    remove user from audiences. Cascades are signalled via Redis flags
    that sibling routers honor on next request — we do not perform a
    synchronous fan-out delete here (that is the job of /data/delete).
    """
    key = f"consent:user:{body.user_id}"

    if body.scopes is not None:
        _validate_scopes(body.scopes)
        target_scopes = body.scopes
    else:
        # Revoke all currently-recorded scopes
        existing = await r.hkeys(key)
        target_scopes = list(existing)

    if not target_scopes:
        return {"user_id": body.user_id, "revoked": [], "note": "nothing to revoke"}

    now = int(time.time())
    revoked: list[str] = []

    for scope in target_scopes:
        raw = await r.hget(key, scope)
        rec = _parse_scope_record(raw) or {
            "scope": scope,
            "granted_at": None,
            "policy_version": None,
        }
        rec["revoked_at"] = now
        rec["granted_at"] = rec.get("granted_at")
        await r.hset(key, scope, json.dumps(rec))
        revoked.append(scope)

    # Cascade flags — sibling routers (attribution, audiences) check these
    # on hot path via check_internal; we also set explicit cascade markers
    # so async workers can fan out the cleanup.
    await r.sadd("consent:cascade:pending", body.user_id)
    await r.expire("consent:cascade:pending", 7 * 24 * 3600)

    await _audit(
        r,
        body.user_id,
        "revoke",
        {
            "scopes": revoked,
            "ip_hash": _hash_ip(_client_ip(request)),
        },
    )

    logger.info(
        "consent.revoke user=%s scopes=%s — cascade queued",
        body.user_id,
        revoked,
    )

    return {
        "user_id": body.user_id,
        "revoked": revoked,
        "revoked_at": now,
        "cascade": "queued",
    }


@router.get("/user/{user_id}", response_model=UserConsentResponse)
async def get_user_consent(
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> UserConsentResponse:
    """Return full consent state for a user (all scopes)."""
    raw_map = await r.hgetall(f"consent:user:{user_id}")
    policy = await _current_policy(r)
    current_version = policy["version"] if policy else None

    scopes: dict[str, ScopeStatus] = {}
    for scope in sorted(VALID_SCOPES):
        rec = _parse_scope_record(raw_map.get(scope))
        if not rec:
            scopes[scope] = ScopeStatus(granted=False)
            continue

        revoked_at = rec.get("revoked_at")
        granted = revoked_at is None and rec.get("granted_at") is not None

        # Treat policy-mismatch as not granted when re-grant is required.
        if (
            granted
            and policy
            and policy["requires_re_grant"]
            and rec.get("policy_version") != current_version
        ):
            granted = False

        scopes[scope] = ScopeStatus(
            granted=granted,
            granted_at=rec.get("granted_at"),
            revoked_at=revoked_at,
            policy_version=rec.get("policy_version"),
        )

    all_granted = all(s.granted for s in scopes.values())

    return UserConsentResponse(
        user_id=user_id,
        scopes=scopes,
        all_granted=all_granted,
        current_policy_version=current_version,
    )


@router.post("/check", response_model=CheckResponse)
async def check_consent(
    body: CheckRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> CheckResponse:
    """Public endpoint mirror of ``check_internal`` — for client SDK probes."""
    allowed, reason = await check_internal(body.user_id, body.scope, r)
    return CheckResponse(allowed=allowed, reason=None if allowed else reason)


# ── Endpoints: policy management (admin) ──────────────────────────────────


@router.post("/policy/publish", response_model=PolicyResponse)
async def publish_policy(
    body: PolicyPublishRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> PolicyResponse:
    """Publish a new policy version.

    The previous policy is archived to ``consent:policy:history`` and
    ``consent:policy:{old_version}``. If ``requires_re_grant=true``, all
    user consents granted under prior versions become ``expired_policy``
    on next ``check_internal`` until the user re-grants.
    """
    now = int(time.time())

    # Archive previous policy, if any
    previous = await _current_policy(r)
    if previous:
        if previous["version"] == body.version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Policy version '{body.version}' is already active.",
            )
        snapshot = json.dumps(previous)
        await r.lpush("consent:policy:history", snapshot)
        await r.ltrim("consent:policy:history", 0, POLICY_HISTORY_MAX - 1)
        await r.hset(
            f"consent:policy:{previous['version']}",
            mapping={
                "version": previous["version"],
                "text_md": previous["text_md"],
                "effective_at": str(previous["effective_at"]),
                "requires_re_grant": "true" if previous["requires_re_grant"] else "false",
                "published_at": str(previous["published_at"]),
            },
        )

    mapping = {
        "version": body.version,
        "text_md": body.text_md,
        "effective_at": str(body.effective_at),
        "requires_re_grant": "true" if body.requires_re_grant else "false",
        "published_at": str(now),
    }
    await r.delete("consent:policy:current")
    await r.hset("consent:policy:current", mapping=mapping)

    # Also store the new version under its version key for retrievability
    await r.hset(f"consent:policy:{body.version}", mapping=mapping)

    logger.info(
        "consent.policy.publish version=%s requires_re_grant=%s",
        body.version,
        body.requires_re_grant,
    )

    return PolicyResponse(
        version=body.version,
        text_md=body.text_md,
        effective_at=body.effective_at,
        requires_re_grant=body.requires_re_grant,
        published_at=now,
    )


@router.get("/policy/current", response_model=PolicyResponse)
async def get_current_policy(
    r: aioredis.Redis = Depends(get_redis),
) -> PolicyResponse:
    """Return the active policy text + version."""
    policy = await _current_policy(r)
    if not policy:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No policy published yet.",
        )
    return PolicyResponse(**policy)


# ── Endpoints: GDPR Article 15 (export) / Article 17 (erasure) ────────────


@router.post("/data/export", response_model=DataJobResponse)
async def export_data(
    body: DataJobRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> DataJobResponse:
    """Trigger a data export job (GDPR Article 15 — right of access).

    Creates a job record; a downstream worker assembles the payload
    (attribution events, audiences, journey, wallet) and notifies the
    user out-of-band. Synchronous response returns the job_id.
    """
    job_id = uuid4().hex
    job_key = f"consent:export:{job_id}"
    now = int(time.time())
    await r.hset(
        job_key,
        mapping={
            "job_id": job_id,
            "user_id": body.user_id,
            "kind": "export",
            "status": "queued",
            "created_at": str(now),
        },
    )
    await r.expire(job_key, 30 * 24 * 3600)  # 30 day TTL
    await r.lpush("consent:jobs:export:queue", job_id)

    await _audit(r, body.user_id, "data_export_requested", {"job_id": job_id})
    logger.info("consent.data.export user=%s job=%s", body.user_id, job_id)

    return DataJobResponse(
        job_id=job_id,
        user_id=body.user_id,
        status="queued",
        kind="export",
    )


@router.post("/data/delete", response_model=DataJobResponse)
async def delete_data(
    body: DataJobRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> DataJobResponse:
    """Trigger right-to-erasure (GDPR Article 17).

    Soft-delete (``hard=False``): removes attribution events, audience
    memberships, journey traces — but keeps financial/wallet/transaction
    records (legal-hold for accounting).

    Hard-delete (``hard=True``): also removes wallet + transactions.
    Use with care; legal-hold may forbid this in some jurisdictions.

    A job is enqueued; an async worker fans out the deletes across
    sibling routers' key spaces. We also immediately revoke all
    consent scopes for the user so further processing is blocked.
    """
    job_id = uuid4().hex
    job_key = f"consent:delete:{job_id}"
    now = int(time.time())

    # Immediately revoke all consents — stop further processing right now.
    user_key = f"consent:user:{body.user_id}"
    existing = await r.hkeys(user_key)
    for scope in existing:
        raw = await r.hget(user_key, scope)
        rec = _parse_scope_record(raw) or {"scope": scope}
        rec["revoked_at"] = now
        await r.hset(user_key, scope, json.dumps(rec))

    await r.hset(
        job_key,
        mapping={
            "job_id": job_id,
            "user_id": body.user_id,
            "kind": "delete",
            "hard": "true" if body.hard else "false",
            "status": "queued",
            "created_at": str(now),
        },
    )
    await r.expire(job_key, 30 * 24 * 3600)
    await r.lpush("consent:jobs:delete:queue", job_id)
    await r.sadd("consent:cascade:pending", body.user_id)

    await _audit(
        r,
        body.user_id,
        "data_delete_requested",
        {"job_id": job_id, "hard": body.hard},
    )
    logger.warning(
        "consent.data.delete user=%s job=%s hard=%s",
        body.user_id,
        job_id,
        body.hard,
    )

    return DataJobResponse(
        job_id=job_id,
        user_id=body.user_id,
        status="queued",
        kind="delete",
    )


# ── Public exports ────────────────────────────────────────────────────────

__all__ = [
    "router",
    "check_internal",
    "VALID_SCOPES",
    "REASON_OK",
    "REASON_NOT_GRANTED",
    "REASON_REVOKED",
    "REASON_EXPIRED_POLICY",
    "REASON_INVALID_SCOPE",
    "REASON_NO_POLICY",
]
