import sys

import pytest

from untether.utils import subprocess as subprocess_utils


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
