"""Users router — user-facing voucher endpoints.

Requires JWT authentication. Provides voucher listing and self-report
redemption ("I have used it" button).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models import BrandConfig, VoucherPool
from app.schemas import VoucherResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/me/vouchers",
    response_model=list[VoucherResponse],
    summary="List my vouchers",
    description=(
        "Retrieve all vouchers assigned to the authenticated user, "
        "including brand name and redemption status."
    ),
)
async def list_my_vouchers(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[VoucherResponse]:
    """List all vouchers belonging to the current user.

    Joins with brand_configs to include brand_name in the response.
    Results are ordered by assignment date, most recent first.
    """
    user_id = current_user["sub"]

    result = await db.execute(
        select(
            VoucherPool.code,
            VoucherPool.description,
            VoucherPool.tier,
            VoucherPool.assigned_at,
            VoucherPool.expires_at,
            VoucherPool.status,
            BrandConfig.brand_name,
        )
        .join(BrandConfig, BrandConfig.brand_id == VoucherPool.brand_id)
        .where(VoucherPool.assigned_to == user_id)
        .order_by(VoucherPool.assigned_at.desc())
    )

    rows = result.all()

    return [
        VoucherResponse(
            code=row.code,
            description=row.description,
            tier=row.tier,
            assigned_at=row.assigned_at.isoformat() if row.assigned_at else None,
            expires_at=row.expires_at.isoformat() if row.expires_at else None,
            status=row.status,
            brand_name=row.brand_name,
        )
        for row in rows
    ]


@router.post(
    "/me/vouchers/{voucher_id}/redeem",
    summary="Self-report voucher redemption",
    description=(
        'Mark a voucher as redeemed (self-report "I have used it" button). '
        "The voucher must belong to the current user, not already be redeemed, "
        "and not be expired."
    ),
)
async def redeem_voucher(
    voucher_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Self-report redemption of a voucher.

    Validates ownership, checks expiry, and updates status to 'redeemed'.
    """
    user_id = current_user["sub"]
    now = datetime.now(timezone.utc)

    # Fetch the voucher
    result = await db.execute(
        select(VoucherPool).where(VoucherPool.id == voucher_id)
    )
    voucher = result.scalar_one_or_none()

    if voucher is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Voucher not found",
        )

    # Ownership check
    if str(voucher.assigned_to) != str(user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Voucher does not belong to you",
        )

    # Already redeemed check
    if voucher.status == "redeemed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Voucher already redeemed",
        )

    # Expiry check
    if voucher.expires_at is not None and voucher.expires_at < now:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Voucher has expired",
        )

    # Status must be 'assigned' to redeem
    if voucher.status != "assigned":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Voucher cannot be redeemed (current status: {voucher.status})",
        )

    # Mark as redeemed
    await db.execute(
        update(VoucherPool)
        .where(VoucherPool.id == voucher_id)
        .values(status="redeemed", redeemed_at=now)
    )

    logger.info(
        "Voucher %s redeemed by user %s",
        voucher.code,
        user_id,
    )

    return {
        "status": "redeemed",
        "redeemed_at": now.isoformat(),
    }
