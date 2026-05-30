"""Support email templates — ticket received / staff reply / resolved.

Three templates power the merchant-facing notifications driven by
:mod:`app.routers.support`. Same dual-locale (``en-SG`` / ``zh-Hans-SG``)
convention as the alpha templates. Templates auto-register on import
via :func:`register()`. Import is side-effectful and idempotent.
"""

from __future__ import annotations

from app.email_templates import (
    ALL_TEMPLATES,
    EMAIL_TEMPLATES,
    SUPPORTED_TEMPLATE_LOCALES,
    EmailTemplate,
)

__all__ = ["SUPPORT_TEMPLATES", "register"]


# ── support_ticket_received ──────────────────────────────────────────────

_T_RECEIVED = EmailTemplate(
    template_id="support_ticket_received",
    category="transactional",
    required_vars=["brand_name", "ticket_id", "subject_line", "portal_url"],
    locales_supported=list(SUPPORTED_TEMPLATE_LOCALES),
    subject={
        "en-SG": "We got your ticket — {{ ticket_id }}",
        "zh-Hans-SG": "已收到你的支持请求 — {{ ticket_id }}",
    },
    body_text={
        "en-SG": (
            "Hi {{ brand_name }},\n\n"
            "Thanks for reaching out. Ticket {{ ticket_id }} is in our queue.\n"
            "Subject: {{ subject_line }}\n\n"
            "Alpha cohort merchants get a first staff reply within 4 business hours.\n"
            "Follow the conversation: {{ portal_url }}\n\n"
            "— KiX Support\n"
        ),
        "zh-Hans-SG": (
            "你好 {{ brand_name }}，\n\n"
            "已收到你的请求。工单 {{ ticket_id }} 已进入处理队列。\n"
            "主题：{{ subject_line }}\n\n"
            "Alpha 商户首次回复时间为 4 个工作小时内。\n"
            "查看进度：{{ portal_url }}\n\n"
            "— KiX 客服\n"
        ),
    },
    body_html={
        "en-SG": (
            "<p>Hi <strong>{{ brand_name }}</strong>,</p>"
            "<p>Thanks for reaching out. Ticket <code>{{ ticket_id }}</code> is in our queue.</p>"
            "<p><em>Subject:</em> {{ subject_line }}</p>"
            "<p>Alpha cohort merchants get a first staff reply within 4 business hours.</p>"
            "<p><a href=\"{{ portal_url }}\">Follow the conversation</a></p>"
            "<p>— KiX Support</p>"
        ),
        "zh-Hans-SG": (
            "<p>你好 <strong>{{ brand_name }}</strong>，</p>"
            "<p>已收到你的请求。工单 <code>{{ ticket_id }}</code> 已进入处理队列。</p>"
            "<p><em>主题：</em>{{ subject_line }}</p>"
            "<p>Alpha 商户首次回复时间为 4 个工作小时内。</p>"
            "<p><a href=\"{{ portal_url }}\">查看进度</a></p>"
            "<p>— KiX 客服</p>"
        ),
    },
)


# ── support_reply ────────────────────────────────────────────────────────

_T_REPLY = EmailTemplate(
    template_id="support_reply",
    category="transactional",
    required_vars=["brand_name", "ticket_id", "reply_excerpt", "portal_url"],
    locales_supported=list(SUPPORTED_TEMPLATE_LOCALES),
    subject={
        "en-SG": "Reply on ticket {{ ticket_id }}",
        "zh-Hans-SG": "工单 {{ ticket_id }} 有新回复",
    },
    body_text={
        "en-SG": (
            "Hi {{ brand_name }},\n\n"
            "We've replied on ticket {{ ticket_id }} (\"{{ subject_line }}\").\n\n"
            "{{ reply_excerpt }}\n\n"
            "Read the full reply + respond: {{ portal_url }}\n\n"
            "— KiX Support\n"
        ),
        "zh-Hans-SG": (
            "你好 {{ brand_name }}，\n\n"
            "工单 {{ ticket_id }}（“{{ subject_line }}”）有新回复。\n\n"
            "{{ reply_excerpt }}\n\n"
            "查看完整回复并继续对话：{{ portal_url }}\n\n"
            "— KiX 客服\n"
        ),
    },
    body_html={
        "en-SG": (
            "<p>Hi <strong>{{ brand_name }}</strong>,</p>"
            "<p>We've replied on ticket <code>{{ ticket_id }}</code> "
            "(<em>{{ subject_line }}</em>).</p>"
            "<blockquote>{{ reply_excerpt }}</blockquote>"
            "<p><a href=\"{{ portal_url }}\">Read the full reply + respond</a></p>"
            "<p>— KiX Support</p>"
        ),
        "zh-Hans-SG": (
            "<p>你好 <strong>{{ brand_name }}</strong>，</p>"
            "<p>工单 <code>{{ ticket_id }}</code> 有新回复 "
            "(<em>{{ subject_line }}</em>)。</p>"
            "<blockquote>{{ reply_excerpt }}</blockquote>"
            "<p><a href=\"{{ portal_url }}\">查看完整回复</a></p>"
            "<p>— KiX 客服</p>"
        ),
    },
)


# ── support_resolved ─────────────────────────────────────────────────────

_T_RESOLVED = EmailTemplate(
    template_id="support_resolved",
    category="transactional",
    required_vars=["brand_name", "ticket_id", "resolution"],
    locales_supported=list(SUPPORTED_TEMPLATE_LOCALES),
    subject={
        "en-SG": "Ticket {{ ticket_id }} resolved",
        "zh-Hans-SG": "工单 {{ ticket_id }} 已解决",
    },
    body_text={
        "en-SG": (
            "Hi {{ brand_name }},\n\n"
            "Ticket {{ ticket_id }} is resolved.\n\n"
            "Resolution: {{ resolution }}\n\n"
            "If anything is still off, just reply to this email to re-open the ticket.\n"
            "— KiX Support\n"
        ),
        "zh-Hans-SG": (
            "你好 {{ brand_name }}，\n\n"
            "工单 {{ ticket_id }} 已解决。\n\n"
            "解决方案：{{ resolution }}\n\n"
            "如还有问题，直接回复此邮件即可重新开启工单。\n"
            "— KiX 客服\n"
        ),
    },
    body_html={
        "en-SG": (
            "<p>Hi <strong>{{ brand_name }}</strong>,</p>"
            "<p>Ticket <code>{{ ticket_id }}</code> is resolved.</p>"
            "<p><strong>Resolution:</strong> {{ resolution }}</p>"
            "<p>If anything is still off, reply to this email to re-open.</p>"
            "<p>— KiX Support</p>"
        ),
        "zh-Hans-SG": (
            "<p>你好 <strong>{{ brand_name }}</strong>，</p>"
            "<p>工单 <code>{{ ticket_id }}</code> 已解决。</p>"
            "<p><strong>解决方案：</strong>{{ resolution }}</p>"
            "<p>如仍有问题，直接回复此邮件可重新开启工单。</p>"
            "<p>— KiX 客服</p>"
        ),
    },
)


SUPPORT_TEMPLATES: dict[str, EmailTemplate] = {
    t.template_id: t for t in (_T_RECEIVED, _T_REPLY, _T_RESOLVED)
}


def register() -> None:
    """Idempotently extend the global template registries."""
    for tid, tmpl in SUPPORT_TEMPLATES.items():
        EMAIL_TEMPLATES.setdefault(tid, tmpl)
        ALL_TEMPLATES.setdefault(tid, tmpl)


register()
