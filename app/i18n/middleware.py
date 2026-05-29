"""FastAPI middleware that resolves the request locale.

Resolution priority (highest wins)
==================================

1. ``?lang=<tag>`` query parameter — explicit user override, e.g.
   for testing or a locale switcher widget link.
2. Authenticated user's saved preference
   (``user_profiles.preferred_locale`` — looked up by ``user_id`` from
   the JWT in the ``Authorization: Bearer ...`` header).
3. ``Accept-Language`` header — standard BCP 47 negotiation with
   quality-value weighting.
4. ``KIX_REGION`` region's primary language (via
   :func:`app.region.get_region_config`).
5. Hard-coded default ``en-SG`` (matches the SG bilingual launch).

The middleware mutates the request-scoped contextvar in
:mod:`app.i18n.context` and never raises; if every lookup fails the
default locale is used. The chosen tag is also exposed on
``request.state.locale`` for downstream handlers that prefer attribute
access to the contextvar.

The middleware also sets a ``Content-Language`` response header so
intermediate caches key correctly.

NOTE: the user-preference lookup is wired but intentionally tolerant —
if no JWT is present or the lookup helper isn't available the request
proceeds with whatever the next-lower priority source supplies. This
keeps the middleware compatible with the existing test fixtures (which
hit the API anonymously).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Iterable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.i18n import SUPPORTED_LOCALES
from app.i18n.context import DEFAULT_LOCALE, set_current_locale, reset_current_locale

logger = logging.getLogger(__name__)


class LanguageMiddleware(BaseHTTPMiddleware):
    """Resolve the active locale per-request and bind the contextvar.

    Mount this **before** auth middleware so handlers that read the
    locale during authentication / consent / GDPR responses see the
    correct tag.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        locale = await self._resolve_locale(request)

        # Bind to the contextvar for the duration of this request. The
        # token is reset in ``finally`` so the contextvar leaks no
        # cross-request state even though FastAPI tasks share a loop.
        token = set_current_locale(locale)
        request.state.locale = locale
        try:
            response = await call_next(request)
        finally:
            reset_current_locale(token)

        # Advertise the chosen locale to caches + clients.
        response.headers.setdefault("Content-Language", locale)
        return response

    # ── Resolution chain ──────────────────────────────────────────────

    async def _resolve_locale(self, request: Request) -> str:
        # 1. ?lang=
        q = request.query_params.get("lang")
        match = _match(q)
        if match:
            return match

        # 2. User preference (JWT → DB)
        match = await _user_preference(request)
        if match:
            return match

        # 3. Accept-Language header
        header = request.headers.get("accept-language")
        match = _negotiate_accept_language(header)
        if match:
            return match

        # 4. KIX_REGION primary language
        match = _region_default()
        if match:
            return match

        # 5. Hard default
        return DEFAULT_LOCALE


# ── Helpers (module-level so they're trivial to unit-test) ─────────────────


def _match(candidate: str | None) -> str | None:
    """Return ``candidate`` iff it appears in ``SUPPORTED_LOCALES``.

    Falsy values and unknown tags return ``None``.
    """
    if not candidate:
        return None
    if candidate in SUPPORTED_LOCALES:
        return candidate
    # Case-insensitive match (BCP 47 tags are case-insensitive).
    lower = candidate.lower()
    for supported in SUPPORTED_LOCALES:
        if supported.lower() == lower:
            return supported
    return None


def _parse_accept_language(header: str) -> list[tuple[str, float]]:
    """Parse an ``Accept-Language`` header into ``[(tag, q), ...]``.

    Results are sorted descending by quality value. Malformed parts are
    skipped silently — this is an HTTP header, we never raise on it.
    """
    if not header:
        return []
    parts = []
    for raw in header.split(","):
        token = raw.strip()
        if not token:
            continue
        tag = token
        q = 1.0
        if ";" in token:
            tag, _, params = token.partition(";")
            tag = tag.strip()
            for param in params.split(";"):
                key, _, value = param.partition("=")
                if key.strip().lower() == "q":
                    try:
                        q = float(value.strip())
                    except ValueError:
                        q = 0.0
        if not tag or tag == "*":
            continue
        parts.append((tag, q))
    parts.sort(key=lambda kv: kv[1], reverse=True)
    return parts


def _negotiate_accept_language(header: str | None) -> str | None:
    """Pick the best supported locale from an Accept-Language header.

    Strategy
    --------
    For each requested tag (in descending q order):

    1. Exact match in ``SUPPORTED_LOCALES``.
    2. Prefix-extended match (e.g. requested ``zh-CN`` matches
       ``zh-Hans-CN`` when the base language ``zh`` aligns).
    3. Base-language match (requested ``zh`` matches ``zh-Hans-CN``).
    """
    if not header:
        return None
    candidates = _parse_accept_language(header)
    if not candidates:
        return None

    supported_by_lang = _index_supported_by_language()

    for tag, _q in candidates:
        # 1. Exact case-insensitive match
        exact = _match(tag)
        if exact:
            return exact
        # 2. Base-language match — `zh-CN` → `zh-Hans-CN`, `en-GB` → `en-SG`.
        base = tag.split("-", 1)[0].lower()
        if base in supported_by_lang:
            # Prefer a supported tag whose region matches if possible.
            requested_region = tag.split("-")[-1].lower() if "-" in tag else None
            candidates_for_lang = supported_by_lang[base]
            if requested_region:
                for sup in candidates_for_lang:
                    if sup.lower().endswith("-" + requested_region):
                        return sup
            return candidates_for_lang[0]
    return None


def _index_supported_by_language() -> dict[str, list[str]]:
    """Return ``{base_language: [supported_locale, ...]}``."""
    out: dict[str, list[str]] = {}
    for loc in SUPPORTED_LOCALES:
        base = loc.split("-", 1)[0].lower()
        out.setdefault(base, []).append(loc)
    return out


async def _user_preference(request: Request) -> str | None:
    """Look up the authenticated user's stored locale preference.

    Tolerant of every failure mode (no header, malformed JWT, DB lookup
    failure, lookup helper absent). On any error returns ``None`` so the
    next-lower priority source is consulted.
    """
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None

    # The middleware deliberately avoids importing the JWT decoder at
    # module-import time — keeping this lookup behind a try/except means
    # bootstrap order can't break the request pipeline.
    try:
        from app.security import decode_token  # type: ignore
        payload: dict[str, Any] = decode_token(auth.split(None, 1)[1])  # type: ignore
        user_id = payload.get("sub") or payload.get("user_id")
        if not user_id:
            return None
    except Exception:
        return None

    # Lookup helper is opt-in: routers may register a callable into
    # ``LanguageMiddleware.user_locale_lookup`` so the i18n module
    # never has to import the user-profile model directly. This keeps
    # the middleware decoupled from the ORM.
    lookup = getattr(LanguageMiddleware, "user_locale_lookup", None)
    if lookup is None:
        return None
    try:
        result = lookup(user_id)
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[assignment]
    except Exception:
        return None
    return _match(result)  # validate against SUPPORTED_LOCALES


def _region_default() -> str | None:
    """Return the active region's primary language if supported.

    Consults the BCP 47 ``language_fallback_chain`` first (the modern
    field added by the i18n scaffold) and falls back to the legacy
    ``languages`` list. We walk the chain so a region whose primary
    language isn't yet shipped (e.g. ``ms-MY`` in SG Phase 1) still
    gets a usable locale rather than skipping straight to the hard
    default.
    """
    try:
        from app.region import get_supported_locales_for_region
        langs: Iterable[str] = get_supported_locales_for_region() or []
        for lang in langs:
            match = _match(lang)
            if match:
                return match
    except Exception:
        return None
    return None
