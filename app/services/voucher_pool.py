"""Cross-brand voucher pooling service.

This is the **network-effect lock-in** primitive: a voucher minted at
brand A can be redeemed at brand B if both brands are members of the
same *voucher pool*. Pools are explicit, opt-in membership contracts —
unlike the legacy ``master:{mid}:voucher_network`` policy in
``vouchers.py`` which only spans a single multi-store master account.

Why a separate service?
~~~~~~~~~~~~~~~~~~~~~~~

The existing ``cross_store_router`` handles **intra-master** redemption
(老王's 10 bubble-tea stores). Cross-brand pooling is a fundamentally
different shape:

* Membership crosses master account boundaries — Toast Box (master M1)
  and Ya Kun (master M2) can share a pool.
* Reciprocity is bilateral and ratio'd — 1 Toast Box voucher might equal
  0.8 Ya Kun vouchers.
* Restrictions are pool-scoped — "same-district only" / "same-cuisine".
* Settlement is **net-position weekly** — not real-time as with
  intra-master where the issuer absorbs the cost.

State model
~~~~~~~~~~~

All state lives in Redis (durable through the same flush discipline as
payouts/vouchers). Keys::

    pool:{pool_id}                       HASH    pool config
    pool:{pool_id}:brands                SET     member brand_ids
    pool:discovery                       ZSET    score=created_at,
                                                 member=pool_id (public)
    brand:{bid}:pools                    SET     pool_ids the brand is in
    pool_voucher:{vid}                   HASH    pooled voucher state
    pool_voucher:{vid}:audit             LIST    JSON events
    pool:{pool_id}:issued:{bid}          ZSET    score=ts, member=vid
    pool:{pool_id}:redeemed:{bid}        ZSET    score=ts, member=vid
    pool:{pool_id}:flow:{src_bid}:{dst_bid}  HASH  cumulative
                                              {count, amount_cents}
    pool:{pool_id}:settlement:{week}     HASH    last settlement snapshot
    pool_redeem_idem:{idem}              STRING  → voucher_id (24h TTL)

Atomicity
~~~~~~~~~

``record_redemption`` mirrors the WATCH/MULTI pattern from
``payouts._inter_brand_transfer_impl``: we WATCH the voucher state
key + the per-pool flow hash, validate-under-watch, then MULTI-apply
the redeemed state, flow counters, and audit log entry in one shot.
Two concurrent redemptions of the same voucher cannot both succeed —
the second WATCH will fail and the loop re-checks state. Idempotency on
``transaction_id`` short-circuits the WATCH loop for retry-safe POSTs.

Settlement
~~~~~~~~~~

``compute_net_positions`` aggregates ``pool:{pool_id}:flow:*`` hashes
into a per-brand net position for a given pool. The settlement worker
(see ``app/workers/voucher_pool_settlement_worker.py``) reduces this
to a minimum-edge transfer plan and dispatches each leg through
``payouts._inter_brand_transfer_impl`` with reason
``joint_campaign_settlement`` so the existing ledger picks it up.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from uuid import uuid4

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────

MAX_WATCH_RETRIES = 8
DEFAULT_RECIPROCITY = 1.0  # 1:1 unless a per-edge ratio is set
SETTLEMENT_WEEK_SECONDS = 7 * 24 * 3600
POOLED_VOUCHER_TTL_SECONDS = 90 * 24 * 3600
IDEM_TTL_SECONDS = 24 * 3600

# Reasonable status transition table.
VOUCHER_STATUSES = {"issued", "redeemed", "expired", "void"}

# Restriction keys we know how to evaluate at redemption time.
KNOWN_RESTRICTION_KEYS = {
    "same_district",
    "same_cuisine",
    "min_purchase_cents",
    "max_amount_cents",
    "allowed_brands",  # explicit allow-list override
    "blocked_brands",
}


# ── Redis key helpers ────────────────────────────────────────────────────


def _k_pool(pid: str) -> str:
    return f"pool:{pid}"


def _k_pool_brands(pid: str) -> str:
    return f"pool:{pid}:brands"


def _k_pool_discovery() -> str:
    return "pool:discovery"


def _k_brand_pools(bid: str) -> str:
    return f"brand:{bid}:pools"


def _k_pool_voucher(vid: str) -> str:
    return f"pool_voucher:{vid}"


def _k_pool_voucher_audit(vid: str) -> str:
    return f"pool_voucher:{vid}:audit"


def _k_pool_issued(pid: str, bid: str) -> str:
    return f"pool:{pid}:issued:{bid}"


def _k_pool_redeemed(pid: str, bid: str) -> str:
    return f"pool:{pid}:redeemed:{bid}"


def _k_pool_flow(pid: str, src: str, dst: str) -> str:
    return f"pool:{pid}:flow:{src}:{dst}"


def _k_pool_settlement(pid: str, week: int) -> str:
    return f"pool:{pid}:settlement:{week}"


def _k_pool_redeem_idem(idem: str) -> str:
    return f"pool_redeem_idem:{idem}"


# ── Small utilities ──────────────────────────────────────────────────────


def _now() -> int:
    return int(time.time())


def _new_pool_id() -> str:
    return f"pool_{uuid4().hex[:16]}"


def _new_voucher_id() -> str:
    return f"pvch_{uuid4().hex[:20]}"


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def _loads_or(raw: str | bytes | None, default: Any) -> Any:
    if not raw:
        return default
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def _hash_to_dict(h: dict[str, Any]) -> dict[str, Any]:
    """Decode a Redis hash (bytes-or-str) into a plain dict."""
    out: dict[str, Any] = {}
    for k, v in h.items():
        kk = k.decode() if isinstance(k, bytes) else k
        vv = v.decode() if isinstance(v, bytes) else v
        out[kk] = vv
    return out


def _current_week() -> int:
    """Week number since epoch — used as settlement bucket id."""
    return int(time.time()) // SETTLEMENT_WEEK_SECONDS


# ── Validation ───────────────────────────────────────────────────────────


def _validate_rules(rules: dict[str, Any]) -> dict[str, Any]:
    """Normalise + check the ``rules`` dict supplied at pool creation.

    Unknown restriction keys are kept (forwards-compatible) but logged
    so we can spot typos. Reciprocity ratios are normalised into a
    nested dict ``{src: {dst: ratio}}`` for O(1) lookup at redemption.
    """
    if not isinstance(rules, dict):
        raise ValueError("rules must be a dict")

    out: dict[str, Any] = {}

    restrictions = rules.get("restrictions") or {}
    if not isinstance(restrictions, dict):
        raise ValueError("rules.restrictions must be a dict")
    for k in restrictions:
        if k not in KNOWN_RESTRICTION_KEYS:
            logger.info("voucher_pool unknown restriction key=%s (kept)", k)
    out["restrictions"] = restrictions

    reciprocity = rules.get("reciprocity") or {}
    if not isinstance(reciprocity, dict):
        raise ValueError("rules.reciprocity must be a dict")
    # Permit either {"default": 1.0} flat form, or
    # {"src_brand": {"dst_brand": 0.8}} per-edge form.
    norm_recip: dict[str, Any] = {}
    for k, v in reciprocity.items():
        if isinstance(v, dict):
            norm_recip[k] = {kk: float(vv) for kk, vv in v.items()}
        else:
            norm_recip[k] = float(v)
    out["reciprocity"] = norm_recip

    out["settlement_currency"] = rules.get("settlement_currency", "SGD")
    out["auto_settle"] = bool(rules.get("auto_settle", True))
    return out


def _reciprocity_for(
    rules: dict[str, Any], src_bid: str, dst_bid: str
) -> float:
    """Resolve the ratio applied when ``src_bid``'s voucher is burned
    at ``dst_bid``.

    Order of precedence:
      1. ``rules.reciprocity[src][dst]`` (explicit edge)
      2. ``rules.reciprocity[src]`` (flat per-source ratio)
      3. ``rules.reciprocity["default"]``
      4. :data:`DEFAULT_RECIPROCITY` (1.0)
    """
    recip = rules.get("reciprocity") or {}
    src_entry = recip.get(src_bid)
    if isinstance(src_entry, dict) and dst_bid in src_entry:
        return float(src_entry[dst_bid])
    if isinstance(src_entry, (int, float)):
        return float(src_entry)
    default = recip.get("default")
    if isinstance(default, (int, float)):
        return float(default)
    return DEFAULT_RECIPROCITY


# ── Pool CRUD ────────────────────────────────────────────────────────────


async def create_pool(
    r: aioredis.Redis,
    *,
    brand_ids: list[str],
    district: str,
    name: str,
    rules: dict[str, Any] | None = None,
    pool_id: str | None = None,
    discoverable: bool = True,
) -> dict[str, Any]:
    """Create a new cross-brand voucher pool.

    All ``brand_ids`` are inducted as founding members. A pool can be
    later joined/left atomically (see ``join_pool`` / ``leave_pool``).

    ``rules`` is validated via :func:`_validate_rules`.
    ``discoverable`` controls whether the pool appears in the public
    discovery feed (private invite-only pools are still queryable by
    members through ``brand:{bid}:pools``).
    """
    if not name or not name.strip():
        raise ValueError("name is required")
    if not district or not district.strip():
        raise ValueError("district is required")
    if not isinstance(brand_ids, list) or not brand_ids:
        raise ValueError("brand_ids must be a non-empty list")
    # De-dupe brand IDs while preserving order so test assertions are
    # stable.
    seen: set[str] = set()
    members: list[str] = []
    for b in brand_ids:
        if not isinstance(b, str) or not b.strip():
            raise ValueError("brand_id must be a non-empty string")
        if b not in seen:
            seen.add(b)
            members.append(b)

    norm_rules = _validate_rules(rules or {})
    pid = pool_id or _new_pool_id()

    now = _now()
    record = {
        "pool_id": pid,
        "name": name,
        "district": district,
        "rules": _dumps(norm_rules),
        "created_at": str(now),
        "updated_at": str(now),
        "status": "active",
        "discoverable": "1" if discoverable else "0",
    }

    async with r.pipeline(transaction=True) as pipe:
        pipe.hset(_k_pool(pid), mapping=record)
        for b in members:
            pipe.sadd(_k_pool_brands(pid), b)
            pipe.sadd(_k_brand_pools(b), pid)
        if discoverable:
            pipe.zadd(_k_pool_discovery(), {pid: float(now)})
        await pipe.execute()

    logger.info(
        "voucher_pool.create pool=%s district=%s members=%d",
        pid, district, len(members),
    )
    return {
        "pool_id": pid,
        "name": name,
        "district": district,
        "rules": norm_rules,
        "members": members,
        "created_at": now,
        "status": "active",
        "discoverable": discoverable,
    }


async def get_pool(r: aioredis.Redis, pool_id: str) -> dict[str, Any] | None:
    """Fetch pool config + membership. Returns ``None`` if missing."""
    raw = await r.hgetall(_k_pool(pool_id))
    if not raw:
        return None
    data = _hash_to_dict(raw)
    members = await r.smembers(_k_pool_brands(pool_id))
    members_decoded = sorted(
        m.decode() if isinstance(m, bytes) else m for m in members
    )
    return {
        "pool_id": data.get("pool_id", pool_id),
        "name": data.get("name", ""),
        "district": data.get("district", ""),
        "rules": _loads_or(data.get("rules"), {}),
        "created_at": int(data.get("created_at") or 0),
        "updated_at": int(data.get("updated_at") or 0),
        "status": data.get("status", "active"),
        "discoverable": data.get("discoverable", "1") == "1",
        "members": members_decoded,
    }


async def join_pool(
    r: aioredis.Redis, pool_id: str, brand_id: str
) -> dict[str, Any]:
    """Brand opts into an existing pool. Idempotent."""
    exists = await r.exists(_k_pool(pool_id))
    if not exists:
        raise LookupError(f"pool {pool_id} not found")
    async with r.pipeline(transaction=True) as pipe:
        pipe.sadd(_k_pool_brands(pool_id), brand_id)
        pipe.sadd(_k_brand_pools(brand_id), pool_id)
        pipe.hset(_k_pool(pool_id), "updated_at", str(_now()))
        await pipe.execute()
    logger.info("voucher_pool.join pool=%s brand=%s", pool_id, brand_id)
    return {"pool_id": pool_id, "brand_id": brand_id, "joined": True}


async def leave_pool(
    r: aioredis.Redis, pool_id: str, brand_id: str
) -> dict[str, Any]:
    """Brand exits a pool. Existing pooled vouchers held by users remain
    redeemable at OTHER members until they expire — leaving does not
    revoke outstanding obligations."""
    async with r.pipeline(transaction=True) as pipe:
        pipe.srem(_k_pool_brands(pool_id), brand_id)
        pipe.srem(_k_brand_pools(brand_id), pool_id)
        pipe.hset(_k_pool(pool_id), "updated_at", str(_now()))
        await pipe.execute()
    logger.info("voucher_pool.leave pool=%s brand=%s", pool_id, brand_id)
    return {"pool_id": pool_id, "brand_id": brand_id, "left": True}


async def list_brand_pools(
    r: aioredis.Redis, brand_id: str
) -> list[str]:
    members = await r.smembers(_k_brand_pools(brand_id))
    return sorted(m.decode() if isinstance(m, bytes) else m for m in members)


async def discovery(
    r: aioredis.Redis, *, limit: int = 50
) -> list[dict[str, Any]]:
    """Public discovery feed (newest pools first)."""
    raw_ids = await r.zrevrange(_k_pool_discovery(), 0, max(0, limit - 1))
    out: list[dict[str, Any]] = []
    for rid in raw_ids:
        pid = rid.decode() if isinstance(rid, bytes) else rid
        p = await get_pool(r, pid)
        if p and p.get("status") == "active":
            out.append(p)
    return out


# ── Voucher issue / redeem ───────────────────────────────────────────────


async def issue_pooled_voucher(
    r: aioredis.Redis,
    *,
    user_id: str,
    source_brand_id: str,
    pool_id: str,
    amount_cents: int,
    currency: str = "SGD",
    expires_at: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mint a new pooled voucher held by ``user_id``.

    The voucher's ``redeemable_at`` is implicitly *all current members
    of the pool*, modulated by ``rules.restrictions`` and per-edge
    reciprocity ratios at redemption time.
    """
    if amount_cents <= 0:
        raise ValueError("amount_cents must be > 0")

    pool = await get_pool(r, pool_id)
    if not pool:
        raise LookupError(f"pool {pool_id} not found")
    if source_brand_id not in pool["members"]:
        raise PermissionError(
            f"brand {source_brand_id} is not a member of pool {pool_id}"
        )

    vid = _new_voucher_id()
    now = _now()
    exp = expires_at or (now + POOLED_VOUCHER_TTL_SECONDS)

    record = {
        "voucher_id": vid,
        "pool_id": pool_id,
        "user_id": user_id,
        "source_brand_id": source_brand_id,
        "amount_cents": str(int(amount_cents)),
        "currency": currency,
        "status": "issued",
        "issued_at": str(now),
        "expires_at": str(exp),
        "metadata": _dumps(metadata or {}),
    }

    event = {
        "event": "issued",
        "ts": now,
        "source_brand_id": source_brand_id,
        "amount_cents": amount_cents,
    }

    async with r.pipeline(transaction=True) as pipe:
        pipe.hset(_k_pool_voucher(vid), mapping=record)
        pipe.expire(
            _k_pool_voucher(vid), POOLED_VOUCHER_TTL_SECONDS + 30 * 86400
        )
        pipe.rpush(_k_pool_voucher_audit(vid), _dumps(event))
        pipe.zadd(
            _k_pool_issued(pool_id, source_brand_id),
            {vid: float(now)},
        )
        await pipe.execute()

    logger.info(
        "voucher_pool.issue pool=%s vid=%s user=%s src=%s amount=%d",
        pool_id, vid, user_id, source_brand_id, amount_cents,
    )
    return {
        "voucher_id": vid,
        "pool_id": pool_id,
        "user_id": user_id,
        "source_brand_id": source_brand_id,
        "amount_cents": amount_cents,
        "currency": currency,
        "status": "issued",
        "issued_at": now,
        "expires_at": exp,
        "metadata": metadata or {},
    }


async def get_voucher(
    r: aioredis.Redis, voucher_id: str
) -> dict[str, Any] | None:
    raw = await r.hgetall(_k_pool_voucher(voucher_id))
    if not raw:
        return None
    d = _hash_to_dict(raw)
    return {
        "voucher_id": d.get("voucher_id", voucher_id),
        "pool_id": d.get("pool_id"),
        "user_id": d.get("user_id"),
        "source_brand_id": d.get("source_brand_id"),
        "amount_cents": int(d.get("amount_cents") or 0),
        "currency": d.get("currency", "SGD"),
        "status": d.get("status", "issued"),
        "issued_at": int(d.get("issued_at") or 0),
        "expires_at": int(d.get("expires_at") or 0),
        "redeemed_at": int(d.get("redeemed_at") or 0) or None,
        "redeemed_at_brand_id": d.get("redeemed_at_brand_id") or None,
        "redeemed_transaction_id": d.get("redeemed_transaction_id") or None,
        "metadata": _loads_or(d.get("metadata"), {}),
    }


async def validate_redemption(
    r: aioredis.Redis,
    *,
    voucher_id: str,
    target_brand_id: str,
    target_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check whether ``voucher_id`` is accepted at ``target_brand_id``.

    Returns ``{"ok": True, "credit_amount_cents": int, "ratio": float,
    "pool_id": str}`` on success, or
    ``{"ok": False, "reason": "..."}`` on failure. Never raises for
    user-facing rejection reasons — only raises for invariant
    violations.

    ``target_context`` is an opaque dict supplied by the redeeming
    storefront (district, cuisine, purchase_amount_cents, …) used to
    evaluate ``rules.restrictions``.
    """
    v = await get_voucher(r, voucher_id)
    if not v:
        return {"ok": False, "reason": "voucher_not_found"}
    if v["status"] != "issued":
        return {"ok": False, "reason": f"voucher_{v['status']}"}
    now = _now()
    if v["expires_at"] and v["expires_at"] < now:
        return {"ok": False, "reason": "voucher_expired"}

    pool_id = v["pool_id"]
    pool = await get_pool(r, pool_id)
    if not pool:
        return {"ok": False, "reason": "pool_not_found"}
    if pool.get("status") != "active":
        return {"ok": False, "reason": "pool_inactive"}
    if target_brand_id not in pool["members"]:
        return {"ok": False, "reason": "target_brand_not_in_pool"}

    rules = pool.get("rules") or {}
    restrictions = rules.get("restrictions") or {}
    ctx = target_context or {}

    # same_district: pool's own district must match issuer + target
    # OR the rule is "any" — we keep simple here: respect the pool
    # district for both ends when the flag is on.
    if restrictions.get("same_district") and pool.get("district"):
        # If the caller supplies issuer / target districts use them,
        # otherwise default to the pool's district (trust the pool
        # boundary).
        td = ctx.get("target_district") or pool["district"]
        sd = ctx.get("source_district") or pool["district"]
        if td != sd:
            return {"ok": False, "reason": "district_mismatch"}

    if restrictions.get("same_cuisine"):
        tc = ctx.get("target_cuisine")
        sc = ctx.get("source_cuisine")
        if tc and sc and tc != sc:
            return {"ok": False, "reason": "cuisine_mismatch"}

    min_purchase = restrictions.get("min_purchase_cents")
    if isinstance(min_purchase, (int, float)) and min_purchase > 0:
        purch = ctx.get("purchase_amount_cents") or 0
        if int(purch) < int(min_purchase):
            return {
                "ok": False,
                "reason": "below_min_purchase",
                "min_purchase_cents": int(min_purchase),
            }

    allowed = restrictions.get("allowed_brands")
    if isinstance(allowed, list) and allowed and target_brand_id not in allowed:
        return {"ok": False, "reason": "target_brand_not_allowed"}

    blocked = restrictions.get("blocked_brands")
    if isinstance(blocked, list) and target_brand_id in blocked:
        return {"ok": False, "reason": "target_brand_blocked"}

    src = v["source_brand_id"]
    ratio = _reciprocity_for(rules, src, target_brand_id)
    credit_cents = int(round(int(v["amount_cents"]) * ratio))

    max_amount = restrictions.get("max_amount_cents")
    if isinstance(max_amount, (int, float)) and max_amount > 0:
        credit_cents = min(credit_cents, int(max_amount))

    return {
        "ok": True,
        "credit_amount_cents": credit_cents,
        "ratio": ratio,
        "pool_id": pool_id,
        "source_brand_id": src,
        "target_brand_id": target_brand_id,
        "currency": v["currency"],
    }


async def record_redemption(
    r: aioredis.Redis,
    *,
    voucher_id: str,
    target_brand_id: str,
    transaction_id: str,
    target_context: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Atomically burn the voucher and bump cross-brand flow counters.

    Idempotent on ``idempotency_key`` (defaults to ``transaction_id``).
    On replay the prior result is returned with ``idempotent: True``.

    Atomicity: WATCH on the voucher hash + idempotency key. We
    re-validate under WATCH (status, expiry, pool membership, target
    membership) and refuse the burn if anything has shifted. The MULTI
    block flips status → redeemed, persists target brand + txn,
    appends the audit log, indexes the redemption in the per-brand
    ZSET, and increments the directional flow hash — all in one
    round-trip.

    The bilateral flow hash ``pool:{pool}:flow:{src}:{dst}`` records:
      * ``count`` — number of vouchers burned src→dst
      * ``amount_cents`` — cumulative *credit* (post-ratio) amount

    Settlement reads these hashes; see ``compute_net_positions``.
    """
    idem = idempotency_key or transaction_id
    idem_key = _k_pool_redeem_idem(idem)

    # Fast-path idempotency replay outside the WATCH loop.
    existing = await r.get(idem_key)
    if existing:
        vid_stored = existing.decode() if isinstance(existing, bytes) else existing
        prior = await get_voucher(r, vid_stored)
        if prior and prior["status"] == "redeemed":
            return {
                "ok": True,
                "idempotent": True,
                "voucher_id": vid_stored,
                "target_brand_id": prior["redeemed_at_brand_id"],
                "transaction_id": prior["redeemed_transaction_id"],
                "credit_amount_cents": prior["amount_cents"],
                "pool_id": prior["pool_id"],
            }

    for attempt in range(MAX_WATCH_RETRIES):
        try:
            async with r.pipeline(transaction=True) as pipe:
                await pipe.watch(_k_pool_voucher(voucher_id), idem_key)

                # Re-check idempotency under WATCH.
                claimed = await pipe.get(idem_key)
                if claimed:
                    await pipe.unwatch()
                    cvid = claimed.decode() if isinstance(claimed, bytes) else claimed
                    prior = await get_voucher(r, cvid)
                    return {
                        "ok": True,
                        "idempotent": True,
                        "voucher_id": cvid,
                        "target_brand_id": prior["redeemed_at_brand_id"] if prior else target_brand_id,
                        "transaction_id": prior["redeemed_transaction_id"] if prior else transaction_id,
                        "credit_amount_cents": prior["amount_cents"] if prior else 0,
                        "pool_id": prior["pool_id"] if prior else None,
                    }

                # Validate fully under WATCH so a transfer/void can't race.
                check = await validate_redemption(
                    r,
                    voucher_id=voucher_id,
                    target_brand_id=target_brand_id,
                    target_context=target_context,
                )
                if not check["ok"]:
                    await pipe.unwatch()
                    return check

                v = await get_voucher(r, voucher_id)
                assert v is not None  # narrowed by validate_redemption
                pool_id = v["pool_id"]
                src = v["source_brand_id"]
                credit_cents = check["credit_amount_cents"]
                ratio = check["ratio"]
                now = _now()

                event = {
                    "event": "redeemed",
                    "ts": now,
                    "target_brand_id": target_brand_id,
                    "transaction_id": transaction_id,
                    "credit_amount_cents": credit_cents,
                    "ratio": ratio,
                }

                pipe.multi()
                pipe.hset(
                    _k_pool_voucher(voucher_id),
                    mapping={
                        "status": "redeemed",
                        "redeemed_at": str(now),
                        "redeemed_at_brand_id": target_brand_id,
                        "redeemed_transaction_id": transaction_id,
                        "redeemed_credit_cents": str(credit_cents),
                        "redeemed_ratio": str(ratio),
                    },
                )
                pipe.rpush(_k_pool_voucher_audit(voucher_id), _dumps(event))
                pipe.zadd(
                    _k_pool_redeemed(pool_id, target_brand_id),
                    {voucher_id: float(now)},
                )
                pipe.hincrby(_k_pool_flow(pool_id, src, target_brand_id), "count", 1)
                pipe.hincrby(
                    _k_pool_flow(pool_id, src, target_brand_id),
                    "amount_cents",
                    credit_cents,
                )
                pipe.set(idem_key, voucher_id, ex=IDEM_TTL_SECONDS)
                await pipe.execute()

                logger.info(
                    "voucher_pool.redeem pool=%s vid=%s src=%s dst=%s credit=%d ratio=%.3f",
                    pool_id, voucher_id, src, target_brand_id, credit_cents, ratio,
                )
                return {
                    "ok": True,
                    "voucher_id": voucher_id,
                    "pool_id": pool_id,
                    "source_brand_id": src,
                    "target_brand_id": target_brand_id,
                    "transaction_id": transaction_id,
                    "credit_amount_cents": credit_cents,
                    "ratio": ratio,
                    "redeemed_at": now,
                }
        except aioredis.WatchError:
            logger.info(
                "voucher_pool.redeem WATCH conflict vid=%s attempt=%d",
                voucher_id, attempt + 1,
            )
            continue

    return {"ok": False, "reason": "contention", "retries": MAX_WATCH_RETRIES}


# ── Discovery / UX support ───────────────────────────────────────────────


async def redemption_options(
    r: aioredis.Redis, voucher_id: str
) -> dict[str, Any]:
    """List every brand where this voucher can be burned, with credit
    amount + ratio. Used by the user-facing voucher card UI."""
    v = await get_voucher(r, voucher_id)
    if not v:
        return {"ok": False, "reason": "voucher_not_found", "options": []}
    pool = await get_pool(r, v["pool_id"])
    if not pool:
        return {"ok": False, "reason": "pool_not_found", "options": []}

    options: list[dict[str, Any]] = []
    rules = pool.get("rules") or {}
    src = v["source_brand_id"]
    for brand_id in pool["members"]:
        if brand_id == src:
            # Don't surface the source brand as an "other shop" option.
            continue
        ratio = _reciprocity_for(rules, src, brand_id)
        credit_cents = int(round(int(v["amount_cents"]) * ratio))
        options.append({
            "brand_id": brand_id,
            "credit_amount_cents": credit_cents,
            "ratio": ratio,
            "currency": v["currency"],
        })

    return {
        "ok": True,
        "voucher_id": voucher_id,
        "pool_id": v["pool_id"],
        "pool_name": pool["name"],
        "district": pool["district"],
        "options_count": len(options),
        "options": options,
    }


# ── Pool value / net position / settlement ───────────────────────────────


async def _flow_pair(
    r: aioredis.Redis, pool_id: str, src: str, dst: str
) -> tuple[int, int]:
    raw = await r.hgetall(_k_pool_flow(pool_id, src, dst))
    if not raw:
        return 0, 0
    d = _hash_to_dict(raw)
    return int(d.get("count") or 0), int(d.get("amount_cents") or 0)


async def compute_pool_value(
    r: aioredis.Redis, brand_id: str
) -> dict[str, Any]:
    """How much *incoming traffic* (in cents redeemed) ``brand_id``
    gets from being in its pools, summed across every pool it's a
    member of and every other member that has burned vouchers at it.

    Returns a per-pool breakdown plus a global total — this is the
    "show me the money" view a brand sees on their pool dashboard.
    """
    pool_ids = await list_brand_pools(r, brand_id)
    per_pool: list[dict[str, Any]] = []
    total_incoming_cents = 0
    total_outgoing_cents = 0
    total_incoming_count = 0
    total_outgoing_count = 0

    for pid in pool_ids:
        pool = await get_pool(r, pid)
        if not pool:
            continue
        in_cents = 0
        out_cents = 0
        in_count = 0
        out_count = 0
        for other in pool["members"]:
            if other == brand_id:
                continue
            # Vouchers issued by ``other`` burned at ``brand_id`` = incoming
            cnt_in, amt_in = await _flow_pair(r, pid, other, brand_id)
            in_count += cnt_in
            in_cents += amt_in
            # Vouchers issued by ``brand_id`` burned at ``other`` = outgoing
            cnt_out, amt_out = await _flow_pair(r, pid, brand_id, other)
            out_count += cnt_out
            out_cents += amt_out
        per_pool.append({
            "pool_id": pid,
            "pool_name": pool["name"],
            "district": pool["district"],
            "incoming_count": in_count,
            "incoming_cents": in_cents,
            "outgoing_count": out_count,
            "outgoing_cents": out_cents,
            "net_cents": in_cents - out_cents,
        })
        total_incoming_count += in_count
        total_incoming_cents += in_cents
        total_outgoing_count += out_count
        total_outgoing_cents += out_cents

    return {
        "brand_id": brand_id,
        "pool_count": len(pool_ids),
        "totals": {
            "incoming_count": total_incoming_count,
            "incoming_cents": total_incoming_cents,
            "outgoing_count": total_outgoing_count,
            "outgoing_cents": total_outgoing_cents,
            "net_cents": total_incoming_cents - total_outgoing_cents,
        },
        "per_pool": per_pool,
    }


async def net_position(
    r: aioredis.Redis, *, pool_id: str, brand_id: str
) -> dict[str, Any]:
    """One brand's net settlement position inside one pool.

    ``net_cents > 0`` means the brand is owed money (more vouchers were
    burned at it than its own users burned elsewhere). ``< 0`` means
    the brand owes the pool.
    """
    pool = await get_pool(r, pool_id)
    if not pool:
        raise LookupError(f"pool {pool_id} not found")
    if brand_id not in pool["members"]:
        # Allow query for ex-members so settlement can finish their
        # outstanding obligations.
        pass

    incoming_cents = 0
    outgoing_cents = 0
    incoming_count = 0
    outgoing_count = 0
    other_brands: set[str] = set(pool["members"])
    # Include any historical counter-party recorded in flow keys —
    # critical when a brand has since left the pool.
    pattern = f"pool:{pool_id}:flow:*"
    async for key in r.scan_iter(match=pattern):
        k = key.decode() if isinstance(key, bytes) else key
        try:
            _, _, _, src, dst = k.split(":")
        except ValueError:
            continue
        other_brands.add(src)
        other_brands.add(dst)

    for other in other_brands:
        if other == brand_id:
            continue
        cnt_in, amt_in = await _flow_pair(r, pool_id, other, brand_id)
        incoming_count += cnt_in
        incoming_cents += amt_in
        cnt_out, amt_out = await _flow_pair(r, pool_id, brand_id, other)
        outgoing_count += cnt_out
        outgoing_cents += amt_out

    return {
        "pool_id": pool_id,
        "brand_id": brand_id,
        "incoming_count": incoming_count,
        "incoming_cents": incoming_cents,
        "outgoing_count": outgoing_count,
        "outgoing_cents": outgoing_cents,
        "net_cents": incoming_cents - outgoing_cents,
        "currency": pool.get("rules", {}).get("settlement_currency", "SGD"),
    }


async def compute_net_positions(
    r: aioredis.Redis, pool_id: str
) -> dict[str, Any]:
    """Pool-wide settlement matrix: each member's net position.

    Returns ``{"pool_id", "as_of", "currency", "positions": [{brand_id,
    net_cents, ...}], "edges": [{src, dst, count, amount_cents}]}``.

    The settlement worker consumes ``positions`` and emits a minimal
    set of inter-brand transfers that zero them out.
    """
    pool = await get_pool(r, pool_id)
    if not pool:
        raise LookupError(f"pool {pool_id} not found")

    # Walk every flow edge in this pool so positions reflect all
    # historical counter-parties (members + ex-members).
    edges: list[dict[str, Any]] = []
    pattern = f"pool:{pool_id}:flow:*"
    brand_in: dict[str, int] = {}
    brand_out: dict[str, int] = {}
    brand_in_count: dict[str, int] = {}
    brand_out_count: dict[str, int] = {}

    async for key in r.scan_iter(match=pattern):
        k = key.decode() if isinstance(key, bytes) else key
        try:
            _, _, _, src, dst = k.split(":")
        except ValueError:
            continue
        cnt, amt = await _flow_pair(r, pool_id, src, dst)
        if cnt == 0 and amt == 0:
            continue
        edges.append({
            "src": src,
            "dst": dst,
            "count": cnt,
            "amount_cents": amt,
        })
        brand_in[dst] = brand_in.get(dst, 0) + amt
        brand_out[src] = brand_out.get(src, 0) + amt
        brand_in_count[dst] = brand_in_count.get(dst, 0) + cnt
        brand_out_count[src] = brand_out_count.get(src, 0) + cnt

    all_brands = set(pool["members"]) | set(brand_in) | set(brand_out)
    positions: list[dict[str, Any]] = []
    for b in sorted(all_brands):
        i = brand_in.get(b, 0)
        o = brand_out.get(b, 0)
        positions.append({
            "brand_id": b,
            "incoming_cents": i,
            "outgoing_cents": o,
            "incoming_count": brand_in_count.get(b, 0),
            "outgoing_count": brand_out_count.get(b, 0),
            "net_cents": i - o,
        })

    return {
        "pool_id": pool_id,
        "as_of": _now(),
        "currency": pool.get("rules", {}).get("settlement_currency", "SGD"),
        "edges": edges,
        "positions": positions,
    }


def plan_settlement_transfers(
    positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reduce a list of net positions into a minimal set of transfers.

    Greedy match-largest-debtor-to-largest-creditor algorithm — for N
    parties produces at most N-1 transfers and zeroes every position.
    Pure function so it's trivially unit-testable from the worker.
    """
    creditors = sorted(
        [p for p in positions if p["net_cents"] > 0],
        key=lambda x: -x["net_cents"],
    )
    debtors = sorted(
        [p for p in positions if p["net_cents"] < 0],
        key=lambda x: x["net_cents"],
    )

    # Mutable copies — we'll consume from these.
    cred = [(c["brand_id"], int(c["net_cents"])) for c in creditors]
    debt = [(d["brand_id"], -int(d["net_cents"])) for d in debtors]

    transfers: list[dict[str, Any]] = []
    i = j = 0
    while i < len(debt) and j < len(cred):
        d_bid, d_amt = debt[i]
        c_bid, c_amt = cred[j]
        pay = min(d_amt, c_amt)
        if pay > 0:
            transfers.append({
                "from_brand_id": d_bid,
                "to_brand_id": c_bid,
                "amount_cents": pay,
            })
        d_amt -= pay
        c_amt -= pay
        debt[i] = (d_bid, d_amt)
        cred[j] = (c_bid, c_amt)
        if d_amt == 0:
            i += 1
        if c_amt == 0:
            j += 1
    return transfers


async def snapshot_settlement(
    r: aioredis.Redis,
    pool_id: str,
    *,
    week: int | None = None,
) -> dict[str, Any]:
    """Compute + persist a settlement snapshot for a given week.

    Idempotent on (pool_id, week). Returns the snapshot. The worker
    invokes this then dispatches the transfer plan through
    payouts._inter_brand_transfer_impl.
    """
    wk = week if week is not None else _current_week()
    matrix = await compute_net_positions(r, pool_id)
    plan = plan_settlement_transfers(matrix["positions"])

    snapshot = {
        "pool_id": pool_id,
        "week": wk,
        "as_of": matrix["as_of"],
        "currency": matrix["currency"],
        "positions": matrix["positions"],
        "edges": matrix["edges"],
        "transfer_plan": plan,
    }
    await r.hset(
        _k_pool_settlement(pool_id, wk),
        mapping={"data": _dumps(snapshot), "computed_at": str(_now())},
    )
    return snapshot


async def get_settlement_snapshot(
    r: aioredis.Redis, pool_id: str, week: int
) -> dict[str, Any] | None:
    raw = await r.hgetall(_k_pool_settlement(pool_id, week))
    if not raw:
        return None
    d = _hash_to_dict(raw)
    return _loads_or(d.get("data"), None)


__all__ = [
    "create_pool",
    "get_pool",
    "join_pool",
    "leave_pool",
    "list_brand_pools",
    "discovery",
    "issue_pooled_voucher",
    "get_voucher",
    "validate_redemption",
    "record_redemption",
    "redemption_options",
    "compute_pool_value",
    "net_position",
    "compute_net_positions",
    "plan_settlement_transfers",
    "snapshot_settlement",
    "get_settlement_snapshot",
    "_current_week",
    "POOLED_VOUCHER_TTL_SECONDS",
    "DEFAULT_RECIPROCITY",
]
