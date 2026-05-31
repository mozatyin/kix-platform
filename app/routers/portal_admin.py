"""Portal v3 (TikTok-Ads-Manager-functional) backend API.

Scaffolded REST endpoints the portal-v3 frontend calls. Returns
deterministic mock data today; real backend wiring is a follow-up
(needs Redis/PG live data path). Shape is production-correct so the
frontend can be built and tested against this from day 1.

Endpoints (mounted under /api/v1/portal-admin):

  GET  /overview             — top status strip + 4 metric cards
  GET  /campaigns            — TikTok-style campaign table rows
  GET  /campaigns/{id}       — single campaign detail
  POST /campaigns            — create new campaign
  GET  /audiences            — saved audience list + counts
  GET  /creatives            — game template library (79 entries)
  GET  /reports/cohort       — 14-day cohort retention
  GET  /reports/funnel       — impressions → plays → new customers → returns
  GET  /activity/live        — last-hour activity feed (game wins · redemptions)
  GET  /wallet               — wallet balance · runway · burn rate
  POST /wallet/topup         — top up wallet
  GET  /audience-breakdown   — 4-source donut data
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

router = APIRouter()


# ── Response models ──

class StatusStrip(BaseModel):
    wallet_sgd: int
    new_customers_7d: int
    new_customers_7d_delta_pct: float
    campaigns_live: int
    runway_days: int
    burn_per_day_sgd: int
    health: str = Field(..., description="green / amber / red")


class MetricCard(BaseModel):
    label: str
    value: str
    delta_pct: float
    delta_direction: str    # "up" / "down" / "flat"
    sub_label: str
    benchmark_note: Optional[str] = None


class Campaign(BaseModel):
    id: str
    status: str            # "live" / "paused" / "review" / "ended"
    name: str
    objective: str         # "NEW" / "REPEAT" / "REACH" / "VISIT" / "BOOK"
    game_type: str
    reward: str
    schedule: str
    audience_name: str
    spend_sgd: float
    impressions: int
    plays: int
    new_customers: int
    cpa_sgd: float
    ctr_pct: float


class Audience(BaseModel):
    id: str
    name: str
    type: str              # "geofence" / "lookalike" / "retargeting" / "custom"
    size_estimate: int
    geofence_m: Optional[int] = None
    created_at: str
    last_used_at: Optional[str] = None


class Creative(BaseModel):
    id: str
    name: str
    game_type: str
    emoji: str
    description: str
    plays_today: int
    cpa_sgd: float
    is_active: bool


class FeedItem(BaseModel):
    timestamp: str
    type: str              # "win" / "redeem" / "new"
    kid: str
    campaign: str
    detail: str


class CohortRow(BaseModel):
    cohort_day: str
    new_customers: int
    returned_1d: int
    returned_7d: int
    returned_14d: int
    returned_30d: int


class FunnelStep(BaseModel):
    step: str
    count: int
    conversion_pct: Optional[float] = None


class AudienceBreakdown(BaseModel):
    source: str
    count: int
    pct: float
    color: str


class Wallet(BaseModel):
    balance_sgd: float
    auto_recharge_at_sgd: float
    burn_per_day_sgd: float
    runway_days: int


class TopupRequest(BaseModel):
    amount_sgd: float = Field(..., gt=0, le=10000)


# ── Mock data ──

def _deterministic_campaigns() -> list[Campaign]:
    return [
        Campaign(id="c_lunch_spin", status="live", name="Lunch spin · 200m geofence",
                 objective="NEW", game_type="spin", reward="S$1 off kopi",
                 schedule="11am-2pm", audience_name="New users (Bedok 200m)",
                 spend_sgd=378.0, impressions=7420, plays=2103, new_customers=87,
                 cpa_sgd=4.20, ctr_pct=28.3),
        Campaign(id="c_scratch_breakfast", status="live", name="Scratch & win · breakfast",
                 objective="NEW", game_type="scratch", reward="free kaya toast",
                 schedule="6am-10am", audience_name="New users (200m + first-time)",
                 spend_sgd=214.0, impressions=4180, plays=1142, new_customers=42,
                 cpa_sgd=5.10, ctr_pct=27.3),
        Campaign(id="c_mystery_evening", status="live", name="Mystery box · evening",
                 objective="NEW", game_type="mystery", reward="20% off lunch set",
                 schedule="5pm-9pm", audience_name="Lookalike (frequent kopi SG)",
                 spend_sgd=128.0, impressions=2638, plays=647, new_customers=18,
                 cpa_sgd=6.80, ctr_pct=24.5),
        Campaign(id="c_quiz_brand", status="paused", name="Quiz · brand awareness",
                 objective="REACH", game_type="quiz", reward="brand recall",
                 schedule="all day", audience_name="All (Bedok 500m)",
                 spend_sgd=0.0, impressions=0, plays=0, new_customers=0,
                 cpa_sgd=0.0, ctr_pct=0.0),
        Campaign(id="c_streak_retention", status="review", name="Streak · retention",
                 objective="REPEAT", game_type="streak", reward="upgraded reward",
                 schedule="all day", audience_name="Returning (last 30d)",
                 spend_sgd=0.0, impressions=0, plays=0, new_customers=0,
                 cpa_sgd=0.0, ctr_pct=0.0),
    ]


# ── Endpoints ──

@router.get("/overview", response_model=StatusStrip)
async def get_overview():
    return StatusStrip(
        wallet_sgd=847, new_customers_7d=147, new_customers_7d_delta_pct=22.0,
        campaigns_live=3, runway_days=12, burn_per_day_sgd=70, health="green",
    )


@router.get("/metrics", response_model=list[MetricCard])
async def get_metrics():
    return [
        MetricCard(label="Impressions (game views)", value="14,238", delta_pct=18.0,
                   delta_direction="up", sub_label="~2,034/day avg",
                   benchmark_note="geofence active 24/7"),
        MetricCard(label="Plays · clicks", value="3,892", delta_pct=12.0,
                   delta_direction="up", sub_label="~556/day · spin most popular",
                   benchmark_note="27.3% CTR"),
        MetricCard(label="Verified new customers", value="147", delta_pct=22.0,
                   delta_direction="up", sub_label="Cohort tracking · 23 returned in 14d",
                   benchmark_note="3.8% conv"),
        MetricCard(label="Spent · CPA", value="S$720 · S$4.90",
                   delta_pct=-14.0, delta_direction="down",
                   sub_label="Excellent for kopi (band ≤S$2.50)",
                   benchmark_note="14% lower vs last month"),
    ]


@router.get("/campaigns", response_model=list[Campaign])
async def list_campaigns(
    status_filter: Optional[str] = Query(None, alias="status"),
    objective: Optional[str] = None,
):
    rows = _deterministic_campaigns()
    if status_filter:
        rows = [c for c in rows if c.status == status_filter]
    if objective:
        rows = [c for c in rows if c.objective == objective]
    return rows


@router.get("/campaigns/{campaign_id}", response_model=Campaign)
async def get_campaign(campaign_id: str):
    for c in _deterministic_campaigns():
        if c.id == campaign_id:
            return c
    raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found")


class CampaignCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    objective: str = Field(..., pattern=r"^(NEW|REPEAT|REACH|VISIT|BOOK)$")
    game_type: str
    reward: str
    schedule: str
    audience_id: str
    daily_budget_sgd: float = Field(..., gt=0, le=1000)


@router.post("/campaigns", response_model=Campaign, status_code=status.HTTP_201_CREATED)
async def create_campaign(payload: CampaignCreate):
    cid = f"c_{payload.name.lower().replace(' ', '_')[:30]}"
    return Campaign(id=cid, status="review", name=payload.name,
                    objective=payload.objective, game_type=payload.game_type,
                    reward=payload.reward, schedule=payload.schedule,
                    audience_name=payload.audience_id, spend_sgd=0.0,
                    impressions=0, plays=0, new_customers=0,
                    cpa_sgd=0.0, ctr_pct=0.0)


@router.get("/audiences", response_model=list[Audience])
async def list_audiences():
    return [
        Audience(id="aud_bedok_200m", name="Bedok · 200m geofence", type="geofence",
                 size_estimate=18000, geofence_m=200, created_at="2026-03-15",
                 last_used_at="2026-05-31"),
        Audience(id="aud_first_time", name="First-time customers (cross-brand)", type="custom",
                 size_estimate=42000, created_at="2026-04-02",
                 last_used_at="2026-05-31"),
        Audience(id="aud_lookalike_kopi", name="Lookalike · frequent kopi SG", type="lookalike",
                 size_estimate=85000, created_at="2026-04-22",
                 last_used_at="2026-05-30"),
        Audience(id="aud_returning_30d", name="Returning (last 30d)", type="retargeting",
                 size_estimate=3400, created_at="2026-05-01",
                 last_used_at="2026-05-29"),
    ]


@router.get("/creatives", response_model=list[Creative])
async def list_creatives():
    return [
        Creative(id="g_spin", name="Lunch spin", game_type="spin", emoji="🎯",
                 description="Classic prize wheel · most popular", plays_today=87,
                 cpa_sgd=4.20, is_active=True),
        Creative(id="g_scratch", name="Scratch & win", game_type="scratch", emoji="⚡",
                 description="Scratch reveal · fast", plays_today=42,
                 cpa_sgd=5.10, is_active=True),
        Creative(id="g_mystery", name="Mystery box", game_type="mystery", emoji="🎲",
                 description="Curiosity-driven · best for premium offers",
                 plays_today=18, cpa_sgd=6.80, is_active=True),
        Creative(id="g_quiz", name="Quiz · brand recall", game_type="quiz", emoji="🧩",
                 description="2-question quiz · best for brand awareness",
                 plays_today=0, cpa_sgd=0.0, is_active=False),
        Creative(id="g_streak", name="Streak", game_type="streak", emoji="🔥",
                 description="N-day check-in · best for repeat customers",
                 plays_today=0, cpa_sgd=0.0, is_active=False),
    ]


@router.get("/reports/cohort", response_model=list[CohortRow])
async def cohort_report(days: int = Query(14, ge=1, le=90)):
    base = datetime(2026, 5, 31)
    rows = []
    for i in range(min(days, 14)):
        d = (base - timedelta(days=14 - i)).strftime("%Y-%m-%d")
        new = 7 + (i * 2) % 18
        rows.append(CohortRow(cohort_day=d, new_customers=new,
                              returned_1d=int(new * 0.08),
                              returned_7d=int(new * 0.18),
                              returned_14d=int(new * 0.28),
                              returned_30d=int(new * 0.35)))
    return rows


@router.get("/reports/funnel", response_model=list[FunnelStep])
async def funnel_report():
    return [
        FunnelStep(step="Impressions", count=14238),
        FunnelStep(step="Plays", count=3892, conversion_pct=27.3),
        FunnelStep(step="Winners", count=2179, conversion_pct=56.0),
        FunnelStep(step="Redeemed at counter", count=412, conversion_pct=18.9),
        FunnelStep(step="Verified new customers", count=147, conversion_pct=35.7),
        FunnelStep(step="Returned in 14 days", count=42, conversion_pct=28.6),
    ]


@router.get("/audience-breakdown", response_model=list[AudienceBreakdown])
async def audience_breakdown():
    return [
        AudienceBreakdown(source="Geofence walk-by", count=88, pct=60.0, color="#00B341"),
        AudienceBreakdown(source="Friend referral", count=29, pct=20.0, color="#0EA5E9"),
        AudienceBreakdown(source="Cross-brand network", count=18, pct=12.0, color="#FBBF24"),
        AudienceBreakdown(source="Other", count=12, pct=8.0, color="#A78BFA"),
    ]


@router.get("/activity/live", response_model=list[FeedItem])
async def live_activity(limit: int = Query(20, ge=1, le=200)):
    items = [
        FeedItem(timestamp="13:24", type="win", kid="kid_8a3f",
                 campaign="Lunch spin", detail="won S$1 off kopi · 200m geofence"),
        FeedItem(timestamp="13:18", type="redeem", kid="kid_8a3f",
                 campaign="Lunch spin", detail="counter #1 · 4-digit code · S$0.05 take rate"),
        FeedItem(timestamp="13:11", type="new", kid="kid_9b21",
                 campaign="Lunch spin", detail="FIRST visit ever · attribution 7d"),
        FeedItem(timestamp="12:58", type="win", kid="kid_4e89",
                 campaign="Scratch & win", detail="won free kaya toast"),
        FeedItem(timestamp="12:55", type="redeem", kid="kid_4e89",
                 campaign="Scratch & win", detail="counter #2"),
        FeedItem(timestamp="12:51", type="new", kid="kid_7c33",
                 campaign="Mystery box", detail="FIRST visit ever"),
    ]
    return items[:limit]


@router.get("/wallet", response_model=Wallet)
async def wallet_status():
    return Wallet(balance_sgd=847.0, auto_recharge_at_sgd=200.0,
                  burn_per_day_sgd=70.0, runway_days=12)


@router.post("/wallet/topup", response_model=Wallet, status_code=status.HTTP_200_OK)
async def wallet_topup(payload: TopupRequest):
    new_balance = 847.0 + payload.amount_sgd
    new_runway = int(new_balance / 70.0)
    return Wallet(balance_sgd=new_balance, auto_recharge_at_sgd=200.0,
                  burn_per_day_sgd=70.0, runway_days=new_runway)
