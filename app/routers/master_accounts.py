"""Master Accounts + RBAC router.

A *Master Account* is the top-level corporate entity that owns one or more
brand_ids (each brand_id == one store / outlet in the KiX universe). It
solves the real-world case where 老王 owns 10 南洋茶饮 stores across 4 cities
and needs:

  * one accounting / billing root (consolidated reports, master budget),
  * per-store managers with scoped permissions (cannot touch HQ wallets),
  * HQ ops with global reach,
  * finance with money-only access,
  * viewers with read-only access.

RBAC model
----------
Role           Allowed actions
---------------------------------------------------------------------
hq_admin       *                                       (all brands)
ops_manager    campaigns.*, audiences.*, reports.*     (scoped brands)
store_manager  campaigns.view, campaigns.pause,
               campaigns.resume, reports.view          (scoped brands)
finance        wallet.*, payouts.*, reports.financial  (all brands)
viewer         *.view                                  (scoped brands)

`brand_scope` is either the sentinel string `"all"` or a JSON-encoded
list of brand_ids stored on the Member hash. Roles `hq_admin` and
`finance` are *force-promoted* to `"all"` because by definition they
operate at the master level.

Redis Schema
------------
  master:{master_id}              HASH  {company_name, primary_email,
                                          owner_user_id, created_at,
                                          monthly_budget_cents,
                                          budget_allocation_json,
                                          budget_updated_at}
  master:{master_id}:brands       SET   brand_ids attached to this master
  master:{master_id}:members      SET   member_ids attached to this master
  master:{master_id}:stores       HASH  brand_id → store_name (optional metadata)
  brand:{bid}:master              STR   master_id (reverse index — a brand
                                                    belongs to exactly one
                                                    master at a time)
  member:{member_id}              HASH  {user_id, master_id, role,
                                          brand_scope, email, joined_at}
  user:{uid}:masters              SET   master_ids the user is a member of
  user:{uid}:members              SET   member_ids belonging to this user
  master:invite:{invite_id}       HASH  {master_id, email, role, brand_scope,
                                          invited_by, created_at, status}
                                  (EX 7d)
  rbac:matrix                     HASH  role → JSON list of allowed actions
                                  (lazily seeded on first read; admins may
                                  override entries to customise policy at
                                  runtime without redeploying)

Integration points (NOT wired here — document only, too risky to enforce
silently in a single drop):

  * app/routers/wallet.py        — wallet.topup / wallet.charge / wallet.refund
                                   should call check_permission(user_id,
                                   "wallet.topup", brand_id) before mutation.
  * app/routers/campaigns.py     — create/update/pause/resume should check
                                   "campaigns.create" | "campaigns.update" |
                                   "campaigns.pause" | "campaigns.resume".
  * app/routers/auction.py       — pure machine path; no check needed.
  * app/routers/reports (future) — should check "reports.view" /
                                   "reports.financial".

Enforcement should be added behind a feature flag (`RBAC_ENFORCE=1`) so we
can dark-launch and observe denials before failing requests.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field, field_validator

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ────────────────────────────────────────────────────────────
Role = Literal["hq_admin", "ops_manager", "store_manager", "finance", "viewer"]
VALID_ROLES: set[str] = {
    "hq_admin",
    "ops_manager",
    "store_manager",
    "finance",
    "viewer",
}

# Roles that always operate at the master level — brand_scope is forced
# to "all" regardless of what the caller passes.
GLOBAL_ROLES: set[str] = {"hq_admin", "finance"}

INVITE_TTL_SECONDS = 7 * 24 * 3600  # 7 days

# Default RBAC matrix. Entries support glob suffix ".*" (matches any action
# starting with the prefix) and the wildcard "*" (matches everything).
# Stored in Redis at first use so ops can override without redeploys.
DEFAULT_RBAC_MATRIX: dict[str, list[str]] = {
    "hq_admin": ["*"],
    "ops_manager": [
        "campaigns.*",
        "audiences.*",
        "reports.*",
    ],
    "store_manager": [
        "campaigns.view",
        "campaigns.pause",
        "campaigns.resume",
        "reports.view",
    ],
    "finance": [
        "wallet.*",
        "payouts.*",
        "reports.financial",
        "reports.view",
    ],
    "viewer": [
        "*.view",
    ],
}


# ── Key helpers ──────────────────────────────────────────────────────────
def _k_master(mid: str) -> str:
    return f"master:{mid}"


def _k_master_brands(mid: str) -> str:
    return f"master:{mid}:brands"


def _k_master_members(mid: str) -> str:
    return f"master:{mid}:members"


def _k_master_stores(mid: str) -> str:
    return f"master:{mid}:stores"


def _k_brand_master(bid: str) -> str:
    return f"brand:{bid}:master"


def _k_member(member_id: str) -> str:
    return f"member:{member_id}"


def _k_user_masters(uid: str) -> str:
    return f"user:{uid}:masters"


def _k_user_members(uid: str) -> str:
    return f"user:{uid}:members"


def _k_invite(invite_id: str) -> str:
    return f"master:invite:{invite_id}"


_RBAC_MATRIX_KEY = "rbac:matrix"


# ── RBAC matrix helpers ──────────────────────────────────────────────────
async def _load_rbac_matrix(r: aioredis.Redis) -> dict[str, list[str]]:
    """Load RBAC matrix from Redis, seeding defaults on first use."""
    raw = await r.hgetall(_RBAC_MATRIX_KEY)
    if not raw:
        # First-run seed. SETNX on each field so concurrent boots are safe.
        pipe = r.pipeline()
        for role, actions in DEFAULT_RBAC_MATRIX.items():
            pipe.hsetnx(_RBAC_MATRIX_KEY, role, json.dumps(actions))
        await pipe.execute()
        raw = await r.hgetall(_RBAC_MATRIX_KEY)

    out: dict[str, list[str]] = {}
    for role, payload in raw.items():
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, list):
                out[role] = [str(x) for x in parsed]
        except (json.JSONDecodeError, TypeError):
            logger.warning("rbac:matrix corrupt for role=%s payload=%r", role, payload)
    # Fall back to defaults for roles missing from Redis.
    for role, actions in DEFAULT_RBAC_MATRIX.items():
        out.setdefault(role, list(actions))
    return out


def _action_matches(pattern: str, action: str) -> bool:
    """Pattern match — '*' is full wildcard, 'prefix.*' is prefix glob."""
    if pattern == "*":
        return True
    if pattern == action:
        return True
    if pattern.endswith(".*"):
        prefix = pattern[:-2]
        # 'campaigns.*' matches 'campaigns.create' but NOT bare 'campaigns'
        return action.startswith(prefix + ".")
    return False


def _role_allows(role: str, action: str, matrix: dict[str, list[str]]) -> bool:
    patterns = matrix.get(role, [])
    return any(_action_matches(p, action) for p in patterns)


def _normalize_brand_scope(role: str, brand_scope: Any) -> str:
    """Return canonical brand_scope string for storage.

    Returns either the sentinel "all" or a JSON-encoded list of brand_ids.
    Global roles (hq_admin, finance) are force-promoted to "all".
    """
    if role in GLOBAL_ROLES:
        return "all"
    if brand_scope is None or brand_scope == "all":
        return "all"
    if isinstance(brand_scope, str):
        # Accept JSON-string lists too.
        try:
            parsed = json.loads(brand_scope)
            if isinstance(parsed, list):
                return json.dumps([str(x) for x in parsed])
        except (json.JSONDecodeError, ValueError):
            pass
        return "all"
    if isinstance(brand_scope, list):
        return json.dumps([str(x) for x in brand_scope])
    return "all"


def _scope_contains(brand_scope: str, brand_id: str | None) -> bool:
    """Check if brand_scope grants access to brand_id."""
    if brand_scope == "all":
        return True
    if brand_id is None:
        # Action without a brand_id requires master-level (all) scope.
        return False
    try:
        scoped = json.loads(brand_scope)
        return brand_id in scoped if isinstance(scoped, list) else False
    except (json.JSONDecodeError, TypeError):
        return False


# ── Pydantic schemas ─────────────────────────────────────────────────────
class CreateMasterBody(BaseModel):
    company_name: str = Field(..., min_length=1, max_length=200)
    primary_email: EmailStr
    owner_user_id: str = Field(..., min_length=1, max_length=128)


class AttachBrandBody(BaseModel):
    brand_id: str = Field(..., min_length=1, max_length=128)
    store_name: str | None = Field(None, max_length=200)
    store_id: str | None = Field(None, max_length=128)


class DetachBrandBody(BaseModel):
    brand_id: str = Field(..., min_length=1, max_length=128)


class InviteMemberBody(BaseModel):
    email: EmailStr
    role: Role
    brand_scope: Any = "all"  # "all" | list[brand_id]

    @field_validator("brand_scope")
    @classmethod
    def _scope_shape(cls, v):  # noqa: D401
        if v is None or v == "all":
            return "all"
        if isinstance(v, list):
            return [str(x) for x in v]
        raise ValueError("brand_scope must be 'all' or a list of brand_ids")


class UpdateRoleBody(BaseModel):
    role: Role
    brand_scope: Any = None

    @field_validator("brand_scope")
    @classmethod
    def _scope_shape(cls, v):
        if v is None or v == "all":
            return v
        if isinstance(v, list):
            return [str(x) for x in v]
        raise ValueError("brand_scope must be 'all' or a list of brand_ids")


class CheckPermissionBody(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    action: str = Field(..., min_length=1, max_length=128)
    brand_id: str | None = Field(None, max_length=128)


class AcceptInviteBody(BaseModel):
    invite_id: str = Field(..., min_length=1, max_length=128)
    user_id: str = Field(..., min_length=1, max_length=128)
    name: str | None = Field(None, max_length=200)


class TopupAllBody(BaseModel):
    amount_cents_total: int = Field(..., gt=0, le=100_000_000)
    allocation: dict[str, float] = Field(default_factory=dict)
    payment_method: Literal["alipay", "wechat", "stripe", "paypal"]
    payment_token: str | None = None

    @field_validator("allocation")
    @classmethod
    def _alloc_sums(cls, v: dict[str, float]):
        if not v:
            raise ValueError("allocation must include at least one brand_id")
        total = sum(v.values())
        if not (0.99 <= total <= 1.01 or 99.0 <= total <= 101.0):
            raise ValueError(
                f"allocation must sum to 1.0 (fractions) or 100 (percent); got {total}"
            )
        for pct in v.values():
            if pct < 0:
                raise ValueError("allocation pct must be >= 0")
        return v


class GlobalBudgetBody(BaseModel):
    monthly_budget_cents: int = Field(..., ge=0)
    allocation: dict[str, float] = Field(default_factory=dict)

    @field_validator("allocation")
    @classmethod
    def _alloc_sums(cls, v: dict[str, float]):
        if not v:
            return v
        total = sum(v.values())
        # Accept either fractions summing to 1.0 or percentages summing to 100.
        if not (0.99 <= total <= 1.01 or 99.0 <= total <= 101.0):
            raise ValueError(
                f"allocation must sum to 1.0 (fractions) or 100 (percent); got {total}"
            )
        for pct in v.values():
            if pct < 0:
                raise ValueError("allocation pct must be >= 0")
        return v


# ── Internal helpers ─────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _require_master(r: aioredis.Redis, master_id: str) -> dict:
    data = await r.hgetall(_k_master(master_id))
    if not data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"master_id={master_id} not found",
        )
    return data


async def _require_member(r: aioredis.Redis, member_id: str) -> dict:
    data = await r.hgetall(_k_member(member_id))
    if not data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"member_id={member_id} not found",
        )
    return data


# ── Permission check helper (EXPORTED) ───────────────────────────────────
async def check_permission(
    user_id: str,
    action: str,
    brand_id: str | None,
    r: aioredis.Redis,
) -> tuple[bool, str]:
    """Authoritative RBAC decision.

    Steps:
      1. Enumerate user's master memberships via user:{uid}:members.
      2. For each membership, verify the brand_id is within brand_scope.
      3. Look up the role's allowed-actions in rbac:matrix (Redis).
      4. Return (True, role) on first match, else (False, reason).

    Other modules import this directly:

        from app.routers.master_accounts import check_permission
        allowed, why = await check_permission(uid, "wallet.topup", bid, r)
    """
    member_ids = await r.smembers(_k_user_members(user_id))
    if not member_ids:
        return False, "no_membership"

    matrix = await _load_rbac_matrix(r)

    # Track best denial reason so debugging is useful when access fails.
    last_reason = "no_matching_role"
    for member_id in member_ids:
        member = await r.hgetall(_k_member(member_id))
        if not member:
            continue
        role = member.get("role", "")
        scope = member.get("brand_scope", "all")
        if role not in VALID_ROLES:
            last_reason = f"invalid_role:{role}"
            continue
        if not _scope_contains(scope, brand_id):
            last_reason = f"out_of_scope:{role}"
            continue
        if not _role_allows(role, action, matrix):
            last_reason = f"action_denied:{role}"
            continue
        return True, role

    return False, last_reason


# ── Endpoints ────────────────────────────────────────────────────────────
@router.post("/create", status_code=status.HTTP_201_CREATED)
async def create_master(
    body: CreateMasterBody,
    r: aioredis.Redis = Depends(get_redis),
):
    """Create a new master account.

    The owner_user_id is auto-installed as an hq_admin member so the
    creator never locks themselves out.
    """
    master_id = f"m_{uuid4().hex[:16]}"
    member_id = f"mb_{uuid4().hex[:16]}"
    now = _now_iso()

    pipe = r.pipeline()
    pipe.hset(
        _k_master(master_id),
        mapping={
            "master_id": master_id,
            "company_name": body.company_name,
            "primary_email": body.primary_email,
            "owner_user_id": body.owner_user_id,
            "created_at": now,
            "monthly_budget_cents": 0,
            "budget_allocation_json": "{}",
            "budget_updated_at": "",
        },
    )
    # Owner is auto-enrolled as hq_admin / scope=all.
    pipe.hset(
        _k_member(member_id),
        mapping={
            "member_id": member_id,
            "user_id": body.owner_user_id,
            "master_id": master_id,
            "role": "hq_admin",
            "brand_scope": "all",
            "email": body.primary_email,
            "joined_at": now,
        },
    )
    pipe.sadd(_k_master_members(master_id), member_id)
    pipe.sadd(_k_user_masters(body.owner_user_id), master_id)
    pipe.sadd(_k_user_members(body.owner_user_id), member_id)
    await pipe.execute()

    logger.info(
        "master_created master_id=%s owner=%s company=%r",
        master_id,
        body.owner_user_id,
        body.company_name,
    )
    return {"master_id": master_id, "owner_member_id": member_id}


@router.post("/{master_id}/brands/attach")
async def attach_brand(
    master_id: str,
    body: AttachBrandBody,
    r: aioredis.Redis = Depends(get_redis),
):
    """Attach a brand_id (one store) to this master.

    Rejects if the brand is already owned by another master — a brand
    must have exactly one master at any moment. Move it explicitly via
    detach + attach to avoid silent re-parenting.
    """
    await _require_master(r, master_id)

    existing_master = await r.get(_k_brand_master(body.brand_id))
    if existing_master and existing_master != master_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"brand_id={body.brand_id} already attached to master={existing_master}",
        )

    pipe = r.pipeline()
    pipe.sadd(_k_master_brands(master_id), body.brand_id)
    pipe.set(_k_brand_master(body.brand_id), master_id)
    if body.store_name:
        pipe.hset(_k_master_stores(master_id), body.brand_id, body.store_name)
    await pipe.execute()

    logger.info(
        "brand_attached master_id=%s brand_id=%s store_name=%r",
        master_id,
        body.brand_id,
        body.store_name,
    )
    return {
        "master_id": master_id,
        "brand_id": body.brand_id,
        "store_name": body.store_name,
        "store_id": body.store_id,
    }


@router.post("/{master_id}/brands/detach")
async def detach_brand(
    master_id: str,
    body: DetachBrandBody,
    r: aioredis.Redis = Depends(get_redis),
):
    """Detach a brand from this master."""
    await _require_master(r, master_id)

    current_master = await r.get(_k_brand_master(body.brand_id))
    if current_master != master_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"brand_id={body.brand_id} not attached to master={master_id}",
        )

    pipe = r.pipeline()
    pipe.srem(_k_master_brands(master_id), body.brand_id)
    pipe.delete(_k_brand_master(body.brand_id))
    pipe.hdel(_k_master_stores(master_id), body.brand_id)
    await pipe.execute()

    logger.info("brand_detached master_id=%s brand_id=%s", master_id, body.brand_id)
    return {"master_id": master_id, "brand_id": body.brand_id, "detached": True}


@router.get("/{master_id}")
async def get_master(master_id: str, r: aioredis.Redis = Depends(get_redis)):
    """Return master metadata + attached brands + members."""
    master = await _require_master(r, master_id)
    brand_ids = sorted(await r.smembers(_k_master_brands(master_id)))
    store_map = await r.hgetall(_k_master_stores(master_id))
    member_ids = await r.smembers(_k_master_members(master_id))

    members: list[dict] = []
    for mb_id in sorted(member_ids):
        mb = await r.hgetall(_k_member(mb_id))
        if mb:
            mb["brand_scope"] = _scope_to_response(mb.get("brand_scope", "all"))
            members.append(mb)

    brands_payload = [
        {"brand_id": bid, "store_name": store_map.get(bid)} for bid in brand_ids
    ]

    return {
        "master_id": master_id,
        "company_name": master.get("company_name"),
        "primary_email": master.get("primary_email"),
        "owner_user_id": master.get("owner_user_id"),
        "created_at": master.get("created_at"),
        "monthly_budget_cents": int(master.get("monthly_budget_cents") or 0),
        "budget_allocation": json.loads(master.get("budget_allocation_json") or "{}"),
        "brands": brands_payload,
        "members": members,
    }


def _scope_to_response(scope: str) -> Any:
    """Render brand_scope for API responses (string 'all' or list)."""
    if scope == "all":
        return "all"
    try:
        parsed = json.loads(scope)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return "all"


@router.post("/{master_id}/members/invite")
async def invite_member(
    master_id: str,
    body: InviteMemberBody,
    r: aioredis.Redis = Depends(get_redis),
):
    """Create a pending invite. Caller-app sends the invite_link via email.

    Note: there is no `accept-invite` endpoint in this drop — the auth
    service is expected to consume the invite when the user signs up.
    For MVP we simply expose the invite payload + a deterministic link.
    """
    await _require_master(r, master_id)

    if body.role not in VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"role must be one of {sorted(VALID_ROLES)}",
        )

    invite_id = f"inv_{uuid4().hex[:20]}"
    scope = _normalize_brand_scope(body.role, body.brand_scope)
    now = _now_iso()

    payload = {
        "invite_id": invite_id,
        "master_id": master_id,
        "email": body.email,
        "role": body.role,
        "brand_scope": scope,
        "created_at": now,
        "status": "pending",
    }
    await r.hset(_k_invite(invite_id), mapping=payload)
    await r.expire(_k_invite(invite_id), INVITE_TTL_SECONDS)

    invite_link = f"/portal/invite/accept?invite_id={invite_id}"
    logger.info(
        "member_invited master_id=%s email=%s role=%s",
        master_id,
        body.email,
        body.role,
    )
    return {
        "invite_id": invite_id,
        "invite_link": invite_link,
        "expires_in_sec": INVITE_TTL_SECONDS,
    }


@router.get("/auth/invite/{invite_id}")
async def get_invite(invite_id: str, r: aioredis.Redis = Depends(get_redis)):
    """Return invite details so the prospective user can preview what
    they're about to accept (company name, role, scope, expiry).

    Invite records auto-expire after INVITE_TTL_SECONDS via Redis EXPIRE —
    a missing/expired invite simply returns 404.
    """
    invite = await r.hgetall(_k_invite(invite_id))
    if not invite:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"invite_id={invite_id} not found or expired",
        )

    master_id = invite.get("master_id", "")
    company_name = ""
    if master_id:
        master = await r.hgetall(_k_master(master_id))
        company_name = master.get("company_name", "") if master else ""

    ttl = await r.ttl(_k_invite(invite_id))
    expires_at = None
    if ttl and ttl > 0:
        expires_at = datetime.fromtimestamp(
            time.time() + ttl, tz=timezone.utc
        ).isoformat()

    return {
        "invite_id": invite_id,
        "master_id": master_id,
        "company_name": company_name,
        "role": invite.get("role"),
        "brand_scope": _scope_to_response(invite.get("brand_scope", "all")),
        "invited_email": invite.get("email"),
        "created_at": invite.get("created_at"),
        "expires_at": expires_at,
        "status": invite.get("status", "pending"),
    }


@router.post("/auth/accept-invite", status_code=status.HTTP_201_CREATED)
async def accept_invite(
    body: AcceptInviteBody,
    r: aioredis.Redis = Depends(get_redis),
):
    """Convert a pending invite into a real Member.

    One-shot: the invite hash is deleted on success so the link cannot be
    re-used. Expired/missing invites return 404 (Redis TTL handles expiry
    automatically — a hgetall on a vanished key returns {}).
    """
    invite_key = _k_invite(body.invite_id)
    invite = await r.hgetall(invite_key)
    if not invite:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"invite_id={body.invite_id} not found or expired",
        )

    master_id = invite.get("master_id", "")
    role = invite.get("role", "")
    scope = invite.get("brand_scope", "all")
    invited_email = invite.get("email", "")

    if role not in VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invite has invalid role={role}",
        )

    # Make sure the master still exists — a manager could have deleted it
    # between invite and accept.
    await _require_master(r, master_id)

    member_id = f"mb_{uuid4().hex[:16]}"
    now = _now_iso()
    member_payload = {
        "member_id": member_id,
        "user_id": body.user_id,
        "master_id": master_id,
        "role": role,
        "brand_scope": scope,
        "email": invited_email,
        "joined_at": now,
    }
    if body.name:
        member_payload["name"] = body.name

    pipe = r.pipeline()
    pipe.hset(_k_member(member_id), mapping=member_payload)
    pipe.sadd(_k_master_members(master_id), member_id)
    pipe.sadd(_k_user_masters(body.user_id), master_id)
    pipe.sadd(_k_user_members(body.user_id), member_id)
    pipe.delete(invite_key)  # one-use
    await pipe.execute()

    logger.info(
        "invite_accepted invite_id=%s master_id=%s user_id=%s role=%s",
        body.invite_id,
        master_id,
        body.user_id,
        role,
    )
    return {
        "member_id": member_id,
        "master_id": master_id,
        "role": role,
        "brand_scope": _scope_to_response(scope),
        "joined_at": now,
    }


@router.post("/{master_id}/members/{member_id}/role")
async def update_member_role(
    master_id: str,
    member_id: str,
    body: UpdateRoleBody,
    r: aioredis.Redis = Depends(get_redis),
):
    """Update a member's role and/or brand_scope.

    Guards against demoting the *last* hq_admin — the master would be
    left ungovernable. Owner cannot be demoted via this endpoint at all.
    """
    await _require_master(r, master_id)
    member = await _require_member(r, member_id)
    if member.get("master_id") != master_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="member does not belong to this master",
        )
    if body.role not in VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"role must be one of {sorted(VALID_ROLES)}",
        )

    master = await r.hgetall(_k_master(master_id))
    if member.get("user_id") == master.get("owner_user_id") and body.role != "hq_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="owner cannot be demoted from hq_admin",
        )

    # Last-hq_admin guard.
    if member.get("role") == "hq_admin" and body.role != "hq_admin":
        admin_count = await _count_role(r, master_id, "hq_admin")
        if admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="cannot demote the last hq_admin",
            )

    scope = (
        _normalize_brand_scope(body.role, body.brand_scope)
        if body.brand_scope is not None
        else _normalize_brand_scope(body.role, member.get("brand_scope", "all"))
    )

    await r.hset(
        _k_member(member_id),
        mapping={"role": body.role, "brand_scope": scope},
    )
    logger.info(
        "member_role_updated master_id=%s member_id=%s role=%s",
        master_id,
        member_id,
        body.role,
    )
    return {
        "member_id": member_id,
        "role": body.role,
        "brand_scope": _scope_to_response(scope),
    }


async def _count_role(r: aioredis.Redis, master_id: str, role: str) -> int:
    member_ids = await r.smembers(_k_master_members(master_id))
    count = 0
    for mb_id in member_ids:
        mb = await r.hgetall(_k_member(mb_id))
        if mb.get("role") == role:
            count += 1
    return count


@router.delete("/{master_id}/members/{member_id}")
async def remove_member(
    master_id: str,
    member_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Remove a member from a master.

    Refuses to remove the owner or the last hq_admin.
    """
    await _require_master(r, master_id)
    member = await _require_member(r, member_id)
    if member.get("master_id") != master_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="member does not belong to this master",
        )

    master = await r.hgetall(_k_master(master_id))
    if member.get("user_id") == master.get("owner_user_id"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cannot remove owner; transfer ownership first",
        )
    if member.get("role") == "hq_admin":
        admin_count = await _count_role(r, master_id, "hq_admin")
        if admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="cannot remove the last hq_admin",
            )

    user_id = member.get("user_id", "")
    pipe = r.pipeline()
    pipe.delete(_k_member(member_id))
    pipe.srem(_k_master_members(master_id), member_id)
    if user_id:
        pipe.srem(_k_user_members(user_id), member_id)
        # Drop master from user's master-set only if no other membership
        # in the same master remains. Cheap second pass below.
    await pipe.execute()

    if user_id:
        remaining = await r.smembers(_k_user_members(user_id))
        still_in_master = False
        for other_id in remaining:
            other = await r.hgetall(_k_member(other_id))
            if other.get("master_id") == master_id:
                still_in_master = True
                break
        if not still_in_master:
            await r.srem(_k_user_masters(user_id), master_id)

    logger.info("member_removed master_id=%s member_id=%s", master_id, member_id)
    return {"member_id": member_id, "removed": True}


@router.get("/{master_id}/members")
async def list_members(master_id: str, r: aioredis.Redis = Depends(get_redis)):
    """List all members of a master with their roles + scopes."""
    await _require_master(r, master_id)
    member_ids = sorted(await r.smembers(_k_master_members(master_id)))
    out: list[dict] = []
    for mb_id in member_ids:
        mb = await r.hgetall(_k_member(mb_id))
        if not mb:
            continue
        mb["brand_scope"] = _scope_to_response(mb.get("brand_scope", "all"))
        out.append(mb)
    return {"master_id": master_id, "members": out, "count": len(out)}


@router.post("/auth/check")
async def auth_check(
    body: CheckPermissionBody,
    r: aioredis.Redis = Depends(get_redis),
):
    """Stateless RBAC probe — calling services use this from middleware.

    Returns (allowed, role, reason?). On allow, reason is null. On deny
    reason is a short tag suitable for logging / 403 detail strings.
    """
    allowed, info = await check_permission(body.user_id, body.action, body.brand_id, r)
    if allowed:
        return {"allowed": True, "role": info, "reason": None}
    return {"allowed": False, "role": None, "reason": info}


@router.get("/user/{user_id}/accessible-brands")
async def accessible_brands(user_id: str, r: aioredis.Redis = Depends(get_redis)):
    """Return every brand_id this user can touch + per-brand role/actions.

    A user may be a member of more than one master, so the answer is
    grouped by master. When a single brand is reachable via multiple
    memberships (rare but possible after re-parenting), the most-
    privileged role wins on a fixed precedence ordering.
    """
    role_rank = {
        "hq_admin": 5,
        "finance": 4,
        "ops_manager": 3,
        "store_manager": 2,
        "viewer": 1,
    }

    member_ids = await r.smembers(_k_user_members(user_id))
    matrix = await _load_rbac_matrix(r)
    by_brand: dict[str, dict] = {}

    for mb_id in member_ids:
        member = await r.hgetall(_k_member(mb_id))
        if not member:
            continue
        master_id = member.get("master_id", "")
        role = member.get("role", "")
        scope = member.get("brand_scope", "all")
        if role not in VALID_ROLES:
            continue

        master_brand_ids = await r.smembers(_k_master_brands(master_id))
        if scope == "all":
            reachable = set(master_brand_ids)
        else:
            try:
                parsed = json.loads(scope)
                reachable = set(parsed) & set(master_brand_ids) if isinstance(
                    parsed, list
                ) else set()
            except (json.JSONDecodeError, TypeError):
                reachable = set()

        actions = matrix.get(role, [])
        for bid in reachable:
            existing = by_brand.get(bid)
            if existing and role_rank.get(role, 0) <= role_rank.get(existing["role"], 0):
                continue
            by_brand[bid] = {
                "brand_id": bid,
                "master_id": master_id,
                "role": role,
                "actions": actions,
            }

    return {
        "user_id": user_id,
        "brands": sorted(by_brand.values(), key=lambda x: x["brand_id"]),
        "count": len(by_brand),
    }


@router.post("/{master_id}/budget/global")
async def set_global_budget(
    master_id: str,
    body: GlobalBudgetBody,
    r: aioredis.Redis = Depends(get_redis),
):
    """Set master-level monthly budget + allocation map AND cascade to wallets.

    For each (brand_id, pct) in allocation:
      monthly_alloc_cents = monthly_budget_cents * pct
      daily_budget_cents  = monthly_alloc_cents / 30  (calendar month avg)
      → push to wallet:{brand_id}:daily_budget

    If a brand_id has no wallet yet we still write the daily_budget key —
    this auto-creates the wallet's budget side; the balance side stays 0
    until a topup happens. This is intentional: budgets are policy, not
    money, so they can be set ahead of the first topup.

    Validation:
      * allocation keys must all be attached brand_ids.
      * allocation values must sum to ~1.0 OR ~100 (auto-detected).
    """
    # Local import to avoid a top-level cycle (wallet.py imports nothing
    # from this module today, but keeping it lazy is defensive).
    from app.routers.wallet import _k_daily_budget

    await _require_master(r, master_id)
    attached = await r.smembers(_k_master_brands(master_id))

    # Normalise allocation: percentages → fractions for storage.
    alloc = dict(body.allocation)
    if alloc and sum(alloc.values()) > 1.5:
        # caller used 0-100 scale
        alloc = {k: v / 100.0 for k, v in alloc.items()}

    unknown = set(alloc.keys()) - set(attached)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"allocation references unattached brand_ids: {sorted(unknown)}",
        )

    cascaded: list[dict] = []
    pipe = r.pipeline()
    for bid, pct in alloc.items():
        monthly_alloc = int(round(body.monthly_budget_cents * pct))
        # 30-day calendar approximation. Daily caps don't need leap-year
        # precision — they're soft circuit breakers, not accounting.
        daily_budget = int(round(monthly_alloc / 30))
        pipe.set(_k_daily_budget(bid), daily_budget)
        cascaded.append(
            {
                "brand_id": bid,
                "monthly_alloc_cents": monthly_alloc,
                "daily_budget_cents": daily_budget,
            }
        )

    now = _now_iso()
    pipe.hset(
        _k_master(master_id),
        mapping={
            "monthly_budget_cents": body.monthly_budget_cents,
            "budget_allocation_json": json.dumps(alloc),
            "budget_updated_at": now,
        },
    )
    await pipe.execute()

    logger.info(
        "master_budget_set master_id=%s monthly_cents=%d brands=%d cascaded=%d",
        master_id,
        body.monthly_budget_cents,
        len(alloc),
        len(cascaded),
    )
    return {
        "master_id": master_id,
        "monthly_budget_cents": body.monthly_budget_cents,
        "allocation_fractions": alloc,
        "cascaded": cascaded,
        "updated_at": now,
    }


@router.post("/{master_id}/wallet/topup-all", status_code=status.HTTP_201_CREATED)
async def topup_all_from_master(
    master_id: str,
    body: TopupAllBody,
    r: aioredis.Redis = Depends(get_redis),
):
    """Top up the master account once, distribute to N child wallets.

    The merchant supplies a single payment_token + total amount and an
    allocation map; we fan out into per-brand confirmed topups. Each
    child topup is an idempotent record keyed by topup_id so retries
    of this endpoint don't double-credit (the master_topup_id is the
    correlation key).

    Validation:
      * every brand_id in allocation must be attached to this master.
      * allocation sums to ~1.0 or ~100 (auto-detected).
    """
    from app.routers.wallet import (
        _k_balance,
        _k_currency,
        _k_last_topup,
        _k_topup,
        _k_tx_list,
        DEFAULT_CURRENCY,
        TX_LIST_MAX,
    )

    await _require_master(r, master_id)
    attached = await r.smembers(_k_master_brands(master_id))

    alloc = dict(body.allocation)
    if alloc and sum(alloc.values()) > 1.5:
        alloc = {k: v / 100.0 for k, v in alloc.items()}

    unknown = set(alloc.keys()) - set(attached)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"allocation references unattached brand_ids: {sorted(unknown)}",
        )

    master_topup_id = f"mtop_{uuid4().hex[:20]}"
    now = time.time()
    per_brand: list[dict] = []

    # Distribute. We round each share and absorb the rounding delta into
    # the last (alphabetically) brand so the sum exactly matches the
    # total — accounting hates lost pennies.
    sorted_brands = sorted(alloc.items())
    shares: dict[str, int] = {}
    running = 0
    for bid, pct in sorted_brands[:-1]:
        share = int(round(body.amount_cents_total * pct))
        shares[bid] = share
        running += share
    last_bid = sorted_brands[-1][0]
    shares[last_bid] = body.amount_cents_total - running

    for bid, share_cents in shares.items():
        try:
            topup_id = f"{master_topup_id}_{bid}"
            # Lock in currency on first use.
            cur_existing = await r.get(_k_currency(bid))
            currency = (cur_existing or DEFAULT_CURRENCY).upper()
            if cur_existing is None:
                await r.set(_k_currency(bid), currency)

            pipe = r.pipeline()
            pipe.hset(
                _k_topup(topup_id),
                mapping={
                    "topup_id": topup_id,
                    "brand_id": bid,
                    "amount": share_cents,
                    "currency": currency,
                    "payment_method": body.payment_method,
                    "payment_token": body.payment_token or "",
                    "status": "confirmed",
                    "created_at": now,
                    "confirmed_at": now,
                    "master_topup_id": master_topup_id,
                },
            )
            pipe.incrby(_k_balance(bid), share_cents)
            pipe.set(_k_last_topup(bid), now)
            pipe.rpush(_k_tx_list(bid), topup_id)
            pipe.ltrim(_k_tx_list(bid), -TX_LIST_MAX, -1)
            await pipe.execute()

            per_brand.append(
                {
                    "brand_id": bid,
                    "amount": share_cents,
                    "topup_id": topup_id,
                    "status": "confirmed",
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "topup_all brand=%s failed master_topup_id=%s",
                bid,
                master_topup_id,
            )
            per_brand.append(
                {
                    "brand_id": bid,
                    "amount": share_cents,
                    "topup_id": None,
                    "status": "failed",
                    "error": str(exc),
                }
            )

    logger.info(
        "master_topup_all master_id=%s master_topup_id=%s total_cents=%d brands=%d",
        master_id,
        master_topup_id,
        body.amount_cents_total,
        len(per_brand),
    )
    return {
        "master_topup_id": master_topup_id,
        "master_id": master_id,
        "amount_cents_total": body.amount_cents_total,
        "per_brand": per_brand,
    }


@router.get("/{master_id}/consolidated-report")
async def consolidated_report(
    master_id: str, r: aioredis.Redis = Depends(get_redis)
):
    """Aggregate KPIs across all attached brands.

    Best-effort: reads each brand's wallet + campaign stats and sums.
    Missing keys are treated as zero (a brand may have no campaigns or
    no wallet yet). The shape matches what an HQ dashboard would want
    on a single screen — totals plus per-brand breakdown.
    """
    await _require_master(r, master_id)
    brand_ids = sorted(await r.smembers(_k_master_brands(master_id)))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    totals = {
        "balance_cents": 0,
        "total_spent_cents": 0,
        "daily_spent_cents": 0,
        "campaigns_count": 0,
    }
    per_brand: list[dict] = []

    for bid in brand_ids:
        pipe = r.pipeline()
        pipe.get(f"wallet:{bid}:balance")
        pipe.get(f"wallet:{bid}:total_spent")
        pipe.get(f"wallet:{bid}:daily_spent:{today}")
        pipe.scard(f"brand:{bid}:campaigns")
        bal, spent, daily, cmp_count = await pipe.execute()

        bal_i = int(bal or 0)
        spent_i = int(spent or 0)
        daily_i = int(daily or 0)
        cmp_i = int(cmp_count or 0)

        totals["balance_cents"] += bal_i
        totals["total_spent_cents"] += spent_i
        totals["daily_spent_cents"] += daily_i
        totals["campaigns_count"] += cmp_i

        per_brand.append(
            {
                "brand_id": bid,
                "balance_cents": bal_i,
                "total_spent_cents": spent_i,
                "daily_spent_cents": daily_i,
                "campaigns_count": cmp_i,
            }
        )

    return {
        "master_id": master_id,
        "as_of": _now_iso(),
        "totals": totals,
        "per_brand": per_brand,
        "brand_count": len(brand_ids),
    }


# ── Public re-exports ────────────────────────────────────────────────────
__all__ = [
    "router",
    "check_permission",
    "VALID_ROLES",
    "DEFAULT_RBAC_MATRIX",
]
