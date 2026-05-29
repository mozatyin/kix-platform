"""GDPR Article-15 data export — encrypted S3 storage + signed URLs.

This module replaces the original Redis JSON-blob storage strategy used
by ``app/routers/consent.py`` (where exports were ``r.set(blob_key,
payload, ex=7d)``). That worked for an MVP but had four hard problems:

1. Redis is the wrong store for opaque ≥1 MB blobs — it competes for
   memory with hot-path keys (consent records, sessions, audit lists).
2. Payloads were stored *unencrypted at rest* — a Redis snapshot
   leak would dump every user's PII verbatim.
3. Downloads were authenticated by knowing the (UUID-shaped) export_id
   only — anyone with the link could replay it within the 7-day TTL.
4. There was no retry or idempotency story for partial failures.

The new design:
    * Payload is compiled in-memory (caller's job), passed to
      :func:`store_export` as bytes.
    * We derive a Fernet (AES-128-CBC + HMAC-SHA256) key per-user from a
      KMS master secret, so a single user's key cannot decrypt another
      user's blob.
    * Ciphertext is written through ``app.services.asset_storage``,
      which can be ``LocalStorage`` in dev or ``S3Storage`` /
      ``OSSStorage`` in prod — see that module for backend selection.
    * Downloads use the storage layer's :meth:`signed_url`, scoped to a
      7-day expiry. In prod this is a real S3 presigned URL; locally
      it's an HMAC-signed token URL.

Master-key sourcing
-------------------
Resolution order:
    1. ``KMS_GDPR_MASTER_KEY`` env var (recommended; rotate via KMS).
    2. ``settings.jwt_secret`` (MVP fallback — *NOT for production*;
       rotating JWT will invalidate every existing user's export key).

The derived per-user key is ``urlsafe_b64encode(sha256(master :
user_id))`` — Fernet requires 32-byte url-safe base64-encoded keys.

Audit
-----
Every export is appended to a capped Redis list
``gdpr:export:audit`` (LPUSH + LTRIM to 100k entries) so an Article-30
"records of processing" report can be reconstructed without grepping
application logs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from typing import Any

import redis.asyncio as aioredis
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.services.asset_storage import get_storage

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────

EXPORT_TTL_SECONDS = 7 * 86400
S3_KEY_PREFIX = "gdpr-exports"
AUDIT_LIST_KEY = "gdpr:export:audit"
AUDIT_LIST_MAX = 100_000


# ── Key derivation + encryption ──────────────────────────────────────────


def _master_key() -> bytes:
    """Resolve the KMS master key, with a development fallback.

    In production deploy with ``KMS_GDPR_MASTER_KEY`` set from your
    secrets manager. Rotating this value invalidates *every* user's
    derived export key — coordinate with a re-encryption window.
    """
    master = os.environ.get("KMS_GDPR_MASTER_KEY", "")
    if master:
        return master.encode()
    # MVP fallback: JWT secret. Production MUST set KMS_GDPR_MASTER_KEY.
    return settings.jwt_secret.encode()


def derive_key_for_user(user_id: str) -> bytes:
    """Derive a Fernet-shaped key from KMS master + user_id.

    Returns the 44-byte url-safe base64 encoding of a 32-byte sha256
    digest — exactly what :class:`cryptography.fernet.Fernet` expects.
    Because the master + user_id are mixed via sha256 (not hkdf), the
    output is one-way: leaking one user's derived key reveals nothing
    about the master or any other user's key.
    """
    if not user_id:
        raise ValueError("user_id required for key derivation")
    derived = hashlib.sha256(_master_key() + b":" + user_id.encode()).digest()
    return base64.urlsafe_b64encode(derived)


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt ``plaintext`` with a Fernet key. Returns the token bytes."""
    return Fernet(key).encrypt(plaintext)


def decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """Decrypt a Fernet token. Raises :class:`InvalidToken` on tamper / wrong key."""
    return Fernet(key).decrypt(ciphertext)


# ── Storage ──────────────────────────────────────────────────────────────


def s3_key_for(user_id: str, export_id: str) -> str:
    """Compute the S3 object key for an export.

    Uses a 4-character user prefix to shard objects across the
    bucket key-space (S3 partitions on key prefix for throttling).
    """
    prefix = (user_id[:4] or "____").ljust(4, "_")
    return f"{S3_KEY_PREFIX}/{prefix}/{export_id}.enc"


async def store_export(user_id: str, payload: bytes, export_id: str) -> str:
    """Encrypt ``payload`` and upload it; return a 7-day signed URL.

    The plaintext never touches disk or the storage backend — only the
    Fernet ciphertext is written. Object metadata records the algorithm
    so a future migration (e.g. AES-256-GCM via KMS DataKeys) can
    decrypt legacy objects.
    """
    if not payload:
        raise ValueError("payload is empty; nothing to export")

    key = derive_key_for_user(user_id)
    encrypted = encrypt(payload, key)
    s3_key = s3_key_for(user_id, export_id)

    storage = get_storage()
    await storage.put(
        s3_key,
        encrypted,
        content_type="application/octet-stream",
        metadata={
            "user_id": user_id,
            "export_id": export_id,
            "encryption": "fernet-aes128-cbc-hmac-sha256",
            "plaintext_size": str(len(payload)),
        },
    )

    signed = await storage.signed_url(s3_key, ttl_seconds=EXPORT_TTL_SECONDS)
    logger.info(
        "gdpr_export.stored user=%s export_id=%s bytes=%d",
        user_id,
        export_id,
        len(encrypted),
    )
    return signed


async def fetch_export(user_id: str, export_id: str) -> bytes:
    """Fetch + decrypt an export (admin / regulator-replay path).

    Storage-layer reads are signed by the same per-user key, so an
    administrator with bucket read access still cannot read plaintext
    without the KMS master + the user_id.
    """
    storage = get_storage()
    s3_key = s3_key_for(user_id, export_id)
    ciphertext = await storage.get(s3_key)
    key = derive_key_for_user(user_id)
    try:
        return decrypt(ciphertext, key)
    except InvalidToken as exc:
        raise RuntimeError(
            f"failed to decrypt export {export_id} for user {user_id}: "
            "wrong key or corrupted blob"
        ) from exc


# ── Audit (Article-30 records of processing) ─────────────────────────────


async def audit_export(
    r: aioredis.Redis,
    *,
    export_id: str,
    user_id: str,
    ip: str | None = None,
    user_agent: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append an audit row for an export request.

    The list is capped at :data:`AUDIT_LIST_MAX` entries (≈100k) via
    ``LTRIM``; older rows roll off. Forensics retention beyond this
    cap should pipe the list to cold storage via a daily cron.
    """
    entry: dict[str, Any] = {
        "export_id": export_id,
        "user_id": user_id,
        "ts": int(time.time()),
        "ip": ip,
        "user_agent": (user_agent or "")[:200],
    }
    if extra:
        entry.update(extra)
    await r.lpush(AUDIT_LIST_KEY, json.dumps(entry, ensure_ascii=False))
    await r.ltrim(AUDIT_LIST_KEY, 0, AUDIT_LIST_MAX - 1)


__all__ = [
    "EXPORT_TTL_SECONDS",
    "S3_KEY_PREFIX",
    "AUDIT_LIST_KEY",
    "derive_key_for_user",
    "encrypt",
    "decrypt",
    "s3_key_for",
    "store_export",
    "fetch_export",
    "audit_export",
]
