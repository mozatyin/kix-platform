"""Consent router tests — policy publish, grant, revoke, document sign, export."""

from __future__ import annotations

import time

import pytest


async def _publish_policy(client, version: str = "v1"):
    res = await client.post(
        "/api/v1/consent/policy/publish",
        json={
            "version": version,
            "text_md": "# Privacy policy",
            "effective_at": int(time.time()),
            "requires_re_grant": False,
        },
    )
    assert res.status_code == 200, res.text
    return res


@pytest.mark.asyncio
async def test_consent_grant_after_policy_publish(client, clean_redis):
    await _publish_policy(client)
    res = await client.post(
        "/api/v1/consent/grant",
        json={
            "user_id": "u_cons_1",
            "scopes": ["marketing", "personalization"],
            "policy_version": "v1",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "marketing" in body["granted"]


@pytest.mark.asyncio
async def test_consent_grant_unknown_scope_rejected(client, clean_redis):
    """bug-bait: unknown scope must 400."""
    await _publish_policy(client)
    res = await client.post(
        "/api/v1/consent/grant",
        json={
            "user_id": "u_cons_2",
            "scopes": ["totally_made_up_scope"],
            "policy_version": "v1",
        },
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_consent_grant_without_policy_published_rejected(client, clean_redis):
    """Fail-closed: no policy on file ⇒ 409 conflict."""
    res = await client.post(
        "/api/v1/consent/grant",
        json={
            "user_id": "u_cons_3",
            "scopes": ["marketing"],
            "policy_version": "v1",
        },
    )
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_consent_regulated_scope_requires_evidence(client, clean_redis):
    """bug-bait: granting phi_storage without consent_evidence must be 400."""
    await _publish_policy(client)
    res = await client.post(
        "/api/v1/consent/grant",
        json={
            "user_id": "u_cons_4",
            "scopes": ["phi_storage"],
            "policy_version": "v1",
            # No consent_evidence provided.
        },
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_consent_revoke_round_trip(client, clean_redis):
    await _publish_policy(client)
    await client.post(
        "/api/v1/consent/grant",
        json={
            "user_id": "u_cons_5",
            "scopes": ["marketing"],
            "policy_version": "v1",
        },
    )
    res = await client.post(
        "/api/v1/consent/revoke",
        json={"user_id": "u_cons_5", "scopes": ["marketing"]},
    )
    assert res.status_code == 200
    assert "marketing" in res.json()["revoked"]

    # Check now reports not-allowed.
    check = await client.post(
        "/api/v1/consent/check",
        json={"user_id": "u_cons_5", "scope": "marketing"},
    )
    assert check.status_code == 200
    assert check.json()["allowed"] is False


@pytest.mark.asyncio
async def test_consent_document_sign_basic(client, clean_redis):
    res = await client.post(
        "/api/v1/consent/document/sign",
        json={
            "user_id": "u_cons_6",
            "document_type": "tos",
            "document_version": "2026-01-01",
            "signature_method": "click_agree",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["document_consent_id"].startswith("dcons_")


@pytest.mark.asyncio
async def test_consent_document_sign_strong_type_rejects_click(client, clean_redis):
    """bug-bait: medical_consent with click_agree must 400 (needs stronger method)."""
    res = await client.post(
        "/api/v1/consent/document/sign",
        json={
            "user_id": "u_cons_7",
            "document_type": "medical_consent",
            "document_version": "v1",
            "signature_method": "click_agree",
        },
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_consent_data_export_queues_job(client, clean_redis):
    res = await client.post(
        "/api/v1/consent/data/export",
        json={"user_id": "u_cons_8", "format": "json"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["export_id"]
    # Status may be "queued" if worker hasn't picked it up yet, or "ready"
    # if the sync executor ran inline. Both are valid initial states.
    assert body["status"] in {"queued", "ready", "running"}
