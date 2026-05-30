"""Pytest configuration & fixtures for the KiX Platform test suite.

Tests require a live Redis instance (matching ``REDIS_URL`` in the
environment, default ``redis://localhost:6379/0``). The ``clean_redis``
fixture flushes the database between tests so they are isolated.

Run with::

    pytest tests/ -v

# Isolation discipline

The full suite was historically flaky because individual tests mutated
two pieces of *process-wide* state without restoring them:

  1. **Redis** — keys written by one test leaked into the next.
  2. **``os.environ``** — e.g. ``KIX_ADMIN_TOKEN`` rewritten by one test
     made later admin-gated endpoints 403 because the new token didn't
     match the ``"admin-dev-token"`` fallback that other tests rely on.

To stop the bleeding *globally* (without touching every individual
test), this conftest now:

  * flushes the Redis DB once at session start (``_redis_session_clean``),
  * **snapshots ``os.environ`` before each test and restores it after**
    (``_env_isolation``, autouse),
  * and exposes ``--strict-isolation`` to optionally flush Redis before
    *every* test — catches future pollution early in CI.

See ``tests/CONFTEST.md`` for the full pollution catalogue and the
required cleanup discipline for new tests.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Make sure the project root is importable when pytest is invoked from
# inside the tests/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402
from app.redis_client import close_redis, get_redis, init_redis  # noqa: E402


# ── CLI options ──────────────────────────────────────────────────────────


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register isolation-related flags.

    By default the suite runs with **strict isolation**: every test
    starts against a freshly-flushed Redis DB. This is the only mode
    that produces a reliably-green suite — multiple parallel agents
    have shipped tests with overlapping Redis keys, and any shared
    state between tests will cause cross-pollination flakes.

    For local debugging where you *want* to inspect cross-test state
    (e.g. to reproduce a real production race), pass
    ``--allow-pollution`` to fall back to the legacy "flush only on
    ``clean_redis`` opt-in" behaviour.

    ``--strict-isolation`` is kept as an explicit opt-in alias for
    documentation / CI clarity — it's a no-op now that strict mode is
    the default.
    """
    parser.addoption(
        "--strict-isolation",
        action="store_true",
        default=False,
        help=(
            "Explicit alias for the default behaviour: flush Redis "
            "before every test. Kept for CI script clarity."
        ),
    )
    parser.addoption(
        "--allow-pollution",
        action="store_true",
        default=False,
        help=(
            "Disable the autouse per-test Redis flush. Tests will see "
            "state left over from previous tests — useful for local "
            "race-reproduction debugging only."
        ),
    )


# ── Session scaffolding ──────────────────────────────────────────────────


# pytest-asyncio v1.x: bind the session-scoped Redis pool to a single
# session-scoped loop so teardown isn't called after the loop closes.
@pytest.fixture(scope="session")
def anyio_backend() -> str:  # pragma: no cover — compatibility shim
    return "asyncio"


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def redis_pool():
    """Initialise the shared Redis pool once per test session."""
    await init_redis()
    yield
    await close_redis()


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def _redis_session_clean(redis_pool):
    """Flush Redis once at the start of the session.

    Autouse + session-scope: pytest-asyncio sets up the session event
    loop here, which the few sync tests that lean on
    ``asyncio.get_event_loop()`` (e.g. ``test_audit_log_pg::_run``)
    silently depend on after any async test has run earlier in the
    session.
    """
    r = await get_redis()
    await r.flushdb()
    yield


@pytest_asyncio.fixture(loop_scope="session")
async def client(request, redis_pool):
    """ASGI HTTPX client bound to the FastAPI app.

    Auto-flushes Redis before yielding the client unless
    ``--allow-pollution`` is passed. This is the single chokepoint
    through which every async router test reaches the app, so flushing
    here is equivalent to autouse isolation without disturbing the
    event-loop lifecycle for the small number of sync tests that don't
    request ``client``.
    """
    if not request.config.getoption("--allow-pollution"):
        await _drain_background_tasks()
        r = await get_redis()
        await r.flushdb()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


@pytest_asyncio.fixture(loop_scope="session")
async def clean_redis(request, redis_pool):
    """Flush Redis before each test to guarantee isolation.

    Now also drains pending fire-and-forget tasks from the *previous*
    test so their late writes can't land in this test's "clean" DB.
    """
    if not request.config.getoption("--allow-pollution"):
        await _drain_background_tasks()
    r = await get_redis()
    await r.flushdb()
    yield r
    # No teardown flush — leave state for ad-hoc inspection on failure.


# ── Auto-isolation: process-wide state ───────────────────────────────────


# Env vars that tests are known to mutate. Snapshotting + restoring the
# full ``os.environ`` is the safest default, but we explicitly track the
# known-polluted keys here for documentation and for assert-after-test
# checks below.
_KNOWN_MUTATED_ENV_VARS = (
    "KIX_ADMIN_TOKEN",
    "ANTHROPIC_API_KEY",
)


@pytest.fixture(autouse=True)
def _env_isolation():
    """Snapshot ``os.environ`` before each test and restore it after.

    Several tests historically wrote to ``os.environ`` directly (rather
    than using ``monkeypatch.setenv``) — most notoriously
    ``test_audit_log_pg::test_router_requires_admin_token`` which
    rewrites ``KIX_ADMIN_TOKEN`` to ``"test-token-xyz"``. Downstream
    tests fall back to ``"admin-dev-token"`` and 403 because the env
    they see is now mismatched.

    This autouse fixture snapshots-and-restores ``os.environ`` so a
    test's env mutations can never leak. Use ``monkeypatch.setenv`` in
    new tests for clarity, but this fixture is the backstop.
    """
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        # Restore: drop any keys the test added, reset any keys it
        # changed, re-insert any keys it deleted.
        current_keys = set(os.environ.keys())
        for k in current_keys - set(snapshot.keys()):
            os.environ.pop(k, None)
        for k, v in snapshot.items():
            if os.environ.get(k) != v:
                os.environ[k] = v


async def _drain_background_tasks() -> None:
    """Wait briefly for any fire-and-forget tasks to finish.

    Two ``sleep(0)`` rounds yield to the event loop so one-shot tasks
    can complete. We then wait on all *pending* tasks (other than
    ourselves) with a short timeout so we don't block on long-running
    background services. After this, ``FLUSHDB`` reliably wipes
    anything those tasks wrote.
    """
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    try:
        pending = [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        if pending:
            await asyncio.wait(pending, timeout=0.2)
    except RuntimeError:  # pragma: no cover — no running loop
        pass


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _strict_isolation(request, redis_pool):
    """Autouse pre-test cleanup for *async* tests (the default).

    Runs ``_drain_background_tasks()`` then ``FLUSHDB`` before every
    async test. ``client`` and ``clean_redis`` fixtures do this too as
    a defense-in-depth so a future test that doesn't depend on either
    still gets a clean DB. Sync tests don't request this (pytest-asyncio
    skips async-fixture wiring for non-async tests when they don't
    transitively need a loop), so the
    ``asyncio.get_event_loop()``-based ``test_audit_log_pg`` pattern
    stays intact.

    Pass ``--allow-pollution`` to skip — only useful for local
    debugging of cross-test races. CI must never set that flag.
    """
    if not request.config.getoption("--allow-pollution"):
        await _drain_background_tasks()
        r = await get_redis()
        await r.flushdb()
    yield
