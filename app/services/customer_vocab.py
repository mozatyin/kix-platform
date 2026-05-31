"""CLASS-D structural fix — customer vocabulary gate.

Internal team vocabulary (Trinity 3T, PDCA, ELTM, WAFL, enterprise) keeps
leaking onto customer landing pages because hand-edits don't have a
language firewall.

This module:
  1. Holds the canonical FORBIDDEN word set (internal-only terms).
  2. Holds PREFERRED replacements ("enterprise" → "for chains").
  3. Provides `vocab_check(html)` which raises on any forbidden hit.
  4. Provides `suggest(text)` which returns a rewritten string for
     pre-LLM seed copy.

Call site: landing_gen.generate_landing() runs the rendered HTML through
`vocab_check()` BEFORE returning. Raising fails-closed, so the gate
cannot be bypassed by a generator caller.

Founder-cited examples (preserved verbatim from conversation history):
  - "Trinity 3T 是我们内部的使用方法，我们不应该跟客户介绍这件事"
  - "enterprise 这个事情，我们感觉定位有点模糊"
  - "PDCA" / "WAFL" / "Pre-process pipeline" → internal lingo
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# Words that MUST NOT appear in customer-visible output.
# Case-insensitive match. Word-boundary enforced to avoid false positives
# (e.g. "enterprise" matches but "enterprises" should also — we use \b).
FORBIDDEN: frozenset[str] = frozenset({
    "trinity 3t",
    "trinity protocol",
    "trinity-iterated",
    "pdca",
    "eltm",
    "wafl",
    "pre-process pipeline",
    "soul-graph",
    "brick library",
    "trisoul",
    "soulmate",
    "code-soul",
    "pm-soul",
})


# When generating seed copy or rewriting hand-drafted text, replace
# internal terms with customer-facing equivalents BEFORE LLM expansion.
# Note: "enterprise" is allowed as a plain noun in some contexts (legal
# entity names, sector terms), so it is NOT in FORBIDDEN — but the
# PREFERRED dict steers seed copy toward "for chains" instead.
PREFERRED: dict[str, str] = {
    "enterprise customer": "chain owner",
    "enterprise tier": "chains tier",
    "for enterprise": "for chains",
    "enterprise sales": "chain partnerships",
    "trinity 3t": "how we build",
    "trinity protocol": "how we build",
    "trinity-iterated": "iteratively improved",
    "pdca cycle": "rapid iteration loop",
    "brick library": "game template library",
    "soul-graph": "personalization model",
}


@dataclass
class VocabHit:
    word: str
    position: int
    context: str   # ~40 chars around the hit


class VocabViolation(ValueError):
    """Raised when forbidden words appear in customer-visible output."""

    def __init__(self, hits: list[VocabHit]):
        self.hits = hits
        words = ", ".join(sorted({h.word for h in hits}))
        msg = (
            f"customer_vocab gate REJECTED output — forbidden terms found: {words}\n"
            f"  {len(hits)} hit(s):\n"
        )
        for h in hits[:5]:
            msg += f"    @{h.position}: '{h.word}' in '...{h.context}...'\n"
        if len(hits) > 5:
            msg += f"    ... +{len(hits)-5} more\n"
        msg += (
            "  Fix the seed copy (BrandConfig fields) or use PREFERRED replacements. "
            "Do NOT lower the gate."
        )
        super().__init__(msg)


def find_forbidden(text: str) -> list[VocabHit]:
    """Return all forbidden-word hits (empty list if clean)."""
    if not text:
        return []
    hits: list[VocabHit] = []
    lower = text.lower()
    for word in FORBIDDEN:
        # Word-boundary regex; handle multi-word phrases too.
        pattern = r"\b" + re.escape(word) + r"\b"
        for m in re.finditer(pattern, lower):
            start = max(0, m.start() - 20)
            end = min(len(text), m.end() + 20)
            ctx = text[start:end].replace("\n", " ")
            hits.append(VocabHit(word=word, position=m.start(), context=ctx))
    return hits


def vocab_check(text: str) -> None:
    """Raise VocabViolation if text contains any forbidden word."""
    hits = find_forbidden(text)
    if hits:
        raise VocabViolation(hits)


def is_clean(text: str) -> bool:
    """Boolean check — for callers that want to branch, not raise."""
    return not find_forbidden(text)


def suggest(text: str) -> str:
    """Apply PREFERRED replacements (case-insensitive) and return rewritten string.
    Does NOT raise. Useful for pre-LLM seed copy.
    """
    if not text:
        return text
    out = text
    for old, new in PREFERRED.items():
        pattern = re.compile(r"\b" + re.escape(old) + r"\b", re.IGNORECASE)
        out = pattern.sub(new, out)
    return out
