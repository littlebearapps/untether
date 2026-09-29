"""#777: live background-task status — rendering, throttle, status lifecycle.

Renders over the native task map (``ClaudeTask`` from #776); tasks here are
real ``ClaudeTask`` objects so the duck-typed fields can't drift from the
runner's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest

from tests.telegram_fakes import FakeTransport
from untether.background_status import (
    BackgroundStatusManager,
    BackgroundStatusPanel,
    chat_live_background_count,
    escape_markdown,
    format_bg_elapsed,
    format_done_row,
    format_live_row,
    format_tokens,
    register_live_count_source,
    render_background_block,
    unregister_live_count_source,
)
from untether.commands import CommandContext
from untether.markdown import MarkdownFormatter, MarkdownParts
from untether.progress import ProgressState, ProgressTracker
from untether.runner_bridge import ProgressEdits
from untether.runners.claude import ClaudeTask
from untether.settings import ProgressSettings
from untether.telegram.commands.ping import BACKEND as PING
from untether.telegram.render import prepare_telegram
from untether.transport import MessageRef

pytestmark = pytest.mark.anyio


def _agent(
    tid: str = "a1",
    desc: str = "verifier",
    *,
    started: float = 0.0,
    tokens: int | None = 52_470,
    tools: int | None = 7,
    step: str | None = "Running tests",
    status: str = "running",
    ended: float | None = None,
) -> ClaudeTask:
    usage: dict[str, Any] | None = None
    if tokens is not None or tools is not None:
        usage = {}
        if tokens is not None:
            usage["total_tokens"] = tokens
        if tools is not None:
            usage["tool_uses"] = tools
    return ClaudeTask(
        task_id=tid,
        task_type="local_agent",
        description=desc,
        is_backgrounded=True,
        status=status,
        started_at=started,
        ended_at=ended,
        last_usage=usage,
        last_step=step,
    )


def _bash(
    tid: str = "b1",
    desc: str = "gh run watch",
    *,
    started: float = 0.0,
    status: str = "running",
    ended: float | None = None,
) -> ClaudeTask:
    return ClaudeTask(
        task_id=tid,
        task_type="local_bash",
        description=desc,
        is_backgrounded=True,
        status=status,
        started_at=started,
        ended_at=ended,
    )


# ── formatting ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0"),
        (950, "950"),
        (1_000, "1k"),
        (9_500, "9.5k"),
        (38_104, "38k"),
        (52_470, "52k"),
        (999_999, "1M"),
        (1_234_567, "1.2M"),
        (12_600_000, "13M"),
    ],
)
def test_format_tokens(value: int, expected: str) -> None:
    assert format_tokens(value) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (45, "45s"), (65, "1m05s"), (192, "3m12s"), (3900, "1h05m")],
)
def test_format_bg_elapsed(seconds: float, expected: str) -> None:
    assert format_bg_elapsed(seconds) == expected


def test_agent_row_has_elapsed_tokens_tools_and_step() -> None:
    row = format_live_row(_agent(started=100.0), now=292.0)
    assert row == "🤖 verifier · 3m12s · 52k tok · 7 tools · Running tests"


def test_agent_row_before_first_progress_event() -> None:
    row = format_live_row(_agent(tokens=None, tools=None, step=None), now=5.0)
    assert row == "🤖 verifier · 5s"


def test_single_tool_is_singular() -> None:
    assert "1 tool ·" in format_live_row(_agent(tools=1), now=1.0)


def test_bash_row_is_description_and_elapsed() -> None:
    assert format_live_row(_bash(started=0.0), now=65.0) == "🐚 gh run watch · 1m05s"


def test_long_description_and_step_are_shortened_to_one_line() -> None:
    task = _agent(desc="line one\nline two " + "x" * 200, step="Running " + "y" * 200)
    row = format_live_row(task, now=1.0)
    assert "\n" not in row
    label, *_rest, step = row.split(" · ")
    assert len(label) <= 2 + 60 and label.endswith("…")
    assert len(step) <= 40


@pytest.mark.parametrize(
    ("status", "mark", "word"),
    [
        ("completed", "✅", "done"),
        ("ended", "✅", "done"),
        ("failed", "❌", "failed"),
        ("killed", "⏹️", "stopped"),
        ("stopped", "⏹️", "stopped"),
        ("running", "⏹️", "stopped"),  # the process went away first
    ],
)
def test_done_row_marks(status: str, mark: str, word: str) -> None:
    task = _agent(status=status, started=0.0, ended=260.0, tokens=61_000)
    label = "verifier" if word == "done" else f"verifier {word}"
    assert format_done_row(task, now=999.0) == f"{mark} {label} · 4m20s · 61k tok"


def test_done_row_never_says_done_twice() -> None:
    task = _agent(desc="Run python sleep 40 agent done", status="completed", ended=50.0)
    row = format_done_row(task, now=99.0)
    assert row == "✅ Run python sleep 40 agent done · 50s · 52k tok"


def test_block_lists_only_live_top_level_background_tasks() -> None:
    nested = ClaudeTask(
        task_id="n1",
        task_type="local_bash",
        description="subagent's own tool",
        is_backgrounded=False,
        owned_by_subagent=True,
    )
    fg = ClaudeTask(task_id="f1", task_type="local_bash", is_backgrounded=False)
    done = _bash("b0", "old", status="completed", ended=1.0)
    block = render_background_block(
        [_agent(started=0.0), _bash(started=1.0), nested, fg, done], now=60.0
    )
    assert block is not None
    lines = block.split("  \n")
    assert lines[0] == "⏳ background (2)"
    assert lines[1].startswith("🤖 verifier · 1m00s")
    assert lines[2] == "🐚 gh run watch · 59s"
    assert len(lines) == 3


def test_block_is_none_without_live_background_work() -> None:
    assert render_background_block([], now=0.0) is None
    assert (
        render_background_block([_bash(status="completed", ended=1.0)], now=2.0) is None
    )


def test_block_row_cap_adds_more_line() -> None:
    tasks = [_bash(f"b{i}", f"job {i}", started=float(i)) for i in range(8)]
    block = render_background_block(tasks, now=100.0, max_rows=3)
    assert block is not None
    lines = block.split("  \n")
    assert lines[0] == "⏳ background (8)"
    assert [ln.split(" · ")[0] for ln in lines[1:4]] == [
        "🐚 job 0",
        "🐚 job 1",
        "🐚 job 2",
    ]
    assert lines[4] == "+5 more"


def test_block_escapes_markdown_in_descriptions() -> None:
    task = _bash(desc="**bold** [x](http://evil.example) <b>hi</b> `code`")
    block = render_background_block([task], now=1.0)
    assert block is not None
    text, entities = prepare_telegram(MarkdownParts(header="working", body=block))
    assert "**bold** [x](http://evil.example) <b>hi</b> `code`" in text
    assert not [e for e in entities if e["type"] in {"bold", "code", "italic"}]
    # A bare URL may still be linkified, but only as itself — the escaped
    # ``[x](…)`` can't hide a link behind other text.
    for e in entities:
        if e["type"] == "text_link":
            shown = text.encode("utf-16-le")[
                e["offset"] * 2 : (e["offset"] + e["length"]) * 2
            ].decode("utf-16-le")
            assert shown == e["url"]


def test_escape_markdown_is_reversible_by_the_renderer() -> None:
    raw = r"a\b*c_d[e]f(g)h~i|j<k>l#m!n&o`p"
    text, _ = prepare_telegram(MarkdownParts(header=escape_markdown(raw)))
    assert text == raw


def test_formatter_appends_block_below_actions() -> None:
    state = ProgressState(
        engine="claude",
        action_count=0,
        actions=(),
        resume=None,
        resume_line=None,
        context_line=None,
        background="⏳ background (1)",
    )
    parts = MarkdownFormatter().render_progress_parts(state, elapsed_s=1.0)
    assert parts.body == "⏳ background (1)"


def test_progress_settings_defaults_and_bounds() -> None:
    settings = ProgressSettings()
    assert settings.show_background_tasks is True
    assert settings.background_tasks_max_rows == 5
    with pytest.raises(ValueError, match="background_tasks_max_rows"):
        ProgressSettings(background_tasks_max_rows=0)


# ── pre-result block through ProgressEdits ─────────────────────────────────


@dataclass
class _Presenter:
    rendered: list[ProgressState] = field(default_factory=list)

    def render_progress(self, state, *, elapsed_s, label="working", now=None):
        from untether.transport import RenderedMessage

        self.rendered.append(state)
        return RenderedMessage(text=state.background or "")

    def render_final(self, state, *, elapsed_s, status, answer):  # pragma: no cover
        raise AssertionError


def _edits(presenter: _Presenter, transport: FakeTransport) -> ProgressEdits:
    return ProgressEdits(
        transport=transport,
        presenter=presenter,  # type: ignore[arg-type]
        channel_id=1,
        progress_ref=MessageRef(channel_id=1, message_id=5),
        tracker=ProgressTracker(engine="claude"),
        started_at=0.0,
        clock=lambda: 10.0,
        last_rendered=None,
    )


async def test_progress_render_includes_background_block() -> None:
    presenter = _Presenter()
    transport = FakeTransport()
    edits = _edits(presenter, transport)
    edits.background_provider = lambda: "⏳ background (1)"
    edits._bump_heartbeat()
    with anyio.move_on_after(0.5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(edits._run_loop, tg)
            while not presenter.rendered:
                await anyio.sleep(0.01)
            tg.cancel_scope.cancel()
    assert presenter.rendered[0].background == "⏳ background (1)"
    assert transport.edit_calls[0]["message"].text == "⏳ background (1)"


async def test_broken_provider_never_breaks_the_render() -> None:
    presenter = _Presenter()
    edits = _edits(presenter, FakeTransport())

    def boom() -> str:
        raise RuntimeError("x")

    edits.background_provider = boom
    assert edits._background_block() is None


def test_heartbeat_refreshes_while_background_tasks_live() -> None:
    edits = _edits(_Presenter(), FakeTransport())
    edits.background_provider = lambda: "⏳ background (1)"
    before = edits.event_seq
    edits._heartbeat_tick()
    assert edits.event_seq == before + 1
    # Once the final answer is being delivered the block stops repainting.
    edits._finalizing = True
    edits._heartbeat_tick()
    assert edits.event_seq == before + 1


def test_heartbeat_quiet_without_background_tasks() -> None:
    edits = _edits(_Presenter(), FakeTransport())
    edits.background_provider = lambda: None
    before = edits.event_seq
    edits._heartbeat_tick()
    assert edits.event_seq == before


# ── post-result status message ─────────────────────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _panel(
    transport: FakeTransport, clock: _Clock, tmp_path: Path | None = None
) -> BackgroundStatusPanel:
    return BackgroundStatusPanel(
        transport=transport,
        channel_id=123,
        clock=clock,
        persistence_path=None if tmp_path is None else tmp_path / "ap.json",
    )


async def test_status_message_lifecycle(tmp_path: Path) -> None:
    """create → throttled edit → early edit on a completion → finalise."""
    from untether.telegram.progress_persistence import load_active_progress

    transport = FakeTransport()
    clock = _Clock()
    agent = _agent(started=clock.t - 60)
    bash = _bash(started=clock.t - 30)
    panel = _panel(transport, clock, tmp_path)
    panel.track([agent, bash])
    assert await panel.open(MessageRef(channel_id=123, message_id=10))

    sent = transport.send_calls[0]
    assert sent["options"].notify is False
    assert sent["options"].reply_to.message_id == 10
    assert "entities" not in sent["message"].extra  # plain text
    assert sent["message"].text.splitlines()[0] == "⏳ background (2)"
    assert load_active_progress(tmp_path / "ap.json") == {
        "123:1": {"chat_id": 123, "message_id": 1}
    }

    # Nothing changed and the throttle hasn't elapsed: no edit.
    clock.t += 10
    await panel.sync([agent, bash])
    assert transport.edit_calls == []
    # The throttle elapsed: refresh elapsed / tokens.
    clock.t += 21
    agent.last_usage = {"total_tokens": 61_000, "tool_uses": 9}
    await panel.sync([agent, bash])
    assert len(transport.edit_calls) == 1
    assert "61k tok · 9 tools" in transport.edit_calls[-1]["message"].text

    # A task finishing edits early (only the min-edit spacing applies).
    clock.t += 3
    bash.status, bash.ended_at = "completed", clock.t
    await panel.sync([agent, bash])
    assert len(transport.edit_calls) == 2
    text = transport.edit_calls[-1]["message"].text
    assert text.splitlines()[0] == "⏳ background (1) · 1 done"
    assert "✅ gh run watch · 1m04s" in text

    # The set empties: finalised, persistence entry dropped.
    clock.t += 5
    agent.status, agent.ended_at = "completed", clock.t
    await panel.sync([agent, bash])
    final = transport.edit_calls[-1]["message"].text
    assert final.splitlines()[0] == "✅ all 2 background tasks done"
    assert "✅ verifier · 1m39s · 61k tok" in final
    assert panel.finalised
    assert load_active_progress(tmp_path / "ap.json") == {}
    # Finalised panels don't edit again.
    await panel.sync([agent, bash])
    assert transport.edit_calls[-1]["message"].text == final


async def test_early_edit_respects_min_spacing() -> None:
    transport = FakeTransport()
    clock = _Clock()
    a, b = _bash("b1", started=clock.t), _bash("b2", "second", started=clock.t)
    panel = _panel(transport, clock)
    panel.track([a, b])
    await panel.open(None)
    clock.t += 0.5
    a.status, a.ended_at = "completed", clock.t
    await panel.sync([a, b])
    assert transport.edit_calls == []  # too soon after the last write
    clock.t += 2
    await panel.sync([a, b])
    assert len(transport.edit_calls) == 1


async def test_panel_picks_up_tasks_launched_later() -> None:
    transport = FakeTransport()
    clock = _Clock()
    first = _bash("b1", started=clock.t)
    panel = _panel(transport, clock)
    panel.track([first])
    await panel.open(None)
    clock.t += 5
    later = _agent("a2", "late agent", started=clock.t)
    await panel.sync([first, later])
    assert "late agent" in transport.edit_calls[-1]["message"].text
    assert transport.edit_calls[-1]["message"].text.startswith("⏳ background (2)")


async def test_panel_row_cap_for_live_and_done() -> None:
    transport = FakeTransport()
    clock = _Clock()
    tasks = [_bash(f"b{i}", f"job {i}", started=float(i)) for i in range(9)]
    panel = _panel(transport, clock)
    panel.max_rows = 3
    panel.track(tasks)
    await panel.open(None)
    lines = transport.send_calls[0]["message"].text.splitlines()
    assert lines[0] == "⏳ background (9)"
    assert lines[4] == "+6 more"
    for t in tasks:
        t.status, t.ended_at = "completed", 50.0
    await panel.finalise()
    final = transport.edit_calls[-1]["message"].text.splitlines()
    assert final[0] == "✅ all 9 background tasks done"
    assert final[1] == "+6 more ended"
    assert len(final) == 5


async def test_close_with_live_tasks_marks_them_stopped_with_reason() -> None:
    transport = FakeTransport()
    clock = _Clock()
    done = _bash("b1", "quick", started=clock.t, status="completed", ended=clock.t)
    live = _agent(started=clock.t)
    panel = _panel(transport, clock)
    panel.tasks = {"b1": done, "a1": live}
    await panel.open(None)
    await panel.finalise("max_hold")
    final = transport.edit_calls[-1]["message"].text
    assert final.splitlines()[0] == (
        "⏹️ background tasks ended · 1 done · 1 stopped (background hold limit reached)"
    )
    assert "⏹️ verifier stopped" in final
    assert "running" not in final


async def test_single_task_final_header() -> None:
    transport = FakeTransport()
    clock = _Clock()
    task = _bash(started=clock.t)
    panel = _panel(transport, clock)
    panel.track([task])
    await panel.open(None)
    task.status, task.ended_at = "completed", clock.t + 1
    await panel.sync([task])
    assert transport.edit_calls[-1]["message"].text.startswith(
        "✅ background task done\n"
    )


class _Store:
    def __init__(self, tasks: list[Any] | None = None) -> None:
        self.tasks = tasks or []


def _manager(
    transport: FakeTransport,
    store: _Store,
    *,
    settings: ProgressSettings | None = None,
    anchor: MessageRef | None = None,
) -> BackgroundStatusManager:
    return BackgroundStatusManager(
        transport=transport,
        channel_id=123,
        tasks_source=lambda: store.tasks,
        anchor_for=lambda _live: anchor,
        settings_source=lambda: settings or ProgressSettings(),
        poll_s=0.01,
    )


async def test_manager_opens_only_with_live_background_work() -> None:
    transport = FakeTransport()
    store = _Store([_bash(status="completed", ended=1.0)])
    manager = _manager(transport, store)
    await manager.after_turn()
    assert transport.send_calls == []
    store.tasks.append(_agent())
    await manager.after_turn()
    assert len(transport.send_calls) == 1
    # A second turn while the panel is active doesn't open another message.
    await manager.after_turn()
    assert len(transport.send_calls) == 1
    assert manager.panels_opened == 1


async def test_manager_respects_show_background_tasks_off() -> None:
    transport = FakeTransport()
    manager = _manager(
        transport,
        _Store([_agent()]),
        settings=ProgressSettings(show_background_tasks=False),
    )
    await manager.after_turn()
    assert transport.send_calls == []


async def test_manager_replies_to_anchor_and_opens_new_panel_for_new_batch() -> None:
    transport = FakeTransport()
    task = _bash(started=0.0)
    store = _Store([task])
    anchor = MessageRef(channel_id=123, message_id=42)
    manager = _manager(transport, store, anchor=anchor)
    await manager.after_turn()
    assert transport.send_calls[0]["options"].reply_to == anchor
    task.status, task.ended_at = "completed", 1.0
    await manager.poll_once()
    assert manager.panel is not None and manager.panel.finalised
    # A later turn launches new background work: a fresh status message.
    store.tasks.append(_agent("a9", "second batch"))
    await manager.after_turn()
    assert manager.panels_opened == 2
    assert "second batch" in transport.send_calls[-1]["message"].text


async def test_manager_run_loop_polls_and_aclose_finalises() -> None:
    transport = FakeTransport()
    task = _agent()
    manager = _manager(transport, _Store([task]))
    await manager.after_turn()
    async with anyio.create_task_group() as tg:
        tg.start_soon(manager.run)
        await anyio.sleep(0.05)
        tg.cancel_scope.cancel()
    await manager.aclose("cancel")
    final = transport.edit_calls[-1]["message"].text
    assert final.startswith("⏹️ background tasks ended · 1 stopped (cancelled)")
    await manager.aclose("cancel")  # idempotent
    assert transport.edit_calls[-1]["message"].text == final


async def test_manager_hot_reloads_row_cap() -> None:
    transport = FakeTransport()
    tasks = [_bash(f"b{i}", f"job {i}", started=float(i)) for i in range(4)]
    settings = ProgressSettings()
    manager = BackgroundStatusManager(
        transport=transport,
        channel_id=123,
        tasks_source=lambda: tasks,
        anchor_for=lambda _live: None,
        settings_source=lambda: settings,
    )
    await manager.after_turn()
    assert "+" not in transport.send_calls[0]["message"].text
    settings = ProgressSettings(background_tasks_max_rows=2)
    tasks.append(_bash("b9", "job 9", started=9.0))
    await manager.after_turn()
    assert "+3 more" in transport.edit_calls[-1]["message"].text


# ── /ping ─────────────────────────────────────────────────────────────────


def _ping_ctx(chat_id: int) -> CommandContext:
    return CommandContext(
        command="ping",
        text="/ping",
        args_text="",
        args=(),
        message=MessageRef(channel_id=chat_id, message_id=1),
        reply_to=None,
        reply_text=None,
        config_path=None,
        plugin_config={},
        runtime=AsyncMock(),
        executor=AsyncMock(),
    )


async def test_ping_shows_the_chats_live_background_count() -> None:
    token_a = register_live_count_source(77, lambda: 2)
    token_b = register_live_count_source(88, lambda: 5)
    try:
        assert chat_live_background_count(77) == 2
        result = await PING.handle(_ping_ctx(77))
        assert "⏳ background: 2 tasks running" in result.text
    finally:
        unregister_live_count_source(token_a)
        unregister_live_count_source(token_b)
    result = await PING.handle(_ping_ctx(77))
    assert "background" not in result.text


# ── #785 part 2: wake-turn consolidation ─────────────────────────────────────

from untether.background_status import (  # noqa: E402
    FOLD_MAX_CHARS,
    wake_fold_decision,
)


def _decide(**overrides: Any) -> str:
    values: dict[str, Any] = {
        "reason": "task_finished",
        "ok": True,
        "answer": "Sweep one is back; waiting on the others.",
        "substantive_actions": 0,
        "already_announced": False,
        "live_tasks_remaining": 2,
        "batch_announced": False,
    }
    values.update(overrides)
    return wake_fold_decision(**values)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "fold"),
        ({"reason": "unknown"}, "fold"),
        ({"reason": "scheduled_wakeup", "live_tasks_remaining": 0}, "fold"),
        ({"reason": "monitor_event"}, "fold"),
        ({"reason": "followup"}, "not_wake"),
        ({"ok": False}, "error"),
        ({"substantive_actions": 1}, "tools"),
        ({"answer": "x" * (FOLD_MAX_CHARS + 1)}, "long_answer"),
        ({"answer": "x" * FOLD_MAX_CHARS}, "fold"),
        ({"answer": ""}, "fold"),
        # the last task's turn is the report: it breaks out…
        ({"live_tasks_remaining": 0}, "last_task"),
        ({"reason": "unknown", "live_tasks_remaining": 0}, "last_task"),
        # …but its restatement folds once the batch has pushed,
        (
            {
                "live_tasks_remaining": 0,
                "already_announced": True,
                "batch_announced": True,
            },
            "fold",
        ),
        # and carries the push when the report itself folded (raced).
        (
            {"live_tasks_remaining": 0, "already_announced": True},
            "last_task",
        ),
        # substantive content always wins over "last task"
        ({"live_tasks_remaining": 0, "substantive_actions": 2}, "tools"),
    ],
)
def test_wake_fold_decision(overrides: dict[str, Any], expected: str) -> None:
    assert _decide(**overrides) == expected


async def _open_panel(transport: FakeTransport, clock: _Clock, *tasks: Any):
    panel = _panel(transport, clock)
    panel.track(tasks)
    await panel.open(None)
    return panel


async def test_fold_puts_the_ack_on_the_task_row_in_full() -> None:
    transport = FakeTransport()
    clock = _Clock()
    a1, a2 = _agent("a1", "sweep one"), _agent("a2", "sweep two")
    panel = await _open_panel(transport, clock, a1, a2)
    a1.status, a1.ended_at = "completed", clock.t + 60
    ack = "Sweep one is back —\nwaiting on sweep two. " + "y" * 200
    assert await panel.fold(ack, task_ids=["a1"])
    text = transport.edit_calls[-1]["message"].text
    lines = text.splitlines()
    row = lines.index(next(ln for ln in lines if ln.startswith("✅ sweep one ·")))
    assert lines[row + 1] == "   ↳ " + " ".join(ack.split())
    assert not panel.finalised  # sweep two still running


async def test_fold_without_a_task_is_a_note_then_filed_by_the_restatement() -> None:
    transport = FakeTransport()
    clock = _Clock()
    a1, a2 = _agent("a1", "sweep one"), _agent("a2", "sweep two")
    panel = await _open_panel(transport, clock, a1, a2)
    assert await panel.fold("Sweep one is back.")
    assert transport.edit_calls[-1]["message"].text.endswith("💬 Sweep one is back.")
    a1.status, a1.ended_at = "completed", clock.t + 1
    assert await panel.fold(
        "Sweep one finished.", task_ids=["a1"], already_announced=True
    )
    lines = transport.edit_calls[-1]["message"].text.splitlines()
    row = lines.index(next(ln for ln in lines if ln.startswith("✅ sweep one ·")))
    assert lines[row + 1 : row + 3] == [
        "   ↳ Sweep one is back.",
        "   ↳ Sweep one finished.",
    ]
    assert not any(ln.startswith("💬") for ln in lines)


async def test_fold_ending_the_last_task_finalises() -> None:
    transport = FakeTransport()
    clock = _Clock()
    a1 = _agent("a1", "sweep one")
    panel = await _open_panel(transport, clock, a1)
    a1.status, a1.ended_at = "completed", clock.t + 1
    assert await panel.fold("done.", task_ids=["a1"], already_announced=True)
    assert panel.finalised
    assert transport.edit_calls[-1]["message"].text.startswith(
        "✅ background task done"
    )


async def test_fold_into_a_finalised_status_message_still_edits_it() -> None:
    transport = FakeTransport()
    clock = _Clock()
    a1 = _bash("b1", "sleep 20", status="completed", ended=clock.t)
    panel = await _open_panel(transport, clock, _bash("b0", started=clock.t))
    panel.tasks = {"b1": a1}
    await panel.finalise()
    assert await panel.fold("Nothing new since then.")
    assert transport.edit_calls[-1]["message"].text.endswith(
        "💬 Nothing new since then."
    )


async def test_fold_refuses_what_it_cannot_show_in_full() -> None:
    transport = FakeTransport()
    clock = _Clock()
    tasks = [_agent(f"a{i}", f"agent {i}") for i in range(12)]
    panel = await _open_panel(transport, clock, *tasks)
    for i, task in enumerate(tasks):
        task.status, task.ended_at = "completed", clock.t + 1
        ok = await panel.fold("z" * 290 + f" {i}", task_ids=[task.task_id])
        if not ok:
            break
    else:  # pragma: no cover
        raise AssertionError("expected the message to fill up")
    # The refused ack left nothing behind, and every accepted one is shown.
    text = transport.edit_calls[-1]["message"].text
    assert len(text) < 3500
    assert text.count("↳") == i
    assert f" {i}" not in text.split("\n")[-1]


async def test_fold_rows_survive_the_row_cap() -> None:
    transport = FakeTransport()
    clock = _Clock()
    tasks = [_bash(f"b{i}", f"job {i}", started=float(i)) for i in range(8)]
    panel = await _open_panel(transport, clock, *tasks)
    panel.max_rows = 2
    for t in tasks:
        t.status, t.ended_at = "completed", 20.0
    assert await panel.fold("first job ack", task_ids=["b0"])
    lines = transport.edit_calls[-1]["message"].text.splitlines()
    assert "✅ job 0 · 20s" in lines  # the oldest row, kept for its ack
    assert "   ↳ first job ack" in lines
    assert "+6 more ended" in lines


async def test_manager_fold_adds_a_known_task_missing_from_the_message() -> None:
    transport = FakeTransport()
    first = _bash("b1", started=0.0)
    store = _Store([first])
    manager = _manager(transport, store)
    await manager.after_turn()
    late = _agent("a9", "late", status="completed", ended=5.0)
    store.tasks.append(late)
    assert await manager.fold("late one done", task_ids=["a9"])
    text = transport.edit_calls[-1]["message"].text
    assert "✅ late ·" in text and "   ↳ late one done" in text


async def test_manager_fold_without_status_message_is_refused() -> None:
    manager = _manager(FakeTransport(), _Store())
    assert manager.fold_target is None
    assert await manager.fold("ack") is False


def test_consolidate_setting_default_on() -> None:
    assert ProgressSettings().consolidate_wake_turns is True
