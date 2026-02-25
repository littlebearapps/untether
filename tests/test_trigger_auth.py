"""Tests for webhook authentication verification."""

from __future__ import annotations

import hashlib
import hmac

from untether.triggers.auth import verify_auth
from untether.triggers.settings import WebhookConfig


def _make_webhook(
    auth: str = "bearer",
    secret: str | None = "test_secret",
) -> WebhookConfig:
    return WebhookConfig(
        id="test",
        path="/hooks/test",
        auth=auth,
        secret=secret,
        prompt_template="Hello",
    )


class TestBearerAuth:
    def test_valid_bearer(self):
        wh = _make_webhook(auth="bearer", secret="tok_123")
        headers = {"authorization": "Bearer tok_123"}
        assert verify_auth(wh, headers, b"") is True

    def test_invalid_bearer(self):
        wh = _make_webhook(auth="bearer", secret="tok_123")
        headers = {"authorization": "Bearer wrong_token"}
        assert verify_auth(wh, headers, b"") is False

    def test_missing_bearer_header(self):
        wh = _make_webhook(auth="bearer", secret="tok_123")
        assert verify_auth(wh, {}, b"") is False

    def test_malformed_bearer_header(self):
        wh = _make_webhook(auth="bearer", secret="tok_123")
        headers = {"authorization": "Basic dXNlcjpwYXNz"}
        assert verify_auth(wh, headers, b"") is False


class TestHmacAuth:
    def _sign(self, body: bytes, secret: str, algo: str = "sha256") -> str:
        if algo == "sha256":
            digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            return f"sha256={digest}"
        digest = hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
        return f"sha1={digest}"

    def test_valid_hmac_sha256(self):
        wh = _make_webhook(auth="hmac-sha256", secret="my_secret")
        body = b'{"action": "push"}'
        sig = self._sign(body, "my_secret", "sha256")
        headers = {"X-Hub-Signature-256": sig}
        assert verify_auth(wh, headers, body) is True

    def test_invalid_hmac_sha256(self):
        wh = _make_webhook(auth="hmac-sha256", secret="my_secret")
        body = b'{"action": "push"}'
        headers = {"X-Hub-Signature-256": "sha256=deadbeef"}
        assert verify_auth(wh, headers, body) is False

    def test_valid_hmac_sha1(self):
        wh = _make_webhook(auth="hmac-sha1", secret="my_secret")
        body = b'{"text": "alert"}'
        sig = self._sign(body, "my_secret", "sha1")
        headers = {"X-Hub-Signature": sig}
        assert verify_auth(wh, headers, body) is True

    def test_missing_signature_header(self):
        wh = _make_webhook(auth="hmac-sha256", secret="my_secret")
        assert verify_auth(wh, {}, b"body") is False

    def test_x_signature_header_fallback(self):
        wh = _make_webhook(auth="hmac-sha256", secret="my_secret")
        body = b"payload"
        digest = hmac.new(b"my_secret", body, hashlib.sha256).hexdigest()
        headers = {"X-Signature": f"sha256={digest}"}
        assert verify_auth(wh, headers, body) is True


class TestHmacHeaderScoping:
    """Security fix: HMAC-SHA256 must not check SHA-1 headers."""

    def test_sha256_config_ignores_x_hub_signature(self):
        wh = _make_webhook(auth="hmac-sha256", secret="my_secret")
        body = b'{"text": "test"}'
        # Sign with SHA-1 and put in x-hub-signature (the SHA-1 header)
        sha1_digest = hmac.new(b"my_secret", body, hashlib.sha1).hexdigest()
        headers = {"X-Hub-Signature": f"sha1={sha1_digest}"}
        # Should fail: hmac-sha256 config should NOT check x-hub-signature
        assert verify_auth(wh, headers, body) is False

    def test_sha1_config_ignores_x_hub_signature_256(self):
        wh = _make_webhook(auth="hmac-sha1", secret="my_secret")
        body = b'{"text": "test"}'
        # Sign with SHA-256 and put in x-hub-signature-256 (the SHA-256 header)
        sha256_digest = hmac.new(b"my_secret", body, hashlib.sha256).hexdigest()
        headers = {"X-Hub-Signature-256": f"sha256={sha256_digest}"}
        # Should fail: hmac-sha1 config should NOT check x-hub-signature-256
        assert verify_auth(wh, headers, body) is False


class TestBearerCaseInsensitive:
    """Security fix: Bearer scheme is case-insensitive per RFC 6750."""

    def test_lowercase_bearer(self):
        wh = _make_webhook(auth="bearer", secret="tok_123")
        headers = {"authorization": "bearer tok_123"}
        assert verify_auth(wh, headers, b"") is True

    def test_uppercase_bearer(self):
        wh = _make_webhook(auth="bearer", secret="tok_123")
        headers = {"authorization": "BEARER tok_123"}
        assert verify_auth(wh, headers, b"") is True

    def test_mixed_case_bearer(self):
        wh = _make_webhook(auth="bearer", secret="tok_123")
        headers = {"authorization": "BeArEr tok_123"}
        assert verify_auth(wh, headers, b"") is True


class TestNoAuth:
    def test_none_always_passes(self):
        wh = _make_webhook(auth="none", secret=None)
        assert verify_auth(wh, {}, b"anything") is True


class TestMissingSecret:
    def test_auth_with_no_secret_fails(self):
        # Manually construct to bypass pydantic validation
        wh = WebhookConfig.model_construct(
            id="bad",
            path="/hooks/bad",
            auth="bearer",
            secret=None,
            prompt_template="Fail",
        )
        assert verify_auth(wh, {"authorization": "Bearer x"}, b"") is False
