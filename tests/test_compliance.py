"""Compliance router tests — scan, rules, auto-inject, retention class."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_compliance_scan_clean_text_passes(client, clean_redis):
    res = await client.post(
        "/api/v1/compliance/scan",
        json={
            "industry": "general",
            "creative_text": "Welcome to our new cafe — try our seasonal latte.",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # ``pass`` is aliased; with no violations text passes.
    assert body["pass"] is True
    assert body["violations"] == []


@pytest.mark.asyncio
async def test_compliance_scan_blocks_extreme_words(client, clean_redis):
    """Chinese 极限词 (best/top/national/etc) are blocked under 广告法 §9.

    bug-bait: multiple banned phrases must all surface, not just the first.
    """
    res = await client.post(
        "/api/v1/compliance/scan",
        json={
            "industry": "general",
            # "最好" and "国家级" are both 极限词 hits.
            "creative_text": "我们是最好的产品，国家级品牌",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["pass"] is False
    # bug-bait: scan_text with multiple violations — must list >1.
    assert len(body["violations"]) >= 2


@pytest.mark.asyncio
async def test_compliance_scan_invalid_industry_rejected(client, clean_redis):
    """bug-bait: industry outside the Literal whitelist is 422."""
    res = await client.post(
        "/api/v1/compliance/scan",
        json={
            "industry": "spaceflight",
            "creative_text": "some text",
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_compliance_register_rule_then_list(client, clean_redis):
    reg = await client.post(
        "/api/v1/compliance/rules/register",
        json={
            "industry": "general",
            "phrase": "保证退款",
            "severity": "block",
            "rule_id": "test_block_refund",
            "jurisdiction": "CN",
        },
    )
    assert reg.status_code == 200, reg.text

    listed = await client.get("/api/v1/compliance/rules?industry=general")
    assert listed.status_code == 200
    rule_ids = [r["rule_id"] for r in listed.json()]
    assert "test_block_refund" in rule_ids


@pytest.mark.asyncio
async def test_compliance_register_invalid_regex_rejected(client, clean_redis):
    """bug-bait: bogus regex must surface as 400, not silently store."""
    res = await client.post(
        "/api/v1/compliance/rules/register",
        json={
            "industry": "general",
            "phrase": "x",
            "regex": "[unclosed",
            "severity": "warn",
            "rule_id": "bad_regex_test",
        },
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_compliance_disable_unknown_rule_is_404(client, clean_redis):
    """bug-bait: disabling a rule that doesn't exist must 404."""
    res = await client.post("/api/v1/compliance/rules/nonexistent_rule_xxx/disable")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_compliance_auto_inject_disclaimer(client, clean_redis):
    res = await client.post(
        "/api/v1/compliance/auto-inject",
        json={
            "creative_text": "Our medical clinic offers world-class treatment.",
            "industry": "medical",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # The medical disclaimer should be appended.
    assert body["changed"] is True
    assert len(body["disclaimers"]) >= 1


@pytest.mark.asyncio
async def test_compliance_retention_class_configure_and_list(client, clean_redis):
    cfg = await client.post(
        "/api/v1/compliance/retention-class/configure",
        json={
            "scope": "medical_record_retention",
            "retention_years": 15,
            "mandatory": True,
            "citation": "PIPL §47",
        },
    )
    assert cfg.status_code == 200, cfg.text

    listed = await client.get("/api/v1/compliance/retention-class")
    assert listed.status_code == 200
    classes = listed.json()["classes"]
    scopes = [c["scope"] for c in classes]
    assert "medical_record_retention" in scopes
    # Ensure retention_years deserialized as int 15.
    record = next(c for c in classes if c["scope"] == "medical_record_retention")
    assert record["retention_years"] == 15
