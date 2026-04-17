import signal
import sys
from dataclasses import dataclass

import pytest

from untether.utils import subprocess as subprocess_utils


@dataclass
class _FakeProc:
    """Minimal Process stand-in for _signal_process tests.

    ``subprocess_utils._signal_process`` only reads ``.pid`` / ``.returncode``
    and invokes ``.terminate`` / ``.kill``; a real anyio Process isn't needed.
    """

    pid: int = 1234
    returncode: int | None = None
    terminate_called: int = 0
    kill_called: int = 0

    def terminate(self) -> None:
        self.terminate_called += 1

    def kill(self) -> None:
        self.kill_called += 1


@pytest.mark.anyio
async def test_manage_subprocess_kills_when_terminate_times_out(
    monkeypatch,
) -> None:
    async def fake_wait_for_process(_proc, timeout: float) -> bool:
        _ = timeout
        return True

    monkeypatch.setattr(subprocess_utils, "wait_for_process", fake_wait_for_process)

    async with subprocess_utils.manage_subprocess(
        [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(10)",
        ]
    ) as proc:
        assert proc.returncode is None

    assert proc.returncode is not None
    assert proc.returncode != 0


@pytest.mark.anyio
async def test_manage_subprocess_uses_10s_kill_timeout(
    monkeypatch,
) -> None:
    """Verify the SIGTERM grace period before SIGKILL is 10 seconds."""
    captured_timeout: list[float] = []

    async def capture_wait(_proc, timeout: float) -> bool:
        captured_timeout.append(timeout)
        return False  # Process exited in time, no SIGKILL needed

    monkeypatch.setattr(subprocess_utils, "wait_for_process", capture_wait)

    async with subprocess_utils.manage_subprocess(
        [sys.executable, "-c", "pass"]
    ) as proc:
        assert proc.returncode is None

    assert captured_timeout == [10.0]


# ---------------------------------------------------------------------------
# #275 — descendant-tree cleanup tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc only")
def test_terminate_process_signals_captured_descendants(monkeypatch) -> None:
    """SIGTERM is sent both to the pgroup AND to each descendant captured
    before the killpg — grandchildren in separate process groups (#275, e.g.
    vitest → workerd) don't get reached by killpg alone."""
    calls: list[tuple[str, int, int]] = []

    def fake_killpg(pid: int, sig: int) -> None:
        calls.append(("killpg", pid, sig))

    def fake_kill(pid: int, sig: int) -> None:
        calls.append(("kill", pid, sig))

    def fake_find(pid: int) -> list[int]:
        assert pid == 1234
        return [5001, 5002, 5003]

    monkeypatch.setattr(subprocess_utils.os, "killpg", fake_killpg)
    monkeypatch.setattr(subprocess_utils.os, "kill", fake_kill)
    monkeypatch.setattr(subprocess_utils, "find_descendants", fake_find)

    subprocess_utils.terminate_process(_FakeProc())

    # killpg fires first, then each descendant PID individually.
    assert calls[0] == ("killpg", 1234, signal.SIGTERM)
    assert ("kill", 5001, signal.SIGTERM) in calls
    assert ("kill", 5002, signal.SIGTERM) in calls
    assert ("kill", 5003, signal.SIGTERM) in calls


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc only")
def test_kill_process_signals_descendants_with_sigkill(monkeypatch) -> None:
    """SIGKILL escalation also walks the descendant tree (#275)."""
    calls: list[tuple[str, int, int]] = []

    monkeypatch.setattr(
        subprocess_utils.os,
        "killpg",
        lambda pid, sig: calls.append(("killpg", pid, sig)),
    )
    monkeypatch.setattr(
        subprocess_utils.os,
        "kill",
        lambda pid, sig: calls.append(("kill", pid, sig)),
    )
    monkeypatch.setattr(subprocess_utils, "find_descendants", lambda pid: [9001])

    subprocess_utils.kill_process(_FakeProc(pid=4242))

    assert ("killpg", 4242, signal.SIGKILL) in calls
    assert ("kill", 9001, signal.SIGKILL) in calls


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc only")
def test_signal_process_swallows_descendant_process_lookup(monkeypatch) -> None:
    """If a descendant already exited, ProcessLookupError is swallowed
    (descendants snapshot is inherently racy — they may die between snapshot
    and signal)."""
    monkeypatch.setattr(subprocess_utils.os, "killpg", lambda pid, sig: None)

    def raising_kill(pid: int, sig: int) -> None:
        if pid == 7001:
            raise ProcessLookupError
        if pid == 7002:
            raise PermissionError  # reparented to another user's systemd
        # 7003 succeeds silently

    monkeypatch.setattr(subprocess_utils.os, "kill", raising_kill)
    monkeypatch.setattr(
        subprocess_utils, "find_descendants", lambda pid: [7001, 7002, 7003]
    )

    # Must not raise.
    subprocess_utils.terminate_process(_FakeProc())


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc only")
def test_signal_process_descendant_oserror_is_logged_not_raised(
    monkeypatch,
) -> None:
    """Unexpected OSError on a descendant signal is logged at debug, not
    propagated — cleanup of the other descendants must continue."""
    remaining: list[int] = []

    def partial_kill(pid: int, sig: int) -> None:
        if pid == 6001:
            raise OSError(22, "bad argument")
        remaining.append(pid)

    monkeypatch.setattr(subprocess_utils.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(subprocess_utils.os, "kill", partial_kill)
    monkeypatch.setattr(subprocess_utils, "find_descendants", lambda pid: [6001, 6002])

    subprocess_utils.terminate_process(_FakeProc())

    assert 6002 in remaining  # 6001 errored but 6002 still got signalled


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc only")
def test_signal_process_degrades_when_find_descendants_raises(
    monkeypatch,
) -> None:
    """OSError from find_descendants (e.g. /proc unavailable) falls back to
    the legacy pgroup-only path without crashing."""
    calls: list[str] = []

    def fake_killpg(pid: int, sig: int) -> None:
        calls.append("killpg")

    def raise_oserror(pid: int) -> list[int]:
        raise OSError("proc unavailable")

    monkeypatch.setattr(subprocess_utils.os, "killpg", fake_killpg)
    monkeypatch.setattr(subprocess_utils, "find_descendants", raise_oserror)

    # Must not raise.
    subprocess_utils.terminate_process(_FakeProc())
    assert calls == ["killpg"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc only")
def test_signal_process_still_signals_descendants_when_parent_gone(
    monkeypatch,
) -> None:
    """If the parent exits between snapshot and killpg (races are possible),
    killpg raises ProcessLookupError — but captured descendants still need
    signalling in case they're alive and reparented (#275 root cause)."""
    descendants_signalled: list[int] = []

    def fake_killpg(pid: int, sig: int) -> None:
        raise ProcessLookupError  # parent died between snapshot and kill

    def record_kill(pid: int, sig: int) -> None:
        descendants_signalled.append(pid)

    monkeypatch.setattr(subprocess_utils.os, "killpg", fake_killpg)
    monkeypatch.setattr(subprocess_utils.os, "kill", record_kill)
    monkeypatch.setattr(subprocess_utils, "find_descendants", lambda pid: [8001, 8002])

    subprocess_utils.terminate_process(_FakeProc())

    assert descendants_signalled == [8001, 8002]
