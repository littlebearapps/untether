"""``untether agy-gate status --canary``: do the hooks *run*? (13 §6, m2).

``agy -p /hooks`` proves the plugin is loaded, not that its hooks execute
(macOS 1.2.x loaded hooks that never ran). The canary sends one trivial turn
(≈ 13 k tokens) in an empty temp folder with a gate socket of its own and
requires a ``PreInvocation`` request on it. Manual only: never per run and
never per agy version (agy self-updates almost daily).

Not checked here, re-probe on a new agy minor version (security review 3
M3, ``docs/how-to/security.md``): what agy does when the ``PreToolUse`` hook
fails without a decision. agy 1.3.4 (2026-10-13, under
``--dangerously-skip-permissions``) blocks the tool for exit 1, exit 2, exit
127 (missing interpreter), SIGKILL, non-JSON and truncated JSON output —
the step ends ``TOOL_ERROR`` "JSON hook … failed" / "failed to unmarshal
result from hook". Only an empty output with exit 0 means "no decision"
(runs). The plugin's shell guard denies on its own when the interpreter is
missing; the Python hook prints a deny for every failure it can catch.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

import anyio

from ..events import EventFactory
from . import gate_hook
from .gate_server import GateRun

CANARY_PROMPT = "Reply with the single word OK."
CANARY_TIMEOUT_S = 120.0


@dataclass(frozen=True, slots=True)
class CanaryOutcome:
    ok: bool
    summary: str


async def run_canary(agy_cmd: str) -> CanaryOutcome:
    folder = tempfile.mkdtemp(prefix="untether-agy-canary-")
    gate = GateRun(config_path=None, factory=EventFactory("antigravity"))
    try:
        await gate.open()
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(gate_hook.ENV_MARKER)
        }
        env.update(gate.env())
        env.setdefault("NO_COLOR", "1")
        argv = [
            agy_cmd,
            "-p",
            CANARY_PROMPT,
            "--output-format",
            "stream-json",
            "--mode=accept-edits",
        ]
        send, receive = anyio.create_memory_object_stream[object](16)
        rc: int | None = None
        async with anyio.create_task_group() as tg:
            tg.start_soon(gate.serve, send)

            async def drain() -> None:
                async with receive:
                    async for _ in receive:
                        pass

            tg.start_soon(drain)
            with anyio.move_on_after(CANARY_TIMEOUT_S):
                proc = await anyio.run_process(
                    argv,
                    cwd=folder,
                    env=env,
                    check=False,
                    stdin=subprocess.DEVNULL,
                )
                rc = proc.returncode
            gate.stop_emitting()
            tg.cancel_scope.cancel()
    except OSError as exc:
        return CanaryOutcome(False, f"couldn't run agy ({exc.__class__.__name__})")
    finally:
        await gate.aclose()
        shutil.rmtree(folder, ignore_errors=True)
    if rc is None:
        return CanaryOutcome(False, f"agy didn't finish in {CANARY_TIMEOUT_S:.0f} s")
    if not gate.preinvocation_seen:
        return CanaryOutcome(
            False,
            f"agy ran (rc {rc}) but no hook reached Untether — the plugin is "
            "loaded but its hooks don't run here",
        )
    return CanaryOutcome(True, f"hooks ran and reached Untether (agy rc {rc})")
