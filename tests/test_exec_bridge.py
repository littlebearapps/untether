import contextlib
import sys
import uuid

import anyio
import pytest

from untether.progress import ProgressTracker
from untether.runner_bridge import (
    ExecBridgeConfig,
    IncomingMessage,
    ProgressEdits,
    _EPHEMERAL_MSGS,
    _format_run_cost,
    handle_message,
    register_ephemeral_message,
)
from untether.markdown import MarkdownParts, MarkdownPresenter
from untether.model import ResumeToken, UntetherEvent
from untether.telegram.render import prepare_telegram
from untether.runners.codex import CodexRunner
from untether.runners.mock import Advance, Emit, Raise, Return, ScriptRunner, Wait
from untether.settings import load_settings, require_telegram
from untether.transport import MessageRef, RenderedMessage, SendOptions
from tests.factories import action_completed, action_started

CODEX_ENGINE = "codex"


class FakeTransport:
    def __init__(self) -> None:
        self._next_id = 1
        self.send_calls: list[dict] = []
        self.edit_calls: list[dict] = []
        self.delete_calls: list[MessageRef] = []

    async def send(
        self,
        *,
        channel_id: int | str,
        message: RenderedMessage,
        options: SendOptions | None = None,
    ) -> MessageRef:
        ref = MessageRef(channel_id=channel_id, message_id=self._next_id)
        self._next_id += 1
        self.send_calls.append(
            {
                "ref": ref,
                "channel_id": channel_id,
                "message": message,
                "options": options,
            }
        )
        return ref

    async def edit(
        self, *, ref: MessageRef, message: RenderedMessage, wait: bool = True
    ) -> MessageRef:
        self.edit_calls.append({"ref": ref, "message": message, "wait": wait})
        return ref

    async def delete(self, *, ref: MessageRef) -> bool:
        self.delete_calls.append(ref)
        return True

    async def close(self) -> None:
        return None


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def set(self, value: float) -> None:
        self._now = value


def _return_runner(
    *, answer: str = "ok", resume_value: str | None = None
) -> ScriptRunner:
    return ScriptRunner(
        [Return(answer=answer)],
        engine=CODEX_ENGINE,
        resume_value=resume_value,
    )


def test_require_telegram_rejects_empty_token(tmp_path) -> None:
    from untether.config import ConfigError

    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        'transport = "telegram"\n\n[transports.telegram]\n'
        'bot_token = "   "\nchat_id = 123\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="bot_token"):
        settings, _ = load_settings(config_path)
        require_telegram(settings, config_path)


def test_load_settings_rejects_string_chat_id(tmp_path) -> None:
    from untether.config import ConfigError

    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        'transport = "telegram"\n\n[transports.telegram]\n'
        'bot_token = "token"\nchat_id = "123"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="chat_id"):
        load_settings(config_path)


def test_codex_extract_resume_finds_command() -> None:
    uuid = "019b66fc-64c2-7a71-81cd-081c504cfeb2"
    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    text = f"`codex resume {uuid}`"

    assert runner.extract_resume(text) == ResumeToken(engine=CODEX_ENGINE, value=uuid)


def test_codex_extract_resume_uses_last_resume_line() -> None:
    uuid_first = "019b66fc-64c2-7a71-81cd-081c504cfeb2"
    uuid_last = "123e4567-e89b-12d3-a456-426614174000"
    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    text = f"`codex resume {uuid_first}`\n\n`codex resume {uuid_last}`"

    assert runner.extract_resume(text) == ResumeToken(
        engine=CODEX_ENGINE, value=uuid_last
    )


def test_codex_extract_resume_ignores_malformed_resume_line() -> None:
    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    text = "codex resume"

    assert runner.extract_resume(text) is None


def test_codex_extract_resume_accepts_plain_line() -> None:
    uuid = "019b66fc-64c2-7a71-81cd-081c504cfeb2"
    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    text = f"codex resume {uuid}"

    assert runner.extract_resume(text) == ResumeToken(engine=CODEX_ENGINE, value=uuid)


@pytest.mark.skipif(sys.version_info < (3, 14), reason="uuid.uuid7 requires Python 3.14+")
def test_codex_extract_resume_accepts_uuid7() -> None:
    uuid7 = uuid.uuid7
    token = str(uuid7())
    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    text = f"`codex resume {token}`"

    assert runner.extract_resume(text) == ResumeToken(engine=CODEX_ENGINE, value=token)


def test_prepare_telegram_trims_body_preserves_footer() -> None:
    body_limit = 3500
    parts = MarkdownParts(
        header="header",
        body="x" * (body_limit + 100),
        footer="footer",
    )

    rendered, _ = prepare_telegram(parts)

    chunks = [chunk for chunk in rendered.split("\n\n") if chunk]
    assert chunks[0] == "header"
    assert chunks[-1].rstrip() == "footer"
    assert len(chunks[1]) == body_limit
    assert chunks[1].endswith("…")


def test_prepare_telegram_preserves_entities_on_truncate() -> None:
    body_limit = 3500
    parts = MarkdownParts(
        header="h",
        body="**bold** " + ("x" * (body_limit + 100)),
    )

    _, entities = prepare_telegram(parts)

    assert any(e.get("type") == "bold" for e in entities)


@pytest.mark.anyio
async def test_final_notify_sends_loud_final_message() -> None:
    transport = FakeTransport()
    runner = _return_runner(answer="ok")
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
    )

    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=10, text="hi"),
        resume_token=None,
    )

    assert len(transport.send_calls) == 2
    assert transport.send_calls[0]["options"].notify is False
    assert transport.send_calls[1]["options"].notify is True


@pytest.mark.anyio
async def test_handle_message_strips_resume_line_from_prompt() -> None:
    transport = FakeTransport()
    runner = ScriptRunner([Return(answer="ok")], engine=CODEX_ENGINE)
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
    )
    resume = ResumeToken(engine=CODEX_ENGINE, value="sid")
    text = "do this\n`codex resume sid`\nand that"

    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=10, text=text),
        resume_token=resume,
    )

    assert runner.calls
    prompt, passed_resume = runner.calls[0]
    assert prompt == "do this\nand that"
    assert passed_resume == resume


@pytest.mark.anyio
async def test_long_final_message_edits_progress_message() -> None:
    transport = FakeTransport()
    runner = _return_runner(answer="x" * 10_000)
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=False,
    )

    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=10, text="hi"),
        resume_token=None,
    )

    assert len(transport.send_calls) == 1
    assert transport.send_calls[0]["options"].notify is False
    assert transport.edit_calls
    assert "done" in transport.edit_calls[-1]["message"].text.lower()


@pytest.mark.anyio
async def test_progress_edits_are_best_effort() -> None:
    transport = FakeTransport()
    clock = _FakeClock()
    events: list[UntetherEvent] = [
        action_started("item_0", "command", "echo 1"),
        action_started("item_1", "command", "echo 2"),
    ]
    runner = ScriptRunner(
        [
            Emit(events[0], at=0.2),
            Emit(events[1], at=0.4),
            Advance(1.0),
            Return(answer="ok"),
        ],
        engine=CODEX_ENGINE,
        advance=clock.set,
    )
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
    )

    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=10, text="hi"),
        resume_token=None,
        clock=clock,
    )

    assert transport.edit_calls
    assert all(call["wait"] is False for call in transport.edit_calls)
    assert "working" in transport.edit_calls[-1]["message"].text.lower()


@pytest.mark.anyio
async def test_bridge_flow_sends_progress_edits_and_final_resume() -> None:
    transport = FakeTransport()
    clock = _FakeClock()
    events: list[UntetherEvent] = [
        action_started("item_0", "command", "echo ok"),
        action_completed(
            "item_0",
            "command",
            "echo ok",
            ok=True,
            detail={"exit_code": 0},
        ),
    ]
    session_id = "123e4567-e89b-12d3-a456-426614174000"
    runner = ScriptRunner(
        [
            Emit(events[0], at=0.0),
            Emit(events[1], at=2.1),
            Return(answer="done"),
        ],
        engine=CODEX_ENGINE,
        advance=clock.set,
        resume_value=session_id,
    )
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
    )

    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=42, text="do it"),
        resume_token=None,
        clock=clock,
    )

    assert transport.send_calls[0]["options"].reply_to.message_id == 42
    assert "starting" in transport.send_calls[0]["message"].text
    assert "codex" in transport.send_calls[0]["message"].text
    assert len(transport.edit_calls) >= 1
    assert session_id in transport.send_calls[-1]["message"].text
    assert "codex resume" in transport.send_calls[-1]["message"].text.lower()
    assert transport.send_calls[-1]["options"].replace == transport.send_calls[0]["ref"]


@pytest.mark.anyio
async def test_final_message_includes_ctx_line() -> None:
    transport = FakeTransport()
    clock = _FakeClock()
    session_id = "123e4567-e89b-12d3-a456-426614174000"
    runner = ScriptRunner(
        [Return(answer="done")],
        engine=CODEX_ENGINE,
        resume_value=session_id,
    )
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
    )

    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=42, text="do it"),
        resume_token=None,
        context_line="`ctx: untether @feat/api`",
        clock=clock,
    )

    assert transport.send_calls
    final_text = transport.send_calls[-1]["message"].text
    assert "`ctx: untether @feat/api`" in final_text
    assert "codex resume" in final_text.lower()


@pytest.mark.anyio
async def test_handle_message_cancelled_renders_cancelled_state() -> None:
    transport = FakeTransport()
    session_id = "019b66fc-64c2-7a71-81cd-081c504cfeb2"
    hold = anyio.Event()
    runner = ScriptRunner(
        [Wait(hold)],
        engine=CODEX_ENGINE,
        resume_value=session_id,
    )
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
    )
    running_tasks: dict = {}

    async def run_handle_message() -> None:
        await handle_message(
            cfg,
            runner=runner,
            incoming=IncomingMessage(
                channel_id=123, message_id=10, text="do something"
            ),
            resume_token=None,
            running_tasks=running_tasks,
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_handle_message)
        for _ in range(100):
            if running_tasks:
                break
            await anyio.sleep(0)
        assert running_tasks
        running_task = running_tasks[next(iter(running_tasks))]
        with anyio.fail_after(1):
            await running_task.resume_ready.wait()
        running_task.cancel_requested.set()

    assert len(transport.send_calls) == 1  # Progress message
    assert len(transport.edit_calls) >= 1
    last_edit = transport.edit_calls[-1]["message"].text
    assert "cancelled" in last_edit.lower()
    assert session_id in last_edit


@pytest.mark.anyio
async def test_handle_message_error_preserves_resume_token() -> None:
    transport = FakeTransport()
    session_id = "019b66fc-64c2-7a71-81cd-081c504cfeb2"
    runner = ScriptRunner(
        [Raise(RuntimeError("boom"))],
        engine=CODEX_ENGINE,
        resume_value=session_id,
    )
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
    )

    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=10, text="do something"),
        resume_token=None,
    )

    assert transport.edit_calls
    last_edit = transport.edit_calls[-1]["message"].text
    assert "error" in last_edit.lower()
    assert session_id in last_edit
    assert "codex resume" in last_edit.lower()


# ---------------------------------------------------------------------------
# ProgressEdits ephemeral notification cleanup tests
# ---------------------------------------------------------------------------


class _KeyboardPresenter:
    """Presenter that returns RenderedMessages with controllable inline keyboards."""

    def __init__(self) -> None:
        self.keyboard: list[list[dict]] = [[{"text": "Cancel"}]]

    def set_approval_buttons(self) -> None:
        self.keyboard = [
            [{"text": "Approve"}, {"text": "Deny"}],
            [{"text": "Cancel"}],
        ]

    def set_no_approval(self) -> None:
        self.keyboard = [[{"text": "Cancel"}]]

    def render_progress(self, state, *, elapsed_s, label="working"):
        return RenderedMessage(
            text=f"{label} {elapsed_s:.0f}s",
            extra={"reply_markup": {"inline_keyboard": self.keyboard}},
        )

    def render_final(self, state, *, elapsed_s, status, answer):
        return RenderedMessage(text=f"{status}: {answer}")


def _make_edits(
    transport: FakeTransport,
    presenter: _KeyboardPresenter,
    clock: _FakeClock | None = None,
) -> ProgressEdits:
    if clock is None:
        clock = _FakeClock()
    tracker = ProgressTracker(engine="codex")
    progress_ref = MessageRef(channel_id=123, message_id=1)
    return ProgressEdits(
        transport=transport,
        presenter=presenter,
        channel_id=123,
        progress_ref=progress_ref,
        tracker=tracker,
        started_at=0.0,
        clock=clock,
        last_rendered=None,
    )


@pytest.mark.anyio
async def test_progress_edits_deletes_approval_notification_on_button_disappear() -> (
    None
):
    """When approval buttons disappear, the 'Action required' notification is deleted."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)

    # Simulate event that triggers an edit with approval buttons
    presenter.set_approval_buttons()
    edits.event_seq = 1
    with contextlib.suppress(anyio.WouldBlock):
        edits.signal_send.send_nowait(None)

    async with anyio.create_task_group() as tg:

        async def run_one_cycle() -> None:
            # Let the edit loop run one iteration
            await anyio.sleep(0)
            await anyio.sleep(0)
            # Now remove approval buttons and trigger another iteration
            presenter.set_no_approval()
            edits.event_seq = 2
            with contextlib.suppress(anyio.WouldBlock):
                edits.signal_send.send_nowait(None)
            await anyio.sleep(0)
            await anyio.sleep(0)
            # Close the signal to end the loop
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(run_one_cycle)

    # The notification send + its deletion should have happened
    notification_sends = [
        s for s in transport.send_calls if "approval" in s["message"].text.lower()
    ]
    assert len(notification_sends) == 1
    assert len(transport.delete_calls) == 1
    assert transport.delete_calls[0] == notification_sends[0]["ref"]


@pytest.mark.anyio
async def test_progress_edits_delete_ephemeral_cleans_pending_notification() -> None:
    """delete_ephemeral() cleans up a pending approval notification."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)

    # Manually set a pending notification ref
    notify_ref = MessageRef(channel_id=123, message_id=99)
    edits._approval_notify_ref = notify_ref

    await edits.delete_ephemeral()

    assert len(transport.delete_calls) == 1
    assert transport.delete_calls[0] == notify_ref
    assert edits._approval_notify_ref is None


@pytest.mark.anyio
async def test_progress_edits_delete_ephemeral_noop_when_no_notification() -> None:
    """delete_ephemeral() does nothing when no notification is pending."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)

    await edits.delete_ephemeral()

    assert len(transport.delete_calls) == 0


@pytest.mark.anyio
async def test_progress_edits_delete_ephemeral_drains_registry() -> None:
    """delete_ephemeral() deletes messages registered via the ephemeral registry."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)

    # Register two ephemeral messages for this progress_ref
    feedback_ref_1 = MessageRef(channel_id=123, message_id=50)
    feedback_ref_2 = MessageRef(channel_id=123, message_id=51)
    register_ephemeral_message(123, 1, feedback_ref_1)  # anchor = progress msg id 1
    register_ephemeral_message(123, 1, feedback_ref_2)

    await edits.delete_ephemeral()

    assert len(transport.delete_calls) == 2
    assert feedback_ref_1 in transport.delete_calls
    assert feedback_ref_2 in transport.delete_calls
    # Registry should be drained
    assert (123, 1) not in _EPHEMERAL_MSGS


# ---------------------------------------------------------------------------
# _format_run_cost tests
# ---------------------------------------------------------------------------


class TestFormatRunCost:
    def test_none_usage(self):
        assert _format_run_cost(None) is None

    def test_no_cost(self):
        assert _format_run_cost({"num_turns": 5}) is None

    def test_cost_only(self):
        result = _format_run_cost({"total_cost_usd": 0.15})
        assert result is not None
        assert "$0.15" in result

    def test_small_cost(self):
        result = _format_run_cost({"total_cost_usd": 0.003})
        assert result is not None
        assert "$0.0030" in result

    def test_full_usage(self):
        result = _format_run_cost(
            {
                "total_cost_usd": 1.23,
                "num_turns": 8,
                "duration_ms": 45000,
                "usage": {"input_tokens": 15000, "output_tokens": 3200},
            }
        )
        assert result is not None
        assert "$1.23" in result
        assert "8 turns" in result
        assert "45.0s API" in result
        assert "15.0k in" in result
        assert "3.2k out" in result

    def test_large_token_counts(self):
        result = _format_run_cost(
            {
                "total_cost_usd": 5.00,
                "usage": {"input_tokens": 1500000, "output_tokens": 250000},
            }
        )
        assert result is not None
        assert "1.5M in" in result
        assert "250.0k out" in result

    def test_long_duration(self):
        result = _format_run_cost(
            {
                "total_cost_usd": 0.50,
                "duration_ms": 125000,
            }
        )
        assert result is not None
        assert "2m 5s API" in result


# ---------------------------------------------------------------------------
# format_usage_compact tests
# ---------------------------------------------------------------------------


class TestFormatUsageCompact:
    def test_both_windows(self):
        from untether.telegram.commands.usage import format_usage_compact

        data = {
            "five_hour": {
                "utilization": 45.0,
                "resets_at": "2026-02-25T20:00:00+00:00",
            },
            "seven_day": {
                "utilization": 30.0,
                "resets_at": "2026-03-01T00:00:00+00:00",
            },
        }
        result = format_usage_compact(data)
        assert result is not None
        assert "5h: 45%" in result
        assert "7d: 30%" in result
        assert "|" in result

    def test_five_hour_only(self):
        from untether.telegram.commands.usage import format_usage_compact

        data = {
            "five_hour": {
                "utilization": 60.0,
                "resets_at": "2026-02-25T20:00:00+00:00",
            },
        }
        result = format_usage_compact(data)
        assert result is not None
        assert "5h: 60%" in result
        assert "|" not in result

    def test_no_data(self):
        from untether.telegram.commands.usage import format_usage_compact

        assert format_usage_compact({}) is None

    def test_at_limit(self):
        from untether.telegram.commands.usage import format_usage_compact

        data = {
            "five_hour": {
                "utilization": 100.0,
                "resets_at": "2026-02-25T20:00:00+00:00",
            },
            "seven_day": {
                "utilization": 80.0,
                "resets_at": "2026-03-01T00:00:00+00:00",
            },
        }
        result = format_usage_compact(data)
        assert result is not None
        assert "5h: 100%" in result
        assert "7d: 80%" in result


# ---------------------------------------------------------------------------
# _maybe_append_usage_footer always_show tests
# ---------------------------------------------------------------------------


class TestMaybeAppendUsageFooterAlwaysShow:
    @pytest.mark.anyio
    async def test_always_show_appends_compact(self, monkeypatch):
        from untether.runner_bridge import _maybe_append_usage_footer

        async def _fake_fetch():
            return {
                "five_hour": {
                    "utilization": 25.0,
                    "resets_at": "2026-02-25T20:00:00+00:00",
                },
                "seven_day": {
                    "utilization": 10.0,
                    "resets_at": "2026-03-01T00:00:00+00:00",
                },
            }

        monkeypatch.setattr(
            "untether.telegram.commands.usage.fetch_claude_usage", _fake_fetch
        )

        msg = RenderedMessage(text="Done.", extra={})
        result = await _maybe_append_usage_footer(msg, always_show=True)
        assert "5h: 25%" in result.text
        assert "7d: 10%" in result.text
        assert "\u26a1" in result.text

    @pytest.mark.anyio
    async def test_always_show_false_hides_below_threshold(self, monkeypatch):
        from untether.runner_bridge import _maybe_append_usage_footer

        async def _fake_fetch():
            return {
                "five_hour": {
                    "utilization": 25.0,
                    "resets_at": "2026-02-25T20:00:00+00:00",
                },
                "seven_day": {
                    "utilization": 10.0,
                    "resets_at": "2026-03-01T00:00:00+00:00",
                },
            }

        monkeypatch.setattr(
            "untether.telegram.commands.usage.fetch_claude_usage", _fake_fetch
        )

        msg = RenderedMessage(text="Done.", extra={})
        result = await _maybe_append_usage_footer(msg, always_show=False)
        assert result.text == "Done."

    @pytest.mark.anyio
    async def test_always_show_false_shows_above_threshold(self, monkeypatch):
        from untether.runner_bridge import _maybe_append_usage_footer

        async def _fake_fetch():
            return {
                "five_hour": {
                    "utilization": 85.0,
                    "resets_at": "2026-02-25T20:00:00+00:00",
                },
                "seven_day": {
                    "utilization": 40.0,
                    "resets_at": "2026-03-01T00:00:00+00:00",
                },
            }

        monkeypatch.setattr(
            "untether.telegram.commands.usage.fetch_claude_usage", _fake_fetch
        )

        msg = RenderedMessage(text="Done.", extra={})
        result = await _maybe_append_usage_footer(msg, always_show=False)
        assert "5h: 85%" in result.text
