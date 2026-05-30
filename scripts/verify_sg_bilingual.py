"""SG bilingual integration verifier — Wave 2 deliverable.

Hits a representative slice of the live FastAPI app with both
``Accept-Language: en-SG`` and ``Accept-Language: zh-Hans-SG`` and confirms
per-locale invariants:

  * Response is reachable (any 2xx/3xx/4xx ≠ 500).
  * Content-Language header echoes the requested locale (when middleware
    is mounted).
  * ``/api/v1/i18n/translate`` returns different rendered text for the
    same key across the two locales (i.e. the catalog actually has both
    sides populated).
  * No Chinese glyph leaks into the en-SG response body for known
    translated keys.
  * No raw English template leaks into the zh-Hans-SG response body for
    the same keys.

Pass/fail per endpoint is printed; exit code is the count of failures.

Run::

    .venv/bin/python -m scripts.verify_sg_bilingual
    .venv/bin/python -m scripts.verify_sg_bilingual --verbose
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

CJK_RE = re.compile(r"[一-鿿]")

# Probe matrix: (path, params). Use translate-debug + format-debug + preview
# rather than mutating endpoints — verifier must be safe to run anywhere.
PROBES: list[tuple[str, dict[str, str]]] = [
    # tutorials module names (47 keys total — sample the load-bearing ones)
    ("/api/v1/i18n/translate", {"key": "tutorials-module-progression"}),
    ("/api/v1/i18n/translate", {"key": "tutorials-module-currency"}),
    ("/api/v1/i18n/translate", {"key": "tutorials-module-energy"}),
    ("/api/v1/i18n/translate", {"key": "tutorials-module-voucher"}),
    ("/api/v1/i18n/translate", {"key": "tutorials-module-tourney"}),
    ("/api/v1/i18n/translate", {"key": "tutorials-module-streak"}),
    # tutorials step templates
    ("/api/v1/i18n/translate", {"key": "tutorials-step-navigate-engagement"}),
    ("/api/v1/i18n/translate", {"key": "tutorials-step-navigate-vouchers"}),
    ("/api/v1/i18n/translate", {"key": "tutorials-step-navigate-rules"}),
    # conditions blockers
    ("/api/v1/i18n/translate", {"key": "conditions-blocker-supply_exhausted"}),
    ("/api/v1/i18n/translate", {"key": "conditions-blocker-budget_exhausted"}),
    ("/api/v1/i18n/translate", {"key": "conditions-blocker-tier_required"}),
    ("/api/v1/i18n/translate", {"key": "conditions-blocker-frequency_per_user_per_day"}),
    ("/api/v1/i18n/translate", {"key": "conditions-blocker-time_already_ended"}),
    ("/api/v1/i18n/translate", {"key": "conditions-blocker-reservation_expired"}),
    # welcome_kit items
    ("/api/v1/i18n/translate", {"key": "welcome_kit-item-table_stand-title"}),
    ("/api/v1/i18n/translate", {"key": "welcome_kit-item-table_stand-desc"}),
    ("/api/v1/i18n/translate", {"key": "welcome_kit-item-counter_standing-title"}),
    ("/api/v1/i18n/translate", {"key": "welcome_kit-item-door_sticker-title"}),
    ("/api/v1/i18n/translate", {"key": "welcome_kit-item-social_poster-title"}),
    # recipe_generator
    ("/api/v1/i18n/translate", {"key": "recipe_generator-heuristic-fallback"}),
    ("/api/v1/i18n/translate", {"key": "recipe_generator-summary-untitled"}),
    ("/api/v1/i18n/translate", {"key": "recipe_generator-summary-empty-modules"}),
    # common UI
    ("/api/v1/i18n/translate", {"key": "common-cta-login"}),
    ("/api/v1/i18n/translate", {"key": "common-cta-logout"}),
    ("/api/v1/i18n/translate", {"key": "common-cta-cancel"}),
    ("/api/v1/i18n/translate", {"key": "common-cta-save"}),
    # error codes
    ("/api/v1/i18n/translate", {"key": "error-not_found"}),
    ("/api/v1/i18n/translate", {"key": "error-unauthorized"}),
    ("/api/v1/i18n/translate", {"key": "error-rate_limited"}),
]


def _verify_one(
    client, path: str, params: dict[str, str], verbose: bool
) -> dict[str, Any]:
    """Hit ``path`` with both locales and confirm the bilingual invariants."""
    result: dict[str, Any] = {"path": path, "params": params, "ok": True, "issues": []}

    # en-SG hit
    r_en = client.get(
        path, params=params, headers={"Accept-Language": "en-SG"}
    )
    # zh-Hans-SG hit
    r_zh = client.get(
        path, params=params, headers={"Accept-Language": "zh-Hans-SG"}
    )

    if r_en.status_code >= 500:
        result["ok"] = False
        result["issues"].append(f"en-SG 5xx: {r_en.status_code}")
        return result
    if r_zh.status_code >= 500:
        result["ok"] = False
        result["issues"].append(f"zh-Hans-SG 5xx: {r_zh.status_code}")
        return result

    # Content-Language header check
    if r_en.headers.get("Content-Language") != "en-SG":
        result["issues"].append(
            f"en-SG Content-Language={r_en.headers.get('Content-Language')!r}"
        )
        result["ok"] = False
    if r_zh.headers.get("Content-Language") != "zh-Hans-SG":
        result["issues"].append(
            f"zh-Hans-SG Content-Language={r_zh.headers.get('Content-Language')!r}"
        )
        result["ok"] = False

    # JSON body checks (the translate endpoint returns ``{rendered, ...}``)
    body_en = r_en.json() if r_en.headers.get("content-type", "").startswith(
        "application/json"
    ) else {}
    body_zh = r_zh.json() if r_zh.headers.get("content-type", "").startswith(
        "application/json"
    ) else {}

    rendered_en = body_en.get("rendered", "")
    rendered_zh = body_zh.get("rendered", "")
    result["en"] = rendered_en
    result["zh"] = rendered_zh

    # 1. Both translated (rendered != key)
    if rendered_en == params.get("key"):
        result["issues"].append("en-SG verbatim key (catalog miss)")
        result["ok"] = False
    if rendered_zh == params.get("key"):
        result["issues"].append("zh-Hans-SG verbatim key (catalog miss)")
        result["ok"] = False

    # 2. Bodies differ
    if rendered_en and rendered_zh and rendered_en == rendered_zh:
        result["issues"].append("same string in both locales (suspect)")
        result["ok"] = False

    # 3. No Chinese leak in en-SG
    if CJK_RE.search(rendered_en):
        result["issues"].append(f"Chinese leaked into en-SG: {rendered_en!r}")
        result["ok"] = False

    # 4. zh-Hans-SG should contain Chinese
    if rendered_zh and not CJK_RE.search(rendered_zh):
        result["issues"].append(
            f"zh-Hans-SG has no Chinese: {rendered_zh!r}"
        )
        result["ok"] = False

    if verbose:
        print(f"  en: {rendered_en!r}")
        print(f"  zh: {rendered_zh!r}")

    return result


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--region", default="sg", help="KIX_REGION for the test app")
    args = p.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    # Use the ASGI test client — no external server needed.
    os.environ["KIX_REGION"] = args.region

    # Force region module reload so the SG region default propagates.
    import importlib
    import app.region
    importlib.reload(app.region)

    from fastapi.testclient import TestClient
    from app.main import create_app

    client = TestClient(create_app())

    print(f"=== SG bilingual verifier (KIX_REGION={args.region}) ===")
    print(f"Probing {len(PROBES)} endpoints...")
    print()

    results: list[dict[str, Any]] = []
    for path, params in PROBES:
        if args.verbose:
            print(f"[probe] {path} {params}")
        r = _verify_one(client, path, params, args.verbose)
        results.append(r)
        status = "PASS" if r["ok"] else "FAIL"
        marker = "·" if r["ok"] else "✗"
        key = params.get("key", "—")
        print(f"  {marker} {status:4s} {key}")
        if not r["ok"]:
            for issue in r["issues"]:
                print(f"      └ {issue}")

    pass_n = sum(1 for r in results if r["ok"])
    fail_n = len(results) - pass_n
    print()
    print(f"Result: {pass_n}/{len(results)} passed, {fail_n} failed")

    if args.verbose:
        sample_lines = [
            f"{r['params'].get('key')}: en={r.get('en')!r} zh={r.get('zh')!r}"
            for r in results[:5] if r["ok"]
        ]
        print()
        print("Sample translations:")
        for line in sample_lines:
            print("  " + line)

    return fail_n


if __name__ == "__main__":
    sys.exit(main())
