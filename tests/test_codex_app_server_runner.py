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
from untether.error_hints import CODEX_ERROR_INFO_HINTS
from untether.events import EventFactory
from untether.model import ActionEvent, CompletedEvent, ResumeToken, StartedEvent
from untether.runner import JsonlSubprocessRunner, prespawn_blocked_reason
from untether.runner_bridge import (
    _close_runner_events,
    _resume_failure_clears_session,
    _run_error_hint,
)
from untether.runners import codex_app_server as gate
from untether.runners.codex import (
    _UNTRUSTED_PROJECT_NOTE,
    CodexRunner,
    build_runner,
    translate_codex_event,
)
from untether.runners.codex_rpc import Notification
from untether.runners.extra_args_guard import BlockedExtraArgsError
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.schemas import codex as codex_schema
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
CUR = {}
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
        for n in SC.get("after_init", []):
            out(n)
        if SC.get("close_stdin_after_init"):
            os.close(0)
            time.sleep(30)
        if SC.get("hang_after_init"):
            time.sleep(30)
    elif method == "turn/interrupt":
        if SC.get("ignore_interrupt"):
            continue
        if SC.get("partial"):
            sys.stdout.write(SC["partial"][20:] + "\n")
        if SC.get("interrupt_error"):
            out({"id": rid, "error": {"code": -32600, "message": "no active turn"}})
        else:
            out({"id": rid, "result": {}})
        out({"method": "turn/completed", "params": {"threadId": CUR.get("tid"), "turn": {
            "id": "turn-1", "status": SC.get("interrupt_status", "interrupted"),
            "items": [], "error": None}}})
    elif method == "config/read":
        out({"id": rid, "result": {"config": SC.get("config", {}), "origins": {}}})
    elif method in ("thread/start", "thread/resume"):
        p = msg["params"]
        if method == "thread/resume" and SC.get("resume_error"):
            out({"id": rid, "error": {"code": -32600,
                 "message": SC.get("resume_error_message",
                     "no rollout found for thread id " + p["threadId"])}})
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
        tid = CUR["tid"] = msg["params"]["threadId"]
        turn = {"id": "turn-1", "status": "inProgress", "items": []}
        out({"id": rid, "result": {"turn": turn}})
        if "exit_after_turn_start" in SC:
            sys.exit(SC["exit_after_turn_start"])
        out({"method": "turn/started", "params": {"threadId": tid, "turn": turn}})
        if SC.get("unknown_notification"):
            out({"method": "brand/new", "params": {"x": 1}})
        for n in SC.get("notifications", []):
            out(n)
        # #963: each approval waits for the client's answer (logged), then
        # Codex's serverRequest/resolved; "withdraw" resolves it unanswered.
        for step in SC.get("approvals", []):
            for n in step.get("before", []):
                out(n)
            req = step["request"]
            out(req)
            if step.get("exit"):
                sys.exit(1)
            if not step.get("withdraw"):
                while True:
                    ans = sys.stdin.readline()
                    if not ans:
                        sys.exit(0)
                    log.write(ans)
                    log.flush()
                    a = json.loads(ans)
                    if a.get("id") == req["id"] and "method" not in a:
                        break
            out({"method": "serverRequest/resolved", "params": {
                "threadId": req["params"]["threadId"], "requestId": req["id"]}})
            for n in step.get("after", []):
                out(n)
        if SC.get("hold_turn"):
            if SC.get("partial"):
                sys.stdout.write(SC["partial"][:20])
                sys.stdout.flush()
            continue  # until turn/interrupt or EOF
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
async def test_session_summary_names_the_last_jsonrpc_method(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    """#1386: last_event_type (session.summary's source) is the frame's
    JSON-RPC method, not unknown."""
    fake = Fake(tmp_path, messages=[["the answer", "final_answer"]])
    runner = _runner(fake)
    assert _completed(await _run(runner)).ok
    stream = runner.current_stream
    assert stream is not None
    assert stream.last_event_type == "turn/completed"
    labels = [label for _, label in stream.recent_events]
    assert "unknown" not in labels


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
            "excludeTurns": True,  # no history, no deprecationNotice (#962 QA)
            "cwd": str(project),
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "sandbox": "read-only",
            "model": "gpt-x",
        }
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("configured", ["gpt-default", None])
async def test_resume_without_override_sends_codex_configured_model(
    tmp_path: Path, project: Path, gate_calls: list[str], configured: str | None
) -> None:
    """#1369: a resumed thread restores its saved model, so after
    `/model clear` a bad model stuck until /new. With no override the resume
    carries Codex's own configured model (config/read), even in safe mode."""
    fake = Fake(tmp_path, config={"model": configured})
    token = ResumeToken(engine="codex", value=THREAD)
    events = await _run(
        _runner(fake), resume=token, options=EngineRunOptions(permission_mode="safe")
    )
    assert _completed(events).ok
    assert fake.requests("config/read") == [{"cwd": str(project)}]
    resumed = fake.requests("thread/resume")[0]
    assert resumed.get("model") == configured
    assert resumed["sandbox"] == "read-only"


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
    assert fake.requests("thread/resume")[0]["excludeTurns"] is True
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


# --- 962-3: translation ---------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "codex_app_server"


def _bare_runner() -> gate.CodexAppServerRunner:
    return gate.CodexAppServerRunner(codex_cmd="codex", extra_args=[])


def _translate(runner: Any, state: Any, method: str, params: dict) -> list[Any]:
    state.inbound = Notification(method, params)
    try:
        return runner.translate(b"", state=state, resume=None, found_session=None)
    finally:
        state.inbound = None


def _app_state(runner: Any, thread: str = THREAD) -> Any:
    state = runner.new_state("p", None)
    state.thread_id = thread
    return state


def _item(method: str, item: dict, thread: str = THREAD) -> tuple[str, dict]:
    return method, {"threadId": thread, "turnId": "turn-1", "item": item}


def _rows(events: list[Any]) -> list[tuple]:
    return [
        (e.phase, e.action.kind, e.action.title, e.ok, e.action.detail)
        for e in events
        if isinstance(e, ActionEvent)
    ]


def _exec_rows(phase: str, item: dict) -> list[tuple]:
    tag = {"started": "item.started", "completed": "item.completed"}[phase]
    event = codex_schema.decode_event(json.dumps({"type": tag, "item": item}))
    return _rows(
        translate_codex_event(event, title="Codex", factory=EventFactory("codex"))
    )


_PARITY = [
    pytest.param(
        "started",
        {
            "type": "commandExecution",
            "id": "c1",
            "command": "bash -lc ls",
            "status": "inProgress",
            "exitCode": None,
            "aggregatedOutput": None,
            "commandActions": [{"type": "listFiles", "command": "ls"}],
        },
        {
            "type": "command_execution",
            "id": "c1",
            "command": "bash -lc ls",
            "aggregated_output": "",
            "exit_code": None,
            "status": "in_progress",
        },
        id="command-started",
    ),
    pytest.param(
        "completed",
        {
            "type": "commandExecution",
            "id": "c1",
            "command": "bash -lc false",
            "status": "failed",
            "exitCode": 1,
            "aggregatedOutput": "",
            "durationMs": 4,
            "startedAtMs": 1,
            "completedAtMs": 5,
        },
        {
            "type": "command_execution",
            "id": "c1",
            "command": "bash -lc false",
            "aggregated_output": "",
            "exit_code": 1,
            "status": "failed",
        },
        id="command-failed",
    ),
    pytest.param(
        "completed",
        {
            "type": "fileChange",
            "id": "f1",
            "status": "completed",
            "changes": [
                {
                    "path": "src/a.py",
                    "kind": {"type": "update", "move_path": None},
                    "diff": "+x",
                },
                {"path": "b.txt", "kind": {"type": "add"}},
            ],
        },
        {
            "type": "file_change",
            "id": "f1",
            "status": "completed",
            "changes": [
                {"path": "src/a.py", "kind": "update"},
                {"path": "b.txt", "kind": "add"},
            ],
        },
        id="file-change-kind-object",
    ),
    pytest.param(
        "completed",
        {
            "type": "mcpToolCall",
            "id": "t1",
            "server": "docs",
            "tool": "search",
            "status": "completed",
            "arguments": {"q": "x"},
            "result": {"content": [{"type": "text"}], "structuredContent": {"a": 1}},
            "error": None,
        },
        {
            "type": "mcp_tool_call",
            "id": "t1",
            "server": "docs",
            "tool": "search",
            "status": "completed",
            "arguments": {"q": "x"},
            "result": {"content": [{"type": "text"}], "structured_content": {"a": 1}},
            "error": None,
        },
        id="mcp-ok",
    ),
    pytest.param(
        "completed",
        {
            "type": "mcpToolCall",
            "id": "t1",
            "server": "docs",
            "tool": "search",
            "status": "failed",
            "arguments": {},
            "result": None,
            "error": {"message": "boom"},
        },
        {
            "type": "mcp_tool_call",
            "id": "t1",
            "server": "docs",
            "tool": "search",
            "status": "failed",
            "arguments": {},
            "result": None,
            "error": {"message": "boom"},
        },
        id="mcp-failed",
    ),
    pytest.param(
        "completed",
        {
            "type": "webSearch",
            "id": "w1",
            "query": "",
            "action": {"type": "openPage", "url": "https://example.com/x"},
        },
        {
            "type": "web_search",
            "id": "w1",
            "query": "",
            "action": {"type": "open_page", "url": "https://example.com/x"},
        },
        id="web-open-page",
    ),
    pytest.param(
        "started",
        {"type": "webSearch", "id": "w2", "query": "", "action": None},
        {"type": "web_search", "id": "w2", "query": ""},
        id="web-started-placeholder",
    ),
    pytest.param(
        "completed",
        {
            "type": "reasoning",
            "id": "r1",
            "summary": ["**Planning** the fix"],
            "content": [],
        },
        {"type": "reasoning", "id": "r1", "text": "**Planning** the fix"},
        id="reasoning",
    ),
    pytest.param(
        "completed",
        {
            "type": "agentMessage",
            "id": "m1",
            "text": "Looking at the tests",
            "phase": "commentary",
        },
        {
            "type": "agent_message",
            "id": "m1",
            "text": "Looking at the tests",
            "phase": "commentary",
        },
        id="commentary-note",
    ),
]


@pytest.mark.parametrize(("phase", "app_item", "exec_item"), _PARITY)
def test_item_rows_match_exec(phase: str, app_item: dict, exec_item: dict) -> None:
    runner = _bare_runner()
    state = _app_state(runner)
    rows = _rows(_translate(runner, state, *_item(f"item/{phase}", app_item)))
    assert rows
    assert rows == _exec_rows(phase, exec_item)


def test_items_exec_shows_no_started_row_for_have_none_here() -> None:
    runner = _bare_runner()
    state = _app_state(runner)
    for item in (
        {"type": "fileChange", "id": "f", "status": "inProgress", "changes": []},
        {"type": "reasoning", "id": "r", "summary": [], "content": []},
        {"type": "agentMessage", "id": "m", "text": "", "phase": None},
        {"type": "userMessage", "id": "u", "content": []},
    ):
        assert _translate(runner, state, *_item("item/started", item)) == []


def test_unknown_item_type_gets_a_generic_row_and_one_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "_UNKNOWN_ITEMS_LOGGED", set())
    runner = _bare_runner()
    state = _app_state(runner)
    with capture_logs() as logs:
        for phase in ("started", "completed", "started"):
            item = {"type": "brandNewThing", "id": f"x-{phase}", "status": "completed"}
            rows = _rows(_translate(runner, state, *_item(f"item/{phase}", item)))
            assert [r[:3] for r in rows] == [(phase, "tool", "brandNewThing")]
    logged = [e for e in logs if e["event"] == "codex.app_server.unknown_item"]
    assert [e["item_type"] for e in logged] == ["brandNewThing"]


def test_subagent_and_image_view_rows() -> None:
    runner = _bare_runner()
    state = _app_state(runner)
    sub = {
        "type": "collabAgentToolCall",
        "id": "a1",
        "tool": "spawnAgent",
        "status": "completed",
        "prompt": "check the docs",
    }
    img = {"type": "imageView", "id": "i1", "path": "shot.png"}
    sub_rows = _rows(_translate(runner, state, *_item("item/completed", sub)))
    assert sub_rows[0][:4] == ("completed", "subagent", "check the docs", True)
    img_rows = _rows(_translate(runner, state, *_item("item/completed", img)))
    assert img_rows[0][1:3] == ("tool", "view image shot.png")


def test_items_of_another_thread_are_ignored() -> None:
    runner = _bare_runner()
    state = _app_state(runner)
    item = {
        "type": "commandExecution",
        "id": "c",
        "command": "ls",
        "status": "inProgress",
    }
    assert _translate(runner, state, *_item("item/started", item, OTHER_THREAD)) == []
    assert state.open_items == {}


def test_replay_of_the_probe_turn_closes_the_open_command() -> None:
    """codex-cli 0.169.0 capture: a fileChange, a `sleep 30` the probe
    interrupted (never item/completed, D19), and one config warning sent
    twice (configWarning, then warning)."""
    text = (FIXTURES / "live-2-turns-experimental-0.169.0.jsonl").read_text()
    lines = [json.loads(x) for x in text.splitlines()]
    end = next(i for i, x in enumerate(lines) if "TURN 2" in json.dumps(x))
    runner = _bare_runner()
    state = _app_state(runner, "01a1e886-cabf-71e2-b75f-163eeec13b3f")
    events: list[Any] = []
    for line in lines[:end]:
        msg = line.get("msg", {})
        if line.get("dir") == "s2c" and "id" not in msg and "method" in msg:
            events += _translate(runner, state, msg["method"], msg.get("params") or {})
    rows = _rows(events)
    warnings = [r for r in rows if r[1] == "warning"]
    assert len(warnings) == 1
    assert "`project` is ignored" in warnings[0][2]
    assert (
        "completed",
        "file_change",
        "/tmp/codexplan-scratch/probe.txt",
        True,
    ) in [r[:4] for r in rows]
    command = [r for r in rows if r[1] == "command"]
    assert [(r[0], r[2], r[3]) for r in command] == [
        ("started", "/usr/bin/zsh -lc 'sleep 30'", None),
        ("completed", "/usr/bin/zsh -lc 'sleep 30'", False),
    ]
    assert command[1][4] == {"status": "cancelled"}
    done = _completed(events)
    assert not done.ok
    assert done.error == "Codex turn interrupted"


def _cmd_started(cid: str = "c1") -> dict:
    return {
        "method": "item/started",
        "params": {
            "threadId": THREAD,
            "turnId": "turn-1",
            "item": {
                "type": "commandExecution",
                "id": cid,
                "command": "sleep 60",
                "status": "inProgress",
            },
        },
    }


@pytest.mark.anyio
async def test_failed_turn_closes_open_rows_and_carries_the_typed_error(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(
        tmp_path,
        notifications=[_cmd_started()],
        status="failed",
        turn_error={
            "message": "model gpt-x not found",
            "codexErrorInfo": "modelNotFound",
        },
    )
    events = await _run(_runner(fake))
    done = _completed(events)
    rows = [e for e in events if isinstance(e, ActionEvent) and e.action.id == "c1"]
    assert [(r.phase, r.ok) for r in rows] == [("started", None), ("completed", False)]
    assert rows[1].action.detail == {"status": "failed"}
    assert events.index(rows[1]) == len(events) - 2  # just before the final
    assert done.usage == {"codex_error_info": "modelNotFound"}
    hint = _run_error_hint(done.error or "", usage=done.usage, close_reason=None)
    assert hint == CODEX_ERROR_INFO_HINTS["modelNotFound"]


@pytest.mark.anyio
async def test_final_answer_is_kept_when_the_turn_then_fails(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(
        tmp_path,
        messages=[["the answer", "final_answer"]],
        status="failed",
        turn_error={
            "message": "stream disconnected",
            "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": None}},
        },
    )
    done = _completed(await _run(_runner(fake)))
    assert not done.ok
    assert done.answer == "the answer"
    assert done.error == "stream disconnected"
    assert done.usage == {"codex_error_info": "responseStreamDisconnected"}


_SAFE = EngineRunOptions(permission_mode="safe")


@pytest.mark.anyio
async def test_warnings_dedupe_redact_and_wait_for_the_started_event(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    text = "Codex is ignoring 1 unrecognized configuration setting."
    fake = Fake(
        tmp_path,
        after_init=[{"method": "configWarning", "params": {"summary": text}}],
        notifications=[
            {"method": "warning", "params": {"threadId": THREAD, "message": text}},
            {
                "method": "deprecationNotice",
                "params": {
                    "summary": "see https://example.com/d?token=s3cret",
                    "details": None,
                },
            },
        ],
    )
    with capture_logs() as logs:
        events = await _run(_runner(fake), options=_SAFE)
    assert isinstance(events[0], StartedEvent)
    titles = [e.action.title for e in events if isinstance(e, ActionEvent)]
    assert titles[0] == f"\N{WARNING SIGN}\N{VARIATION SELECTOR-16} {text}"
    assert len(titles) == 2
    assert "s3cret" not in titles[1]
    assert any(e["event"] == "codex.app_server.deprecation" for e in logs)


@pytest.mark.anyio
async def test_untrusted_config_warning_gives_the_exec_note_and_footer_flag(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    summary = (
        "Project-local config, hooks, and exec policies are disabled in the "
        f"following folders until the project is trusted.\n 1. {project}/.codex\n"
    )
    fake = Fake(
        tmp_path,
        after_init=[{"method": "configWarning", "params": {"summary": summary}}],
    )
    events = await _run(_runner(fake), options=_SAFE)
    assert isinstance(events[0], StartedEvent)
    rows = [e for e in events if isinstance(e, ActionEvent)]
    assert [r.action.title for r in rows] == [_UNTRUSTED_PROJECT_NOTE]
    assert _completed(events).usage == {"project_config_untrusted": True}


def _retry(message: str) -> dict:
    return {
        "method": "error",
        "params": {
            "threadId": THREAD,
            "turnId": "turn-1",
            "willRetry": True,
            "error": {"message": message, "codexErrorInfo": "serverOverloaded"},
        },
    }


@pytest.mark.anyio
async def test_retrying_error_is_a_reconnect_row(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(
        tmp_path,
        notifications=[_retry("Reconnecting... 1/5"), _retry("Reconnecting... 2/5")],
    )
    events = await _run(_runner(fake), options=_SAFE)
    rows = [e for e in events if isinstance(e, ActionEvent)]
    assert [(r.phase, r.action.id, r.action.kind, r.action.title) for r in rows] == [
        ("started", "codex.reconnect", "note", "Reconnecting... 1/5"),
        ("updated", "codex.reconnect", "note", "Reconnecting... 2/5"),
    ]
    assert rows[0].action.detail["codex_error_info"] == "serverOverloaded"
    assert _completed(events).ok


@pytest.mark.anyio
async def test_server_requests_mid_turn_add_one_row_each_and_one_reply(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(
        tmp_path,
        notifications=[
            {
                "id": 7,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": THREAD, "turnId": "turn-1", "itemId": "c"},
            },
            {"id": 8, "method": "item/tool/requestUserInput", "params": {}},
        ],
    )
    # #963: attended approvals become cards (test_codex_app_server_approvals);
    # an unattended run's are declined by the peer with the #835 🔒 row.
    options = EngineRunOptions(permission_mode="safe", unattended_trigger="cron:c")
    events = await _run(_runner(fake), options=options)
    titles = [e.action.title for e in events if isinstance(e, ActionEvent)]
    assert titles == [
        "\N{LOCK} Unattended run — declined a command request: nobody to approve it",
        "\N{WARNING SIGN}\N{VARIATION SELECTOR-16} Codex sent a request Untether"
        " doesn't support yet (`item/tool/requestUserInput`); declined",
    ]
    assert _completed(events).ok
    replies = [
        m for m in fake.received() if m.get("id") in (7, 8) and "method" not in m
    ]
    assert len(replies) == 2
    assert replies[0] == {"id": 7, "result": {"decision": "decline"}}
    assert replies[1]["error"]["code"] == -32601


@pytest.mark.anyio
async def test_moderated_answer_is_shown_and_logged_once(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    moderated = {"moderated": True, "display": {"mode": "hide", "version": 1}}
    msg = {
        "method": "item/completed",
        "params": {
            "threadId": THREAD,
            "turnId": "turn-1",
            "item": {
                "type": "agentMessage",
                "id": "mm",
                "text": "kept as sent",
                "phase": "final_answer",
                "moderation": moderated,
            },
        },
    }
    fake = Fake(tmp_path, notifications=[msg, msg], messages=[])
    with capture_logs() as logs:
        done = _completed(await _run(_runner(fake), options=_SAFE))
    assert done.ok
    assert done.answer == "kept as sent"
    moderation = [e for e in logs if e["event"] == "codex.app_server.moderated"]
    assert [e["mode"] for e in moderation] == ["hide"]


# --- 962-4: /cancel via turn/interrupt, resume ------------------------------------


async def _cancel_when(
    runner: Any,
    when: Any = None,
    *,
    after_s: float | None = None,
    resume: ResumeToken | None = None,
) -> list[Any]:
    """Consume a run like the bridge and cancel it (its task group) when
    ``when(evt)`` holds or after ``after_s``."""
    events: list[Any] = []
    with apply_run_options(_SAFE), anyio.fail_after(20):
        async with anyio.create_task_group() as tg:

            async def consume() -> None:
                async for evt in runner.run("hello", resume):
                    events.append(evt)
                    if when is not None and when(evt):
                        tg.cancel_scope.cancel()

            tg.start_soon(consume)
            if after_s is not None:
                await anyio.sleep(after_s)
                tg.cancel_scope.cancel()
    return events


def _command_started(evt: Any) -> bool:
    return (
        isinstance(evt, ActionEvent)
        and evt.phase == "started"
        and evt.action.kind == "command"
    )


def _interrupt_logs(logs: list[dict]) -> list[dict]:
    return [e for e in logs if e["event"] == "codex.app_server.interrupt"]


def _teardown_exit(logs: list[dict]) -> dict:
    exits = [
        e
        for e in logs
        if e["event"] == "subprocess.exit" and e.get("reason") == "teardown"
    ]
    assert len(exits) == 1
    return exits[0]


@pytest.mark.anyio
async def test_cancel_interrupts_the_turn_and_codex_exits_by_itself(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, hold_turn=True, notifications=[_cmd_started()])
    with capture_logs() as logs:
        events = await _cancel_when(_runner(fake), _command_started)
    assert not any(isinstance(e, CompletedEvent) for e in events)
    assert fake.requests("turn/interrupt") == [{"threadId": THREAD, "turnId": "turn-1"}]
    [log] = _interrupt_logs(logs)
    assert log["ok"] is True
    assert log["open_items_closed"] == 1
    assert log["elapsed_s"] < 3
    # stdin closed, the process exited 0 on its own: no SIGTERM.
    exit_log = _teardown_exit(logs)
    assert exit_log["rc"] == 0
    assert exit_log["killed"] is False


@pytest.mark.anyio
async def test_cancel_falls_back_to_teardown_when_the_turn_never_ends(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(
        tmp_path, hold_turn=True, ignore_interrupt=True, notifications=[_cmd_started()]
    )
    with capture_logs() as logs:
        await _cancel_when(_runner(fake, interrupt_bound_s=0.3), _command_started)
    assert len(fake.requests("turn/interrupt")) == 1
    [log] = _interrupt_logs(logs)
    assert log["ok"] is False
    assert 0.25 <= log["elapsed_s"] < 2
    assert _teardown_exit(logs)["rc"] != 0  # SIGTERM


@pytest.mark.anyio
async def test_cancel_during_the_handshake_is_a_plain_teardown(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, hang_after_init=True)
    with capture_logs() as logs:
        events = await _cancel_when(_runner(fake), after_s=0.5)
    assert events == []
    assert fake.requests("turn/interrupt") == []
    assert _interrupt_logs(logs) == []
    assert _teardown_exit(logs)["rc"] != 0


@pytest.mark.anyio
async def test_cancel_after_the_final_sends_no_interrupt(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, linger=True)
    with capture_logs() as logs:
        events = await _cancel_when(
            _runner(fake), lambda e: isinstance(e, CompletedEvent)
        )
    assert _completed(events).ok
    assert fake.requests("turn/interrupt") == []
    assert _interrupt_logs(logs) == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param({"interrupt_status": "completed"}, id="finish-wins-race"),
        pytest.param({"interrupt_error": True}, id="interrupt-error-ignored"),
    ],
)
async def test_interrupt_ends_on_any_turn_completed(
    tmp_path: Path, project: Path, gate_calls: list[str], scenario: dict
) -> None:
    fake = Fake(tmp_path, hold_turn=True, notifications=[_cmd_started()], **scenario)
    with capture_logs() as logs:
        events = await _cancel_when(_runner(fake), _command_started)
    assert not any(isinstance(e, CompletedEvent) for e in events)
    [log] = _interrupt_logs(logs)
    assert log["ok"] is True
    assert _teardown_exit(logs)["rc"] == 0


@pytest.mark.anyio
async def test_a_line_part_read_before_the_cancel_is_still_parsed(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    partial = json.dumps(
        {"method": "warning", "params": {"threadId": THREAD, "message": "late"}}
    )
    fake = Fake(
        tmp_path, hold_turn=True, notifications=[_cmd_started()], partial=partial
    )
    with capture_logs() as logs:
        await _cancel_when(_runner(fake), _command_started)
    [log] = _interrupt_logs(logs)
    assert log["ok"] is True
    assert not any(e["event"] == "codex.rpc.bad_line" for e in logs)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "message",
    [
        f"no rollout found for thread id {THREAD}",
        f"invalid session id: {THREAD}",
    ],
)
async def test_resume_errors_keep_codex_text_and_clear_the_session(
    tmp_path: Path, project: Path, gate_calls: list[str], message: str
) -> None:
    fake = Fake(tmp_path, resume_error=True, resume_error_message=message)
    token = ResumeToken(engine="codex", value=THREAD)
    done = _completed(await _run(_runner(fake), resume=token, options=_SAFE))
    assert not done.ok
    assert message in (done.error or "")
    assert _resume_failure_clears_session("codex", done.usage, done.error)


@pytest.mark.anyio
async def test_full_auto_resume_runs_read_only_where_codex_would_save_trust(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, config={"sandbox_mode": "workspace-write"})
    token = ResumeToken(engine="codex", value=THREAD)
    events = await _run(_runner(fake), resume=token)
    assert _completed(events).ok
    assert fake.requests("thread/resume")[0]["sandbox"] == "read-only"


@pytest.mark.anyio
async def test_resume_lines_cross_transports(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    exec_runner = CodexRunner(codex_cmd="codex", extra_args=[])
    app_runner = _bare_runner()
    token = ResumeToken(engine="codex", value=THREAD)
    assert app_runner.extract_resume(exec_runner.format_resume(token)) == token
    assert exec_runner.extract_resume(app_runner.format_resume(token)) == token
    fake = Fake(tmp_path)
    events = await _run(_runner(fake), resume=token, options=_SAFE)
    assert fake.requests("thread/resume")[0]["threadId"] == THREAD
    assert _completed(events).resume == token


@pytest.mark.anyio
async def test_cancel_while_the_bridge_is_busy_still_interrupts(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    """The cancel lands in the bridge (e.g. a stalled Telegram send), not in
    the runner's read: the generator is then closed at its yield (#614)."""
    fake = Fake(tmp_path, hold_turn=True, notifications=[_cmd_started()])
    with capture_logs() as logs, apply_run_options(_SAFE), anyio.fail_after(20):
        events = _runner(fake).run("hello", None)
        with anyio.CancelScope() as scope:
            try:
                async for evt in events:
                    if _command_started(evt):
                        scope.cancel()
                        await anyio.sleep(5)  # cancelled here
            finally:
                await _close_runner_events(events)
    [log] = _interrupt_logs(logs)
    assert log["ok"] is True
    # exited by itself (a GeneratorExit close logs no teardown exit line)
    assert not [e for e in logs if e["event"] == "subprocess.exit" and e["rc"] != 0]


# --- 962-5: % ctx, compaction row, footer tokens (#832) -----------------------

P8 = "live-turn-compact-stable-0.170.0.jsonl"
P8_THREAD = "01a1f210-f3f7-7552-94ed-7056e743e68d"


def _p8_segments() -> tuple[list[dict], list[dict]]:
    """codex-cli 0.170.0 capture (P8): "Reply with the word hi.", then
    ``thread/compact/start`` on the same thread. Server notifications only,
    split at the probe's COMPACT note."""
    lines = [json.loads(x) for x in (FIXTURES / P8).read_text().splitlines()]
    split = next(i for i, x in enumerate(lines) if "COMPACT" in json.dumps(x))

    def notes(part: list[dict]) -> list[dict]:
        return [
            x["msg"]
            for x in part
            if x["dir"] == "s2c" and "id" not in x["msg"] and "method" in x["msg"]
        ]

    return notes(lines[:split]), notes(lines[split:])


def _replay(messages: list[dict]) -> tuple[Any, list[Any]]:
    runner = _bare_runner()
    state = _app_state(runner, P8_THREAD)
    state.model = "gpt-6.1-sol"
    events: list[Any] = []
    for msg in messages:
        events += _translate(runner, state, msg["method"], msg.get("params") or {})
    return state, events


def _telemetry_rows(events: list[Any]) -> list[dict]:
    return [
        e.action.detail
        for e in events
        if isinstance(e, ActionEvent) and e.action.kind == "telemetry"
    ]


def _usage_update(
    last: int, window: int | None, *, total: int = 0, thread: str = THREAD
) -> tuple[str, dict]:
    def breakdown(n: int) -> dict:
        return {"totalTokens": n, "inputTokens": n, "outputTokens": 0}

    return "thread/tokenUsage/updated", {
        "threadId": thread,
        "turnId": "turn-1",
        "tokenUsage": {
            "total": breakdown(total or last),
            "last": breakdown(last),
            "modelContextWindow": window,
        },
    }


def _turn_completed(status: str = "completed", **turn: Any) -> tuple[str, dict]:
    return "turn/completed", {
        "threadId": THREAD,
        "turn": {"id": "turn-1", "status": status, **turn},
    }


def test_p8_turn_gives_ctx_pct_and_exec_footer_keys() -> None:
    turn, _ = _p8_segments()
    _, events = _replay(turn)
    assert _telemetry_rows(events) == [
        {
            "context_pct": 3,  # last.totalTokens 13447 / 475000
            "context_used": 13447,
            "context_window": 475000,
            "model": "gpt-6.1-sol",
        }
    ]
    done = _completed(events)
    assert done.ok and done.answer == "hi"
    usage = done.usage or {}
    assert {k: usage[k] for k in codex_schema.CODEX_USAGE_FIELDS} == {
        "input_tokens": 13442,
        "cached_input_tokens": 7936,
        "output_tokens": 5,
        "cache_write_input_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    assert usage["context"] == {
        "pct": 3,
        "used": 13447,
        "window": 475000,
        "model": "gpt-6.1-sol",
    }
    assert "compaction" not in usage


def test_p8_compaction_row_and_count() -> None:
    _, compact = _p8_segments()
    with capture_logs() as logs:
        state, events = _replay(compact)
    rows = [r for r in _rows(events) if r[1] == "note"]
    assert [(r[0], r[2], r[3]) for r in rows] == [
        ("started", gate._COMPACTING_TITLE, None),
        ("completed", gate._COMPACTED_TITLE, True),
    ]
    assert state.open_items == {}
    done = _completed(events)
    assert done.ok
    assert done.usage["compaction"] == {"count": 1, "trigger": "auto"}
    # the compaction turn's ``last`` (24777, breakdown all zero) is what it reports
    assert done.usage["context"]["pct"] == 5
    counts = [e["count"] for e in logs if e["event"] == "codex.compaction.completed"]
    assert counts == [1]


def test_compaction_wording_matches_claude() -> None:
    from untether.runners import claude

    assert gate._COMPACTING_TITLE == claude._COMPACTING_TITLE
    assert gate._COMPACTED_TITLE == claude._COMPACTED_TITLE


def test_compaction_interrupted_closes_the_row() -> None:
    runner = _bare_runner()
    state = _app_state(runner)
    item = {"type": "contextCompaction", "id": "cc1"}
    _translate(runner, state, *_item("item/started", item))
    events = _translate(runner, state, *_turn_completed("interrupted"))
    closed = [r for r in _rows(events) if r[1] == "note"]
    assert [(r[0], r[2], r[3]) for r in closed] == [
        ("completed", gate._COMPACTING_TITLE, False)
    ]
    assert "compaction" not in (_completed(events).usage or {})


def test_ctx_emitted_only_on_change_and_per_update_window() -> None:
    runner = _bare_runner()
    state = _app_state(runner)
    pcts = []
    for last, window in [
        (1000, 100_000),  # 1%
        (1200, 100_000),  # still 1%: no event
        (5000, 100_000),  # 5%
        (5000, 50_000),  # the window changed: 10%
        (5000, 50_000),  # same: no event
    ]:
        events = _translate(runner, state, *_usage_update(last, window))
        pcts += [d["context_pct"] for d in _telemetry_rows(events)]
    assert pcts == [1, 5, 10]


@pytest.mark.parametrize("window", [None, 0])
def test_no_ctx_without_a_window(window: int | None) -> None:
    runner = _bare_runner()
    state = _app_state(runner)
    assert _translate(runner, state, *_usage_update(5000, window)) == []
    usage = _completed(_translate(runner, state, *_turn_completed())).usage or {}
    assert "context" not in usage
    assert usage["input_tokens"] == 5000  # footer tokens are still known


def test_ctx_clamped_and_nothing_before_the_first_update() -> None:
    runner = _bare_runner()
    state = _app_state(runner)
    assert _completed(_translate(runner, state, *_turn_completed())).usage is None
    state = _app_state(runner)
    events = _translate(runner, state, *_usage_update(900, 600))
    assert _telemetry_rows(events)[0]["context_pct"] == 100


def test_subagent_usage_update_is_ignored() -> None:
    runner = _bare_runner()
    state = _app_state(runner)
    update = _usage_update(5000, 100_000, thread=OTHER_THREAD)
    assert _translate(runner, state, *update) == []
    assert state.tokens is None and state.context is None


def test_failed_turn_keeps_usage_and_error_info() -> None:
    runner = _bare_runner()
    state = _app_state(runner)
    _translate(runner, state, *_usage_update(5000, 100_000, total=9000))
    error = {"message": "boom", "codexErrorInfo": "usageLimitExceeded"}
    events = _translate(runner, state, *_turn_completed("failed", error=error))
    usage = _completed(events).usage or {}
    assert usage["codex_error_info"] == "usageLimitExceeded"
    assert usage["input_tokens"] == 9000  # ``total``, not ``last``
    assert usage["context"]["pct"] == 5
