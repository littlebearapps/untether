"""Tests for `utils/env_audit.py` — runtime /proc/<pid>/environ probe (#361)."""

from __future__ import annotations

import sys

import pytest

from untether.utils import env_audit
from untether.utils.env_audit import audit_proc_env, read_proc_environ


class TestReadProcEnviron:
    def test_parses_null_separated_chunks(self, tmp_path, monkeypatch):
        # Fake a /proc/<pid>/environ file with NUL-separated KEY=VAL entries.
        fake_environ = tmp_path / "environ"
        fake_environ.write_bytes(b"PATH=/usr/bin\x00HOME=/home/u\x00BWS=secret\x00")

        original_open = open

        def fake_open(path, *args, **kwargs):
            if isinstance(path, str) and path.startswith("/proc/"):
                return original_open(fake_environ, *args, **kwargs)
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", fake_open)
        monkeypatch.setattr(sys, "platform", "linux")

        result = read_proc_environ(12345)
        assert result == {"PATH": "/usr/bin", "HOME": "/home/u", "BWS": "secret"}

    def test_skips_chunks_without_equals(self, tmp_path, monkeypatch):
        fake_environ = tmp_path / "environ"
        # Two valid + one malformed (no '=') chunk.
        fake_environ.write_bytes(b"A=1\x00garbage_no_equals\x00B=2\x00")

        original_open = open

        def fake_open(path, *args, **kwargs):
            if isinstance(path, str) and path.startswith("/proc/"):
                return original_open(fake_environ, *args, **kwargs)
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", fake_open)
        monkeypatch.setattr(sys, "platform", "linux")

        result = read_proc_environ(12345)
        assert result == {"A": "1", "B": "2"}

    def test_non_linux_returns_none(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        assert read_proc_environ(1) is None

    def test_unreadable_pid_returns_none(self):
        # PID 999999999 is extremely unlikely to exist; expect None, no raise.
        assert read_proc_environ(999_999_999) is None


class TestAuditProcEnv:
    @pytest.fixture(autouse=True)
    def _force_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")

    def test_returns_only_disallowed_names(self, monkeypatch):
        fake_env = {
            "PATH": "/usr/bin",
            "HOME": "/home/u",
            "ANTHROPIC_API_KEY": "sk-ant-",
            "BWS_ACCESS_TOKEN": "0.f3a-...",  # #409: now in default allowlist
            "STRIPE_SECRET_KEY": "sk-live-...",
            "DROP_ME": "leak",
        }
        monkeypatch.setattr(env_audit, "read_proc_environ", lambda pid: fake_env)

        result = audit_proc_env(12345)
        assert result == ["DROP_ME", "STRIPE_SECRET_KEY"]

    def test_empty_when_all_allowed(self, monkeypatch):
        fake_env = {"PATH": "/usr/bin", "HOME": "/home/u"}
        monkeypatch.setattr(env_audit, "read_proc_environ", lambda pid: fake_env)
        assert audit_proc_env(12345) == []

    def test_respects_expected_extras(self, monkeypatch):
        fake_env = {
            "PATH": "/usr/bin",
            "STRIPE_SECRET_KEY": "x",
            "CUSTOM_RUNNER_ENV": "y",
        }
        monkeypatch.setattr(env_audit, "read_proc_environ", lambda pid: fake_env)

        # CUSTOM_RUNNER_ENV is permitted by the caller as an extra; only
        # STRIPE_SECRET_KEY should be reported.
        result = audit_proc_env(12345, expected_extras=("CUSTOM_RUNNER_ENV",))
        assert result == ["STRIPE_SECRET_KEY"]

    def test_respects_user_extra_exact(self, monkeypatch):
        """#409: user-allowed exact names must not be flagged as leaks."""
        fake_env = {
            "PATH": "/usr/bin",
            "OP_SERVICE_ACCOUNT_TOKEN": "1p-...",
            "STRIPE_SECRET_KEY": "leak",
        }
        monkeypatch.setattr(env_audit, "read_proc_environ", lambda pid: fake_env)

        result = audit_proc_env(12345, user_extra_exact=("OP_SERVICE_ACCOUNT_TOKEN",))
        assert result == ["STRIPE_SECRET_KEY"]

    def test_respects_user_extra_prefix(self, monkeypatch):
        """#409: user-allowed prefix names must not be flagged as leaks."""
        fake_env = {
            "PATH": "/usr/bin",
            "VAULT_TOKEN": "v",
            "VAULT_ADDR": "https://vault",
            "STRIPE_SECRET_KEY": "leak",
        }
        monkeypatch.setattr(env_audit, "read_proc_environ", lambda pid: fake_env)

        result = audit_proc_env(12345, user_extra_prefix=("VAULT_",))
        assert result == ["STRIPE_SECRET_KEY"]

    def test_unreadable_returns_empty(self, monkeypatch):
        monkeypatch.setattr(env_audit, "read_proc_environ", lambda pid: None)
        assert audit_proc_env(12345) == []

    def test_non_linux_returns_empty(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        assert audit_proc_env(12345) == []
