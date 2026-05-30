"""Portal Settings + Account Switcher tests.

12 in-memory ASGITransport tests covering profile, billing, payment
methods, team, notifications, integrations, security, demo-mode and
the multi-brand /accounts/me switcher.

All endpoints accept ``X-Owner-Id: <bid>`` for service-to-service auth;
we use that here so we don't have to mint a portal JWT in every test.
"""

from __future__ import annotations

import json

import pytest

BID = "b_settings_test"
HDRS = {"X-Owner-Id": BID}


async def _seed_wallet_currency(redis, bid: str, currency: str = "SGD") -> None:
    """Pin the brand's wallet currency so locale formatting picks SGD."""
    await redis.set(f"wallet:{bid}:currency", currency)


# ── 1. Auth gate ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auth_required_without_headers(client, clean_redis):
    """No Bearer + no X-Owner-Id → 401."""
    r = await client.get(f"/api/v1/portal/settings/profile/{BID}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_auth_wrong_owner_rejected(client, clean_redis):
    """X-Owner-Id that doesn't match the path bid → 401 (not 403)."""
    r = await client.get(
        f"/api/v1/portal/settings/profile/{BID}",
        headers={"X-Owner-Id": "someone_else"},
    )
    assert r.status_code == 401


# ── 2. Profile ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_profile_default_then_update(client, clean_redis):
    # Empty → defaults
    r = await client.get(
        f"/api/v1/portal/settings/profile/{BID}", headers=HDRS
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["brand_id"] == BID
    assert body["profile"]["business_type"] == "company"
    assert body["updated_at"] is None

    # Patch a couple of fields
    r = await client.put(
        f"/api/v1/portal/settings/profile/{BID}",
        json={"brand_name": "Demo Cafe", "tax_id": "201912345R"},
        headers=HDRS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["profile"]["brand_name"] == "Demo Cafe"
    assert body["profile"]["tax_id"] == "201912345R"
    assert body["updated_at"] is not None
    # Locale-formatted dt envelope
    assert "epoch_seconds" in body["updated_at"]
    assert "iso8601" in body["updated_at"]


# ── 3. Billing ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_billing_summary_empty(client, clean_redis):
    await _seed_wallet_currency(clean_redis, BID, "SGD")
    await clean_redis.set(f"wallet:{BID}:balance", 250_000)
    r = await client.get(
        f"/api/v1/portal/settings/billing/{BID}", headers=HDRS
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["currency"] == "SGD"
    assert body["current_balance"]["value_cents"] == 250_000
    assert body["current_balance"]["currency"] == "SGD"
    assert body["last_invoice"] is None
    assert body["invoices"]["count"] == 0


@pytest.mark.asyncio
async def test_billing_with_invoices(client, clean_redis):
    """Seed an invoice via raw redis keys (mirrors invoices.py layout)."""
    import time

    await _seed_wallet_currency(clean_redis, BID, "SGD")
    inv_id = "in_demo_001"
    ts = time.time()
    await clean_redis.zadd(f"customer:{BID}:invoices", {inv_id: ts})
    await clean_redis.hset(
        f"invoice:{inv_id}",
        mapping={
            "status": "paid",
            "total_cents": "10000",
            "amount_due_cents": "0",
            "amount_paid_cents": "10000",
            "created_at": str(ts),
            "paid_at": str(ts),
            "number": "INV-0001",
        },
    )
    r = await client.get(
        f"/api/v1/portal/settings/billing/{BID}", headers=HDRS
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["last_invoice"] is not None
    assert body["last_invoice"]["status"]["value"] == "paid"
    assert "display_label_i18n_key" in body["last_invoice"]["status"]


# ── 4. Payment methods ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_payment_methods_listing_and_auto_recharge_update(
    client, clean_redis
):
    await _seed_wallet_currency(clean_redis, BID, "SGD")
    # Seed one card
    pm_id = "pm_test_123"
    await clean_redis.sadd(f"brand:{BID}:payment_methods", pm_id)
    await clean_redis.set(f"brand:{BID}:payment_method:default", pm_id)
    await clean_redis.hset(
        f"payment_method:{pm_id}",
        mapping={
            "type": "card", "brand": "visa", "last4": "4242",
            "state": "active", "created_at": "1700000000",
        },
    )

    r = await client.get(
        f"/api/v1/portal/settings/payment-methods/{BID}", headers=HDRS
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["payment_methods"]) == 1
    assert body["payment_methods"][0]["is_default"] is True
    assert body["default_payment_method_id"] == pm_id
    assert body["auto_recharge"]["enabled"] is False

    # Now update auto-recharge
    r = await client.put(
        f"/api/v1/portal/settings/payment-methods/{BID}/auto-recharge",
        json={
            "enabled": True,
            "threshold_cents": 100_000,
            "recharge_amount_cents": 500_000,
            "payment_method_id": pm_id,
        },
        headers=HDRS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["auto_recharge"]["enabled"] is True
    assert body["auto_recharge"]["threshold"]["value_cents"] == 100_000
    assert body["auto_recharge"]["topup_amount"]["value_cents"] == 500_000


# ── 5. Team ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_team_seed_and_invite(client, clean_redis):
    # Empty roster → implicit owner
    r = await client.get(f"/api/v1/portal/settings/team/{BID}", headers=HDRS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["members"]) == 1
    assert body["members"][0]["is_implicit_owner"] is True
    assert "Admin" in body["roles"]

    # Invite a staff member
    r = await client.post(
        f"/api/v1/portal/settings/team/{BID}/invite",
        json={"email": "staff@example.com", "role": "Editor"},
        headers=HDRS,
    )
    assert r.status_code == 201, r.text
    invite = r.json()["invite"]
    assert invite["email"] == "staff@example.com"
    assert invite["role"] == "Editor"
    assert invite["status"] == "pending"

    # Bad role rejected
    r = await client.post(
        f"/api/v1/portal/settings/team/{BID}/invite",
        json={"email": "bad@example.com", "role": "GodKing"},
        headers=HDRS,
    )
    assert r.status_code == 422


# ── 6. Notification prefs ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notification_prefs_default_then_update(client, clean_redis):
    r = await client.get(
        f"/api/v1/portal/settings/notifications/{BID}", headers=HDRS
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # All categories present, email default on
    assert "billing" in body["preferences"]
    assert body["preferences"]["billing"]["email"] is True

    # Update — drop unknown category, flip valid one off
    r = await client.put(
        f"/api/v1/portal/settings/notifications/{BID}",
        json={"preferences": {
            "billing": {"email": False, "sms": True, "push": True},
            "totally_made_up": {"email": True},
        }},
        headers=HDRS,
    )
    assert r.status_code == 200, r.text
    prefs = r.json()["preferences"]
    assert prefs["billing"]["email"] is False
    assert prefs["billing"]["sms"] is True
    assert "totally_made_up" not in prefs


# ── 7. Integrations ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_integrations_registry_shape(client, clean_redis):
    await clean_redis.hset(
        f"brand:{BID}:integrations",
        mapping={"stripe": "1", "stripe:at": "1700000000"},
    )
    r = await client.get(
        f"/api/v1/portal/settings/integrations/{BID}", headers=HDRS
    )
    assert r.status_code == 200, r.text
    body = r.json()
    by_key = {x["key"]: x for x in body["integrations"]}
    assert "stripe" in by_key and by_key["stripe"]["connected"] is True
    assert "meta" in by_key and by_key["meta"]["connected"] is False
    # 8 integrations registered
    assert body["count"] == 8


# ── 8. Security ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_security_overview(client, clean_redis):
    await clean_redis.hset(
        f"brand:{BID}:security",
        mapping={"2fa_enabled": "1", "2fa_method": "totp"},
    )
    await clean_redis.set(f"brand:{BID}:last_login", "1700000000")
    await clean_redis.lpush(
        f"brand:{BID}:sessions",
        json.dumps({"session_id": "s1", "ip": "1.1.1.1",
                    "last_seen": 1700000000}),
    )
    r = await client.get(
        f"/api/v1/portal/settings/security/{BID}", headers=HDRS
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["two_factor_enabled"] is True
    assert body["two_factor_method"] == "totp"
    assert body["session_count"] == 1
    assert body["last_login"]["epoch_seconds"] == 1700000000


# ── 9. Demo mode ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_demo_mode_toggle_then_billing_empty(client, clean_redis):
    # Default: demo enabled (no key set)
    r = await client.get(
        f"/api/v1/portal/settings/demo-mode/{BID}", headers=HDRS
    )
    assert r.status_code == 200, r.text
    assert r.json()["demo_enabled"] is True

    # Disable demo
    r = await client.put(
        f"/api/v1/portal/settings/demo-mode/{BID}",
        json={"demo_enabled": False},
        headers=HDRS,
    )
    assert r.status_code == 200, r.text
    assert r.json()["demo_enabled"] is False

    # Billing now returns empty arrays even if invoices exist on-disk
    await _seed_wallet_currency(clean_redis, BID, "SGD")
    await clean_redis.set(f"wallet:{BID}:balance", 100)
    await clean_redis.zadd(
        f"customer:{BID}:invoices", {"in_x": 1700000000}
    )
    await clean_redis.hset(
        f"invoice:in_x", mapping={"status": "paid", "total_cents": "1"}
    )
    r = await client.get(
        f"/api/v1/portal/settings/billing/{BID}", headers=HDRS
    )
    body = r.json()
    assert body.get("demo_mode_disabled") is True
    assert body["invoices"]["count"] == 0


# ── 10. /accounts/me ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_accounts_me_with_owner_header(client, clean_redis):
    """X-Owner-Id alone resolves at least one brand entry."""
    # Seed profile so we get a friendly name
    await clean_redis.hset(
        f"brand:{BID}:profile",
        mapping={"brand_name": "Demo Cafe", "logo_url": "https://x/y.png"},
    )
    # Multi-brand: explicit user→brands set
    await clean_redis.sadd(f"user:{BID}:owned_brands", BID, "b_other")
    r = await client.get(
        "/api/v1/portal/accounts/me", headers={"X-Owner-Id": BID}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    bids = {b["brand_id"] for b in body["brands"]}
    assert BID in bids
    assert "b_other" in bids
    assert body["active_brand_id"] == BID
    # Returned active brand has friendly profile name
    me = next(b for b in body["brands"] if b["brand_id"] == BID)
    assert me["brand_name"] == "Demo Cafe"
    assert me["is_active"] is True
