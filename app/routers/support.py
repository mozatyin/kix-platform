"""Customer-support + admin tooling for the alpha launch.

Purpose
-------
Give the 5 Singapore F&B alpha merchants a real channel to ask for help
*and* give the founding team the tools to triage / answer / escalate at
the speed alpha demands. The router owns:

  * Merchant-facing ticket submit / list / reply.
  * Admin queue with filtering, assignment, resolution, escalation.
  * Refund processing (mock-Stripe by default; real PSP when wired).
  * Bulk announcements to all alpha merchants (email + in-app banner).
  * FAQ search + macro / canned-response lookup for the admin UI.

It deliberately does **not** own:
  * The portal session / brand-config writes — we only *read* brand_config
    for contact info.
  * Email rendering — we ``enqueue_email`` against three new templates
    registered side-effectfully on first import (``support_ticket_received``,
    ``support_reply``, ``support_resolved``).
  * The actual SLA-breach scan — that lives in the
    ``app/workers/support_sla_worker.py`` hourly cron. This router exposes
    the *primitives* (``_is_sla_breached``) the worker reuses.

Endpoints
---------
Merchant (no admin token):
  ``POST /api/v1/support/ticket``                — submit a new ticket
  ``GET  /api/v1/support/tickets/{brand_id}``    — list own tickets
  ``POST /api/v1/support/tickets/{tid}/reply``   — append a merchant reply

Admin (KIX_ADMIN_TOKEN — query string or X-Admin-Token header):
  ``GET  /api/v1/admin/support/queue``                       — all tickets
  ``POST /api/v1/admin/support/tickets/{tid}/assign``        — assign staff
  ``POST /api/v1/admin/support/tickets/{tid}/resolve``       — close out
  ``POST /api/v1/admin/support/tickets/{tid}/escalate``      — bump to senior
  ``POST /api/v1/admin/support/tickets/{tid}/reply``         — staff reply
  ``POST /api/v1/admin/support/tickets/{tid}/note``          — internal note
  ``POST /api/v1/admin/support/tickets/{tid}/refund``        — process refund
  ``GET  /api/v1/admin/support/macros``                      — canned responses
  ``GET  /api/v1/admin/support/faq``                         — full FAQ JSON
  ``POST /api/v1/admin/support/announce``                    — bulk broadcast

Public (no auth):
  ``GET  /api/v1/support/faq``                   — public FAQ JSON (read-only)
  ``GET  /api/v1/support/faq/search?q=...``      — full-text search FAQ

Storage (Redis)
---------------
``support:ticket:{tid}``         HASH  — ticket envelope
``support:ticket:{tid}:msgs``    LIST  — JSON-encoded message thread
``support:ticket:{tid}:notes``   LIST  — JSON-encoded internal notes
``support:tickets:by_brand:{b}`` LIST  — newest-first ticket ids per brand
``support:tickets:queue``        LIST  — global newest-first ticket index
``support:tickets:open``         SET   — open ticket ids (cheap admin counts)
``support:announce:index``       LIST  — broadcast history (newest first)
``support:announce:{aid}``       HASH  — one broadcast envelope

Auth
----
Admin endpoints honour ``KIX_ADMIN_TOKEN`` via either ``?admin_token=``
query string or ``X-Admin-Token`` header (matches alpha_program + email_admin).
Default for local/dev is ``admin-dev-token`` so tests run without setup.

SLA
---
P1 tickets must receive a *staff* response within 4 hours. The breach
detector ``_is_sla_breached(ticket)`` is the canonical check — both the
admin queue endpoint (which surfaces a ``sla_breach`` flag) and the
hourly worker share the same code path.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.redis_client import get_redis
from app.security import constant_time_eq

# Side-effect import: registers support_ticket_received / support_reply /
# support_resolved into the global template registries. Idempotent.
from app.email_templates import support as _support_templates  # noqa: F401

logger = logging.getLogger(__name__)
# Merchant + public routes — mounted at /api/v1/support
router = APIRouter()
# Admin routes — mounted at /api/v1/admin/support
admin_router = APIRouter()


# ── constants ────────────────────────────────────────────────────────────

ADMIN_TOKEN_DEFAULT = "admin-dev-token"

TICKET_CATEGORIES = {
    "getting_started",
    "campaigns",
    "wallet_billing",
    "reporting",
    "account",
    "pixels_integrations",
    "troubleshooting",
    "refund",
    "other",
}

TICKET_PRIORITIES = {"p0", "p1", "p2", "p3"}  # p0 = outage, p3 = nice-to-have

TICKET_STATUSES = {"open", "assigned", "waiting_merchant", "escalated", "resolved"}

# SLA targets (seconds) keyed by priority. P0 is the outage tier — 1 hr.
# P1 (production blocker) — 4 hrs, the spec value.
_SLA_SECONDS: dict[str, int] = {
    "p0": 3_600,
    "p1": 4 * 3_600,
    "p2": 24 * 3_600,
    "p3": 72 * 3_600,
}


# ── auth helpers ─────────────────────────────────────────────────────────


def _check_admin(token: str | None) -> None:
    if not token:
        raise HTTPException(status_code=403, detail="admin_token_required")
    expected = os.getenv("KIX_ADMIN_TOKEN", ADMIN_TOKEN_DEFAULT)
    if not constant_time_eq(token, expected):
        raise HTTPException(status_code=403, detail="invalid_admin_token")


def _admin_token_from_request(request: Request) -> str | None:
    qs = request.query_params.get("admin_token")
    if qs:
        return qs
    return request.headers.get("x-admin-token")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_ts() -> float:
    return time.time()


def _decode_hash(raw: dict[Any, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in raw.items():
        ks = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
        vs = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
        out[ks] = vs
    return out


def _b(s: Any) -> str:
    return s.decode() if isinstance(s, (bytes, bytearray)) else str(s)


# ── pydantic models ──────────────────────────────────────────────────────


class TicketCreate(BaseModel):
    brand_id: str = Field(..., min_length=2, max_length=64, pattern=r"^[a-z0-9_-]+$")
    subject: str = Field(..., min_length=2, max_length=240)
    body: str = Field(..., min_length=2, max_length=20_000)
    category: str = Field("other", min_length=2, max_length=32)
    priority: str = Field("p2", min_length=2, max_length=4)
    screenshots: list[str] = Field(default_factory=list, max_length=10)
    current_page: str = Field("", max_length=500)
    recent_actions: list[str] = Field(default_factory=list, max_length=50)
    contact_email: str = Field("", max_length=240)


class TicketCreated(BaseModel):
    ticket_id: str
    status: str
    submitted_at: str
    sla_deadline: str


class TicketReply(BaseModel):
    body: str = Field(..., min_length=1, max_length=20_000)
    author: str = Field("merchant", max_length=120)
    attachments: list[str] = Field(default_factory=list, max_length=10)


class AdminReply(BaseModel):
    admin_token: str = Field(..., min_length=1, max_length=512)
    # Body may be empty when a ``macro_id`` is supplied — the macro fills in.
    # Handler enforces "either body or macro must be non-empty".
    body: str = Field("", max_length=20_000)
    staff: str = Field("support", max_length=120)
    attachments: list[str] = Field(default_factory=list, max_length=10)
    macro_id: str = Field("", max_length=64)


class AdminAssign(BaseModel):
    admin_token: str = Field(..., min_length=1, max_length=512)
    assignee: str = Field(..., min_length=1, max_length=120)


class AdminResolve(BaseModel):
    admin_token: str = Field(..., min_length=1, max_length=512)
    resolution: str = Field(..., min_length=1, max_length=4_000)
    staff: str = Field("support", max_length=120)


class AdminEscalate(BaseModel):
    admin_token: str = Field(..., min_length=1, max_length=512)
    reason: str = Field(..., min_length=1, max_length=2_000)
    escalate_to: str = Field("senior", max_length=120)


class AdminNote(BaseModel):
    admin_token: str = Field(..., min_length=1, max_length=512)
    note: str = Field(..., min_length=1, max_length=4_000)
    staff: str = Field("support", max_length=120)


class RefundRequest(BaseModel):
    admin_token: str = Field(..., min_length=1, max_length=512)
    amount_cents: int = Field(..., ge=1, le=1_000_000_00)
    currency: str = Field("SGD", min_length=3, max_length=8)
    reason: str = Field(..., min_length=1, max_length=2_000)
    stripe_charge_id: str = Field("", max_length=240)
    staff: str = Field("support", max_length=120)


class AnnounceRequest(BaseModel):
    admin_token: str = Field(..., min_length=1, max_length=512)
    subject: str = Field(..., min_length=2, max_length=240)
    body: str = Field(..., min_length=2, max_length=20_000)
    cohort: str = Field("2026q1", max_length=32)
    schedule_at: float | None = Field(None, ge=0)  # epoch seconds; None = now
    preview_only: bool = False
    in_app_banner: bool = True
    send_email: bool = True


# ── ticket helpers ───────────────────────────────────────────────────────


def _ticket_key(tid: str) -> str:
    return f"support:ticket:{tid}"


def _msg_key(tid: str) -> str:
    return f"support:ticket:{tid}:msgs"


def _note_key(tid: str) -> str:
    return f"support:ticket:{tid}:notes"


def _sla_deadline_iso(priority: str, created_ts: float) -> str:
    secs = _SLA_SECONDS.get(priority, _SLA_SECONDS["p2"])
    return datetime.fromtimestamp(created_ts + secs, timezone.utc).isoformat(
        timespec="seconds"
    )


def _is_sla_breached(ticket: dict[str, str]) -> bool:
    """Return True iff ticket's *first staff response* SLA has elapsed.

    Considered breached when:
      * status is not 'resolved'
      * the ticket has had **no** staff reply (first_staff_reply_ts empty)
      * (now - created_ts) > SLA window for this priority
    """
    if ticket.get("status") == "resolved":
        return False
    if ticket.get("first_staff_reply_ts"):
        return False
    try:
        created = float(ticket.get("created_ts", "0") or 0)
    except ValueError:
        return False
    if created <= 0:
        return False
    priority = ticket.get("priority", "p2")
    window = _SLA_SECONDS.get(priority, _SLA_SECONDS["p2"])
    return (_now_ts() - created) > window


async def _load_ticket(r: aioredis.Redis, tid: str) -> dict[str, str]:
    rec = _decode_hash(await r.hgetall(_ticket_key(tid)))
    if not rec:
        raise HTTPException(404, "ticket_not_found")
    return rec


# ── merchant endpoints ───────────────────────────────────────────────────


@router.post("/ticket", response_model=TicketCreated, status_code=201)
async def submit_ticket(
    body: TicketCreate,
    r: aioredis.Redis = Depends(get_redis),
) -> TicketCreated:
    """Merchant submits a new support ticket."""
    cat = body.category.lower().strip()
    if cat not in TICKET_CATEGORIES:
        raise HTTPException(422, f"invalid_category: {sorted(TICKET_CATEGORIES)}")
    prio = body.priority.lower().strip()
    if prio not in TICKET_PRIORITIES:
        raise HTTPException(422, f"invalid_priority: {sorted(TICKET_PRIORITIES)}")

    tid = f"tkt_{uuid.uuid4().hex[:12]}"
    now_ts = _now_ts()
    now_iso = _now_iso()

    # Auto-tag from brand_config: contact_email + brand_name.
    bcfg = _decode_hash(await r.hgetall(f"brand_config:{body.brand_id}"))
    contact_email = body.contact_email or bcfg.get("contact_email", "")
    brand_name = bcfg.get("brand_name", body.brand_id)

    record = {
        "ticket_id": tid,
        "brand_id": body.brand_id,
        "brand_name": brand_name,
        "subject": body.subject,
        "category": cat,
        "priority": prio,
        "status": "open",
        "assignee": "",
        "screenshots": json.dumps(body.screenshots),
        "current_page": body.current_page,
        "recent_actions": json.dumps(body.recent_actions),
        "contact_email": contact_email,
        "created_at": now_iso,
        "created_ts": str(now_ts),
        "updated_at": now_iso,
        "updated_ts": str(now_ts),
        "first_staff_reply_ts": "",
        "resolved_at": "",
        "resolution": "",
        "escalated": "false",
        "escalation_reason": "",
        "refund_processed": "false",
        "refund_amount_cents": "0",
    }

    first_msg = {
        "ts": now_ts,
        "iso": now_iso,
        "author": "merchant",
        "role": "merchant",
        "body": body.body,
        "attachments": body.screenshots,
    }

    pipe = r.pipeline()
    pipe.hset(_ticket_key(tid), mapping=record)
    pipe.rpush(_msg_key(tid), json.dumps(first_msg))
    pipe.lpush(f"support:tickets:by_brand:{body.brand_id}", tid)
    pipe.ltrim(f"support:tickets:by_brand:{body.brand_id}", 0, 999)
    pipe.lpush("support:tickets:queue", tid)
    pipe.ltrim("support:tickets:queue", 0, 4_999)
    pipe.sadd("support:tickets:open", tid)
    await pipe.execute()

    # Best-effort acknowledgement email — we never let an email failure block
    # the merchant's submission acknowledgement.
    if contact_email:
        try:
            from app.services.email_template_service import enqueue_email

            await enqueue_email(
                r,
                brand_id=body.brand_id,
                template_id="support_ticket_received",
                locale=bcfg.get("locale", "en-SG"),
                recipient=contact_email,
                brand_name=brand_name,
                ticket_id=tid,
                subject_line=body.subject,
                portal_url=f"https://partner.letskix.com/{body.brand_id}/support/{tid}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("support_ticket_received enqueue failed: %s", exc)

    logger.info(
        "support_ticket_created tid=%s brand=%s cat=%s prio=%s",
        tid, body.brand_id, cat, prio,
    )

    return TicketCreated(
        ticket_id=tid,
        status="open",
        submitted_at=now_iso,
        sla_deadline=_sla_deadline_iso(prio, now_ts),
    )


@router.get("/tickets/{brand_id}")
async def list_brand_tickets(
    brand_id: str,
    limit: int = 50,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Merchant view: their own tickets, newest first."""
    limit = max(1, min(limit, 200))
    raw_ids = await r.lrange(f"support:tickets:by_brand:{brand_id}", 0, limit - 1)
    items: list[dict[str, Any]] = []
    for raw in raw_ids:
        tid = _b(raw)
        rec = _decode_hash(await r.hgetall(_ticket_key(tid)))
        if not rec:
            continue
        # Surface stored JSON back as lists for the UI.
        try:
            rec["screenshots"] = json.loads(rec.get("screenshots", "[]"))
        except json.JSONDecodeError:
            rec["screenshots"] = []
        try:
            rec["recent_actions"] = json.loads(rec.get("recent_actions", "[]"))
        except json.JSONDecodeError:
            rec["recent_actions"] = []
        rec["sla_breach"] = _is_sla_breached(rec)
        items.append(rec)
    return {"brand_id": brand_id, "count": len(items), "items": items}


@router.post("/tickets/{ticket_id}/reply")
async def merchant_reply(
    ticket_id: str,
    body: TicketReply,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Merchant appends a reply to an existing ticket."""
    rec = await _load_ticket(r, ticket_id)
    if rec.get("status") == "resolved":
        # Re-open: a merchant reply on a closed ticket reopens it.
        await r.hset(_ticket_key(ticket_id), mapping={
            "status": "open",
            "updated_at": _now_iso(),
            "updated_ts": str(_now_ts()),
        })
        await r.sadd("support:tickets:open", ticket_id)

    msg = {
        "ts": _now_ts(),
        "iso": _now_iso(),
        "author": body.author or "merchant",
        "role": "merchant",
        "body": body.body,
        "attachments": body.attachments,
    }
    pipe = r.pipeline()
    pipe.rpush(_msg_key(ticket_id), json.dumps(msg))
    pipe.hset(_ticket_key(ticket_id), mapping={
        "updated_at": _now_iso(),
        "updated_ts": str(_now_ts()),
        "status": "open" if rec.get("status") == "waiting_merchant" else rec.get("status", "open"),
    })
    await pipe.execute()
    return {"ok": True, "ticket_id": ticket_id, "messages": await _msg_count(r, ticket_id)}


async def _msg_count(r: aioredis.Redis, tid: str) -> int:
    return int(await r.llen(_msg_key(tid)))


# ── admin endpoints ──────────────────────────────────────────────────────


@admin_router.get("/queue")
async def admin_queue(
    request: Request,
    status_filter: str = "",
    priority: str = "",
    category: str = "",
    assignee: str = "",
    sort: str = "newest",
    limit: int = 200,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Admin: queue of all tickets with filter + sort."""
    _check_admin(_admin_token_from_request(request))
    limit = max(1, min(limit, 1_000))

    raw_ids = await r.lrange("support:tickets:queue", 0, 4_999)
    items: list[dict[str, Any]] = []
    for raw in raw_ids:
        tid = _b(raw)
        rec = _decode_hash(await r.hgetall(_ticket_key(tid)))
        if not rec:
            continue
        if status_filter and rec.get("status") != status_filter:
            continue
        if priority and rec.get("priority") != priority.lower():
            continue
        if category and rec.get("category") != category.lower():
            continue
        if assignee and rec.get("assignee") != assignee:
            continue
        rec["sla_breach"] = _is_sla_breached(rec)
        rec["msg_count"] = await _msg_count(r, tid)
        items.append(rec)
        if len(items) >= limit:
            break

    if sort == "oldest":
        items.sort(key=lambda x: float(x.get("created_ts", "0") or 0))
    elif sort == "priority":
        # P0 → P3 then newest within priority.
        order = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}
        items.sort(key=lambda x: (
            order.get(x.get("priority", "p2"), 9),
            -float(x.get("created_ts", "0") or 0),
        ))
    # else "newest" — already newest-first from LPUSH.

    open_count = int(await r.scard("support:tickets:open"))
    breached = sum(1 for x in items if x.get("sla_breach"))
    return {
        "count": len(items),
        "open_count": open_count,
        "sla_breach_count": breached,
        "items": items,
    }


@admin_router.get("/tickets/{ticket_id}")
async def admin_get_ticket(
    ticket_id: str,
    request: Request,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Admin: full ticket detail including messages + internal notes."""
    _check_admin(_admin_token_from_request(request))
    rec = await _load_ticket(r, ticket_id)
    rec["sla_breach"] = _is_sla_breached(rec)
    msgs_raw = await r.lrange(_msg_key(ticket_id), 0, -1)
    notes_raw = await r.lrange(_note_key(ticket_id), 0, -1)
    msgs = [json.loads(_b(m)) for m in msgs_raw]
    notes = [json.loads(_b(n)) for n in notes_raw]
    return {"ticket": rec, "messages": msgs, "notes": notes}


@admin_router.post("/tickets/{ticket_id}/assign")
async def admin_assign(
    ticket_id: str,
    body: AdminAssign,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _check_admin(body.admin_token)
    await _load_ticket(r, ticket_id)
    await r.hset(_ticket_key(ticket_id), mapping={
        "assignee": body.assignee,
        "status": "assigned",
        "updated_at": _now_iso(),
        "updated_ts": str(_now_ts()),
    })
    return {"ok": True, "ticket_id": ticket_id, "assignee": body.assignee}


@admin_router.post("/tickets/{ticket_id}/reply")
async def admin_reply(
    ticket_id: str,
    body: AdminReply,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Staff reply — also stamps first_staff_reply_ts (SLA stop-the-clock)."""
    _check_admin(body.admin_token)
    rec = await _load_ticket(r, ticket_id)

    text = body.body
    if body.macro_id:
        macro = _MACROS.get(body.macro_id)
        if macro:
            text = macro["body"] + "\n\n" + text if text.strip() else macro["body"]
    if not text.strip():
        raise HTTPException(422, "body_or_macro_required")

    msg = {
        "ts": _now_ts(),
        "iso": _now_iso(),
        "author": body.staff,
        "role": "staff",
        "body": text,
        "attachments": body.attachments,
        "macro_id": body.macro_id or None,
    }
    updates: dict[str, str] = {
        "updated_at": _now_iso(),
        "updated_ts": str(_now_ts()),
        "status": "waiting_merchant",
    }
    if not rec.get("first_staff_reply_ts"):
        updates["first_staff_reply_ts"] = str(_now_ts())

    pipe = r.pipeline()
    pipe.rpush(_msg_key(ticket_id), json.dumps(msg))
    pipe.hset(_ticket_key(ticket_id), mapping=updates)
    await pipe.execute()

    # Best-effort merchant notification.
    contact = rec.get("contact_email", "")
    if contact:
        try:
            from app.services.email_template_service import enqueue_email
            await enqueue_email(
                r,
                brand_id=rec.get("brand_id", ""),
                template_id="support_reply",
                locale="en-SG",
                recipient=contact,
                brand_name=rec.get("brand_name", rec.get("brand_id", "")),
                ticket_id=ticket_id,
                subject_line=rec.get("subject", ""),
                reply_excerpt=text[:200],
                portal_url=f"https://partner.letskix.com/{rec.get('brand_id', '')}/support/{ticket_id}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("support_reply enqueue failed: %s", exc)

    return {"ok": True, "ticket_id": ticket_id}


@admin_router.post("/tickets/{ticket_id}/resolve")
async def admin_resolve(
    ticket_id: str,
    body: AdminResolve,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _check_admin(body.admin_token)
    rec = await _load_ticket(r, ticket_id)
    now = _now_iso()
    now_ts = _now_ts()
    updates = {
        "status": "resolved",
        "resolution": body.resolution,
        "resolved_at": now,
        "updated_at": now,
        "updated_ts": str(now_ts),
    }
    if not rec.get("first_staff_reply_ts"):
        updates["first_staff_reply_ts"] = str(now_ts)

    # Append the resolution as a final staff message so the thread is complete.
    final_msg = {
        "ts": now_ts,
        "iso": now,
        "author": body.staff,
        "role": "staff",
        "body": f"[RESOLVED] {body.resolution}",
        "attachments": [],
    }

    pipe = r.pipeline()
    pipe.rpush(_msg_key(ticket_id), json.dumps(final_msg))
    pipe.hset(_ticket_key(ticket_id), mapping=updates)
    pipe.srem("support:tickets:open", ticket_id)
    await pipe.execute()

    contact = rec.get("contact_email", "")
    if contact:
        try:
            from app.services.email_template_service import enqueue_email
            await enqueue_email(
                r,
                brand_id=rec.get("brand_id", ""),
                template_id="support_resolved",
                locale="en-SG",
                recipient=contact,
                brand_name=rec.get("brand_name", rec.get("brand_id", "")),
                ticket_id=ticket_id,
                subject_line=rec.get("subject", ""),
                resolution=body.resolution[:400],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("support_resolved enqueue failed: %s", exc)

    return {"ok": True, "ticket_id": ticket_id, "status": "resolved"}


@admin_router.post("/tickets/{ticket_id}/escalate")
async def admin_escalate(
    ticket_id: str,
    body: AdminEscalate,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _check_admin(body.admin_token)
    rec = await _load_ticket(r, ticket_id)
    updates = {
        "status": "escalated",
        "escalated": "true",
        "escalation_reason": body.reason,
        "assignee": body.escalate_to,
        "updated_at": _now_iso(),
        "updated_ts": str(_now_ts()),
    }
    await r.hset(_ticket_key(ticket_id), mapping=updates)
    # Note in the internal log so the assignee picks up context.
    note = {
        "ts": _now_ts(),
        "iso": _now_iso(),
        "author": "system",
        "body": f"[ESCALATED to {body.escalate_to}] {body.reason}",
    }
    await r.rpush(_note_key(ticket_id), json.dumps(note))
    logger.info("support_ticket_escalated tid=%s to=%s reason=%s",
                ticket_id, body.escalate_to, body.reason[:120])
    return {"ok": True, "ticket_id": ticket_id, "escalated_to": body.escalate_to}


@admin_router.post("/tickets/{ticket_id}/note")
async def admin_note(
    ticket_id: str,
    body: AdminNote,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Internal note — not visible to the merchant."""
    _check_admin(body.admin_token)
    await _load_ticket(r, ticket_id)
    note = {
        "ts": _now_ts(),
        "iso": _now_iso(),
        "author": body.staff,
        "body": body.note,
    }
    await r.rpush(_note_key(ticket_id), json.dumps(note))
    return {"ok": True, "ticket_id": ticket_id}


@admin_router.post("/tickets/{ticket_id}/refund")
async def admin_refund(
    ticket_id: str,
    body: RefundRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Process a refund tied to a support ticket.

    Talks to Stripe in live mode (when ``STRIPE_API_KEY`` is set) — otherwise
    runs a deterministic mock that always succeeds and writes the same audit
    trail. Either path appends an internal note + updates ticket fields.
    """
    _check_admin(body.admin_token)
    rec = await _load_ticket(r, ticket_id)

    refund_id = f"re_{uuid.uuid4().hex[:14]}"
    mode = "live" if os.getenv("STRIPE_API_KEY") else "mock"
    success = True
    error = ""

    if mode == "live":  # pragma: no cover — exercised only with real key
        try:
            import stripe  # type: ignore[import-not-found]
            stripe.api_key = os.getenv("STRIPE_API_KEY", "")
            kwargs: dict[str, Any] = {"amount": body.amount_cents,
                                      "reason": "requested_by_customer"}
            if body.stripe_charge_id:
                kwargs["charge"] = body.stripe_charge_id
            stripe_resp = stripe.Refund.create(**kwargs)  # type: ignore[attr-defined]
            refund_id = getattr(stripe_resp, "id", refund_id)
        except Exception as exc:  # noqa: BLE001
            success = False
            error = str(exc)
            logger.warning("stripe_refund_failed tid=%s err=%s", ticket_id, exc)

    audit_entry = {
        "event": "support_refund",
        "ticket_id": ticket_id,
        "brand_id": rec.get("brand_id", ""),
        "refund_id": refund_id,
        "amount_cents": body.amount_cents,
        "currency": body.currency.upper(),
        "reason": body.reason,
        "stripe_charge_id": body.stripe_charge_id,
        "staff": body.staff,
        "mode": mode,
        "success": success,
        "error": error,
        "ts": _now_ts(),
        "iso": _now_iso(),
    }
    await r.lpush("audit:support:refund", json.dumps(audit_entry))
    await r.ltrim("audit:support:refund", 0, 9_999)

    note = {
        "ts": _now_ts(),
        "iso": _now_iso(),
        "author": body.staff,
        "body": (
            f"[REFUND {'OK' if success else 'FAIL'}] "
            f"{body.amount_cents/100:.2f} {body.currency.upper()} — "
            f"refund_id={refund_id} mode={mode}. "
            f"Reason: {body.reason}"
        ),
    }
    pipe = r.pipeline()
    pipe.rpush(_note_key(ticket_id), json.dumps(note))
    if success:
        pipe.hset(_ticket_key(ticket_id), mapping={
            "refund_processed": "true",
            "refund_amount_cents": str(body.amount_cents),
            "updated_at": _now_iso(),
            "updated_ts": str(_now_ts()),
        })
    await pipe.execute()

    return {
        "ok": success,
        "refund_id": refund_id,
        "mode": mode,
        "amount_cents": body.amount_cents,
        "currency": body.currency.upper(),
        "error": error or None,
    }


# ── bulk announcements ───────────────────────────────────────────────────


@admin_router.post("/announce")
async def admin_announce(
    body: AnnounceRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Send a bulk announcement to every brand in a cohort.

    * ``preview_only=True`` returns the resolved recipient list + rendered
      subject/body without enqueuing anything.
    * ``schedule_at`` (epoch seconds) is stored on the envelope but we still
      enqueue immediately — the email worker honours its own delay if needed.
      For now schedule_at is purely informational (alpha cohort fits in one
      window — we can wire a delay-queue later).
    """
    _check_admin(body.admin_token)
    raw_members = await r.smembers(f"alpha:cohort:{body.cohort}")
    members = sorted(_b(m) for m in raw_members)

    recipients: list[dict[str, str]] = []
    for bid in members:
        bcfg = _decode_hash(await r.hgetall(f"brand_config:{bid}"))
        contact = bcfg.get("contact_email", "")
        if not contact:
            continue
        recipients.append({
            "brand_id": bid,
            "brand_name": bcfg.get("brand_name", bid),
            "contact_email": contact,
            "locale": bcfg.get("locale", "en-SG"),
        })

    aid = f"ann_{uuid.uuid4().hex[:12]}"
    envelope = {
        "announcement_id": aid,
        "subject": body.subject,
        "body": body.body,
        "cohort": body.cohort,
        "recipient_count": str(len(recipients)),
        "scheduled_at": str(body.schedule_at or _now_ts()),
        "created_at": _now_iso(),
        "in_app_banner": "true" if body.in_app_banner else "false",
        "send_email": "true" if body.send_email else "false",
        "preview_only": "true" if body.preview_only else "false",
    }

    if body.preview_only:
        return {
            "preview": True,
            "announcement_id": aid,
            "subject": body.subject,
            "body": body.body,
            "recipient_count": len(recipients),
            "recipients": recipients,
        }

    pipe = r.pipeline()
    pipe.hset(f"support:announce:{aid}", mapping=envelope)
    pipe.lpush("support:announce:index", aid)
    pipe.ltrim("support:announce:index", 0, 999)
    # In-app banner: write a per-brand banner key the portal reads on load.
    if body.in_app_banner:
        for rcpt in recipients:
            pipe.hset(
                f"brand:{rcpt['brand_id']}:banner",
                mapping={
                    "announcement_id": aid,
                    "subject": body.subject,
                    "body": body.body[:500],
                    "created_at": _now_iso(),
                },
            )
    await pipe.execute()

    sent = 0
    if body.send_email:
        # Reuse welcome_new_merchant as a generic shell — the env contains the
        # full subject + body so the worker can render either way. We pass
        # ``platform_name`` and ``portal_url`` (the merchant's own portal).
        for rcpt in recipients:
            try:
                # Direct RPUSH onto the per-brand outbox so we don't have to
                # ship a separate marketing template just for one alpha blast.
                envelope_msg = {
                    "template_id": "alpha_announcement",
                    "locale": rcpt["locale"],
                    "recipient": rcpt["contact_email"],
                    "subject": body.subject,
                    "body_text": body.body,
                    "body_html": f"<p>{body.body}</p>",
                    "announcement_id": aid,
                }
                await r.rpush(
                    f"email_queue:brand:{rcpt['brand_id']}",
                    json.dumps(envelope_msg),
                )
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("announce enqueue failed bid=%s err=%s",
                               rcpt["brand_id"], exc)

    logger.info("support_announce aid=%s cohort=%s recipients=%d sent=%d",
                aid, body.cohort, len(recipients), sent)

    return {
        "ok": True,
        "announcement_id": aid,
        "recipient_count": len(recipients),
        "emails_enqueued": sent,
        "in_app_banner": body.in_app_banner,
    }


# ── macros / canned responses ────────────────────────────────────────────

_MACROS: dict[str, dict[str, str]] = {
    "wallet_topup_steps": {
        "title": "Wallet top-up steps",
        "body": (
            "Hi! To top up your wallet:\n"
            "1. Open Portal → Wallet → Add Funds.\n"
            "2. Choose amount (min S$50) and PayNow or card.\n"
            "3. Funds appear within 60 seconds for PayNow.\n"
            "Reply here if anything is stuck."
        ),
    },
    "campaign_rejected_review": {
        "title": "Why campaign was rejected",
        "body": (
            "Your campaign hit our automated content review. The most common "
            "causes are: (a) brand logo missing, (b) ad copy >120 chars, "
            "(c) prohibited category words. We'll re-review within 1 hour "
            "of you re-submitting."
        ),
    },
    "refund_acknowledged": {
        "title": "Refund acknowledged",
        "body": (
            "We've received your refund request and will action it within "
            "1 business day. You'll get an email confirmation when Stripe "
            "completes the transfer back to the original card / PayNow."
        ),
    },
    "schedule_call_alpha": {
        "title": "Schedule a call (alpha)",
        "body": (
            "Happy to jump on a 20-min call. Book any slot here: "
            "https://cal.com/kix-alpha — we keep two slots free for "
            "alpha merchants every weekday."
        ),
    },
    "data_export_steps": {
        "title": "How to export your data",
        "body": (
            "Portal → Reports → Export → choose date range → CSV/Parquet. "
            "Files >250 MB are emailed when ready (usually <5 min). "
            "Need a custom column? Reply here."
        ),
    },
}


@admin_router.get("/macros")
async def admin_macros(request: Request) -> dict[str, Any]:
    _check_admin(_admin_token_from_request(request))
    return {
        "count": len(_MACROS),
        "items": [{"id": k, **v} for k, v in _MACROS.items()],
    }


# ── FAQ ──────────────────────────────────────────────────────────────────


FAQ: list[dict[str, str]] = [
    # — Getting started (5)
    {"id": "gs01", "category": "getting_started", "q": "How do I create my first campaign?",
     "a": "Portal → Campaigns → New. Pick a game template, set a daily budget (min S$5), confirm reward, and publish. Your first campaign goes live in under 60 seconds."},
    {"id": "gs02", "category": "getting_started", "q": "What is KiX and how does it work?",
     "a": "KiX is performance-marketing for F&B. Customers play short branded games (3-15s) and earn rewards redeemable at your store. You pay only when a new customer discovers you."},
    {"id": "gs03", "category": "getting_started", "q": "How long until my game is live?",
     "a": "After publish, the storefront game is live in 30–60 seconds. The discovery feed picks it up within 5 minutes after pixel/QR setup."},
    {"id": "gs04", "category": "getting_started", "q": "Which games convert best?",
     "a": "For alpha cohort F&B, the Tap-and-Win and Spin-the-Wheel templates show the highest redeem-rate (32–41% across SG merchants in pilots)."},
    {"id": "gs05", "category": "getting_started", "q": "Where do I find my QR code?",
     "a": "Portal → Assets → QR. Download in PNG, SVG, or printable PDF. Place at till, on the receipt, and the table tent for best scan-rate."},

    # — Campaigns (6)
    {"id": "cm01", "category": "campaigns", "q": "Why was my campaign rejected?",
     "a": "Most rejections are: (a) logo missing, (b) ad copy over 120 chars, (c) prohibited words (e.g. medical claims, age-restricted goods). Fix and re-submit; re-review is under 1 hour."},
    {"id": "cm02", "category": "campaigns", "q": "How do I pause a campaign?",
     "a": "Portal → Campaigns → click the campaign → toggle Active to Paused. Spend stops within 60 seconds. You can resume any time."},
    {"id": "cm03", "category": "campaigns", "q": "How does the bid floor work?",
     "a": "We auction your impressions against other merchants in your geo. Floor in SG is S$0.08 CPM; raising your bid surfaces you in more discovery slots."},
    {"id": "cm04", "category": "campaigns", "q": "Can I run multiple campaigns at once?",
     "a": "Yes — there is no cap during alpha. We recommend at most 3 concurrent campaigns per store so reporting stays signal-rich."},
    {"id": "cm05", "category": "campaigns", "q": "What is the reward I should offer?",
     "a": "10–15% value (e.g. free side, S$3 off main, 1-for-1) gives the highest redemption. <5% values rarely beat the friction of pulling up the reward."},
    {"id": "cm06", "category": "campaigns", "q": "How do I A/B test creatives?",
     "a": "Portal → Campaigns → Variants. Upload up to 4 variants; we'll split traffic and auto-promote the winner after 1,000 plays per arm."},

    # — Wallet & billing (6)
    {"id": "wb01", "category": "wallet_billing", "q": "How do I add money to my wallet?",
     "a": "Portal → Wallet → Add Funds. Choose amount (min S$50). PayNow lands in under 60 seconds; cards take up to 2 minutes."},
    {"id": "wb02", "category": "wallet_billing", "q": "How do I get a refund?",
     "a": "Reply 'refund' to any KiX email or open a support ticket from the Help Centre. Refunds for unused wallet balance process within 1 business day to the original payment method."},
    {"id": "wb03", "category": "wallet_billing", "q": "What payment methods are accepted?",
     "a": "PayNow, Visa, Mastercard, AMEX. Bank transfer for monthly invoicing tier (STARTER+ and up)."},
    {"id": "wb04", "category": "wallet_billing", "q": "Why is my card being declined?",
     "a": "Most often the card issuer's 3DS step timed out. Retry; if it fails twice, switch to PayNow or contact your bank to whitelist KIX*SG."},
    {"id": "wb05", "category": "wallet_billing", "q": "When does my trial end?",
     "a": "Portal → Account → Subscription. Alpha cohort merchants get 90 days STARTER free; we notify at day-60 and day-83 before the trial closes."},
    {"id": "wb06", "category": "wallet_billing", "q": "Can I get a tax invoice?",
     "a": "Yes — every top-up auto-generates a GST-inclusive invoice. Portal → Wallet → Invoices → download. Email accounts@letskix.com if you need a re-issue."},

    # — Reporting (5)
    {"id": "rp01", "category": "reporting", "q": "How do I export my data?",
     "a": "Portal → Reports → Export → pick date range → CSV or Parquet. Files under 250 MB download immediately; larger ones email when ready."},
    {"id": "rp02", "category": "reporting", "q": "My ROAS is low, what should I do?",
     "a": "Check (1) reward value (aim 10–15%), (2) variant performance — kill the worst arm, (3) targeting — narrow to 1km radius for first 2 weeks. Open a ticket if ROAS<0.5 after 2 weeks; we'll review the funnel with you."},
    {"id": "rp03", "category": "reporting", "q": "What does 'qualified play' mean?",
     "a": "A play that completed the full game and saw the reward screen. We bill on qualified plays — partial / bounced plays are free."},
    {"id": "rp04", "category": "reporting", "q": "How do I track in-store redemption?",
     "a": "Two options: (a) staff scans the user's redeem QR, or (b) we read your POS via the Lightspeed/Square integration. Setup in Portal → Pixels & Integrations."},
    {"id": "rp05", "category": "reporting", "q": "Why are my numbers different from GA4?",
     "a": "GA4 counts page-views; KiX counts qualified plays + redemptions. Use GA4 for site traffic, KiX for game performance. Reach out if the delta exceeds 30%."},

    # — Account (5)
    {"id": "ac01", "category": "account", "q": "How do I add a team member?",
     "a": "Portal → Account → Team → Invite. Choose role: Owner, Manager, or Viewer. Invites expire after 7 days."},
    {"id": "ac02", "category": "account", "q": "I want to cancel my subscription.",
     "a": "Portal → Account → Subscription → Cancel. You keep access until the end of the paid period. Any wallet balance refunds to the original payment method."},
    {"id": "ac03", "category": "account", "q": "How do I change my brand name?",
     "a": "Portal → Account → Brand → Edit. Display name updates instantly; URL slug requires support approval (we'll redirect old links)."},
    {"id": "ac04", "category": "account", "q": "I forgot my password.",
     "a": "Login page → 'Forgot password' → enter your email. Reset link expires in 30 minutes."},
    {"id": "ac05", "category": "account", "q": "Can I have multiple stores under one account?",
     "a": "Yes — STARTER includes 3 stores; PRO includes unlimited. Portal → Account → Stores → Add."},

    # — Pixels & integrations (5)
    {"id": "px01", "category": "pixels_integrations", "q": "How do I install the conversion pixel?",
     "a": "Portal → Pixels → copy the snippet, paste before </body> on your thank-you page. Validate with the Pixel Helper button — green tick within 30 seconds."},
    {"id": "px02", "category": "pixels_integrations", "q": "Does KiX integrate with Shopify?",
     "a": "Yes. Install 'KiX for Shopify' from the Shopify App Store; one click connects orders + pixel."},
    {"id": "px03", "category": "pixels_integrations", "q": "Does KiX work with Lightspeed POS?",
     "a": "Yes — Portal → Pixels & Integrations → Lightspeed → Connect. Redemptions sync within 60 seconds of the transaction."},
    {"id": "px04", "category": "pixels_integrations", "q": "Can I send my customer list to KiX?",
     "a": "Yes — Portal → Audiences → Upload. CSV with hashed email or phone. Used for look-alike targeting; never shared with other merchants."},
    {"id": "px05", "category": "pixels_integrations", "q": "Is there a Meta CAPI integration?",
     "a": "Yes — Portal → Pixels & Integrations → Meta. We forward server-side events to your Meta Pixel ID so the iOS-14 attribution gap closes."},

    # — Troubleshooting (8)
    {"id": "tb01", "category": "troubleshooting", "q": "My customers can't see my game.",
     "a": "Check: (1) campaign Active, (2) wallet balance > S$5, (3) geo-targeting includes their location. Most 'invisible game' tickets resolve via wallet top-up."},
    {"id": "tb02", "category": "troubleshooting", "q": "The reward QR is not scanning.",
     "a": "Ask the customer to increase phone brightness. If still failing, staff can type the 6-digit code below the QR in Portal → Redeem → Manual code."},
    {"id": "tb03", "category": "troubleshooting", "q": "Game loads slowly on customer phones.",
     "a": "Usually a CDN cold-start on older Android. We pre-warm the asset edge after a campaign first publishes; load drops below 800ms within 30 minutes."},
    {"id": "tb04", "category": "troubleshooting", "q": "The portal shows a 5xx error.",
     "a": "Hard-refresh (Cmd-Shift-R / Ctrl-F5). If it persists, check status.kix.com. We page on-call within 60 seconds of any P1 incident."},
    {"id": "tb05", "category": "troubleshooting", "q": "I see double-counted redemptions.",
     "a": "Almost always the pixel firing twice (e.g. SPA route + page-view). Use the Pixel Helper to confirm; we de-duplicate within a 5-minute window automatically."},
    {"id": "tb06", "category": "troubleshooting", "q": "Campaign spend exceeds my daily cap.",
     "a": "Daily cap is a soft target — auction races can overshoot by up to 12%. We refund overshoots automatically at midnight SGT."},
    {"id": "tb07", "category": "troubleshooting", "q": "My logo looks pixelated.",
     "a": "Upload at 1024×1024 PNG with transparent background. SVG works too. We rebuild the asset cache within 5 minutes."},
    {"id": "tb08", "category": "troubleshooting", "q": "Push notifications aren't arriving.",
     "a": "Customer must have opted-in inside the KiX app. Some Android OEMs (Xiaomi, Vivo) require an extra 'autostart' permission — we surface a helper card automatically."},
]

assert len(FAQ) == 40, f"FAQ must ship 40 entries (got {len(FAQ)})"


@router.get("/faq")
async def public_faq() -> dict[str, Any]:
    """Public read-only FAQ — used by the merchant help-centre page."""
    return {"count": len(FAQ), "items": FAQ}


@router.get("/faq/search")
async def faq_search(q: str = "") -> dict[str, Any]:
    """Naive substring + token search across questions and answers."""
    q = (q or "").strip().lower()
    if not q:
        return {"count": 0, "items": [], "query": ""}
    tokens = [t for t in re.split(r"\s+", q) if t]
    scored: list[tuple[int, dict[str, str]]] = []
    for entry in FAQ:
        haystack = (entry["q"] + " " + entry["a"] + " " + entry["category"]).lower()
        score = 0
        for tok in tokens:
            if tok in haystack:
                score += haystack.count(tok)
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda x: -x[0])
    items = [e for _, e in scored[:25]]
    return {"count": len(items), "items": items, "query": q}


@admin_router.get("/faq")
async def admin_faq(request: Request) -> dict[str, Any]:
    """Admin-only mirror — same payload but gated, useful for suggestion UI."""
    _check_admin(_admin_token_from_request(request))
    return {"count": len(FAQ), "items": FAQ}
