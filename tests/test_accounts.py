"""Accounts router tests — register, multi-user uniqueness, member roles,
buying-committee filtering, org-chart subtree traversal + cycle detection.

Covers the high-priority untested surface called out in the Trinity-E audit.
"""

from __future__ import annotations

import pytest


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


async def _register(client, *, name: str = "Acme Co", primary: str = "u_primary") -> str:
    res = await client.post(
        "/api/v1/accounts/register",
        json={
            "account_name": name,
            "industry": "tech",
            "size": "11-50",
            "primary_contact_user_id": primary,
        },
    )
    assert res.status_code == 200, res.text
    return res.json()["account_id"]


async def _add_member(client, aid: str, uid: str, role: str = "end_user") -> None:
    res = await client.post(
        f"/api/v1/accounts/{aid}/members/add",
        json={"user_id": uid, "role": role},
    )
    assert res.status_code == 200, res.text


# ──────────────────────────────────────────────────────────────────────────
# Account registration
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_account_creates_decision_maker_primary(client, clean_redis):
    aid = await _register(client, primary="alice")
    assert aid.startswith("acct_")

    # Primary contact auto-enrolled as decision_maker.
    res = await client.get(f"/api/v1/accounts/{aid}/members")
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 1
    assert body["members"][0]["user_id"] == "alice"
    assert body["members"][0]["role"] == "decision_maker"


@pytest.mark.asyncio
async def test_user_to_account_reverse_lookup(client, clean_redis):
    aid = await _register(client, primary="bob")
    res = await client.get("/api/v1/accounts/user/bob/account")
    assert res.status_code == 200
    body = res.json()
    assert body["account_id"] == aid
    assert body["role"] == "decision_maker"


@pytest.mark.asyncio
async def test_user_to_account_returns_null_for_unknown(client, clean_redis):
    res = await client.get("/api/v1/accounts/user/never_seen/account")
    assert res.status_code == 200
    assert res.json()["account_id"] is None


# ──────────────────────────────────────────────────────────────────────────
# Multi-user uniqueness — bug-bait
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_cannot_belong_to_two_accounts(client, clean_redis):
    """BUG-BAIT: a user can only be a member of one account at a time."""
    a1 = await _register(client, name="Acme", primary="alice")
    a2 = await _register(client, name="Globex", primary="bob")

    # Adding bob to a1 must conflict — bob already belongs to a2 (auto-enrolled
    # as a2's primary contact).
    res = await client.post(
        f"/api/v1/accounts/{a1}/members/add",
        json={"user_id": "bob", "role": "influencer"},
    )
    assert res.status_code == 409, res.text
    detail = res.json().get("detail", {})
    assert detail.get("error") == "user_already_in_account"
    assert detail.get("existing_account_id") == a2


# ──────────────────────────────────────────────────────────────────────────
# Member role assignment
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_member_role_persists(client, clean_redis):
    aid = await _register(client, primary="alice")
    await _add_member(client, aid, "carol", role="end_user")

    res = await client.post(
        f"/api/v1/accounts/{aid}/members/carol/role",
        json={"role": "procurement", "department": "Finance"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["role"] == "procurement"
    assert body["department"] == "Finance"


@pytest.mark.asyncio
async def test_update_role_member_not_found_404(client, clean_redis):
    aid = await _register(client, primary="alice")
    res = await client.post(
        f"/api/v1/accounts/{aid}/members/ghost/role",
        json={"role": "executive"},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_add_member_invalid_role_422(client, clean_redis):
    """BUG-BAIT: Literal validation rejects unknown roles."""
    aid = await _register(client, primary="alice")
    res = await client.post(
        f"/api/v1/accounts/{aid}/members/add",
        json={"user_id": "dave", "role": "intern"},
    )
    assert res.status_code == 422


# ──────────────────────────────────────────────────────────────────────────
# Buying-committee filter
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_buying_committee_excludes_end_users(client, clean_redis):
    aid = await _register(client, primary="alice")  # decision_maker
    await _add_member(client, aid, "bob", role="end_user")  # excluded
    await _add_member(client, aid, "carol", role="procurement")  # included
    await _add_member(client, aid, "dave", role="executive")  # included

    res = await client.get(f"/api/v1/accounts/{aid}/buying-committee")
    assert res.status_code == 200, res.text
    body = res.json()
    user_ids = {m["user_id"] for m in body["members"]}
    assert user_ids == {"alice", "carol", "dave"}
    assert "bob" not in user_ids


# ──────────────────────────────────────────────────────────────────────────
# Org chart — edge add + cycle detection + subtree traversal
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_org_chart_subtree_traversal(client, clean_redis):
    aid = await _register(client, primary="ceo")
    await _add_member(client, aid, "vp1", role="executive")
    await _add_member(client, aid, "vp2", role="executive")
    await _add_member(client, aid, "ic1", role="end_user")
    await _add_member(client, aid, "ic2", role="end_user")

    # ceo -> vp1 -> ic1; ceo -> vp2 -> ic2
    for manager, report in [
        ("ceo", "vp1"), ("ceo", "vp2"),
        ("vp1", "ic1"), ("vp2", "ic2"),
    ]:
        res = await client.post(
            f"/api/v1/accounts/{aid}/org-chart/edge",
            json={"manager_user_id": manager, "report_user_id": report},
        )
        assert res.status_code == 200, res.text

    res = await client.get(
        f"/api/v1/accounts/{aid}/org-chart/subtree",
        params={"root_user_id": "ceo", "max_depth": 5},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["node_count"] == 5
    user_ids = {n["user_id"] for n in body["nodes"]}
    assert user_ids == {"ceo", "vp1", "vp2", "ic1", "ic2"}

    # Subtree from a leaf returns just the leaf at depth 0.
    res = await client.get(
        f"/api/v1/accounts/{aid}/org-chart/subtree",
        params={"root_user_id": "ic1"},
    )
    body = res.json()
    assert body["node_count"] == 1
    assert body["nodes"][0]["user_id"] == "ic1"
    assert body["nodes"][0]["depth"] == 0


@pytest.mark.asyncio
async def test_org_chart_rejects_self_loop_and_cycle(client, clean_redis):
    """BUG-BAIT: cycle detection — manager → report → manager must 409."""
    aid = await _register(client, primary="alice")
    await _add_member(client, aid, "bob", role="executive")
    await _add_member(client, aid, "carol", role="executive")

    # Self-loop rejected.
    res = await client.post(
        f"/api/v1/accounts/{aid}/org-chart/edge",
        json={"manager_user_id": "bob", "report_user_id": "bob"},
    )
    assert res.status_code == 400
    assert "self_loop" in str(res.json().get("detail", "")).lower()

    # Build alice -> bob -> carol.
    await client.post(
        f"/api/v1/accounts/{aid}/org-chart/edge",
        json={"manager_user_id": "alice", "report_user_id": "bob"},
    )
    await client.post(
        f"/api/v1/accounts/{aid}/org-chart/edge",
        json={"manager_user_id": "bob", "report_user_id": "carol"},
    )

    # Now try carol -> alice → would create a cycle.
    res = await client.post(
        f"/api/v1/accounts/{aid}/org-chart/edge",
        json={"manager_user_id": "carol", "report_user_id": "alice"},
    )
    assert res.status_code == 409, res.text
    detail = res.json().get("detail")
    assert detail == "cycle_detected"


@pytest.mark.asyncio
async def test_org_chart_edge_requires_both_users_to_be_members(client, clean_redis):
    aid = await _register(client, primary="alice")
    # Try to point alice → ghost (not a member).
    res = await client.post(
        f"/api/v1/accounts/{aid}/org-chart/edge",
        json={"manager_user_id": "alice", "report_user_id": "ghost"},
    )
    assert res.status_code == 404
