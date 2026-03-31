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
