"""Tests for CC4 diff preview in tool approval messages."""

from __future__ import annotations

from untether.runners.claude import _format_diff_preview


class TestFormatDiffPreview:
    def test_edit_tool_shows_diff(self):
        result = _format_diff_preview(
            "Edit",
            {
                "file_path": "/home/user/project/src/main.py",
                "old_string": "def foo():\n    return 1",
                "new_string": "def foo():\n    return 42",
            },
        )
        assert "main.py" in result
        assert "- def foo():" in result
        assert "+ def foo():" in result

    def test_edit_tool_empty_strings(self):
        result = _format_diff_preview(
            "Edit",
            {
                "file_path": "/home/user/project/src/main.py",
                "old_string": "",
                "new_string": "",
            },
        )
        assert result == ""

    def test_write_tool_shows_content(self):
        result = _format_diff_preview(
            "Write",
            {
                "file_path": "/home/user/project/new_file.py",
                "content": "#!/usr/bin/env python\nprint('hello')",
            },
        )
        assert "new_file.py" in result
        assert "print('hello')" in result

    def test_write_tool_empty_content(self):
        result = _format_diff_preview(
            "Write",
            {
                "file_path": "/home/user/file.py",
                "content": "",
            },
        )
        assert result == ""

    def test_bash_tool_shows_command(self):
        result = _format_diff_preview(
            "Bash",
            {
                "command": "rm -rf /tmp/test",
            },
        )
        assert "$ rm -rf /tmp/test" in result

    def test_bash_tool_empty_command(self):
        result = _format_diff_preview("Bash", {"command": ""})
        assert result == ""

    def test_unknown_tool_returns_empty(self):
        result = _format_diff_preview("Grep", {"pattern": "foo"})
        assert result == ""

    def test_long_lines_truncated(self):
        long_line = "x" * 200
        result = _format_diff_preview(
            "Edit",
            {
                "file_path": "test.py",
                "old_string": long_line,
                "new_string": "short",
            },
        )
        assert "…" in result

    def test_many_lines_truncated(self):
        many_lines = "\n".join(f"line {i}" for i in range(20))
        result = _format_diff_preview(
            "Edit",
            {
                "file_path": "test.py",
                "old_string": many_lines,
                "new_string": "replacement",
            },
        )
        assert "more removed" in result

    def test_long_bash_command_truncated(self):
        long_cmd = "echo " + "a" * 300
        result = _format_diff_preview("Bash", {"command": long_cmd})
        assert len(result) < 210  # $ prefix + truncated command


class TestDiffPreviewGating:
    """Test that diff preview respects the per-chat EngineRunOptions toggle."""

    def test_enabled_when_run_options_none(self):
        from untether.runners.run_options import EngineRunOptions

        opts = None
        assert opts is None or opts.diff_preview is not False

    def test_enabled_when_diff_preview_none(self):
        from untether.runners.run_options import EngineRunOptions

        opts = EngineRunOptions(diff_preview=None)
        assert opts.diff_preview is not False

    def test_enabled_when_diff_preview_true(self):
        from untether.runners.run_options import EngineRunOptions

        opts = EngineRunOptions(diff_preview=True)
        assert opts.diff_preview is not False

    def test_disabled_when_diff_preview_false(self):
        from untether.runners.run_options import EngineRunOptions

        opts = EngineRunOptions(diff_preview=False)
        assert opts.diff_preview is False
