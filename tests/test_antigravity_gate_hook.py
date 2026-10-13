"""The agy gate hook command (#980): ``untether.antigravity.gate_hook``.

Runs the REAL plugin command line through ``sh -c`` (as agy does) against a
throwaway Unix socket server, so the shell guard, the interpreter hand-off
(``$UNTETHER_AGY_GATE_PY -I -m …``) and the fail-closed matrix (13 §4) are
all exercised.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.antigravity_gate_support import FakeGateServer, short_dir
from untether.antigravity import gate_hook, gate_plugin

ASK_PAYLOAD = {
    "conversationId": "c1",
    "stepIdx": 2,
    "toolCall": {"name": "ask_question", "args": {"questions": []}},
}


def _run(
    event: str,
    payload: object = ASK_PAYLOAD,
    *,
    env: dict[str, str] | None = None,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    base.update(env or {})
    data = stdin if stdin is not None else json.dumps(payload).encode()
    return subprocess.run(
        ["sh", "-c", gate_plugin.hook_command(event)],
        input=data,
        capture_output=True,
        env=base,
        timeout=30,
        check=False,
    )


def _gate_env(sock: Path, **extra: str) -> dict[str, str]:
    env = {
        gate_hook.ENV_MARKER: "1",
        gate_hook.ENV_SOCK: str(sock),
        gate_hook.ENV_TOKEN: "t" * 64,
        gate_hook.ENV_PY: sys.executable,
        gate_hook.ENV_DEADLINE: "5",
    }
    env.update(extra)
    return env


def _serve(reply: object | None) -> tuple[FakeGateServer, Path]:
    sock = short_dir() / "g.sock"
    raw = None if reply is None else (json.dumps(reply) + "\n").encode()
    return FakeGateServer(sock, raw), sock


def _out(proc: subprocess.CompletedProcess[bytes]) -> object:
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def test_no_marker_exits_without_output() -> None:
    """The user's own agy: the shell guard exits before Python starts."""
    for event in gate_hook.EVENTS:
        proc = _run(event, env={gate_hook.ENV_PY: "/nonexistent/python"})
        assert proc.returncode == 0
        assert proc.stdout == b"" and proc.stderr == b""


@pytest.mark.parametrize("python", ["/nonexistent/python", "", "/etc/hostname"])
def test_missing_interpreter_denies_in_the_shell_guard(python: str) -> None:
    """Security review 3 M3: with the marker set but Untether's interpreter
    missing (or not executable), the shell guard itself prints a deny for a
    tool — never a failed hook with no decision. Other events go on."""
    env = {gate_hook.ENV_MARKER: "1", gate_hook.ENV_PY: python}
    proc = _run("PreToolUse", env=env)
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {
        "decision": "deny",
        "reason": gate_plugin.GUARD_DENY_REASON,
    }
    for event in ("PreInvocation", "Stop"):
        other = _run(event, env=env)
        assert other.returncode == 0 and other.stdout == b""


def test_round_trip_sends_one_line_with_token_and_payload() -> None:
    server, sock = _serve({"v": 1, "out": {}})
    try:
        proc = _run("PreToolUse", env=_gate_env(sock))
    finally:
        server.close()
    assert _out(proc) is None  # empty = no decision (P-H1)
    (request,) = server.requests
    assert request["v"] == 1 and request["event"] == "PreToolUse"
    assert request["token"] == "t" * 64
    assert request["payload"] == ASK_PAYLOAD
    assert isinstance(request["pid"], int) and isinstance(request["ppid"], int)


def test_protocol_mismatch_denies() -> None:
    proc = _run(
        "PreToolUse", env={gate_hook.ENV_MARKER: "2", gate_hook.ENV_PY: sys.executable}
    )
    out = _out(proc)
    assert out == {"decision": "deny", "reason": gate_hook.MISMATCH}
    assert (
        _out(
            _run(
                "PreInvocation",
                env={gate_hook.ENV_MARKER: "2", gate_hook.ENV_PY: sys.executable},
            )
        )
        == {}
    )


def test_socket_unreachable_denies(tmp_path: Path) -> None:
    env = _gate_env(short_dir() / "missing.sock")
    assert _out(_run("PreToolUse", env=env)) == {
        "decision": "deny",
        "reason": gate_hook.UNREACHABLE,
    }
    # PreInvocation / Stop have no safety role: fail open.
    assert _out(_run("PreInvocation", env=env)) == {}
    assert _out(_run("Stop", env=env)) == {}


def test_relative_socket_path_denies() -> None:
    env = _gate_env(Path("g.sock"))
    assert _out(_run("PreToolUse", env=env))["decision"] == "deny"  # type: ignore[index]


def test_server_closing_without_reply_denies() -> None:
    server, sock = _serve(None)
    try:
        out = _out(_run("PreToolUse", env=_gate_env(sock)))
    finally:
        server.close()
    assert out == {"decision": "deny", "reason": gate_hook.UNREACHABLE}


def test_malformed_reply_denies() -> None:
    sock = short_dir() / "g.sock"
    server = FakeGateServer(sock, b"not json\n")
    try:
        out = _out(_run("PreToolUse", env=_gate_env(sock)))
    finally:
        server.close()
    assert out == {"decision": "deny", "reason": gate_hook.UNREACHABLE}


def test_oversize_payload_denies() -> None:
    big = b"x" * (gate_hook.MAX_PAYLOAD_BYTES + 1)
    out = _out(_run("PreToolUse", env=_gate_env(short_dir() / "g.sock"), stdin=big))
    assert out == {"decision": "deny", "reason": gate_hook.TOO_LARGE}


def test_unreadable_payload_denies() -> None:
    out = _out(_run("PreToolUse", env=_gate_env(short_dir() / "g.sock"), stdin=b"{"))
    assert out == {"decision": "deny", "reason": gate_hook.BAD_PAYLOAD}


@pytest.mark.parametrize(
    "stdin",
    [
        b'{"toolCall":{"name":"run_command","args":{"CommandLine":"ls",'
        b'"CommandLine":"rm -rf /"}}}',
        b'{"toolCall":{"name":"view_file"},"toolCall":{"name":"run_command"}}',
        # Go matches field names case-insensitively: these are one field.
        b'{"toolCall":{"name":"run_command","args":{"CommandLine":"ls",'
        b'"commandline":"rm -rf /"}}}',
        b'{"toolCall":{"name":"run_command","args":{"commandLINE":"rm -rf /",'
        b'"CommandLine":"ls"}}}',
    ],
)
def test_duplicate_json_keys_deny(stdin: bytes) -> None:
    """Security review (parser differential): Python keeps the last of two
    equal keys and agy's parser may keep another, so such a payload never
    reaches the gate's policy — and the server is never asked."""
    server, sock = _serve({"v": 1, "out": {"decision": "allow"}})
    try:
        out = _out(_run("PreToolUse", env=_gate_env(sock), stdin=stdin))
    finally:
        server.close()
    assert out == {"decision": "deny", "reason": gate_hook.DUPLICATE_KEYS}
    assert server.requests == []


@pytest.mark.parametrize("decision", ["ask", "force_ask", "deny_unless_prior_grant", 7])
def test_never_emits_ask_or_force_ask(decision: object) -> None:
    """D22.3: under bypass agy approves an ``ask`` — never print one."""
    server, sock = _serve({"v": 1, "out": {"decision": decision}})
    try:
        out = _out(_run("PreToolUse", env=_gate_env(sock)))
    finally:
        server.close()
    assert out == {"decision": "deny", "reason": gate_hook.BAD_REPLY}


def test_strips_permission_overrides_and_overwrite() -> None:
    reply = {
        "v": 1,
        "out": {
            "decision": "allow",
            "permissionOverrides": {"x": 1},
            "overwrite": {"CommandLine": "rm -rf /"},
        },
    }
    server, sock = _serve(reply)
    try:
        out = _out(_run("PreToolUse", env=_gate_env(sock)))
    finally:
        server.close()
    assert out == {"decision": "allow"}


def test_deny_reason_capped() -> None:
    server, sock = _serve({"v": 1, "out": {"decision": "deny", "reason": "r" * 5000}})
    try:
        out = _out(_run("PreToolUse", env=_gate_env(sock)))
    finally:
        server.close()
    assert out == {"decision": "deny", "reason": "r" * gate_hook.MAX_REASON_CHARS}


def test_preinvocation_injects_only_user_messages_capped() -> None:
    steps = [{"userMessage": f"m{i}", "ephemeralMessage": "x"} for i in range(12)]
    steps.insert(0, {"userMessage": "y" * 9000})
    server, sock = _serve({"v": 1, "out": {"injectSteps": steps, "continue": True}})
    try:
        out = _out(_run("PreInvocation", {"conversationId": "c1"}, env=_gate_env(sock)))
    finally:
        server.close()
    assert isinstance(out, dict) and set(out) == {"injectSteps"}
    injected = out["injectSteps"]
    assert len(injected) == gate_hook.MAX_INJECT_STEPS
    assert injected[0] == {"userMessage": "y" * gate_hook.MAX_INJECT_CHARS}
    assert all(set(step) == {"userMessage"} for step in injected)


def test_preinvocation_failure_returns_empty() -> None:
    server, sock = _serve(None)
    try:
        out = _out(_run("PreInvocation", {"conversationId": "c1"}, env=_gate_env(sock)))
    finally:
        server.close()
    assert out == {}


def test_stop_always_empty() -> None:
    server, sock = _serve({"v": 1, "out": {"continue": True, "decision": "block"}})
    try:
        out = _out(
            _run(
                "Stop",
                {"conversationId": "c1", "fullyIdle": False},
                env=_gate_env(sock),
            )
        )
    finally:
        server.close()
    assert out == {}
    assert server.requests[0]["payload"]["fullyIdle"] is False


def test_unknown_event_prints_nothing() -> None:
    assert gate_hook.run("PostToolUse") is None
    assert gate_hook.main(["gate_hook", "PostToolUse"]) == 0


def test_gate_hook_imports_stdlib_only() -> None:
    """Runs once per tool call: nothing third-party may load (13 §3)."""
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-X",
            "importtime",
            "-m",
            "untether.antigravity.gate_hook",
            "Nope",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0
    modules = {
        line.rsplit("|", 1)[-1].strip()
        for line in proc.stderr.splitlines()
        if line.startswith("import time:") and "|" in line
    }
    top = {m.split(".")[0] for m in modules if m}
    # Site machinery an editable / pipx install brings along is not imported
    # by the hook itself.
    site_bits = {
        "untether",
        "_distutils_hack",
        "_virtualenv",
        "sitecustomize",
        "usercustomize",
        "apport_python_hook",  # Debian/Ubuntu system sitecustomize
        "imported package",  # the header line of -X importtime
    }
    third_party = {
        m
        for m in top - set(sys.stdlib_module_names) - site_bits
        if not m.startswith("__editable__")
    }
    assert not third_party, sorted(third_party)
    untether_modules = {m for m in modules if m.startswith("untether")}
    assert untether_modules <= {
        "untether",
        "untether.antigravity",
        "untether.antigravity.gate_hook",
    }, sorted(untether_modules)
