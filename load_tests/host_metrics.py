"""Lightweight host metric sampler.

Polls CPU/RAM, postgres connection count, redis memory at 5s cadence and
writes a CSV next to the locust CSVs. Designed to run as a sidecar:

    python -m load_tests.host_metrics --out load_tests/results/host_baseline.csv

Both postgres and redis are optional — if libs aren't installed or the
connection fails the columns are left blank instead of crashing the sampler.
"""
from __future__ import annotations

import argparse
import csv
import os
import socket
import time
from datetime import datetime, timezone

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None

try:
    import psycopg2  # type: ignore
except Exception:  # pragma: no cover
    psycopg2 = None

try:
    import redis  # type: ignore
except Exception:  # pragma: no cover
    redis = None


def _pg_active_conns() -> int | None:
    if not psycopg2:
        return None
    dsn = os.environ.get("KIX_PG_DSN")
    if not dsn:
        return None
    try:
        with psycopg2.connect(dsn, connect_timeout=2) as c:  # type: ignore[arg-type]
            with c.cursor() as cur:
                cur.execute("SELECT count(*) FROM pg_stat_activity WHERE state='active'")
                return int(cur.fetchone()[0])
    except Exception:
        return None


def _redis_mem_mb() -> float | None:
    if not redis:
        return None
    url = os.environ.get("KIX_REDIS_URL", "redis://localhost:6379/0")
    try:
        r = redis.Redis.from_url(url, socket_timeout=2)
        info = r.info("memory")
        return round(info.get("used_memory", 0) / (1024 * 1024), 2)
    except Exception:
        return None


def sample_once() -> dict:
    cpu = psutil.cpu_percent(interval=0.1) if psutil else None
    ram = psutil.virtual_memory().percent if psutil else None
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "host": socket.gethostname(),
        "cpu_pct": cpu,
        "ram_pct": ram,
        "pg_active_conns": _pg_active_conns(),
        "redis_used_mb": _redis_mem_mb(),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--duration", type=float, default=60 * 60)
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "ts", "host", "cpu_pct", "ram_pct", "pg_active_conns", "redis_used_mb",
        ])
        w.writeheader()
        end = time.time() + args.duration
        while time.time() < end:
            row = sample_once()
            w.writerow(row)
            f.flush()
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
