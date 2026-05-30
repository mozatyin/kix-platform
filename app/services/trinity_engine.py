"""Trinity 3T Iteration Engine.

This is **meta-infrastructure**, not a product feature. It institutionalises
the manual Trinity Protocol cycle (Industry × Academic × Reality) that was
hand-run for 5 rounds with the 小王 (shop-owner) persona, so any team can
spin up a multi-round audit on any artifact with any persona and converge
to a verdict without bespoke orchestration.

The engine's contract
---------------------
A ``TrinityIteration`` is one stateful run identified by ``iteration_id``.
Each ``round()`` call executes the canonical Trinity pipeline against the
artifact:

1. **Persona walk**         — the persona "uses" the artifact and emits
                              concerns from its lens (small-business
                              merchant cares about ROI; ops cares about
                              click depth; consumer cares about clarity).
2. **Industry comparison**  — diff the artifact behaviour against named
                              industry baselines (Google Ads, TikTok Ads
                              Manager, Stripe Dashboard, ...).
3. **Academic check**       — Nielsen heuristics + Jakob's Law + the
                              persona's domain literature (Octalysis for
                              gamification, etc.).
4. **Reality dump**         — codebase grep so a complaint pointing at
                              "FAQ link missing" actually checks the
                              repo before being filed.
5. **Synthesize**           — merge into a single complaint list with
                              the canonical schema (see
                              ``ComplaintSchema``).
6. **Categorise**           — assign P0/P1/P2 severity and a category
                              tag (visual / IA / terminology / workflow
                              / pricing / data / trust).
7. **Verdict**              — persona returns a 0-10 quality score and a
                              short headline ("S$5K yes" / "needs work").

Persistence
-----------
State lives in Redis (durable enough for engine runs, no PG migrations
needed). The keys are documented inline on ``_iter_key`` /
``_round_key`` / ``_complaints_key`` so an operator can `redis-cli`
inspect a stuck run.

Convergence
-----------
Two stop conditions, whichever fires first:
* ≤3 *new* P0/P1 complaints in two consecutive rounds, OR
* persona verdict ≥ ``target_quality``.

Both protect against runaway iteration burning the LLM budget.

LLM integration
---------------
Persona simulation calls ``wait_if_paused()`` so a busy quota guard
auto-throttles us. The actual LLM call is behind a single hook
(``_persona_walk_llm``) — tests stub it via a deterministic walker so
the suite runs hermetically.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from app.api_standards import mint_id
from app.redis_client import get_redis

logger = logging.getLogger(__name__)


# ── Severity & category enums ────────────────────────────────────────────


class Severity(str, Enum):
    """Complaint severity. ``P0`` = blocker, ``P1`` = serious, ``P2`` = nit."""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


# Category tags used across audits — keep small, stable, surveyable.
KNOWN_CATEGORIES: frozenset[str] = frozenset(
    {
        "visual",
        "ia",            # information architecture / nav
        "terminology",
        "workflow",
        "pricing",
        "data",
        "trust",
        "performance",
        "accessibility",
        "content",
    }
)


# ── Complaint schema ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Complaint:
    """Canonical complaint record. Stable schema for cross-round dedup +
    downstream auto-fix dispatch.

    A complaint is identified by ``fingerprint`` — a hash over
    (category, persona_concern, expected, got) so the same issue raised
    in round 1 and round 3 dedupes to one record with ``occurrences=2``
    instead of bloating the list.
    """

    severity: Severity
    category: str
    persona_concern: str
    expected: str
    got: str
    fix_estimate_hours: int = 1
    fingerprint: str = ""
    occurrences: int = 1
    first_seen_round: int = 1
    last_seen_round: int = 1

    def __post_init__(self) -> None:
        if not self.fingerprint:
            # frozen=True — bypass via object.__setattr__.
            object.__setattr__(self, "fingerprint", self.compute_fingerprint())

    def compute_fingerprint(self) -> str:
        """Stable identifier for cross-round dedup."""
        h = hashlib.sha256()
        h.update(self.category.encode())
        h.update(b"|")
        # Lowercase + collapse whitespace so cosmetic differences don't
        # produce a "new" complaint.
        for s in (self.persona_concern, self.expected, self.got):
            h.update(re.sub(r"\s+", " ", s.lower().strip()).encode())
            h.update(b"|")
        return h.hexdigest()[:16]

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Complaint":
        d = dict(d)
        sev = d.pop("severity")
        return cls(severity=Severity(sev), **d)


# ── Persona definitions ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Persona:
    """A persona who walks the artifact and emits complaints.

    ``focus`` and ``red_flags`` are the deterministic backbone the
    walker uses when no LLM is available (or in tests); the LLM hook
    layers richer context on top.
    """

    slug: str
    label: str
    description: str
    industry_baselines: tuple[str, ...]
    focus: tuple[str, ...]
    red_flags: tuple[str, ...]
    verdict_phrase_good: str = "yes"
    verdict_phrase_bad: str = "needs work"

    def render_verdict(self, score: int) -> str:
        return self.verdict_phrase_good if score >= 7 else self.verdict_phrase_bad


def ShopOwnerPersona() -> Persona:
    """Small-business merchant — the 小王 archetype.

    Cares about: ROI / "will this make me money" / pricing clarity /
    how-to-start friction / does it work on my phone.
    """

    return Persona(
        slug="shop-owner",
        label="Small Business Shop Owner",
        description="Singapore F&B / retail merchant, S$5–10K/mo marketing budget",
        industry_baselines=("Google Ads", "Meta Ads", "TikTok Ads", "Shopify"),
        focus=("pricing", "roi", "onboarding", "support", "mobile"),
        red_flags=(
            "unclear price",
            "no faq",
            "english only",
            "no chat",
            "complex jargon",
            "no demo",
            "no refund",
        ),
        verdict_phrase_good="S$5K yes",
        verdict_phrase_bad="not paying S$5K",
    )


def MarketingAgencyPersona() -> Persona:
    """Ad professional — agency planner / media buyer."""

    return Persona(
        slug="marketing-agency",
        label="Marketing Agency Planner",
        description="Mid-tier APAC agency, manages multiple brand accounts",
        industry_baselines=("Google Ads", "DV360", "The Trade Desk", "Meta Ads"),
        focus=("campaigns", "attribution", "creative", "audiences", "reporting"),
        red_flags=(
            "no bulk edit",
            "no attribution",
            "no export",
            "shallow targeting",
            "no api",
            "no multi-account",
        ),
    )


def ConsumerPersona() -> Persona:
    """End user — the person actually seeing the gamified offer."""

    return Persona(
        slug="consumer",
        label="End Consumer",
        description="Mobile-first user, low patience, expects iOS-grade polish",
        industry_baselines=("Starbucks Rewards", "Shopee", "Duolingo", "TikTok"),
        focus=("speed", "clarity", "reward", "trust", "fun"),
        red_flags=(
            "slow load",
            "confusing reward",
            "no progress",
            "broken animation",
            "creepy data",
        ),
    )


def AdminPersona() -> Persona:
    """KiX ops / customer success — the internal operator."""

    return Persona(
        slug="admin",
        label="KiX Ops Admin",
        description="Internal cohort/ops manager, runs alpha programmes daily",
        industry_baselines=("Stripe Dashboard", "Linear", "Notion", "PagerDuty"),
        focus=("cohort", "audit", "ops", "support", "incident"),
        red_flags=(
            "no audit log",
            "no bulk action",
            "no search",
            "no export",
            "no rbac",
        ),
    )


def InvestorPersona() -> Persona:
    """Diligence reviewer — VC associate / strategic acquirer."""

    return Persona(
        slug="investor",
        label="Investor / Diligence Reviewer",
        description="Series-B VC associate doing platform diligence",
        industry_baselines=("Bunchball", "Centrical", "Trophy.so", "Smartico"),
        focus=("moat", "scalability", "unit-economics", "team", "competitive"),
        red_flags=(
            "no moat",
            "no metrics",
            "no traction",
            "no team",
            "tam unclear",
        ),
    )


PERSONA_REGISTRY: dict[str, Callable[[], Persona]] = {
    "shop-owner": ShopOwnerPersona,
    "marketing-agency": MarketingAgencyPersona,
    "consumer": ConsumerPersona,
    "admin": AdminPersona,
    "investor": InvestorPersona,
}


def get_persona(slug: str) -> Persona:
    if slug not in PERSONA_REGISTRY:
        raise ValueError(
            f"unknown persona {slug!r}; known: {sorted(PERSONA_REGISTRY)}"
        )
    return PERSONA_REGISTRY[slug]()


# ── Round result ─────────────────────────────────────────────────────────


@dataclass
class RoundResult:
    """The output of one round of the engine."""

    round_number: int
    complaints: list[Complaint]
    verdict_score: int   # 0-10
    verdict_headline: str
    persona_slug: str
    artifact_path: str
    timestamp: float
    new_complaint_count: int   # how many were not seen in earlier rounds
    industry_gaps: list[str] = field(default_factory=list)
    academic_gaps: list[str] = field(default_factory=list)
    reality_findings: list[str] = field(default_factory=list)

    def p0_count(self) -> int:
        return sum(1 for c in self.complaints if c.severity is Severity.P0)

    def p1_count(self) -> int:
        return sum(1 for c in self.complaints if c.severity is Severity.P1)

    def to_json(self) -> dict[str, Any]:
        return {
            "round_number": self.round_number,
            "complaints": [c.to_json() for c in self.complaints],
            "verdict_score": self.verdict_score,
            "verdict_headline": self.verdict_headline,
            "persona_slug": self.persona_slug,
            "artifact_path": self.artifact_path,
            "timestamp": self.timestamp,
            "new_complaint_count": self.new_complaint_count,
            "industry_gaps": self.industry_gaps,
            "academic_gaps": self.academic_gaps,
            "reality_findings": self.reality_findings,
        }


# ── Reality (codebase grep) helpers ──────────────────────────────────────


def _read_artifact(artifact_path: str) -> str:
    """Read the artifact file. Returns '' if missing — engine still runs."""
    try:
        return Path(artifact_path).read_text(encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        logger.warning("trinity: artifact %r not found, running with empty body", artifact_path)
        return ""
    except IsADirectoryError:
        # Allow directory artifacts (e.g. SDK folder) — concatenate top-level files.
        p = Path(artifact_path)
        chunks: list[str] = []
        for f in sorted(p.glob("*.html")) + sorted(p.glob("*.js"))[:20]:
            try:
                chunks.append(f.read_text(encoding="utf-8", errors="ignore"))
            except Exception:  # noqa: BLE001
                continue
        return "\n".join(chunks)


# Phrase-form red flags map to the *positive* affordances that clear them.
# "unclear price" doesn't mean "look for the word 'unclear'" — it means
# "the price story should be transparent". Map such phrases explicitly so
# the deterministic walker matches operator intent.
_PHRASE_NEEDLES: dict[str, tuple[str, ...]] = {
    "unclear price": ("price", "pricing"),
    "complex jargon": ("simple", "plain"),
    "english only": ("chinese", "mandarin", "中文", "multilingual"),
    "shallow targeting": ("audience", "segment", "targeting"),
    "creepy data": ("privacy", "consent", "gdpr"),
}


def _flag_needles(flag: str) -> list[str]:
    """Return the keyword tokens we look for in the body to *clear* this flag.

    "no faq"            → ["faq"]
    "english only"      → ["chinese", "mandarin", ...]   (explicit map)
    "complex jargon"    → ["simple", "plain"]            (explicit map)
    """
    if flag in _PHRASE_NEEDLES:
        return list(_PHRASE_NEEDLES[flag])
    if flag.startswith("no "):
        token = flag[3:]
        return [w for w in token.split() if w]
    # Generic two-word phrase fallback: use the noun (last word).
    parts = [w for w in flag.split() if w and w != "only"]
    return parts[-1:] if parts else []


def _flag_satisfied(body_low: str, flag: str) -> bool:
    """True iff at least one needle for this flag is present in the body."""
    needles = _flag_needles(flag)
    return any(n in body_low for n in needles) if needles else True


def _reality_grep(body: str, red_flags: Sequence[str]) -> list[str]:
    """Deterministic body grep — find which red-flag tokens are *missing*.

    A red flag like "no faq" means: the body should mention "faq" somewhere.
    If it doesn't, that's a real-reality finding.
    """
    found: list[str] = []
    low = body.lower()
    for flag in red_flags:
        if _flag_satisfied(low, flag):
            continue
        needles = _flag_needles(flag)
        if not needles:
            continue
        found.append(f"missing keyword {needles[0]!r} in artifact ({flag})")
    return found


# ── Persona walk (LLM hook) ──────────────────────────────────────────────


# Type alias for the persona-walk hook. Tests / scripts stub this.
PersonaWalkFn = Callable[[Persona, str, str], Awaitable[list[Complaint]]]


async def _persona_walk_default(
    persona: Persona, artifact_path: str, artifact_body: str
) -> list[Complaint]:
    """Deterministic default walker — runs when no LLM hook is registered.

    The walker emits one complaint per *missing* red-flag keyword, mapped
    to a severity by the keyword's position in ``persona.red_flags``
    (earlier = more important = P0).

    This is **good enough for the engine to converge in tests** and gives
    a real baseline finding set in production until the LLM hook lands.
    """

    body_low = artifact_body.lower()
    complaints: list[Complaint] = []
    for idx, flag in enumerate(persona.red_flags):
        if _flag_satisfied(body_low, flag):
            continue
        needles = _flag_needles(flag)
        if not needles:
            continue
        token = flag[3:] if flag.startswith("no ") else flag
        severity = (
            Severity.P0 if idx < 2
            else Severity.P1 if idx < 4
            else Severity.P2
        )
        category = _infer_category(flag)
        complaints.append(
            Complaint(
                severity=severity,
                category=category,
                persona_concern=f"{persona.label} cares about {token}",
                expected=f"artifact addresses {token}",
                got=f"no mention of {needles[0]} in {artifact_path}",
                fix_estimate_hours=2 if severity is Severity.P0 else 1,
            )
        )
    return complaints


_PERSONA_WALK_HOOK: PersonaWalkFn = _persona_walk_default


def register_persona_walk_hook(fn: PersonaWalkFn) -> None:
    """Swap the persona-walk implementation (e.g. real LLM call).

    Production wires an LLM-backed walker here; tests can override to a
    deterministic stub. The hook receives ``(persona, artifact_path,
    artifact_body)`` and must return a list of ``Complaint``.
    """

    global _PERSONA_WALK_HOOK
    _PERSONA_WALK_HOOK = fn


def reset_persona_walk_hook() -> None:
    """Restore the deterministic default walker (used by test teardown)."""

    global _PERSONA_WALK_HOOK
    _PERSONA_WALK_HOOK = _persona_walk_default


def _infer_category(flag: str) -> str:
    f = flag.lower()
    if "price" in f or "refund" in f:
        return "pricing"
    if "faq" in f or "chat" in f or "support" in f or "trust" in f:
        return "trust"
    if "english" in f or "jargon" in f or "creepy" in f:
        return "content"
    if "audit" in f or "rbac" in f or "export" in f or "bulk" in f:
        return "workflow"
    if "load" in f or "slow" in f or "broken" in f:
        return "performance"
    if "attribution" in f or "api" in f or "multi" in f:
        return "ia"
    return "workflow"


# ── Industry & Academic checks ───────────────────────────────────────────


# Nielsen's 10 heuristics — abbreviated keys used to flag academic gaps.
NIELSEN_HEURISTICS = (
    "visibility-of-system-status",
    "match-system-real-world",
    "user-control-freedom",
    "consistency-standards",
    "error-prevention",
    "recognition-not-recall",
    "flexibility-efficiency",
    "aesthetic-minimalist",
    "help-recover-errors",
    "help-documentation",
)


def _industry_gaps(persona: Persona, body: str) -> list[str]:
    """Cheap heuristic: each baseline tool maps to one expected affordance.

    If the artifact body lacks the affordance keyword the baseline is famous
    for, flag it. This is intentionally simple — the LLM hook can replace
    with a real comparison.
    """

    expectations = {
        "Google Ads": "campaign",
        "Meta Ads": "audience",
        "TikTok Ads": "creative",
        "Shopify": "checkout",
        "DV360": "deal id",
        "The Trade Desk": "bid",
        "Starbucks Rewards": "tier",
        "Shopee": "voucher",
        "Duolingo": "streak",
        "TikTok": "feed",
        "Stripe Dashboard": "payout",
        "Linear": "issue",
        "Notion": "doc",
        "PagerDuty": "incident",
        "Bunchball": "badge",
        "Centrical": "mission",
        "Trophy.so": "sdk",
        "Smartico": "wheel",
    }
    low = body.lower()
    gaps: list[str] = []
    for tool in persona.industry_baselines:
        kw = expectations.get(tool)
        if kw and kw not in low:
            gaps.append(f"{tool} expects {kw!r} affordance — not found")
    return gaps


def _academic_gaps(persona: Persona, body: str) -> list[str]:
    """Lightweight Nielsen / Jakob's Law check.

    Looks for the absence of canonical UX affordances tied to each
    heuristic. The full LLM walker can produce richer analysis; this
    layer just guarantees we never ship an audit with **zero** academic
    grounding.
    """

    low = body.lower()
    gaps: list[str] = []
    checks = [
        ("visibility-of-system-status", ("status", "progress", "loading")),
        ("match-system-real-world", ("price", "tier")),
        ("user-control-freedom", ("undo", "cancel", "back")),
        ("error-prevention", ("confirm", "are you sure", "warning")),
        ("recognition-not-recall", ("menu", "nav", "search")),
        ("help-documentation", ("help", "faq", "docs", "support")),
    ]
    for heuristic, tokens in checks:
        if not any(t in low for t in tokens):
            gaps.append(f"Nielsen[{heuristic}] — no token from {list(tokens)} in artifact")
    return gaps


# ── Iteration state (Redis-backed) ───────────────────────────────────────


def _iter_key(iteration_id: str) -> str:
    """``trinity:iteration:{id}`` HASH — iteration metadata + status."""
    return f"trinity:iteration:{iteration_id}"


def _round_key(iteration_id: str, n: int) -> str:
    """``trinity:iteration:{id}:round:{n}`` STRING (JSON) — one round result."""
    return f"trinity:iteration:{iteration_id}:round:{n}"


def _complaints_key(iteration_id: str) -> str:
    """``trinity:iteration:{id}:complaints`` HASH fingerprint → JSON complaint."""
    return f"trinity:iteration:{iteration_id}:complaints"


def _index_key() -> str:
    """``trinity:iterations`` ZSET (score=last_update_ts) — leaderboard."""
    return "trinity:iterations"


# ── The engine ───────────────────────────────────────────────────────────


@dataclass
class TrinityIteration:
    """A long-running Trinity audit. Stateful, resumable, Redis-backed.

    Lifecycle::

        it = await TrinityIteration.create(persona="shop-owner",
                                            artifact_path="landing/portal.html",
                                            target_quality=7)
        while not await it.has_converged():
            await it.round()
        verdict = await it.final_verdict()
    """

    iteration_id: str
    persona: Persona
    artifact_path: str
    target_quality: int = 7
    industry_baseline: list[str] = field(default_factory=list)
    rounds_executed: int = 0
    max_rounds: int = 10
    _converged: bool = False
    _consecutive_quiet_rounds: int = 0   # round streak with ≤3 new P0/P1

    # ── construction / resume ────────────────────────────────────────

    @classmethod
    async def create(
        cls,
        *,
        persona: str,
        artifact_path: str,
        target_quality: int = 7,
        max_rounds: int = 10,
        iteration_id: str | None = None,
    ) -> "TrinityIteration":
        """Mint a fresh iteration and persist initial metadata to Redis."""
        p = get_persona(persona)
        iid = iteration_id or mint_id("trin")
        it = cls(
            iteration_id=iid,
            persona=p,
            artifact_path=artifact_path,
            target_quality=target_quality,
            industry_baseline=list(p.industry_baselines),
            max_rounds=max_rounds,
        )
        await it._persist_metadata()
        return it

    @classmethod
    async def resume(cls, iteration_id: str) -> "TrinityIteration":
        """Reload an iteration from Redis — supports crash-resume."""
        r = await get_redis()
        meta = await r.hgetall(_iter_key(iteration_id))
        if not meta:
            raise KeyError(f"iteration {iteration_id} not found")
        # redis returns bytes by default in some clients — be defensive.
        def _s(v: Any) -> str:
            return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
        persona = get_persona(_s(meta[b"persona"] if b"persona" in meta else meta["persona"]))
        return cls(
            iteration_id=iteration_id,
            persona=persona,
            artifact_path=_s(meta.get(b"artifact_path", meta.get("artifact_path", ""))),
            target_quality=int(_s(meta.get(b"target_quality", meta.get("target_quality", 7)))),
            industry_baseline=list(persona.industry_baselines),
            rounds_executed=int(_s(meta.get(b"rounds_executed", meta.get("rounds_executed", 0)))),
            max_rounds=int(_s(meta.get(b"max_rounds", meta.get("max_rounds", 10)))),
            _converged=bool(int(_s(meta.get(b"converged", meta.get("converged", 0))))),
        )

    async def _persist_metadata(self) -> None:
        r = await get_redis()
        await r.hset(
            _iter_key(self.iteration_id),
            mapping={
                "persona": self.persona.slug,
                "artifact_path": self.artifact_path,
                "target_quality": str(self.target_quality),
                "max_rounds": str(self.max_rounds),
                "rounds_executed": str(self.rounds_executed),
                "converged": "1" if self._converged else "0",
                "updated_at": str(int(time.time())),
            },
        )
        await r.zadd(_index_key(), {self.iteration_id: time.time()})

    # ── round execution ──────────────────────────────────────────────

    async def round(self, prior_complaints: list[Complaint] | None = None) -> RoundResult:
        """Run one full Trinity round and persist its result.

        Steps mirror the docstring at module top. ``prior_complaints`` is
        optional — the engine pulls them from Redis itself when not
        supplied (so callers don't need to bookkeep).
        """

        if self._converged:
            raise RuntimeError("iteration already converged; resume disallowed")
        if self.rounds_executed >= self.max_rounds:
            raise RuntimeError(f"max_rounds={self.max_rounds} exhausted")

        # LLM quota guard. NOOPs in test environments (Redis pause flag never set).
        try:
            from scripts.llm_quota_monitor import wait_if_paused
            await wait_if_paused(max_wait_seconds=600)
        except Exception:  # pragma: no cover — best-effort guard
            pass

        n = self.rounds_executed + 1
        body = _read_artifact(self.artifact_path)

        # 1-2-3. Persona walk + Industry + Academic + Reality.
        persona_complaints = await _PERSONA_WALK_HOOK(self.persona, self.artifact_path, body)
        industry_gaps = _industry_gaps(self.persona, body)
        academic_gaps = _academic_gaps(self.persona, body)
        reality_findings = _reality_grep(body, self.persona.red_flags)

        # 4. Synthesize — merge industry/academic gaps in as P2 complaints
        # so they're tracked under one schema (not separate "gap" buckets).
        for g in industry_gaps:
            persona_complaints.append(
                Complaint(
                    severity=Severity.P1,
                    category="ia",
                    persona_concern=f"{self.persona.label} expects industry parity",
                    expected=g,
                    got="affordance absent",
                )
            )
        for g in academic_gaps:
            persona_complaints.append(
                Complaint(
                    severity=Severity.P2,
                    category="ia",
                    persona_concern="UX heuristic violation",
                    expected=g,
                    got="no matching token in artifact",
                )
            )

        # 5. Dedup against prior complaints — update occurrences in place.
        prior = await self._load_complaints() if prior_complaints is None else prior_complaints
        merged, new_count = self._merge_complaints(prior, persona_complaints, current_round=n)

        # 6. Escalate severity if same complaint repeats ≥3 rounds.
        merged = [self._escalate_if_repeated(c) for c in merged]

        # 7. Verdict — score is monotonic in (10 - P0*2 - P1*1) clipped.
        score = max(
            0,
            10 - 2 * sum(1 for c in merged if c.severity is Severity.P0)
            - sum(1 for c in merged if c.severity is Severity.P1),
        )
        result = RoundResult(
            round_number=n,
            complaints=merged,
            verdict_score=score,
            verdict_headline=self.persona.render_verdict(score),
            persona_slug=self.persona.slug,
            artifact_path=self.artifact_path,
            timestamp=time.time(),
            new_complaint_count=new_count,
            industry_gaps=industry_gaps,
            academic_gaps=academic_gaps,
            reality_findings=reality_findings,
        )

        # Persist.
        await self._save_round(result)
        await self._save_complaints(merged)
        self.rounds_executed = n

        # Convergence tracking.
        if new_count <= 3:
            self._consecutive_quiet_rounds += 1
        else:
            self._consecutive_quiet_rounds = 0
        if (
            self._consecutive_quiet_rounds >= 2
            or score >= self.target_quality
            or n >= self.max_rounds
        ):
            self._converged = True

        await self._persist_metadata()
        return result

    # ── convergence / verdict ────────────────────────────────────────

    async def has_converged(self) -> bool:
        return self._converged

    async def final_verdict(self) -> dict[str, Any]:
        last = await self.get_round(self.rounds_executed) if self.rounds_executed else None
        return {
            "iteration_id": self.iteration_id,
            "persona": self.persona.slug,
            "artifact_path": self.artifact_path,
            "target_quality": self.target_quality,
            "rounds_executed": self.rounds_executed,
            "converged": self._converged,
            "final_score": last.verdict_score if last else None,
            "final_headline": last.verdict_headline if last else None,
            "p0_remaining": last.p0_count() if last else None,
            "p1_remaining": last.p1_count() if last else None,
        }

    # ── helpers: complaint merging + persistence ─────────────────────

    def _merge_complaints(
        self,
        prior: list[Complaint],
        fresh: list[Complaint],
        *,
        current_round: int,
    ) -> tuple[list[Complaint], int]:
        """Dedup fresh into prior. Returns (merged, count_of_new_complaints)."""
        by_fp: dict[str, Complaint] = {c.fingerprint: c for c in prior}
        new_count = 0
        for c in fresh:
            existing = by_fp.get(c.fingerprint)
            if existing is None:
                by_fp[c.fingerprint] = Complaint(
                    severity=c.severity,
                    category=c.category,
                    persona_concern=c.persona_concern,
                    expected=c.expected,
                    got=c.got,
                    fix_estimate_hours=c.fix_estimate_hours,
                    fingerprint=c.fingerprint,
                    occurrences=1,
                    first_seen_round=current_round,
                    last_seen_round=current_round,
                )
                if c.severity in (Severity.P0, Severity.P1):
                    new_count += 1
            else:
                by_fp[c.fingerprint] = Complaint(
                    severity=existing.severity,
                    category=existing.category,
                    persona_concern=existing.persona_concern,
                    expected=existing.expected,
                    got=existing.got,
                    fix_estimate_hours=existing.fix_estimate_hours,
                    fingerprint=existing.fingerprint,
                    occurrences=existing.occurrences + 1,
                    first_seen_round=existing.first_seen_round,
                    last_seen_round=current_round,
                )
        return list(by_fp.values()), new_count

    def _escalate_if_repeated(self, c: Complaint) -> Complaint:
        """Repeated complaints get more painful — P2→P1, P1→P0 at ≥3 occ."""
        if c.occurrences < 3:
            return c
        if c.severity is Severity.P2:
            new = Severity.P1
        elif c.severity is Severity.P1:
            new = Severity.P0
        else:
            return c
        return Complaint(
            severity=new,
            category=c.category,
            persona_concern=c.persona_concern,
            expected=c.expected,
            got=c.got,
            fix_estimate_hours=c.fix_estimate_hours,
            fingerprint=c.fingerprint,
            occurrences=c.occurrences,
            first_seen_round=c.first_seen_round,
            last_seen_round=c.last_seen_round,
        )

    async def _save_round(self, result: RoundResult) -> None:
        r = await get_redis()
        await r.set(
            _round_key(self.iteration_id, result.round_number),
            json.dumps(result.to_json()),
        )

    async def get_round(self, n: int) -> RoundResult | None:
        r = await get_redis()
        raw = await r.get(_round_key(self.iteration_id, n))
        if not raw:
            return None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode()
        d = json.loads(raw)
        d["complaints"] = [Complaint.from_json(c) for c in d["complaints"]]
        return RoundResult(**d)

    async def _save_complaints(self, complaints: list[Complaint]) -> None:
        r = await get_redis()
        key = _complaints_key(self.iteration_id)
        # Wipe and replace — the merged list is canonical.
        await r.delete(key)
        if complaints:
            await r.hset(
                key,
                mapping={c.fingerprint: json.dumps(c.to_json()) for c in complaints},
            )

    async def _load_complaints(self) -> list[Complaint]:
        r = await get_redis()
        h = await r.hgetall(_complaints_key(self.iteration_id))
        out: list[Complaint] = []
        for _, v in h.items():
            if isinstance(v, (bytes, bytearray)):
                v = v.decode()
            out.append(Complaint.from_json(json.loads(v)))
        return out

    async def list_complaints(self) -> list[Complaint]:
        return await self._load_complaints()

    # ── auto-fix dispatch ────────────────────────────────────────────

    async def dispatch_autofix(
        self,
        *,
        dispatch_fn: Callable[[Complaint], Awaitable[str]] | None = None,
        severities: tuple[Severity, ...] = (Severity.P0,),
        max_tasks: int = 8,
    ) -> list[str]:
        """Spawn parallel fix-agents for each complaint in ``severities``.

        ``dispatch_fn(complaint) -> task_id`` is the per-complaint hook.
        When None, returns a list of *prompt strings* that a caller can
        feed to whatever orchestrator is wired up (Claude Code / Cursor /
        a queue worker). The engine is intentionally decoupled from any
        specific agent runtime.
        """

        targets = [c for c in await self._load_complaints() if c.severity in severities]
        targets = targets[:max_tasks]
        if dispatch_fn is None:
            return [_build_fix_prompt(c, self.artifact_path) for c in targets]

        coros = [dispatch_fn(c) for c in targets]
        return await asyncio.gather(*coros)


def _build_fix_prompt(c: Complaint, artifact_path: str) -> str:
    """Standard fix prompt — what we'd send to a fix-agent."""

    return (
        f"Fix this specific complaint on {artifact_path}.\n"
        f"Severity: {c.severity.value}\n"
        f"Category: {c.category}\n"
        f"Persona concern: {c.persona_concern}\n"
        f"Expected: {c.expected}\n"
        f"Got: {c.got}\n"
        f"Estimated effort: {c.fix_estimate_hours}h\n"
        "Produce a minimal patch; do not gold-plate."
    )


# ── Leaderboard ──────────────────────────────────────────────────────────


async def list_iterations(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most-recently-updated iterations for the admin dashboard."""

    r = await get_redis()
    ids = await r.zrevrange(_index_key(), 0, limit - 1)
    out: list[dict[str, Any]] = []
    for raw_iid in ids:
        iid = raw_iid.decode() if isinstance(raw_iid, (bytes, bytearray)) else raw_iid
        meta = await r.hgetall(_iter_key(iid))
        if not meta:
            continue
        def _s(v: Any) -> str:
            return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
        norm = {_s(k): _s(v) for k, v in meta.items()}
        out.append(
            {
                "iteration_id": iid,
                "persona": norm.get("persona"),
                "artifact_path": norm.get("artifact_path"),
                "target_quality": int(norm.get("target_quality", "7")),
                "rounds_executed": int(norm.get("rounds_executed", "0")),
                "converged": norm.get("converged") == "1",
                "updated_at": int(norm.get("updated_at", "0")),
            }
        )
    return out


# ── Public API surface ───────────────────────────────────────────────────


__all__ = [
    "Severity",
    "KNOWN_CATEGORIES",
    "Complaint",
    "Persona",
    "PERSONA_REGISTRY",
    "get_persona",
    "ShopOwnerPersona",
    "MarketingAgencyPersona",
    "ConsumerPersona",
    "AdminPersona",
    "InvestorPersona",
    "RoundResult",
    "TrinityIteration",
    "register_persona_walk_hook",
    "reset_persona_walk_hook",
    "list_iterations",
    "NIELSEN_HEURISTICS",
]
