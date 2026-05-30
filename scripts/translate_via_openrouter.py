"""Batch translate Fluent (.ftl) catalogs via OpenRouter (DeepSeek primary).

User explicitly authorized OpenRouter for this task 2026-05-31 — "启动
使用我们系统的 openrouter 的 key". Per window principles this is a
one-shot system-layer LLM call gated by explicit user approval.

Reads source en-SG .ftl, batches messages (20 per call), preserves ICU
plural syntax + variable placeholders, writes to target locale dir.

Models:
  Primary:   deepseek/deepseek-chat (cheap, good for translation)
  Fallback:  anthropic/claude-sonnet-4.5 (for tricky cases)

Cost: ~$0.001 per locale (vs $0.008 via Claude Haiku direct).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEEPSEEK_MODEL = "deepseek/deepseek-chat"
SONNET_FALLBACK_MODEL = "anthropic/claude-sonnet-4.5"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CATALOG_ROOT = PROJECT_ROOT / "app" / "i18n" / "catalogs"
ELTM_ENV = Path.home() / "eltm" / ".env"

# Locales we ship to (LANG_NAME used in LLM prompt)
LOCALE_NAMES = {
    "ms-MY": "Malay (Malaysia)",
    "id-ID": "Indonesian (Bahasa Indonesia)",
    "th-TH": "Thai",
    "vi-VN": "Vietnamese",
    "ar-EG": "Arabic (Egypt)",
    "ar-SA": "Arabic (Saudi Arabia)",
    "he-IL": "Hebrew (Israel)",
}

# DO NOT translate these (preserve as-is in target)
DO_NOT_TRANSLATE = {
    "KiX", "TikTok", "Google", "Meta", "Facebook", "WhatsApp", "Stripe",
    "PayNow", "GrabPay", "Alipay", "WeChat", "OVO",
}


def load_openrouter_key() -> str:
    """OR key — env first, then eltm/.env (where it's stored under
    ANTHROPIC_API_KEY since they use OR proxy mode)."""
    if key := os.environ.get("OPENROUTER_API_KEY"):
        return key
    if ELTM_ENV.exists():
        for raw in ELTM_ENV.read_text().splitlines():
            if raw.startswith("ANTHROPIC_API_KEY=") and "sk-or-" in raw:
                return raw.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("No OpenRouter key found")


def parse_ftl(src: str) -> list[tuple[str, str]]:
    """Crude FTL parser: returns list of (key, raw_text) tuples.
    Comments + blank lines skipped. Each entry includes attributes."""
    entries = []
    current_key = None
    current_lines = []
    for line in src.splitlines():
        if not line.strip() or line.strip().startswith(("#", "//")):
            if current_key:
                entries.append((current_key, "\n".join(current_lines)))
                current_key = None; current_lines = []
            continue
        m = re.match(r"^([a-zA-Z][\w.-]*)\s*=\s*(.*)$", line)
        if m:
            if current_key:
                entries.append((current_key, "\n".join(current_lines)))
            current_key = m.group(1)
            current_lines = [line]
        elif line.startswith((" ", "\t")) and current_key:
            current_lines.append(line)
        else:
            if current_key:
                entries.append((current_key, "\n".join(current_lines)))
                current_key = None; current_lines = []
    if current_key:
        entries.append((current_key, "\n".join(current_lines)))
    return entries


def call_llm(key: str, system: str, user: str, *,
             model: str = DEEPSEEK_MODEL,
             max_tokens: int = 4000) -> Optional[str]:
    for attempt_model in (model, SONNET_FALLBACK_MODEL):
        try:
            r = httpx.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://kix.app",
                    "X-Title": "KiX i18n translate",
                },
                json={
                    "model": attempt_model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": 0.2,
                },
                timeout=120,
            )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"]
            print(f"  ⚠ {attempt_model} → HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"  ⚠ {attempt_model} → {e}")
    return None


def translate_locale(key: str, target_locale: str, source_ftl: str) -> str:
    """Translate all FTL entries to target_locale. Returns the new FTL text."""
    entries = parse_ftl(source_ftl)
    print(f"  Parsed {len(entries)} entries")

    # Header (preserve / annotate)
    out_lines = [
        f"### KiX Platform — {LOCALE_NAMES[target_locale]} catalog",
        f"### Auto-translated via OpenRouter (DeepSeek) 2026-05-31",
        f"### Source: app/i18n/catalogs/en-SG/main.ftl",
        "###",
    ]

    # Batch in groups of 20
    BATCH = 20
    system_prompt = (
        f"You are a translation engine. Translate Fluent (.ftl) message "
        f"file entries from English to {LOCALE_NAMES[target_locale]}.\n\n"
        f"RULES:\n"
        f"1. Preserve every {{ $variable }} placeholder verbatim.\n"
        f"2. Preserve ICU plural syntax: [one], *[other], { '{' }count, plural, ... { '}' }\n"
        f"3. Preserve the .attribute = ... structure (attributes belong to previous key).\n"
        f"4. Do NOT translate: KiX, TikTok, Google, Meta, Facebook, WhatsApp, Stripe, "
        f"   PayNow, GrabPay, Alipay, WeChat, OVO (product/brand names).\n"
        f"5. Keep the same line structure — key = value, with attribute indentation.\n"
        f"6. Output ONLY the translated FTL lines. No commentary. No code fences.\n"
        f"7. Tone: professional, plain, suitable for SMB merchant SaaS.\n"
    )

    for i in range(0, len(entries), BATCH):
        batch = entries[i:i+BATCH]
        batch_input = "\n\n".join(text for _, text in batch)
        user_prompt = (
            f"Translate this Fluent (.ftl) block to {LOCALE_NAMES[target_locale]}:\n\n"
            f"```\n{batch_input}\n```\n\n"
            f"Output ONLY the translated FTL. No backticks, no commentary."
        )
        print(f"  Batch {i//BATCH + 1}/{(len(entries)+BATCH-1)//BATCH} "
              f"({len(batch)} entries)...", end=" ", flush=True)
        t0 = time.time()
        result = call_llm(key, system_prompt, user_prompt)
        if result is None:
            print("FAILED")
            # Fall back to source for this batch
            for _, raw in batch:
                out_lines.append(raw)
            continue
        # Strip code fences if model added them
        result = re.sub(r"^```\w*\n", "", result.strip())
        result = re.sub(r"\n```$", "", result.strip())
        out_lines.append(result.strip())
        print(f"OK ({time.time() - t0:.1f}s)")

    return "\n\n".join(out_lines) + "\n"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--target", required=True,
                   help="Single target locale (e.g. ms-MY) or 'all'")
    p.add_argument("--source", default=str(CATALOG_ROOT / "en-SG" / "main.ftl"))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    key = load_openrouter_key()
    print(f"OpenRouter key loaded (len={len(key)})")

    source_ftl = Path(args.source).read_text()
    print(f"Source: {args.source} ({len(source_ftl)} chars)")

    targets = list(LOCALE_NAMES.keys()) if args.target == "all" else [args.target]
    if args.target != "all" and args.target not in LOCALE_NAMES:
        print(f"Unknown locale: {args.target}. Options: {list(LOCALE_NAMES.keys())}")
        sys.exit(1)

    for target in targets:
        print(f"\n=== Translating to {target} ({LOCALE_NAMES[target]}) ===")
        target_dir = CATALOG_ROOT / target
        target_dir.mkdir(parents=True, exist_ok=True)
        out_path = target_dir / "main.ftl"
        # Backup existing
        if out_path.exists():
            backup = out_path.with_suffix(".ftl.preTranslate")
            out_path.replace(backup)
            print(f"  Backed up existing → {backup}")

        translated = translate_locale(key, target, source_ftl)

        if args.dry_run:
            print("\n--- Dry run output (first 30 lines) ---")
            print("\n".join(translated.splitlines()[:30]))
        else:
            out_path.write_text(translated)
            print(f"  ✓ Wrote {out_path} ({len(translated)} chars)")


if __name__ == "__main__":
    main()
