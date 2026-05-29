"""Viral compounding tests — K-factor, inheritance depth cap, A/B, quota guard.

These cover the P0 fix for the dead viral loop found in the 30-day SG F&B
sim (K-factor=0). The fix turns single-leg invites into a compounding
multi-leg tree by auto-emitting a fresh invite token to every redeemer,
capped at MAX_INHERITANCE_DEPTH to prevent runaway recursion.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.routers import network_effect as ne


# ── Helpers ────────────────────────────────────────────────────────────────


async def _init_share_to_win(client, *, user_id: str, brand_id: str) -> str:
    res = await client.post(
        "/api/v1/network/share-to-win",
        json={
            "user_id": user_id,
            "brand_id": brand_id,
            "score": 1234,
            "game_slug": "ttt",
        },
    )
    assert res.status_code == 200, res.text
    return res.json()["invite_token"]


async def _redeem(client, *, token: str, new_user_id: str, brand_id: str) -> dict:
    res = await client.post(
        "/api/v1/network/redeem",
        json={
            "invite_token": token,
            "new_user_id": new_user_id,
            "brand_id": brand_id,
        },
    )
    assert res.status_code == 200, res.text
    return res.json()


# ── Test 1: auto-emit on redeem ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redeem_auto_emits_new_invite(client, clean_redis):
    """Redeeming an invite must mint a fresh token to the new user
    tagged with the original inviter for tree tracking."""
    brand = "brand_auto_emit"
    inviter = "u_root"
    redeemer = "u_child"

    token = await _init_share_to_win(client, user_id=inviter, brand_id=brand)
    result = await _redeem(
        client, token=token, new_user_id=redeemer, brand_id=brand
    )

    auto = result.get("auto_emitted_invite")
    assert auto is not None and auto["emitted"] is True, result
    assert auto["depth"] == 1
    assert auto["inherited_from"] == inviter
    assert auto["invite_token"] and auto["invite_token"] != token

    # The freshly emitted invite must be redeemable too — proves the
    # loop now actually compounds.
    grandchild = await _redeem(
        client,
        token=auto["invite_token"],
        new_user_id="u_grandchild",
        brand_id=brand,
    )
    assert grandchild["inviter_id"] == redeemer
    assert grandchild["auto_emitted_invite"]["emitted"] is True
    assert grandchild["auto_emitted_invite"]["depth"] == 2
    # Root inviter is preserved across the tree.
    assert grandchild["auto_emitted_invite"]["inherited_from"] == inviter


# ── Test 2: inheritance depth cap ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_inheritance_depth_capped(client, clean_redis):
    """The auto-emit chain must stop at MAX_INHERITANCE_DEPTH (5).
    Redemption itself still succeeds — only the *new* token is withheld."""
    brand = "brand_depth_cap"

    token = await _init_share_to_win(client, user_id="u0", brand_id=brand)
    current_token = token
    parent = "u0"
    # Walk the chain. Each redeem mints a child token until cap.
    for depth in range(1, ne.MAX_INHERITANCE_DEPTH + 1):
        child = f"u{depth}"
        result = await _redeem(
            client, token=current_token, new_user_id=child, brand_id=brand
        )
        auto = result["auto_emitted_invite"]
        assert auto["emitted"] is True, f"unexpected stop at depth {depth}"
        assert auto["depth"] == depth
        current_token = auto["invite_token"]
        parent = child

    # Now we are at depth==MAX. Redeeming the last auto-emitted token
    # must succeed but MUST NOT emit a (depth+1) token.
    final_user = f"u{ne.MAX_INHERITANCE_DEPTH + 1}"
    final = await _redeem(
        client, token=current_token, new_user_id=final_user, brand_id=brand
    )
    auto = final["auto_emitted_invite"]
    assert auto["emitted"] is False
    assert auto.get("reason") == "depth_cap_reached"
    assert auto.get("depth_cap") == ne.MAX_INHERITANCE_DEPTH


# ── Test 3: K-factor endpoint ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_k_factor_endpoint_reports_trailing_metric(client, clean_redis):
    """K-factor endpoint must reflect counters from the trailing window
    and include per-mechanic breakdown."""
    brand = "brand_kf_metric"

    # Issue 3 invites, redeem 1 — note: every redemption auto-emits a
    # fresh issued invite, so K_issued grows automatically too.
    t1 = await _init_share_to_win(client, user_id="A", brand_id=brand)
    t2 = await _init_share_to_win(client, user_id="B", brand_id=brand)
    _ = await _init_share_to_win(client, user_id="C", brand_id=brand)
    await _redeem(client, token=t1, new_user_id="A2", brand_id=brand)
    await _redeem(client, token=t2, new_user_id="B2", brand_id=brand)

    res = await client.get(f"/api/v1/network/k-factor/{brand}")
    assert res.status_code == 200, res.text
    body = res.json()
    # 3 manual + 2 auto-emitted = 5 issued. 2 redeemed.
    assert body["invites_issued"] == 5
    assert body["invites_redeemed"] == 2
    assert body["k_factor"] == round(2 / 5, 4)
    assert body["window_days"] == ne.DEFAULT_K_WINDOW_DAYS
    # Per-mechanic breakdown should expose share_to_win counts
    mech = body["per_mechanic"]
    assert mech["share_to_win"]["issued"] == 5
    assert mech["share_to_win"]["redeemed"] == 2
    # Untouched mechanics report zeros, never error
    assert mech["auto_share"]["issued"] == 0
    assert mech["auto_share"]["k_factor"] == 0.0
    # Below explosion threshold — flag should be off
    assert body["explosion_warning"] is False


# ── Test 4: explosion warning at K>1.0 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_explosion_warning_flips_when_k_exceeds_one(client, clean_redis):
    """When K>1.0 the brand should be flagged for ops review.
    Directly drive the daily counters so we don't depend on depth-cap
    dynamics (which legitimately bound real-world K)."""
    brand = "brand_explosion"
    r = clean_redis
    today = date.today().isoformat()

    # 10 issued, 12 redeemed → K = 1.2 > 1.0
    await r.set(ne._k_viral_issued_day(brand, today), 10)
    await r.set(ne._k_viral_redeemed_day(brand, today), 12)

    res = await client.get(f"/api/v1/network/k-factor/{brand}")
    assert res.status_code == 200
    body = res.json()
    assert body["k_factor"] == 1.2
    assert body["explosion_warning"] is True
    # Redis explosion flag persisted
    assert await r.get(ne._k_explosion_warning(brand)) == "1"

    # When K drops back below threshold the flag must clear.
    await r.set(ne._k_viral_redeemed_day(brand, today), 5)
    res2 = await client.get(f"/api/v1/network/k-factor/{brand}")
    body2 = res2.json()
    assert body2["k_factor"] == 0.5
    assert body2["explosion_warning"] is False
    assert await r.get(ne._k_explosion_warning(brand)) is None


# ── Test 5: personalization respects quota guard ───────────────────────────


@pytest.mark.asyncio
async def test_personalized_message_respects_quota_guard(
    client, clean_redis, monkeypatch
):
    """When the LLM quota monitor reports paused, the personalized
    endpoint must NOT call the LLM and must serve a deterministic
    template fallback."""

    # Pin the inviter into the "personalized" arm so we *would* call LLM.
    brand = "brand_quota_guard"
    inviter = "u_persona"

    # Pre-assign the arm so the test is deterministic.
    r = clean_redis
    await r.set(ne._k_ab_assignment(brand, inviter), "personalized")

    # Patch the quota check to claim paused.
    async def fake_paused() -> bool:
        return True

    # Tripwire: if the LLM HTTP path runs we should see the explosion.
    class _ExplodingClient:
        def __init__(self, *a, **kw):
            raise RuntimeError(
                "LLM HTTP call must not happen when quota is paused"
            )

    monkeypatch.setattr(ne, "_llm_quota_paused", fake_paused)
    monkeypatch.setattr(ne.httpx, "AsyncClient", _ExplodingClient)

    res = await client.get(
        "/api/v1/network/share-to-win/personalized-message",
        params={
            "inviter": inviter,
            "invitee_persona": "competitive",
            "brand_id": brand,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ab_arm"] == "personalized"
    # Quota paused → source falls back to template even though arm is personalized
    assert body["source"] == "template"
    # Message is one of the bundled templates (not an LLM hallucination)
    assert body["message"] in ne._TEMPLATE_INVITES.values()

    # A/B "issued" counter must still increment even on fallback —
    # the arm assignment is what we're measuring.
    issued = await r.get(ne._k_ab_metric(brand, "personalized", "issued"))
    assert int(issued or 0) == 1
