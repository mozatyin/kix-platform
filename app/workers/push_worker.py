"""Push notification dispatcher.

Consumes the sharded ``push:outbound:queue:{N}`` LISTs (written by
``app.routers.push_engine.dispatch``) and routes each push to the right
platform gateway based on the kid's registered devices:

* ``ios``     → APNS (Apple Push Notification Service)
* ``android`` → FCM  (Firebase Cloud Messaging)
* ``wechat``  → WeChat template / subscribe message
* ``web``     → Web Push (browser notification, VAPID)

Real production wires this to:

* ``firebase_admin.messaging`` for FCM
* ``apns2.client.APNsClient`` (or ``aioapns``) for APNS
* WeChat Open Platform ``cgi-bin/message/template/send`` HTTP API
* ``pywebpush`` for Web Push

MVP behaviour: simulated success + structured log to
``push:outbound:log`` so the rest of the pipeline (delivery state,
metrics, merchant billing, inbox surfacing) can be validated end-to-end
without external dependencies. The platform-routing seam (
``_send_to_platform``) is the single place to swap in real SDK calls.

Throughput design (Trinity-F audit, target 1M pushes/day)
---------------------------------------------------------
* ``BATCH_SIZE = 500`` (was 50) — bigger LPOP batch per cycle.
* Pipelined ``HGETALL`` for device lookups — 1 RTT for N lookups
  instead of N RTTs.
* Sharded outbound queues — ``push:outbound:queue:{0..15}`` lets us
  scale to 16 worker processes without contention on a single LIST.
* Parallel delivery within a batch (``asyncio.gather`` up to
  ``CONCURRENT_LIMIT``) — overlaps gateway I/O.
* Pipelined bulk Redis writes for ``push:{id}`` status + log + retry.
* Backpressure detection — logs WARN when a shard queue exceeds
  ``QUEUE_DEPTH_WARN``.
* Worker metrics persisted to ``push_worker:metrics`` for monitoring.

Queue payload contract
----------------------
``push_engine.dispatch`` ``LPUSH``-es a bare ``push_id`` string onto the
shard determined by ``shard_for(push_id)``. We accept BOTH forms
transparently:

* bare push_id string → we ``HGETALL push:{push_id}`` to recover the
  ``kid``/``title``/``body``/``deep_link`` fields.
* JSON object payload → we read ``push_id`` / ``kid`` directly off the
  payload (used by retries and any future enqueuers that want to ship a
  self-contained envelope).

Usage
-----
::

    # Single-worker mode (consumes ALL shards round-robin)
    .venv/bin/python -m app.workers.push_worker --once
    .venv/bin/python -m app.workers.push_worker

    # Multi-worker mode (one process per shard)
    .venv/bin/python -m app.workers.push_worker --shard 0
    .venv/bin/python -m app.workers.push_worker --shard 1

    # Range mode (half-cluster)
    .venv/bin/python -m app.workers.push_worker --shards 0-7

Redis schema written by this worker
-----------------------------------
::

    push:outbound:queue:{0..15}   LIST  (sharded outbound)
    push:outbound:log             LIST  (newest first, capped 10k)
    push:outbound:retry           LIST  payloads pending backoff
    push_device:{device_id}       HASH  {kid, platform, token, registered_at}
    kid:{kid}:push_devices        SET   of device_ids
    push:{push_id}                HASH  (mutated: delivered_at, delivery_status,
                                         delivery_attempts, last_error)
    push_worker:metrics           HASH  {delivered_total, failed_total,
                                         last_cycle_ts}
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import time
from typing import Any

from app.redis_client import close_redis, get_redis, init_redis

logger = logging.getLogger("push_worker")

# ── Tunables ──────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS = 5
BATCH_SIZE = 500                # was 50 — Trinity-F throughput audit
CONCURRENT_LIMIT = 50           # in-flight delivery_push() coroutines per batch
MAX_DELIVERY_ATTEMPTS = 3
RETRY_BASE_BACKOFF_SECONDS = 30
OUTBOUND_LOG_MAX = 9999
QUEUE_DEPTH_WARN = 10000        # log WARN when a shard queue exceeds this

# Sharding
NUM_SHARDS = 16
OUTBOUND_QUEUE_KEY_BASE = "push:outbound:queue"
OUTBOUND_QUEUE_KEYS = [f"{OUTBOUND_QUEUE_KEY_BASE}:{i}" for i in range(NUM_SHARDS)]
# Legacy non-sharded key — drained for backwards compatibility on cold start.
LEGACY_OUTBOUND_QUEUE_KEY = OUTBOUND_QUEUE_KEY_BASE

RETRY_QUEUE_KEY = "push:outbound:retry"
OUTBOUND_LOG_KEY = "push:outbound:log"
METRICS_KEY = "push_worker:metrics"


# ── Sharding helpers ─────────────────────────────────────────────────────


def shard_for(push_id: str) -> int:
    """Deterministic shard from push_id.

    Uses md5 (not Python's built-in ``hash``) so the shard is stable
    across processes — Python randomises ``hash()`` per interpreter.
    """
    if not push_id:
        return 0
    digest = hashlib.md5(push_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % NUM_SHARDS


def outbound_queue_key(push_id: str) -> str:
    """Compute the sharded queue key for a given push_id."""
    return OUTBOUND_QUEUE_KEYS[shard_for(push_id)]


def parse_shard_range(spec: str) -> list[int]:
    """Parse '0-7' or '3' or '1,4,9' into a list of shard ids."""
    out: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo, hi = chunk.split("-", 1)
            for i in range(int(lo), int(hi) + 1):
                if 0 <= i < NUM_SHARDS:
                    out.add(i)
        else:
            i = int(chunk)
            if 0 <= i < NUM_SHARDS:
                out.add(i)
    return sorted(out)


# ── Payload helpers ──────────────────────────────────────────────────────


def _decode_queue_item(item: str) -> dict[str, Any]:
    """Accept either a bare ``push_id`` string or a JSON object.

    Returns a dict with at minimum a ``push_id`` (possibly empty) and any
    additional fields the producer chose to carry.
    """
    if not item:
        return {}
    s = item.strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    # Treat as opaque push_id string.
    return {"push_id": s}


async def _hydrate_payloads_pipelined(
    r, payloads: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Batch-hydrate payloads that only carry a push_id.

    Pipelines all ``HGETALL push:{push_id}`` calls into a single RTT.
    Payloads already carrying ``kid`` are passed through untouched.
    """
    if not payloads:
        return payloads

    pipe = r.pipeline()
    to_fill: list[int] = []
    for idx, payload in enumerate(payloads):
        push_id = payload.get("push_id")
        if push_id and not payload.get("kid"):
            pipe.hgetall(f"push:{push_id}")
            to_fill.append(idx)

    if not to_fill:
        return payloads

    results = await pipe.execute()
    for slot, idx in enumerate(to_fill):
        record = results[slot] or {}
        if record:
            merged = dict(record)
            # Caller-supplied values win over the stored hash so retries
            # can override e.g. ``attempts``.
            merged.update(
                {k: v for k, v in payloads[idx].items() if v not in (None, "")}
            )
            payloads[idx] = merged
    return payloads


# ── Gateway routing ──────────────────────────────────────────────────────


async def _send_to_platform(
    platform: str, token: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Stub for actual gateway send. Replace with real SDK calls in prod.

    In production this is the single seam where we wire in:

    * ``ios``      → ``APNsClient(...).send_notification(token, ...)``
    * ``android``  → ``messaging.send(messaging.Message(token=token, ...))``
    * ``wechat``   → ``wechat_api.send_template_message(openid=token, ...)``
    * ``web``      → ``pywebpush.webpush(subscription_info=..., data=...)``

    All branches return the same envelope so the caller can treat them
    uniformly.
    """
    if not platform:
        return {"success": False, "error": "missing_platform"}
    if not token:
        return {"success": False, "error": "missing_token", "platform": platform}

    # MVP: simulate a tiny amount of network latency so the metrics look
    # roughly realistic in dev/staging dashboards.
    await asyncio.sleep(0.05)

    title = payload.get("title", "")
    body = payload.get("body", "")
    logger.info(
        "push.deliver platform=%s token=%s… push_id=%s title=%r",
        platform, token[:8], payload.get("push_id"), title[:40],
    )

    return {
        "success": True,
        "platform": platform,
        "simulated": True,
        "title": title,
        "body_preview": body[:64],
        "ts": time.time(),
    }


async def deliver_push(payload: dict[str, Any]) -> dict[str, Any]:
    """Route push to every device registered against payload['kid'].

    Pipelines ``HGETALL push_device:{id}`` across all the kid's devices so
    we issue one RTT per push instead of N (was N+1).
    """
    kid = payload.get("kid")
    if not kid:
        return {"success": False, "error": "no_kid"}

    r = await get_redis()
    devices = await r.smembers(f"kid:{kid}:push_devices")
    if not devices:
        return {"success": False, "error": "no_devices_registered", "kid": kid}

    device_ids = list(devices)
    pipe = r.pipeline()
    for device_id in device_ids:
        pipe.hgetall(f"push_device:{device_id}")
    device_infos = await pipe.execute()

    # Clean up stale set members in a second pipeline (don't block first
    # one — if cleanup fails we still attempt delivery).
    stale = [
        device_id
        for device_id, info in zip(device_ids, device_infos)
        if not info
    ]
    if stale:
        cleanup = r.pipeline()
        for device_id in stale:
            cleanup.srem(f"kid:{kid}:push_devices", device_id)
        try:
            await cleanup.execute()
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            logger.debug("stale-device cleanup failed kid=%s: %s", kid, exc)

    results: list[dict[str, Any]] = []
    for device_id, device_info in zip(device_ids, device_infos):
        if not device_info:
            continue
        platform = device_info.get("platform")
        token = device_info.get("token")
        result = await _send_to_platform(platform, token, payload)
        results.append({"device_id": device_id, "platform": platform, **result})

    if not results:
        return {"success": False, "error": "no_live_devices", "kid": kid}

    success = any(item.get("success") for item in results)
    return {"success": success, "deliveries": results}


# ── Queue processing ─────────────────────────────────────────────────────


async def _drain_shard(r, queue_key: str, n: int) -> list[str]:
    """LPOP up to n items from a single shard queue.

    Uses pipelined LPOPs — redis-py exposes ``LPOP key count`` only on
    Redis 6.2+; we stick with single-item LPOPs in a pipeline to remain
    compatible with older deployments.
    """
    if n <= 0:
        return []
    pipe = r.pipeline()
    for _ in range(n):
        pipe.lpop(queue_key)
    raw = await pipe.execute()
    return [item for item in raw if item]


async def _drain_shards(r, shards: list[int], total: int) -> list[str]:
    """Drain up to ``total`` items, round-robining across shards.

    First does one llen-pipelined pass so we can fairly distribute the
    batch budget. Falls back to even split when depths aren't known.
    """
    if not shards or total <= 0:
        return []

    # Look at queue depths so we don't waste pipeline slots on empty shards.
    depth_pipe = r.pipeline()
    for s in shards:
        depth_pipe.llen(OUTBOUND_QUEUE_KEYS[s])
    depths = await depth_pipe.execute()

    # Backpressure warning + per-shard quota (proportional, min 1 if depth>0)
    quotas: dict[int, int] = {}
    nonempty = [(s, int(d or 0)) for s, d in zip(shards, depths) if int(d or 0) > 0]
    if not nonempty:
        return []

    for s, d in nonempty:
        if d > QUEUE_DEPTH_WARN:
            logger.warning(
                "Push queue shard %d depth: %d (backpressure)", s, d,
            )

    total_depth = sum(d for _, d in nonempty)
    remaining = total
    for s, d in nonempty:
        # proportional share, clamped to depth
        share = max(1, min(d, (d * total) // total_depth)) if total_depth else 0
        share = min(share, remaining, d)
        quotas[s] = share
        remaining -= share
        if remaining <= 0:
            break

    items: list[str] = []
    # Single pipeline for ALL LPOPs across shards (1 RTT for the batch).
    pipe = r.pipeline()
    plan: list[int] = []  # not strictly needed but keeps debugging easier
    for s, take in quotas.items():
        for _ in range(take):
            pipe.lpop(OUTBOUND_QUEUE_KEYS[s])
            plan.append(s)
    raw = await pipe.execute()
    items.extend([x for x in raw if x])
    return items


async def process_batch(r, shards: list[int]) -> tuple[int, int]:
    """Pop a batch from the assigned shard queues and deliver each item.

    Returns ``(delivered, failed)`` counters for the cycle.
    """
    delivered = 0
    failed = 0

    items = await _drain_shards(r, shards, BATCH_SIZE)
    if not items:
        # Fallback: drain any items still sitting on the legacy non-sharded
        # queue (so this rollout is backwards compatible with any older
        # writer that hasn't been deployed yet).
        items = await _drain_shard(r, LEGACY_OUTBOUND_QUEUE_KEY, BATCH_SIZE)
        if not items:
            return 0, 0

    # 1) Decode + hydrate (pipelined HGETALL push:{id}).
    payloads = [_decode_queue_item(item) for item in items]
    payloads = await _hydrate_payloads_pipelined(r, payloads)

    # 2) Deliver in parallel, bounded by CONCURRENT_LIMIT.
    sem = asyncio.Semaphore(CONCURRENT_LIMIT)

    async def _bounded(p: dict[str, Any]) -> dict[str, Any]:
        try:
            async with sem:
                return await deliver_push(p)
        except Exception as exc:  # noqa: BLE001 — capture per-item failure
            logger.exception("delivery failed: %s", exc)
            return {"success": False, "error": f"exception:{exc!r}"}

    results = await asyncio.gather(*(_bounded(p) for p in payloads))

    # 3) Pre-fetch existing push:{id} hashes + delivery_attempts in one pipeline
    #    (needed to compute attempts++ and skip resurrecting expired keys).
    attempts_pipe = r.pipeline()
    push_ids: list[str | None] = []
    for payload in payloads:
        pid = payload.get("push_id")
        push_ids.append(pid)
        if pid:
            attempts_pipe.hget(f"push:{pid}", "delivery_attempts")
            attempts_pipe.exists(f"push:{pid}")
    raw_attempts = await attempts_pipe.execute() if any(push_ids) else []

    # Walk results paired with (prev_attempts, exists) pairs
    write_pipe = r.pipeline()
    log_entries: list[str] = []
    retries: list[str] = []
    cursor = 0
    now = time.time()
    for payload, pid, result in zip(payloads, push_ids, results):
        prev_attempts = 0
        push_exists = False
        if pid:
            prev_attempts = int(raw_attempts[cursor] or 0)
            push_exists = bool(raw_attempts[cursor + 1])
            cursor += 2

        if result.get("success"):
            delivered += 1
        else:
            failed += 1

        # Status hash mutation (skip if push key has expired).
        if pid and push_exists:
            update = {
                "delivery_status": (
                    "delivered" if result.get("success") else "failed"
                ),
                "delivery_attempts": str(prev_attempts + 1),
            }
            if result.get("success"):
                update["delivered_at"] = str(now)
            else:
                err = str(result.get("error") or "unknown")
                update["last_error"] = err[:200]
            write_pipe.hset(f"push:{pid}", mapping=update)

        log_entries.append(
            json.dumps({
                "push_id": pid,
                "kid": payload.get("kid"),
                "result": result,
                "ts": now,
            })
        )

        # Retry envelope on failure (within attempt budget).
        if not result.get("success"):
            attempts = int(payload.get("attempts", 0)) + 1
            if attempts < MAX_DELIVERY_ATTEMPTS:
                retry_envelope = dict(payload)
                retry_envelope["attempts"] = attempts
                retry_envelope["last_attempt_at"] = now
                retries.append(json.dumps(retry_envelope))

    # 4) Single pipeline for log + log-trim + retries.
    if log_entries:
        write_pipe.lpush(OUTBOUND_LOG_KEY, *log_entries)
        write_pipe.ltrim(OUTBOUND_LOG_KEY, 0, OUTBOUND_LOG_MAX)
    if retries:
        write_pipe.rpush(RETRY_QUEUE_KEY, *retries)

    # 5) Metrics update — persist counters so monitoring can scrape them.
    if delivered:
        write_pipe.hincrby(METRICS_KEY, "delivered_total", delivered)
    if failed:
        write_pipe.hincrby(METRICS_KEY, "failed_total", failed)
    write_pipe.hset(METRICS_KEY, "last_cycle_ts", str(now))
    write_pipe.hset(METRICS_KEY, "last_batch_size", str(len(items)))

    await write_pipe.execute()

    return delivered, failed


async def process_retry(r) -> int:
    """Promote ready retry-queue items back onto a sharded outbound queue.

    Each item carries ``attempts`` and ``last_attempt_at``. We use
    exponential backoff (``2**attempts * base``) — items not yet ripe go
    to the tail of the retry queue so we round-robin through them
    without busy-spinning.

    Returns the number of items promoted in this cycle (one).
    """
    item = await r.lpop(RETRY_QUEUE_KEY)
    if not item:
        return 0

    try:
        payload = json.loads(item)
    except json.JSONDecodeError:
        logger.warning("dropping malformed retry item: %r", item[:80])
        return 0

    last_attempt_at = float(payload.get("last_attempt_at") or 0)
    attempts = int(payload.get("attempts") or 1)

    backoff = (2 ** attempts) * RETRY_BASE_BACKOFF_SECONDS  # 60, 120, 240
    if time.time() < last_attempt_at + backoff:
        # Not ready — return to tail so other items get a turn first.
        await r.rpush(RETRY_QUEUE_KEY, json.dumps(payload))
        return 0

    # Ready: re-enqueue onto the appropriate sharded outbound queue.
    pid = payload.get("push_id") or ""
    target_key = outbound_queue_key(pid) if pid else OUTBOUND_QUEUE_KEYS[0]
    await r.rpush(target_key, json.dumps(payload))
    return 1


# ── Device registration helpers (called from routers) ────────────────────


async def device_register(
    r,
    kid: str,
    platform: str,
    token: str,
    device_id: str | None = None,
) -> str:
    """Register a push device for a kid.

    Returns the resolved ``device_id``. If ``device_id`` is supplied, the
    record is upserted in place (handy for re-registering after token
    rotation on the same physical device). Otherwise we mint a stable id
    by hashing the token.
    """
    if not kid or not platform or not token:
        raise ValueError("kid, platform, token are all required")
    if not device_id:
        device_id = f"pd_{int(time.time())}_{hash(token) & 0xFFFFFFFF:x}"

    await r.hset(
        f"push_device:{device_id}",
        mapping={
            "kid": kid,
            "platform": platform,
            "token": token,
            "registered_at": str(time.time()),
        },
    )
    await r.sadd(f"kid:{kid}:push_devices", device_id)
    return device_id


async def device_unregister(r, kid: str, device_id: str) -> bool:
    """Remove a registered push device. Returns True if anything was deleted."""
    if not kid or not device_id:
        return False
    info = await r.hgetall(f"push_device:{device_id}")
    if info and info.get("kid") and info.get("kid") != kid:
        # device belongs to a different kid — refuse silently to avoid
        # leaking cross-user info.
        return False
    removed_set = await r.srem(f"kid:{kid}:push_devices", device_id)
    removed_hash = await r.delete(f"push_device:{device_id}")
    return bool(removed_set or removed_hash)


# ── Loop ─────────────────────────────────────────────────────────────────


async def run_once(shards: list[int] | None = None) -> dict[str, int]:
    r = await get_redis()
    active_shards = shards if shards is not None else list(range(NUM_SHARDS))
    delivered, failed = await process_batch(r, active_shards)
    promoted = await process_retry(r)
    return {"delivered": delivered, "failed": failed, "retried": promoted}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="push_worker")
    p.add_argument("--once", action="store_true", help="run one cycle and exit")
    p.add_argument(
        "--shard", type=int, default=None,
        help=f"consume only shard N (0..{NUM_SHARDS - 1})",
    )
    p.add_argument(
        "--shards", type=str, default=None,
        help='consume a range of shards, e.g. "0-7" or "1,3,5"',
    )
    return p.parse_args(argv)


def _resolve_shards(args: argparse.Namespace) -> list[int]:
    if args.shard is not None:
        if not 0 <= args.shard < NUM_SHARDS:
            raise SystemExit(
                f"--shard must be in [0,{NUM_SHARDS - 1}], got {args.shard}"
            )
        return [args.shard]
    if args.shards:
        shards = parse_shard_range(args.shards)
        if not shards:
            raise SystemExit(f"--shards yielded no valid shards: {args.shards!r}")
        return shards
    return list(range(NUM_SHARDS))


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    args = _parse_args(sys.argv[1:])
    shards = _resolve_shards(args)

    await init_redis()
    try:
        if args.once:
            result = await run_once(shards)
            print(json.dumps(result))
            return
        logger.info(
            "push_worker started: shards=%s polling every %ss",
            shards, POLL_INTERVAL_SECONDS,
        )
        while True:
            try:
                result = await run_once(shards)
                if result["delivered"] or result["failed"] or result["retried"]:
                    logger.info("cycle: %s", result)
            except Exception as exc:
                logger.exception("cycle failed: %s", exc)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    finally:
        await close_redis()


if __name__ == "__main__":
    asyncio.run(main())
