"""Push notification template registry.

Push templates share the same locale-dict shape as emails, but are
single-line (no separate text/HTML bodies) and capped at 160 chars
post-render to fit FCM/APNS/WeChat payload limits.

The 160-char cap is enforced at *render time* by the email template
service — templates here can technically include longer bodies, but
the service will raise ``ValueError`` if the rendered output exceeds
the limit. This catches "well-meaning translation that doubled the
character count" before it hits the push pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

__all__ = ["PushTemplate", "PUSH_TEMPLATES", "PUSH_BODY_CHAR_LIMIT"]


PUSH_BODY_CHAR_LIMIT: Final[int] = 160


@dataclass(frozen=True)
class PushTemplate:
    template_id: str
    title: dict[str, str]
    body: dict[str, str]
    required_vars: list[str] = field(default_factory=list)
    category: str = "push"
    locales_supported: list[str] = field(
        default_factory=lambda: ["en-SG", "zh-Hans-SG"]
    )
    # Marker so renderer code can distinguish push vs email payloads.
    is_push: bool = True


_P_VOUCHER_NEARBY = PushTemplate(
    template_id="push_voucher_nearby",
    required_vars=["brand_name", "distance"],
    title={
        "en-SG": "{{ brand_name }} voucher near you",
        "zh-Hans-SG": "{{ brand_name }} 优惠就在附近",
    },
    body={
        "en-SG": "{{ distance }} away — tap to claim.",
        "zh-Hans-SG": "距离 {{ distance }} — 点击领取。",
    },
)


_P_FRIEND_PLAYED = PushTemplate(
    template_id="push_friend_played",
    required_vars=["friend_name", "game_name"],
    title={
        "en-SG": "{{ friend_name }} just played",
        "zh-Hans-SG": "{{ friend_name }} 刚玩了",
    },
    body={
        "en-SG": "{{ friend_name }} scored on {{ game_name }} — beat them?",
        "zh-Hans-SG": "{{ friend_name }} 在 {{ game_name }} 创下新纪录，挑战吗？",
    },
)


_P_STREAK_BREAK = PushTemplate(
    template_id="push_streak_about_to_break",
    required_vars=["streak_days"],
    title={
        "en-SG": "Your {{ streak_days }}-day streak ends soon",
        "zh-Hans-SG": "您的 {{ streak_days }} 天连胜即将中断",
    },
    body={
        "en-SG": "Play once today to keep it going.",
        "zh-Hans-SG": "今日游玩一局即可延续。",
    },
)


_P_NEW_BRAND_AREA = PushTemplate(
    template_id="push_new_brand_in_area",
    required_vars=["brand_name"],
    title={
        "en-SG": "New: {{ brand_name }}",
        "zh-Hans-SG": "新店：{{ brand_name }}",
    },
    body={
        "en-SG": "{{ brand_name }} just joined KiX near you.",
        "zh-Hans-SG": "{{ brand_name }} 刚加入您附近的 KiX。",
    },
)


_P_CAMPAIGN_MATCH = PushTemplate(
    template_id="push_campaign_match",
    required_vars=["campaign_title"],
    title={
        "en-SG": "Matched for you",
        "zh-Hans-SG": "为您匹配",
    },
    body={
        "en-SG": "{{ campaign_title }} — open to play.",
        "zh-Hans-SG": "{{ campaign_title }} — 立即开玩。",
    },
)


_P_INVITE_REDEEMED = PushTemplate(
    template_id="push_invite_redeemed",
    required_vars=["friend_name"],
    title={
        "en-SG": "{{ friend_name }} joined!",
        "zh-Hans-SG": "{{ friend_name }} 加入了！",
    },
    body={
        "en-SG": "Your reward is in your wallet.",
        "zh-Hans-SG": "奖励已发放到您的钱包。",
    },
)


PUSH_TEMPLATES: Final[dict[str, PushTemplate]] = {
    t.template_id: t
    for t in (
        _P_VOUCHER_NEARBY,
        _P_FRIEND_PLAYED,
        _P_STREAK_BREAK,
        _P_NEW_BRAND_AREA,
        _P_CAMPAIGN_MATCH,
        _P_INVITE_REDEEMED,
    )
}
