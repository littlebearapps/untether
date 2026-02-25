"""Webhook authentication verification."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping

from .settings import WebhookConfig

# HMAC signature headers scoped by algorithm.
_ALGO_HEADERS: dict[str, tuple[str, ...]] = {
    "hmac-sha256": ("x-hub-signature-256", "x-signature"),
    "hmac-sha1": ("x-hub-signature", "x-signature"),
}


def verify_auth(
    config: WebhookConfig,
    headers: Mapping[str, str],
    body: bytes,
) -> bool:
    """Verify a webhook request against its configured auth mode."""
    if config.auth == "none":
        return True
    if not config.secret:
        return False

    if config.auth == "bearer":
        return _verify_bearer(config.secret, headers)

    if config.auth in ("hmac-sha256", "hmac-sha1"):
        algo = hashlib.sha256 if config.auth == "hmac-sha256" else hashlib.sha1
        sig_headers = _ALGO_HEADERS[config.auth]
        return _verify_hmac(config.secret, body, headers, algo, sig_headers)

    return False


def _verify_bearer(secret: str, headers: Mapping[str, str]) -> bool:
    auth_header = headers.get("authorization", "")
    # RFC 6750: scheme keyword is case-insensitive.
    if len(auth_header) < 7 or auth_header[:7].lower() != "bearer ":
        return False
    token = auth_header[7:]
    return hmac.compare_digest(token, secret)


def _verify_hmac(
    secret: str,
    body: bytes,
    headers: Mapping[str, str],
    algo: type,
    sig_headers: tuple[str, ...],
) -> bool:
    expected = hmac.new(secret.encode(), body, algo).hexdigest()
    # Normalise header keys to lowercase for lookup
    lower_headers = {k.lower(): v for k, v in headers.items()}
    for header in sig_headers:
        sig = lower_headers.get(header, "")
        if not sig:
            continue
        # Strip algorithm prefix (e.g. "sha256=", "sha1=")
        if "=" in sig:
            sig = sig.split("=", 1)[1]
        if hmac.compare_digest(sig, expected):
            return True
    return False
