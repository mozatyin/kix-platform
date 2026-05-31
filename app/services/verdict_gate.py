"""Gap A — verdict-gate the generation pipeline (Wave M-3 / "fix the machine").

After ELTM produces a candidate game HTML, run it through N shop-owner
persona sims and ONLY ship if the verdict score is above the threshold.
Same pattern we use for landing pages (see scripts/sim_users_v2.py).

Architecture:
  generated_html + persona_set
    → for each persona: persona_evaluator(html) → VerdictScore(0-100, reasons)
    → aggregate scores
    → if avg >= threshold: ACCEPT; else: REJECT (return reasons for fixer)

The persona_evaluator is INJECTED — defaults to a deterministic stub for
unit tests; production callers wire in `sim_users_v2.simulate_v2` or
similar LLM-backed evaluator.

This lives in app/services/ (not inside ELTM) because the gate is a
KiX-side decision — KiX owns the merchant relationship and decides what
quality bar a game must clear before reaching the merchant.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Callable, Optional


# ── Score model ──

@dataclass
class VerdictScore:
    """A single persona's evaluation of a candidate game HTML."""
    persona_id: str
    score: float                           # 0.0 (won't play) – 100.0 (would pay for this)
    verdict_text: str                      # 1-3 sentence verbatim verdict
    reasons: list[str] = field(default_factory=list)   # what specifically worked or didn't
    would_recommend: bool = False

    def __post_init__(self):
        if not (0 <= self.score <= 100):
            raise ValueError(f"score must be 0-100, got {self.score}")


@dataclass
class GateDecision:
    """Aggregate decision returned by verdict_gate()."""
    accepted: bool
    avg_score: float
    min_score: float
    threshold_used: float
    persona_scores: list[VerdictScore] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)   # actionable for the fixer

    @property
    def num_personas(self) -> int:
        return len(self.persona_scores)

    @property
    def num_below_min(self) -> int:
        return sum(1 for v in self.persona_scores if v.score < self.threshold_used)


# ── Public API ──

PersonaEvaluator = Callable[[str, str], VerdictScore]
"""Signature: (game_html, persona_id) -> VerdictScore"""


def verdict_gate(
    game_html: str,
    persona_ids: list[str],
    evaluator: PersonaEvaluator,
    *,
    threshold: float = 60.0,
    min_score_floor: float = 30.0,
    require_majority_pass: bool = True,
) -> GateDecision:
    """Run game_html past each persona, aggregate, return ACCEPT/REJECT.

    Args:
      game_html: the candidate HTML output from ELTM
      persona_ids: list of persona keys (e.g. ['ahmad_kopi_chain', 'aminah'])
      evaluator: function(html, persona_id) -> VerdictScore (inject from caller)
      threshold: average score required to accept (default 60.0)
      min_score_floor: any single persona below this is an auto-reject
                      regardless of average (default 30.0 — prevents the
                      "great-on-average but breaks for one shop owner" trap)
      require_majority_pass: if True, ≥majority of personas must clear threshold

    Returns:
      GateDecision with accepted=True/False + per-persona scores + reasons
    """
    if not isinstance(game_html, str) or not game_html.strip():
        raise ValueError("game_html must be a non-empty string")
    if not isinstance(persona_ids, list) or not persona_ids:
        raise ValueError("persona_ids must be a non-empty list")
    if not callable(evaluator):
        raise TypeError("evaluator must be callable")

    scores: list[VerdictScore] = []
    for pid in persona_ids:
        try:
            score = evaluator(game_html, pid)
            if not isinstance(score, VerdictScore):
                raise TypeError(f"evaluator returned {type(score).__name__}, expected VerdictScore")
            scores.append(score)
        except Exception as e:
            # Fail-safe: a single evaluator error doesn't bring down the gate;
            # treat the persona's score as 0 with the error as the reason.
            scores.append(VerdictScore(
                persona_id=pid, score=0.0,
                verdict_text=f"[evaluator-error: {type(e).__name__}: {str(e)[:100]}]",
                reasons=[f"evaluator failed: {e}"],
            ))

    avg = statistics.mean(s.score for s in scores)
    min_s = min(s.score for s in scores)
    passing = sum(1 for s in scores if s.score >= threshold)
    majority_ok = passing >= (len(scores) + 1) // 2

    # Aggregate rejection reasons (deduped, ordered by frequency)
    reason_counts: dict[str, int] = {}
    for s in scores:
        if s.score < threshold:
            for r in s.reasons:
                reason_counts[r] = reason_counts.get(r, 0) + 1
    rejection_reasons = sorted(reason_counts.keys(),
                                key=lambda r: -reason_counts[r])[:8]

    accepted = (
        avg >= threshold
        and min_s >= min_score_floor
        and (not require_majority_pass or majority_ok)
    )

    return GateDecision(
        accepted=accepted,
        avg_score=round(avg, 1),
        min_score=round(min_s, 1),
        threshold_used=threshold,
        persona_scores=scores,
        rejection_reasons=rejection_reasons,
    )


# ── Deterministic stub evaluator (for unit tests + dev) ──

def stub_evaluator(game_html: str, persona_id: str) -> VerdictScore:
    """Pure deterministic stub. Score = 100 - 'TODO' count - 'broken' count.
    Useful for unit tests; production uses LLM-backed evaluator.
    """
    h = game_html.lower()
    score = 100.0
    reasons: list[str] = []
    if "todo" in h or "placeholder" in h:
        score -= 30
        reasons.append("placeholder text leaked into output (TODO / placeholder)")
    if "<error" in h or "uncaught" in h:
        score -= 40
        reasons.append("error markup in output")
    if len(game_html) < 500:
        score -= 35
        reasons.append("output too small to be a real game (<500 chars)")
    if "{{" in game_html and "}}" in game_html:
        score -= 25
        reasons.append("unfilled template placeholders ({{...}})")
    # Persona-specific tweak — different personas weight things differently
    if persona_id == "aminah_first_time_merchant" and len(game_html) > 50000:
        score -= 10
        reasons.append("page too heavy for first-time-merchant attention span")
    score = max(0.0, score)
    return VerdictScore(
        persona_id=persona_id, score=score,
        verdict_text=f"[stub] persona={persona_id} html_len={len(game_html)}",
        reasons=reasons,
        would_recommend=score >= 60,
    )


# ── LLM-backed evaluator factory (wires to scripts/sim_users_v2) ──

def make_llm_evaluator(persona_loader=None, llm_call=None) -> PersonaEvaluator:
    """Factory: produce a PersonaEvaluator that wraps an LLM call.

    Caller passes a `persona_loader(persona_id) -> dict(name, role, context)`
    and an `llm_call(system, user) -> {text, ok}` (matches the call_llm
    shape in scripts/sim_users_v2.py).

    Returns a function(html, persona_id) -> VerdictScore.

    If either dependency is None, returns the stub_evaluator (so callers can
    deploy this safely with no LLM configured — gate just no-ops to 'accept
    most things').
    """
    if persona_loader is None or llm_call is None:
        return stub_evaluator

    def _eval(game_html: str, persona_id: str) -> VerdictScore:
        try:
            persona = persona_loader(persona_id)
        except Exception as e:
            return VerdictScore(persona_id=persona_id, score=0.0,
                                verdict_text=f"[persona-load-failed: {e}]",
                                reasons=[f"unknown persona {persona_id}"])
        system = (
            "You are a strict shop-owner critic evaluating a generated mini-game "
            "before it ships to your customers. Return ONLY a JSON object with "
            "fields {score: 0-100, verdict: '1-2 sentences', reasons: [string]}."
        )
        user = (
            f"PERSONA: {persona.get('name','?')} — {persona.get('role','?')[:300]}\n\n"
            f"CONTEXT: {persona.get('context','')[:200]}\n\n"
            f"GAME HTML (first 6000 chars):\n```\n{game_html[:6000]}\n```\n\n"
            "Score 0 (won't ship this) – 100 (this is a hit). Be honest."
        )
        result = llm_call(system, user)
        if not result.get("ok"):
            return VerdictScore(persona_id=persona_id, score=0.0,
                                verdict_text="[llm-call-failed]",
                                reasons=[result.get("error", "llm error")])
        import json
        text = (result.get("text") or "").strip()
        try:
            # Strip code fences if present
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            data = json.loads(text)
            return VerdictScore(
                persona_id=persona_id,
                score=float(data.get("score", 0)),
                verdict_text=str(data.get("verdict", ""))[:500],
                reasons=list(data.get("reasons", []))[:10],
                would_recommend=float(data.get("score", 0)) >= 60,
            )
        except Exception as e:
            return VerdictScore(persona_id=persona_id, score=0.0,
                                verdict_text=f"[llm-parse-error: {e}]",
                                reasons=[f"llm returned non-JSON: {text[:120]}"])

    return _eval
