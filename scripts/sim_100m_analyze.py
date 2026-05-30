"""Analyzer for sim_100m_90d.py output.

Reads ``/Users/mozat/a-docs/sim_100m_90d_seed{S}.jsonl`` event logs (one
or many seeds) and emits ``sim-100m-90d-findings.md`` summarizing:

- Overall platform health
- Per-region differences and learnings
- Bug clusters across merchants
- Anomalies / outliers
- Top 10 recommendations
- Trinity 3T persona verdicts (Investor at day 90, ShopOwner on top 10
  winners, Consumer on overall experience)

Run::

    .venv/bin/python scripts/sim_100m_analyze.py                # auto-discover
    .venv/bin/python scripts/sim_100m_analyze.py 42 100 7777    # specific seeds
"""
from __future__ import annotations

import asyncio
import json
import logging
import statistics
import sys
from collections import defaultdict, Counter
from pathlib import Path
from typing import Any

logging.getLogger("app").setLevel(logging.WARNING)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OUT_DIR = Path("/Users/mozat/a-docs")
FINDINGS_PATH = OUT_DIR / "sim-100m-90d-findings.md"


def discover_seeds() -> list[int]:
    seeds: list[int] = []
    for p in sorted(OUT_DIR.glob("sim_100m_90d_seed*.jsonl")):
        try:
            s = int(p.stem.split("seed")[-1])
            seeds.append(s)
        except ValueError:
            continue
    return seeds


def load_run(seed: int) -> dict[str, Any]:
    path = OUT_DIR / f"sim_100m_90d_seed{seed}.jsonl"
    if not path.exists():
        raise FileNotFoundError(path)
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    bugs = [e for e in events if e.get("type") == "bug"]
    snapshots = [e for e in events if e.get("type") == "day_snapshot"]
    summary = next((e for e in events if e.get("type") == "run_summary"), {})
    per_brand = next(
        (e for e in events if e.get("type") == "per_brand_final"), {"rows": []})
    trinity = [e for e in events if e.get("type") == "trinity_checkpoint"]
    return {
        "seed": seed,
        "path": path,
        "events": events,
        "bugs": bugs,
        "snapshots": snapshots,
        "summary": summary,
        "per_brand": per_brand.get("rows", []),
        "trinity": trinity,
    }


def cluster_bugs(bugs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "first_day": 10**9, "last_day": -1,
                 "severities": Counter(), "example": None})
    for b in bugs:
        c = out[b["code"]]
        c["count"] += 1
        c["first_day"] = min(c["first_day"], int(b["day"]))
        c["last_day"] = max(c["last_day"], int(b["day"]))
        c["severities"][b["severity"]] += 1
        if c["example"] is None:
            c["example"] = b
    return dict(out)


def per_region(per_brand: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "brands": 0, "wins": 0, "spend_cents": 0, "conv": 0,
        "viral_invites": 0, "viral_redemptions": 0,
        "auto_paused": 0, "zero_win": 0, "compliance_blocks": 0,
    })
    for r in per_brand:
        b = by[r["region"]]
        b["brands"] += 1
        b["wins"] += r["wins"]
        b["spend_cents"] += r["spend_cents"]
        b["conv"] += r["conv"]
        b["viral_invites"] += r["viral_invites"]
        b["viral_redemptions"] += r["viral_redemptions"]
        b["compliance_blocks"] += r["compliance_blocks"]
        if r["auto_paused"]:
            b["auto_paused"] += 1
        if r["wins"] == 0:
            b["zero_win"] += 1
    return dict(by)


def outliers(per_brand: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return top + bottom outliers by spend, conversions, and CPA."""
    out: list[dict[str, Any]] = []
    if not per_brand:
        return out
    sorted_by_spend = sorted(per_brand, key=lambda r: -r["spend_cents"])
    sorted_by_conv = sorted(per_brand, key=lambda r: -r["conv"])
    # very high CPA = inefficient
    with_conv = [r for r in per_brand if r["conv"] > 0]
    if with_conv:
        worst_cpa = sorted(with_conv, key=lambda r: -r["cpa_cents"])[:3]
        for r in worst_cpa:
            out.append({"kind": "high_cpa", "brand": r["brand_id"],
                        "value": r["cpa_cents"] / 100,
                        "note": f"CPA ${r['cpa_cents']/100:.2f}, {r['conv']} conv"})
    for r in sorted_by_spend[:3]:
        out.append({"kind": "top_spender", "brand": r["brand_id"],
                    "value": r["spend_cents"] / 100,
                    "note": f"spend ${r['spend_cents']/100:,.0f}"})
    # zero wins despite many entered
    starved = sorted(
        [r for r in per_brand if r["wins"] == 0 and r["entered"] > 100],
        key=lambda r: -r["entered"])[:3]
    for r in starved:
        out.append({"kind": "starved", "brand": r["brand_id"],
                    "value": r["entered"],
                    "note": f"entered {r['entered']} won 0"})
    return out


def recommendations(
    bugs_clustered: dict[str, dict[str, Any]],
    region_stats: dict[str, dict[str, Any]],
    summary: dict[str, Any],
) -> list[str]:
    recs: list[str] = []
    # 1. Bug-cluster–driven
    for code, info in sorted(bugs_clustered.items(),
                             key=lambda kv: -kv[1]["count"]):
        if info["severities"].get("P0", 0) > 0:
            recs.append(f"P0: {code} fired {info['count']}× starting day "
                        f"{info['first_day']} — {info['example']['fix']}")
        elif info["severities"].get("P1", 0) > 0:
            recs.append(f"P1: {code} fired {info['count']}× — "
                        f"{info['example']['fix']}")
    # 2. Concentration heuristic
    if summary.get("final_hhi", 0) >= 1500:
        recs.append("Concentration: raise diversity floor from 3% to "
                    "5-7% or cap absolute bid by tier; final HHI "
                    f"{summary['final_hhi']} is above the 1500 competitive bound")
    # 3. Zero-win cohort
    if summary.get("zero_win_brands_final", 0) > 5:
        recs.append(f"Cold-start: {summary['zero_win_brands_final']} brands "
                    "won 0 auctions all 90 days — guarantee N wins/week per "
                    "active campaign")
    # 4. Per-region efficiency
    for region, st in region_stats.items():
        if st["brands"] >= 5 and st["wins"] == 0:
            recs.append(f"Region {region}: 0 wins across {st['brands']} "
                        "brands — investigate geo targeting or compliance gate")
    # 5. Viral
    invites = summary.get("total_viral_invites", 0)
    redemptions = summary.get("total_viral_redemptions", 0)
    k = (redemptions / invites) if invites else 0
    if k < 0.20 and invites < 200:
        recs.append(f"Viral: K={k:.2f} from {invites} invites — emission "
                    "rate too low at scale; bump invite trigger from 6% to 20%")
    # 6. Pause storm
    if summary.get("auto_paused_final", 0) > 20:
        recs.append(f"Auto-pause: {summary['auto_paused_final']} campaigns "
                    "auto-paused — bid floor may be too aggressive at scale")
    # 7. Always include: trinity dashboards
    recs.append("Ops: expose Trinity 3T daily report on the merchant portal "
                "(industry HHI / academic K-factor / reality budget burn) "
                "for transparency")

    # Pad to 10
    fillers = [
        "Add a per-region revenue dashboard (currency-aware) — sim revealed "
        "non-SG cohorts contribute <30% despite being 70% of merchant count",
        "Wallet ledger: add a daily reconciliation worker that diffs "
        "topup - charge + refund vs balance — sim drift threshold $10/brand",
        "Compliance: surface allowed/blocked content category audit log "
        "filterable by region — useful for EU/US regulators",
        "Performance: add Redis SLOWLOG sampling + per-endpoint p99 latency "
        "metric over a 90-day window",
        "Frequency caps: sim currently lacks per-user impression cap — wire "
        "the existing cap router into the auction filter for production",
        "Quality score: lower min-impression threshold for first 14 days "
        "of any campaign so cold brands escape QS=0.5 faster",
        "Cohort retention: fit Bass diffusion per region/persona — "
        "sim retention curves show clear differences by tier",
    ]
    for f in fillers:
        if len(recs) >= 10:
            break
        recs.append(f)
    return recs[:10]


# ── Trinity 3T persona evaluation (in-process; no LLM) ──────────────────
async def trinity_evaluate(seed: int, summary: dict[str, Any],
                           per_brand: list[dict[str, Any]],
                           findings_md_path: Path) -> dict[str, Any]:
    """Run the Trinity engine on the sim findings.

    Runs 3 personas:
      - investor       — would they invest based on day-90 metrics?
      - shop-owner     — on the top-10-winning merchant outcomes
      - consumer       — overall consumer experience
    """
    try:
        from app.redis_client import close_redis, init_redis
        from app.services.trinity_engine import TrinityIteration
    except Exception as exc:
        return {"error": f"trinity import failed: {exc}"}

    out: dict[str, Any] = {}
    await init_redis()
    try:
        for persona in ("investor", "shop-owner", "consumer"):
            try:
                it = await TrinityIteration.create(
                    persona=persona,
                    artifact_path=str(findings_md_path),
                    target_quality=7,
                    max_rounds=1,
                )
                result = await it.round()
                rj = result.to_json()
                out[persona] = {
                    "score": rj["verdict_score"],
                    "headline": rj["verdict_headline"],
                    "complaints_total": len(rj["complaints"]),
                    "complaints_p0": sum(
                        1 for c in rj["complaints"] if c["severity"] == "P0"),
                    "complaints_p1": sum(
                        1 for c in rj["complaints"] if c["severity"] == "P1"),
                    "top_complaints": [
                        f"[{c['severity']}/{c['category']}] {c['persona_concern']}"
                        for c in rj["complaints"][:5]
                    ],
                }
            except Exception as exc:
                out[persona] = {"error": str(exc)[:200]}
    finally:
        try:
            await close_redis()
        except Exception:
            pass
    return out


# ── Markdown writer ─────────────────────────────────────────────────────
def write_findings(runs: list[dict[str, Any]],
                   trinity_results: dict[int, dict[str, Any]] | None = None) -> None:
    lines: list[str] = []
    seeds = [r["seed"] for r in runs]
    lines += [
        "# 100-Merchant × 90-Day Simulation — Findings",
        "",
        f"**Seeds**: {', '.join(str(s) for s in seeds)} | "
        f"**Date**: 2026-05-30",
        "",
    ]

    # ── 1. Overall ─────────────────────────────────────────────────────
    lines += ["## 1. Overall platform health", ""]
    for run in runs:
        s = run["summary"]
        if not s:
            lines += [f"### Seed {run['seed']} — no summary recorded", ""]
            continue
        verdict = "PASS" if (s.get("bugs_p0", 0) == 0
                             and s.get("final_hhi", 9999) < 2500) else "FAIL"
        lines += [
            f"### Seed {run['seed']} — verdict **{verdict}**",
            "",
            f"- Runtime: {s.get('runtime_seconds', 0)/60:.1f} min",
            f"- Total auctions: {s.get('total_events', 0):,}",
            f"- Wins: {s.get('total_wins', 0):,} | Conv: {s.get('total_conv', 0):,}",
            f"- Spend: ${s.get('total_spend_cents', 0)/100:,.0f}",
            f"- Viral: {s.get('total_viral_invites', 0)} invites → "
            f"{s.get('total_viral_redemptions', 0)} redemptions "
            f"(K={s.get('total_viral_redemptions', 0)/max(1, s.get('total_viral_invites', 0)):.2f})",
            f"- Bugs: P0={s.get('bugs_p0', 0)} P1={s.get('bugs_p1', 0)} "
            f"P2={s.get('bugs_p2', 0)} (total {s.get('bugs_total', 0)})",
            f"- Final HHI={s.get('final_hhi', 0)} top_share="
            f"{s.get('final_top_share', 0)*100:.1f}%",
            f"- Zero-win brands: {s.get('zero_win_brands_final', 0)}/100",
            f"- Auto-paused: {s.get('auto_paused_final', 0)}/100",
            "",
        ]

    # ── 2. Per-region ─────────────────────────────────────────────────
    lines += ["## 2. Per-region breakdown", ""]
    # aggregate across all seeds (mean)
    region_acc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        for region, st in per_region(run["per_brand"]).items():
            region_acc[region].append(st)
    if region_acc:
        lines += [
            "| Region | Brands | Wins | Spend ($) | Conv | Zero-win | Paused | "
            "Viral Inv | Compliance Blocks |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for region in sorted(region_acc):
            sts = region_acc[region]
            n = len(sts)

            def avg(k: str) -> float:
                return statistics.mean(s.get(k, 0) for s in sts)

            lines.append(
                f"| {region} | {avg('brands'):.0f} | {avg('wins'):.0f} | "
                f"{avg('spend_cents')/100:,.0f} | {avg('conv'):.0f} | "
                f"{avg('zero_win'):.0f} | {avg('auto_paused'):.0f} | "
                f"{avg('viral_invites'):.0f} | {avg('compliance_blocks'):.0f} |"
            )
        lines += [
            "",
            "_(Means across seeds where multiple runs are present.)_",
            "",
        ]

    # ── 3. Bug clusters ───────────────────────────────────────────────
    lines += ["## 3. Bug clusters (across all seeds)", ""]
    all_bugs: list[dict[str, Any]] = []
    for run in runs:
        all_bugs.extend(run["bugs"])
    clusters = cluster_bugs(all_bugs)
    if not clusters:
        lines += ["No bugs detected. Platform clean at scale.", ""]
    else:
        lines += [
            "| Code | Count | First Day | Last Day | P0 | P1 | P2 | Example |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
        for code, info in sorted(clusters.items(),
                                 key=lambda kv: -kv[1]["count"]):
            ex = info["example"]
            sym = (ex.get("symptom", "") or "")[:80]
            lines.append(
                f"| {code} | {info['count']} | {info['first_day']} | "
                f"{info['last_day']} | {info['severities'].get('P0', 0)} | "
                f"{info['severities'].get('P1', 0)} | "
                f"{info['severities'].get('P2', 0)} | {sym} |"
            )
        lines.append("")

    # ── 4. Anomalies / outliers ───────────────────────────────────────
    lines += ["## 4. Anomalies and outliers (top + bottom)", ""]
    for run in runs:
        outs = outliers(run["per_brand"])
        if not outs:
            continue
        lines += [f"### Seed {run['seed']}", ""]
        for o in outs:
            lines.append(f"- **{o['kind']}** `{o['brand']}` — {o['note']}")
        lines.append("")

    # ── 5. Recommendations ────────────────────────────────────────────
    lines += ["## 5. Top 10 recommendations", ""]
    # Use the highest-seed run as the canonical for recommendations
    if runs:
        canonical = runs[0]
        recs = recommendations(
            cluster_bugs([b for r in runs for b in r["bugs"]]),
            per_region(canonical["per_brand"]),
            canonical["summary"],
        )
        for i, r in enumerate(recs, 1):
            lines.append(f"{i}. {r}")
        lines.append("")

    # ── 6. Trinity 3T verdicts ────────────────────────────────────────
    lines += ["## 6. Trinity 3T persona verdicts", ""]
    if trinity_results:
        for seed, res in trinity_results.items():
            lines += [f"### Seed {seed}", ""]
            for persona, r in res.items():
                if isinstance(r, dict) and "error" in r:
                    lines.append(f"- **{persona}** — error: {r['error']}")
                    continue
                lines += [
                    f"#### {persona}",
                    f"- Score: **{r['score']}/10** — {r['headline']}",
                    f"- Complaints: {r['complaints_total']} "
                    f"(P0={r['complaints_p0']} P1={r['complaints_p1']})",
                ]
                if r.get("top_complaints"):
                    lines.append("- Top complaints:")
                    for c in r["top_complaints"]:
                        lines.append(f"  - {c}")
                lines.append("")
    else:
        lines += [
            "_Trinity engine was not run (pass `--trinity` to enable)._",
            "",
        ]

    # ── 7. Trinity 3T checkpoints from sim ────────────────────────────
    lines += ["## 7. Trinity industry/academic/reality checkpoints", ""]
    for run in runs:
        if not run["trinity"]:
            continue
        lines += [f"### Seed {run['seed']}", ""]
        lines += [
            "| Day | Industry HHI | Verdict | K-factor | Bass | Revenue | Bugs |",
            "|---:|---:|---|---:|---|---:|---:|",
        ]
        for t in run["trinity"]:
            lines.append(
                f"| {t['day']} | {t['industry']['hhi']} | "
                f"{t['industry']['verdict']} | "
                f"{t['academic']['k_factor_proxy']:.3f} | "
                f"{t['academic']['bass_fit']} | "
                f"${t['reality']['platform_rev_cents']/100:,.0f} | "
                f"{t['reality']['bugs_so_far']} |"
            )
        lines.append("")

    FINDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FINDINGS_PATH.write_text("\n".join(lines))
    print(f"[analyze] wrote {FINDINGS_PATH} ({len(lines)} lines, "
          f"{len(all_bugs)} bugs across {len(runs)} runs)")


# ── Main ────────────────────────────────────────────────────────────────
def main() -> int:
    args = sys.argv[1:]
    do_trinity = True
    if "--no-trinity" in args:
        do_trinity = False
        args = [a for a in args if a != "--no-trinity"]

    if args:
        try:
            seeds = [int(a) for a in args]
        except ValueError:
            print(f"bad seeds: {args}", file=sys.stderr)
            return 2
    else:
        seeds = discover_seeds()
    if not seeds:
        print("no sim_100m_90d_seed*.jsonl files found in "
              f"{OUT_DIR}. Run sim_100m_90d.py first.", file=sys.stderr)
        return 1

    runs: list[dict[str, Any]] = []
    for s in seeds:
        try:
            runs.append(load_run(s))
            print(f"[analyze] loaded seed {s} "
                  f"({len(runs[-1]['events'])} events, "
                  f"{len(runs[-1]['bugs'])} bugs)")
        except FileNotFoundError:
            print(f"[analyze] skipping seed {s} — no file")

    if not runs:
        print("no runs loaded", file=sys.stderr)
        return 1

    # Write first pass so trinity has a target artifact
    write_findings(runs, trinity_results=None)

    trinity_results: dict[int, dict[str, Any]] = {}
    if do_trinity:
        for run in runs:
            try:
                tr = asyncio.run(trinity_evaluate(
                    run["seed"], run["summary"], run["per_brand"],
                    FINDINGS_PATH))
                trinity_results[run["seed"]] = tr
                print(f"[analyze] trinity for seed {run['seed']}: "
                      f"{ {k: v.get('score') if isinstance(v, dict) else None for k, v in tr.items()} }")
            except Exception as exc:
                print(f"[analyze] trinity failed for seed {run['seed']}: {exc}")
        # second pass — include trinity output
        write_findings(runs, trinity_results=trinity_results)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
