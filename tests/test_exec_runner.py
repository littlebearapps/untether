import sys
from collections.abc import AsyncIterator

import anyio
import pytest

from untether.model import (
    ActionEvent,
    CompletedEvent,
    ResumeToken,
    StartedEvent,
    UntetherEvent,
)
from untether.runners.codex import CodexRunner, find_exec_only_flag

CODEX_ENGINE = "codex"


@pytest.mark.anyio
async def test_run_serializes_same_session() -> None:
    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    gate = anyio.Event()
    in_flight = 0
    max_in_flight = 0

    async def run_stub(*_args, **_kwargs) -> AsyncIterator[UntetherEvent]:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await gate.wait()
            yield CompletedEvent(
                engine=CODEX_ENGINE,
                resume=ResumeToken(engine=CODEX_ENGINE, value="sid"),
                ok=True,
                answer="ok",
            )
        finally:
            in_flight -= 1

    runner.run_impl = run_stub  # type: ignore[assignment]

    async def drain(prompt: str, resume: ResumeToken | None) -> None:
        async for _event in runner.run(prompt, resume):
            pass

    token = ResumeToken(engine=CODEX_ENGINE, value="sid")
    async with anyio.create_task_group() as tg:
        tg.start_soon(drain, "a", token)
        tg.start_soon(drain, "b", token)
        await anyio.lowlevel.checkpoint()
        gate.set()
    assert max_in_flight == 1


@pytest.mark.anyio
async def test_run_allows_parallel_new_sessions() -> None:
    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    gate = anyio.Event()
    in_flight = 0
    max_in_flight = 0

    async def run_stub(*_args, **_kwargs) -> AsyncIterator[UntetherEvent]:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await gate.wait()
            yield CompletedEvent(
                engine=CODEX_ENGINE,
                resume=ResumeToken(engine=CODEX_ENGINE, value="sid"),
                ok=True,
                answer="ok",
            )
        finally:
            in_flight -= 1

    runner.run_impl = run_stub  # type: ignore[assignment]

    async def drain(prompt: str, resume: ResumeToken | None) -> None:
        async for _event in runner.run(prompt, resume):
            pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(drain, "a", None)
        tg.start_soon(drain, "b", None)
        await anyio.lowlevel.checkpoint()
        gate.set()
    assert max_in_flight == 2


@pytest.mark.anyio
async def test_run_allows_parallel_different_sessions() -> None:
    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    gate = anyio.Event()
    in_flight = 0
    max_in_flight = 0

    async def run_stub(*_args, **_kwargs) -> AsyncIterator[UntetherEvent]:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await gate.wait()
            yield CompletedEvent(
                engine=CODEX_ENGINE,
                resume=ResumeToken(engine=CODEX_ENGINE, value="sid"),
                ok=True,
                answer="ok",
            )
        finally:
            in_flight -= 1

    runner.run_impl = run_stub  # type: ignore[assignment]

    async def drain(prompt: str, resume: ResumeToken | None) -> None:
        async for _event in runner.run(prompt, resume):
            pass

    token_a = ResumeToken(engine=CODEX_ENGINE, value="sid-a")
    token_b = ResumeToken(engine=CODEX_ENGINE, value="sid-b")
    async with anyio.create_task_group() as tg:
        tg.start_soon(drain, "a", token_a)
        tg.start_soon(drain, "b", token_b)
        await anyio.lowlevel.checkpoint()
        gate.set()
    assert max_in_flight == 2


def test_codex_exec_flags_after_exec() -> None:
    runner = CodexRunner(
        codex_cmd="codex",
        extra_args=["-c", "notify=[]"],
    )
    state = runner.new_state("hi", None)
    args = runner.build_args("hi", None, state=state)
    assert args == [
        "-c",
        "notify=[]",
        "--ask-for-approval",
        "never",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--color=never",
        "-",
    ]


@pytest.mark.parametrize(
    ("extra_args", "expected"),
    [
        ([], None),
        (["-c", "notify=[]"], None),
        (["--skip-git-repo-check"], "--skip-git-repo-check"),
        (["--color=never"], "--color=never"),
        (["--output-schema", "schema.json"], "--output-schema"),
        (["--output-last-message=out.txt"], "--output-last-message=out.txt"),
        (["-o", "out.txt"], "-o"),
    ],
)
def test_find_exec_only_flag(extra_args: list[str], expected: str | None) -> None:
    assert find_exec_only_flag(extra_args) == expected


@pytest.mark.anyio
async def test_run_serializes_new_session_after_session_is_known(
    tmp_path, monkeypatch
) -> None:
    gate_path = tmp_path / "gate"
    resume_marker = tmp_path / "resume_started"
    thread_id = "019b73c4-0c3f-7701-a0bb-aac6b4d8a3bc"

    codex_path = tmp_path / "codex"
    codex_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "\n"
        "gate = os.environ['CODEX_TEST_GATE']\n"
        "resume_marker = os.environ['CODEX_TEST_RESUME_MARKER']\n"
        "thread_id = os.environ['CODEX_TEST_THREAD_ID']\n"
        "\n"
        "args = sys.argv[1:]\n"
        "if 'resume' in args:\n"
        "    print(json.dumps({'type': 'thread.started', 'thread_id': thread_id}), flush=True)\n"
        "    with open(resume_marker, 'w', encoding='utf-8') as f:\n"
        "        f.write('started')\n"
        "        f.flush()\n"
        "    sys.exit(0)\n"
        "\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': thread_id}), flush=True)\n"
        "while not os.path.exists(gate):\n"
        "    time.sleep(0.001)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    codex_path.chmod(0o755)

    monkeypatch.setenv("CODEX_TEST_GATE", str(gate_path))
    monkeypatch.setenv("CODEX_TEST_RESUME_MARKER", str(resume_marker))
    monkeypatch.setenv("CODEX_TEST_THREAD_ID", thread_id)

    runner = CodexRunner(codex_cmd=str(codex_path), extra_args=[])

    session_started = anyio.Event()
    resume_value: str | None = None

    new_done = anyio.Event()

    async def run_new() -> None:
        nonlocal resume_value
        async for event in runner.run("hello", None):
            if isinstance(event, StartedEvent):
                resume_value = event.resume.value
                session_started.set()
        new_done.set()

    async def run_resume() -> None:
        assert resume_value is not None
        async for _event in runner.run(
            "resume", ResumeToken(engine=CODEX_ENGINE, value=resume_value)
        ):
            pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_new)
        await session_started.wait()

        tg.start_soon(run_resume)
        await anyio.sleep(0.01)

        assert not resume_marker.exists()

        gate_path.write_text("go", encoding="utf-8")
        await new_done.wait()

        with anyio.fail_after(2):
            while not resume_marker.exists():
                await anyio.sleep(0.001)


@pytest.mark.anyio
async def test_codex_runner_preserves_warning_order(tmp_path) -> None:
    thread_id = "019b73c4-0c3f-7701-a0bb-aac6b4d8a3bc"

    codex_path = tmp_path / "codex"
    codex_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'error', 'message': 'warning one'}), flush=True)\n"
        f"print(json.dumps({{'type': 'thread.started', 'thread_id': '{thread_id}'}}), flush=True)\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'agent_message', 'text': 'ok'}}), flush=True)\n",
        encoding="utf-8",
    )
    codex_path.chmod(0o755)

    runner = CodexRunner(codex_cmd=str(codex_path), extra_args=[])
    seen = [evt async for evt in runner.run("hi", None)]

    assert len(seen) == 3
    assert isinstance(seen[0], ActionEvent)
    assert seen[0].phase == "completed"
    assert seen[0].ok is False
    assert seen[0].action.kind == "warning"
    assert seen[0].action.title == "warning one"

    assert isinstance(seen[1], StartedEvent)
    assert seen[1].resume.value == thread_id

    assert isinstance(seen[2], CompletedEvent)
    assert seen[2].resume == seen[1].resume
    assert seen[2].answer == "ok"


@pytest.mark.anyio
async def test_codex_runner_reconnect_notice_is_non_fatal(tmp_path) -> None:
    thread_id = "019b73c4-0c3f-7701-a0bb-aac6b4d8a3bc"

    codex_path = tmp_path / "codex"
    codex_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'error', 'message': 'Reconnecting... 1/5'}), flush=True)\n"
        f"print(json.dumps({{'type': 'thread.started', 'thread_id': '{thread_id}'}}), flush=True)\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'agent_message', 'text': 'ok'}}), flush=True)\n",
        encoding="utf-8",
    )
    codex_path.chmod(0o755)

    runner = CodexRunner(codex_cmd=str(codex_path), extra_args=[])
    seen = [evt async for evt in runner.run("hi", None)]

    assert len(seen) == 3
    assert isinstance(seen[0], ActionEvent)
    assert seen[0].phase == "started"
    assert seen[0].ok is None
    assert seen[0].action.kind == "note"
    assert seen[0].action.title == "Reconnecting... 1/5"

    assert isinstance(seen[1], StartedEvent)
    assert seen[1].resume.value == thread_id

    assert isinstance(seen[2], CompletedEvent)
    assert seen[2].resume == seen[1].resume
    assert seen[2].answer == "ok"


@pytest.mark.anyio
async def test_codex_runner_reconnect_notice_updates_phase(tmp_path) -> None:
    thread_id = "019b73c4-0c3f-7701-a0bb-aac6b4d8a3bc"

    codex_path = tmp_path / "codex"
    codex_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'error', 'message': 'Reconnecting... 1/5'}), flush=True)\n"
        "print(json.dumps({'type': 'error', 'message': 'Reconnecting... 2/5'}), flush=True)\n"
        f"print(json.dumps({{'type': 'thread.started', 'thread_id': '{thread_id}'}}), flush=True)\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'agent_message', 'text': 'ok'}}), flush=True)\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'cached_input_tokens': 0, 'output_tokens': 1}}), flush=True)\n",
        encoding="utf-8",
    )
    codex_path.chmod(0o755)

    runner = CodexRunner(codex_cmd=str(codex_path), extra_args=[])
    seen = [evt async for evt in runner.run("hi", None)]

    assert len(seen) == 4
    first = seen[0]
    second = seen[1]
    assert isinstance(first, ActionEvent)
    assert isinstance(second, ActionEvent)
    assert first.phase == "started"
    assert second.phase == "updated"
    assert first.action.id == second.action.id == "codex.reconnect"
    assert isinstance(seen[2], StartedEvent)
    assert isinstance(seen[3], CompletedEvent)


@pytest.mark.anyio
async def test_codex_runner_prefers_final_answer_phase(tmp_path) -> None:
    thread_id = "019b73c4-0c3f-7701-a0bb-aac6b4d8a3bc"

    codex_path = tmp_path / "codex"
    codex_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "\n"
        "sys.stdin.read()\n"
        f"print(json.dumps({{'type': 'thread.started', 'thread_id': '{thread_id}'}}), flush=True)\n"
        "print(json.dumps({'type': 'turn.started'}), flush=True)\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'agent_message', 'phase': 'commentary', 'text': 'Working through the task.'}}), flush=True)\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_1', 'type': 'agent_message', 'phase': 'final_answer', 'text': 'Done.'}}), flush=True)\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'cached_input_tokens': 0, 'output_tokens': 1}}), flush=True)\n",
        encoding="utf-8",
    )
    codex_path.chmod(0o755)

    runner = CodexRunner(codex_cmd=str(codex_path), extra_args=[])
    seen = [evt async for evt in runner.run("hi", None)]

    assert len(seen) == 4
    assert isinstance(seen[0], StartedEvent)
    assert isinstance(seen[1], ActionEvent)
    assert seen[1].action.kind == "turn"
    assert isinstance(seen[2], ActionEvent)
    assert seen[2].action.kind == "note"
    assert seen[2].action.title == "Working through the task."
    assert seen[2].phase == "completed"
    assert seen[2].ok is True
    assert isinstance(seen[3], CompletedEvent)
    assert seen[3].answer == "Done."


@pytest.mark.anyio
async def test_codex_runner_legacy_agent_message_no_phase(tmp_path) -> None:
    thread_id = "019b73c4-0c3f-7701-a0bb-aac6b4d8a3bc"

    codex_path = tmp_path / "codex"
    codex_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "\n"
        "sys.stdin.read()\n"
        f"print(json.dumps({{'type': 'thread.started', 'thread_id': '{thread_id}'}}), flush=True)\n"
        "print(json.dumps({'type': 'turn.started'}), flush=True)\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'agent_message', 'text': 'first'}}), flush=True)\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_1', 'type': 'agent_message', 'text': 'second'}}), flush=True)\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'cached_input_tokens': 0, 'output_tokens': 1}}), flush=True)\n",
        encoding="utf-8",
    )
    codex_path.chmod(0o755)

    runner = CodexRunner(codex_cmd=str(codex_path), extra_args=[])
    seen = [evt async for evt in runner.run("hi", None)]

    completed = next(evt for evt in seen if isinstance(evt, CompletedEvent))
    assert completed.answer == "second"


@pytest.mark.anyio
async def test_codex_runner_collab_tool_call_does_not_break_stream(tmp_path) -> None:
    thread_id = "019b73c4-0c3f-7701-a0bb-aac6b4d8a3bc"

    codex_path = tmp_path / "codex"
    codex_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "\n"
        "sys.stdin.read()\n"
        f"print(json.dumps({{'type': 'thread.started', 'thread_id': '{thread_id}'}}), flush=True)\n"
        "print(json.dumps({'type': 'turn.started'}), flush=True)\n"
        "print(json.dumps({'type': 'item.started', 'item': {'id': 'item_0', 'type': 'collab_tool_call', 'tool': 'spawn_agent', 'sender_thread_id': 'main', 'receiver_thread_ids': ['worker'], 'prompt': 'check tests', 'agents_states': {}, 'status': 'in_progress'}}), flush=True)\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'collab_tool_call', 'tool': 'spawn_agent', 'sender_thread_id': 'main', 'receiver_thread_ids': ['worker'], 'prompt': 'check tests', 'agents_states': {'worker': {'status': 'completed', 'message': 'ok'}}, 'status': 'completed'}}), flush=True)\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_1', 'type': 'agent_message', 'text': 'ok'}}), flush=True)\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'cached_input_tokens': 0, 'output_tokens': 1}}), flush=True)\n",
        encoding="utf-8",
    )
    codex_path.chmod(0o755)

    runner = CodexRunner(codex_cmd=str(codex_path), extra_args=[])
    seen = [evt async for evt in runner.run("hi", None)]

    completed = next(evt for evt in seen if isinstance(evt, CompletedEvent))
    assert completed.answer == "ok"


@pytest.mark.anyio
async def test_codex_runner_unknown_item_type_does_not_break_stream(tmp_path) -> None:
    thread_id = "019b73c4-0c3f-7701-a0bb-aac6b4d8a3bc"

    codex_path = tmp_path / "codex"
    codex_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "\n"
        "sys.stdin.read()\n"
        f"print(json.dumps({{'type': 'thread.started', 'thread_id': '{thread_id}'}}), flush=True)\n"
        "print(json.dumps({'type': 'turn.started'}), flush=True)\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'future_item', 'foo': 'bar'}}), flush=True)\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_1', 'type': 'agent_message', 'text': 'ok'}}), flush=True)\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'cached_input_tokens': 0, 'output_tokens': 1}}), flush=True)\n",
        encoding="utf-8",
    )
    codex_path.chmod(0o755)

    runner = CodexRunner(codex_cmd=str(codex_path), extra_args=[])
    seen = [evt async for evt in runner.run("hi", None)]

    completed = next(evt for evt in seen if isinstance(evt, CompletedEvent))
    assert completed.ok is True
    assert completed.answer == "ok"


@pytest.mark.anyio
async def test_codex_runner_includes_stderr_reason(tmp_path) -> None:
    codex_path = tmp_path / "codex"
    codex_path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "\n"
        "sys.stderr.write('Not inside a trusted directory and --skip-git-repo-check was not specified.\\n')\n"
        "sys.stderr.flush()\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    codex_path.chmod(0o755)

    runner = CodexRunner(codex_cmd=str(codex_path), extra_args=[])
    events = [evt async for evt in runner.run("hi", None)]

    completed = next(evt for evt in events if isinstance(evt, CompletedEvent))
    assert completed.ok is False
    assert completed.error is not None
    assert "codex exec failed (rc=1)" in completed.error
    assert "Not inside a trusted directory" in completed.error


@pytest.mark.anyio
async def test_run_serializes_two_new_sessions_same_thread(
    tmp_path, monkeypatch
) -> None:
    gate_path = tmp_path / "gate"
    thread_id = "019b73c4-0c3f-7701-a0bb-aac6b4d8a3bc"

    codex_path = tmp_path / "codex"
    codex_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "\n"
        "gate = os.environ['CODEX_TEST_GATE']\n"
        "thread_id = os.environ['CODEX_TEST_THREAD_ID']\n"
        "\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': thread_id}), flush=True)\n"
        "while not os.path.exists(gate):\n"
        "    time.sleep(0.001)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    codex_path.chmod(0o755)

    monkeypatch.setenv("CODEX_TEST_GATE", str(gate_path))
    monkeypatch.setenv("CODEX_TEST_THREAD_ID", thread_id)

    runner = CodexRunner(codex_cmd=str(codex_path), extra_args=[])

    started_first = anyio.Event()
    started_second = anyio.Event()

    async def run_first() -> None:
        async for event in runner.run("one", None):
            if isinstance(event, StartedEvent):
                started_first.set()

    async def run_second() -> None:
        async for event in runner.run("two", None):
            if isinstance(event, StartedEvent):
                started_second.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_first)
        tg.start_soon(run_second)

        with anyio.fail_after(2):
            while not (started_first.is_set() or started_second.is_set()):
                await anyio.sleep(0.001)

        assert not (started_first.is_set() and started_second.is_set())

        gate_path.write_text("go", encoding="utf-8")

        with anyio.fail_after(2):
            await started_first.wait()
            await started_second.wait()


@pytest.mark.anyio
async def test_watchdog_force_closes_orphaned_pipes(tmp_path, monkeypatch) -> None:
    """When subprocess dies but stdout stays open, watchdog force-closes pipes."""
    # Create a script that spawns a child holding stdout open, then exits.
    script = tmp_path / "codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        # Print a started event
        "sid = '019b73c4-0000-0000-0000-000000000001'\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': sid}), flush=True)\n"
        # Fork a child that holds stdout open
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    time.sleep(60)\n"  # child: hold pipe open for 60s
        "    sys.exit(0)\n"
        "# parent exits immediately without CompletedEvent\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    runner = CodexRunner(codex_cmd=str(script), extra_args=[])
    # Use a short grace period so the test doesn't wait long
    runner._WATCHDOG_GRACE_SECONDS = 0.5

    with anyio.fail_after(5):
        events: list[UntetherEvent] = [evt async for evt in runner.run("test", None)]

    # Should have completed (watchdog force-closed the orphaned pipe)
    assert any(isinstance(e, StartedEvent) for e in events)
    assert any(isinstance(e, CompletedEvent) for e in events)


@pytest.mark.anyio
async def test_jsonl_stream_state_tracks_events(tmp_path) -> None:
    """JsonlStreamState tracks event count, type, and recent events."""
    thread_id = "019b73c4-0c3f-7701-a0bb-aac6b4d8a3bc"

    codex_path = tmp_path / "codex"
    codex_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "\n"
        "sys.stdin.read()\n"
        f"print(json.dumps({{'type': 'thread.started', 'thread_id': '{thread_id}'}}), flush=True)\n"
        "print(json.dumps({'type': 'item.started', 'item': {'id': 'i1', 'type': 'function_call', 'name': 'shell', 'arguments': 'echo hi'}}), flush=True)\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'id': 'i1', 'type': 'function_call_output', 'output': 'hi'}}), flush=True)\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'agent_message', 'text': 'ok'}}), flush=True)\n",
        encoding="utf-8",
    )
    codex_path.chmod(0o755)

    runner = CodexRunner(codex_cmd=str(codex_path), extra_args=[])
    events = [evt async for evt in runner.run("hi", None)]

    # Verify stream tracking
    stream = runner.current_stream
    assert stream is not None
    assert stream.event_count >= 3  # at least the 3 events above
    assert stream.last_stdout_at > 0
    assert len(stream.recent_events) > 0
    # last event type should be set
    assert stream.last_event_type is not None

    # Verify PID was injected into StartedEvent meta
    started = next(e for e in events if isinstance(e, StartedEvent))
    assert started.meta is not None
    assert "pid" in started.meta
    assert isinstance(started.meta["pid"], int)


@pytest.mark.anyio
async def test_jsonl_stream_state_skips_control_channel_events(tmp_path) -> None:
    """#502: control_request / control_response events on stdout must not
    overwrite ``stream.last_event_type``. They are permission-flow traffic,
    not stream-result events, so the session.summary should reflect the
    last actual stream event. ``recent_events`` still records them for
    diagnostics."""
    thread_id = "019b73c4-0c3f-7701-a0bb-aac6b4d8a3bc"

    codex_path = tmp_path / "codex"
    codex_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "\n"
        "sys.stdin.read()\n"
        f"print(json.dumps({{'type': 'thread.started', 'thread_id': '{thread_id}'}}), flush=True)\n"
        # A real stream event — should be reflected in last_event_type
        "print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'agent_message', 'text': 'ok'}}), flush=True)\n"
        # Control-channel chatter — must NOT overwrite last_event_type
        "print(json.dumps({'type': 'control_request', 'request_id': 'req_1', 'request': {'subtype': 'mcp_status'}}), flush=True)\n"
        "print(json.dumps({'type': 'control_response', 'request_id': 'req_1', 'response': {'subtype': 'success'}}), flush=True)\n",
        encoding="utf-8",
    )
    codex_path.chmod(0o755)

    runner = CodexRunner(codex_cmd=str(codex_path), extra_args=[])
    _ = [evt async for evt in runner.run("hi", None)]

    stream = runner.current_stream
    assert stream is not None
    # last_event_type reflects the last *stream* event, not the control chatter
    assert stream.last_event_type == "item.completed"
    # But the control events ARE still recorded in recent_events for diagnostics
    recent_labels = [label for (_ts, label) in stream.recent_events]
    assert "control_request" in recent_labels
    assert "control_response" in recent_labels


@pytest.mark.anyio
async def test_liveness_stall_increments_counter(tmp_path) -> None:
    """#494-A: subprocess.liveness_stall increments stream.liveness_stalls so
    session.summary can surface the subprocess-health canary independently of
    the user-facing _total_stall_warn_count. Today `liveness_warned` latches
    after the first warning, so this field will be 0 or 1 per run.

    #494-B: the warning's cpu_active field should be a real bool (True/False)
    once the baseline prev_diag is populated at watchdog poll start — not None
    as observed in the rc13 audit.
    """
    from structlog.testing import capture_logs

    thread_id = "019b73c4-0c3f-7701-a0bb-aac6b4d8a3bc"

    codex_path = tmp_path / "codex"
    # Emit one event (sets last_stdout_at > 0), then sleep past the threshold
    # so the liveness watchdog fires. After the sleep the script exits cleanly
    # so the test doesn't hang.
    codex_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "import time\n"
        "\n"
        "sys.stdin.read()\n"
        f"print(json.dumps({{'type': 'thread.started', 'thread_id': '{thread_id}'}}), flush=True)\n"
        # Sleep long enough for _LIVENESS_TIMEOUT_SECONDS + _WATCHDOG_POLL to fire
        "time.sleep(1.0)\n",
        encoding="utf-8",
    )
    codex_path.chmod(0o755)

    runner = CodexRunner(codex_cmd=str(codex_path), extra_args=[])
    # Tight timing so the watchdog fires within the test's runtime
    runner._LIVENESS_TIMEOUT_SECONDS = 0.2
    runner._WATCHDOG_POLL_SECONDS = 0.05
    runner._WATCHDOG_GRACE_SECONDS = 0.5

    with capture_logs() as logs:
        with anyio.fail_after(5):
            _ = [evt async for evt in runner.run("hi", None)]

    stream = runner.current_stream
    assert stream is not None
    assert stream.liveness_stalls == 1, (
        f"Expected liveness_stalls=1 after watchdog fired, got {stream.liveness_stalls}"
    )

    # #494-B: cpu_active should be True/False (a real bool comparing prev to
    # curr snapshot), not None. Linux-only assertion since /proc is required
    # for collect_proc_diag to return non-None.
    if sys.platform.startswith("linux"):
        liveness_records = [
            r for r in logs if r.get("event") == "subprocess.liveness_stall"
        ]
        assert len(liveness_records) == 1, (
            f"Expected 1 liveness_stall log record, got {len(liveness_records)}: "
            f"{liveness_records!r}"
        )
        cpu_active = liveness_records[0].get("cpu_active", "MISSING")
        assert isinstance(cpu_active, bool), (
            f"Expected cpu_active to be bool, got {cpu_active!r}"
        )


def test_jsonl_stream_state_defaults() -> None:
    """JsonlStreamState initialises with correct defaults."""
    from untether.runner import JsonlStreamState

    stream = JsonlStreamState(expected_session=None)
    assert stream.last_stdout_at == 0.0
    assert stream.last_event_type is None
    assert stream.last_event_tool is None
    assert stream.event_count == 0
    assert len(stream.recent_events) == 0
    assert stream.stderr_capture == []
    assert stream.proc_returncode is None
    # #494: liveness_stalls canary counter — separate from user-facing
    # _total_stall_warn_count so audits can see subprocess-health hits
    # independently.
    assert stream.liveness_stalls == 0
    # #631 (W5-diag): both default False — set at the claude.py SIGTERM
    # site and mirrored from ClaudeStreamState.background_observed
    # respectively; see runner.empty_result's diagnostic fields.
    assert stream.sigterm_sent is False
    assert stream.background_observed is False


def test_jsonl_stream_state_recent_events_ring_buffer() -> None:
    """Recent events deque respects maxlen=10."""
    from untether.runner import JsonlStreamState

    stream = JsonlStreamState(expected_session=None)
    for i in range(15):
        stream.recent_events.append((float(i), f"type_{i}"))
    assert len(stream.recent_events) == 10
    # Oldest entries should have been evicted
    assert stream.recent_events[0] == (5.0, "type_5")


# ===========================================================================
# #526 rc20 follow-up — watchdog approval-pending awareness
# ===========================================================================


def test_recent_event_is_control_request_true_when_last_label_matches() -> None:
    """#526 rc20: the watchdog uses ``recent_events[-1] == 'control_request'``
    as its approval-pending signal so a session waiting on an
    ExitPlanMode/CanUseTool/AskUserQuestion approval doesn't flood the
    operator dashboard with ``subprocess.liveness_stall`` WARNs.
    """
    from untether.runner import JsonlStreamState, _recent_event_is_control_request

    stream = JsonlStreamState(expected_session=None)
    stream.recent_events.append((1.0, "assistant"))
    stream.recent_events.append((2.0, "control_request"))

    assert _recent_event_is_control_request(stream) is True


def test_recent_event_is_control_request_false_when_resolved() -> None:
    """Once the approval resolves and Claude emits a ``control_response``
    (followed by assistant work), the predicate must report False — the
    session is no longer awaiting user input and a subsequent stall
    SHOULD escalate to the normal WARN path."""
    from untether.runner import JsonlStreamState, _recent_event_is_control_request

    stream = JsonlStreamState(expected_session=None)
    stream.recent_events.append((1.0, "control_request"))
    stream.recent_events.append((2.0, "control_response"))
    stream.recent_events.append((3.0, "assistant"))

    assert _recent_event_is_control_request(stream) is False


def test_recent_event_is_control_request_false_when_buffer_empty() -> None:
    """A fresh subprocess with no JSONL events yet is not approval-pending
    — return False rather than raising IndexError."""
    from untether.runner import JsonlStreamState, _recent_event_is_control_request

    stream = JsonlStreamState(expected_session=None)
    assert _recent_event_is_control_request(stream) is False


def test_approval_pending_refire_constant_is_30_min() -> None:
    """rc19 picked a 30-minute pacing window for the bridge-side INFO. The
    watchdog (rc20 follow-up) reuses the SAME constant so both detectors
    agree on the heartbeat cadence — operators don't see one INFO per
    detector per session per stall window."""
    from untether.runner import _APPROVAL_PENDING_REFIRE_S

    assert _APPROVAL_PENDING_REFIRE_S == 1800.0


@pytest.mark.anyio
async def test_watchdog_demotes_to_approval_pending_when_control_request_recent(
    tmp_path,
) -> None:
    """#526 rc20 follow-up: when the most recent JSONL event in
    ``stream.recent_events`` is ``control_request`` (Claude awaiting an
    approval), the watchdog must emit ``subprocess.approval_pending``
    INFO instead of the ``subprocess.liveness_stall`` WARN. This is what
    stops ``untether-issue-watcher`` from auto-filing GitHub issues on
    routine approval-pending sessions.
    """
    from structlog.testing import capture_logs

    thread_id = "019b73c4-0c3f-7701-a0bb-aac6b4d8a3bc"
    codex_path = tmp_path / "codex"
    # Emit the codex thread.started event (so the runner stays alive past
    # the schema bootstrap) and then a ``control_request``-typed line so
    # recent_events[-1] is ``"control_request"`` when the watchdog fires.
    # Then sleep past the liveness threshold so the watchdog fires.
    codex_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "import time\n"
        "\n"
        "sys.stdin.read()\n"
        f"print(json.dumps({{'type': 'thread.started', 'thread_id': '{thread_id}'}}), flush=True)\n"
        "print(json.dumps({'type': 'control_request', 'request_id': 'req_1'}), flush=True)\n"
        "time.sleep(1.0)\n",
        encoding="utf-8",
    )
    codex_path.chmod(0o755)

    runner = CodexRunner(codex_cmd=str(codex_path), extra_args=[])
    runner._LIVENESS_TIMEOUT_SECONDS = 0.2
    runner._WATCHDOG_POLL_SECONDS = 0.05
    runner._WATCHDOG_GRACE_SECONDS = 0.5

    with capture_logs() as logs:
        with anyio.fail_after(5):
            _ = [evt async for evt in runner.run("hi", None)]

    stream = runner.current_stream
    assert stream is not None

    # The WARN must NOT have been emitted.
    liveness_warns = [r for r in logs if r.get("event") == "subprocess.liveness_stall"]
    assert liveness_warns == [], (
        f"Watchdog must demote WARN to INFO when control_request is most "
        f"recent, got: {liveness_warns}"
    )

    # The INFO replacement MUST have been emitted exactly once.
    approval_infos = [
        r for r in logs if r.get("event") == "subprocess.approval_pending"
    ]
    assert len(approval_infos) == 1, (
        f"Expected exactly 1 subprocess.approval_pending INFO, "
        f"got {len(approval_infos)}: {approval_infos!r}"
    )
    assert approval_infos[0].get("approval_pending") is True
    assert approval_infos[0].get("source") == "watchdog"
    # The latch (liveness_stalls counter) must NOT have been bumped — that
    # field is reserved for the WARN path so session.summary still reflects
    # approval-pending separately from actual liveness fires.
    assert stream.liveness_stalls == 0


@pytest.mark.anyio
async def test_watchdog_warn_still_fires_when_no_control_request(tmp_path) -> None:
    """The rc20 follow-up must NOT silence the WARN for genuinely-hung
    sessions. When the most recent event is a plain ``assistant`` (or
    anything that's not ``control_request``), keep the existing WARN +
    liveness_stalls counter behaviour."""
    from structlog.testing import capture_logs

    thread_id = "019b73c4-0c3f-7701-a0bb-aac6b4d8a3bd"
    codex_path = tmp_path / "codex"
    # No control_request — last recent_event will be the thread.started.
    codex_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "import time\n"
        "\n"
        "sys.stdin.read()\n"
        f"print(json.dumps({{'type': 'thread.started', 'thread_id': '{thread_id}'}}), flush=True)\n"
        "time.sleep(1.0)\n",
        encoding="utf-8",
    )
    codex_path.chmod(0o755)

    runner = CodexRunner(codex_cmd=str(codex_path), extra_args=[])
    runner._LIVENESS_TIMEOUT_SECONDS = 0.2
    runner._WATCHDOG_POLL_SECONDS = 0.05
    runner._WATCHDOG_GRACE_SECONDS = 0.5

    with capture_logs() as logs:
        with anyio.fail_after(5):
            _ = [evt async for evt in runner.run("hi", None)]

    stream = runner.current_stream
    assert stream is not None
    assert stream.liveness_stalls == 1

    # WARN fired exactly once with approval_pending=False as the new
    # disambiguating field.
    liveness_warns = [r for r in logs if r.get("event") == "subprocess.liveness_stall"]
    assert len(liveness_warns) == 1
    assert liveness_warns[0].get("approval_pending") is False

    # No approval-pending INFO.
    approval_infos = [
        r for r in logs if r.get("event") == "subprocess.approval_pending"
    ]
    assert approval_infos == []


# ===========================================================================
# Phase 2e: _ResumeLineProxy.current_stream forwarding (#98)
# ===========================================================================


def test_resume_line_proxy_current_stream_forwarding() -> None:
    """_ResumeLineProxy.current_stream returns inner runner's stream."""
    from untether.runner import JsonlStreamState
    from untether.telegram.commands.executor import _ResumeLineProxy

    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    stream = JsonlStreamState(expected_session=None)
    runner.current_stream = stream

    proxy = _ResumeLineProxy(runner=runner)
    assert proxy.current_stream is stream


def test_resume_line_proxy_current_stream_none() -> None:
    """_ResumeLineProxy.current_stream returns None when runner has no stream."""
    from untether.telegram.commands.executor import _ResumeLineProxy

    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    runner.current_stream = None

    proxy = _ResumeLineProxy(runner=runner)
    assert proxy.current_stream is None


def test_resume_line_proxy_current_stream_no_attr() -> None:
    """_ResumeLineProxy.current_stream returns None for runners without the attr."""
    from untether.runners.mock import MockRunner
    from untether.telegram.commands.executor import _ResumeLineProxy

    runner = MockRunner(engine="mock")
    proxy = _ResumeLineProxy(runner=runner)
    assert proxy.current_stream is None


# ===========================================================================
# #505 — base runner _iter_jsonl_events breaks after CompletedEvent
# ===========================================================================


@pytest.mark.anyio
async def test_base_iter_jsonl_breaks_on_did_emit_completed() -> None:
    """Base ``_iter_jsonl_events`` must stop reading stdout after a
    CompletedEvent. Without the break, a child process inheriting the
    stdout fd (e.g. MCP server, backgrounded shell) would keep the pipe
    open and the loop would block on ``iter_json_lines`` waiting for an
    EOF that never comes.

    Validates the fix for #505 by replacing ``iter_json_lines`` with a
    stub that yields a ``TurnCompleted`` line then a ``hang`` event that
    never fires. Without the break, the test would deadlock.
    """
    import anyio
    import structlog

    from untether.runner import JsonlStreamState

    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    state = runner.new_state("hi", ResumeToken(engine=CODEX_ENGINE, value="sid"))

    completed_line = (
        b'{"type":"turn.completed","turn_id":"t1","usage":{"input_tokens":1,'
        b'"cached_input_tokens":0,"output_tokens":1,"reasoning_output_tokens":0,'
        b'"total_tokens":2}}'
    )

    async def fake_iter_json_lines(_stream):
        yield completed_line
        # Without the break, the runner would await this event forever and the
        # test would hang past the fail_after deadline.
        await anyio.Event().wait()
        yield b"never reached"

    runner.iter_json_lines = fake_iter_json_lines  # type: ignore[assignment]

    stream = JsonlStreamState(expected_session=None)
    logger = structlog.get_logger()

    with anyio.fail_after(2.0):
        events: list[UntetherEvent] = [
            evt
            async for evt in runner._iter_jsonl_events(
                stdout=None,
                stream=stream,
                state=state,
                resume=None,
                logger=logger,
                pid=1234,
            )
        ]

    assert stream.did_emit_completed is True
    assert any(isinstance(e, CompletedEvent) for e in events)
