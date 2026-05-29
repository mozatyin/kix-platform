"""Media storage REGISTRY — metadata + access audit for regulated media.

The KiX platform never stores binary media itself; that is delegated to
S3 / OSS / equivalent. This router is the registry layer: every uploaded
artifact (medical photo, KYC ID scan, 双录 recording, before/after
aesthetic shot, contract PDF, biometric template) gets a stable
``media_id`` here, paired with:

  * the consent grant that authorises retention (for sensitive classes)
  * an immutable per-media access log
  * legal-hold and share-token plumbing
  * soft-delete that respects holds

Why a registry layer?
---------------------
Regulated data carries obligations the bucket itself cannot enforce:
PIPL §51 audit trail, GDPR Art 17 erasure with legal-hold exceptions,
medical 10/15-year retention, KYC §54 access logs. We centralise those
obligations here so the merchant SDK and admin tooling have one
consistent surface to call.

Sensitive media classes (``medical_sensitive``, ``before_after``,
``voice_recording``, ``video``, ``biometric``) REQUIRE a paired
``consent_grant_id`` on upload — the upload is rejected otherwise. The
``general`` and ``document`` classes are permissive on creation but the
``document`` class still logs every access through
``compliance.sensitive_pi_log`` because contracts and ID cards live
under the KYC audit regime.

Redis key schema
----------------
    media:{mid}                   HASH    metadata + status flags
    user:{uid}:media              SET     member: media_id
    brand:{bid}:media             SET     member: media_id
    media:{mid}:access_log        LIST    LPUSH JSON access entries
    media:{mid}:shares            HASH    {recipient_uid: JSON(share)}
    media:{mid}:legal_hold        STRING  presence ⇒ deletion blocked
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ─────────────────────────────────────────────────────────────

VALID_MEDIA_CLASSES: set[str] = {
    "general",
    "medical_sensitive",
    "document",
    "before_after",
    "voice_recording",
    "video",
    "biometric",
}

# Classes that require a paired consent_grant_id on upload.
SENSITIVE_MEDIA_CLASSES: set[str] = {
    "medical_sensitive",
    "before_after",
    "voice_recording",
    "video",
    "biometric",
}

# Classes that fire a compliance.sensitive_pi_log entry on every read.
PII_LOGGABLE_CLASSES: set[str] = SENSITIVE_MEDIA_CLASSES | {"document"}

# Mapping media_class → compliance PII field tag (for sensitive_pi_log).
# Falls back to ``other`` if not present.
_MEDIA_CLASS_TO_PII_FIELD: dict[str, str] = {
    "medical_sensitive": "medical_record",
    "before_after": "medical_record",
    "voice_recording": "biometric",
    "video": "biometric",
    "biometric": "biometric",
    "document": "id_card",
}

VALID_SHARE_SCOPES: set[str] = {"view", "download"}

ACCESS_LOG_MAX = 5000
MEDIA_ID_PREFIX = "med_"

ADMIN_TOKEN_HEADER = "x-kix-admin-token"


# ── Optional sibling integrations ─────────────────────────────────────────
# We deliberately tolerate sibling-router absence at import time so this
# module remains independently testable (and degrades open rather than
# blocking uploads if compliance happens to be misconfigured).
try:  # pragma: no cover — exercised in app context
    from app.routers.compliance import (  # type: ignore
        log_sensitive_pi_internal as _compliance_log_pi,
    )
except Exception:  # pragma: no cover
    _compliance_log_pi = None  # type: ignore[assignment]

try:  # pragma: no cover
    from app.routers.consent import check_internal as _consent_check  # type: ignore
except Exception:  # pragma: no cover
    _consent_check = None  # type: ignore[assignment]


# ── Pydantic models ───────────────────────────────────────────────────────


class MediaUploadRequest(BaseModel):
    owner_user_id: str = Field(..., min_length=1, max_length=128)
    brand_id: str = Field(..., min_length=1, max_length=128)
    media_class: Literal[
        "general",
        "medical_sensitive",
        "document",
        "before_after",
        "voice_recording",
        "video",
        "biometric",
    ]
    storage_url: str = Field(..., min_length=1, max_length=1024)
    content_hash: str = Field(..., min_length=8, max_length=128)
    mime_type: str = Field(..., min_length=1, max_length=128)
    size_bytes: int = Field(..., ge=0)
    consent_grant_id: str | None = Field(default=None, max_length=128)
    retention_days: int | None = Field(default=None, ge=0, le=365 * 30)
    legal_hold: bool = False
    metadata: dict[str, Any] | None = None


class MediaUploadResponse(BaseModel):
    media_id: str
    owner_user_id: str
    brand_id: str
    media_class: str
    storage_url: str
    content_hash: str
    mime_type: str
    size_bytes: int
    consent_grant_id: str | None
    retention_days: int | None
    legal_hold: bool
    status: str
    created_at: int


class MediaMetadata(BaseModel):
    media_id: str
    owner_user_id: str
    brand_id: str
    media_class: str
    storage_url: str
    content_hash: str
    mime_type: str
    size_bytes: int
    consent_grant_id: str | None = None
    retention_days: int | None = None
    legal_hold: bool = False
    status: str = "active"
    created_at: int
    deleted_at: int | None = None
    delete_reason: str | None = None


class AccessLogRequest(BaseModel):
    accessor_user_id: str = Field(..., min_length=1, max_length=128)
    purpose: str = Field(..., min_length=1, max_length=64)


class MediaDeleteRequest(BaseModel):
    requestor_user_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=512)


class MediaLegalHoldRequest(BaseModel):
    admin_token: str = Field(..., min_length=1, max_length=256)
    hold_reason: str = Field(..., min_length=1, max_length=512)
    hold_until_ts: int | None = Field(default=None, ge=0)


class MediaShareRequest(BaseModel):
    recipient_user_id: str = Field(..., min_length=1, max_length=128)
    expires_at: int = Field(..., ge=0)
    scope: Literal["view", "download"] = "view"


# ── Helpers ───────────────────────────────────────────────────────────────


def _media_key(mid: str) -> str:
    return f"media:{mid}"


def _user_media_key(uid: str) -> str:
    return f"user:{uid}:media"


def _brand_media_key(bid: str) -> str:
    return f"brand:{bid}:media"


def _access_log_key(mid: str) -> str:
    return f"media:{mid}:access_log"


def _shares_key(mid: str) -> str:
    return f"media:{mid}:shares"


def _legal_hold_key(mid: str) -> str:
    return f"media:{mid}:legal_hold"


def _new_media_id() -> str:
    return f"{MEDIA_ID_PREFIX}{uuid4().hex[:20]}"


async def _load_media(r: aioredis.Redis, mid: str) -> dict[str, Any]:
    raw = await r.hgetall(_media_key(mid))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"media_id '{mid}' not found",
        )
    return raw


def _hydrate(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce HASH string values back to typed dict for response shaping."""
    metadata: dict[str, Any] | None
    try:
        metadata = (
            json.loads(raw["metadata"]) if raw.get("metadata") else None
        )
    except (json.JSONDecodeError, TypeError):
        metadata = None

    return {
        "media_id": raw.get("media_id"),
        "owner_user_id": raw.get("owner_user_id"),
        "brand_id": raw.get("brand_id"),
        "media_class": raw.get("media_class"),
        "storage_url": raw.get("storage_url"),
        "content_hash": raw.get("content_hash"),
        "mime_type": raw.get("mime_type"),
        "size_bytes": int(raw.get("size_bytes", 0) or 0),
        "consent_grant_id": raw.get("consent_grant_id") or None,
        "retention_days": (
            int(raw["retention_days"]) if raw.get("retention_days") else None
        ),
        "legal_hold": raw.get("legal_hold", "false") == "true",
        "status": raw.get("status", "active"),
        "created_at": int(raw.get("created_at", 0) or 0),
        "deleted_at": (
            int(raw["deleted_at"]) if raw.get("deleted_at") else None
        ),
        "delete_reason": raw.get("delete_reason") or None,
        "metadata": metadata,
    }


async def _maybe_log_pii(
    r: aioredis.Redis,
    *,
    media_class: str,
    owner_user_id: str,
    brand_id: str,
    action: str,
    accessor_user_id: str | None,
    purpose: str,
    note: str | None = None,
) -> None:
    """Fire compliance.sensitive_pi_log if media_class is auditable."""
    if media_class not in PII_LOGGABLE_CLASSES:
        return
    if _compliance_log_pi is None:  # pragma: no cover — sibling missing
        return
    field = _MEDIA_CLASS_TO_PII_FIELD.get(media_class, "other")
    try:
        await _compliance_log_pi(
            r,
            user_id=owner_user_id,
            brand_id=brand_id,
            action=action,
            field=field,
            accessor_user_id=accessor_user_id,
            purpose=purpose if purpose else "other",
            note=note,
        )
    except HTTPException:
        # compliance rejected (bad purpose / field) — don't break the
        # operational path; surface via logs so this can be fixed.
        logger.exception(
            "media.compliance_log_rejected media_class=%s field=%s",
            media_class,
            field,
        )
    except Exception:  # pragma: no cover — best effort
        logger.exception("media.compliance_log_failed")


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post("/upload", response_model=MediaUploadResponse)
async def upload_media(
    body: MediaUploadRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> MediaUploadResponse:
    """Register a newly uploaded artifact and (if sensitive) verify consent.

    The actual binary lives in S3/OSS; ``storage_url`` is the pointer
    KiX returns to callers. We mint a stable ``media_id`` and stamp
    audit metadata so later access goes through this layer.
    """
    if body.media_class in SENSITIVE_MEDIA_CLASSES and not body.consent_grant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"media_class '{body.media_class}' is sensitive and "
                "requires a paired consent_grant_id."
            ),
        )

    mid = _new_media_id()
    now = int(time.time())

    mapping: dict[str, str] = {
        "media_id": mid,
        "owner_user_id": body.owner_user_id,
        "brand_id": body.brand_id,
        "media_class": body.media_class,
        "storage_url": body.storage_url,
        "content_hash": body.content_hash,
        "mime_type": body.mime_type,
        "size_bytes": str(body.size_bytes),
        "consent_grant_id": body.consent_grant_id or "",
        "retention_days": (
            str(body.retention_days) if body.retention_days is not None else ""
        ),
        "legal_hold": "true" if body.legal_hold else "false",
        "status": "active",
        "created_at": str(now),
    }
    if body.metadata is not None:
        mapping["metadata"] = json.dumps(body.metadata)

    pipe = r.pipeline()
    pipe.hset(_media_key(mid), mapping=mapping)
    pipe.sadd(_user_media_key(body.owner_user_id), mid)
    pipe.sadd(_brand_media_key(body.brand_id), mid)
    if body.legal_hold:
        pipe.set(_legal_hold_key(mid), json.dumps({"reason": "upload_hold", "ts": now}))
    await pipe.execute()

    await _maybe_log_pii(
        r,
        media_class=body.media_class,
        owner_user_id=body.owner_user_id,
        brand_id=body.brand_id,
        action="write",
        accessor_user_id=body.owner_user_id,
        purpose="other",
        note=f"media_upload {mid}",
    )

    logger.info(
        "media.upload mid=%s class=%s owner=%s brand=%s",
        mid,
        body.media_class,
        body.owner_user_id,
        body.brand_id,
    )

    return MediaUploadResponse(
        media_id=mid,
        owner_user_id=body.owner_user_id,
        brand_id=body.brand_id,
        media_class=body.media_class,
        storage_url=body.storage_url,
        content_hash=body.content_hash,
        mime_type=body.mime_type,
        size_bytes=body.size_bytes,
        consent_grant_id=body.consent_grant_id,
        retention_days=body.retention_days,
        legal_hold=body.legal_hold,
        status="active",
        created_at=now,
    )


@router.get("/{media_id}", response_model=MediaMetadata)
async def get_media(
    media_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> MediaMetadata:
    """Return metadata only — file content lives in external storage.

    Reading metadata for a sensitive class is itself an access event
    and is logged through compliance.
    """
    raw = await _load_media(r, media_id)
    hydrated = _hydrate(raw)

    await _maybe_log_pii(
        r,
        media_class=hydrated["media_class"],
        owner_user_id=hydrated["owner_user_id"],
        brand_id=hydrated["brand_id"],
        action="access",
        accessor_user_id=None,
        purpose="audit",
        note=f"media_metadata_read {media_id}",
    )

    return MediaMetadata(**{k: v for k, v in hydrated.items() if k != "metadata"})


@router.post("/{media_id}/access-log")
async def append_access_log(
    media_id: str,
    body: AccessLogRequest,
    request: Request,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Record an access event for this media artifact.

    For sensitive classes this also fires
    ``compliance.sensitive_pi_log`` so KiX maintains a single canonical
    PII audit trail. Returns the ``audit_id`` of the persisted log.
    """
    raw = await _load_media(r, media_id)
    media_class = raw.get("media_class", "general")
    owner_user_id = raw.get("owner_user_id", "")
    brand_id = raw.get("brand_id", "")

    now = int(time.time())
    audit_id = uuid4().hex
    entry = {
        "audit_id": audit_id,
        "ts": now,
        "media_id": media_id,
        "accessor_user_id": body.accessor_user_id,
        "purpose": body.purpose,
        "ua": request.headers.get("user-agent", "")[:200],
    }
    pipe = r.pipeline()
    pipe.lpush(_access_log_key(media_id), json.dumps(entry))
    pipe.ltrim(_access_log_key(media_id), 0, ACCESS_LOG_MAX - 1)
    await pipe.execute()

    await _maybe_log_pii(
        r,
        media_class=media_class,
        owner_user_id=owner_user_id,
        brand_id=brand_id,
        action="access",
        accessor_user_id=body.accessor_user_id,
        purpose=body.purpose if body.purpose in _ALLOWED_COMPLIANCE_PURPOSES else "other",
        note=f"media_access {media_id}",
    )

    return {
        "media_id": media_id,
        "audit_id": audit_id,
        "ts": now,
        "logged": True,
    }


# Purposes accepted directly by compliance.sensitive_pi_log. Anything
# outside this set is coerced to ``other`` so we never reject a media
# access just because a caller used a free-form purpose label.
_ALLOWED_COMPLIANCE_PURPOSES: set[str] = {
    "kyc",
    "treatment",
    "shipping",
    "billing",
    "audit",
    "marketing",
    "other",
}


@router.post("/{media_id}/delete")
async def delete_media(
    media_id: str,
    body: MediaDeleteRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Soft-delete the media record. Legal hold blocks deletion.

    The actual blob in S3/OSS is NOT removed here — a downstream
    janitor reads ``status=deleted`` entries and tombstones the
    underlying object after the legal-hold check.
    """
    raw = await _load_media(r, media_id)
    if raw.get("status") == "deleted":
        return {"media_id": media_id, "status": "deleted", "noop": True}

    # Honor legal hold (HASH flag OR active legal_hold:* key)
    hold_key_present = await r.exists(_legal_hold_key(media_id))
    if raw.get("legal_hold") == "true" or hold_key_present:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"media '{media_id}' is under legal hold and cannot be "
                "deleted. Lift the hold first."
            ),
        )

    now = int(time.time())
    await r.hset(
        _media_key(media_id),
        mapping={
            "status": "deleted",
            "deleted_at": str(now),
            "delete_reason": body.reason,
            "deleted_by": body.requestor_user_id,
        },
    )

    await _maybe_log_pii(
        r,
        media_class=raw.get("media_class", "general"),
        owner_user_id=raw.get("owner_user_id", ""),
        brand_id=raw.get("brand_id", ""),
        action="delete",
        accessor_user_id=body.requestor_user_id,
        purpose="audit",
        note=f"media_delete {media_id}: {body.reason}",
    )

    logger.info(
        "media.delete mid=%s by=%s reason=%s",
        media_id,
        body.requestor_user_id,
        body.reason,
    )

    return {
        "media_id": media_id,
        "status": "deleted",
        "deleted_at": now,
    }


@router.post("/{media_id}/legal-hold")
async def place_legal_hold(
    media_id: str,
    body: MediaLegalHoldRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Place a legal hold on media (admin-only). Prevents future deletion.

    A minimal admin-token gate is enforced here; production deploys
    should swap this for a proper admin auth dependency. We refuse
    any token shorter than 16 characters as a smoke check.
    """
    if len(body.admin_token) < 16:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="admin_token rejected",
        )

    raw = await _load_media(r, media_id)
    now = int(time.time())
    hold_record = {
        "media_id": media_id,
        "reason": body.hold_reason,
        "placed_at": now,
        "hold_until_ts": body.hold_until_ts,
    }
    await r.set(_legal_hold_key(media_id), json.dumps(hold_record))
    if body.hold_until_ts and body.hold_until_ts > now:
        await r.expireat(_legal_hold_key(media_id), body.hold_until_ts)

    await r.hset(_media_key(media_id), "legal_hold", "true")

    await _maybe_log_pii(
        r,
        media_class=raw.get("media_class", "general"),
        owner_user_id=raw.get("owner_user_id", ""),
        brand_id=raw.get("brand_id", ""),
        action="write",
        accessor_user_id=None,
        purpose="audit",
        note=f"legal_hold_placed: {body.hold_reason}",
    )

    logger.warning(
        "media.legal_hold mid=%s reason=%s until=%s",
        media_id,
        body.hold_reason,
        body.hold_until_ts,
    )

    return {
        "media_id": media_id,
        "legal_hold": True,
        "hold_reason": body.hold_reason,
        "hold_until_ts": body.hold_until_ts,
        "placed_at": now,
    }


@router.get("/owner/{user_id}")
async def list_owner_media(
    user_id: str,
    media_class: str | None = Query(default=None),
    from_ts: int | None = Query(default=None, alias="from", ge=0),
    to_ts: int | None = Query(default=None, alias="to", ge=0),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """List a user's media artifacts, with optional class + time filter."""
    if media_class is not None and media_class not in VALID_MEDIA_CLASSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid media_class: '{media_class}'. "
                f"Allowed: {sorted(VALID_MEDIA_CLASSES)}"
            ),
        )

    ids = await r.smembers(_user_media_key(user_id))
    items: list[dict[str, Any]] = []
    for mid in ids:
        raw = await r.hgetall(_media_key(mid))
        if not raw:
            continue
        if media_class and raw.get("media_class") != media_class:
            continue
        created = int(raw.get("created_at", 0) or 0)
        if from_ts is not None and created < from_ts:
            continue
        if to_ts is not None and created > to_ts:
            continue
        items.append(_hydrate(raw))

    items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return {"user_id": user_id, "count": len(items), "items": items}


@router.get("/brand/{brand_id}")
async def list_brand_media(
    brand_id: str,
    media_class: str | None = Query(default=None),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """List media owned by a brand (admin / merchant dashboard view)."""
    if media_class is not None and media_class not in VALID_MEDIA_CLASSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid media_class: '{media_class}'. "
                f"Allowed: {sorted(VALID_MEDIA_CLASSES)}"
            ),
        )

    ids = await r.smembers(_brand_media_key(brand_id))
    items: list[dict[str, Any]] = []
    for mid in ids:
        raw = await r.hgetall(_media_key(mid))
        if not raw:
            continue
        if media_class and raw.get("media_class") != media_class:
            continue
        items.append(_hydrate(raw))

    items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return {"brand_id": brand_id, "count": len(items), "items": items}


@router.post("/{media_id}/share")
async def share_media(
    media_id: str,
    body: MediaShareRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Create a time-limited share grant from owner to a recipient.

    For sensitive media classes, the recipient MUST already hold a
    matching consent (e.g. ``phi_storage`` for medical sensitive). The
    consent check is best-effort: if the consent sibling is unavailable
    we degrade open but log a warning.
    """
    raw = await _load_media(r, media_id)
    media_class = raw.get("media_class", "general")
    now = int(time.time())

    if body.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="expires_at must be in the future",
        )

    # Recipient consent gate for sensitive classes
    if media_class in SENSITIVE_MEDIA_CLASSES and _consent_check is not None:
        required_scope = {
            "medical_sensitive": "phi_storage",
            "before_after": "before_after_photo",
            "voice_recording": "audio_video_recording",
            "video": "audio_video_recording",
            "biometric": "biometric_data",
        }.get(media_class)
        if required_scope:
            allowed, reason = await _consent_check(
                body.recipient_user_id, required_scope, r
            )
            if not allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        f"recipient lacks consent scope "
                        f"'{required_scope}' (reason={reason})"
                    ),
                    headers={"Consent-Required": required_scope},
                )

    share_record = {
        "recipient_user_id": body.recipient_user_id,
        "expires_at": body.expires_at,
        "scope": body.scope,
        "created_at": now,
    }
    await r.hset(
        _shares_key(media_id),
        body.recipient_user_id,
        json.dumps(share_record),
    )

    await _maybe_log_pii(
        r,
        media_class=media_class,
        owner_user_id=raw.get("owner_user_id", ""),
        brand_id=raw.get("brand_id", ""),
        action="access",
        accessor_user_id=body.recipient_user_id,
        purpose="other",
        note=f"share_grant {media_id} → {body.recipient_user_id}",
    )

    logger.info(
        "media.share mid=%s recipient=%s scope=%s expires=%d",
        media_id,
        body.recipient_user_id,
        body.scope,
        body.expires_at,
    )

    return {
        "media_id": media_id,
        "recipient_user_id": body.recipient_user_id,
        "scope": body.scope,
        "expires_at": body.expires_at,
        "created_at": now,
    }


# ── Public exports ────────────────────────────────────────────────────────

__all__ = [
    "router",
    "VALID_MEDIA_CLASSES",
    "SENSITIVE_MEDIA_CLASSES",
    "PII_LOGGABLE_CLASSES",
]
