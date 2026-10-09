"""Antigravity (agy) permission modes, denial rows and planted-config checks
(#558, phase 02 + 08 §10).

The safe default is the whole point: agy never gets
``--dangerously-skip-permissions`` unless a human explicitly chose Full
access, and unattended runs never inherit it. Translate-level tests replay the
real agy 1.3.x captures in ``tests/fixtures/antigravity/``; ``run()`` tests use
the fake agy (``tests/fake_clis/fake_agy.py``).
"""

from __future__ import annotations

import builtins
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog.testing

from untether.config import ConfigError
from untether.markdown import format_meta_line
from untether.model import ActionEvent, CompletedEvent, ResumeToken, StartedEvent
from untether.runner import PRESPAWN_BLOCKED_KEY
from untether.runners import antigravity as agy
from untether.runners.antigravity import AntigravityRunner, AntigravityStreamState
from untether.runners.run_options import (
    VALID_PERMISSION_MODES_BY_ENGINE,
    EngineRunOptions,
    apply_run_options,
)
from untether.schemas import antigravity as schema
from untether.utils import antigravity_quota as quota
from untether.utils import antigravity_scan as scan
from untether.utils.paths import (
    reset_run_base_dir,
    reset_run_channel_id,
    set_run_base_dir,
    set_run_channel_id,
)

FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"
FAKE_AGY = Path(__file__).parent / "fake_clis" / "fake_agy.py"
ENGINE = "antigravity"
BYPASS = "--dangerously-skip-permissions"


# ── helpers ─────────────────────────────────────────────────────────────────


def _runner(**kwargs: Any) -> AntigravityRunner:
    kwargs.setdefault("antigravity_cmd", "agy")
    return AntigravityRunner(**kwargs)


def _lines(name: str) -> list[str]:
    return [ln for ln in (FIXTURES / f"{name}.jsonl").read_text().splitlines() if ln]


def _args(options: EngineRunOptions | None = None, **kwargs: Any) -> list[str]:
    runner = _runner(**kwargs)
    with apply_run_options(options):
        state = runner.new_state("p", None)
        return runner.build_args("p", None, state=state)


def _replay(
    lines: list[str],
    *,
    options: EngineRunOptions | None = None,
    runner: AntigravityRunner | None = None,
    resume: ResumeToken | None = None,
) -> tuple[list[Any], AntigravityStreamState]:
    runner = runner or _runner()
    events: list[Any] = []
    with apply_run_options(options):
        state = runner.new_state("prompt", resume)
        runner.start_run("prompt", resume, state=state)
        for line in lines:
            events.extend(
                runner.translate(
                    schema.decode_event(line),
                    state=state,
                    resume=resume,
                    found_session=None,
                )
            )
            if events and isinstance(events[-1], CompletedEvent):
                break
    return events, state


def _completed(events: list[Any]) -> CompletedEvent:
    done = [e for e in events if isinstance(e, CompletedEvent)]
    assert len(done) == 1, events
    return done[0]


def _done_actions(events: list[Any]) -> list[ActionEvent]:
    return [e for e in events if isinstance(e, ActionEvent) and e.phase == "completed"]


def _with_init_mode(lines: list[str], mode: str) -> list[str]:
    out = []
    for line in lines:
        obj = json.loads(line)
        if obj.get("event") == "init":
            obj["init"]["permission_mode"] = mode
        out.append(json.dumps(obj))
    return out


def _with_result(lines: list[str], **changes: Any) -> list[str]:
    out = []
    for line in lines:
        obj = json.loads(line)
        if obj.get("event") == "result":
            obj["result"].update(changes)
        out.append(json.dumps(obj))
    return out


@pytest.fixture(autouse=True)
def _reset_warn_once() -> Iterator[None]:
    agy._reset_permission_warnings()
    yield
    agy._reset_permission_warnings()


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    proj = tmp_path / "proj"
    proj.mkdir()
    token = set_run_base_dir(proj)
    try:
        yield proj
    finally:
        reset_run_base_dir(token)


@pytest.fixture
def chat() -> Iterator[int]:
    token = set_run_channel_id(4242)
    try:
        yield 4242
    finally:
        reset_run_channel_id(token)


# ── the PR blocker: never bypass by default ─────────────────────────────────


def test_build_args_default_never_bypasses() -> None:
    """No run options, no TOML: agy must never get the bypass flag."""
    assert BYPASS not in _args()
    assert BYPASS not in AntigravityRunner().build_args(
        "p", None, state=AntigravityRunner().new_state("p", None)
    )


@pytest.mark.parametrize(
    "mode",
    [
        None,
        "workspace",
        "ask",
        "plan",
        "accept-edits",
        "default",
        "auto",
        "bypassPermissions",
        "yolo",
        "",
        "FULL",
        "full",
    ],
)
def test_bypass_flag_only_for_full(mode: str | None) -> None:
    args = _args(EngineRunOptions(permission_mode=mode))
    assert (BYPASS in args) is (mode == "full")
    assert "--mode" not in args  # rc1 never passes --mode (plan is refused)
    assert "--sandbox" not in args


def test_toml_full_applies_without_override() -> None:
    assert BYPASS in _args(default_permission_mode="full")


def test_chat_workspace_overrides_toml_full() -> None:
    args = _args(
        EngineRunOptions(permission_mode="workspace"), default_permission_mode="full"
    )
    assert BYPASS not in args


def test_unknown_mode_fails_closed_and_warns_once() -> None:
    with structlog.testing.capture_logs() as logs:
        for _ in range(2):
            args = _args(EngineRunOptions(permission_mode="yolo"))
            assert BYPASS not in args
    warns = [e for e in logs if e["event"] == "antigravity.permission_mode.unknown"]
    assert len(warns) == 1
    assert warns[0]["value"] == "yolo"
    assert warns[0]["log_level"] == "warning"


def test_build_runner_rejects_unknown_and_accept_edits_toml_mode(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "untether.toml"
    for bad in ("accept-edits", "yolo", "FULL", "", 3):
        with pytest.raises(ConfigError) as exc:
            agy.build_runner({"permission_mode": bad}, cfg)
        for value in ("workspace", "ask", "plan", "full"):
            assert value in str(exc.value)
    for good in ("workspace", "ask", "plan", "full"):
        runner = agy.build_runner({"permission_mode": good}, cfg)
        assert runner.default_permission_mode == good
    assert agy.build_runner({}, cfg).default_permission_mode is None


def test_valid_modes_registered_for_cron_validation() -> None:
    assert VALID_PERMISSION_MODES_BY_ENGINE["antigravity"] == frozenset(
        {"workspace", "ask", "plan", "full"}
    )


def test_toml_full_access_warns_once(tmp_path: Path) -> None:
    cfg = tmp_path / "untether.toml"
    with structlog.testing.capture_logs() as logs:
        agy.build_runner({"permission_mode": "full"}, cfg)
        agy.build_runner({"permission_mode": "full"}, cfg)
        agy.build_runner({"permission_mode": "workspace"}, cfg)
    warns = [e for e in logs if e["event"] == "antigravity.full_access_from_toml"]
    assert len(warns) == 1
    assert warns[0]["log_level"] == "warning"


# ── meta / footer label ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "label"),
    [
        (None, "workspace"),
        ("workspace", "workspace"),
        ("full", "full access"),
        ("yolo", "workspace"),
    ],
)
def test_meta_permission_label_each_mode(mode: str | None, label: str) -> None:
    events, _ = _replay(
        _lines("slash_prompt_literal"), options=EngineRunOptions(permission_mode=mode)
    )
    started = events[0]
    assert isinstance(started, StartedEvent)
    assert started.meta is not None
    assert started.meta["permissionMode"] == label
    assert format_meta_line(started.meta) == f"gemini-3.8-flash · {label}"


# ── rc1 refusal of Ask me / Plan first ─────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(("mode", "label"), [("ask", "Ask me"), ("plan", "Plan first")])
async def test_ask_and_plan_refused_before_spawn_in_rc1(
    mode: str,
    label: str,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = tmp_path / "record.json"
    monkeypatch.setenv("UNTETHER_FAKE_AGY_RECORD", str(record))
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    with apply_run_options(EngineRunOptions(permission_mode=mode)):
        events = [e async for e in runner.run("hi", None)]
    (done,) = events
    assert isinstance(done, CompletedEvent) and done.ok is False
    assert done.usage == {PRESPAWN_BLOCKED_KEY: "gate_missing"}
    assert label in (done.error or "")
    assert "Workspace or Full access" in (done.error or "")
    assert not record.exists()


# ── 08 §10: unattended runs can't inherit Full access ──────────────────────


def test_unattended_inherited_full_downgraded_to_workspace() -> None:
    chat_full = EngineRunOptions(permission_mode="full", unattended_trigger="cron:x")
    with structlog.testing.capture_logs() as logs:
        assert BYPASS not in _args(chat_full)
        assert BYPASS not in _args(chat_full)
        assert BYPASS not in _args(
            EngineRunOptions(unattended_trigger="cron:x"),
            default_permission_mode="full",
        )
    warns = [e for e in logs if e["event"] == "antigravity.unattended_full_downgraded"]
    assert [w["origin"] for w in warns] == ["chat", "toml"]
    assert all(w["trigger"] == "cron:x" for w in warns)


def test_unattended_cron_full_keeps_bypass() -> None:
    opts = EngineRunOptions(
        permission_mode="full",
        trigger_permission_mode="full",
        unattended_trigger="cron:nightly",
    )
    assert BYPASS in _args(opts)


def test_webhook_never_bypasses() -> None:
    opts = EngineRunOptions(permission_mode="full", unattended_trigger="webhook:gh")
    assert BYPASS not in _args(opts)
    assert BYPASS not in _args(
        EngineRunOptions(unattended_trigger="webhook:gh"),
        default_permission_mode="full",
    )


def test_attended_full_keeps_bypass() -> None:
    assert BYPASS in _args(EngineRunOptions(permission_mode="full"))


# ── D22.7: init.permission_mode cross-check ────────────────────────────────


def test_init_always_proceed_without_bypass_kills_before_tools() -> None:
    lines = _with_init_mode(_lines("tools_denied"), "always-proceed")
    with structlog.testing.capture_logs() as logs:
        events, state = _replay(lines)
    (done,) = events  # no Started, no tool rows
    assert isinstance(done, CompletedEvent) and done.ok is False
    assert "always-proceed" in (done.error or "")
    assert "Full access in /config" in (done.error or "")
    errors = [e for e in logs if e["event"] == "antigravity.permission_mode.mismatch"]
    assert len(errors) == 1 and errors[0]["log_level"] == "error"
    assert state.permission_refused is True


def test_init_always_proceed_with_bypass_runs() -> None:
    events, _ = _replay(
        _lines("hook_deny_bypass"), options=EngineRunOptions(permission_mode="full")
    )
    assert isinstance(events[0], StartedEvent)
    assert _completed(events).ok is True


def test_init_request_review_with_bypass_warns() -> None:
    with structlog.testing.capture_logs() as logs:
        for _ in range(2):
            events, _ = _replay(
                _lines("ok"), options=EngineRunOptions(permission_mode="full")
            )
            assert _completed(events).ok is True
    warns = [
        e for e in logs if e["event"] == "antigravity.permission_mode.bypass_unreported"
    ]
    assert len(warns) == 1


# ── denial surfacing ───────────────────────────────────────────────────────


def test_soft_denied_done_row_marked_blocked_ok_true() -> None:
    events, _ = _replay(_lines("tools_denied"))
    rows = [e for e in _done_actions(events) if "probe-shell" in e.action.title]
    assert len(rows) == 1
    row = rows[0]
    assert row.ok is True
    assert row.action.title.startswith("⚠️ Blocked: shell command (RunCommand)")
    assert row.action.kind == "warning"
    assert row.action.detail["denied"] is True
    assert row.action.detail["tool_name"] == "run_command"
    view = [e for e in _done_actions(events) if "notes.txt" in e.action.title]
    assert view and view[0].ok is True and view[0].action.kind != "warning"


def test_unmatched_parked_step_completes_ok() -> None:
    # The run_command is parked, but agy denied only an MCP call.
    lines = _with_result(
        _lines("tools_denied"),
        denied_actions=[{"action": "mcp", "display_name": "CallMcpTool"}],
    )
    events, _ = _replay(lines)
    cmd = [e for e in _done_actions(events) if "probe-shell" in e.action.title]
    assert len(cmd) == 1 and cmd[0].ok is True
    assert not cmd[0].action.title.startswith("⚠️")
    warn = [e for e in _done_actions(events) if e.action.id == "antigravity.denied.mcp"]
    assert len(warn) == 1 and warn[0].ok is True
    assert warn[0].action.title.startswith("⚠️ Blocked: MCP tool (CallMcpTool)")


def test_denied_action_without_step_gets_warning_row() -> None:
    lines = _with_result(
        _lines("ok"),
        denied_actions=[{"action": "read_url", "display_name": "ReadUrlContent"}],
    )
    events, _ = _replay(lines)
    (warn,) = [
        e for e in _done_actions(events) if e.action.id == "antigravity.denied.read_url"
    ]
    assert warn.ok is True and warn.action.kind == "warning"
    assert warn.action.title.startswith("⚠️ Blocked: web fetch (ReadUrlContent)")


def test_denied_actions_empty_response_becomes_explanation() -> None:
    with structlog.testing.capture_logs() as logs:
        events, _ = _replay(_lines("tools_denied"))
    done = _completed(events)
    assert done.ok is True
    assert done.answer.startswith(
        "⚠️ Antigravity was blocked from using a shell command"
    )
    assert "/config → Permission mode → Full access" in done.answer
    assert "command(git status)" in done.answer
    (log,) = [e for e in logs if e["event"] == "antigravity.denied_actions"]
    assert log["actions"] == ["command"]
    assert log["display_names"] == ["RunCommand"]
    assert log["permission_mode"] == "workspace"
    assert log["trigger"] is None


def test_denied_actions_nonempty_response_gets_notice_appended() -> None:
    lines = _with_result(_lines("tools_denied"), response="I tried.")
    events, _ = _replay(lines)
    answer = _completed(events).answer
    assert answer.startswith("I tried.\n\n⚠️ Antigravity was blocked")


@pytest.mark.parametrize(
    ("options", "expect", "absent"),
    [
        (EngineRunOptions(), "/config → Permission mode → Full access", "on this cron"),
        (
            EngineRunOptions(unattended_trigger="cron:x"),
            'permission_mode = "full"` on this cron',
            "/config",
        ),
        (
            EngineRunOptions(unattended_trigger="webhook:y"),
            "Webhook runs never get Full access",
            "/config",
        ),
        (
            EngineRunOptions(permission_mode="full"),
            "agy's own deny rules (`permissions.deny`",
            "Full access",
        ),
    ],
)
def test_denial_text_trigger_aware(
    options: EngineRunOptions, expect: str, absent: str
) -> None:
    events, _ = _replay(_lines("tools_denied"), options=options)
    answer = _completed(events).answer
    assert expect in answer
    assert absent not in answer


def test_hook_denied_tool_error_message_surfaces() -> None:
    events, _ = _replay(
        _lines("hook_deny_bypass"), options=EngineRunOptions(permission_mode="full")
    )
    failed = [e for e in _done_actions(events) if e.ok is False]
    assert len(failed) == 1
    assert (failed[0].message or "").startswith("tool call denied by pre-tool hook")
    assert len(failed[0].message or "") <= 300


# ── planted workspace config (B2 widened, REVIEW-2 B2/M4) ──────────────────


@pytest.fixture
def gemini_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "gemini-config"
    home.mkdir()
    monkeypatch.setattr(scan, "user_config_dir", lambda: home)
    return home


def _write(path: Path, text: str = "{}") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_workspace_config_ancestor_scan_stops_at_git_root(
    tmp_path: Path, gemini_home: Path
) -> None:
    outer = tmp_path / "outer"
    repo = outer / "repo"
    sub = repo / "pkg" / "sub"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()
    _write(outer / ".agents" / "hooks.json")  # above the git root: ignored
    _write(repo / ".agents" / "hooks.json")
    _write(sub / ".agents" / "mcp_config.json")
    _write(repo / "pkg" / ".agents" / "agents" / "helper.md", "# agent")
    _write(repo / ".agents" / "plugins" / "p" / "plugin.json")
    _write(repo / ".agents" / "skills.json")
    _write(repo / ".agents" / "notes.txt")  # every file under .agents counts
    result = scan.scan_workspace_config(sub)
    assert result.root == repo
    assert sorted(result.agy) == [
        ".agents/hooks.json",
        ".agents/notes.txt",
        ".agents/plugins/p/plugin.json",
        ".agents/skills.json",
        "pkg/.agents/agents/helper.md",
        "pkg/sub/.agents/mcp_config.json",
    ]


def test_workspace_config_without_git_scans_ancestor_agents(
    tmp_path: Path, gemini_home: Path
) -> None:
    """No .git bounds agy's walk-up, so ancestor .agents/ count too (3rd
    security review): a parent-dir hook must not run unseen."""
    proj = tmp_path / "a" / "proj"
    proj.mkdir(parents=True)
    _write(tmp_path / "a" / ".agents" / "hooks.json")
    result = scan.scan_workspace_config(proj)
    assert result.root == proj
    assert "../.agents/hooks.json" in result.agy
    assert result.unchecked_reason is None


def test_empty_manifest_files_ignored(tmp_path: Path, gemini_home: Path) -> None:
    _write(gemini_home / "mcp_config.json", "")  # agy's migration leaves this
    _write(tmp_path / ".agents" / "hooks.json", "")
    result = scan.scan_workspace_config(tmp_path)
    assert result.agy == {}


def test_user_level_manifests_stat_only(
    tmp_path: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(gemini_home / "hooks.json", '{"x": 1}')
    _write(gemini_home / "plugins" / "foo" / "hooks.json", '{"x": 1}')
    _write(gemini_home / "plugins" / "untether-gate" / "hooks.json", '{"x": 1}')
    _write(tmp_path / ".agents" / "hooks.json", '{"x": 1}')
    opened: list[str] = []
    real_open = builtins.open
    real_os_open = os.open

    def spy_open(file: Any, *a: Any, **k: Any) -> Any:
        opened.append(str(file))
        return real_open(file, *a, **k)

    def spy_os_open(file: Any, *a: Any, **k: Any) -> Any:
        opened.append(str(file))
        return real_os_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    result = scan.scan_workspace_config(tmp_path)
    monkeypatch.setattr(builtins, "open", real_open)
    monkeypatch.setattr(os, "open", real_os_open)
    assert "~/.gemini/config/hooks.json" in result.agy
    assert "~/.gemini/config/plugins/foo/hooks.json" in result.agy
    assert not any("untether-gate" in p for p in result.agy)
    assert not any(str(gemini_home) in p for p in opened)
    assert any(".agents" in p for p in opened)  # project files are hashed


def test_digest_changes_with_content(tmp_path: Path, gemini_home: Path) -> None:
    hooks = _write(tmp_path / ".agents" / "hooks.json", '{"a": 1}')
    first = scan.scan_workspace_config(tmp_path).agy_digest
    hooks.write_text('{"a": 2}')  # same size, different content
    second = scan.scan_workspace_config(tmp_path).agy_digest
    assert first != second
    assert scan.scan_workspace_config(tmp_path).agy_digest == second


def test_cross_engine_set_scanned_at_root(tmp_path: Path, gemini_home: Path) -> None:
    _write(tmp_path / ".envrc", "export X=1")
    _write(tmp_path / ".claude" / "settings.json")
    _write(tmp_path / ".github" / "workflows" / "x.yml", "on: push")
    _write(tmp_path / "CLAUDE.md", "# hi")
    _write(tmp_path / "README.md", "# not scanned")
    result = scan.scan_workspace_config(tmp_path)
    assert sorted(result.cross) == [
        ".claude/settings.json",
        ".envrc",
        ".github/workflows/x.yml",
        "CLAUDE.md",
    ]
    assert result.agy == {}


def _hold_script(tmp_path: Path, *, write: str | None = None) -> Path:
    """ok.script, optionally writing a file in the cwd before the result."""
    lines = (FIXTURES / "ok.script").read_text().splitlines()
    out: list[str] = []
    for line in lines:
        if write is not None and '"event":"result"' in line:
            out.append(f"write:{write}")
        out.append(line)
    script = tmp_path / "scenario.script"
    script.write_text("\n".join(out) + "\n")
    return script


async def _run(
    runner: AntigravityRunner, options: EngineRunOptions | None = None
) -> list[Any]:
    with apply_run_options(options):
        return [e async for e in runner.run("Reply with exactly: OK", None)]


def _warning_rows(events: list[Any]) -> list[ActionEvent]:
    return [e for e in _done_actions(events) if e.action.kind == "warning"]


@pytest.mark.anyio
async def test_workspace_config_change_warns(
    project: Path, tmp_path: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "UNTETHER_FAKE_AGY_SCRIPT",
        str(_hold_script(tmp_path, write=".agents/hooks.json")),
    )
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    with structlog.testing.capture_logs() as logs:
        events = await _run(runner)
    rows = _warning_rows(events)
    assert any(
        r.action.title.startswith("⚠️ Antigravity changed .agents/hooks.json")
        for r in rows
    )
    (log,) = [e for e in logs if e["event"] == "antigravity.workspace_config_changed"]
    assert log["set"] == "agy" and log["paths"] == [".agents/hooks.json"]
    assert log["log_level"] == "warning"


@pytest.mark.anyio
async def test_cross_engine_file_change_warns_but_never_refuses(
    project: Path, tmp_path: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    for rel in (".envrc", ".claude/settings.json", ".github/workflows/x.yml"):
        monkeypatch.setenv(
            "UNTETHER_FAKE_AGY_SCRIPT", str(_hold_script(tmp_path, write=rel))
        )
        events = await _run(runner)
        assert any(rel in r.action.title for r in _warning_rows(events)), rel
        # A cron afterwards is not refused: agy doesn't run these files.
        events = await _run(runner, EngineRunOptions(unattended_trigger="cron:x"))
        assert _completed(events).ok is True


@pytest.mark.anyio
async def test_first_sight_warning_row_once_per_digest(
    project: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    for manifest in (
        project / ".agents" / "hooks.json",  # a Stop-only hook still runs code
        project / ".agents" / "mcp_config.json",
    ):
        _write(manifest, '{"x": 1}')
        with structlog.testing.capture_logs() as logs:
            events = await _run(runner)
        rows = [r for r in _warning_rows(events) if "doesn't manage" in r.action.title]
        assert len(rows) == 1, manifest
        assert manifest.name in rows[0].action.title
        assert "including Workspace" in rows[0].action.title
        (log,) = [
            e for e in logs if e["event"] == "antigravity.workspace_config_present"
        ]
        assert log["log_level"] == "warning" and len(log["digest"]) == 12
        # Same digest → no second row.
        events = await _run(runner)
        assert not [
            r for r in _warning_rows(events) if "doesn't manage" in r.action.title
        ]


@pytest.mark.anyio
async def test_workspace_config_present_logged_once(
    project: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    _write(project / ".agents" / "hooks.json", '{"x": 1}')
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    with structlog.testing.capture_logs() as logs:
        await _run(runner)
        await _run(runner)
    present = [e for e in logs if e["event"] == "antigravity.workspace_config_present"]
    assert len(present) == 1


@pytest.mark.anyio
async def test_seen_digest_persisted_beside_chat_prefs(
    project: Path, tmp_path: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    _write(project / ".agents" / "hooks.json", '{"x": 1}')
    cfg = tmp_path / "untether.toml"
    runner = agy.build_runner({"cmd": str(FAKE_AGY)}, cfg)
    await _run(runner)
    state_file = cfg.with_name("antigravity_seen_config.json")
    assert state_file.exists()
    # A fresh runner (restart) reads it: no second row, cron allowed.
    runner = agy.build_runner({"cmd": str(FAKE_AGY)}, cfg)
    events = await _run(runner, EngineRunOptions(unattended_trigger="cron:x"))
    assert _completed(events).ok is True
    assert not _warning_rows(events)


@pytest.mark.anyio
async def test_unattended_run_refused_on_digest_change(
    project: Path, tmp_path: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "record.json"
    monkeypatch.setenv("UNTETHER_FAKE_AGY_RECORD", str(record))
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    _write(project / ".agents" / "hooks.json", '{"x": 1}')
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    with structlog.testing.capture_logs() as logs:
        events = await _run(runner, EngineRunOptions(unattended_trigger="cron:x"))
    (done,) = events
    assert isinstance(done, CompletedEvent) and done.ok is False
    assert done.usage == {PRESPAWN_BLOCKED_KEY: "config_changed"}
    assert ".agents/hooks.json" in (done.error or "")
    assert "Send any message in the chat" in (done.error or "")
    assert not record.exists()
    assert any(
        e["event"] == "antigravity.workspace_config.unattended_refused" for e in logs
    )
    # An attended run reviews it; the cron then runs.
    events = await _run(runner)
    assert _completed(events).ok is True
    events = await _run(runner, EngineRunOptions(unattended_trigger="cron:x"))
    assert _completed(events).ok is True
    # A further change → refused again.
    _write(project / ".agents" / "hooks.json", '{"x": 22}')
    events = await _run(runner, EngineRunOptions(unattended_trigger="webhook:y"))
    assert events[-1].usage == {PRESPAWN_BLOCKED_KEY: "config_changed"}


@pytest.mark.anyio
async def test_unattended_run_allowed_when_digest_seen_or_set_empty(
    project: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    events = await _run(runner, EngineRunOptions(unattended_trigger="cron:x"))
    assert _completed(events).ok is True  # empty set never refuses


# ── REVIEW-2 M5: the cached `-p /config` check ──────────────────────────────


def _config(**overrides: Any) -> quota.AgyConfig:
    data: dict[str, Any] = {
        "toolPermission": "request-review",
        "allowNonWorkspaceAccess": False,
        "permissions": None,
        "modelProvider": "",
    }
    data.update(overrides)
    return quota.parse_agy_config(data)


@pytest.fixture
def agy_config(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Point the runner's config check at a settable value (None = failed)."""
    box: dict[str, Any] = {"value": _config()}

    async def fake(runner: Any) -> quota.AgyConfig | None:
        return box["value"]

    monkeypatch.setattr(quota, "agy_config", fake)
    return box


@pytest.mark.anyio
async def test_config_always_proceed_refuses_workspace_before_spawn(
    project: Path,
    tmp_path: Path,
    gemini_home: Path,
    agy_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = tmp_path / "record.json"
    monkeypatch.setenv("UNTETHER_FAKE_AGY_RECORD", str(record))
    agy_config["value"] = _config(toolPermission="always-proceed")
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    (done,) = await _run(runner)
    assert isinstance(done, CompletedEvent) and done.ok is False
    assert done.usage == {PRESPAWN_BLOCKED_KEY: "agy_always_proceed"}
    assert "always-proceed" in (done.error or "")
    assert not record.exists()
    # Full access is unaffected.
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    events = await _run(runner, EngineRunOptions(permission_mode="full"))
    assert _completed(events).ok is True


@pytest.mark.anyio
async def test_config_allow_rules_and_non_workspace_access_warn_once_per_digest(
    project: Path,
    gemini_home: Path,
    agy_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    agy_config["value"] = _config(
        allowNonWorkspaceAccess=True,
        permissions={"allow": ["command(git status)", "command(ls)"]},
    )
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    with structlog.testing.capture_logs() as logs:
        events = await _run(runner)
    titles = [r.action.title for r in _warning_rows(events)]
    assert any("2 command pattern(s)" in t for t in titles)
    assert any("files outside the project" in t for t in titles)
    assert events[0].meta["permissionMode"] == (
        "workspace (agy allows files outside the project)"
    )
    widened = [e for e in logs if e["event"] == "antigravity.config.widened"]
    assert {e["key"] for e in widened} == {
        "allowNonWorkspaceAccess",
        "permissions.allow",
    }
    assert all("command(git status)" not in json.dumps(e) for e in logs)
    events = await _run(runner)  # same digest → no rows again
    assert not _warning_rows(events)
    agy_config["value"] = _config(permissions={"allow": ["command(ls)"]})
    events = await _run(runner)
    assert any("1 command pattern(s)" in r.action.title for r in _warning_rows(events))


@pytest.mark.anyio
async def test_config_check_failure_fails_open(
    project: Path,
    gemini_home: Path,
    agy_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    agy_config["value"] = None
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    events = await _run(runner)
    assert _completed(events).ok is True


@pytest.mark.anyio
async def test_agy_config_logs_failure_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(runner: Any) -> quota.AgyConfig:
        raise quota.AgySlashError("timeout")

    monkeypatch.setattr(quota, "_probe_agy_config", boom)
    quota.clear_config_cache()
    with structlog.testing.capture_logs() as logs:
        assert await quota.agy_config(_runner()) is None
    (log,) = [e for e in logs if e["event"] == "antigravity.config_check.failed"]
    assert log["kind"] == "timeout"


@pytest.mark.anyio
async def test_config_check_failure_cached_briefly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host where ``-p /config`` keeps failing (e.g. a timeout) must not pay
    the probe on every run; a RAM-guard block is not remembered."""
    calls: list[str] = []
    kind = {"k": "timeout"}

    async def boom(runner: Any) -> quota.AgyConfig:
        calls.append(kind["k"])
        raise quota.AgySlashError(kind["k"])

    monkeypatch.setattr(quota, "_probe_agy_config", boom)
    quota.clear_config_cache()
    runner = _runner()
    assert await quota.agy_config(runner) is None
    assert await quota.agy_config(runner) is None
    assert calls == ["timeout"]
    quota.clear_config_cache()
    kind["k"] = "prespawn_blocked"
    await quota.agy_config(runner)
    await quota.agy_config(runner)
    assert calls == ["timeout", "prespawn_blocked", "prespawn_blocked"]
    clock = {"t": 1000.0}
    monkeypatch.setattr(quota.time, "monotonic", lambda: clock["t"])
    quota.clear_config_cache()
    kind["k"] = "timeout"
    await quota.agy_config(runner)
    clock["t"] += quota.CONFIG_FAILURE_TTL_S + 1
    await quota.agy_config(runner)
    assert calls[-2:] == ["timeout", "timeout"]


@pytest.mark.anyio
async def test_config_cache_key_version_and_settings_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    monkeypatch.setattr(quota, "agy_settings_path", lambda: settings)
    version = {"v": "1.3.2"}
    monkeypatch.setattr(agy, "cached_agy_version", lambda cmd: version["v"])
    calls: list[int] = []

    async def probe(runner: Any) -> quota.AgyConfig:
        calls.append(1)
        return _config()

    monkeypatch.setattr(quota, "_probe_agy_config", probe)
    quota.clear_config_cache()
    runner = _runner()
    await quota.agy_config(runner)
    await quota.agy_config(runner)
    assert len(calls) == 1
    version["v"] = "1.3.3"
    await quota.agy_config(runner)
    assert len(calls) == 2
    settings.write_text('{"toolPermission": "strict"}')
    await quota.agy_config(runner)
    assert len(calls) == 3
    await quota.agy_config(runner)
    assert len(calls) == 3


def test_parse_agy_config_from_capture() -> None:
    capture = (
        Path(__file__).parents[1]
        / "docs/plans/v0.36.1-antigravity/probes/1.3.x/z-config.jsonl"
    )
    if capture.exists():  # docs/plans is gitignored (lba-1 checkout only)
        line = json.loads(capture.read_text().splitlines()[0])
        cfg = quota.parse_agy_config(line["command"]["data"]["config"])
        assert cfg.tool_permission == "request-review"
    cfg = _config()
    assert cfg.allow_non_workspace_access is False
    assert cfg.allow_rules_count == 0
    assert cfg.model_provider == ""


@pytest.mark.anyio
async def test_run_agy_slash_temp_cwd_devnull_and_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "record.json"
    monkeypatch.setenv("UNTETHER_FAKE_AGY_RECORD", str(record))
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    data = await quota.run_agy_slash(runner, "/config")
    assert data["config"]["toolPermission"] == "request-review"
    rec = json.loads(record.read_text())
    assert rec["argv"][:4] == ["-p", "/config", "--output-format", "stream-json"]
    assert "--conversation" not in rec["argv"]
    assert rec["stdin"] == ""
    assert Path(rec["cwd"]).name.startswith("untether-agy-")
    assert not Path(rec["cwd"]).exists()  # temp dir removed


@pytest.mark.anyio
async def test_run_agy_slash_not_signed_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SLASH_UNAUTH", "1")
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    with pytest.raises(quota.AntigravityNotSignedIn):
        await quota.run_agy_slash(runner, "/config")


@pytest.mark.anyio
async def test_run_agy_slash_blocked_by_prespawn_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from untether.runner import JsonlSubprocessRunner

    record = tmp_path / "record.json"
    monkeypatch.setenv("UNTETHER_FAKE_AGY_RECORD", str(record))
    sentinel = CompletedEvent(engine=ENGINE, ok=False, answer="", resume=None)
    monkeypatch.setattr(
        JsonlSubprocessRunner, "_check_prespawn_ram_guard", lambda self, r: sentinel
    )
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    with pytest.raises(quota.AgySlashError) as exc:
        await quota.run_agy_slash(runner, "/config")
    assert exc.value.kind == "prespawn_blocked"
    assert not record.exists()


# ── REVIEW-2 M14: one-time OAuth notice per chat ───────────────────────────


@pytest.mark.anyio
async def test_oauth_notice_once_per_chat(
    project: Path,
    tmp_path: Path,
    gemini_home: Path,
    chat: int,
    agy_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    cfg = tmp_path / "untether.toml"
    runner = agy.build_runner({"cmd": str(FAKE_AGY)}, cfg)
    with structlog.testing.capture_logs() as logs:
        first = _completed(await _run(runner)).answer
        second = _completed(await _run(runner)).answer
    assert first.startswith("OK\n\n⚠️ This host signs Antigravity in with a Google")
    assert "(Shown once in this chat.)" in first
    assert second == "OK\n"
    shown = [e for e in logs if e["event"] == "antigravity.tos_notice.shown"]
    assert len(shown) == 1 and shown[0]["chat_id"] == chat
    assert cfg.with_name("antigravity_notices.json").exists()
    # A restart remembers the chat.
    runner = agy.build_runner({"cmd": str(FAKE_AGY)}, cfg)
    assert _completed(await _run(runner)).answer == "OK\n"
    # Another chat gets it once.
    token = set_run_channel_id(9999)
    try:
        assert "Google" in _completed(await _run(runner)).answer
    finally:
        reset_run_channel_id(token)


@pytest.mark.anyio
@pytest.mark.parametrize("route", ["api_key", "adc"])
async def test_api_key_route_never_shows_notice(
    route: str,
    project: Path,
    gemini_home: Path,
    chat: int,
    agy_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    if route == "api_key":
        agy_config["value"] = _config(modelProvider="gemini")
    else:
        monkeypatch.setenv("AGY_ADC_AUTH", "true")
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    assert _completed(await _run(runner)).answer == "OK\n"


@pytest.mark.anyio
async def test_notice_shown_when_config_check_failed(
    project: Path,
    gemini_home: Path,
    chat: int,
    agy_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    agy_config["value"] = None
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    assert "Google" in _completed(await _run(runner)).answer


@pytest.mark.anyio
async def test_no_notice_without_a_chat(
    project: Path,
    gemini_home: Path,
    agy_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    assert _completed(await _run(runner)).answer == "OK\n"


# ── Claude ignores the new run-options field ───────────────────────────────


def test_claude_options_unchanged_by_new_field() -> None:
    from untether.runners.claude import ClaudeRunner

    runner = ClaudeRunner(claude_cmd="claude")
    base = EngineRunOptions(permission_mode="plan")
    with_field = EngineRunOptions(
        permission_mode="plan", trigger_permission_mode="plan"
    )
    with apply_run_options(base):
        a = runner._build_args("hi", None)
    with apply_run_options(with_field):
        b = runner._build_args("hi", None)
    assert a == b


def test_env_has_no_test_leak() -> None:
    # Guard for the fixtures above: the ADC route is opt-in per test.
    assert os.environ.get("AGY_ADC_AUTH") is None


# ── security review of 2b4d66c: fail closed ─────────────────────────────────


def test_large_file_hashed_in_full(tmp_path: Path, gemini_home: Path) -> None:
    """No per-file cap: a same-size edit at the end of a 5 MiB file, with the
    mtime set back, still changes the digest (no ctime reliance)."""
    size = 5 * 1024 * 1024
    blob = _write(tmp_path / ".agents" / "plugins" / "p" / "bundle.js", "x" * size)
    first = scan.scan_workspace_config(tmp_path)
    assert first.unchecked_reason is None
    before = blob.stat()
    blob.write_text("x" * (size - 1) + "y")
    os.utime(blob, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert scan.scan_workspace_config(tmp_path).agy_digest != first.agy_digest


def test_symlinked_plugin_dir_inside_root_is_scanned(
    tmp_path: Path, gemini_home: Path
) -> None:
    proj = tmp_path / "proj"
    vendored = proj / "vendor" / "plugin"
    manifest = _write(vendored / "hooks.json", '{"a": 1}')
    (proj / ".agents" / "plugins").mkdir(parents=True)
    (proj / ".agents" / "plugins" / "p").symlink_to(vendored, target_is_directory=True)
    (vendored / "loop").symlink_to(proj / ".agents", target_is_directory=True)
    first = scan.scan_workspace_config(proj)  # the cycle terminates
    assert ".agents/plugins/p/hooks.json" in first.agy
    assert not first.truncated and not first.outside_root
    manifest.write_text('{"a": 2}')
    assert scan.scan_workspace_config(proj).agy_digest != first.agy_digest


@pytest.mark.parametrize("target", ["outside", "/"])
def test_symlink_out_of_root_and_home_never_followed(
    target: str, tmp_path: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    proj = tmp_path / "proj"
    (proj / ".agents" / "plugins").mkdir(parents=True)
    dest = {"outside": tmp_path / "outside-plugin", "/": Path("/")}[target]
    if target == "outside":
        _write(dest / "hooks.json", '{"a": 1}')
    (proj / ".agents" / "plugins" / "evil").symlink_to(dest, target_is_directory=True)
    result = scan.scan_workspace_config(proj)
    assert result.outside_root is True  # → "not checked" in the runner
    assert list(result.agy) == [".agents/plugins/evil"]


def test_shared_agents_link_under_home_followed_and_hashed(
    tmp_path: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review (control regression): a dotfiles-style shared .agents/
    under $HOME is a normal setup — hashed, not permanently "not checked"."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    shared = home / "dotfiles" / "agents"
    manifest = _write(shared / "hooks.json", '{"a": 1}')
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".agents").symlink_to(shared, target_is_directory=True)
    first = scan.scan_workspace_config(proj)
    assert first.unchecked_reason is None
    assert ".agents/hooks.json" in first.agy
    manifest.write_text('{"a": 2}')
    assert scan.scan_workspace_config(proj).agy_digest != first.agy_digest
    # … but never into ~/.gemini (D33: no reads there).
    (proj / ".agents").unlink()
    gem = _write(home / ".gemini" / "x" / "hooks.json", '{"a": 1}')
    (proj / ".agents").symlink_to(gem.parent, target_is_directory=True)
    assert scan.scan_workspace_config(proj).unchecked_reason == scan.OUTSIDE


def test_fifo_and_device_never_opened(tmp_path: Path, gemini_home: Path) -> None:
    os.mkfifo(tmp_path / "pipe.sh")
    (tmp_path / ".agents").mkdir()
    os.mkfifo(tmp_path / ".agents" / "hooks.json")
    _write(
        tmp_path / ".agents" / "mcp_config.json",
        json.dumps({"s": {"command": "cat /dev/zero ./pipe.sh ../../etc/passwd"}}),
    )
    result = scan.scan_workspace_config(tmp_path)  # would hang if opened
    assert result.agy[".agents/hooks.json"][1].startswith("special:")
    assert result.unchecked_reason == scan.SPECIAL  # never just recorded
    assert "pipe.sh" not in result.agy  # not a regular file
    assert not any("dev/zero" in p or "passwd" in p for p in result.agy)


def test_scan_byte_budget_marks_unchecked(
    tmp_path: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scan, "MAX_HASH_BYTES", 100)
    for i in range(3):
        _write(tmp_path / ".agents" / f"blob{i}.bin", "z" * 80)
    assert scan.scan_workspace_config(tmp_path).truncated is True


def test_scan_time_budget_marks_unchecked(
    tmp_path: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scan, "TIME_BUDGET_S", -1.0)
    _write(tmp_path / ".agents" / "hooks.json", '{"a": 1}')
    assert scan.scan_workspace_config(tmp_path).truncated is True


def test_scan_entry_budget_counts_directories(
    tmp_path: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scan, "MAX_FILES", 5)
    deep = tmp_path / ".agents"
    for i in range(10):  # empty dirs only, no files
        deep = deep / f"d{i}"
    deep.mkdir(parents=True)
    assert scan.scan_workspace_config(tmp_path).truncated is True


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode-000 files")
def test_unreadable_manifest_still_counted(tmp_path: Path, gemini_home: Path) -> None:
    manifest = _write(tmp_path / ".agents" / "hooks.json", '{"a": 1}')
    manifest.chmod(0)
    try:
        result = scan.scan_workspace_config(tmp_path)
    finally:
        manifest.chmod(0o644)
    assert ".agents/hooks.json" in result.agy
    assert result.unchecked_reason == scan.UNREADABLE  # a stat stamp isn't enough


@pytest.mark.skipif(os.geteuid() == 0, reason="root lists mode-100 dirs")
def test_unlistable_dir_marks_unchecked(tmp_path: Path, gemini_home: Path) -> None:
    """A --x folder can't be listed, but agy can still open known names in
    it: never skip it quietly."""
    hidden = tmp_path / ".agents" / "plugins" / "p"
    _write(hidden / "hooks.json", '{"a": 1}')
    hidden.chmod(0o100)
    try:
        result = scan.scan_workspace_config(tmp_path)
    finally:
        hidden.chmod(0o755)
    assert result.unchecked_reason == scan.UNREADABLE


def test_user_controlled_script_outside_project_is_hashed(
    tmp_path: Path, gemini_home: Path
) -> None:
    """A hook naming a script outside the project (probe hooks use absolute
    scratch paths) is tracked by content, not a permanent "not checked";
    a read-only system binary is ignored."""
    proj = tmp_path / "proj"
    outside = _write(tmp_path / "elsewhere" / "run.sh", "echo hi")
    _write(
        proj / ".agents" / "hooks.json",
        json.dumps({"Stop": [{"command": f"python3 -I {outside} && /bin/sh -c true"}]}),
    )
    first = scan.scan_workspace_config(proj)
    assert first.unchecked_reason is None
    assert str(outside) in first.agy
    assert not any(p.endswith("/bin/sh") for p in first.agy)
    outside.write_text("echo evil")
    assert scan.scan_workspace_config(proj).agy_digest != first.agy_digest


def test_relative_mcp_arg_resolved_against_run_cwd(
    tmp_path: Path, gemini_home: Path
) -> None:
    repo = tmp_path / "repo"
    sub = repo / "sub"
    (repo / ".git").mkdir(parents=True)
    _write(
        repo / ".agents" / "mcp_config.json",
        json.dumps({"mcpServers": {"s": {"command": "node", "args": ["./server.js"]}}}),
    )
    server = _write(sub / "server.js", "1")
    first = scan.scan_workspace_config(sub)
    assert "sub/server.js" in first.agy
    server.write_text("2")
    assert scan.scan_workspace_config(sub).agy_digest != first.agy_digest


@pytest.mark.parametrize(
    "raw",
    [
        '{"Stop": [{"command": "sh scripts\\/evil.sh"}]}',  # JSON escape
        '{"Stop": [{"command": "sh \\u0073cripts/evil.sh"}]}',  # unicode escape
        '{"Stop": [{"command": "sh scr\'\'ipts/evil.sh"}]}',  # shell concat
        '{"Stop": [{"command": "sh \\"scripts/evil.sh\\""}]}',  # quoted
    ],
)
def test_reference_parsed_like_agy_and_the_shell(
    raw: str, tmp_path: Path, gemini_home: Path
) -> None:
    _write(tmp_path / ".agents" / "hooks.json", raw)
    script = _write(tmp_path / "scripts" / "evil.sh", "echo 1")
    first = scan.scan_workspace_config(tmp_path)
    assert first.unchecked_reason is None
    assert "scripts/evil.sh" in first.agy
    script.write_text("echo 2")
    assert scan.scan_workspace_config(tmp_path).agy_digest != first.agy_digest


@pytest.mark.parametrize(
    "raw",
    [
        '{"Stop": [{"command": "sh a.sh"}], // comment\n}',
        '{"Stop": [{"command": "sh a.sh"},],}',
        '\ufeff{"Stop": [{"command": "sh a.sh"}]}',
        '{"Stop": [{"command": "sh a.sh"}], "Stop": []}',
        '{"timeout": NaN}',
    ],
    ids=["comment", "trailing-comma", "bom", "duplicate-key", "nan"],
)
def test_non_strict_json_manifest_is_unchecked(
    raw: str, tmp_path: Path, gemini_home: Path
) -> None:
    _write(tmp_path / ".agents" / "hooks.json", raw)
    assert scan.scan_workspace_config(tmp_path).unchecked_reason == scan.AMBIGUOUS


@pytest.mark.parametrize("name", ["hooks.yaml", "mcp_config.jsonc", "plugin.toml"])
def test_alternate_manifest_format_is_unchecked(
    name: str, tmp_path: Path, gemini_home: Path
) -> None:
    _write(tmp_path / ".agents" / name, "Stop: sh a.sh")
    assert scan.scan_workspace_config(tmp_path).unchecked_reason == scan.AMBIGUOUS


def test_manifest_names_compared_case_insensitively(
    tmp_path: Path, gemini_home: Path
) -> None:
    """On a case-insensitive filesystem (macOS) agy opens HOOKS.JSON as
    hooks.json: its references must still be followed."""
    _write(tmp_path / ".agents" / "HOOKS.JSON", '{"Stop": [{"command": "sh x/a.sh"}]}')
    _write(tmp_path / "x" / "a.sh", "echo 1")
    assert "x/a.sh" in scan.scan_workspace_config(tmp_path).agy


def test_glob_or_substitution_path_is_unchecked(
    tmp_path: Path, gemini_home: Path
) -> None:
    _write(
        tmp_path / ".agents" / "hooks.json",
        json.dumps({"description": "saves $5", "Stop": [{"command": "echo $USER"}]}),
    )
    assert scan.scan_workspace_config(tmp_path).unchecked_reason is None
    for command in ("sh scripts/*.sh", "sh $(cat x)/a.sh", "sh `pwd`/a.sh"):
        _write(
            tmp_path / ".agents" / "hooks.json",
            json.dumps({"Stop": [{"command": command}]}),
        )
        assert scan.scan_workspace_config(tmp_path).unchecked_reason == (
            scan.VARIABLE_PATH
        ), command


def test_normal_project_and_settings_never_unchecked(
    tmp_path: Path, gemini_home: Path
) -> None:
    """Final review (control regression): agy's own settings.json and
    instruction trees (skills/rules) don't make every project's set
    non-empty — a cron in a plain project isn't held."""
    _write(gemini_home.parent / "antigravity-cli" / "settings.json", "{}")
    _write(tmp_path / ".agents" / "skills" / "s" / "SKILL.md", "# skill")
    _write(tmp_path / ".agents" / "rules" / "style.md", "# rules")
    result = scan.scan_workspace_config(tmp_path)
    assert result.agy == {}
    assert result.unchecked_reason is None


def test_variable_script_path_marks_unchecked(
    tmp_path: Path, gemini_home: Path
) -> None:
    _write(
        tmp_path / ".agents" / "mcp_config.json",
        json.dumps({"s": {"command": "node", "args": ["$PLUGIN_DIR/server.js"]}}),
    )
    assert scan.scan_workspace_config(tmp_path).unchecked_reason == (scan.VARIABLE_PATH)


def test_oversize_reference_manifest_marks_unchecked(
    tmp_path: Path, gemini_home: Path
) -> None:
    padding = "x" * (300 * 1024)
    _write(
        tmp_path / ".agents" / "hooks.json",
        json.dumps({"pad": padding, "Stop": [{"command": "sh scripts/a.sh"}]}),
    )
    assert scan.scan_workspace_config(tmp_path).unchecked_reason == scan.TOO_LARGE


def test_references_beyond_old_token_cap_and_agent_md_tracked(
    tmp_path: Path, gemini_home: Path
) -> None:
    filler = " ".join(f"w{i}" for i in range(500))
    _write(
        tmp_path / ".agents" / "hooks.json",
        json.dumps({"Stop": [{"command": f"echo {filler}; sh scripts/late.sh"}]}),
    )
    _write(
        tmp_path / ".agents" / "agents" / "helper.md",
        "---\nhooks:\n  Stop: ./tools/agent-hook.py\n---\n",
    )
    late = _write(tmp_path / "scripts" / "late.sh", "echo ok")
    agent_hook = _write(tmp_path / "tools" / "agent-hook.py", "print(1)")
    first = scan.scan_workspace_config(tmp_path)
    assert {"scripts/late.sh", "tools/agent-hook.py"} <= set(first.agy)
    late.write_text("echo evil")
    second = scan.scan_workspace_config(tmp_path)
    assert second.agy_digest != first.agy_digest
    agent_hook.write_text("print(2)")
    assert scan.scan_workspace_config(tmp_path).agy_digest != second.agy_digest


def test_linked_user_dirs(
    tmp_path: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ~/.gemini/config link into $HOME (dotfiles) is followed (stat only);
    one leading outside $HOME is "not checked"."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    dotfiles = home / "dotfiles-plugins"
    manifest = _write(dotfiles / "p" / "hooks.json", '{"a": 1}')
    (gemini_home / "plugins").symlink_to(dotfiles, target_is_directory=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    first = scan.scan_workspace_config(proj)
    assert first.unchecked_reason is None
    assert "~/.gemini/config/plugins/p/hooks.json" in first.agy
    manifest.write_text('{"a": 22}')
    assert scan.scan_workspace_config(proj).agy_digest != first.agy_digest
    (gemini_home / "plugins").unlink()
    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere / "hooks.json", '{"a": 1}')
    (gemini_home / "plugins").symlink_to(elsewhere, target_is_directory=True)
    assert scan.scan_workspace_config(proj).unchecked_reason == scan.OUTSIDE


def test_huge_directory_is_bounded_per_entry(
    tmp_path: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scan, "MAX_FILES", 5)
    junk = tmp_path / ".agents" / "plugins" / "junk"
    junk.mkdir(parents=True)
    for i in range(50):
        (junk / f"f{i}").write_text("x")
    assert scan.scan_workspace_config(tmp_path).truncated is True


@pytest.mark.anyio
async def test_scan_timeout_is_unchecked_not_wedged(
    project: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan stuck in a blocking read (hung mount) is abandoned: attended runs
    warn, unattended runs are held."""
    import threading

    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    monkeypatch.setattr(scan, "SCAN_TIMEOUT_S", 0.2)
    release = threading.Event()

    def stuck(cwd: Path) -> scan.ScanResult:
        release.wait(5)
        return scan.ScanResult(root=cwd)

    monkeypatch.setattr(scan, "scan_workspace_config", stuck)
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    try:
        (done,) = await _run(runner, EngineRunOptions(unattended_trigger="cron:x"))
        assert done.usage == {PRESPAWN_BLOCKED_KEY: "config_unchecked"}
        events = await _run(runner)
        assert _completed(events).ok is True
        assert any("timed out" in r.action.title for r in _warning_rows(events))
    finally:
        release.set()


@pytest.mark.anyio
async def test_unchecked_reason_from_scan_reaches_runner(
    project: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    os.mkfifo(project / ".agents-fifo")
    (project / ".agents").mkdir()
    os.symlink(project / ".agents-fifo", project / ".agents" / "hooks.json")
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    rows = _warning_rows(await _run(runner))
    assert any(scan.SPECIAL in r.action.title for r in rows)
    (done,) = await _run(runner, EngineRunOptions(unattended_trigger="cron:x"))
    assert done.usage == {PRESPAWN_BLOCKED_KEY: "config_unchecked"}


def test_every_file_under_agents_and_hook_scripts_tracked(
    tmp_path: Path, gemini_home: Path
) -> None:
    _write(tmp_path / ".agents" / "hooks" / "run.sh", "echo hi")
    _write(
        tmp_path / ".agents" / "hooks.json",
        json.dumps({"Stop": [{"command": "sh scripts/guard.sh --fast"}]}),
    )
    script = _write(tmp_path / "scripts" / "guard.sh", "echo ok")
    first = scan.scan_workspace_config(tmp_path)
    assert ".agents/hooks/run.sh" in first.agy
    assert "scripts/guard.sh" in first.agy
    script.write_text("curl evil")
    assert scan.scan_workspace_config(tmp_path).agy_digest != first.agy_digest


@pytest.mark.anyio
async def test_truncated_scan_never_counts_as_seen(
    project: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    monkeypatch.setattr(scan, "MAX_FILES", 3)
    for i in range(5):
        _write(project / ".agents" / "plugins" / "p" / f"f{i}.json", '{"x": 1}')
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    for _ in range(2):  # every attended run warns; nothing becomes "seen"
        rows = _warning_rows(await _run(runner))
        assert any("couldn't check" in r.action.title for r in rows)
    events = await _run(runner, EngineRunOptions(unattended_trigger="cron:x"))
    assert events[-1].usage == {PRESPAWN_BLOCKED_KEY: "config_unchecked"}


@pytest.mark.anyio
async def test_scan_failure_refuses_unattended_and_warns_attended(
    project: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")

    def boom(cwd: Path) -> scan.ScanResult:
        raise PermissionError("nope")

    monkeypatch.setattr(scan, "scan_workspace_config", boom)
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    (done,) = await _run(runner, EngineRunOptions(unattended_trigger="webhook:y"))
    assert done.usage == {PRESPAWN_BLOCKED_KEY: "config_unchecked"}
    events = await _run(runner)
    assert _completed(events).ok is True
    assert any("couldn't check" in r.action.title for r in _warning_rows(events))


@pytest.mark.anyio
async def test_result_rescan_failure_warns(
    project: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    real = scan.scan_workspace_config
    calls = {"n": 0}

    def flaky(cwd: Path) -> scan.ScanResult:
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("gone")
        return real(cwd)

    monkeypatch.setattr(scan, "scan_workspace_config", flaky)
    events = await _run(AntigravityRunner(antigravity_cmd=str(FAKE_AGY)))
    assert any("couldn't re-check" in r.action.title for r in _warning_rows(events))


@pytest.mark.parametrize("reported", ["proceed-in-sandbox", "something-new"])
def test_init_any_non_default_mode_without_bypass_refused(reported: str) -> None:
    events, _ = _replay(_with_init_mode(_lines("tools_denied"), reported))
    (done,) = events
    assert isinstance(done, CompletedEvent) and done.ok is False
    assert reported in (done.error or "")


@pytest.mark.parametrize("reported", ["request-review", "strict"])
def test_init_default_or_stricter_mode_runs(reported: str) -> None:
    events, _ = _replay(_with_init_mode(_lines("ok"), reported))
    assert isinstance(events[0], StartedEvent)


@pytest.mark.anyio
async def test_config_non_default_tool_permission_refuses_workspace(
    project: Path,
    gemini_home: Path,
    agy_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    agy_config["value"] = _config(toolPermission="proceed-in-sandbox")
    (done,) = await _run(runner)
    assert done.usage == {PRESPAWN_BLOCKED_KEY: "agy_always_proceed"}
    assert "proceed-in-sandbox" in (done.error or "")
    agy_config["value"] = _config(toolPermission="strict")
    assert _completed(await _run(runner)).ok is True


@pytest.mark.anyio
async def test_out_of_root_link_is_unchecked_in_runner(
    project: Path, tmp_path: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere / "hooks.json", '{"a": 1}')
    (project / ".agents" / "plugins").mkdir(parents=True)
    (project / ".agents" / "plugins" / "x").symlink_to(
        elsewhere, target_is_directory=True
    )
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    for _ in range(2):
        rows = _warning_rows(await _run(runner))
        assert any("links outside the project" in r.action.title for r in rows)
    events = await _run(runner, EngineRunOptions(unattended_trigger="cron:x"))
    assert events[-1].usage == {PRESPAWN_BLOCKED_KEY: "config_unchecked"}


@pytest.mark.anyio
async def test_scans_run_off_the_event_loop(
    project: Path, gemini_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    real = scan.scan_workspace_config
    threads: list[bool] = []

    def spy(cwd: Path) -> scan.ScanResult:
        threads.append(threading.current_thread() is threading.main_thread())
        return real(cwd)

    monkeypatch.setattr(scan, "scan_workspace_config", spy)
    events = await _run(AntigravityRunner(antigravity_cmd=str(FAKE_AGY)))
    assert _completed(events).ok is True
    assert threads == [False, False]  # pre-spawn and post-run, both off-loop
