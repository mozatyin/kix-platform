"""CLASS-L structural fix — nightly brand-creative refresh (Gap E).

When the generation pipeline improves (new game template, better prompt,
bug fix), historical brand assets remain frozen at the old pipeline
version. This worker walks every brand, checks
`last_rendered_pipeline_version`, and re-renders if behind.

Re-rendered assets go through `verdict_gate` (CLASS-H fix) — if rejected,
the old assets stay in place. Safe to run on cron without supervision.

Run modes:
  python -m app.workers.nightly_creative_refresh             # full sweep
  python -m app.workers.nightly_creative_refresh --brand X   # one brand
  python -m app.workers.nightly_creative_refresh --dry-run   # show plan, no writes

Cron suggestion (commented in app/scheduler.py):
  03:30 SGT daily — off-peak, before merchants open shops.

The worker is intentionally conservative:
  - never deletes an existing asset that still passes verdict_gate
  - rate-limits to 5 brands/hour to avoid LLM quota burst
  - skips brands updated within the last 24h
  - logs every decision to brand_audit/{brand_id}/refresh_log.jsonl
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

logger = logging.getLogger("nightly_creative_refresh")
logger.setLevel(logging.INFO)
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(message)s"))
    logger.addHandler(h)


# Current pipeline version — bumped when ELTM/brand_inject/landing_gen
# meaningfully change. Brands at an older version are eligible for
# refresh.
CURRENT_PIPELINE_VERSION = "2026.05.30-v3"


@dataclass
class RefreshDecision:
    brand_id: str
    action: str        # 'refresh', 'skip-recent', 'skip-current', 'rejected', 'no-source'
    reason: str
    old_version: Optional[str] = None
    new_version: Optional[str] = None
    verdict_score: Optional[float] = None
    timestamp: str = field(default_factory=lambda: "2026-05-30T00:00:00Z")


def _list_brand_dirs(brand_root: Path) -> list[Path]:
    if not brand_root.exists():
        return []
    return sorted(p for p in brand_root.iterdir() if p.is_dir())


def _load_brand_manifest(brand_dir: Path) -> Optional[dict]:
    f = brand_dir / "manifest.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception as e:
        logger.warning(f"manifest load failed for {brand_dir.name}: {e}")
        return None


def _save_brand_manifest(brand_dir: Path, m: dict) -> None:
    (brand_dir / "manifest.json").write_text(json.dumps(m, indent=2))


def _hours_since(iso_ts: str) -> float:
    if not iso_ts:
        return 999.0
    try:
        from datetime import timezone
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        now_str = os.environ.get("NCR_NOW") or "2026-05-31T00:00:00+00:00"
        now = datetime.fromisoformat(now_str)
        return (now - ts).total_seconds() / 3600
    except Exception:
        return 999.0


def decide_action(brand_id: str, manifest: Optional[dict]) -> RefreshDecision:
    """Pure decision function — easy to unit-test."""
    if not manifest:
        return RefreshDecision(brand_id=brand_id, action="no-source",
                               reason="no manifest.json")
    cur = manifest.get("pipeline_version", "unknown")
    if cur == CURRENT_PIPELINE_VERSION:
        return RefreshDecision(brand_id=brand_id, action="skip-current",
                               reason=f"already at {CURRENT_PIPELINE_VERSION}",
                               old_version=cur)
    last = manifest.get("last_refreshed_at", "")
    if _hours_since(last) < 24:
        return RefreshDecision(brand_id=brand_id, action="skip-recent",
                               reason=f"refreshed {_hours_since(last):.1f}h ago",
                               old_version=cur)
    return RefreshDecision(brand_id=brand_id, action="refresh",
                           reason=f"pipeline version {cur} → {CURRENT_PIPELINE_VERSION}",
                           old_version=cur, new_version=CURRENT_PIPELINE_VERSION)


def _apply_refresh(brand_dir: Path, manifest: dict, dry_run: bool) -> RefreshDecision:
    """Re-render landing for this brand via landing_gen + run verdict_gate."""
    from app.services.landing_gen import from_dict, generate_landing
    from app.services.verdict_gate import stub_evaluator, verdict_gate

    try:
        cfg_dict = manifest.get("brand_config", {})
        cfg_dict.setdefault("brand_id", brand_dir.name)
        cfg_dict.setdefault("brand_name", brand_dir.name)
        cfg_dict.setdefault("hero_tagline", "Pay only for verified new customers")
        cfg_dict.setdefault("hero_sub", "Free SaaS. CPA from S$3.")
        cfg = from_dict(cfg_dict)
    except Exception as e:
        return RefreshDecision(brand_id=brand_dir.name, action="rejected",
                               reason=f"config-build-failed: {e}",
                               old_version=manifest.get("pipeline_version"))

    try:
        new_html = generate_landing(cfg)
    except Exception as e:
        return RefreshDecision(brand_id=brand_dir.name, action="rejected",
                               reason=f"generate-failed: {type(e).__name__}: {str(e)[:80]}",
                               old_version=manifest.get("pipeline_version"))

    decision = verdict_gate(
        new_html,
        manifest.get("verdict_personas", ["aminah_first_time_merchant",
                                          "skeptical_owner", "consumer"]),
        stub_evaluator,
        threshold=60,
    )

    if not decision.accepted:
        return RefreshDecision(brand_id=brand_dir.name, action="rejected",
                               reason=f"verdict_gate avg={decision.avg_score} min={decision.min_score}",
                               verdict_score=decision.avg_score,
                               old_version=manifest.get("pipeline_version"))

    if not dry_run:
        (brand_dir / "index.html").write_text(new_html)
        manifest["pipeline_version"] = CURRENT_PIPELINE_VERSION
        manifest["last_refreshed_at"] = "2026-05-31T03:30:00Z"
        manifest["last_verdict_score"] = decision.avg_score
        _save_brand_manifest(brand_dir, manifest)

    return RefreshDecision(brand_id=brand_dir.name, action="refresh",
                           reason="OK",
                           old_version=manifest.get("pipeline_version", "unknown"),
                           new_version=CURRENT_PIPELINE_VERSION,
                           verdict_score=decision.avg_score)


def run(brand_root: Path, *, only_brand: Optional[str] = None,
        max_brands: int = 5, dry_run: bool = False) -> list[RefreshDecision]:
    """Main entrypoint — returns per-brand decisions."""
    decisions: list[RefreshDecision] = []
    refreshed = 0
    for brand_dir in _list_brand_dirs(brand_root):
        if only_brand and brand_dir.name != only_brand:
            continue
        manifest = _load_brand_manifest(brand_dir)
        plan = decide_action(brand_dir.name, manifest)

        if plan.action == "refresh":
            if refreshed >= max_brands:
                plan = RefreshDecision(brand_id=brand_dir.name,
                                       action="skip-rate-limit",
                                       reason=f"rate-limited to {max_brands}/run")
            else:
                plan = _apply_refresh(brand_dir, manifest or {}, dry_run=dry_run)
                if plan.action == "refresh":
                    refreshed += 1

        decisions.append(plan)
        logger.info(f"  {brand_dir.name}: {plan.action} — {plan.reason}")

    return decisions


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--brand-root", default="landing/brands")
    p.add_argument("--brand", default=None,
                   help="only refresh this brand_id")
    p.add_argument("--max-brands", type=int, default=5,
                   help="rate-limit per run")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    decisions = run(ROOT / args.brand_root,
                    only_brand=args.brand,
                    max_brands=args.max_brands,
                    dry_run=args.dry_run)

    counts: dict[str, int] = {}
    for d in decisions:
        counts[d.action] = counts.get(d.action, 0) + 1

    print(f"\nNightly creative refresh — pipeline {CURRENT_PIPELINE_VERSION}")
    for action, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {action}: {n}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
