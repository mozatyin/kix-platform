"""Tests for app/workers/nightly_creative_refresh.py — CLASS-L structural fix."""
import json
import os
import pytest
from pathlib import Path

from app.workers.nightly_creative_refresh import (
    CURRENT_PIPELINE_VERSION, RefreshDecision,
    decide_action, run,
)


def test_no_manifest_returns_no_source():
    d = decide_action("brand_x", None)
    assert d.action == "no-source"


def test_current_pipeline_skipped():
    m = {"pipeline_version": CURRENT_PIPELINE_VERSION,
         "last_refreshed_at": "2026-01-01T00:00:00Z"}
    d = decide_action("brand_x", m)
    assert d.action == "skip-current"


def test_recently_refreshed_skipped(monkeypatch):
    monkeypatch.setenv("NCR_NOW", "2026-05-30T12:00:00+00:00")
    m = {"pipeline_version": "2025.12.01-v1",
         "last_refreshed_at": "2026-05-30T10:00:00Z"}  # 2h ago
    d = decide_action("brand_x", m)
    assert d.action == "skip-recent"


def test_old_pipeline_triggers_refresh(monkeypatch):
    monkeypatch.setenv("NCR_NOW", "2026-05-30T12:00:00+00:00")
    m = {"pipeline_version": "2025.10.01-v1",
         "last_refreshed_at": "2026-04-01T00:00:00Z"}
    d = decide_action("brand_x", m)
    assert d.action == "refresh"
    assert d.old_version == "2025.10.01-v1"
    assert d.new_version == CURRENT_PIPELINE_VERSION


def test_run_empty_dir(tmp_path):
    decisions = run(tmp_path, dry_run=True)
    assert decisions == []


def test_run_skips_brand_at_current_version(tmp_path):
    b = tmp_path / "brand_a"
    b.mkdir()
    (b / "manifest.json").write_text(json.dumps({
        "pipeline_version": CURRENT_PIPELINE_VERSION,
    }))
    decisions = run(tmp_path, dry_run=True)
    assert len(decisions) == 1
    assert decisions[0].action == "skip-current"


def test_run_refreshes_brand_at_old_version(tmp_path, monkeypatch):
    monkeypatch.setenv("NCR_NOW", "2026-05-30T12:00:00+00:00")
    b = tmp_path / "brand_a"
    b.mkdir()
    (b / "manifest.json").write_text(json.dumps({
        "pipeline_version": "2025.10.01-v1",
        "last_refreshed_at": "2026-04-01T00:00:00Z",
        "brand_config": {
            "brand_id": "brand_a", "brand_name": "Brand A",
            "hero_tagline": "Pay only for verified new customers",
            "hero_sub": "Free SaaS.",
        },
    }))
    decisions = run(tmp_path, dry_run=True)
    assert len(decisions) == 1
    assert decisions[0].action == "refresh"
    assert decisions[0].new_version == CURRENT_PIPELINE_VERSION


def test_rate_limit_caps_refreshes(tmp_path, monkeypatch):
    monkeypatch.setenv("NCR_NOW", "2026-05-30T12:00:00+00:00")
    for i in range(7):
        b = tmp_path / f"brand_{i}"
        b.mkdir()
        (b / "manifest.json").write_text(json.dumps({
            "pipeline_version": "2025.10.01-v1",
            "last_refreshed_at": "2026-04-01T00:00:00Z",
            "brand_config": {
                "brand_id": f"brand_{i}", "brand_name": f"Brand {i}",
                "hero_tagline": "Pay only for verified new customers",
                "hero_sub": "Free SaaS.",
            },
        }))
    decisions = run(tmp_path, max_brands=3, dry_run=True)
    refreshed = sum(1 for d in decisions if d.action == "refresh")
    limited = sum(1 for d in decisions if d.action == "skip-rate-limit")
    assert refreshed == 3
    assert limited == 4


def test_dry_run_doesnt_write_to_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("NCR_NOW", "2026-05-30T12:00:00+00:00")
    b = tmp_path / "brand_a"
    b.mkdir()
    (b / "manifest.json").write_text(json.dumps({
        "pipeline_version": "2025.10.01-v1",
        "last_refreshed_at": "2026-04-01T00:00:00Z",
        "brand_config": {
            "brand_id": "brand_a", "brand_name": "Brand A",
            "hero_tagline": "Pay only for verified new customers",
            "hero_sub": "Free SaaS.",
        },
    }))
    run(tmp_path, dry_run=True)
    # index.html should NOT exist after dry-run
    assert not (b / "index.html").exists()
    # manifest unchanged
    m = json.loads((b / "manifest.json").read_text())
    assert m["pipeline_version"] == "2025.10.01-v1"


def test_only_brand_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("NCR_NOW", "2026-05-30T12:00:00+00:00")
    for n in ["a", "b"]:
        b = tmp_path / f"brand_{n}"
        b.mkdir()
        (b / "manifest.json").write_text(json.dumps({
            "pipeline_version": "2025.10.01-v1",
            "last_refreshed_at": "2026-04-01T00:00:00Z",
            "brand_config": {
                "brand_id": f"brand_{n}", "brand_name": f"Brand {n}",
                "hero_tagline": "Pay only for verified new customers",
                "hero_sub": "Free SaaS.",
            },
        }))
    decisions = run(tmp_path, only_brand="brand_a", dry_run=True)
    assert len(decisions) == 1
    assert decisions[0].brand_id == "brand_a"
