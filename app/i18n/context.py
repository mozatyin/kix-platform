"""Per-request locale context.

Mirrors the Soul vendor's ``LanguageContext`` pattern: a single
``contextvars.ContextVar`` is set in :class:`app.i18n.middleware.LanguageMiddleware`
once per request, then read everywhere downstream — services, LLM
prompts, render helpers, ICU catalog lookups.

The default is ``en-SG``, matching the SG bilingual launch (see
``/Users/mozat/a-docs/i18n-trinity-strategy.md`` §4.1).

Usage::

    from app.i18n.context import get_current_locale, set_current_locale

    locale = get_current_locale()             # read in any handler
    token = set_current_locale("zh-Hans-SG")  # override for a scope
    try:
        ...
    finally:
        reset_current_locale(token)
"""

from __future__ import annotations

import contextvars
from typing import Final

DEFAULT_LOCALE: Final[str] = "en-SG"

# Module-level ContextVar — see PEP 567. ContextVars are async-safe and
# isolate state per-request when FastAPI is run on a single asyncio loop.
current_locale: contextvars.ContextVar[str] = contextvars.ContextVar(
    "kix_current_locale",
    default=DEFAULT_LOCALE,
)


def get_current_locale() -> str:
    """Return the active locale for the current request scope."""
    return current_locale.get()


def set_current_locale(locale: str) -> contextvars.Token[str]:
    """Override the active locale; returns a token usable with ``reset``."""
    return current_locale.set(locale)


def reset_current_locale(token: contextvars.Token[str]) -> None:
    """Restore the previous locale (paired with :func:`set_current_locale`)."""
    current_locale.reset(token)
