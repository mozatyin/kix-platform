"""Post-test analysis: parse Locust CSVs, find bottlenecks, write report.

Usage:
    python -m load_tests.analyze --results load_tests/results \
        --out /Users/mozat/a-docs/load-test-report.md

Locust writes three CSVs per run prefix:
    <prefix>_stats.csv          aggregated stats per endpoint
    <prefix>_stats_history.csv  time-series of req/s + p95/p99
    <prefix>_failures.csv       one row per failure type

We aggregate across all prefixes found and produce a single markdown report.
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# SLO definitions (matches the in-locust detector)
P95_BUDGET_MS = 1000.0
ERR_RATE_BUDGET = 0.01


@dataclass
class EndpointStat:
    profile: str
    name: str
    requests: int
    failures: int
    median_ms: float
    p95_ms: float
    p99_ms: float
    rps: float

    @property
    def err_rate(self) -> float:
        return (self.failures / self.requests) if self.requests else 0.0


def _read_stats_csv(path: Path) -> list[EndpointStat]:
    profile = path.name.replace("_stats.csv", "")
    out: list[EndpointStat] = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("Name") or row.get("name") or ""
            if not name or name == "Aggregated":
                continue
            try:
                reqs = int(row.get("Request Count", 0) or 0)
                fails = int(row.get("Failure Count", 0) or 0)
                # locust uses "50%", "95%", "99%" headers
                med = float(row.get("50%", 0) or 0)
                p95 = float(row.get("95%", 0) or 0)
                p99 = float(row.get("99%", 0) or 0)
                rps = float(row.get("Requests/s", 0) or 0)
            except (ValueError, TypeError):
                continue
            out.append(EndpointStat(profile, name, reqs, fails, med, p95, p99, rps))
    return out


def _aggregated_row(path: Path) -> dict | None:
    with open(path) as f:
        for row in csv.DictReader(f):
            if (row.get("Name") or row.get("name")) == "Aggregated":
                return row
    return None


def _read_history(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with open(path) as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def _detect_breaking_step(history: list[dict]) -> tuple[int | None, dict | None]:
    """Find first time-point where p95 > 1s or error rate > 1%."""
    for row in history:
        try:
            user_count = int(float(row.get("User Count", 0) or 0))
            p95 = float(row.get("95%", 0) or 0)
            reqs = float(row.get("Total Request Count", 0) or 0)
            fails = float(row.get("Total Failure Count", 0) or 0)
        except (ValueError, TypeError):
            continue
        err_rate = (fails / reqs) if reqs else 0.0
        if p95 > P95_BUDGET_MS or err_rate > ERR_RATE_BUDGET:
            return user_count, row
    return None, None


def _capacity_table(profiles: dict[str, list[EndpointStat]]) -> str:
    lines = ["| Profile | Total RPS | Aggregate p95 | Error rate | Within SLO |",
             "|---|---|---|---|---|"]
    for profile, stats in profiles.items():
        if not stats:
            continue
        total_reqs = sum(s.requests for s in stats)
        total_fails = sum(s.failures for s in stats)
        # weighted p95 ≈ max(p95) across endpoints
        p95 = max((s.p95_ms for s in stats), default=0.0)
        rps = sum(s.rps for s in stats)
        err = (total_fails / total_reqs) if total_reqs else 0.0
        ok = "✓" if p95 <= P95_BUDGET_MS and err <= ERR_RATE_BUDGET else "✗"
        lines.append(
            f"| {profile} | {rps:.1f} | {p95:.0f} ms | {err:.2%} | {ok} |"
        )
    return "\n".join(lines)


def _slowest_table(all_stats: list[EndpointStat], n: int = 10) -> str:
    # Drop endpoints with negligible volume (<100 reqs) to avoid noise.
    candidates = [s for s in all_stats if s.requests >= 100]
    candidates.sort(key=lambda s: s.p95_ms, reverse=True)
    lines = ["| Rank | Endpoint | Profile | Reqs | p95 ms | p99 ms | err |",
             "|---|---|---|---|---|---|---|"]
    for i, s in enumerate(candidates[:n], start=1):
        lines.append(
            f"| {i} | `{s.name}` | {s.profile} | {s.requests} | "
            f"{s.p95_ms:.0f} | {s.p99_ms:.0f} | {s.err_rate:.2%} |"
        )
    return "\n".join(lines) if len(lines) > 2 else "_(no endpoints with >=100 requests)_"


def _highest_err_table(all_stats: list[EndpointStat], n: int = 10) -> str:
    candidates = [s for s in all_stats if s.requests >= 50 and s.failures > 0]
    candidates.sort(key=lambda s: s.err_rate, reverse=True)
    lines = ["| Endpoint | Profile | Reqs | Failures | Err % |",
             "|---|---|---|---|---|"]
    for s in candidates[:n]:
        lines.append(
            f"| `{s.name}` | {s.profile} | {s.requests} | {s.failures} | "
            f"{s.err_rate:.2%} |"
        )
    return "\n".join(lines) if len(lines) > 2 else "_(no failing endpoints)_"


def _recommendations(all_stats: list[EndpointStat]) -> list[str]:
    """Map observed pathologies → likely fix. Generic heuristics only."""
    recs: list[str] = []
    by_name: dict[str, EndpointStat] = {}
    for s in all_stats:
        prev = by_name.get(s.name)
        if prev is None or s.p95_ms > prev.p95_ms:
            by_name[s.name] = s

    def hit(substr: str) -> EndpointStat | None:
        return next((s for n, s in by_name.items() if substr in n), None)

    if (s := hit("/dashboards/brand")) and s.p95_ms > 500:
        recs.append(
            f"- Dashboard p95 = {s.p95_ms:.0f}ms — cache the rollup in Redis "
            f"(TTL 30-60s) and serve stale-while-revalidate. Est gain: 5-10×.")
    if (s := hit("/campaigns/[brand_id]")) and s.p95_ms > 400:
        recs.append(
            f"- `list_campaigns` p95 = {s.p95_ms:.0f}ms — likely missing index on "
            f"`campaigns(brand_id, status, updated_at desc)`. Est gain: 3-8×.")
    if (s := hit("/wallet/")) and s.p95_ms > 300:
        recs.append(
            f"- `wallet` p95 = {s.p95_ms:.0f}ms — hot row contention on wallet "
            f"balance. Use atomic INCR in Redis and reconcile to PG async. "
            f"Est gain: 10×+.")
    if (s := hit("/auction/run")) and s.p95_ms > 500:
        recs.append(
            f"- `auction.run` p95 = {s.p95_ms:.0f}ms — bidder scan is O(N). "
            f"Pre-shard by audience+region, keep candidate list in Redis ZSET. "
            f"Est gain: 4-6×.")
    if (s := hit("/auction/report-engagement")) and s.p95_ms > 250:
        recs.append(
            f"- `report-engagement` p95 = {s.p95_ms:.0f}ms — write-amplification "
            f"from synchronous fraud-check. Move to async worker, ack immediately. "
            f"Est gain: 3×.")
    if (s := hit("HERD")) and s.err_rate > 0.005:
        recs.append(
            f"- Thundering-herd `create_campaign` err = {s.err_rate:.2%} — PG "
            f"connection pool is exhausted. Raise pool size (or PgBouncer "
            f"transaction-mode) and add per-brand rate limit. ")
    if (s := hit("HOT")) and s.p95_ms > 200:
        recs.append(
            f"- Hot-key p95 = {s.p95_ms:.0f}ms — Redis hot key on single "
            f"campaign counter. Shard counter across N keys, sum on read. "
            f"Est gain: linear in N.")
    if (s := hit("WEBHOOK")) and (s.err_rate > 0.005 or s.p95_ms > 500):
        recs.append(
            f"- Stripe webhook p95 = {s.p95_ms:.0f}ms / err = {s.err_rate:.2%} — "
            f"do not process inline. Enqueue and 200 OK immediately.")
    if (s := hit("PACING")) and s.p95_ms > 200:
        recs.append(
            f"- Pacing controller p95 = {s.p95_ms:.0f}ms — `pacing_controller.py` "
            f"holds a row-level lock per spend. Move to lock-free token bucket in "
            f"Redis, reconcile every N seconds.")
    if not recs:
        recs.append("- No high-confidence bottlenecks at this volume. Re-run "
                    "the `breaking` profile to find the next ceiling.")
    return recs


def _breaking_section(results_dir: Path) -> str:
    history_path = results_dir / "breaking_stats_history.csv"
    if not history_path.exists():
        return "_(no breaking-profile history found — run `load_tests/breaking.py`)_"
    history = _read_history(history_path)
    bp_users, bp_row = _detect_breaking_step(history)
    if bp_users is None:
        return ("All profile levels stayed within SLO (p95 ≤ 1s, err ≤ 1%). "
                "Recommend pushing `breaking.py` past 50K concurrent users.")
    p95 = float(bp_row.get("95%", 0) or 0)
    reqs = float(bp_row.get("Total Request Count", 0) or 0)
    fails = float(bp_row.get("Total Failure Count", 0) or 0)
    err = (fails / reqs) if reqs else 0.0
    # roughly: merchants ≈ 1/11 of users (1:10 split)
    merchants = int(bp_users / 11)
    return (
        f"- **Breaking point detected at ~{bp_users} concurrent users** "
        f"(~{merchants} merchants + ~{bp_users - merchants} consumers)\n"
        f"- p95 at break: **{p95:.0f} ms**, error rate: **{err:.2%}**\n"
        f"- vs claim of **10K merchants**: "
        f"{'EXCEEDS' if merchants >= 10_000 else 'BELOW'} claim by "
        f"{abs(merchants - 10_000):,} merchants"
    )


def build_report(results_dir: Path) -> str:
    profiles: dict[str, list[EndpointStat]] = {}
    for path in sorted(results_dir.glob("*_stats.csv")):
        profile = path.name.replace("_stats.csv", "")
        profiles[profile] = _read_stats_csv(path)
    all_stats: list[EndpointStat] = [s for lst in profiles.values() for s in lst]

    sections: list[str] = []
    sections.append("# KiX Load Test Report")
    sections.append("")
    sections.append("_Generated by `load_tests/analyze.py`. SLO: p95 ≤ 1000 ms, "
                    "error rate ≤ 1%._")
    sections.append("")
    sections.append("## Capacity headroom by profile")
    sections.append("")
    sections.append(_capacity_table(profiles) if profiles
                    else "_(no profile data — run baseline/stress/breaking first)_")
    sections.append("")
    sections.append("## Breaking-point estimate")
    sections.append("")
    sections.append(_breaking_section(results_dir))
    sections.append("")
    sections.append("## Top 10 slowest endpoints (p95)")
    sections.append("")
    sections.append(_slowest_table(all_stats))
    sections.append("")
    sections.append("## Endpoints with highest error rate")
    sections.append("")
    sections.append(_highest_err_table(all_stats))
    sections.append("")
    sections.append("## Recommended optimizations")
    sections.append("")
    sections.extend(_recommendations(all_stats))
    sections.append("")
    sections.append("## Comparison vs claimed 10K-merchant target")
    sections.append("")
    sections.append(
        "KiX marketing claims **\"10K+ merchants\"**. The `breaking` profile "
        "ramps up to 50K total users (≈4.5K merchants at the 1:10 split) and "
        "beyond. The breaking-point section above reports the observed "
        "ceiling. Anything below ~10K merchants of headroom should be treated "
        "as marketing risk and addressed before the next external claim.")
    sections.append("")
    return "\n".join(sections) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="load_tests/results")
    ap.add_argument("--out", default="/Users/mozat/a-docs/load-test-report.md")
    args = ap.parse_args()
    results_dir = Path(args.results)
    results_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(results_dir)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(f"Wrote {out_path} ({len(report)} bytes)")


if __name__ == "__main__":
    main()
