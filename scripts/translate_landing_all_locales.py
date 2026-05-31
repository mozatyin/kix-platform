"""Translate landing.json into all 11 supported locales via OpenRouter (Sonnet).

Founder mandate 2026-06-01: "所有支持的语言翻译全都做到底 · 彻底多语言"

Reads landing/i18n/locales/en-SG/landing.json (source of truth).
Translates into each missing locale in parallel via OpenRouter Sonnet
(per the standing rule: translation tasks use OpenRouter parallel agents).

Supported locales (from i18next-runtime.js NAMESPACES):
  en-SG (source) · en-US · zh-Hans-SG (done) · zh-Hans-CN (done)
  id-ID · ms-MY · th-TH · vi-VN · ar-EG · ar-SA · he-IL
"""
from __future__ import annotations
import asyncio, json, os, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from sim_users_deepseek import load_openrouter_key
from sim_users_v2 import call_llm


SOURCE_LOCALE = "en-SG"
TARGET_LOCALES = {
    "en-US": "American English (use $ not S$ where context allows; localize idioms · keep brand+vertical English terms)",
    "id-ID": "Bahasa Indonesia · formal but warm · keep brand names + KiX in English",
    "ms-MY": "Bahasa Melayu (Malaysia) · friendly merchant tone · keep brand names + KiX in English · S$ → RM where context allows",
    "th-TH": "ภาษาไทย · polite formal · keep brand names + KiX in English · S$ → ฿",
    "vi-VN": "Tiếng Việt · friendly formal · keep brand names + KiX in English · S$ → ₫",
    "ar-EG": "العربية المصرية · formal RTL · keep brand names + KiX in Latin · numbers stay Western Arabic",
    "ar-SA": "العربية السعودية الفصحى · formal RTL · keep brand names + KiX in Latin · numbers stay Western Arabic",
    "he-IL": "עברית · formal RTL · keep brand names + KiX in Latin · numbers stay Western Arabic",
}


def translate_one(key: str, source_json: dict, target_locale: str, style: str) -> dict:
    """Call OpenRouter to translate the full landing.json into target_locale.
    Returns {locale, json_dict, ok, error}."""
    src = json.dumps(source_json, indent=2, ensure_ascii=False)
    system = (
        "You are a professional B2B SaaS landing-page translator. Return ONLY "
        "a valid JSON object with the SAME keys as the source. Translate values "
        "ONLY. Preserve numbers, brand names (KiX, Heng Heng Kopi, Brew Lab), "
        "currency markers (S$, RM, ¥, etc.), arrows (→), bullets (·), and "
        "<em>/<strong> HTML tags exactly. Do not add comments or markdown fencing."
    )
    user = (
        f"Translate the following landing-page i18n JSON into {target_locale}.\n"
        f"Style notes: {style}\n\n"
        f"SOURCE (en-SG):\n{src}\n\n"
        f"Return ONLY the translated JSON object."
    )
    out = call_llm(key, system, user, max_tokens=3500, temp=0.2)
    if not out.get("ok"):
        return {"locale": target_locale, "ok": False, "error": out.get("error", "llm-fail")}
    text = out["text"].strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        data = json.loads(text)
        # validate keys
        missing = set(source_json.keys()) - set(data.keys())
        if missing:
            return {"locale": target_locale, "ok": False,
                    "error": f"missing keys: {sorted(missing)[:5]}"}
        return {"locale": target_locale, "ok": True, "json": data}
    except json.JSONDecodeError as e:
        return {"locale": target_locale, "ok": False,
                "error": f"non-JSON · {e} · raw_head: {text[:200]}"}


async def main() -> int:
    key = load_openrouter_key()
    if not key:
        print("OPENROUTER_API_KEY missing", file=sys.stderr)
        return 1

    source_path = ROOT / "landing" / "i18n" / "locales" / SOURCE_LOCALE / "landing.json"
    source = json.loads(source_path.read_text())
    print(f"Source: {source_path.relative_to(ROOT)} · {len(source)} keys")

    # Skip locales already done (3-key sniff check)
    todo = []
    for loc, style in TARGET_LOCALES.items():
        out = ROOT / "landing" / "i18n" / "locales" / loc / "landing.json"
        if out.exists():
            try:
                existing = json.loads(out.read_text())
                if set(existing.keys()) >= set(source.keys()):
                    print(f"  ✓ {loc} already complete ({len(existing)} keys)")
                    continue
                else:
                    print(f"  ↻ {loc} partial → re-translating")
            except Exception:
                pass
        todo.append((loc, style))

    if not todo:
        print("\nAll target locales already complete.")
        return 0

    print(f"\nTranslating {len(todo)} locale(s) in parallel via OpenRouter Sonnet...")
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = await asyncio.gather(*[
            loop.run_in_executor(pool, translate_one, key, source, loc, style)
            for loc, style in todo
        ])

    written = 0
    for r in results:
        loc = r["locale"]
        if not r["ok"]:
            print(f"  ✗ {loc} FAIL · {r.get('error', '?')[:100]}")
            continue
        out = ROOT / "landing" / "i18n" / "locales" / loc / "landing.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(r["json"], indent=2, ensure_ascii=False) + "\n")
        print(f"  ✓ wrote {out.relative_to(ROOT)} · {len(r['json'])} keys")
        written += 1

    print(f"\nDone · {written}/{len(todo)} locales translated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
