"""Playwright verification — confirm BM + RM switching actually work in real
browser (vs DeepSeek sim which reads raw HTML).

Loads pricing.html with MY-mocked timezone, checks CPA displays as RM. Then
clicks locale switcher → ms-MY, verifies hero title becomes BM.
"""
from __future__ import annotations
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LANDING_DIR = PROJECT_ROOT / "landing"
PORT = 8765

CHECKS = []

def ok(msg): print(f"  ✓ {msg}"); CHECKS.append(("ok", msg))
def fail(msg): print(f"  ✗ {msg}"); CHECKS.append(("fail", msg))

async def run():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # ─── Test 1: pricing.html with MY timezone → should show RM ───
        print("\n=== Test 1: pricing.html @ MY timezone → expect RM CPA ===")
        ctx = await browser.new_context(
            locale="ms-MY",
            timezone_id="Asia/Kuala_Lumpur",
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()
        await page.goto(f"http://localhost:{PORT}/landing/pricing.html",
                        wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1500)  # JS country detect + FX swap

        cpa_text = await page.locator("#px-cpa").text_content() or ""
        if "RM" in cpa_text:
            ok(f"pricing.html shows RM CPA: '{cpa_text}'")
        else:
            fail(f"pricing.html still shows: '{cpa_text}' (expected RM)")

        fxnote = await page.locator("#fx-note").text_content() or ""
        if "SGD" in fxnote or "S$3" in fxnote:
            ok(f"FX note explains SGD billing: '{fxnote[:80]}…'")
        else:
            fail(f"FX note missing SGD billing context: '{fxnote[:80]}'")

        # ─── Test 2: pricing.html locale switcher → ms-MY ───
        print("\n=== Test 2: locale switcher → BM ===")
        # Wait for switcher to appear
        try:
            await page.wait_for_selector(".kix-locale-switcher", timeout=8000)
            ok("locale switcher rendered")
            await page.click(".kix-ls-button")
            await page.wait_for_timeout(300)
            # Click ms-MY menu item
            ms_items = await page.locator(".kix-ls-item").all()
            clicked = False
            for it in ms_items:
                t = await it.text_content() or ""
                if "Melayu" in t or "MS" in t or "ms-MY" in t:
                    await it.click()
                    clicked = True
                    break
            if clicked:
                ok("clicked Bahasa Melayu in switcher")
                await page.wait_for_timeout(1500)
                h1 = await page.locator("h1").first.text_content() or ""
                if "Percuma" in h1 or "Saas" in h1.lower() or "bayar" in h1.lower():
                    ok(f"hero h1 translated: '{h1[:60]}…'")
                else:
                    fail(f"hero h1 not BM after switch: '{h1[:60]}'")
            else:
                fail("could not find Melayu in switcher items")
        except Exception as e:
            fail(f"locale switcher test errored: {e}")

        await ctx.close()

        # ─── Test 3: index.html compliance section visible ───
        print("\n=== Test 3: index.html compliance section ===")
        ctx2 = await browser.new_context(viewport={"width":1280,"height":800})
        page2 = await ctx2.new_page()
        await page2.goto(f"http://localhost:{PORT}/landing/index.html",
                         wait_until="networkidle", timeout=30000)
        await page2.wait_for_timeout(1000)
        compliance = await page2.locator("#compliance").count()
        if compliance > 0:
            ok("compliance section exists in DOM")
            pdpa_my = await page2.locator("#compliance").get_by_text("PDPA Malaysia").count()
            halal = await page2.locator("#compliance").get_by_text("Halal-aware").count()
            if pdpa_my and halal:
                ok("PDPA-MY + Halal-aware badges both present")
            else:
                fail(f"missing badges: pdpa_my={pdpa_my} halal={halal}")
        else:
            fail("compliance section missing from DOM")
        await ctx2.close()

        # ─── Test 4: connect.html new pixel integrations ───
        print("\n=== Test 4: connect.html ads-attribution badges ===")
        ctx3 = await browser.new_context(viewport={"width":1280,"height":800})
        page3 = await ctx3.new_page()
        await page3.goto(f"http://localhost:{PORT}/landing/connect.html?brand_id=demo&scopes=profile&redirect_uri=http://example.com",
                         wait_until="networkidle", timeout=30000)
        await page3.wait_for_timeout(1500)
        for name in ["TikTok Pixel","Meta Conversions API","Google Analytics 4","FPX"]:
            n = await page3.get_by_text(name, exact=False).count()
            if n > 0: ok(f"connect shows '{name}' ({n}x)")
            else: fail(f"connect missing '{name}'")
        await ctx3.close()

        # ─── Test 5: play.html demo mode (no brand param) ───
        print("\n=== Test 5: play.html demo banner ===")
        ctx4 = await browser.new_context(viewport={"width":1280,"height":800})
        page4 = await ctx4.new_page()
        await page4.goto(f"http://localhost:{PORT}/landing/play.html",
                         wait_until="networkidle", timeout=30000)
        await page4.wait_for_timeout(1500)
        demo_banner = await page4.get_by_text("Demo mode", exact=False).count()
        if demo_banner > 0:
            ok("play.html shows Demo mode banner (no longer error screen)")
        else:
            fail("play.html still shows error screen, not demo banner")
        await ctx4.close()

        # ─── Test 6: my-case-studies.html loads + 5 cases ───
        print("\n=== Test 6: my-case-studies.html ===")
        ctx5 = await browser.new_context(viewport={"width":1280,"height":800})
        page5 = await ctx5.new_page()
        await page5.goto(f"http://localhost:{PORT}/landing/my-case-studies.html",
                         wait_until="networkidle", timeout=30000)
        await page5.wait_for_timeout(500)
        cases = await page5.locator(".case").count()
        if cases >= 5:
            ok(f"my-case-studies has {cases} case cards")
        else:
            fail(f"my-case-studies only has {cases} case cards (expected 5)")
        archetypes = await page5.get_by_text("archetype", exact=False).count()
        if archetypes >= 5:
            ok(f"'archetype' honest framing present ({archetypes}x)")
        else:
            fail(f"'archetype' framing under-present: {archetypes}")
        await ctx5.close()

        await browser.close()

    return CHECKS

def main():
    # Start a local server on landing/ — assume the user has one OR launch one
    # Try to detect existing server first
    import urllib.request
    try:
        urllib.request.urlopen(f"http://localhost:{PORT}/landing/index.html", timeout=2)
        print(f"✓ server already running on :{PORT}")
        server_proc = None
    except Exception:
        print(f"… launching http.server on :{PORT}")
        server_proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(PORT)],
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(2)

    try:
        results = asyncio.run(run())
    finally:
        if server_proc:
            server_proc.terminate()
            time.sleep(0.5)

    pass_n = sum(1 for s,_ in results if s == "ok")
    fail_n = sum(1 for s,_ in results if s == "fail")
    print(f"\n=== SUMMARY: {pass_n} pass / {fail_n} fail ===")
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
