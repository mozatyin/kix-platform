"""Translate landing/i18n/locales/{locale}/*.json via OpenRouter (DeepSeek).

User-authorized OpenRouter use 2026-05-31 (Wave I.A-2 continuation). Mirrors
translate_via_openrouter.py but for the JSON-shaped landing catalogues.

Usage:
  python -m scripts.translate_landing_json --locale ms-MY --files index portal pricing connect play
"""
from __future__ import annotations
import argparse, json, os, sys, time
from pathlib import Path
import httpx

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEEPSEEK_MODEL  = "deepseek/deepseek-chat"
FALLBACK_MODEL  = "anthropic/claude-sonnet-4.5"
ELTM_ENV = Path.home() / "eltm" / ".env"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LANDING_LOCALES = PROJECT_ROOT / "landing" / "i18n" / "locales"

LOCALE_NAMES = {
    "ms-MY": "Malay (Bahasa Melayu, Malaysia)",
    "id-ID": "Indonesian (Bahasa Indonesia)",
    "th-TH": "Thai",
    "vi-VN": "Vietnamese",
    "ar-EG": "Arabic (Egypt)",
    "ar-SA": "Arabic (Saudi Arabia)",
    "he-IL": "Hebrew (Israel)",
    "zh-Hans-CN": "Simplified Chinese (Mainland)",
    "zh-Hans-SG": "Simplified Chinese (Singapore)",
}

DO_NOT_TRANSLATE = {
    "KiX","TikTok","Google","Meta","Facebook","WhatsApp","Stripe","Square",
    "PayNow","GrabPay","Alipay","WeChat","OVO","Shopify","StoreHub","FPX",
    "TikTok Pixel","Meta Pixel","Meta CAPI","GA4","Google Analytics 4",
    "CPA","CPS","CPM","CPV","CPE","SDK","API","CSV","PDF","QR","ROI","CAC",
}


def load_key() -> str:
    if k := os.environ.get("OPENROUTER_API_KEY"): return k
    if ELTM_ENV.exists():
        for line in ELTM_ENV.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY=") and "sk-or-" in line:
                return line.split("=",1)[1].strip().strip('"').strip("'")
    raise RuntimeError("No OpenRouter key")


def call_llm(key, system, user, *, model=DEEPSEEK_MODEL, max_tokens=6000):
    for m in (model, FALLBACK_MODEL):
        try:
            r = httpx.post(f"{OPENROUTER_BASE}/chat/completions",
                headers={"Authorization":f"Bearer {key}","Content-Type":"application/json",
                         "HTTP-Referer":"https://kix.app","X-Title":"KiX landing i18n"},
                json={"model":m,"messages":[{"role":"system","content":system},
                                            {"role":"user","content":user}],
                      "max_tokens":max_tokens,"temperature":0.2}, timeout=180)
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"]
            print(f"  ⚠ {m} HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"  ⚠ {m} → {e}")
    return None


def translate_dict(key, locale_name, en_dict, file_name):
    """Translate a JSON dict's values, preserving keys + HTML tags + brand names."""
    keys = list(en_dict.keys())
    # Batch in chunks of ~30 entries
    BATCH = 30
    out = {}
    system = (
        f"You translate web-app UI strings from English to {locale_name}.\n"
        f"INPUT: JSON object with English values. OUTPUT: JSON object with same keys, translated values.\n"
        f"RULES:\n"
        f"1. Preserve every <em>/<strong>/<br>/<a> tag verbatim around translated content.\n"
        f"2. Preserve {{variable}} placeholders verbatim.\n"
        f"3. DO NOT translate brand/product/acronym names: {', '.join(sorted(DO_NOT_TRANSLATE))}.\n"
        f"4. Tone: professional, plain, SMB merchant SaaS (clear, short, action-oriented).\n"
        f"5. Output ONLY a valid JSON object. No commentary, no code fences.\n"
        f"6. If unsure, keep the English term and translate only the rest of the sentence.\n"
    )
    for i in range(0, len(keys), BATCH):
        batch_keys = keys[i:i+BATCH]
        batch_dict = {k: en_dict[k] for k in batch_keys}
        user = (f"Translate this JSON to {locale_name}. Return ONLY the JSON.\n\n"
                f"```json\n{json.dumps(batch_dict, ensure_ascii=False, indent=2)}\n```")
        print(f"  {file_name} batch {i//BATCH + 1}/{(len(keys)+BATCH-1)//BATCH} ({len(batch_keys)} keys)...", end=" ", flush=True)
        t0 = time.time()
        result = call_llm(key, system, user)
        if result is None:
            print("FAILED, keeping English")
            out.update(batch_dict); continue
        # Strip code fences
        s = result.strip()
        if s.startswith("```"):
            s = s.split("\n",1)[1] if "\n" in s else s
            if s.endswith("```"):
                s = s.rsplit("```",1)[0]
            if s.startswith("json"):
                s = s[4:].lstrip()
        try:
            parsed = json.loads(s)
            out.update(parsed)
            print(f"OK ({time.time()-t0:.1f}s)")
        except json.JSONDecodeError as e:
            print(f"JSON parse error: {e}, keeping English for batch")
            out.update(batch_dict)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--locale", required=True)
    p.add_argument("--files", nargs="+", required=True, help="json basenames without extension, e.g. index pricing connect")
    p.add_argument("--source", default="en-SG")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.locale not in LOCALE_NAMES:
        print(f"Unknown locale: {args.locale}. Options: {list(LOCALE_NAMES.keys())}")
        sys.exit(1)

    key = load_key()
    print(f"OpenRouter key loaded; target locale: {args.locale} ({LOCALE_NAMES[args.locale]})")

    src_dir = LANDING_LOCALES / args.source
    dst_dir = LANDING_LOCALES / args.locale
    dst_dir.mkdir(parents=True, exist_ok=True)

    for fname in args.files:
        src_path = src_dir / f"{fname}.json"
        if not src_path.exists():
            print(f"  ✗ source missing: {src_path}")
            continue
        en = json.loads(src_path.read_text())
        print(f"\n=== {fname}.json ({len(en)} keys) ===")
        translated = translate_dict(key, LOCALE_NAMES[args.locale], en, fname)
        dst_path = dst_dir / f"{fname}.json"
        if args.dry_run:
            print(json.dumps(translated, ensure_ascii=False, indent=2)[:500])
        else:
            # Backup
            if dst_path.exists():
                bk = dst_path.with_suffix(".json.preTranslate")
                dst_path.replace(bk)
            dst_path.write_text(json.dumps(translated, ensure_ascii=False, indent=2) + "\n")
            print(f"  ✓ wrote {dst_path}")


if __name__ == "__main__":
    main()
