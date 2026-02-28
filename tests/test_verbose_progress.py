"""Tests for verbose progress mode rendering."""

from __future__ import annotations

from untether.markdown import (
    MarkdownFormatter,
    format_verbose_detail,
)
from untether.model import Action
from untether.progress import ActionState, ProgressState


# --- format_verbose_detail tests ---


class TestFormatVerboseDetail:
    """Test format_verbose_detail for each tool type."""

    def test_bash_command(self):
        action = Action(
            id="1",
            kind="command",
            title="git status",
            detail={"name": "Bash", "input": {"command": "git diff --cached"}},
        )
        result = format_verbose_detail(action)
        assert result == "git diff --cached"

    def test_bash_command_truncated(self):
        action = Action(
            id="1",
            kind="command",
            title="long command",
            detail={"name": "Bash", "input": {"command": "x" * 300}},
        )
        result = format_verbose_detail(action)
        assert result is not None
        assert len(result) <= 201  # 200 + ellipsis char

    def test_read_file(self):
        action = Action(
            id="1",
            kind="tool",
            title="Read",
            detail={
                "name": "Read",
                "input": {"file_path": "/home/user/project/src/settings.py"},
                "result_len": 4821,
            },
        )
        result = format_verbose_detail(action)
        assert result is not None
        assert "settings.py" in result
        assert "4821 chars" in result

    def test_read_no_result_len(self):
        action = Action(
            id="1",
            kind="tool",
            title="Read",
            detail={
                "name": "Read",
                "input": {"file_path": "/home/user/project/README.md"},
            },
        )
        result = format_verbose_detail(action)
        assert result is not None
        assert "README.md" in result
        assert "chars" not in result

    def test_edit_file(self):
        action = Action(
            id="1",
            kind="tool",
            title="Edit",
            detail={
                "name": "Edit",
                "input": {
                    "file_path": "/home/user/project/src/markdown.py",
                    "old_string": "def old_function():",
                    "new_string": "def new_function():",
                },
            },
        )
        result = format_verbose_detail(action)
        assert result is not None
        assert "markdown.py" in result
        assert "old_function" in result

    def test_write_file(self):
        action = Action(
            id="1",
            kind="tool",
            title="Write",
            detail={
                "name": "Write",
                "input": {"file_path": "/home/user/project/new_file.py"},
            },
        )
        result = format_verbose_detail(action)
        assert result is not None
        assert "new_file.py" in result

    def test_grep_pattern(self):
        action = Action(
            id="1",
            kind="tool",
            title="Grep",
            detail={
                "name": "Grep",
                "input": {"pattern": "verbose.*mode", "path": "src/"},
            },
        )
        result = format_verbose_detail(action)
        assert result is not None
        assert "verbose.*mode" in result

    def test_glob_pattern(self):
        action = Action(
            id="1",
            kind="tool",
            title="Glob",
            detail={
                "name": "Glob",
                "input": {"pattern": "**/*.py"},
            },
        )
        result = format_verbose_detail(action)
        assert result is not None
        assert "**/*.py" in result

    def test_task_subagent(self):
        action = Action(
            id="1",
            kind="subagent",
            title="Task",
            detail={
                "name": "Task",
                "input": {"description": "Explore codebase patterns"},
            },
        )
        result = format_verbose_detail(action)
        assert result is not None
        assert "Explore codebase patterns" in result

    def test_web_search(self):
        action = Action(
            id="1",
            kind="web_search",
            title="WebSearch",
            detail={
                "name": "WebSearch",
                "input": {"query": "untether telegram verbose mode"},
            },
        )
        result = format_verbose_detail(action)
        assert result is not None
        assert "untether telegram verbose mode" in result

    def test_mcp_tool(self):
        action = Action(
            id="1",
            kind="tool",
            title="brave_web_search",
            detail={
                "name": "brave_web_search",
                "server": "brave-search",
                "tool": "brave_web_search",
                "input": {"query": "test"},
            },
        )
        result = format_verbose_detail(action)
        assert result is not None
        assert "brave-search:brave_web_search" in result

    def test_no_detail(self):
        action = Action(id="1", kind="tool", title="unknown", detail={})
        result = format_verbose_detail(action)
        assert result is None

    def test_none_detail(self):
        action = Action(id="1", kind="tool", title="unknown", detail=None)
        result = format_verbose_detail(action)
        assert result is None

    def test_fallback_string_arg(self):
        action = Action(
            id="1",
            kind="tool",
            title="CustomTool",
            detail={
                "name": "CustomTool",
                "input": {"some_arg": "some value"},
            },
        )
        result = format_verbose_detail(action)
        assert result is not None
        assert "some value" in result

    def test_empty_command(self):
        """Bash action with empty command returns None."""
        action = Action(
            id="1",
            kind="command",
            title="",
            detail={"name": "Bash", "input": {"command": ""}},
        )
        result = format_verbose_detail(action)
        assert result is None


# --- MarkdownFormatter verbose mode tests ---


def _make_action_state(
    action_id: str,
    kind: str = "tool",
    title: str = "Read",
    phase: str = "completed",
    ok: bool = True,
    detail: dict | None = None,
) -> ActionState:
    action = Action(id=action_id, kind=kind, title=title, detail=detail)
    return ActionState(
        action=action,
        phase=phase,
        ok=ok,
        display_phase=phase,
        completed=phase == "completed",
        first_seen=0,
        last_update=0,
    )


class TestMarkdownFormatterVerbose:
    """Test MarkdownFormatter with verbose mode."""

    def test_compact_mode_no_detail(self):
        """Compact mode should not include detail lines."""
        formatter = MarkdownFormatter(verbosity="compact")
        state = ProgressState(
            engine="claude",
            action_count=1,
            actions=(
                _make_action_state(
                    "1",
                    detail={
                        "name": "Read",
                        "input": {"file_path": "/src/test.py"},
                    },
                ),
            ),
            resume=None,
            resume_line=None,
            context_line=None,
        )
        lines = formatter._format_actions(state)
        assert len(lines) == 1
        assert "→" not in lines[0]

    def test_verbose_mode_adds_detail(self):
        """Verbose mode should add a detail line below each action."""
        formatter = MarkdownFormatter(verbosity="verbose")
        state = ProgressState(
            engine="claude",
            action_count=1,
            actions=(
                _make_action_state(
                    "1",
                    detail={
                        "name": "Read",
                        "input": {"file_path": "/src/test.py"},
                    },
                ),
            ),
            resume=None,
            resume_line=None,
            context_line=None,
        )
        lines = formatter._format_actions(state)
        assert len(lines) == 2
        assert "→" in lines[1]
        assert "test.py" in lines[1]

    def test_verbose_detail_indented(self):
        """Verbose detail lines should be indented with 2 spaces."""
        formatter = MarkdownFormatter(verbosity="verbose")
        state = ProgressState(
            engine="claude",
            action_count=1,
            actions=(
                _make_action_state(
                    "1",
                    detail={
                        "name": "Grep",
                        "input": {"pattern": "verbose"},
                    },
                ),
            ),
            resume=None,
            resume_line=None,
            context_line=None,
        )
        lines = formatter._format_actions(state)
        assert lines[1].startswith("  ")

    def test_verbose_no_detail_for_empty(self):
        """Actions with no extractable detail should not get detail lines."""
        formatter = MarkdownFormatter(verbosity="verbose")
        state = ProgressState(
            engine="claude",
            action_count=1,
            actions=(_make_action_state("1", detail={}),),
            resume=None,
            resume_line=None,
            context_line=None,
        )
        lines = formatter._format_actions(state)
        assert len(lines) == 1

    def test_verbose_multiple_actions(self):
        """Multiple actions each get their own detail lines."""
        formatter = MarkdownFormatter(verbosity="verbose")
        state = ProgressState(
            engine="claude",
            action_count=2,
            actions=(
                _make_action_state(
                    "1",
                    kind="command",
                    title="git status",
                    detail={"name": "Bash", "input": {"command": "git status"}},
                ),
                _make_action_state(
                    "2",
                    detail={
                        "name": "Read",
                        "input": {"file_path": "/src/main.py"},
                    },
                ),
            ),
            resume=None,
            resume_line=None,
            context_line=None,
        )
        lines = formatter._format_actions(state)
        assert len(lines) == 4  # 2 action lines + 2 detail lines

    def test_max_actions_respected_in_verbose(self):
        """max_actions limits the number of actions shown in verbose mode."""
        formatter = MarkdownFormatter(verbosity="verbose", max_actions=1)
        state = ProgressState(
            engine="claude",
            action_count=2,
            actions=(
                _make_action_state(
                    "1",
                    detail={
                        "name": "Read",
                        "input": {"file_path": "/src/old.py"},
                    },
                ),
                _make_action_state(
                    "2",
                    detail={
                        "name": "Read",
                        "input": {"file_path": "/src/new.py"},
                    },
                ),
            ),
            resume=None,
            resume_line=None,
            context_line=None,
        )
        lines = formatter._format_actions(state)
        # Only the last action (max_actions=1) with its detail
        assert len(lines) == 2
        assert "new.py" in lines[1]
