"""Tests — Wave F campaign template gallery service + router."""

from __future__ import annotations

import os
import tempfile

import pytest

from app.main import app
from app.deps import get_current_user
from app.services import wavef_template_gallery as svc


def _override_user(brand_id: str = "b-tpl"):
    async def _fake():
        return {
            "sub": "u-1",
            "brand_id": brand_id,
            "device_sig": "dev",
            "session_id": "s",
            "is_day1": False,
            "exp": 0,
        }
    return _fake


# ── Service ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_loaded_seeds_redis(clean_redis):
    r = clean_redis
    with tempfile.TemporaryDirectory() as td:
        n = await svc.ensure_loaded(r, template_dir=td, force=True)
        assert n >= len(svc._SEED)
        items = await svc.list_templates(r)
        ids = {i["id"] for i in items}
        assert "qsr_lunch_rush" in ids
        assert "fashion_seasonal_drop" in ids


@pytest.mark.asyncio
async def test_vertical_filter(clean_redis):
    r = clean_redis
    with tempfile.TemporaryDirectory() as td:
        await svc.ensure_loaded(r, template_dir=td, force=True)
    items = await svc.list_templates(r, vertical="qsr")
    assert items
    assert all(i["vertical"] == "qsr" for i in items)


@pytest.mark.asyncio
async def test_mechanic_filter(clean_redis):
    r = clean_redis
    with tempfile.TemporaryDirectory() as td:
        await svc.ensure_loaded(r, template_dir=td, force=True)
    items = await svc.list_templates(r, mechanic="spin_wheel")
    assert items
    assert all("spin_wheel" in i["mechanics"] for i in items)


@pytest.mark.asyncio
async def test_clone_default_creates_campaign_id(clean_redis):
    r = clean_redis
    with tempfile.TemporaryDirectory() as td:
        await svc.ensure_loaded(r, template_dir=td, force=True)
    res = await svc.clone_template(
        r, "qsr_lunch_rush", brand_id="b-x",
        overrides={"duration_days": 7},
    )
    assert res["campaign_id"].startswith("cmp_")
    assert res["template_id"] == "qsr_lunch_rush"
    assert res["merged"]["duration_days"] == 7
    assert res["merged"]["brand_id"] == "b-x"


@pytest.mark.asyncio
async def test_clone_unknown_raises(clean_redis):
    r = clean_redis
    with tempfile.TemporaryDirectory() as td:
        await svc.ensure_loaded(r, template_dir=td, force=True)
    with pytest.raises(KeyError):
        await svc.clone_template(r, "no-such-tpl", brand_id="b-x")


@pytest.mark.asyncio
async def test_clone_with_creator_callback(clean_redis):
    r = clean_redis
    with tempfile.TemporaryDirectory() as td:
        await svc.ensure_loaded(r, template_dir=td, force=True)
    seen: dict = {}

    def creator(payload):
        seen["payload"] = payload
        return "cmp_external_42"

    res = await svc.clone_template(
        r, "qsr_lunch_rush", brand_id="b-y", creator=creator,
    )
    assert res["campaign_id"] == "cmp_external_42"
    assert seen["payload"]["template_id"] == "qsr_lunch_rush"


def test_seed_to_disk_writes_yaml():
    with tempfile.TemporaryDirectory() as td:
        out = svc.seed_to_disk(td)
        files = os.listdir(out)
        assert any(f.endswith(".yaml") for f in files)
        assert "qsr_lunch_rush.yaml" in files


# ── Router ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_list_templates(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        r = await client.get("/api/v1/wavef/templates")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] >= 6
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_get_and_clone(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        r1 = await client.get("/api/v1/wavef/templates/qsr_lunch_rush")
        assert r1.status_code == 200
        assert r1.json()["vertical"] == "qsr"

        r2 = await client.post(
            "/api/v1/wavef/templates/qsr_lunch_rush/clone",
            json={"brand_id": "b-cl1", "overrides": {"duration_days": 30}},
        )
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["campaign_id"].startswith("cmp_")
        assert body["merged"]["duration_days"] == 30
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_404s(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        r1 = await client.get("/api/v1/wavef/templates/no-such")
        assert r1.status_code == 404
        r2 = await client.post(
            "/api/v1/wavef/templates/no-such/clone",
            json={"brand_id": "b"},
        )
        assert r2.status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)
