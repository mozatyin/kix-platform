"""Pydantic v2 request/response schemas for KiX Platform R5."""

from __future__ import annotations

from pydantic import BaseModel, Field


# ── Common ────────────────────────────────────────────────────────────────
class ErrorResponse(BaseModel):
    code: str
    message: str
    details: dict | None = None
    request_id: str | None = None


# ── Auth ──────────────────────────────────────────────────────────────────
class TokenRequest(BaseModel):
    brand_id: str
    device_sig: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    user_id: str
    energy: int
    is_day1: bool
    day1_expires_at: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str
    device_sig: str


# ── Game ──────────────────────────────────────────────────────────────────
class GameStartRequest(BaseModel):
    brand_id: str
    game_id: str


class GameStartResponse(BaseModel):
    session_id: str
    access_token: str
    energy_remaining: int
    cost_charged: int


class GameEndRequest(BaseModel):
    session_id: str
    score: int  # 0-100000


class RewardInfo(BaseModel):
    type: str
    voucher_code: str | None = None
    voucher_description: str | None = None
    voucher_expires_at: str | None = None


class GameEndResponse(BaseModel):
    rank: int
    season_id: str
    reward: RewardInfo | None = None


# ── Energy ────────────────────────────────────────────────────────────────
class EnergyGrantRequest(BaseModel):
    brand_id: str
    qr_token: str


class EnergyGrantResponse(BaseModel):
    energy_granted: int
    energy_balance: int
    next_grant_available_at: str


# ── Leaderboard ──────────────────────────────────────────────────────────
class LeaderboardEntry(BaseModel):
    rank: int
    user_id: str
    display_name: str
    score: int
    is_self: bool


class LeaderboardResponse(BaseModel):
    entries: list[LeaderboardEntry]
    season_id: str
    total_players: int
    updated_at: str


class NearbyResponse(BaseModel):
    entries: list[LeaderboardEntry]
    self_rank: int | None
    season_id: str


# ── Streak ────────────────────────────────────────────────────────────────
class StreakCheckRequest(BaseModel):
    brand_id: str


class StreakCheckResponse(BaseModel):
    current_streak: int
    longest_streak: int
    today_completed: bool
    next_milestone: int | None
    milestone_reward: dict | None = None


# ── Brands ────────────────────────────────────────────────────────────────
class BrandConfigCreate(BaseModel):
    """Payload for creating a brand configuration.

    ``config_json`` MUST contain the following top-level sections:

    * ``energy``      — energy economy config (regen rate, max, refill costs).
                        See ``BrandConfigEnergySection`` for the canonical
                        shape; the brand_register flow auto-fills sane
                        defaults when missing rather than 422-ing.
    * ``games``       — game catalog binding (which titles this brand
                        exposes + per-game multipliers).
    * ``leaderboard`` — leaderboard scope/window/visibility settings.

    For a complete valid minimal payload call
    ``GET /api/v1/brands/config-template``. For the JSON schema describing
    these sections, call ``GET /api/v1/brands/{brand_id}/config-schema``.

    The legacy "send any dict" behaviour is preserved on the wire — the
    field is still ``dict`` — but the router auto-fills missing sections
    (energy/games/leaderboard) with documented defaults before persisting.
    """

    brand_id: str = Field(
        ...,
        description="Stable merchant identifier. Lowercase letters / digits / "
        "underscores. Used as the partition key in Redis and PG.",
        examples=["acme_coffee"],
    )
    brand_name: str = Field(
        ...,
        description="Human-readable brand name shown in dashboards and the "
        "consumer Portal.",
        examples=["Acme Coffee"],
    )
    brand_slug: str = Field(
        ...,
        description="URL-safe slug for the public Storefront. Must be unique "
        "platform-wide.",
        examples=["acme-coffee"],
    )
    config_json: dict = Field(
        ...,
        description=(
            "Brand-scoped config payload. Required top-level sections: "
            "`energy`, `games`, `leaderboard`. Missing sections are auto-"
            "filled with documented defaults; explicit invalid sections "
            "(wrong types) still 422. Call "
            "`GET /api/v1/brands/config-template` for a complete example."
        ),
        examples=[
            {
                "energy": {
                    "max": 5,
                    "regen_minutes": 30,
                    "refill_cost_cents": 100,
                },
                "games": {"catalog": ["spin_wheel", "scratch_card"]},
                "leaderboard": {
                    "scope": "brand",
                    "window": "weekly",
                    "visibility": "public",
                },
            }
        ],
    )


class BrandConfigUpdate(BaseModel):
    config_json: dict = Field(
        ...,
        description="Full config_json replacement. Same shape contract as "
        "BrandConfigCreate.config_json.",
    )


class BrandConfigResponse(BaseModel):
    brand_id: str
    brand_name: str
    brand_slug: str
    config_json: dict
    status: str
    created_at: str
    updated_at: str


class BrandLocationCreate(BaseModel):
    location_id: str
    brand_id: str
    location_name: str
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class BrandLocationResponse(BaseModel):
    location_id: str
    brand_id: str
    location_name: str
    address: str | None
    latitude: float | None
    longitude: float | None
    status: str


# ── Vouchers ──────────────────────────────────────────────────────────────
class VoucherUploadResponse(BaseModel):
    imported: int
    skipped_duplicates: int
    errors: list[dict]


class VoucherResponse(BaseModel):
    code: str
    description: str | None
    tier: str
    assigned_at: str | None
    expires_at: str | None
    status: str
    brand_name: str | None = None


class VoucherSummary(BaseModel):
    available: int = 0
    assigned: int = 0
    redeemed: int = 0
    expired: int = 0


class VoucherListItem(BaseModel):
    code: str
    tier: str
    status: str
    description: str | None = None


class VoucherListResponse(BaseModel):
    vouchers: list[VoucherListItem]
    summary: VoucherSummary


# ── Reward (internal) ────────────────────────────────────────────────────
class RewardEvaluateRequest(BaseModel):
    session_id: str
    user_id: str
    brand_id: str
    game_id: str
    score: int
    season_id: str
    rank: int


class RewardEvaluateResponse(BaseModel):
    decision: str
    voucher: dict | None = None
    reason: str


# ── QR ────────────────────────────────────────────────────────────────────
class QRGenerateRequest(BaseModel):
    brand_id: str
    location_id: str
    duration_minutes: int = 15


class QRGenerateResponse(BaseModel):
    qr_token: str
    qr_url: str
    valid_until: str
    next_rotation_at: str


# ── Portal Auth ───────────────────────────────────────────────────────────
class PortalLoginRequest(BaseModel):
    email: str
    password: str


class PortalLoginResponse(BaseModel):
    access_token: str
    refresh_token: str


class PortalRegisterRequest(BaseModel):
    email: str
    password: str
    brand_name: str
    brand_color: str = ""


class PortalRegisterResponse(BaseModel):
    access_token: str
    refresh_token: str
    brand_id: str
    brand_name: str


# ── Health ────────────────────────────────────────────────────────────────
class HealthResponse(BaseModel):
    status: str
    version: str
    uptime_seconds: int


class ReadyCheck(BaseModel):
    redis: str
    config_loaded: bool
    brands_count: int


class ReadyResponse(BaseModel):
    status: str
    checks: ReadyCheck


# ── Game Catalog ─────────────────────────────────────────────────────────
class GameCatalogEntry(BaseModel):
    slug: str
    name: str
    category: str
    description: str
    player_count: int
    primary_color: str
    accent_color: str


class GameCatalogResponse(BaseModel):
    games: list[GameCatalogEntry]
    total: int
    categories: list[str]


class GameOrderRequest(BaseModel):
    brand_id: str
    game_slug: str | None = None
    description: str
    theme: str | None = None
    requirements: str | None = None


class GameOrderResponse(BaseModel):
    order_id: str
    status: str
    game_slug: str | None = None
    description: str = ""
    created_at: str = ""
    game_file: str | None = None
    game_name: str | None = None
    order_type: str | None = None
    error: str | None = None


class GameOrderListResponse(BaseModel):
    orders: list[GameOrderResponse]


class AddGameToBrandRequest(BaseModel):
    brand_id: str
    game_slug: str


class AddGameToBrandResponse(BaseModel):
    brand_id: str
    game_slug: str
    game_name: str
    energy_cost: int
    message: str
