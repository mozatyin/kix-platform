"""Accounts — B2B company entities as first-class platform citizens.

PROBLEM: every existing primitive (user, wallet, brand, attribution, …) is
keyed on ``user_id``. A B2B sale to "Acme Corp" with 200 employees has no
first-class home — there's nowhere to hang the buying committee, the org
chart, the seat-count, the procurement contact. NDR/GRR is uncomputable,
ABM journeys are unattributable, and seat-based subscriptions can't bind
their seats to anything real.

This router mints account IDs (``acct_<12hex>``), tracks role-tagged
membership, and stores the org-chart adjacency lists used by
buying-committee lookups and ABM journey rollups.

Key schema
----------
    account:{aid}                      HASH   — account record
    account:{aid}:members              SET    — user_ids
    account:{aid}:member:{uid}         HASH   — role / dept / seat / joined
    account:{aid}:org_chart:reports    HASH   — manager_uid → JSON list[uid]
    account:{aid}:org_chart:manager    HASH   — report_uid → manager_uid
    user:{uid}:account                 STRING — reverse lookup
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Constants ──────────────────────────────────────────────────────────────

ACCOUNT_ID_PREFIX = "acct_"
ACCOUNT_ID_NIBBLES = 12  # 48 bits of entropy, plenty for SMB tenancy

VALID_ROLES = {
    "decision_maker",
    "influencer",
    "end_user",
    "finance",
    "procurement",
    "executive",
}

# Roles that constitute the "buying committee" — the people who can
# actually authorize spend. Procurement and finance are included because
# they hold veto power even when they don't drive the decision.
BUYING_COMMITTEE_ROLES = {
    "decision_maker",
    "influencer",
    "procurement",
    "finance",
    "executive",
}

VALID_SIZES = {"1-10", "11-50", "51-200", "201-1000", "1000+"}

MAX_ORG_DEPTH = 32  # cycle guard for subtree traversal


# ── Pydantic models ────────────────────────────────────────────────────────


class AccountRegisterRequest(BaseModel):
    account_name: str = Field(min_length=1, max_length=200)
    industry: str = Field(min_length=1, max_length=80)
    size: Literal["1-10", "11-50", "51-200", "201-1000", "1000+"]
    primary_contact_user_id: str = Field(min_length=1)
    billing_contact_user_id: str | None = None
    domain: str | None = None
    tax_id_hash: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AccountRegisterResponse(BaseModel):
    account_id: str
    created_at: float


class AccountResponse(BaseModel):
    account_id: str
    account_name: str
    industry: str
    size: str
    primary_contact_user_id: str
    billing_contact_user_id: str | None = None
    domain: str | None = None
    tax_id_hash: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: float
    member_count: int


class MemberAddRequest(BaseModel):
    user_id: str = Field(min_length=1)
    role: Literal[
        "decision_maker", "influencer", "end_user",
        "finance", "procurement", "executive",
    ]
    department: str | None = None
    seat_status: str | None = None  # "active" | "suspended" | "pending"


class MemberRoleUpdateRequest(BaseModel):
    role: Literal[
        "decision_maker", "influencer", "end_user",
        "finance", "procurement", "executive",
    ]
    department: str | None = None
    seat_status: str | None = None


class MemberEntry(BaseModel):
    user_id: str
    role: str
    department: str | None = None
    seat_status: str | None = None
    joined_at: float


class MemberListResponse(BaseModel):
    account_id: str
    count: int
    members: list[MemberEntry]


class OrgChartEdgeRequest(BaseModel):
    manager_user_id: str = Field(min_length=1)
    report_user_id: str = Field(min_length=1)


class OrgChartSubtreeNode(BaseModel):
    user_id: str
    depth: int
    role: str | None = None
    department: str | None = None


class OrgChartSubtreeResponse(BaseModel):
    account_id: str
    root_user_id: str
    max_depth: int
    node_count: int
    nodes: list[OrgChartSubtreeNode]


class UserAccountResponse(BaseModel):
    user_id: str
    account_id: str | None
    role: str | None = None
    department: str | None = None
    seat_status: str | None = None


# ── Helpers ────────────────────────────────────────────────────────────────


def _now() -> float:
    return time.time()


def _new_account_id() -> str:
    return f"{ACCOUNT_ID_PREFIX}{secrets.token_hex(ACCOUNT_ID_NIBBLES // 2)}"


async def _require_account(r: aioredis.Redis, account_id: str) -> dict[str, str]:
    raw = await r.hgetall(f"account:{account_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="account_not_found")
    return raw


def _account_from_hash(aid: str, raw: dict[str, str], member_count: int) -> AccountResponse:
    try:
        metadata = json.loads(raw.get("metadata") or "{}")
        if not isinstance(metadata, dict):
            metadata = {}
    except json.JSONDecodeError:
        metadata = {}
    try:
        created_at = float(raw.get("created_at", 0) or 0)
    except (TypeError, ValueError):
        created_at = 0.0
    return AccountResponse(
        account_id=aid,
        account_name=raw.get("account_name", ""),
        industry=raw.get("industry", ""),
        size=raw.get("size", ""),
        primary_contact_user_id=raw.get("primary_contact_user_id", ""),
        billing_contact_user_id=raw.get("billing_contact_user_id") or None,
        domain=raw.get("domain") or None,
        tax_id_hash=raw.get("tax_id_hash") or None,
        metadata=metadata,
        created_at=created_at,
        member_count=member_count,
    )


async def _load_member(
    r: aioredis.Redis, account_id: str, user_id: str
) -> MemberEntry | None:
    raw = await r.hgetall(f"account:{account_id}:member:{user_id}")
    if not raw:
        return None
    try:
        joined = float(raw.get("joined_at", 0) or 0)
    except (TypeError, ValueError):
        joined = 0.0
    return MemberEntry(
        user_id=user_id,
        role=raw.get("role", ""),
        department=raw.get("department") or None,
        seat_status=raw.get("seat_status") or None,
        joined_at=joined,
    )


async def get_user_account(r: aioredis.Redis, user_id: str) -> str | None:
    """Public helper — sibling routers (subscriptions, attribution) use this."""
    aid = await r.get(f"user:{user_id}:account")
    return aid or None


async def get_account_members(
    r: aioredis.Redis,
    account_id: str,
    *,
    role_filter: set[str] | None = None,
) -> list[MemberEntry]:
    """Public helper — used for ABM journey expansion + co-attribution."""
    members: list[MemberEntry] = []
    uids = await r.smembers(f"account:{account_id}:members")
    for uid in uids:
        entry = await _load_member(r, account_id, uid)
        if not entry:
            continue
        if role_filter and entry.role not in role_filter:
            continue
        members.append(entry)
    return members


# ── Endpoints — account lifecycle ──────────────────────────────────────────


@router.post("/register", response_model=AccountRegisterResponse)
async def register_account(
    req: AccountRegisterRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Mint a new account. Primary contact is auto-enrolled as a member
    with role=decision_maker so the account is never empty."""
    if req.size not in VALID_SIZES:
        raise HTTPException(status_code=400, detail="invalid_size")

    account_id = _new_account_id()
    now = _now()

    mapping = {
        "account_id": account_id,
        "account_name": req.account_name,
        "industry": req.industry,
        "size": req.size,
        "primary_contact_user_id": req.primary_contact_user_id,
        "billing_contact_user_id": req.billing_contact_user_id or "",
        "domain": req.domain or "",
        "tax_id_hash": req.tax_id_hash or "",
        "metadata": json.dumps(req.metadata or {}, separators=(",", ":")),
        "created_at": f"{now:.6f}",
    }
    member_mapping = {
        "role": "decision_maker",
        "department": "",
        "seat_status": "active",
        "joined_at": f"{now:.6f}",
    }

    pipe = r.pipeline(transaction=True)
    pipe.hset(f"account:{account_id}", mapping=mapping)
    pipe.sadd(f"account:{account_id}:members", req.primary_contact_user_id)
    pipe.hset(
        f"account:{account_id}:member:{req.primary_contact_user_id}",
        mapping=member_mapping,
    )
    pipe.set(f"user:{req.primary_contact_user_id}:account", account_id)
    await pipe.execute()

    return AccountRegisterResponse(account_id=account_id, created_at=now)


@router.get("/{account_id}", response_model=AccountResponse)
async def get_account(
    account_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await _require_account(r, account_id)
    count = await r.scard(f"account:{account_id}:members")
    return _account_from_hash(account_id, raw, count)


# ── Endpoints — membership ─────────────────────────────────────────────────


@router.post("/{account_id}/members/add", response_model=MemberEntry)
async def add_member(
    account_id: str,
    req: MemberAddRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    await _require_account(r, account_id)
    if req.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="invalid_role")

    # Reverse-lookup guard: a user can only belong to one account at a time.
    existing = await r.get(f"user:{req.user_id}:account")
    if existing and existing != account_id:
        raise HTTPException(
            status_code=409,
            detail={"error": "user_already_in_account", "existing_account_id": existing},
        )

    now = _now()
    mapping = {
        "role": req.role,
        "department": req.department or "",
        "seat_status": req.seat_status or "active",
        "joined_at": f"{now:.6f}",
    }
    pipe = r.pipeline(transaction=True)
    pipe.sadd(f"account:{account_id}:members", req.user_id)
    pipe.hset(f"account:{account_id}:member:{req.user_id}", mapping=mapping)
    pipe.set(f"user:{req.user_id}:account", account_id)
    await pipe.execute()

    return MemberEntry(
        user_id=req.user_id,
        role=req.role,
        department=req.department,
        seat_status=req.seat_status or "active",
        joined_at=now,
    )


@router.post("/{account_id}/members/{user_id}/role", response_model=MemberEntry)
async def update_member_role(
    account_id: str,
    user_id: str,
    req: MemberRoleUpdateRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    await _require_account(r, account_id)
    if not await r.sismember(f"account:{account_id}:members", user_id):
        raise HTTPException(status_code=404, detail="member_not_found")
    if req.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="invalid_role")

    member_key = f"account:{account_id}:member:{user_id}"
    updates: dict[str, str] = {"role": req.role}
    if req.department is not None:
        updates["department"] = req.department
    if req.seat_status is not None:
        updates["seat_status"] = req.seat_status
    await r.hset(member_key, mapping=updates)

    entry = await _load_member(r, account_id, user_id)
    if not entry:  # pragma: no cover — defensive
        raise HTTPException(status_code=500, detail="member_load_failed")
    return entry


@router.delete("/{account_id}/members/{user_id}")
async def remove_member(
    account_id: str,
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    await _require_account(r, account_id)
    if not await r.sismember(f"account:{account_id}:members", user_id):
        raise HTTPException(status_code=404, detail="member_not_found")

    # Also detach from any org-chart edges they participate in.
    manager = await r.hget(f"account:{account_id}:org_chart:manager", user_id)
    pipe = r.pipeline(transaction=True)
    pipe.srem(f"account:{account_id}:members", user_id)
    pipe.delete(f"account:{account_id}:member:{user_id}")
    pipe.hdel(f"account:{account_id}:org_chart:manager", user_id)
    pipe.hdel(f"account:{account_id}:org_chart:reports", user_id)
    # Reverse lookup — only clear if it still points here.
    current = await r.get(f"user:{user_id}:account")
    if current == account_id:
        pipe.delete(f"user:{user_id}:account")
    await pipe.execute()

    # Strip this user from their old manager's reports list.
    if manager:
        await _remove_from_reports_list(r, account_id, manager, user_id)

    return {"ok": True, "account_id": account_id, "user_id": user_id, "removed": True}


@router.get("/{account_id}/members", response_model=MemberListResponse)
async def list_members(
    account_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    await _require_account(r, account_id)
    members = await get_account_members(r, account_id)
    members.sort(key=lambda m: m.joined_at)
    return MemberListResponse(
        account_id=account_id, count=len(members), members=members,
    )


@router.get("/{account_id}/buying-committee", response_model=MemberListResponse)
async def buying_committee(
    account_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Only members with buying authority. End-users are excluded —
    they don't sign POs, they consume the product."""
    await _require_account(r, account_id)
    members = await get_account_members(
        r, account_id, role_filter=BUYING_COMMITTEE_ROLES,
    )
    members.sort(key=lambda m: m.joined_at)
    return MemberListResponse(
        account_id=account_id, count=len(members), members=members,
    )


# ── Org chart ──────────────────────────────────────────────────────────────


async def _read_reports(
    r: aioredis.Redis, account_id: str, manager_uid: str
) -> list[str]:
    raw = await r.hget(f"account:{account_id}:org_chart:reports", manager_uid)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data]


async def _write_reports(
    r: aioredis.Redis, account_id: str, manager_uid: str, reports: list[str]
) -> None:
    key = f"account:{account_id}:org_chart:reports"
    if reports:
        await r.hset(key, manager_uid, json.dumps(reports, separators=(",", ":")))
    else:
        await r.hdel(key, manager_uid)


async def _remove_from_reports_list(
    r: aioredis.Redis, account_id: str, manager_uid: str, report_uid: str
) -> None:
    reports = await _read_reports(r, account_id, manager_uid)
    if report_uid in reports:
        reports = [x for x in reports if x != report_uid]
        await _write_reports(r, account_id, manager_uid, reports)


@router.post("/{account_id}/org-chart/edge")
async def add_org_chart_edge(
    account_id: str,
    req: OrgChartEdgeRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Add a manager → report edge. Idempotent: re-adding is a no-op.

    Rejects self-loops and obvious cycles (where the proposed manager is
    already in the report's subtree).
    """
    await _require_account(r, account_id)
    if req.manager_user_id == req.report_user_id:
        raise HTTPException(status_code=400, detail="self_loop_forbidden")

    members_key = f"account:{account_id}:members"
    if not await r.sismember(members_key, req.manager_user_id):
        raise HTTPException(status_code=404, detail="manager_not_member")
    if not await r.sismember(members_key, req.report_user_id):
        raise HTTPException(status_code=404, detail="report_not_member")

    # Cycle check: walk down from `report_user_id`; if we land on `manager_user_id`,
    # adding this edge would create a cycle.
    visited: set[str] = set()
    frontier = [req.report_user_id]
    while frontier:
        nxt: list[str] = []
        for node in frontier:
            if node in visited:
                continue
            visited.add(node)
            children = await _read_reports(r, account_id, node)
            for child in children:
                if child == req.manager_user_id:
                    raise HTTPException(status_code=409, detail="cycle_detected")
                nxt.append(child)
        frontier = nxt
        if len(visited) > 10_000:  # defensive runaway guard
            break

    # If this report already has a different manager, detach first.
    prev_manager = await r.hget(
        f"account:{account_id}:org_chart:manager", req.report_user_id
    )
    if prev_manager and prev_manager != req.manager_user_id:
        await _remove_from_reports_list(
            r, account_id, prev_manager, req.report_user_id
        )

    reports = await _read_reports(r, account_id, req.manager_user_id)
    if req.report_user_id not in reports:
        reports.append(req.report_user_id)
        await _write_reports(r, account_id, req.manager_user_id, reports)
    await r.hset(
        f"account:{account_id}:org_chart:manager",
        req.report_user_id,
        req.manager_user_id,
    )

    return {
        "ok": True,
        "account_id": account_id,
        "manager_user_id": req.manager_user_id,
        "report_user_id": req.report_user_id,
    }


@router.get(
    "/{account_id}/org-chart/subtree",
    response_model=OrgChartSubtreeResponse,
)
async def org_chart_subtree(
    account_id: str,
    root_user_id: str = Query(..., min_length=1),
    max_depth: int = Query(default=10, ge=1, le=MAX_ORG_DEPTH),
    r: aioredis.Redis = Depends(get_redis),
):
    """Transitive reports under ``root_user_id``, BFS up to max_depth.

    Root itself is included at depth=0. Visited-set guards against cycles
    that might have slipped past the edge-add validation (e.g. legacy data).
    """
    await _require_account(r, account_id)
    if not await r.sismember(f"account:{account_id}:members", root_user_id):
        raise HTTPException(status_code=404, detail="root_not_member")

    nodes: list[OrgChartSubtreeNode] = []
    visited: set[str] = {root_user_id}
    frontier: list[tuple[str, int]] = [(root_user_id, 0)]

    while frontier:
        nxt: list[tuple[str, int]] = []
        for uid, depth in frontier:
            entry = await _load_member(r, account_id, uid)
            nodes.append(OrgChartSubtreeNode(
                user_id=uid,
                depth=depth,
                role=entry.role if entry else None,
                department=entry.department if entry else None,
            ))
            if depth >= max_depth:
                continue
            children = await _read_reports(r, account_id, uid)
            for child in children:
                if child in visited:
                    continue
                visited.add(child)
                nxt.append((child, depth + 1))
        frontier = nxt

    return OrgChartSubtreeResponse(
        account_id=account_id,
        root_user_id=root_user_id,
        max_depth=max_depth,
        node_count=len(nodes),
        nodes=nodes,
    )


@router.get("/user/{user_id}/account", response_model=UserAccountResponse)
async def user_to_account(
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Reverse lookup: which account does this user belong to?"""
    aid = await r.get(f"user:{user_id}:account")
    if not aid:
        return UserAccountResponse(user_id=user_id, account_id=None)
    entry = await _load_member(r, aid, user_id)
    return UserAccountResponse(
        user_id=user_id,
        account_id=aid,
        role=entry.role if entry else None,
        department=entry.department if entry else None,
        seat_status=entry.seat_status if entry else None,
    )


@router.get("/health")
async def accounts_health(r: aioredis.Redis = Depends(get_redis)):
    pong = await r.ping()
    return {"ok": bool(pong), "module": "accounts"}
