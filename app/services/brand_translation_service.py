"""Brand translation service — read/write helpers + LLM bulk-translate.

The service is the single supported surface for accessing the
``brand_translations`` sidecar table. Callers should never compose raw
SQL — go through here so the fallback chain + LLM quota guard stay in
one place.

API
---
* :func:`get_translation`   — single-field lookup with BCP-47 fallback
* :func:`set_translation`   — upsert (manual or auto-translated)
* :func:`list_review_queue` — admin review backlog (auto + unreviewed)
* :func:`mark_reviewed`     — flip the ``reviewed`` bit
* :func:`bulk_translate_brand` — LLM-translate every translatable field
  on a brand into a target locale, quota-guarded.
* :func:`validate_locale`   — minimal BCP-47 sanity check
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.brand_translation import BrandTranslation

logger = logging.getLogger(__name__)


# ── Locale validation ────────────────────────────────────────────────────

# Minimal BCP 47 shape: ``language[-script][-region]`` where:
#   language = 2–3 lowercase letters (ISO 639)
#   script   = optional, 4 letters (ISO 15924), title-cased ("Hans", "Latn")
#   region   = optional, 2 letters OR 3 digits (ISO 3166-1 / UN M.49)
# We accept the common subset used across the platform; this is a sanity
# guard, not a full RFC 5646 parser.
_BCP47_RE = re.compile(
    r"^[a-z]{2,3}"           # language
    r"(?:-[A-Z][a-z]{3})?"   # optional script
    r"(?:-(?:[A-Z]{2}|[0-9]{3}))?"  # optional region
    r"$"
)


def validate_locale(locale: str) -> bool:
    """Return ``True`` when ``locale`` is a syntactically valid BCP-47 tag.

    Designed to reject the common bad shapes that slip through input
    validation: empty string, ``_`` separators, country-only codes,
    arbitrary garbage.
    """
    if not locale or not isinstance(locale, str):
        return False
    if len(locale) > 16:  # column width
        return False
    return bool(_BCP47_RE.match(locale))


# ── Fallback chain (delegates to app.i18n) ───────────────────────────────


def _resolve_fallback_chain(locale: str) -> list[str]:
    """Build the lookup-order list for ``locale``.

    Delegates to :func:`app.i18n.fallback_chain` so the rules stay in
    sync with the Fluent catalog resolver. Falls back to a minimal
    parent-chain if the i18n module is unavailable (e.g. partial test
    imports).
    """
    try:
        from app.i18n import fallback_chain

        return fallback_chain(locale)
    except Exception:  # pragma: no cover — defensive
        parts = locale.split("-")
        chain = [locale]
        for i in range(len(parts) - 1, 0, -1):
            chain.append("-".join(parts[:i]))
        return chain


# ── Read ────────────────────────────────────────────────────────────────


async def get_translation(
    session: AsyncSession,
    brand_id: str,
    field: str,
    locale: str,
) -> str | None:
    """Return the best-match translation value, or ``None`` if no row hits.

    Walks the BCP-47 fallback chain (``zh-Hans-SG`` → ``zh-Hans`` →
    ``zh-Hans-CN`` → ``en-US``) and returns the first row that exists.
    A single batched ``SELECT ... IN`` query is used so the cost is
    one round-trip regardless of chain depth.
    """
    chain = _resolve_fallback_chain(locale)
    stmt = (
        select(BrandTranslation)
        .where(BrandTranslation.brand_id == brand_id)
        .where(BrandTranslation.field_name == field)
        .where(BrandTranslation.locale.in_(chain))
    )
    result = await session.execute(stmt)
    rows = {row.locale: row for row in result.scalars().all()}
    # Honour chain order: first match wins.
    for loc in chain:
        if loc in rows:
            return rows[loc].value
    return None


async def list_translations_for_brand(
    session: AsyncSession,
    brand_id: str,
) -> list[BrandTranslation]:
    """Return every translation row for ``brand_id`` (any field, any locale)."""
    stmt = (
        select(BrandTranslation)
        .where(BrandTranslation.brand_id == brand_id)
        .order_by(
            BrandTranslation.field_name, BrandTranslation.locale
        )
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ── Write ───────────────────────────────────────────────────────────────


async def set_translation(
    session: AsyncSession,
    brand_id: str,
    field: str,
    locale: str,
    value: str,
    auto: bool = False,
    reviewer: str | None = None,
) -> BrandTranslation:
    """Upsert one translation row.

    ``auto=True`` marks the row as LLM-produced so it surfaces in the
    admin review queue. Passing ``reviewer`` implicitly flips
    ``reviewed=True`` — that's how merchant-owner edits skip the queue.
    """
    if not validate_locale(locale):
        raise ValueError(f"invalid BCP-47 locale: {locale!r}")
    if not brand_id or not field:
        raise ValueError("brand_id and field are required")

    reviewed = reviewer is not None

    # ``ON CONFLICT DO UPDATE`` keeps the call idempotent — the admin
    # UI may re-PUT the same value repeatedly without creating dupes.
    stmt = pg_insert(BrandTranslation).values(
        brand_id=brand_id,
        field_name=field,
        locale=locale,
        value=value,
        auto_translated=bool(auto),
        reviewed=bool(reviewed),
        reviewer_id=reviewer,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["brand_id", "field_name", "locale"],
        set_={
            "value": stmt.excluded.value,
            "auto_translated": stmt.excluded.auto_translated,
            "reviewed": stmt.excluded.reviewed,
            "reviewer_id": stmt.excluded.reviewer_id,
        },
    )
    await session.execute(stmt)
    await session.flush()

    fetched = await session.execute(
        select(BrandTranslation)
        .where(BrandTranslation.brand_id == brand_id)
        .where(BrandTranslation.field_name == field)
        .where(BrandTranslation.locale == locale)
    )
    row = fetched.scalar_one()
    return row


async def delete_translation(
    session: AsyncSession,
    brand_id: str,
    field: str,
    locale: str,
) -> int:
    """Delete a single translation row. Returns rows-affected count."""
    stmt = (
        delete(BrandTranslation)
        .where(BrandTranslation.brand_id == brand_id)
        .where(BrandTranslation.field_name == field)
        .where(BrandTranslation.locale == locale)
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


# ── Review queue ─────────────────────────────────────────────────────────


async def list_review_queue(
    session: AsyncSession,
    limit: int = 100,
) -> list[BrandTranslation]:
    """Return the oldest auto-translated, unreviewed rows.

    The partial index ``idx_brand_translations_review_queue`` makes
    this scan O(queue-size), independent of total translation volume.
    """
    stmt = (
        select(BrandTranslation)
        .where(BrandTranslation.auto_translated.is_(True))
        .where(BrandTranslation.reviewed.is_(False))
        .order_by(BrandTranslation.created_at.asc())
        .limit(max(1, min(int(limit), 1000)))
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def mark_reviewed(
    session: AsyncSession,
    brand_id: str,
    field: str,
    locale: str,
    reviewer_id: str,
) -> bool:
    """Flip ``reviewed`` to True. Returns ``True`` if a row was updated."""
    fetched = await session.execute(
        select(BrandTranslation)
        .where(BrandTranslation.brand_id == brand_id)
        .where(BrandTranslation.field_name == field)
        .where(BrandTranslation.locale == locale)
    )
    row = fetched.scalar_one_or_none()
    if row is None:
        return False
    row.reviewed = True
    row.reviewer_id = reviewer_id
    await session.flush()
    return True


# ── LLM-driven bulk translate ────────────────────────────────────────────


# Brand fields that the platform considers translatable. Anything not on
# this list is treated as locale-invariant (slugs, IDs, internal flags).
TRANSLATABLE_BRAND_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "tagline",
    "voucher_title",
    "voucher_description",
    "recipe_title",
    "recipe_description",
    "welcome_message",
)


# Default Singapore launch locales — Phase 1 target per i18n-trinity-strategy.md.
SG_LAUNCH_LOCALES: tuple[str, ...] = ("en-SG", "zh-Hans-SG")


async def _llm_translate(
    text: str, target_locale: str, source_locale: str = "en-US"
) -> str:
    """Translate one string via the platform LLM, quota-guarded.

    Always calls :func:`scripts.llm_quota_monitor.wait_if_paused` first
    so long bulk-translate jobs respect the 95/90 platform quota gate
    (see ``feedback_llm_quota_guard.md``). When the live LLM is
    unreachable, falls back to returning the source text unchanged so
    the caller still gets a row to review.
    """
    try:
        from scripts.llm_quota_monitor import wait_if_paused

        await wait_if_paused(max_wait_seconds=3600)
    except Exception as exc:  # pragma: no cover — monitor optional
        logger.debug("wait_if_paused unavailable (%s); continuing", exc)

    try:  # pragma: no cover — exercised via integration tests only
        from scripts.i18n_prompts import llm_translate  # type: ignore

        return await llm_translate(text, source_locale, target_locale)
    except Exception as exc:
        logger.warning(
            "llm_translate fallback (text=%r target=%s): %s",
            text[:40],
            target_locale,
            exc,
        )
        return text  # safe degrade: caller sees original, marks for review


async def bulk_translate_brand(
    session: AsyncSession,
    brand_id: str,
    target_locale: str,
    *,
    source_fields: dict[str, str] | None = None,
    source_locale: str = "en-US",
    llm_fn: Any = None,
) -> dict[str, Any]:
    """LLM-translate every translatable field of ``brand_id`` into ``target_locale``.

    Parameters
    ----------
    source_fields
        Map of ``field_name → source_value`` (e.g. fetched from
        ``brand_configs.config_json``). When omitted we look up the
        source-locale rows from ``brand_translations`` itself — useful
        when chaining bulk-translate across locales.
    llm_fn
        Optional override for the translation function. Production
        defaults to :func:`_llm_translate`; tests inject a deterministic
        stub. Must be ``async``: ``(text, target_locale) -> str``.

    Returns ``{"brand_id", "target_locale", "translated": {field: value}}``.
    All produced rows are flagged ``auto_translated=True, reviewed=False``
    so they surface in the admin review queue.
    """
    if not validate_locale(target_locale):
        raise ValueError(f"invalid BCP-47 target_locale: {target_locale!r}")

    if source_fields is None:
        # Pull whatever we have in the source locale already.
        stmt = (
            select(BrandTranslation)
            .where(BrandTranslation.brand_id == brand_id)
            .where(BrandTranslation.locale == source_locale)
        )
        result = await session.execute(stmt)
        source_fields = {
            row.field_name: row.value for row in result.scalars().all()
        }

    fn = llm_fn if llm_fn is not None else (
        lambda text, target: _llm_translate(text, target, source_locale)
    )

    translated: dict[str, str] = {}
    for field, value in source_fields.items():
        if field not in TRANSLATABLE_BRAND_FIELDS:
            continue
        if not value:
            continue
        out = await fn(value, target_locale)
        await set_translation(
            session,
            brand_id=brand_id,
            field=field,
            locale=target_locale,
            value=out,
            auto=True,
            reviewer=None,
        )
        translated[field] = out

    return {
        "brand_id": brand_id,
        "target_locale": target_locale,
        "translated": translated,
        "count": len(translated),
    }
