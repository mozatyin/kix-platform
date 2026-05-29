"""ELTM Creative Generator integration router.

Merchants can request a fresh AI-generated game (HTML creative) for a
campaign. This router proxies the request to the ELTM HTTP service
(default http://localhost:8001), tracks job status in Redis, exposes
approve/reject + A/B test selection, and lets the auction engine pick a
creative variant when serving impressions.

Storage (Redis):
    creative:{creative_id}            HASH
        brand_id, campaign_id?, name, status, spec_json,
        eltm_job_id, eltm_order_id, html_url, thumbnail_url,
        generated_at, eltm_quality_score, rejection_reason, ab_test_id
    brand:{bid}:creatives             SET of creative_ids
    campaign:{cid}:ab_test            HASH {ab_test_id, creative_ids,
                                            traffic_split, started_at}
    ab_test:{tid}                     HASH {campaign_id, creative_ids,
                                            traffic_split, started_at}
    ab_test:{tid}:stats               HASH (per-creative
                                            impressions/clicks/conversions)
    creative_gen:queue                LIST (FIFO of creative_ids polling
                                            ELTM)

A/B auction integration:
    When the auction picks a winning campaign, it should call
    ``pick_creative_for_campaign(r, campaign_id)`` from this module.
    That returns the actual creative_id to render (per traffic_split)
    and atomically increments the impression counter — record the
    returned creative_id inside the impression token so click/conv
    callbacks can attribute correctly.

Endpoints (all under prefix ``/api/v1/creative-gen``):
    POST   /request                              create + kick build
    GET    /{creative_id}                        get status
    POST   /{creative_id}/approve                merchant approves
    POST   /{creative_id}/reject                 merchant rejects + reason
    GET    /brand/{brand_id}                     list all for brand
    POST   /{creative_id}/attach-to-campaign     bind creative → campaign
    GET    /{creative_id}/preview                iframe-able URL
    POST   /ab-test/create                       multi-variant test
    GET    /ab-test/{ab_test_id}/winner          pick best variant
    POST   /ab-test/{ab_test_id}/record          record imp/click/conv
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import uuid
from typing import Any, Literal

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.redis_client import get_redis

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Config ────────────────────────────────────────────────────────────────

ELTM_BASE_URL = os.environ.get("ELTM_BASE_URL", "http://localhost:8001")
ELTM_BUILD_PATH = "/v1/kix/build-for-business"
ELTM_READY_PATH = "/ready"
ELTM_TIMEOUT_S = float(os.environ.get("ELTM_TIMEOUT_S", "300"))  # 5 min
ELTM_POLL_INTERVAL_S = float(os.environ.get("ELTM_POLL_INTERVAL_S", "10"))

# KiX callback the ELTM async build will POST back to.
KIX_PUBLIC_URL = os.environ.get("KIX_PUBLIC_URL", "http://localhost:8000")
KIX_CALLBACK_PATH = "/internal/eltm/callback"

# Redis key prefixes
_K_CREATIVE = "creative:{cid}"
_K_BRAND_CREATIVES = "brand:{bid}:creatives"
_K_CAMPAIGN_ABTEST = "campaign:{cid}:ab_test"
_K_ABTEST = "ab_test:{tid}"
_K_ABTEST_STATS = "ab_test:{tid}:stats"
_K_QUEUE = "creative_gen:queue"
_K_GAME_ORDER = "game_order:{oid}"  # written by ELTM callback router

VALID_STATUSES = {"queued", "generating", "ready", "approved", "rejected", "failed"}


# ── Pydantic models ───────────────────────────────────────────────────────

GameType = Literal[
    "match3", "runner", "trivia", "merge", "puzzle",
    "shooter", "tap", "swipe", "casino", "card",
]
Goal = Literal["engagement", "acquisition", "retention", "upsell"]
Reward = Literal["voucher", "points", "item"]


class CreativeSpec(BaseModel):
    game_type: GameType
    brand_description: str = Field(min_length=3, max_length=2000)
    brand_color: str = Field(default="#8B4513", pattern=r"^#[0-9A-Fa-f]{6}$")
    goal: Goal = "engagement"
    reward: Reward = "voucher"
    voucher_template_id: str | None = None
    duration_seconds: int = Field(default=60, ge=10, le=600)


class CreativeRequest(BaseModel):
    brand_id: str = Field(min_length=1)
    campaign_id: str | None = None
    name: str = Field(min_length=1, max_length=120)
    spec: CreativeSpec


class CreativeRequestResponse(BaseModel):
    creative_id: str
    status: str
    job_id: str


class CreativeStatus(BaseModel):
    creative_id: str
    brand_id: str
    campaign_id: str | None = None
    name: str
    status: str
    html_url: str | None = None
    thumbnail_url: str | None = None
    generated_at: float | None = None
    eltm_quality_score: float | None = None
    rejection_reason: str | None = None
    spec: dict[str, Any] | None = None


class RejectBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class AttachBody(BaseModel):
    campaign_id: str


class ABTestCreate(BaseModel):
    campaign_id: str
    creative_ids: list[str] = Field(min_length=2, max_length=10)
    traffic_split: list[float] | None = None


class ABTestRecord(BaseModel):
    creative_id: str
    event: Literal["impression", "click", "conversion"]


# ── Redis helpers ─────────────────────────────────────────────────────────

def _k(template: str, **kw) -> str:
    return template.format(**kw)


def _now() -> float:
    return time.time()


async def _load_creative(r, creative_id: str) -> dict[str, Any]:
    raw = await r.hgetall(_k(_K_CREATIVE, cid=creative_id))
    if not raw:
        raise HTTPException(404, detail=f"creative {creative_id} not found")
    return raw


def _hydrate(raw: dict[str, Any]) -> dict[str, Any]:
    """Decode a creative HASH row into a typed dict."""
    spec = {}
    if raw.get("spec_json"):
        try:
            spec = json.loads(raw["spec_json"])
        except Exception:
            spec = {}
    generated_at = float(raw["generated_at"]) if raw.get("generated_at") else None
    quality = (
        float(raw["eltm_quality_score"])
        if raw.get("eltm_quality_score")
        else None
    )
    return {
        "creative_id": raw.get("creative_id", ""),
        "brand_id": raw.get("brand_id", ""),
        "campaign_id": raw.get("campaign_id") or None,
        "name": raw.get("name", ""),
        "status": raw.get("status", "queued"),
        "html_url": raw.get("html_url") or None,
        "thumbnail_url": raw.get("thumbnail_url") or None,
        "generated_at": generated_at,
        "eltm_quality_score": quality,
        "rejection_reason": raw.get("rejection_reason") or None,
        "spec": spec,
    }


# ── ELTM integration ──────────────────────────────────────────────────────

async def _eltm_alive() -> bool:
    """Best-effort readiness check, never raises."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{ELTM_BASE_URL}{ELTM_READY_PATH}")
            return resp.status_code == 200
    except Exception as exc:
        logger.warning("ELTM ready probe failed: %s", exc)
        return False


def _spec_to_eltm_payload(
    spec: dict[str, Any],
    *,
    brand_id: str,
    order_id: str,
    callback_url: str,
) -> dict[str, Any]:
    """Map our CreativeSpec into ELTM's build-for-business body."""
    brand_desc = spec.get("brand_description", "")
    game_type = spec.get("game_type", "match3")
    color = spec.get("brand_color", "#8B4513")
    goal = spec.get("goal", "engagement")
    enriched = (
        f"{brand_desc}\n\n"
        f"[KiX hints] game_type={game_type}, brand_color={color}, "
        f"goal={goal}, duration_s={spec.get('duration_seconds', 60)}"
    )
    return {
        "order_id": order_id,
        "brand_id": brand_id,
        "business_description": enriched,
        # ELTM expects an existing game_slug from the catalogue; we use
        # ``game_type`` as a coarse hint — ELTM falls back to ranking if
        # the slug is unknown.
        "game_slug": game_type,
        "callback_url": callback_url,
    }


async def _submit_to_eltm(
    creative_id: str,
    brand_id: str,
    spec: dict[str, Any],
) -> tuple[str, str]:
    """Submit a build request to ELTM.

    Returns ``(eltm_job_id, eltm_order_id)``. Raises on transport errors.
    """
    order_id = f"ord_{uuid.uuid4().hex[:10]}"
    callback_url = f"{KIX_PUBLIC_URL.rstrip('/')}{KIX_CALLBACK_PATH}"
    payload = _spec_to_eltm_payload(
        spec, brand_id=brand_id, order_id=order_id, callback_url=callback_url,
    )
    async with httpx.AsyncClient(timeout=ELTM_TIMEOUT_S) as client:
        resp = await client.post(
            f"{ELTM_BASE_URL}{ELTM_BUILD_PATH}", json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
    job_id = data.get("job_id") or f"job_{uuid.uuid4().hex[:8]}"
    return job_id, order_id


async def _kick_build(r, creative_id: str) -> None:
    """Background task: submit to ELTM, then poll the callback-fed
    game_order:{order_id} row until terminal status, mirroring the result
    back into the creative hash.
    """
    creative_key = _k(_K_CREATIVE, cid=creative_id)
    raw = await r.hgetall(creative_key)
    if not raw:
        return
    brand_id = raw.get("brand_id", "")
    spec = json.loads(raw.get("spec_json") or "{}")

    # Phase 1: submit ---------------------------------------------------
    await r.hset(creative_key, "status", "generating")
    if not await _eltm_alive():
        await r.hset(creative_key, mapping={
            "status": "failed",
            "rejection_reason": "eltm_offline",
        })
        logger.warning("Creative %s failed: ELTM offline", creative_id)
        return
    try:
        job_id, order_id = await _submit_to_eltm(creative_id, brand_id, spec)
    except Exception as exc:
        await r.hset(creative_key, mapping={
            "status": "failed",
            "rejection_reason": f"eltm_submit_error: {exc}",
        })
        logger.error("Creative %s submit failed: %s", creative_id, exc)
        return

    await r.hset(creative_key, mapping={
        "eltm_job_id": job_id,
        "eltm_order_id": order_id,
    })
    # Seed the order row so the ELTM callback router has somewhere to write.
    order_key = _k(_K_GAME_ORDER, oid=order_id)
    await r.hset(order_key, mapping={
        "status": "building",
        "creative_id": creative_id,
        "brand_id": brand_id,
    })

    # Phase 2: poll for callback writes --------------------------------
    deadline = _now() + ELTM_TIMEOUT_S
    import asyncio
    while _now() < deadline:
        await asyncio.sleep(ELTM_POLL_INTERVAL_S)
        order = await r.hgetall(order_key)
        ostatus = order.get("status", "building")
        if ostatus == "completed":
            game_file = order.get("game_file", "")
            html_url = (
                f"/landing/games/{game_file}" if game_file else ""
            )
            await r.hset(creative_key, mapping={
                "status": "ready",
                "html_url": html_url,
                "thumbnail_url": (
                    html_url.replace(".html", ".png") if html_url else ""
                ),
                "generated_at": str(_now()),
                "eltm_quality_score": order.get("quality_score", ""),
            })
            logger.info("Creative %s ready: %s", creative_id, html_url)
            return
        if ostatus in {"failed", "spec_ready"}:
            await r.hset(creative_key, mapping={
                "status": "failed",
                "rejection_reason": order.get("error", "eltm_build_failed"),
            })
            logger.warning(
                "Creative %s failed: %s",
                creative_id, order.get("error", "")[:120],
            )
            return

    # Timed out
    await r.hset(creative_key, mapping={
        "status": "failed",
        "rejection_reason": "eltm_timeout",
    })


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.post("/request", response_model=CreativeRequestResponse, status_code=202)
async def request_creative(
    body: CreativeRequest,
    background_tasks: BackgroundTasks,
    r=Depends(get_redis),
):
    """Create a creative_id and kick an ELTM build in the background."""
    # ── Tier-quota gate (games) ────────────────────────────────────────
    # Each creative-generation request produces a game-style creative; it
    # consumes the brand's "games" quota. Fail-open if the subscription
    # module is unavailable.
    try:
        from app.routers.brand_subscriptions import check_quota
        allowed, info = await check_quota(body.brand_id, "games", r)
        if not allowed:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "tier_limit_reached",
                    "message": (
                        f"Your {info['tier']} tier allows "
                        f"{info['limit']} games. Upgrade to "
                        f"{info['upgrade_required_to']} for more."
                    ),
                    "tier": info["tier"],
                    "current": info["current"],
                    "limit": info["limit"],
                    "upgrade_required_to": info["upgrade_required_to"],
                },
            )
    except HTTPException:
        raise
    except (ImportError, ValueError):
        # Module not available or unknown resource — fail-open.
        pass

    creative_id = f"crv_{uuid.uuid4().hex[:12]}"
    job_id = f"job_{uuid.uuid4().hex[:8]}"

    mapping = {
        "creative_id": creative_id,
        "brand_id": body.brand_id,
        "campaign_id": body.campaign_id or "",
        "name": body.name,
        "status": "queued",
        "spec_json": body.spec.model_dump_json(),
        "eltm_job_id": job_id,
        "eltm_order_id": "",
        "html_url": "",
        "thumbnail_url": "",
        "generated_at": "",
        "eltm_quality_score": "",
        "rejection_reason": "",
        "ab_test_id": "",
        "created_at": str(_now()),
    }
    pipe = r.pipeline()
    pipe.hset(_k(_K_CREATIVE, cid=creative_id), mapping=mapping)
    pipe.sadd(_k(_K_BRAND_CREATIVES, bid=body.brand_id), creative_id)
    pipe.rpush(_K_QUEUE, creative_id)
    # Tier-quota counter — INCR games_count for this brand.
    pipe.incr(f"brand:{body.brand_id}:games_count")
    await pipe.execute()

    background_tasks.add_task(_kick_build, r, creative_id)

    logger.info(
        "Creative %s queued for brand=%s game_type=%s",
        creative_id, body.brand_id, body.spec.game_type,
    )
    return CreativeRequestResponse(
        creative_id=creative_id, status="queued", job_id=job_id,
    )


@router.get("/brand/{brand_id}")
async def list_brand_creatives(brand_id: str, r=Depends(get_redis)):
    """List every creative for a brand."""
    ids = await r.smembers(_k(_K_BRAND_CREATIVES, bid=brand_id))
    out = []
    for cid in ids:
        raw = await r.hgetall(_k(_K_CREATIVE, cid=cid))
        if raw:
            out.append(_hydrate(raw))
    out.sort(key=lambda x: x.get("generated_at") or 0, reverse=True)
    return {"brand_id": brand_id, "count": len(out), "creatives": out}


@router.get("/{creative_id}", response_model=CreativeStatus)
async def get_creative(creative_id: str, r=Depends(get_redis)):
    """Current status of one creative."""
    raw = await _load_creative(r, creative_id)
    return CreativeStatus(**_hydrate(raw))


@router.post("/{creative_id}/approve")
async def approve_creative(creative_id: str, r=Depends(get_redis)):
    """Merchant approves a ready creative; becomes usable in campaigns."""
    raw = await _load_creative(r, creative_id)
    if raw.get("status") != "ready":
        raise HTTPException(
            409,
            detail=f"creative is {raw.get('status')}, only 'ready' can be approved",
        )
    await r.hset(_k(_K_CREATIVE, cid=creative_id), "status", "approved")
    return {"creative_id": creative_id, "status": "approved"}


@router.post("/{creative_id}/reject")
async def reject_creative(
    creative_id: str, body: RejectBody, r=Depends(get_redis),
):
    """Merchant rejects a creative; can be regenerated later."""
    await _load_creative(r, creative_id)
    await r.hset(_k(_K_CREATIVE, cid=creative_id), mapping={
        "status": "rejected",
        "rejection_reason": body.reason[:500],
    })
    return {"creative_id": creative_id, "status": "rejected"}


@router.post("/{creative_id}/attach-to-campaign")
async def attach_to_campaign(
    creative_id: str, body: AttachBody, r=Depends(get_redis),
):
    """Bind a creative_id onto campaign.creative.

    Only approved (or ready) creatives can be attached. Updates the
    campaign hash's ``creative`` field, preserving other Creative subfields
    where possible.
    """
    raw = await _load_creative(r, creative_id)
    if raw.get("status") not in {"ready", "approved"}:
        raise HTTPException(
            409,
            detail="only ready/approved creatives can attach to a campaign",
        )

    campaign_key = f"campaign:{body.campaign_id}"
    cdata = await r.hgetall(campaign_key)
    if not cdata:
        raise HTTPException(404, detail=f"campaign {body.campaign_id} not found")

    creative_field = {}
    try:
        creative_field = json.loads(cdata.get("creative") or "{}")
    except Exception:
        creative_field = {}
    creative_field["creative_id"] = creative_id
    # convenience mirrors
    spec = {}
    try:
        spec = json.loads(raw.get("spec_json") or "{}")
    except Exception:
        pass
    if spec.get("voucher_template_id"):
        creative_field["voucher_template_id"] = spec["voucher_template_id"]
    if raw.get("html_url"):
        creative_field["html_url"] = raw["html_url"]

    await r.hset(campaign_key, "creative", json.dumps(creative_field))
    await r.hset(
        _k(_K_CREATIVE, cid=creative_id), "campaign_id", body.campaign_id,
    )
    return {
        "creative_id": creative_id,
        "campaign_id": body.campaign_id,
        "creative": creative_field,
    }


@router.get("/{creative_id}/preview")
async def preview_creative(creative_id: str, r=Depends(get_redis)):
    """Return an iframe-able URL for the generated HTML."""
    raw = await _load_creative(r, creative_id)
    if raw.get("status") not in {"ready", "approved"}:
        raise HTTPException(
            409, detail=f"creative is {raw.get('status')}, not previewable",
        )
    html_url = raw.get("html_url") or ""
    if not html_url:
        raise HTTPException(404, detail="no html_url for this creative")
    return {
        "creative_id": creative_id,
        "preview_url": html_url,
        "embed_html": f'<iframe src="{html_url}" frameborder="0" '
                       'allow="autoplay" style="width:100%;height:100%"></iframe>',
    }


# ── A/B testing ───────────────────────────────────────────────────────────

def _normalize_split(n: int, split: list[float] | None) -> list[float]:
    """Validate or default a traffic_split list summing to ~1.0."""
    if split is None:
        each = 1.0 / n
        return [each] * n
    if len(split) != n:
        raise HTTPException(400, detail="traffic_split length must match creative_ids")
    total = sum(split)
    if total <= 0:
        raise HTTPException(400, detail="traffic_split must sum to >0")
    return [s / total for s in split]


@router.post("/ab-test/create")
async def create_ab_test(body: ABTestCreate, r=Depends(get_redis)):
    """Group N creatives under one A/B test on a campaign."""
    split = _normalize_split(len(body.creative_ids), body.traffic_split)

    # Validate each creative
    for cid in body.creative_ids:
        raw = await r.hgetall(_k(_K_CREATIVE, cid=cid))
        if not raw:
            raise HTTPException(404, detail=f"creative {cid} not found")
        if raw.get("status") not in {"ready", "approved"}:
            raise HTTPException(
                409,
                detail=f"creative {cid} is {raw.get('status')}, must be ready/approved",
            )

    ab_test_id = f"ab_{uuid.uuid4().hex[:10]}"
    started_at = _now()
    payload = {
        "ab_test_id": ab_test_id,
        "campaign_id": body.campaign_id,
        "creative_ids": json.dumps(body.creative_ids),
        "traffic_split": json.dumps(split),
        "started_at": str(started_at),
    }

    stats_map: dict[str, str] = {}
    for cid in body.creative_ids:
        stats_map[f"{cid}:imp"] = "0"
        stats_map[f"{cid}:clk"] = "0"
        stats_map[f"{cid}:cvr"] = "0"

    pipe = r.pipeline()
    pipe.hset(_k(_K_ABTEST, tid=ab_test_id), mapping=payload)
    pipe.hset(_k(_K_ABTEST_STATS, tid=ab_test_id), mapping=stats_map)
    pipe.hset(_k(_K_CAMPAIGN_ABTEST, cid=body.campaign_id), mapping=payload)
    for cid in body.creative_ids:
        pipe.hset(_k(_K_CREATIVE, cid=cid), "ab_test_id", ab_test_id)
    await pipe.execute()

    return {
        "ab_test_id": ab_test_id,
        "campaign_id": body.campaign_id,
        "creative_ids": body.creative_ids,
        "traffic_split": split,
    }


@router.get("/ab-test/{ab_test_id}/winner")
async def ab_test_winner(ab_test_id: str, r=Depends(get_redis)):
    """Pick the current winner by CTR×CVR with a small-sample guard."""
    test = await r.hgetall(_k(_K_ABTEST, tid=ab_test_id))
    if not test:
        raise HTTPException(404, detail=f"ab_test {ab_test_id} not found")
    creative_ids = json.loads(test.get("creative_ids", "[]"))
    stats = await r.hgetall(_k(_K_ABTEST_STATS, tid=ab_test_id))

    rows = []
    total_imp = 0
    for cid in creative_ids:
        imp = int(stats.get(f"{cid}:imp", "0") or 0)
        clk = int(stats.get(f"{cid}:clk", "0") or 0)
        cvr_n = int(stats.get(f"{cid}:cvr", "0") or 0)
        ctr = (clk / imp) if imp > 0 else 0.0
        cvr = (cvr_n / clk) if clk > 0 else 0.0
        score = ctr * (cvr if cvr > 0 else 1.0)
        rows.append({
            "creative_id": cid,
            "imp": imp, "clk": clk, "cvr": cvr_n,
            "ctr": ctr, "cvr_rate": cvr, "score": score,
        })
        total_imp += imp

    rows.sort(key=lambda x: x["score"], reverse=True)
    winner = rows[0]
    # Confidence proxy: more impressions + bigger gap → higher confidence.
    gap = (rows[0]["score"] - rows[1]["score"]) if len(rows) > 1 else rows[0]["score"]
    confidence = min(1.0, (total_imp / 1000.0) * (gap + 0.01))

    reason = "ctr"
    if winner["cvr_rate"] > 0:
        reason = "cvr"
    if total_imp < 50:
        reason = "manual"  # not enough data — treat as inconclusive

    return {
        "ab_test_id": ab_test_id,
        "winner_creative_id": winner["creative_id"],
        "reason": reason,
        "confidence": round(confidence, 3),
        "stats": rows,
    }


@router.post("/ab-test/{ab_test_id}/record")
async def ab_test_record(
    ab_test_id: str, body: ABTestRecord, r=Depends(get_redis),
):
    """Record imp/click/conv for one creative within an A/B test."""
    if not await r.exists(_k(_K_ABTEST, tid=ab_test_id)):
        raise HTTPException(404, detail=f"ab_test {ab_test_id} not found")
    suffix = {"impression": "imp", "click": "clk", "conversion": "cvr"}[body.event]
    field = f"{body.creative_id}:{suffix}"
    new = await r.hincrby(_k(_K_ABTEST_STATS, tid=ab_test_id), field, 1)
    return {"ab_test_id": ab_test_id, "field": field, "count": int(new)}


# ── Auction integration helper (importable, not an endpoint) ──────────────

async def pick_creative_for_campaign(r, campaign_id: str) -> str | None:
    """Used by the auction engine when a campaign wins an impression.

    If the campaign has an A/B test, sample one creative_id according to
    ``traffic_split`` and atomically increment its impression counter.
    Returns ``None`` if no A/B test is configured — caller falls back to
    the campaign's static ``creative.creative_id`` / ``recipe_id``.

    Record the returned creative_id inside the impression token so click
    and conversion callbacks attribute to the correct variant.
    """
    ab = await r.hgetall(_k(_K_CAMPAIGN_ABTEST, cid=campaign_id))
    if not ab:
        return None
    try:
        creative_ids = json.loads(ab.get("creative_ids", "[]"))
        split = json.loads(ab.get("traffic_split", "[]"))
    except Exception:
        return None
    if not creative_ids or not split or len(creative_ids) != len(split):
        return None

    # Weighted random pick
    roll = random.random()
    cum = 0.0
    chosen = creative_ids[-1]
    for cid, w in zip(creative_ids, split):
        cum += w
        if roll <= cum:
            chosen = cid
            break

    ab_test_id = ab.get("ab_test_id", "")
    if ab_test_id:
        await r.hincrby(
            _k(_K_ABTEST_STATS, tid=ab_test_id), f"{chosen}:imp", 1,
        )
    return chosen
