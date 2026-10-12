#!/usr/bin/env python3
"""Puppet Claude Code CLI for the golden replay harness (#977, #960).

Unlike ``fake_claude_live.py`` (which runs timed scenarios by itself), this
double has no behaviour of its own. It is a real process — real pid, real
pipes, real exit code — that the test drives over a Unix socket
(``CLAUDE_GOLDEN_PUPPET_SOCK``):

- harness → puppet, one JSON object per line:
  ``{"emit": {...}}`` prints that stream-json line on stdout;
  ``{"exit": rc}`` exits with ``rc``.
- puppet → harness: ``{"hello": {"argv": [...]}}`` once, ``{"stdin": "<line>"}``
  for every line the runner writes to stdin, ``{"eof": true}`` when stdin
  closes.

Every stream line and every wait is therefore a handshake in the test, not a
sleep here. If the socket goes away the puppet exits, so a failed test leaves
no process behind. Test-only.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading

SOCK_ENV = "CLAUDE_GOLDEN_PUPPET_SOCK"


def main() -> None:
    if "--help" in sys.argv or "--version" in sys.argv:
        print("fake claude puppet")
        return
    path = os.environ.get(SOCK_ENV)
    if not path:
        print(f"{SOCK_ENV} is not set", file=sys.stderr)
        sys.exit(2)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    send_lock = threading.Lock()

    def send(obj: dict) -> None:
        data = (json.dumps(obj) + "\n").encode()
        with send_lock:
            try:
                sock.sendall(data)
            except OSError:
                os._exit(0)

    send({"hello": {"argv": sys.argv[1:]}})

    def stdin_reader() -> None:
        for raw in sys.stdin:
            raw = raw.rstrip("\n")
            if raw:
                send({"stdin": raw})
        send({"eof": True})

    threading.Thread(target=stdin_reader, daemon=True).start()

    for line in sock.makefile("r"):
        line = line.strip()
        if not line:
            continue
        cmd = json.loads(line)
        if "emit" in cmd:
            print(json.dumps(cmd["emit"]), flush=True)
        elif "exit" in cmd:
            sys.stdout.flush()
            os._exit(int(cmd["exit"]))
    os._exit(0)


if __name__ == "__main__":
    main()
