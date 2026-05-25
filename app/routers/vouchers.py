"""Vouchers router — brand portal voucher management.

Provides CSV upload for bulk voucher ingestion. Voucher codes are validated
(4-50 chars, A-Z0-9- only), deduplicated, and inserted into the voucher_pool
table.
"""

from __future__ import annotations

import io
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import VoucherPool
from app.schemas import VoucherListItem, VoucherListResponse, VoucherSummary, VoucherUploadResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# Valid voucher code pattern: 4-50 chars, uppercase letters, digits, hyphens
_CODE_PATTERN = re.compile(r"^[A-Z0-9\-]{4,50}$")


@router.get(
    "/{brand_id}/vouchers",
    response_model=VoucherListResponse,
    summary="List vouchers for a brand",
    description="Returns all vouchers for the brand with a status summary.",
)
async def list_vouchers(
    brand_id: str,
    db: AsyncSession = Depends(get_db),
) -> VoucherListResponse:
    """List all vouchers for a brand with status summary."""
    result = await db.execute(
        select(VoucherPool).where(VoucherPool.brand_id == brand_id)
    )
    vouchers = result.scalars().all()

    items: list[VoucherListItem] = []
    summary_counts: dict[str, int] = {"available": 0, "assigned": 0, "redeemed": 0, "expired": 0}

    for v in vouchers:
        items.append(VoucherListItem(
            code=v.code,
            tier=v.tier,
            status=v.status,
            description=v.description,
        ))
        if v.status in summary_counts:
            summary_counts[v.status] += 1

    return VoucherListResponse(
        vouchers=items,
        summary=VoucherSummary(**summary_counts),
    )


@router.post(
    "/{brand_id}/vouchers/upload",
    response_model=VoucherUploadResponse,
    summary="Upload voucher codes via CSV",
    description=(
        "Upload a CSV file containing voucher codes (one code per line). "
        "Codes must be 4-50 characters, uppercase letters, digits, and "
        "hyphens only. Duplicates are skipped."
    ),
    status_code=status.HTTP_201_CREATED,
)
async def upload_vouchers(
    brand_id: str,
    file: UploadFile = File(..., description="CSV file with one voucher code per line"),
    tier: str = Query(..., description="Voucher tier: bronze, silver, or gold"),
    description: str = Query("", description="Human-readable voucher description"),
    valid_days: int = Query(30, ge=1, le=365, description="Days until voucher expires after assignment"),
    db: AsyncSession = Depends(get_db),
) -> VoucherUploadResponse:
    """Upload voucher codes from a CSV file for a specific brand and tier.

    Each line of the CSV is treated as a single voucher code. Codes are
    normalized to uppercase and validated against the pattern [A-Z0-9-]{4,50}.
    """
    # Validate tier
    valid_tiers = {"bronze", "silver", "gold"}
    if tier not in valid_tiers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid tier '{tier}'. Must be one of: {', '.join(sorted(valid_tiers))}",
        )

    # Read and decode the uploaded file
    content = await file.read()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File must be UTF-8 encoded",
        )

    imported = 0
    skipped_duplicates = 0
    errors: list[dict] = []

    lines = text.strip().splitlines()
    if not lines:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File is empty",
        )

    for line_num, raw_line in enumerate(lines, start=1):
        code = raw_line.strip().upper()

        # Skip empty lines and header-like lines
        if not code or code.startswith("#"):
            continue

        # Validate format
        if not _CODE_PATTERN.match(code):
            errors.append({
                "line": line_num,
                "code": code[:50],
                "error": "invalid_format",
            })
            continue

        # Try to insert, catching unique constraint violations
        voucher = VoucherPool(
            brand_id=brand_id,
            code=code,
            tier=tier,
            description=description or None,
            status="available",
        )
        db.add(voucher)

        try:
            await db.flush()
            imported += 1
        except IntegrityError:
            await db.rollback()
            skipped_duplicates += 1
            logger.debug("Duplicate voucher code skipped: %s", code)

    logger.info(
        "Voucher upload: brand=%s tier=%s imported=%d skipped=%d errors=%d",
        brand_id,
        tier,
        imported,
        skipped_duplicates,
        len(errors),
    )

    return VoucherUploadResponse(
        imported=imported,
        skipped_duplicates=skipped_duplicates,
        errors=errors,
    )
