"""The hook command of Untether's agy gate plugin (#980, #981).

agy runs ``sh -c`` on the command line in the plugin's ``hooks.json``::

    [ -n "$UNTETHER_AGY_GATE" ] || exit 0; exec "$UNTETHER_AGY_GATE_PY" -I -m untether.antigravity.gate_hook <Event>

so the user's own agy sessions (no marker in the environment) never start
Python, and an agy run Untether started reaches this module under the
interpreter of the Untether instance that started it. One invocation:

1. read the hook payload from stdin (capped);
2. connect to the run's Unix socket named in ``UNTETHER_AGY_GATE_SOCK`` and
   send one line ``{"v", "token", "event", "pid", "ppid", "payload"}``;
3. read one line back and print exactly what agy may see.

Only ``PreToolUse`` has a safety role, so only it fails **closed**: anything
that goes wrong (no socket, a bad reply, a timeout) prints a ``deny``.
``PreInvocation`` and ``Stop`` fail open (``{}``: nothing injected, the stop
goes ahead). The server's answer for a tool is ``allow``, ``deny`` or no
decision at all (empty output: agy's own permission check decides, P-H1);
``ask`` / ``force_ask`` / ``permissionOverrides`` / ``overwrite`` are never
printed (D22.3: under bypass agy approves an ``ask``).

Standard library only, and nothing from Untether: this runs once per tool
call (``tests/test_antigravity_gate_hook.py`` pins the imports).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time

PROTOCOL = "1"
EVENTS = ("PreToolUse", "PreInvocation", "Stop")

ENV_MARKER = "UNTETHER_AGY_GATE"
ENV_SOCK = "UNTETHER_AGY_GATE_SOCK"
ENV_TOKEN = "UNTETHER_AGY_GATE_TOKEN"
ENV_PY = "UNTETHER_AGY_GATE_PY"
ENV_DEADLINE = "UNTETHER_AGY_GATE_DEADLINE_S"

MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_REPLY_BYTES = 1024 * 1024
MAX_REASON_CHARS = 2000
MAX_INJECT_STEPS = 8
MAX_INJECT_CHARS = 8000
CONNECT_TIMEOUT_S = 2.0
SHORT_REPLY_TIMEOUT_S = 10.0
DEFAULT_DEADLINE_S = 900.0
# The server answers a held tool at its own deadline; the hook waits a
# little longer so the server's answer, not this timeout, decides. agy's own
# hook timeout (``wait + 60`` in hooks.json) is longer still.
DEADLINE_SLACK_S = 30.0
MAX_DEADLINE_S = 3600.0

UNREACHABLE = "Untether isn't reachable, so the tool was blocked."
MISMATCH = (
    "Untether's gate protocol doesn't match this agy plugin, so the tool was "
    "blocked. Run `untether agy-gate install` on the host."
)
TOO_LARGE = "The tool call is too large for Untether's gate, so it was blocked."
BAD_PAYLOAD = "Untether's gate couldn't read this tool call, so it was blocked."
DUPLICATE_KEYS = (
    "This tool call names the same field twice, so Untether can't tell which "
    "one agy would use and blocked it. Call the tool again with each field once."
)


class _DuplicateKey(ValueError):
    pass


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Python keeps the last of two equal keys; agy's own parser may not, and
    Go's JSON decoder also matches field names case-insensitively, so
    ``CommandLine`` and ``commandline`` in one object are the same field to
    agy. A payload that says two things never reaches the gate's policy."""
    out: dict[str, object] = {}
    folded: set[str] = set()
    for key, value in pairs:
        fold = key.casefold()
        if key in out or fold in folded:
            raise _DuplicateKey(key)
        folded.add(fold)
        out[key] = value
    return out


BAD_REPLY = (
    "Untether's gate gave an answer it doesn't understand, so the tool was blocked."
)


def _deny(reason: str) -> dict[str, str]:
    return {"decision": "deny", "reason": reason[:MAX_REASON_CHARS]}


def _failed(event: str, reason: str) -> dict[str, object] | None:
    """What agy sees when the round trip didn't produce a usable answer."""
    if event == "PreToolUse":
        return _deny(reason)
    return {}


def _deadline_s() -> float:
    raw = os.environ.get(ENV_DEADLINE, "")
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_DEADLINE_S
    if not 1.0 <= value <= MAX_DEADLINE_S:
        return DEFAULT_DEADLINE_S
    return value


def _read_stdin() -> bytes | None:
    """The payload, or None when it is over the cap."""
    data = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
    if len(data) > MAX_PAYLOAD_BYTES:
        return None
    return data


def _recv_line(sock: socket.socket, deadline: float) -> bytes | None:
    buf = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        sock.settimeout(remaining)
        chunk = sock.recv(65536)
        if not chunk:
            return None
        buf.extend(chunk)
        newline = buf.find(b"\n")
        if newline >= 0:
            return bytes(buf[:newline])
        if len(buf) > MAX_REPLY_BYTES:
            return None


def _round_trip(event: str, payload: object) -> dict[str, object] | None:
    """Send the request; the server's ``out`` object, or None on failure."""
    path = os.environ.get(ENV_SOCK, "")
    token = os.environ.get(ENV_TOKEN, "")
    if not path or not os.path.isabs(path) or not token:
        return None
    request = {
        "v": int(PROTOCOL),
        "token": token,
        "event": event,
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "payload": payload,
    }
    line = (json.dumps(request, ensure_ascii=False) + "\n").encode()
    if event == "PreToolUse":
        wait_s = _deadline_s() + DEADLINE_SLACK_S
    else:
        wait_s = SHORT_REPLY_TIMEOUT_S
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(CONNECT_TIMEOUT_S)
        sock.connect(path)
        sock.settimeout(CONNECT_TIMEOUT_S)
        sock.sendall(line)
        raw = _recv_line(sock, time.monotonic() + wait_s)
    except OSError:
        return None
    finally:
        sock.close()
    if raw is None:
        return None
    try:
        reply = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(reply, dict):
        return None
    out = reply.get("out")
    return out if isinstance(out, dict) else None


def _validated(event: str, out: dict[str, object]) -> dict[str, object] | None:
    """Only the keys agy may see. ``None``: print nothing (no decision)."""
    if event == "PreToolUse":
        if not out:
            return None
        decision = out.get("decision")
        if decision == "allow":
            return {"decision": "allow"}
        if decision == "deny":
            reason = out.get("reason")
            text = reason if isinstance(reason, str) and reason else "Denied."
            return _deny(text)
        return _deny(BAD_REPLY)
    if event == "PreInvocation":
        steps = out.get("injectSteps")
        if not isinstance(steps, list) or not steps:
            return {}
        clean: list[dict[str, str]] = []
        for step in steps[:MAX_INJECT_STEPS]:
            message = step.get("userMessage") if isinstance(step, dict) else None
            if isinstance(message, str) and message:
                clean.append({"userMessage": message[:MAX_INJECT_CHARS]})
        return {"injectSteps": clean} if clean else {}
    return {}


def run(event: str) -> dict[str, object] | None:
    """The object to print for *event* (None: print nothing)."""
    if event not in EVENTS:
        return None
    if os.environ.get(ENV_MARKER) != PROTOCOL:
        return _failed(event, MISMATCH)
    data = _read_stdin()
    if data is None:
        return _failed(event, TOO_LARGE)
    try:
        payload = (
            json.loads(data, object_pairs_hook=_no_duplicates) if data.strip() else {}
        )
    except _DuplicateKey:
        return _failed(event, DUPLICATE_KEYS)
    except (ValueError, RecursionError):
        return _failed(event, BAD_PAYLOAD)
    out = _round_trip(event, payload)
    if out is None:
        return _failed(event, UNREACHABLE)
    if event == "Stop":
        return {}
    return _validated(event, out)


def main(argv: list[str]) -> int:
    event = argv[1] if len(argv) > 1 else ""
    try:
        result = run(event)
    except Exception:  # noqa: BLE001 — never a traceback where agy reads JSON
        result = _failed(event, UNREACHABLE) if event in EVENTS else None
    if result is not None:
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
