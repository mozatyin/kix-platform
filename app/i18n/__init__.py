"""KiX Platform i18n runtime (Project Fluent / ICU MessageFormat).

This package is **scaffolding only** — actual UI strings are migrated
into the catalogs in Wave 2. See
``/Users/mozat/a-docs/i18n-trinity-strategy.md`` §4.1 for the strategy
overview, and ``docs/i18n-adding-a-locale.md`` for the per-locale
checklist.

Public surface
==============

``t(key, locale=None, **vars) -> str``
    Translate ``key`` against ``locale`` (defaults to the current
    request locale from :mod:`app.i18n.context`). Falls back through
    the chain returned by :func:`fallback_chain`. Never raises — a
    missing key is logged and returned verbatim.

``SUPPORTED_LOCALES``
    The exhaustive list of BCP 47 locale tags the platform serves.

``fallback_chain(locale)``
    Returns the ordered list of locales to consult when resolving a
    message — most-specific first, then language-only, then the
    base-language regional default, then English.

``get_localization(locale)``
    Returns a cached :class:`fluent.runtime.FluentLocalization` for the
    given locale (with its full fallback chain). Cached via
    :func:`functools.lru_cache` so per-request overhead is a single
    dict-get after first warm-up.

Layout
======

Each locale lives at ``app/i18n/catalogs/<locale>/main.ftl`` — the
canonical layout that ``fluent.runtime.FluentResourceLoader`` expects.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from fluent.runtime import FluentLocalization, FluentResourceLoader

from app.i18n.context import (  # noqa: F401 (re-export)
    DEFAULT_LOCALE,
    get_current_locale,
    reset_current_locale,
    set_current_locale,
)

logger = logging.getLogger(__name__)

# ── Locale registry ───────────────────────────────────────────────────────
SUPPORTED_LOCALES: Final[list[str]] = [
    "en-SG",
    "zh-Hans-SG",
    "en-US",
    "zh-Hans-CN",
]

# Terminal fallback when nothing else resolves.
ULTIMATE_FALLBACK: Final[str] = "en-US"

# Per-language regional fallback (the "base" locale for a language).
# This lets ``en-SG`` fall back to ``en-US`` and ``zh-Hans-SG`` fall back
# to ``zh-Hans-CN`` without hard-coding pair tables in callers.
_LANGUAGE_REGIONAL_FALLBACK: Final[dict[str, str]] = {
    "en": "en-US",
    "zh-Hans": "zh-Hans-CN",
}

_CATALOG_DIR: Final[Path] = Path(__file__).parent / "catalogs"
_RESOURCE_NAME: Final[str] = "main.ftl"


# ── Fallback chain ────────────────────────────────────────────────────────


def fallback_chain(locale: str) -> list[str]:
    """Return the ordered fallback chain for ``locale``.

    Examples
    --------
    - ``en-SG``      → ``["en-SG", "en", "en-US"]``
    - ``zh-Hans-SG`` → ``["zh-Hans-SG", "zh-Hans", "zh-Hans-CN", "en-US"]``
    - ``zh-Hans-CN`` → ``["zh-Hans-CN", "zh-Hans", "en-US"]``
    - ``en-US``      → ``["en-US", "en"]``
    - ``ja-JP``      → ``["ja-JP", "ja", "en-US"]`` (unsupported → English)

    The list is de-duplicated while preserving order so the Fluent
    loader never wastes a parse on the same file twice.
    """
    chain: list[str] = [locale]
    parts = locale.split("-")
    # Build progressively shorter parents: "zh-Hans-SG" → "zh-Hans" → "zh".
    for i in range(len(parts) - 1, 0, -1):
        parent = "-".join(parts[:i])
        chain.append(parent)
        regional = _LANGUAGE_REGIONAL_FALLBACK.get(parent)
        if regional and regional != locale:
            chain.append(regional)
    if ULTIMATE_FALLBACK not in chain:
        chain.append(ULTIMATE_FALLBACK)

    seen: set[str] = set()
    deduped: list[str] = []
    for entry in chain:
        if entry not in seen:
            seen.add(entry)
            deduped.append(entry)
    return deduped


# ── Catalog loading ───────────────────────────────────────────────────────


def _available_locales() -> set[str]:
    """Return the set of locales for which a catalog directory exists."""
    if not _CATALOG_DIR.is_dir():
        return set()
    return {
        p.name
        for p in _CATALOG_DIR.iterdir()
        if p.is_dir() and (p / _RESOURCE_NAME).is_file()
    }


@lru_cache(maxsize=64)
def get_localization(locale: str) -> FluentLocalization:
    """Return a cached :class:`FluentLocalization` for ``locale``.

    The fallback chain is resolved up-front; locales without an on-disk
    catalog are silently skipped so Fluent only parses files that
    actually exist.
    """
    available = _available_locales()
    chain = [loc for loc in fallback_chain(locale) if loc in available]
    if not chain:
        # Nothing on disk — degrade to ultimate fallback if present, or
        # an empty bundle (format_value will then return the key).
        chain = [ULTIMATE_FALLBACK] if ULTIMATE_FALLBACK in available else [locale]

    loader = FluentResourceLoader(str(_CATALOG_DIR / "{locale}"))
    return FluentLocalization(
        locales=chain,
        resource_ids=[_RESOURCE_NAME],
        resource_loader=loader,
    )


def _clear_cache() -> None:
    """Test-only helper: drop the cached FluentLocalization instances.

    Used by ``tests/test_i18n_infra.py`` to verify the LRU cache hit
    behaviour without relying on internal caches across runs.
    """
    get_localization.cache_clear()


# ── Public translate API ──────────────────────────────────────────────────


def t(key: str, locale: str | None = None, **variables: Any) -> str:
    """Translate ``key`` to ``locale`` (or the current request locale).

    Parameters
    ----------
    key:
        The Fluent message id, e.g. ``"welcome-message"`` or
        ``"welcome-message.description"`` for an attribute lookup.
    locale:
        Explicit BCP 47 tag. Defaults to :func:`get_current_locale`.
    **variables:
        Variables passed into the Fluent message (``{ $name }``,
        plural operands, etc.).

    Returns
    -------
    str
        The formatted string. If the key resolves nowhere in the
        fallback chain a structured warning is logged and ``key`` is
        returned verbatim — callers never see an exception.
    """
    target = locale or get_current_locale()
    loc = get_localization(target)

    # Fluent's ``format_value`` doesn't natively address attributes via
    # ``message.attribute`` syntax — it treats the dot as part of the
    # message id. We support it manually by splitting once on '.' and
    # walking the message bundles directly.
    message_id, _, attr_name = key.partition(".")

    try:
        if attr_name:
            result = _format_attribute(loc, message_id, attr_name, variables) or key
        else:
            result = loc.format_value(message_id, variables)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "i18n.format_failed key=%s locale=%s error=%s",
            key, target, exc,
        )
        return key

    # FluentLocalization returns the message id verbatim when the key is
    # absent in every locale in the chain — that's our "missing" signal.
    if result == key or result == message_id:
        if result == message_id and attr_name:
            # Bare message id came back when we asked for an attribute —
            # the attribute is missing.
            logger.warning(
                "i18n.missing_translation key=%s locale=%s chain=%s",
                key, target, fallback_chain(target),
            )
            return key
        if result == key:
            logger.warning(
                "i18n.missing_translation key=%s locale=%s chain=%s",
                key, target, fallback_chain(target),
            )
    return result


def _format_attribute(
    loc: FluentLocalization,
    message_id: str,
    attr_name: str,
    variables: dict[str, Any],
) -> str | None:
    """Resolve a Fluent ``message.attribute`` against the loc's bundle chain.

    Returns ``None`` if the attribute is missing in every bundle so the
    caller can emit the missing-translation warning.
    """
    for bundle in loc._bundles():  # noqa: SLF001 — public surface lacking
        try:
            msg = bundle.get_message(message_id)
        except (KeyError, LookupError):
            continue
        if msg is None:
            continue
        attr = msg.attributes.get(attr_name)
        if attr is None:
            continue
        formatted, _errs = bundle.format_pattern(attr, variables)
        return formatted
    return None


__all__ = [
    "SUPPORTED_LOCALES",
    "DEFAULT_LOCALE",
    "ULTIMATE_FALLBACK",
    "fallback_chain",
    "get_localization",
    "get_current_locale",
    "set_current_locale",
    "reset_current_locale",
    "t",
]
