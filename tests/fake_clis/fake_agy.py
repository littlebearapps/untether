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
    rc:<int>             exit code (last line)

Environment (the ``UNTETHER_`` prefix survives the runner's env filter):

- ``UNTETHER_FAKE_AGY_SCENARIO``   fixture name (default ``ok``)
- ``UNTETHER_FAKE_AGY_SCRIPT``     absolute script path (wins over the name)
- ``UNTETHER_FAKE_AGY_VERSION``    what ``--version`` prints (default 1.3.1)
- ``UNTETHER_FAKE_AGY_RECORD``     write ``{"argv","stdin","env_keys"}`` here
- ``UNTETHER_FAKE_AGY_TIME_SCALE`` multiply ``sleep:`` lines (default 0)
- ``UNTETHER_FAKE_AGY_HOLD_S``     sleep after the last line before exiting

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
    _out(json.dumps(result))
    _err("error: interrupted")
    os._exit(1)


def main(argv: list[str]) -> int:
    if "--version" in argv:
        print(os.environ.get("UNTETHER_FAKE_AGY_VERSION", "1.3.1"), flush=True)
        return 0
    signal.signal(signal.SIGTERM, _interrupted)
    signal.signal(signal.SIGINT, _interrupted)
    stdin = sys.stdin.read()
    record = os.environ.get("UNTETHER_FAKE_AGY_RECORD")
    if record:
        Path(record).write_text(
            json.dumps({"argv": argv, "stdin": stdin, "env_keys": sorted(os.environ)})
        )
    script_path = os.environ.get("UNTETHER_FAKE_AGY_SCRIPT")
    if not script_path:
        name = os.environ.get("UNTETHER_FAKE_AGY_SCENARIO", "ok")
        script_path = str(_FIXTURES / f"{name}.script")
    scale = float(os.environ.get("UNTETHER_FAKE_AGY_TIME_SCALE", "0") or 0)
    rc = 0
    for raw in Path(script_path).read_text(encoding="utf-8").splitlines():
        kind, _, payload = raw.partition(":")
        if kind == "out":
            _out(payload)
        elif kind == "err":
            _err(payload)
        elif kind == "sleep":
            delay = float(payload) * scale
            if delay > 0:
                time.sleep(delay)
        elif kind == "rc":
            rc = int(payload)
    hold = float(os.environ.get("UNTETHER_FAKE_AGY_HOLD_S", "0") or 0)
    if hold > 0:
        time.sleep(hold)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
