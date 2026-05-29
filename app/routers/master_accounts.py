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


# ─────────────────────────────────────────────────────────────────────────
# FEATURE 1 — Cross-Brand Visit Reports
# ─────────────────────────────────────────────────────────────────────────
#
# Why this exists
# ---------------
# A 5-store gym chain (老周 fitness sim P0) needed a consolidated view of
# user flow across its stores: who comes back, who tries 2+ stores, which
# stores feed each other. The data lives across three brand-scoped indices:
#
#   brand:{bid}:attr_incoming      ZSET  score=ts  member=event_id
#   brand:{bid}:redeemed_vouchers  ZSET  score=redeemed_at  member=vid
#   brand:{bid}:reservations       ZSET  score=scheduled_at member=rid
#                                  (we filter by status == "honored")
#
# We walk each brand under the master, collect (user_id, ts) visit tuples,
# aggregate per user and per (brand_a → brand_b) ordered pair, and cache
# the result for 30 minutes in `master:{mid}:cross_brand_cache:{key}`.
#
# Cache key shape: f"{endpoint}:{from_ts}:{to_ts}:{extra}". Invalidation is
# TTL-only; cross-brand traffic is bursty but tolerant of 30-min staleness.
# ─────────────────────────────────────────────────────────────────────────

_CROSS_BRAND_CACHE_TTL = 30 * 60  # 30 minutes
_TOP_CROSS_VISITORS_DEFAULT = 50


def _k_cross_brand_cache(master_id: str, key: str) -> str:
    return f"master:{master_id}:cross_brand_cache:{key}"


async def _collect_brand_visits(
    r: aioredis.Redis,
    brand_id: str,
    from_ts: float | None,
    to_ts: float | None,
    user_filter: str | None,
) -> list[tuple[str, float]]:
    """Return chronological (user_id, ts) visits for a single brand.

    Three sources are unioned and de-duplicated by (user_id, source, id):
      * attr_incoming     — every tracked impression/click/visit landing here
      * redeemed_vouchers — voucher redemptions (definite physical visits)
      * reservations w/ status=honored — booked + showed up

    For attr_incoming and redeemed_vouchers the score IS the timestamp.
    For reservations we resolve the hash and use honored_at.
    """
    lo = "-inf" if from_ts is None else from_ts
    hi = "+inf" if to_ts is None else to_ts
    visits: list[tuple[str, float]] = []

    # 1) attribution events landing on this brand
    try:
        events = await r.zrangebyscore(
            f"brand:{brand_id}:attr_incoming", lo, hi
        )
    except Exception:  # noqa: BLE001
        events = []
    for ev_id in events or []:
        try:
            ev = await r.hgetall(f"attr:{ev_id}")
        except Exception:  # noqa: BLE001
            ev = {}
        if not ev:
            continue
        uid = ev.get("user_id") or ""
        if not uid:
            continue
        if user_filter and uid != user_filter:
            continue
        try:
            ts = float(ev.get("timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        visits.append((uid, ts))

    # 2) voucher redemptions
    try:
        vids = await r.zrangebyscore(
            f"brand:{brand_id}:redeemed_vouchers", lo, hi, withscores=True
        )
    except Exception:  # noqa: BLE001
        vids = []
    for vid, ts in vids or []:
        try:
            v = await r.hgetall(f"voucher:{vid}")
        except Exception:  # noqa: BLE001
            v = {}
        uid = (v or {}).get("holder_user_id") or (v or {}).get(
            "original_holder_user_id", ""
        )
        if not uid:
            continue
        if user_filter and uid != user_filter:
            continue
        visits.append((uid, float(ts)))

    # 3) honored reservations
    try:
        rids = await r.zrangebyscore(
            f"brand:{brand_id}:reservations", lo, hi, withscores=True
        )
    except Exception:  # noqa: BLE001
        rids = []
    for rid, _sched_ts in rids or []:
        try:
            res = await r.hgetall(f"reservation:{rid}")
        except Exception:  # noqa: BLE001
            res = {}
        if (res or {}).get("status") != "honored":
            continue
        uid = res.get("user_id", "")
        if not uid:
            continue
        if user_filter and uid != user_filter:
            continue
        try:
            ts = float(res.get("honored_at") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        # Also accept reservations with no honored_at by using the score.
        if ts <= 0:
            ts = float(_sched_ts)
        visits.append((uid, ts))

    visits.sort(key=lambda t: t[1])
    return visits


async def _gather_master_visits(
    r: aioredis.Redis,
    master_id: str,
    from_ts: float | None,
    to_ts: float | None,
    user_filter: str | None = None,
) -> tuple[list[str], dict[str, list[tuple[str, float]]]]:
    """Return (sorted_brand_ids, {brand_id: visits}).

    `visits` is sorted by ts ascending per brand. Empty brands stay in the
    output so downstream callers can render zeros — that's deliberate for
    matrix tables.
    """
    brand_ids = sorted(await r.smembers(_k_master_brands(master_id)))
    out: dict[str, list[tuple[str, float]]] = {}
    for bid in brand_ids:
        out[bid] = await _collect_brand_visits(
            r, bid, from_ts, to_ts, user_filter
        )
    return brand_ids, out


async def _cache_get_json(r: aioredis.Redis, key: str) -> Any | None:
    raw = await r.get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


async def _cache_set_json(r: aioredis.Redis, key: str, payload: Any) -> None:
    try:
        await r.set(key, json.dumps(payload), ex=_CROSS_BRAND_CACHE_TTL)
    except Exception:  # noqa: BLE001
        logger.warning("cross_brand_cache write failed key=%s", key, exc_info=True)


@router.get("/{master_id}/cross-brand-visits")
async def cross_brand_visits(
    master_id: str,
    from_ts: float | None = None,
    to_ts: float | None = None,
    user_id: str | None = None,
    top_n: int = _TOP_CROSS_VISITORS_DEFAULT,
    r: aioredis.Redis = Depends(get_redis),
):
    """Cross-store visit matrix for a master.

    Returns the brand-to-brand transition counts (`brand_a_to_brand_b`),
    where each user's chronological visit sequence is walked once and
    every consecutive pair contributes +1 to the corresponding matrix
    cell. Self-loops (`brand_a_to_brand_a`) count returning customers.

    Result is cached 30 min in `master:{mid}:cross_brand_cache:{key}`.
    """
    await _require_master(r, master_id)

    cache_key = f"cbv:{from_ts}:{to_ts}:{user_id}:{top_n}"
    cached = await _cache_get_json(r, _k_cross_brand_cache(master_id, cache_key))
    if cached is not None:
        cached["_cached"] = True
        return cached

    brand_ids, per_brand = await _gather_master_visits(
        r, master_id, from_ts, to_ts, user_id
    )

    # Per-user chronological sequence of brand visits.
    user_visits: dict[str, list[tuple[str, float]]] = {}
    total_visits = 0
    for bid, visits in per_brand.items():
        for uid, ts in visits:
            user_visits.setdefault(uid, []).append((bid, ts))
            total_visits += 1
    for uid in user_visits:
        user_visits[uid].sort(key=lambda t: t[1])

    # Brand-to-brand transition matrix (ordered: a→b).
    matrix: dict[str, int] = {}
    for seq in user_visits.values():
        for i in range(len(seq) - 1):
            a = seq[i][0]
            b = seq[i + 1][0]
            matrix[f"{a}_to_{b}"] = matrix.get(f"{a}_to_{b}", 0) + 1

    # Top cross-visitors (most visits across the network).
    top_visitors: list[dict] = []
    for uid, seq in user_visits.items():
        brand_set = {b for b, _ in seq}
        last_ts = seq[-1][1] if seq else 0.0
        top_visitors.append(
            {
                "user_id": uid,
                "visit_count": len(seq),
                "brand_count": len(brand_set),
                "last_visit_ts": last_ts,
            }
        )
    # Sort by brand_count desc (cross-store engagement is the real signal),
    # then by visit_count desc as tiebreaker.
    top_visitors.sort(
        key=lambda d: (-d["brand_count"], -d["visit_count"], -d["last_visit_ts"])
    )
    top_visitors = top_visitors[: max(0, int(top_n))]

    result = {
        "master_id": master_id,
        "brands": brand_ids,
        "total_visits": total_visits,
        "unique_users": len(user_visits),
        "matrix": matrix,
        "top_cross_visitors": top_visitors,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "_cached": False,
    }
    await _cache_set_json(r, _k_cross_brand_cache(master_id, cache_key), result)
    return result


def _period_bucket(ts: float, period: str) -> str:
    """Bucket a unix timestamp into a YYYY-MM-DD / week / month label."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(
        timezone.utc
    )
    if period == "weekly":
        # ISO year-week so weeks crossing months still line up.
        y, w, _ = dt.isocalendar()
        return f"{y}-W{w:02d}"
    if period == "monthly":
        return dt.strftime("%Y-%m")
    return dt.strftime("%Y-%m-%d")


@router.get("/{master_id}/user-flow")
async def master_user_flow(
    master_id: str,
    user_id: str | None = None,
    period: Literal["daily", "weekly", "monthly"] = "daily",
    from_ts: float | None = None,
    to_ts: float | None = None,
    r: aioredis.Redis = Depends(get_redis),
):
    """Time-series of user flow across brands.

    For each time bucket and each brand-set membership (which brands a
    user touched in that bucket), count distinct users. The output series
    surfaces, e.g., how many users were in (gym_jingan only) vs (徐汇
    only) vs (both) per day — which is exactly how you read overlap.

    Buckets are computed from the *first* visit in the bucket for each
    (user, period). A user appearing in 3 brands in one day shows up once
    in the `gym_a|gym_b|gym_c` overlap row, not three times.
    """
    await _require_master(r, master_id)

    cache_key = f"uflow:{from_ts}:{to_ts}:{user_id}:{period}"
    cached = await _cache_get_json(r, _k_cross_brand_cache(master_id, cache_key))
    if cached is not None:
        cached["_cached"] = True
        return cached

    brand_ids, per_brand = await _gather_master_visits(
        r, master_id, from_ts, to_ts, user_id
    )

    # bucket → user → set(brand_ids)
    bucket_user_brands: dict[str, dict[str, set[str]]] = {}
    for bid, visits in per_brand.items():
        for uid, ts in visits:
            bucket = _period_bucket(ts, period)
            bucket_user_brands.setdefault(bucket, {}).setdefault(uid, set()).add(bid)

    # Render: for each bucket, count by sorted-brand-set signature.
    series: list[dict] = []
    for bucket in sorted(bucket_user_brands.keys()):
        groups: dict[str, int] = {}
        active_users = 0
        for uid, brands in bucket_user_brands[bucket].items():
            sig = "|".join(sorted(brands))
            groups[sig] = groups.get(sig, 0) + 1
            active_users += 1
        series.append(
            {
                "bucket": bucket,
                "active_users": active_users,
                "groups": groups,
            }
        )

    result = {
        "master_id": master_id,
        "period": period,
        "brands": brand_ids,
        "series": series,
        "_cached": False,
    }
    await _cache_set_json(r, _k_cross_brand_cache(master_id, cache_key), result)
    return result


@router.get("/{master_id}/cross-store-cohort")
async def cross_store_cohort(
    master_id: str,
    from_ts: float | None = None,
    size: int = 30,
    r: aioredis.Redis = Depends(get_redis),
):
    """Cohort users by FIRST brand visited, track subsequent brands.

    For each user in the window we identify their first brand
    (chronologically); they enter that cohort. We then count which OTHER
    brands they subsequently touch. Output is a cohort × subsequent-brand
    matrix — useful for "does store A feed store B?" answers.

    `size` caps the per-cohort sample size in the response (full counts
    are still aggregated; size only limits the user-level sample echoed
    back).
    """
    await _require_master(r, master_id)

    cache_key = f"cohort:{from_ts}:{size}"
    cached = await _cache_get_json(r, _k_cross_brand_cache(master_id, cache_key))
    if cached is not None:
        cached["_cached"] = True
        return cached

    brand_ids, per_brand = await _gather_master_visits(
        r, master_id, from_ts, None, None
    )

    user_visits: dict[str, list[tuple[str, float]]] = {}
    for bid, visits in per_brand.items():
        for uid, ts in visits:
            user_visits.setdefault(uid, []).append((bid, ts))
    for uid in user_visits:
        user_visits[uid].sort(key=lambda t: t[1])

    # cohort_brand → {subsequent_brand → count}
    cohorts: dict[str, dict] = {}
    for uid, seq in user_visits.items():
        if not seq:
            continue
        first_brand = seq[0][0]
        cohort = cohorts.setdefault(
            first_brand,
            {"cohort_size": 0, "subsequent": {}, "sample_users": []},
        )
        cohort["cohort_size"] += 1
        seen_subseq: set[str] = set()
        for bid, _ts in seq[1:]:
            if bid == first_brand:
                # Returning visits are interesting — track as self-return.
                key = "_returned_to_self"
            else:
                key = bid
            if key in seen_subseq:
                continue
            seen_subseq.add(key)
            cohort["subsequent"][key] = cohort["subsequent"].get(key, 0) + 1
        if len(cohort["sample_users"]) < size:
            cohort["sample_users"].append(uid)

    result = {
        "master_id": master_id,
        "brands": brand_ids,
        "from_ts": from_ts,
        "size": size,
        "cohorts": cohorts,
        "_cached": False,
    }
    await _cache_set_json(r, _k_cross_brand_cache(master_id, cache_key), result)
    return result


@router.get("/{master_id}/consolidated-funnel")
async def consolidated_funnel(
    master_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Master-level acquisition funnel summed across all brands.

    Reads each brand's funnel counters (impressions / clicks / conversions
    / gmv) and aggregates. Missing keys are treated as zero so a brand
    with no traffic does not blow up the rollup.

    Funnel counter keys are best-effort discovered from the attribution
    module's conventions:
      brand:{bid}:funnel:impressions
      brand:{bid}:funnel:clicks
      brand:{bid}:funnel:conversions
      brand:{bid}:funnel:gmv_cents

    Brands that haven't published any of those keys fall back to ZCARD of
    their attr_incoming index for an impression-equivalent count, so the
    endpoint still answers something sensible during early integration.
    """
    await _require_master(r, master_id)

    brand_ids = sorted(await r.smembers(_k_master_brands(master_id)))

    totals = {
        "impressions": 0,
        "clicks": 0,
        "conversions": 0,
        "gmv_cents": 0,
    }
    per_brand_breakdown: list[dict] = []

    for bid in brand_ids:
        pipe = r.pipeline()
        pipe.get(f"brand:{bid}:funnel:impressions")
        pipe.get(f"brand:{bid}:funnel:clicks")
        pipe.get(f"brand:{bid}:funnel:conversions")
        pipe.get(f"brand:{bid}:funnel:gmv_cents")
        pipe.zcard(f"brand:{bid}:attr_incoming")
        imps, clks, convs, gmv, attr_card = await pipe.execute()

        imp_i = int(imps or 0)
        clk_i = int(clks or 0)
        cnv_i = int(convs or 0)
        gmv_i = int(gmv or 0)
        # Fallback: if dedicated funnel counters aren't published yet,
        # use the attribution incoming-zset cardinality as the impression
        # floor. Zero is a worse answer than approximate.
        if imp_i == 0 and attr_card:
            imp_i = int(attr_card)

        totals["impressions"] += imp_i
        totals["clicks"] += clk_i
        totals["conversions"] += cnv_i
        totals["gmv_cents"] += gmv_i

        per_brand_breakdown.append(
            {
                "brand_id": bid,
                "impressions": imp_i,
                "clicks": clk_i,
                "conversions": cnv_i,
                "gmv_cents": gmv_i,
            }
        )

    # Derived rates — safe-division.
    ctr = (totals["clicks"] / totals["impressions"]) if totals["impressions"] else 0.0
    cvr = (
        totals["conversions"] / totals["clicks"]
    ) if totals["clicks"] else 0.0

    return {
        "master_id": master_id,
        "as_of": _now_iso(),
        "impressions": totals["impressions"],
        "clicks": totals["clicks"],
        "conversions": totals["conversions"],
        "gmv_cents": totals["gmv_cents"],
        "ctr": ctr,
        "cvr": cvr,
        "per_brand_breakdown": per_brand_breakdown,
        "brand_count": len(brand_ids),
    }


# ─────────────────────────────────────────────────────────────────────────
# FEATURE 2 — Master-Scoped Tier (cross-brand portability)
# ─────────────────────────────────────────────────────────────────────────
#
# Brand-scoped tiers (existing) record a user as Elite at brand A but as
# Guest at brand B in the same chain. For a chain operator that's wrong:
# loyalty should follow the master. We layer a master-level ladder on top.
#
# Storage
#   master:{mid}:tier_config        LIST  JSON tier dicts {name, xp_min}
#                                         sorted ascending by xp_min
#   master:{mid}:tier_aggregation   STR   "sum" | "max" | "avg"
#                                         (default "sum")
#   master:{mid}:tier_promotion     HASH  {rule, weights_json}
#                                         rule ∈ {"max_of_brand_tier",
#                                                 "sum_xp_then_tier",
#                                                 "weighted_brand_xp"}
#
# Per-brand XP comes from `user:{uid}:currency:{bid}:xp` (the same source
# of truth used by primitives.py). We never overwrite brand tiers — this
# is a strictly additive layer that callers consult FIRST.
# ─────────────────────────────────────────────────────────────────────────

VALID_TIER_AGGREGATIONS: set[str] = {"sum", "max", "avg"}
VALID_PROMOTION_RULES: set[str] = {
    "max_of_brand_tier",
    "sum_xp_then_tier",
    "weighted_brand_xp",
}


class MasterTierEntry(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    xp_min: int = Field(..., ge=0)


class ConfigureMasterTiersBody(BaseModel):
    tiers: list[MasterTierEntry]
    aggregation: Literal["sum", "max", "avg"] = "sum"

    @field_validator("tiers")
    @classmethod
    def _non_empty(cls, v):
        if not v:
            raise ValueError("tiers must include at least one entry")
        names = [t.name for t in v]
        if len(set(names)) != len(names):
            raise ValueError("tier names must be unique")
        return v


class MasterPromotionRuleBody(BaseModel):
    rule: Literal[
        "max_of_brand_tier", "sum_xp_then_tier", "weighted_brand_xp"
    ]
    weights: dict[str, float] | None = None

    @field_validator("weights")
    @classmethod
    def _weights_shape(cls, v):
        if v is None:
            return v
        for bid, w in v.items():
            if w < 0:
                raise ValueError(f"weight for {bid} must be >= 0")
        return v


def _k_master_tier_config(mid: str) -> str:
    return f"master:{mid}:tier_config"


def _k_master_tier_aggregation(mid: str) -> str:
    return f"master:{mid}:tier_aggregation"


def _k_master_tier_promotion(mid: str) -> str:
    return f"master:{mid}:tier_promotion"


async def _read_master_tier_config(
    r: aioredis.Redis, master_id: str
) -> list[dict]:
    raw = await r.lrange(_k_master_tier_config(master_id), 0, -1)
    out: list[dict] = []
    for item in raw or []:
        try:
            d = json.loads(item)
            out.append({"name": str(d["name"]), "xp_min": int(d["xp_min"])})
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda d: d["xp_min"])
    return out


async def _read_master_aggregation(
    r: aioredis.Redis, master_id: str
) -> str:
    val = await r.get(_k_master_tier_aggregation(master_id))
    if val in VALID_TIER_AGGREGATIONS:
        return val
    return "sum"


async def _read_master_promotion(
    r: aioredis.Redis, master_id: str
) -> tuple[str, dict[str, float]]:
    raw = await r.hgetall(_k_master_tier_promotion(master_id))
    rule = (raw or {}).get("rule", "sum_xp_then_tier")
    if rule not in VALID_PROMOTION_RULES:
        rule = "sum_xp_then_tier"
    weights: dict[str, float] = {}
    try:
        weights = json.loads((raw or {}).get("weights_json") or "{}")
        if not isinstance(weights, dict):
            weights = {}
    except (json.JSONDecodeError, TypeError):
        weights = {}
    return rule, {str(k): float(v) for k, v in weights.items()}


def _aggregate_xp(values: list[int], how: str) -> int:
    if not values:
        return 0
    if how == "max":
        return max(values)
    if how == "avg":
        return int(round(sum(values) / len(values)))
    return sum(values)


def _resolve_tier_name(tiers: list[dict], xp: int) -> tuple[str | None, int | None]:
    """Return (current_tier_name, next_tier_threshold_or_None)."""
    if not tiers:
        return None, None
    current = None
    nxt_threshold = None
    for t in tiers:
        if xp >= t["xp_min"]:
            current = t["name"]
        else:
            nxt_threshold = t["xp_min"]
            break
    return current, nxt_threshold


@router.post("/{master_id}/tier/configure")
async def configure_master_tier(
    master_id: str,
    body: ConfigureMasterTiersBody,
    r: aioredis.Redis = Depends(get_redis),
):
    """Install (or replace) the master-wide tier ladder.

    Tiers are stored as a Redis LIST of JSON blobs so order is explicit
    (we sort at read time anyway, but writing pre-sorted is friendlier
    for ad-hoc Redis inspection). Replacing is a DEL-then-RPUSH — there
    is no incremental update path on purpose: tier ladders are policy,
    and partial edits are an error class we don't want to enable here.
    """
    await _require_master(r, master_id)

    sorted_tiers = sorted(body.tiers, key=lambda t: t.xp_min)
    serialized = [json.dumps({"name": t.name, "xp_min": t.xp_min}) for t in sorted_tiers]

    pipe = r.pipeline()
    pipe.delete(_k_master_tier_config(master_id))
    pipe.rpush(_k_master_tier_config(master_id), *serialized)
    pipe.set(_k_master_tier_aggregation(master_id), body.aggregation)
    await pipe.execute()

    logger.info(
        "master_tier_configured master_id=%s tiers=%d aggregation=%s",
        master_id,
        len(sorted_tiers),
        body.aggregation,
    )
    return {
        "master_id": master_id,
        "tiers": [{"name": t.name, "xp_min": t.xp_min} for t in sorted_tiers],
        "aggregation": body.aggregation,
    }


@router.post("/{master_id}/tier/promotion-rule")
async def configure_master_promotion_rule(
    master_id: str,
    body: MasterPromotionRuleBody,
    r: aioredis.Redis = Depends(get_redis),
):
    """Configure how master tier is resolved from per-brand state.

    * `sum_xp_then_tier`     — sum (or aggregate) brand XP, look up in ladder.
    * `max_of_brand_tier`    — take the highest per-brand tier rank.
    * `weighted_brand_xp`    — weighted sum of brand XPs, then ladder.
    """
    await _require_master(r, master_id)

    if body.rule == "weighted_brand_xp" and not body.weights:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="weighted_brand_xp rule requires weights map",
        )

    payload: dict[str, str] = {"rule": body.rule}
    if body.weights is not None:
        payload["weights_json"] = json.dumps(body.weights)
    else:
        payload["weights_json"] = "{}"

    pipe = r.pipeline()
    pipe.delete(_k_master_tier_promotion(master_id))
    pipe.hset(_k_master_tier_promotion(master_id), mapping=payload)
    await pipe.execute()

    logger.info(
        "master_promotion_rule_set master_id=%s rule=%s",
        master_id,
        body.rule,
    )
    return {
        "master_id": master_id,
        "rule": body.rule,
        "weights": body.weights or {},
    }


async def _gather_per_brand_xp(
    r: aioredis.Redis, user_id: str, brand_ids: list[str]
) -> dict[str, int]:
    """Read per-brand XP for a user; missing keys → 0 (skipped)."""
    out: dict[str, int] = {}
    for bid in brand_ids:
        try:
            raw = await r.get(f"user:{user_id}:currency:{bid}:xp")
            if raw is None:
                continue
            out[bid] = int(raw)
        except (TypeError, ValueError):
            continue
    return out


async def _resolve_master_tier_internal(
    r: aioredis.Redis, master_id: str, user_id: str
) -> dict[str, Any]:
    """Compute master-tier payload (used by both the endpoint and the
    exported helper). Returns enough context for downstream consumers
    to debug what happened."""
    tiers = await _read_master_tier_config(r, master_id)
    aggregation = await _read_master_aggregation(r, master_id)
    rule, weights = await _read_master_promotion(r, master_id)

    brand_ids = sorted(await r.smembers(_k_master_brands(master_id)))
    per_brand_xp = await _gather_per_brand_xp(r, user_id, brand_ids)

    aggregated_xp = 0
    if rule == "weighted_brand_xp":
        # Normalize weights to fractions (accept either 0-1 or 0-100).
        total_w = sum(weights.values()) if weights else 0.0
        if total_w > 1.5:
            norm = {k: v / 100.0 for k, v in weights.items()}
        elif total_w > 0:
            norm = dict(weights)
        else:
            norm = {}
        aggregated_xp = int(
            round(sum(per_brand_xp.get(bid, 0) * norm.get(bid, 0) for bid in brand_ids))
        )
    elif rule == "max_of_brand_tier":
        # Use the highest-XP brand's value to look up master tier; tier
        # names take precedence via _max_brand_tier below.
        aggregated_xp = max(per_brand_xp.values()) if per_brand_xp else 0
    else:
        # sum_xp_then_tier — honour the configured aggregation knob.
        aggregated_xp = _aggregate_xp(list(per_brand_xp.values()), aggregation)

    current_tier, next_threshold = _resolve_tier_name(tiers, aggregated_xp)

    # For max_of_brand_tier we additionally inspect per-brand tier name
    # strings (if the brand has one stored at user:{uid}:tier:{bid}) and
    # rank them against the master ladder.
    if rule == "max_of_brand_tier":
        best_idx = -1
        best_name = current_tier
        tier_rank = {t["name"]: idx for idx, t in enumerate(tiers)}
        for bid in brand_ids:
            try:
                name = await r.get(f"user:{user_id}:tier:{bid}")
            except Exception:  # noqa: BLE001
                name = None
            if name and name in tier_rank and tier_rank[name] > best_idx:
                best_idx = tier_rank[name]
                best_name = name
        if best_name:
            current_tier = best_name

    return {
        "master_id": master_id,
        "user_id": user_id,
        "aggregated_xp": aggregated_xp,
        "current_master_tier": current_tier,
        "per_brand_xp": per_brand_xp,
        "next_master_tier_threshold": next_threshold,
        "cross_brand_portability": bool(tiers),
        "aggregation": aggregation,
        "rule": rule,
        "tiers": tiers,
    }


@router.get("/{master_id}/user/{user_id}/tier")
async def get_master_user_tier(
    master_id: str,
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Return a user's resolved master-level tier.

    If no master tier ladder is configured the response still returns
    aggregated_xp + per_brand_xp + `cross_brand_portability: false`, so
    callers can detect "this master has no portability configured" and
    fall back to brand-level tiers cleanly.
    """
    await _require_master(r, master_id)
    out = await _resolve_master_tier_internal(r, master_id, user_id)
    # Strip the internal debug-ish keys from the public payload.
    payload = {
        "master_id": out["master_id"],
        "user_id": out["user_id"],
        "aggregated_xp": out["aggregated_xp"],
        "current_master_tier": out["current_master_tier"],
        "per_brand_xp": out["per_brand_xp"],
        "next_master_tier_threshold": out["next_master_tier_threshold"],
        "cross_brand_portability": out["cross_brand_portability"],
        "aggregation": out["aggregation"],
        "rule": out["rule"],
    }
    return payload


async def resolve_master_tier(
    r: aioredis.Redis, user_id: str, brand_id: str
) -> str | None:
    """Exported helper: resolve a user's master-level tier from a brand_id.

    Other modules (frequency_cap, vouchers, etc.) call this FIRST when
    resolving a user's effective tier; on `None` they fall back to the
    per-brand tier. That's how "Elite at gym A → Elite at gym B" works
    in practice: brand B's resolver looks up the master, computes the
    aggregated XP across all chain brands, and returns the master tier
    name, which beats brand B's local (likely lower) tier.

    Returns None when:
      * the brand isn't attached to any master
      * the master has no tier_config (chain hasn't opted in)
      * the user has zero XP in all brands of the master
    """
    if not brand_id or not user_id:
        return None
    try:
        master_id = await r.get(_k_brand_master(brand_id))
    except Exception:  # noqa: BLE001
        return None
    if not master_id:
        return None

    try:
        out = await _resolve_master_tier_internal(r, master_id, user_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "resolve_master_tier failed master=%s user=%s brand=%s",
            master_id,
            user_id,
            brand_id,
            exc_info=True,
        )
        return None

    if not out.get("tiers"):
        return None
    return out.get("current_master_tier")


# ─────────────────────────────────────────────────────────────────────────
# FEATURE 3 — Master-level Health Dashboard
# ─────────────────────────────────────────────────────────────────────────
#
# One-screen status for an HQ ops user: brand count, member count, GMV,
# commission paid to KiX, top/worst brand, network density (% of members
# active in 2+ brands), plus any health alerts surfaced from sibling
# modules. Built on top of the cross-brand visit machinery + funnel
# rollup so the numbers are consistent with the per-feature endpoints.
# ─────────────────────────────────────────────────────────────────────────

_THIRTY_DAYS_SECONDS = 30 * 24 * 3600


@router.get("/{master_id}/dashboard")
async def master_dashboard(
    master_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Master-level health dashboard.

    Combines:
      * brand_count / member_count (from existing master indices)
      * members_active_30d         — distinct users w/ ≥1 visit in 30d
      * total_gmv_30d              — sum of funnel:gmv_cents counters
      * total_commission_paid_to_kix — sum of brand:{bid}:kix_take_cents
      * top/worst brand by 30-day gmv (best-effort tiebreak by visits)
      * cross_brand_score          — % visits that are cross-brand (0-100)
      * network_density            — fraction of users in 2+ brands
      * health_alerts              — currently sourced from
                                     `brand:{bid}:health_alerts` LIST,
                                     each entry a JSON dict. Empty when
                                     the alerts module isn't wired.
    """
    master = await _require_master(r, master_id)

    brand_ids = sorted(await r.smembers(_k_master_brands(master_id)))
    member_ids = await r.smembers(_k_master_members(master_id))

    now = time.time()
    from_ts = now - _THIRTY_DAYS_SECONDS

    _, per_brand_visits = await _gather_master_visits(
        r, master_id, from_ts, None, None
    )

    # Network density: users in 2+ brands / total active users.
    user_brand_count: dict[str, set[str]] = {}
    cross_brand_visits = 0
    total_visits = 0
    last_brand_per_user: dict[str, str] = {}
    visits_chrono: list[tuple[float, str, str]] = []
    for bid, visits in per_brand_visits.items():
        for uid, ts in visits:
            user_brand_count.setdefault(uid, set()).add(bid)
            visits_chrono.append((ts, uid, bid))
            total_visits += 1
    visits_chrono.sort(key=lambda t: t[0])
    for _ts, uid, bid in visits_chrono:
        prev = last_brand_per_user.get(uid)
        if prev is not None and prev != bid:
            cross_brand_visits += 1
        last_brand_per_user[uid] = bid

    active_users = len(user_brand_count)
    multi_brand_users = sum(1 for s in user_brand_count.values() if len(s) >= 2)
    network_density = (multi_brand_users / active_users) if active_users else 0.0
    cross_brand_score = int(
        round((cross_brand_visits / total_visits) * 100)
    ) if total_visits else 0

    # GMV + commission per brand.
    gmv_per_brand: dict[str, int] = {}
    visits_count_per_brand: dict[str, int] = {
        bid: len(per_brand_visits.get(bid, [])) for bid in brand_ids
    }
    total_gmv_30d = 0
    total_commission = 0
    for bid in brand_ids:
        pipe = r.pipeline()
        pipe.get(f"brand:{bid}:funnel:gmv_cents")
        pipe.get(f"brand:{bid}:kix_take_cents")
        gmv_raw, take_raw = await pipe.execute()
        gmv_i = int(gmv_raw or 0)
        take_i = int(take_raw or 0)
        gmv_per_brand[bid] = gmv_i
        total_gmv_30d += gmv_i
        total_commission += take_i

    top_brand = None
    worst_brand = None
    if brand_ids:
        # Rank by (gmv, visits) — visits is a tiebreak for brands with
        # zero published GMV so the dashboard still picks something
        # meaningful in pre-revenue stores.
        ranked = sorted(
            brand_ids,
            key=lambda b: (gmv_per_brand.get(b, 0), visits_count_per_brand.get(b, 0)),
            reverse=True,
        )
        top_brand = ranked[0]
        worst_brand = ranked[-1]

    # Health alerts: each brand may publish a list of JSON-encoded alerts.
    health_alerts: list[dict] = []
    for bid in brand_ids:
        try:
            raw_alerts = await r.lrange(f"brand:{bid}:health_alerts", 0, -1)
        except Exception:  # noqa: BLE001
            raw_alerts = []
        for raw in raw_alerts or []:
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    continue
                data.setdefault("brand_id", bid)
                health_alerts.append(data)
            except (json.JSONDecodeError, TypeError):
                continue

    return {
        "master_id": master_id,
        "company_name": master.get("company_name", ""),
        "brands_count": len(brand_ids),
        "members_count": len(member_ids),
        "members_active_30d": active_users,
        "total_gmv_30d": total_gmv_30d,
        "total_commission_paid_to_kix": total_commission,
        "top_performing_brand": top_brand,
        "worst_performing_brand": worst_brand,
        "cross_brand_score": cross_brand_score,
        "network_density": round(network_density, 4),
        "health_alerts": health_alerts,
        "as_of": _now_iso(),
    }


# ── Public re-exports ────────────────────────────────────────────────────
__all__ = [
    "router",
    "check_permission",
    "resolve_master_tier",
    "VALID_ROLES",
    "DEFAULT_RBAC_MATRIX",
]
