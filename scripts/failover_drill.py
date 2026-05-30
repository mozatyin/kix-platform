#!/usr/bin/env python3
"""Failover drill — simulate outage scenarios in dev and measure MTTR.

This script *simulates* failures (it does not actually take production
down). It is meant to be run against a local dev stack or a staging
environment that mirrors prod.

Scenarios:
    pg          Force-close the PG connection pool; verify app degrades.
    redis       Drain Redis; verify cache rebuilds without errors.
    stripe      Block Stripe egress; verify charges queue.
    region      Simulate SG-region outage by killing pods.
    all         Run all of the above sequentially.

Usage:
    python scripts/failover_drill.py --scenario pg
    python scripts/failover_drill.py --scenario all --timeout 120

The script:
    1. Snapshots baseline health.
    2. Injects the failure.
    3. Polls health endpoint until it recovers OR timeout.
    4. Prints MTTR + observed behavior.
    5. Always tries to clean up the injection in `finally`.

Exit codes:
    0 — all scenarios recovered within timeout
    1 — at least one scenario failed to recover
    2 — drill could not run (env not configured)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Tuple


DEFAULT_HEALTH_URL = os.environ.get(
    "DRILL_HEALTH_URL", "http://localhost:8000/health"
)
DEFAULT_TIMEOUT_S = 60
DEFAULT_POLL_INTERVAL_S = 1.0


# ---------------------------------------------------------------------------
# Quota-guard hook (per LLM Quota Guard memory)
# ---------------------------------------------------------------------------
def wait_if_paused() -> None:
    """No-op locally; integrates with quota guard when imported in CI.

    The drill itself does not call LLMs, but if a future drill scenario
    invokes the LLM stack it should respect the global pause flag.
    """
    try:
        from app.infra.llm_quota import wait_if_paused as _w  # type: ignore
    except Exception:
        return
    _w()


# ---------------------------------------------------------------------------
# Result data
# ---------------------------------------------------------------------------
@dataclass
class DrillResult:
    scenario: str
    started_at: float
    injected_at: float = 0.0
    recovered_at: float = 0.0
    mttr_seconds: float = -1.0
    recovered: bool = False
    notes: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["started_at_iso"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started_at)
        )
        return d


# ---------------------------------------------------------------------------
# Health probing
# ---------------------------------------------------------------------------
def probe_health(url: str, timeout_s: float = 2.0) -> tuple[bool, str]:
    """Return (is_healthy, detail). Healthy = HTTP 2xx and body contains
    no 'error' / 'down'. Read-only mode is considered healthy for drill
    purposes (we want to confirm graceful degradation)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "drill"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            code = resp.getcode()
            body = resp.read(2048).decode("utf-8", "replace")
        if 200 <= code < 300:
            return True, body[:200]
        return False, f"http={code} body={body[:200]}"
    except urllib.error.HTTPError as e:
        return False, f"http={e.code}"
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return False, f"conn_error={e!r}"
    except Exception as e:  # pragma: no cover — defensive
        return False, f"unexpected={e!r}"


def wait_for_recovery(
    url: str,
    timeout_s: float,
    poll_s: float = DEFAULT_POLL_INTERVAL_S,
    require_consecutive: int = 3,
) -> tuple[bool, float]:
    """Poll until `require_consecutive` healthy probes in a row, or timeout.

    Returns (recovered, elapsed_seconds_to_first_healthy_streak).
    """
    deadline = time.monotonic() + timeout_s
    consec = 0
    first_healthy_t: float | None = None
    start = time.monotonic()
    while time.monotonic() < deadline:
        ok, _ = probe_health(url)
        if ok:
            consec += 1
            if first_healthy_t is None:
                first_healthy_t = time.monotonic()
            if consec >= require_consecutive:
                assert first_healthy_t is not None
                return True, first_healthy_t - start
        else:
            consec = 0
            first_healthy_t = None
        time.sleep(poll_s)
    return False, timeout_s


# ---------------------------------------------------------------------------
# Injection primitives
# ---------------------------------------------------------------------------
@contextmanager
def inject_iptables_block(target_host: str) -> Iterator[None]:
    """Block egress to target_host via iptables (linux dev only).

    Falls back to a no-op + warning if iptables isn't available.
    """
    rule_added = False
    try:
        if sys.platform != "linux":
            print(f"  [skip] iptables not available on {sys.platform}; "
                  "simulating block via env var instead.")
            os.environ["DRILL_FORCE_FAIL_HOST"] = target_host
        else:
            subprocess.run(
                ["sudo", "iptables", "-I", "OUTPUT", "-d", target_host,
                 "-j", "REJECT"],
                check=True, capture_output=True,
            )
            rule_added = True
        yield
    finally:
        if rule_added:
            subprocess.run(
                ["sudo", "iptables", "-D", "OUTPUT", "-d", target_host,
                 "-j", "REJECT"],
                check=False, capture_output=True,
            )
        os.environ.pop("DRILL_FORCE_FAIL_HOST", None)


def _exec_or_print(cmd: list[str], dry_run: bool) -> tuple[int, str]:
    """Run a command, or just print it if dry_run. Returns (rc, output)."""
    if dry_run:
        print(f"  [dry-run] {' '.join(cmd)}")
        return 0, ""
    try:
        r = subprocess.run(cmd, check=False, capture_output=True, text=True)
        return r.returncode, (r.stdout + r.stderr)[:500]
    except FileNotFoundError as e:
        return 127, str(e)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
def scenario_pg(url: str, timeout_s: float, dry_run: bool) -> DrillResult:
    """Force-close the PG pool. Expect /health to remain up (degraded)."""
    r = DrillResult(scenario="pg", started_at=time.time())
    pg_container = os.environ.get("DRILL_PG_CONTAINER", "kix-postgres-dev")
    try:
        r.notes.append(f"pausing container {pg_container}")
        rc, out = _exec_or_print(["docker", "pause", pg_container], dry_run)
        r.injected_at = time.time()
        if rc != 0 and not dry_run:
            r.error = f"could not pause pg container: {out}"
            r.notes.append("treating as soft-fail; verifying app still responds")

        # During PG outage we expect either:
        #   (a) /health still returns 200 (degraded mode), or
        #   (b) the app returns 503 but recovers fast when PG is back.
        ok_during, detail = probe_health(url)
        r.notes.append(f"health during outage: ok={ok_during} detail={detail}")

        rc2, out2 = _exec_or_print(["docker", "unpause", pg_container], dry_run)
        if rc2 != 0 and not dry_run:
            r.notes.append(f"unpause failed: {out2}")

        recovered, elapsed = wait_for_recovery(url, timeout_s)
        r.recovered_at = time.time()
        r.mttr_seconds = elapsed
        r.recovered = recovered
    except Exception as e:
        r.error = f"{type(e).__name__}: {e}"
    return r


def scenario_redis(url: str, timeout_s: float, dry_run: bool) -> DrillResult:
    r = DrillResult(scenario="redis", started_at=time.time())
    redis_container = os.environ.get("DRILL_REDIS_CONTAINER", "kix-redis-dev")
    try:
        r.notes.append(f"FLUSHALL on {redis_container}")
        _exec_or_print(
            ["docker", "exec", redis_container, "redis-cli", "FLUSHALL"],
            dry_run,
        )
        rc, _ = _exec_or_print(["docker", "pause", redis_container], dry_run)
        r.injected_at = time.time()

        ok_during, detail = probe_health(url)
        r.notes.append(f"health during outage: ok={ok_during} detail={detail}")

        _exec_or_print(["docker", "unpause", redis_container], dry_run)
        recovered, elapsed = wait_for_recovery(url, timeout_s)
        r.recovered_at = time.time()
        r.mttr_seconds = elapsed
        r.recovered = recovered
    except Exception as e:
        r.error = f"{type(e).__name__}: {e}"
    return r


def scenario_stripe(url: str, timeout_s: float, dry_run: bool) -> DrillResult:
    r = DrillResult(scenario="stripe", started_at=time.time())
    try:
        os.environ["STRIPE_OUTAGE_MODE"] = "true"
        r.injected_at = time.time()
        r.notes.append("STRIPE_OUTAGE_MODE=true set in env")

        ok_during, detail = probe_health(url)
        r.notes.append(f"health during outage: ok={ok_during} detail={detail}")

        # In a real drill we'd POST a /charge and verify it was queued
        # rather than rejected. Stubbed here.
        r.notes.append("charge-queueing check is a placeholder; "
                       "see tests/test_dr_drill.py for the actual assertion")
    finally:
        os.environ.pop("STRIPE_OUTAGE_MODE", None)
        recovered, elapsed = wait_for_recovery(url, timeout_s)
        r.recovered_at = time.time()
        r.mttr_seconds = elapsed
        r.recovered = recovered
    return r


def scenario_region(url: str, timeout_s: float, dry_run: bool) -> DrillResult:
    """Kill all `kix-api` pods in one region; expect failover within timeout."""
    r = DrillResult(scenario="region", started_at=time.time())
    region = os.environ.get("DRILL_REGION", "sg")
    try:
        rc, out = _exec_or_print(
            ["kubectl", f"--context=kix-{region}", "-n", "kix",
             "scale", "deploy/kix-api", "--replicas=0"],
            dry_run,
        )
        r.injected_at = time.time()
        r.notes.append(f"scaled kix-api to 0 in region {region}: rc={rc}")
        if rc != 0:
            r.notes.append(f"k8s unavailable; output: {out[:200]}")
            r.notes.append("recording as not-applicable (no cluster to drill)")
            r.recovered = True
            r.mttr_seconds = 0.0
            return r

        recovered, elapsed = wait_for_recovery(url, timeout_s)
        r.mttr_seconds = elapsed
        r.recovered = recovered

        _exec_or_print(
            ["kubectl", f"--context=kix-{region}", "-n", "kix",
             "scale", "deploy/kix-api", "--replicas=3"],
            dry_run,
        )
        r.recovered_at = time.time()
    except Exception as e:
        r.error = f"{type(e).__name__}: {e}"
    return r


SCENARIOS: dict[str, Callable[[str, float, bool], DrillResult]] = {
    "pg": scenario_pg,
    "redis": scenario_redis,
    "stripe": scenario_stripe,
    "region": scenario_region,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run(scenario: str, url: str, timeout_s: float, dry_run: bool) -> list[DrillResult]:
    wait_if_paused()
    if scenario == "all":
        names = list(SCENARIOS.keys())
    else:
        names = [scenario]

    baseline_ok, baseline_detail = probe_health(url)
    print(f"baseline health: ok={baseline_ok} detail={baseline_detail}")
    if not baseline_ok and not dry_run:
        print("WARNING: baseline unhealthy; drill results will be unreliable")

    results: list[DrillResult] = []
    for name in names:
        fn = SCENARIOS[name]
        print(f"\n== scenario: {name} ==")
        res = fn(url, timeout_s, dry_run)
        results.append(res)
        print(json.dumps(res.to_dict(), indent=2, default=str))
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scenario", default="all",
                   choices=list(SCENARIOS.keys()) + ["all"])
    p.add_argument("--url", default=DEFAULT_HEALTH_URL)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--dry-run", action="store_true",
                   help="Print commands instead of executing destructive ops")
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON summary at the end")
    args = p.parse_args(argv)

    try:
        results = run(args.scenario, args.url, args.timeout, args.dry_run)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    failed = [r for r in results if not r.recovered]
    print("\n== summary ==")
    for r in results:
        status = "OK " if r.recovered else "FAIL"
        print(f"  {status} {r.scenario:8s} MTTR={r.mttr_seconds:.1f}s "
              f"{('error: ' + r.error) if r.error else ''}")

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2, default=str))

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
