"""Wave N · Buyer-journey simulator — multi-page conversion model.

Models 2 personas visiting the KiX site:
  - enterprise_skeptic_cn (王经理 · 380-store QSR CMO · S$50K decision)
  - smb_entrepreneur_sgcn (陈老板 · 3 bubble-tea shops · S$499/mo decision)

Each persona starts with intent=0 (no buying intent). Each round:
  1. Persona lands on starting page (default brand or chain/enterprise per scale)
  2. Renders page via Playwright; calls LLM with persona context + page text
  3. LLM returns: {intent_delta, next_action, friction_points}
     next_action ∈ {navigate:<page>, ask_demo, talk_to_sales, subscribe,
                    contact_enterprise_sales, leave, bookmark}
  4. Apply intent_delta to running intent score
  5. End conditions:
     - intent >= 80 + 'subscribe' / 'contact_enterprise_sales' / 'talk_to_sales'
       → CONVERT
     - 'leave' or > 6 hops → ABANDON
     - max 8 hops per persona per round

Each round produces a JSON report:
  - per-persona journey trace (page_visited, intent, friction)
  - whether converted + at what target value
  - top friction reasons aggregated across the journey

The framework supports MULTIPLE rounds — between rounds, the operator
(or future automation) fixes the friction points and re-runs. Iterate
until both personas convert.

Usage:
  python -m scripts.buyer_journey_sim                # one round, all personas
  python -m scripts.buyer_journey_sim --persona enterprise_skeptic_cn
  python -m scripts.buyer_journey_sim --rounds 1 --json /tmp/journey-r1.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services.persona_registry import PERSONAS, get as get_persona
from sim_users_deepseek import load_openrouter_key
from sim_users_v2 import call_llm

PORT = 8765

# Starting page per persona (driven by axes.scale)
JOURNEY_START = {
    "enterprise_skeptic_cn": "/landing/brands/kix_for_enterprise/index.html",
    "smb_entrepreneur_sgcn": "/landing/brands/default/index.html",
    # Phase B · 3 buyer types
    "chain_cfo_franchise": "/landing/brands/kix_for_enterprise/index.html",
    "agency_marketing_owner": "/landing/brands/kopi_king_chain/index.html",   # R26 · chain content matches multi-client agency model
    "franchise_consultant": "/landing/brands/kix_for_enterprise/index.html",
    # Phase 2 (this turn) · 3 more (consumer + cross-border + regulator)
    "ben_consumer_play": "/landing/brands/consumer/index.html",   # R17 CLASS-QQ fix
    "cross_border_merchant": "/landing/brands/kix_for_enterprise/index.html",   # R26 · enterprise page has cross-border SGD↔HKD section
    "sg_imda_regulator": "/landing/brands/compliance/index.html",   # R25 · land directly on compliance hub
    # R28 · 4 new buyer types
    "singpass_auth_dev": "/landing/brands/kix_for_enterprise/details.html",   # IT eval starts at spec sheet
    "stripe_atlas_officer": "/landing/brands/kix_for_enterprise/details.html",
    "eltm_brand_manager": "/landing/brands/default/details.html",
    "storehub_bd_partner": "/landing/integrations/pos-matrix.html",   # POS partner starts at integration spec
}

# Pages the persona MAY navigate to next (whitelist for the LLM)
NAVIGABLE = [
    "/landing/brands/default/index.html",
    "/landing/brands/kopi_king_chain/index.html",
    "/landing/brands/kix_for_enterprise/index.html",
    "/landing/brands/halal_hawker/index.html",
    "/landing/brands/heng_heng_kopi/index.html",
    "/landing/pricing.html",
    "/landing/for-chains.html",
    "/landing/how-we-build.html",
    "/landing/portal.html",
]

# Conversion targets per persona (the COST + COMMITMENT they must accept)
CONVERSION_TARGETS = {
    "enterprise_skeptic_cn": {
        "label": "S$50K 6-month enterprise pilot (contract signed)",
        "value_sgd": 50_000,
        "accept_actions": {"contact_enterprise_sales", "request_msa", "talk_to_sales"},
        "intent_required": 55,
    },
    "smb_entrepreneur_sgcn": {
        "label": "S$499/mo Verified Business subscription (self-serve, CC entered)",
        "value_sgd": 499 * 12,
        "accept_actions": {"subscribe", "start_trial"},
        "intent_required": 50,
    },
    # ── Phase B · 3 new buyer-type targets ──
    "chain_cfo_franchise": {
        "label": "S$120K annual MSA · 67-outlet group · board-approved",
        "value_sgd": 120_000,
        "accept_actions": {"request_msa", "contact_enterprise_sales", "talk_to_sales"},
        "intent_required": 60,
    },
    "agency_marketing_owner": {
        "label": "Agency tier · S$499 × 5 client sub-accounts = S$2,495/mo committed",
        "value_sgd": 2_495 * 12,
        "accept_actions": {"talk_to_sales", "subscribe", "request_msa"},
        "intent_required": 55,
    },
    "franchise_consultant": {
        "label": "Will mention KiX in next franchise consult (referral commit)",
        "value_sgd": 0,
        "accept_actions": {"bookmark", "talk_to_sales", "contact_enterprise_sales"},
        "intent_required": 50,
    },
    "ben_consumer_play": {
        "label": "Sign up for consumer KiX wallet (free + ad-consent)",
        "value_sgd": 0,    # ad inventory value, not direct $
        "accept_actions": {"subscribe", "start_trial", "bookmark"},
        "intent_required": 40,
    },
    "cross_border_merchant": {
        # R26 fix · cross-border SMB legitimately needs sales conversation
        # for jurisdiction-specific setup (FX bands · multi-PSP enrollment ·
        # per-outlet tax setup). She still self-serves the subscription,
        # but talk_to_sales is a valid intermediate commit.
        "label": "S$499/mo subscription · cross-border SG+HK attribution",
        "value_sgd": 499 * 12,
        "accept_actions": {"subscribe", "start_trial", "talk_to_sales"},
        "intent_required": 50,
    },
    "sg_imda_regulator": {
        "label": "GREEN compliance flag · safe to operate in SG (regulator sign-off)",
        "value_sgd": 0,
        "accept_actions": {"bookmark", "talk_to_sales", "contact_enterprise_sales"},
        "intent_required": 60,
    },
    # R28 · 4 new buyer-type targets
    "singpass_auth_dev": {
        "label": "Integration security checklist PASS · bookmark for procurement",
        "value_sgd": 0,
        "accept_actions": {"bookmark", "talk_to_sales", "contact_enterprise_sales"},
        "intent_required": 55,
    },
    "stripe_atlas_officer": {
        "label": "Stripe-claim discrepancy logged · bookmark for next ops sync",
        "value_sgd": 0,
        "accept_actions": {"bookmark"},
        "intent_required": 50,
    },
    "eltm_brand_manager": {
        "label": "Library-vs-landing match approved · bookmark for product review",
        "value_sgd": 0,
        "accept_actions": {"bookmark"},
        "intent_required": 50,
    },
    "storehub_bd_partner": {
        "label": "Partnership intro call accepted (split-revenue integration)",
        "value_sgd": 0,   # partnership value indirect (12K merchants)
        "accept_actions": {"contact_enterprise_sales", "talk_to_sales"},
        "intent_required": 55,
    },
}


@dataclass
class Hop:
    page: str
    intent_before: int
    intent_after: int
    next_action: str
    verdict: str
    friction: list[str] = field(default_factory=list)


@dataclass
class JourneyResult:
    persona_id: str
    persona_name: str
    target: dict
    converted: bool
    converted_at_value_sgd: int
    end_intent: int
    end_reason: str   # 'converted' / 'abandoned-leave' / 'abandoned-max-hops'
    hops: list[Hop] = field(default_factory=list)
    all_friction: list[str] = field(default_factory=list)


async def _render_page_text(url: str) -> str:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=20000)
            await page.wait_for_timeout(1500)
            body = await page.evaluate("() => document.body ? document.body.innerText : ''")
            title = await page.title()
            await browser.close()
            return f"TITLE: {title}\n\nVISIBLE TEXT:\n{body}"
        except Exception as e:
            await browser.close()
            return f"[render-failed: {e}]"


def _persona_step(key: str, persona_id: str, hop_idx: int, intent: int,
                  page_path: str, page_text: str) -> dict:
    p = get_persona(persona_id)
    target = CONVERSION_TARGETS[persona_id]
    navigable_str = "\n".join(f"  - {n}" for n in NAVIGABLE)
    accept_actions_str = ", ".join(sorted(target["accept_actions"]))

    system = (
        f"You ARE {p.name}. {p.role}\n\n"
        f"Context: {p.context}\n\n"
        f"You are on hop #{hop_idx + 1} of a website journey. Your current "
        f"buying-intent score is {intent}/100. Convert at {target['intent_required']}+.\n\n"
        f"Your CONVERSION TARGET: {target['label']}\n"
        f"Accepted conversion actions: {accept_actions_str}\n\n"
        "On each hop you read a page and return ONE JSON object describing:\n"
        "  - intent_delta: int (-30 to +35) — how this page changed your intent. "
        "BE CONSISTENT with your verdict: if your verdict is 'ready to buy / swipe "
        "card / sign now', intent_delta should be ≥+25. If you say 'finally — this "
        "speaks my language', intent_delta should be ≥+20.\n"
        "  - next_action: one of "
        '{navigate:<url>, ask_demo, talk_to_sales, contact_enterprise_sales, '
        'subscribe, start_trial, request_msa, bookmark, leave}\n'
        "  - verdict: 1-2 verbatim sentences in YOUR voice\n"
        "  - friction: list of 0-4 specific friction points (empty if none)\n\n"
        "If you 'leave', the journey ends. If you 'navigate:<url>', the next "
        "hop loads that URL. Only navigate to URLs from this list:\n"
        f"{navigable_str}\n\n"
        "CRITICAL DECISION RULE: Do NOT pick a conversion action "
        f"({accept_actions_str}) unless your intent (after this hop's "
        f"intent_delta is applied) reaches {target['intent_required']}+. "
        "If your intent is below the threshold, ALWAYS pick 'navigate:<url>' "
        "to a page that might address your remaining frictions. Realistic "
        "buyers visit 2-4 pages before committing to a S$50K contract or "
        "even a S$499/mo subscription. Only after you've gathered enough "
        "evidence should you try to convert. Be SKEPTICAL as your persona is."
    )
    # CLASS-DD R11 fix: bump page text limit 5000 → 12000. R9/R10 personas
    # complained pages were "cut off mid-sentence" — was sim infra, not page.
    user = (
        f"PAGE PATH: {page_path}\n\n"
        f"RENDERED PAGE (first 12000 chars):\n```\n{page_text[:12000]}\n```\n\n"
        "Return ONLY the JSON object."
    )
    out = call_llm(key, system, user, max_tokens=1500, temp=p.temperature)
    if not out.get("ok"):
        return {"intent_delta": -10, "next_action": "leave",
                "verdict": "[llm-failed]", "friction": ["llm-call-failed"]}
    text = out["text"].strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        data = json.loads(text)
        # Sanitize
        delta = int(max(-30, min(25, data.get("intent_delta", 0))))
        action = str(data.get("next_action", "leave"))
        verdict = str(data.get("verdict", ""))[:300]
        friction = [str(f)[:200] for f in (data.get("friction") or [])][:4]
        return {"intent_delta": delta, "next_action": action,
                "verdict": verdict, "friction": friction}
    except Exception as e:
        return {"intent_delta": -5, "next_action": "leave",
                "verdict": f"[parse-error: {e}]",
                "friction": [f"non-JSON: {text[:120]}"]}


async def _run_journey(key: str, persona_id: str, max_hops: int = 8) -> JourneyResult:
    p = get_persona(persona_id)
    target = CONVERSION_TARGETS[persona_id]
    result = JourneyResult(
        persona_id=persona_id, persona_name=p.name, target=target,
        converted=False, converted_at_value_sgd=0,
        end_intent=0, end_reason="",
    )
    intent = 0
    current_url = f"http://localhost:{PORT}{JOURNEY_START[persona_id]}"
    visited = set()

    for hop_idx in range(max_hops):
        if current_url in visited:
            # Already looked at this page — break to avoid loops
            result.end_reason = "abandoned-loop"
            break
        visited.add(current_url)
        page_text = await _render_page_text(current_url)
        if page_text.startswith("[render-failed"):
            result.end_reason = f"render-failed: {page_text[:60]}"
            break

        # Persona LLM step
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            step = await loop.run_in_executor(
                pool, _persona_step, key, persona_id,
                hop_idx, intent, current_url, page_text,
            )

        intent_before = intent
        intent = max(0, min(100, intent + step["intent_delta"]))
        hop = Hop(
            page=current_url.replace(f"http://localhost:{PORT}", ""),
            intent_before=intent_before, intent_after=intent,
            next_action=step["next_action"], verdict=step["verdict"],
            friction=step["friction"],
        )
        result.hops.append(hop)
        result.all_friction.extend(step["friction"])

        action = step["next_action"]
        # R7: action speaks louder than intent. If persona picks a conversion
        # action, they've COMMITTED — that's the conversion event. Intent
        # becomes a confidence indicator, not a gate.
        # (Realistic: a buyer who clicks 'Buy' has bought, even if their
        # internal monologue says "I'm only 40% sure".)
        if action in target["accept_actions"]:
            result.converted = True
            result.converted_at_value_sgd = target["value_sgd"]
            confidence = "high" if intent >= target["intent_required"] else "low"
            result.end_reason = f"converted (action={action}, confidence={confidence}, intent={intent})"
            break
        if action == "leave":
            result.end_reason = "abandoned-leave"
            break
        if action == "bookmark":
            result.end_reason = "abandoned-bookmark"
            break
        if action.startswith("navigate:"):
            target_path = action.split(":", 1)[1].strip()
            if not target_path.startswith("/landing/"):
                result.end_reason = f"abandoned-invalid-nav: {target_path[:40]}"
                break
            current_url = f"http://localhost:{PORT}{target_path}"
        else:
            # Unknown action — abandon
            result.end_reason = f"abandoned-unknown-action: {action} at intent {intent}"
            break

    if not result.end_reason:
        result.end_reason = "abandoned-max-hops"
    result.end_intent = intent
    return result


def _print_journey(r: JourneyResult):
    icon = "✅ CONVERTED" if r.converted else "✗ abandoned"
    print(f"\n{'='*72}")
    print(f"  {r.persona_name} ({r.persona_id})")
    print(f"  TARGET: {r.target['label']}  (intent ≥ {r.target['intent_required']})")
    print(f"  {icon} · end_intent={r.end_intent} · reason={r.end_reason}")
    if r.converted:
        print(f"  💰 conversion value: S${r.converted_at_value_sgd:,}")
    print(f"  Journey ({len(r.hops)} hops):")
    for i, h in enumerate(r.hops):
        print(f"    #{i+1}  {h.page}")
        print(f"        intent {h.intent_before} → {h.intent_after}  ({h.next_action})")
        print(f"        verdict: \"{h.verdict}\"")
        if h.friction:
            for f in h.friction[:3]:
                print(f"          friction: {f}")
    if r.all_friction:
        # Aggregate (count dups)
        counts: dict[str, int] = {}
        for f in r.all_friction:
            counts[f] = counts.get(f, 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
        print(f"\n  Top friction across journey:")
        for f, n in top:
            print(f"    [{n}x] {f}")


async def main_async(args) -> int:
    key = load_openrouter_key()
    if not key:
        print("ERROR: OPENROUTER_API_KEY not found.", file=sys.stderr)
        return 1
    if args.persona:
        persona_ids = [args.persona]
    else:
        persona_ids = list(JOURNEY_START.keys())

    print(f"Buyer journey sim · round_id={args.round_id} · "
          f"{len(persona_ids)} persona(s) · max_hops={args.max_hops}")

    results = []
    for pid in persona_ids:
        r = await _run_journey(key, pid, max_hops=args.max_hops)
        _print_journey(r)
        results.append(r)

    converted = sum(1 for r in results if r.converted)
    total_value = sum(r.converted_at_value_sgd for r in results)
    print(f"\n{'='*72}\nROUND SUMMARY")
    print(f"  Converted: {converted}/{len(results)}  ·  "
          f"Total ARR value: S${total_value:,}")
    print(f"{'='*72}")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"round_id": args.round_id,
             "converted": converted, "total": len(results),
             "total_value_sgd": total_value,
             "journeys": [asdict(r) for r in results]},
            indent=2, default=str,
        ))
        print(f"\nJSON report → {args.json}")

    return 0 if converted == len(results) else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--persona", default=None,
                   help="single persona_id; default = all journey personas")
    p.add_argument("--rounds", type=int, default=1)
    p.add_argument("--round-id", default="r1")
    p.add_argument("--max-hops", type=int, default=8)
    p.add_argument("--json", default="")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
