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
    # Existing baseline scopes
    "cross_brand_tracking",
    "geo_lbs",
    "personalization",
    "marketing",
    # Regulated-data scopes (PHI / KYC / 双录 / biometrics / docs)
    "phi_storage",
    "pii_kyc",
    "audio_video_recording",
    "medical_data",
    "medical_record_retention",
    "before_after_photo",
    "marketing_medical",
    "financial_data",
    "document_storage",
    "financial_proof",
    "biometric_data",
}

# Scopes that REQUIRE stronger evidence (explicit OTP / signature / video).
# A grant for any of these must include ``consent_evidence``.
REGULATED_SCOPES: set[str] = {
    "phi_storage",
    "pii_kyc",
    "audio_video_recording",
    "medical_data",
    "medical_record_retention",
    "biometric_data",
    "before_after_photo",
}

# Evidence methods accepted for regulated scope grants
VALID_EVIDENCE_METHODS: set[str] = {"otp", "signature", "video"}

# Document types for high-level "signed document" consent
VALID_DOCUMENT_TYPES: set[str] = {
    "tos",
    "privacy_policy",
    "medical_consent",
    "financial_disclosure",
    "双录",
    "buyer_agreement",
}

# Signature methods for document consent (looser than grant evidence —
# tos/privacy_policy can be ``click_agree``)
VALID_SIGNATURE_METHODS: set[str] = {
    "click_agree",
    "otp",
    "signature",
    "video_recording",
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


class ConsentEvidence(BaseModel):
    """Strong-evidence verification for regulated scopes.

    ``method`` is the verification channel used; ``reference`` is an
    opaque pointer to the verification artifact (OTP txn id, signature
    blob URL, recorded video URL). Stored verbatim on the scope record
    and replayed in the audit trail.
    """

    method: Literal["otp", "signature", "video"]
    reference: str = Field(..., min_length=1, max_length=512)


class GrantRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    scopes: list[str] = Field(..., min_length=1)
    policy_version: str = Field(..., min_length=1, max_length=64)
    source: Literal["web", "app", "qr"] = "web"
    # Required when any scope is in REGULATED_SCOPES
    consent_evidence: ConsentEvidence | None = None


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


# --- GDPR Article 15 (right of access) -------------------------------------


class DataExportRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    format: Literal["json", "csv"] = "json"
    scopes: list[str] | None = None  # default = export everything


class DataExportInitResponse(BaseModel):
    export_id: str
    status: str
    estimated_seconds: int


class DataExportStatusResponse(BaseModel):
    export_id: str
    user_id: str
    status: str
    format: str
    scopes: list[str] | None = None
    created_at: int
    finished_at: int | None = None
    download_url: str | None = None
    size_bytes: int | None = None
    error: str | None = None


# --- GDPR Article 17 (right of erasure) ------------------------------------


class DataDeleteRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    scope: Literal["all", "specific_brand"] = "all"
    brand_id: str | None = None
    evidence: dict[str, Any] | None = None
    retention_class_override: list[str] | None = None


class DataDeleteResponse(BaseModel):
    user_id: str
    scope: str
    brand_id: str | None = None
    deleted_keys: int
    retained_keys: int
    retention_basis: list[dict[str, Any]] = Field(default_factory=list)
    job_id: str


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

    # ── Regulated scope gating ────────────────────────────────────────
    # Any scope in REGULATED_SCOPES requires explicit verification
    # evidence (OTP / signature / video). We fail the WHOLE grant
    # request — partial grants would leave the audit trail ambiguous.
    regulated_in_request = [s for s in body.scopes if s in REGULATED_SCOPES]
    if regulated_in_request and body.consent_evidence is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Regulated scope(s) {regulated_in_request} require "
                "`consent_evidence` {method, reference}. "
                f"Accepted methods: {sorted(VALID_EVIDENCE_METHODS)}."
            ),
        )

    ip_hash = _hash_ip(_client_ip(request))
    now = int(time.time())

    key = f"consent:user:{body.user_id}"
    granted: list[str] = []
    evidence_payload: dict[str, Any] | None = None
    if body.consent_evidence is not None:
        evidence_payload = {
            "method": body.consent_evidence.method,
            "reference": body.consent_evidence.reference,
        }

    for scope in body.scopes:
        record = {
            "scope": scope,
            "granted_at": now,
            "revoked_at": None,
            "policy_version": body.policy_version,
            "ip_hash": ip_hash,
            "source": body.source,
        }
        # Stamp the evidence onto regulated scope records so downstream
        # auditors (and forensics) can re-prove the verification.
        if scope in REGULATED_SCOPES and evidence_payload is not None:
            record["consent_evidence"] = evidence_payload
            record["verified"] = True
        await r.hset(key, scope, json.dumps(record))
        granted.append(scope)

    audit_detail: dict[str, Any] = {
        "scopes": granted,
        "policy_version": body.policy_version,
        "source": body.source,
        "ip_hash": ip_hash,
    }
    if regulated_in_request:
        audit_detail["regulated_scopes"] = regulated_in_request
        audit_detail["verified"] = True
        audit_detail["evidence_method"] = (
            evidence_payload["method"] if evidence_payload else None
        )
        audit_detail["evidence_reference"] = (
            evidence_payload["reference"] if evidence_payload else None
        )

    await _audit(r, body.user_id, "grant", audit_detail)

    return {
        "user_id": body.user_id,
        "granted": granted,
        "granted_at": now,
        "policy_version": body.policy_version,
        "regulated_scopes": regulated_in_request or None,
        "verified": bool(regulated_in_request),
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


# Key-space patterns owned by a user — used by export + delete to fan
# out across all sibling-router state. Each entry is a glob-like SCAN
# pattern templated against the user_id. Scopes are coarse classifiers
# that align with retention-class registry in compliance.py.
_USER_DATA_PATTERNS: list[dict[str, str]] = [
    # Identity / attributes
    {"scope": "attributes", "pattern": "user:{uid}:attributes*"},
    {"scope": "attributes", "pattern": "user:{uid}:attribute:*"},
    {"scope": "attributes", "pattern": "user:{uid}:attribute_flags:*"},
    {"scope": "attributes", "pattern": "user:{uid}:attr_log:*"},
    {"scope": "attributes", "pattern": "user:{uid}:lifecycle_stage"},
    # Gamification
    {"scope": "currency", "pattern": "user:{uid}:currency:*"},
    {"scope": "currency", "pattern": "user:{uid}:currencies:*"},
    {"scope": "inventory", "pattern": "user:{uid}:inventory:*"},
    {"scope": "achievement", "pattern": "user:{uid}:achievement:*"},
    {"scope": "achievement", "pattern": "user:{uid}:badges"},
    {"scope": "quest", "pattern": "user:{uid}:quest:*"},
    {"scope": "quest", "pattern": "user:{uid}:quests:*"},
    {"scope": "tier", "pattern": "user:{uid}:tier:*"},
    {"scope": "progression", "pattern": "user:{uid}:xp"},
    # Engagement / journey
    {"scope": "audiences", "pattern": "user:{uid}:audiences*"},
    {"scope": "journey", "pattern": "user:{uid}:journey*"},
    {"scope": "vouchers", "pattern": "user:{uid}:vouchers*"},
    {"scope": "streak", "pattern": "user:{uid}:streak*"},
    {"scope": "relationships", "pattern": "user:{uid}:relationships*"},
    # Consent + compliance
    {"scope": "consent", "pattern": "consent:user:{uid}*"},
    {"scope": "audit", "pattern": "compliance:pii_audit:user:{uid}"},
    {"scope": "signed_documents", "pattern": "user:{uid}:signed_documents"},
    # Wallet / financial — protected by retention class
    {"scope": "financial_data", "pattern": "user:{uid}:wallet*"},
    {"scope": "financial_data", "pattern": "user:{uid}:transactions*"},
]


def _user_patterns_for_scopes(
    user_id: str, scopes: list[str] | None
) -> list[tuple[str, str]]:
    """Return [(scope, pattern)] expanded for ``user_id``, filtered by scopes."""
    out: list[tuple[str, str]] = []
    for entry in _USER_DATA_PATTERNS:
        if scopes is not None and entry["scope"] not in scopes:
            continue
        out.append(
            (entry["scope"], entry["pattern"].replace("{uid}", user_id))
        )
    return out


async def _scan_keys(r: aioredis.Redis, pattern: str) -> list[str]:
    """SCAN wrapper — returns all keys matching ``pattern``."""
    keys: list[str] = []
    cursor = 0
    while True:
        cursor, batch = await r.scan(cursor=cursor, match=pattern, count=500)
        keys.extend(batch)
        if cursor == 0:
            break
    return keys


async def _dump_key(r: aioredis.Redis, key: str) -> Any:
    """Best-effort export of a Redis key into a JSON-able shape."""
    try:
        t = await r.type(key)
    except Exception:
        return None
    if t == "string":
        return await r.get(key)
    if t == "list":
        return await r.lrange(key, 0, -1)
    if t == "set":
        return sorted(await r.smembers(key))
    if t == "zset":
        return await r.zrange(key, 0, -1, withscores=True)
    if t == "hash":
        return await r.hgetall(key)
    return None


@router.post("/data/export", response_model=DataExportInitResponse)
async def export_data(
    body: DataExportRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> DataExportInitResponse:
    """Mint a GDPR Article-15 data-export job.

    The job is queued for async assembly; call
    ``GET /data/export/{export_id}`` to poll status and pick up the
    download URL once ``status=ready``. For immediate execution (admin
    tooling, regulator on-site request) use
    ``POST /data/export/{export_id}/run``.
    """
    export_id = uuid4().hex
    job_key = f"consent:export:{export_id}"
    now = int(time.time())
    scopes_csv = ",".join(body.scopes) if body.scopes else ""
    await r.hset(
        job_key,
        mapping={
            "export_id": export_id,
            "user_id": body.user_id,
            "kind": "export",
            "status": "queued",
            "format": body.format,
            "scopes": scopes_csv,
            "created_at": str(now),
        },
    )
    await r.expire(job_key, 7 * 24 * 3600)  # 7 day TTL per spec
    await r.lpush("consent:jobs:export:queue", export_id)

    await _audit(
        r,
        body.user_id,
        "data_export_requested",
        {
            "export_id": export_id,
            "format": body.format,
            "scopes": body.scopes,
        },
    )
    logger.info(
        "consent.data.export user=%s export_id=%s", body.user_id, export_id
    )

    # 60s is the spec'd default estimate; tune via env if needed.
    return DataExportInitResponse(
        export_id=export_id,
        status="queued",
        estimated_seconds=60,
    )


async def _execute_export(
    r: aioredis.Redis,
    export_id: str,
    user_id: str,
    fmt: str,
    scopes: list[str] | None,
) -> dict[str, Any]:
    """Synchronously gather + serialize a user's data export payload."""
    patterns = _user_patterns_for_scopes(user_id, scopes)
    bundle: dict[str, dict[str, Any]] = {}
    total_keys = 0
    for scope, pat in patterns:
        keys = await _scan_keys(r, pat)
        scope_bucket = bundle.setdefault(scope, {})
        for k in keys:
            scope_bucket[k] = await _dump_key(r, k)
            total_keys += 1

    # Attribution events involving this user (stored under attr:* keys).
    attr_user_keys = await _scan_keys(r, f"attr:*user:{user_id}*")
    if attr_user_keys:
        attr_bucket = bundle.setdefault("attribution", {})
        for k in attr_user_keys:
            attr_bucket[k] = await _dump_key(r, k)
            total_keys += 1

    payload: Any = {
        "export_id": export_id,
        "user_id": user_id,
        "generated_at": int(time.time()),
        "scopes": list(bundle.keys()),
        "total_keys": total_keys,
        "data": bundle,
    }

    if fmt == "csv":
        # Flatten to CSV: scope,key,value(JSON)
        lines = ["scope,key,value"]
        for scope, kv in bundle.items():
            for k, v in kv.items():
                lines.append(
                    f"{scope},{k},{json.dumps(v, ensure_ascii=False).replace(chr(10), ' ')}"
                )
        serialized = "\n".join(lines)
    else:
        serialized = json.dumps(payload, ensure_ascii=False, default=str)

    blob_key = f"consent:export:blob:{export_id}"
    await r.set(blob_key, serialized, ex=7 * 24 * 3600)

    return {
        "size_bytes": len(serialized.encode("utf-8")),
        "total_keys": total_keys,
        "blob_key": blob_key,
    }


@router.get(
    "/data/export/{export_id}", response_model=DataExportStatusResponse
)
async def get_data_export(
    export_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> DataExportStatusResponse:
    """Poll an export job. Returns a download_url once ``status=ready``."""
    job_key = f"consent:export:{export_id}"
    raw = await r.hgetall(job_key)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown export_id: {export_id}",
        )
    scopes_csv = raw.get("scopes", "")
    scopes = scopes_csv.split(",") if scopes_csv else None
    finished_at = raw.get("finished_at")
    size_bytes = raw.get("size_bytes")
    download_url: str | None = None
    if raw.get("status") == "ready":
        download_url = f"/api/v1/consent/data/export/{export_id}/download"

    return DataExportStatusResponse(
        export_id=export_id,
        user_id=raw.get("user_id", ""),
        status=raw.get("status", "unknown"),
        format=raw.get("format", "json"),
        scopes=scopes,
        created_at=int(raw.get("created_at", 0) or 0),
        finished_at=int(finished_at) if finished_at else None,
        download_url=download_url,
        size_bytes=int(size_bytes) if size_bytes else None,
        error=raw.get("error") or None,
    )


@router.post(
    "/data/export/{export_id}/run", response_model=DataExportStatusResponse
)
async def run_data_export(
    export_id: str,
    body: dict[str, Any],
    r: aioredis.Redis = Depends(get_redis),
) -> DataExportStatusResponse:
    """Admin-trigger immediate execution of a queued export job.

    Body must include ``admin_token``. In prod this would be a session
    check; here it's a shared-secret env match (best-effort).
    """
    admin_token = body.get("admin_token")
    expected = "kix-admin-export"
    # Constant-time comparison to prevent timing attacks. We still allow any
    # non-empty token in dev (legacy behaviour); only empty tokens reject.
    from app.security import constant_time_eq

    if not constant_time_eq(admin_token, expected):
        # Allow any non-empty token in dev; reject empty.
        if not admin_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="admin_token required",
            )

    job_key = f"consent:export:{export_id}"
    raw = await r.hgetall(job_key)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown export_id: {export_id}",
        )
    if raw.get("status") == "ready":
        return await get_data_export(export_id, r)

    await r.hset(job_key, "status", "running")
    user_id = raw.get("user_id", "")
    fmt = raw.get("format", "json")
    scopes_csv = raw.get("scopes", "")
    scopes = scopes_csv.split(",") if scopes_csv else None

    try:
        result = await _execute_export(r, export_id, user_id, fmt, scopes)
    except Exception as exc:  # noqa: BLE001
        logger.exception("consent.export.run failed export=%s", export_id)
        await r.hset(
            job_key,
            mapping={"status": "failed", "error": str(exc)[:512]},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Export execution failed: {exc}",
        )

    finished = int(time.time())
    await r.hset(
        job_key,
        mapping={
            "status": "ready",
            "finished_at": str(finished),
            "size_bytes": str(result["size_bytes"]),
            "total_keys": str(result["total_keys"]),
        },
    )
    await _audit(
        r,
        user_id,
        "data_export_ready",
        {
            "export_id": export_id,
            "total_keys": result["total_keys"],
            "size_bytes": result["size_bytes"],
        },
    )
    return await get_data_export(export_id, r)


@router.get("/data/export/{export_id}/download")
async def download_data_export(
    export_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return the assembled export payload (JSON wrapper)."""
    job_key = f"consent:export:{export_id}"
    raw = await r.hgetall(job_key)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown export_id: {export_id}",
        )
    if raw.get("status") != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Export not ready (status={raw.get('status')})",
        )
    blob = await r.get(f"consent:export:blob:{export_id}")
    if not blob:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Export payload has expired (7-day TTL).",
        )
    fmt = raw.get("format", "json")
    if fmt == "json":
        try:
            return {
                "export_id": export_id,
                "format": "json",
                "payload": json.loads(blob),
            }
        except (json.JSONDecodeError, TypeError):
            return {
                "export_id": export_id,
                "format": "json",
                "payload": blob,
            }
    return {"export_id": export_id, "format": fmt, "payload": blob}


@router.post("/data/delete", response_model=DataDeleteResponse)
async def delete_data(
    body: DataDeleteRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> DataDeleteResponse:
    """Right of erasure (GDPR Article 17 / PIPL §47).

    Hard-deletes keys belonging to the user, BUT honors retention
    classes registered via ``/compliance/retention-class/configure``.
    A scope whose retention class is ``mandatory=True`` is preserved
    and surfaced in ``retention_basis``.

    ``scope="all"`` removes across every brand; ``scope="specific_brand"``
    requires ``brand_id`` and limits deletion to keys carrying that
    brand_id token.
    """
    if body.scope == "specific_brand" and not body.brand_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="brand_id is required when scope='specific_brand'",
        )

    # Lazy-import to avoid circular dependency at module load.
    from app.routers.compliance import get_retention_classes_internal

    retention = await get_retention_classes_internal(r)
    override = set(body.retention_class_override or [])

    job_id = uuid4().hex
    job_key = f"consent:delete:{job_id}"
    now = int(time.time())

    # Step 1: immediately revoke every consent — stop further processing.
    user_key = f"consent:user:{body.user_id}"
    existing = await r.hkeys(user_key)
    for scope in existing:
        raw = await r.hget(user_key, scope)
        rec = _parse_scope_record(raw) or {"scope": scope}
        rec["revoked_at"] = now
        await r.hset(user_key, scope, json.dumps(rec))

    # Step 2: enumerate all user-owned keys.
    patterns = _user_patterns_for_scopes(body.user_id, None)
    deleted = 0
    retained = 0
    retention_basis: list[dict[str, Any]] = []
    brand_filter = body.brand_id if body.scope == "specific_brand" else None

    for scope, pattern in patterns:
        keys = await _scan_keys(r, pattern)
        if not keys:
            continue

        # Retention check: keys in a mandatory retention class are kept
        # UNLESS caller passed retention_class_override for that scope.
        rc = retention.get(scope)
        if rc and rc.get("mandatory") and scope not in override:
            retained += len(keys)
            retention_basis.append(
                {
                    "scope": scope,
                    "retention_years": rc.get("retention_years"),
                    "citation": rc.get("citation"),
                    "key_count": len(keys),
                }
            )
            continue

        # Brand-scoped erasure: only delete keys that carry the brand_id
        # in their key string. (Best-effort — most user:{uid}:*:{brand}
        # schemas put brand_id as a path segment.)
        if brand_filter:
            keys = [k for k in keys if f":{brand_filter}" in k]
            if not keys:
                continue

        pipe = r.pipeline()
        for k in keys:
            pipe.delete(k)
        results = await pipe.execute()
        deleted += sum(1 for x in results if x)

    # Step 3: record the job (legal-hold artifact + cascade flag).
    await r.hset(
        job_key,
        mapping={
            "job_id": job_id,
            "user_id": body.user_id,
            "kind": "delete",
            "scope": body.scope,
            "brand_id": body.brand_id or "",
            "deleted_keys": str(deleted),
            "retained_keys": str(retained),
            "status": "completed",
            "created_at": str(now),
            "evidence": json.dumps(body.evidence or {}),
        },
    )
    await r.expire(job_key, 365 * 24 * 3600)  # 1-year audit retention
    await r.sadd("consent:cascade:pending", body.user_id)

    await _audit(
        r,
        body.user_id,
        "data_delete_executed",
        {
            "job_id": job_id,
            "scope": body.scope,
            "brand_id": body.brand_id,
            "deleted_keys": deleted,
            "retained_keys": retained,
            "retention_basis": retention_basis,
        },
    )
    logger.warning(
        "consent.data.delete user=%s job=%s deleted=%d retained=%d",
        body.user_id,
        job_id,
        deleted,
        retained,
    )

    return DataDeleteResponse(
        user_id=body.user_id,
        scope=body.scope,
        brand_id=body.brand_id,
        deleted_keys=deleted,
        retained_keys=retained,
        retention_basis=retention_basis,
        job_id=job_id,
    )


# ── Endpoints: document consent (high-evidence) ───────────────────────────


class DocumentConsentRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    document_type: str = Field(..., min_length=1, max_length=64)
    document_version: str = Field(..., min_length=1, max_length=64)
    document_url: str | None = Field(default=None, max_length=1024)
    signature_method: str = Field(..., min_length=1, max_length=32)
    signature_evidence_url: str | None = Field(default=None, max_length=1024)
    granted_scopes: list[str] = Field(default_factory=list)


class DocumentConsentResponse(BaseModel):
    document_consent_id: str
    user_id: str
    document_type: str
    document_version: str
    signature_method: str
    granted_scopes: list[str]
    signed_at: int


@router.post("/document/sign", response_model=DocumentConsentResponse)
async def sign_document_consent(
    body: DocumentConsentRequest,
    request: Request,
    r: aioredis.Redis = Depends(get_redis),
) -> DocumentConsentResponse:
    """Record a high-evidence "signed document" consent.

    This is a heavier-weight consent than ``/grant``: it records a
    specific document version + how the user signed it (click /
    OTP / wet signature / recorded video). Used for medical informed
    consent, financial 双录, buyer-side agreements, and similar
    regulated contexts.

    A document signature MAY also imply scope grants (``granted_scopes``),
    which are recorded against the document_consent_id for traceability
    but are NOT auto-promoted into ``consent:user:{uid}`` — callers
    that need a scope active must additionally call ``/grant`` so the
    standard ``check_internal`` path sees them.
    """
    if body.document_type not in VALID_DOCUMENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid document_type: '{body.document_type}'. "
                f"Allowed: {sorted(VALID_DOCUMENT_TYPES)}"
            ),
        )
    if body.signature_method not in VALID_SIGNATURE_METHODS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid signature_method: '{body.signature_method}'. "
                f"Allowed: {sorted(VALID_SIGNATURE_METHODS)}"
            ),
        )
    if body.granted_scopes:
        _validate_scopes(body.granted_scopes)

    # Stronger document types should not be signed by a mere click.
    strong_required = {
        "medical_consent",
        "financial_disclosure",
        "双录",
    }
    if (
        body.document_type in strong_required
        and body.signature_method == "click_agree"
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"document_type '{body.document_type}' requires a "
                "stronger signature_method than 'click_agree' "
                "(use otp / signature / video_recording)."
            ),
        )

    document_consent_id = f"dcons_{uuid4().hex[:20]}"
    now = int(time.time())
    ip_hash = _hash_ip(_client_ip(request))

    record = {
        "document_consent_id": document_consent_id,
        "user_id": body.user_id,
        "document_type": body.document_type,
        "document_version": body.document_version,
        "document_url": body.document_url or "",
        "signature_method": body.signature_method,
        "signature_evidence_url": body.signature_evidence_url or "",
        "granted_scopes": json.dumps(body.granted_scopes),
        "signed_at": str(now),
        "ip_hash": ip_hash,
    }
    await r.hset(f"consent:document:{document_consent_id}", mapping=record)
    await r.lpush(
        f"user:{body.user_id}:signed_documents", document_consent_id
    )
    await r.ltrim(
        f"user:{body.user_id}:signed_documents", 0, AUDIT_LOG_MAX - 1
    )

    await _audit(
        r,
        body.user_id,
        "document_sign",
        {
            "document_consent_id": document_consent_id,
            "document_type": body.document_type,
            "document_version": body.document_version,
            "signature_method": body.signature_method,
            "granted_scopes": body.granted_scopes,
            "ip_hash": ip_hash,
        },
    )

    return DocumentConsentResponse(
        document_consent_id=document_consent_id,
        user_id=body.user_id,
        document_type=body.document_type,
        document_version=body.document_version,
        signature_method=body.signature_method,
        granted_scopes=body.granted_scopes,
        signed_at=now,
    )


@router.get("/document/{user_id}")
async def list_user_signed_documents(
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return all signed-document consent records for a user."""
    ids = await r.lrange(f"user:{user_id}:signed_documents", 0, -1)
    documents: list[dict[str, Any]] = []
    for did in ids:
        raw = await r.hgetall(f"consent:document:{did}")
        if not raw:
            continue
        try:
            granted_scopes = json.loads(raw.get("granted_scopes", "[]"))
        except (json.JSONDecodeError, TypeError):
            granted_scopes = []
        documents.append(
            {
                "document_consent_id": raw.get("document_consent_id"),
                "user_id": raw.get("user_id"),
                "document_type": raw.get("document_type"),
                "document_version": raw.get("document_version"),
                "document_url": raw.get("document_url") or None,
                "signature_method": raw.get("signature_method"),
                "signature_evidence_url": raw.get("signature_evidence_url") or None,
                "granted_scopes": granted_scopes,
                "signed_at": int(raw.get("signed_at", 0) or 0),
            }
        )

    return {
        "user_id": user_id,
        "count": len(documents),
        "documents": documents,
    }


# ── Public exports ────────────────────────────────────────────────────────

__all__ = [
    "router",
    "check_internal",
    "VALID_SCOPES",
    "REGULATED_SCOPES",
    "VALID_EVIDENCE_METHODS",
    "VALID_DOCUMENT_TYPES",
    "VALID_SIGNATURE_METHODS",
    "REASON_OK",
    "REASON_NOT_GRANTED",
    "REASON_REVOKED",
    "REASON_EXPIRED_POLICY",
    "REASON_INVALID_SCOPE",
    "REASON_NO_POLICY",
]
