"""#838: every engine spawn path runs the #350/#589 pre-spawn guard first.

``ClaudeRunner`` overrode ``run_impl`` wholesale and silently skipped the
guard (the second such override drift after #640). These tests pin the
invariant for every engine, lexically and behaviourally.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import sys
from pathlib import Path

import pytest

from untether.engines import list_backends
from untether.model import CompletedEvent, ResumeToken
from untether.runner import JsonlSubprocessRunner

SRC = Path(__file__).resolve().parents[1] / "src" / "untether"


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_838_every_manage_subprocess_call_is_guarded() -> None:
    """Each function that calls ``manage_subprocess(`` must call
    ``self._check_prespawn_ram_guard(`` earlier in the same body."""
    sites: list[str] = []
    unguarded: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.relative_to(SRC).as_posix() == "utils/subprocess.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
            spawn_lines = [
                c.lineno for c in calls if _call_name(c) == "manage_subprocess"
            ]
            if not spawn_lines:
                continue
            label = f"{path.relative_to(SRC)}:{fn.name}"
            sites.append(label)
            guard_lines = [
                c.lineno
                for c in calls
                if isinstance(c.func, ast.Attribute)
                and c.func.attr == "_check_prespawn_ram_guard"
            ]
            if not guard_lines or min(guard_lines) > min(spawn_lines):
                unguarded.append(label)
    assert len(sites) >= 2, f"spawn-site scan went vacuous: {sites}"
    assert not unguarded, f"spawn sites without a prior pre-spawn guard: {unguarded}"


def _runner_cases() -> list[tuple[str, dict]]:
    cases = [(b.id, {}) for b in list_backends()]
    # The Claude control-channel path (production default) as its own case.
    cases.append(("claude", {"permission_mode": "acceptEdits"}))
    return cases


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("engine", "config"),
    _runner_cases(),
    ids=lambda v: v if isinstance(v, str) else ("control" if v else "default"),
)
@pytest.mark.parametrize("resumed", [False, True], ids=["fresh", "resume"])
async def test_838_every_engine_run_impl_hits_guard_first(
    engine: str,
    config: dict,
    resumed: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from untether.config import ConfigError
    from untether.engines import get_backend

    backend = get_backend(engine)
    try:
        runner = backend.build_runner(dict(config), tmp_path / "untether.toml")
    except ConfigError as exc:  # pragma: no cover — mechanical, never fix here
        pytest.xfail(f"{engine} build_runner needs config: {exc}")

    resume = ResumeToken(engine=engine, value="sess-838") if resumed else None
    sentinel = CompletedEvent(
        engine=engine,
        ok=False,
        answer="",
        resume=resume,
        error="blocked-838",
        usage={"prespawn_blocked": "concurrency"},
    )
    monkeypatch.setattr(
        JsonlSubprocessRunner,
        "_check_prespawn_ram_guard",
        lambda self, resume: sentinel,
    )

    def _no_spawn(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{engine}: manage_subprocess reached past a block")

    for name, module in list(sys.modules.items()):
        if name.startswith("untether") and hasattr(module, "manage_subprocess"):
            monkeypatch.setattr(module, "manage_subprocess", _no_spawn)

    events = [evt async for evt in runner.run_impl("hi", resume)]
    assert events == [sentinel]


def test_838_guard_is_not_overridden() -> None:
    """An override returning None would defeat both tests above."""
    offenders: list[str] = []
    for backend in list_backends():
        module = importlib.import_module(f"untether.runners.{backend.id}")
        for _name, cls in inspect.getmembers(module, inspect.isclass):
            if (
                cls is not JsonlSubprocessRunner
                and issubclass(cls, JsonlSubprocessRunner)
                and "_check_prespawn_ram_guard" in vars(cls)
            ):
                offenders.append(f"{module.__name__}.{cls.__name__}")
    assert not offenders, offenders


# --- #1195: the block messages name idle sessions and give advice that works --

_SUPPORTED = ("claude", "codex", "opencode", "pi", "antigravity")


class _IdleBackend:
    def __init__(self, count: int) -> None:
        self._count = count

    def idle_count(self) -> int:
        return self._count


def _guard_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **kw) -> None:
    from untether import settings as settings_module
    from untether.settings import WatchdogSettings
    from untether.utils import proc_diag

    class _Settings:
        watchdog = WatchdogSettings(**kw)

    monkeypatch.setattr(
        settings_module,
        "load_settings_if_exists",
        lambda: (_Settings(), tmp_path / "untether.toml"),
    )
    monkeypatch.setattr(proc_diag, "mem_available_kb", lambda: 100 * 1024)


@pytest.mark.parametrize("engine", _SUPPORTED)
@pytest.mark.parametrize("block", ["ram", "concurrency"])
@pytest.mark.parametrize(
    ("idle", "clause"),
    [
        ({}, None),
        ({"claude": 2}, ", including 2 idle Claude session(s) kept open"),
        ({"claude": 1, "other": 1}, ", including 2 idle live session(s)"),
    ],
    ids=["no_idle", "idle_claude", "idle_mixed"],
)
def test_1195_block_message_names_idle_sessions_and_host_wide_runs(
    engine: str,
    block: str,
    idle: dict[str, int],
    clause: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five runs open on this host (any engine, any chat) block this
    engine's spawn: the message says they are host-wide, names the idle
    sessions on both blocks, and never suggests restarting the service."""
    from untether import live_sessions
    from untether.engines import get_backend
    from untether.utils import subprocess as sp

    if block == "concurrency":
        _guard_settings(monkeypatch, tmp_path, max_concurrent_engine_runs=5)
    else:
        _guard_settings(monkeypatch, tmp_path, prespawn_ram_block_mb=500)
    monkeypatch.setattr(
        live_sessions,
        "_BACKENDS",
        {name: _IdleBackend(count) for name, count in idle.items()},
    )

    runner = get_backend(engine).build_runner({}, tmp_path / "untether.toml")
    sp._incr_live_engine_subprocesses(5)
    try:
        result = runner._check_prespawn_ram_guard(resume=None)
    finally:
        sp._incr_live_engine_subprocesses(-5)

    assert result is not None and result.ok is False
    error = result.error or ""
    assert result.usage == {"prespawn_blocked": block}
    if block == "concurrency":
        assert "runs open on this host (5/5" in error
    else:
        assert "5 engine run(s) already open on this host" in error
    if clause is None:
        assert "idle" not in error
    else:
        assert clause in error
    assert error.endswith(
        "Wait a minute and send it again, or /cancel a run in the chat that started it."
    )
    assert "restart" not in error


def test_1195_ram_block_with_no_runs_only_says_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from untether.runners.codex import CodexRunner

    _guard_settings(monkeypatch, tmp_path, prespawn_ram_block_mb=500)

    result = CodexRunner(codex_cmd="codex", extra_args=[])._check_prespawn_ram_guard(
        resume=None
    )

    assert result is not None
    assert result.error == (
        "🛑 Insufficient RAM to start engine (100 MB free, threshold 500 MB). "
        "Wait a minute and send it again."
    )
