"""Tests for app/services/verdict_gate.py — Gap A."""
import pytest

from app.services.verdict_gate import (
    GateDecision, VerdictScore, make_llm_evaluator,
    stub_evaluator, verdict_gate,
)


# ── VerdictScore validation ──

def test_verdict_score_valid():
    s = VerdictScore(persona_id="p1", score=80.0, verdict_text="ok")
    assert s.score == 80.0


def test_verdict_score_rejects_out_of_range():
    with pytest.raises(ValueError):
        VerdictScore(persona_id="p", score=150, verdict_text="x")
    with pytest.raises(ValueError):
        VerdictScore(persona_id="p", score=-5, verdict_text="x")


# ── stub_evaluator behavior ──

def test_stub_evaluator_clean_html_high_score():
    html = "<html>" + "x" * 1000 + "</html>"
    v = stub_evaluator(html, "p1")
    assert v.score == 100.0
    assert v.would_recommend


def test_stub_evaluator_placeholder_drops_score():
    html = "<html>TODO: design</html>" + "x" * 1000
    v = stub_evaluator(html, "p1")
    assert v.score == 70.0


def test_stub_evaluator_unfilled_template_drops_score():
    html = "<h1>{{brand_name}}</h1>" + "x" * 1000
    v = stub_evaluator(html, "p1")
    assert v.score == 75.0


def test_stub_evaluator_too_short_drops_score():
    v = stub_evaluator("<html></html>", "p1")
    assert v.score == 65.0   # 100 - 35


def test_stub_evaluator_aminah_penalty_for_heavy_page():
    html = "<html>" + "x" * 60000 + "</html>"
    v = stub_evaluator(html, "aminah_first_time_merchant")
    assert v.score == 90.0


# ── verdict_gate aggregate ──

def test_gate_accepts_when_all_pass():
    html = "<html>" + "x" * 2000 + "</html>"
    d = verdict_gate(html, ["p1", "p2", "p3"], stub_evaluator, threshold=60)
    assert d.accepted
    assert d.avg_score == 100.0
    assert d.min_score == 100.0
    assert d.num_personas == 3


def test_gate_rejects_when_avg_below_threshold():
    html = "<html>TODO TODO {{x}}</html>"   # short + placeholder + template
    d = verdict_gate(html, ["p1", "p2"], stub_evaluator, threshold=60)
    assert not d.accepted
    assert d.avg_score < 60


def test_gate_rejects_when_one_persona_below_floor():
    """Bug-trap test: avg can be high but one persona below floor → reject."""
    def mixed_evaluator(html, pid):
        if pid == "harsh":
            return VerdictScore(persona_id=pid, score=20.0, verdict_text="nope", reasons=["x"])
        return VerdictScore(persona_id=pid, score=95.0, verdict_text="great")

    html = "<html>" + "x" * 2000 + "</html>"
    d = verdict_gate(html, ["good1", "good2", "harsh"], mixed_evaluator,
                     threshold=60, min_score_floor=30)
    assert not d.accepted  # because harsh=20 < floor=30
    # Avg might pass on its own (95+95+20)/3 = 70 > 60 — but floor catches it
    assert d.avg_score > 60
    assert d.min_score == 20


def test_gate_majority_required():
    def two_low(html, pid):
        if pid in ("p1", "p2"):
            return VerdictScore(persona_id=pid, score=40, verdict_text="meh", reasons=["dull"])
        return VerdictScore(persona_id=pid, score=80, verdict_text="ok")

    html = "<html>" + "x" * 2000 + "</html>"
    d = verdict_gate(html, ["p1", "p2", "p3"], two_low,
                     threshold=60, require_majority_pass=True, min_score_floor=0)
    # only 1 of 3 passes — majority fails
    assert not d.accepted


def test_gate_evaluator_exception_doesnt_crash():
    def crasher(html, pid):
        if pid == "broken":
            raise RuntimeError("kaboom")
        return VerdictScore(persona_id=pid, score=80, verdict_text="ok")

    html = "<html>" + "x" * 2000 + "</html>"
    d = verdict_gate(html, ["good", "broken"], crasher, threshold=60, min_score_floor=0)
    # broken persona gets score=0 + reason "evaluator failed"
    assert d.num_personas == 2
    assert d.min_score == 0
    bad = [s for s in d.persona_scores if s.persona_id == "broken"][0]
    assert "evaluator failed" in bad.reasons[0]


def test_gate_aggregates_top_rejection_reasons():
    def reason_emitter(html, pid):
        return VerdictScore(persona_id=pid, score=20,
                             verdict_text="no",
                             reasons=["too_long", "no_voucher", "ugly_color"])

    html = "<html>" + "x" * 2000 + "</html>"
    d = verdict_gate(html, ["p1", "p2", "p3"], reason_emitter, threshold=60,
                     min_score_floor=0)
    assert not d.accepted
    # all 3 personas emit same reasons → top reasons are those 3
    assert "too_long" in d.rejection_reasons
    assert "no_voucher" in d.rejection_reasons
    assert len(d.rejection_reasons) <= 8


# ── Input validation ──

def test_empty_html_raises():
    with pytest.raises(ValueError):
        verdict_gate("", ["p"], stub_evaluator)
    with pytest.raises(ValueError):
        verdict_gate("   ", ["p"], stub_evaluator)


def test_no_personas_raises():
    with pytest.raises(ValueError):
        verdict_gate("<html></html>" + "x" * 1000, [], stub_evaluator)


def test_non_callable_evaluator_raises():
    with pytest.raises(TypeError):
        verdict_gate("<html></html>" + "x" * 1000, ["p"], "not a function")


# ── num_below_min property ──

def test_gate_decision_num_below_min():
    def mixed(html, pid):
        if pid == "low":
            return VerdictScore(persona_id=pid, score=10, verdict_text="x")
        return VerdictScore(persona_id=pid, score=90, verdict_text="ok")

    html = "<html>" + "x" * 2000 + "</html>"
    d = verdict_gate(html, ["high", "low"], mixed, threshold=60, min_score_floor=0)
    assert d.num_below_min == 1


# ── make_llm_evaluator factory ──

def test_make_llm_evaluator_falls_back_to_stub_when_no_deps():
    ev = make_llm_evaluator(persona_loader=None, llm_call=None)
    assert ev is stub_evaluator


def test_make_llm_evaluator_with_mocked_llm():
    def fake_loader(pid):
        return {"name": "Test User", "role": "test", "context": "test"}

    def fake_llm(system, user):
        return {"ok": True, "text": '{"score": 75, "verdict": "decent", "reasons": ["small things"]}'}

    ev = make_llm_evaluator(persona_loader=fake_loader, llm_call=fake_llm)
    html = "<html>" + "x" * 2000 + "</html>"
    score = ev(html, "test_persona")
    assert score.score == 75.0
    assert "decent" in score.verdict_text
    assert "small things" in score.reasons


def test_make_llm_evaluator_handles_llm_failure():
    def fake_loader(pid): return {"name": "x", "role": "y", "context": "z"}
    def fake_llm(system, user): return {"ok": False, "error": "rate-limited"}

    ev = make_llm_evaluator(persona_loader=fake_loader, llm_call=fake_llm)
    score = ev("<html></html>" + "x" * 1000, "p")
    assert score.score == 0.0
    assert "rate-limited" in score.reasons[0]


def test_make_llm_evaluator_handles_persona_load_failure():
    def fake_loader(pid): raise KeyError("unknown_persona")
    def fake_llm(s, u): return {"ok": True, "text": "{}"}

    ev = make_llm_evaluator(persona_loader=fake_loader, llm_call=fake_llm)
    score = ev("<html></html>" + "x" * 1000, "missing")
    assert score.score == 0.0
    assert "unknown persona missing" in score.reasons[0]


def test_make_llm_evaluator_handles_non_json_response():
    def fake_loader(pid): return {"name": "x", "role": "y", "context": "z"}
    def fake_llm(s, u): return {"ok": True, "text": "I am a critic, not JSON!"}

    ev = make_llm_evaluator(persona_loader=fake_loader, llm_call=fake_llm)
    score = ev("<html></html>" + "x" * 1000, "p")
    assert score.score == 0.0
    assert "non-JSON" in score.reasons[0]
