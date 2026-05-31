"""KiX callback receiver — receives ELTM build progress/finished callbacks.

POST /internal/eltm/callback
  - progress: updates order hset "progress_message"
  - finished: updates order status (completed/spec_ready/failed)
"""

from __future__ import annotations

import json
import logging
import os

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.redis_client import get_redis
from app.services.verdict_gate import GateDecision, stub_evaluator, verdict_gate

logger = logging.getLogger(__name__)

router = APIRouter()

_REDIS_ORDER_PREFIX = "game_order:"
_CALLBACK_SECRET = os.environ.get("ELTM_CALLBACK_SECRET", "")

# CLASS-H structural fix: persona verdict gate.
# Threshold + persona set are env-tunable so ops can dial up/down
# without a deploy. Floor (per-persona min) catches "great-on-average
# but breaks for one shop owner" pattern. See docs/all-bugs-catalog.md.
_VERDICT_THRESHOLD = float(os.environ.get("VERDICT_GATE_THRESHOLD", "60"))
_VERDICT_MIN_FLOOR = float(os.environ.get("VERDICT_GATE_MIN_FLOOR", "30"))
_VERDICT_PERSONAS = [
    p.strip() for p in os.environ.get(
        "VERDICT_GATE_PERSONAS",
        "aminah_first_time_merchant,skeptical_owner,consumer",
    ).split(",") if p.strip()
]
# Root for resolving ELTM `relative_path` to filesystem. ELTM writes to
# its own repo; this points at where the KiX static mount picks them up.
_GAME_ROOT = Path(os.environ.get(
    "ELTM_GAME_ROOT",
    str(Path(__file__).resolve().parents[2] / "landing" / "games"),
))


def _read_game_html(relative_path: str) -> str | None:
    """Resolve ELTM relative_path → file contents. Return None on miss."""
    if not relative_path:
        return None
    candidates = [
        _GAME_ROOT / relative_path,
        _GAME_ROOT / Path(relative_path).name,
        Path(relative_path),  # in case absolute path passed
    ]
    for p in candidates:
        try:
            if p.is_file():
                return p.read_text()
        except Exception as e:
            logger.warning("game_html read failed at %s: %s", p, e)
            continue
    return None


def _run_verdict_gate(html: str) -> GateDecision:
    """Run the configured personas through the gate. Stub evaluator by default;
    in production, ops swaps in `make_llm_evaluator(...)` via DI in app startup."""
    return verdict_gate(
        html,
        _VERDICT_PERSONAS,
        stub_evaluator,
        threshold=_VERDICT_THRESHOLD,
        min_score_floor=_VERDICT_MIN_FLOOR,
        require_majority_pass=True,
    )


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
            # CLASS-H gate: run generated HTML past N personas before
            # marking completed. Reject → status=failed with reasons.
            relative_path = result.get("relative_path", "")
            html = _read_game_html(relative_path)
            gate_status = "skipped"
            gate_avg = 0.0
            gate_min = 0.0
            gate_reasons = ""

            if html is None:
                gate_status = "no-file"
                logger.warning(
                    "Order %s: verdict gate skipped — file not found at %s",
                    order_id[:8], relative_path,
                )
            else:
                try:
                    decision = _run_verdict_gate(html)
                    gate_avg = decision.avg_score
                    gate_min = decision.min_score
                    gate_reasons = " | ".join(decision.rejection_reasons)[:300]
                    gate_status = "accepted" if decision.accepted else "rejected"
                except Exception as e:
                    logger.exception("verdict_gate raised on order %s: %s",
                                     order_id[:8], e)
                    gate_status = "error"

            if gate_status == "rejected":
                new_status = "failed"
                await r.hset(order_key, mapping={
                    "status": "failed",
                    "game_file": relative_path,  # keep for forensics
                    "game_name": result.get("game_name", ""),
                    "game_slug": result.get("game_slug", ""),
                    "elapsed_s": str(result.get("elapsed_s", 0)),
                    "error": f"verdict_gate rejected (avg={gate_avg}, min={gate_min}): {gate_reasons}"[:500],
                    "verdict_avg": str(gate_avg),
                    "verdict_min": str(gate_min),
                    "verdict_status": gate_status,
                })
            else:
                new_status = "completed"
                await r.hset(order_key, mapping={
                    "status": "completed",
                    "game_file": relative_path,
                    "game_name": result.get("game_name", ""),
                    "game_slug": result.get("game_slug", ""),
                    "elapsed_s": str(result.get("elapsed_s", 0)),
                    "error": "",
                    "verdict_avg": str(gate_avg),
                    "verdict_min": str(gate_min),
                    "verdict_status": gate_status,
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
