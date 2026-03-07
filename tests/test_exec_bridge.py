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
from untether.runners.mock import (
    Advance,
    Emit,
    ErrorReturn,
    Raise,
    Return,
    ScriptRunner,
    Wait,
)
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


@pytest.mark.skipif(
    sys.version_info < (3, 14), reason="uuid.uuid7 requires Python 3.14+"
)
def test_codex_extract_resume_accepts_uuid7() -> None:
    uuid7 = uuid.uuid7  # type: ignore[attr-defined]
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
    assert prompt.endswith("do this\nand that")
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
        context_line="dir: untether @feat/api",
        clock=clock,
    )

    assert transport.send_calls
    final_text = transport.send_calls[-1]["message"].text
    assert "dir: untether @feat/api" in final_text
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

    @pytest.mark.anyio
    async def test_missing_credentials_returns_original_message(self, monkeypatch):
        from untether.runner_bridge import _maybe_append_usage_footer

        async def _raise_fnf():
            raise FileNotFoundError("No Claude Code credentials")

        monkeypatch.setattr(
            "untether.telegram.commands.usage.fetch_claude_usage", _raise_fnf
        )

        msg = RenderedMessage(text="Done.", extra={})
        result = await _maybe_append_usage_footer(msg, always_show=True)
        assert result.text == "Done."

    @pytest.mark.anyio
    async def test_http_error_returns_original_message(self, monkeypatch):
        import httpx

        from untether.runner_bridge import _maybe_append_usage_footer

        async def _raise_http():
            response = httpx.Response(
                status_code=401, request=httpx.Request("GET", "https://example.com")
            )
            raise httpx.HTTPStatusError(
                "Unauthorized", request=response.request, response=response
            )

        monkeypatch.setattr(
            "untether.telegram.commands.usage.fetch_claude_usage", _raise_http
        )

        msg = RenderedMessage(text="Done.", extra={})
        result = await _maybe_append_usage_footer(msg, always_show=True)
        assert result.text == "Done."

    @pytest.mark.anyio
    async def test_read_timeout_returns_original_message(self, monkeypatch):
        """ReadTimeout on usage API must not block final message delivery (#53)."""
        import httpx

        from untether.runner_bridge import _maybe_append_usage_footer

        async def _raise_timeout():
            raise httpx.ReadTimeout("")

        monkeypatch.setattr(
            "untether.telegram.commands.usage.fetch_claude_usage", _raise_timeout
        )

        msg = RenderedMessage(text="Done.", extra={})
        result = await _maybe_append_usage_footer(msg, always_show=True)
        assert result.text == "Done."

    @pytest.mark.anyio
    async def test_connect_error_returns_original_message(self, monkeypatch):
        """Network errors must not block final message delivery (#53)."""
        import httpx

        from untether.runner_bridge import _maybe_append_usage_footer

        async def _raise_connect():
            raise httpx.ConnectError("")

        monkeypatch.setattr(
            "untether.telegram.commands.usage.fetch_claude_usage", _raise_connect
        )

        msg = RenderedMessage(text="Done.", extra={})
        result = await _maybe_append_usage_footer(msg, always_show=True)
        assert result.text == "Done."


# ---------------------------------------------------------------------------
# _read_access_token credential source tests
# ---------------------------------------------------------------------------


class TestReadAccessToken:
    def test_reads_from_file(self, tmp_path):
        import json

        from untether.telegram.commands.usage import _read_access_token

        creds = {
            "claudeAiOauth": {
                "accessToken": "sk-test-token",
                "expiresAt": 9999999999999,
            }
        }
        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(json.dumps(creds))

        token, is_expired = _read_access_token(creds_file)
        assert token == "sk-test-token"
        assert not is_expired

    def test_file_not_found_raises(self, tmp_path):
        from untether.telegram.commands.usage import _read_access_token

        missing = tmp_path / "nonexistent.json"
        with pytest.raises(FileNotFoundError):
            _read_access_token(missing)

    def test_macos_keychain_fallback(self, tmp_path, monkeypatch):
        import json

        from untether.telegram.commands.usage import _read_access_token

        monkeypatch.setattr("untether.telegram.commands.usage.sys.platform", "darwin")

        creds = {
            "claudeAiOauth": {
                "accessToken": "sk-keychain-token",
                "expiresAt": 9999999999999,
            }
        }

        fake_result = type(
            "Result", (), {"returncode": 0, "stdout": json.dumps(creds)}
        )()
        monkeypatch.setattr(
            "untether.telegram.commands.usage.subprocess.run",
            lambda *args, **kwargs: fake_result,
        )

        missing = tmp_path / "nonexistent.json"
        token, is_expired = _read_access_token(missing)
        assert token == "sk-keychain-token"
        assert not is_expired

    def test_file_preferred_over_keychain(self, tmp_path, monkeypatch):
        import json

        from untether.telegram.commands.usage import _read_access_token

        monkeypatch.setattr("untether.telegram.commands.usage.sys.platform", "darwin")

        creds = {
            "claudeAiOauth": {
                "accessToken": "sk-file-token",
                "expiresAt": 9999999999999,
            }
        }
        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(json.dumps(creds))

        # Keychain would return a different token — but file should win
        keychain_creds = {
            "claudeAiOauth": {
                "accessToken": "sk-keychain-token",
                "expiresAt": 9999999999999,
            }
        }
        fake_result = type(
            "Result", (), {"returncode": 0, "stdout": json.dumps(keychain_creds)}
        )()
        monkeypatch.setattr(
            "untether.telegram.commands.usage.subprocess.run",
            lambda *args, **kwargs: fake_result,
        )

        token, _ = _read_access_token(creds_file)
        assert token == "sk-file-token"


# ---------------------------------------------------------------------------
# ExceptionGroup unwrapping in run_runner_with_cancel (issue #17)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_handle_message_catches_exception_group() -> None:
    """ExceptionGroup from runner should be caught and rendered as error."""
    transport = FakeTransport()
    session_id = "019b66fc-64c2-7a71-81cd-081c504cfeb2"
    runner = ScriptRunner(
        [Raise(ExceptionGroup("task group", [RuntimeError("inner boom")]))],
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
    assert "inner boom" in last_edit


@pytest.mark.anyio
async def test_handle_message_exception_group_preserves_resume() -> None:
    """ExceptionGroup error path should still include the resume token."""
    transport = FakeTransport()
    session_id = "019b66fc-64c2-7a71-81cd-081c504cfeb2"
    runner = ScriptRunner(
        [Raise(ExceptionGroup("tg", [ValueError("fail")]))],
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
        incoming=IncomingMessage(channel_id=123, message_id=10, text="test"),
        resume_token=None,
    )

    assert transport.edit_calls
    last_edit = transport.edit_calls[-1]["message"].text
    assert session_id in last_edit


@pytest.mark.anyio
async def test_format_error_with_exception_group() -> None:
    """_format_error should flatten ExceptionGroup and show all inner exceptions."""
    from untether.runner_bridge import _format_error

    eg = ExceptionGroup("group", [RuntimeError("boom"), ValueError("pow")])
    result = _format_error(eg)
    assert "boom" in result
    assert "pow" in result


# ---------------------------------------------------------------------------
# ProgressEdits transport resilience (issue #15)
# ---------------------------------------------------------------------------


class _FailingTransport(FakeTransport):
    """Transport that raises on edit calls to simulate network timeouts."""

    def __init__(self, *, fail_count: int = 1) -> None:
        super().__init__()
        self._fail_count = fail_count
        self._edit_attempts = 0

    async def edit(
        self, *, ref: MessageRef, message: RenderedMessage, wait: bool = True
    ) -> MessageRef:
        self._edit_attempts += 1
        if self._edit_attempts <= self._fail_count:
            raise TimeoutError("read timeout")
        return await super().edit(ref=ref, message=message, wait=wait)


@pytest.mark.anyio
async def test_progress_edits_survives_transport_error() -> None:
    """ProgressEdits should continue running when transport.edit raises."""
    transport = _FailingTransport(fail_count=1)
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            # First edit — will raise TimeoutError inside ProgressEdits.run()
            edits.event_seq = 1
            with contextlib.suppress(anyio.WouldBlock):
                edits.signal_send.send_nowait(None)
            await anyio.sleep(0)
            await anyio.sleep(0)

            # Second edit — transport succeeds this time
            presenter.set_no_approval()  # change rendered text to trigger an edit
            edits.event_seq = 2
            with contextlib.suppress(anyio.WouldBlock):
                edits.signal_send.send_nowait(None)
            await anyio.sleep(0)
            await anyio.sleep(0)

            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    # First edit raised TimeoutError, but ProgressEdits continued.
    # Second edit should have succeeded.
    assert transport._edit_attempts == 2
    assert len(transport.edit_calls) == 1  # only the successful one recorded


# ---------------------------------------------------------------------------
# on_resume_failed auto-clear tests (issue #44)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_on_resume_failed_called_for_zero_turn_error() -> None:
    """Callback fires when a resumed run fails with 0 turns."""
    transport = FakeTransport()
    runner = ScriptRunner(
        [ErrorReturn(error="error_during_execution", usage={"num_turns": 0})],
        engine=CODEX_ENGINE,
        resume_value="broken-session",
    )
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
    )
    resume = ResumeToken(engine=CODEX_ENGINE, value="broken-session")
    cleared_tokens: list[ResumeToken] = []

    async def on_resume_failed(token: ResumeToken) -> None:
        cleared_tokens.append(token)

    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=10, text="hi"),
        resume_token=resume,
        on_resume_failed=on_resume_failed,
    )

    assert len(cleared_tokens) == 1
    assert cleared_tokens[0] == resume


@pytest.mark.anyio
async def test_on_resume_failed_not_called_on_success() -> None:
    """Callback does not fire when the run succeeds."""
    transport = FakeTransport()
    runner = ScriptRunner(
        [Return(answer="done")],
        engine=CODEX_ENGINE,
        resume_value="good-session",
    )
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
    )
    resume = ResumeToken(engine=CODEX_ENGINE, value="good-session")
    cleared_tokens: list[ResumeToken] = []

    async def on_resume_failed(token: ResumeToken) -> None:
        cleared_tokens.append(token)

    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=10, text="hi"),
        resume_token=resume,
        on_resume_failed=on_resume_failed,
    )

    assert len(cleared_tokens) == 0


@pytest.mark.anyio
async def test_on_resume_failed_not_called_with_turns() -> None:
    """Callback does not fire when num_turns > 0 (partial progress made)."""
    transport = FakeTransport()
    runner = ScriptRunner(
        [ErrorReturn(error="some error", usage={"num_turns": 3})],
        engine=CODEX_ENGINE,
        resume_value="partial-session",
    )
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
    )
    resume = ResumeToken(engine=CODEX_ENGINE, value="partial-session")
    cleared_tokens: list[ResumeToken] = []

    async def on_resume_failed(token: ResumeToken) -> None:
        cleared_tokens.append(token)

    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=10, text="hi"),
        resume_token=resume,
        on_resume_failed=on_resume_failed,
    )

    assert len(cleared_tokens) == 0


@pytest.mark.anyio
async def test_on_resume_failed_not_called_when_not_resumed() -> None:
    """Callback does not fire for new sessions (resume_token=None)."""
    transport = FakeTransport()
    runner = ScriptRunner(
        [ErrorReturn(error="error_during_execution", usage={"num_turns": 0})],
        engine=CODEX_ENGINE,
    )
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
    )
    cleared_tokens: list[ResumeToken] = []

    async def on_resume_failed(token: ResumeToken) -> None:
        cleared_tokens.append(token)

    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=10, text="hi"),
        resume_token=None,
        on_resume_failed=on_resume_failed,
    )

    assert len(cleared_tokens) == 0


# ---------------------------------------------------------------------------
# Error/answer deduplication tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_error_answer_dedup_strips_duplicate_first_line() -> None:
    """When answer and error share the same first line, avoid repeating it."""
    shared_line = "You're out of extra usage \N{MIDDLE DOT} resets 7am (UTC)"
    transport = FakeTransport()
    runner = ScriptRunner(
        [
            ErrorReturn(
                answer=shared_line,
                error=f"{shared_line}\nsession: 73fe \N{MIDDLE DOT} resumed \N{MIDDLE DOT} turns: 61 \N{MIDDLE DOT} cost: $5.77",
            )
        ],
        engine=CODEX_ENGINE,
    )
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
    )

    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=10, text="go"),
        resume_token=None,
    )

    # Final message is sent (not edited) when final_notify=True
    assert len(transport.send_calls) >= 2
    final_text = transport.send_calls[-1]["message"].text
    # The shared line should appear exactly once, not twice
    assert final_text.count(shared_line) == 1
    # The diagnostic context should still be present
    assert "session: 73fe" in final_text
    # The hint should be appended
    assert "\N{ELECTRIC LIGHT BULB}" in final_text


@pytest.mark.anyio
async def test_error_answer_no_dedup_when_different() -> None:
    """When answer and error differ, both are shown in full."""
    transport = FakeTransport()
    runner = ScriptRunner(
        [
            ErrorReturn(
                answer="Task completed partially",
                error="429 rate limit exceeded",
            )
        ],
        engine=CODEX_ENGINE,
    )
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
    )

    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=10, text="go"),
        resume_token=None,
    )

    assert len(transport.send_calls) >= 2
    final_text = transport.send_calls[-1]["message"].text
    assert "Task completed partially" in final_text
    assert "rate limit" in final_text.lower()


# ===========================================================================
# Post-outline flow guidance
# ===========================================================================


@pytest.mark.anyio
async def test_outline_pending_session_gets_resume_guidance() -> None:
    """When a Claude Code run completes while outline-pending, append resume guidance."""
    from untether.runners.claude import _OUTLINE_PENDING

    session_id = f"outline-test-{uuid.uuid4().hex[:8]}"
    runner = ScriptRunner(
        [Return(answer="Here is my detailed plan outline...")],
        engine="claude",
        resume_value=session_id,
    )
    transport = FakeTransport()
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
    )

    # Mark session as outline-pending before the run
    _OUTLINE_PENDING.add(session_id)
    try:
        await handle_message(
            cfg,
            runner=runner,
            incoming=IncomingMessage(channel_id=123, message_id=10, text="go"),
            resume_token=None,
        )

        final_text = transport.send_calls[-1]["message"].text
        assert "Resume and say" in final_text
        assert "approved" in final_text.lower()
    finally:
        _OUTLINE_PENDING.discard(session_id)


@pytest.mark.anyio
async def test_normal_completion_no_outline_guidance() -> None:
    """Normal completions (no outline-pending) should NOT get resume guidance."""
    runner = ScriptRunner(
        [Return(answer="Done!")],
        engine="claude",
        resume_value=f"normal-{uuid.uuid4().hex[:8]}",
    )
    transport = FakeTransport()
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
    )

    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=10, text="go"),
        resume_token=None,
    )

    final_text = transport.send_calls[-1]["message"].text
    assert "Resume and say" not in final_text


# ---------------------------------------------------------------------------
# Render debounce tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_progress_edits_debounce_skips_first_render() -> None:
    """First render is never debounced, even with a positive interval."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=0.0)
    sleep_calls: list[float] = []

    async def fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    edits = _make_edits(transport, presenter, clock=clock)
    edits._min_render_interval = 5.0
    edits._sleep = fake_sleep

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            edits.event_seq = 1
            with contextlib.suppress(anyio.WouldBlock):
                edits.signal_send.send_nowait(None)
            await anyio.sleep(0)
            await anyio.sleep(0)
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    # First render should not have triggered a sleep
    assert sleep_calls == []
    assert len(transport.edit_calls) == 1


@pytest.mark.anyio
async def test_progress_edits_debounce_delays_second_render() -> None:
    """Second render sleeps for the remaining debounce interval."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=0.0)
    sleep_calls: list[float] = []

    async def fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)
        # Simulate time passing during sleep
        clock.set(clock() + secs)

    edits = _make_edits(transport, presenter, clock=clock)
    edits._min_render_interval = 2.0
    edits._sleep = fake_sleep

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            # First render (no debounce)
            edits.event_seq = 1
            with contextlib.suppress(anyio.WouldBlock):
                edits.signal_send.send_nowait(None)
            await anyio.sleep(0)
            await anyio.sleep(0)

            # Advance clock by 0.5s — less than the 2.0s interval
            clock.set(0.5)
            presenter.set_no_approval()  # Change output to trigger a real edit
            edits.event_seq = 2
            with contextlib.suppress(anyio.WouldBlock):
                edits.signal_send.send_nowait(None)
            await anyio.sleep(0)
            await anyio.sleep(0)

            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    # Should have slept for ~1.5s (2.0 - 0.5)
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == pytest.approx(1.5, abs=0.1)
    assert len(transport.edit_calls) == 2


@pytest.mark.anyio
async def test_progress_edits_debounce_zero_interval_no_delay() -> None:
    """With min_render_interval=0, no debouncing occurs."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=0.0)
    sleep_calls: list[float] = []

    async def fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    edits = _make_edits(transport, presenter, clock=clock)
    edits._min_render_interval = 0.0
    edits._sleep = fake_sleep

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            edits.event_seq = 1
            with contextlib.suppress(anyio.WouldBlock):
                edits.signal_send.send_nowait(None)
            await anyio.sleep(0)
            await anyio.sleep(0)

            # Advance clock so the rendered text changes (elapsed_s differs)
            clock.set(5.0)
            edits.event_seq = 2
            with contextlib.suppress(anyio.WouldBlock):
                edits.signal_send.send_nowait(None)
            await anyio.sleep(0)
            await anyio.sleep(0)

            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    assert sleep_calls == []
    assert len(transport.edit_calls) == 2


# ---------------------------------------------------------------------------
# Non-blocking approval notification tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_progress_edits_notification_does_not_block_render() -> None:
    """Approval notification send runs in background, not blocking the render loop."""
    send_started = anyio.Event()
    send_proceed = anyio.Event()

    class SlowSendTransport(FakeTransport):
        async def send(self, **kwargs):
            send_started.set()
            await send_proceed.wait()
            return await super().send(**kwargs)

    transport = SlowSendTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=0.0)
    edits = _make_edits(transport, presenter, clock=clock)

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            # Trigger approval buttons → notification send starts in background
            presenter.set_approval_buttons()
            edits.event_seq = 1
            with contextlib.suppress(anyio.WouldBlock):
                edits.signal_send.send_nowait(None)

            # Wait for the slow send to start
            await send_started.wait()

            # Trigger another event while the send is still pending —
            # the render loop should NOT be blocked.
            presenter.set_no_approval()
            edits.event_seq = 2
            with contextlib.suppress(anyio.WouldBlock):
                edits.signal_send.send_nowait(None)
            await anyio.sleep(0)
            await anyio.sleep(0)

            # Unblock the slow send and close
            send_proceed.set()
            await anyio.sleep(0)
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    # Both edits should have been processed (loop wasn't blocked)
    assert len(transport.edit_calls) >= 2


@pytest.mark.anyio
async def test_progress_edits_debounce_no_delay_when_interval_elapsed() -> None:
    """No sleep when enough time has passed since the last render."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=0.0)
    sleep_calls: list[float] = []

    async def fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    edits = _make_edits(transport, presenter, clock=clock)
    edits._min_render_interval = 2.0
    edits._sleep = fake_sleep

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            # First render
            edits.event_seq = 1
            with contextlib.suppress(anyio.WouldBlock):
                edits.signal_send.send_nowait(None)
            await anyio.sleep(0)
            await anyio.sleep(0)

            # Advance clock well past the interval
            clock.set(10.0)
            edits.event_seq = 2
            with contextlib.suppress(anyio.WouldBlock):
                edits.signal_send.send_nowait(None)
            await anyio.sleep(0)
            await anyio.sleep(0)

            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    # No sleep needed — interval already elapsed
    assert sleep_calls == []
    assert len(transport.edit_calls) == 2


@pytest.mark.anyio
async def test_progress_edits_end_of_stream_exits_during_debounce() -> None:
    """Closing the signal while waiting to debounce terminates the loop."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=0.0)

    edits = _make_edits(transport, presenter, clock=clock)
    edits._min_render_interval = 999.0  # Very long, would hang if not cancelled
    edits._sleep = anyio.sleep  # Real sleep — but cancel scope should terminate it

    async with anyio.create_task_group() as tg:
        edits_scope = anyio.CancelScope()

        async def run_edits() -> None:
            with edits_scope:
                await edits.run()

        async def drive() -> None:
            # First render
            edits.event_seq = 1
            with contextlib.suppress(anyio.WouldBlock):
                edits.signal_send.send_nowait(None)
            await anyio.sleep(0)
            await anyio.sleep(0)

            # Second event, then immediately cancel the scope
            edits.event_seq = 2
            with contextlib.suppress(anyio.WouldBlock):
                edits.signal_send.send_nowait(None)
            await anyio.sleep(0)
            edits_scope.cancel()

        tg.start_soon(run_edits)
        tg.start_soon(drive)

    # Should have finished cleanly without hanging
    assert len(transport.edit_calls) == 1  # Only first render completed


@pytest.mark.anyio
async def test_progress_edits_notification_failure_does_not_crash() -> None:
    """If the background notification send raises, the run loop continues."""

    class FailingSendTransport(FakeTransport):
        async def send(self, **kwargs):
            raise RuntimeError("network error")

    transport = FailingSendTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=0.0)
    edits = _make_edits(transport, presenter, clock=clock)

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            # Trigger approval buttons → notification send will fail
            presenter.set_approval_buttons()
            edits.event_seq = 1
            with contextlib.suppress(anyio.WouldBlock):
                edits.signal_send.send_nowait(None)
            await anyio.sleep(0)
            await anyio.sleep(0)
            await anyio.sleep(0)

            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    # The loop should have completed without crashing
    assert len(transport.edit_calls) == 1
    assert edits._approval_notify_ref is None  # send failed, ref stays None


@pytest.mark.anyio
async def test_handle_message_with_min_render_interval() -> None:
    """Integration: ExecBridgeConfig.min_render_interval flows through to ProgressEdits."""
    transport = FakeTransport()
    clock = _FakeClock()
    runner = ScriptRunner(
        [
            Emit(action_started("a1", "command", "echo 1"), at=0.1),
            Emit(action_started("a2", "command", "echo 2"), at=0.2),
            Advance(3.0),
            Return(answer="done"),
        ],
        engine=CODEX_ENGINE,
        advance=clock.set,
    )
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
        min_render_interval=1.0,
    )

    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=10, text="hi"),
        resume_token=None,
        clock=clock,
    )

    # Should complete successfully with the interval set
    assert any("done" in c["message"].text.lower() for c in transport.send_calls)


@pytest.mark.anyio
async def test_progress_edits_stall_monitor_logs_warning(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Stall monitor warns when no events arrive for _STALL_THRESHOLD_SECONDS."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    # Shorten thresholds for test speed
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05

    # Simulate one event arriving, then stall
    edits._last_event_at = 100.0

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            # Advance clock past stall threshold
            clock.set(100.1)
            await anyio.sleep(0.05)
            # Close signal to end the run loop
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    # Check that stall was detected
    assert edits._stall_warned is True


@pytest.mark.anyio
async def test_progress_edits_stall_recovery_clears_warning() -> None:
    """Receiving an event after a stall clears the warning flag."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)

    # Simulate stall state
    edits._stall_warned = True
    edits._last_event_at = 100.0

    # Receive a new event
    clock.set(200.0)
    from untether.model import ActionEvent, Action

    evt = ActionEvent(
        engine="codex",
        action=Action(id="x", kind="command", title="echo"),
        phase="started",
    )
    await edits.on_event(evt)

    assert edits._stall_warned is False
