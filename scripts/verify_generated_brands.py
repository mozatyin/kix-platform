"""Persona verdict-gate verification on the 3 generated brand landings.

Loads landing/brands/{default,heng_heng_kopi,aminah_halal}/index.html
in a real browser (Playwright), extracts visible text, then runs 4
personas in PARALLEL via OpenRouter (per founder OpenRouter-parallel rule):
  - aminah_first_time_merchant (Tampines halal stall owner)
  - skeptical_owner (Sarah, café owner, low trust)
  - ahmad_kopi_chain (Malaysian chain CEO)
  - consumer (Ben Tan, end-customer)

Each persona scores 0-100; aggregated by app.services.verdict_gate.
"""
from __future__ import annotations
import asyncio, json, sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services.verdict_gate import GateDecision, VerdictScore, verdict_gate
from app.services.persona_registry import (
    PERSONAS as REGISTRY_PERSONAS, for_page_ids,
)
from sim_users_deepseek import load_openrouter_key
from sim_users_v2 import call_llm

PORT = 8765

from scripts.generate_landing_sites import BRANDS as BRAND_CONFIGS

BRANDS = [
    (bid, f"{cfg.brand_name} ({cfg.audience})")
    for bid, cfg in BRAND_CONFIGS.items()
]


# C · persona_registry is the single source of truth — used to live in
# 3 places (PERSONAS, PERSONA_PROFILES, PERSONA_AXES). Now centralized
# in app/services/persona_registry.py.
def personas_for(audience: str, scale: str = "single") -> list[str]:
    return for_page_ids(audience, scale)


# PERSONA_PROFILES removed — persona_registry is the source of truth.
def _profile(pid: str) -> dict:
    p = REGISTRY_PERSONAS[pid]
    return {"name": p.name, "role": p.role, "context": p.context}


async def render_page_text(url: str) -> str:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)
            body = await page.evaluate("() => document.body ? document.body.innerText : ''")
            title = await page.title()
            widgets = await page.evaluate("""
                () => {
                  const items = [];
                  document.querySelectorAll('button, a[href]').forEach(el => {
                    const t = (el.innerText || '').trim();
                    if (t && t.length < 80) items.push(t);
                  });
                  return [...new Set(items)].slice(0, 40).join(' | ');
                }
            """)
            await browser.close()
            return f"TITLE: {title}\nNAV/BUTTONS: {widgets}\n\nVISIBLE TEXT:\n{body}"
        except Exception as e:
            await browser.close()
            return f"[render-failed: {e}]"


def persona_critique(key: str, persona_id: str, brand_label: str, page_text: str) -> VerdictScore:
    profile = _profile(persona_id)
    system = (
        f"You are {profile['name']}, {profile['role']}\n\n"
        f"Context: {profile['context']}\n\n"
        "You are reviewing a KiX landing page as if you were genuinely "
        "considering signing up (or, for Ben, scanning the QR). "
        "Return ONLY a JSON object: "
        '{"score": 0-100, "verdict": "1-2 sentence verbatim verdict", '
        '"reasons": ["specific issue 1", "specific issue 2", ...], '
        '"would_recommend": true/false}'
    )
    user = (
        f"LANDING PAGE: {brand_label}\n\n"
        f"RENDERED CONTENT (first 6000 chars):\n```\n{page_text[:6000]}\n```\n\n"
        "Score honestly. 0 = won't engage. 100 = best landing you've seen in your sector."
    )
    out = call_llm(key, system, user, max_tokens=1800, temp=0.4)
    if not out["ok"]:
        return VerdictScore(persona_id=persona_id, score=0,
                            verdict_text="[llm-failed]", reasons=["llm-call failed"])
    text = out["text"].strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        data = json.loads(text)
        return VerdictScore(
            persona_id=persona_id,
            score=float(data.get("score", 0)),
            verdict_text=str(data.get("verdict", ""))[:400],
            reasons=list(data.get("reasons", []))[:6],
            would_recommend=bool(data.get("would_recommend", False)),
        )
    except Exception:
        # Regex fallback for truncated JSON (Sonnet sometimes cuts off mid-string)
        import re
        score_m = re.search(r'"score"\s*:\s*(\d+(?:\.\d+)?)', text)
        verdict_m = re.search(r'"verdict"\s*:\s*"([^"\\]+(?:\\.[^"\\]*)*)"', text)
        reasons_m = re.findall(r'"([^"\\]{20,200})"', text)
        if score_m:
            return VerdictScore(
                persona_id=persona_id, score=float(score_m.group(1)),
                verdict_text=(verdict_m.group(1)[:400] if verdict_m else "[regex-recovered]"),
                reasons=reasons_m[1:6],
                would_recommend=float(score_m.group(1)) >= 60,
            )
        return VerdictScore(persona_id=persona_id, score=0,
                            verdict_text="[parse-failed]",
                            reasons=[f"non-JSON: {text[:120]}"])


async def verify_brand(key: str, brand_id: str, brand_label: str):
    url = f"http://localhost:{PORT}/landing/brands/{brand_id}/index.html"
    cfg = BRAND_CONFIGS[brand_id]
    pids = personas_for(cfg.audience, cfg.scale)
    if not pids:
        print(f"\n[skip {brand_id}: no personas match audience={cfg.audience} scale={cfg.scale}]")
        return
    print(f"\n{'='*70}\n  {brand_label} · scale={cfg.scale}\n  URL: {url}\n  Personas: {pids}\n{'='*70}")
    text = await render_page_text(url)
    if text.startswith("[render-failed"):
        print(f"  ✗ {text}")
        return

    # Parallel persona critique (OpenRouter rule)
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=max(2, len(pids))) as pool:
        scores = await asyncio.gather(*[
            loop.run_in_executor(pool, persona_critique, key, pid, brand_label, text)
            for pid in pids
        ])

    # Aggregate via verdict_gate (with the real scores as a custom evaluator)
    def eval_lookup(_html, pid):
        return next(s for s in scores if s.persona_id == pid)

    # D · per-page threshold override. cfg.verdict_threshold=0 means inherit
    # default 65; non-zero overrides. Same for min_floor.
    threshold = cfg.verdict_threshold or 65
    min_floor = cfg.verdict_min_floor or 40
    decision = verdict_gate(text, pids, eval_lookup,
                            threshold=threshold, min_score_floor=min_floor)
    print(f"  (gate · threshold={threshold} min_floor={min_floor})")

    for s in scores:
        mark = "✓" if s.score >= 65 else "△" if s.score >= 40 else "✗"
        print(f"\n  {mark} {s.persona_id:35s} {s.score:5.1f}/100")
        print(f"     verdict: {s.verdict_text}")
        if s.reasons:
            print(f"     reasons:")
            for r in s.reasons[:4]:
                print(f"       - {r}")

    print(f"\n  AGGREGATE: avg={decision.avg_score} min={decision.min_score} "
          f"→ {'ACCEPT ✅' if decision.accepted else 'REJECT ✗'}")
    if not decision.accepted:
        print(f"  Top rejection reasons: {decision.rejection_reasons[:5]}")


async def main():
    key = load_openrouter_key()
    if not key:
        print("ERROR: OPENROUTER_API_KEY not found.", file=sys.stderr)
        return 1
    print(f"Verifying {len(BRANDS)} generated brand landings · "
          f"audience-matched personas · threshold=65 min_floor=40")
    for brand_id, label in BRANDS:
        await verify_brand(key, brand_id, label)
    print(f"\n{'='*70}\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
