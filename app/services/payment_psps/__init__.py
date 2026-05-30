"""PSP (Payment Service Provider) integration layer.

Wave C deliverable: real integration wrappers for the top 5 non-Stripe
payment methods prioritised by SG/SEA market share:

    1. PayNow      — Singapore bank-to-bank, instant settlement
    2. GrabPay     — SEA multi-country wallet (SG/MY/PH/TH/VN/ID)
    3. Alipay      — Cross-border (CN/HK/SG/MY)
    4. WeChat Pay  — Cross-border (CN/HK)
    5. OVO         — Indonesia mobile wallet

Each PSP wrapper lives in its own module under
``app.services.payment_psps.<psp_name>`` and implements the
:class:`PSPClient` abstract base.

Design goals
------------
* **Mock-first** — every wrapper auto-detects ``live``/``test``/``mock``
  modes from env vars. With no credentials, mock mode returns
  deterministic, plausibly-shaped data so CI and dev work network-isolated.
* **Uniform shape** — ``create_charge``, ``verify_webhook``,
  ``process_event``, ``refund`` and ``health_check`` are identical
  across all PSPs so the routing layer is dumb.
* **No coupling** — wrappers never import the wallet directly. The
  webhook receiver router does the wallet credit centrally.
* **Strict separation from registry** — :mod:`app.payments_regional`
  remains pure metadata; this module is the execution layer.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Abstract base — every PSP wrapper implements this contract.
# ─────────────────────────────────────────────────────────────────
class PSPClient(ABC):
    """Abstract base for all PSP wrappers.

    Concrete subclasses set ``psp_code`` (matches the registry code in
    :mod:`app.payments_regional`) and implement the five money-flow
    methods.  Every method returns a plain ``dict`` so the HTTP layer
    can serialise it directly to JSON without an adapter shim.
    """

    psp_code: str = ""

    @abstractmethod
    def get_mode(self) -> str:
        """Return one of ``"live"``, ``"test"``, ``"mock"``."""

    @abstractmethod
    def create_charge(
        self,
        amount: int,
        currency: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Initiate a charge.

        Returns a dict containing ``charge_id`` plus at least one of
        ``checkout_url``, ``qr_code``, or ``deeplink``.
        Mode-dependent: mock returns deterministic test data.
        """

    @abstractmethod
    def verify_webhook(
        self, payload: bytes | str, signature: str
    ) -> dict[str, Any]:
        """Verify the inbound webhook signature.

        Raises :class:`ValueError` on failure. Returns the parsed event
        dict on success.
        """

    @abstractmethod
    def process_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Standardise a PSP-specific event into KiX's canonical shape.

        Canonical shape::

            {
                "psp": <psp_code>,
                "event_type": "charge.succeeded" | "charge.failed" | "refund.succeeded",
                "charge_id": str,
                "amount_cents": int,
                "currency": str,                 # ISO-4217 upper
                "brand_id": str | None,          # from metadata
                "reference_id": str | None,
                "raw": <the original event>,
            }
        """

    @abstractmethod
    def refund(
        self, charge_id: str, amount: Optional[int] = None
    ) -> dict[str, Any]:
        """Refund (full if ``amount`` is None, otherwise partial)."""

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        """Return ``{psp, mode, ready, last_charge_ts}``."""


# ─────────────────────────────────────────────────────────────────
# Registry — lazy-loaded singletons so tests can re-instantiate
# easily after monkeypatching env vars.
# ─────────────────────────────────────────────────────────────────
_REGISTRY: dict[str, PSPClient] = {}


def get_psp_client(psp_code: str) -> PSPClient:
    """Return the singleton client for ``psp_code``.

    Lazy-imports so a misbehaving wrapper never breaks application boot.
    Raises :class:`KeyError` for unknown codes.
    """
    code = (psp_code or "").lower()
    if code in _REGISTRY:
        return _REGISTRY[code]

    if code == "paynow":
        from .paynow_sg import PayNowClient

        _REGISTRY[code] = PayNowClient()
    elif code == "grabpay":
        from .grabpay import GrabPayClient

        _REGISTRY[code] = GrabPayClient()
    elif code == "alipay":
        from .alipay_global import AlipayGlobalClient

        _REGISTRY[code] = AlipayGlobalClient()
    elif code == "wechat_pay":
        from .wechat_pay import WeChatPayClient

        _REGISTRY[code] = WeChatPayClient()
    elif code == "ovo":
        from .ovo_indonesia import OVOClient

        _REGISTRY[code] = OVOClient()
    else:
        raise KeyError(f"unknown PSP code: {psp_code!r}")

    return _REGISTRY[code]


def reset_registry() -> None:
    """Test-only helper: drop singletons so env-var changes take effect."""
    _REGISTRY.clear()


def all_psp_codes() -> list[str]:
    """Codes of every wrapper this layer knows how to construct."""
    return ["paynow", "grabpay", "alipay", "wechat_pay", "ovo"]


__all__ = [
    "PSPClient",
    "get_psp_client",
    "all_psp_codes",
    "reset_registry",
]
