"""Steve Jobs UX-teardown sweep across all KiX pages.

Per user request 2026-05-31: walk through entire portal/website, record
every UX problem in a single punch list. Sonnet 4.5 + playwright (real
rendered DOM, post-JS). Output: docs/sim-results/steve-jobs-ux-sweep.md
"""
from __future__ import annotations
import asyncio, sys, datetime as _dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sim_users_deepseek import PERSONAS, load_openrouter_key
from sim_users_v2 import render_page_text, call_llm, PAGE_PATHS, ensure_server

# Override default templates: Jobs sweep needs a punch-list format, not 7-section walkthrough
SYSTEM = (
    "You are Steve Jobs doing a brutal UX teardown for a friend who runs a "
    "tech startup. You are NOT a customer. You are not polite. You are looking "
    "for every detail that's OK but not great. The page text below is the "
    "actual rendered DOM post-JavaScript — trust what you see.\n\n"
    "Return ONLY a punch list. Format each item as:\n"
    "  [SEVERITY · CATEGORY] one-line description — what's wrong and what would fix it\n"
    "Severities: P0 (broken / confusing / drives users away), P1 (cheap-looking / inconsistent / breaks flow), P2 (small polish miss).\n"
    "Categories: Copy / Spacing / Hierarchy / Color / Type / Interaction / Loading / Empty / Accessibility / Mobile / Trust / Performance / Other.\n"
    "Be terse. No softening. No 'overall it's good' preamble. No closing summary. Lists only.\n"
    "If a page is genuinely impressive in some way, allowed ONE line at end: [GREAT] one-thing-actually-great.\n"
)

USER_TPL = """PAGE: {url}

{page_text}

---

Punch list, in priority order (P0 first). Aim for 8-15 items per page. Be specific — quote the exact element / copy you're criticising."""


# Add a few missing PAGE_PATHS for sweep
PAGE_PATHS.update({
    "calculator": "/landing/calculator.html",
    "trinity-artifacts": "/landing/trinity-artifacts.html",
    "fnb": "/landing/verticals/fnb.html",
    "beauty": "/landing/verticals/beauty.html",
    "retail": "/landing/verticals/retail.html",
    "fitness": "/landing/verticals/fitness.html",
    "pos": "/landing/integrations/pos-integrations.html",
    "tiktok": "/landing/integrations/tiktok-pixel.html",
    "new-customer": "/landing/legal/new-customer-definition.html",
})

# Pages to sweep (in user-journey order)
SWEEP_PAGES = [
    "index",         # marketing landing
    "pricing",       # pricing page
    "enterprise",    # B2B
    "sg-case-studies",
    "my-case-studies",
    "fnb",           # vertical
    "tiktok",        # integration
    "pos",           # integration
    "trinity-artifacts",
    "calculator",
    "new-customer",  # legal
    "portal",        # merchant portal
    "storefront",    # consumer side
    "play",          # consumer play (demo)
]


async def sweep_one(page_name: str, key: str) -> dict:
    url = f"http://localhost:8765{PAGE_PATHS[page_name]}"
    print(f"\n→ Rendering {page_name} ({url})")
    page_text = await render_page_text(page_name, locale="en-SG", timezone="Asia/Singapore")
    print(f"  text len: {len(page_text)}")
    prompt = USER_TPL.format(url=url, page_text=page_text[:14000])
    result = call_llm(key, SYSTEM, prompt, max_tokens=2500, temp=0.3)
    print(f"  → {result['model_used']} tokens={result['tokens_in']}→{result['tokens_out']}")
    return {"page": page_name, "url": url, "result": result}


async def main():
    key = load_openrouter_key()
    server = ensure_server()
    out_path = Path("docs/sim-results/steve-jobs-ux-sweep.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Run all sweeps in PARALLEL (OpenRouter parallel rule per memory)
        tasks = [sweep_one(p, key) for p in SWEEP_PAGES]
        results = await asyncio.gather(*tasks)
    finally:
        if server:
            server.terminate(); await asyncio.sleep(0.3)

    # Synthesize single punch-list doc
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    md = [
        f"# Steve Jobs UX Teardown — KiX full website sweep",
        f"\n**Date:** {ts}",
        "\n**Method:** Claude Sonnet 4.5 role-playing Steve Jobs critic. Real rendered DOM via playwright.",
        f"**Pages swept:** {len(SWEEP_PAGES)} ({', '.join(SWEEP_PAGES)})",
        "\n**Severity legend:** P0 = drives users away · P1 = cheap-looking / breaks flow · P2 = polish miss",
        "\n---\n",
    ]
    for r in results:
        md.append(f"## {r['page']}")
        md.append(f"\n[`{r['url']}`]({r['url']})\n")
        md.append(r['result']['text'])
        md.append("\n---\n")
    out_path.write_text("\n".join(md))
    print(f"\n✓ Wrote {out_path}")

if __name__ == "__main__":
    asyncio.run(main())
