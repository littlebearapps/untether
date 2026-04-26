"""Tests for `utils/env_policy.py` — the engine-subprocess env allowlist (#198, #409)."""

from __future__ import annotations

from untether.utils.env_policy import (
    _is_allowed,
    _reset_log_latch_for_tests,
    filtered_env,
    is_allowed,
    is_allowed_with_extras,
    log_user_extensions_once,
)


class TestIsAllowed:
    """Public predicate exposed for utils.env_audit (#361)."""

    def test_exact_allow_returns_true(self):
        assert is_allowed("PATH") is True
        assert is_allowed("ANTHROPIC_API_KEY") is True
        assert is_allowed("UNTETHER_SESSION") is True
        assert is_allowed("BWS_ACCESS_TOKEN") is True

    def test_prefix_allow_returns_true(self):
        assert is_allowed("CLAUDE_CODE_FOO") is True
        assert is_allowed("MCP_SERVER_BAR") is True

    def test_disallowed_returns_false(self):
        assert is_allowed("AWS_SECRET_ACCESS_KEY") is False
        assert is_allowed("STRIPE_SECRET_KEY") is False

    def test_underscore_alias_back_compat(self):
        # _is_allowed is preserved as a deprecation alias for any
        # external importers; verify it points to the same callable.
        assert _is_allowed is is_allowed


class TestExactAllowlist:
    def test_basic_os_vars_pass(self):
        src = {
            "PATH": "/usr/bin",
            "HOME": "/home/u",
            "USER": "u",
            "SHELL": "/bin/zsh",
            "TERM": "xterm",
            "LANG": "en_AU.UTF-8",
        }
        assert filtered_env(src) == src

    def test_provider_api_keys_pass(self):
        src = {
            "ANTHROPIC_API_KEY": "sk-ant-...",
            "OPENAI_API_KEY": "sk-...",
            "GOOGLE_API_KEY": "AIza...",
            "GEMINI_API_KEY": "gem_...",
            "GITHUB_TOKEN": "ghp_...",
            "CLOUDFLARE_API_TOKEN": "cf_...",
        }
        assert filtered_env(src) == src

    def test_untether_session_marker_passes(self):
        assert filtered_env({"UNTETHER_SESSION": "1"}) == {"UNTETHER_SESSION": "1"}


class TestPrefixAllowlist:
    def test_claude_prefix_passes(self):
        src = {
            "CLAUDE_ENABLE_STREAM_WATCHDOG": "1",
            "CLAUDE_STREAM_IDLE_TIMEOUT_MS": "60000",
            "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
        }
        assert filtered_env(src) == src

    def test_mcp_prefix_passes(self):
        src = {
            "MCP_TOOL_TIMEOUT": "120000",
            "MCP_SERVER_CONFIG": "/etc/mcp.json",
            "MAX_MCP_OUTPUT_TOKENS": "12000",
        }
        assert filtered_env(src) == src

    def test_lc_locale_variants_pass(self):
        src = {"LC_NUMERIC": "C", "LC_TIME": "en_AU.UTF-8"}
        assert filtered_env(src) == src

    def test_node_npm_uv_prefixes_pass(self):
        src = {
            "NPM_CONFIG_PREFIX": "/home/u/.npm-global",
            "NODE_TLS_REJECT_UNAUTHORIZED": "0",
            "UV_PYTHON": "python3.12",
            "PNPM_HOME": "/home/u/.pnpm",
            "PIP_INDEX_URL": "https://pypi.org/simple",
        }
        assert filtered_env(src) == src


class TestDenied:
    def test_arbitrary_secrets_stripped(self):
        """Third-party tokens that happen to be in the parent env must
        not reach the engine subprocess."""
        src = {
            "AWS_SECRET_ACCESS_KEY": "secret-aws",
            "AWS_ACCESS_KEY_ID": "akid",
            "DIGITALOCEAN_TOKEN": "do-tok",
            "STRIPE_SECRET_KEY": "sk_live_...",
            "DATABASE_URL": "postgres://...",
            "DB_PASSWORD": "hunter2",
            "SOME_APP_TOKEN": "random",
        }
        assert filtered_env(src) == {}

    def test_personal_env_not_leaked(self):
        """Random variables users set in ~/.zshrc for their own tooling
        must not automatically propagate."""
        src = {
            "MY_PROJECT_DIR": "/home/u/my-project",
            "EDITOR": "nvim",  # not in allowlist
            "PS1": "zsh prompt",
            "HISTSIZE": "10000",
        }
        assert filtered_env(src) == {}


class TestMixedInput:
    def test_keeps_allowed_drops_denied(self):
        src = {
            "PATH": "/usr/bin",
            "AWS_SECRET_ACCESS_KEY": "leak",
            "ANTHROPIC_API_KEY": "sk-ant",
            "EDITOR": "nvim",
            "MCP_TOOL_TIMEOUT": "120000",
        }
        out = filtered_env(src)
        assert out == {
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": "sk-ant",
            "MCP_TOOL_TIMEOUT": "120000",
        }

    def test_empty_source_returns_empty(self):
        assert filtered_env({}) == {}

    def test_extra_allow_widens_filter(self):
        """Per-engine / per-site keys that aren't in the global set can be
        opted in via extra_allow without polluting the shared policy."""
        src = {"CUSTOM_KEY": "v", "PATH": "/usr/bin", "BLOCKED": "x"}
        assert filtered_env(src, extra_allow=["CUSTOM_KEY"]) == {
            "CUSTOM_KEY": "v",
            "PATH": "/usr/bin",
        }

    def test_default_source_is_os_environ(self, monkeypatch):
        """Without explicit source, filtered_env() reads os.environ."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "probe-value")
        monkeypatch.setenv("DEFINITELY_NOT_ALLOWED_XYZ", "leak")
        out = filtered_env()
        assert out.get("ANTHROPIC_API_KEY") == "probe-value"
        assert "DEFINITELY_NOT_ALLOWED_XYZ" not in out


class TestUserExtensions:
    """#409: per-deployment user extras via [security] env_extra_allow /
    env_extra_prefix_allow surface here as `extra_allow` / `extra_prefix`
    parameters to filtered_env."""

    def test_is_allowed_with_extras_falls_back_to_default(self):
        # No extras: behaves identically to is_allowed().
        assert is_allowed_with_extras("PATH") is True
        assert is_allowed_with_extras("AWS_SECRET_ACCESS_KEY") is False

    def test_is_allowed_with_extras_admits_user_exact(self):
        assert (
            is_allowed_with_extras(
                "OP_SERVICE_ACCOUNT_TOKEN",
                extra_exact=["OP_SERVICE_ACCOUNT_TOKEN"],
            )
            is True
        )
        # Names not in the user exacts still get rejected.
        assert (
            is_allowed_with_extras(
                "OTHER_TOKEN", extra_exact=["OP_SERVICE_ACCOUNT_TOKEN"]
            )
            is False
        )

    def test_is_allowed_with_extras_admits_user_prefix(self):
        assert is_allowed_with_extras("VAULT_TOKEN", extra_prefix=["VAULT_"]) is True
        assert is_allowed_with_extras("VAULT_ADDR", extra_prefix=["VAULT_"]) is True
        assert (
            is_allowed_with_extras("STRIPE_VAULT_KEY", extra_prefix=["VAULT_"]) is False
        )

    def test_filtered_env_admits_extra_prefix(self):
        src = {
            "VAULT_TOKEN": "v-tok",
            "VAULT_ADDR": "https://vault",
            "STRIPE_SECRET_KEY": "sk_live_x",
            "PATH": "/usr/bin",
        }
        out = filtered_env(src, extra_prefix=["VAULT_"])
        assert out == {
            "VAULT_TOKEN": "v-tok",
            "VAULT_ADDR": "https://vault",
            "PATH": "/usr/bin",
        }

    def test_filtered_env_combines_extra_allow_and_extra_prefix(self):
        src = {
            "DOPPLER_TOKEN": "d-tok",
            "VAULT_TOKEN": "v-tok",
            "STRIPE_SECRET_KEY": "leak",
        }
        out = filtered_env(
            src,
            extra_allow=["DOPPLER_TOKEN"],
            extra_prefix=["VAULT_"],
        )
        assert out == {"DOPPLER_TOKEN": "d-tok", "VAULT_TOKEN": "v-tok"}

    def test_default_still_blocks_random_env_vars(self):
        """Without user extras, prior denial behaviour is preserved."""
        src = {"AWS_SECRET_ACCESS_KEY": "leak", "STRIPE_SECRET_KEY": "leak"}
        assert filtered_env(src) == {}


class TestUserExtensionLogging:
    """#409: log_user_extensions_once emits one structured INFO per process."""

    def setup_method(self):
        _reset_log_latch_for_tests()

    def teardown_method(self):
        _reset_log_latch_for_tests()

    def test_logs_once_when_extras_provided(self):
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            log_user_extensions_once(
                extra_exact=["OP_SERVICE_ACCOUNT_TOKEN"],
                extra_prefix=["VAULT_"],
            )
            log_user_extensions_once(
                extra_exact=["OP_SERVICE_ACCOUNT_TOKEN"],
                extra_prefix=["VAULT_"],
            )

        ext_events = [r for r in logs if r.get("event") == "env_policy.user_extension"]
        assert len(ext_events) == 1
        assert ext_events[0]["extra_exact"] == ["OP_SERVICE_ACCOUNT_TOKEN"]
        assert ext_events[0]["extra_prefix"] == ["VAULT_"]

    def test_no_log_when_no_extras(self):
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            log_user_extensions_once()
            log_user_extensions_once(extra_exact=[], extra_prefix=[])

        ext_events = [r for r in logs if r.get("event") == "env_policy.user_extension"]
        assert ext_events == []
