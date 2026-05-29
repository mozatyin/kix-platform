"""Cross-brand partnership router.

Allows two **independent** brands (potentially owned by different masters)
to formally agree on voucher exchange, joint campaigns, or cross-promotion.
The classic motivating case: 老李's book-club brand wants to co-promote with
a coffee-shop chain so members can redeem a free latte on their second visit.

This module is intentionally distinct from the master/brand hierarchy:

  * `master_accounts` covers HQ → store rollups (single owner, many outlets).
  * `voucher` / `voucher_builder` cover a single brand's own templates.
  * **Partnerships** cover *peer* brands with different owners co-signing
    an agreement and exchanging value across the boundary.

Lifecycle (state machine)
--------------------------
    proposed   ──signed by both──▶  active
       │
       ├─ one side rejects        ▶  rejected      (terminal)
       │
       ├─ TTL hits without accept ▶  expired       (terminal)
       │
       │                             active
       │                                │
       │                                └─ either party terminate ▶ terminated
       └─────────────────────────────────────────────────────────  (terminal)

Redis schema
------------
    partnership:{pid}                  HASH
        proposer_brand_id, target_brand_id, type, terms_json,
        status, proposer_signed_at, target_signed_at,
        proposer_signatory_user_id, target_signatory_user_id,
        created_at, expires_at, terminated_at, terminated_by,
        terminate_reason, reject_reason, evidence_url, notes

    brand:{bid}:partnerships           SET    pids the brand is party to
    partnership:{pid}:bridged_vouchers LIST   JSON {voucher_id, recipient_user_id, at}
    partnership:{pid}:stats            HASH   {vouchers_exchanged,
                                                conversions_attributed,
                                                gmv_generated_cents,
                                                joint_campaigns_count}
    partnership:{pid}:joint_campaigns  SET    campaign_ids spawned by this pship

Integration points (try/except, never break callers)
----------------------------------------------------
  * `vouchers.redeem`     — if a voucher's `redeemable_at` resolves to
                            `"partnership:{pid}"` the redeemer must call
                            `assert_active(pid)` before allowing.
  * `attribution.track`   — if a conversion crosses a partnership bridge,
                            commissions should be split per terms.

These hooks live in this module as helper coroutines (`assert_active`,
`record_conversion`); other routers may import them defensively.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, HttpUrl

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ────────────────────────────────────────────────────────────

PartnershipType = Literal[
    "voucher_exchange",
    "joint_campaign",
    "cross_promotion",
    "co_marketing",
]
VALID_TYPES: set[str] = {
    "voucher_exchange",
    "joint_campaign",
    "cross_promotion",
    "co_marketing",
}

PartnershipStatus = Literal[
    "proposed", "active", "rejected", "expired", "terminated"
]
TERMINAL_STATES: set[str] = {"rejected", "expired", "terminated"}

AttributionCredit = Literal["first_touch", "split_50_50", "last_touch"]
VoucherAcceptance = Literal["all", "specific_templates", "none"]

# Defaults
DEFAULT_PROPOSAL_TTL_DAYS = 14
DEFAULT_DURATION_DAYS = 90
MAX_DURATION_DAYS = 365 * 2
MAX_NOTES_LEN = 2048
MAX_BRIDGED_LIST = 1000


# ── Pydantic models ──────────────────────────────────────────────────────


class PartnershipTerms(BaseModel):
    duration_days: int = Field(default=DEFAULT_DURATION_DAYS, ge=1, le=MAX_DURATION_DAYS)
    exclusivity: bool = False
    commission_split_bps: int | None = Field(
        default=None,
        ge=0,
        le=10_000,
        description="basis points (0-10000) of commission to proposer; "
                    "remainder goes to target. Only meaningful when both "
                    "parties share attribution.",
    )
    voucher_acceptance_policy: VoucherAcceptance = "all"
    acceptable_template_ids: list[str] = Field(default_factory=list)
    attribution_credit: AttributionCredit = "split_50_50"
    cap_users_per_day: int | None = Field(default=None, ge=1)


class ProposeRequest(BaseModel):
    proposer_brand_id: str = Field(..., min_length=1)
    target_brand_id: str = Field(..., min_length=1)
    type: PartnershipType
    terms: PartnershipTerms = Field(default_factory=PartnershipTerms)
    proposer_signatory_user_id: str = Field(..., min_length=1)
    evidence_url: HttpUrl | None = None
    notes: str | None = Field(default=None, max_length=MAX_NOTES_LEN)
    proposal_ttl_days: int = Field(default=DEFAULT_PROPOSAL_TTL_DAYS, ge=1, le=60)


class AcceptRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    signatory_user_id: str = Field(..., min_length=1)


class RejectRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    reason: str | None = Field(default=None, max_length=MAX_NOTES_LEN)


class TerminateRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    reason: str | None = Field(default=None, max_length=MAX_NOTES_LEN)


class VoucherBridgeRequest(BaseModel):
    brand_id: str = Field(
        ..., min_length=1,
        description="brand id of the party issuing the voucher (must be one "
                    "of the two partners).",
    )
    voucher_id: str = Field(..., min_length=1)
    recipient_user_id: str = Field(..., min_length=1)


class JointCampaignRequest(BaseModel):
    campaign: dict[str, Any]
    contribution_split_bps: dict[str, int] = Field(
        ...,
        description="brand_id → basis points of campaign budget. Must sum to "
                    "10000 and cover exactly the two partner brand_ids.",
    )


# ── Key helpers ──────────────────────────────────────────────────────────


def _k_partnership(pid: str) -> str:
    return f"partnership:{pid}"


def _k_brand_partnerships(bid: str) -> str:
    return f"brand:{bid}:partnerships"


def _k_bridged_vouchers(pid: str) -> str:
    return f"partnership:{pid}:bridged_vouchers"


def _k_stats(pid: str) -> str:
    return f"partnership:{pid}:stats"


def _k_joint_campaigns(pid: str) -> str:
    return f"partnership:{pid}:joint_campaigns"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_epoch() -> int:
    return int(time.time())


# ── Helpers ──────────────────────────────────────────────────────────────


async def _load(r: aioredis.Redis, pid: str) -> dict[str, str]:
    raw = await r.hgetall(_k_partnership(pid))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Partnership {pid} not found",
        )
    return raw


def _parties(record: dict[str, str]) -> tuple[str, str]:
    return record["proposer_brand_id"], record["target_brand_id"]


def _assert_party(record: dict[str, str], brand_id: str) -> str:
    """Return 'proposer' or 'target' depending on which side brand_id is."""
    p, t = _parties(record)
    if brand_id == p:
        return "proposer"
    if brand_id == t:
        return "target"
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"brand_id {brand_id} is not a party to this partnership",
    )


def _public_view(pid: str, record: dict[str, str]) -> dict[str, Any]:
    try:
        terms = json.loads(record.get("terms_json", "{}"))
    except json.JSONDecodeError:
        terms = {}
    return {
        "partnership_id": pid,
        "proposer_brand_id": record.get("proposer_brand_id"),
        "target_brand_id": record.get("target_brand_id"),
        "type": record.get("type"),
        "status": record.get("status"),
        "terms": terms,
        "proposer_signatory_user_id": record.get("proposer_signatory_user_id"),
        "target_signatory_user_id": record.get("target_signatory_user_id"),
        "proposer_signed_at": record.get("proposer_signed_at"),
        "target_signed_at": record.get("target_signed_at"),
        "created_at": record.get("created_at"),
        "expires_at": record.get("expires_at"),
        "terminated_at": record.get("terminated_at"),
        "terminated_by": record.get("terminated_by"),
        "terminate_reason": record.get("terminate_reason"),
        "reject_reason": record.get("reject_reason"),
        "evidence_url": record.get("evidence_url"),
        "notes": record.get("notes"),
    }


def _expire_if_needed(record: dict[str, str]) -> dict[str, str]:
    """Mutate-in-place: if proposal expired, transition status."""
    if record.get("status") != "proposed":
        return record
    expires_at = record.get("expires_at")
    if not expires_at:
        return record
    try:
        exp_dt = datetime.fromisoformat(expires_at)
    except ValueError:
        return record
    if datetime.now(timezone.utc) >= exp_dt:
        record["status"] = "expired"
    return record


async def _persist_status(
    r: aioredis.Redis, pid: str, record: dict[str, str]
) -> None:
    """Write back fields that may have shifted via expiry transition."""
    await r.hset(_k_partnership(pid), mapping={"status": record["status"]})


# ── Public hooks (importable by other routers) ───────────────────────────


async def assert_active(r: aioredis.Redis, pid: str) -> dict[str, str]:
    """Raise unless partnership is currently active. Used by vouchers."""
    record = await _load(r, pid)
    _expire_if_needed(record)
    if record.get("status") == "expired":
        await _persist_status(r, pid, record)
    if record.get("status") != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Partnership {pid} is not active (status={record.get('status')})",
        )
    return record


async def record_conversion(
    r: aioredis.Redis,
    pid: str,
    *,
    gmv_cents: int = 0,
) -> None:
    """Best-effort conversion counter for attribution integration."""
    try:
        pipe = r.pipeline()
        pipe.hincrby(_k_stats(pid), "conversions_attributed", 1)
        if gmv_cents:
            pipe.hincrby(_k_stats(pid), "gmv_generated_cents", int(gmv_cents))
        await pipe.execute()
    except Exception:  # pragma: no cover — defensive, never break callers
        logger.exception("partnership.record_conversion failed pid=%s", pid)


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("/propose", status_code=status.HTTP_201_CREATED)
async def propose(
    body: ProposeRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Brand A proposes a partnership to Brand B. Returns the new pid."""
    if body.proposer_brand_id == body.target_brand_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="proposer_brand_id and target_brand_id must differ",
        )
    if body.terms.voucher_acceptance_policy == "specific_templates" and \
            not body.terms.acceptable_template_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="acceptable_template_ids required when policy=specific_templates",
        )

    pid = f"pship_{uuid4().hex[:16]}"
    created_at = _now_iso()
    expires_dt = datetime.now(timezone.utc).timestamp() + body.proposal_ttl_days * 86400
    expires_at = datetime.fromtimestamp(expires_dt, tz=timezone.utc).isoformat()

    mapping = {
        "proposer_brand_id": body.proposer_brand_id,
        "target_brand_id": body.target_brand_id,
        "type": body.type,
        "status": "proposed",
        "terms_json": json.dumps(body.terms.model_dump(mode="json")),
        "proposer_signatory_user_id": body.proposer_signatory_user_id,
        "proposer_signed_at": created_at,
        "created_at": created_at,
        "expires_at": expires_at,
    }
    if body.evidence_url is not None:
        mapping["evidence_url"] = str(body.evidence_url)
    if body.notes:
        mapping["notes"] = body.notes

    pipe = r.pipeline()
    pipe.hset(_k_partnership(pid), mapping=mapping)
    pipe.sadd(_k_brand_partnerships(body.proposer_brand_id), pid)
    pipe.sadd(_k_brand_partnerships(body.target_brand_id), pid)
    await pipe.execute()

    return {
        "partnership_id": pid,
        "status": "proposed",
        "expires_at": expires_at,
    }


@router.post("/{partnership_id}/accept")
async def accept(
    partnership_id: str,
    body: AcceptRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    record = await _load(r, partnership_id)
    _expire_if_needed(record)
    if record["status"] == "expired":
        await _persist_status(r, partnership_id, record)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Proposal has expired",
        )
    if record["status"] != "proposed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot accept partnership in status={record['status']}",
        )

    side = _assert_party(record, body.brand_id)
    # The proposer already signed at creation. Only target's signature
    # transitions to active.
    if side == "proposer":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Proposer has already signed; awaiting target acceptance",
        )

    now = _now_iso()
    await r.hset(
        _k_partnership(partnership_id),
        mapping={
            "status": "active",
            "target_signatory_user_id": body.signatory_user_id,
            "target_signed_at": now,
        },
    )
    return {"partnership_id": partnership_id, "status": "active", "signed_at": now}


@router.post("/{partnership_id}/reject")
async def reject(
    partnership_id: str,
    body: RejectRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    record = await _load(r, partnership_id)
    _expire_if_needed(record)
    if record["status"] not in {"proposed"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot reject partnership in status={record['status']}",
        )
    _assert_party(record, body.brand_id)

    now = _now_iso()
    mapping = {"status": "rejected", "rejected_at": now, "rejected_by": body.brand_id}
    if body.reason:
        mapping["reject_reason"] = body.reason
    await r.hset(_k_partnership(partnership_id), mapping=mapping)
    return {"partnership_id": partnership_id, "status": "rejected"}


@router.post("/{partnership_id}/terminate")
async def terminate(
    partnership_id: str,
    body: TerminateRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    record = await _load(r, partnership_id)
    _expire_if_needed(record)
    if record["status"] in TERMINAL_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Partnership already in terminal status={record['status']}",
        )
    _assert_party(record, body.brand_id)

    now = _now_iso()
    mapping = {
        "status": "terminated",
        "terminated_at": now,
        "terminated_by": body.brand_id,
    }
    if body.reason:
        mapping["terminate_reason"] = body.reason
    await r.hset(_k_partnership(partnership_id), mapping=mapping)

    # Best-effort cascade: pause any joint campaigns spawned by this partnership.
    try:
        campaign_ids = await r.smembers(_k_joint_campaigns(partnership_id))
        for cid in campaign_ids:
            try:
                await r.hset(
                    f"campaign:{cid}",
                    mapping={"status": "paused", "paused_reason": f"partnership_{partnership_id}_terminated"},
                )
            except Exception:
                logger.exception(
                    "partnership.cascade pause failed pid=%s cid=%s",
                    partnership_id, cid,
                )
    except Exception:
        logger.exception(
            "partnership.cascade enumeration failed pid=%s", partnership_id,
        )

    return {"partnership_id": partnership_id, "status": "terminated"}


@router.get("/{partnership_id}")
async def get_partnership(
    partnership_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    record = await _load(r, partnership_id)
    _expire_if_needed(record)
    if record.get("status") == "expired":
        await _persist_status(r, partnership_id, record)
    return _public_view(partnership_id, record)


@router.get("/brand/{brand_id}")
async def list_for_brand(
    brand_id: str,
    status_filter: str | None = Query(None, alias="status"),
    type_filter: str | None = Query(None, alias="type"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    pids = await r.smembers(_k_brand_partnerships(brand_id))
    out: list[dict[str, Any]] = []
    for pid in pids:
        try:
            raw = await r.hgetall(_k_partnership(pid))
            if not raw:
                continue
            _expire_if_needed(raw)
            if status_filter and raw.get("status") != status_filter:
                continue
            if type_filter and raw.get("type") != type_filter:
                continue
            out.append(_public_view(pid, raw))
        except Exception:
            logger.exception("list_for_brand: bad partnership pid=%s", pid)
    out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"brand_id": brand_id, "count": len(out), "partnerships": out}


@router.post("/{partnership_id}/voucher-bridge")
async def voucher_bridge(
    partnership_id: str,
    body: VoucherBridgeRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Issue a voucher across the partnership boundary.

    The issuing brand must be one of the two partners. The partnership must
    be active and of type 'voucher_exchange'. Acceptance policy is enforced
    against the voucher's underlying template if available.
    """
    record = await assert_active(r, partnership_id)
    if record.get("type") != "voucher_exchange":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Partnership type is {record.get('type')!r}, not voucher_exchange",
        )
    _assert_party(record, body.brand_id)

    try:
        terms = json.loads(record.get("terms_json", "{}"))
    except json.JSONDecodeError:
        terms = {}

    # Optional policy check: if specific_templates, verify voucher's template
    # is on the acceptable list. Done best-effort against vouchers store.
    policy = terms.get("voucher_acceptance_policy", "all")
    if policy == "none":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Partnership voucher_acceptance_policy=none — bridging disabled",
        )
    if policy == "specific_templates":
        allowed = set(terms.get("acceptable_template_ids", []))
        template_id: str | None = None
        try:
            v_payload = await r.hget(f"voucher:{body.voucher_id}", "template_id")
            if v_payload:
                template_id = v_payload
        except Exception:
            logger.exception(
                "voucher_bridge: failed reading voucher %s", body.voucher_id,
            )
        if template_id is not None and template_id not in allowed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"voucher template {template_id!r} not in acceptable list",
            )

    # Daily cap (best-effort, per partnership)
    cap = terms.get("cap_users_per_day")
    if isinstance(cap, int) and cap > 0:
        day_key = f"partnership:{partnership_id}:bridge_count:{datetime.now(timezone.utc).date().isoformat()}"
        try:
            current = await r.incr(day_key)
            if current == 1:
                await r.expire(day_key, 2 * 86400)
            if current > cap:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"daily bridge cap of {cap} reached",
                )
        except HTTPException:
            raise
        except Exception:
            logger.exception(
                "voucher_bridge: cap check failed pid=%s", partnership_id,
            )

    record_payload = {
        "voucher_id": body.voucher_id,
        "issuing_brand_id": body.brand_id,
        "recipient_user_id": body.recipient_user_id,
        "at": _now_iso(),
    }
    pipe = r.pipeline()
    pipe.lpush(_k_bridged_vouchers(partnership_id), json.dumps(record_payload))
    pipe.ltrim(_k_bridged_vouchers(partnership_id), 0, MAX_BRIDGED_LIST - 1)
    pipe.hincrby(_k_stats(partnership_id), "vouchers_exchanged", 1)
    await pipe.execute()

    return {
        "partnership_id": partnership_id,
        "bridged": True,
        "voucher_id": body.voucher_id,
        "recipient_user_id": body.recipient_user_id,
    }


@router.get("/{partnership_id}/stats")
async def get_stats(
    partnership_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    await _load(r, partnership_id)  # 404 if missing
    raw = await r.hgetall(_k_stats(partnership_id))
    return {
        "partnership_id": partnership_id,
        "vouchers_exchanged": int(raw.get("vouchers_exchanged", 0) or 0),
        "conversions_attributed": int(raw.get("conversions_attributed", 0) or 0),
        "gmv_generated_cents": int(raw.get("gmv_generated_cents", 0) or 0),
        "joint_campaigns_count": int(raw.get("joint_campaigns_count", 0) or 0),
    }


@router.post("/{partnership_id}/joint-campaign", status_code=status.HTTP_201_CREATED)
async def create_joint_campaign(
    partnership_id: str,
    body: JointCampaignRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Spawn a shared campaign owned by both partners.

    The actual campaign is stored as a minimal record here; the caller is
    expected to wire it up via the regular `campaigns` router for richer
    targeting (we deliberately do not import campaigns.create to keep this
    module loosely coupled). Returns the new campaign_id.
    """
    record = await assert_active(r, partnership_id)
    if record.get("type") not in {"joint_campaign", "co_marketing"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Partnership type {record.get('type')!r} does not permit "
                "joint campaigns"
            ),
        )

    p, t = _parties(record)
    parties = {p, t}
    split_keys = set(body.contribution_split_bps.keys())
    if split_keys != parties:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "contribution_split_bps must cover exactly the two partner "
                f"brand_ids ({parties}); got {split_keys}"
            ),
        )
    total_bps = sum(int(v) for v in body.contribution_split_bps.values())
    if total_bps != 10_000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"contribution_split_bps must sum to 10000, got {total_bps}",
        )

    campaign_id = f"cmp_joint_{uuid4().hex[:16]}"
    campaign_record = {
        "id": campaign_id,
        "partnership_id": partnership_id,
        "owner_brand_ids_json": json.dumps(sorted(parties)),
        "contribution_split_bps_json": json.dumps(body.contribution_split_bps),
        "campaign_json": json.dumps(body.campaign),
        "status": "active",
        "created_at": _now_iso(),
    }

    pipe = r.pipeline()
    pipe.hset(f"campaign:{campaign_id}", mapping=campaign_record)
    pipe.sadd(_k_joint_campaigns(partnership_id), campaign_id)
    pipe.hincrby(_k_stats(partnership_id), "joint_campaigns_count", 1)
    # Index under each partner so listing by brand surfaces it too.
    pipe.sadd(f"brand:{p}:joint_campaigns", campaign_id)
    pipe.sadd(f"brand:{t}:joint_campaigns", campaign_id)
    await pipe.execute()

    return {
        "campaign_id": campaign_id,
        "partnership_id": partnership_id,
        "owner_brand_ids": sorted(parties),
        "status": "active",
    }
