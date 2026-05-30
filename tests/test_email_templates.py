"""Tests for the locale-aware email + push template system.

Covers:
  - rendering in both supported locales
  - missing-var / missing-locale handling
  - HTML autoescape (XSS protection)
  - per-template locale coverage
  - admin preview + send-test endpoints
  - integration with wallet low-balance + viral invite flows
  - ICU plural-style branches inside templates
  - currency formatting (via app.i18n.formatting if available)
  - locale fallback chain
  - admin-token gating
"""

from __future__ import annotations

import json
import logging
import os

import pytest

from app.email_templates import (
    EMAIL_TEMPLATES,
    PUSH_TEMPLATES,
    SUPPORTED_TEMPLATE_LOCALES,
)
from app.services.email_template_service import (
    email_queue_key,
    enqueue_email,
    render_email,
)
from app.workers.email_worker import drain_email_queue


ADMIN_TOKEN = os.getenv("KIX_ADMIN_TOKEN", "admin-dev-token")


# ── 1. Render welcome_new_merchant in both locales ────────────────────────


def test_render_welcome_new_merchant_en_sg():
    out = render_email(
        "welcome_new_merchant",
        "en-SG",
        brand_name="Tea House",
        portal_url="https://partner.letskix.com/acme",
    )
    assert "Tea House" in out["subject"]
    assert "Welcome" in out["subject"]
    assert "https://partner.letskix.com/acme" in out["body_text"]
    assert "Tea House" in out["body_html"]


def test_render_welcome_new_merchant_zh_hans_sg():
    out = render_email(
        "welcome_new_merchant",
        "zh-Hans-SG",
        brand_name="老茶馆",
        portal_url="https://portal/x",
    )
    assert "老茶馆" in out["subject"]
    assert "欢迎" in out["subject"]
    assert "老茶馆" in out["body_text"]


# ── 2. Missing var → ValueError ───────────────────────────────────────────


def test_missing_required_var_raises():
    with pytest.raises(ValueError) as exc:
        render_email("welcome_new_merchant", "en-SG", brand_name="X")
        # portal_url missing
    assert "portal_url" in str(exc.value)


# ── 3. Missing locale → fallback to en-SG + WARN ──────────────────────────


def test_missing_locale_falls_back_to_en_sg(caplog):
    caplog.set_level(logging.WARNING, logger="app.services.email_template_service")
    out = render_email(
        "welcome_new_user",
        "ja-JP",  # unsupported
        user_name="Aiko",
    )
    # Body should be the English fallback.
    assert "Welcome" in out["subject"] or "Welcome" in out["body_text"]
    # And a WARN should have been logged.
    assert any("missing_locale" in rec.getMessage() for rec in caplog.records)


# ── 4. HTML autoescape (XSS) ──────────────────────────────────────────────


def test_html_autoescape_prevents_xss():
    out = render_email(
        "welcome_new_user",
        "en-SG",
        user_name="<script>alert(1)</script>",
    )
    # The plaintext body keeps the raw chars (no XSS surface in text).
    assert "<script>" in out["body_text"]
    # But the HTML body must escape them.
    assert "<script>" not in out["body_html"]
    assert "&lt;script&gt;" in out["body_html"]


# ── 5. All 12 + 6 templates have both supported locales ───────────────────


def test_all_templates_have_both_locales():
    expected = set(SUPPORTED_TEMPLATE_LOCALES)
    for tid, t in EMAIL_TEMPLATES.items():
        for field_name in ("subject", "body_text", "body_html"):
            keys = set(getattr(t, field_name).keys())
            assert expected <= keys, (
                f"email template {tid!r} field {field_name} missing locales: "
                f"{expected - keys}"
            )
    # 12 core transactional templates + 4 alpha-programme templates
    # (registered via app/email_templates/alpha.py at import time).
    assert len(EMAIL_TEMPLATES) >= 12
    for tid, p in PUSH_TEMPLATES.items():
        assert expected <= set(p.title.keys()), f"push {tid} title locale gap"
        assert expected <= set(p.body.keys()), f"push {tid} body locale gap"
    assert len(PUSH_TEMPLATES) == 6


# ── 6. Preview endpoint returns valid render ──────────────────────────────


@pytest.mark.asyncio
async def test_preview_endpoint_returns_render(client, clean_redis):
    res = await client.get(
        "/api/v1/admin/email-templates/welcome_new_user/preview",
        params={
            "admin_token": ADMIN_TOKEN,
            "locale": "en-SG",
            "user_name": "Mia",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["template_id"] == "welcome_new_user"
    assert body["locale"] == "en-SG"
    assert "Mia" in body["rendered"]["body_text"]


# ── 7. Send-test endpoint enqueues to Redis ───────────────────────────────


@pytest.mark.asyncio
async def test_send_test_endpoint_enqueues(client, clean_redis):
    res = await client.post(
        "/api/v1/admin/email-templates/welcome_new_user/send-test",
        json={
            "admin_token": ADMIN_TOKEN,
            "brand_id": "brand_test_1",
            "locale": "en-SG",
            "recipient": "user@example.com",
            "template_vars": {"user_name": "Jojo"},
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["enqueued"] is True
    queue_len = await clean_redis.llen(email_queue_key("brand_test_1"))
    assert queue_len == 1


# ── 8. Wallet low triggers correct template (enqueue path) ────────────────


@pytest.mark.asyncio
async def test_wallet_low_email_enqueued_via_service(clean_redis):
    """Direct test of the enqueue helper used by the wallet hook —
    asserts that the rendered envelope has the expected shape."""
    envelope = await enqueue_email(
        clean_redis,
        brand_id="brand_w_low",
        template_id="wallet_low_balance",
        locale="en-SG",
        brand_name="Cafe Mocha",
        balance_display="$12.50",
        threshold_display="$50.00",
    )
    assert "Cafe Mocha" in envelope["subject"]
    assert "$12.50" in envelope["body_text"]
    queue = await clean_redis.lrange(email_queue_key("brand_w_low"), 0, -1)
    assert len(queue) == 1
    decoded = json.loads(queue[0])
    assert decoded["template_id"] == "wallet_low_balance"


# ── 9. Viral invite triggers correct template ─────────────────────────────


@pytest.mark.asyncio
async def test_viral_invite_enqueue_via_service(clean_redis):
    envelope = await enqueue_email(
        clean_redis,
        brand_id="brand_v",
        template_id="viral_invite_received",
        locale="zh-Hans-SG",
        user_name="小明",
        inviter_name="小红",
        invite_url="https://kix/i/abc",
    )
    assert "小红" in envelope["subject"]
    # zh body has 邀请 verb.
    assert "邀请" in envelope["body_text"]


# ── 10. Required vars validated (every template) ──────────────────────────


def test_required_vars_validated_for_every_template():
    """Every email template that declares required_vars rejects empty
    var dicts with a ValueError listing the missing vars."""
    for tid, t in EMAIL_TEMPLATES.items():
        if not t.required_vars:
            continue
        with pytest.raises(ValueError) as exc:
            render_email(tid, "en-SG")
        msg = str(exc.value)
        # At least one required var should be named.
        assert any(v in msg for v in t.required_vars), (
            f"template {tid}: ValueError message {msg!r} doesn't reference "
            f"required vars {t.required_vars}"
        )


# ── 11. ICU plural-style works in templates ───────────────────────────────


def test_invoice_plural_branch_renders():
    """monthly_invoice flips between singular/plural copy based on
    line_count — verifies the Jinja-encoded ICU-style plural works."""
    singular = render_email(
        "monthly_invoice",
        "en-SG",
        brand_name="X",
        period="2026-05",
        total_display="$10",
        line_count=1,
        invoice_url="https://x",
    )
    plural = render_email(
        "monthly_invoice",
        "en-SG",
        brand_name="X",
        period="2026-05",
        total_display="$10",
        line_count=7,
        invoice_url="https://x",
    )
    assert "1 line item" in singular["body_text"]
    assert "7 line items" in plural["body_text"]


# ── 12. Currency formatting in body (uses app.i18n if available) ─────────


def test_currency_formatting_smoke():
    """Caller supplies pre-formatted currency string; the template
    interpolates it verbatim. If app.i18n.format_currency lands,
    callers will pass its output here — this test pins the contract."""
    try:
        from app.i18n.formatting import format_currency
        display = format_currency(1050, "SGD", "en-SG")
    except Exception:  # pragma: no cover — fallback path
        display = "S$10.50"
    out = render_email(
        "wallet_low_balance",
        "en-SG",
        brand_name="X",
        balance_display=display,
        threshold_display=display,
    )
    assert display in out["body_text"]


# ── 13. Locale fallback chain (zh-Hans-SG and unknown both work) ─────────


def test_locale_fallback_chain():
    # zh-Hans-SG is present — should render natively.
    out_zh = render_email(
        "welcome_new_user", "zh-Hans-SG", user_name="阿明"
    )
    assert "阿明" in out_zh["body_text"]
    assert "欢迎" in out_zh["subject"]

    # zh-Hans-CN is *not* registered — falls back to en-SG.
    out_fallback = render_email(
        "welcome_new_user", "zh-Hans-CN", user_name="阿明"
    )
    # Falls back to English subject.
    assert "Welcome" in out_fallback["subject"]


# ── 14. Admin-token gated ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_endpoints_require_token(client, clean_redis):
    # List without token → 403
    res = await client.get("/api/v1/admin/email-templates")
    assert res.status_code == 403

    # Preview without token → 403
    res = await client.get(
        "/api/v1/admin/email-templates/welcome_new_user/preview",
        params={"locale": "en-SG", "user_name": "X"},
    )
    assert res.status_code == 403

    # Send-test with wrong token → 403
    res = await client.post(
        "/api/v1/admin/email-templates/welcome_new_user/send-test",
        json={
            "admin_token": "wrong",
            "locale": "en-SG",
            "template_vars": {"user_name": "X"},
        },
    )
    assert res.status_code == 403

    # With correct token → 200
    res = await client.get(
        "/api/v1/admin/email-templates",
        params={"admin_token": ADMIN_TOKEN},
    )
    assert res.status_code == 200
    body = res.json()
    # 12 core email + 4 alpha email + 6 push = 22 baseline; allow growth.
    assert body["count"] >= 18


# ── Bonus: push body length cap enforced ──────────────────────────────────


def test_push_body_cap_enforced(monkeypatch):
    """If a translator submits an over-long body, render_push raises."""
    from app.email_templates import push as push_mod

    # Patch one template body to >160 chars in en-SG.
    orig = push_mod.PUSH_TEMPLATES["push_voucher_nearby"]
    long_body = "x" * 200
    monkeypatch.setitem(orig.body, "en-SG", long_body)
    with pytest.raises(ValueError) as exc:
        render_email("push_voucher_nearby", "en-SG", brand_name="X", distance="2km")
    assert "exceeds limit" in str(exc.value)


# ── Bonus: email worker drains queue ──────────────────────────────────────


@pytest.mark.asyncio
async def test_email_worker_drains_queue(clean_redis):
    await enqueue_email(
        clean_redis,
        brand_id="brand_drain",
        template_id="welcome_new_user",
        locale="en-SG",
        user_name="Tester",
    )
    receipts = await drain_email_queue(clean_redis, "brand_drain")
    assert len(receipts) == 1
    assert receipts[0]["delivered"] is True
    # Queue is empty.
    assert await clean_redis.llen(email_queue_key("brand_drain")) == 0
