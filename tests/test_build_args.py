"""Tests for build_args() across all engine runners.

Validates that CLI argument construction is correct for each engine,
covering prompt format, model override, resume, permission mode, and
engine-specific flags. Prevents regressions like #75, #76, #77, #78.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from untether.model import ResumeToken
from untether.runners.run_options import EngineRunOptions as RunOptions

# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------


class TestClaudeBuildArgs:
    def _runner(self, **kwargs: Any):
        from untether.runners.claude import ClaudeRunner

        return ClaudeRunner(claude_cmd="claude", **kwargs)

    def test_basic_prompt_no_permission_mode(self) -> None:
        runner = self._runner()
        from untether.runners.claude import ClaudeStreamState

        state = ClaudeStreamState()
        args = runner.build_args("hello", None, state=state)
        assert "--output-format" in args
        assert "stream-json" in args
        # Without permission mode, prompt is passed via -p + CLI arg
        assert "-p" in args
        assert "hello" in args

    def test_resume(self) -> None:
        runner = self._runner()
        from untether.runners.claude import ClaudeStreamState

        state = ClaudeStreamState()
        token = ResumeToken(engine="claude", value="sess123")
        args = runner.build_args("hello", token, state=state)
        assert "--resume" in args
        idx = args.index("--resume")
        assert args[idx + 1] == "sess123"

    def test_continue(self) -> None:
        runner = self._runner()
        from untether.runners.claude import ClaudeStreamState

        state = ClaudeStreamState()
        token = ResumeToken(engine="claude", value="", is_continue=True)
        args = runner.build_args("hello", token, state=state)
        assert "--continue" in args
        assert "--resume" not in args

    def test_model_override(self) -> None:
        runner = self._runner()
        from untether.runners.claude import ClaudeStreamState

        state = ClaudeStreamState()
        opts = RunOptions(model="opus-4")
        with patch("untether.runners.claude.get_run_options", return_value=opts):
            args = runner.build_args("hello", None, state=state)
        assert "--model" in args
        idx = args.index("--model")
        assert args[idx + 1] == "opus-4"

    def test_permission_mode(self) -> None:
        runner = self._runner()
        from untether.runners.claude import ClaudeStreamState

        state = ClaudeStreamState()
        opts = RunOptions(permission_mode="plan")
        with patch("untether.runners.claude.get_run_options", return_value=opts):
            args = runner.build_args("hello", None, state=state)
        assert "--permission-mode" in args
        idx = args.index("--permission-mode")
        assert args[idx + 1] == "plan"

    def test_allowed_tools(self) -> None:
        from untether.runners.claude import DEFAULT_ALLOWED_TOOLS

        runner = self._runner(allowed_tools=DEFAULT_ALLOWED_TOOLS)
        from untether.runners.claude import ClaudeStreamState

        state = ClaudeStreamState()
        args = runner.build_args("hello", None, state=state)
        assert "--allowedTools" in args
        idx = args.index("--allowedTools")
        # Should be comma-separated list
        assert "Bash" in args[idx + 1]

    def test_extra_args_default_empty(self) -> None:
        """`extra_args=[]` produces byte-identical argv to the pre-#407
        behaviour — no extra tokens introduced."""
        runner_none = self._runner()
        runner_empty = self._runner(extra_args=[])
        from untether.runners.claude import ClaudeStreamState

        state = ClaudeStreamState()
        args_none = runner_none.build_args("hello", None, state=state)
        args_empty = runner_empty.build_args("hello", None, state=state)
        assert args_none == args_empty

    def test_extra_args_chrome(self) -> None:
        """`extra_args=['--chrome']` lands on argv after the managed
        prelude and before resume/model/allowed-tools, and does not
        displace the `-p <prompt>` suffix (#407)."""
        runner = self._runner(extra_args=["--chrome"])
        from untether.runners.claude import ClaudeStreamState

        state = ClaudeStreamState()
        token = ResumeToken(engine="claude", value="sess123")
        args = runner.build_args("hello", token, state=state)
        assert "--chrome" in args
        chrome_idx = args.index("--chrome")
        verbose_idx = args.index("--verbose")
        resume_idx = args.index("--resume")
        assert verbose_idx < chrome_idx < resume_idx
        # Prompt still last after `--`
        assert args[-2] == "--"
        assert args[-1] == "hello"

    def test_extra_args_chrome_permission_mode(self) -> None:
        """`extra_args` survives the permission-mode argv path (no -p,
        prompt sent via stdin)."""
        runner = self._runner(extra_args=["--chrome"])
        from untether.runners.claude import ClaudeStreamState

        state = ClaudeStreamState()
        opts = RunOptions(permission_mode="plan")
        with patch("untether.runners.claude.get_run_options", return_value=opts):
            args = runner.build_args("hello", None, state=state)
        assert "--chrome" in args
        assert "--permission-mode" in args
        chrome_idx = args.index("--chrome")
        perm_idx = args.index("--permission-mode")
        assert chrome_idx < perm_idx
        # permission-mode path sends prompt via stdin, no trailing `-- hello`
        assert "--" not in args
        assert "hello" not in args

    def test_extra_args_multiple(self) -> None:
        """Order between multiple user-supplied flags is preserved."""
        runner = self._runner(extra_args=["--chrome", "--strict-mcp-config"])
        from untether.runners.claude import ClaudeStreamState

        state = ClaudeStreamState()
        args = runner.build_args("hello", None, state=state)
        chrome_idx = args.index("--chrome")
        strict_idx = args.index("--strict-mcp-config")
        assert chrome_idx < strict_idx


class TestClaudeBuildRunner:
    """Coverage for extra_args parsing + reserved-flag validation in
    `build_runner` (#407)."""

    def _call(self, config: dict[str, Any]):
        from pathlib import Path

        from untether.runners.claude import build_runner

        return build_runner(config, Path("/tmp/untether.toml"))

    def test_extra_args_missing_yields_empty(self) -> None:
        runner = self._call({})
        assert runner.extra_args == []

    def test_extra_args_list_of_strings(self) -> None:
        runner = self._call({"extra_args": ["--chrome"]})
        assert runner.extra_args == ["--chrome"]

    def test_extra_args_non_list_raises(self) -> None:
        import pytest

        from untether.config import ConfigError

        with pytest.raises(ConfigError, match="list of strings"):
            self._call({"extra_args": "--chrome"})

    def test_extra_args_non_string_element_raises(self) -> None:
        import pytest

        from untether.config import ConfigError

        with pytest.raises(ConfigError, match="list of strings"):
            self._call({"extra_args": ["--chrome", 42]})

    def test_reserved_flag_rejected(self) -> None:
        import pytest

        from untether.config import ConfigError

        for reserved in (
            "-p",
            "--print",
            "--output-format",
            "--input-format",
            "--resume",
            "--continue",
            "--permission-mode",
            "--permission-prompt-tool",
        ):
            with pytest.raises(ConfigError, match="managed by Untether"):
                self._call({"extra_args": [reserved]})

    def test_reserved_prefix_rejected(self) -> None:
        import pytest

        from untether.config import ConfigError

        with pytest.raises(ConfigError, match="managed by Untether"):
            self._call({"extra_args": ["--output-format=text"]})

    def test_non_reserved_flag_accepted(self) -> None:
        # Sanity: `--chrome`, `--no-chrome`, `--mcp-config`, and other
        # upstream flags Untether doesn't manage must pass through.
        for flag in ("--chrome", "--no-chrome", "--mcp-config"):
            runner = self._call({"extra_args": [flag]})
            assert flag in runner.extra_args


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


class TestCodexBuildArgs:
    def _runner(self, **kwargs: Any):
        from untether.runners.codex import CodexRunner

        return CodexRunner(codex_cmd="codex", extra_args=[], **kwargs)

    def test_basic_prompt(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        args = runner.build_args("hello", None, state=state)
        assert "exec" in args
        assert "--json" in args
        assert args[-1] == "-"

    def test_resume(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        token = ResumeToken(engine="codex", value="thread123")
        args = runner.build_args("hello", token, state=state)
        assert "resume" in args
        assert "thread123" in args

    def test_continue(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        token = ResumeToken(engine="codex", value="", is_continue=True)
        args = runner.build_args("hello", token, state=state)
        assert "resume" in args
        assert "--last" in args
        assert args[-1] == "-"

    def test_model_override(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        opts = RunOptions(model="gpt-4o")
        with patch("untether.runners.codex.get_run_options", return_value=opts):
            args = runner.build_args("hello", None, state=state)
        assert "--model" in args
        idx = args.index("--model")
        assert args[idx + 1] == "gpt-4o"

    def test_reasoning_effort(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        opts = RunOptions(reasoning="high")
        with patch("untether.runners.codex.get_run_options", return_value=opts):
            args = runner.build_args("hello", None, state=state)
        assert "-c" in args
        idx = args.index("-c")
        assert "model_reasoning_effort=high" in args[idx + 1]

    def test_extra_args(self) -> None:
        from untether.runners.codex import CodexRunner

        runner = CodexRunner(codex_cmd="codex", extra_args=["-c", "notify=[]"])
        state = runner.new_state("hello", None)
        args = runner.build_args("hello", None, state=state)
        assert "-c" in args
        assert "notify=[]" in args

    def test_permission_mode_safe(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        opts = RunOptions(permission_mode="safe")
        with patch("untether.runners.codex.get_run_options", return_value=opts):
            args = runner.build_args("hello", None, state=state)
        assert "--ask-for-approval" in args
        idx = args.index("--ask-for-approval")
        assert args[idx + 1] == "untrusted"
        # Must come before "exec" (top-level flag, not exec subcommand flag)
        assert idx < args.index("exec")

    def test_permission_mode_none_defaults_to_never(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        opts = RunOptions(permission_mode=None)
        with patch("untether.runners.codex.get_run_options", return_value=opts):
            args = runner.build_args("hello", None, state=state)
        assert "--ask-for-approval" in args
        idx = args.index("--ask-for-approval")
        assert args[idx + 1] == "never"
        assert idx < args.index("exec")

    def test_run_options_none_defaults_to_never(self) -> None:
        """When run_options is None (no /config overrides), default to never."""
        runner = self._runner()
        state = runner.new_state("hello", None)
        args = runner.build_args("hello", None, state=state)
        assert "--ask-for-approval" in args
        idx = args.index("--ask-for-approval")
        assert args[idx + 1] == "never"
        assert idx < args.index("exec")


# ---------------------------------------------------------------------------
# OpenCode
# ---------------------------------------------------------------------------


class TestOpenCodeBuildArgs:
    def _runner(self, **kwargs: Any):
        from untether.runners.opencode import OpenCodeRunner

        return OpenCodeRunner(**kwargs)

    def test_basic_prompt(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        args = runner.build_args("hello", None, state=state)
        assert "run" in args
        assert "--format" in args
        assert "json" in args
        assert "--" in args
        assert args[-1] == "hello"

    def test_resume(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        token = ResumeToken(engine="opencode", value="ses_abc123")
        args = runner.build_args("hello", token, state=state)
        assert "--session" in args
        idx = args.index("--session")
        assert args[idx + 1] == "ses_abc123"

    def test_continue(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        token = ResumeToken(engine="opencode", value="", is_continue=True)
        args = runner.build_args("hello", token, state=state)
        assert "--continue" in args
        assert "--session" not in args

    def test_model_override(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        opts = RunOptions(model="gpt-4o")
        with patch("untether.runners.opencode.get_run_options", return_value=opts):
            args = runner.build_args("hello", None, state=state)
        assert "--model" in args
        idx = args.index("--model")
        assert args[idx + 1] == "gpt-4o"


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


class TestGeminiBuildArgs:
    def _runner(self, **kwargs: Any):
        from untether.runners.gemini import GeminiRunner

        return GeminiRunner(**kwargs)

    def test_basic_prompt(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        args = runner.build_args("hello", None, state=state)
        assert "--output-format" in args
        assert "stream-json" in args
        assert "--prompt=hello" in args

    def test_resume(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        token = ResumeToken(engine="gemini", value="abc123")
        args = runner.build_args("hello", token, state=state)
        assert "--resume" in args
        idx = args.index("--resume")
        assert args[idx + 1] == "abc123"

    def test_continue(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        token = ResumeToken(engine="gemini", value="", is_continue=True)
        args = runner.build_args("hello", token, state=state)
        assert "--resume" in args
        idx = args.index("--resume")
        assert args[idx + 1] == "latest"

    def test_model_override(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        opts = RunOptions(model="gemini-2.5-pro")
        with patch("untether.runners.gemini.get_run_options", return_value=opts):
            args = runner.build_args("hello", None, state=state)
        assert "--model" in args
        idx = args.index("--model")
        assert args[idx + 1] == "gemini-2.5-pro"

    def test_model_from_config(self) -> None:
        runner = self._runner(model="gemini-2.0-flash")
        state = runner.new_state("hello", None)
        with patch("untether.runners.gemini.get_run_options", return_value=None):
            args = runner.build_args("hello", None, state=state)
        assert "--model" in args
        idx = args.index("--model")
        assert args[idx + 1] == "gemini-2.0-flash"

    def test_permission_mode(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        opts = RunOptions(permission_mode="auto")
        with patch("untether.runners.gemini.get_run_options", return_value=opts):
            args = runner.build_args("hello", None, state=state)
        assert "--approval-mode" in args
        idx = args.index("--approval-mode")
        assert args[idx + 1] == "auto"

    def test_permission_mode_auto_edit(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        opts = RunOptions(permission_mode="auto_edit")
        with patch("untether.runners.gemini.get_run_options", return_value=opts):
            args = runner.build_args("hello", None, state=state)
        assert "--approval-mode" in args
        idx = args.index("--approval-mode")
        assert args[idx + 1] == "auto_edit"

    def test_permission_mode_none_defaults_to_yolo(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        opts = RunOptions(permission_mode=None)
        with patch("untether.runners.gemini.get_run_options", return_value=opts):
            args = runner.build_args("hello", None, state=state)
        assert "--approval-mode" in args
        idx = args.index("--approval-mode")
        assert args[idx + 1] == "yolo"

    def test_run_options_none_defaults_to_yolo(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        with patch("untether.runners.gemini.get_run_options", return_value=None):
            args = runner.build_args("hello", None, state=state)
        assert "--approval-mode" in args
        idx = args.index("--approval-mode")
        assert args[idx + 1] == "yolo"

    def test_skip_trust_default_includes_flag(self) -> None:
        """#471 — runs should pass --skip-trust by default so headless runs
        work outside ~/.gemini/trustedFolders.json."""
        runner = self._runner()
        state = runner.new_state("hello", None)
        with patch("untether.runners.gemini.get_run_options", return_value=None):
            args = runner.build_args("hello", None, state=state)
        assert "--skip-trust" in args

    def test_skip_trust_opt_out_omits_flag(self) -> None:
        """#471 — `[gemini] skip_trust = false` opts out so Gemini's own
        project-local trust gate is enforced (security-conscious deployments)."""
        runner = self._runner(skip_trust=False)
        state = runner.new_state("hello", None)
        with patch("untether.runners.gemini.get_run_options", return_value=None):
            args = runner.build_args("hello", None, state=state)
        assert "--skip-trust" not in args


# ---------------------------------------------------------------------------
# AMP
# ---------------------------------------------------------------------------


class TestAmpBuildArgs:
    def _runner(self, **kwargs: Any):
        from untether.runners.amp import AmpRunner

        return AmpRunner(**kwargs)

    def test_basic_prompt(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        args = runner.build_args("hello", None, state=state)
        assert "--stream-json" in args
        assert "-x" in args
        idx = args.index("-x")
        assert args[idx + 1] == "hello"

    def test_resume(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        token = ResumeToken(engine="amp", value="T-abc-123")
        args = runner.build_args("hello", token, state=state)
        assert "threads" in args
        assert "continue" in args
        assert "T-abc-123" in args

    def test_continue_skipped(self) -> None:
        """AMP has no 'most recent' mode, so is_continue is a no-op (starts new)."""
        runner = self._runner()
        state = runner.new_state("hello", None)
        token = ResumeToken(engine="amp", value="", is_continue=True)
        args = runner.build_args("hello", token, state=state)
        assert "threads" not in args
        assert "continue" not in args

    def test_model_override_becomes_mode(self) -> None:
        """AMP uses --mode not --model; model override maps to mode."""
        runner = self._runner()
        state = runner.new_state("hello", None)
        opts = RunOptions(model="deep")
        with patch("untether.runners.amp.get_run_options", return_value=opts):
            args = runner.build_args("hello", None, state=state)
        assert "--mode" in args
        idx = args.index("--mode")
        assert args[idx + 1] == "deep"

    def test_mode_from_config(self) -> None:
        runner = self._runner(mode="rush")
        state = runner.new_state("hello", None)
        with patch("untether.runners.amp.get_run_options", return_value=None):
            args = runner.build_args("hello", None, state=state)
        assert "--mode" in args
        idx = args.index("--mode")
        assert args[idx + 1] == "rush"

    def test_dangerously_allow_all_default(self) -> None:
        # #206: default is now safe — opt-in only via [amp] config.
        runner = self._runner()
        state = runner.new_state("hello", None)
        args = runner.build_args("hello", None, state=state)
        assert "--dangerously-allow-all" not in args

    def test_dangerously_allow_all_enabled(self) -> None:
        runner = self._runner(dangerously_allow_all=True)
        state = runner.new_state("hello", None)
        args = runner.build_args("hello", None, state=state)
        assert "--dangerously-allow-all" in args

    def test_dangerously_allow_all_disabled(self) -> None:
        runner = self._runner(dangerously_allow_all=False)
        state = runner.new_state("hello", None)
        args = runner.build_args("hello", None, state=state)
        assert "--dangerously-allow-all" not in args

    def test_flag_like_prompt_sanitised(self) -> None:
        """Prompts starting with - are sanitised to prevent flag injection (#194)."""
        runner = self._runner()
        state = runner.new_state("--help", None)
        args = runner.build_args("--help", None, state=state)
        idx = args.index("-x")
        assert args[idx + 1] == " --help"


# ---------------------------------------------------------------------------
# Gemini prompt sanitisation (#194)
# ---------------------------------------------------------------------------


class TestGeminiPromptSanitisation:
    def _runner(self, **kwargs: Any):
        from untether.runners.gemini import GeminiRunner

        return GeminiRunner(**kwargs)

    def test_flag_like_prompt_sanitised(self) -> None:
        """Prompts starting with - are sanitised in --prompt= value (#194)."""
        runner = self._runner()
        state = runner.new_state("--help", None)
        with patch("untether.runners.gemini.get_run_options", return_value=None):
            args = runner.build_args("--help", None, state=state)
        prompt_arg = [a for a in args if a.startswith("--prompt=")]
        assert len(prompt_arg) == 1
        assert prompt_arg[0] == "--prompt= --help"

    def test_normal_prompt_unchanged(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello world", None)
        with patch("untether.runners.gemini.get_run_options", return_value=None):
            args = runner.build_args("hello world", None, state=state)
        prompt_arg = [a for a in args if a.startswith("--prompt=")]
        assert prompt_arg[0] == "--prompt=hello world"


# ---------------------------------------------------------------------------
# Pi
# ---------------------------------------------------------------------------


class TestPiBuildArgs:
    def _runner(self, **kwargs: Any):
        from untether.runners.pi import PiRunner

        defaults: dict[str, Any] = {"extra_args": [], "model": None, "provider": None}
        defaults.update(kwargs)
        return PiRunner(**defaults)

    def test_basic_prompt(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        args = runner.build_args("hello", None, state=state)
        assert "--print" in args
        assert "--mode" in args
        assert "json" in args
        # prompt is the last arg
        assert args[-1] == "hello"

    def test_session_path(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        args = runner.build_args("hello", None, state=state)
        assert "--session" in args
        # Session path should be present
        idx = args.index("--session")
        assert args[idx + 1]  # non-empty

    def test_resume(self) -> None:
        runner = self._runner()
        token = ResumeToken(engine="pi", value="/path/to/session.jsonl")
        state = runner.new_state("hello", token)
        args = runner.build_args("hello", token, state=state)
        assert "--session" in args
        idx = args.index("--session")
        assert args[idx + 1] == "/path/to/session.jsonl"

    def test_continue(self) -> None:
        runner = self._runner()
        token = ResumeToken(engine="pi", value="", is_continue=True)
        state = runner.new_state("hello", token)
        args = runner.build_args("hello", token, state=state)
        assert "--continue" in args
        assert "--session" not in args

    def test_model_override(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        opts = RunOptions(model="claude-sonnet-4-20250514")
        with patch("untether.runners.pi.get_run_options", return_value=opts):
            args = runner.build_args("hello", None, state=state)
        assert "--model" in args
        idx = args.index("--model")
        assert args[idx + 1] == "claude-sonnet-4-20250514"

    def test_provider(self) -> None:
        runner = self._runner(provider="openrouter")
        state = runner.new_state("hello", None)
        args = runner.build_args("hello", None, state=state)
        assert "--provider" in args
        idx = args.index("--provider")
        assert args[idx + 1] == "openrouter"

    def test_prompt_sanitise_leading_dash(self) -> None:
        runner = self._runner()
        state = runner.new_state("-dangerous", None)
        args = runner.build_args("-dangerous", None, state=state)
        # Should prepend space to avoid flag parsing
        assert args[-1] == " -dangerous"
