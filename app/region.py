"""KiX region-aware configuration.

Each KiX deployment runs in a specific geographic region. This module
exposes the active region's compliance jurisdiction, currency defaults,
payment methods, supported languages, and per-region infra URLs.

Usage::

    from app.region import get_region_config, CURRENT_REGION

    cfg = get_region_config()
    if cfg["compliance_jurisdiction"] == "CN":
        # apply PIPL-specific rules
        ...

Set ``KIX_REGION`` env to ``cn`` / ``id`` / ``sg`` / ``us``.  Falls back
to ``cn`` (the launch region) when unset, matching MVP defaults.
"""

from __future__ import annotations

import os
from typing import Any

CURRENT_REGION: str = os.environ.get("KIX_REGION", "cn").lower()

# Region table — each region is self-contained (MVP: no cross-region sync).
REGION_CONFIG: dict[str, dict[str, Any]] = {
    "cn": {
        "region_code": "cn",
        "region_name": "China-North",
        "compliance_jurisdiction": "CN",
        "applicable_laws": ["PIPL", "Cybersecurity Law", "Data Security Law"],
        "primary_currency": "CNY",
        "supported_currencies": ["CNY"],
        "payment_methods": ["alipay", "wechat", "credit_card"],
        "languages": ["zh-CN"],
        "default_phone_country_code": "+86",
        "redis_url": os.environ.get("REDIS_URL_CN", "redis://redis:6379/0"),
        "db_url": os.environ.get("DATABASE_URL_CN", ""),
        "data_residency_required": True,
    },
    "id": {
        "region_code": "id",
        "region_name": "Indonesia-Jakarta",
        "compliance_jurisdiction": "ID",
        "applicable_laws": ["PDP Law (UU 27/2022)"],
        "primary_currency": "IDR",
        "supported_currencies": ["IDR", "USD"],
        "payment_methods": ["gopay", "ovo", "dana", "credit_card"],
        "languages": ["id-ID", "en-US"],
        "default_phone_country_code": "+62",
        "redis_url": os.environ.get("REDIS_URL_ID", "redis://redis:6379/0"),
        "db_url": os.environ.get("DATABASE_URL_ID", ""),
        "data_residency_required": False,
    },
    "sg": {
        "region_code": "sg",
        "region_name": "Singapore",
        "compliance_jurisdiction": "SG",
        "applicable_laws": ["PDPA (Singapore)"],
        "primary_currency": "SGD",
        "supported_currencies": ["SGD", "USD"],
        "payment_methods": ["paynow", "grabpay", "credit_card"],
        "languages": ["en-US", "zh-CN", "id-ID", "ms-MY"],
        "default_phone_country_code": "+65",
        "redis_url": os.environ.get("REDIS_URL_SG", "redis://redis:6379/0"),
        "db_url": os.environ.get("DATABASE_URL_SG", ""),
        "data_residency_required": False,
    },
    "us": {
        "region_code": "us",
        "region_name": "US-West",
        "compliance_jurisdiction": "US",
        "applicable_laws": ["CCPA", "COPPA"],
        "primary_currency": "USD",
        "supported_currencies": ["USD"],
        "payment_methods": ["credit_card", "apple_pay", "google_pay"],
        "languages": ["en-US", "es-US"],
        "default_phone_country_code": "+1",
        "redis_url": os.environ.get("REDIS_URL_US", "redis://redis:6379/0"),
        "db_url": os.environ.get("DATABASE_URL_US", ""),
        "data_residency_required": False,
    },
    "eu": {
        "region_code": "eu",
        "region_name": "EU-Frankfurt",
        "compliance_jurisdiction": "EU",
        "applicable_laws": ["GDPR", "ePrivacy Directive"],
        "primary_currency": "EUR",
        "supported_currencies": ["EUR", "USD"],
        "payment_methods": ["sepa", "credit_card", "ideal"],
        "languages": ["en-GB", "de-DE", "fr-FR"],
        "default_phone_country_code": "+49",
        "redis_url": os.environ.get("REDIS_URL_EU", "redis://redis:6379/0"),
        "db_url": os.environ.get("DATABASE_URL_EU", ""),
        "data_residency_required": True,
    },
}


def get_region_config(region: str | None = None) -> dict[str, Any]:
    """Return the config dict for ``region`` (or current region)."""
    key = (region or CURRENT_REGION).lower()
    return REGION_CONFIG.get(key, REGION_CONFIG["cn"])


def get_current_region() -> str:
    """Return the active region code."""
    return CURRENT_REGION


def get_primary_currency(region: str | None = None) -> str:
    return get_region_config(region)["primary_currency"]


def get_compliance_jurisdiction(region: str | None = None) -> str:
    return get_region_config(region)["compliance_jurisdiction"]


def get_payment_methods(region: str | None = None) -> list[str]:
    return list(get_region_config(region)["payment_methods"])


def get_default_phone_country_code(region: str | None = None) -> str:
    return get_region_config(region)["default_phone_country_code"]


def get_applicable_laws(region: str | None = None) -> list[str]:
    return list(get_region_config(region)["applicable_laws"])


def is_currency_supported(currency: str, region: str | None = None) -> bool:
    return currency.upper() in {
        c.upper() for c in get_region_config(region)["supported_currencies"]
    }
