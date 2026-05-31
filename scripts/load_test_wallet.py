"""A · Load-SLA harness for wallet cross-brand path.

Bible §1.4 says: "cross-brand commission transfer atomic in code but
not battle-tested under sustained load". This module defines the SLA
and the harness that enforces it.

## SLA

| Metric | Target | Rationale |
|---|---|---|
| Sustained throughput | 10,000 ops/sec | KiX deck claim: 100m daily events ≈ 1,150/sec avg, 10x peak |
| p99 latency | < 500 ms | Wallet write is in the user critical path |
| p99.9 latency | < 1,000 ms | Hard ceiling — anything slower visibly degrades UX |
| Error rate | < 0.01% | Money math; ANY error needs forensic trail |
| Deadlock count (over 1h) | 0 | Wallet uses Redis WATCH/MULTI; deadlocks = data corruption risk |
| Memory growth (over 1h) | < 50 MB / worker | Catches consumer leaks |

## Why locust

- Pure Python — same venv as the codebase, no JS toolchain
- Distributed mode for >1 machine
- HTTP + non-HTTP (we use the latter — direct service call, not HTTP)
- Real-time web UI for ad-hoc; CLI + headless for CI

## Usage

  # Smoke test (CI gate)
  python -m scripts.load_test_wallet --mode smoke

  # Soak test (staging only — pre-prod gate)
  python -m scripts.load_test_wallet --mode soak --duration 3600

  # Headless CI run (smoke + report)
  python -m scripts.load_test_wallet --mode smoke --json /tmp/load-report.json

## What it exercises

The cross-brand commission path (commission_split):
  brand_A.wallet -= amount
  brand_B.wallet += amount * (1 - take_rate)
  kix.wallet     += amount * take_rate

All three in one WATCH/MULTI transaction. Bible §1.4 + ADR-12 promise atomicity.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class LoadSLA:
    """The contract — what the harness enforces."""
    sustained_ops_per_sec: int = 10_000
    p99_latency_ms: float = 500.0
    p99_9_latency_ms: float = 1000.0
    max_error_rate_pct: float = 0.01
    max_deadlocks: int = 0
    duration_seconds: int = 60        # smoke=60s, soak=3600s
    memory_growth_mb: float = 50.0


@dataclass
class LoadReport:
    sla: LoadSLA
    mode: str
    actual_ops: int = 0
    actual_duration_s: float = 0.0
    actual_ops_per_sec: float = 0.0
    p50_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    p99_9_latency_ms: float = 0.0
    max_latency_ms: float = 0.0
    error_count: int = 0
    error_rate_pct: float = 0.0
    deadlock_count: int = 0
    passed_sla: bool = False
    sla_failures: list[str] = field(default_factory=list)


# ── Simulated wallet path (smoke mode) ──
# In real staging run we'd import app.services.wallet.commission_split.
# For local smoke we simulate the timing profile so the harness itself
# can be tested without a live Redis cluster.


class _SimulatedWallet:
    """Mimics the WATCH/MULTI commission_split timing characteristics."""

    def __init__(self):
        self._lock_contention = 0

    async def commission_split(self, brand_a: str, brand_b: str,
                               amount: float, take_rate: float) -> dict:
        """Returns {ok, latency_ms, deadlock_flag} — mimics the real call signature.

        Latency model: 0.5-3ms base + 0.5-2ms variance + occasional 50ms spike.
        Real wallet should match or beat this; smoke harness just verifies
        the SLA arithmetic works.
        """
        import random
        base = random.uniform(0.5, 3.0)
        variance = random.uniform(0.5, 2.0)
        # 1-in-10000 chance of slow path (network blip)
        spike = random.uniform(20, 80) if random.random() < 0.0001 else 0
        latency_ms = base + variance + spike
        await asyncio.sleep(latency_ms / 1000)
        return {
            "ok": True,
            "latency_ms": latency_ms,
            "deadlock": False,
        }


class _RealRedisWallet:
    """Wave N Phase D · exercises a real Redis connection via WATCH/MULTI.

    Approximates the production commission_split path:
      WATCH wallet:{a}:balance · wallet:{b}:balance · wallet:kix:balance
      check balance_a >= amount
      MULTI: DECRBY a · INCRBY b · INCRBY kix · EXEC
      retry on WatchError (deadlock proxy)

    For staging runs only. Reset balances before each soak.
    """

    def __init__(self, redis_url: str):
        try:
            import redis.asyncio as redis_async
        except ImportError as e:
            raise RuntimeError(
                "redis-py not installed in current venv — pip install redis>=5.0"
            ) from e
        self._r = redis_async.from_url(redis_url, decode_responses=True)
        self._wlist = [f"wallet:bench_brand_{i}:balance" for i in range(1000)]
        self._kix = "wallet:bench_kix:balance"

    async def setup(self):
        """Seed each test wallet with enough balance for the run."""
        pipe = self._r.pipeline()
        for w in self._wlist:
            pipe.set(w, "1000000000")    # 10M units · plenty for 60min @ 10k/sec
        pipe.set(self._kix, "0")
        await pipe.execute()

    async def commission_split(self, brand_a: str, brand_b: str,
                               amount: float, take_rate: float) -> dict:
        import time
        t0 = time.monotonic()
        amount_units = int(amount * 100)
        take_units = int(amount_units * take_rate)
        merchant_units = amount_units - take_units
        wa = f"wallet:{brand_a}:balance"
        wb = f"wallet:{brand_b}:balance"
        deadlock = False
        # Retry up to 3 times on WATCH conflict (mimics real prod retry policy)
        from redis.exceptions import WatchError
        for attempt in range(3):
            async with self._r.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(wa, wb, self._kix)
                    bal_a = int(await self._r.get(wa) or 0)
                    if bal_a < amount_units:
                        await pipe.unwatch()
                        return {"ok": False, "latency_ms": (time.monotonic() - t0) * 1000,
                                "deadlock": False, "error": "insufficient_balance"}
                    pipe.multi()
                    pipe.decrby(wa, amount_units)
                    pipe.incrby(wb, merchant_units)
                    pipe.incrby(self._kix, take_units)
                    await pipe.execute()
                    return {"ok": True, "latency_ms": (time.monotonic() - t0) * 1000,
                            "deadlock": deadlock}
                except WatchError:
                    deadlock = True
                    continue
        return {"ok": False, "latency_ms": (time.monotonic() - t0) * 1000,
                "deadlock": True, "error": "max_watch_retries"}

    async def teardown(self):
        try:
            await self._r.aclose()
        except AttributeError:
            await self._r.close()


async def _run_smoke_load(sla: LoadSLA, mode: str, redis_url: str = "") -> LoadReport:
    """Drive load. Real Redis if redis_url given (Wave N Phase D), else simulator."""
    if redis_url:
        wallet = _RealRedisWallet(redis_url)
        await wallet.setup()
        print(f"  · using REAL Redis at {redis_url}")
    else:
        wallet = _SimulatedWallet()
    report = LoadReport(sla=sla, mode=mode)
    latencies: list[float] = []
    errors = 0
    deadlocks = 0

    # Concurrency limits: we want sustained_ops_per_sec
    # Each op ≈ 2-5ms, so we need (target_ops × avg_latency / 1000) workers
    target_concurrency = min(500, max(50, sla.sustained_ops_per_sec // 100))

    print(f"  · target {sla.sustained_ops_per_sec} ops/sec for {sla.duration_seconds}s")
    print(f"  · concurrency {target_concurrency} workers")

    stop_at = time.monotonic() + sla.duration_seconds
    sem = asyncio.Semaphore(target_concurrency)

    async def _one_op(i: int):
        nonlocal errors, deadlocks
        async with sem:
            try:
                result = await wallet.commission_split(
                    f"brand_a_{i % 1000}", f"brand_b_{(i + 1) % 1000}",
                    amount=10.0 + (i % 100), take_rate=0.10,
                )
                if not result.get("ok"):
                    errors += 1
                if result.get("deadlock"):
                    deadlocks += 1
                latencies.append(result["latency_ms"])
            except Exception:
                errors += 1

    started = time.monotonic()
    i = 0
    in_flight = set()
    while time.monotonic() < stop_at:
        # Pace by sleeping (1/target_ops_per_sec) between op launches
        task = asyncio.create_task(_one_op(i))
        in_flight.add(task)
        task.add_done_callback(in_flight.discard)
        i += 1
        # Small yield so loop doesn't starve
        if i % 100 == 0:
            await asyncio.sleep(1.0 / sla.sustained_ops_per_sec * 100)
    # Drain
    if in_flight:
        await asyncio.gather(*in_flight, return_exceptions=True)

    actual_duration = time.monotonic() - started
    report.actual_ops = len(latencies) + errors
    report.actual_duration_s = round(actual_duration, 2)
    report.actual_ops_per_sec = round(report.actual_ops / actual_duration, 1) if actual_duration else 0
    report.error_count = errors
    report.error_rate_pct = round(100.0 * errors / max(1, report.actual_ops), 4)
    report.deadlock_count = deadlocks
    if latencies:
        latencies.sort()
        report.p50_latency_ms = round(statistics.median(latencies), 2)
        report.p99_latency_ms = round(latencies[int(len(latencies) * 0.99)], 2)
        report.p99_9_latency_ms = round(latencies[int(len(latencies) * 0.999)], 2)
        report.max_latency_ms = round(latencies[-1], 2)

    # SLA check
    failures = []
    if report.actual_ops_per_sec < sla.sustained_ops_per_sec * 0.95:
        failures.append(
            f"throughput {report.actual_ops_per_sec:.0f}/s "
            f"< {sla.sustained_ops_per_sec} target (95% threshold)"
        )
    if report.p99_latency_ms > sla.p99_latency_ms:
        failures.append(f"p99 {report.p99_latency_ms}ms > {sla.p99_latency_ms}ms SLA")
    if report.p99_9_latency_ms > sla.p99_9_latency_ms:
        failures.append(f"p99.9 {report.p99_9_latency_ms}ms > {sla.p99_9_latency_ms}ms SLA")
    if report.error_rate_pct > sla.max_error_rate_pct:
        failures.append(f"error rate {report.error_rate_pct}% > {sla.max_error_rate_pct}% SLA")
    if report.deadlock_count > sla.max_deadlocks:
        failures.append(f"deadlocks {report.deadlock_count} > {sla.max_deadlocks} SLA")
    report.sla_failures = failures
    report.passed_sla = not failures
    if isinstance(wallet, _RealRedisWallet):
        await wallet.teardown()
    return report


def _print(report: LoadReport):
    print("\n" + "=" * 68)
    print(f"Wallet cross-brand load report · mode={report.mode}")
    print("=" * 68)
    print(f"  Duration       : {report.actual_duration_s}s")
    print(f"  Total ops      : {report.actual_ops:,}")
    print(f"  Throughput     : {report.actual_ops_per_sec:,.0f} ops/sec "
          f"(target {report.sla.sustained_ops_per_sec:,})")
    print(f"  p50 latency    : {report.p50_latency_ms} ms")
    print(f"  p99 latency    : {report.p99_latency_ms} ms (SLA {report.sla.p99_latency_ms})")
    print(f"  p99.9 latency  : {report.p99_9_latency_ms} ms (SLA {report.sla.p99_9_latency_ms})")
    print(f"  max latency    : {report.max_latency_ms} ms")
    print(f"  errors         : {report.error_count} ({report.error_rate_pct}% · SLA <{report.sla.max_error_rate_pct}%)")
    print(f"  deadlocks      : {report.deadlock_count} (SLA <={report.sla.max_deadlocks})")
    print(f"  SLA verdict    : {'✅ PASS' if report.passed_sla else '✗ FAIL'}")
    if report.sla_failures:
        for f in report.sla_failures:
            print(f"    - {f}")
    print("=" * 68)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", default="smoke",
                   choices=("smoke", "soak"),
                   help="smoke=60s @ 5k/sec · soak=3600s @ 10k/sec")
    p.add_argument("--duration", type=int, default=0,
                   help="override duration in seconds")
    p.add_argument("--ops-per-sec", type=int, default=0,
                   help="override target throughput")
    p.add_argument("--json", default="",
                   help="path to write JSON report")
    p.add_argument("--real-redis", default="",
                   help="redis://host:port URL · Wave N Phase D · STAGING ONLY")
    args = p.parse_args()

    sla = LoadSLA()
    if args.mode == "smoke":
        sla.duration_seconds = args.duration or 60
        sla.sustained_ops_per_sec = args.ops_per_sec or 5_000   # easier smoke target
    else:
        sla.duration_seconds = args.duration or 3600
        sla.sustained_ops_per_sec = args.ops_per_sec or 10_000

    report = asyncio.run(_run_smoke_load(sla, args.mode, args.real_redis))
    _print(report)

    if args.json:
        Path(args.json).write_text(json.dumps(asdict(report), indent=2, default=str))
        print(f"\nJSON report → {args.json}")

    return 0 if report.passed_sla else 1


if __name__ == "__main__":
    raise SystemExit(main())
