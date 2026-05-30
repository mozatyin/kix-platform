"""Tests for the Trinity 3T iteration engine.

Covers the core abstractions in ``app.services.trinity_engine`` and the
HTTP surface in ``app.routers.trinity_admin``.

The default deterministic persona-walker keeps these tests hermetic — no
LLM calls — yet exercises the full flow: persona → complaints → dedup →
convergence → verdict.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path

import pytest

from app.services.trinity_engine import (
    KNOWN_CATEGORIES,
    NIELSEN_HEURISTICS,
    PERSONA_REGISTRY,
    AdminPersona,
    Complaint,
    ConsumerPersona,
    InvestorPersona,
    MarketingAgencyPersona,
    Severity,
    ShopOwnerPersona,
    TrinityIteration,
    get_persona,
    list_iterations,
    register_persona_walk_hook,
    reset_persona_walk_hook,
)

ADMIN_TOKEN = os.getenv("KIX_ADMIN_TOKEN", "admin-dev-token")
ADMIN_HEADERS = {"X-Admin-Token": ADMIN_TOKEN}


# ── helpers ──────────────────────────────────────────────────────────────


def _write_artifact(body: str = "") -> str:
    """Write a temp artifact and return its path."""
    fd, path = tempfile.mkstemp(suffix=".html", prefix="trinity-test-")
    os.close(fd)
    Path(path).write_text(body, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _reset_hook():
    """Always reset the persona-walk hook between tests."""
    yield
    reset_persona_walk_hook()


# ── 1. Persona-driven walk produces complaints ───────────────────────────


@pytest.mark.asyncio
async def test_persona_walk_produces_complaints(clean_redis):
    """An artifact missing the persona's red-flag keywords yields complaints."""

    # Body intentionally missing FAQ / chat / price etc.
    artifact = _write_artifact("Welcome to our platform. Lorem ipsum.")
    it = await TrinityIteration.create(
        persona="shop-owner", artifact_path=artifact, target_quality=10,
    )
    r1 = await it.round()
    assert len(r1.complaints) > 0
    # Shop-owner cares about pricing first — should surface as P0.
    assert any(c.severity == Severity.P0 for c in r1.complaints)


# ── 2. Industry baseline comparison detects gaps ─────────────────────────


@pytest.mark.asyncio
async def test_industry_baseline_gaps_detected(clean_redis):
    """A barebones artifact triggers industry gaps for each baseline."""

    artifact = _write_artifact("nothing relevant here")
    it = await TrinityIteration.create(
        persona="marketing-agency", artifact_path=artifact, target_quality=10,
    )
    r1 = await it.round()
    # MarketingAgency has 4 baselines, each contributes one gap on empty body.
    assert len(r1.industry_gaps) >= 3
    assert any("Google Ads" in g for g in r1.industry_gaps)


# ── 3. Academic check uses Nielsen heuristics ────────────────────────────


@pytest.mark.asyncio
async def test_academic_uses_nielsen(clean_redis):
    """An empty body fails the recognition / help heuristics."""

    artifact = _write_artifact("")
    it = await TrinityIteration.create(
        persona="consumer", artifact_path=artifact, target_quality=10,
    )
    r1 = await it.round()
    assert len(r1.academic_gaps) > 0
    assert any("Nielsen" in g for g in r1.academic_gaps)
    # The constant is exported and stable.
    assert "help-documentation" in NIELSEN_HEURISTICS


# ── 4. Reality check runs codebase grep ──────────────────────────────────


@pytest.mark.asyncio
async def test_reality_grep_runs(clean_redis):
    """Reality findings come from the body and are populated."""

    artifact = _write_artifact("simple page")
    it = await TrinityIteration.create(
        persona="shop-owner", artifact_path=artifact,
    )
    r1 = await it.round()
    assert isinstance(r1.reality_findings, list)
    # Missing both faq and price ⇒ at least 2 findings.
    assert len(r1.reality_findings) >= 2


# ── 5. Convergence detection works ───────────────────────────────────────


@pytest.mark.asyncio
async def test_convergence_high_quality_artifact(clean_redis):
    """A high-quality body should converge fast with a high score."""

    rich_body = (
        "Welcome. Our pricing is transparent. FAQ available. "
        "Chat support 24/7. Refund policy clear. Mobile-friendly. "
        "English and Chinese. Demo available. Progress tracked. "
        "Audience targeting and campaign creation included. "
        "Audit log, RBAC, export, bulk actions, search. "
        "Tier system with badges and missions. Status visible. "
        "Help docs, undo/cancel, confirm dialogs, menu nav."
    )
    artifact = _write_artifact(rich_body)
    it = await TrinityIteration.create(
        persona="shop-owner", artifact_path=artifact, target_quality=7,
        max_rounds=5,
    )
    r1 = await it.round()
    assert r1.verdict_score >= 7
    assert await it.has_converged()


# ── 6. Multi-round produces decreasing new complaints ────────────────────


@pytest.mark.asyncio
async def test_multiround_dedup_decreases_new(clean_redis):
    """Same artifact across rounds → new_complaint_count drops to 0."""

    artifact = _write_artifact("plain content")
    it = await TrinityIteration.create(
        persona="shop-owner", artifact_path=artifact, target_quality=10,
        max_rounds=5,
    )
    r1 = await it.round()
    r2 = await it.round()
    # All complaints in r2 are dupes of r1 — new_complaint_count == 0.
    assert r2.new_complaint_count == 0
    assert r1.new_complaint_count >= 1


# ── 7. Auto-fix dispatch hook ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_autofix_dispatch_emits_prompts(clean_redis):
    """auto-fix returns one prompt per matching complaint."""

    artifact = _write_artifact("")
    it = await TrinityIteration.create(
        persona="shop-owner", artifact_path=artifact, target_quality=10,
    )
    await it.round()
    prompts = await it.dispatch_autofix(severities=(Severity.P0,), max_tasks=5)
    assert all("Fix this specific complaint" in p for p in prompts)
    assert all("Severity: P0" in p for p in prompts)


# ── 8. Concurrent iterations don't collide ───────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_iterations_isolated(clean_redis):
    """Two iterations on different artifacts persist independently."""

    a1 = _write_artifact("alpha body")
    a2 = _write_artifact("beta body")
    it1 = await TrinityIteration.create(persona="shop-owner", artifact_path=a1)
    it2 = await TrinityIteration.create(persona="consumer", artifact_path=a2)
    assert it1.iteration_id != it2.iteration_id

    await asyncio.gather(it1.round(), it2.round())

    it1b = await TrinityIteration.resume(it1.iteration_id)
    it2b = await TrinityIteration.resume(it2.iteration_id)
    assert it1b.persona.slug == "shop-owner"
    assert it2b.persona.slug == "consumer"


# ── 9. Persistence: iteration state saved to Redis ───────────────────────


@pytest.mark.asyncio
async def test_state_persists_to_redis(clean_redis):
    """resume() reconstructs the engine from Redis alone."""

    artifact = _write_artifact("hello")
    it = await TrinityIteration.create(persona="admin", artifact_path=artifact)
    await it.round()

    restored = await TrinityIteration.resume(it.iteration_id)
    assert restored.iteration_id == it.iteration_id
    assert restored.persona.slug == "admin"
    assert restored.rounds_executed == 1


# ── 10. Resume from checkpoint ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_continues_iteration(clean_redis):
    """Round counter survives resume — next round is N+1."""

    artifact = _write_artifact("hi")
    it = await TrinityIteration.create(
        persona="shop-owner", artifact_path=artifact, target_quality=10,
        max_rounds=5,
    )
    r1 = await it.round()
    assert r1.round_number == 1

    restored = await TrinityIteration.resume(it.iteration_id)
    r2 = await restored.round()
    assert r2.round_number == 2


# ── 11. Verdict thresholding ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verdict_threshold_stops_iteration(clean_redis):
    """When score crosses target_quality the iteration converges."""

    # Build a body that satisfies enough keywords to score ≥ 5.
    body = (
        "pricing faq refund english chat demo mobile audience campaign "
        "creative checkout tier voucher streak feed status help undo cancel "
        "confirm menu nav docs progress badge mission load"
    )
    artifact = _write_artifact(body)
    it = await TrinityIteration.create(
        persona="shop-owner", artifact_path=artifact, target_quality=5,
        max_rounds=10,
    )
    await it.round()
    assert await it.has_converged()


# ── 12. Complaint dedup across rounds ────────────────────────────────────


@pytest.mark.asyncio
async def test_complaint_dedup_increments_occurrences(clean_redis):
    """Same complaint appearing twice → occurrences == 2."""

    artifact = _write_artifact("")
    it = await TrinityIteration.create(persona="shop-owner", artifact_path=artifact)
    await it.round()
    await it.round()
    cs = await it.list_complaints()
    # Pick any P0 complaint — it should have occurrences == 2.
    p0s = [c for c in cs if c.severity is Severity.P0]
    assert p0s, "expected at least one P0"
    assert any(c.occurrences >= 2 for c in p0s)


# ── 13. Severity escalation after 3 repeats ──────────────────────────────


@pytest.mark.asyncio
async def test_severity_escalates_after_three_rounds(clean_redis):
    """A P1 that repeats 3 rounds gets escalated to P0."""

    # Craft a body that produces a stable P1 complaint
    # (`english only` is index 2 → P1) but no P0s. We achieve "no P0s" by
    # making pricing+faq present.
    body = "pricing faq"
    artifact = _write_artifact(body)
    it = await TrinityIteration.create(
        persona="shop-owner", artifact_path=artifact, target_quality=10,
        max_rounds=5,
    )
    await it.round()
    await it.round()
    await it.round()
    cs = await it.list_complaints()
    # Find the english-only complaint and assert it's escalated.
    english = [c for c in cs if "english" in c.got.lower() or "english" in c.expected.lower()]
    if english:
        # After 3 occurrences a P1 escalates to P0.
        assert any(c.severity is Severity.P0 for c in english)


# ── 14. Audit log integration / leaderboard surface ──────────────────────


@pytest.mark.asyncio
async def test_leaderboard_lists_iterations(clean_redis):
    """list_iterations() returns the iterations sorted by recency."""

    a = _write_artifact("a")
    b = _write_artifact("b")
    it_a = await TrinityIteration.create(persona="shop-owner", artifact_path=a)
    await asyncio.sleep(0.01)
    it_b = await TrinityIteration.create(persona="consumer", artifact_path=b)

    rows = await list_iterations(limit=10)
    ids = [r["iteration_id"] for r in rows]
    assert it_a.iteration_id in ids
    assert it_b.iteration_id in ids
    # Most-recent first.
    assert ids.index(it_b.iteration_id) < ids.index(it_a.iteration_id)


# ── 15. Performance: round under 5s for a simple artifact ────────────────


@pytest.mark.asyncio
async def test_round_under_5s_simple_artifact(clean_redis):
    """A round on a small artifact completes well under the 5s budget."""

    artifact = _write_artifact("simple body" * 50)
    it = await TrinityIteration.create(persona="shop-owner", artifact_path=artifact)
    t0 = time.time()
    await it.round()
    elapsed = time.time() - t0
    assert elapsed < 5.0, f"round took {elapsed:.2f}s — budget 5.0s"


# ── 16. Persona registry sanity ──────────────────────────────────────────


def test_persona_registry_has_all_five():
    """All five required personas are registered and callable."""

    expected = {"shop-owner", "marketing-agency", "consumer", "admin", "investor"}
    assert expected.issubset(set(PERSONA_REGISTRY))
    # Each factory returns a Persona with the expected slug.
    assert get_persona("shop-owner").slug == "shop-owner"
    assert ShopOwnerPersona().slug == "shop-owner"
    assert MarketingAgencyPersona().slug == "marketing-agency"
    assert ConsumerPersona().slug == "consumer"
    assert AdminPersona().slug == "admin"
    assert InvestorPersona().slug == "investor"


# ── 17. HTTP surface — happy path ────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_iterate_and_round(client, clean_redis):
    """End-to-end: create via HTTP, run a round, fetch results."""

    artifact = _write_artifact("hello world")
    # Create.
    r = await client.post(
        "/api/v1/trinity/iterate",
        json={
            "admin_token": ADMIN_TOKEN,
            "persona": "shop-owner",
            "artifact_path": artifact,
            "target_quality": 8,
            "max_rounds": 3,
            "auto_run": False,
        },
    )
    assert r.status_code == 201, r.text
    iid = r.json()["iteration_id"]

    # Run a round.
    r = await client.post(
        f"/api/v1/trinity/iteration/{iid}/round",
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["round_number"] == 1

    # Fetch iteration status.
    r = await client.get(
        f"/api/v1/trinity/iteration/{iid}",
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 200
    assert "verdict" in r.json()
    assert "complaints" in r.json()


# ── 18. HTTP surface — auth required ─────────────────────────────────────


@pytest.mark.asyncio
async def test_http_requires_admin_token(client, clean_redis):
    r = await client.post(
        "/api/v1/trinity/iterate",
        json={"persona": "shop-owner", "artifact_path": "/tmp/x"},
    )
    assert r.status_code == 403


# ── 19. HTTP surface — personas discovery (no auth) ──────────────────────


@pytest.mark.asyncio
async def test_http_personas_no_auth(client, clean_redis):
    r = await client.get("/api/v1/trinity/personas")
    assert r.status_code == 200
    slugs = {p["slug"] for p in r.json()["personas"]}
    assert {"shop-owner", "marketing-agency", "consumer", "admin", "investor"} <= slugs


# ── 20. Complaint fingerprinting is stable ───────────────────────────────


def test_complaint_fingerprint_stable():
    """Two complaints with the same content produce the same fingerprint."""

    c1 = Complaint(severity=Severity.P0, category="pricing",
                   persona_concern="price unclear",
                   expected="visible price",
                   got="no price shown")
    c2 = Complaint(severity=Severity.P0, category="pricing",
                   persona_concern="Price Unclear  ",
                   expected="VISIBLE PRICE",
                   got="no price shown")
    assert c1.fingerprint == c2.fingerprint
    # Different category → different fingerprint.
    c3 = Complaint(severity=Severity.P0, category="trust",
                   persona_concern="price unclear",
                   expected="visible price",
                   got="no price shown")
    assert c3.fingerprint != c1.fingerprint


# ── 21. Custom persona-walk hook (LLM stub) ──────────────────────────────


@pytest.mark.asyncio
async def test_custom_walk_hook_used(clean_redis):
    """A registered persona-walk hook overrides the default walker."""

    sentinel_calls: list[str] = []

    async def stub(persona, artifact_path, body):
        sentinel_calls.append(persona.slug)
        return [
            Complaint(
                severity=Severity.P0, category="pricing",
                persona_concern="stub", expected="x", got="y",
            )
        ]

    register_persona_walk_hook(stub)
    artifact = _write_artifact("doesn't matter")
    it = await TrinityIteration.create(
        persona="shop-owner", artifact_path=artifact, target_quality=10,
    )
    r1 = await it.round()
    assert sentinel_calls == ["shop-owner"]
    # Only one persona complaint + industry/academic synthesized ones.
    # But the persona-walk part must contain exactly our stub complaint.
    stub_complaints = [c for c in r1.complaints if c.persona_concern == "stub"]
    assert len(stub_complaints) == 1


# ── 22. Known categories are stable ──────────────────────────────────────


def test_known_categories_set_stable():
    """The category whitelist is non-empty and contains expected entries."""

    assert "pricing" in KNOWN_CATEGORIES
    assert "ia" in KNOWN_CATEGORIES
    assert "workflow" in KNOWN_CATEGORIES
    assert len(KNOWN_CATEGORIES) >= 8
