"""Codex app-server support gate (#961) and transport (#962), against fakes.

Gate: the fake answers ``--version`` and ``app-server generate-json-schema
[--experimental] --out DIR`` by copying a hand-built bundle, and logs each
call so the per-binary cache can be checked.

Transport: a fake ``codex app-server`` that speaks the JSON-RPC subset over
stdio, replies only after reading each request, records every message it
receives, and follows a per-test scenario.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from structlog.testing import capture_logs

from untether.config import ConfigError
from untether.model import CompletedEvent, ResumeToken, StartedEvent
from untether.runner import JsonlSubprocessRunner, prespawn_blocked_reason
from untether.runner_bridge import _resume_failure_clears_session
from untether.runners import codex_app_server as gate
from untether.runners.codex import CodexRunner, build_runner
from untether.runners.extra_args_guard import BlockedExtraArgsError
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.schemas.codex_app_server import (
    CODEX_APP_SERVER_USED,
    MIN_TESTED_APP_SERVER_VERSION,
)
from untether.utils.paths import reset_run_base_dir, set_run_base_dir

_UNION = {
    "client_request": "ClientRequest",
    "client_notification": "ClientNotification",
    "server_notification": "ServerNotification",
    "server_request": "ServerRequest",
}


def _bundle(*, skip: frozenset[str] = frozenset()) -> dict:
    defs: dict = {name: {"oneOf": []} for name in _UNION.values()}
    for kind, method in CODEX_APP_SERVER_USED:
        if method not in skip:
            defs[_UNION[kind]]["oneOf"].append(
                {"properties": {"method": {"enum": [method]}}}
            )
    return {"definitions": defs}


def _fake_codex(
    tmp_path: Path,
    *,
    stable: dict | None = None,
    experimental: dict | None = None,
    fail: bool = False,
) -> Path:
    stable_path = tmp_path / "stable.json"
    exp_path = tmp_path / "exp.json"
    stable_path.write_text(json.dumps(stable or _bundle()))
    exp_path.write_text(json.dumps(experimental or _bundle()))
    script = tmp_path / "codex"
    script.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, shutil, sys\n"
        "args = sys.argv[1:]\n"
        f"with open({str(tmp_path / 'calls.log')!r}, 'a') as fh:\n"
        "    fh.write(' '.join(args) + '\\n')\n"
        "if args == ['--version']:\n"
        "    print('codex-cli 9.9.9'); sys.exit(0)\n"
        f"if {fail!r}:\n"
        "    print(\"error: unrecognized subcommand 'app-server'\", file=sys.stderr)\n"
        "    sys.exit(2)\n"
        "out = pathlib.Path(args[args.index('--out') + 1])\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        f"src = {str(exp_path)!r} if '--experimental' in args else {str(stable_path)!r}\n"
        "shutil.copy(src, out / 'codex_app_server_protocol.schemas.json')\n"
    )
    script.chmod(0o755)
    return script


def _schema_calls(tmp_path: Path) -> int:
    log = tmp_path / "calls.log"
    lines = log.read_text().splitlines() if log.exists() else []
    return sum("generate-json-schema" in line for line in lines)


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "_SUPPORT_CACHE", {})


def test_supported_cli_passes_logs_ready_once_and_caches(tmp_path: Path) -> None:
    cmd = str(_fake_codex(tmp_path))
    with capture_logs() as logs:
        first = gate.codex_app_server_support(cmd)
        second = gate.codex_app_server_support(cmd)
    assert first.ok and first.version == "9.9.9"
    assert second is first
    assert _schema_calls(tmp_path) == 2  # stable + experimental, once
    ready = [e for e in logs if e["event"] == "codex.app_server.ready"]
    assert [e["version"] for e in ready] == ["9.9.9"]


def test_binary_change_reprobes(tmp_path: Path) -> None:
    script = _fake_codex(tmp_path)
    gate.codex_app_server_support(str(script))
    st = script.stat()
    os.utime(script, (st.st_atime, st.st_mtime + 10))
    gate.codex_app_server_support(str(script))
    assert _schema_calls(tmp_path) == 4


def test_missing_method_is_unsupported(tmp_path: Path) -> None:
    gone = frozenset({"turn/interrupt"})
    cmd = str(
        _fake_codex(
            tmp_path, stable=_bundle(skip=gone), experimental=_bundle(skip=gone)
        )
    )
    with capture_logs() as logs:
        support = gate.codex_app_server_support(cmd)
    assert support.problems == ("`turn/interrupt` missing",)
    assert any(e["event"] == "codex.app_server.unsupported" for e in logs)


def test_stable_method_moved_to_experimental_is_unsupported(tmp_path: Path) -> None:
    cmd = str(_fake_codex(tmp_path, stable=_bundle(skip=frozenset({"thread/resume"}))))
    support = gate.codex_app_server_support(cmd)
    assert support.problems == ("`thread/resume` is experimental-only",)


def test_cli_without_generate_json_schema_is_unsupported_and_cached(
    tmp_path: Path,
) -> None:
    cmd = str(_fake_codex(tmp_path, fail=True))
    support = gate.codex_app_server_support(cmd)
    assert not support.ok
    assert "schema unavailable" in support.problems[0]
    gate.codex_app_server_support(cmd)
    assert _schema_calls(tmp_path) == 1


def test_probe_that_cannot_run_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd = str(_fake_codex(tmp_path))

    def boom(_cmd: str) -> dict:
        raise subprocess.TimeoutExpired(cmd=_cmd, timeout=30)

    monkeypatch.setattr(gate, "probe_schema_subset", boom)
    with capture_logs() as logs:
        support = gate.codex_app_server_support(cmd)
    assert support.problems == ("schema probe could not run",)
    assert any(e["event"] == "codex.app_server.probe_failed" for e in logs)
    assert gate._SUPPORT_CACHE == {}


def test_missing_binary_is_unsupported(tmp_path: Path) -> None:
    support = gate.codex_app_server_support("untether-no-such-codex-binary")
    assert not support.ok and "not found" in support.problems[0]


@pytest.mark.anyio
async def test_prespawn_refusal_keeps_the_saved_session(tmp_path: Path) -> None:
    gone = frozenset({"turn/start"})
    cmd = str(
        _fake_codex(
            tmp_path, stable=_bundle(skip=gone), experimental=_bundle(skip=gone)
        )
    )
    resume = ResumeToken(engine="codex", value="00000000-0000-0000-0000-000000000000")
    event = await gate.app_server_unsupported_event(cmd, resume)
    assert event is not None and not event.ok
    assert event.resume == resume
    assert prespawn_blocked_reason(event.usage) == gate.APP_SERVER_UNSUPPORTED_BLOCK
    assert not _resume_failure_clears_session("codex", event.usage, event.error)
    assert "9.9.9" in event.error and "`turn/start` missing" in event.error
    assert MIN_TESTED_APP_SERVER_VERSION in event.error
    assert 'transport = "exec"' in event.error


@pytest.mark.anyio
async def test_supported_cli_yields_no_refusal(tmp_path: Path) -> None:
    assert (
        await gate.app_server_unsupported_event(str(_fake_codex(tmp_path)), None)
        is None
    )


# --- #962 transport: config -------------------------------------------------


def test_transport_defaults_to_exec_and_selects_app_server(tmp_path: Path) -> None:
    exec_runner = build_runner({}, tmp_path)
    assert type(exec_runner) is CodexRunner
    app = build_runner({"transport": "app-server"}, tmp_path)
    assert isinstance(app, gate.CodexAppServerRunner)
    assert app.extra_args == ["-c", "notify=[]"]  # default kept
    assert type(build_runner({"transport": "exec"}, tmp_path)) is CodexRunner


def test_invalid_transport_names_the_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"codex\.transport"):
        build_runner({"transport": "websocket"}, tmp_path)


@pytest.mark.parametrize(
    "flag",
    [["--skip-git-repo-check"], ["--json"], ["-o", "x"], ["--yolo"], ["--search"]],
)
def test_app_server_refuses_flags_outside_its_allow_list(
    tmp_path: Path, flag: list[str]
) -> None:
    with pytest.raises(BlockedExtraArgsError):
        build_runner({"transport": "app-server", "extra_args": flag}, tmp_path)


def test_app_server_allows_its_own_options(tmp_path: Path) -> None:
    args = ["-c", "notify=[]", "--enable", "x", "--disable", "y", "--strict-config"]
    runner = build_runner({"transport": "app-server", "extra_args": args}, tmp_path)
    assert runner.extra_args == args
    # The #209 deny list still applies to -c values.
    with pytest.raises(BlockedExtraArgsError):
        build_runner(
            {
                "transport": "app-server",
                "extra_args": ["-c", 'sandbox_mode="danger-full-access"'],
            },
            tmp_path,
        )


def test_profile_is_refused_on_app_server_only(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"codex\.profile"):
        build_runner({"transport": "app-server", "profile": "work"}, tmp_path)
    exec_runner = build_runner({"profile": "work"}, tmp_path)
    assert exec_runner.extra_args[-2:] == ["--profile", "work"]


def test_exec_argv_unchanged_with_transport_unset(tmp_path: Path) -> None:
    runner = build_runner({}, tmp_path)
    assert runner.build_args("hi", None, state=runner.new_state("hi", None)) == [
        "-c",
        "notify=[]",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--color=never",
        "-",
    ]


# --- #962 transport: fake app-server ----------------------------------------

THREAD = "01a1f1db-f8ae-74a2-be6f-babda77caa8f"
OTHER_THREAD = "01a1f1dc-09ac-7ec3-851e-ef00717cb6a2"

_FAKE_APP_SERVER = r"""
import json, os, sys, time
SC = json.load(open(SC_PATH))
log = open(LOG_PATH, "a")
with open(ARGV_PATH, "w") as fh:
    json.dump(sys.argv[1:], fh)

def out(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()

def thread(tid):
    return {"id": tid, "source": "vscode", "model": None}

POLICY = {
    "read-only": {"type": "readOnly", "networkAccess": False},
    "workspace-write": {"type": "workspaceWrite", "writableRoots": []},
}

for line in sys.stdin:
    msg = json.loads(line)
    log.write(json.dumps(msg) + "\n")
    log.flush()
    method, rid = msg.get("method"), msg.get("id")
    if method is None:
        continue  # the client's answer to a server request
    if method == "initialize":
        if SC.get("server_request_first"):
            out({"id": 0, "method": "item/fileChange/requestApproval",
                 "params": {"itemId": "i", "threadId": "t", "turnId": "u"}})
            log.write(sys.stdin.readline())
            log.flush()
        out({"id": rid, "result": {"userAgent": "fake/0", "codexHome": "",
             "platformFamily": "unix", "platformOs": "linux"}})
        if SC.get("close_stdin_after_init"):
            os.close(0)
            time.sleep(30)
    elif method == "config/read":
        out({"id": rid, "result": {"config": SC.get("config", {}), "origins": {}}})
    elif method in ("thread/start", "thread/resume"):
        p = msg["params"]
        if method == "thread/resume" and SC.get("resume_error"):
            out({"id": rid, "error": {"code": -32600,
                 "message": "no rollout found for thread id " + p["threadId"]}})
            continue
        tid = p.get("threadId", SC.get("thread_id", "THREAD"))
        sandbox = SC.get("echo_sandbox") or POLICY.get(
            p.get("sandbox"), SC.get("default_sandbox", POLICY["read-only"]))
        out({"id": rid, "result": {
            "thread": thread(tid), "model": "gpt-fake", "modelProvider": "openai",
            "cwd": p.get("cwd"), "approvalPolicy": p.get("approvalPolicy"),
            "approvalsReviewer": p.get("approvalsReviewer"), "sandbox": sandbox}})
        out({"method": "thread/started", "params": {"thread": thread(tid)}})
        if SC.get("stop_reading_after_thread"):
            time.sleep(30)
    elif method == "thread/list":
        out({"id": rid, "result": {"data": [thread(t) for t in SC.get("listed", [])]}})
    elif method == "turn/start":
        tid = msg["params"]["threadId"]
        turn = {"id": "turn-1", "status": "inProgress", "items": []}
        out({"id": rid, "result": {"turn": turn}})
        if "exit_after_turn_start" in SC:
            sys.exit(SC["exit_after_turn_start"])
        out({"method": "turn/started", "params": {"threadId": tid, "turn": turn}})
        if SC.get("unknown_notification"):
            out({"method": "brand/new", "params": {"x": 1}})
        # A sub-agent thread finishing must not end this run.
        out({"method": "turn/completed", "params": {"threadId": "subagent",
             "turn": {"id": "s", "status": "completed", "items": []}}})
        for i, (text, phase) in enumerate(SC.get("messages", [["done", "final_answer"]])):
            out({"method": "item/completed", "params": {"threadId": tid, "turnId": "turn-1",
                 "item": {"type": "agentMessage", "id": f"m{i}", "text": text, "phase": phase}}})
        if SC.get("exit_before_complete"):
            sys.exit(0)
        out({"method": "turn/completed", "params": {"threadId": tid, "turn": {
            "id": "turn-1", "status": SC.get("status", "completed"), "items": [],
            "error": SC.get("turn_error")}}})
if SC.get("linger"):
    time.sleep(30)
"""


class Fake:
    def __init__(self, tmp_path: Path, **scenario: Any) -> None:
        self.dir = tmp_path / "fake"
        self.dir.mkdir()
        self.log = self.dir / "received.jsonl"
        self.argv = self.dir / "argv.json"
        sc = self.dir / "scenario.json"
        sc.write_text(json.dumps({"thread_id": THREAD, **scenario}))
        self.cmd = self.dir / "codex"
        self.cmd.write_text(
            f"#!{sys.executable}\n"
            f"SC_PATH, LOG_PATH, ARGV_PATH = {str(sc)!r}, {str(self.log)!r}, "
            f"{str(self.argv)!r}\n" + _FAKE_APP_SERVER
        )
        self.cmd.chmod(0o755)

    def received(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text().splitlines() if x]

    def requests(self, method: str) -> list[dict]:
        return [m.get("params") for m in self.received() if m.get("method") == method]


@pytest.fixture
def project(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    token = set_run_base_dir(proj)
    try:
        yield proj
    finally:
        reset_run_base_dir(token)


@pytest.fixture
def gate_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def supported(cmd: str, resume: ResumeToken | None) -> None:
        calls.append(cmd)
        return

    monkeypatch.setattr(gate, "app_server_unsupported_event", supported)
    return calls


def _runner(fake: Fake, **attrs: Any) -> gate.CodexAppServerRunner:
    runner = gate.CodexAppServerRunner(codex_cmd=str(fake.cmd), extra_args=[])
    runner.exit_grace_s = attrs.pop("exit_grace_s", 5.0)
    for key, value in attrs.items():
        setattr(runner, key, value)
    return runner


async def _run(
    runner: Any,
    resume: ResumeToken | None = None,
    options: EngineRunOptions | None = None,
    prompt: str = "hello",
) -> list[Any]:
    with apply_run_options(options), anyio.fail_after(20):
        return [evt async for evt in runner.run(prompt, resume)]


def _completed(events: list[Any]) -> CompletedEvent:
    assert isinstance(events[-1], CompletedEvent)
    assert sum(isinstance(e, CompletedEvent) for e in events) == 1
    return events[-1]


@pytest.mark.anyio
async def test_new_run_end_to_end(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(
        tmp_path,
        messages=[["looking", "commentary"], ["the answer", "final_answer"]],
    )
    events = await _run(_runner(fake), options=EngineRunOptions(reasoning="high"))
    assert gate_calls == [str(fake.cmd)]
    assert json.loads(fake.argv.read_text()) == ["app-server", "--listen", "stdio://"]
    started = [e for e in events if isinstance(e, StartedEvent)]
    assert len(started) == 1 and isinstance(events[0], StartedEvent)
    assert started[0].resume == ResumeToken(engine="codex", value=THREAD)
    assert started[0].meta["model"] == "gpt-fake"
    done = _completed(events)
    assert done.ok and done.answer == "the answer"
    assert done.resume == ResumeToken(engine="codex", value=THREAD)

    init = fake.requests("initialize")[0]
    assert init["clientInfo"]["name"] == "untether"
    assert init["capabilities"]["experimentalApi"] is False
    assert (
        "item/agentMessage/delta" in init["capabilities"]["optOutNotificationMethods"]
    )
    methods = [m.get("method") for m in fake.received()]
    assert methods[:3] == ["initialize", "initialized", "config/read"]
    turn = fake.requests("turn/start")[0]
    assert turn == {
        "threadId": THREAD,
        "input": [{"type": "text", "text": "hello"}],
        "turnTrigger": "untether",
        "effort": "high",
    }


@pytest.mark.anyio
async def test_answer_falls_back_to_the_last_agent_message(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, messages=[["one", "commentary"], ["two", "commentary"]])
    assert _completed(await _run(_runner(fake))).answer == "two"


@pytest.mark.anyio
async def test_base_run_impl_still_calls_both_seams(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    calls: list[str] = []

    class Spy(gate.CodexAppServerRunner):
        async def _send_payload(self, *args: Any, **kwargs: Any) -> None:
            calls.append("send")
            await super()._send_payload(*args, **kwargs)

        async def _iter_jsonl_events(self, **kwargs: Any):
            calls.append("iter")
            async for evt in super()._iter_jsonl_events(**kwargs):
                yield evt

    fake = Fake(tmp_path)
    runner = Spy(codex_cmd=str(fake.cmd), extra_args=[])
    assert _completed(await _run(runner)).ok
    assert calls == ["send", "iter"]
    assert gate.CodexAppServerRunner.run_impl is not JsonlSubprocessRunner.run_impl


# --- modes and the trust rule (P1, DEC-7) ------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("options", "sandbox"),
    [
        (EngineRunOptions(permission_mode="safe"), "read-only"),
        (
            EngineRunOptions(permission_mode="weird", unattended_trigger="webhook:w"),
            "read-only",
        ),
        (EngineRunOptions(permission_mode="weird"), "read-only"),  # DEC-5
    ],
)
async def test_safe_and_unknown_modes_send_read_only_without_config_read(
    tmp_path: Path,
    project: Path,
    gate_calls: list[str],
    options: EngineRunOptions,
    sandbox: str,
) -> None:
    fake = Fake(tmp_path)
    events = await _run(_runner(fake), options=options)
    assert _completed(events).ok
    start = fake.requests("thread/start")[0]
    assert start == {
        "cwd": str(project),
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "sandbox": sandbox,
    }
    assert fake.requests("config/read") == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "options",
    [None, EngineRunOptions(permission_mode="auto", unattended_trigger="cron:c")],
)
async def test_full_auto_omits_sandbox_in_a_project_codex_knows(
    tmp_path: Path, project: Path, gate_calls: list[str], options: Any
) -> None:
    config = {
        "sandbox_mode": "workspace-write",
        "projects": {str(project.parent): {"trust_level": "untrusted"}},
    }
    fake = Fake(tmp_path, config=config, default_sandbox={"type": "workspaceWrite"})
    events = await _run(_runner(fake), options=options)
    assert _completed(events).ok
    assert fake.requests("config/read") == [{"cwd": str(project)}]
    start = fake.requests("thread/start")[0]
    assert "sandbox" not in start and start["approvalPolicy"] == "never"
    assert not any(getattr(e, "action", None) for e in events[1:-1])


@pytest.mark.anyio
async def test_full_auto_runs_read_only_where_codex_would_save_trust(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, config={"sandbox_mode": "workspace-write", "projects": {}})
    with capture_logs() as logs:
        events = await _run(_runner(fake))
    assert fake.requests("thread/start")[0]["sandbox"] == "read-only"
    rows = [e for e in events if getattr(e, "action", None)]
    assert [r.action.id for r in rows] == ["codex.untrusted_read_only"]
    assert "--trust" not in rows[0].action.title
    assert any(e["event"] == "codex.app_server.untrusted_read_only" for e in logs)
    assert isinstance(events[0], StartedEvent) and _completed(events).ok


@pytest.mark.anyio
async def test_full_auto_read_only_quietly_when_codex_default_is_read_only(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, config={})  # no projects, no sandbox_mode
    events = await _run(_runner(fake))
    assert fake.requests("thread/start")[0]["sandbox"] == "read-only"
    assert not any(getattr(e, "action", None) for e in events)


def test_has_trust_entry_matches_cwd_or_a_parent() -> None:
    projects = {"/srv/repo": {"trust_level": "trusted"}}
    assert gate.has_trust_entry(projects, "/srv/repo")
    assert gate.has_trust_entry(projects, "/srv/repo/sub/dir")
    assert not gate.has_trust_entry(projects, "/srv/other")
    assert not gate.has_trust_entry(None, "/srv/repo")
    assert not gate.has_trust_entry({"/srv/repo": "x"}, "/srv/repo")


@pytest.mark.parametrize(
    ("policy", "mode"),
    [
        ({"type": "readOnly", "networkAccess": False}, "read-only"),
        ({"type": "workspaceWrite"}, "workspace-write"),
        ({"type": "dangerFullAccess"}, "danger-full-access"),
        ({"type": "externalSandbox"}, "danger-full-access"),
        ("read-only", "read-only"),
        ({"type": "new"}, None),
        (None, None),
    ],
)
def test_sandbox_policy_echo_normalises(policy: Any, mode: str | None) -> None:
    assert gate.sandbox_policy_mode(policy) == mode


@pytest.mark.anyio
async def test_echo_mismatch_warns_and_adds_a_row(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, echo_sandbox={"type": "workspaceWrite"})
    with capture_logs() as logs:
        events = await _run(
            _runner(fake), options=EngineRunOptions(permission_mode="safe")
        )
    assert any(e["event"] == "codex.permission_mode.mismatch" for e in logs)
    rows = [e for e in events if getattr(e, "action", None)]
    assert [r.action.id for r in rows] == ["codex.permission_mismatch"]
    perms = [e for e in logs if e["event"] == "codex.app_server.permissions"]
    assert perms[0]["effective"]["sandbox"] == "workspace-write"


@pytest.mark.anyio
async def test_matching_echo_object_gives_no_warning(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path)  # echoes {"type": "readOnly"} for read-only
    with capture_logs() as logs:
        await _run(_runner(fake), options=EngineRunOptions(permission_mode="safe"))
    assert not any(e["event"] == "codex.permission_mode.mismatch" for e in logs)


# --- resume and /continue ----------------------------------------------------


@pytest.mark.anyio
async def test_resume_uses_thread_resume_with_the_same_overrides(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path)
    token = ResumeToken(engine="codex", value=THREAD)
    events = await _run(
        _runner(fake),
        resume=token,
        options=EngineRunOptions(permission_mode="safe", model="gpt-x"),
    )
    assert _completed(events).ok
    assert fake.requests("thread/start") == []
    assert fake.requests("thread/resume") == [
        {
            "threadId": THREAD,
            "cwd": str(project),
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "sandbox": "read-only",
            "model": "gpt-x",
        }
    ]


@pytest.mark.anyio
async def test_resume_failure_keeps_codex_text_for_the_bridge(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, resume_error=True)
    token = ResumeToken(engine="codex", value=THREAD)
    events = await _run(_runner(fake), resume=token)
    done = _completed(events)
    assert not done.ok and "no rollout found" in (done.error or "")
    assert _resume_failure_clears_session("codex", done.usage, done.error)
    assert fake.requests("turn/start") == []


@pytest.mark.anyio
async def test_continue_lists_with_all_four_source_kinds_then_resumes(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, listed=[OTHER_THREAD])
    cont = ResumeToken(engine="codex", value="", is_continue=True)
    events = await _run(
        _runner(fake), resume=cont, options=EngineRunOptions(permission_mode="safe")
    )
    assert fake.requests("thread/list") == [
        {
            "cwd": str(project),
            "sourceKinds": ["cli", "vscode", "exec", "appServer"],
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "limit": 1,
            "archived": False,
        }
    ]
    assert fake.requests("thread/resume")[0]["threadId"] == OTHER_THREAD
    assert events[0].resume.value == OTHER_THREAD
    assert _completed(events).ok


@pytest.mark.anyio
async def test_continue_with_no_earlier_thread_starts_one_with_a_note(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, listed=[])
    cont = ResumeToken(engine="codex", value="", is_continue=True)
    events = await _run(
        _runner(fake), resume=cont, options=EngineRunOptions(permission_mode="safe")
    )
    assert len(fake.requests("thread/start")) == 1
    assert isinstance(events[0], StartedEvent)
    notes = [e for e in events if getattr(e, "action", None)]
    assert "No earlier Codex session" in notes[0].action.title
    assert _completed(events).ok


# --- lifecycle and robustness -------------------------------------------------


@pytest.mark.anyio
async def test_failed_turn_is_an_error_with_codex_text(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(
        tmp_path,
        status="failed",
        turn_error={
            "message": "usage limit reached",
            "codexErrorInfo": "usageLimitExceeded",
        },
        messages=[["partial", "commentary"]],
    )
    done = _completed(await _run(_runner(fake)))
    assert not done.ok and done.error == "usage limit reached"
    assert done.answer == "partial"


@pytest.mark.anyio
async def test_process_death_mid_turn_is_an_app_server_error(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, exit_after_turn_start=3)
    done = _completed(await _run(_runner(fake)))
    assert not done.ok and "codex app-server failed" in (done.error or "")


@pytest.mark.anyio
async def test_clean_exit_without_turn_completed_is_not_success(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(
        tmp_path, exit_before_complete=True, messages=[["kept", "final_answer"]]
    )
    done = _completed(await _run(_runner(fake)))
    assert not done.ok
    assert done.error == "codex app-server exited before the turn finished"
    assert done.answer == "kept"


@pytest.mark.anyio
async def test_closed_stdin_ends_the_run_with_an_error(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, close_stdin_after_init=True)
    with capture_logs() as logs:
        done = _completed(await _run(_runner(fake, exit_grace_s=0.3)))
    assert not done.ok and done.error == "codex app-server stopped accepting input"
    assert any(e["event"] == "codex.app_server.write_failed" for e in logs)


@pytest.mark.anyio
async def test_stalled_write_ends_the_run_within_the_bound(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, stop_reading_after_thread=True)
    runner = _runner(fake, exit_grace_s=0.3, write_timeout_s=0.3)
    with anyio.fail_after(10):
        events = await _run(
            runner,
            options=EngineRunOptions(permission_mode="safe"),
            prompt="x" * 2_000_000,
        )
    done = _completed(events)
    assert not done.ok and done.error == "codex app-server stopped accepting input"


@pytest.mark.anyio
async def test_lingering_process_is_terminated_after_the_grace(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, linger=True)
    with capture_logs() as logs:
        events = await _run(_runner(fake, exit_grace_s=0.3))
    assert _completed(events).ok
    assert any(e["event"] == "codex.app_server.exit_timeout" for e in logs)


@pytest.mark.anyio
async def test_stdin_closed_after_the_final_and_exit_awaited(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path)
    with capture_logs() as logs:
        events = await _run(_runner(fake))
    assert _completed(events).ok
    exits = [e for e in logs if e["event"] == "subprocess.exit"]
    assert exits and exits[0]["rc"] == 0
    assert not any(e["event"] == "codex.app_server.exit_timeout" for e in logs)


@pytest.mark.anyio
async def test_unknown_notification_is_ignored(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, unknown_notification=True)
    events = await _run(_runner(fake))
    assert _completed(events).ok
    assert not any(getattr(e, "action", None) for e in events)


@pytest.mark.anyio
async def test_server_request_during_handshake_is_declined(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, server_request_first=True)
    events = await _run(_runner(fake))
    assert _completed(events).ok
    replies = [m for m in fake.received() if m.get("id") == 0 and "method" not in m]
    assert replies == [{"id": 0, "result": {"decision": "decline"}}]


# --- the gate on each transport ------------------------------------------------


@pytest.mark.anyio
async def test_unsupported_cli_is_refused_before_spawn(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refusal = gate.EventFactory("codex").completed_error(
        error="unsupported", usage={gate.PRESPAWN_BLOCKED_KEY: "x"}
    )

    async def unsupported(cmd: str, resume: ResumeToken | None) -> CompletedEvent:
        return refusal

    monkeypatch.setattr(gate, "app_server_unsupported_event", unsupported)
    fake = Fake(tmp_path)
    events = await _run(_runner(fake))
    assert events == [refusal]
    assert not fake.argv.exists()  # nothing spawned


@pytest.mark.anyio
async def test_gate_never_runs_on_exec(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(cmd: str, resume: ResumeToken | None) -> None:
        raise AssertionError("gate called on exec")

    monkeypatch.setattr(gate, "app_server_unsupported_event", boom)
    script = tmp_path / "codex-exec"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "sys.stdin.read()\n"
        f"print(json.dumps({{'type': 'thread.started', 'thread_id': '{THREAD}'}}))\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {}}))\n"
    )
    script.chmod(0o755)
    events = await _run(CodexRunner(codex_cmd=str(script), extra_args=[]))
    assert _completed(events).ok
