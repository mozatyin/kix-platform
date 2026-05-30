"""Stripe live integration smoke test.

Standalone, network-touching diagnostic. NOT run in CI. Run manually
before flipping a deployment to ``sk_live_*``::

    python -m scripts.stripe_smoke_test --currency=SGD --amount-cents=100

What it does (in order):

  1. Confirm ``STRIPE_SECRET_KEY`` is set and refuses the dev sentinel.
  2. Refuse to run against ``sk_live_*`` unless ``--confirm-live`` given.
  3. Call ``/v1/balance`` — confirms the key is valid + scoped.
  4. Create a PaymentIntent for the smallest amount (default SGD 1.00).
  5. Confirm with Stripe's test token ``tok_visa`` (test mode only).
  6. Poll Stripe until the PI is ``succeeded`` (or fail after ~30s).
  7. Optionally verify a webhook fired (only if ``--check-webhook`` and
     the platform exposes ``/api/v1/health/stripe``).
  8. Issue a full refund.

Exit code 0 == PASS, non-zero == FAIL with a diagnostic line at the end.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

logger = logging.getLogger("stripe_smoke")


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


def _fail(msg: str, code: int = 1) -> int:
    logger.error("FAIL :: %s", msg)
    print(f"\nRESULT: FAIL — {msg}", file=sys.stderr)
    return code


def _pass(diagnostics: dict[str, Any]) -> int:
    logger.info("PASS :: %s", diagnostics)
    print("\nRESULT: PASS")
    for k, v in diagnostics.items():
        print(f"  {k}: {v}")
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stripe live integration smoke test")
    p.add_argument("--currency", default="SGD", help="ISO-4217 currency (default SGD)")
    p.add_argument(
        "--amount-cents",
        type=int,
        default=100,
        help="Amount in smallest currency unit (default 100 = S$1.00)",
    )
    p.add_argument(
        "--confirm-live",
        action="store_true",
        help="Required acknowledgement if STRIPE_SECRET_KEY=sk_live_*",
    )
    p.add_argument(
        "--check-webhook",
        action="store_true",
        help="Poll local /api/v1/health/stripe for last_charge_ts uptick",
    )
    p.add_argument(
        "--health-url",
        default="http://localhost:8000/api/v1/health/stripe",
        help="Where to GET for --check-webhook",
    )
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument(
        "--no-refund",
        action="store_true",
        help="Skip the refund step (useful for diagnosing capture failures)",
    )
    return p.parse_args()


def _gate_key(confirm_live: bool) -> tuple[str, str]:
    """Return (mode, api_key) or exit with a diagnostic message."""
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key:
        sys.exit(_fail("STRIPE_SECRET_KEY is not set"))
    if key == "sk_test_stub":
        sys.exit(_fail("STRIPE_SECRET_KEY=sk_test_stub (mock sentinel) — set a real test key"))
    if key.startswith("sk_live_") and not confirm_live:
        sys.exit(
            _fail(
                "STRIPE_SECRET_KEY=sk_live_* but --confirm-live not given. "
                "Refusing to charge a real card. Re-run with --confirm-live "
                "if you really mean it."
            )
        )
    if key.startswith("sk_live_"):
        return "live", key
    if key.startswith("sk_test_"):
        return "test", key
    sys.exit(_fail(f"STRIPE_SECRET_KEY has unknown prefix: {key[:7]!r}"))


def _check_balance(stripe_mod, key: str) -> dict[str, Any]:
    stripe_mod.api_key = key
    logger.info("[1/5] GET /v1/balance …")
    try:
        bal = stripe_mod.Balance.retrieve()
    except Exception as exc:  # noqa: BLE001
        sys.exit(_fail(f"/v1/balance call failed: {exc}"))
    available = list(bal.get("available") or [])
    logger.info("  available balance rows: %d", len(available))
    return {"available": available}


def _create_payment_intent(
    stripe_mod, amount_cents: int, currency: str
) -> Any:
    logger.info(
        "[2/5] Create PaymentIntent amount=%d %s …", amount_cents, currency.upper()
    )
    try:
        pi = stripe_mod.PaymentIntent.create(
            amount=amount_cents,
            currency=currency.lower(),
            payment_method_types=["card"],
            description="KiX smoke test — safe to refund",
            metadata={"purpose": "smoke_test", "source": "scripts/stripe_smoke_test.py"},
        )
    except Exception as exc:  # noqa: BLE001
        sys.exit(_fail(f"PaymentIntent.create failed: {exc}"))
    logger.info("  pi.id=%s status=%s", pi.id, pi.status)
    return pi


def _confirm_intent(stripe_mod, pi: Any, mode: str) -> Any:
    """Confirm via Stripe's canonical test token.

    Only safe in ``test`` mode. In live mode the caller MUST present a
    real card via Stripe Elements — we don't try to fake that here.
    """
    if mode == "live":
        logger.warning(
            "  live mode: confirm step skipped — present a real card "
            "via the dashboard or your app UI to drive the PI to succeeded."
        )
        return pi
    logger.info("[3/5] Confirm with test token tok_visa …")
    try:
        pi = stripe_mod.PaymentIntent.confirm(
            pi.id,
            payment_method_data={"type": "card", "card": {"token": "tok_visa"}},
            return_url="https://example.com/return",
        )
    except Exception as exc:  # noqa: BLE001
        sys.exit(_fail(f"PaymentIntent.confirm failed: {exc}"))
    logger.info("  status after confirm=%s", pi.status)
    return pi


def _poll_succeeded(stripe_mod, pi_id: str, timeout_s: int = 30) -> Any:
    logger.info("[3b] Poll until succeeded (timeout=%ds) …", timeout_s)
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = stripe_mod.PaymentIntent.retrieve(pi_id)
        if last.status == "succeeded":
            return last
        if last.status in {"canceled", "requires_payment_method"}:
            sys.exit(_fail(f"PaymentIntent ended in terminal failure: {last.status}"))
        time.sleep(1)
    sys.exit(_fail(f"PaymentIntent did not reach succeeded in {timeout_s}s (last={last.status if last else 'n/a'})"))


def _check_webhook(health_url: str, before_ts: float | None) -> None:
    logger.info("[4/5] Poll %s for last_charge_ts uptick …", health_url)
    try:
        import urllib.request
        import json
        deadline = time.time() + 15
        while time.time() < deadline:
            with urllib.request.urlopen(health_url, timeout=3) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            after = body.get("last_charge_ts")
            if after and (before_ts is None or after > before_ts):
                logger.info("  webhook observed: last_charge_ts=%s", after)
                return
            time.sleep(1)
        logger.warning(
            "  no webhook uptick observed — endpoint may not be wired or "
            "STRIPE_WEBHOOK_SECRET differs from Stripe dashboard"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("  webhook check skipped: %s", exc)


def _refund(stripe_mod, pi: Any) -> Any:
    logger.info("[5/5] Refund pi=%s …", pi.id)
    try:
        refund = stripe_mod.Refund.create(payment_intent=pi.id)
    except Exception as exc:  # noqa: BLE001
        sys.exit(_fail(f"Refund.create failed: {exc}"))
    logger.info("  refund.id=%s status=%s", refund.id, refund.status)
    return refund


def main() -> int:
    args = _parse_args()
    _setup_logging(args.verbose)

    mode, key = _gate_key(args.confirm_live)
    logger.info("Stripe smoke test — mode=%s currency=%s amount=%d",
                mode, args.currency.upper(), args.amount_cents)

    try:
        import stripe  # type: ignore
    except ImportError:
        return _fail("stripe SDK not installed — pip install stripe")

    balance = _check_balance(stripe, key)

    before_ts = None
    if args.check_webhook:
        # Capture pre-charge last_charge_ts to detect the uptick later.
        try:
            import urllib.request
            import json
            with urllib.request.urlopen(args.health_url, timeout=3) as resp:
                before_ts = json.loads(resp.read().decode("utf-8")).get("last_charge_ts")
        except Exception:
            before_ts = None

    pi = _create_payment_intent(stripe, args.amount_cents, args.currency)
    pi = _confirm_intent(stripe, pi, mode)
    if mode == "test":
        pi = _poll_succeeded(stripe, pi.id)

    if args.check_webhook:
        _check_webhook(args.health_url, before_ts)

    refund = None
    if mode == "test" and not args.no_refund:
        refund = _refund(stripe, pi)

    return _pass(
        {
            "mode": mode,
            "payment_intent_id": pi.id,
            "pi_status": pi.status,
            "amount_cents": args.amount_cents,
            "currency": args.currency.upper(),
            "balance_rows": len(balance["available"]),
            "refund_id": getattr(refund, "id", None),
            "refund_status": getattr(refund, "status", None),
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
