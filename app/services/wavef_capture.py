"""Email / SMS capture gate — Wave F spec #11.

Inspired by BRAME's lead-capture gate: an optional pre-game form that
collects an email or phone with explicit consent flags. The captured
contact is stored under a campaign-scoped Redis hash so the brand owner
can later export it as CSV.

Privacy posture
---------------
* The plaintext email/phone is **encrypted at rest** with Fernet keyed
  off ``settings.jwt_secret`` (deterministic for the same secret).
  This keeps the storage layer opaque while letting the export endpoint
  decrypt for admins on the same host.
* The hash key is SHA-256 of the lower-cased email or normalised phone,
  so a second submit by the same contact is **idempotent** — it
  overwrites instead of creating a duplicate row.
* A brand-scoped opt-out set blocks future submits and is honoured by
  the export endpoint.

Redis schema
------------
::

    capture:{cid}                 HASH email_hash -> json (full record)
    capture:{cid}:by_phone        HASH phone_hash -> json
    capture:optout:{brand_id}     SET (hashes of opted-out contacts)
    capture:{cid}:meta            HASH {brand_id}

NEW file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


# ── Crypto helpers ───────────────────────────────────────────────────────


def _fernet() -> Fernet:
    """Derive a stable Fernet key from ``settings.jwt_secret``."""
    raw = hashlib.sha256(settings.jwt_secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def _encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def _decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


# ── Normalisation ────────────────────────────────────────────────────────


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _norm_email(email: str) -> str:
    return email.strip().lower()


def _norm_phone(phone: str) -> str:
    return re.sub(r"[^\d+]", "", phone)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ── Redis keys ───────────────────────────────────────────────────────────


def _k_emails(cid: str) -> str:
    return f"capture:{cid}"


def _k_phones(cid: str) -> str:
    return f"capture:{cid}:by_phone"


def _k_optout(brand_id: str) -> str:
    return f"capture:optout:{brand_id}"


def _k_meta(cid: str) -> str:
    return f"capture:{cid}:meta"


# ── Public service API ───────────────────────────────────────────────────


async def submit(
    r,
    *,
    campaign_id: str,
    brand_id: str,
    email: Optional[str],
    phone: Optional[str],
    sms_opt_in: bool,
    marketing_opt_in: bool,
) -> dict:
    """Persist a capture record. Returns ``{accepted, idempotent}``.

    * At least one of ``email`` / ``phone`` is required.
    * If the contact is on the brand's opt-out list, the submit is
      silently dropped with ``{accepted: False, reason: "opted_out"}``.
    """
    if not email and not phone:
        raise ValueError("at least one of email or phone is required")
    if email and not _EMAIL_RE.match(email.strip()):
        raise ValueError("invalid email format")

    norm_email = _norm_email(email) if email else None
    norm_phone = _norm_phone(phone) if phone else None
    email_h = _hash(norm_email) if norm_email else None
    phone_h = _hash(norm_phone) if norm_phone else None

    # Opt-out gate.
    opt_set = _k_optout(brand_id)
    for h in (email_h, phone_h):
        if h and await r.sismember(opt_set, h):
            return {"accepted": False, "reason": "opted_out"}

    record = {
        "email_enc": _encrypt(norm_email) if norm_email else "",
        "phone_enc": _encrypt(norm_phone) if norm_phone else "",
        "sms_opt_in": bool(sms_opt_in),
        "marketing_opt_in": bool(marketing_opt_in),
        "submitted_at_ms": int(time.time() * 1000),
        "brand_id": brand_id,
        "campaign_id": campaign_id,
    }
    payload = json.dumps(record)

    # Remember the brand for export auth.
    await r.hset(_k_meta(campaign_id), mapping={"brand_id": brand_id})

    idempotent = False
    if email_h:
        existed = await r.hexists(_k_emails(campaign_id), email_h)
        idempotent = idempotent or bool(existed)
        await r.hset(_k_emails(campaign_id), email_h, payload)
    if phone_h:
        existed = await r.hexists(_k_phones(campaign_id), phone_h)
        idempotent = idempotent or bool(existed)
        await r.hset(_k_phones(campaign_id), phone_h, payload)

    return {"accepted": True, "idempotent": idempotent}


async def export_records(r, campaign_id: str) -> list[dict]:
    """Decrypted, opt-out-filtered list of captured contacts.

    Returned rows: ``{email, phone, sms_opt_in, marketing_opt_in,
    submitted_at_ms}``.
    """
    meta = await r.hgetall(_k_meta(campaign_id))
    brand_id = ""
    for k, v in (meta or {}).items():
        k = k.decode() if isinstance(k, bytes) else k
        v = v.decode() if isinstance(v, bytes) else v
        if k == "brand_id":
            brand_id = v

    opt_hashes: set[str] = set()
    if brand_id:
        members = await r.smembers(_k_optout(brand_id))
        for m in members or []:
            opt_hashes.add(m.decode() if isinstance(m, bytes) else m)

    rows: list[dict] = []
    seen: set[str] = set()
    for key in (_k_emails(campaign_id), _k_phones(campaign_id)):
        raw = await r.hgetall(key)
        for h, payload in (raw or {}).items():
            h = h.decode() if isinstance(h, bytes) else h
            if h in opt_hashes or h in seen:
                continue
            seen.add(h)
            payload = payload.decode() if isinstance(payload, bytes) else payload
            try:
                rec = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                continue
            rows.append(
                {
                    "email": _decrypt(rec.get("email_enc", "")),
                    "phone": _decrypt(rec.get("phone_enc", "")),
                    "sms_opt_in": bool(rec.get("sms_opt_in")),
                    "marketing_opt_in": bool(rec.get("marketing_opt_in")),
                    "submitted_at_ms": int(rec.get("submitted_at_ms", 0) or 0),
                }
            )
    rows.sort(key=lambda x: x["submitted_at_ms"])
    return rows


def to_csv(rows: list[dict]) -> str:
    """Render rows as CSV. Always emits a header for empty input."""
    header = "email,phone,sms_opt_in,marketing_opt_in,submitted_at_ms"

    def _esc(v: str) -> str:
        if any(c in v for c in (",", "\"", "\n")):
            return '"' + v.replace('"', '""') + '"'
        return v

    lines = [header]
    for row in rows:
        lines.append(
            ",".join(
                [
                    _esc(str(row.get("email") or "")),
                    _esc(str(row.get("phone") or "")),
                    "1" if row.get("sms_opt_in") else "0",
                    "1" if row.get("marketing_opt_in") else "0",
                    str(row.get("submitted_at_ms") or 0),
                ]
            )
        )
    return "\n".join(lines) + "\n"


async def optout(
    r,
    *,
    brand_id: str,
    email: Optional[str] = None,
    phone: Optional[str] = None,
) -> dict:
    """Add a contact to the brand's opt-out set."""
    if not email and not phone:
        raise ValueError("at least one of email or phone is required")
    added = 0
    if email:
        added += int(await r.sadd(_k_optout(brand_id), _hash(_norm_email(email))))
    if phone:
        added += int(await r.sadd(_k_optout(brand_id), _hash(_norm_phone(phone))))
    return {"added": added}
