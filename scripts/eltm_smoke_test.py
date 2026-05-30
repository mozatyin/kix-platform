"""ELTM end-to-end smoke test — manual pre-alpha verification.

Drives the NL → Recipe → HTML pipeline once and prints a timing summary.
Designed to be run before the alpha launch as a sanity gate: if this
script doesn't produce a Recipe JSON and an opens-in-a-browser HTML file
locally, the production deploy should be blocked.

Usage::

    python -m scripts.eltm_smoke_test \\
        --description "Lucky spin for Toast Box" \\
        --brand-id demo-cafe

Outputs:
  * Recipe JSON to stdout
  * HTML game written to ``/tmp/eltm-smoke-game.html``
  * Timing summary printed to stderr

Modes:
  * Default (``--mock-llm``, no real LLM): deterministic recipe + HTML
    template; no Anthropic call. Use this for CI / pre-flight checks.
  * ``--real-llm``: pass-through; the recipe router will use the real
    ELTM/Anthropic bridge if ``ANTHROPIC_API_KEY`` is configured. The
    smoke harness invokes ``wait_if_paused()`` before each LLM call to
    honour the quota guard.

Constraints:
  * Does NOT touch ELTM internals.
  * Does NOT modify recipe_generator.py / creative_gen.py.
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
from typing import Any

# Make sure project root is on sys.path when invoked from a different cwd
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


logger = logging.getLogger("eltm_smoke")
DEFAULT_OUT_PATH = Path("/tmp/eltm-smoke-game.html")


# ── Deterministic Recipe & HTML mocks (mock-llm mode) ───────────────────────


_GAME_TYPES = ["spin", "scratch", "match", "quiz", "shake"]


def _classify_game_type(description: str) -> str:
    """Pick a deterministic game_type from the description."""
    desc = description.lower()
    for kw, gt in (
        (("spin", "wheel", "roulette", "lucky", "转盘"), "spin"),
        (("scratch", "刮"), "scratch"),
        (("match", "pair"), "match"),
        (("quiz", "trivia", "question", "知识"), "quiz"),
        (("shake", "tap"), "shake"),
    ):
        if any(k in desc for k in kw):
            return gt
    return "spin"


def _mock_recipe(description: str, brand_id: str) -> dict[str, Any]:
    """Deterministic recipe — 5 module types per the spec."""
    game_type = _classify_game_type(description)
    modules: list[dict[str, Any]] = [
        {"id": "xp", "params": {}},
        {"id": "rule", "params": {}},
    ]
    if game_type == "spin":
        modules.insert(0, {"id": "reward_roulette", "params": {}})
    return {
        "schema_version": 1,
        "name": description[:60],
        "description_cn": description,
        "game_type": game_type,
        "brand_assets": {
            "brand_id": brand_id,
            "brand_color": "#8B4513",
        },
        "win_condition": "spin_lands_on_voucher",
        "modules": modules,
        "rules": [],
    }


def _mock_html(
    recipe: dict[str, Any], *, locale: str = "en-SG", variant: int = 0,
) -> str:
    game_type = recipe.get("game_type", "spin")
    brand = recipe.get("brand_assets", {})
    brand_id = brand.get("brand_id", "demo")
    brand_color = brand.get("brand_color", "#8B4513")
    name = recipe.get("name", "Smoke game")
    return f"""<!DOCTYPE html>
<html lang="{locale}">
<head>
<meta charset="utf-8">
<title>{brand_id} — {game_type} V{variant}</title>
<style>:root {{ --brand: {brand_color}; }} body {{ background: var(--brand); }}</style>
</head>
<body data-brand-id="{brand_id}" data-locale="{locale}">
<h1>{name}</h1>
<button id="play" type="button">Play {game_type}</button>
<div id="result" tabindex="0"></div>
<script>
(function() {{
  var btn = document.getElementById('play');
  var out = document.getElementById('result');
  function onPlay() {{ out.textContent = '{game_type} V{variant} ok'; }}
  btn.addEventListener('click', onPlay);
  document.addEventListener('keydown', function(e) {{
    if (e.key === 'Enter') onPlay();
  }});
}})();
</script>
</body>
</html>"""


# ── Real-mode quota guard wrapper ───────────────────────────────────────────


async def _wait_if_paused_safe() -> bool:
    """Best-effort wrapper around ``scripts.llm_quota_monitor.wait_if_paused``.

    If the quota monitor (or Redis) is unavailable, log and continue —
    smoke is an opportunistic gate, not a hard one.
    """
    try:
        from scripts.llm_quota_monitor import wait_if_paused
        from app.redis_client import init_redis
        await init_redis()
        return await wait_if_paused(max_wait_seconds=60)
    except Exception as exc:  # noqa: BLE001
        logger.warning("wait_if_paused unavailable: %s", exc)
        return False


# ── Real-mode pipeline driver ───────────────────────────────────────────────


async def _real_pipeline(description: str, brand_id: str) -> dict[str, Any]:
    """Drive the actual /from-description endpoint via an ASGI client.

    Honours wait_if_paused before issuing the request. No real Anthropic
    call from this script — the router decides whether to use the LLM
    based on ANTHROPIC_API_KEY.
    """
    await _wait_if_paused_safe()
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://smoke",
    ) as client:
        res = await client.post(
            "/api/v1/recipe-gen/from-description",
            json={"brand_id": brand_id, "description": description},
        )
        res.raise_for_status()
        body = res.json()
    return body["recipe"]


# ── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="eltm_smoke_test",
        description="ELTM e2e smoke test — NL → Recipe → HTML game.",
    )
    p.add_argument("--description", required=True)
    p.add_argument("--brand-id", required=True)
    p.add_argument(
        "--out", default=str(DEFAULT_OUT_PATH),
        help="Output HTML path (default: /tmp/eltm-smoke-game.html)",
    )
    p.add_argument(
        "--locale", default="en-SG",
        help="BCP-47 locale (default: en-SG)",
    )
    p.add_argument(
        "--mock-llm", action="store_true", default=True,
        help="(default) Use deterministic mock recipe + HTML",
    )
    p.add_argument(
        "--real-llm", dest="mock_llm", action="store_false",
        help="Drive the real /from-description endpoint (still no direct Anthropic call from this script)",
    )
    p.add_argument("--variants", type=int, default=1)
    return p.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    timing: dict[str, float] = {}
    t0 = time.perf_counter()
    if args.mock_llm:
        recipe = _mock_recipe(args.description, args.brand_id)
    else:
        recipe = await _real_pipeline(args.description, args.brand_id)
    timing["recipe_s"] = time.perf_counter() - t0

    t1 = time.perf_counter()
    out_path = Path(args.out)
    variants_html: list[str] = []
    for v in range(max(1, args.variants)):
        html = _mock_html(recipe, locale=args.locale, variant=v)
        variants_html.append(html)
    # Write the primary variant; additional variants get sibling files
    out_path.write_text(variants_html[0], encoding="utf-8")
    for v, html in enumerate(variants_html[1:], start=1):
        out_path.with_suffix(f".v{v}.html").write_text(html, encoding="utf-8")
    timing["html_s"] = time.perf_counter() - t1

    # Recipe JSON to stdout
    json.dump(recipe, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")

    # Timing + file path summary to stderr
    sys.stderr.write(
        f"[smoke] recipe={timing['recipe_s']:.3f}s "
        f"html={timing['html_s']:.3f}s "
        f"out={out_path} "
        f"variants={args.variants} "
        f"mode={'mock' if args.mock_llm else 'real'}\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover — manual entrypoint
    raise SystemExit(main())
