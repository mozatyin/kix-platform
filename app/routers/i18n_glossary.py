"""Read + admin endpoints for the i18n glossary.

The glossary file format lives at ``app/i18n/glossary/`` and is the
source-of-truth for `LLM-translator do-not-translate` decisions
(see :mod:`scripts.i18n_translate`).

Mounted at ``/api/v1/i18n/glossary`` by :mod:`app.main`.

Endpoints
---------
``GET  /``                       List the merged global glossary.
``GET  /{locale}``               List the locale-scoped merged glossary.
``PUT  /term``                   Admin upsert (requires ``x-kix-admin-token``).
``DELETE /term/{term_id}``       Admin remove.
``GET  /admin/tm-stats``         Translation-memory hit-rate stats.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Path as FPath, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter()

ADMIN_TOKEN_HEADER = "x-kix-admin-token"


# ── Lazy imports — keep router cheap to import in test envs ─────────────


_GLOSSARY_MOD_CACHE = None


def _glossary_mod():
    """Import :mod:`scripts.i18n_glossary` by file path.

    The shared ``scripts`` namespace is hijacked by sibling repos
    (notably ``/Users/mozat/eltm/scripts/__init__.py``), so plain
    ``from scripts import i18n_glossary`` resolves to the wrong package
    at runtime. We side-step the lookup with an explicit
    ``importlib.util.spec_from_file_location`` against this repo's
    ``scripts/i18n_glossary.py``.
    """
    global _GLOSSARY_MOD_CACHE
    if _GLOSSARY_MOD_CACHE is not None:
        return _GLOSSARY_MOD_CACHE

    import importlib.util
    from pathlib import Path

    import sys

    repo_root = Path(__file__).resolve().parents[2]
    src = repo_root / "scripts" / "i18n_glossary.py"
    mod_name = "kix_i18n_glossary"
    spec = importlib.util.spec_from_file_location(mod_name, src)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass can look up __module__.
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    _GLOSSARY_MOD_CACHE = mod
    return mod


def _check_admin(token: str | None) -> None:
    """Mirror the pattern used by :mod:`app.routers.media`.

    Fail-closed when neither env var nor sane fallback is configured.
    """
    expected = os.environ.get("KIX_ADMIN_TOKEN") or os.environ.get(
        "KIX_ADMIN_TOKEN_DEFAULT"
    )
    if not expected:
        # No admin configured → reject all admin actions.
        raise HTTPException(status_code=403, detail="admin token not configured")
    if not token or token != expected:
        raise HTTPException(status_code=403, detail="invalid admin token")


# ── Pydantic schemas ───────────────────────────────────────────────────


class TermIn(BaseModel):
    term_id: str = Field(..., min_length=1, max_length=128)
    source_term: str | None = None
    do_not_translate: bool | None = None
    category: str | None = Field(None, pattern=r"^(product_name|technical|brand_specific|ui_label|other)$")
    translation: str | None = None
    locale: str | None = Field(None, max_length=16)


class TermOut(BaseModel):
    term_id: str
    source_term: str
    do_not_translate: bool
    category: str
    translation: str | None = None
    locale: str | None = None


# ── Routes ─────────────────────────────────────────────────────────────


@router.get("", response_model=list[TermOut])
async def list_global_glossary() -> list[TermOut]:
    """Return the merged global glossary (locale-agnostic)."""
    g = _glossary_mod()
    return [TermOut(**t.to_dict()) for t in g.load_glossary(None)]


@router.get("/admin/tm-stats")
async def tm_stats(locale: str | None = Query(None)) -> dict[str, Any]:
    """Translation-memory hit-rate stats per locale.

    Falls back to ``{"redis": False}`` when Redis is not configured.
    """
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        return {"redis": False, "stats": {}}
    try:
        from redis import asyncio as aioredis  # type: ignore

        r = aioredis.from_url(redis_url, decode_responses=True)
        keys = await r.keys("i18n:tm:stats:*")
        out: dict[str, Any] = {}
        for k in keys:
            loc = k.split(":")[-1]
            if locale and loc != locale:
                continue
            row = await r.hgetall(k)
            hits = int(row.get("hits", 0))
            writes = int(row.get("writes", 0))
            total = hits + writes
            out[loc] = {
                "hits": hits,
                "writes": writes,
                "hit_rate": (hits / total) if total else 0.0,
            }
        return {"redis": True, "stats": out}
    except Exception as e:  # pragma: no cover
        logger.warning("TM stats failed: %s", e)
        return {"redis": False, "stats": {}, "error": str(e)}


@router.get("/{locale}", response_model=list[TermOut])
async def list_locale_glossary(
    locale: str = FPath(..., max_length=16),
) -> list[TermOut]:
    """Return the merged glossary for ``locale`` (global + locale overrides)."""
    g = _glossary_mod()
    return [TermOut(**t.to_dict()) for t in g.load_glossary(locale)]


@router.put("/term", response_model=TermOut)
async def upsert_term(
    body: TermIn,
    x_kix_admin_token: str | None = Header(None, alias=ADMIN_TOKEN_HEADER),
) -> TermOut:
    """Admin upsert. ``locale=None`` writes to ``global.json``."""
    _check_admin(x_kix_admin_token)
    g = _glossary_mod()
    term = g.upsert_term(
        body.term_id,
        source_term=body.source_term,
        do_not_translate=body.do_not_translate,
        category=body.category,
        translation=body.translation,
        locale=body.locale,
    )
    return TermOut(**term.to_dict())


@router.delete("/term/{term_id}")
async def delete_term(
    term_id: str,
    locale: str | None = Query(None),
    x_kix_admin_token: str | None = Header(None, alias=ADMIN_TOKEN_HEADER),
) -> dict[str, Any]:
    _check_admin(x_kix_admin_token)
    g = _glossary_mod()
    ok = g.delete_term(term_id, locale=locale)
    if not ok:
        raise HTTPException(status_code=404, detail="term_id not found")
    return {"deleted": True, "term_id": term_id, "locale": locale}
