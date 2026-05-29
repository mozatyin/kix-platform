"""LLM Quota Monitor — Anthropic API rate-limit guard.

Polls Anthropic API every 10 minutes via a tiny probe call. Reads:
  - HTTP 429 status (rate-limited)
  - `anthropic-ratelimit-tokens-remaining` + `-limit` headers
  - `retry-after` on 429

Writes Redis flag:
  kix:llm:quota:paused           — "1" when usage >=95% or 429; cleared when <90%
  kix:llm:quota:usage_pct        — last observed usage %
  kix:llm:quota:last_check_ts    — last probe timestamp
  kix:llm:quota:audit            — LIST of recent probes (capped 1k)

Agents / workers / LLM callers MUST check `kix:llm:quota:paused` before
invoking expensive LLM calls. If set, wait 10 min and retry.

Usage:
    .venv/bin/python -m scripts.llm_quota_monitor        # continuous
    .venv/bin/python -m scripts.llm_quota_monitor --once # single probe
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.redis_client import close_redis, get_redis, init_redis  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("llm_quota_monitor")

POLL_INTERVAL_SECONDS = 600  # 10 minutes
PAUSE_FLAG = "kix:llm:quota:paused"
USAGE_KEY = "kix:llm:quota:usage_pct"
LAST_CHECK_KEY = "kix:llm:quota:last_check_ts"
AUDIT_KEY = "kix:llm:quota:audit"
AUDIT_MAX = 1000

PAUSE_THRESHOLD_PCT = 95
RESUME_THRESHOLD_PCT = 90


async def probe_anthropic() -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"available": True, "usage_pct": 0, "reason": "no_api_key"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 5,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
        if r.status_code == 429:
            return {"available": False, "usage_pct": 100,
                    "retry_after": int(r.headers.get("retry-after", 60)), "reason": "http_429"}
        if r.status_code != 200:
            return {"available": True, "usage_pct": 0, "reason": f"http_{r.status_code}"}
        limit = r.headers.get("anthropic-ratelimit-tokens-limit")
        remaining = r.headers.get("anthropic-ratelimit-tokens-remaining")
        usage_pct = 0
        if limit and remaining:
            try:
                usage_pct = round((1 - int(remaining) / max(int(limit), 1)) * 100)
            except (ValueError, ZeroDivisionError):
                pass
        return {"available": usage_pct < PAUSE_THRESHOLD_PCT, "usage_pct": usage_pct, "reason": "ok"}
    except Exception as e:
        return {"available": True, "usage_pct": 0, "reason": f"error:{e}"}


async def run_once() -> dict:
    r = await get_redis()
    result = await probe_anthropic()
    now = time.time()
    usage = result.get("usage_pct", 0)
    available = result["available"]
    currently_paused = bool(await r.get(PAUSE_FLAG))
    if not available or usage >= PAUSE_THRESHOLD_PCT:
        await r.set(PAUSE_FLAG, "1", ex=POLL_INTERVAL_SECONDS * 2)
        action = "paused"
        logger.warning(f"LLM quota {usage}% → PAUSED")
    elif currently_paused and usage < RESUME_THRESHOLD_PCT:
        await r.delete(PAUSE_FLAG)
        action = "resumed"
    elif currently_paused:
        action = "still_paused"
    else:
        action = "ok"
    await r.set(USAGE_KEY, usage)
    await r.set(LAST_CHECK_KEY, now)
    await r.lpush(AUDIT_KEY, json.dumps({"ts": now, "usage_pct": usage, "action": action,
                                         "reason": result.get("reason")}))
    await r.ltrim(AUDIT_KEY, 0, AUDIT_MAX - 1)
    return {"ts": now, "usage_pct": usage, "action": action, **result}


async def is_paused() -> bool:
    r = await get_redis()
    return bool(await r.get(PAUSE_FLAG))


async def wait_if_paused(max_wait_seconds: int = 3600) -> bool:
    waited = 0
    paused_once = False
    while await is_paused() and waited < max_wait_seconds:
        paused_once = True
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        waited += POLL_INTERVAL_SECONDS
    if waited >= max_wait_seconds and await is_paused():
        raise RuntimeError(f"LLM quota still paused after {max_wait_seconds}s")
    return paused_once


async def main():
    await init_redis()
    try:
        if "--once" in sys.argv:
            print(json.dumps(await run_once(), indent=2))
        else:
            logger.info(f"LLM quota monitor starting (interval={POLL_INTERVAL_SECONDS}s)")
            while True:
                try:
                    await run_once()
                except Exception as e:
                    logger.exception(f"Probe cycle failed: {e}")
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
    finally:
        await close_redis()


if __name__ == "__main__":
    asyncio.run(main())
