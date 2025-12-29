import asyncio
import sys

from takopi import exec_bridge


def test_manage_subprocess_kills_when_terminate_times_out(monkeypatch) -> None:
    async def fake_wait_for(awaitable, *args, **kwargs):
        if hasattr(awaitable, "close"):
            awaitable.close()
        elif hasattr(awaitable, "cancel"):
            awaitable.cancel()
        raise asyncio.TimeoutError

    monkeypatch.setattr(exec_bridge.asyncio, "wait_for", fake_wait_for)

    async def run() -> int | None:
        async with exec_bridge.manage_subprocess(
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(10)",
        ) as proc:
            assert proc.returncode is None
        return proc.returncode

    rc = asyncio.run(run())

    assert rc is not None
    assert rc != 0
