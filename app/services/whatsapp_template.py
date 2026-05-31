"""WhatsApp Cloud API template-message sender for Bedok pilot ops.

Sends the 4 utility templates documented in docs/bedok-pilot-ops.md:
  - bedok_t24h     — Day 1 check
  - bedok_t72h     — Day 3 pulse
  - bedok_t7d      — Week 1 wrap
  - bedok_t14d     — Two-week review

All templates pre-approved on Meta Business Manager (category=UTILITY).

Env vars required:
  WHATSAPP_CLOUD_TOKEN  — long-lived access token from Meta Business
  WHATSAPP_PHONE_NUMBER_ID  — sender phone-number-id (numeric)
  WHATSAPP_BUSINESS_ID  — Meta Business Account id

If env vars are missing OR running in test mode, the sender returns a
DryRun result without making the API call. Tests for this module use
DryRun by default and don't hit the Meta API.

Public API:
  send_bedok_template(template, merchant, fields) -> SendResult
  schedule_bedok_followups(merchant, campaign_start_at) -> list[SendResult]
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

WHATSAPP_GRAPH_BASE = "https://graph.facebook.com/v19.0"

TEMPLATE_DEFS = {
    "bedok_t24h": {
        "name": "bedok_t24h",
        "language": "en_SG",
        "param_order": [
            "merchant_first_name", "plays_24h", "regs_24h",
            "redeems_24h", "spent_24h",
        ],
    },
    "bedok_t72h": {
        "name": "bedok_t72h",
        "language": "en_SG",
        "param_order": [
            "merchant_first_name", "cpa_72h", "top_game_name",
            "top_game_plays", "insight_1", "insight_2",
        ],
    },
    "bedok_t7d": {
        "name": "bedok_t7d",
        "language": "en_SG",
        "param_order": [
            "merchant_first_name", "new_customers_7d", "return_rate_proj",
            "effective_cac", "comparison_text", "calendar_link",
        ],
    },
    "bedok_t14d": {
        "name": "bedok_t14d",
        "language": "en_SG",
        "param_order": [
            "merchant_first_name", "kix_cac", "fb_cac_baseline",
            "plays_per_visit", "return_14d", "calendar_link",
        ],
    },
}


@dataclass
class Merchant:
    """Slim merchant struct used by this service.

    Real callers pass an ORM-level object that exposes these fields. Tests
    construct Merchant directly.
    """
    brand_id: str
    first_name: str
    phone_e164: str          # e.g. "+6591234567"
    locale: str = "en_SG"    # affects template language
    consent_whatsapp: bool = True


@dataclass
class SendResult:
    ok: bool
    template: str
    merchant_brand_id: str
    sent_to_phone: str
    api_response: dict[str, Any] | None = None
    dry_run: bool = False
    skipped_reason: str | None = None
    error: str | None = None


# ── credentials ──

def _credentials() -> tuple[str | None, str | None]:
    token = os.environ.get("WHATSAPP_CLOUD_TOKEN")
    phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    return token, phone_id


def _is_dry_run() -> bool:
    """True when env vars missing OR test mode flag set."""
    if os.environ.get("KIX_WHATSAPP_DRY_RUN") == "1":
        return True
    token, phone_id = _credentials()
    return not (token and phone_id)


# ── core sender ──

def _build_template_payload(template: str, params: list[str]) -> dict[str, Any]:
    """Construct WhatsApp Cloud API template-send payload."""
    tdef = TEMPLATE_DEFS[template]
    components = []
    if params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in params],
        })
    return {
        "messaging_product": "whatsapp",
        "type": "template",
        "template": {
            "name": tdef["name"],
            "language": {"code": tdef["language"]},
            "components": components,
        },
    }


def send_bedok_template(
    template: str,
    merchant: Merchant,
    fields: dict[str, Any],
    *,
    http_client: Optional[httpx.Client] = None,
) -> SendResult:
    """Send one Bedok template to one merchant. Idempotent — caller must
    track already-sent templates to avoid duplicates."""
    if template not in TEMPLATE_DEFS:
        return SendResult(
            ok=False, template=template, merchant_brand_id=merchant.brand_id,
            sent_to_phone=merchant.phone_e164,
            error=f"unknown template: {template}",
        )

    if not merchant.consent_whatsapp:
        return SendResult(
            ok=False, template=template, merchant_brand_id=merchant.brand_id,
            sent_to_phone=merchant.phone_e164,
            skipped_reason="merchant has not opted in to WhatsApp ops messages",
        )

    if not merchant.phone_e164.startswith("+"):
        return SendResult(
            ok=False, template=template, merchant_brand_id=merchant.brand_id,
            sent_to_phone=merchant.phone_e164,
            error="phone_e164 must be E.164 format starting with +",
        )

    tdef = TEMPLATE_DEFS[template]
    params = [fields.get(k, "") for k in tdef["param_order"]]
    payload = _build_template_payload(template, params)
    payload["to"] = merchant.phone_e164.lstrip("+")

    if _is_dry_run():
        logger.info("whatsapp_template DRY RUN brand=%s template=%s", merchant.brand_id, template)
        return SendResult(
            ok=True, template=template, merchant_brand_id=merchant.brand_id,
            sent_to_phone=merchant.phone_e164,
            dry_run=True, api_response={"dry_run_payload": payload},
        )

    token, phone_id = _credentials()
    url = f"{WHATSAPP_GRAPH_BASE}/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    client = http_client or httpx.Client(timeout=15)
    try:
        r = client.post(url, headers=headers, json=payload)
        if r.status_code == 200:
            data = r.json()
            return SendResult(
                ok=True, template=template, merchant_brand_id=merchant.brand_id,
                sent_to_phone=merchant.phone_e164, api_response=data,
            )
        return SendResult(
            ok=False, template=template, merchant_brand_id=merchant.brand_id,
            sent_to_phone=merchant.phone_e164,
            error=f"HTTP {r.status_code}: {r.text[:200]}",
        )
    except Exception as e:
        return SendResult(
            ok=False, template=template, merchant_brand_id=merchant.brand_id,
            sent_to_phone=merchant.phone_e164,
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        if http_client is None:
            client.close()


# ── scheduling helper ──

@dataclass
class ScheduledFollowup:
    template: str
    fire_at: _dt.datetime
    description: str


def schedule_bedok_followups(
    campaign_start_at: _dt.datetime,
) -> list[ScheduledFollowup]:
    """Return the 4 standard Bedok-pilot follow-up schedule for one campaign.

    Caller persists these to a delayed-job queue (e.g., dramatiq / arq /
    celery beat). The actual sender (send_bedok_template) is called at
    fire_at time with current-data fields.
    """
    if campaign_start_at.tzinfo is None:
        raise ValueError("campaign_start_at must be timezone-aware")

    return [
        ScheduledFollowup(
            template="bedok_t24h",
            fire_at=campaign_start_at + _dt.timedelta(hours=24),
            description="Day 1 check — first 24h plays/regs/redeems/spend",
        ),
        ScheduledFollowup(
            template="bedok_t72h",
            fire_at=campaign_start_at + _dt.timedelta(hours=72),
            description="Day 3 pulse — CPA + top game + 2 insights",
        ),
        ScheduledFollowup(
            template="bedok_t7d",
            fire_at=campaign_start_at + _dt.timedelta(days=7),
            description="Week 1 wrap — CAC + return rate + comparison",
        ),
        ScheduledFollowup(
            template="bedok_t14d",
            fire_at=campaign_start_at + _dt.timedelta(days=14),
            description="Two-week review — decision-point with FB benchmark",
        ),
    ]


# ── CLI for ops convenience ──

def _cli() -> int:
    import argparse, sys
    p = argparse.ArgumentParser(description="Send a Bedok WhatsApp template")
    p.add_argument("template", choices=list(TEMPLATE_DEFS.keys()))
    p.add_argument("--brand-id", required=True)
    p.add_argument("--phone", required=True, help="E.164 format, e.g. +6591234567")
    p.add_argument("--first-name", required=True)
    p.add_argument("--field", action="append", default=[],
                   help="Template field as key=value, repeat per field")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.dry_run:
        os.environ["KIX_WHATSAPP_DRY_RUN"] = "1"

    fields = {}
    for spec in args.field:
        if "=" not in spec:
            print(f"bad --field (need k=v): {spec}", file=sys.stderr); return 2
        k, v = spec.split("=", 1)
        fields[k.strip()] = v.strip()

    merchant = Merchant(
        brand_id=args.brand_id,
        first_name=args.first_name,
        phone_e164=args.phone,
        consent_whatsapp=True,
    )
    result = send_bedok_template(args.template, merchant, fields)
    print(f"ok={result.ok} dry_run={result.dry_run}")
    if result.error:    print(f"error: {result.error}")
    if result.skipped_reason: print(f"skipped: {result.skipped_reason}")
    if result.api_response: print(f"response: {result.api_response}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
