"""Ask me's safe list, path rules and card previews (#980, phase 14 §3-§5).

``gate_policy`` is pure: no socket, no agy. Tool names and argument shapes
come from agy 1.3.3 / 1.3.4 hook payloads (E2, P-B4, P-H8, the 2026-10-13
live runs).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from untether.antigravity import gate_plugin, gate_policy
from untether.antigravity.gate_policy import classify, classify_edit
from untether.utils.paths import reset_run_base_dir, set_run_base_dir

CID = "43209de7-b728-4f19-8a03-ca4d0ae8f0d6"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "README.md").write_text("hello\n")
    return proj


@pytest.fixture(autouse=True)
def _grants() -> None:
    gate_policy._reset_for_tests()


def _edit(target: str | Path, **extra: object) -> dict[str, object]:
    return {"TargetFile": str(target), "CodeContent": "x", **extra}


@pytest.mark.parametrize(
    "tool",
    [
        "view_file",
        "list_dir",
        "find_by_name",
        "grep_search",
        "command_status",
        "finish",
    ],
)
def test_read_tools_allowed(tool: str, root: Path) -> None:
    assert classify(tool, {}, root=root, conversation_id=CID).allow


@pytest.mark.parametrize(
    "tool",
    [
        "run_command",
        "send_command_input",
        "read_url_content",
        "search_web",
        "call_mcp_tool",
        "browser_click_element",
        "invoke_subagent",
        "schedule",
        "manage_task",
        "sed_file",
        "notebook_edit",
        "generate_image",
        "a_tool_from_agy_2_0",
        "",
    ],
)
def test_unknown_and_ask_list_tools_ask(tool: str, root: Path) -> None:
    verdict = classify(tool, {}, root=root, conversation_id=CID)
    assert not verdict.allow and verdict.why == "tool"


def test_edit_in_project_allowed(root: Path) -> None:
    for tool in gate_policy.EDIT_TOOLS:
        assert classify(
            tool, _edit(root / "src" / "new.py"), root=root, conversation_id=CID
        ).allow
    # Relative targets resolve against the project.
    assert classify_edit(_edit("README.md"), root=root, conversation_id=CID).allow


def test_edit_outside_project_asks(root: Path, tmp_path: Path) -> None:
    for target in (tmp_path / "other.txt", "/etc/passwd", root / ".." / "x.txt"):
        verdict = classify_edit(_edit(target), root=root, conversation_id=CID)
        assert not verdict.allow and verdict.why == "outside"
    missing = classify_edit({"CodeContent": "x"}, root=root, conversation_id=CID)
    assert not missing.allow


def test_edit_artifact_dir_allowed(root: Path) -> None:
    target = gate_policy.artifact_dir(CID) / "task.md"
    assert classify_edit(_edit(target), root=root, conversation_id=CID).allow
    # Another conversation's artifact folder is outside.
    other = gate_policy.artifact_dir("someone-else") / "task.md"
    assert not classify_edit(_edit(other), root=root, conversation_id=CID).allow


def test_edit_symlink_escape_asks(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("x")
    (root / "link.txt").symlink_to(outside / "f.txt")
    verdict = classify_edit(_edit(root / "link.txt"), root=root, conversation_id=CID)
    assert not verdict.allow and verdict.why == "outside"


def test_edit_new_file_under_symlinked_dir_asks(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    target = root / "linked" / "deeper" / "new.txt"
    verdict = classify_edit(_edit(target), root=root, conversation_id=CID)
    assert not verdict.allow and verdict.why == "outside"


@pytest.mark.parametrize(
    "rel",
    [
        ".agents/hooks.json",
        ".git/hooks/pre-commit",
        ".git/config",
        "sub/.git/config",
        ".gemini/settings.json",
        ".env",
        "app/.env.local",
        "keys/server.pem",
        ".untether-outbox/x.txt",
        ".claude/settings.json",
        ".mcp.json",
        ".envrc",
        ".github/workflows/ci.yml",
        ".vscode/tasks.json",
        ".pre-commit-config.yaml",
        ".husky/pre-push",
        "CLAUDE.md",
        "AGENTS.md",
        "GEMINI.md",
    ],
)
def test_edit_protected_paths_ask(rel: str, root: Path) -> None:
    verdict = classify_edit(_edit(root / rel), root=root, conversation_id=CID)
    assert not verdict.allow and verdict.why == "protected"


def test_edit_untether_package_venv_and_plugin_dir_ask(root: Path) -> None:
    import untether

    for target in (
        Path(untether.__file__).parent / "antigravity" / "gate_hook.py",
        Path(sys.prefix) / "bin" / "x",
        gate_plugin.plugin_dir() / "hooks.json",
    ):
        verdict = classify_edit(_edit(target), root=root, conversation_id=CID)
        assert not verdict.allow and verdict.why == "self"


def test_workspace_paths_mismatch_asks(root: Path, tmp_path: Path) -> None:
    verdict = classify_edit(
        _edit(root / "a.txt"),
        root=root,
        conversation_id=CID,
        workspace_paths=[str(tmp_path)],
    )
    assert not verdict.allow and verdict.why == "mismatch"
    assert classify_edit(
        _edit(root / "a.txt"),
        root=root,
        conversation_id=CID,
        workspace_paths=[str(root)],
    ).allow


def test_diff_preview_toggle_routes_edits_to_card(root: Path) -> None:
    verdict = classify(
        "write_to_file",
        _edit(root / "a.txt"),
        root=root,
        conversation_id=CID,
        diff_preview=True,
    )
    assert not verdict.allow and verdict.why == "preview"
    # Reads still pass; the default (None / False) lets in-project edits run.
    assert classify(
        "view_file", {}, root=root, conversation_id=CID, diff_preview=True
    ).allow
    assert classify(
        "write_to_file",
        _edit(root / "a.txt"),
        root=root,
        conversation_id=CID,
        diff_preview=False,
    ).allow


# ── card previews ───────────────────────────────────────────────────────────


def test_card_preview_replace_file_content(root: Path) -> None:
    token = set_run_base_dir(root)
    try:
        text = gate_policy.preview(
            "replace_file_content",
            {
                "TargetFile": str(root / "README.md"),
                "TargetContent": "hello",
                "ReplacementContent": "hello world",
            },
            root=root,
        )
    finally:
        reset_run_base_dir(token)
    assert "📝 README.md" in text
    assert "- hello" in text and "+ hello world" in text


def test_card_preview_multi_replace_chunks(root: Path) -> None:
    chunks = [
        {"TargetContent": f"a{n}", "ReplacementContent": f"b{n}"} for n in range(4)
    ]
    text = gate_policy.preview(
        "multi_replace_file_content",
        {"TargetFile": str(root / "x.py"), "ReplacementChunks": chunks},
        root=root,
    )
    assert "- a0" in text and "+ b1" in text and "a2" not in text
    assert "…2 more chunks" in text


def test_card_preview_write_overwrite_note(root: Path) -> None:
    text = gate_policy.preview(
        "write_to_file",
        {
            "TargetFile": str(root / "README.md"),
            "CodeContent": "new\n",
            "Overwrite": True,
        },
        root=root,
    )
    assert "+ new" in text and "(overwrites an existing file)" in text
    fresh = gate_policy.preview(
        "write_to_file",
        {"TargetFile": str(root / "fresh.md"), "CodeContent": "x", "Overwrite": True},
        root=root,
    )
    assert "overwrites" not in fresh


def test_card_preview_run_command_multiline_fenced_cwd_wait_daemon(root: Path) -> None:
    (root / "sub").mkdir()
    text = gate_policy.preview(
        "run_command",
        {
            "CommandLine": "cat <<EOF\nhi\nEOF",
            "Cwd": str(root / "sub"),
            "WaitMsBeforeAsync": 10000,
            "IsDaemon": True,
        },
        root=root,
    )
    assert text.startswith("$\n```sh\ncat <<EOF")
    assert "in sub" in text
    assert "background after 10 s" in text
    assert "keeps running after this turn ends" in text
    plain = gate_policy.preview(
        "run_command", {"CommandLine": "git status", "Cwd": str(root)}, root=root
    )
    assert plain == "$ `git status`"


def test_tool_summary_escaped_and_after_args(root: Path) -> None:
    title = gate_policy.card_title(
        "run_command",
        {"CommandLine": "ls", "toolSummary": "*List* the [files](http://x)"},
        gate_policy.Verdict(False),
        root=root,
    )
    lines = title.splitlines()
    assert lines[0] == "Permission Request [agy] - shell command"
    assert lines[1] == "$ `ls`"
    assert lines[-1].startswith("agy says: ")
    assert "\\*List\\*" in lines[-1] and "\\[files\\]" in lines[-1]


def test_card_names_the_subagent_and_masks_secrets(root: Path) -> None:
    title = gate_policy.card_title(
        "call_mcp_tool",
        {"ServerName": "gh", "ToolName": "create_issue", "api_token": "s3cret"},
        gate_policy.Verdict(False),
        root=root,
        child_role="Researcher",
    )
    # Names from the call's arguments are escaped like the rest.
    assert title.startswith("Permission Request [agy] - MCP tool gh/create\\_issue")
    assert "s3cret" not in title and "•••" in title
    assert "from subagent: Researcher" in title
    subagents = gate_policy.preview(
        "invoke_subagent",
        {"Subagents": [{"Role": "Alpha", "Prompt": "read notes.txt"}]},
        root=root,
    )
    assert "Alpha: read notes.txt" in subagents
    assert "ask here too" in subagents


def test_edit_card_says_why(root: Path, tmp_path: Path) -> None:
    title = gate_policy.card_title(
        "write_to_file",
        _edit(tmp_path / "out.txt"),
        gate_policy.Verdict(False, "outside"),
        root=root,
    )
    assert title.startswith(
        "Permission Request [agy] - write a file (outside this project)"
    )


# ── "Always allow this command" ────────────────────────────────────────────


def test_always_allow_exact_command_and_cwd_only() -> None:
    args = {"CommandLine": " npm test ", "Cwd": "/p"}
    assert gate_policy.grant(1, CID, args)
    assert gate_policy.granted(1, CID, {"CommandLine": "npm test", "Cwd": "/p"})
    assert not gate_policy.granted(1, CID, {"CommandLine": "npm test", "Cwd": "/q"})
    assert not gate_policy.granted(1, CID, {"CommandLine": "npm test -x", "Cwd": "/p"})
    assert not gate_policy.granted(2, CID, args)  # another chat
    assert not gate_policy.granted(1, "other-conversation", args)
    assert not gate_policy.grant(1, CID, {"Cwd": "/p"})


def test_always_allow_cleared_on_new_conversation_and_mode_change() -> None:
    args = {"CommandLine": "make", "Cwd": "/p"}
    gate_policy.grant(1, CID, args)
    gate_policy.forget_grants(CID)
    assert not gate_policy.granted(1, CID, args)


def test_always_allow_bounded() -> None:
    for n in range(gate_policy._GRANTS_MAX + 5):
        gate_policy.grant(1, CID, {"CommandLine": f"echo {n}"})
    assert not gate_policy.granted(1, CID, {"CommandLine": "echo 0"})
    assert gate_policy.granted(
        1, CID, {"CommandLine": f"echo {gate_policy._GRANTS_MAX + 4}"}
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="case-insensitive file systems")
def test_protected_paths_case_folded_on_mac(root: Path) -> None:  # pragma: no cover
    verdict = classify_edit(
        _edit(root / ".Agents/hooks.json"), root=root, conversation_id=CID
    )
    assert not verdict.allow


def test_realpath_of_missing_tail_keeps_rest() -> None:
    assert gate_policy._real("/nonexistent-untether/a/b") == os.path.realpath(
        "/nonexistent-untether/a/b"
    )
