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


# ---------------------------------------------------------------------------
# #599 — subprocess transport explicitly closed on every exit path
# ---------------------------------------------------------------------------


class _FakeAcloseProc:
    """Process stand-in that records ``aclose()`` calls."""

    def __init__(self, returncode: int | None = 0, pid: int = 4321) -> None:
        self.pid = pid
        self.returncode = returncode
        self.aclose_called = 0
        self.aclose_error: Exception | None = None

    async def wait(self) -> int | None:
        return self.returncode

    async def aclose(self) -> None:
        self.aclose_called += 1
        if self.aclose_error is not None:
            raise self.aclose_error


@pytest.mark.anyio
async def test_manage_subprocess_acloses_transport_on_clean_exit(
    monkeypatch,
) -> None:
    """#599: aclose() must run even when the subprocess exited cleanly —
    the clean-exit path is exactly where the transport used to leak until
    interpreter shutdown ("RuntimeError: Event loop is closed" in
    BaseSubprocessTransport.__del__)."""
    fake = _FakeAcloseProc(returncode=0)

    async def fake_open(cmd, **kwargs):
        return fake

    monkeypatch.setattr("untether.utils.subprocess.anyio.open_process", fake_open)

    async with subprocess_utils.manage_subprocess(["fake-cmd"]) as proc:
        assert proc is fake

    assert fake.aclose_called == 1


@pytest.mark.anyio
async def test_manage_subprocess_acloses_transport_after_kill_path(
    monkeypatch,
) -> None:
    """#599: aclose() also runs after the SIGTERM/SIGKILL teardown path."""
    fake = _FakeAcloseProc(returncode=None)

    async def fake_open(cmd, **kwargs):
        return fake

    def fake_terminate(proc) -> None:
        proc.returncode = -15

    async def fake_wait_for_process(proc, timeout: float) -> bool:
        return False  # exited within the grace window — no SIGKILL

    monkeypatch.setattr("untether.utils.subprocess.anyio.open_process", fake_open)
    monkeypatch.setattr(subprocess_utils, "terminate_process", fake_terminate)
    monkeypatch.setattr(subprocess_utils, "wait_for_process", fake_wait_for_process)

    async with subprocess_utils.manage_subprocess(["fake-cmd"]):
        pass

    assert fake.returncode == -15
    assert fake.aclose_called == 1


@pytest.mark.anyio
async def test_manage_subprocess_aclose_errors_are_suppressed(
    monkeypatch,
) -> None:
    """#599: a failing aclose() must never mask the run's own outcome."""
    fake = _FakeAcloseProc(returncode=0)
    fake.aclose_error = RuntimeError("Event loop is closed")

    async def fake_open(cmd, **kwargs):
        return fake

    monkeypatch.setattr("untether.utils.subprocess.anyio.open_process", fake_open)

    async with subprocess_utils.manage_subprocess(["fake-cmd"]):
        pass  # exiting must not raise despite aclose blowing up

    assert fake.aclose_called == 1


# ---------------------------------------------------------------------------
# #590 — descendant-aware signalling + post-exit orphan sweep
# ---------------------------------------------------------------------------


def test_signal_pid_group_delivers_to_group_and_descendants(monkeypatch) -> None:
    """signal_pid_group snapshots descendants BEFORE killpg and delivers the
    signal to both — bare killpg missed pgroup escapees."""
    killpg_calls: list[tuple[int, int]] = []
    kill_calls: list[tuple[int, int]] = []

    monkeypatch.setattr(subprocess_utils, "find_descendants", lambda pid: [111, 222])
    monkeypatch.setattr(
        subprocess_utils.os,
        "killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )
    monkeypatch.setattr(
        subprocess_utils.os, "kill", lambda pid, sig: kill_calls.append((pid, sig))
    )
    monkeypatch.setattr(subprocess_utils.sys, "platform", "linux")

    subprocess_utils.signal_pid_group(4242, signal.SIGTERM)

    assert killpg_calls == [(4242, signal.SIGTERM)]
    assert (111, signal.SIGTERM) in kill_calls
    assert (222, signal.SIGTERM) in kill_calls


@pytest.mark.anyio
async def test_reap_orphaned_group_noop_when_group_empty(monkeypatch) -> None:
    """No group members and no extras — the sweep returns instantly and
    signals nothing (the common clean-teardown case must stay free)."""
    signals: list[tuple[int, int]] = []

    def probe_killpg(pgid: int, sig: int) -> None:
        if sig == 0:
            raise ProcessLookupError
        signals.append((pgid, sig))

    monkeypatch.setattr(subprocess_utils.os, "killpg", probe_killpg)

    reaped = await subprocess_utils.reap_orphaned_group(999)

    assert reaped == []
    assert signals == []


@pytest.mark.anyio
async def test_reap_orphaned_group_sigterms_then_sigkills_survivors(
    monkeypatch,
) -> None:
    """Group members survive the leader: SIGTERM the group; members that
    ignore it are SIGKILLed after the grace."""
    alive = {31001: True, 31002: True}
    killpg_signals: list[int] = []
    kill_calls: list[tuple[int, int]] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        if sig == 0:
            if not any(alive.values()):
                raise ProcessLookupError
            return
        killpg_signals.append(sig)
        if sig == signal.SIGTERM:
            alive[31001] = False  # one member obeys SIGTERM
        if sig == signal.SIGKILL:
            alive[31002] = False

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            if not alive.get(pid, False):
                raise ProcessLookupError
            return
        kill_calls.append((pid, sig))

    monkeypatch.setattr(subprocess_utils.os, "killpg", fake_killpg)
    monkeypatch.setattr(subprocess_utils.os, "kill", fake_kill)
    monkeypatch.setattr(subprocess_utils, "_pgid_members", lambda pgid: [31001, 31002])

    reaped = await subprocess_utils.reap_orphaned_group(31000, grace_s=0.2)

    assert reaped == [31001, 31002]
    assert killpg_signals[0] == signal.SIGTERM
    assert signal.SIGKILL in killpg_signals
    assert (31002, signal.SIGKILL) in kill_calls


@pytest.mark.anyio
async def test_reap_orphaned_group_kills_snapshot_escapees(monkeypatch) -> None:
    """PIDs captured while the leader was alive (pgroup escapees in their
    own session) are signalled even though killpg can't reach them."""
    alive = {41001: True}
    kill_calls: list[tuple[int, int]] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        raise ProcessLookupError  # the group itself is already empty

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            if not alive.get(pid, False):
                raise ProcessLookupError
            return
        kill_calls.append((pid, sig))
        if sig == signal.SIGTERM:
            alive[pid] = False

    monkeypatch.setattr(subprocess_utils.os, "killpg", fake_killpg)
    monkeypatch.setattr(subprocess_utils.os, "kill", fake_kill)
    monkeypatch.setattr(subprocess_utils, "_pgid_members", lambda pgid: [])

    reaped = await subprocess_utils.reap_orphaned_group(
        41000, extra_pids=(41001,), grace_s=0.2
    )

    assert reaped == [41001]
    assert (41001, signal.SIGTERM) in kill_calls


@pytest.mark.anyio
async def test_manage_subprocess_reaps_after_clean_exit(monkeypatch) -> None:
    """#590: the sweep runs on the clean rc=0 path with the caller's
    snapshot, and honours reap_orphans=False."""
    reap_calls: list[tuple[int, tuple[int, ...]]] = []

    async def fake_reap(
        pgid: int,
        *,
        extra_pids=(),
        extra_pid_starttimes=None,
        grace_s: float = 2.0,
    ):
        reap_calls.append((pgid, tuple(extra_pids)))
        return list(extra_pids)

    monkeypatch.setattr(subprocess_utils, "reap_orphaned_group", fake_reap)

    fake = _FakeAcloseProc(returncode=0, pid=5555)

    async def fake_open(cmd, **kwargs):
        return fake

    monkeypatch.setattr("untether.utils.subprocess.anyio.open_process", fake_open)

    snapshot = [777]
    async with subprocess_utils.manage_subprocess(
        ["fake-cmd"], orphan_pid_snapshot=snapshot
    ):
        snapshot.append(888)  # captured mid-run, read at teardown

    assert reap_calls == [(5555, (777, 888))]

    reap_calls.clear()
    fake2 = _FakeAcloseProc(returncode=0, pid=5556)

    async def fake_open2(cmd, **kwargs):
        return fake2

    monkeypatch.setattr("untether.utils.subprocess.anyio.open_process", fake_open2)
    async with subprocess_utils.manage_subprocess(["fake-cmd"], reap_orphans=False):
        pass
    assert reap_calls == []


@pytest.mark.anyio
async def test_reap_orphaned_group_skips_recycled_pid(monkeypatch) -> None:
    """#590 hardening: a captured extra PID whose /proc starttime no longer
    matches (PID recycled by an unrelated process) is NOT signalled."""
    kill_calls: list[tuple[int, int]] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        raise ProcessLookupError  # group already empty

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            return  # pretend alive
        kill_calls.append((pid, sig))

    monkeypatch.setattr(subprocess_utils.os, "killpg", fake_killpg)
    monkeypatch.setattr(subprocess_utils.os, "kill", fake_kill)
    monkeypatch.setattr(subprocess_utils, "_pgid_members", lambda pgid: [])
    # recorded starttime 100, but current is 999 → identity mismatch.
    monkeypatch.setattr(subprocess_utils, "pid_starttime", lambda pid: 999)

    reaped = await subprocess_utils.reap_orphaned_group(
        42000, extra_pids=(42001,), extra_pid_starttimes={42001: 100}, grace_s=0.05
    )

    assert reaped == []
    assert kill_calls == []


@pytest.mark.anyio
async def test_reap_orphaned_group_signals_matching_identity(monkeypatch) -> None:
    """#590 hardening: a captured extra PID whose starttime still matches IS
    signalled (identity verified, not a recycled PID)."""
    alive = {43001: True}
    kill_calls: list[tuple[int, int]] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        raise ProcessLookupError

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            if not alive.get(pid, False):
                raise ProcessLookupError
            return
        kill_calls.append((pid, sig))
        if sig == signal.SIGTERM:
            alive[pid] = False

    monkeypatch.setattr(subprocess_utils.os, "killpg", fake_killpg)
    monkeypatch.setattr(subprocess_utils.os, "kill", fake_kill)
    monkeypatch.setattr(subprocess_utils, "_pgid_members", lambda pgid: [])
    monkeypatch.setattr(subprocess_utils, "pid_starttime", lambda pid: 100)

    reaped = await subprocess_utils.reap_orphaned_group(
        43000, extra_pids=(43001,), extra_pid_starttimes={43001: 100}, grace_s=0.2
    )

    assert reaped == [43001]
    assert (43001, signal.SIGTERM) in kill_calls
