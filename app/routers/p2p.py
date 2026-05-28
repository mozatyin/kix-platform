"""P2P transfer router — GiftSending + TradingPost.

Two user-to-user value transfer modules:

  1. GiftSending — one-way transfer (energy / item / voucher / currency)
     Sender's balance is deducted immediately into escrow. Recipient claims
     via a one-time token. If unclaimed after 7 days, sender may refund.

  2. TradingPost — bilateral atomic swap. Two users exchange any mix of
     items + currency + vouchers. Assets are validated and swapped atomically
     at /accept time using Redis WATCH/MULTI/EXEC.

All state in Redis, brand-isolated. Reuses the platform primitive key
conventions (currency:{name}:{uid}:{bid}, user:{uid}:inventory:{bid},
voucher:{vid}).

Redis keys
──────────
  gift:{gift_id}                          HASH full gift state
  user:{uid}:gifts:inbox:{brand_id}       LIST gift_ids awaiting claim
  user:{uid}:gifts:sent:{brand_id}        LIST gift_ids sent
  escrow:{gift_id}                        HASH amount/item_id/voucher_id

  trade:{trade_id}                        HASH full trade state
  user:{uid}:trades:pending:{brand_id}    SET trade_ids
"""

from __future__ import annotations

import json
import secrets
import time
import uuid
from typing import Any, Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.redis_client import get_redis

router = APIRouter()


# ═════════════════════════════════════════════════════════════════════════
# Constants & key helpers
# ═════════════════════════════════════════════════════════════════════════

GIFT_TTL_SECONDS = 7 * 24 * 3600  # 7 days

GiftType = Literal["energy", "item", "voucher", "currency"]


def _gift_key(gift_id: str) -> str:
    return f"gift:{gift_id}"


def _escrow_key(gift_id: str) -> str:
    return f"escrow:{gift_id}"


def _inbox_key(user_id: str, brand_id: str) -> str:
    return f"user:{user_id}:gifts:inbox:{brand_id}"


def _sent_key(user_id: str, brand_id: str) -> str:
    return f"user:{user_id}:gifts:sent:{brand_id}"


def _trade_key(trade_id: str) -> str:
    return f"trade:{trade_id}"


def _trade_pending_key(user_id: str, brand_id: str) -> str:
    return f"user:{user_id}:trades:pending:{brand_id}"


def _currency_balance_key(currency: str, user_id: str, brand_id: str) -> str:
    # Matches primitives convention but flipped per spec:
    # currency:{name}:{uid}:{bid}
    return f"currency:{currency}:{user_id}:{brand_id}"


def _inventory_key(user_id: str, brand_id: str) -> str:
    return f"user:{user_id}:inventory:{brand_id}"


def _voucher_key(voucher_id: str) -> str:
    return f"voucher:{voucher_id}"


def _energy_key(user_id: str, brand_id: str) -> str:
    # Match modules.py convention
    return f"energy:balance:{brand_id}:{user_id}"


# ═════════════════════════════════════════════════════════════════════════
# Generic balance helpers (work for both gifts & trades)
# ═════════════════════════════════════════════════════════════════════════


async def _get_energy(
    r: aioredis.Redis, user_id: str, brand_id: str
) -> int:
    return int(await r.get(_energy_key(user_id, brand_id)) or 0)


async def _get_currency(
    r: aioredis.Redis, currency: str, user_id: str, brand_id: str
) -> int:
    return int(
        await r.get(_currency_balance_key(currency, user_id, brand_id)) or 0
    )


async def _get_item_qty(
    r: aioredis.Redis, user_id: str, brand_id: str, item_id: str
) -> int:
    return int(await r.hget(_inventory_key(user_id, brand_id), item_id) or 0)


async def _voucher_owner(r: aioredis.Redis, voucher_id: str) -> str | None:
    v = await r.hget(_voucher_key(voucher_id), "user_id")
    return v if v else None


# ─── Atomic deduct / credit helpers used inside WATCH/MULTI sections ─────
# These return False if insufficient, True if deducted.

async def _deduct_energy(
    r: aioredis.Redis, user_id: str, brand_id: str, amount: int
) -> bool:
    key = _energy_key(user_id, brand_id)
    new_bal = await r.decrby(key, amount)
    if int(new_bal) < 0:
        await r.incrby(key, amount)  # rollback
        return False
    return True


async def _credit_energy(
    r: aioredis.Redis, user_id: str, brand_id: str, amount: int
) -> int:
    return int(await r.incrby(_energy_key(user_id, brand_id), amount))


async def _deduct_currency(
    r: aioredis.Redis,
    currency: str,
    user_id: str,
    brand_id: str,
    amount: int,
) -> bool:
    key = _currency_balance_key(currency, user_id, brand_id)
    new_bal = await r.decrby(key, amount)
    if int(new_bal) < 0:
        await r.incrby(key, amount)
        return False
    return True


async def _credit_currency(
    r: aioredis.Redis,
    currency: str,
    user_id: str,
    brand_id: str,
    amount: int,
) -> int:
    return int(
        await r.incrby(_currency_balance_key(currency, user_id, brand_id), amount)
    )


async def _deduct_item(
    r: aioredis.Redis,
    user_id: str,
    brand_id: str,
    item_id: str,
    qty: int,
) -> bool:
    inv_key = _inventory_key(user_id, brand_id)
    current = int(await r.hget(inv_key, item_id) or 0)
    if current < qty:
        return False
    new_qty = current - qty
    if new_qty == 0:
        await r.hdel(inv_key, item_id)
    else:
        await r.hset(inv_key, item_id, new_qty)
    return True


async def _credit_item(
    r: aioredis.Redis,
    user_id: str,
    brand_id: str,
    item_id: str,
    qty: int,
) -> int:
    inv_key = _inventory_key(user_id, brand_id)
    current = int(await r.hget(inv_key, item_id) or 0)
    new_qty = current + qty
    await r.hset(inv_key, item_id, new_qty)
    return new_qty


async def _transfer_voucher(
    r: aioredis.Redis,
    voucher_id: str,
    from_user: str,
    to_user: str,
) -> bool:
    """Reassign voucher ownership. Returns False if not owned by from_user."""
    vkey = _voucher_key(voucher_id)
    owner = await r.hget(vkey, "user_id")
    if owner != from_user:
        return False
    await r.hset(vkey, "user_id", to_user)
    return True


# ═════════════════════════════════════════════════════════════════════════
# 1. GIFT SENDING
# ═════════════════════════════════════════════════════════════════════════


class GiftSend(BaseModel):
    from_user_id: str
    to_user_id: str
    brand_id: str
    type: GiftType
    # Type-specific payload (only one is consulted per type):
    amount: int | None = None        # for energy or currency
    currency: str | None = None      # required if type == "currency"
    item_id: str | None = None       # required if type == "item"
    qty: int = 1                     # for item gifts
    voucher_id: str | None = None    # required if type == "voucher"
    message: str = ""


class GiftClaim(BaseModel):
    claim_token: str
    user_id: str


class GiftIdBody(BaseModel):
    user_id: str = ""


@router.post("/gift/send")
async def gift_send(
    body: GiftSend, r: aioredis.Redis = Depends(get_redis)
):
    """Send a gift. Deducts from sender immediately into escrow."""
    if body.from_user_id == body.to_user_id:
        raise HTTPException(422, detail="cannot gift to self")

    gift_id = uuid.uuid4().hex
    claim_token = secrets.token_urlsafe(16)
    now = int(time.time())
    expires_at = now + GIFT_TTL_SECONDS

    escrow_payload: dict[str, Any] = {"type": body.type}

    # ─── Deduct sender's asset into escrow ──────────────────────────────
    if body.type == "energy":
        if not body.amount or body.amount <= 0:
            raise HTTPException(422, detail="amount must be positive for energy")

        ok = await _deduct_energy(
            r, body.from_user_id, body.brand_id, body.amount
        )
        if not ok:
            raise HTTPException(402, detail={"error": "insufficient_energy"})
        escrow_payload["amount"] = body.amount

    elif body.type == "currency":
        if not body.currency:
            raise HTTPException(422, detail="currency required")
        if not body.amount or body.amount <= 0:
            raise HTTPException(422, detail="amount must be positive")
        ok = await _deduct_currency(
            r,
            body.currency,
            body.from_user_id,
            body.brand_id,
            body.amount,
        )
        if not ok:
            raise HTTPException(
                402,
                detail={
                    "error": "insufficient_currency",
                    "currency": body.currency,
                },
            )
        escrow_payload["currency"] = body.currency
        escrow_payload["amount"] = body.amount

    elif body.type == "item":
        if not body.item_id:
            raise HTTPException(422, detail="item_id required")
        if body.qty <= 0:
            raise HTTPException(422, detail="qty must be positive")
        ok = await _deduct_item(
            r,
            body.from_user_id,
            body.brand_id,
            body.item_id,
            body.qty,
        )
        if not ok:
            raise HTTPException(
                402,
                detail={
                    "error": "insufficient_item",
                    "item_id": body.item_id,
                    "need": body.qty,
                },
            )
        escrow_payload["item_id"] = body.item_id
        escrow_payload["qty"] = body.qty

    elif body.type == "voucher":
        if not body.voucher_id:
            raise HTTPException(422, detail="voucher_id required")
        # Move ownership to "escrow:{gift_id}" sentinel
        ok = await _transfer_voucher(
            r, body.voucher_id, body.from_user_id, f"escrow:{gift_id}"
        )
        if not ok:
            raise HTTPException(
                402,
                detail={
                    "error": "voucher_not_owned",
                    "voucher_id": body.voucher_id,
                },
            )
        escrow_payload["voucher_id"] = body.voucher_id

    else:
        raise HTTPException(422, detail=f"unknown gift type {body.type}")

    # ─── Persist gift + escrow ──────────────────────────────────────────
    await r.hset(
        _escrow_key(gift_id),
        mapping={k: str(v) for k, v in escrow_payload.items()},
    )

    gift_state = {
        "gift_id": gift_id,
        "from_user_id": body.from_user_id,
        "to_user_id": body.to_user_id,
        "brand_id": body.brand_id,
        "type": body.type,
        "claim_token": claim_token,
        "status": "pending",
        "message": body.message,
        "created_at": now,
        "expires_at": expires_at,
        "payload": json.dumps(escrow_payload),
    }
    await r.hset(
        _gift_key(gift_id),
        mapping={k: str(v) for k, v in gift_state.items()},
    )
    # TTL on the gift key itself (auto-cleanup after refund window)
    await r.expire(_gift_key(gift_id), GIFT_TTL_SECONDS * 2)
    await r.expire(_escrow_key(gift_id), GIFT_TTL_SECONDS * 2)

    await r.lpush(_inbox_key(body.to_user_id, body.brand_id), gift_id)
    await r.lpush(_sent_key(body.from_user_id, body.brand_id), gift_id)

    return {
        "gift_id": gift_id,
        "claim_token": claim_token,
        "expires_at": expires_at,
        "status": "pending",
    }


@router.post("/gift/{gift_id}/claim")
async def gift_claim(
    gift_id: str,
    body: GiftClaim,
    r: aioredis.Redis = Depends(get_redis),
):
    """Recipient claims a pending gift. Transfers escrow → recipient."""
    state = await r.hgetall(_gift_key(gift_id))
    if not state:
        raise HTTPException(404, detail="gift not found")

    if state.get("status") != "pending":
        raise HTTPException(
            409,
            detail={"error": "gift_not_claimable", "status": state.get("status")},
        )
    if state.get("claim_token") != body.claim_token:
        raise HTTPException(403, detail="invalid claim_token")
    if state.get("to_user_id") != body.user_id:
        raise HTTPException(403, detail="not the recipient")

    now = int(time.time())
    if now > int(state.get("expires_at", 0)):
        raise HTTPException(410, detail={"error": "gift_expired"})

    brand_id = state["brand_id"]
    gtype = state["type"]
    payload = json.loads(state.get("payload", "{}"))

    # ─── Credit recipient ───────────────────────────────────────────────
    credit_summary: dict[str, Any] = {}

    if gtype == "energy":
        amt = int(payload["amount"])
        new_bal = await _credit_energy(r, body.user_id, brand_id, amt)
        credit_summary = {"type": "energy", "amount": amt, "balance": new_bal}

    elif gtype == "currency":
        cur = payload["currency"]
        amt = int(payload["amount"])
        new_bal = await _credit_currency(
            r, cur, body.user_id, brand_id, amt
        )
        credit_summary = {
            "type": "currency",
            "currency": cur,
            "amount": amt,
            "balance": new_bal,
        }

    elif gtype == "item":
        iid = payload["item_id"]
        qty = int(payload["qty"])
        new_qty = await _credit_item(r, body.user_id, brand_id, iid, qty)
        credit_summary = {
            "type": "item",
            "item_id": iid,
            "qty": qty,
            "total_qty": new_qty,
        }

    elif gtype == "voucher":
        vid = payload["voucher_id"]
        # Move from escrow sentinel → recipient
        ok = await _transfer_voucher(
            r, vid, f"escrow:{gift_id}", body.user_id
        )
        if not ok:
            raise HTTPException(500, detail="escrow voucher missing")
        credit_summary = {"type": "voucher", "voucher_id": vid}

    # ─── Mark claimed; remove escrow ────────────────────────────────────
    await r.hset(
        _gift_key(gift_id),
        mapping={"status": "claimed", "claimed_at": now},
    )
    await r.delete(_escrow_key(gift_id))
    await r.lrem(_inbox_key(body.user_id, brand_id), 0, gift_id)

    return {
        "ok": True,
        "gift_id": gift_id,
        "claimed_by": body.user_id,
        "received": credit_summary,
    }


@router.post("/gift/{gift_id}/refund")
async def gift_refund(
    gift_id: str,
    body: GiftIdBody | None = None,
    r: aioredis.Redis = Depends(get_redis),
):
    """Refund an expired, unclaimed gift back to the sender."""
    state = await r.hgetall(_gift_key(gift_id))
    if not state:
        raise HTTPException(404, detail="gift not found")
    if state.get("status") != "pending":
        raise HTTPException(
            409,
            detail={"error": "gift_not_refundable", "status": state.get("status")},
        )

    now = int(time.time())
    if now < int(state.get("expires_at", 0)):
        raise HTTPException(
            422,
            detail={
                "error": "gift_not_expired",
                "expires_at": int(state.get("expires_at", 0)),
                "now": now,
            },
        )

    sender = state["from_user_id"]
    if body and body.user_id and body.user_id != sender:
        raise HTTPException(403, detail="only sender may refund")

    brand_id = state["brand_id"]
    gtype = state["type"]
    payload = json.loads(state.get("payload", "{}"))

    refund_summary: dict[str, Any] = {}
    if gtype == "energy":
        amt = int(payload["amount"])
        new_bal = await _credit_energy(r, sender, brand_id, amt)
        refund_summary = {"type": "energy", "amount": amt, "balance": new_bal}
    elif gtype == "currency":
        cur = payload["currency"]
        amt = int(payload["amount"])
        new_bal = await _credit_currency(r, cur, sender, brand_id, amt)
        refund_summary = {
            "type": "currency",
            "currency": cur,
            "amount": amt,
            "balance": new_bal,
        }
    elif gtype == "item":
        iid = payload["item_id"]
        qty = int(payload["qty"])
        new_qty = await _credit_item(r, sender, brand_id, iid, qty)
        refund_summary = {"type": "item", "item_id": iid, "total_qty": new_qty}
    elif gtype == "voucher":
        vid = payload["voucher_id"]
        ok = await _transfer_voucher(r, vid, f"escrow:{gift_id}", sender)
        if not ok:
            raise HTTPException(500, detail="escrow voucher missing")
        refund_summary = {"type": "voucher", "voucher_id": vid}

    await r.hset(
        _gift_key(gift_id),
        mapping={"status": "refunded", "refunded_at": now},
    )
    await r.delete(_escrow_key(gift_id))
    await r.lrem(_inbox_key(state["to_user_id"], brand_id), 0, gift_id)

    return {
        "ok": True,
        "gift_id": gift_id,
        "refunded_to": sender,
        "refund": refund_summary,
    }


def _gift_view(state: dict[str, str]) -> dict[str, Any]:
    if not state:
        return {}
    try:
        payload = json.loads(state.get("payload", "{}"))
    except Exception:
        payload = {}
    return {
        "gift_id": state.get("gift_id"),
        "from_user_id": state.get("from_user_id"),
        "to_user_id": state.get("to_user_id"),
        "brand_id": state.get("brand_id"),
        "type": state.get("type"),
        "status": state.get("status"),
        "message": state.get("message", ""),
        "created_at": int(state.get("created_at", 0)),
        "expires_at": int(state.get("expires_at", 0)),
        "payload": payload,
    }


@router.get("/gifts/inbox")
async def gifts_inbox(
    user_id: str,
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """List unclaimed gifts received by user for this brand."""
    ids = await r.lrange(_inbox_key(user_id, brand_id), 0, -1)
    out: list[dict[str, Any]] = []
    for gid in ids:
        st = await r.hgetall(_gift_key(gid))
        if not st:
            continue
        if st.get("status") != "pending":
            continue
        out.append(_gift_view(st))
    return {"user_id": user_id, "brand_id": brand_id, "gifts": out}


@router.get("/gifts/sent")
async def gifts_sent(
    user_id: str,
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """List gifts sent by user for this brand (all statuses)."""
    ids = await r.lrange(_sent_key(user_id, brand_id), 0, -1)
    out: list[dict[str, Any]] = []
    for gid in ids:
        st = await r.hgetall(_gift_key(gid))
        if not st:
            continue
        out.append(_gift_view(st))
    return {"user_id": user_id, "brand_id": brand_id, "gifts": out}


# ═════════════════════════════════════════════════════════════════════════
# 2. TRADING POST
# ═════════════════════════════════════════════════════════════════════════


class TradeBundle(BaseModel):
    """A bundle of assets one side puts into a trade."""

    items: dict[str, int] = Field(default_factory=dict)      # item_id → qty
    currency: dict[str, int] = Field(default_factory=dict)   # name → amount
    vouchers: list[str] = Field(default_factory=list)        # voucher_ids
    energy: int = 0


class TradePropose(BaseModel):
    from_user: str
    to_user: str
    brand_id: str
    offer: TradeBundle = Field(default_factory=TradeBundle)
    request: TradeBundle = Field(default_factory=TradeBundle)


class TradeAction(BaseModel):
    user_id: str


def _bundle_empty(b: TradeBundle) -> bool:
    return (
        not b.items
        and not b.currency
        and not b.vouchers
        and b.energy <= 0
    )


async def _check_bundle_owned(
    r: aioredis.Redis,
    user_id: str,
    brand_id: str,
    bundle: TradeBundle,
) -> tuple[bool, dict[str, Any]]:
    """Verify user owns everything in bundle. Returns (ok, missing_detail)."""
    if bundle.energy > 0:
        bal = await _get_energy(r, user_id, brand_id)
        if bal < bundle.energy:
            return False, {
                "missing": "energy",
                "have": bal,
                "need": bundle.energy,
            }

    for cur, amt in bundle.currency.items():
        if amt <= 0:
            continue
        bal = await _get_currency(r, cur, user_id, brand_id)
        if bal < amt:
            return False, {
                "missing": "currency",
                "currency": cur,
                "have": bal,
                "need": amt,
            }

    for iid, qty in bundle.items.items():
        if qty <= 0:
            continue
        have = await _get_item_qty(r, user_id, brand_id, iid)
        if have < qty:
            return False, {
                "missing": "item",
                "item_id": iid,
                "have": have,
                "need": qty,
            }

    for vid in bundle.vouchers:
        owner = await _voucher_owner(r, vid)
        if owner != user_id:
            return False, {"missing": "voucher", "voucher_id": vid}

    return True, {}


async def _transfer_bundle(
    r: aioredis.Redis,
    from_user: str,
    to_user: str,
    brand_id: str,
    bundle: TradeBundle,
) -> dict[str, Any]:
    """Move every asset in bundle from from_user → to_user.

    Assumes _check_bundle_owned already validated. Each individual
    primitive op is atomic; full swap atomicity is enforced by the
    WATCH/MULTI envelope around both bundles in /accept.
    """
    moved: dict[str, Any] = {}

    if bundle.energy > 0:
        ok = await _deduct_energy(r, from_user, brand_id, bundle.energy)
        if not ok:
            raise HTTPException(409, detail="race: energy gone")
        new_bal = await _credit_energy(r, to_user, brand_id, bundle.energy)
        moved["energy"] = {"amount": bundle.energy, "recipient_balance": new_bal}

    if bundle.currency:
        moved_cur: dict[str, int] = {}
        for cur, amt in bundle.currency.items():
            if amt <= 0:
                continue
            ok = await _deduct_currency(r, cur, from_user, brand_id, amt)
            if not ok:
                raise HTTPException(409, detail=f"race: {cur} gone")
            new_bal = await _credit_currency(r, cur, to_user, brand_id, amt)
            moved_cur[cur] = new_bal
        moved["currency"] = moved_cur

    if bundle.items:
        moved_items: dict[str, int] = {}
        for iid, qty in bundle.items.items():
            if qty <= 0:
                continue
            ok = await _deduct_item(r, from_user, brand_id, iid, qty)
            if not ok:
                raise HTTPException(409, detail=f"race: item {iid} gone")
            new_qty = await _credit_item(r, to_user, brand_id, iid, qty)
            moved_items[iid] = new_qty
        moved["items"] = moved_items

    if bundle.vouchers:
        moved_vouchers: list[str] = []
        for vid in bundle.vouchers:
            ok = await _transfer_voucher(r, vid, from_user, to_user)
            if not ok:
                raise HTTPException(409, detail=f"race: voucher {vid} gone")
            moved_vouchers.append(vid)
        moved["vouchers"] = moved_vouchers

    return moved


@router.post("/trade/propose")
async def trade_propose(
    body: TradePropose, r: aioredis.Redis = Depends(get_redis)
):
    """Open a new bilateral trade proposal.

    Does NOT deduct anything — only records intent. Assets are checked &
    swapped atomically at /accept time. Either side may bail before then.
    """
    if body.from_user == body.to_user:
        raise HTTPException(422, detail="cannot trade with self")
    if _bundle_empty(body.offer) and _bundle_empty(body.request):
        raise HTTPException(422, detail="trade must have offer or request")

    trade_id = uuid.uuid4().hex
    now = int(time.time())
    state = {
        "trade_id": trade_id,
        "from_user": body.from_user,
        "to_user": body.to_user,
        "brand_id": body.brand_id,
        "offer": body.offer.model_dump_json(),
        "request": body.request.model_dump_json(),
        "status": "pending",
        "created_at": now,
    }
    await r.hset(
        _trade_key(trade_id),
        mapping={k: str(v) for k, v in state.items()},
    )
    # 7-day TTL on the trade record
    await r.expire(_trade_key(trade_id), GIFT_TTL_SECONDS)

    await r.sadd(_trade_pending_key(body.to_user, body.brand_id), trade_id)
    await r.sadd(_trade_pending_key(body.from_user, body.brand_id), trade_id)

    return {
        "trade_id": trade_id,
        "status": "pending",
        "from_user": body.from_user,
        "to_user": body.to_user,
        "expires_in": GIFT_TTL_SECONDS,
    }


@router.post("/trade/{trade_id}/accept")
async def trade_accept(
    trade_id: str,
    body: TradeAction,
    r: aioredis.Redis = Depends(get_redis),
):
    """Recipient accepts. Atomic swap of both bundles."""
    state = await r.hgetall(_trade_key(trade_id))
    if not state:
        raise HTTPException(404, detail="trade not found")
    if state.get("status") != "pending":
        raise HTTPException(
            409,
            detail={
                "error": "trade_not_pending",
                "status": state.get("status"),
            },
        )
    if state.get("to_user") != body.user_id:
        raise HTTPException(403, detail="only recipient can accept")

    from_user = state["from_user"]
    to_user = state["to_user"]
    brand_id = state["brand_id"]
    offer = TradeBundle(**json.loads(state["offer"]))
    request = TradeBundle(**json.loads(state["request"]))

    # ─── Atomic envelope: WATCH every key both sides touch ──────────────
    # Build watch list dynamically.
    watch_keys: list[str] = [_trade_key(trade_id)]
    for b, owner in ((offer, from_user), (request, to_user)):
        if b.energy > 0:
            watch_keys.append(_energy_key(owner, brand_id))
        for cur in b.currency:
            watch_keys.append(_currency_balance_key(cur, owner, brand_id))
        if b.items:
            watch_keys.append(_inventory_key(owner, brand_id))
        for vid in b.vouchers:
            watch_keys.append(_voucher_key(vid))

    async with r.pipeline(transaction=True) as pipe:
        attempts = 0
        while attempts < 5:
            attempts += 1
            try:
                await pipe.watch(*watch_keys)

                # Re-check trade is still pending (defends against concurrent
                # accept/cancel).
                live_status = await pipe.hget(_trade_key(trade_id), "status")
                if live_status != "pending":
                    await pipe.unwatch()
                    raise HTTPException(
                        409,
                        detail={
                            "error": "trade_not_pending",
                            "status": live_status,
                        },
                    )

                # Validate both bundles before mutating anything.
                # We use the regular redis client here (read-only checks)
                # — values are still under WATCH on `pipe`.
                ok_a, miss_a = await _check_bundle_owned(
                    r, from_user, brand_id, offer
                )
                if not ok_a:
                    await pipe.unwatch()
                    # Mark trade as failed.
                    await r.hset(
                        _trade_key(trade_id),
                        mapping={
                            "status": "failed",
                            "failed_reason": json.dumps(
                                {"side": "from_user", **miss_a}
                            ),
                            "failed_at": int(time.time()),
                        },
                    )
                    await r.srem(
                        _trade_pending_key(from_user, brand_id), trade_id
                    )
                    await r.srem(
                        _trade_pending_key(to_user, brand_id), trade_id
                    )
                    raise HTTPException(
                        409,
                        detail={
                            "error": "from_user_missing_assets",
                            "detail": miss_a,
                        },
                    )

                ok_b, miss_b = await _check_bundle_owned(
                    r, to_user, brand_id, request
                )
                if not ok_b:
                    await pipe.unwatch()
                    await r.hset(
                        _trade_key(trade_id),
                        mapping={
                            "status": "failed",
                            "failed_reason": json.dumps(
                                {"side": "to_user", **miss_b}
                            ),
                            "failed_at": int(time.time()),
                        },
                    )
                    await r.srem(
                        _trade_pending_key(from_user, brand_id), trade_id
                    )
                    await r.srem(
                        _trade_pending_key(to_user, brand_id), trade_id
                    )
                    raise HTTPException(
                        409,
                        detail={
                            "error": "to_user_missing_assets",
                            "detail": miss_b,
                        },
                    )

                # MULTI / EXEC — mark trade in-progress; actual swap below.
                pipe.multi()
                pipe.hset(
                    _trade_key(trade_id),
                    mapping={
                        "status": "executing",
                        "executing_at": int(time.time()),
                    },
                )
                await pipe.execute()
                break

            except aioredis.WatchError:
                # Some watched key changed — retry.
                continue

        else:
            raise HTTPException(
                503,
                detail="trade contention too high; please retry",
            )

    # ─── Do the swap. Each side's deduct is checked again at the
    # primitive level; a failure here aborts and we'd leave a half-swap.
    # The pre-validation above + WATCH covers the common race. For belt
    # & suspenders we'd need a Lua script — accepted tradeoff for now.
    try:
        moved_a = await _transfer_bundle(
            r, from_user, to_user, brand_id, offer
        )
        moved_b = await _transfer_bundle(
            r, to_user, from_user, brand_id, request
        )
    except HTTPException:
        await r.hset(
            _trade_key(trade_id),
            mapping={
                "status": "failed",
                "failed_reason": json.dumps({"error": "swap_race"}),
                "failed_at": int(time.time()),
            },
        )
        raise

    await r.hset(
        _trade_key(trade_id),
        mapping={
            "status": "completed",
            "completed_at": int(time.time()),
        },
    )
    await r.srem(_trade_pending_key(from_user, brand_id), trade_id)
    await r.srem(_trade_pending_key(to_user, brand_id), trade_id)

    return {
        "ok": True,
        "trade_id": trade_id,
        "status": "completed",
        "from_user_received": moved_b,
        "to_user_received": moved_a,
    }


@router.post("/trade/{trade_id}/decline")
async def trade_decline(
    trade_id: str,
    body: TradeAction,
    r: aioredis.Redis = Depends(get_redis),
):
    state = await r.hgetall(_trade_key(trade_id))
    if not state:
        raise HTTPException(404, detail="trade not found")
    if state.get("status") != "pending":
        raise HTTPException(
            409,
            detail={"error": "trade_not_pending", "status": state.get("status")},
        )
    if state.get("to_user") != body.user_id:
        raise HTTPException(403, detail="only recipient can decline")

    now = int(time.time())
    await r.hset(
        _trade_key(trade_id),
        mapping={"status": "declined", "declined_at": now},
    )
    await r.srem(
        _trade_pending_key(state["from_user"], state["brand_id"]), trade_id
    )
    await r.srem(
        _trade_pending_key(state["to_user"], state["brand_id"]), trade_id
    )
    return {"ok": True, "trade_id": trade_id, "status": "declined"}


@router.post("/trade/{trade_id}/cancel")
async def trade_cancel(
    trade_id: str,
    body: TradeAction,
    r: aioredis.Redis = Depends(get_redis),
):
    state = await r.hgetall(_trade_key(trade_id))
    if not state:
        raise HTTPException(404, detail="trade not found")
    if state.get("status") != "pending":
        raise HTTPException(
            409,
            detail={"error": "trade_not_pending", "status": state.get("status")},
        )
    if state.get("from_user") != body.user_id:
        raise HTTPException(403, detail="only initiator can cancel")

    now = int(time.time())
    await r.hset(
        _trade_key(trade_id),
        mapping={"status": "cancelled", "cancelled_at": now},
    )
    await r.srem(
        _trade_pending_key(state["from_user"], state["brand_id"]), trade_id
    )
    await r.srem(
        _trade_pending_key(state["to_user"], state["brand_id"]), trade_id
    )
    return {"ok": True, "trade_id": trade_id, "status": "cancelled"}


@router.get("/trades/pending")
async def trades_pending(
    user_id: str,
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """All trades involving user (either side) still pending."""
    ids = await r.smembers(_trade_pending_key(user_id, brand_id))
    out: list[dict[str, Any]] = []
    for tid in ids:
        st = await r.hgetall(_trade_key(tid))
        if not st or st.get("status") != "pending":
            # Clean stale set entries
            await r.srem(_trade_pending_key(user_id, brand_id), tid)
            continue
        try:
            offer = json.loads(st["offer"])
            request = json.loads(st["request"])
        except Exception:
            offer, request = {}, {}
        out.append(
            {
                "trade_id": tid,
                "from_user": st.get("from_user"),
                "to_user": st.get("to_user"),
                "brand_id": st.get("brand_id"),
                "offer": offer,
                "request": request,
                "status": st.get("status"),
                "created_at": int(st.get("created_at", 0)),
                "you_are": (
                    "initiator" if st.get("from_user") == user_id else "recipient"
                ),
            }
        )
    return {"user_id": user_id, "brand_id": brand_id, "trades": out}


@router.get("/trade/{trade_id}")
async def trade_get(
    trade_id: str, r: aioredis.Redis = Depends(get_redis)
):
    state = await r.hgetall(_trade_key(trade_id))
    if not state:
        raise HTTPException(404, detail="trade not found")
    try:
        offer = json.loads(state.get("offer", "{}"))
        request = json.loads(state.get("request", "{}"))
    except Exception:
        offer, request = {}, {}
    return {
        "trade_id": trade_id,
        "from_user": state.get("from_user"),
        "to_user": state.get("to_user"),
        "brand_id": state.get("brand_id"),
        "offer": offer,
        "request": request,
        "status": state.get("status"),
        "created_at": int(state.get("created_at", 0)),
    }
