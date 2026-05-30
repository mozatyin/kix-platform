"""Rule engine router — configure, list, get, disable/enable, delete, emit."""

from __future__ import annotations

import pytest


async def _make_rule(client, brand="b1", rid="r1"):
    return await client.post(
        "/api/v1/rules/configure",
        json={
            "id": rid,
            "brand_id": brand,
            "name": "Test Rule",
            "trigger_event": "purchase",
            "actions": [{"module": "wallet", "method": "topup", "params": {}}],
        },
    )


@pytest.mark.asyncio
async def test_configure_rule_happy(client, clean_redis):
    res = await _make_rule(client)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_configure_rule_missing_id_422(client, clean_redis):
    res = await client.post(
        "/api/v1/rules/configure",
        json={
            "brand_id": "b",
            "name": "X",
            "trigger_event": "y",
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_list_rules_empty(client, clean_redis):
    res = await client.get("/api/v1/rules/no_brand")
    assert res.status_code == 200
    assert res.json()["count"] == 0


@pytest.mark.asyncio
async def test_get_rule_404(client, clean_redis):
    res = await client.get("/api/v1/rules/b/nope")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_disable_enable_cycle(client, clean_redis):
    await _make_rule(client, brand="b_de", rid="rule_de")
    d = await client.post("/api/v1/rules/b_de/rule_de/disable")
    assert d.status_code == 200
    assert d.json()["active"] is False
    e = await client.post("/api/v1/rules/b_de/rule_de/enable")
    assert e.status_code == 200
    assert e.json()["active"] is True


@pytest.mark.asyncio
async def test_delete_rule_404(client, clean_redis):
    res = await client.delete("/api/v1/rules/b/nope")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_emit_event_runs(client, clean_redis):
    await _make_rule(client, brand="b_emit", rid="rule_e")
    res = await client.post(
        "/api/v1/rules/events/emit",
        json={
            "brand_id": "b_emit",
            "user_id": "u1",
            "event_name": "purchase",
            "payload": {},
        },
    )
    assert res.status_code == 200
