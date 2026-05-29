"""Locale-aware email + push notification template registry.

This package is the **single source of truth** for all server-rendered
user/merchant communications. It deliberately keeps templates as
Python data (not loose .html files) for three reasons:

1. **Discoverability** — `list_templates()` enumerates everything at
   import time; the test suite enforces every template has a body in
   every supported locale.
2. **Refactor safety** — moving a template field is a `mypy` error,
   not a silent 404 at render time.
3. **No filesystem in hot path** — Lambda / container cold starts
   don't need to walk a templates/ tree.

The strategy doc (`a-docs/i18n-trinity-strategy.md` §4.6 Notifications)
explicitly calls for "template per locale", which is what this module
implements. Bodies are Jinja2 fragments — rendering happens in
:mod:`app.services.email_template_service`. We never store rendered
output here; only the template strings.

Public surface
==============

``EmailTemplate``
    Dataclass describing one template (subject / body_text / body_html
    each as ``{locale: jinja2_source}``, plus required vars and
    category metadata).

``EMAIL_TEMPLATES``
    Mapping of ``template_id → EmailTemplate`` for the 12 core
    transactional emails.

``PUSH_TEMPLATES``
    Mapping of ``template_id → PushTemplate`` for the 6 push
    notifications (160-char body limit enforced by the service).

``list_templates()``
    Returns all template IDs (email + push).

``get_template(template_id)``
    Returns the EmailTemplate or PushTemplate, raising ``KeyError`` if
    unknown. Used by the admin preview/send-test endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from app.email_templates.push import PUSH_TEMPLATES, PushTemplate

__all__ = [
    "EmailTemplate",
    "EMAIL_TEMPLATES",
    "PUSH_TEMPLATES",
    "PushTemplate",
    "list_templates",
    "get_template",
    "ALL_TEMPLATES",
    "SUPPORTED_TEMPLATE_LOCALES",
]


# Templates ship with these two locales day-one. Indonesian, Thai, etc.
# arrive via Phase 2 LLM translation (gated by wait_if_paused()).
SUPPORTED_TEMPLATE_LOCALES: Final[tuple[str, ...]] = ("en-SG", "zh-Hans-SG")


@dataclass(frozen=True)
class EmailTemplate:
    """One transactional/marketing/alert email template.

    Subject, plaintext body, and HTML body are *each* a per-locale
    dict. We don't auto-generate plaintext from HTML because the
    plaintext version is sent to mail clients that prefer it (and to
    SMS gateways for fallback) — a stripped HTML is rarely good UX.
    """

    template_id: str
    subject: dict[str, str]
    body_text: dict[str, str]
    body_html: dict[str, str]
    required_vars: list[str] = field(default_factory=list)
    category: str = "transactional"  # transactional | marketing | alert
    locales_supported: list[str] = field(default_factory=lambda: list(SUPPORTED_TEMPLATE_LOCALES))


# ── 12 core templates ──────────────────────────────────────────────────────
# Bodies are Jinja2. ``{{ var }}`` interpolates; ``{% if %}`` conditionals
# work. HTML autoescape is enabled by the renderer — variables inside
# body_html are escaped automatically (XSS-safe by default).

_T_WELCOME_MERCHANT = EmailTemplate(
    template_id="welcome_new_merchant",
    category="transactional",
    required_vars=["brand_name", "portal_url"],
    subject={
        "en-SG": "Welcome to {{ platform_name }}, {{ brand_name }}",
        "zh-Hans-SG": "欢迎加入 {{ platform_name }}，{{ brand_name }}",
    },
    body_text={
        "en-SG": (
            "Hi {{ brand_name }},\n\n"
            "Welcome to {{ platform_name }}. Your merchant portal is ready: "
            "{{ portal_url }}\n\n"
            "Next steps:\n"
            "  1. Fund your wallet\n"
            "  2. Create your first campaign\n"
            "  3. Pay only when new users discover you\n\n"
            "Reach us anytime at {{ support_email }}.\n"
        ),
        "zh-Hans-SG": (
            "你好 {{ brand_name }}，\n\n"
            "欢迎加入 {{ platform_name }}。商户后台已就绪：{{ portal_url }}\n\n"
            "下一步：\n"
            "  1. 充值钱包\n"
            "  2. 创建第一个广告\n"
            "  3. 仅按新用户获取付费\n\n"
            "如有问题请联系 {{ support_email }}。\n"
        ),
    },
    body_html={
        "en-SG": (
            "<p>Hi <strong>{{ brand_name }}</strong>,</p>"
            "<p>Welcome to {{ platform_name }}. "
            "<a href=\"{{ portal_url }}\">Open your portal</a>.</p>"
            "<ol><li>Fund your wallet</li>"
            "<li>Create your first campaign</li>"
            "<li>Pay only for new users</li></ol>"
            "<p>Support: <a href=\"mailto:{{ support_email }}\">{{ support_email }}</a></p>"
        ),
        "zh-Hans-SG": (
            "<p>你好 <strong>{{ brand_name }}</strong>，</p>"
            "<p>欢迎加入 {{ platform_name }}。"
            "<a href=\"{{ portal_url }}\">打开商户后台</a>。</p>"
            "<ol><li>充值钱包</li><li>创建第一个广告</li><li>仅按新用户付费</li></ol>"
            "<p>客服：<a href=\"mailto:{{ support_email }}\">{{ support_email }}</a></p>"
        ),
    },
)


_T_WELCOME_USER = EmailTemplate(
    template_id="welcome_new_user",
    category="transactional",
    required_vars=["user_name"],
    subject={
        "en-SG": "Welcome to {{ platform_name }}, {{ user_name }}!",
        "zh-Hans-SG": "{{ user_name }}，欢迎来到 {{ platform_name }}！",
    },
    body_text={
        "en-SG": (
            "Hi {{ user_name }}!\n\n"
            "Your {{ platform_name }} account is live. "
            "Scan any KiX QR to start collecting rewards.\n\n"
            "Questions? {{ support_email }}\n"
        ),
        "zh-Hans-SG": (
            "你好 {{ user_name }}！\n\n"
            "您的 {{ platform_name }} 账号已开通。"
            "扫描任意 KiX 二维码即可开始收集奖励。\n\n"
            "有问题请联系 {{ support_email }}\n"
        ),
    },
    body_html={
        "en-SG": (
            "<p>Hi <strong>{{ user_name }}</strong>!</p>"
            "<p>Your {{ platform_name }} account is live. "
            "Scan any KiX QR to start collecting rewards.</p>"
        ),
        "zh-Hans-SG": (
            "<p>你好 <strong>{{ user_name }}</strong>！</p>"
            "<p>您的 {{ platform_name }} 账号已开通。"
            "扫描任意 KiX 二维码即可开始收集奖励。</p>"
        ),
    },
)


# wallet_low_balance — references the 84b3318 fix: auto-recharge warning
# is fired *before* hitting the trigger threshold, so this email's role
# is "you have time, top up at your convenience".
_T_WALLET_LOW = EmailTemplate(
    template_id="wallet_low_balance",
    category="alert",
    required_vars=["brand_name", "balance_display", "threshold_display"],
    subject={
        "en-SG": "[{{ platform_name }}] Wallet running low — {{ brand_name }}",
        "zh-Hans-SG": "[{{ platform_name }}] 钱包余额偏低 — {{ brand_name }}",
    },
    body_text={
        "en-SG": (
            "Hi {{ brand_name }},\n\n"
            "Your wallet balance is {{ balance_display }}, below the "
            "auto-recharge warning threshold ({{ threshold_display }}).\n\n"
            "Your campaigns are still running. Top up at your convenience "
            "or wait — auto-recharge will fire automatically if enabled.\n"
        ),
        "zh-Hans-SG": (
            "你好 {{ brand_name }}，\n\n"
            "您的钱包余额为 {{ balance_display }}，"
            "低于自动充值预警线 ({{ threshold_display }})。\n\n"
            "广告仍在运行。可手动充值，或等待自动充值触发（如已启用）。\n"
        ),
    },
    body_html={
        "en-SG": (
            "<p>Hi <strong>{{ brand_name }}</strong>,</p>"
            "<p>Wallet balance: <strong>{{ balance_display }}</strong> "
            "(below {{ threshold_display }}).</p>"
            "<p>Campaigns still running. Top up at your convenience.</p>"
        ),
        "zh-Hans-SG": (
            "<p>你好 <strong>{{ brand_name }}</strong>，</p>"
            "<p>余额：<strong>{{ balance_display }}</strong>"
            "（低于 {{ threshold_display }}）。</p>"
            "<p>广告仍在运行，请适时充值。</p>"
        ),
    },
)


_T_WALLET_CHARGED = EmailTemplate(
    template_id="wallet_charged",
    category="transactional",
    required_vars=["brand_name", "amount_display", "new_balance_display", "reference_id"],
    subject={
        "en-SG": "[{{ platform_name }}] Auto-recharge receipt — {{ amount_display }}",
        "zh-Hans-SG": "[{{ platform_name }}] 自动充值凭证 — {{ amount_display }}",
    },
    body_text={
        "en-SG": (
            "Hi {{ brand_name }},\n\n"
            "Auto-recharge succeeded.\n"
            "  Amount:  {{ amount_display }}\n"
            "  New balance: {{ new_balance_display }}\n"
            "  Reference:   {{ reference_id }}\n"
        ),
        "zh-Hans-SG": (
            "你好 {{ brand_name }}，\n\n"
            "自动充值成功。\n"
            "  金额：{{ amount_display }}\n"
            "  新余额：{{ new_balance_display }}\n"
            "  凭证号：{{ reference_id }}\n"
        ),
    },
    body_html={
        "en-SG": (
            "<p>Auto-recharge succeeded.</p>"
            "<table><tr><td>Amount</td><td>{{ amount_display }}</td></tr>"
            "<tr><td>New balance</td><td>{{ new_balance_display }}</td></tr>"
            "<tr><td>Reference</td><td><code>{{ reference_id }}</code></td></tr></table>"
        ),
        "zh-Hans-SG": (
            "<p>自动充值成功。</p>"
            "<table><tr><td>金额</td><td>{{ amount_display }}</td></tr>"
            "<tr><td>新余额</td><td>{{ new_balance_display }}</td></tr>"
            "<tr><td>凭证号</td><td><code>{{ reference_id }}</code></td></tr></table>"
        ),
    },
)


# Uses bid_floor / low-performance pause flow.
_T_CAMPAIGN_PAUSED = EmailTemplate(
    template_id="campaign_paused_low_performance",
    category="alert",
    required_vars=["brand_name", "campaign_name", "reason"],
    subject={
        "en-SG": "[{{ platform_name }}] Campaign paused — {{ campaign_name }}",
        "zh-Hans-SG": "[{{ platform_name }}] 广告已暂停 — {{ campaign_name }}",
    },
    body_text={
        "en-SG": (
            "Hi {{ brand_name }},\n\n"
            "Campaign '{{ campaign_name }}' has been paused.\n"
            "Reason: {{ reason }}\n\n"
            "Adjust your bid above the floor or refresh your creatives, "
            "then resume the campaign from the portal.\n"
        ),
        "zh-Hans-SG": (
            "你好 {{ brand_name }}，\n\n"
            "广告「{{ campaign_name }}」已暂停。\n"
            "原因：{{ reason }}\n\n"
            "请提高出价或更新素材后，在后台手动恢复。\n"
        ),
    },
    body_html={
        "en-SG": (
            "<p>Campaign <strong>{{ campaign_name }}</strong> paused.</p>"
            "<p>Reason: {{ reason }}</p>"
            "<p>Adjust bid or refresh creatives, then resume from portal.</p>"
        ),
        "zh-Hans-SG": (
            "<p>广告 <strong>{{ campaign_name }}</strong> 已暂停。</p>"
            "<p>原因：{{ reason }}</p>"
            "<p>请提高出价或更新素材后在后台恢复。</p>"
        ),
    },
)


_T_VOUCHER_ISSUED = EmailTemplate(
    template_id="voucher_issued",
    category="transactional",
    required_vars=["user_name", "voucher_title", "voucher_code", "expires_at"],
    subject={
        "en-SG": "You got a voucher — {{ voucher_title }}",
        "zh-Hans-SG": "您获得了一张优惠券 — {{ voucher_title }}",
    },
    body_text={
        "en-SG": (
            "Hi {{ user_name }},\n\n"
            "{{ voucher_title }}\n"
            "Code: {{ voucher_code }}\n"
            "Expires: {{ expires_at }}\n"
        ),
        "zh-Hans-SG": (
            "你好 {{ user_name }}，\n\n"
            "{{ voucher_title }}\n"
            "券码：{{ voucher_code }}\n"
            "有效期至：{{ expires_at }}\n"
        ),
    },
    body_html={
        "en-SG": (
            "<p>Hi <strong>{{ user_name }}</strong>!</p>"
            "<h2>{{ voucher_title }}</h2>"
            "<p>Code: <code>{{ voucher_code }}</code></p>"
            "<p>Expires: {{ expires_at }}</p>"
        ),
        "zh-Hans-SG": (
            "<p>你好 <strong>{{ user_name }}</strong>！</p>"
            "<h2>{{ voucher_title }}</h2>"
            "<p>券码：<code>{{ voucher_code }}</code></p>"
            "<p>有效期至：{{ expires_at }}</p>"
        ),
    },
)


_T_VOUCHER_REDEEMED = EmailTemplate(
    template_id="voucher_redeemed",
    category="transactional",
    required_vars=["brand_name", "voucher_title", "user_label", "redeemed_at"],
    subject={
        "en-SG": "[{{ platform_name }}] Voucher redeemed — {{ voucher_title }}",
        "zh-Hans-SG": "[{{ platform_name }}] 优惠券已核销 — {{ voucher_title }}",
    },
    body_text={
        "en-SG": (
            "Hi {{ brand_name }},\n\n"
            "{{ user_label }} redeemed '{{ voucher_title }}' at {{ redeemed_at }}.\n"
        ),
        "zh-Hans-SG": (
            "你好 {{ brand_name }}，\n\n"
            "用户 {{ user_label }} 于 {{ redeemed_at }} 核销了「{{ voucher_title }}」。\n"
        ),
    },
    body_html={
        "en-SG": (
            "<p>{{ user_label }} redeemed <strong>{{ voucher_title }}</strong> "
            "at {{ redeemed_at }}.</p>"
        ),
        "zh-Hans-SG": (
            "<p>{{ user_label }} 于 {{ redeemed_at }} 核销了 "
            "<strong>{{ voucher_title }}</strong>。</p>"
        ),
    },
)


_T_DISPUTE_OPENED = EmailTemplate(
    template_id="dispute_opened",
    category="transactional",
    required_vars=["brand_name", "dispute_id", "amount_display", "reason"],
    subject={
        "en-SG": "[{{ platform_name }}] Dispute opened — {{ dispute_id }}",
        "zh-Hans-SG": "[{{ platform_name }}] 申诉已开启 — {{ dispute_id }}",
    },
    body_text={
        "en-SG": (
            "Hi {{ brand_name }},\n\n"
            "Dispute {{ dispute_id }} opened.\n"
            "Amount in dispute: {{ amount_display }}\n"
            "Reason: {{ reason }}\n\n"
            "We will review within 5 business days.\n"
        ),
        "zh-Hans-SG": (
            "你好 {{ brand_name }}，\n\n"
            "申诉 {{ dispute_id }} 已开启。\n"
            "争议金额：{{ amount_display }}\n"
            "原因：{{ reason }}\n\n"
            "我们将在 5 个工作日内审核。\n"
        ),
    },
    body_html={
        "en-SG": (
            "<p>Dispute <strong>{{ dispute_id }}</strong> opened.</p>"
            "<p>Amount: {{ amount_display }} — Reason: {{ reason }}</p>"
        ),
        "zh-Hans-SG": (
            "<p>申诉 <strong>{{ dispute_id }}</strong> 已开启。</p>"
            "<p>金额：{{ amount_display }} — 原因：{{ reason }}</p>"
        ),
    },
)


_T_DISPUTE_RESOLVED = EmailTemplate(
    template_id="dispute_resolved",
    category="transactional",
    required_vars=["recipient_name", "dispute_id", "outcome", "amount_display"],
    subject={
        "en-SG": "[{{ platform_name }}] Dispute resolved — {{ dispute_id }}",
        "zh-Hans-SG": "[{{ platform_name }}] 申诉已裁决 — {{ dispute_id }}",
    },
    body_text={
        "en-SG": (
            "Hi {{ recipient_name }},\n\n"
            "Dispute {{ dispute_id }} resolved.\n"
            "Outcome: {{ outcome }}\n"
            "Amount: {{ amount_display }}\n"
        ),
        "zh-Hans-SG": (
            "你好 {{ recipient_name }}，\n\n"
            "申诉 {{ dispute_id }} 已裁决。\n"
            "结果：{{ outcome }}\n"
            "金额：{{ amount_display }}\n"
        ),
    },
    body_html={
        "en-SG": (
            "<p>Dispute <strong>{{ dispute_id }}</strong> resolved.</p>"
            "<p>Outcome: {{ outcome }} — Amount: {{ amount_display }}</p>"
        ),
        "zh-Hans-SG": (
            "<p>申诉 <strong>{{ dispute_id }}</strong> 已裁决。</p>"
            "<p>结果：{{ outcome }} — 金额：{{ amount_display }}</p>"
        ),
    },
)


# ICU-style plural for invoice line count. Jinja2 doesn't natively
# implement ICU plurals, so we encode the rule with {% if count == 1 %}.
_T_MONTHLY_INVOICE = EmailTemplate(
    template_id="monthly_invoice",
    category="transactional",
    required_vars=["brand_name", "period", "total_display", "line_count", "invoice_url"],
    subject={
        "en-SG": "[{{ platform_name }}] Invoice for {{ period }}",
        "zh-Hans-SG": "[{{ platform_name }}] {{ period }} 账单",
    },
    body_text={
        "en-SG": (
            "Hi {{ brand_name }},\n\n"
            "Your invoice for {{ period }} is ready.\n"
            "Total: {{ total_display }}\n"
            "{% if line_count == 1 %}1 line item"
            "{% else %}{{ line_count }} line items{% endif %}\n\n"
            "View: {{ invoice_url }}\n"
        ),
        "zh-Hans-SG": (
            "你好 {{ brand_name }}，\n\n"
            "{{ period }} 账单已生成。\n"
            "合计：{{ total_display }}\n"
            "{{ line_count }} 项明细\n\n"
            "查看：{{ invoice_url }}\n"
        ),
    },
    body_html={
        "en-SG": (
            "<p>Invoice for <strong>{{ period }}</strong>:</p>"
            "<p>Total: {{ total_display }} "
            "({% if line_count == 1 %}1 line item"
            "{% else %}{{ line_count }} line items{% endif %})</p>"
            "<p><a href=\"{{ invoice_url }}\">View invoice</a></p>"
        ),
        "zh-Hans-SG": (
            "<p><strong>{{ period }}</strong> 账单：</p>"
            "<p>合计：{{ total_display }}（{{ line_count }} 项明细）</p>"
            "<p><a href=\"{{ invoice_url }}\">查看账单</a></p>"
        ),
    },
)


_T_RENEWAL_REMINDER = EmailTemplate(
    template_id="subscription_renewal_reminder",
    category="transactional",
    required_vars=["brand_name", "plan_name", "renewal_date", "amount_display"],
    subject={
        "en-SG": "Your {{ plan_name }} subscription renews on {{ renewal_date }}",
        "zh-Hans-SG": "您的 {{ plan_name }} 订阅将于 {{ renewal_date }} 续费",
    },
    body_text={
        "en-SG": (
            "Hi {{ brand_name }},\n\n"
            "Your {{ plan_name }} subscription renews on {{ renewal_date }} "
            "for {{ amount_display }}.\n\n"
            "Change plan or cancel anytime from the portal.\n"
        ),
        "zh-Hans-SG": (
            "你好 {{ brand_name }}，\n\n"
            "您的 {{ plan_name }} 订阅将于 {{ renewal_date }} "
            "续费 {{ amount_display }}。\n\n"
            "可随时在后台变更或取消。\n"
        ),
    },
    body_html={
        "en-SG": (
            "<p>Your <strong>{{ plan_name }}</strong> subscription renews "
            "<strong>{{ renewal_date }}</strong> for {{ amount_display }}.</p>"
        ),
        "zh-Hans-SG": (
            "<p>您的 <strong>{{ plan_name }}</strong> 订阅将于 "
            "<strong>{{ renewal_date }}</strong> 续费 {{ amount_display }}。</p>"
        ),
    },
)


# Wired to network_effect invite issuance (commit 6e46083).
_T_VIRAL_INVITE = EmailTemplate(
    template_id="viral_invite_received",
    category="marketing",
    required_vars=["user_name", "inviter_name", "invite_url"],
    subject={
        "en-SG": "{{ inviter_name }} invited you to {{ platform_name }}",
        "zh-Hans-SG": "{{ inviter_name }} 邀请您加入 {{ platform_name }}",
    },
    body_text={
        "en-SG": (
            "Hi {{ user_name }},\n\n"
            "{{ inviter_name }} thinks you'll like {{ platform_name }}. "
            "Open the invite to claim your reward:\n{{ invite_url }}\n"
        ),
        "zh-Hans-SG": (
            "你好 {{ user_name }}，\n\n"
            "{{ inviter_name }} 邀请您加入 {{ platform_name }}。"
            "打开链接领取奖励：\n{{ invite_url }}\n"
        ),
    },
    body_html={
        "en-SG": (
            "<p>{{ inviter_name }} invited you to {{ platform_name }}.</p>"
            "<p><a href=\"{{ invite_url }}\">Claim your reward</a></p>"
        ),
        "zh-Hans-SG": (
            "<p>{{ inviter_name }} 邀请您加入 {{ platform_name }}。</p>"
            "<p><a href=\"{{ invite_url }}\">领取奖励</a></p>"
        ),
    },
)


EMAIL_TEMPLATES: Final[dict[str, EmailTemplate]] = {
    t.template_id: t
    for t in (
        _T_WELCOME_MERCHANT,
        _T_WELCOME_USER,
        _T_WALLET_LOW,
        _T_WALLET_CHARGED,
        _T_CAMPAIGN_PAUSED,
        _T_VOUCHER_ISSUED,
        _T_VOUCHER_REDEEMED,
        _T_DISPUTE_OPENED,
        _T_DISPUTE_RESOLVED,
        _T_MONTHLY_INVOICE,
        _T_RENEWAL_REMINDER,
        _T_VIRAL_INVITE,
    )
}


ALL_TEMPLATES: Final[dict[str, EmailTemplate | PushTemplate]] = {
    **EMAIL_TEMPLATES,
    **PUSH_TEMPLATES,
}


def list_templates() -> list[str]:
    """Return the sorted list of every known template id (email + push)."""
    return sorted(ALL_TEMPLATES.keys())


def get_template(template_id: str) -> EmailTemplate | PushTemplate:
    """Return the template descriptor or raise ``KeyError``."""
    return ALL_TEMPLATES[template_id]
