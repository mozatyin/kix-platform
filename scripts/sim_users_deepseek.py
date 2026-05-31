"""DeepSeek-driven user simulation for KiX platform.

KiX 窗口双层模型 — 这是【模拟用户层】实现.

模型选择 (per window principles):
  Primary:  deepseek/deepseek-chat-v4 via OpenRouter (cheap, high concurrency)
  Fallback: anthropic/claude-sonnet-4-6 via OpenRouter (复杂任务)

NOT FOR system-layer use. System layer is Opus 4.7 Anthropic direct.

Usage:
  python -m scripts.sim_users_deepseek --persona shop_owner --page portal
  python -m scripts.sim_users_deepseek --persona consumer --page play
  python -m scripts.sim_users_deepseek --list-personas

Output: /Users/mozat/a-docs/deepseek-user-sim-{date}-{persona}.md
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

# ── Config ──────────────────────────────────────────────────────────────

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
ELTM_ENV_PATH = Path.home() / "eltm" / ".env"

DEEPSEEK_MODEL = "deepseek/deepseek-chat"  # v4 not listed; v3 closest
SONNET_FALLBACK_MODEL = "anthropic/claude-sonnet-4.5"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = Path("/Users/mozat/a-docs")
OUTPUT_DIR.mkdir(exist_ok=True)


# ── Key loading ─────────────────────────────────────────────────────────

def load_openrouter_key() -> str:
    """Resolve OpenRouter key — env first, then eltm/.env."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    # eltm/.env stores it under ANTHROPIC_API_KEY name (since they use OR
    # as Anthropic-compatible proxy)
    if ELTM_ENV_PATH.exists():
        for raw in ELTM_ENV_PATH.read_text().splitlines():
            if raw.startswith("ANTHROPIC_API_KEY=") and "sk-or-" in raw:
                return raw.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(
        "No OpenRouter key found. Set OPENROUTER_API_KEY or store "
        "`ANTHROPIC_API_KEY=sk-or-v1-...` in ~/eltm/.env"
    )


def check_balance(key: str) -> dict[str, float]:
    """Returns {remaining, used_monthly, limit, pct_used}."""
    r = httpx.get(
        f"{OPENROUTER_BASE}/auth/key",
        headers={"Authorization": f"Bearer {key}"},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json().get("data", {})
    limit = data.get("limit", 0) or 1
    used = data.get("usage_monthly", 0)
    return {
        "remaining": data.get("limit_remaining", 0),
        "used_monthly": used,
        "limit": limit,
        "pct_used": (used / limit) * 100 if limit else 0,
    }


# ── LLM Client (DeepSeek primary + Sonnet fallback) ────────────────────

def call_llm(
    key: str,
    system_prompt: str,
    user_prompt: str,
    *,
    model: str = DEEPSEEK_MODEL,
    max_tokens: int = 2000,
    temperature: float = 0.8,
) -> dict[str, Any]:
    """Single LLM call with model fallback. Returns {text, model_used, tokens_in, tokens_out, ok}."""
    for attempt_model in (model, SONNET_FALLBACK_MODEL):
        try:
            r = httpx.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://kix.app",
                    "X-Title": "KiX User Sim",
                },
                json={
                    "model": attempt_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=120,
            )
            if r.status_code == 200:
                data = r.json()
                return {
                    "ok": True,
                    "text": data["choices"][0]["message"]["content"],
                    "model_used": attempt_model,
                    "tokens_in": data.get("usage", {}).get("prompt_tokens", 0),
                    "tokens_out": data.get("usage", {}).get("completion_tokens", 0),
                }
            print(f"  ⚠ {attempt_model} → HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"  ⚠ {attempt_model} → {e}")
    return {"ok": False, "text": "", "model_used": None, "tokens_in": 0, "tokens_out": 0}


# ── Personas ────────────────────────────────────────────────────────────

PERSONAS = {
    "shop_owner": {
        "name": "小王 (Xiao Wang)",
        "role": "F&B shop owner in Bedok, Singapore. Runs 1 kopitiam stall (Toast Box style). 34 years old. Spent S$3K/month on Google Ads + TikTok Ads for past year — knows ad platforms intimately. Bilingual EN+ZH. No marketing team — manages everything himself. Looking for cheaper customer acquisition.",
        "context": "Just heard about KiX from a friend who said 'free 90-day trial + 100 merchants free per country'. Visiting their portal for the first time.",
    },
    "skeptical_owner": {
        "name": "Sarah Chen",
        "role": "Owns 3-location café chain in Singapore. 42 years old. Used Playable.com for 2 years on a S$15K/year contract — found it overpromised. Has hired marketing manager. Demands proof, asks hard questions about ROI.",
        "context": "Marketing manager dragged her to evaluate KiX. She's the decision-maker, deeply skeptical.",
    },
    "first_time_merchant": {
        "name": "Aminah Binti",
        "role": "Just opened her first nasi padang stall 6 months ago in Tampines. 28 years old. Never used any merchant SaaS. Has Instagram (3K followers) but doesn't know how to convert them to customers. Limited English.",
        "context": "A friend at the wet market mentioned KiX. She has 10 minutes between lunch rush.",
    },
    "consumer": {
        "name": "Ben Tan",
        "role": "35 year old IT engineer living in Bedok. Eats out 5x/week. Loves Toast Box, Ya Kun, hawker centers. Uses GrabFood + ShopBack + Burpple. Tech-savvy but impatient — abandons apps that take >30 seconds to figure out.",
        "context": "Saw a QR code at Toast Box that said 'Scan to play, win free coffee'. Curious enough to scan.",
    },
    "enterprise_manager": {
        "name": "Sandeep Kumar",
        "role": "Regional Loyalty Manager at Starbucks SG. 38 years old. Manages S$2M/year promotion budget. Reports to APAC marketing director. Buys from Salesforce, Klaviyo, Eber today. Need clear ROI metrics + enterprise contract terms.",
        "context": "KiX founder cold-emailed about a 'gamification pilot'. He's giving 15 minutes to look at the platform.",
    },
    "steve_jobs": {
        "name": "Steve Jobs (UX critic persona, 2026 if alive)",
        "role": (
            "World-class product critic in the Steve Jobs mold. You walk through "
            "a website looking for everything that is OK-but-not-great, anything "
            "that breaks the user's flow, every word that's vague when it should "
            "be specific, every visual choice that's lazy. You are NOT a customer — "
            "you are the founder's brutal honest friend doing a UX teardown. "
            "Your taste was forged at NeXT, Pixar, Apple. You measure everything "
            "against the principle: 'great design is a thousand polished details.' "
            "You catch: spacing inconsistencies, button hierarchy ambiguity, copy "
            "that says nothing concrete, weasel words ('basically', 'leverage', "
            "'best-in-class'), animation that delays without payoff, accessibility "
            "gaps (low contrast, tiny tap targets, missing alt), inconsistent "
            "iconography, missed opportunities to delight, anything that says "
            "'good enough' instead of 'this is the one'. You DO acknowledge what "
            "actually IS great — sparingly. You are not contrarian for sport; you "
            "are searching for the one thing that, if fixed, would transform the "
            "experience. You write in short sentences. You make lists. You don't "
            "soften."
        ),
        "context": (
            "You are doing a comprehensive UX teardown of KiX — a self-serve "
            "gamification platform for offline merchants (the product Michael is "
            "shipping). You will walk through every important page and write a "
            "single punch-list of every imperfection. The founder asked for this. "
            "He wants the brutal version, not the polite one. He'll fix them."
        ),
    },
    "ahmad_kopi_chain": {
        "name": "Ahmad bin Hashim (CEO, Kopi Senandung Sdn Bhd)",
        "role": (
            "Malaysian F&B entrepreneur. Owns Kopi Senandung — 100-outlet local "
            "coffee-shop chain across all 13 Malaysian states (KL, Penang, JB, Kuching, "
            "Kota Kinabalu, etc). 47 years old. Started with 1 kopitiam in Subang 2009, "
            "scaled to 100 outlets by 2024. RM 80M annual revenue. Reports to a 5-person "
            "board (himself + 2 investors + 2 family). Has a 12-person marketing team "
            "(2 TikTok specialists, 1 Meta lead, 2 designers, 1 data analyst, others). "
            "Personally runs RM 200K/month TikTok ad budget — knows TikTok Ads Manager "
            "intimately (UTM tracking, lookalike audiences, custom conversion events, "
            "Spark Ads, Shop Tab, TikTok Pixel events, Smart Performance Campaign, "
            "DPA, custom audiences from CRM upload, retargeting funnels). Was an early "
            "TikTok Malaysia partner — has direct WhatsApp to TikTok ID/SG sales rep. "
            "Bilingual Bahasa Melayu + English, conversational Mandarin. Sharp, "
            "data-driven, allergic to vendor BS. Asks about: (a) does it integrate "
            "with my TikTok Pixel? (b) what's the CAC on a coffee voucher game? "
            "(c) can I scale to 100 outlets in 1 deploy or one-by-one? (d) does it "
            "respect Malaysian PDPA + halal sensitivities? (e) Bahasa Melayu UI? "
            "(f) RM pricing not USD? (g) is the 100-free-per-country mechanic real?"
        ),
        "context": (
            "Heard about KiX from a Singapore-based F&B WhatsApp group. The pitch: "
            "'TikTok-style games for offline merchants, first 100 merchants per "
            "country get 0% take rate forever'. He's intrigued because his TikTok "
            "CAC has crept from RM 18 → RM 47 over 2 years. Visiting kix.app this "
            "morning between board prep meetings. Has ~30 minutes. If it impresses "
            "him, he'll commit a 5-outlet pilot within the week. If not, he'll "
            "close tab and forget it. He's evaluating as an OPERATOR running 100 "
            "real stores, not a curious tourist."
        ),
    },
}


def list_personas() -> None:
    print("Available personas:")
    for key, p in PERSONAS.items():
        print(f"\n  {key} — {p['name']}")
        print(f"    {p['role'][:120]}...")


# ── Page loaders ────────────────────────────────────────────────────────

PAGE_MAP = {
    "portal": PROJECT_ROOT / "landing" / "portal.html",
    "storefront": PROJECT_ROOT / "landing" / "storefront.html",
    "play": PROJECT_ROOT / "landing" / "play.html",
    "alpha": PROJECT_ROOT / "landing" / "alpha.html",
    "index": PROJECT_ROOT / "landing" / "index.html",
    "pricing": PROJECT_ROOT / "landing" / "pricing.html",
    "connect": PROJECT_ROOT / "landing" / "connect.html",
    "sg-case-studies": PROJECT_ROOT / "landing" / "sg-case-studies.html",
}


def load_page_summary(page_name: str, max_chars: int = 12000) -> str:
    """Return a condensed text description of the HTML page — strip script/style/CSS."""
    import re
    path = PAGE_MAP.get(page_name)
    if not path or not path.exists():
        return f"[page not found: {page_name}]"
    html = path.read_text()
    # Strip <script>...</script>, <style>...</style>
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style\b[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Strip HTML tags but keep text + visible attrs
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"\s+", " ", html).strip()
    if len(html) > max_chars:
        html = html[:max_chars] + "\n[... truncated]"
    return html


# ── Simulation ──────────────────────────────────────────────────────────

PROMPT_TEMPLATE = """You are roleplaying as **{name}**.

YOUR BACKGROUND: {role}

CURRENT SITUATION: {context}

The webpage you're looking at:
================
{page_text}
================

Walk through this page AS THIS PERSONA. Be honest and specific:

1. **First reaction** (3 seconds): What's your gut feeling? Familiar? Confusing? Trustworthy? Cheap?
2. **What you understand**: What does this platform actually do, in YOUR words?
3. **What you DON'T understand**: List things that confused you or felt off.
4. **What you'd do next**: Click? Close tab? Show a friend? Sign up?
5. **Specific friction**: 3-5 concrete things that would make you bounce or doubt.
6. **What would make you sign up TODAY**: 2-3 concrete asks.
7. **Final verdict** (1 sentence): Would you spend money / time here? Why?

Format: Use the 7 numbered sections above. Be CONCRETE not generic. Cite specific text from the page when relevant. Use your persona's natural tone (e.g., 小王 mixes 中文 + Singlish; Sandeep is formal English).
"""


def simulate(persona_key: str, page_name: str, key: str) -> dict[str, Any]:
    persona = PERSONAS[persona_key]
    page_text = load_page_summary(page_name)
    user_prompt = PROMPT_TEMPLATE.format(
        name=persona["name"],
        role=persona["role"],
        context=persona["context"],
        page_text=page_text,
    )
    print(f"\n→ Persona: {persona['name']}")
    print(f"→ Page: {page_name} ({len(page_text)} chars)")
    print(f"→ Calling DeepSeek...")

    result = call_llm(
        key,
        system_prompt="You are a realistic user simulator. Stay in character. Be specific and honest.",
        user_prompt=user_prompt,
    )

    if not result["ok"]:
        print("  ✗ LLM call failed")
        return result

    print(f"  ✓ {result['model_used']} ({result['tokens_in']}→{result['tokens_out']} tokens)")
    return result


# ── Output ──────────────────────────────────────────────────────────────

def write_report(persona_key: str, page_name: str, result: dict[str, Any]) -> Path:
    persona = PERSONAS[persona_key]
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = OUTPUT_DIR / f"deepseek-user-sim-{ts}-{persona_key}-{page_name}.md"
    out_path.write_text(
        f"""# DeepSeek User Simulation — {persona['name']} on /{page_name}

**Persona**: {persona_key}
**Role**: {persona['role']}
**Context**: {persona['context']}
**Model used**: {result.get('model_used', 'n/a')}
**Tokens**: {result.get('tokens_in', 0)} → {result.get('tokens_out', 0)}
**Timestamp**: {_dt.datetime.now().isoformat()}

---

## Persona's Walkthrough

{result.get('text', '[no response]')}

---
*Generated by `scripts/sim_users_deepseek.py`*
"""
    )
    return out_path


# ── CLI ─────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--persona", default="shop_owner", choices=list(PERSONAS.keys()))
    p.add_argument("--page", default="portal", choices=list(PAGE_MAP.keys()))
    p.add_argument("--list-personas", action="store_true")
    p.add_argument("--check-balance", action="store_true")
    args = p.parse_args()

    if args.list_personas:
        list_personas()
        return 0

    key = load_openrouter_key()

    if args.check_balance:
        bal = check_balance(key)
        print(json.dumps(bal, indent=2))
        return 0

    bal = check_balance(key)
    if bal["pct_used"] > 90:
        print(f"⚠ OpenRouter budget {bal['pct_used']:.1f}% used — pausing for safety")
        return 1
    print(f"OpenRouter budget: {bal['pct_used']:.1f}% used (${bal['remaining']:.0f} left)")

    result = simulate(args.persona, args.page, key)
    out_path = write_report(args.persona, args.page, result)
    print(f"\n✓ Report: {out_path}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
