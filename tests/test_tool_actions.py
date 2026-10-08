from __future__ import annotations

import pytest

from untether.runners import tool_actions
from untether.utils.paths import reset_run_base_dir, set_run_base_dir


def test_tool_input_path_picks_first_match() -> None:
    tool_input = {"path": "src/main.py", "file": "ignored.txt"}
    assert (
        tool_actions.tool_input_path(tool_input, path_keys=("file", "path"))
        == "ignored.txt"
    )
    assert (
        tool_actions.tool_input_path(tool_input, path_keys=("path", "file"))
        == "src/main.py"
    )
    assert tool_actions.tool_input_path(tool_input, path_keys=("missing",)) is None


@pytest.mark.parametrize(
    ("tool_name", "tool_input", "expected_kind", "expected_title"),
    [
        ("bash", {"command": "echo hi"}, "command", "echo hi"),
        ("shell", {"command": "pwd"}, "command", "pwd"),
        ("edit", {"path": "src/app.py"}, "file_change", "src/app.py"),
        ("write", {"path": "notes.txt"}, "file_change", "notes.txt"),
        ("read", {"path": "README.md"}, "tool", "read: `README.md`"),
        ("glob", {"pattern": "*.py"}, "tool", "glob: `*.py`"),
        # #871 D1: grep/find patterns are code spans too (no stray italics)
        ("grep", {"pattern": "TODO"}, "tool", "grep: `TODO`"),
        ("find", {"pattern": "*.toml"}, "tool", "find: `*.toml`"),
        ("grep", {"pattern": "`foo`"}, "tool", "grep: `` `foo` ``"),
        ("ls", {"path": "src"}, "tool", "ls: `src`"),
        ("websearch", {"query": "untether"}, "web_search", "untether"),
        (
            "webfetch",
            {"url": "https://example.com"},
            "web_search",
            "https://example.com",
        ),
        ("todowrite", {}, "note", "update todos"),
        ("todoread", {}, "note", "read todos"),
        ("askuserquestion", {}, "note", "ask user"),
        ("task", {"description": "do work"}, "subagent", "do work"),
        ("agent", {"prompt": "assist"}, "subagent", "assist"),
        ("unknown", {}, "tool", "unknown"),
    ],
)
def test_tool_kind_and_title_cases(
    tool_name: str,
    tool_input: dict[str, object],
    expected_kind: str,
    expected_title: str,
) -> None:
    token = set_run_base_dir(None)
    try:
        kind, title = tool_actions.tool_kind_and_title(
            tool_name,
            tool_input,
            path_keys=("path", "file"),
        )
    finally:
        reset_run_base_dir(token)

    assert kind == expected_kind
    assert title == expected_title


def test_tool_kind_and_title_task_kind_override() -> None:
    kind, title = tool_actions.tool_kind_and_title(
        "agent",
        {"description": "spawn worker"},
        path_keys=(),
        task_kind="warning",
    )

    assert kind == "warning"
    assert title == "spawn worker"


def test_871_tool_title_pattern_renders_without_italics() -> None:
    from untether.telegram.render import render_markdown

    kind, title = tool_actions.tool_kind_and_title(
        "grep", {"pattern": "*x*"}, path_keys=("path",)
    )
    assert kind == "tool"
    text, entities = render_markdown(title)
    assert text == "grep: *x*"
    assert [e["type"] for e in entities] == ["code"]
