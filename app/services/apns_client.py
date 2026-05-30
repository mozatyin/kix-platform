"""APNS Client — thin wrapper that routes Apple Push delivery via FCM.

We deliberately consolidate APNS into ``fcm_client`` rather than running
a separate native ``aioapns`` client. Reasons:

* One Firebase project + one set of credentials is dramatically simpler
  to operate than juggling a separate APNS team certificate + key file.
* FCM's iOS SDK on-device generates a Firebase token that is bound to
  the underlying APNS token. Server-side, FCM forwards the push through
  Apple's gateway — we only need to call ``fcm_client.send_to_token``
  with the Firebase iOS token and badge/sound get translated to APNS
  payload fields automatically.
* The single-client model means the worker only needs one auth path,
  one rate-limit bucket, one retry policy, and one quota guard.

If a future deployment ever needs to bypass FCM (e.g. for VoIP push,
which Firebase doesn't support), drop a native ``aioapns`` client here
and the worker's seam (`_send_to_platform("ios", …)`) will pick it up.

For now, this module exposes a tiny compatibility surface that just
delegates to ``fcm_client`` so calling code can be platform-agnostic.
"""

from __future__ import annotations

from typing import Any

from app.services import fcm_client


# ── Mode / status ─────────────────────────────────────────────────────────


def get_mode() -> str:
    """Reflect FCM client mode (we share the same auth path)."""
    return fcm_client.get_mode()


def is_configured() -> bool:
    """True when running against real Apple Push (via FCM)."""
    return fcm_client.is_configured()


# ── Send API ──────────────────────────────────────────────────────────────


async def send_to_token(
    token: str,
    title: str,
    body: str,
    badge: int | None = None,
    sound: str | None = "default",
    data: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Send a single APNS push (routed through FCM).

    ``token`` must be a Firebase iOS registration token (NOT a raw 64-char
    APNS device token — Firebase translates internally). See
    ``docs/push-deployment.md`` for the device-side registration flow.
    """
    return await fcm_client.send_to_token(
        token=token,
        title=title,
        body=body,
        data=data,
        badge=badge,
        sound=sound,
        platform="ios",
    )


async def send_multicast(
    tokens: list[str],
    title: str,
    body: str,
    badge: int | None = None,
    sound: str | None = "default",
    data: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fan-out to many iOS tokens (batches of 500 via FCM)."""
    # FCM multicast doesn't expose per-message APNS payload, so badge/sound
    # are passed through the data channel for clients that want to apply
    # them locally on receipt. For per-message badges, callers should use
    # ``send_to_token`` individually.
    enriched = dict(data or {})
    if badge is not None:
        enriched["badge"] = str(int(badge))
    if sound:
        enriched["sound"] = sound
    return await fcm_client.send_multicast(
        tokens=tokens, title=title, body=body, data=enriched,
    )


def validate_token(token: str | None) -> bool:
    """Same structural validation as FCM (we share the token format)."""
    return fcm_client.validate_token(token)


__all__ = [
    "send_to_token",
    "send_multicast",
    "validate_token",
    "get_mode",
    "is_configured",
]
