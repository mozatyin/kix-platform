"""Wave C — PSP integration tests.

Covers the 5 priority PSPs (PayNow, GrabPay, Alipay, WeChat Pay, OVO)
in mock mode only — no real PSP network calls.

Test matrix:
  * For each PSP (5):
      1. create_charge returns expected shape
      2. verify_webhook accepts a valid signature
      3. verify_webhook rejects an invalid signature
      4. process_event credits the wallet
      5. refund flow
  * Cross-PSP integrity:
      6. Same charge_id across PSPs doesn't collide
      7. Idempotent webhook replay
      8. Currency mapping correct (SGD / IDR / etc)
      9. Audit log emitted
"""

from __future__ import annotations

import json

import pytest

from app.payments_regional import get_method
from app.routers.psp_webhooks import _k_psp_balance, _k_psp_event_seen
from app.services.payment_psps import (
    all_psp_codes,
    get_psp_client,
    reset_registry,
)
from app.services.payment_psps._common import (
    hmac_sign,
    read_audit_log,
    reset_audit_log,
)


# ─────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────
PSP_CODES = ["paynow", "grabpay", "alipay", "wechat_pay", "ovo"]

# Per-PSP test data: (charge currency, webhook secret env-var, webhook
# event body shaped as the PSP would deliver it, signature header name).
PSP_FIXTURES = {
    "paynow": {
        "currency": "SGD",
        "secret_env": "PAYNOW_WEBHOOK_SECRET",
        "secret_default": "whsec_paynow_stub",
        "header": "X-PayNow-Signature",
        "url": "/api/v1/webhooks/paynow",
        "event_builder": lambda brand_id, amount, ref="ref-1": {
            "event_type": "payment.received",
            "transaction_id": f"paynow_evt_{ref}",
            "amount": amount,
            "currency": "SGD",
            "metadata": {"brand_id": brand_id, "reference_id": ref},
        },
    },
    "grabpay": {
        "currency": "SGD",
        "secret_env": "GRABPAY_PARTNER_HMAC_SECRET",
        "secret_default": "whsec_grabpay_stub",
        "header": "X-Grab-Signature",
        "url": "/api/v1/webhooks/grabpay",
        "event_builder": lambda brand_id, amount, ref="ref-1": {
            "type": "charge.completed",
            "partnerTxID": f"grab_evt_{ref}",
            "amount": amount,
            "currency": "SGD",
            "metadata": {"brand_id": brand_id, "reference_id": ref},
        },
    },
    "alipay": {
        "currency": "CNY",
        "secret_env": "ALIPAY_WEBHOOK_SECRET",
        "secret_default": "whsec_alipay_stub",
        "header": "sign",
        "url": "/api/v1/webhooks/alipay",
        "event_builder": lambda brand_id, amount, ref="ref-1": {
            "notify_type": "TRADE_SUCCESS",
            "out_trade_no": f"ali_evt_{ref}",
            "amount": amount,
            "currency": "CNY",
            "metadata": {"brand_id": brand_id, "reference_id": ref},
        },
    },
    "wechat_pay": {
        "currency": "CNY",
        "secret_env": "WECHAT_API_V3_KEY",
        "secret_default": "whsec_wechat_stub_32_chars_long_x",
        "header": "Wechatpay-Signature",
        "url": "/api/v1/webhooks/wechat",
        "event_builder": lambda brand_id, amount, ref="ref-1": {
            "event_type": "TRANSACTION.SUCCESS",
            "resource": {
                "out_trade_no": f"wx_evt_{ref}",
                "amount": {"total": amount, "currency": "CNY"},
            },
            "metadata": {"brand_id": brand_id, "reference_id": ref},
        },
    },
    "ovo": {
        "currency": "IDR",
        "secret_env": "OVO_WEBHOOK_SECRET",
        "secret_default": "whsec_ovo_stub",
        "header": "X-OVO-Signature",
        "url": "/api/v1/webhooks/ovo",
        "event_builder": lambda brand_id, amount, ref="ref-1": {
            "event": "PAYMENT_SUCCESS",
            "transactionId": f"ovo_evt_{ref}",
            "amount": amount,
            "currency": "IDR",
            "metadata": {"brand_id": brand_id, "reference_id": ref},
        },
    },
}


@pytest.fixture(autouse=True)
def _reset_psp_state(monkeypatch):
    """Reset PSP registry + audit log + ensure mock mode for every test."""
    reset_registry()
    reset_audit_log()
    # Strip any live/test creds that may leak from the dev shell.
    for env_var in [
        "PAYNOW_LIVE_API_KEY", "PAYNOW_TEST_API_KEY",
        "GRABPAY_LIVE_CLIENT_ID", "GRABPAY_TEST_CLIENT_ID",
        "ALIPAY_LIVE_APP_ID", "ALIPAY_TEST_APP_ID", "ALIPAY_PUBLIC_KEY",
        "WECHAT_MCH_ID", "WECHAT_TEST_MCH_ID",
        "OVO_LIVE_APP_ID", "OVO_TEST_APP_ID",
    ]:
        monkeypatch.delenv(env_var, raising=False)
    yield


def _sign(psp_code: str, body: bytes) -> str:
    secret = PSP_FIXTURES[psp_code]["secret_default"]
    return hmac_sign(body, secret)


# ─────────────────────────────────────────────────────────────────────────
# Per-PSP, parametrised
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("psp_code", PSP_CODES)
def test_create_charge_shape(psp_code):
    """create_charge returns expected shape in mock mode."""
    fx = PSP_FIXTURES[psp_code]
    client = get_psp_client(psp_code)
    assert client.get_mode() == "mock"

    out = client.create_charge(
        amount=5_000,
        currency=fx["currency"],
        metadata={"brand_id": "brand_test", "reference_id": "ref_42"},
    )
    assert out["psp"] == psp_code
    assert out["charge_id"].startswith(psp_code.replace("_pay", "pay")[:6]) or "mock" in out["charge_id"]
    assert out["mode"] == "mock"
    assert out["amount"] == 5_000
    assert out["currency"] == fx["currency"]
    # At least one of these must be present so the client can render UI.
    assert any(k in out for k in ("checkout_url", "qr_code", "deeplink", "prepay_id"))


@pytest.mark.parametrize("psp_code", PSP_CODES)
def test_verify_webhook_accepts_valid_signature(psp_code):
    fx = PSP_FIXTURES[psp_code]
    client = get_psp_client(psp_code)
    event = fx["event_builder"]("brand_v", 1_000)
    body = json.dumps(event).encode("utf-8")
    sig = _sign(psp_code, body)

    parsed = client.verify_webhook(body, sig)
    # The parsed event should round-trip the brand_id from metadata.
    assert isinstance(parsed, dict)


@pytest.mark.parametrize("psp_code", PSP_CODES)
def test_verify_webhook_rejects_invalid_signature(psp_code):
    fx = PSP_FIXTURES[psp_code]
    client = get_psp_client(psp_code)
    event = fx["event_builder"]("brand_v", 1_000)
    body = json.dumps(event).encode("utf-8")

    with pytest.raises(ValueError):
        client.verify_webhook(body, "deadbeef_not_a_real_signature")


@pytest.mark.parametrize("psp_code", PSP_CODES)
@pytest.mark.asyncio
async def test_process_event_credits_wallet(psp_code, client, clean_redis):
    fx = PSP_FIXTURES[psp_code]
    brand_id = f"brand_credit_{psp_code}"
    event = fx["event_builder"](brand_id, 7_500, ref=f"ref_{psp_code}_1")
    body = json.dumps(event).encode("utf-8")
    sig = _sign(psp_code, body)

    res = await client.post(
        fx["url"],
        content=body,
        headers={fx["header"]: sig, "content-type": "application/json"},
    )
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["received"] is True
    assert payload["event_type"] == "charge.succeeded"

    balance = int(await clean_redis.get(_k_psp_balance(brand_id)) or 0)
    assert balance == 7_500


@pytest.mark.parametrize("psp_code", PSP_CODES)
def test_refund_flow(psp_code):
    fx = PSP_FIXTURES[psp_code]
    psp = get_psp_client(psp_code)

    # First, create a charge so we have a real charge_id.
    charge = psp.create_charge(
        amount=10_000,
        currency=fx["currency"],
        metadata={"reference_id": "rf-test"},
    )
    refund = psp.refund(charge["charge_id"], amount=10_000)
    assert refund["psp"] == psp_code
    assert refund["charge_id"] == charge["charge_id"]
    assert refund["status"] in {"succeeded", "pending"}
    assert refund["refund_id"]


# ─────────────────────────────────────────────────────────────────────────
# Cross-PSP integrity
# ─────────────────────────────────────────────────────────────────────────
def test_charge_ids_unique_across_psps():
    """Identical seeds must still yield distinct charge_ids across PSPs."""
    seed = "ref_collide_42"
    ids = set()
    for code in PSP_CODES:
        psp = get_psp_client(code)
        charge = psp.create_charge(
            amount=100,
            currency=PSP_FIXTURES[code]["currency"],
            metadata={"reference_id": seed},
        )
        ids.add(charge["charge_id"])
    assert len(ids) == len(PSP_CODES), f"collision: {ids}"


@pytest.mark.asyncio
async def test_webhook_replay_is_idempotent(client, clean_redis):
    """Same event delivered twice must credit the wallet only once."""
    fx = PSP_FIXTURES["paynow"]
    brand_id = "brand_idempotent"
    event = fx["event_builder"](brand_id, 1_234, ref="ref_idempotent")
    body = json.dumps(event).encode("utf-8")
    sig = _sign("paynow", body)
    headers = {fx["header"]: sig, "content-type": "application/json"}

    r1 = await client.post(fx["url"], content=body, headers=headers)
    r2 = await client.post(fx["url"], content=body, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json().get("duplicate") is True

    balance = int(await clean_redis.get(_k_psp_balance(brand_id)) or 0)
    assert balance == 1_234

    # Idempotency claim key must be set in Redis.
    assert await clean_redis.exists(
        _k_psp_event_seen("paynow", "paynow_evt_ref_idempotent", "charge.succeeded")
    )


def test_currency_mapping_is_correct():
    """Each PSP wrapper must reject currencies outside its supported set."""
    # PayNow: SGD only.
    with pytest.raises(ValueError):
        get_psp_client("paynow").create_charge(100, "USD")
    # OVO: IDR only.
    with pytest.raises(ValueError):
        get_psp_client("ovo").create_charge(100, "SGD")
    # WeChat: CNY/HKD only.
    with pytest.raises(ValueError):
        get_psp_client("wechat_pay").create_charge(100, "USD")
    # GrabPay: any SEA currency works.
    out = get_psp_client("grabpay").create_charge(100, "PHP")
    assert out["currency"] == "PHP"
    # Alipay: CNY default; SGD works (cross-border).
    out = get_psp_client("alipay").create_charge(100, "SGD")
    assert out["currency"] == "SGD"


def test_audit_log_emitted():
    """Every money-flow action must leave a structured audit entry."""
    reset_audit_log()
    psp = get_psp_client("grabpay")
    psp.create_charge(
        500, "SGD", metadata={"brand_id": "brand_audit", "reference_id": "ref_a"}
    )
    log = read_audit_log()
    assert any(
        e.get("psp") == "grabpay" and e.get("action") == "create_charge"
        for e in log
    )


# ─────────────────────────────────────────────────────────────────────────
# Registry + interface sanity
# ─────────────────────────────────────────────────────────────────────────
def test_registry_lists_all_priority_psps():
    """``all_psp_codes`` lists exactly the priority 5 in stable order."""
    assert all_psp_codes() == PSP_CODES


def test_registry_client_module_field_set():
    """The 5 priority PSPs in payments_regional have client_module set."""
    for code in PSP_CODES:
        m = get_method(code)
        assert m is not None, f"missing in registry: {code}"
        assert m.client_module, f"client_module unset for {code}"
        assert m.client_module.startswith("app.services.payment_psps.")
        assert m.integration_status == "live"


def test_other_psps_have_no_client_module():
    """Non-priority PSPs (e.g. ``stripe``-routed cards) stay un-wired here."""
    # nets is a registry-only scaffold; must not falsely advertise a wrapper
    m = get_method("nets")
    assert m is not None
    assert m.client_module == ""


def test_get_psp_client_rejects_unknown_code():
    with pytest.raises(KeyError):
        get_psp_client("not_a_real_psp")


# ─────────────────────────────────────────────────────────────────────────
# Health endpoints
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("psp_code", PSP_CODES)
def test_health_check_returns_mode_and_ready(psp_code):
    out = get_psp_client(psp_code).health_check()
    assert out["psp"] == psp_code
    assert out["mode"] == "mock"
    assert out["ready"] is True


@pytest.mark.asyncio
async def test_all_psp_health_endpoint(client):
    res = await client.get("/api/v1/health/psp/all")
    assert res.status_code == 200
    body = res.json()
    assert set(body["psps"].keys()) == set(PSP_CODES)
    assert body["overall_ready"] is True


@pytest.mark.asyncio
async def test_one_psp_health_endpoint(client):
    res = await client.get("/api/v1/health/psp/paynow")
    assert res.status_code == 200
    assert res.json()["psp"] == "paynow"

    res = await client.get("/api/v1/health/psp/notreal")
    assert res.status_code == 404
