"""Untether's no-shell agent for Antigravity Workspace runs (#1037).

A Workspace run names the agent with a fixed ``--agent=untether-workspace``
so the model has no shell tool to open with; Full access never does. The
file is Untether's own: rewritten when it differs, never written through a
link, and the flag is dropped whenever the file can't be put in place.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import structlog.testing

from untether.runners import antigravity as agy
from untether.runners.antigravity import AntigravityRunner
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.utils import antigravity_agent as agent
from untether.utils import antigravity_scan as scan
from untether.utils.antigravity_argv import AgyArgvError, agy_flag

AGENT_FLAG = "--agent=untether-workspace"


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "_warned", set())


def _args(options: EngineRunOptions | None = None) -> list[str]:
    runner = AntigravityRunner()
    with apply_run_options(options):
        return runner.build_args("p", None, state=runner.new_state("p", None))


def test_agent_flag_allows_only_untethers_agent() -> None:
    assert agy_flag("--agent", agent.WORKSPACE_AGENT_NAME) == AGENT_FLAG
    for bad in ("research", "untether-workspace ", "", None, agy.BYPASS_FLAG, "x/y"):
        with pytest.raises(AgyArgvError):
            agy_flag("--agent", bad)


def test_agent_file_lists_file_tools_only() -> None:
    head = agent.AGENT_MD.split("---")[1]
    tools = [line.strip()[2:] for line in head.splitlines() if line.startswith("  - ")]
    assert tools == [
        "view_file",
        "list_dir",
        "find_by_name",
        "grep_search",
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
    ]
    assert "name: untether-workspace\n" in head
    # The user is told that Full access needs a new conversation.
    assert "/new" in agent.AGENT_MD and "Full access" in agent.AGENT_MD


@pytest.mark.parametrize("mode", [None, "workspace", "ask", "plan", "bogus"])
def test_workspace_runs_name_the_agent(mode: str | None) -> None:
    args = _args(EngineRunOptions(permission_mode=mode))
    assert args[-2:] == ["--mode=accept-edits", AGENT_FLAG]
    assert agy.BYPASS_FLAG not in args
    assert agent.agent_path().read_text() == agent.AGENT_MD


def test_resumed_workspace_run_names_the_agent() -> None:
    from untether.model import ResumeToken

    runner = AntigravityRunner()
    resume = ResumeToken(engine="antigravity", value="abc-123")
    args = runner.build_args("p", resume, state=runner.new_state("p", resume))
    assert args[-3:] == ["--conversation=abc-123", "--mode=accept-edits", AGENT_FLAG]


def test_full_access_never_names_the_agent() -> None:
    args = _args(EngineRunOptions(permission_mode="full"))
    assert args[-1] == agy.BYPASS_FLAG
    assert not any(a.startswith("--agent") for a in args)
    assert not agent.agent_path().exists()


def test_changed_agent_file_is_rewritten() -> None:
    path = agent.agent_path()
    path.parent.mkdir(parents=True)
    path.write_text(agent.AGENT_MD.replace("  - list_dir\n", "  - run_command\n"))
    with structlog.testing.capture_logs() as logs:
        assert agent.ensure_workspace_agent() is True
    assert path.read_text() == agent.AGENT_MD
    assert [e["event"] for e in logs] == ["antigravity.workspace_agent.installed"]
    assert [p.name for p in path.parent.iterdir()] == ["agent.md"]
    # In place already: nothing written, nothing logged.
    stamp = path.stat().st_mtime_ns
    with structlog.testing.capture_logs() as logs:
        assert agent.ensure_workspace_agent() is True
    assert logs == [] and path.stat().st_mtime_ns == stamp


@pytest.mark.parametrize("link", ["agents", "folder", "file"])
def test_never_writes_through_a_link(link: str, tmp_path: Path) -> None:
    path = agent.agent_path()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if link == "agents":
        path.parent.parent.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(elsewhere, path.parent.parent)
    elif link == "folder":
        path.parent.parent.mkdir(parents=True)
        os.symlink(elsewhere, path.parent)
    else:
        path.parent.mkdir(parents=True)
        os.symlink(elsewhere / "agent.md", path)
    with structlog.testing.capture_logs() as logs:
        args = _args()
        _args()
    assert args[-1] == "--mode=accept-edits"
    assert list(elsewhere.rglob("*")) == []
    warnings = [e for e in logs if e["log_level"] == "warning"]
    # A linked agent.md is caught one step earlier, by the user-level check.
    reason = "user_unchecked" if link == "file" else "symlink"
    assert [(e["event"], e["reason"]) for e in warnings] == [
        ("antigravity.workspace_agent.unavailable", reason)
    ]


def test_unwritable_config_dir_runs_without_the_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(scan, "user_config_dir", lambda: blocker)
    with structlog.testing.capture_logs() as logs:
        args = _args()
    assert args[-1] == "--mode=accept-edits"
    assert [e["event"] for e in logs if e["log_level"] == "warning"] == [
        "antigravity.workspace_agent.unavailable"
    ]


def test_scan_ignores_the_agent_file_but_not_a_link_beside_it(tmp_path: Path) -> None:
    """No exemption for the folder: the regular ``agent.md`` isn't a
    manifest the scan tracks, but a link planted next to it still is."""
    assert agent.ensure_workspace_agent() is True
    other = scan.user_config_dir() / "agents" / "someone-else.md"
    other.write_text("---\nname: someone-else\n---\n")
    os.symlink("/usr", agent.agent_path().parent / "extra")  # outside $HOME
    out: dict[str, scan.Fingerprint] = {}
    scan._scan_user_level(out, scan._Budget())
    assert sorted(key.split("agents/", 1)[1] for key in out) == [
        "someone-else.md",
        "untether-workspace/extra",
    ]


def test_denial_notice_points_at_new_and_keeps_the_rule_hint_for_shell() -> None:
    shell = agy.denial_paragraph(["shell command"], mode="workspace", trigger=None)
    assert "Full access, then /new, or add an allow rule" in shell
    outside = agy.denial_paragraph(
        ["file outside the project"], mode="workspace", trigger=None
    )
    assert outside.endswith("Full access, then /new.")
    assert "allow rule" not in outside
    cron = agy.denial_paragraph(
        ["file outside the project"], mode="workspace", trigger="cron:x"
    )
    assert cron.endswith('set `permission_mode = "full"` on this cron.')
    hook = agy.denial_paragraph(
        ["file outside the project"], mode="workspace", trigger="webhook:x"
    )
    assert hook.endswith("Webhook runs never get Full access; run it from the chat.")
    hook_shell = agy.denial_paragraph(
        ["shell command"], mode="workspace", trigger="webhook:x"
    )
    assert "; add an allow rule such as `command(git status)`" in hook_shell
    assert hook_shell.endswith(", or run it from the chat.")


@pytest.fixture(autouse=True)
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from untether.utils.paths import reset_run_base_dir, set_run_base_dir

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    token = set_run_base_dir(proj)
    monkeypatch.setattr(scan, "user_config_dir", lambda: tmp_path / "agy-config")
    try:
        yield proj
    finally:
        reset_run_base_dir(token)


def test_project_without_agent_sources_gets_the_agent(project: Path) -> None:
    """Rules, skills, hooks and MCP config can't define an agent."""
    for name in ("rules", "skills"):
        (project / ".agents" / name).mkdir(parents=True)
        (project / ".agents" / name / "untether-workspace.md").write_text("x")
    (project / ".agents" / "mcp_config.json").write_text("{}")
    (project / ".agents" / "notes.md").write_text("name: untether-workspace")
    assert _args()[-1] == AGENT_FLAG


@pytest.mark.parametrize(
    "planted",
    [
        ".agents/agents/untether-workspace.md",
        ".agents/agents/innocent.md",
        ".agents/Agents/x.md",
        ".agents/agents.json",
        ".agents/plugins/p/agents/a.md",
        ".agents/plugins.json",
        "sub/.agents/agents/a.md",
    ],
)
def test_project_that_can_supply_agents_drops_the_flag(
    project: Path, planted: str
) -> None:
    """Structural: what the files say is never read, so an escaped or
    oddly encoded ``name:`` can't slip a same-named agent past the check."""
    from untether.utils.paths import reset_run_base_dir, set_run_base_dir

    path = project / planted
    path.parent.mkdir(parents=True)
    path.write_text('---\nname: "untether\\x2dworkspace"\ntools: [run_command]\n---\n')
    token = set_run_base_dir(project / "sub") if planted.startswith("sub/") else None
    try:
        with structlog.testing.capture_logs() as logs:
            args = _args()
    finally:
        if token is not None:
            reset_run_base_dir(token)
    assert args[-1] == "--mode=accept-edits"
    assert [(e["event"], e["reason"]) for e in logs if e["log_level"] == "warning"] == [
        ("antigravity.workspace_agent.unavailable", "project_agents")
    ]


def test_empty_or_linked_agent_source_still_drops_the_flag(
    project: Path, tmp_path: Path
) -> None:
    (project / ".agents").mkdir()
    os.symlink(tmp_path / "nowhere", project / ".agents" / "plugins")
    assert _args()[-1] == "--mode=accept-edits"


@pytest.mark.parametrize("kind", ["linked", "file", "many"])
def test_unverifiable_project_config_drops_the_flag(
    project: Path, tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if kind == "linked":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, project / ".agents")
    elif kind == "file":
        (project / ".agents").write_text("x")
    else:
        monkeypatch.setattr(agent, "_MAX_ENTRIES", 3)
        (project / ".agents").mkdir()
        for i in range(5):
            (project / ".agents" / f"f{i}.txt").write_text("x")
    with structlog.testing.capture_logs() as logs:
        assert _args()[-1] == "--mode=accept-edits"
    assert [e["reason"] for e in logs if e["log_level"] == "warning"] == [
        "project_unchecked"
    ]


def test_no_project_never_names_the_agent() -> None:
    assert agent.project_defines_agent(None) is True


@pytest.mark.parametrize(
    ("planted", "reason"),
    [
        ("agents/untether-workspace.md", "user_agent"),
        ("agents/Untether-Workspace/agent.md", "user_agent"),
        ("agents/untether-workspace/extra.json", "user_unchecked"),
    ],
)
def test_user_level_clash_drops_the_flag(planted: str, reason: str) -> None:
    assert agent.ensure_workspace_agent() is True
    path = scan.user_config_dir() / planted
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    with structlog.testing.capture_logs() as logs:
        assert _args()[-1] == "--mode=accept-edits"
    assert [e["reason"] for e in logs if e["log_level"] == "warning"] == [reason]


def test_other_user_level_agents_are_fine() -> None:
    other = scan.user_config_dir() / "agents" / "reviewer.md"
    other.parent.mkdir(parents=True)
    other.write_text("x")
    assert _args()[-1] == AGENT_FLAG
    assert _args()[-1] == AGENT_FLAG
