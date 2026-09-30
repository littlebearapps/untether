"""Tests for build_args() across all engine runners.

Validates that CLI argument construction is correct for each engine,
covering prompt format, model override, resume, permission mode, and
engine-specific flags. Prevents regressions like #75, #76, #77, #78.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from untether.model import ResumeToken
from untether.runners.run_options import CLAUDE_PLAN_AUTO_MODE
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

    def _cli_mode_for(self, mode: str) -> str:
        runner = self._runner()
        from untether.runners.claude import ClaudeStreamState

        state = ClaudeStreamState()
        opts = RunOptions(permission_mode=mode)
        with patch("untether.runners.claude.get_run_options", return_value=opts):
            args = runner.build_args("hello", None, state=state)
        return args[args.index("--permission-mode") + 1]

    @pytest.mark.parametrize(
        "mode",
        ["default", "manual", "plan", "auto", "acceptEdits", "dontAsk"],
    )
    def test_genuine_cli_modes_pass_through_verbatim(self, mode: str) -> None:
        """#741 every real CLI mode reaches the binary unmodified.

        `auto` is the regression guard: until 0.35.5rc8 it was rewritten to
        `plan`, which made Claude Code's own auto mode unreachable.
        """
        assert self._cli_mode_for(mode) == mode

    def test_plan_auto_sugar_sends_plan(self) -> None:
        """Untether's own `plan-auto` is the only translated value (#741)."""
        assert self._cli_mode_for(CLAUDE_PLAN_AUTO_MODE) == "plan"

    def test_permission_prompt_tool_still_set_in_auto(self) -> None:
        """Auto mode keeps the stdio control channel wired.

        Verified against CLI 2.1.228: an `AskUserQuestion` call still raises a
        `can_use_tool` control_request in auto mode, so Telegram approval
        buttons keep working. Dropping the flag would silently break that.
        """
        runner = self._runner()
        from untether.runners.claude import ClaudeStreamState

        state = ClaudeStreamState()
        opts = RunOptions(permission_mode="auto")
        with patch("untether.runners.claude.get_run_options", return_value=opts):
            args = runner.build_args("hello", None, state=state)
        idx = args.index("--permission-prompt-tool")
        assert args[idx + 1] == "stdio"

    def test_auto_does_not_arm_exit_plan_mode_rubber_stamp(self) -> None:
        """The security half of #741.

        Upstream `auto` has no plan gate, so it must not set the flag that
        auto-approves `ExitPlanMode` and (via #283) bypasses downstream diff
        previews. Only `plan-auto` may do that.
        """
        runner = self._runner()
        opts = RunOptions(permission_mode="auto")
        with patch("untether.runners.claude.get_run_options", return_value=opts):
            state = runner.new_state("hello", None)
        assert state.auto_approve_exit_plan_mode is False

        opts = RunOptions(permission_mode=CLAUDE_PLAN_AUTO_MODE)
        with patch("untether.runners.claude.get_run_options", return_value=opts):
            state = runner.new_state("hello", None)
        assert state.auto_approve_exit_plan_mode is True

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

    # --- #830: safe → exec-level `--sandbox read-only`; no --ask-for-approval ---

    def _safe_args(self, resume: ResumeToken | None = None, **kwargs: Any):
        runner = self._runner(**kwargs)
        state = runner.new_state("hello", None)
        opts = RunOptions(permission_mode="safe")
        with patch("untether.runners.codex.get_run_options", return_value=opts):
            return runner.build_args("hello", resume, state=state)

    def test_permission_mode_safe_uses_read_only_sandbox(self) -> None:
        args = self._safe_args()
        i = args.index("--sandbox")
        assert args[i : i + 2] == ["--sandbox", "read-only"]
        # Exec-level: after `exec`, so it outranks any root-level sandbox flag.
        assert i > args.index("exec")
        assert args[-1] == "-"
        # Regression for #830: `untrusted` was removed in codex-cli 0.149.0.
        assert "--ask-for-approval" not in args
        assert "-a" not in args
        assert "untrusted" not in args

    def test_permission_mode_safe_resume_sandbox_before_resume(self) -> None:
        args = self._safe_args(ResumeToken(engine="codex", value="abc"))
        assert args[-5:] == ["--sandbox", "read-only", "resume", "abc", "-"]

    def test_permission_mode_safe_continue(self) -> None:
        args = self._safe_args(ResumeToken(engine="codex", value="", is_continue=True))
        assert args[-5:] == ["--sandbox", "read-only", "resume", "--last", "-"]

    def test_safe_sandbox_is_exec_level_despite_root_extra_args(self) -> None:
        from untether.runners.codex import CodexRunner

        runner = CodexRunner(
            codex_cmd="codex", extra_args=["--sandbox", "danger-full-access"]
        )
        state = runner.new_state("hello", None)
        opts = RunOptions(permission_mode="safe")
        with patch("untether.runners.codex.get_run_options", return_value=opts):
            args = runner.build_args("hello", None, state=state)
        exec_idx = args.index("exec")
        # The root copy stays before `exec` (upstream precedence: an exec-level
        # --sandbox wins, R4) and Untether's read-only follows `exec`.
        assert args[:2] == ["--sandbox", "danger-full-access"]
        tail = args[exec_idx:]
        assert tail[tail.index("--sandbox") + 1] == "read-only"

    def test_permission_mode_none_passes_no_policy_flags(self) -> None:
        runner = self._runner()
        state = runner.new_state("hello", None)
        opts = RunOptions(permission_mode=None)
        with patch("untether.runners.codex.get_run_options", return_value=opts):
            args = runner.build_args("hello", None, state=state)
        assert "--ask-for-approval" not in args
        assert "-a" not in args
        assert "--sandbox" not in args

    def test_run_options_none_passes_no_policy_flags(self) -> None:
        """No /config overrides: full auto, Codex's own sandbox setting."""
        runner = self._runner()
        state = runner.new_state("hello", None)
        args = runner.build_args("hello", None, state=state)
        assert "--ask-for-approval" not in args
        assert "-a" not in args
        assert "--sandbox" not in args

    def test_permission_mode_auto_is_full_auto_without_warn(
        self, reset_codex_pm_warned: None
    ) -> None:
        from structlog.testing import capture_logs

        runner = self._runner()
        state = runner.new_state("hello", None)
        opts = RunOptions(permission_mode="auto")
        with (
            capture_logs() as logs,
            patch("untether.runners.codex.get_run_options", return_value=opts),
        ):
            args = runner.build_args("hello", None, state=state)
        assert "--sandbox" not in args
        assert not [e for e in logs if e["event"] == "codex.permission_mode.unknown"]

    def test_unknown_permission_mode_warns_once_and_runs_default(
        self, reset_codex_pm_warned: None
    ) -> None:
        from structlog.testing import capture_logs

        runner = self._runner()
        opts = RunOptions(permission_mode="read-only")
        with (
            capture_logs() as logs,
            patch("untether.runners.codex.get_run_options", return_value=opts),
        ):
            first = runner.build_args("hello", None, state=runner.new_state("h", None))
            second = runner.build_args("hello", None, state=runner.new_state("h", None))
        assert "--sandbox" not in first
        assert "--sandbox" not in second
        warns = [e for e in logs if e["event"] == "codex.permission_mode.unknown"]
        assert len(warns) == 1
        assert warns[0]["mode"] == "read-only"
        assert warns[0]["log_level"] == "warning"


@pytest.fixture
def reset_codex_pm_warned(monkeypatch: pytest.MonkeyPatch) -> None:
    import untether.runners.codex as codex_mod

    monkeypatch.setattr(codex_mod, "_UNKNOWN_PM_WARNED", set())


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


# ---------------------------------------------------------------------------
# #749 phase 02 — mode-aware `--allowedTools`
#
# Stage 5 (`permissions.allow` + `--allowedTools`) sits BEFORE the stage-6
# prompt.  Sending `Bash,Read,Edit,Write` there pre-approves exactly the tools
# a prompting mode exists to ask about, so phase 01's gate would never see
# them.  The two phases are one fix: reverting 02 alone degrades to
# "allowlist pre-approves"; reverting 01 alone leaves requests reaching a
# handler that blanket-approves.  Do not revert 01 without also reverting 02.
# ---------------------------------------------------------------------------


class TestClaudeAllowedToolsByMode:
    def _runner(self, **kwargs: Any):
        from untether.runners.claude import DEFAULT_ALLOWED_TOOLS, ClaudeRunner

        kwargs.setdefault("allowed_tools", DEFAULT_ALLOWED_TOOLS)
        return ClaudeRunner(claude_cmd="claude", **kwargs)

    def _args(self, mode: str | None, **kwargs: Any) -> list[str]:
        from untether.runners.claude import ClaudeStreamState

        runner = self._runner(permission_mode=mode, **kwargs)
        return runner.build_args("hello", None, state=ClaudeStreamState())

    # -- prompting modes: the flag must be gone ---------------------------

    def test_749_no_allowed_tools_for_default(self) -> None:
        assert "--allowedTools" not in self._args("default")

    def test_749_no_allowed_tools_for_manual(self) -> None:
        assert "--allowedTools" not in self._args("manual")

    def test_749_no_allowed_tools_for_accept_edits(self) -> None:
        """D-4: out-of-scope writes are supposed to prompt under acceptEdits."""
        assert "--allowedTools" not in self._args("acceptEdits")

    def test_749_dropping_the_flag_leaves_the_rest_of_argv_intact(self) -> None:
        """Only the allowlist pair is removed — nothing else shifts.

        `extra_args` position is load-bearing (#407), so prove the removal is
        surgical rather than a rebuild of the argv.
        """
        with_flag = self._args("plan")
        without = self._args("default")
        idx = with_flag.index("--allowedTools")
        expected = with_flag[:idx] + with_flag[idx + 2 :]
        # Only the permission-mode value itself should differ.
        assert [a for a in expected if a not in ("plan", "default")] == [
            a for a in without if a not in ("plan", "default")
        ]

    # -- autonomous modes: the flag must stay -----------------------------

    def test_749_allowed_tools_present_for_plan(self) -> None:
        """D-3 / probe H+I: dropping it here is pure stage-6 overhead."""
        assert "--allowedTools" in self._args("plan")

    def test_749_allowed_tools_present_for_plan_auto(self) -> None:
        assert "--allowedTools" in self._args(CLAUDE_PLAN_AUTO_MODE)

    def test_749_allowed_tools_present_for_auto(self) -> None:
        assert "--allowedTools" in self._args("auto")

    def test_749_allowed_tools_present_for_dont_ask(self) -> None:
        """D-6 / probe F: `dontAsk` auto-denies anything not pre-approved, so
        without an allowlist it is unusable."""
        assert "--allowedTools" in self._args("dontAsk")

    def test_749_allowed_tools_present_for_bypass(self) -> None:
        assert "--allowedTools" in self._args("bypassPermissions")

    def test_749_allowed_tools_present_when_no_mode_configured(self) -> None:
        """No mode => legacy `-p` path, no control channel, nothing to gate."""
        assert "--allowedTools" in self._args(None)

    # -- explicit user override ------------------------------------------

    def test_749_explicit_user_allowed_tools_honoured_in_prompting_mode(self) -> None:
        """An explicit `[engines.claude] allowed_tools` is not overridden.

        Dropping a value the user deliberately wrote would be Untether
        silently reversing their configuration; the log line is how they find
        out the two settings interact.
        """
        args = self._args(
            "default", allowed_tools=["Read", "Grep"], allowed_tools_explicit=True
        )
        assert "--allowedTools" in args
        assert args[args.index("--allowedTools") + 1] == "Read,Grep"

    def test_749_default_allowlist_is_not_treated_as_explicit(self) -> None:
        """The fallback must stay droppable — it is plumbing, not a choice."""
        from untether.runners.claude import DEFAULT_ALLOWED_TOOLS

        args = self._args(
            "default",
            allowed_tools=DEFAULT_ALLOWED_TOOLS,
            allowed_tools_explicit=False,
        )
        assert "--allowedTools" not in args

    def test_749_explicit_override_logs_once_per_process(self) -> None:
        """One INFO, not one per run — a chatty log is an ignored log."""
        import untether.runners.claude as claude_mod

        claude_mod._PROMPTING_MODE_ALLOWLIST_LOGGED.clear()
        with patch.object(claude_mod.logger, "info") as mock_info:
            for _ in range(3):
                self._args(
                    "default",
                    allowed_tools=["Read"],
                    allowed_tools_explicit=True,
                )
        calls = [
            c
            for c in mock_info.call_args_list
            if c.args and c.args[0] == "claude.allowed_tools.prompting_mode_override"
        ]
        assert len(calls) == 1, f"expected exactly one INFO, got {len(calls)}"
        assert calls[0].kwargs.get("permission_mode") == "default"

    def test_749_no_override_log_in_an_autonomous_mode(self) -> None:
        """Nothing is being overridden there — the allowlist is sent anyway."""
        import untether.runners.claude as claude_mod

        claude_mod._PROMPTING_MODE_ALLOWLIST_LOGGED.clear()
        with patch.object(claude_mod.logger, "info") as mock_info:
            self._args("plan", allowed_tools=["Read"], allowed_tools_explicit=True)
        assert not [
            c
            for c in mock_info.call_args_list
            if c.args and c.args[0] == "claude.allowed_tools.prompting_mode_override"
        ]

    # -- regressions ------------------------------------------------------

    def test_749_empty_allowed_tools_list_still_drops_flag(self) -> None:
        """`_coerce_comma_list([])` returns None — an empty list must not
        become a bare `--allowedTools` with no value, in any mode."""
        for mode in ("default", "plan", "auto", None):
            args = self._args(mode, allowed_tools=[], allowed_tools_explicit=True)
            assert "--allowedTools" not in args, f"empty list leaked in {mode!r}"

    def test_749_build_runner_marks_explicit_config(self) -> None:
        """The flag must come from real config parsing, not just the ctor."""
        from pathlib import Path

        from untether.runners.claude import build_runner

        explicit = build_runner(
            {"permission_mode": "default", "allowed_tools": ["Read"]},
            Path("untether.toml"),
        )
        assert explicit.allowed_tools_explicit is True

        implicit = build_runner({"permission_mode": "default"}, Path("untether.toml"))
        assert implicit.allowed_tools_explicit is False


# ---------------------------------------------------------------------------
# #812 — `--include-hook-events`
#
# Passed only in control-channel mode, behind `[watchdog] hold_for_async_hooks`,
# when a cached `claude --help` probe lists the flag, and never twice. Not a
# reserved flag (D-2): a config that already passes it keeps working.
# ---------------------------------------------------------------------------

_HELP_WITH_FLAG = (
    "Usage: claude [options]\n"
    "  --include-hook-events  Include all hook lifecycle events in the output "
    "stream (only works with --output-format=stream-json)\n"
)


class TestClaudeIncludeHookEvents:
    @pytest.fixture
    def cli(self, tmp_path, monkeypatch: pytest.MonkeyPatch):
        from untether.runners import claude as claude_mod

        binary = tmp_path / "claude"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        calls: list[str] = []
        help_text = {"value": _HELP_WITH_FLAG}

        def _probe(path: str) -> str | None:
            calls.append(path)
            return help_text["value"]

        monkeypatch.setattr(claude_mod, "_probe_cli_help", _probe)
        return {"path": str(binary), "calls": calls, "help": help_text}

    def _args(self, cli: dict[str, Any], mode: str | None = "plan", **kw: Any):
        from untether.runners.claude import ClaudeRunner, ClaudeStreamState

        runner = ClaudeRunner(claude_cmd=cli["path"], permission_mode=mode, **kw)
        return runner.build_args("hello", None, state=ClaudeStreamState())

    def test_812_include_hook_events_in_control_channel(self, cli) -> None:
        args = self._args(cli)
        assert args.count("--include-hook-events") == 1
        # One probe, cached per (path, mtime): a second build doesn't re-run it.
        self._args(cli)
        assert len(cli["calls"]) == 1

    def test_812_include_hook_events_dedup_extra_args(self, cli) -> None:
        args = self._args(cli, extra_args=["--include-hook-events"])
        assert args.count("--include-hook-events") == 1

    def test_812_flag_is_not_reserved(self) -> None:
        from pathlib import Path

        from untether.runners.claude import build_runner

        runner = build_runner(
            {"extra_args": ["--include-hook-events"]}, Path("/tmp/untether.toml")
        )
        assert runner.extra_args == ["--include-hook-events"]

    def test_812_kill_switch_off_omits_flag(
        self, cli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pathlib import Path
        from types import SimpleNamespace

        from untether.runners import claude as claude_mod
        from untether.settings import WatchdogSettings

        watchdog = WatchdogSettings(hold_for_async_hooks=False)
        monkeypatch.setattr(
            claude_mod,
            "load_settings_if_exists",
            lambda *a, **k: (SimpleNamespace(watchdog=watchdog), Path("x")),
        )
        assert "--include-hook-events" not in self._args(cli)

    def test_812_cli_lacks_flag_omits_it(self, cli) -> None:
        cli["help"]["value"] = "Usage: claude [options]\n  --verbose\n"
        assert "--include-hook-events" not in self._args(cli)

    def test_812_probe_failure_is_treated_as_unsupported(self, cli) -> None:
        cli["help"]["value"] = None
        assert "--include-hook-events" not in self._args(cli)

    def test_812_unresolvable_cli_omits_flag(self) -> None:
        from untether.runners.claude import cli_supports_hook_events

        assert cli_supports_hook_events("definitely-not-a-claude-binary-812") is False

    def test_812_cli_upgrade_reprobes(self, cli) -> None:
        import os

        self._args(cli)
        st = os.stat(cli["path"])
        os.utime(cli["path"], (st.st_atime, st.st_mtime + 10))
        self._args(cli)
        assert len(cli["calls"]) == 2

    def test_812_not_passed_in_plain_p_mode(self, cli) -> None:
        args = self._args(cli, mode=None)
        assert "-p" in args
        assert "--include-hook-events" not in args
        assert cli["calls"] == []  # no probe when it can't matter

    def test_812_settings_defaults(self) -> None:
        import pydantic

        from untether.settings import WatchdogSettings

        wd = WatchdogSettings()
        assert wd.hold_for_async_hooks is True
        assert wd.async_hook_max_hold == 630.0
        WatchdogSettings(async_hook_max_hold=0)
        WatchdogSettings(async_hook_max_hold=3600)
        with pytest.raises(pydantic.ValidationError):
            WatchdogSettings(async_hook_max_hold=3601)
        with pytest.raises(pydantic.ValidationError):
            WatchdogSettings(async_hook_max_hold=-1)
