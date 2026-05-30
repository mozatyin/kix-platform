"""Tests for the customer-support + admin tooling.

Covers ``app.routers.support`` (merchant + admin endpoints), the three new
``support_*`` email templates, the FAQ payload + search, the macro
registry, the refund + bulk-announcement flows, and the SLA-monitor
worker in ``app.workers.support_sla_worker``.

The suite reuses ``client`` + ``clean_redis`` from ``tests/conftest.py``.
Admin endpoints accept the dev token ``admin-dev-token`` so no env-var
setup is required (matches the conventions in test_alpha_program.py).
"""

from __future__ import annotations

import json
import os
import time

import pytest

from app.email_templates import EMAIL_TEMPLATES  # noqa: F401 — registry import
from app.email_templates import support as _support_templates  # noqa: F401
from app.routers.support import (
    FAQ,
    TICKET_CATEGORIES,
    TICKET_PRIORITIES,
    _is_sla_breached,
    _SLA_SECONDS,
)
from app.services.email_template_service import email_queue_key
from app.workers.support_sla_worker import run_once as sla_run_once

ADMIN_TOKEN = os.getenv("KIX_ADMIN_TOKEN", "admin-dev-token")
ADMIN_HEADERS = {"X-Admin-Token": ADMIN_TOKEN}


# ── helpers ──────────────────────────────────────────────────────────────


async def _seed_brand(r, brand_id: str = "alpha_tea") -> None:
    """Seed a minimal brand_config so contact-email auto-tagging works."""
    await r.hset(
        f"brand_config:{brand_id}",
        mapping={
            "brand_id": brand_id,
            "brand_name": "Tea House SG",
            "contact_email": "owner@example.sg",
            "locale": "en-SG",
        },
    )


async def _submit_ticket(client, brand_id: str = "alpha_tea", **overrides) -> dict:
    body = {
        "brand_id": brand_id,
        "subject": "Cannot top up wallet",
        "body": "PayNow says timeout. Tried 3 times.",
        "category": "wallet_billing",
        "priority": "p1",
        "current_page": "/wallet",
        "recent_actions": ["click_topup", "submit_paynow"],
    }
    body.update(overrides)
    r = await client.post("/api/v1/support/ticket", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ── 1. submit ticket ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_ticket(client, clean_redis):
    await _seed_brand(clean_redis)
    out = await _submit_ticket(client)
    assert out["ticket_id"].startswith("tkt_")
    assert out["status"] == "open"
    assert "T" in out["submitted_at"]
    assert "T" in out["sla_deadline"]
    # SLA deadline is 4 hrs after now for P1 — sanity check it's in the future.
    # (we don't pin the clock here — just check shape.)


# ── 2. list tickets per brand ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tickets_per_brand(client, clean_redis):
    await _seed_brand(clean_redis)
    a = await _submit_ticket(client, subject="Ticket A")
    b = await _submit_ticket(client, subject="Ticket B")

    r = await client.get("/api/v1/support/tickets/alpha_tea")
    assert r.status_code == 200
    js = r.json()
    assert js["count"] == 2
    ids = [x["ticket_id"] for x in js["items"]]
    # newest first
    assert ids == [b["ticket_id"], a["ticket_id"]]
    # screenshots + recent_actions surface as lists, not JSON strings
    assert isinstance(js["items"][0]["recent_actions"], list)


# ── 3. reply to a ticket ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_to_ticket(client, clean_redis):
    await _seed_brand(clean_redis)
    out = await _submit_ticket(client)
    tid = out["ticket_id"]
    r = await client.post(
        f"/api/v1/support/tickets/{tid}/reply",
        json={"body": "Still failing — any update?"},
    )
    assert r.status_code == 200
    js = r.json()
    assert js["ok"] is True
    assert js["messages"] == 2  # original + reply


# ── 4. admin queue access (gated + populated) ────────────────────────────


@pytest.mark.asyncio
async def test_admin_queue_access(client, clean_redis):
    await _seed_brand(clean_redis)
    await _submit_ticket(client, subject="Q1", priority="p1")
    await _submit_ticket(client, subject="Q2", priority="p3")

    # No token → 403
    r = await client.get("/api/v1/admin/support/queue")
    assert r.status_code == 403

    # With token → list
    r = await client.get("/api/v1/admin/support/queue", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    js = r.json()
    assert js["count"] == 2
    assert js["open_count"] == 2

    # Filter by priority
    r = await client.get(
        "/api/v1/admin/support/queue?priority=p1", headers=ADMIN_HEADERS
    )
    js = r.json()
    assert js["count"] == 1
    assert js["items"][0]["priority"] == "p1"

    # Sort by priority puts p1 above p3
    r = await client.get(
        "/api/v1/admin/support/queue?sort=priority", headers=ADMIN_HEADERS
    )
    js = r.json()
    assert js["items"][0]["priority"] == "p1"


# ── 5. resolve ticket ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_ticket(client, clean_redis):
    await _seed_brand(clean_redis)
    out = await _submit_ticket(client)
    tid = out["ticket_id"]
    r = await client.post(
        f"/api/v1/admin/support/tickets/{tid}/resolve",
        json={"admin_token": ADMIN_TOKEN, "resolution": "PayNow gateway restored at 14:02"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "resolved"

    # The detail view shows resolved + an extra (resolution) staff message.
    r = await client.get(
        f"/api/v1/admin/support/tickets/{tid}", headers=ADMIN_HEADERS
    )
    js = r.json()
    assert js["ticket"]["status"] == "resolved"
    assert js["ticket"]["resolution"].startswith("PayNow")
    assert any("[RESOLVED]" in m["body"] for m in js["messages"])


# ── 6. escalate ticket ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_escalate_ticket(client, clean_redis):
    await _seed_brand(clean_redis)
    out = await _submit_ticket(client)
    tid = out["ticket_id"]
    r = await client.post(
        f"/api/v1/admin/support/tickets/{tid}/escalate",
        json={
            "admin_token": ADMIN_TOKEN,
            "reason": "Stripe webhook failing for 3+ merchants",
            "escalate_to": "founder@letskix.com",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["escalated_to"] == "founder@letskix.com"

    # Internal note auto-appended.
    r = await client.get(
        f"/api/v1/admin/support/tickets/{tid}", headers=ADMIN_HEADERS
    )
    js = r.json()
    assert js["ticket"]["status"] == "escalated"
    assert any("ESCALATED" in n["body"] for n in js["notes"])


# ── 7. refund flow (mock mode) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_refund_flow(client, clean_redis):
    await _seed_brand(clean_redis)
    out = await _submit_ticket(client, category="refund", subject="Need refund")
    tid = out["ticket_id"]

    # Make sure we're in mock-Stripe mode for the test
    os.environ.pop("STRIPE_API_KEY", None)
    r = await client.post(
        f"/api/v1/admin/support/tickets/{tid}/refund",
        json={
            "admin_token": ADMIN_TOKEN,
            "amount_cents": 5_000,
            "currency": "SGD",
            "reason": "Double-charged on top-up",
            "stripe_charge_id": "ch_test_123",
        },
    )
    assert r.status_code == 200
    js = r.json()
    assert js["ok"] is True
    assert js["mode"] == "mock"
    assert js["amount_cents"] == 5_000
    assert js["refund_id"].startswith("re_")

    # Audit log written
    raw = await clean_redis.lrange("audit:support:refund", 0, -1)
    assert len(raw) == 1
    entry = json.loads(raw[0])
    assert entry["event"] == "support_refund"
    assert entry["amount_cents"] == 5_000

    # Ticket updated
    detail = await client.get(
        f"/api/v1/admin/support/tickets/{tid}", headers=ADMIN_HEADERS
    )
    assert detail.json()["ticket"]["refund_processed"] == "true"


# ── 8. bulk announcement ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bulk_announcement(client, clean_redis):
    # Seed two cohort members
    for bid in ("alpha_tea", "alpha_kopi"):
        await clean_redis.hset(f"brand_config:{bid}", mapping={
            "brand_id": bid,
            "brand_name": bid.replace("_", " ").title(),
            "contact_email": f"{bid}@example.sg",
            "locale": "en-SG",
        })
        await clean_redis.sadd("alpha:cohort:2026q1", bid)

    # Preview first
    r = await client.post("/api/v1/admin/support/announce", json={
        "admin_token": ADMIN_TOKEN,
        "subject": "Scheduled maintenance Sat 02:00 SGT",
        "body": "Portal offline 02:00-02:30 SGT.",
        "cohort": "2026q1",
        "preview_only": True,
    })
    assert r.status_code == 200
    js = r.json()
    assert js["preview"] is True
    assert js["recipient_count"] == 2

    # Real send
    r = await client.post("/api/v1/admin/support/announce", json={
        "admin_token": ADMIN_TOKEN,
        "subject": "Scheduled maintenance Sat 02:00 SGT",
        "body": "Portal offline 02:00-02:30 SGT.",
        "cohort": "2026q1",
        "in_app_banner": True,
        "send_email": True,
    })
    assert r.status_code == 200
    js = r.json()
    assert js["recipient_count"] == 2
    assert js["emails_enqueued"] == 2

    # Per-brand banner key written
    banner = await clean_redis.hgetall("brand:alpha_tea:banner")
    assert banner  # non-empty

    # Email queue has the announcement
    q = await clean_redis.lrange(email_queue_key("alpha_tea"), 0, -1)
    assert len(q) == 1
    env = json.loads(q[0])
    assert env["template_id"] == "alpha_announcement"


# ── 9. FAQ + search ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_faq_search(client, clean_redis):
    # Public FAQ payload has all 40 entries
    r = await client.get("/api/v1/support/faq")
    assert r.status_code == 200
    js = r.json()
    assert js["count"] == 40
    assert len(js["items"]) == 40
    assert len(FAQ) == 40

    # Search hits the wallet question
    r = await client.get("/api/v1/support/faq/search?q=wallet")
    assert r.status_code == 200
    js = r.json()
    assert js["count"] >= 1
    assert any("wallet" in i["q"].lower() for i in js["items"])

    # Empty query → empty
    r = await client.get("/api/v1/support/faq/search?q=")
    assert r.json()["count"] == 0


# ── 10. macro insertion in admin reply ───────────────────────────────────


@pytest.mark.asyncio
async def test_macro_insertion_in_reply(client, clean_redis):
    await _seed_brand(clean_redis)
    out = await _submit_ticket(client, category="refund", subject="Refund pls")
    tid = out["ticket_id"]

    # Verify macro list is gated + populated
    r = await client.get("/api/v1/admin/support/macros")
    assert r.status_code == 403  # no header
    r = await client.get("/api/v1/admin/support/macros", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    macros = r.json()["items"]
    assert any(m["id"] == "refund_acknowledged" for m in macros)

    # Reply with a macro id
    r = await client.post(
        f"/api/v1/admin/support/tickets/{tid}/reply",
        json={
            "admin_token": ADMIN_TOKEN,
            "body": "",
            "macro_id": "refund_acknowledged",
            "staff": "jane@letskix.com",
        },
    )
    assert r.status_code == 200

    detail = await client.get(
        f"/api/v1/admin/support/tickets/{tid}", headers=ADMIN_HEADERS
    )
    msgs = detail.json()["messages"]
    staff_msgs = [m for m in msgs if m.get("role") == "staff"]
    assert staff_msgs and "refund" in staff_msgs[-1]["body"].lower()


# ── 11. admin-only endpoints gated ───────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_only_endpoints_gated(client, clean_redis):
    await _seed_brand(clean_redis)
    out = await _submit_ticket(client)
    tid = out["ticket_id"]

    # Each admin endpoint rejects without a token (or with a wrong one).
    endpoints = [
        ("GET", "/api/v1/admin/support/queue", None),
        ("GET", f"/api/v1/admin/support/tickets/{tid}", None),
        ("POST", f"/api/v1/admin/support/tickets/{tid}/assign",
            {"admin_token": "wrong", "assignee": "x"}),
        ("POST", f"/api/v1/admin/support/tickets/{tid}/resolve",
            {"admin_token": "wrong", "resolution": "x"}),
        ("POST", f"/api/v1/admin/support/tickets/{tid}/escalate",
            {"admin_token": "wrong", "reason": "x"}),
        ("POST", f"/api/v1/admin/support/tickets/{tid}/note",
            {"admin_token": "wrong", "note": "x"}),
        ("POST", f"/api/v1/admin/support/tickets/{tid}/refund",
            {"admin_token": "wrong", "amount_cents": 100, "reason": "x"}),
        ("POST", "/api/v1/admin/support/announce",
            {"admin_token": "wrong", "subject": "subj here",
             "body": "body here", "cohort": "2026q1"}),
        ("GET", "/api/v1/admin/support/macros", None),
        ("GET", "/api/v1/admin/support/faq", None),
    ]
    for method, path, payload in endpoints:
        if method == "GET":
            r = await client.get(path)
        else:
            r = await client.post(path, json=payload)
        assert r.status_code == 403, f"{method} {path} → {r.status_code}"


# ── 12. SLA breach detection (unit + worker) ─────────────────────────────


@pytest.mark.asyncio
async def test_sla_breach_detection(client, clean_redis):
    await _seed_brand(clean_redis)
    out = await _submit_ticket(client, priority="p1")
    tid = out["ticket_id"]

    # Manually rewind created_ts to 5 hrs ago — past the 4 hr P1 SLA.
    now = time.time()
    past = now - (5 * 3_600)
    await clean_redis.hset(f"support:ticket:{tid}", mapping={
        "created_ts": str(past),
    })

    # Pure-function check (the canonical detector).
    rec = await clean_redis.hgetall(f"support:ticket:{tid}")
    rec_str = {
        (k.decode() if isinstance(k, (bytes, bytearray)) else str(k)):
        (v.decode() if isinstance(v, (bytes, bytearray)) else str(v))
        for k, v in rec.items()
    }
    assert _is_sla_breached(rec_str) is True

    # The queue endpoint surfaces sla_breach=True and sla_breach_count>=1.
    r = await client.get("/api/v1/admin/support/queue", headers=ADMIN_HEADERS)
    js = r.json()
    assert js["sla_breach_count"] >= 1
    breached = [x for x in js["items"] if x.get("sla_breach")]
    assert any(x["ticket_id"] == tid for x in breached)

    # Worker run picks it up exactly once (idempotent on re-run).
    rep1 = await sla_run_once(clean_redis)
    assert rep1["newly_alerted"] == 1
    assert rep1["breached_total"] >= 1
    rep2 = await sla_run_once(clean_redis)
    assert rep2["newly_alerted"] == 0, "second run must be idempotent"

    # Alert envelope landed on the alerts queue.
    raw = await clean_redis.lrange("support:alerts:queue", 0, -1)
    assert raw
    alert = json.loads(raw[0])
    assert alert["event"] == "support_sla_breach"
    assert alert["ticket_id"] == tid


# ── 13. ticket history preserved ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_ticket_history_preserved(client, clean_redis):
    await _seed_brand(clean_redis)
    out = await _submit_ticket(client)
    tid = out["ticket_id"]

    # Merchant reply
    await client.post(f"/api/v1/support/tickets/{tid}/reply",
                      json={"body": "Update from merchant"})
    # Staff reply
    await client.post(f"/api/v1/admin/support/tickets/{tid}/reply",
                      json={"admin_token": ADMIN_TOKEN, "body": "Looking into it"})
    # Merchant follow-up
    await client.post(f"/api/v1/support/tickets/{tid}/reply",
                      json={"body": "Still broken"})

    detail = await client.get(
        f"/api/v1/admin/support/tickets/{tid}", headers=ADMIN_HEADERS
    )
    msgs = detail.json()["messages"]
    # original + 3 replies = 4
    assert len(msgs) == 4
    # Order preserved (chronological)
    timestamps = [m["ts"] for m in msgs]
    assert timestamps == sorted(timestamps)
    # Roles alternate as expected
    roles = [m["role"] for m in msgs]
    assert roles == ["merchant", "merchant", "staff", "merchant"]


# ── 14. attachment handling ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_attachment_handling(client, clean_redis):
    await _seed_brand(clean_redis)
    screenshots = ["https://cdn.kix.com/s/abc.png", "https://cdn.kix.com/s/def.png"]
    out = await _submit_ticket(client, screenshots=screenshots)
    tid = out["ticket_id"]

    listing = await client.get("/api/v1/support/tickets/alpha_tea")
    item = next(x for x in listing.json()["items"] if x["ticket_id"] == tid)
    assert item["screenshots"] == screenshots

    # Reply with attachments
    await client.post(f"/api/v1/support/tickets/{tid}/reply", json={
        "body": "Adding more screenshots",
        "attachments": ["https://cdn.kix.com/s/ghi.png"],
    })
    detail = await client.get(
        f"/api/v1/admin/support/tickets/{tid}", headers=ADMIN_HEADERS
    )
    msgs = detail.json()["messages"]
    reply = msgs[-1]
    assert reply["attachments"] == ["https://cdn.kix.com/s/ghi.png"]


# ── 15. notification email on reply ──────────────────────────────────────


@pytest.mark.asyncio
async def test_notification_on_reply(client, clean_redis):
    await _seed_brand(clean_redis)
    out = await _submit_ticket(client)
    tid = out["ticket_id"]

    # Submitting the ticket already enqueued a support_ticket_received email.
    q0 = await clean_redis.lrange(email_queue_key("alpha_tea"), 0, -1)
    assert any(
        json.loads(e)["template_id"] == "support_ticket_received" for e in q0
    )

    # Staff reply triggers support_reply email.
    await client.post(f"/api/v1/admin/support/tickets/{tid}/reply", json={
        "admin_token": ADMIN_TOKEN,
        "body": "On it now — should be resolved in 30 min",
    })
    q1 = await clean_redis.lrange(email_queue_key("alpha_tea"), 0, -1)
    templates = [json.loads(e)["template_id"] for e in q1]
    assert "support_reply" in templates

    # Resolution triggers support_resolved email.
    await client.post(f"/api/v1/admin/support/tickets/{tid}/resolve", json={
        "admin_token": ADMIN_TOKEN,
        "resolution": "Gateway restored",
    })
    q2 = await clean_redis.lrange(email_queue_key("alpha_tea"), 0, -1)
    templates = [json.loads(e)["template_id"] for e in q2]
    assert "support_resolved" in templates


# ── bonus: invalid category / priority rejected ──────────────────────────


@pytest.mark.asyncio
async def test_invalid_category_or_priority_rejected(client, clean_redis):
    await _seed_brand(clean_redis)
    r = await client.post("/api/v1/support/ticket", json={
        "brand_id": "alpha_tea",
        "subject": "Hi",
        "body": "test",
        "category": "NOT_REAL",
        "priority": "p2",
    })
    assert r.status_code == 422

    r = await client.post("/api/v1/support/ticket", json={
        "brand_id": "alpha_tea",
        "subject": "Hi",
        "body": "test",
        "category": "other",
        "priority": "p9",
    })
    assert r.status_code == 422


# ── bonus: assign sets status + assignee ─────────────────────────────────


@pytest.mark.asyncio
async def test_assign_ticket(client, clean_redis):
    await _seed_brand(clean_redis)
    out = await _submit_ticket(client)
    tid = out["ticket_id"]
    r = await client.post(f"/api/v1/admin/support/tickets/{tid}/assign", json={
        "admin_token": ADMIN_TOKEN,
        "assignee": "jane@letskix.com",
    })
    assert r.status_code == 200
    detail = await client.get(
        f"/api/v1/admin/support/tickets/{tid}", headers=ADMIN_HEADERS
    )
    js = detail.json()
    assert js["ticket"]["assignee"] == "jane@letskix.com"
    assert js["ticket"]["status"] == "assigned"
