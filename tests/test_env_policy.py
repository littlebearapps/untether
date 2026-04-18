"""Tests for `utils/env_policy.py` — the engine-subprocess env allowlist (#198)."""

from __future__ import annotations

from untether.utils.env_policy import filtered_env


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
