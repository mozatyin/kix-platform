"""ML router — admin-gated training, list models, health (smoke)."""

from __future__ import annotations

import os
import pytest


@pytest.mark.asyncio
async def test_health(client, clean_redis):
    res = await client.get("/api/v1/ml/health")
    assert res.status_code == 200
    body = res.json()
    assert "ml_enabled" in body


@pytest.mark.asyncio
async def test_list_models_empty(client, clean_redis):
    res = await client.get("/api/v1/ml/models")
    assert res.status_code == 200
    body = res.json()
    assert "items" in body
    assert body["count"] >= 0


@pytest.mark.asyncio
async def test_list_jobs_empty(client, clean_redis):
    res = await client.get("/api/v1/ml/jobs")
    assert res.status_code == 200
    assert "items" in res.json()


@pytest.mark.asyncio
async def test_get_job_404(client, clean_redis):
    res = await client.get("/api/v1/ml/jobs/job_doesnotexist")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_train_admin_token_required(client, clean_redis):
    res = await client.post(
        "/api/v1/ml/train/quality_score",
        json={"admin_token": "wrong", "train_period_days": 7},
    )
    # KIX_ADMIN_TOKEN unset → fail-closed → 403
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_train_unknown_model_404(client, clean_redis):
    os.environ["KIX_ADMIN_TOKEN"] = "test-admin-token"
    try:
        res = await client.post(
            "/api/v1/ml/train/not_a_model",
            json={"admin_token": "test-admin-token"},
        )
        assert res.status_code == 404
    finally:
        os.environ.pop("KIX_ADMIN_TOKEN", None)
