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

    # Load Recipe Library seed catalog into Redis
    try:
        from app.routers.recipes import load_seed_recipes
        await load_seed_recipes(r)
    except Exception as _exc:  # pragma: no cover — never fail startup
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "recipes seed load failed: %s", _exc
        )

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

    # ── Internal: ELTM callback ────────────────────────────────────────
    from app.routers import eltm_callback
    app.include_router(
        eltm_callback.router,
        prefix="/internal/eltm",
        tags=["internal-eltm"],
    )

    # ── Progression: XP/Levels/Badges/Daily Check-in ───────────────────
    from app.routers import progression
    app.include_router(progression.router, prefix="/api/v1/progression", tags=["progression"])

    # ── Layer 1 Primitives: Currency/Item/Achievement/Quest/Tier/Event ─
    from app.routers import primitives
    app.include_router(
        primitives.router, prefix="/api/v1/primitives", tags=["primitives"]
    )

    # ── Network Effect: viral growth triggers ──────────────────────────
    from app.routers import network_effect
    app.include_router(
        network_effect.router,
        prefix="/api/v1/network",
        tags=["network-effect"],
    )

    # ── Brand Modules: gamification module marketplace + config ────────
    from app.routers import brand_modules
    app.include_router(
        brand_modules.router, prefix="/api/v1", tags=["brand-modules"]
    )

    # ── Commerce Loop Engine: ScoreToCoupon / Energy / Upsell / Store ──
    from app.routers import commerce_loop
    app.include_router(
        commerce_loop.router, prefix="/api/v1/commerce", tags=["commerce"]
    )

    # ── Composable Gamification Modules (10 mechanics) ─────────────────
    from app.routers import modules as gamif_modules
    app.include_router(
        gamif_modules.router, prefix="/api/v1/modules", tags=["modules"]
    )

    # ── Rule Engine: When-Then composition across all modules ──────────
    from app.routers import rule_engine
    app.include_router(
        rule_engine.router, prefix="/api/v1/rules", tags=["rule-engine"]
    )

    # ── Group Actions: Pinduoduo-style viral mechanics (拼团/砍一刀) ────
    from app.routers import group_actions
    app.include_router(
        group_actions.router, prefix="/api/v1/groups", tags=["groups"]
    )

    # ── Voucher Builder / Social / Triggers ────────────────────────────
    from app.routers import voucher_builder, social, triggers
    app.include_router(
        voucher_builder.router,
        prefix="/api/v1/vouchers",
        tags=["voucher-builder"],
    )
    app.include_router(social.router, prefix="/api/v1/social", tags=["social"])
    app.include_router(
        triggers.router, prefix="/api/v1/triggers", tags=["triggers"]
    )

    # ── P2P transfer: GiftSending + TradingPost ────────────────────────
    from app.routers import p2p
    app.include_router(p2p.router, prefix="/api/v1/p2p", tags=["p2p"])

    # ── Cooperative multiplayer: Quest / Raid / Squad / Territory ─────
    from app.routers import multiplayer
    app.include_router(
        multiplayer.router, prefix="/api/v1/multiplayer", tags=["multiplayer"]
    )

    # ── Recipe Library: pre-built gamification blueprints ──────────────
    from app.routers import recipes
    app.include_router(
        recipes.router, prefix="/api/v1/recipes", tags=["recipes"]
    )

    # ── Recipe Generator: NL → Recipe (LLM-driven) ─────────────────────
    from app.routers import recipe_generator
    app.include_router(
        recipe_generator.router,
        prefix="/api/v1/recipe-gen",
        tags=["recipe-generator"],
    )

    # ── Tutorials: Recipe → step-by-step guided Portal setup ───────────
    from app.routers import tutorials
    app.include_router(
        tutorials.router, prefix="/api/v1/tutorials", tags=["tutorials"]
    )

    # ── Conditions Engine: unified gating across all gamification ──────
    from app.routers import conditions
    app.include_router(
        conditions.router, prefix="/api/v1/conditions", tags=["conditions"]
    )

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
