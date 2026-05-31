"""User-sim v2 — Playwright-rendered DOM + Sonnet primary (DeepSeek fallback).

Fixes v1 limitation where DeepSeek read raw HTML and missed JS-rendered i18n,
country-detected currency, dynamic UI.

Per memory feedback_sim_js_fallback_sonnet.md (2026-05-31), JS-affected pages
MUST use v2.

Usage:
  python -m scripts.sim_users_v2 --persona ahmad_kopi_chain --page pricing \
      --timezone Asia/Kuala_Lumpur --locale ms-MY
"""
from __future__ import annotations
import argparse, asyncio, datetime as _dt, json, os, subprocess, sys, time
from pathlib import Path
import httpx

# Reuse PERSONAS + PROMPT_TEMPLATE from v1
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sim_users_deepseek import (
    PERSONAS, PROMPT_TEMPLATE, load_openrouter_key, check_balance,
    OPENROUTER_BASE,
)

# v2 reversed model priority — Sonnet first for JS-aware reasoning
PRIMARY_MODEL  = "anthropic/claude-sonnet-4.5"
FALLBACK_MODEL = "deepseek/deepseek-chat"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = Path("/Users/mozat/a-docs")
PORT = 8765

PAGE_PATHS = {
    "portal": "/landing/portal.html",
    "storefront": "/landing/storefront.html",
    "play": "/landing/play.html",
    "alpha": "/landing/alpha.html",
    "index": "/landing/index.html",
    "pricing": "/landing/pricing.html",
    "connect": "/landing/connect.html?brand_id=demo&scopes=profile&redirect_uri=http://example.com",
    "sg-case-studies": "/landing/sg-case-studies.html",
    "my-case-studies": "/landing/my-case-studies.html",
}


def ensure_server() -> subprocess.Popen | None:
    """Start http.server on PORT if not already running."""
    import urllib.request
    try:
        urllib.request.urlopen(f"http://localhost:{PORT}/landing/index.html", timeout=2)
        return None
    except Exception:
        proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(PORT)],
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(2)
        return proc


async def render_page_text(page_name: str, locale: str, timezone: str) -> str:
    """Load page via playwright, wait for JS, return visible text + key
    attributes the user would actually see."""
    from playwright.async_api import async_playwright
    path = PAGE_PATHS.get(page_name)
    if not path:
        return f"[unknown page: {page_name}]"
    url = f"http://localhost:{PORT}{path}"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(locale=locale, timezone_id=timezone,
                                        viewport={"width":1280,"height":900})
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            await browser.close()
            return f"[page load failed: {e}]"
        # Let i18next + dynamic JS settle
        await page.wait_for_timeout(2500)

        # Extract visible text from body (post-JS DOM)
        body_text = await page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
        # Extract key buttons/labels with role / aria-label
        widget_summary = await page.evaluate("""
            () => {
              const items = [];
              document.querySelectorAll('button, a[href], [role="button"], input[placeholder]').forEach(el => {
                const txt = (el.innerText || el.placeholder || el.getAttribute('aria-label') || '').trim();
                if (txt && txt.length < 80) items.push(txt);
              });
              return [...new Set(items)].slice(0, 60).join(' | ');
            }
        """)
        # Currency / language signals
        lang = await page.get_attribute('html', 'lang') or 'unknown'
        title = await page.title()
        await browser.close()

    summary = (
        f"[PAGE TITLE] {title}\n"
        f"[HTML lang] {lang}\n"
        f"[BROWSER LOCALE] {locale}  [TIMEZONE] {timezone}\n"
        f"[KEY WIDGETS / BUTTONS] {widget_summary}\n\n"
        f"[VISIBLE BODY TEXT — post-JS rendered]\n{body_text}"
    )
    if len(summary) > 16000:
        summary = summary[:16000] + "\n[... truncated]"
    return summary


def call_llm(key: str, system: str, user: str, max_tokens=2500, temp=0.7) -> dict:
    """Sonnet primary, DeepSeek fallback. Returns dict with text + model_used."""
    for m in (PRIMARY_MODEL, FALLBACK_MODEL):
        try:
            r = httpx.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers={"Authorization":f"Bearer {key}","Content-Type":"application/json",
                         "HTTP-Referer":"https://kix.app","X-Title":"KiX user-sim v2"},
                json={"model":m,"messages":[{"role":"system","content":system},
                                            {"role":"user","content":user}],
                      "max_tokens":max_tokens,"temperature":temp},
                timeout=180,
            )
            if r.status_code == 200:
                d = r.json()
                return {"ok":True,"text":d["choices"][0]["message"]["content"],
                        "model_used":m,
                        "tokens_in":d.get("usage",{}).get("prompt_tokens",0),
                        "tokens_out":d.get("usage",{}).get("completion_tokens",0)}
            print(f"  ⚠ {m} HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"  ⚠ {m} → {e}")
    return {"ok":False,"text":"","model_used":None,"tokens_in":0,"tokens_out":0}


async def simulate_v2(persona_key: str, page_name: str, key: str,
                       locale: str, timezone: str) -> dict:
    persona = PERSONAS[persona_key]
    print(f"\n→ Persona: {persona['name']}")
    print(f"→ Page: {page_name} (locale={locale}, tz={timezone})")
    print("→ Rendering page in playwright...")
    page_text = await render_page_text(page_name, locale, timezone)
    print(f"→ Got {len(page_text)} chars of rendered text")
    user_prompt = PROMPT_TEMPLATE.format(
        name=persona["name"], role=persona["role"],
        context=persona["context"], page_text=page_text,
    )
    print(f"→ Calling Sonnet 4.5 (primary)...")
    result = call_llm(key,
        "You are a realistic user simulator. Stay in character. Be specific and "
        "honest. The page text below is the ACTUAL rendered DOM post-JavaScript, "
        "so JS-driven i18n, currency formatting and dynamic widgets are already "
        "applied. Trust what you see.",
        user_prompt)
    if not result["ok"]:
        print("  ✗ LLM call failed"); return result
    print(f"  ✓ {result['model_used']} ({result['tokens_in']}→{result['tokens_out']} tokens)")
    return result


def write_report(persona_key, page_name, locale, timezone, result) -> Path:
    persona = PERSONAS[persona_key]
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = OUTPUT_DIR / f"sim-v2-{ts}-{persona_key}-{page_name}-{locale}.md"
    out.write_text(
        f"""# User Sim v2 (playwright + Sonnet) — {persona['name']} on /{page_name}

**Persona**: {persona_key}
**Role**: {persona['role'][:300]}…
**Browser locale**: {locale}  **Timezone**: {timezone}
**Model used**: {result.get('model_used','n/a')}
**Tokens**: {result.get('tokens_in',0)} → {result.get('tokens_out',0)}

---

## Walkthrough (post-JS rendered)

{result.get('text','[no response]')}

---
*Generated by `scripts/sim_users_v2.py` — actual DOM, not raw HTML*
"""
    )
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--persona", default="ahmad_kopi_chain", choices=list(PERSONAS.keys()))
    p.add_argument("--page", default="pricing", choices=list(PAGE_PATHS.keys()))
    p.add_argument("--locale", default="ms-MY")
    p.add_argument("--timezone", default="Asia/Kuala_Lumpur")
    args = p.parse_args()

    key = load_openrouter_key()
    bal = check_balance(key)
    if bal["pct_used"] > 90:
        print(f"⚠ OpenRouter {bal['pct_used']:.1f}% — pause"); return 1
    print(f"OpenRouter: {bal['pct_used']:.1f}% used (${bal['remaining']:.0f} left)")

    server = ensure_server()
    try:
        result = asyncio.run(simulate_v2(args.persona, args.page, key,
                                          args.locale, args.timezone))
    finally:
        if server:
            server.terminate(); time.sleep(0.3)

    out = write_report(args.persona, args.page, args.locale, args.timezone, result)
    print(f"\n✓ Report: {out}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
