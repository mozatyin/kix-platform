"""One-shot Steve Jobs critique on R19 Shopify-styled pages."""
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services.persona_registry import get as get_persona
from sim_users_deepseek import load_openrouter_key
from sim_users_v2 import call_llm


async def render(url: str) -> str:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        page = await (await b.new_context(viewport={"width": 1280, "height": 900})).new_page()
        await page.goto(url, wait_until="networkidle", timeout=20000)
        await page.wait_for_timeout(1500)
        t = await page.evaluate("document.body.innerText")
        await b.close()
        return t


def critique(key: str, persona_id: str, url: str, text: str) -> dict:
    p = get_persona(persona_id)
    sysmsg = (
        f"You ARE {p.name}. {p.role}\n\nContext: {p.context}\n\n"
        'Return ONLY JSON: {"score":0-100, "top3_complaints":[3 issues],'
        ' "top3_loves":[3 working], "single_biggest_fix":"one sentence"}'
    )
    usermsg = f"PAGE: {url}\n\nCONTENT (first 8000 chars):\n```\n{text[:8000]}\n```"
    out = call_llm(key, sysmsg, usermsg, max_tokens=1200, temp=0.6)
    if not out["ok"]:
        return {"error": "llm-failed"}
    txt = out["text"].strip()
    if txt.startswith("```"):
        txt = txt.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        return json.loads(txt)
    except Exception as e:
        return {"parse_err": str(e), "raw": txt[:400]}


async def main() -> int:
    key = load_openrouter_key()
    if not key:
        print("no OPENROUTER_API_KEY", file=sys.stderr); return 1
    urls = [
        "http://localhost:8765/landing/brands/default/index.html",
        "http://localhost:8765/landing/brands/kix_for_enterprise/index.html",
        "http://localhost:8765/landing/portal-tiktok-preview.html",
    ]
    for url in urls:
        path = url.replace("http://localhost:8765", "")
        print(f"\n=== Steve Jobs · {path} ===")
        text = await render(url)
        r = critique(key, "steve_jobs", url, text)
        print(json.dumps(r, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
