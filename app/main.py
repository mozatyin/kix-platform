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

    # ── Stripe integration: log active mode on boot ──────────────────
    # Surfaces live/test/mock at startup so operators can immediately
    # spot misconfiguration (e.g. live pod accidentally booting with
    # ``sk_test_stub`` and silently falling back to mock).
    try:
        from app.services.stripe_live import log_startup_mode
        log_startup_mode()
    except Exception as _exc:  # pragma: no cover — never fail startup
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "stripe startup mode log skipped: %s", _exc
        )

    # ── Schema health check: detect missing critical PG tables ────────
    # If alembic migrations haven't been applied to this env, certain
    # routers (geofence/PostGIS) will 500 at first request. We log a WARN
    # at startup so operators see the problem in the boot log instead of
    # discovering it via traffic. NEVER fatal — the Redis-only geofence
    # path keeps working, and other tables degrade gracefully too.
    try:
        from app.database import write_engine
        from sqlalchemy import text as _sql_text

        _CRITICAL_TABLES = ("geofences",)
        async with write_engine.connect() as _conn:
            for _tbl in _CRITICAL_TABLES:
                row = await _conn.execute(
                    _sql_text(
                        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_name=:t)"
                    ),
                    {"t": _tbl},
                )
                exists = bool(row.scalar())
                if not exists:
                    import logging as _logging
                    _logging.getLogger(__name__).warning(
                        "schema_health: missing PG table %r — run "
                        "`alembic upgrade head` (Redis-only fallback active)",
                        _tbl,
                    )
    except Exception as _exc:  # pragma: no cover — never fail startup
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "schema_health check skipped: %s", _exc
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

    # ── Multi-tenant isolation: per-brand RPM limit + usage tracking ──
    from app.middleware.tenant_isolation import TenantIsolationMiddleware
    app.add_middleware(TenantIsolationMiddleware)

    # ── i18n: resolve request locale before auth/handlers run ─────────
    # Mounted *after* tenant isolation so quota enforcement still fires
    # first, but *before* any router-level dependency that reads the
    # contextvar (push templates, error messages, etc.). Starlette's
    # ``add_middleware`` is LIFO at request-time, so this stays close
    # to the application boundary.
    from app.i18n.middleware import LanguageMiddleware
    app.add_middleware(LanguageMiddleware)

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

    # ── WhatsApp OTP auth (Wave E item 6) ──────────────────────────────
    # Additive — coexists with the legacy device-sig flow on
    # /api/v1/auth/token. SMB merchants + SEA consumers prefer phone-OTP
    # over email; this router is the back-end for the WhatsApp Business
    # Cloud API send. Mock mode is default when WHATSAPP_API_TOKEN is
    # unset so dev/CI need zero extra config.
    from app.routers import whatsapp_auth as _whatsapp_auth_router
    app.include_router(
        _whatsapp_auth_router.router,
        prefix="/api/v1/auth/whatsapp",
        tags=["auth-whatsapp"],
    )

    app.include_router(game.router, prefix="/api/v1/game", tags=["game"])
    app.include_router(energy.router, prefix="/api/v1/energy", tags=["energy"])
    app.include_router(
        leaderboard.router, prefix="/api/v1/leaderboard", tags=["leaderboard"]
    )
    app.include_router(streak.router, prefix="/api/v1/streak", tags=["streak"])
    app.include_router(brands.router, prefix="/api/v1/brands", tags=["brands"])

    # ── i18n: brand translation sidecar (merchant + admin) ─────────────
    from app.routers import brand_translations as _i18n_brand_router
    app.include_router(
        _i18n_brand_router.router,
        prefix="/api/v1/brands",
        tags=["brand-translations"],
    )
    app.include_router(
        _i18n_brand_router.admin_router,
        prefix="/api/v1/admin",
        tags=["admin-translations"],
    )

    # ── Audit log (durable PG spine — PIPL §51 / GDPR Art. 30) ─────────
    # Replaces the legacy Redis ``audit:*`` LIST. Admin-token gated;
    # the audit log is itself a PII surface so we never expose it to
    # merchants. Search/export sit under /api/v1/audit; the retention
    # dashboard sits under /api/v1/admin/audit.
    from app.routers import audit_log as _audit_log_router
    app.include_router(
        _audit_log_router.router,
        prefix="/api/v1/audit",
        tags=["audit-log"],
    )
    app.include_router(
        _audit_log_router.admin_router,
        prefix="/api/v1/admin/audit",
        tags=["admin-audit"],
    )

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
    # Cross-brand voucher pooling — KiX's killer network-effect lock-in.
    # See app/services/voucher_pool.py for the data model + WATCH/MULTI
    # atomicity contract.
    from app.routers import voucher_pools
    app.include_router(
        voucher_pools.router,
        prefix="/api/v1/voucher-pools",
        tags=["voucher-pools"],
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

    # ── i18n format validators (phone E.164 + per-country address) ────
    # Standard internationalization helpers (libphonenumber + per-country
    # postal specs). Pure format library — no DB, no LLM, no PII.
    from app.routers import i18n_validators as _i18n_val_router
    app.include_router(
        _i18n_val_router.router,
        prefix="/api/v1",
        tags=["i18n-validators"],
    )

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

    # ── i18n debug + locale-aware format preview ────────────────────────
    from app.routers import i18n as i18n_router
    app.include_router(
        i18n_router.router, prefix="/api/v1/i18n", tags=["i18n"]
    )

    # ── i18n glossary — terminology manager for the LLM translator ──────
    from app.routers import i18n_glossary as i18n_glossary_router
    app.include_router(
        i18n_glossary_router.router,
        prefix="/api/v1/i18n/glossary",
        tags=["i18n"],
    )

    # ── Retention Engine: streaks / habits / cohorts / churn / nudges ──
    from app.routers import retention
    app.include_router(
        retention.router,
        prefix="/api/v1/retention",
        tags=["retention"],
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

    # ── Compliance Regional: per-region GDPR/LGPD/PDPA/DPDP rule sets ───
    from app.routers import compliance_regional
    app.include_router(
        compliance_regional.router,
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
    from app import quality_score as qs_module
    app.include_router(
        campaigns.router, prefix="/api/v1/campaigns", tags=["campaigns"]
    )
    # P2 fix: QS auto-compute / decay / breakdown / override endpoints
    # mount alongside campaigns so clients hit them at
    # /api/v1/campaigns/{cid}/qs-{breakdown,override}.
    app.include_router(
        qs_module.router, prefix="/api/v1/campaigns", tags=["campaigns"]
    )
    app.include_router(
        auction.router, prefix="/api/v1/auction", tags=["auction"]
    )

    # ── Multi-week Campaign Arcs (Monopoly-style + advent + bracket) ───
    from app.routers import campaign_arcs
    app.include_router(
        campaign_arcs.router,
        prefix="/api/v1/campaign-arcs",
        tags=["campaign-arcs"],
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

    # ── TriSoul Integration: adaptive routing by user attention vector ─
    # Feature-flag gated; default OFF. Additive hooks into push_engine /
    # auction / recipe_generator score TriSoul × campaign affinity.
    from app.routers import trisoul_integration
    app.include_router(
        trisoul_integration.router,
        prefix="/api/v1/trisoul",
        tags=["trisoul"],
    )

    # ── Push Topics + FCM token registration + push health ─────────────
    from app.routers import push_topics
    app.include_router(
        push_topics.router,
        prefix="/api/v1/push",
        tags=["push-topics"],
    )

    # ── Email + push template admin (locale-aware) ─────────────────────
    from app.routers import email_admin
    app.include_router(
        email_admin.router,
        prefix="/api/v1/admin",
        tags=["email-admin"],
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

    # ── Regional Payments: per-country capability registry (read-only) ─
    from app.routers import payments_regional
    app.include_router(
        payments_regional.router,
        prefix="/api/v1/payments",
        tags=["payments_regional"],
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

    # ── Stripe live-integration health (/api/v1/health/stripe) ─────────
    try:
        from app.services import stripe_live as _stripe_live
        if getattr(_stripe_live, "router", None) is not None:
            app.include_router(_stripe_live.router, tags=["stripe_health"])
    except Exception as _exc:  # pragma: no cover — never fail boot
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "stripe health router not mounted: %s", _exc
        )

    # ── Outbound Webhooks: KiX → merchant event delivery (HMAC + retry) ─
    from app.routers import webhooks_outbound
    app.include_router(
        webhooks_outbound.router,
        prefix="/api/v1/webhooks-outbound",
        tags=["webhooks_outbound"],
    )

    # ── PSP Webhooks: PayNow / GrabPay / Alipay / WeChat / OVO ─────────
    try:
        from app.routers import psp_webhooks
        app.include_router(
            psp_webhooks.router,
            prefix="/api/v1/webhooks",
            tags=["psp_webhooks"],
        )
        app.include_router(
            psp_webhooks.health_router,
            prefix="/api/v1/health",
            tags=["psp_health"],
        )
    except Exception as _exc:  # pragma: no cover — never fail boot
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "psp webhook router not mounted: %s", _exc
        )

    # ── POS Integration: Toast / Square / Loyverse / Foodzaps ──────────
    try:
        from app.routers import pos_integration
        app.include_router(
            pos_integration.router,
            prefix="/api/v1/pos",
            tags=["pos_integration"],
        )
        app.include_router(
            pos_integration.webhook_router,
            prefix="/api/v1/webhooks",
            tags=["pos_webhooks"],
        )
    except Exception as _exc:  # pragma: no cover — never fail boot
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "pos integration router not mounted: %s", _exc
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

    # ── Admin: tenant isolation introspection + circuit reset ──────────
    from app.routers import tenant_admin
    app.include_router(
        tenant_admin.router,
        prefix="/api/v1/admin",
        tags=["tenant_admin"],
    )

    # ── ML: Quality Score / Relevance / Smart Bid (admin) ──────────────
    from app.routers import ml as ml_router
    app.include_router(
        ml_router.router,
        prefix="/api/v1/ml",
        tags=["ml"],
    )

    # ── Asset CDN: merchant logos/images/videos w/ storage abstraction ─
    from app.routers import assets
    app.include_router(
        assets.router,
        prefix="/api/v1/assets",
        tags=["assets"],
    )

    # ── A/B Testing: split-test voucher/campaign/push/recipe configs ───
    from app.routers import ab_testing
    app.include_router(
        ab_testing.router,
        prefix="/api/v1/ab-testing",
        tags=["ab_testing"],
    )

    # ── Enterprise Portal meta-endpoints (read-only convenience layer) ──
    # Composes campaigns / audiences / dashboards / wallet / reporting
    # into a small set of locale-aware payloads the new portal UI consumes
    # in one round-trip. See app/routers/portal_api.py for design notes.
    from app.routers import portal_api
    app.include_router(
        portal_api.router,
        prefix="/api/v1/portal",
        tags=["portal"],
    )

    # ── Portal Settings: profile / billing / PMs / team / notif / etc. ──
    # See app/routers/portal_settings.py — read-mostly composition over
    # wallet / payment_methods / invoices for the Settings view, plus the
    # account switcher (/api/v1/portal/accounts/me).
    from app.routers import portal_settings
    app.include_router(
        portal_settings.router,
        prefix="/api/v1/portal",
        tags=["portal-settings"],
    )

    # ── Portal Conversion Pixels: create / list / events / snippet ──────
    from app.routers import portal_pixels
    app.include_router(
        portal_pixels.router,
        prefix="/api/v1/portal",
        tags=["portal-pixels"],
    )

    # ── Alpha-Merchant Program: invite / signup / feedback / cohort ────
    # Lazy import: side-effect registers the 4 alpha_* email templates.
    from app.routers import alpha_program
    app.include_router(
        alpha_program.router,
        prefix="/api/v1/alpha",
        tags=["alpha-program"],
    )

    # ── Customer Support: tickets / refunds / FAQ / announcements ─────
    # Lazy import side-effect registers the 3 support_* email templates.
    # Merchant + public routes mount under /api/v1/support; admin routes
    # live under /api/v1/admin/support so the URL space mirrors the
    # other admin tooling (email_admin, tenant_admin).
    from app.routers import support as _support_router
    app.include_router(
        _support_router.router,
        prefix="/api/v1/support",
        tags=["support"],
    )
    app.include_router(
        _support_router.admin_router,
        prefix="/api/v1/admin/support",
        tags=["support-admin"],
    )

    # ── Prize fulfillment (Realtime Media / Merkle ePrize parity) ─────
    # Instant-win + sweepstakes pools, per-jurisdiction legal compliance
    # (US W-9 trigger, EU GDPR consent, SG prize cap, CN raffle ban),
    # anti-fraud (rate limit + IP collision review queue), claim flow.
    # Public routes under /api/v1/prizes; admin under /api/v1/admin/prizes.
    from app.routers import prizes as _prizes_router
    app.include_router(
        _prizes_router.router,
        prefix="/api/v1/prizes",
        tags=["prizes"],
    )
    app.include_router(
        _prizes_router.admin_router,
        prefix="/api/v1/admin/prizes",
        tags=["prizes-admin"],
    )

    # ── Trinity 3T iteration engine (meta-tooling, admin-only) ────────
    # Institutionalises the manual Trinity Protocol cycle (Industry ×
    # Academic × Reality) as a callable engine. See
    # docs/trinity-3t-handbook.md for usage and case studies.
    from app.routers import trinity_admin
    app.include_router(
        trinity_admin.router,
        prefix="/api/v1/trinity",
        tags=["trinity"],
    )

    # ── Wave-C Observability: ML & viral telemetry (no prefix — routes
    # under the router carry their own /api/v1/... prefixes so the health
    # endpoint can mount at /api/v1/health/observability alongside the
    # other health probes).
    from app.routers import observability as _obs_router
    app.include_router(_obs_router.router, tags=["observability"])

    # ── Wave-E Step 5: Re-engagement (Return funnel step) ──────────────
    # Multi-channel cascade (WhatsApp/push/email) when users go quiet.
    # Backed by app.services.reengagement_orchestrator + the
    # reengagement_worker cron. Brand-scoped routes mount under
    # /api/v1/reengagement; the admin test-cascade route lives at
    # /api/v1/admin/reengagement so ops scripts can fire it without
    # discovering a brand id.
    from app.routers import reengagement as _reeng_router
    app.include_router(
        _reeng_router.router,
        prefix="/api/v1/reengagement",
        tags=["reengagement"],
    )
    app.include_router(
        _reeng_router.admin_router,
        prefix="/api/v1/admin/reengagement",
        tags=["reengagement-admin"],
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

    # ── Wave F: competitor-mined obvious wins (additive, no overlap) ────
    # See /Users/mozat/a-docs/9-competitor-feature-mining-master-backlog.md
    # for the per-competitor Trinity rationale. Each router is in
    # app/routers/wavef_*.py and is fully NEW (no existing module touched).
    try:
        from app.routers import (
            wavef_sweepstakes,
            wavef_daily_checkin,
            wavef_brand_color,
            wavef_poll,
            wavef_certificate,
        )
        app.include_router(
            wavef_sweepstakes.router,
            prefix="/api/v1/wavef/sweepstakes",
            tags=["wavef", "sweepstakes"],
        )
        app.include_router(
            wavef_daily_checkin.router,
            prefix="/api/v1/wavef/daily-checkin",
            tags=["wavef", "daily-checkin"],
        )
        app.include_router(
            wavef_brand_color.router,
            prefix="/api/v1/wavef/brand-color",
            tags=["wavef", "brand-color"],
        )
        app.include_router(
            wavef_poll.router,
            prefix="/api/v1/wavef/poll",
            tags=["wavef", "poll"],
        )
        app.include_router(
            wavef_certificate.router,
            prefix="/api/v1/wavef/certificate",
            tags=["wavef", "certificate"],
        )
    except Exception as _exc:  # pragma: no cover — never fail boot
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "wave-f routers skipped: %s", _exc
        )

    # ── Wave F specs 06-10 (each in its own isolated try-block so any
    # one failure doesn't break the others — independent feature
    # batches built additively on top of 01-05). ─────────────────────────
    try:
        from app.routers import wavef_geofenced_voucher as _wf06
        app.include_router(
            _wf06.router,
            prefix="/api/v1/wavef/geo-voucher",
            tags=["wavef", "geo-voucher"],
        )
    except Exception as _exc:  # pragma: no cover
        import logging as _logging
        _logging.getLogger(__name__).warning("wavef-06 skipped: %s", _exc)

    try:
        from app.routers import wavef_flash_promo as _wf07
        app.include_router(
            _wf07.router,
            prefix="/api/v1/wavef/flash",
            tags=["wavef", "flash-promo"],
        )
    except Exception as _exc:  # pragma: no cover
        import logging as _logging
        _logging.getLogger(__name__).warning("wavef-07 skipped: %s", _exc)

    try:
        from app.routers import wavef_template_gallery as _wf08
        app.include_router(
            _wf08.router,
            prefix="/api/v1/wavef/templates",
            tags=["wavef", "templates"],
        )
    except Exception as _exc:  # pragma: no cover
        import logging as _logging
        _logging.getLogger(__name__).warning("wavef-08 skipped: %s", _exc)

    try:
        from app.routers import wavef_referral as _wfr01
        app.include_router(
            _wfr01.router,
            prefix="/api/v1/wavef/referral",
            tags=["wavef", "referral"],
        )
    except Exception as _exc:  # pragma: no cover
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "wavef-spec-01 referral skipped: %s", _exc
        )

    try:
        from app.routers import wavef_calendar as _wfr02
        app.include_router(
            _wfr02.router,
            prefix="/api/v1/wavef/calendar",
            tags=["wavef", "calendar"],
        )
    except Exception as _exc:  # pragma: no cover
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "wavef-spec-02 calendar skipped: %s", _exc
        )

    try:
        from app.routers import wavef_spin as _wfr03
        app.include_router(
            _wfr03.router,
            prefix="/api/v1/wavef/spin",
            tags=["wavef", "spin"],
        )
    except Exception as _exc:  # pragma: no cover
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "wavef-spec-03 spin skipped: %s", _exc
        )

    try:
        from app.routers import wavef_scratch as _wfr04
        app.include_router(
            _wfr04.router,
            prefix="/api/v1/wavef/scratch",
            tags=["wavef", "scratch"],
        )
    except Exception as _exc:  # pragma: no cover
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "wavef-spec-04 scratch skipped: %s", _exc
        )

    try:
        from app.routers import wavef_memory as _wfr05
        app.include_router(
            _wfr05.router,
            prefix="/api/v1/wavef/memory",
            tags=["wavef", "memory"],
        )
    except Exception as _exc:  # pragma: no cover
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "wavef-spec-05 memory skipped: %s", _exc
        )

    try:
        from app.routers import wavef_capture as _wfr11
        app.include_router(
            _wfr11.router,
            prefix="/api/v1/wavef/capture",
            tags=["wavef", "capture"],
        )
    except Exception as _exc:  # pragma: no cover
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "wavef-spec-11 capture skipped: %s", _exc
        )

    try:
        from app.routers import wavef_sets as _wfr12
        app.include_router(
            _wfr12.router,
            prefix="/api/v1/wavef/sets",
            tags=["wavef", "sets"],
        )
    except Exception as _exc:  # pragma: no cover
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "wavef-spec-12 sets skipped: %s", _exc
        )

    try:
        from app.routers import wavef_mechanics as _wfr13
        app.include_router(
            _wfr13.router,
            prefix="/api/v1/wavef/mechanics",
            tags=["wavef", "mechanics"],
        )
    except Exception as _exc:  # pragma: no cover
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "wavef-spec-13 mechanics skipped: %s", _exc
        )

    try:
        from app.routers import wavef_wizard as _wfr14
        app.include_router(
            _wfr14.router,
            prefix="/api/v1/wavef/wizard",
            tags=["wavef", "wizard"],
        )
    except Exception as _exc:  # pragma: no cover
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "wavef-spec-14 wizard skipped: %s", _exc
        )

    try:
        from app.routers import wavef_splash as _wfr15
        app.include_router(
            _wfr15.router,
            prefix="/api/v1/wavef/splash",
            tags=["wavef", "splash"],
        )
    except Exception as _exc:  # pragma: no cover
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "wavef-spec-15 splash skipped: %s", _exc
        )

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
