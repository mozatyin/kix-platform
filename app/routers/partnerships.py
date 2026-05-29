"""Cross-brand partnership router.

⚠️ OPTIONAL ADD-ON, NOT MAIN PATH
==================================

**This module is NOT the default user-acquisition flow on KiX.**

The default KiX model is **brand-only-talks-to-KiX**. Brands NEVER see each
other on the platform. User acquisition is handled by the KiX **auction
algorithm** which routes users to brands without any bilateral agreement
between merchants. Cross-brand voucher distribution likewise happens *only*
through the auction — KiX decides; brands do not negotiate with peers.

This module exists ONLY for a rare advanced case: two brands that want to
formally **co-market** (e.g. a co-branded event, a joint product launch,
a shared sponsorship). 99% of merchants will never need this. If you are
building user-acquisition flows, look at ``/api/v1/auction/run`` instead.

See ``PLATFORM_OVERVIEW.md`` and ``MONETIZATION_V2.md`` for the canonical
model. The Plenti-style "coalition of brands swapping value bilaterally"
pattern is explicitly NOT what KiX does — it failed there and we will not
re-implement it here.

What this module IS for
-----------------------
  * ``joint_campaign``   — Two brands jointly fund / run a single campaign
                           (shared event, co-branded launch). Spend is split.
  * ``shared_event``     — Two brands co-host an event / collaboration with
                           no shared spend; just formal acknowledgement.

What this module is NOT for
---------------------------
  * ❌ Voucher exchange between brands (use the auction).
  * ❌ Generic "cross-promotion" agreements (use the auction).
  * ❌ Any default user-acquisition routing (use the auction).

KiX still mediates these arrangements — they are not pure peer-to-peer.
The ``kix_arbitrated`` flag on the terms is ``True`` by default and the
platform reserves the right to suspend / arbitrate a partnership.

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
    partnership:{pid}:stats            HASH   {conversions_attributed,
                                                gmv_generated_cents,
                                                joint_campaigns_count}
    partnership:{pid}:joint_campaigns  SET    campaign_ids spawned by this pship

(Note: the legacy ``partnership:{pid}:bridged_vouchers`` LIST has been
removed. Cross-brand voucher flows are exclusively auction-driven.)

Integration points (try/except, never break callers)
----------------------------------------------------
  * ``attribution.track`` — if a conversion is attributed to a co-marketing
                            event, commissions may be split per terms.

(Note: the legacy ``vouchers.redeem`` integration that resolved
``redeemable_at = "partnership:{pid}"`` is removed. Vouchers no longer
cross brand boundaries via partnerships; the auction owns that path.)
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


# Standard deprecation breadcrumb attached to every legacy response so that
# integrators discover the auction-first model without reading the docs.
_DEPRECATION_NOTE = (
    "main user acquisition uses /api/v1/auction/run; partnerships are only "
    "for joint marketing events"
)


# ── Constants ────────────────────────────────────────────────────────────

# Partnership types — restricted to the two legitimate co-marketing cases.
#
#   joint_campaign  — Two brands jointly fund a single campaign. Shared
#                     spend (``commission_split_bps`` applies).
#   shared_event    — Two brands co-host an event / collaboration with
#                     no shared spend. Pure acknowledgement.
#
# REMOVED:
#   voucher_exchange — wrong path; cross-brand vouchers go via auction.
#   cross_promotion  — vague; covered by auction.
#   co_marketing     — renamed to ``shared_event`` for clarity.
PartnershipType = Literal[
    "joint_campaign",
    "shared_event",
]
VALID_TYPES: set[str] = {
    "joint_campaign",
    "shared_event",
}

PartnershipStatus = Literal[
    "proposed", "active", "rejected", "expired", "terminated"
]
TERMINAL_STATES: set[str] = {"rejected", "expired", "terminated"}

AttributionCredit = Literal["first_touch", "split_50_50", "last_touch"]

# Defaults
DEFAULT_PROPOSAL_TTL_DAYS = 14
DEFAULT_DURATION_DAYS = 90
MAX_DURATION_DAYS = 365 * 2
MAX_NOTES_LEN = 2048


# ── Pydantic models ──────────────────────────────────────────────────────


class PartnershipTerms(BaseModel):
    duration_days: int = Field(default=DEFAULT_DURATION_DAYS, ge=1, le=MAX_DURATION_DAYS)
    exclusivity: bool = False
    commission_split_bps: int | None = Field(
        default=None,
        ge=0,
        le=10_000,
        description="basis points (0-10000) of shared campaign spend / "
                    "attributed commission allocated to proposer; remainder "
                    "goes to target. Only meaningful for joint_campaign "
                    "partnerships where both parties share spend.",
    )
    attribution_credit: AttributionCredit = "split_50_50"
    kix_arbitrated: bool = Field(
        default=True,
        description="KiX still mediates this partnership; brands are not "
                    "pure peers. KiX reserves the right to arbitrate / "
                    "suspend. Always True in production.",
    )


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
    """Raise unless partnership is currently active.

    Retained for attribution / joint-campaign callers. NOT used by the
    voucher router any more — cross-brand voucher distribution is
    auction-driven, not partnership-mediated.
    """
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
    """Propose a **co-marketing arrangement** between two brands.

    NOT a user-acquisition channel. Use this only for formal
    joint-campaign / shared-event arrangements. Returns the new pid.
    """
    if body.proposer_brand_id == body.target_brand_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="proposer_brand_id and target_brand_id must differ",
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
        "deprecated": _DEPRECATION_NOTE,
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
    return {
        "partnership_id": partnership_id,
        "status": "active",
        "signed_at": now,
        "deprecated": _DEPRECATION_NOTE,
    }


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
    return {
        "partnership_id": partnership_id,
        "status": "rejected",
        "deprecated": _DEPRECATION_NOTE,
    }


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

    return {
        "partnership_id": partnership_id,
        "status": "terminated",
        "deprecated": _DEPRECATION_NOTE,
    }


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


@router.get("/{partnership_id}/stats")
async def get_stats(
    partnership_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    await _load(r, partnership_id)  # 404 if missing
    raw = await r.hgetall(_k_stats(partnership_id))
    return {
        "partnership_id": partnership_id,
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

    This is the **legitimate** use case for partnerships: two brands jointly
    funding / running a single campaign (co-branded event, shared launch).
    Spend is split per ``contribution_split_bps``.

    The actual campaign is stored as a minimal record here; the caller is
    expected to wire it up via the regular ``campaigns`` router for richer
    targeting (we deliberately do not import campaigns.create to keep this
    module loosely coupled). Returns the new campaign_id.
    """
    record = await assert_active(r, partnership_id)
    if record.get("type") not in {"joint_campaign", "shared_event"}:
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
