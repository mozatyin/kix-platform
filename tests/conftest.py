"""Pytest configuration & fixtures for the KiX Platform test suite.

Tests require a live Redis instance (matching ``REDIS_URL`` in the
environment, default ``redis://localhost:6379/0``). The ``clean_redis``
fixture flushes the database between tests so they are isolated.

Run with::

    pytest tests/ -v
"""

from __future__ import annotations

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


@pytest_asyncio.fixture(loop_scope="session")
async def client(redis_pool):
    """ASGI HTTPX client bound to the FastAPI app."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


@pytest_asyncio.fixture(loop_scope="session")
async def clean_redis(redis_pool):
    """Flush Redis before each test to guarantee isolation."""
    r = await get_redis()
    await r.flushdb()
    yield r
    # No teardown flush — leave state for ad-hoc inspection on failure.
