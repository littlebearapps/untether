"""#1194: a cancelled run logs ``subprocess.exit`` on every engine.

``manage_subprocess(log_teardown_exit=True)`` (#1062) logs the exit of a
process the teardown stopped, because a cancelled run never reaches its own
exit line. Claude's runner spawns on its own path and dropped it, so its
cancelled runs looked like leaked sessions in spawn/exit audits.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import anyio
import pytest
from structlog.testing import capture_logs

from untether.model import ResumeToken
from untether.runners.claude import ClaudeRunner

SRC = Path(__file__).resolve().parents[1] / "src" / "untether"


def test_1194_every_engine_spawn_logs_its_teardown_exit() -> None:
    """Every function that logs ``subprocess.spawn`` and spawns through
    ``manage_subprocess(`` (Claude's own path and the shared runner every other
    engine uses) passes ``log_teardown_exit=True``, so a future ``run_impl``
    override can't drop it again. Helper probes (agy ``mcp list``, the agy
    quota probe) log no spawn, so they stay silent and the spawn/exit audit
    stays balanced."""
    sites: list[str] = []
    missing: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.relative_to(SRC).as_posix() == "utils/subprocess.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            nodes = list(ast.walk(fn))
            spawns = [
                n
                for n in nodes
                if isinstance(n, ast.Call)
                and getattr(n.func, "id", getattr(n.func, "attr", None))
                == "manage_subprocess"
            ]
            logs_spawn = any(
                isinstance(n, ast.Constant) and n.value == "subprocess.spawn"
                for n in nodes
            )
            if not spawns or not logs_spawn:
                continue
            for call in spawns:
                label = f"{path.relative_to(SRC)}:{call.lineno}"
                sites.append(label)
                flag = next(
                    (k for k in call.keywords if k.arg == "log_teardown_exit"), None
                )
                if flag is None or getattr(flag.value, "value", None) is not True:
                    missing.append(label)
    assert any(s.startswith("runners/claude.py") for s in sites), sites
    assert any(s.startswith("runner.py") for s in sites), sites
    assert not missing, f"spawns without log_teardown_exit=True: {missing}"


@pytest.mark.anyio
async def test_1194_cancelled_claude_run_logs_teardown_exit(tmp_path: Path) -> None:
    fake = tmp_path / "claude"
    fake.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    fake.chmod(0o755)
    runner = ClaudeRunner(claude_cmd=str(fake))

    async def drive() -> None:
        async for _event in runner.run(
            "hi", ResumeToken(engine="claude", value="s-1194")
        ):
            pass

    with capture_logs() as logs, anyio.fail_after(20):
        async with anyio.create_task_group() as tg:
            tg.start_soon(drive)
            # Cancel once the process is up, so the teardown stops it.
            while not any(e["event"] == "subprocess.spawn" for e in logs):
                await anyio.sleep(0.05)
            tg.cancel_scope.cancel()

    exits = [e for e in logs if e["event"] == "subprocess.exit"]
    assert [e.get("reason") for e in exits] == ["teardown"]
