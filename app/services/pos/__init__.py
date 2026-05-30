"""POS (Point-of-Sale) integration layer.

Wave E item 7 deliverable: real integration wrappers for the top 4
merchant POS systems prioritised by KiX's go-to-market footprint:

    1. Toast      — US F&B (full-service restaurants)
    2. Square     — US small-business retail / cafe
    3. Loyverse   — Singapore + global free POS (popular with SMBs)
    4. Foodzaps   — SEA F&B SMB (Malaysia / Singapore)

Each POS wrapper lives in its own module under
``app.services.pos.<adapter_name>`` and implements the
:class:`POSAdapter` abstract base.

Design goals
------------
* **Mock-first** — every wrapper auto-detects ``live``/``test``/``mock``
  modes from env vars. With no credentials, mock mode returns
  deterministic, plausibly-shaped data so CI and dev work
  network-isolated. CI MUST NEVER call a real POS API.
* **Uniform shape** — ``verify_voucher``, ``apply_discount``,
  ``mark_redeemed`` and ``webhook_handler`` are identical across all
  POS systems so the routing layer is dumb.
* **No coupling** — wrappers never import the wallet directly. The
  redemption router does any wallet / voucher-store mutations centrally.
* **Mirrors the PSP layer** — same factory / registry / mock pattern
  as :mod:`app.services.payment_psps`, so on-call engineers only need
  to learn one shape.

Voucher → POS flow
------------------
1. Consumer wins voucher in-game → ``voucher_id`` issued.
2. Consumer shows voucher (QR or code) at store.
3. Cashier scans / types code into POS or the lightweight web checkout
   page (``landing/pos-checkout.html``).
4. POS adapter calls ``verify_voucher`` against the KiX system.
5. POS adapter calls ``apply_discount`` on the in-progress order.
6. After tender, ``mark_redeemed`` finalises and audit-logs.
7. Optional webhook from the POS confirms the close-out async.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Abstract base — every POS wrapper implements this contract.
# ─────────────────────────────────────────────────────────────────
class POSAdapter(ABC):
    """Abstract base for all POS wrappers.

    Concrete subclasses set ``pos_code`` (the adapter slug used in URL
    routing) and implement the four methods.  Every method returns a
    plain ``dict`` so the HTTP layer can serialise it directly to JSON
    without an adapter shim.
    """

    pos_code: str = ""

    @abstractmethod
    def get_mode(self) -> str:
        """Return one of ``"live"``, ``"test"``, ``"mock"``."""

    @abstractmethod
    def verify_voucher(self, voucher_id: str) -> dict[str, Any]:
        """Real-time voucher lookup + validation.

        Returns::

            {
                "pos": <pos_code>,
                "voucher_id": str,
                "valid": bool,
                "value_cents": int,
                "currency": str,
                "brand_id": str | None,
                "expires_at": int | None,    # unix seconds
                "reason": str | None,        # populated when valid=False
                "mode": str,
            }

        Implementations MUST NOT raise on "voucher not found" — they
        return ``valid=False`` with a ``reason``.  ``ValueError`` is
        reserved for malformed input (empty id, etc.).
        """

    @abstractmethod
    def apply_discount(
        self,
        voucher_id: str,
        order_id: str,
        amount_cents: int,
    ) -> dict[str, Any]:
        """Apply discount to an in-progress POS order.

        Returns::

            {
                "pos": <pos_code>,
                "voucher_id": str,
                "order_id": str,
                "discount_cents": int,
                "applied": bool,
                "pos_discount_ref": str,     # POS-side handle
                "mode": str,
            }
        """

    @abstractmethod
    def mark_redeemed(
        self, voucher_id: str, transaction_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Final confirmation that the voucher was used at the POS.

        ``transaction_data`` typically contains ``order_id``,
        ``tendered_cents``, ``cashier_id``, ``store_id``.
        Returns::

            {
                "pos": <pos_code>,
                "voucher_id": str,
                "redemption_id": str,
                "redeemed_at": int,          # unix seconds
                "transaction_ref": str,
                "mode": str,
            }
        """

    @abstractmethod
    def webhook_handler(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Standardise an inbound POS webhook event.

        Canonical output::

            {
                "pos": <pos_code>,
                "event_type": "order.closed" | "voucher.redeemed"
                              | "refund.issued" | "unknown",
                "voucher_id": str | None,
                "order_id": str | None,
                "amount_cents": int,
                "currency": str,
                "raw": <original payload>,
            }
        """


# ─────────────────────────────────────────────────────────────────
# Registry — lazy-loaded singletons so tests can re-instantiate
# easily after monkeypatching env vars.
# ─────────────────────────────────────────────────────────────────
_REGISTRY: dict[str, POSAdapter] = {}


def get_pos_adapter(pos_code: str) -> POSAdapter:
    """Return the singleton adapter for ``pos_code``.

    Lazy-imports so a misbehaving wrapper never breaks application boot.
    Raises :class:`KeyError` for unknown codes.
    """
    code = (pos_code or "").lower()
    if code in _REGISTRY:
        return _REGISTRY[code]

    if code == "toast":
        from .toast_pos import ToastPOSAdapter

        _REGISTRY[code] = ToastPOSAdapter()
    elif code == "square":
        from .square_pos import SquarePOSAdapter

        _REGISTRY[code] = SquarePOSAdapter()
    elif code == "loyverse":
        from .loyverse_pos import LoyversePOSAdapter

        _REGISTRY[code] = LoyversePOSAdapter()
    elif code == "foodzaps":
        from .foodzaps_pos import FoodzapsPOSAdapter

        _REGISTRY[code] = FoodzapsPOSAdapter()
    else:
        raise KeyError(f"unknown POS code: {pos_code!r}")

    return _REGISTRY[code]


def reset_pos_registry() -> None:
    """Test-only helper: drop singletons so env-var changes take effect."""
    _REGISTRY.clear()


def all_pos_codes() -> list[str]:
    """Codes of every adapter this layer knows how to construct."""
    return ["toast", "square", "loyverse", "foodzaps"]


__all__ = [
    "POSAdapter",
    "get_pos_adapter",
    "all_pos_codes",
    "reset_pos_registry",
]


# ─────────────────────────────────────────────────────────────────
# Standard error taxonomy — shared across adapters.
# Returned in ``verify_voucher().reason`` so the redemption router
# can give consistent error messages regardless of the upstream POS.
# ─────────────────────────────────────────────────────────────────
class POSError:
    NOT_FOUND = "voucher_not_found"
    EXPIRED = "voucher_expired"
    ALREADY_USED = "voucher_already_redeemed"
    WRONG_BRAND = "voucher_wrong_brand"
    AUTH_FAILURE = "pos_auth_failure"
    NETWORK_DOWN = "pos_network_down"
    INVALID_SIGNATURE = "invalid_webhook_signature"
    INVALID_AMOUNT = "invalid_amount"
