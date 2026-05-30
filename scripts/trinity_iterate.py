"""CLI wrapper for the Trinity 3T iteration engine.

Usage
-----
.. code-block:: bash

    python -m scripts.trinity_iterate \
        --persona shop-owner \
        --artifact landing/portal.html \
        --max-rounds 5 \
        --target 7

    python -m scripts.trinity_iterate --list-personas

What it produces
----------------
* Round-by-round complaint dump to stdout (human-friendly summary).
* Full JSON artifact per round saved under
  ``/Users/mozat/a-docs/trinity-runs/{iteration_id}/round-{n}.json``.
* ``summary.json`` with the final verdict.

The script is a thin shell — all real logic lives in
``app.services.trinity_engine`` so the engine can be driven equally from
the REST endpoints in ``app.routers.trinity_admin`` or from CI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

# Ensure the project root is importable when called as `python scripts/...`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.redis_client import close_redis, init_redis  # noqa: E402
from app.services.trinity_engine import (  # noqa: E402
    PERSONA_REGISTRY,
    TrinityIteration,
)

OUTPUT_ROOT = Path("/Users/mozat/a-docs/trinity-runs")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run a Trinity 3T iteration cycle on an artifact",
    )
    p.add_argument("--persona", default="shop-owner",
                   help="persona slug (see --list-personas)")
    p.add_argument("--artifact", default="",
                   help="path to artifact (HTML, JS, MD, or directory)")
    p.add_argument("--max-rounds", type=int, default=5)
    p.add_argument("--target", type=int, default=7,
                   help="target persona quality score 0-10")
    p.add_argument("--list-personas", action="store_true",
                   help="print registered personas and exit")
    p.add_argument("--quiet", action="store_true", help="suppress per-round summary")
    return p.parse_args()


def _print_round_summary(result_json: dict) -> None:
    print(f"\n── Round {result_json['round_number']} "
          f"({result_json['persona_slug']}) ──")
    print(f"  score          : {result_json['verdict_score']}/10")
    print(f"  verdict        : {result_json['verdict_headline']}")
    print(f"  total complaints: {len(result_json['complaints'])}")
    print(f"  new this round : {result_json['new_complaint_count']}")
    p0 = [c for c in result_json["complaints"] if c["severity"] == "P0"]
    p1 = [c for c in result_json["complaints"] if c["severity"] == "P1"]
    print(f"  P0 / P1 / P2   : {len(p0)} / {len(p1)} / "
          f"{len(result_json['complaints']) - len(p0) - len(p1)}")
    if p0:
        print("  top P0:")
        for c in p0[:3]:
            print(f"    - [{c['category']}] {c['persona_concern']}")
            print(f"        expected: {c['expected']}")
            print(f"        got     : {c['got']}")


def _save_run_artifacts(iteration_id: str, results: list[dict],
                        verdict: dict) -> Path:
    out_dir = OUTPUT_ROOT / iteration_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for r in results:
        (out_dir / f"round-{r['round_number']}.json").write_text(
            json.dumps(r, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    (out_dir / "summary.json").write_text(
        json.dumps(verdict, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return out_dir


async def _run(args: argparse.Namespace) -> int:
    if args.list_personas:
        for slug, factory in PERSONA_REGISTRY.items():
            p = factory()
            print(f"  {slug:18s} — {p.label}")
        return 0

    if not args.artifact:
        print("error: --artifact is required (or use --list-personas)", file=sys.stderr)
        return 2

    await init_redis()
    try:
        it = await TrinityIteration.create(
            persona=args.persona,
            artifact_path=args.artifact,
            target_quality=args.target,
            max_rounds=args.max_rounds,
        )
        print(f"trinity: started iteration {it.iteration_id}")
        print(f"  persona  : {it.persona.slug} ({it.persona.label})")
        print(f"  artifact : {it.artifact_path}")
        print(f"  target   : {it.target_quality}/10")
        print(f"  max rnds : {it.max_rounds}")

        results: list[dict] = []
        while not await it.has_converged():
            t0 = time.time()
            result = await it.round()
            elapsed = time.time() - t0
            rj = result.to_json()
            rj["elapsed_seconds"] = round(elapsed, 3)
            results.append(rj)
            if not args.quiet:
                _print_round_summary(rj)

        verdict = await it.final_verdict()
        out_dir = _save_run_artifacts(it.iteration_id, results, verdict)
        print(f"\ntrinity: converged after {verdict['rounds_executed']} rounds")
        print(f"  final score  : {verdict['final_score']}/10 — {verdict['final_headline']}")
        print(f"  P0 remaining : {verdict['p0_remaining']}")
        print(f"  P1 remaining : {verdict['p1_remaining']}")
        print(f"  artifacts    : {out_dir}")
        return 0
    finally:
        await close_redis()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
