"""Content Moderation Pipeline — image safety + LLM text scan + human queue.

Risk surfaces this router defends against:

* 黄赌毒 (adult/gambling/drugs) — auto-block.
* 政治敏感 / 反动言论 (political_sensitive, hate_speech) — review or block.
* 虚假宣传 / 误导 (misleading_claims, fraud/scam) — review.
* 侵权 (intellectual_property_risk) — review.
* 极限词 / spam (already partly handled by ``compliance.py`` — we reuse
  the banned-phrase rules as a fast keyword pre-filter so the LLM is
  only invoked on text that survived the cheap deterministic gate).

Two-layer scan:

    1.  Cheap deterministic keyword filter (re-uses ``compliance.py``
        seeded rules + a small local blocklist). Hard blocks bail early
        without spending an LLM call.
    2.  LLM-based dimension scoring via ``eltm.llm.call_llm`` (sync,
        wrapped in ``asyncio.to_thread``). Returns a 0-100 score per
        category; the max drives the verdict.

Verdict mapping (default thresholds, overridable via policy hash):
    * ``block``  — score >= 80  (auto-reject upstream)
    * ``review`` — score >= 50  (sent to human queue)
    * ``allow``  — score <  50

Image moderation is stubbed (Google Vision Safe Search / Alibaba 内容安全
is the prod target). The stub returns ``allow`` so we do not block
existing flows. Video/audio fall back to "review" until a real backend
is wired in.

Cross-module entrypoints (no HTTP overhead):
    * :func:`moderate_text_internal`
    * :func:`moderate_image_internal`
    * :func:`moderate_internal` (dispatches by content_type)

Redis key schema
----------------
    moderation:policies                       HASH {category → JSON
                                                    {block, review, warn}}
    moderation:blocked_keywords               SET  of literal phrases
    moderation:queue:pending                  ZSET review_id → priority
                                                    (negated, higher
                                                    score = higher
                                                    priority)
    moderation:queue:reviewed                 ZSET review_id → ts
    moderation:review:{review_id}             HASH full payload
    moderation:scan_log:{brand_id}            LIST capped (200)
    moderation:user_block:{user_id}           HASH {count, last_ts}
    moderation:brand_block:{brand_id}         HASH {count, last_ts}

Endpoints (all under prefix ``/api/v1/moderation``):
    POST   /scan                           score arbitrary content
    POST   /queue/add                      enqueue for human review
    GET    /queue                          admin queue view
    POST   /queue/{review_id}/decision     admin decision + cascade
    GET    /policies                       list policies
    POST   /policies/configure             admin threshold tuning
    POST   /internal-check                 alias of /scan for SDK callers
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.redis_client import get_redis

logger = logging.getLogger(__name__)
router = APIRouter()


# ── ELTM LLM bridge (sync helper, wrapped via asyncio.to_thread) ───────────
sys.path.insert(0, "/Users/mozat/eltm")
try:
    from eltm.llm import call_llm as _eltm_call_llm  # type: ignore
except Exception as _imp_err:  # noqa: BLE001
    _eltm_call_llm = None
    _ELTM_IMPORT_ERROR: Exception | None = _imp_err
else:
    _ELTM_IMPORT_ERROR = None


# ── Constants ──────────────────────────────────────────────────────────────

VALID_CONTENT_TYPES = {"text", "image", "video", "audio"}
VALID_CONTEXTS = {
    "ad_creative",
    "voucher_template",
    "game_description",
    "comment",
    "review",
    "campaign_name",
    "brand_profile",
    "other",
}
VALID_VERDICTS = {"allow", "review", "block"}
VALID_DECISIONS = {"approve", "reject", "refer_legal"}

# 8 dimensions the LLM scores; matches the prompt schema below.
SCORE_CATEGORIES: tuple[str, ...] = (
    "adult",
    "violence",
    "political",
    "spam",
    "misleading",
    "intellectual_property",
    "hate_speech",
    "fraud",
)

# Default thresholds — tunable via /policies/configure.
DEFAULT_THRESHOLDS: dict[str, dict[str, int]] = {
    cat: {"block": 80, "review": 50, "warn": 30}
    for cat in SCORE_CATEGORIES
}

# Small literal blocklist used when neither compliance.py nor an admin
# has seeded ``moderation:blocked_keywords`` yet. Kept intentionally
# short — broad coverage lives in ``compliance.py`` (广告法 §9 etc.) and
# the LLM scoring step.
FALLBACK_BLOCKED_KEYWORDS: tuple[str, ...] = (
    "保证收益",
    "稳赚不赔",
    "包治百病",
    "100%治愈",
    "六合彩",
)

# Priority weights for the review queue (higher = handled first).
PRIORITY_BY_CATEGORY: dict[str, int] = {
    "adult": 90,
    "violence": 85,
    "political": 95,
    "hate_speech": 90,
    "fraud": 80,
    "misleading": 60,
    "intellectual_property": 70,
    "spam": 40,
}

# Redis keys
_K_POLICIES = "moderation:policies"
_K_BLOCKED_KW = "moderation:blocked_keywords"
_K_QUEUE_PENDING = "moderation:queue:pending"
_K_QUEUE_REVIEWED = "moderation:queue:reviewed"
_K_REVIEW = "moderation:review:{rid}"
_K_SCAN_LOG = "moderation:scan_log:{actor}"
_K_USER_BLOCK = "moderation:user_block:{uid}"
_K_BRAND_BLOCK = "moderation:brand_block:{bid}"

SCAN_LOG_MAX = 200


# ── Pydantic models ────────────────────────────────────────────────────────


class ScanRequest(BaseModel):
    content_type: Literal["text", "image", "video", "audio"]
    content: str = Field(min_length=1, max_length=20_000)
    context: Literal[
        "ad_creative",
        "voucher_template",
        "game_description",
        "comment",
        "review",
        "campaign_name",
        "brand_profile",
        "other",
    ] = "ad_creative"
    brand_id: str | None = None
    user_id: str | None = None


class ScanResponse(BaseModel):
    ok: bool
    score: int
    categories: dict[str, int]
    verdict: str
    suggested_action: str
    review_required: bool
    reason: str | None = None
    scanned_at: int


class QueueAddRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    content_type: Literal["text", "image", "video", "audio"] = "text"
    context: str = "ad_creative"
    brand_id: str | None = None
    user_id: str | None = None
    scan_result: dict[str, Any] = Field(default_factory=dict)


class QueueAddResponse(BaseModel):
    review_id: str
    queued_at: int
    priority: int


class DecisionRequest(BaseModel):
    admin_token: str = Field(min_length=1)
    decision: Literal["approve", "reject", "refer_legal"]
    reason: str = Field(min_length=1, max_length=1000)
    modifier_actions: list[str] = Field(default_factory=list)


class PolicyConfig(BaseModel):
    admin_token: str = Field(min_length=1)
    category: str
    threshold_block: int = Field(ge=0, le=100)
    threshold_review: int = Field(ge=0, le=100)
    threshold_warn: int = Field(ge=0, le=100)


# ── Helpers ────────────────────────────────────────────────────────────────


def _now() -> int:
    return int(time.time())


def _admin_ok(token: str) -> bool:
    """Trivial admin gate — env-configurable, mirrors compliance.py style.

    Uses constant-time comparison to prevent timing attacks.
    """
    from app.security import constant_time_eq

    expected = os.environ.get("KIX_ADMIN_TOKEN", "kix-admin-dev")
    return constant_time_eq(token, expected)


async def _load_thresholds(r, category: str) -> dict[str, int]:
    raw = await r.hget(_K_POLICIES, category)
    if not raw:
        return DEFAULT_THRESHOLDS.get(category, {"block": 80, "review": 50, "warn": 30})
    try:
        return json.loads(raw)
    except Exception:
        return DEFAULT_THRESHOLDS.get(category, {"block": 80, "review": 50, "warn": 30})


async def _all_thresholds(r) -> dict[str, dict[str, int]]:
    out = {cat: DEFAULT_THRESHOLDS[cat].copy() for cat in SCORE_CATEGORIES}
    raw = await r.hgetall(_K_POLICIES)
    for cat, blob in (raw or {}).items():
        try:
            out[cat] = json.loads(blob)
        except Exception:
            continue
    return out


async def _get_blocked_keywords(r) -> list[str]:
    """Union of admin-seeded set + compliance.py block-severity phrases +
    fallback list. Cached lightly (no TTL — set is small)."""
    kws: set[str] = set()
    try:
        seeded = await r.smembers(_K_BLOCKED_KW)
        for s in seeded or []:
            if s:
                kws.add(s)
    except Exception:
        pass

    # Pull "block"-severity literal phrases from compliance.py at runtime.
    # Soft-imported so a missing/broken compliance module never breaks
    # moderation.
    try:
        from app.routers.compliance import (
            scan_internal as _compliance_scan_internal,  # noqa: F401
        )
        # We don't iterate the full rule set here — the LLM + compliance
        # scan_internal pass below provides coverage. The Redis fallback
        # set captures cases admins want enforced even without compliance.
    except Exception:
        pass

    for kw in FALLBACK_BLOCKED_KEYWORDS:
        kws.add(kw)
    return list(kws)


def _verdict_for(score: int, thresholds: dict[str, int]) -> str:
    if score >= thresholds.get("block", 80):
        return "block"
    if score >= thresholds.get("review", 50):
        return "review"
    return "allow"


def _suggest(verdict: str, top_cat: str | None) -> str:
    if verdict == "block":
        return f"reject_and_log:{top_cat or 'unknown'}"
    if verdict == "review":
        return "queue_for_human_review"
    return "publish"


async def _log_scan(r, *, actor: str, payload: dict[str, Any]) -> None:
    if not actor:
        return
    try:
        key = _K_SCAN_LOG.format(actor=actor)
        await r.lpush(key, json.dumps(payload, ensure_ascii=False))
        await r.ltrim(key, 0, SCAN_LOG_MAX - 1)
    except Exception as exc:  # noqa: BLE001
        logger.debug("moderation scan log failed: %s", exc)


# ── LLM scoring ────────────────────────────────────────────────────────────


_LLM_SYSTEM = (
    "You are a strict content-moderation classifier for an ad/loyalty "
    "platform serving Chinese and global merchants. Output JSON ONLY — "
    "no commentary, no markdown. Each category is an integer 0–100 where "
    "0 = clearly safe, 100 = clearly violating."
)

_LLM_PROMPT_TMPL = """Score this {content_kind} on 8 dimensions (0-100 each):
- adult              (sexual / 黄 / suggestive nudity)
- violence           (gore / weapon glorification)
- political          (政治敏感 / 反动言论 / 涉政话题)
- spam               (excessive promotional / clickbait)
- misleading         (虚假宣传 / unverifiable claims / 极限词 like 100%/最佳)
- intellectual_property  (trademark/celebrity misuse, brand impersonation)
- hate_speech        (slurs, group hatred)
- fraud              (scam / 赌博 / 毒品 / fake investment / 保本)

Text/content: {content!r}
Context: {context}

Respond with JSON ONLY of the form:
{{"adult": int, "violence": int, "political": int, "spam": int,
  "misleading": int, "intellectual_property": int, "hate_speech": int,
  "fraud": int}}
"""


def _safe_parse_scores(raw: str) -> dict[str, int]:
    """Best-effort parse — strip code fences, find first JSON object."""
    if not raw:
        return {}
    cleaned = raw.strip()
    # Strip ```json ... ``` fences
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.S)
    # Find the first '{' ... '}' block.
    m = re.search(r"\{[^{}]*\}", cleaned, flags=re.S)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return {}
    out: dict[str, int] = {}
    for cat in SCORE_CATEGORIES:
        v = obj.get(cat, 0)
        try:
            out[cat] = max(0, min(100, int(v)))
        except (TypeError, ValueError):
            out[cat] = 0
    return out


async def _llm_score(content: str, context: str, *, content_kind: str = "text") -> dict[str, int] | None:
    """Run the LLM classifier. Returns ``None`` if LLM is unavailable
    (caller treats as a soft-review)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or _eltm_call_llm is None:
        logger.info(
            "moderation: LLM unavailable (api_key=%s, import=%s)",
            bool(api_key),
            "ok" if _eltm_call_llm else f"fail:{_ELTM_IMPORT_ERROR!r}",
        )
        return None

    # Truncate aggressively — the classifier doesn't need the whole novel.
    snippet = content if len(content) <= 4000 else content[:4000] + "…"
    prompt = _LLM_PROMPT_TMPL.format(
        content_kind=content_kind, content=snippet, context=context,
    )
    try:
        text = await asyncio.to_thread(
            _eltm_call_llm, api_key, prompt, system=_LLM_SYSTEM,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("moderation LLM call failed: %s", exc)
        return None
    scores = _safe_parse_scores(text or "")
    if not scores:
        logger.warning("moderation: LLM returned unparseable output: %r", (text or "")[:200])
        return None
    return scores


# ── Core moderation functions ──────────────────────────────────────────────


async def moderate_text_internal(
    content: str,
    context: str = "ad_creative",
    *,
    r=None,
    brand_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """LLM-based text moderation, layered on top of a keyword pre-filter.

    Safe to call without an explicit Redis handle — we'll fetch one if
    omitted. Never raises for "normal" failures; on LLM error returns a
    soft ``review`` verdict so the caller doesn't accidentally publish
    unscanned content.
    """
    if r is None:
        r = await get_redis()

    if not content or not content.strip():
        result = {
            "ok": True,
            "score": 0,
            "verdict": "allow",
            "categories": {cat: 0 for cat in SCORE_CATEGORIES},
            "review_required": False,
            "suggested_action": "publish",
            "reason": None,
            "scanned_at": _now(),
        }
        return result

    # ── Layer 0: cheap literal pre-filter ──────────────────────────────
    blocked = await _get_blocked_keywords(r)
    lc = content.lower()
    hits = [kw for kw in blocked if kw and kw.lower() in lc]
    if hits:
        result = {
            "ok": False,
            "score": 100,
            "verdict": "block",
            "categories": {"banned_phrase": 100},
            "review_required": False,
            "suggested_action": f"reject_and_log:banned_phrase:{hits[0]}",
            "reason": f"banned_keywords:{','.join(hits[:3])}",
            "scanned_at": _now(),
        }
        await _log_scan(
            r,
            actor=brand_id or user_id or "anon",
            payload={"verdict": "block", "reason": result["reason"], "ts": result["scanned_at"]},
        )
        await _bump_block_counter(r, brand_id=brand_id, user_id=user_id)
        return result

    # ── Layer 1: compliance.py 广告法/医疗广告 etc. ────────────────────
    # These are deterministic block-rules (极限词, 保本 etc.) and cheap.
    try:
        from app.routers.compliance import scan_internal as _compliance_scan
        compliance = await _compliance_scan("general", content, r)
        if not compliance.get("pass", True):
            phrases = [v.get("phrase", "") for v in compliance.get("violations", [])][:3]
            result = {
                "ok": False,
                "score": 100,
                "verdict": "block",
                "categories": {"misleading": 100},
                "review_required": False,
                "suggested_action": f"reject_and_log:compliance:{phrases[0] if phrases else 'rule'}",
                "reason": f"compliance:{','.join(p for p in phrases if p)}",
                "scanned_at": _now(),
            }
            await _log_scan(
                r,
                actor=brand_id or user_id or "anon",
                payload={"verdict": "block", "reason": result["reason"], "ts": result["scanned_at"]},
            )
            await _bump_block_counter(r, brand_id=brand_id, user_id=user_id)
            return result
    except HTTPException:
        # compliance refused (e.g. unknown industry) — ignore, fall through.
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("compliance scan unavailable: %s", exc)

    # ── Layer 2: LLM scoring ───────────────────────────────────────────
    scores = await _llm_score(content, context, content_kind="text")
    if scores is None:
        # Fail-safe: defer to human review rather than auto-allow.
        result = {
            "ok": True,
            "score": 50,
            "verdict": "review",
            "categories": {cat: 0 for cat in SCORE_CATEGORIES} | {"error": 50},
            "review_required": True,
            "suggested_action": "queue_for_human_review",
            "reason": "llm_unavailable",
            "scanned_at": _now(),
        }
        await _log_scan(
            r,
            actor=brand_id or user_id or "anon",
            payload={"verdict": "review", "reason": "llm_unavailable", "ts": result["scanned_at"]},
        )
        return result

    thresholds = await _all_thresholds(r)
    # Per-category verdict: choose the worst.
    worst_verdict = "allow"
    worst_score = 0
    worst_cat: str | None = None
    for cat, sc in scores.items():
        v = _verdict_for(sc, thresholds.get(cat, {"block": 80, "review": 50}))
        if v == "block" or (v == "review" and worst_verdict != "block"):
            if sc > worst_score:
                worst_score = sc
                worst_cat = cat
            if v == "block":
                worst_verdict = "block"
            elif worst_verdict == "allow":
                worst_verdict = "review"
        if sc > worst_score and worst_verdict == "allow":
            worst_score = sc
            worst_cat = cat
    max_score = max(scores.values()) if scores else 0
    if worst_verdict == "allow":
        worst_score = max_score
        worst_cat = max(scores, key=scores.get) if scores else None

    result = {
        "ok": worst_verdict != "block",
        "score": int(worst_score),
        "verdict": worst_verdict,
        "categories": scores,
        "review_required": worst_verdict == "review",
        "suggested_action": _suggest(worst_verdict, worst_cat),
        "reason": (worst_cat if worst_verdict != "allow" else None),
        "scanned_at": _now(),
    }
    await _log_scan(
        r,
        actor=brand_id or user_id or "anon",
        payload={
            "verdict": worst_verdict,
            "top": worst_cat,
            "score": int(worst_score),
            "ts": result["scanned_at"],
        },
    )
    if worst_verdict == "block":
        await _bump_block_counter(r, brand_id=brand_id, user_id=user_id)
    return result


async def moderate_image_internal(
    image_url: str,
    context: str = "ad_creative",
    *,
    r=None,
    brand_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Image safety stub.

    Real production targets: Google Vision Safe Search or Alibaba Cloud
    内容安全 (green-net). Until one is wired, this returns ``allow`` with
    a tiny baseline score so flows don't block on missing credentials.
    A presence flag in the env (``MODERATION_IMAGE_BACKEND``) makes it
    easy to detect "stub mode" in observability.
    """
    backend = os.environ.get("MODERATION_IMAGE_BACKEND", "stub")
    scores = {"adult": 0, "violence": 0, "medical": 0, "spoof": 5}
    verdict = "allow"
    reason = None

    # Very crude URL-based heuristic: catch obvious filename red flags so
    # we don't ship a completely useless stub.
    url_lc = (image_url or "").lower()
    for needle in ("nsfw", "porn", "xxx", "adult", "nude"):
        if needle in url_lc:
            scores["adult"] = 95
            verdict = "block"
            reason = f"url_hint:{needle}"
            break

    result = {
        "ok": verdict != "block",
        "score": max(scores.values()),
        "verdict": verdict,
        "categories": scores,
        "review_required": False,
        "suggested_action": _suggest(verdict, "adult" if verdict == "block" else None),
        "reason": reason,
        "scanned_at": _now(),
        "backend": backend,
    }
    if r is not None:
        await _log_scan(
            r,
            actor=brand_id or user_id or "anon",
            payload={"verdict": verdict, "kind": "image", "ts": result["scanned_at"]},
        )
        if verdict == "block":
            await _bump_block_counter(r, brand_id=brand_id, user_id=user_id)
    return result


async def moderate_internal(
    *,
    content_type: str,
    content: str,
    context: str = "ad_creative",
    brand_id: str | None = None,
    user_id: str | None = None,
    r=None,
) -> dict[str, Any]:
    """Dispatch entrypoint — sibling routers should prefer this."""
    if content_type not in VALID_CONTENT_TYPES:
        raise HTTPException(400, detail=f"invalid content_type: {content_type}")
    if r is None:
        r = await get_redis()
    if content_type == "text":
        return await moderate_text_internal(
            content, context, r=r, brand_id=brand_id, user_id=user_id,
        )
    if content_type == "image":
        return await moderate_image_internal(
            content, context, r=r, brand_id=brand_id, user_id=user_id,
        )
    # video / audio — no backend yet; defer to human.
    return {
        "ok": True,
        "score": 40,
        "verdict": "review",
        "categories": {"unscanned": 40},
        "review_required": True,
        "suggested_action": "queue_for_human_review",
        "reason": f"{content_type}_backend_not_configured",
        "scanned_at": _now(),
    }


async def _bump_block_counter(r, *, brand_id: str | None, user_id: str | None) -> None:
    try:
        ts = _now()
        if brand_id:
            key = _K_BRAND_BLOCK.format(bid=brand_id)
            await r.hincrby(key, "count", 1)
            await r.hset(key, "last_ts", str(ts))
        if user_id:
            key = _K_USER_BLOCK.format(uid=user_id)
            await r.hincrby(key, "count", 1)
            await r.hset(key, "last_ts", str(ts))
    except Exception as exc:  # noqa: BLE001
        logger.debug("block counter bump failed: %s", exc)


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post("/scan", response_model=ScanResponse)
async def scan(body: ScanRequest, r=Depends(get_redis)):
    """Public scan endpoint."""
    result = await moderate_internal(
        content_type=body.content_type,
        content=body.content,
        context=body.context,
        brand_id=body.brand_id,
        user_id=body.user_id,
        r=r,
    )
    return ScanResponse(**result)


@router.post("/internal-check", response_model=ScanResponse)
async def internal_check(body: ScanRequest, r=Depends(get_redis)):
    """Alias of /scan for service-to-service callers (rate-limit exempt)."""
    result = await moderate_internal(
        content_type=body.content_type,
        content=body.content,
        context=body.context,
        brand_id=body.brand_id,
        user_id=body.user_id,
        r=r,
    )
    return ScanResponse(**result)


@router.post("/queue/add", response_model=QueueAddResponse)
async def queue_add(body: QueueAddRequest, r=Depends(get_redis)):
    """Append a flagged item to the human-review queue.

    Priority is derived from the scan result's worst category if known,
    otherwise defaults to 50 (medium).
    """
    review_id = f"rev_{uuid.uuid4().hex[:12]}"
    ts = _now()

    # Priority: prefer top category from scan_result, else neutral 50.
    priority = 50
    top_cat = (body.scan_result or {}).get("reason")
    if isinstance(top_cat, str):
        for cat, weight in PRIORITY_BY_CATEGORY.items():
            if cat in top_cat:
                priority = weight
                break
    severity_score = (body.scan_result or {}).get("score")
    if isinstance(severity_score, (int, float)):
        priority = max(priority, int(severity_score))

    payload = {
        "review_id": review_id,
        "content": body.content,
        "content_type": body.content_type,
        "context": body.context,
        "brand_id": body.brand_id or "",
        "user_id": body.user_id or "",
        "scan_result": json.dumps(body.scan_result or {}, ensure_ascii=False),
        "status": "pending",
        "priority": str(priority),
        "queued_at": str(ts),
        "decided_at": "",
        "decision": "",
        "decision_reason": "",
        "modifier_actions": "",
    }
    pipe = r.pipeline()
    pipe.hset(_K_REVIEW.format(rid=review_id), mapping=payload)
    # ZSET score = -priority so ZRANGE returns highest-priority first.
    pipe.zadd(_K_QUEUE_PENDING, {review_id: -priority})
    await pipe.execute()

    logger.info(
        "moderation queue +1 review_id=%s priority=%d brand=%s",
        review_id, priority, body.brand_id or "-",
    )
    return QueueAddResponse(review_id=review_id, queued_at=ts, priority=priority)


@router.get("/queue")
async def queue_list(
    status: Literal["pending", "reviewed"] = Query("pending"),
    priority: Literal["high", "medium", "low", "any"] = Query("any"),
    limit: int = Query(50, ge=1, le=500),
    r=Depends(get_redis),
):
    """Admin queue view. Highest-priority first."""
    zkey = _K_QUEUE_PENDING if status == "pending" else _K_QUEUE_REVIEWED
    ids = await r.zrange(zkey, 0, limit - 1)
    out: list[dict[str, Any]] = []
    for rid in ids:
        raw = await r.hgetall(_K_REVIEW.format(rid=rid))
        if not raw:
            continue
        try:
            p = int(raw.get("priority", "0") or 0)
        except ValueError:
            p = 0
        if priority == "high" and p < 80:
            continue
        if priority == "medium" and (p < 50 or p >= 80):
            continue
        if priority == "low" and p >= 50:
            continue
        sr = raw.get("scan_result")
        try:
            raw["scan_result"] = json.loads(sr) if sr else {}
        except Exception:
            raw["scan_result"] = {}
        out.append(raw)
    return {"status": status, "count": len(out), "items": out}


@router.post("/queue/{review_id}/decision")
async def queue_decision(
    review_id: str, body: DecisionRequest, r=Depends(get_redis),
):
    """Admin decides on a queued item; cascades modifier_actions."""
    if not _admin_ok(body.admin_token):
        raise HTTPException(403, detail="invalid admin token")

    key = _K_REVIEW.format(rid=review_id)
    raw = await r.hgetall(key)
    if not raw:
        raise HTTPException(404, detail=f"review {review_id} not found")
    if raw.get("status") == "reviewed":
        raise HTTPException(409, detail="review already decided")

    ts = _now()
    mods = ",".join(a for a in body.modifier_actions if isinstance(a, str))
    pipe = r.pipeline()
    pipe.hset(key, mapping={
        "status": "reviewed",
        "decided_at": str(ts),
        "decision": body.decision,
        "decision_reason": body.reason[:1000],
        "modifier_actions": mods,
    })
    pipe.zrem(_K_QUEUE_PENDING, review_id)
    pipe.zadd(_K_QUEUE_REVIEWED, {review_id: ts})
    await pipe.execute()

    # Cascade modifier actions (best-effort, never raises out).
    cascade_log: list[str] = []
    brand_id = raw.get("brand_id") or None
    user_id = raw.get("user_id") or None
    for action in body.modifier_actions:
        try:
            if action == "block_brand" and brand_id:
                await r.hset(
                    _K_BRAND_BLOCK.format(bid=brand_id),
                    mapping={"hard_block": "1", "blocked_at": str(ts)},
                )
                cascade_log.append(f"brand_blocked:{brand_id}")
            elif action == "warn_user" and user_id:
                await r.hincrby(
                    _K_USER_BLOCK.format(uid=user_id), "warn_count", 1,
                )
                cascade_log.append(f"user_warned:{user_id}")
            elif action == "ban_user" and user_id:
                await r.hset(
                    _K_USER_BLOCK.format(uid=user_id),
                    mapping={"hard_block": "1", "blocked_at": str(ts)},
                )
                cascade_log.append(f"user_banned:{user_id}")
            elif action == "add_to_blocklist":
                # Pull the offending content's reason phrase into the
                # admin blocklist so future identical text auto-blocks.
                content = raw.get("content", "")[:60]
                if content:
                    await r.sadd(_K_BLOCKED_KW, content)
                    cascade_log.append(f"blocklist+:{content[:30]}…")
            else:
                cascade_log.append(f"noop:{action}")
        except Exception as exc:  # noqa: BLE001
            cascade_log.append(f"error:{action}:{exc}")

    return {
        "review_id": review_id,
        "decision": body.decision,
        "decided_at": ts,
        "cascade": cascade_log,
    }


@router.get("/policies")
async def list_policies(r=Depends(get_redis)):
    """Return the active policy threshold table."""
    th = await _all_thresholds(r)
    return {"categories": list(SCORE_CATEGORIES), "thresholds": th}


@router.post("/policies/configure")
async def configure_policy(body: PolicyConfig, r=Depends(get_redis)):
    """Admin sets thresholds for one category."""
    if not _admin_ok(body.admin_token):
        raise HTTPException(403, detail="invalid admin token")
    if body.category not in SCORE_CATEGORIES:
        raise HTTPException(
            400,
            detail=f"unknown category '{body.category}', allowed: {list(SCORE_CATEGORIES)}",
        )
    if not (body.threshold_warn <= body.threshold_review <= body.threshold_block):
        raise HTTPException(
            400, detail="thresholds must satisfy warn <= review <= block",
        )
    payload = {
        "block": body.threshold_block,
        "review": body.threshold_review,
        "warn": body.threshold_warn,
    }
    await r.hset(_K_POLICIES, body.category, json.dumps(payload))
    return {"category": body.category, "thresholds": payload}


# ── Re-exports for sibling routers ─────────────────────────────────────────

__all__ = [
    "router",
    "moderate_text_internal",
    "moderate_image_internal",
    "moderate_internal",
]
