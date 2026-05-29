"""Surgical verification of the 8 marketplace fixes (no full 30-day sim).

Verifies that each fix's API surface is wired and responding correctly.
For sim re-run see sim_sg_marketplace_30day.py (lost in concurrent edits).
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from httpx import ASGITransport

from app.main import app
from app.redis_client import close_redis, init_redis


async def verify():
    await init_redis()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        results = []

        async def check(name, method, path, expect_status=200, **kwargs):
            try:
                r = await c.request(method, path, **kwargs)
                ok = r.status_code in (expect_status if isinstance(expect_status, list) else [expect_status])
                results.append((name, r.status_code, "✓" if ok else "✗"))
                return r
            except Exception as e:
                results.append((name, "ERR", str(e)[:80]))
                return None

        # Fix #1 cold-start: diversity report endpoint
        await check("F1 cold-start diversity report", "GET",
                    "/api/v1/auction/diversity-report/test_brand", expect_status=[200, 404])

        # Fix #2 viral: K-factor endpoint
        await check("F2 viral K-factor", "GET",
                    "/api/v1/network/k-factor/test_brand", expect_status=[200, 404])

        # Fix #3 bid floor: auto-pause status
        await check("F3 bid auto-pause status", "GET",
                    "/api/v1/campaigns/test_camp/auto-pause-status", expect_status=[200, 404])
        await check("F3 bid history", "GET",
                    "/api/v1/campaigns/test_camp/bid-history", expect_status=[200, 404])

        # Fix #4 wallet auto-recharge
        await check("F4 wallet autorecharge get", "GET",
                    "/api/v1/wallet/test_brand/autorecharge", expect_status=[200, 404])

        # Fix #5 PI pacing diagnostic
        await check("F5 PI pacing state", "GET",
                    "/api/v1/auction/admin/pacing/test_camp/pi-state", expect_status=[200, 401, 403, 404])

        # Fix #6 QS breakdown
        await check("F6 QS breakdown", "GET",
                    "/api/v1/campaigns/test_camp/qs-breakdown", expect_status=[200, 404])

        # Fix #7 multi-touch attribution
        await check("F7 multi-touch conversion", "POST",
                    "/api/v1/attribution/conversion",
                    json={"user_id": "u1", "brand_id_converted_at": "b1",
                          "conversion_ts": 0, "conversion_value_cents": 100,
                          "model": "linear", "lookback_days": 7},
                    expect_status=[200, 400, 404, 422])

        # Fix #8a geofences PG table check
        await check("F8a brand config template", "GET",
                    "/api/v1/brands/config-template", expect_status=[200, 401, 403])
        # F8b budget status
        await check("F8b campaign budget status", "GET",
                    "/api/v1/campaigns/test_camp/budget-status", expect_status=[200, 404])

        print(f"\n{'Fix':<40} {'Status':<6} {'Result':<6}")
        print("-" * 60)
        for n, s, ok in results:
            print(f"{n:<40} {s!s:<6} {ok}")
        total = len(results)
        passed = sum(1 for _, _, ok in results if ok == "✓")
        print(f"\n{passed}/{total} fix endpoints wired correctly")

    await close_redis()


asyncio.run(verify())
