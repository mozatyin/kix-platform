"""Public read-only API for the 100-free-per-country slot mechanic.

Backs `landing/pricing.html` (the public counter) and the alpha-signup
flow (`POST /api/v1/country-slots/claim`).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.services import country_slots as svc


router = APIRouter(prefix="/api/v1/country-slots", tags=["country-slots"])


class SlotSummary(BaseModel):
    country_code: str
    total: int
    claimed: int
    remaining: int


class SlotClaimRequest(BaseModel):
    country_code: str = Field(..., min_length=2, max_length=2)
    brand_id: str = Field(..., min_length=1)


class SlotClaimResponse(BaseModel):
    country_code: str
    slot_number: int
    brand_id: str
    claimed_at: float
    founding: bool = True
    take_rate_bps: int = 0  # 0 forever for founding merchants


class OpenCountriesResponse(BaseModel):
    countries: list[SlotSummary]


@router.get("/{country_code}", response_model=SlotSummary)
async def get_country_summary(country_code: str) -> SlotSummary:
    """Public counter: how many founding slots remain in {country_code}?"""
    if not country_code or len(country_code) != 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="country_code must be ISO 3166-1 alpha-2 (2 chars)",
        )
    summary = await svc.get_summary(country_code)
    return SlotSummary(**summary)


@router.get("/", response_model=OpenCountriesResponse)
async def list_countries(limit: int = 20) -> OpenCountriesResponse:
    """Top N countries with open founding slots."""
    if limit < 1 or limit > 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="limit must be 1-200",
        )
    countries = await svc.list_open_countries(limit=limit)
    return OpenCountriesResponse(
        countries=[SlotSummary(**c) for c in countries]
    )


@router.post(
    "/claim", response_model=SlotClaimResponse, status_code=201
)
async def claim_slot(body: SlotClaimRequest) -> SlotClaimResponse:
    """Atomically claim a founding slot for this brand.

    - Idempotent: same brand_id calling twice returns the same slot.
    - Returns 409 if all 100 slots in country are taken.
    """
    claim = await svc.claim_slot(body.country_code, body.brand_id)
    if claim is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"All 100 founding slots in {body.country_code} are taken. "
                f"You can still join — normal CPA/CPS rates apply."
            ),
        )
    return SlotClaimResponse(
        country_code=claim.country_code,
        slot_number=claim.slot_number,
        brand_id=claim.brand_id,
        claimed_at=claim.claimed_at,
        founding=claim.founding,
    )


@router.get("/brand/{brand_id}/status")
async def get_brand_founding_status(brand_id: str) -> dict:
    """Is this brand a founding merchant? Returns take-rate."""
    is_f = await svc.is_founding(brand_id)
    return {
        "brand_id": brand_id,
        "is_founding": is_f,
        "take_rate_bps": 0 if is_f else 500,  # 0% vs 5% default
    }
