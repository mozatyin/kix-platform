"""Portal Pixels management tests.

8 tests covering create / list / detail / install-snippet (3 flavours)
/ test-event / events-listing — all with the ``X-Owner-Id`` auth path.
"""

from __future__ import annotations

import pytest

BID = "b_pixels_test"
HDRS = {"X-Owner-Id": BID}


@pytest.mark.asyncio
async def test_create_pixel_returns_id_and_snippet(client, clean_redis):
    r = await client.post(
        f"/api/v1/portal/pixels/{BID}/create",
        json={"name": "Main store", "website_url": "https://demo.example"},
        headers=HDRS,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["pixel_id"].startswith("px_")
    assert body["brand_id"] == BID
    assert body["name"] == "Main store"
    assert body["events_total"] == 0
    # Default snippet returned
    snippet = body["install_snippet"]
    assert snippet["language"] == "html"
    assert body["pixel_id"] in snippet["code"]


@pytest.mark.asyncio
async def test_list_pixels_pagination(client, clean_redis):
    for i in range(3):
        r = await client.post(
            f"/api/v1/portal/pixels/{BID}/create",
            json={"name": f"pixel_{i}"},
            headers=HDRS,
        )
        assert r.status_code == 201, r.text
    r = await client.get(
        f"/api/v1/portal/pixels/{BID}?limit=2&offset=0", headers=HDRS
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2


@pytest.mark.asyncio
async def test_pixel_detail_404(client, clean_redis):
    r = await client.get(
        f"/api/v1/portal/pixels/{BID}/px_does_not_exist", headers=HDRS
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_install_snippet_all_integrations(client, clean_redis):
    r = await client.post(
        f"/api/v1/portal/pixels/{BID}/create",
        json={"name": "Main store"},
        headers=HDRS,
    )
    pid = r.json()["pixel_id"]
    for integ, lang in (
        ("shopify", "liquid"), ("wordpress", "php"), ("custom", "html"),
    ):
        r = await client.get(
            f"/api/v1/portal/pixels/{BID}/{pid}/install-snippet"
            f"?integration={integ}",
            headers=HDRS,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["language"] == lang
        assert pid in body["code"]


@pytest.mark.asyncio
async def test_install_snippet_rejects_unknown(client, clean_redis):
    r = await client.post(
        f"/api/v1/portal/pixels/{BID}/create",
        json={"name": "Main store"},
        headers=HDRS,
    )
    pid = r.json()["pixel_id"]
    r = await client.get(
        f"/api/v1/portal/pixels/{BID}/{pid}/install-snippet?integration=wix",
        headers=HDRS,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_test_event_increments_stats(client, clean_redis):
    r = await client.post(
        f"/api/v1/portal/pixels/{BID}/create",
        json={"name": "Main store"},
        headers=HDRS,
    )
    pid = r.json()["pixel_id"]
    r = await client.post(
        f"/api/v1/portal/pixels/{BID}/{pid}/test-event",
        json={"event_type": "Purchase", "value_cents": 4_999,
              "currency": "SGD"},
        headers=HDRS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["event"]["is_test"] is True
    assert body["event"]["value_cents"] == 4999
    # Detail now shows events_total=1
    r = await client.get(
        f"/api/v1/portal/pixels/{BID}/{pid}", headers=HDRS
    )
    assert r.json()["events_total"] == 1
    assert r.json()["recent_events"][0]["event_type"] == "Purchase"


@pytest.mark.asyncio
async def test_events_listing(client, clean_redis):
    r = await client.post(
        f"/api/v1/portal/pixels/{BID}/create",
        json={"name": "Main store"},
        headers=HDRS,
    )
    pid = r.json()["pixel_id"]
    for i in range(3):
        await client.post(
            f"/api/v1/portal/pixels/{BID}/{pid}/test-event",
            json={"event_type": "AddToCart", "value_cents": 100 + i,
                  "currency": "SGD"},
            headers=HDRS,
        )
    r = await client.get(
        f"/api/v1/portal/pixels/{BID}/{pid}/events?limit=10", headers=HDRS
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 3
    # Most recent first
    assert body["items"][0]["value_cents"] == 102


@pytest.mark.asyncio
async def test_auth_required(client, clean_redis):
    r = await client.post(
        f"/api/v1/portal/pixels/{BID}/create",
        json={"name": "X"},
    )
    assert r.status_code == 401
