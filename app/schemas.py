"""Pydantic v2 request/response schemas for KiX Platform R5."""

from __future__ import annotations

from pydantic import BaseModel


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
    brand_id: str
    brand_name: str
    brand_slug: str
    config_json: dict


class BrandConfigUpdate(BaseModel):
    config_json: dict


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
