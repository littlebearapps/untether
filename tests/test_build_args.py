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

    def test_permission_mode_none_no_approval_flag(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        opts = RunOptions(permission_mode=None)
        with patch("untether.runners.codex.get_run_options", return_value=opts):
            args = runner.build_args("hello", None, state=state)
        assert "--ask-for-approval" not in args


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
        assert "-p" in args
        idx = args.index("-p")
        assert args[idx + 1] == "hello"

    def test_resume(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        token = ResumeToken(engine="gemini", value="abc123")
        args = runner.build_args("hello", token, state=state)
        assert "--resume" in args
        idx = args.index("--resume")
        assert args[idx + 1] == "abc123"

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
        runner = self._runner()
        state = runner.new_state("hello", None)
        args = runner.build_args("hello", None, state=state)
        assert "--dangerously-allow-all" in args

    def test_dangerously_allow_all_disabled(self) -> None:
        runner = self._runner(dangerously_allow_all=False)
        state = runner.new_state("hello", None)
        args = runner.build_args("hello", None, state=state)
        assert "--dangerously-allow-all" not in args


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
