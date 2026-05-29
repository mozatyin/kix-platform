"""KiX Platform — FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
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
        title="KiX Platform API",
        description="""
KiX = TikTok Ads for Gamification.

## Quick Start

1. Register at [partner.letskix.com](https://partner.letskix.com)
2. Use `/api/v1/wallet/{brand_id}/topup` to fund your account
3. Create campaign via `/api/v1/campaigns/create`
4. Default `target_audience=new_users_only` — pay only for NEW user acquisition
5. Track conversions via Pixel SDK or `/api/v1/attribution/track/conversion`

## Key Concepts
- **Brand**: Your business (one or more stores = a master account)
- **kid**: Universal KiX user identifier (kid_xxx)
- **Auction**: Quality-adjusted Vickrey GSP — bids ranked by `bid × quality × pacing`
- **Push**: KiX algorithm-driven smart push, you pay only on delivery

## Modules (5 categories)

- **Gamification core**: 17 routers managing your existing users (FREE)
- **Ad Platform**: 10 routers for buying NEW users (PAID, auction-based)
- **Identity (KiX ID)**: Universal user identity + OAuth Connect
- **Push Engine**: Right-time/place/user delivery
- **Master Account**: Multi-store hierarchy + RBAC

## API Standards (5 invariants)

Every public endpoint in this API obeys 5 invariants. Full spec lives in
[`API_STANDARDS.md`](https://github.com/mozat/kix-platform/blob/main/API_STANDARDS.md)
and the shim helpers in `app.api_standards`.

1. **ID format** — `<prefix>_<22-char-hex>` (e.g. `cmp_8f3a1c…`,
   `kid_2b71e0…`). Stable prefixes: `acct_ user_ kid_ ent_ lst_ ofr_ med_
   cmp_ adg_ bdg_ qst_ vid_ res_ led_ inc_ tx_ sub_ pm_ dpt_ prt_ crv_`.
2. **Timestamps** — Unix integer seconds, UTC. All `*_at` fields are `int`.
3. **Error envelope** — `{"detail": {"error": "<code>", "message": "...",
   ...context}}`. Pattern-match on `error`, not the message string.
4. **List responses** — `{items, count, total, has_more, limit, offset}`.
5. **HTTP method semantics** — `POST` 201 (create) / 200 (action), `PUT`
   200, `PATCH` 200, `DELETE` 204, `GET` 200.
""",
        version="5.0.0",
        contact={
            "name": "KiX Platform Team",
            "url": "https://partner.letskix.com",
            "email": "partners@letskix.com",
        },
        license_info={
            "name": "KiX Partner Agreement",
            "url": "https://partner.letskix.com/terms",
        },
        openapi_tags=[
            {"name": "Gamification - Progression", "description": "XP, Levels, Badges, Streaks, Daily Check-in"},
            {"name": "Gamification - Primitives", "description": "Currency, Items, Achievements, Quests, Tiers"},
            {"name": "Gamification - Modules", "description": "10 composable top-level modules"},
            {"name": "Ad Platform - Auction", "description": "Quality-adjusted Vickrey GSP auction"},
            {"name": "Ad Platform - Campaigns", "description": "Campaign + AdGroup + Quality Score"},
            {"name": "Ad Platform - Attribution", "description": "7-day last-touch + multi-touch + view-through"},
            {"name": "Ad Platform - Wallet", "description": "Merchant balance, top-up, charge, refund"},
            {"name": "Ad Platform - Audiences", "description": "Custom + Lookalike + Recency filters"},
            {"name": "Ad Platform - Pixel", "description": "JS SDK for conversion tracking"},
            {"name": "Identity - KiX ID", "description": "Universal user identity + OAuth Connect"},
            {"name": "Identity - Consent", "description": "GDPR/PIPL consent management"},
            {"name": "Push Engine", "description": "Smart push delivery with auction integration"},
            {"name": "Geofence + LBS", "description": "Location-based discovery"},
            {"name": "Master + Multi-Store", "description": "Multi-store hierarchy + RBAC"},
            {"name": "Payments", "description": "Payouts, settlement, invoicing"},
            {"name": "Frequency Cap", "description": "Anti-burnout + pacing"},
            {"name": "Vouchers", "description": "Issue, redeem, transfer with relational predicates"},
            {"name": "Reservations", "description": "Future-dated commitments + no-show recovery"},
            {"name": "Disputes", "description": "Merchant complaint + refund workflow"},
            {"name": "Storefront", "description": "Public brand profile + discover"},
            {"name": "Partnerships", "description": "OPTIONAL: joint campaigns (advanced, rarely needed)"},
        ],
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
    # Cross-store voucher lifecycle (issue / redeem / transfer / void /
    # master network configuration). Lives in the same module as the
    # legacy brand-pool router but mounts at the global /api/v1/vouchers
    # prefix.
    app.include_router(
        vouchers.cross_store_router,
        prefix="/api/v1/vouchers",
        tags=["vouchers-cross-store"],
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

    # ── Attribution: invite tokens, 7-day last-touch, anti-fraud ───────
    from app.routers import attribution
    app.include_router(
        attribution.router,
        prefix="/api/v1/attribution",
        tags=["attribution"],
    )

    # ── Merchant Wallet + Billing: topup / charge / refund / forecast ──
    from app.routers import wallet
    app.include_router(
        wallet.router, prefix="/api/v1/wallet", tags=["wallet"]
    )

    # ── Disputes + Refund: merchant challenges fraud/fake conversions ──
    from app.routers import disputes
    app.include_router(
        disputes.router, prefix="/api/v1/disputes", tags=["disputes"]
    )

    # ── Fraud / AML / Trust-Score / Incidents (cross-brand spine) ──────
    from app.routers import fraud
    app.include_router(
        fraud.router, prefix="/api/v1/fraud", tags=["fraud"]
    )

    # ── Geofence: Location-Based Discovery (store geo + push triggers) ─
    from app.routers import geofence
    app.include_router(
        geofence.router, prefix="/api/v1/geofence", tags=["geofence"]
    )

    # ── Consent / Privacy: GDPR/PIPL/Indonesia-PDP legal spine ─────────
    from app.routers import consent
    app.include_router(
        consent.router, prefix="/api/v1/consent", tags=["consent"]
    )

    # ── Compliance: ad-creative scanner + sensitive-PI audit (PIPL §51) ─
    from app.routers import compliance
    app.include_router(
        compliance.router,
        prefix="/api/v1/compliance",
        tags=["compliance"],
    )

    # ── Media: regulated-data registry (PHI / KYC / 双录 / biometric) ──
    from app.routers import media
    app.include_router(
        media.router, prefix="/api/v1/media", tags=["media"]
    )

    # ── Moderation: image safety + LLM text scan + human review queue ──
    from app.routers import moderation
    app.include_router(
        moderation.router,
        prefix="/api/v1/moderation",
        tags=["moderation"],
    )

    # ── Campaign Manager + Auction Engine (Google-Ads-style) ───────────
    from app.routers import campaigns, auction
    app.include_router(
        campaigns.router, prefix="/api/v1/campaigns", tags=["campaigns"]
    )
    app.include_router(
        auction.router, prefix="/api/v1/auction", tags=["auction"]
    )

    # ── Audiences: Custom + Lookalike for retargeting / exclusion ──────
    from app.routers import audiences
    app.include_router(
        audiences.router, prefix="/api/v1/audiences", tags=["audiences"]
    )

    # ── Frequency Cap + Pacing: protect users from ad burn-out ─────────
    from app.routers import frequency_cap
    app.include_router(
        frequency_cap.router,
        prefix="/api/v1/frequency-cap",
        tags=["frequency-cap"],
    )

    # ── Conversion Pixel: GA-style JS pixel for merchant websites ──────
    from app.routers import pixel
    app.include_router(
        pixel.router, prefix="/api/v1/pixel", tags=["pixel"]
    )

    # ── Conversions API (CAPI): server-to-server pixel-parity ingestion ─
    from app.routers import capi
    app.include_router(
        capi.router, prefix="/api/v1/capi", tags=["capi"]
    )

    # ── Payouts & Settlement: bank accounts, payouts, invoices, cron ───
    from app.routers import payouts
    app.include_router(
        payouts.router, prefix="/api/v1/payouts", tags=["payouts"]
    )

    # ── FX Engine: multi-currency rate store + conversion ──────────────
    from app.routers import fx
    app.include_router(
        fx.router, prefix="/api/v1/fx", tags=["fx"]
    )

    # ── Creative Generator: ELTM-powered on-demand HTML game creatives ─
    from app.routers import creative_gen
    app.include_router(
        creative_gen.router,
        prefix="/api/v1/creative-gen",
        tags=["creative_gen"],
    )

    # ── Master Accounts + RBAC (corp-level ownership + per-role scopes) ─
    from app.routers import master_accounts
    app.include_router(
        master_accounts.router,
        prefix="/api/v1/master",
        tags=["master_accounts"],
    )

    # ── Storefront: public brand profile pages + reviews + discover ────
    from app.routers import storefront
    app.include_router(
        storefront.router,
        prefix="/api/v1/storefront",
        tags=["storefront"],
    )

    # ── Partnerships: cross-brand voucher / joint-campaign agreements ──
    from app.routers import partnerships
    app.include_router(
        partnerships.router,
        prefix="/api/v1/partnerships",
        tags=["partnerships"],
    )

    # ── Reservations / Bookings: future-dated commitments + no-show ────
    from app.routers import reservations
    app.include_router(
        reservations.router,
        prefix="/api/v1/reservations",
        tags=["reservations"],
    )

    # ── Listings: C2C marketplace (闲鱼 / 淘宝 / eBay) ────────────────────
    from app.routers import listings
    app.include_router(
        listings.router,
        prefix="/api/v1/listings",
        tags=["listings"],
    )

    # ── KiX ID: universal identity + OAuth-like merchant Connect ───────
    from app.routers import kix_id
    app.include_router(
        kix_id.router, prefix="/api/v1/kix-id", tags=["kix_id"]
    )

    # ── Push Engine: right-time / right-place / right-user delivery ────
    from app.routers import push_engine
    app.include_router(
        push_engine.router,
        prefix="/api/v1/push",
        tags=["push"],
    )

    # ── Transactions: universal commerce ledger (purchase/refund/...) ──
    from app.routers import transactions
    app.include_router(
        transactions.router,
        prefix="/api/v1/transactions",
        tags=["transactions"],
    )

    # ── Consumer Wallet: per-user balance (deposits, prepaid, payouts) ─
    from app.routers import user_wallet
    app.include_router(
        user_wallet.router,
        prefix="/api/v1/user-wallet",
        tags=["user_wallet"],
    )

    # ── Deposit lifecycle: typed wrapper around user-wallet freezes ────
    from app.routers import deposits
    app.include_router(
        deposits.router,
        prefix="/api/v1/deposits",
        tags=["deposits"],
    )

    # ── Dynamic Pricing: time/demand/inventory rule-driven quotes ──────
    from app.routers import pricing
    app.include_router(
        pricing.router,
        prefix="/api/v1/pricing",
        tags=["pricing"],
    )

    # ── Accounts: B2B company entities + buying committee + org chart ──
    from app.routers import accounts
    app.include_router(
        accounts.router,
        prefix="/api/v1/accounts",
        tags=["accounts"],
    )

    # ── Subscriptions: SaaS/membership/streaming + NDR/GRR metrics ─────
    from app.routers import subscriptions
    app.include_router(
        subscriptions.router,
        prefix="/api/v1/subscriptions",
        tags=["subscriptions"],
    )

    # ── Brand Subscriptions: FREE/STARTER/GROWTH/ENTERPRISE tier system ─
    from app.routers import brand_subscriptions
    app.include_router(
        brand_subscriptions.router,
        prefix="/api/v1/brand-subscriptions",
        tags=["brand_subscriptions"],
    )

    # ── Payment Methods: card-on-file + anti-fraud + background charge ──
    from app.routers import payment_methods
    app.include_router(
        payment_methods.router,
        prefix="/api/v1/payment-methods",
        tags=["payment_methods"],
    )

    # ── Customers: Stripe-style merchant billing root + tax/address ────
    from app.routers import customers
    app.include_router(
        customers.router,
        prefix="/api/v1/customers",
        tags=["customers"],
    )

    # ── PaymentIntents + SetupIntents: save-card + charge state machines ─
    from app.routers import payment_intents
    app.include_router(
        payment_intents.router,
        prefix="/api/v1/payment-intents",
        tags=["payment_intents"],
    )

    # ── Invoices: formal billing docs with line items + VAT + PDF ──────
    from app.routers import invoices
    app.include_router(
        invoices.router,
        prefix="/api/v1/invoices",
        tags=["invoices"],
    )

    # ── Stripe Webhook: payment_intent / subscription / invoice / refund ─
    from app.routers import stripe_webhook
    app.include_router(
        stripe_webhook.router,
        prefix="/api/v1/webhooks/stripe",
        tags=["stripe_webhook"],
    )

    # ── Merchant Dashboards: today / cumulative / leaderboard / insights ─
    from app.routers import dashboards
    app.include_router(
        dashboards.router,
        prefix="/api/v1/dashboards",
        tags=["dashboards"],
    )

    # ── Multi-Dimensional Reporting: TikTok/Google Ads Manager parity ──
    from app.routers import reporting
    app.include_router(
        reporting.router,
        prefix="/api/v1/reporting",
        tags=["reporting"],
    )

    # ── Welcome Kit: auto-generated table stand / poster / shipping ────
    from app.routers import welcome_kit
    app.include_router(
        welcome_kit.router,
        prefix="/api/v1/welcome-kit",
        tags=["welcome_kit"],
    )

    # ── Saga: cross-module compensating transactions ───────────────────
    from app.routers import saga as saga_router
    app.include_router(
        saga_router.router,
        prefix="/api/v1/saga",
        tags=["saga"],
    )

    # ── Root redirect to Landing Page ──────────────────────────────────
    @app.get("/")
    async def root_to_landing():
        return RedirectResponse(url="/landing/index.html")

    # ── Public API Reference shortcuts ─────────────────────────────────
    # `/docs` (Swagger UI) and `/redoc` (ReDoc) are auto-mounted by
    # FastAPI. We add two vanity URLs that point at the curated
    # human-friendly landing page under /landing/api-docs/.
    @app.get("/api-docs", include_in_schema=False)
    async def api_docs_redirect():
        return RedirectResponse(url="/landing/api-docs/index.html")

    @app.get("/api-reference", include_in_schema=False)
    async def api_reference_redirect():
        return RedirectResponse(url="/landing/api-docs/index.html")

    # ── Vanity URL for KiX App (`/app/*` → `/landing/app/*`) ───────────
    # Lets us advertise `partner.letskix.com/app/` as the user-facing
    # front door while keeping the static bundle under /landing/app/.
    # NOTE: include_in_schema=False — `Request` forward-refs break OpenAPI
    # generation under `from __future__ import annotations`. These routes
    # are static-asset redirects and have no business being in the schema.
    @app.get("/app", include_in_schema=False)
    @app.get("/app/", include_in_schema=False)
    async def kix_app_root(request: Request):
        qs = ("?" + request.url.query) if request.url.query else ""
        return RedirectResponse(url=f"/landing/app/index.html{qs}")

    @app.get("/app/{path:path}", include_in_schema=False)
    async def kix_app_passthrough(path: str, request: Request):
        qs = ("?" + request.url.query) if request.url.query else ""
        target = (
            f"/landing/app/{path}" if path else "/landing/app/index.html"
        )
        return RedirectResponse(url=f"{target}{qs}")

    # ── Static files: Portal + generated games ──────────────────────────
    import os as _os
    _landing_dir = _os.path.join(_os.path.dirname(__file__), "..", "landing")
    if _os.path.isdir(_landing_dir):
        app.mount("/landing", StaticFiles(directory=_landing_dir), name="landing")
        # Expose JS SDKs (kix.js, kix-pixel.js) at a stable /sdk/* URL so the
        # merchant embed snippet stays short and version-independent.
        _sdk_dir = _os.path.join(_landing_dir, "sdk")
        if _os.path.isdir(_sdk_dir):
            app.mount("/sdk", StaticFiles(directory=_sdk_dir), name="sdk")

    return app


app = create_app()
