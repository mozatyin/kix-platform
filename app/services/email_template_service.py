"""Email + push template rendering service.

Wraps Jinja2 + the in-repo template registry. Responsibilities:

1. **Variable validation** — every ``required_vars`` declared by the
   template must be present at render-call time. Missing vars raise
   ``ValueError`` *before* we hand a half-baked subject line to the
   mail server.
2. **Locale fallback** — if the requested locale is missing for a
   given field (subject / body), we silently fall back to ``en-SG``
   and log a WARN. We never crash the calling pipeline because a
   marketing template hasn't been translated yet.
3. **HTML autoescape** — HTML bodies are rendered in an autoescape
   Jinja2 environment. Plaintext bodies do not autoescape (no XSS
   surface in text).
4. **Global injection** — ``platform_name``, ``support_email`` and
   ``current_year`` are auto-injected so individual templates don't
   need to know them.
5. **Push char-cap enforcement** — push body renders that exceed
   ``PUSH_BODY_CHAR_LIMIT`` raise ``ValueError``. (Push title is
   always allowed to be longer; the platform layer truncates it.)

Public surface
==============

``render_email(template_id, locale, **vars) -> dict[str, str]``
    Returns ``{"subject", "body_text", "body_html"}`` for an email,
    or ``{"title", "body"}`` for a push notification. The shape
    differs so callers naturally distinguish push vs. email.

``enqueue_email(redis, brand_id, template_id, locale, **vars)``
    Renders + RPUSHes the JSON envelope to
    ``email_queue:brand:{brand_id}``. The email worker drains the
    queue and (in prod) ships to SES/SMTP.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from jinja2 import Environment, StrictUndefined, select_autoescape
from jinja2.exceptions import UndefinedError

from app.email_templates import (
    EMAIL_TEMPLATES,
    PUSH_TEMPLATES,
    EmailTemplate,
    PushTemplate,
    get_template,
)
from app.email_templates.push import PUSH_BODY_CHAR_LIMIT

logger = logging.getLogger(__name__)

__all__ = [
    "render_email",
    "render_push",
    "enqueue_email",
    "enqueue_push",
    "email_queue_key",
    "DEFAULT_LOCALE",
]

DEFAULT_LOCALE = "en-SG"
PLATFORM_NAME = os.getenv("KIX_PLATFORM_NAME", "KiX")
SUPPORT_EMAIL = os.getenv("KIX_SUPPORT_EMAIL", "support@letskix.com")


# Two Jinja envs: one HTML (autoescape), one plaintext (no escape).
_html_env = Environment(
    autoescape=select_autoescape(default_for_string=True, default=True),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)
_text_env = Environment(
    autoescape=False,
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)


def _current_year() -> int:
    """Lazy-eval so tests that freeze time also see the frozen year."""
    import datetime as _dt

    return _dt.datetime.now(_dt.UTC).year


def _globals() -> dict[str, Any]:
    return {
        "platform_name": PLATFORM_NAME,
        "support_email": SUPPORT_EMAIL,
        "current_year": _current_year(),
    }


def _pick_locale_source(
    locale_map: dict[str, str],
    requested: str,
    template_id: str,
    field: str,
) -> str:
    """Return the best-matching source string from ``locale_map``.

    Falls back to ``DEFAULT_LOCALE`` with a WARN log if the requested
    locale isn't present. Raises ``KeyError`` only if even the default
    is missing (template registry bug — caught by the locales-coverage
    test).
    """
    if requested in locale_map:
        return locale_map[requested]
    logger.warning(
        "email_template missing_locale template=%s field=%s locale=%s "
        "falling_back_to=%s",
        template_id, field, requested, DEFAULT_LOCALE,
    )
    if DEFAULT_LOCALE in locale_map:
        return locale_map[DEFAULT_LOCALE]
    raise KeyError(
        f"template {template_id!r} field {field!r} has no source "
        f"for locale {requested!r} or fallback {DEFAULT_LOCALE!r}"
    )


def _validate_required(template_id: str, required: list[str], provided: dict[str, Any]) -> None:
    missing = [v for v in required if v not in provided]
    if missing:
        raise ValueError(
            f"template {template_id!r} missing required vars: {missing}"
        )


def _render(env: Environment, source: str, ctx: dict[str, Any]) -> str:
    try:
        return env.from_string(source).render(**ctx)
    except UndefinedError as exc:
        # Re-raise as ValueError so callers handle one error class.
        raise ValueError(f"undefined variable in template: {exc}") from exc


def render_email(
    template_id: str,
    locale: str = DEFAULT_LOCALE,
    **template_vars: Any,
) -> dict[str, str]:
    """Render an email or push template into its final shape.

    For email templates returns ``{subject, body_text, body_html}``.
    For push templates returns ``{title, body}``.
    Callers can branch on the keys returned.
    """
    template = get_template(template_id)
    _validate_required(template_id, template.required_vars, template_vars)
    ctx = {**_globals(), **template_vars}

    if isinstance(template, PushTemplate):
        return _render_push_template(template, locale, ctx)
    return _render_email_template(template, locale, ctx)


def _render_email_template(
    template: EmailTemplate, locale: str, ctx: dict[str, Any]
) -> dict[str, str]:
    subject_src = _pick_locale_source(template.subject, locale, template.template_id, "subject")
    text_src = _pick_locale_source(template.body_text, locale, template.template_id, "body_text")
    html_src = _pick_locale_source(template.body_html, locale, template.template_id, "body_html")

    return {
        "subject": _render(_text_env, subject_src, ctx).strip(),
        "body_text": _render(_text_env, text_src, ctx),
        "body_html": _render(_html_env, html_src, ctx),
    }


def _render_push_template(
    template: PushTemplate, locale: str, ctx: dict[str, Any]
) -> dict[str, str]:
    title_src = _pick_locale_source(template.title, locale, template.template_id, "title")
    body_src = _pick_locale_source(template.body, locale, template.template_id, "body")

    title = _render(_text_env, title_src, ctx).strip()
    body = _render(_text_env, body_src, ctx).strip()

    if len(body) > PUSH_BODY_CHAR_LIMIT:
        raise ValueError(
            f"push template {template.template_id!r} body length {len(body)} "
            f"exceeds limit {PUSH_BODY_CHAR_LIMIT} after render in locale {locale!r}"
        )

    return {"title": title, "body": body}


def render_push(
    template_id: str,
    locale: str = DEFAULT_LOCALE,
    **template_vars: Any,
) -> dict[str, str]:
    """Alias for ``render_email`` when the caller knows it's a push.

    Same behaviour, but raises if the template id is *not* a push
    template — catches "called the wrong function" at the boundary.
    """
    if template_id not in PUSH_TEMPLATES:
        raise ValueError(f"{template_id!r} is not a push template")
    return render_email(template_id, locale, **template_vars)


# ── Queue helpers ─────────────────────────────────────────────────────────


def email_queue_key(brand_id: str) -> str:
    """Redis key for the per-brand email outbox queue."""
    return f"email_queue:brand:{brand_id}"


def push_queue_key(brand_id: str) -> str:
    """Redis key for the per-brand push outbox queue."""
    return f"push_queue:brand:{brand_id}"


async def enqueue_email(
    redis: Any,
    *,
    brand_id: str,
    template_id: str,
    locale: str,
    recipient: str | None = None,
    **template_vars: Any,
) -> dict[str, Any]:
    """Render + RPUSH the email envelope onto the brand's outbox.

    Returns the envelope (handy for tests and tracing). Never raises
    if the render itself succeeds — Redis errors propagate.
    """
    if template_id not in EMAIL_TEMPLATES:
        raise ValueError(f"{template_id!r} is not an email template")
    rendered = render_email(template_id, locale, **template_vars)
    envelope = {
        "template_id": template_id,
        "locale": locale,
        "recipient": recipient,
        "subject": rendered["subject"],
        "body_text": rendered["body_text"],
        "body_html": rendered["body_html"],
    }
    try:
        await redis.rpush(email_queue_key(brand_id), json.dumps(envelope))
    except Exception as exc:  # pragma: no cover — Redis failures
        logger.warning(
            "enqueue_email failed brand=%s template=%s err=%s",
            brand_id, template_id, exc,
        )
        raise
    return envelope


async def enqueue_push(
    redis: Any,
    *,
    brand_id: str,
    template_id: str,
    locale: str,
    recipient_kid: str | None = None,
    **template_vars: Any,
) -> dict[str, Any]:
    """Render + RPUSH a push envelope. Same shape as ``enqueue_email``."""
    if template_id not in PUSH_TEMPLATES:
        raise ValueError(f"{template_id!r} is not a push template")
    rendered = render_push(template_id, locale, **template_vars)
    envelope = {
        "template_id": template_id,
        "locale": locale,
        "recipient_kid": recipient_kid,
        "title": rendered["title"],
        "body": rendered["body"],
    }
    await redis.rpush(push_queue_key(brand_id), json.dumps(envelope))
    return envelope
