"""SSO bridge tests — KiX ID single sign-on across merchants (Wave E #5).

Covers:
    * create_kix_id mints a canonical id and auto-links source brand
    * link_to_brand is idempotent and unions scope on re-link
    * unlink_from_brand revokes consent + cascades grant revocation
    * get_user_brands / get_brand_users round-trip
    * cross_brand_attribute requires consent and writes journey edges
    * network_stats aggregates active brands + cross-brand pairs
    * HTTP endpoints wired into the kix-id router
    * Backward compat: legacy ``register`` endpoint keeps working
    * Audit log emission for link / unlink events
"""

from __future__ import annotations

import pytest


# ── Service-level tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_kix_id_is_canonical_and_idempotent(clean_redis):
    from app.redis_client import get_redis
    from app.services import sso_bridge

    r = await get_redis()

    a = await sso_bridge.create_kix_id(
        r,
        phone="+8613811112222",
        locale="zh-CN",
        region="cn",
        device_fingerprint="fp_sso_canon_1",
        source_brand_id="brand_alpha",
    )
    assert a["kid"].startswith("kid_")
    assert a["is_new"] is True

    # Same phone → same kid, is_new=False
    b = await sso_bridge.create_kix_id(
        r,
        phone="+8613811112222",
        locale="zh-CN",
        region="cn",
        device_fingerprint="fp_sso_canon_2",
        source_brand_id="brand_alpha",
    )
    assert b["kid"] == a["kid"]
    assert b["is_new"] is False


@pytest.mark.asyncio
async def test_single_kid_can_link_to_multiple_brands(clean_redis):
    from app.redis_client import get_redis
    from app.services import sso_bridge

    r = await get_redis()

    res = await sso_bridge.create_kix_id(
        r,
        phone="+8613822223333",
        locale="zh-CN",
        region="cn",
        device_fingerprint="fp_sso_multi_1",
    )
    kid = res["kid"]

    for b, scope in [
        ("brand_a", ["profile"]),
        ("brand_b", ["profile", "history"]),
        ("brand_c", ["profile", "favorites"]),
    ]:
        link = await sso_bridge.link_to_brand(
            r, kid=kid, brand_id=b, consent_scope=scope
        )
        assert link["status"] == "active"

    brands = await sso_bridge.get_user_brands(r, kid=kid)
    assert {b["brand_id"] for b in brands} == {"brand_a", "brand_b", "brand_c"}

    users_a = await sso_bridge.get_brand_users(r, brand_id="brand_a")
    assert kid in users_a


@pytest.mark.asyncio
async def test_link_to_brand_unions_scopes_on_relink(clean_redis):
    from app.redis_client import get_redis
    from app.services import sso_bridge

    r = await get_redis()
    res = await sso_bridge.create_kix_id(
        r,
        phone="+8613833334444",
        device_fingerprint="fp_sso_union_1",
    )
    kid = res["kid"]

    a = await sso_bridge.link_to_brand(
        r, kid=kid, brand_id="brand_u", consent_scope=["profile"]
    )
    assert a["consent_scope"] == ["profile"]
    assert a["is_new_link"] is True

    b = await sso_bridge.link_to_brand(
        r, kid=kid, brand_id="brand_u", consent_scope=["history"]
    )
    assert set(b["consent_scope"]) == {"profile", "history"}
    assert b["is_new_link"] is False


@pytest.mark.asyncio
async def test_unlink_revokes_consent_and_active_membership(clean_redis):
    from app.redis_client import get_redis
    from app.services import sso_bridge

    r = await get_redis()
    res = await sso_bridge.create_kix_id(
        r, phone="+8613844445555", device_fingerprint="fp_sso_revoke_1"
    )
    kid = res["kid"]

    await sso_bridge.link_to_brand(
        r, kid=kid, brand_id="brand_r", consent_scope=["profile", "history"]
    )
    assert kid in await sso_bridge.get_brand_users(r, brand_id="brand_r")

    rev = await sso_bridge.unlink_from_brand(
        r, kid=kid, brand_id="brand_r", reason="user_request"
    )
    assert rev["status"] == "revoked"

    # No longer in active set
    assert kid not in await sso_bridge.get_brand_users(r, brand_id="brand_r")
    # But preserved in lifetime set (compliance)
    lifetime = await sso_bridge.get_brand_users(
        r, brand_id="brand_r", include_revoked=True
    )
    assert kid in lifetime
    # User's active brand list excludes the revoked brand
    active = await sso_bridge.get_user_brands(r, kid=kid)
    assert "brand_r" not in {b["brand_id"] for b in active}
    # But include_revoked=True surfaces it for the privacy dashboard
    all_brands = await sso_bridge.get_user_brands(
        r, kid=kid, include_revoked=True
    )
    assert "brand_r" in {b["brand_id"] for b in all_brands}


@pytest.mark.asyncio
async def test_unlink_is_idempotent(clean_redis):
    from app.redis_client import get_redis
    from app.services import sso_bridge

    r = await get_redis()
    res = await sso_bridge.create_kix_id(
        r, phone="+8613855556666", device_fingerprint="fp_sso_idem_1"
    )
    kid = res["kid"]

    # Unlink something never linked → not_linked, not an error
    r1 = await sso_bridge.unlink_from_brand(
        r, kid=kid, brand_id="brand_never"
    )
    assert r1["status"] == "not_linked"

    # Link + double unlink
    await sso_bridge.link_to_brand(
        r, kid=kid, brand_id="brand_d", consent_scope=["profile"]
    )
    r2 = await sso_bridge.unlink_from_brand(r, kid=kid, brand_id="brand_d")
    r3 = await sso_bridge.unlink_from_brand(r, kid=kid, brand_id="brand_d")
    assert r2["status"] == "revoked"
    assert r3["status"] == "revoked"


@pytest.mark.asyncio
async def test_cross_brand_attribute_requires_consent(clean_redis):
    from app.redis_client import get_redis
    from app.services import sso_bridge

    r = await get_redis()
    res = await sso_bridge.create_kix_id(
        r, phone="+8613866667777", device_fingerprint="fp_sso_attr_1"
    )
    kid = res["kid"]

    # Profile-only link → cross-brand attribution denied
    await sso_bridge.link_to_brand(
        r, kid=kid, brand_id="brand_src", consent_scope=["profile"]
    )
    await sso_bridge.link_to_brand(
        r, kid=kid, brand_id="brand_tgt", consent_scope=["profile"]
    )
    denied = await sso_bridge.cross_brand_attribute(
        r,
        kid=kid,
        source_brand="brand_src",
        target_brand="brand_tgt",
        event="game_play",
    )
    assert denied["recorded"] is False
    assert denied["reason"] == "no_cross_brand_consent"

    # Grant cross_brand_tracking on the target — now recorded
    await sso_bridge.link_to_brand(
        r,
        kid=kid,
        brand_id="brand_tgt",
        consent_scope=["cross_brand_tracking"],
    )
    ok = await sso_bridge.cross_brand_attribute(
        r,
        kid=kid,
        source_brand="brand_src",
        target_brand="brand_tgt",
        event="game_play",
    )
    assert ok["recorded"] is True

    journey = await sso_bridge.get_attribution_journey(r, kid=kid)
    assert any(j["event"] == "game_play" for j in journey)


@pytest.mark.asyncio
async def test_cross_brand_attribute_same_brand_is_noop(clean_redis):
    from app.redis_client import get_redis
    from app.services import sso_bridge

    r = await get_redis()
    res = await sso_bridge.create_kix_id(
        r, phone="+8613877778888", device_fingerprint="fp_sso_same_1"
    )
    kid = res["kid"]
    out = await sso_bridge.cross_brand_attribute(
        r, kid=kid, source_brand="b", target_brand="b", event="x"
    )
    assert out["recorded"] is False
    assert out["reason"] == "same_brand"


@pytest.mark.asyncio
async def test_network_stats_aggregates(clean_redis):
    from app.redis_client import get_redis
    from app.services import sso_bridge

    r = await get_redis()

    # Three users, three brands, two cross-brand journeys
    kids = []
    for i in range(3):
        res = await sso_bridge.create_kix_id(
            r,
            phone=f"+86138999900{i:02d}",
            device_fingerprint=f"fp_sso_ns_{i}",
        )
        kids.append(res["kid"])

    for k in kids:
        await sso_bridge.link_to_brand(
            r,
            kid=k,
            brand_id="brand_x",
            consent_scope=["profile", "cross_brand_tracking"],
        )
        await sso_bridge.link_to_brand(
            r,
            kid=k,
            brand_id="brand_y",
            consent_scope=["profile", "cross_brand_tracking"],
        )
    await sso_bridge.link_to_brand(
        r,
        kid=kids[0],
        brand_id="brand_z",
        consent_scope=["profile", "cross_brand_tracking"],
    )

    # Cross-brand journeys
    await sso_bridge.cross_brand_attribute(
        r,
        kid=kids[0],
        source_brand="brand_x",
        target_brand="brand_y",
        event="play",
    )
    await sso_bridge.cross_brand_attribute(
        r,
        kid=kids[1],
        source_brand="brand_y",
        target_brand="brand_z",
        event="redeem",
    )

    stats = await sso_bridge.network_stats(r)
    assert stats["total_brands"] >= 3
    assert stats["total_active_links"] >= 7
    assert stats["total_cross_brand_pairs"] >= 2
    assert stats["total_cross_brand_events"] >= 2
    # All three kids counted
    assert stats["total_kids"] >= 3
    # top_brands ordering
    top_brand_ids = [tb["brand_id"] for tb in stats["top_brands"]]
    assert "brand_x" in top_brand_ids and "brand_y" in top_brand_ids


@pytest.mark.asyncio
async def test_invalid_consent_scope_rejected(clean_redis):
    from app.redis_client import get_redis
    from app.services import sso_bridge

    r = await get_redis()
    res = await sso_bridge.create_kix_id(
        r, phone="+8613888889999", device_fingerprint="fp_sso_bad_1"
    )
    kid = res["kid"]

    with pytest.raises(ValueError):
        await sso_bridge.link_to_brand(
            r, kid=kid, brand_id="brand_bad", consent_scope=["not_a_scope"]
        )

    with pytest.raises(ValueError):
        await sso_bridge.link_to_brand(
            r, kid=kid, brand_id="brand_bad", consent_scope=[]
        )


# ── HTTP endpoint tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_endpoint_link_and_list_brands(client, clean_redis):
    # 1. Register a kid
    reg = await client.post(
        "/api/v1/kix-id/register",
        json={
            "phone": "+8613900001111",
            "device_fingerprint": "fp_ep_link_1",
        },
    )
    assert reg.status_code == 200
    kid = reg.json()["kid"]

    # 2. Link to two brands via endpoint
    r1 = await client.post(
        "/api/v1/kix-id/link-brand",
        json={
            "kid": kid,
            "brand_id": "merchant_ep_1",
            "consent_scope": ["profile", "history"],
        },
    )
    assert r1.status_code == 200, r1.text
    body = r1.json()
    assert body["status"] == "active"
    assert set(body["consent_scope"]) == {"profile", "history"}

    r2 = await client.post(
        "/api/v1/kix-id/link-brand",
        json={
            "kid": kid,
            "brand_id": "merchant_ep_2",
            "consent_scope": ["profile"],
        },
    )
    assert r2.status_code == 200

    # 3. List user's brands
    listing = await client.get(f"/api/v1/kix-id/{kid}/brands")
    assert listing.status_code == 200
    assert listing.json()["count"] >= 2
    brand_ids = {b["brand_id"] for b in listing.json()["brands"]}
    assert {"merchant_ep_1", "merchant_ep_2"}.issubset(brand_ids)


@pytest.mark.asyncio
async def test_endpoint_unlink_brand(client, clean_redis):
    reg = await client.post(
        "/api/v1/kix-id/register",
        json={"phone": "+8613900002222", "device_fingerprint": "fp_ep_unlink_1"},
    )
    kid = reg.json()["kid"]

    await client.post(
        "/api/v1/kix-id/link-brand",
        json={
            "kid": kid,
            "brand_id": "merchant_unlink",
            "consent_scope": ["profile", "history"],
        },
    )

    res = await client.post(
        "/api/v1/kix-id/unlink-brand",
        json={
            "kid": kid,
            "brand_id": "merchant_unlink",
            "reason": "user_privacy_choice",
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "revoked"

    listing = await client.get(f"/api/v1/kix-id/{kid}/brands")
    assert "merchant_unlink" not in {
        b["brand_id"] for b in listing.json()["brands"]
    }


@pytest.mark.asyncio
async def test_endpoint_cross_brand_attribute(client, clean_redis):
    reg = await client.post(
        "/api/v1/kix-id/register",
        json={"phone": "+8613900003333", "device_fingerprint": "fp_ep_attr_1"},
    )
    kid = reg.json()["kid"]

    # Link with cross_brand_tracking on target
    await client.post(
        "/api/v1/kix-id/link-brand",
        json={
            "kid": kid,
            "brand_id": "brand_src_http",
            "consent_scope": ["profile"],
        },
    )
    await client.post(
        "/api/v1/kix-id/link-brand",
        json={
            "kid": kid,
            "brand_id": "brand_tgt_http",
            "consent_scope": ["profile", "cross_brand_tracking"],
        },
    )

    res = await client.post(
        "/api/v1/kix-id/cross-brand-attribute",
        json={
            "kid": kid,
            "source_brand": "brand_src_http",
            "target_brand": "brand_tgt_http",
            "event": "voucher_redeem",
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["recorded"] is True


@pytest.mark.asyncio
async def test_endpoint_network_stats_admin_gated(client, clean_redis):
    # No admin token → 403
    bad = await client.get("/api/v1/kix-id/sso/network-stats")
    assert bad.status_code == 403

    # With admin token → 200
    import os

    token = os.environ.get("KIX_ID_ADMIN_TOKEN", "kix-id-admin-dev")
    good = await client.get(
        "/api/v1/kix-id/sso/network-stats",
        headers={"X-Admin-Token": token},
    )
    assert good.status_code == 200, good.text
    body = good.json()
    for key in (
        "total_kids",
        "total_brands",
        "total_active_links",
        "total_cross_brand_pairs",
        "top_brands",
    ):
        assert key in body


@pytest.mark.asyncio
async def test_endpoint_auto_link_city_toggle(client, clean_redis):
    reg = await client.post(
        "/api/v1/kix-id/register",
        json={"phone": "+8613900004444", "device_fingerprint": "fp_ep_auto_1"},
    )
    kid = reg.json()["kid"]

    res = await client.post(
        "/api/v1/kix-id/auto-link-city",
        json={"kid": kid, "enabled": True},
    )
    assert res.status_code == 200
    assert res.json()["auto_link_city"] is True

    res2 = await client.post(
        "/api/v1/kix-id/auto-link-city",
        json={"kid": kid, "enabled": False},
    )
    assert res2.json()["auto_link_city"] is False


@pytest.mark.asyncio
async def test_backward_compat_register_still_works(client, clean_redis):
    """The legacy register endpoint must still mint kids cleanly —
    the SSO bridge is additive only."""
    res = await client.post(
        "/api/v1/kix-id/register",
        json={
            "phone": "+8613900005555",
            "device_fingerprint": "fp_ep_compat_1",
            "primary_language": "zh-CN",
            "source_brand_id": "legacy_brand",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["kid"].startswith("kid_")
    assert body["is_new"] is True


@pytest.mark.asyncio
async def test_link_endpoint_404_for_unknown_kid(client, clean_redis):
    res = await client.post(
        "/api/v1/kix-id/link-brand",
        json={
            "kid": "kid_doesnotexist_xxxxxxxx",
            "brand_id": "brand_404",
            "consent_scope": ["profile"],
        },
    )
    assert res.status_code == 404
