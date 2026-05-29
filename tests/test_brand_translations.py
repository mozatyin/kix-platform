"""Tests for the brand-translations i18n stack.

Covers:
* ORM model round-trip (create / read / update / unique-constraint)
* Service-layer fallback chain (zh-Hans-SG → zh-Hans → zh-Hans-CN)
* Auto-translate flag + admin review queue + mark_reviewed transitions
* ``bulk_translate_brand`` calls the (stubbed) LLM and persists rows
* HTTP surface: auth, admin-only review queue, locale validation
* Migration metadata: idempotency (IF NOT EXISTS guards), DDL shape

These tests use an in-memory SQLite engine via the SQLAlchemy
``Base.metadata`` so they don't require a live Postgres. The DDL-level
assertions (ICU collation, partial index, pg_trgm) are verified by
inspecting the migration source — they only execute on Postgres.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.models import Base, BrandTranslation  # noqa: E402
from app.services import brand_translation_service as svc  # noqa: E402


# ── Engine / session fixtures ────────────────────────────────────────────


@pytest.fixture
def db_engine():
    """In-memory aiosqlite engine — fast, isolated per test."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _create():
        async with eng.begin() as conn:
            # Only create the BrandTranslation table — avoids pulling in
            # Postgres-only types (JSONB, Geography) from sibling models.
            await conn.run_sync(
                lambda sync_conn: BrandTranslation.__table__.create(sync_conn)
            )

    asyncio.get_event_loop().run_until_complete(_create())
    yield eng
    asyncio.get_event_loop().run_until_complete(eng.dispose())


@pytest.fixture
def session_factory(db_engine):
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


def _run(coro):
    """Sync wrapper so tests stay top-level functions (no async-marker dep)."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ── 1. Create / read / update ────────────────────────────────────────────


def test_create_read_update_translation(session_factory):
    async def go():
        async with session_factory() as s:
            await svc.set_translation(
                s, "brand-x", "name", "en-SG", "X Coffee"
            )
            await s.commit()

        async with session_factory() as s:
            v = await svc.get_translation(s, "brand-x", "name", "en-SG")
            assert v == "X Coffee"

        async with session_factory() as s:
            await svc.set_translation(
                s, "brand-x", "name", "en-SG", "X Café"
            )
            await s.commit()

        async with session_factory() as s:
            v = await svc.get_translation(s, "brand-x", "name", "en-SG")
            assert v == "X Café"

    _run(go())


# ── 2. Fallback chain ────────────────────────────────────────────────────


def test_fallback_chain_zh_hans(session_factory):
    """zh-Hans-SG miss should fall back through zh-Hans → zh-Hans-CN."""

    async def go():
        async with session_factory() as s:
            await svc.set_translation(
                s, "brand-y", "name", "zh-Hans-CN", "茶店"
            )
            await s.commit()

        async with session_factory() as s:
            # Direct hit
            assert await svc.get_translation(
                s, "brand-y", "name", "zh-Hans-CN"
            ) == "茶店"
            # Fallback hit: requested zh-Hans-SG, served from zh-Hans-CN
            # (chain: zh-Hans-SG → zh-Hans → zh-Hans-CN → en-US)
            assert await svc.get_translation(
                s, "brand-y", "name", "zh-Hans-SG"
            ) == "茶店"
            # Now add a more specific zh-Hans-SG row — direct hit should
            # win over the existing zh-Hans-CN fallback.
            await svc.set_translation(
                s, "brand-y", "name", "zh-Hans-SG", "新店"
            )
            await s.commit()

        async with session_factory() as s:
            assert await svc.get_translation(
                s, "brand-y", "name", "zh-Hans-SG"
            ) == "新店"
            # Unrelated locale never hits this brand's translations
            assert await svc.get_translation(
                s, "brand-y", "name", "ja-JP"
            ) is None

    _run(go())


# ── 3. Auto-translated flag → surfaces in review queue ─────────────────


def test_auto_translated_flag_and_review_queue(session_factory):
    async def go():
        async with session_factory() as s:
            await svc.set_translation(
                s, "brand-z", "name", "en-SG", "auto-en", auto=True
            )
            await svc.set_translation(
                s,
                "brand-z",
                "name",
                "zh-Hans-SG",
                "手动",
                auto=False,
                reviewer="owner:brand-z",
            )
            await s.commit()

        async with session_factory() as s:
            queue = await svc.list_review_queue(s)
            keys = [(r.brand_id, r.field_name, r.locale) for r in queue]
            assert ("brand-z", "name", "en-SG") in keys
            # The reviewed (manual) one must not be in queue
            assert ("brand-z", "name", "zh-Hans-SG") not in keys

    _run(go())


# ── 4. Mark-reviewed transition ──────────────────────────────────────────


def test_mark_reviewed_transitions(session_factory):
    async def go():
        async with session_factory() as s:
            await svc.set_translation(
                s, "brand-r", "name", "en-SG", "auto", auto=True
            )
            await s.commit()

        async with session_factory() as s:
            ok = await svc.mark_reviewed(
                s, "brand-r", "name", "en-SG", "admin-1"
            )
            await s.commit()
            assert ok is True

        async with session_factory() as s:
            queue = await svc.list_review_queue(s)
            assert all(r.brand_id != "brand-r" for r in queue)

        async with session_factory() as s:
            ok2 = await svc.mark_reviewed(
                s, "brand-r", "name", "missing-LOC", "admin-1"
            )
            assert ok2 is False

    _run(go())


# ── 5. Bulk translate calls LLM stub + persists rows ─────────────────────


def test_bulk_translate_brand_calls_llm_and_persists(session_factory):
    calls: list[tuple[str, str]] = []

    async def fake_llm(text: str, target: str) -> str:
        calls.append((text, target))
        return f"[{target}] {text}"

    async def go():
        async with session_factory() as s:
            result = await svc.bulk_translate_brand(
                s,
                brand_id="brand-bt",
                target_locale="zh-Hans-SG",
                source_fields={
                    "name": "Acme",
                    "description": "great coffee",
                    "not_translatable": "leave me",  # should be skipped
                },
                llm_fn=fake_llm,
            )
            await s.commit()

        assert result["count"] == 2
        assert set(result["translated"].keys()) == {"name", "description"}
        # 2 LLM calls, both to the target locale
        assert len(calls) == 2
        assert {c[1] for c in calls} == {"zh-Hans-SG"}

        # Persistence + auto_translated=True
        async with session_factory() as s:
            v = await svc.get_translation(
                s, "brand-bt", "name", "zh-Hans-SG"
            )
            assert v == "[zh-Hans-SG] Acme"
            queue = await svc.list_review_queue(s)
            assert any(r.brand_id == "brand-bt" for r in queue)

    _run(go())


# ── 6. Unique constraint on (brand_id, field, locale) ───────────────────


def test_unique_constraint_on_pk(session_factory):
    """Setting the same triple twice is an UPSERT, not a duplicate insert."""

    async def go():
        async with session_factory() as s:
            await svc.set_translation(s, "u", "f", "en-SG", "v1")
            await s.commit()
        async with session_factory() as s:
            await svc.set_translation(s, "u", "f", "en-SG", "v2")
            await s.commit()

        async with session_factory() as s:
            # Direct ORM count: exactly one row.
            from sqlalchemy import select, func as sa_func

            n = (
                await s.execute(
                    select(sa_func.count())
                    .select_from(BrandTranslation)
                    .where(BrandTranslation.brand_id == "u")
                )
            ).scalar_one()
            assert n == 1
            v = await svc.get_translation(s, "u", "f", "en-SG")
            assert v == "v2"

    _run(go())


# ── 7. Locale validation rejects bad BCP-47 codes ───────────────────────


def test_locale_validation():
    good = ["en", "en-SG", "zh-Hans", "zh-Hans-SG", "ar-EG", "es-419"]
    bad = ["", "EN", "en_SG", "english", "en-singapore", "x" * 32, "12-AB"]
    for tag in good:
        assert svc.validate_locale(tag), f"should accept {tag!r}"
    for tag in bad:
        assert not svc.validate_locale(tag), f"should reject {tag!r}"


def test_set_translation_rejects_bad_locale(session_factory):
    async def go():
        async with session_factory() as s:
            with pytest.raises(ValueError):
                await svc.set_translation(s, "b", "f", "BAD_LOCALE", "x")

    _run(go())


# ── 8. HTTP surface — admin-only review endpoint ────────────────────────


def test_admin_review_endpoint_requires_token(monkeypatch):
    """The /api/v1/admin/translations/review-queue endpoint must 403 without token."""
    monkeypatch.setenv("KIX_ADMIN_TOKEN", "letmein-letmein")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import brand_translations as br

    app = FastAPI()
    app.include_router(br.admin_router, prefix="/api/v1/admin")

    # Provide a no-op DB dependency so the endpoint can run when it
    # gets past the auth gate.
    async def _noop_db():
        class _NullSession:
            async def execute(self, *_a, **_k):
                class _R:
                    def scalars(self):
                        class _S:
                            def all(self):
                                return []
                        return _S()
                return _R()
        yield _NullSession()

    from app.database import get_db
    app.dependency_overrides[get_db] = _noop_db

    client = TestClient(app)

    # No header → 403
    res = client.get("/api/v1/admin/translations/review-queue")
    assert res.status_code == 403

    # Wrong token → 403
    res = client.get(
        "/api/v1/admin/translations/review-queue",
        headers={"X-Admin-Token": "wrong"},
    )
    assert res.status_code == 403

    # Right token → 200
    res = client.get(
        "/api/v1/admin/translations/review-queue",
        headers={"X-Admin-Token": "letmein-letmein"},
    )
    assert res.status_code == 200


# ── 9. Migration 0004: idempotency / DDL shape ──────────────────────────


def _load_migration(name: str):
    spec = importlib.util.spec_from_file_location(
        f"_mig_{name}", f"migrations/versions/{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_0004_metadata_and_idempotency():
    mod = _load_migration("0004_i18n_brand_translations")
    assert mod.revision == "0004_i18n_brand_translations"
    assert mod.down_revision == "0003_geofences"
    assert callable(mod.upgrade) and callable(mod.downgrade)

    with open("migrations/versions/0004_i18n_brand_translations.py") as fh:
        src = fh.read()
    # Idempotent guards everywhere.
    assert src.count("IF NOT EXISTS") >= 4  # table + 3 indexes
    assert "CREATE TABLE IF NOT EXISTS brand_translations" in src
    assert "idx_brand_translations_brand" in src
    assert "idx_brand_translations_locale" in src
    assert "idx_brand_translations_review_queue" in src
    # Partial-index predicate is what makes the queue scan cheap.
    assert "WHERE reviewed = FALSE AND auto_translated = TRUE" in src


# ── 10. Migration 0005: safe ALTERs that don't break existing rows ──────


def test_migration_0005_safe_alters():
    mod = _load_migration("0005_i18n_user_locale_pref")
    assert mod.revision == "0005_i18n_user_locale_pref"
    assert mod.down_revision == "0004_i18n_brand_translations"

    with open("migrations/versions/0005_i18n_user_locale_pref.py") as fh:
        src = fh.read()
    # ADD COLUMN IF NOT EXISTS for every new column — no existing row breakage.
    assert src.count("ADD COLUMN IF NOT EXISTS") >= 4
    for col in ("locale_pref", "region", "country_code", "timezone"):
        assert col in src
    # New columns must be nullable (no NOT NULL added) to preserve existing rows.
    assert "NOT NULL" not in src.split("def upgrade")[1].split("def downgrade")[0]


# ── 11. Migration 0006: ICU collation + trigram GIN index ───────────────


def test_migration_0006_icu_collation_and_trigram():
    mod = _load_migration("0006_i18n_collation")
    assert mod.revision == "0006_i18n_collation"
    assert mod.down_revision == "0005_i18n_user_locale_pref"

    with open("migrations/versions/0006_i18n_collation.py") as fh:
        src = fh.read()
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in src
    assert "CREATE COLLATION IF NOT EXISTS i18n_ci" in src
    assert "provider = icu" in src
    assert "und-u-ks-level2" in src
    assert "deterministic = false" in src
    # The GIN index reference is what makes the collation "usable in query".
    assert "USING GIN (value gin_trgm_ops)" in src
    assert "idx_brand_translations_value_ci" in src


# ── 12. ICU collation declared with correct ICU options ─────────────────


def test_icu_collation_query_shape_is_usable():
    """The collation DDL must be syntactically usable in a query.

    We don't execute against a real PG here (no ICU on CI runners), but
    we lock in the exact tokens so a regression is caught at lint time.
    The same DDL string is what would be applied via ``alembic upgrade``.
    """
    with open("migrations/versions/0006_i18n_collation.py") as fh:
        src = fh.read()
    # All three pieces required for a valid CREATE COLLATION ... ICU statement.
    for required in (
        "CREATE COLLATION IF NOT EXISTS i18n_ci",
        "provider = icu",
        "locale = 'und-u-ks-level2'",
        "deterministic = false",
    ):
        assert required in src, f"missing required ICU token: {required!r}"
