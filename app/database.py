"""Async SQLAlchemy engine and session factory for KiX Platform.

Connection topology
-------------------

* ``write_engine`` — primary read/write engine. Sized by
  ``settings.db_pool_size`` (default 50) + ``settings.db_max_overflow``
  (default 100) → up to 150 concurrent connections.
* ``read_engine`` — optional read-replica engine. Used by
  :func:`get_read_db` for endpoints that only read.  When
  ``settings.database_read_url`` is not set the read engine is an alias
  for the write engine (single-node fallback).

Both engines enable ``pool_pre_ping`` and ``pool_recycle`` so stale
connections from idle pools or replica failovers are discarded
transparently.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings


# ── Engine factory ────────────────────────────────────────────────────────


def _build_engine(url: str) -> AsyncEngine:
    """Create an async engine with the standard pool tuning."""
    return create_async_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=settings.db_pool_pre_ping,
    )


# ── Primary (read/write) engine ───────────────────────────────────────────

write_engine: AsyncEngine = _build_engine(settings.database_url)

# Back-compat alias — older modules import ``engine`` directly.
engine: AsyncEngine = write_engine


# ── Read-replica engine (optional) ────────────────────────────────────────

if settings.database_read_url:
    read_engine: AsyncEngine = _build_engine(settings.database_read_url)
    _has_replica = True
else:
    read_engine = write_engine  # fallback: single-node deployment
    _has_replica = False


# ── Session factories ─────────────────────────────────────────────────────

async_session_factory = async_sessionmaker(
    write_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

read_session_factory = async_sessionmaker(
    read_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ── FastAPI dependencies ──────────────────────────────────────────────────


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async DB session (read/write).

    Commits on success, rolls back on exception.
    """
    session = async_session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_read_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a read-only async DB session.

    Routes through the read replica when configured; otherwise falls
    back to the primary engine. Read sessions are never committed —
    any accidental writes are rolled back on cleanup.
    """
    session = read_session_factory()
    try:
        yield session
        # Read-only: explicitly roll back to release any implicit txn
        # without flushing changes.
        await session.rollback()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


# ── Pool / replica observability helpers ──────────────────────────────────


def _pool_stats(eng: AsyncEngine) -> dict[str, Any]:
    """Return a JSON-friendly snapshot of an engine's pool state.

    Returned keys:
        size      — configured ``pool_size``
        checked_in    — idle connections in the pool right now
        checked_out   — connections currently lent out
        overflow      — connections beyond ``pool_size`` (-1 means
                        none open; otherwise the count of open
                        overflow connections)
        max_overflow  — configured overflow ceiling
        total_capacity — ``pool_size + max_overflow``
    """
    pool = eng.pool
    # SQLAlchemy ``QueuePool`` (the asyncpg default) exposes these.
    size = getattr(pool, "size", lambda: None)()
    checked_in = getattr(pool, "checkedin", lambda: None)()
    checked_out = getattr(pool, "checkedout", lambda: None)()
    overflow = getattr(pool, "overflow", lambda: None)()
    return {
        "size": size,
        "checked_in": checked_in,
        "checked_out": checked_out,
        "overflow": overflow,
        "max_overflow": settings.db_max_overflow,
        "total_capacity": settings.db_pool_size + settings.db_max_overflow,
    }


def pool_stats() -> dict[str, Any]:
    """Public snapshot of write + read pool states."""
    return {
        "write": _pool_stats(write_engine),
        "read": _pool_stats(read_engine) if _has_replica else None,
        "replica_configured": _has_replica,
    }


def has_read_replica() -> bool:
    """Return ``True`` when a distinct read-replica engine is active."""
    return _has_replica
