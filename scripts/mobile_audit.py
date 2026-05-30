"""Mobile-responsive audit — screenshot all landing pages at 3 viewports.

Per Wave H Opp E from Trinity gap analysis. Round 3 sim had 0/25
personas raise mobile (text personas don't physically use mobile),
but real merchants WILL. This catches obvious overflow/clipping bugs
before they're caught by humans.

Captures: 375px (iPhone SE) / 768px (iPad portrait) / 1280px (laptop)
For: portal / storefront / play / index / pricing / sg-case-studies /
     alpha / connect

Output: /Users/mozat/a-docs/mobile-audit/{page}-{width}.png + report.md
"""
from playwright.sync_api import sync_playwright
from pathlib import Path

BASE = "http://localhost:8765"
OUT = Path("/Users/mozat/a-docs/mobile-audit")
OUT.mkdir(exist_ok=True)

PAGES = [
    ("portal", f"{BASE}/landing/portal.html?demo=true&lang=en-SG"),
    ("storefront", f"{BASE}/landing/storefront.html"),
    ("play", f"{BASE}/landing/play.html"),
    ("index", f"{BASE}/landing/index.html"),
    ("pricing", f"{BASE}/landing/pricing.html"),
    ("sg-case-studies", f"{BASE}/landing/sg-case-studies.html"),
    ("alpha", f"{BASE}/landing/alpha.html"),
    ("connect", f"{BASE}/landing/connect.html"),
]

VIEWPORTS = [
    ("mobile-375", 375, 812),   # iPhone SE / 12 / 13 mini
    ("tablet-768", 768, 1024),  # iPad portrait
    ("laptop-1280", 1280, 800), # MacBook Air
]

results = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    for page_name, url in PAGES:
        for vp_name, w, h in VIEWPORTS:
            page = browser.new_page(viewport={"width": w, "height": h})
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=8000)
                page.wait_for_timeout(1500)
                # Detect horizontal overflow (>0 means content wider than viewport)
                overflow_x = page.evaluate(
                    "Math.max(0, document.documentElement.scrollWidth - window.innerWidth)"
                )
                # Check for any element wider than viewport
                wide_elements = page.evaluate(f"""
                    Array.from(document.querySelectorAll('*'))
                      .filter(el => el.scrollWidth > {w} + 1)
                      .slice(0, 5)
                      .map(el => ({{
                        tag: el.tagName, cls: el.className.toString().slice(0, 50),
                        width: el.scrollWidth, id: el.id || ''
                      }}))
                """)
                fname = f"{page_name}-{vp_name}.png"
                page.screenshot(path=str(OUT / fname), full_page=False)
                results.append({
                    "page": page_name, "viewport": vp_name,
                    "size": f"{w}×{h}",
                    "overflow_px": overflow_x,
                    "wide_elements": wide_elements,
                    "status": "OVERFLOW" if overflow_x > 0 else "OK",
                })
                print(f"  ✓ {page_name:<18} @ {vp_name:<12}  overflow={overflow_x}px  status={'OVERFLOW' if overflow_x > 0 else 'OK'}")
            except Exception as e:
                results.append({
                    "page": page_name, "viewport": vp_name,
                    "size": f"{w}×{h}", "status": f"ERROR: {e}",
                })
                print(f"  ✗ {page_name} @ {vp_name}: {e}")
            page.close()
    browser.close()

# Write report
report = ["# Mobile Audit Report — KiX Landing Pages", "", f"Date: 2026-05-31  | Pages: {len(PAGES)}  | Viewports: {len(VIEWPORTS)}", ""]
report.append("## Summary")
overflow_count = sum(1 for r in results if r.get('status') == 'OVERFLOW')
ok_count = sum(1 for r in results if r.get('status') == 'OK')
err_count = sum(1 for r in results if 'ERROR' in str(r.get('status', '')))
report.append(f"- Total runs: {len(results)}")
report.append(f"- OK: {ok_count}")
report.append(f"- Horizontal overflow: {overflow_count}")
report.append(f"- Errors: {err_count}")
report.append("")
report.append("## Per-page × per-viewport")
report.append("| Page | Viewport | Status | Overflow (px) | Wide elements |")
report.append("|---|---|---|---:|---|")
for r in results:
    we = ", ".join(f"{e['tag']}.{e['cls'][:30]}({e['width']}px)" for e in r.get('wide_elements', [])[:3]) or "—"
    report.append(f"| {r['page']} | {r['viewport']} | {r.get('status')} | {r.get('overflow_px', 'n/a')} | {we} |")
report.append("")
report.append("## Action items")
overflow_pages = [r for r in results if r.get('status') == 'OVERFLOW']
if overflow_pages:
    for r in overflow_pages:
        we = r.get('wide_elements', [])
        if we:
            report.append(f"- **{r['page']} @ {r['viewport']}**: overflow {r['overflow_px']}px from `<{we[0]['tag']}.{we[0]['cls'][:30]}>` width={we[0]['width']}px")
else:
    report.append("- No horizontal overflow detected at any viewport ✓")

(OUT / "report.md").write_text("\n".join(report))
print(f"\nReport: {OUT}/report.md")
print(f"Screenshots: {OUT}/")
