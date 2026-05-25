"""KiX Platform — FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.redis_client import close_redis, get_redis, init_redis, load_lua_scripts


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    # ── Startup ───────────────────────────────────────────────────────
    await init_redis()
    r = await get_redis()
    await load_lua_scripts(r)

    yield

    # ── Shutdown ──────────────────────────────────────────────────────
    await close_redis()


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    app = FastAPI(
        title="KiX Platform",
        version="5.0.0",
        lifespan=lifespan,
    )

    # ── CORS ──────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://play.kix.app",
            "http://localhost:3000",
            "http://localhost:8080",
            "http://localhost:5500",
            "http://127.0.0.1:5500",
            "null",  # file:// origin
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Routers ───────────────────────────────────────────────────────
    from app.routers import (
        auth,
        brands,
        energy,
        game,
        game_catalog,
        health,
        leaderboard,
        portal_auth,
        qr,
        reward,
        streak,
        users,
        vouchers,
    )

    app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
    app.include_router(game.router, prefix="/api/v1/game", tags=["game"])
    app.include_router(energy.router, prefix="/api/v1/energy", tags=["energy"])
    app.include_router(
        leaderboard.router, prefix="/api/v1/leaderboard", tags=["leaderboard"]
    )
    app.include_router(streak.router, prefix="/api/v1/streak", tags=["streak"])
    app.include_router(brands.router, prefix="/api/v1/brands", tags=["brands"])
    app.include_router(users.router, prefix="/api/v1/users", tags=["users"])
    app.include_router(
        vouchers.router, prefix="/api/v1/brands", tags=["vouchers"]
    )
    app.include_router(reward.router, prefix="/internal/reward", tags=["reward"])
    app.include_router(qr.router, prefix="/internal/qr", tags=["qr"])
    app.include_router(
        portal_auth.router, prefix="/api/v1/portal/auth", tags=["portal-auth"]
    )
    app.include_router(
        game_catalog.router,
        prefix="/api/v1/game-catalog",
        tags=["game-catalog"],
    )
    app.include_router(health.router, tags=["health"])

    # ── Root redirect to Landing Page ──────────────────────────────────
    @app.get("/")
    async def root_to_landing():
        return RedirectResponse(url="/landing/index.html")

    # ── Static files: Portal + generated games ──────────────────────────
    import os as _os
    _landing_dir = _os.path.join(_os.path.dirname(__file__), "..", "landing")
    if _os.path.isdir(_landing_dir):
        app.mount("/landing", StaticFiles(directory=_landing_dir), name="landing")

    return app


app = create_app()
