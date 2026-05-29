"""LLM prompt templates for i18n string classification.

The classifier asks an LLM whether a candidate Python/HTML/JS string is
user-facing UI text vs developer/log/internal noise, and proposes a
snake.case translation key derived from semantic content.

All calls are quota-guarded via ``scripts.llm_quota_monitor.wait_if_paused``.

The prompt is deliberately small (one example, JSON-only output) so it
fits in Haiku's cheap tier — extraction is high-volume (~3,000 strings)
and must stay under $5 total.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger("i18n_prompts")

SYSTEM_PROMPT = """You are a localization engineer reviewing source-code strings.

For each candidate string, classify:
  - is_user_facing: "yes" (shown to end-user/merchant in UI/email/notification),
                    "no"  (internal log/debug/SQL/regex),
                    "comment" (developer comment text).
  - category: one of [error, notification, button, label, description,
              comment, log, other].
  - proposed_key: snake.case dotted path derived from semantic meaning
                  (e.g. "wallet.insufficient_balance", "tutorial.next_button").
                  Use the file's subsystem (router/module) as the first
                  dot-segment when known.
  - comment: optional short reasoning (<=12 words).

Output STRICT JSON only, no prose, no markdown fence. Schema:
{"is_user_facing":"yes|no|comment","category":"...","proposed_key":"...","comment":"..."}
"""

FEW_SHOT = [
    {
        "context": "app/routers/wallet.py — error raised when balance < amount",
        "string": "余额不足，请充值",
        "json": {
            "is_user_facing": "yes",
            "category": "error",
            "proposed_key": "wallet.insufficient_balance",
            "comment": "User-visible payment error",
        },
    },
    {
        "context": "app/routers/auction.py — log line",
        "string": "auction starvation detected for slot=%s",
        "json": {
            "is_user_facing": "no",
            "category": "log",
            "proposed_key": "",
            "comment": "Internal log, not UI",
        },
    },
    {
        "context": "landing/portal.html — button text",
        "string": "登录",
        "json": {
            "is_user_facing": "yes",
            "category": "button",
            "proposed_key": "auth.login_button",
            "comment": "Primary CTA",
        },
    },
    {
        "context": "app/routers/tutorials.py — name_cn dict value",
        "string": "成长体系",
        "json": {
            "is_user_facing": "yes",
            "category": "label",
            "proposed_key": "tutorial.progression_label",
            "comment": "Curriculum chapter title",
        },
    },
    {
        "context": "app/routers/compliance.py — banned phrase list",
        "string": "六合彩",
        "json": {
            "is_user_facing": "no",
            "category": "other",
            "proposed_key": "",
            "comment": "Regulatory data, not UI",
        },
    },
    {
        "context": "app/services/push_engine.py — push notification body",
        "string": "你的订单已发货",
        "json": {
            "is_user_facing": "yes",
            "category": "notification",
            "proposed_key": "order.shipped_push_body",
            "comment": "Push body",
        },
    },
    {
        "context": "scripts/sim_laowang.py — print debug",
        "string": "round complete, score=%d",
        "json": {
            "is_user_facing": "no",
            "category": "log",
            "proposed_key": "",
            "comment": "Sim debug print",
        },
    },
]


def _build_user_prompt(context: str, string: str) -> str:
    examples = []
    for ex in FEW_SHOT:
        examples.append(
            f'CONTEXT: {ex["context"]}\nSTRING: {ex["string"]!r}\n'
            f"OUTPUT: {json.dumps(ex['json'], ensure_ascii=False)}"
        )
    examples_block = "\n\n".join(examples)
    return (
        f"{examples_block}\n\n"
        f"CONTEXT: {context}\nSTRING: {string!r}\nOUTPUT:"
    )


@dataclass
class Classification:
    is_user_facing: str  # yes|no|comment
    category: str
    proposed_key: str
    comment: str = ""

    @property
    def should_extract(self) -> bool:
        return self.is_user_facing == "yes"


def heuristic_classify(string: str, context: str = "") -> Classification:
    """Cheap heuristic classifier — no LLM. Reasonable defaults.

    Used as the fallback when ``--llm`` is not passed, or when LLM
    quota is paused.
    """
    s = string.strip()
    ctx = context.lower()

    # Definitely-not-UI signals
    if any(p in ctx for p in ("log", "logger", "print", "sim_", "noqa: i18n", "i18n-ignore")):
        return Classification("no", "log", "", "ctx hints internal")
    if re.match(r"^[A-Z_][A-Z0-9_]*$", s):  # SCREAMING_CONST
        return Classification("no", "other", "", "constant name")
    if re.match(r"^[\w./-]+$", s) and " " not in s and not re.search(r"[一-鿿]", s):
        # Identifier-shaped, no CJK, no spaces → likely internal
        return Classification("no", "other", "", "identifier-like")
    if re.match(r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE)\b", s, re.I):
        return Classification("no", "other", "", "SQL")
    if re.match(r"^\s*\{.*\}\s*$", s) and ":" in s:
        # JSON-ish schema
        if not re.search(r"[一-鿿]", s):
            return Classification("no", "other", "", "JSON schema literal")

    has_cjk = bool(re.search(r"[一-鿿　-〿]", s))
    has_sentence_punct = bool(re.search(r"[。！？!?.,，:：;；]", s))
    word_count = len(re.findall(r"\w+", s))

    if has_cjk:
        cat = "label"
        if any(k in s for k in ("？", "?")):
            cat = "description"
        if len(s) <= 8 and not has_sentence_punct:
            cat = "button"
        if any(k in s for k in ("失败", "错误", "不足", "无效")):
            cat = "error"
        if any(k in s for k in ("已", "成功", "提醒", "通知")):
            cat = "notification"
        key = _propose_key(context, s)
        return Classification("yes", cat, key, "CJK literal")

    if word_count >= 2 and has_sentence_punct and s[0:1].isupper():
        # English natural-language sentence
        return Classification(
            "yes", "description", _propose_key(context, s), "EN sentence"
        )
    if word_count >= 2 and s[0:1].isupper():
        return Classification(
            "yes", "label", _propose_key(context, s), "EN multi-word"
        )

    return Classification("no", "other", "", "default skip")


_TRANSLIT_TABLE = {
    # Minimal CJK transliteration crutch — heuristic only, LLM is
    # the real key proposer. Just so heuristic-mode keys are not empty.
    "成长": "progression",
    "代币": "currency",
    "道具": "item",
    "成就": "achievement",
    "任务": "quest",
    "等级": "tier",
    "活动": "event",
    "登录": "login",
    "登出": "logout",
    "注册": "signup",
    "余额": "balance",
    "不足": "insufficient",
    "失败": "failed",
    "错误": "error",
    "成功": "success",
    "提醒": "reminder",
    "通知": "notification",
    "订单": "order",
    "发货": "shipped",
    "支付": "payment",
    "钱包": "wallet",
    "积分": "points",
    "兑换": "redeem",
    "优惠券": "voucher",
    "抽奖": "lottery",
    "战令": "battle_pass",
    "联赛": "league",
    "排行榜": "leaderboard",
    "商品": "product",
    "商户": "merchant",
    "品牌": "brand",
}


def _propose_key(context: str, s: str) -> str:
    """Best-effort snake.case key from context + string."""
    # subsystem from context: app/routers/<x>.py → x
    subsys = "ui"
    m = re.search(r"([\w_]+)\.(?:py|html|js)\b", context)
    if m:
        subsys = m.group(1).lower()
    # transliterate any known CJK words
    body_parts = []
    for token in re.findall(r"[一-鿿]+", s):
        for cn, en in _TRANSLIT_TABLE.items():
            if cn in token:
                body_parts.append(en)
                break
    if not body_parts:
        # ascii fallback
        ascii_body = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()
        if ascii_body:
            body_parts.append(ascii_body[:30])
    if not body_parts:
        # last-resort hash-ish
        import hashlib

        body_parts.append("s_" + hashlib.md5(s.encode()).hexdigest()[:6])
    body = "_".join(body_parts)[:40]
    return f"{subsys}.{body}"


async def llm_classify(
    string: str,
    context: str = "",
    *,
    model: str = "claude-haiku-4-5-20251001",
    timeout: float = 15.0,
) -> Classification:
    """LLM classifier. Quota-guarded. Falls back to heuristic on failure."""
    # Lazy import to avoid pulling Redis at module load
    try:
        from scripts.llm_quota_monitor import wait_if_paused

        await wait_if_paused(max_wait_seconds=3600)
    except Exception as e:  # pragma: no cover — Redis optional in tests
        logger.debug("Quota guard unavailable: %s", e)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY missing — falling back to heuristic")
        return heuristic_classify(string, context)

    user_prompt = _build_user_prompt(context, string)
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                json={
                    "model": model,
                    "max_tokens": 200,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": user_prompt}],
                },
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
        if r.status_code != 200:
            logger.warning("LLM HTTP %s — heuristic fallback", r.status_code)
            return heuristic_classify(string, context)
        body = r.json()
        text = "".join(
            blk.get("text", "") for blk in body.get("content", []) if blk.get("type") == "text"
        ).strip()
        # Strip optional markdown fence
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
        parsed = json.loads(text)
        return Classification(
            is_user_facing=str(parsed.get("is_user_facing", "no")).lower(),
            category=str(parsed.get("category", "other")),
            proposed_key=str(parsed.get("proposed_key", "")),
            comment=str(parsed.get("comment", "")),
        )
    except Exception as e:
        logger.warning("LLM classify failed (%s) — heuristic fallback", e)
        return heuristic_classify(string, context)


def classify_sync(string: str, context: str = "", use_llm: bool = False) -> Classification:
    """Sync wrapper for CLI scripts."""
    if not use_llm:
        return heuristic_classify(string, context)
    try:
        return asyncio.run(llm_classify(string, context))
    except RuntimeError:
        # event loop already running — fall back
        return heuristic_classify(string, context)


__all__ = [
    "Classification",
    "heuristic_classify",
    "llm_classify",
    "classify_sync",
    "SYSTEM_PROMPT",
    "FEW_SHOT",
]
