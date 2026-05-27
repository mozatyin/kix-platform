"""KiX callback receiver — receives ELTM build progress/finished callbacks.

POST /internal/eltm/callback
  - progress: updates order hset "progress_message"
  - finished: updates order status (completed/spec_ready/failed)
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()

_REDIS_ORDER_PREFIX = "game_order:"
_CALLBACK_SECRET = os.environ.get("ELTM_CALLBACK_SECRET", "")


async def _verify_callback_secret(request: Request):
    if not _CALLBACK_SECRET:
        return
    header = request.headers.get("X-ELTM-Callback-Secret", "")
    if header != _CALLBACK_SECRET:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid callback secret",
        )


@router.post("/callback")
async def eltm_callback(
    body: dict,
    request: Request,
    r=Depends(get_redis),
):
    """Receive ELTM build progress / finished callbacks."""
    await _verify_callback_secret(request)

    order_id = body.get("order_id", "")
    event = body.get("event", "")

    if not order_id:
        raise HTTPException(status_code=400, detail="'order_id' is required")

    order_key = f"{_REDIS_ORDER_PREFIX}{order_id}"
    existing = await r.hgetall(order_key)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")

    if event == "progress":
        message = body.get("message", "")
        await r.hset(order_key, "progress_message", message[:500])
        logger.info("Order %s progress: %s", order_id[:8], message[:80])
        return {"ok": True, "order_id": order_id, "status": existing.get("status", "building")}

    if event == "finished":
        result = body.get("result", {})
        ok = result.get("ok", False)
        spec_available = result.get("spec_available", False)

        if ok:
            new_status = "completed"
            await r.hset(order_key, mapping={
                "status": "completed",
                "game_file": result.get("relative_path", ""),
                "game_name": result.get("game_name", ""),
                "game_slug": result.get("game_slug", ""),
                "elapsed_s": str(result.get("elapsed_s", 0)),
                "error": "",
            })
        elif spec_available:
            new_status = "spec_ready"
            await r.hset(order_key, mapping={
                "status": "spec_ready",
                "game_file": "",
                "game_name": result.get("game_name", ""),
                "game_slug": result.get("game_slug", ""),
                "error": result.get("error", "")[:500],
            })
        else:
            new_status = "failed"
            await r.hset(order_key, mapping={
                "status": "failed",
                "error": result.get("error", "")[:500],
            })

        logger.info(
            "Order %s %s: game=%s file=%s",
            order_id[:8], new_status,
            result.get("game_slug", ""),
            result.get("relative_path", ""),
        )
        return {"ok": True, "order_id": order_id, "status": new_status}

    raise HTTPException(status_code=400, detail=f"Unknown event type: {event}")
