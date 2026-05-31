"""Tests for app/services/whatsapp_template.py — Bedok ops sender.

Dry-run by default; no Meta API hits. Uses httpx mock when verifying the
non-dry-run send path.
"""
import datetime as _dt
import os
from unittest.mock import MagicMock

import pytest

from app.services.whatsapp_template import (
    Merchant,
    TEMPLATE_DEFS,
    send_bedok_template,
    schedule_bedok_followups,
    _build_template_payload,
    _is_dry_run,
)


@pytest.fixture(autouse=True)
def _force_dry_run(monkeypatch):
    """Most tests run dry-run; explicit tests opt-out via env-var set."""
    monkeypatch.setenv("KIX_WHATSAPP_DRY_RUN", "1")


@pytest.fixture
def merchant():
    return Merchant(
        brand_id="brand_abc",
        first_name="Uncle Ng",
        phone_e164="+6591234567",
        consent_whatsapp=True,
    )


# ── basic dispatch ──

def test_dry_run_returns_ok(merchant):
    r = send_bedok_template("bedok_t24h", merchant, {
        "merchant_first_name": "Uncle Ng",
        "plays_24h": "47", "regs_24h": "23", "redeems_24h": "8",
        "spent_24h": "S$32.40",
    })
    assert r.ok
    assert r.dry_run
    assert r.template == "bedok_t24h"
    assert r.merchant_brand_id == "brand_abc"


def test_unknown_template_fails(merchant):
    r = send_bedok_template("bedok_nope", merchant, {})
    assert not r.ok
    assert "unknown template" in r.error


def test_consent_false_skips(merchant):
    merchant.consent_whatsapp = False
    r = send_bedok_template("bedok_t24h", merchant, {})
    assert not r.ok
    assert "opted in" in r.skipped_reason


def test_phone_format_validated(merchant):
    merchant.phone_e164 = "6591234567"  # missing +
    r = send_bedok_template("bedok_t24h", merchant, {})
    assert not r.ok
    assert "E.164" in r.error


# ── payload shape ──

def test_payload_param_order_preserved():
    payload = _build_template_payload("bedok_t72h",
        ["Uncle Ng", "S$4.20", "Spice Roulette", "12", "fraud blocked", "added BM"])
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "bedok_t72h"
    assert payload["template"]["language"]["code"] == "en_SG"
    body = payload["template"]["components"][0]
    assert body["type"] == "body"
    assert [p["text"] for p in body["parameters"]] == [
        "Uncle Ng", "S$4.20", "Spice Roulette", "12", "fraud blocked", "added BM"
    ]


def test_payload_empty_params_skips_body_component():
    payload = _build_template_payload("bedok_t24h", ["", "", "", "", ""])
    assert payload["template"]["components"][0]["type"] == "body"
    assert len(payload["template"]["components"][0]["parameters"]) == 5


# ── all 4 templates defined ──

def test_all_four_templates_defined():
    assert set(TEMPLATE_DEFS.keys()) == {"bedok_t24h", "bedok_t72h", "bedok_t7d", "bedok_t14d"}


def test_each_template_has_param_order():
    for name, tdef in TEMPLATE_DEFS.items():
        assert "param_order" in tdef, f"{name} missing param_order"
        assert len(tdef["param_order"]) >= 4, f"{name} too few params"
        assert tdef["param_order"][0] == "merchant_first_name", f"{name} first param must be merchant_first_name"


# ── scheduling ──

def test_schedule_returns_4_followups():
    start = _dt.datetime(2026, 5, 31, 10, 0, tzinfo=_dt.timezone.utc)
    plan = schedule_bedok_followups(start)
    assert len(plan) == 4
    assert [f.template for f in plan] == ["bedok_t24h", "bedok_t72h", "bedok_t7d", "bedok_t14d"]
    assert plan[0].fire_at == start + _dt.timedelta(hours=24)
    assert plan[1].fire_at == start + _dt.timedelta(hours=72)
    assert plan[2].fire_at == start + _dt.timedelta(days=7)
    assert plan[3].fire_at == start + _dt.timedelta(days=14)


def test_schedule_requires_tz_aware():
    naive = _dt.datetime(2026, 5, 31, 10, 0)
    with pytest.raises(ValueError):
        schedule_bedok_followups(naive)


# ── real-send path (with mocked httpx) ──

def test_real_send_path_with_mock(merchant, monkeypatch):
    monkeypatch.delenv("KIX_WHATSAPP_DRY_RUN", raising=False)
    monkeypatch.setenv("WHATSAPP_CLOUD_TOKEN", "fake-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1234567890")
    assert not _is_dry_run()

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"messages": [{"id": "wamid.XYZ"}]}
    mock_client.post.return_value = mock_response

    r = send_bedok_template("bedok_t24h", merchant, {
        "merchant_first_name": "Uncle Ng",
        "plays_24h": "47", "regs_24h": "23",
        "redeems_24h": "8", "spent_24h": "S$32.40",
    }, http_client=mock_client)

    assert r.ok
    assert not r.dry_run
    assert r.api_response == {"messages": [{"id": "wamid.XYZ"}]}
    call = mock_client.post.call_args
    assert "graph.facebook.com/v19.0/1234567890/messages" in call.args[0]
    assert call.kwargs["headers"]["Authorization"] == "Bearer fake-token"
    sent_payload = call.kwargs["json"]
    assert sent_payload["to"] == "6591234567"
    assert sent_payload["template"]["name"] == "bedok_t24h"


def test_real_send_http_error(merchant, monkeypatch):
    monkeypatch.delenv("KIX_WHATSAPP_DRY_RUN", raising=False)
    monkeypatch.setenv("WHATSAPP_CLOUD_TOKEN", "fake-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1234567890")

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.text = "Invalid token"
    mock_client.post.return_value = mock_response

    r = send_bedok_template("bedok_t24h", merchant,
        {"merchant_first_name": "X", "plays_24h":"0","regs_24h":"0","redeems_24h":"0","spent_24h":"S$0"},
        http_client=mock_client)
    assert not r.ok
    assert "HTTP 401" in r.error


def test_real_send_exception(merchant, monkeypatch):
    monkeypatch.delenv("KIX_WHATSAPP_DRY_RUN", raising=False)
    monkeypatch.setenv("WHATSAPP_CLOUD_TOKEN", "fake-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1234567890")

    mock_client = MagicMock()
    mock_client.post.side_effect = TimeoutError("upstream timeout")

    r = send_bedok_template("bedok_t24h", merchant,
        {"merchant_first_name": "X", "plays_24h":"0","regs_24h":"0","redeems_24h":"0","spent_24h":"S$0"},
        http_client=mock_client)
    assert not r.ok
    assert "TimeoutError" in r.error
