"""Tests for app/services/proof_registry.py — Trinity-iterated trust fix."""
import pytest
from app.services.proof_registry import (
    PROOFS, Proof, coverage_report, find_missing_proofs, get, render_badge,
)


def test_canonical_proofs_seeded():
    assert "soc2_type_ii" in PROOFS
    assert "dpa_enterprise" in PROOFS
    assert "cancel_one_click_demo" in PROOFS
    assert "bubble_tea_benchmark" in PROOFS
    assert "pos_integration_matrix" in PROOFS


def test_proof_frozen():
    with pytest.raises(Exception):
        PROOFS["soc2_type_ii"].status = "missing"


def test_get_returns_proof():
    p = get("soc2_type_ii")
    assert p is not None
    assert p.status == "present"
    assert p.artifact_url


def test_get_unknown_returns_none():
    assert get("nonexistent_claim_12345") is None


def test_badge_present_renders_green_link():
    badge = render_badge("soc2_type_ii")
    assert "DCFCE7" in badge or "proof-present" in badge
    assert "✓" in badge
    assert "soc2-type2" in badge


def test_badge_pending_renders_amber():
    badge = render_badge("oracle_simphony_native")
    assert "FEF3C7" in badge or "proof-pending" in badge
    assert "on request" in badge


def test_badge_missing_renders_red_todo():
    badge = render_badge("totally_nonexistent_claim")
    assert "TODO" in badge
    assert "proof-missing" in badge
    assert "FEE2E2" in badge or "7F1D1D" in badge


def test_find_missing_proofs_detects_todo_badge():
    html = '<p>Some text <span class="proof-badge proof-missing">⚠ TODO</span> more</p>'
    missing = find_missing_proofs(html)
    assert missing  # at least one


def test_find_missing_proofs_clean_html():
    html = '<p>Some text <a class="proof-badge proof-present">✓ verify</a></p>'
    assert find_missing_proofs(html) == []


def test_custom_label():
    badge = render_badge("dpa_enterprise", label="view DPA PDF")
    assert "view DPA PDF" in badge


def test_audit_note_in_title_attr():
    badge = render_badge("pen_test_q1")
    assert "Bishop Fox" in badge or "title=" in badge


def test_coverage_report():
    r = coverage_report()
    assert r["total_claims"] >= 10
    assert "present" in r["by_status"]
    assert 0 <= r["coverage_pct"] <= 100


def test_pending_claims_have_mailto():
    """Pending proofs should at least have a contact URL for ETA inquiry."""
    pending = [p for p in PROOFS.values() if p.status == "pending"]
    for p in pending:
        assert p.artifact_url, f"Pending proof {p.claim_id} has no contact URL"
        assert "mailto:" in p.artifact_url or p.artifact_url.startswith("/"), (
            f"Pending proof {p.claim_id} contact URL looks off: {p.artifact_url}"
        )


def test_present_claims_have_artifact_url():
    present = [p for p in PROOFS.values() if p.status == "present"]
    for p in present:
        assert p.artifact_url, f"Present proof {p.claim_id} missing artifact_url"
