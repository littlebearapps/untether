import asyncio

from takopi.exec_bridge import CodexExecRunner


def test_run_serialized_serializes_same_session() -> None:
    runner = CodexExecRunner(codex_cmd="codex", workspace=None, extra_args=[])
    gate = asyncio.Event()
    in_flight = 0
    max_in_flight = 0

    async def run_stub(*_args, **_kwargs):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await gate.wait()
        in_flight -= 1
        return ("sid", "ok", True)

    runner.run = run_stub  # type: ignore[assignment]

    async def run_test() -> None:
        t1 = asyncio.create_task(runner.run_serialized("a", "sid"))
        t2 = asyncio.create_task(runner.run_serialized("b", "sid"))
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(t1, t2)

    asyncio.run(run_test())

    assert max_in_flight == 1
