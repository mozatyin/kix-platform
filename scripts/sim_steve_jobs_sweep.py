"""Steve Jobs UX-teardown sweep across all KiX pages.

v2 architecture (2026-05-31): single chromium instance, sequential nav
(reuses the browser process — much less system contention than spawning
14 chromiums in parallel). LLM calls run in parallel via run_in_executor
AFTER all rendered text is captured.

Output: docs/sim-results/steve-jobs-ux-sweep.md
"""
from __future__ import annotations
import asyncio, sys, datetime as _dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sim_users_deepseek import load_openrouter_key
from sim_users_v2 import call_llm, PAGE_PATHS, ensure_server

SYSTEM = (
    "You are Steve Jobs doing a brutal UX teardown for a friend who runs a "
    "tech startup. You are NOT a customer. You are not polite. You are looking "
    "for every detail that's OK but not great. The page text below is the "
    "actual rendered DOM post-JavaScript - trust what you see.\n\n"
    "Return ONLY a punch list. Format each item as:\n"
    "  [SEVERITY . CATEGORY] one-line description - what's wrong and what would fix it\n"
    "Severities: P0 (broken / confusing / drives users away), P1 (cheap-looking / inconsistent / breaks flow), P2 (small polish miss).\n"
    "Categories: Copy / Spacing / Hierarchy / Color / Type / Interaction / Loading / Empty / Accessibility / Mobile / Trust / Performance / Other.\n"
    "Be terse. No softening. No 'overall it's good' preamble. No closing summary. Lists only.\n"
    "If a page is genuinely impressive in some way, allowed ONE line at end: [GREAT] one-thing-actually-great.\n"
)

USER_TPL = """PAGE: {url}

{page_text}

---

Punch list, in priority order (P0 first). Aim for 8-15 items per page. Be specific - quote the exact element / copy you're criticising."""

PAGE_PATHS.update({
    "enterprise": "/landing/enterprise.html",
    "calculator": "/landing/calculator.html",
    "trinity-artifacts": "/landing/trinity-artifacts.html",
    "fnb": "/landing/verticals/fnb.html",
    "pos": "/landing/integrations/pos-integrations.html",
    "tiktok": "/landing/integrations/tiktok-pixel.html",
    "new-customer": "/landing/legal/new-customer-definition.html",
})

SWEEP_PAGES = [
    "index", "pricing", "enterprise", "sg-case-studies", "my-case-studies",
    "fnb", "tiktok", "pos",
    "trinity-artifacts", "calculator", "new-customer",
    "portal", "storefront", "play",
]


async def render_all_pages_sequentially():
    """Single browser, single context, sequential nav. Fastest stable."""
    from playwright.async_api import async_playwright
    rendered = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            locale="en-SG", timezone_id="Asia/Singapore",
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()
        for name in SWEEP_PAGES:
            url = f"http://localhost:8765{PAGE_PATHS[name]}"
            print(f"  rendering {name}...", end=" ", flush=True)
            try:
                await page.goto(url, wait_until="networkidle", timeout=20000)
                await page.wait_for_timeout(1200)
                body = await page.evaluate("() => document.body ? document.body.innerText : ''")
                widgets = await page.evaluate("""
                    () => {
                      const items = [];
                      document.querySelectorAll('button, a[href], [role=\"button\"], input[placeholder]').forEach(el => {
                        const txt = (el.innerText || el.placeholder || el.getAttribute('aria-label') || '').trim();
                        if (txt && txt.length < 80) items.push(txt);
                      });
                      return [...new Set(items)].slice(0, 40).join(' | ');
                    }
                """)
                title = await page.title()
                summary = (f"[TITLE] {title}\n[KEY BUTTONS] {widgets}\n\n[BODY TEXT]\n{body}")[:14000]
                rendered[name] = summary
                print(f"OK ({len(summary)} chars)", flush=True)
            except Exception as e:
                rendered[name] = f"[render failed: {e}]"
                print(f"FAIL ({e})", flush=True)
        await browser.close()
    return rendered


def critique_one(page_name, url, page_text, key):
    prompt = USER_TPL.format(url=url, page_text=page_text)
    result = call_llm(key, SYSTEM, prompt, max_tokens=2500, temp=0.3)
    return {"page": page_name, "url": url, "result": result}


async def main():
    key = load_openrouter_key()
    server = ensure_server()
    out_path = Path("docs/sim-results/steve-jobs-ux-sweep.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        print(f"=== Phase 1/2: rendering {len(SWEEP_PAGES)} pages (sequential, 1 chromium)", flush=True)
        rendered = await render_all_pages_sequentially()

        print(f"\n=== Phase 2/2: critiquing in parallel via Sonnet 4.5", flush=True)
        loop = asyncio.get_event_loop()
        tasks = []
        for name in SWEEP_PAGES:
            url = f"http://localhost:8765{PAGE_PATHS[name]}"
            tasks.append(loop.run_in_executor(None, critique_one, name, url, rendered[name], key))
        results = await asyncio.gather(*tasks)
        print(f"  all critiques done", flush=True)
    finally:
        if server:
            server.terminate(); await asyncio.sleep(0.3)

    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    md = [
        f"# Steve Jobs UX Teardown - KiX full website sweep",
        f"\n**Date:** {ts}",
        "\n**Method:** Claude Sonnet 4.5 role-playing Steve Jobs critic. Real rendered DOM via playwright (sequential render, parallel critique).",
        f"**Pages swept:** {len(SWEEP_PAGES)} ({', '.join(SWEEP_PAGES)})",
        "\n**Severity legend:** P0 = drives users away . P1 = cheap-looking / breaks flow . P2 = polish miss",
        "\n---\n",
    ]
    for r in results:
        md.append(f"## {r['page']}")
        md.append(f"\n[`{r['url']}`]({r['url']})\n")
        md.append(r['result']['text'])
        md.append("\n---\n")
    out_path.write_text("\n".join(md))
    print(f"\nWrote {out_path} ({len(rendered)} pages)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
