"""Refer-friend, both-win mechanic — Wave F obvious-win #6.

Inspired by BRAME and multi-source viral-loop patterns. When user A invites
user B, both receive a voucher upon B's successful first game completion.
Drives K-factor for self-serve campaigns where brands don't have ad budget.

Redis schema::

    ref:invite:{token}         HASH   {inviter_user_id, brand_id,
                                        created_at_ms, status,
                                        referee_user_id?}
    ref:user:{uid}:invited     ZSET   score=ts, member=token
    ref:user:{uid}:counts      HASH   {invited, accepted, completed}
    ref:pending:{bid}:{uid}    STRING token  (referee → token until complete)
    ref:openinvite:{bid}:{uid} STRING token  (inviter reuse — one open invite)
    ref:vouchered:{bid}:{uid}  STRING "1"    idempotency for completion

NEW file.
"""

from __future__ import annotations

import time
from uuid import uuid4


TOKEN_TTL_SEC = 30 * 24 * 3600  # 30 days
MAX_COMPLETED_PER_MONTH = 20


def _k_invite(token: str) -> str:
    return f"ref:invite:{token}"


def _k_user_invited(uid: str) -> str:
    return f"ref:user:{uid}:invited"


def _k_user_counts(uid: str) -> str:
    return f"ref:user:{uid}:counts"


def _k_pending(bid: str, uid: str) -> str:
    return f"ref:pending:{bid}:{uid}"


def _k_open_invite(bid: str, uid: str) -> str:
    return f"ref:openinvite:{bid}:{uid}"


def _k_vouchered(bid: str, uid: str) -> str:
    return f"ref:vouchered:{bid}:{uid}"


def _k_monthly_completed(uid: str, ym: str) -> str:
    return f"ref:user:{uid}:completed:{ym}"


def _share_url(token: str) -> str:
    return f"https://kix.app/r/{token}"


async def _decode_hash(r, key: str) -> dict[str, str]:
    raw = await r.hgetall(key)
    out: dict[str, str] = {}
    for k, v in raw.items():
        k = k.decode() if isinstance(k, bytes) else k
        v = v.decode() if isinstance(v, bytes) else v
        out[k] = v
    return out


async def create_invite(r, inviter_uid: str, brand_id: str) -> dict:
    """Create an invite token (or reuse the one outstanding open invite)."""
    if not inviter_uid or not brand_id:
        raise ValueError("inviter_uid and brand_id required")

    # Reuse outstanding open invite for (inviter, brand).
    existing = await r.get(_k_open_invite(brand_id, inviter_uid))
    if existing:
        token = existing.decode() if isinstance(existing, bytes) else existing
        meta = await _decode_hash(r, _k_invite(token))
        if meta and meta.get("status") == "open":
            return {"invite_token": token, "share_url": _share_url(token)}

    token = uuid4().hex[:16]
    now_ms = int(time.time() * 1000)
    await r.hset(
        _k_invite(token),
        mapping={
            "inviter_user_id": inviter_uid,
            "brand_id": brand_id,
            "created_at_ms": str(now_ms),
            "status": "open",
        },
    )
    await r.expire(_k_invite(token), TOKEN_TTL_SEC)
    await r.zadd(_k_user_invited(inviter_uid), {token: now_ms})
    await r.hincrby(_k_user_counts(inviter_uid), "invited", 1)
    await r.set(
        _k_open_invite(brand_id, inviter_uid), token, ex=TOKEN_TTL_SEC,
    )
    return {"invite_token": token, "share_url": _share_url(token)}


async def accept_invite(r, token: str, referee_uid: str) -> dict:
    """Mark invite as accepted; referee gets a pending marker."""
    if not token or not referee_uid:
        raise ValueError("token and referee_uid required")
    meta = await _decode_hash(r, _k_invite(token))
    if not meta:
        raise ValueError("invite not found or expired")
    if meta.get("status") not in ("open", "accepted"):
        raise ValueError(f"invite not acceptable (status={meta.get('status')})")
    if meta["inviter_user_id"] == referee_uid:
        raise ValueError("cannot accept own invite")

    brand_id = meta["brand_id"]
    # Referee must be NEW for this brand: no prior voucher issued.
    if await r.exists(_k_vouchered(brand_id, referee_uid)):
        raise ValueError("referee already onboarded for this brand")

    # Already pending under a different token?  Keep the first one — idempotent.
    existing_pending = await r.get(_k_pending(brand_id, referee_uid))
    if existing_pending:
        pend = (
            existing_pending.decode()
            if isinstance(existing_pending, bytes)
            else existing_pending
        )
        return {
            "accepted": pend == token,
            "already_pending": True,
            "token": pend,
        }

    await r.set(
        _k_pending(brand_id, referee_uid), token, ex=TOKEN_TTL_SEC,
    )
    if meta.get("status") == "open":
        await r.hset(
            _k_invite(token),
            mapping={"status": "accepted", "referee_user_id": referee_uid},
        )
        await r.hincrby(_k_user_counts(meta["inviter_user_id"]), "accepted", 1)
    return {"accepted": True, "already_pending": False, "token": token}


async def on_referee_complete(r, brand_id: str, referee_uid: str) -> dict:
    """Called when referee finishes their first game for this brand.

    Idempotent — once vouchered for (brand, referee) we never double-issue.
    Returns whichever side(s) the call newly issued vouchers to.
    """
    if not brand_id or not referee_uid:
        raise ValueError("brand_id and referee_uid required")

    # Idempotency guard.
    set_ok = await r.set(
        _k_vouchered(brand_id, referee_uid), "1", nx=True, ex=TOKEN_TTL_SEC,
    )
    if not set_ok:
        return {"vouchered": False, "reason": "already_vouchered"}

    token_raw = await r.get(_k_pending(brand_id, referee_uid))
    if not token_raw:
        return {"vouchered": False, "reason": "no_pending_invite"}
    token = token_raw.decode() if isinstance(token_raw, bytes) else token_raw

    meta = await _decode_hash(r, _k_invite(token))
    if not meta:
        return {"vouchered": False, "reason": "invite_expired"}

    inviter = meta["inviter_user_id"]

    # Monthly cap: max N completed referrals per inviter per month.
    ym = time.strftime("%Y-%m", time.gmtime())
    monthly_key = _k_monthly_completed(inviter, ym)
    monthly = await r.incr(monthly_key)
    if monthly == 1:
        await r.expire(monthly_key, 35 * 24 * 3600)
    if monthly > MAX_COMPLETED_PER_MONTH:
        # Roll back so we don't permanently over-count if cap was reason.
        await r.decr(monthly_key)
        return {"vouchered": False, "reason": "monthly_cap"}

    await r.hset(_k_invite(token), mapping={"status": "completed"})
    await r.hincrby(_k_user_counts(inviter), "completed", 1)
    await r.delete(_k_pending(brand_id, referee_uid))

    return {
        "vouchered": True,
        "inviter_user_id": inviter,
        "referee_user_id": referee_uid,
        "brand_id": brand_id,
        "token": token,
    }


async def stats(r, user_id: str) -> dict:
    """Return invite/accept/complete counts for a user."""
    raw = await _decode_hash(r, _k_user_counts(user_id))
    invited = int(raw.get("invited", "0") or 0)
    accepted = int(raw.get("accepted", "0") or 0)
    completed = int(raw.get("completed", "0") or 0)
    return {
        "user_id": user_id,
        "invited": invited,
        "accepted": accepted,
        "completed": completed,
        "earned_voucher_count": completed,
    }
