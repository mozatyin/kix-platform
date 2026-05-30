"""Alpha-program email templates — welcome, day-3, week-1, monthly survey.

These four templates power the automated touchpoints sent to merchants
enrolled in the Singapore F&B alpha cohort. They are *additive* — at
import time we extend the existing ``EMAIL_TEMPLATES`` / ``ALL_TEMPLATES``
dicts in :mod:`app.email_templates` rather than touching the canonical
registry file. This keeps the alpha program self-contained: removing
``alpha_program.py`` (and the side-effect import below) cleanly removes
the alpha templates too.

Locales follow the platform convention: ``en-SG`` and ``zh-Hans-SG``.
The SG bilingual alpha cohort always sees both — the email worker picks
the merchant's preferred locale.

Wire-up
-------
The four templates are auto-registered when this module is imported.
``app/routers/alpha_program.py`` imports this module lazily (inside the
``enqueue_email`` call path) — the side effect is idempotent because we
use ``setdefault`` semantics. Re-importing during tests is safe.
"""

from __future__ import annotations

from app.email_templates import (
    ALL_TEMPLATES,
    EMAIL_TEMPLATES,
    SUPPORTED_TEMPLATE_LOCALES,
    EmailTemplate,
)

__all__ = ["ALPHA_TEMPLATES", "register"]


# ── alpha_welcome ────────────────────────────────────────────────────────


_T_ALPHA_WELCOME = EmailTemplate(
    template_id="alpha_welcome",
    category="transactional",
    required_vars=["brand_name", "contact_name", "trial_days", "portal_url"],
    locales_supported=list(SUPPORTED_TEMPLATE_LOCALES),
    subject={
        "en-SG": "Welcome to the KiX Alpha, {{ brand_name }}",
        "zh-Hans-SG": "{{ brand_name }}，欢迎加入 KiX Alpha 计划",
    },
    body_text={
        "en-SG": (
            "Hi {{ contact_name }},\n\n"
            "{{ brand_name }} is officially in the KiX Singapore F&B alpha. "
            "You have {{ trial_days }} days of STARTER on the house — no card needed.\n\n"
            "What's unlocked:\n"
            "  • Full campaign creation + analytics\n"
            "  • S$500 ad credit (auto-applied after first top-up)\n"
            "  • Direct WhatsApp line to the founding team\n\n"
            "Open the portal: {{ portal_url }}\n\n"
            "We'll check in on day 3 — and we read every reply.\n"
            "— The KiX team\n"
        ),
        "zh-Hans-SG": (
            "你好 {{ contact_name }}，\n\n"
            "{{ brand_name }} 正式加入 KiX 新加坡 F&B Alpha 计划。"
            "你将获得 {{ trial_days }} 天 STARTER 免费试用，无需绑卡。\n\n"
            "本期解锁：\n"
            "  • 完整广告投放与数据看板\n"
            "  • S$500 广告金（首次充值后自动到账）\n"
            "  • 创始团队的 WhatsApp 直连支持\n\n"
            "打开后台：{{ portal_url }}\n\n"
            "我们会在第 3 天联系你 —— 你的每条回复我们都会认真读。\n"
            "— KiX 团队\n"
        ),
    },
    body_html={
        "en-SG": (
            "<p>Hi <strong>{{ contact_name }}</strong>,</p>"
            "<p><strong>{{ brand_name }}</strong> is in the KiX Singapore F&amp;B alpha. "
            "Enjoy <strong>{{ trial_days }} days</strong> of STARTER on the house.</p>"
            "<ul>"
            "<li>Full campaign creation + analytics</li>"
            "<li>S$500 ad credit after first top-up</li>"
            "<li>Direct WhatsApp line to the founding team</li>"
            "</ul>"
            "<p><a href=\"{{ portal_url }}\">Open your portal</a></p>"
        ),
        "zh-Hans-SG": (
            "<p>你好 <strong>{{ contact_name }}</strong>，</p>"
            "<p><strong>{{ brand_name }}</strong> 正式加入 KiX 新加坡 Alpha 计划，"
            "享 <strong>{{ trial_days }} 天</strong> STARTER 免费试用。</p>"
            "<ul>"
            "<li>完整广告投放与数据看板</li>"
            "<li>首充后赠 S$500 广告金</li>"
            "<li>创始团队 WhatsApp 直连支持</li>"
            "</ul>"
            "<p><a href=\"{{ portal_url }}\">打开后台</a></p>"
        ),
    },
)


# ── alpha_day3_checkin ───────────────────────────────────────────────────


_T_ALPHA_DAY3 = EmailTemplate(
    template_id="alpha_day3_checkin",
    category="transactional",
    required_vars=["brand_name", "contact_name", "feedback_url"],
    locales_supported=list(SUPPORTED_TEMPLATE_LOCALES),
    subject={
        "en-SG": "How's the first 3 days, {{ brand_name }}?",
        "zh-Hans-SG": "{{ brand_name }}，前 3 天体验如何？",
    },
    body_text={
        "en-SG": (
            "Hi {{ contact_name }},\n\n"
            "It's been 3 days. Two questions only:\n"
            "  1. What's the single most confusing thing so far?\n"
            "  2. What's missing that would make KiX a yes for you?\n\n"
            "Reply to this email or use the feedback form: {{ feedback_url }}\n\n"
            "— The KiX team\n"
        ),
        "zh-Hans-SG": (
            "你好 {{ contact_name }}，\n\n"
            "已经 3 天了，只问两个问题：\n"
            "  1. 目前最让你困惑的是什么？\n"
            "  2. 缺什么会让你说 yes？\n\n"
            "直接回邮件，或填这个表：{{ feedback_url }}\n\n"
            "— KiX 团队\n"
        ),
    },
    body_html={
        "en-SG": (
            "<p>Hi <strong>{{ contact_name }}</strong>,</p>"
            "<p>It's been 3 days. Two questions only:</p>"
            "<ol>"
            "<li>What's the single most confusing thing so far?</li>"
            "<li>What's missing that would make KiX a yes for you?</li>"
            "</ol>"
            "<p><a href=\"{{ feedback_url }}\">Send 1-line feedback</a></p>"
        ),
        "zh-Hans-SG": (
            "<p>你好 <strong>{{ contact_name }}</strong>，</p>"
            "<p>已经 3 天了，只问两个问题：</p>"
            "<ol>"
            "<li>目前最让你困惑的是什么？</li>"
            "<li>缺什么会让你说 yes？</li>"
            "</ol>"
            "<p><a href=\"{{ feedback_url }}\">填写反馈</a></p>"
        ),
    },
)


# ── alpha_week1_summary ──────────────────────────────────────────────────


_T_ALPHA_WEEK1 = EmailTemplate(
    template_id="alpha_week1_summary",
    category="transactional",
    required_vars=[
        "brand_name",
        "contact_name",
        "campaigns_created",
        "spend_total_sgd",
        "portal_url",
    ],
    locales_supported=list(SUPPORTED_TEMPLATE_LOCALES),
    subject={
        "en-SG": "Your first week on KiX, {{ brand_name }}",
        "zh-Hans-SG": "{{ brand_name }}，KiX 第一周战报",
    },
    body_text={
        "en-SG": (
            "Hi {{ contact_name }},\n\n"
            "Week 1 numbers for {{ brand_name }}:\n"
            "  • Campaigns created: {{ campaigns_created }}\n"
            "  • Ad spend so far: S${{ spend_total_sgd }}\n\n"
            "Full dashboard: {{ portal_url }}\n\n"
            "What surprised you this week? Hit reply.\n"
            "— The KiX team\n"
        ),
        "zh-Hans-SG": (
            "你好 {{ contact_name }}，\n\n"
            "{{ brand_name }} 第一周数据：\n"
            "  • 新建广告：{{ campaigns_created }} 个\n"
            "  • 累计投放：S${{ spend_total_sgd }}\n\n"
            "完整看板：{{ portal_url }}\n\n"
            "这一周最意外的是什么？直接回邮件即可。\n"
            "— KiX 团队\n"
        ),
    },
    body_html={
        "en-SG": (
            "<p>Hi <strong>{{ contact_name }}</strong>,</p>"
            "<p>Week 1 numbers for <strong>{{ brand_name }}</strong>:</p>"
            "<ul>"
            "<li>Campaigns created: <strong>{{ campaigns_created }}</strong></li>"
            "<li>Ad spend: <strong>S${{ spend_total_sgd }}</strong></li>"
            "</ul>"
            "<p><a href=\"{{ portal_url }}\">Open dashboard</a></p>"
        ),
        "zh-Hans-SG": (
            "<p>你好 <strong>{{ contact_name }}</strong>，</p>"
            "<p><strong>{{ brand_name }}</strong> 第一周数据：</p>"
            "<ul>"
            "<li>新建广告：<strong>{{ campaigns_created }}</strong> 个</li>"
            "<li>累计投放：<strong>S${{ spend_total_sgd }}</strong></li>"
            "</ul>"
            "<p><a href=\"{{ portal_url }}\">打开看板</a></p>"
        ),
    },
)


# ── alpha_monthly_survey (NPS) ───────────────────────────────────────────


_T_ALPHA_MONTHLY = EmailTemplate(
    template_id="alpha_monthly_survey",
    category="marketing",
    required_vars=["brand_name", "contact_name", "survey_url"],
    locales_supported=list(SUPPORTED_TEMPLATE_LOCALES),
    subject={
        "en-SG": "30-second alpha survey — {{ brand_name }}",
        "zh-Hans-SG": "{{ brand_name }}，30 秒 Alpha 问卷",
    },
    body_text={
        "en-SG": (
            "Hi {{ contact_name }},\n\n"
            "Monthly check-in for the alpha cohort. One question:\n"
            "How likely are you to recommend KiX to another F&B owner? (0–10)\n\n"
            "{{ survey_url }}\n\n"
            "Your input shapes next month's roadmap.\n"
            "— The KiX team\n"
        ),
        "zh-Hans-SG": (
            "你好 {{ contact_name }}，\n\n"
            "Alpha 月度问卷，只有一题：\n"
            "你向其他 F&B 老板推荐 KiX 的可能性？(0–10)\n\n"
            "{{ survey_url }}\n\n"
            "你的反馈会进入下个月的路线图。\n"
            "— KiX 团队\n"
        ),
    },
    body_html={
        "en-SG": (
            "<p>Hi <strong>{{ contact_name }}</strong>,</p>"
            "<p>One question: how likely are you to recommend KiX to another "
            "F&amp;B owner? (0–10)</p>"
            "<p><a href=\"{{ survey_url }}\">Take the 30-second survey</a></p>"
        ),
        "zh-Hans-SG": (
            "<p>你好 <strong>{{ contact_name }}</strong>，</p>"
            "<p>一题就好：你向其他 F&amp;B 老板推荐 KiX 的可能性？(0–10)</p>"
            "<p><a href=\"{{ survey_url }}\">30 秒完成</a></p>"
        ),
    },
)


ALPHA_TEMPLATES: dict[str, EmailTemplate] = {
    t.template_id: t
    for t in (
        _T_ALPHA_WELCOME,
        _T_ALPHA_DAY3,
        _T_ALPHA_WEEK1,
        _T_ALPHA_MONTHLY,
    )
}


def register() -> None:
    """Idempotently extend the global template registries.

    Safe to call multiple times — uses ``setdefault`` so re-imports during
    tests / `importlib.reload` don't clobber an existing in-memory entry.
    """
    for tid, tmpl in ALPHA_TEMPLATES.items():
        EMAIL_TEMPLATES.setdefault(tid, tmpl)
        ALL_TEMPLATES.setdefault(tid, tmpl)


# Auto-register on import. Importing this module is therefore the wire-up:
# `from app.email_templates import alpha  # noqa: F401`
register()
