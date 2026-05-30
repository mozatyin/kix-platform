"""Trinity 3T admin router.

HTTP surface for ``app.services.trinity_engine``. Long-running rounds are
exposed as async REST endpoints — kicking off a round returns immediately
with the ``iteration_id`` and the caller polls the round endpoint.

Auth is the standard ``KIX_ADMIN_TOKEN`` pre-shared key (query string OR
``X-Admin-Token`` header), matching every other admin router in the
platform (alpha_program, email_admin, tenant_admin).

Endpoints
---------
``POST /api/v1/trinity/iterate``                — start a new iteration
``GET  /api/v1/trinity/iteration/{id}``         — status + complaint list
``GET  /api/v1/trinity/iteration/{id}/round/{n}`` — one round result
``POST /api/v1/trinity/iteration/{id}/round``   — run the next round
``POST /api/v1/trinity/iteration/{id}/auto-fix``— dispatch fix-agents
``GET  /api/v1/trinity/leaderboard``            — list all iterations
``GET  /api/v1/trinity/personas``               — registered personas

Background execution
--------------------
``POST /iterate`` and ``POST /round`` accept ``async_mode=true`` to run
in the background via ``asyncio.create_task``. The response returns the
``iteration_id`` and a ``status_url`` for polling.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.security import constant_time_eq
from app.services.trinity_engine import (
    PERSONA_REGISTRY,
    Severity,
    TrinityIteration,
    get_persona,
    list_iterations,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── auth ────────────────────────────────────────────────────────────────


ADMIN_TOKEN_DEFAULT = "admin-dev-token"


def _check_admin(token: str | None) -> None:
    if not token:
        raise HTTPException(status_code=403, detail="admin_token_required")
    expected = os.getenv("KIX_ADMIN_TOKEN", ADMIN_TOKEN_DEFAULT)
    if not constant_time_eq(token, expected):
        raise HTTPException(status_code=403, detail="invalid_admin_token")


def _token_from_request(request: Request) -> str | None:
    qs = request.query_params.get("admin_token")
    if qs:
        return qs
    return request.headers.get("x-admin-token")


# ── schemas ──────────────────────────────────────────────────────────────


class IterateBody(BaseModel):
    persona: str = Field(..., description="persona slug — see /personas")
    artifact_path: str = Field(..., description="filesystem path to artifact under audit")
    target_quality: int = Field(7, ge=0, le=10)
    max_rounds: int = Field(5, ge=1, le=20)
    admin_token: str | None = None
    auto_run: bool = Field(
        False,
        description="When true, immediately run rounds in the background until convergence.",
    )


class AutoFixBody(BaseModel):
    admin_token: str | None = None
    severities: list[str] = Field(default_factory=lambda: ["P0"])
    max_tasks: int = 8


# ── endpoints ────────────────────────────────────────────────────────────


@router.get("/personas")
async def get_personas(request: Request) -> dict[str, Any]:
    """List registered personas (does not require auth — discovery surface)."""
    out = []
    for slug, factory in PERSONA_REGISTRY.items():
        p = factory()
        out.append(
            {
                "slug": p.slug,
                "label": p.label,
                "description": p.description,
                "industry_baselines": list(p.industry_baselines),
                "focus": list(p.focus),
            }
        )
    return {"personas": out}


@router.post("/iterate", status_code=status.HTTP_201_CREATED)
async def start_iteration(body: IterateBody, request: Request) -> dict[str, Any]:
    """Create a new Trinity iteration. Returns ``iteration_id`` immediately.

    When ``auto_run=true`` the engine runs rounds in the background until
    convergence; otherwise the caller drives each round via
    ``POST /iteration/{id}/round``.
    """
    token = body.admin_token or _token_from_request(request)
    _check_admin(token)

    if body.persona not in PERSONA_REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=f"unknown_persona; known={sorted(PERSONA_REGISTRY)}",
        )

    it = await TrinityIteration.create(
        persona=body.persona,
        artifact_path=body.artifact_path,
        target_quality=body.target_quality,
        max_rounds=body.max_rounds,
    )

    if body.auto_run:
        asyncio.create_task(_run_until_converged(it.iteration_id, body.max_rounds))

    return {
        "iteration_id": it.iteration_id,
        "persona": it.persona.slug,
        "artifact_path": it.artifact_path,
        "target_quality": it.target_quality,
        "max_rounds": it.max_rounds,
        "status_url": f"/api/v1/trinity/iteration/{it.iteration_id}",
        "auto_run": body.auto_run,
    }


async def _run_until_converged(iteration_id: str, max_rounds: int) -> None:
    """Background task — runs rounds until convergence or cap."""
    try:
        it = await TrinityIteration.resume(iteration_id)
        for _ in range(max_rounds):
            if await it.has_converged():
                break
            await it.round()
    except Exception:  # noqa: BLE001
        logger.exception("trinity auto-run failed for %s", iteration_id)


@router.get("/iteration/{iteration_id}")
async def get_iteration(iteration_id: str, request: Request) -> dict[str, Any]:
    token = _token_from_request(request)
    _check_admin(token)
    try:
        it = await TrinityIteration.resume(iteration_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="iteration_not_found")

    complaints = await it.list_complaints()
    verdict = await it.final_verdict()
    return {
        "verdict": verdict,
        "complaints": [c.to_json() for c in complaints],
    }


@router.get("/iteration/{iteration_id}/round/{n}")
async def get_round(iteration_id: str, n: int, request: Request) -> dict[str, Any]:
    token = _token_from_request(request)
    _check_admin(token)
    try:
        it = await TrinityIteration.resume(iteration_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="iteration_not_found")
    r = await it.get_round(n)
    if r is None:
        raise HTTPException(status_code=404, detail="round_not_found")
    return r.to_json()


@router.post("/iteration/{iteration_id}/round")
async def run_round(iteration_id: str, request: Request) -> dict[str, Any]:
    """Run one round synchronously and return its result."""
    token = _token_from_request(request)
    _check_admin(token)
    try:
        it = await TrinityIteration.resume(iteration_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="iteration_not_found")
    if await it.has_converged():
        raise HTTPException(status_code=409, detail="iteration_already_converged")
    result = await it.round()
    return result.to_json()


@router.post("/iteration/{iteration_id}/auto-fix")
async def auto_fix(
    iteration_id: str,
    body: AutoFixBody,
    request: Request,
) -> dict[str, Any]:
    """Dispatch fix-agents for complaints at the requested severities.

    Returns the prompts that would be sent to a downstream agent
    orchestrator (the router itself doesn't own the orchestrator).
    """
    token = body.admin_token or _token_from_request(request)
    _check_admin(token)
    try:
        it = await TrinityIteration.resume(iteration_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="iteration_not_found")

    sevs: list[Severity] = []
    for s in body.severities:
        try:
            sevs.append(Severity(s))
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"unknown_severity {s!r}; use P0/P1/P2"
            )

    prompts = await it.dispatch_autofix(
        dispatch_fn=None,
        severities=tuple(sevs),
        max_tasks=body.max_tasks,
    )
    return {
        "iteration_id": iteration_id,
        "dispatched_count": len(prompts),
        "prompts": prompts,
    }


@router.get("/leaderboard")
async def leaderboard(request: Request, limit: int = 50) -> dict[str, Any]:
    """List the N most-recently-touched iterations + their verdicts."""
    token = _token_from_request(request)
    _check_admin(token)
    return {"iterations": await list_iterations(limit=limit)}
