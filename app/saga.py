"""Saga coordinator — distributed transaction with compensating actions.

Trinity-B P0: When refund cascades across modules (wallet refund →
attribution clean → commission reverse), each step is independent. If
step 3 fails after steps 1+2 succeed, state is inconsistent.

This coordinator implements the Saga pattern:

- For each step: execute action; on success record in journal + advance.
- On failure: run COMPENSATE on all previously-completed steps in
  REVERSE order. The saga either fully succeeds or fully rolls back.

Pre-defined sagas live in :mod:`app.saga_definitions` and the HTTP
surface lives in :mod:`app.routers.saga`.

Examples
--------
- refund_cascade: refund wallet → clean attribution → reverse commission
  Compensate: re-credit wallet → restore attribution → re-charge
  commission.
- subscription_upgrade: charge payment → upgrade tier → enable features
  Compensate: refund payment → downgrade tier → disable features.
- voucher_redeem: lock voucher → process order → finalize redeem
  Compensate: unlock voucher (since order didn't go through).

Redis schema
------------
    saga:{saga_id}:journal   LIST   (JSON event records, ordered)
    saga:{saga_id}:meta      HASH   (status, started_at, finished_at,
                                     completed_steps, failed_step,
                                     compensated_steps,
                                     compensation_failures)
    saga:index:failed_24h    ZSET   (score=finished_at, member=saga_id)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────
JOURNAL_TTL_SECONDS = 7 * 86400       # journal retained 7 days
META_TTL_SECONDS = 30 * 86400         # meta retained 30 days for audit
FAILED_INDEX_KEY = "saga:index:failed_24h"
DEFAULT_STEP_TIMEOUT = 30


# ── Key helpers ──────────────────────────────────────────────────────────
def _k_journal(saga_id: str) -> str:
    return f"saga:{saga_id}:journal"


def _k_meta(saga_id: str) -> str:
    return f"saga:{saga_id}:meta"


# ── Types ────────────────────────────────────────────────────────────────
SagaCallable = Callable[[dict, aioredis.Redis], Awaitable[Any]]


@dataclass
class SagaStep:
    """Single step in a saga.

    ``action`` and ``compensate`` are async callables that accept
    ``(context, redis)`` and return any JSON-serialisable value (or
    ``None``).  ``compensate`` MUST be idempotent — it may run multiple
    times if the coordinator crashes mid-rollback and is retried.
    """

    name: str
    action: SagaCallable
    compensate: SagaCallable
    timeout_seconds: int = DEFAULT_STEP_TIMEOUT


@dataclass
class SagaResult:
    saga_id: str
    success: bool
    completed_steps: list[str]
    failed_step: str | None
    failed_reason: str | None
    compensated_steps: list[str]
    compensation_failures: list[str]
    execution_time_ms: int
    context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "saga_id": self.saga_id,
            "success": self.success,
            "completed_steps": self.completed_steps,
            "failed_step": self.failed_step,
            "failed_reason": self.failed_reason,
            "compensated_steps": self.compensated_steps,
            "compensation_failures": self.compensation_failures,
            "execution_time_ms": self.execution_time_ms,
        }


# ── Coordinator ──────────────────────────────────────────────────────────
class SagaCoordinator:
    """Runs a saga end-to-end, persisting a journal in Redis."""

    def __init__(self, r: aioredis.Redis):
        self.r = r

    async def _record_journal(
        self,
        saga_id: str,
        kind: str,
        step_name: str,
        payload: Any,
    ) -> None:
        entry = {
            "kind": kind,
            "step": step_name,
            "payload": _safe_payload(payload),
            "ts": time.time(),
        }
        try:
            await self.r.rpush(_k_journal(saga_id), json.dumps(entry))
            await self.r.expire(_k_journal(saga_id), JOURNAL_TTL_SECONDS)
        except Exception as exc:  # pragma: no cover — log only
            logger.warning("saga journal write failed %s: %s", saga_id, exc)

    async def _write_meta(self, saga_id: str, fields: dict) -> None:
        try:
            mapping = {k: _stringify(v) for k, v in fields.items()}
            await self.r.hset(_k_meta(saga_id), mapping=mapping)
            await self.r.expire(_k_meta(saga_id), META_TTL_SECONDS)
        except Exception as exc:  # pragma: no cover
            logger.warning("saga meta write failed %s: %s", saga_id, exc)

    async def run(
        self,
        saga_id: str,
        steps: list[SagaStep],
        context: dict,
    ) -> SagaResult:
        if not steps:
            raise ValueError("saga must declare at least one step")

        started = time.time()
        await self._write_meta(
            saga_id,
            {
                "status": "running",
                "started_at": started,
                "num_steps": len(steps),
                "context_keys": ",".join(sorted(context.keys())),
            },
        )
        await self._record_journal(saga_id, "saga_started", "_init_", {})

        completed: list[SagaStep] = []

        for step in steps:
            try:
                result = await asyncio.wait_for(
                    step.action(context, self.r),
                    timeout=step.timeout_seconds,
                )
            except asyncio.TimeoutError:
                failed_reason = f"timeout_after_{step.timeout_seconds}s"
                return await self._fail_and_compensate(
                    saga_id, steps, completed, step, failed_reason,
                    context, started,
                )
            except Exception as exc:
                logger.warning(
                    "Saga %s step %s failed: %s",
                    saga_id, step.name, exc,
                )
                return await self._fail_and_compensate(
                    saga_id, steps, completed, step, repr(exc),
                    context, started,
                )

            await self._record_journal(
                saga_id, "step_completed", step.name, result,
            )
            completed.append(step)

        elapsed_ms = int((time.time() - started) * 1000)
        await self._write_meta(
            saga_id,
            {
                "status": "succeeded",
                "finished_at": time.time(),
                "execution_time_ms": elapsed_ms,
                "completed_steps": ",".join(s.name for s in completed),
            },
        )
        await self._record_journal(
            saga_id, "saga_succeeded", "_done_",
            {"execution_time_ms": elapsed_ms},
        )
        return SagaResult(
            saga_id=saga_id,
            success=True,
            completed_steps=[s.name for s in completed],
            failed_step=None,
            failed_reason=None,
            compensated_steps=[],
            compensation_failures=[],
            execution_time_ms=elapsed_ms,
            context=context,
        )

    async def _fail_and_compensate(
        self,
        saga_id: str,
        all_steps: list[SagaStep],
        completed: list[SagaStep],
        failed_step: SagaStep,
        failed_reason: str,
        context: dict,
        started: float,
    ) -> SagaResult:
        await self._record_journal(
            saga_id, "step_failed", failed_step.name,
            {"reason": failed_reason},
        )

        compensated: list[str] = []
        comp_failures: list[str] = []

        for comp_step in reversed(completed):
            try:
                await asyncio.wait_for(
                    comp_step.compensate(context, self.r),
                    timeout=comp_step.timeout_seconds,
                )
                compensated.append(comp_step.name)
                await self._record_journal(
                    saga_id, "step_compensated", comp_step.name, {},
                )
            except Exception as ce:
                logger.exception(
                    "Compensation failed for %s in saga %s: %s",
                    comp_step.name, saga_id, ce,
                )
                comp_failures.append(comp_step.name)
                await self._record_journal(
                    saga_id, "compensation_failed", comp_step.name,
                    {"reason": repr(ce)},
                )

        elapsed_ms = int((time.time() - started) * 1000)
        now = time.time()
        await self._write_meta(
            saga_id,
            {
                "status": (
                    "failed_rolled_back"
                    if not comp_failures
                    else "failed_compensation_failed"
                ),
                "finished_at": now,
                "execution_time_ms": elapsed_ms,
                "completed_steps": ",".join(s.name for s in completed),
                "failed_step": failed_step.name,
                "failed_reason": failed_reason,
                "compensated_steps": ",".join(compensated),
                "compensation_failures": ",".join(comp_failures),
            },
        )

        # Index in the failed-24h set so admin retry endpoint can find it.
        try:
            await self.r.zadd(FAILED_INDEX_KEY, {saga_id: now})
            # Trim to last 24h.
            await self.r.zremrangebyscore(
                FAILED_INDEX_KEY, 0, now - 86400,
            )
        except Exception:  # pragma: no cover
            pass

        await self._record_journal(
            saga_id, "saga_failed", "_done_",
            {
                "failed_step": failed_step.name,
                "compensation_failures": comp_failures,
                "execution_time_ms": elapsed_ms,
            },
        )

        return SagaResult(
            saga_id=saga_id,
            success=False,
            completed_steps=[s.name for s in completed],
            failed_step=failed_step.name,
            failed_reason=failed_reason,
            compensated_steps=compensated,
            compensation_failures=comp_failures,
            execution_time_ms=elapsed_ms,
            context=context,
        )

    async def get_status(self, saga_id: str) -> dict:
        """Retrieve saga meta + journal for debugging."""
        meta_raw = await self.r.hgetall(_k_meta(saga_id))
        if not meta_raw:
            return {"saga_id": saga_id, "found": False}
        journal_raw = await self.r.lrange(_k_journal(saga_id), 0, -1)
        journal: list[dict] = []
        for entry in journal_raw:
            try:
                journal.append(json.loads(entry))
            except Exception:
                journal.append({"raw": entry})
        return {
            "saga_id": saga_id,
            "found": True,
            "meta": meta_raw,
            "journal": journal,
        }


# ── Helpers ──────────────────────────────────────────────────────────────
def _safe_payload(value: Any) -> Any:
    """Best-effort JSON-safe view of a value."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def _stringify(v: Any) -> str:
    if isinstance(v, (str, int, float)):
        return str(v)
    if isinstance(v, bool):
        return "1" if v else "0"
    try:
        return json.dumps(v)
    except (TypeError, ValueError):
        return repr(v)
