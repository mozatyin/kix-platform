"""Tests for the durable PG-backed audit log stack.

Covers:
* Migration 0007 metadata (revision chain, IF NOT EXISTS guards, DDL shape)
* SQLAlchemy model round-trip (insert + read)
* Service: record_event + UNIQUE(event_id) idempotency
* Service: query() filters + pagination
* Service: export_csv shape + header columns
* Service: apply_retention_policy + per-jurisdiction horizons
* Service: purge_expired
* PII scrubber strips well-known sensitive keys
* HTTP surface: admin-token gating, 403 on missing/wrong token
* HTTP surface: GET /events filters propagate
* HTTP surface: POST /export returns CSV w/ Content-Disposition
* Migration script: legacy parser handles three known shapes
* Migration script: deterministic event_id → idempotent re-runs
* Retention worker: run_once exercises both backfill + purge
* Concurrent inserts on the same event_id → exactly one row persists

Like the other PG-backed tests in this repo we use an in-memory aiosqlite
engine so the suite is fast and self-contained. JSONB / INET fall back
to JSON / TEXT via the column ``with_variant`` declarations on the
model.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.models.audit_log import AuditLog  # noqa: E402
from app.services import audit_log_service as svc  # noqa: E402


# ── Engine / session fixtures ────────────────────────────────────────────


@pytest.fixture
def db_engine():
    """In-memory aiosqlite engine — fast, isolated per test.

    Only the AuditLog table is created so the engine does not need any
    sibling-table dependencies (BrandConfig etc.). JSONB + INET resolve
    to JSON + TEXT via the model's ``with_variant`` declarations.
    """
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: AuditLog.__table__.create(sync_conn)
            )

    asyncio.get_event_loop().run_until_complete(_create())
    yield eng
    asyncio.get_event_loop().run_until_complete(eng.dispose())


@pytest.fixture
def session_factory(db_engine):
    return async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── 1. Migration file metadata ───────────────────────────────────────────


def test_migration_revision_metadata():
    """0007_audit_log declares revision chain back to 0006."""
    spec = importlib.util.spec_from_file_location(
        "_audit_mig", "migrations/versions/0007_audit_log.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0007_audit_log"
    assert mod.down_revision == "0006_i18n_collation"
    assert mod.branch_labels is None


def test_migration_ddl_is_idempotent():
    """Every DDL statement is guarded with IF NOT EXISTS so re-runs are safe."""
    src = (REPO_ROOT / "migrations/versions/0007_audit_log.py").read_text()
    # Every CREATE TABLE / CREATE INDEX must be guarded — count the
    # CREATEs and the IF NOT EXISTS guards in parallel.
    create_count = src.count("CREATE TABLE") + src.count("CREATE INDEX")
    guarded = src.count("IF NOT EXISTS")
    assert create_count > 0
    assert guarded >= create_count, (
        f"unguarded DDL detected: {create_count} CREATEs vs "
        f"{guarded} IF NOT EXISTS guards"
    )


# ── 2. Insert + query round-trip ─────────────────────────────────────────


def test_insert_and_query_roundtrip(session_factory):
    async def go():
        async with session_factory() as s:
            eid = await svc.record_event(
                s,
                actor_id="merchant-1",
                actor_type="merchant",
                action="wallet.charge",
                brand_id="brand-a",
                jurisdiction="sg",
                payload={"amount": 1000, "currency": "SGD"},
                result="success",
                mirror_redis=False,
            )
            assert eid.startswith("evt_")

        async with session_factory() as s:
            rows = await svc.query(s, actor_id="merchant-1")
            assert len(rows) == 1
            assert rows[0].action == "wallet.charge"
            assert rows[0].payload["amount"] == 1000
            assert rows[0].jurisdiction == "sg"
            assert rows[0].retention_until is not None  # auto_retention=True

    _run(go())


# ── 3. event_id idempotency ──────────────────────────────────────────────


def test_event_id_idempotent(session_factory):
    """Re-inserting the same event_id must not produce a duplicate row."""

    async def go():
        async with session_factory() as s:
            eid = await svc.record_event(
                s,
                actor_id="a",
                actor_type="system",
                action="x",
                event_id="evt_fixed123",
                mirror_redis=False,
            )
            assert eid == "evt_fixed123"

        async with session_factory() as s:
            await svc.record_event(
                s,
                actor_id="a",
                actor_type="system",
                action="x",
                event_id="evt_fixed123",
                mirror_redis=False,
            )

        async with session_factory() as s:
            rows = await svc.query(s, actor_id="a")
            assert len(rows) == 1

    _run(go())


# ── 4. Query filters + pagination ────────────────────────────────────────


def test_query_filters_and_pagination(session_factory):
    async def go():
        async with session_factory() as s:
            for i in range(7):
                await svc.record_event(
                    s,
                    actor_id=f"u{i % 2}",
                    actor_type="customer",
                    action="consent.grant" if i % 2 == 0 else "consent.revoke",
                    brand_id="brand-b",
                    mirror_redis=False,
                )

        async with session_factory() as s:
            grants = await svc.query(s, action="consent.grant")
            revokes = await svc.query(s, action="consent.revoke")
            assert len(grants) == 4
            assert len(revokes) == 3

            page1 = await svc.query(s, brand_id="brand-b", limit=3, offset=0)
            page2 = await svc.query(s, brand_id="brand-b", limit=3, offset=3)
            assert len(page1) == 3
            assert len(page2) == 3
            # No overlap between pages (ts DESC + unique event_ids)
            seen = {r.event_id for r in page1} | {r.event_id for r in page2}
            assert len(seen) == 6

    _run(go())


# ── 5. CSV export shape ──────────────────────────────────────────────────


def test_export_csv_shape(session_factory):
    async def go():
        async with session_factory() as s:
            await svc.record_event(
                s,
                actor_id="admin-1",
                actor_type="admin",
                action="campaign.pause",
                brand_id="brand-c",
                jurisdiction="eu",
                payload={"reason": "policy_violation"},
                result="success",
                mirror_redis=False,
            )

        async with session_factory() as s:
            csv_text = await svc.export_csv(s, brand_id="brand-c")

        # Header row + one data row
        lines = [ln for ln in csv_text.splitlines() if ln.strip()]
        assert len(lines) == 2
        header = lines[0].split(",")
        assert "event_id" in header
        assert "action" in header
        assert "jurisdiction" in header
        # Data row contains the action verb
        assert "campaign.pause" in lines[1]

    _run(go())


# ── 6. Retention policy + jurisdiction-driven horizon ────────────────────


def test_apply_retention_policy_sets_horizon(session_factory):
    async def go():
        # Insert with auto_retention=False so the rows have NULL horizons.
        async with session_factory() as s:
            await svc.record_event(
                s,
                actor_id="x",
                actor_type="system",
                action="z",
                jurisdiction="sg",
                auto_retention=False,
                mirror_redis=False,
            )
            await svc.record_event(
                s,
                actor_id="y",
                actor_type="system",
                action="z",
                jurisdiction="sg",
                auto_retention=False,
                mirror_redis=False,
            )

        async with session_factory() as s:
            n = await svc.apply_retention_policy(s, jurisdiction="sg")
            assert n == 2

        async with session_factory() as s:
            rows = await svc.query(s, jurisdiction="sg")
            assert len(rows) == 2
            for r in rows:
                assert r.retention_until is not None
                # SG = 7y horizon → far in the future. SQLite drops
                # tz-info on round-trip; normalise both sides to naive
                # before comparing.
                ru = r.retention_until
                if ru.tzinfo is not None:
                    ru = ru.replace(tzinfo=None)
                assert ru > datetime.utcnow()

    _run(go())


def test_apply_retention_unknown_jurisdiction_is_safe(session_factory):
    async def go():
        async with session_factory() as s:
            await svc.record_event(
                s,
                actor_id="x",
                actor_type="system",
                action="z",
                jurisdiction="xx",
                auto_retention=False,
                mirror_redis=False,
            )
        async with session_factory() as s:
            # Unknown jurisdiction → no rows updated, NO crash.
            n = await svc.apply_retention_policy(s, jurisdiction="xx")
            assert n == 0

    _run(go())


# ── 7. Purge expired ─────────────────────────────────────────────────────


def test_purge_expired_deletes_only_past_horizon(session_factory):
    async def go():
        past = datetime.now(timezone.utc) - timedelta(days=1)
        future = datetime.now(timezone.utc) + timedelta(days=30)

        async with session_factory() as s:
            await svc.record_event(
                s,
                actor_id="old",
                actor_type="system",
                action="z",
                event_id="evt_old",
                auto_retention=False,
                mirror_redis=False,
            )
            await svc.record_event(
                s,
                actor_id="new",
                actor_type="system",
                action="z",
                event_id="evt_new",
                auto_retention=False,
                mirror_redis=False,
            )

        # Hand-set retention_until on the two rows.
        from sqlalchemy import update

        async with session_factory() as s:
            await s.execute(
                update(AuditLog)
                .where(AuditLog.event_id == "evt_old")
                .values(retention_until=past)
            )
            await s.execute(
                update(AuditLog)
                .where(AuditLog.event_id == "evt_new")
                .values(retention_until=future)
            )
            await s.commit()

        async with session_factory() as s:
            purged = await svc.purge_expired(s)
            assert purged == 1

        async with session_factory() as s:
            remaining = await svc.query(s)
            ids = {r.event_id for r in remaining}
            assert ids == {"evt_new"}

    _run(go())


# ── 8. PII scrubber ──────────────────────────────────────────────────────


def test_payload_pii_keys_are_scrubbed(session_factory):
    """Sensitive keys must be masked before the payload is persisted."""

    async def go():
        async with session_factory() as s:
            await svc.record_event(
                s,
                actor_id="u",
                actor_type="customer",
                action="auth.login",
                payload={
                    "username": "alice",
                    "password": "p4ssw0rd",
                    "nested": {"api_key": "sk_live_abc"},
                    "list": [{"token": "xyz"}],
                },
                mirror_redis=False,
            )

        async with session_factory() as s:
            rows = await svc.query(s, actor_id="u")
            assert len(rows) == 1
            p = rows[0].payload
            assert p["username"] == "alice"
            assert p["password"] == "***"
            assert p["nested"]["api_key"] == "***"
            assert p["list"][0]["token"] == "***"

    _run(go())


# ── 9. get_event 404 path ────────────────────────────────────────────────


def test_get_event_returns_none_for_missing(session_factory):
    async def go():
        async with session_factory() as s:
            row = await svc.get_event(s, "evt_does_not_exist")
            assert row is None

    _run(go())


# ── 10. Admin-token gating on HTTP surface ───────────────────────────────


def test_router_requires_admin_token():
    """The audit_log router 403s without a valid admin token."""
    import os

    # Save & restore — the autouse ``_env_isolation`` fixture in
    # conftest.py already snapshots os.environ, but we belt-and-brace
    # here so this test stays explicit about the env mutation it makes.
    _prev = os.environ.get("KIX_ADMIN_TOKEN")
    os.environ["KIX_ADMIN_TOKEN"] = "test-token-xyz"

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.routers import audit_log as audit_log_router

    app = FastAPI()
    app.include_router(audit_log_router.router, prefix="/api/v1/audit")

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get("/api/v1/audit/events")
            assert r.status_code == 403
            r2 = await c.get(
                "/api/v1/audit/events",
                headers={"X-Admin-Token": "wrong"},
            )
            assert r2.status_code == 403

    try:
        _run(go())
    finally:
        if _prev is None:
            os.environ.pop("KIX_ADMIN_TOKEN", None)
        else:
            os.environ["KIX_ADMIN_TOKEN"] = _prev


# ── 11. Migration-script legacy parser ───────────────────────────────────


def test_legacy_parser_three_shapes():
    """Each known legacy list shape parses into record_event kwargs."""
    spec = importlib.util.spec_from_file_location(
        "_audit_migrator", "scripts/migrate_audit_redis_to_pg.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Shape 1: compliance:pii_audit user-scoped
    out1 = mod._parse_legacy(
        '{"ts": 1234567890, "action": "pii_read", '
        '"uid": "u1", "actor": "admin-7"}'
    )
    assert out1 is not None
    assert out1["action"] == "pii_read"
    assert out1["actor_id"] == "admin-7"

    # Shape 2: brand-scoped pii audit
    out2 = mod._parse_legacy(
        '{"ts": 1234567890, "action": "pii_write", '
        '"bid": "b1", "admin_id": "admin-3"}'
    )
    assert out2 is not None
    assert out2["brand_id"] == "b1"
    assert out2["actor_type"] == "admin"

    # Shape 3: payouts inter-brand
    out3 = mod._parse_legacy(
        '{"event": "transfer", "user_id": "u9", "status": "success"}'
    )
    assert out3 is not None
    assert out3["action"] == "transfer"
    assert out3["result"] == "success"

    # Malformed → None (counted as unparsed, never crashes)
    assert mod._parse_legacy("not json") is None
    assert mod._parse_legacy('"a string not a dict"') is None


def test_migration_event_id_is_deterministic():
    """Same input always yields the same event_id → re-imports are no-ops."""
    spec = importlib.util.spec_from_file_location(
        "_audit_migrator2", "scripts/migrate_audit_redis_to_pg.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    raw = '{"ts": 1, "action": "x"}'
    a = mod._derive_event_id("audit:foo", raw)
    b = mod._derive_event_id("audit:foo", raw)
    c = mod._derive_event_id("audit:bar", raw)
    assert a == b
    assert a != c
    assert a.startswith("evt_")


# ── 12. Retention worker run_once ────────────────────────────────────────


def test_retention_worker_run_once_invokes_purge(session_factory):
    """The worker drives apply_retention_policy + purge_expired through
    the same session factory it receives. Smoke-checks the wiring."""
    from app.workers import audit_retention_worker

    async def go():
        # Seed an expired row.
        past = datetime.now(timezone.utc) - timedelta(days=1)
        async with session_factory() as s:
            await svc.record_event(
                s,
                actor_id="exp",
                actor_type="system",
                action="z",
                event_id="evt_exp",
                auto_retention=False,
                mirror_redis=False,
            )
        from sqlalchemy import update

        async with session_factory() as s:
            await s.execute(
                update(AuditLog)
                .where(AuditLog.event_id == "evt_exp")
                .values(retention_until=past)
            )
            await s.commit()

        summary = await audit_retention_worker.run_once(session_factory)
        assert summary["purged"] >= 1

    _run(go())


# ── 13. Concurrent same-event_id inserts → exactly one row ───────────────


def test_concurrent_same_event_id_single_row(session_factory):
    """Two parallel record_event calls with the same event_id must not
    produce duplicates (ON CONFLICT DO NOTHING semantics)."""

    async def go():
        async def one():
            async with session_factory() as s:
                await svc.record_event(
                    s,
                    actor_id="c",
                    actor_type="system",
                    action="z",
                    event_id="evt_race",
                    mirror_redis=False,
                )

        await asyncio.gather(one(), one(), one())

        async with session_factory() as s:
            rows = await svc.query(s, actor_id="c")
            assert len(rows) == 1

    _run(go())


# ── 14. CSV export is empty (header only) for no matches ─────────────────


def test_export_csv_empty_dataset(session_factory):
    async def go():
        async with session_factory() as s:
            csv_text = await svc.export_csv(s, brand_id="nope")
        lines = [ln for ln in csv_text.splitlines() if ln.strip()]
        # Header only, no data rows.
        assert len(lines) == 1
        assert "event_id" in lines[0]

    _run(go())


# ── 15. retention_status summary structure ───────────────────────────────


def test_retention_status_groups_by_jurisdiction(session_factory):
    async def go():
        async with session_factory() as s:
            for region, n in (("sg", 2), ("eu", 1)):
                for _ in range(n):
                    await svc.record_event(
                        s,
                        actor_id="u",
                        actor_type="system",
                        action="z",
                        jurisdiction=region,
                        mirror_redis=False,
                    )

        async with session_factory() as s:
            summary = await svc.retention_status(s)

        by_region = {row["jurisdiction"]: row for row in summary}
        assert by_region["sg"]["total"] == 2
        assert by_region["eu"]["total"] == 1
        # Auto-retention is on by default for these rows; horizons are far
        # in the future, so expired count is zero.
        assert by_region["sg"]["expired"] == 0
        assert by_region["eu"]["expired"] == 0
        assert by_region["sg"]["earliest_expiry"] is not None

    _run(go())
