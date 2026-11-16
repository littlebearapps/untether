#!/usr/bin/env python3
"""Deterministic fake ``agy`` (Antigravity CLI) for subprocess tests (#558).

Replays a recorded agy 1.3.x capture so the REAL ``AntigravityRunner``
(spawn -> stdin stream-json -> msgspec decode -> translate) runs end to end
with no network and no Google quota.

Scripts live in ``tests/fixtures/antigravity/<scenario>.script``, one
directive per line, generated from the probe captures' ``.timing`` files so
stdout/stderr interleaving (e.g. stderr *after* the ``result`` line) is
replayed faithfully::

    out:<json line>      write to stdout (flushed)
    err:<text>           write to stderr (flushed)
    sleep:<seconds>      wait (scaled by UNTETHER_FAKE_AGY_TIME_SCALE)
    write:<relpath>      create/overwrite a file under the cwd (planted
                         config tests, phase 02)
    spawn:<seconds>      start ``sleep <seconds>`` in its own session (a
                         background task that escapes agy's process group,
                         #975) and write its pid to
                         ``UNTETHER_FAKE_AGY_CHILD_PIDFILE``
    hook:<Event>:<json>  run the gate plugin's real hook command (#980,
                         see ``_hook``); ``hook:inject_at:<n>`` numbers
                         the ``user_input`` steps an injection adds
    allowed:<json line>  write to stdout only when the last PreToolUse hook
                         answered ``allow`` (Ask me, phase 14)
    denied:<json line>   … only when it answered ``deny`` (or nothing, which
                         agy treats as blocked under a crashed hook);
                         ``__REASON__`` → the deny reason, JSON-escaped
    wait_go:             wait (≤ 10 s) for the file ``UNTETHER_FAKE_AGY_GO``
    rc:<int>             exit code (last line)

``agy -p /<command> …`` (zero-token slash probes, ``run_agy_slash``) prints
a ``command_result`` + ``result`` pair instead of replaying a script: the
data comes from ``UNTETHER_FAKE_AGY_SLASH_DATA`` (JSON) or, for ``/config``,
agy 1.3.1's defaults. ``UNTETHER_FAKE_AGY_SLASH_UNAUTH=1`` answers like a
signed-out agy (stderr ``authentication required``, rc 1).
``UNTETHER_FAKE_AGY_HOLD_S`` holds a slash probe (after the auth error, or
before any output); ``UNTETHER_FAKE_AGY_CHILD_PIDFILE`` makes it spawn a
``sleep 60`` child first and write the child's pid there.

Environment (the ``UNTETHER_`` prefix survives the runner's env filter):

- ``UNTETHER_FAKE_AGY_SCENARIO``   fixture name (default ``ok``)
- ``UNTETHER_FAKE_AGY_SCRIPT``     absolute script path (wins over the name)
- ``UNTETHER_FAKE_AGY_VERSION``    what ``--version`` prints (default 1.3.1)
- ``UNTETHER_FAKE_AGY_RECORD``     write ``{"argv","stdin","env_keys","cwd"}`` here
- ``UNTETHER_FAKE_AGY_TIME_SCALE`` multiply ``sleep:`` lines (default 0)
- ``UNTETHER_FAKE_AGY_HOLD_S``     sleep after the last line before exiting
- ``UNTETHER_FAKE_AGY_IGNORE_TERM`` ignore SIGTERM (a Go binary blocked on an
  OAuth paste may; phase 03's kill escalation must SIGKILL it)

SIGTERM / SIGINT behave like agy 1.3.1: an ``ERROR`` ``"interrupted"`` result
on stdout, ``error: interrupted`` on stderr, rc 1.

Test-only; nothing under ``src/`` imports it.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "antigravity"
_conversation_id = ""


def _out(line: str) -> None:
    global _conversation_id
    try:
        obj = json.loads(line)
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        for key in ("step_update", "result"):
            inner = obj.get(key)
            if isinstance(inner, dict) and inner.get("conversation_id"):
                _conversation_id = inner["conversation_id"]
        if obj.get("conversation_id"):
            _conversation_id = obj["conversation_id"]
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def _interrupted(signum: int, _frame: object) -> None:
    result = {
        "event": "result",
        "result": {
            "conversation_id": _conversation_id,
            "status": "ERROR",
            "response": "",
            "error": "interrupted",
            "duration_seconds": 0.1,
            "num_turns": 1,
        },
    }
    # Raw fds: the signal can land inside ``_out``'s own flush, and a second
    # buffered write there raises "reentrant call inside BufferedWriter".
    os.write(1, ("\n" + json.dumps(result) + "\n").encode())  # end any half line
    os.write(2, b"error: interrupted\n")
    os._exit(1)


_DEFAULT_CONFIG = {
    "agentMode": "",
    "allowNonWorkspaceAccess": False,
    "artifactReviewPolicy": "asks-for-review",
    "model": "",
    "modelProvider": "",
    "permissions": None,
    "toolPermission": "request-review",
}


def _record(argv: list[str], stdin: str) -> None:
    record = os.environ.get("UNTETHER_FAKE_AGY_RECORD")
    if record:
        Path(record).write_text(
            json.dumps(
                {
                    "argv": argv,
                    "stdin": stdin,
                    "env_keys": sorted(os.environ),
                    "cwd": os.getcwd(),
                }
            )
        )


def _slash(argv: list[str]) -> int:
    command = argv[argv.index("-p") + 1].lstrip("/")
    _record(argv, sys.stdin.read() if not sys.stdin.isatty() else "")
    hold = float(os.environ.get("UNTETHER_FAKE_AGY_HOLD_S", "0") or 0)
    pidfile = os.environ.get("UNTETHER_FAKE_AGY_CHILD_PIDFILE")
    if pidfile:  # a descendant the caller's kill must reach (#590)
        import subprocess

        child = subprocess.Popen(["sleep", "60"])
        Path(pidfile).write_text(str(child.pid))
    if os.environ.get("UNTETHER_FAKE_AGY_SLASH_UNAUTH"):
        _err("Error: authentication required. Run 'agy' to log in, then retry.")
        if hold > 0:  # 1.2.14-style: wait for a paste that never comes
            time.sleep(hold)
        return 1
    if hold > 0:  # a hung agy (timeout tests)
        time.sleep(hold)
    raw = os.environ.get("UNTETHER_FAKE_AGY_SLASH_DATA")
    data = json.loads(raw) if raw else {"config": _DEFAULT_CONFIG}
    payload = {"name": command, "data": data}
    _out(json.dumps({"event": "command_result", "command": payload}))
    result = {"conversation_id": "", "status": "SUCCESS", "response": ""}
    _out(json.dumps({"event": "result", "result": {**result, "command": payload}}))
    return 0


_inject_step = 0
# The last PreToolUse hook's answer (``allowed:`` / ``denied:`` lines).
_last_tool_out: dict = {}


def _hook(payload: str) -> None:
    """``hook:<Event>:<payload json>`` (#980): run the REAL command of the
    installed gate plugin for <Event> (``UNTETHER_FAKE_AGY_HOOKS_JSON``)
    through ``sh -c`` with this process's env, as agy does, feeding the
    payload (``__CID__`` → the conversation id) on stdin. Each call is
    appended to ``UNTETHER_FAKE_AGY_HOOK_LOG`` as ``{event, rc, out}``. A
    ``PreInvocation`` answer with ``injectSteps`` emits one ``user_input``
    step per message, numbered from ``inject_at:<n>`` (P-H10)."""
    global _inject_step, _last_tool_out
    import subprocess

    event, _, body = payload.partition(":")
    if event == "inject_at":
        _inject_step = int(body)
        return
    manifest = os.environ.get("UNTETHER_FAKE_AGY_HOOKS_JSON")
    if not manifest:
        return
    groups = json.loads(Path(manifest).read_text())
    commands = [
        hook["command"]
        for group in groups.values()
        for entry in group.get(event, [])
        for hook in entry.get("hooks", [entry])
    ]
    stdin = body.replace("__CID__", _conversation_id).replace(
        "__CWD__", json.dumps(os.getcwd())[1:-1]
    )
    for command in commands:
        proc = subprocess.run(
            ["sh", "-c", command],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )
        out = proc.stdout
        log = os.environ.get("UNTETHER_FAKE_AGY_HOOK_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"event": event, "rc": proc.returncode, "out": out})
                    + "\n"
                )
        if event == "PreToolUse":
            try:
                parsed = json.loads(out) if out.strip() else {}
            except ValueError:
                parsed = {}
            _last_tool_out = parsed if isinstance(parsed, dict) else {}
        if event == "PreInvocation" and out.strip():
            for _step in json.loads(out).get("injectSteps", []):
                _out(
                    json.dumps(
                        {
                            "event": "step_update",
                            "step_update": {
                                "conversation_id": _conversation_id,
                                "step_index": _inject_step,
                                "state": "DONE",
                                "step_type": "user_input",
                            },
                        }
                    )
                )
                _inject_step += 1


def main(argv: list[str]) -> int:
    if "--version" in argv:
        print(os.environ.get("UNTETHER_FAKE_AGY_VERSION", "1.3.1"), flush=True)
        return 0
    if "-p" in argv:
        return _slash(argv)
    if os.environ.get("UNTETHER_FAKE_AGY_IGNORE_TERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGTERM, _interrupted)
    signal.signal(signal.SIGINT, _interrupted)
    stdin = sys.stdin.read()
    _record(argv, stdin)
    script_path = os.environ.get("UNTETHER_FAKE_AGY_SCRIPT")
    if not script_path:
        name = os.environ.get("UNTETHER_FAKE_AGY_SCENARIO", "ok")
        script_path = str(_FIXTURES / f"{name}.script")
    scale = float(os.environ.get("UNTETHER_FAKE_AGY_TIME_SCALE", "0") or 0)
    rc = 0
    for raw in Path(script_path).read_text(encoding="utf-8").splitlines():
        kind, _, payload = raw.partition(":")
        if kind == "out":
            # ``__CWD__``: the project folder (Ask me fixtures, phase 14).
            _out(payload.replace("__CWD__", json.dumps(os.getcwd())[1:-1]))
        elif kind == "err":
            _err(payload)
        elif kind == "sleep":
            delay = float(payload) * scale
            if delay > 0:
                time.sleep(delay)
        elif kind == "write":
            target = Path.cwd() / payload
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"written": time.time_ns()}))
        elif kind == "spawn":
            import subprocess

            child = subprocess.Popen(
                ["sleep", str(float(payload))],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            pidfile = os.environ.get("UNTETHER_FAKE_AGY_CHILD_PIDFILE")
            if pidfile:
                Path(pidfile).write_text(str(child.pid))
        elif kind == "hook":
            _hook(payload)
        elif kind == "allowed":
            if _last_tool_out.get("decision") == "allow":
                _out(payload.replace("__CWD__", json.dumps(os.getcwd())[1:-1]))
        elif kind == "denied":
            if _last_tool_out.get("decision") != "allow":
                reason = str(_last_tool_out.get("reason") or "")
                _out(payload.replace("__REASON__", json.dumps(reason)[1:-1]))
        elif kind == "wait_go":
            # Until the test touches UNTETHER_FAKE_AGY_GO (bounded).
            go = os.environ.get("UNTETHER_FAKE_AGY_GO")
            deadline = time.monotonic() + 10
            while go and not Path(go).exists() and time.monotonic() < deadline:
                time.sleep(0.02)
        elif kind == "rc":
            rc = int(payload)
    hold = float(os.environ.get("UNTETHER_FAKE_AGY_HOLD_S", "0") or 0)
    if hold > 0:
        time.sleep(hold)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
