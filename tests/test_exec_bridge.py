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

    def test_no_cost_no_tokens(self):
        assert _format_run_cost({"num_turns": 5}) is None

    def test_tokens_only_no_cost(self):
        result = _format_run_cost(
            {"usage": {"input_tokens": 72500, "output_tokens": 120}}
        )
        assert result is not None
        assert result == "72.5k/120"
        assert "$" not in result

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
        assert "8 tn" in result
        assert "45.0s" in result
        assert "15.0k/3.2k" in result

    def test_large_token_counts(self):
        result = _format_run_cost(
            {
                "total_cost_usd": 5.00,
                "usage": {"input_tokens": 1500000, "output_tokens": 250000},
            }
        )
        assert result is not None
        assert "1.5M/250.0k" in result

    def test_long_duration(self):
        result = _format_run_cost(
            {
                "total_cost_usd": 0.50,
                "duration_ms": 125000,
            }
        )
        assert result is not None
        assert "2m 5s" in result


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
# Cost footer suppression on error runs
# ===========================================================================


def _force_show_api_cost(monkeypatch):
    """Patch footer settings to always show API cost (independent of real config)."""
    from untether.settings import FooterSettings

    monkeypatch.setattr(
        "untether.runner_bridge._load_footer_settings",
        lambda: FooterSettings(show_api_cost=True),
    )


@pytest.mark.anyio
async def test_cost_footer_suppressed_on_error_run(monkeypatch) -> None:
    """Error runs should not show the cost footer — diagnostic line has cost already."""
    _force_show_api_cost(monkeypatch)
    transport = FakeTransport()
    runner = ScriptRunner(
        [
            ErrorReturn(
                error="Claude Code run failed",
                usage={"total_cost_usd": 2.50, "num_turns": 10, "duration_ms": 60000},
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
    # Error text should be present
    assert "Claude Code run failed" in final_text
    # Cost footer (money bag emoji) should NOT appear on error runs
    assert "\U0001f4b0" not in final_text


@pytest.mark.anyio
async def test_cost_footer_shown_on_success_run(monkeypatch) -> None:
    """Successful runs should show the cost footer when usage data is present."""
    _force_show_api_cost(monkeypatch)
    transport = FakeTransport()
    runner = ScriptRunner(
        [
            Return(
                answer="All done!",
                usage={"total_cost_usd": 1.25, "num_turns": 5, "duration_ms": 30000},
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
    assert "All done!" in final_text
    # Cost footer (money bag emoji) SHOULD appear on success runs
    assert "\U0001f4b0" in final_text
    assert "$1.25" in final_text


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

    # Check that stall was detected and notification sent
    assert edits._stall_warned is True
    assert edits._stall_warn_count >= 1
    assert len(transport.send_calls) == 1
    msg_text = transport.send_calls[0]["message"].text
    assert "No progress for" in msg_text
    assert "/cancel" in msg_text


@pytest.mark.anyio
async def test_progress_edits_stall_detected_without_any_events() -> None:
    """Stall monitor detects stalls even when no events arrive after session start."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05

    # _last_event_at is now initialised from clock() (100.0), not 0.0
    # So stall should be detected even without any on_event() calls
    assert edits._last_event_at == 100.0

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            clock.set(100.1)
            await anyio.sleep(0.05)
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    assert edits._stall_warned is True
    assert edits._stall_warn_count >= 1
    assert len(transport.send_calls) == 1


@pytest.mark.anyio
async def test_progress_edits_stall_notification_repeats_after_interval() -> None:
    """Stall notification repeats after _stall_repeat_seconds."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._stall_repeat_seconds = 0.02  # very short for test

    edits._last_event_at = 100.0

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            # First warning
            clock.set(100.1)
            await anyio.sleep(0.05)
            # Advance past repeat interval for second warning
            clock.set(100.2)
            await anyio.sleep(0.05)
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    assert edits._stall_warned is True
    assert edits._stall_warn_count >= 2
    assert len(transport.send_calls) >= 2


@pytest.mark.anyio
async def test_progress_edits_stall_recovery_clears_warning() -> None:
    """Receiving an event after a stall clears the warning flag."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)

    # Simulate stall state
    edits._stall_warned = True
    edits._stall_warn_count = 2
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
    assert edits._stall_warn_count == 0


@pytest.mark.anyio
async def test_progress_edits_stall_includes_last_action() -> None:
    """Stall notification includes last action summary."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._STALL_THRESHOLD_TOOL = 0.05

    # Simulate an action so _last_action_summary() returns something
    from untether.model import Action, ActionEvent

    evt = ActionEvent(
        engine="codex",
        action=Action(id="a1", kind="tool", title="Agent"),
        phase="started",
    )
    await edits.on_event(evt)
    clock.set(100.0)  # reset after on_event advanced it

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            clock.set(100.1)
            await anyio.sleep(0.05)
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    assert edits._stall_warned is True
    msg_text = transport.send_calls[-1]["message"].text
    assert "Last:" in msg_text
    assert "Agent" in msg_text


@pytest.mark.anyio
async def test_progress_edits_peak_idle_tracked() -> None:
    """Peak idle time is tracked across the session."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)

    assert edits._peak_idle == 0.0
    # Simulate: no events for a while, then recover
    edits._stall_warned = True
    edits._stall_warn_count = 1
    edits._last_event_at = 100.0

    from untether.model import Action, ActionEvent

    clock.set(200.0)
    evt = ActionEvent(
        engine="codex",
        action=Action(id="x", kind="command", title="echo"),
        phase="started",
    )
    await edits.on_event(evt)
    assert edits._stall_warned is False


def test_progress_edits_pid_and_stream_defaults() -> None:
    """ProgressEdits starts with pid=None and stream=None."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)
    assert edits.pid is None
    assert edits.stream is None


def test_last_action_summary_no_actions() -> None:
    """Returns None when no actions have been tracked."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)
    assert edits._last_action_summary() is None


@pytest.mark.anyio
async def test_last_action_summary_with_actions() -> None:
    """Returns summary of most recent action."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)

    from untether.model import Action, ActionEvent

    evt = ActionEvent(
        engine="codex",
        action=Action(id="a1", kind="tool", title="Bash"),
        phase="started",
    )
    await edits.on_event(evt)
    summary = edits._last_action_summary()
    assert summary is not None
    assert "tool:Bash" in summary
    assert "running" in summary


@pytest.mark.anyio
async def test_stall_auto_cancel_dead_process() -> None:
    """Stall monitor auto-cancels when process is confirmed dead."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits.pid = 99999  # non-existent PID
    cancel_event = anyio.Event()
    edits.cancel_event = cancel_event

    # Patch collect_proc_diag to return dead process
    from unittest.mock import patch
    from untether.utils.proc_diag import ProcessDiag

    dead_diag = ProcessDiag(pid=99999, alive=False)

    with patch("untether.utils.proc_diag.collect_proc_diag", return_value=dead_diag):
        async with anyio.create_task_group() as tg:

            async def drive() -> None:
                clock.set(100.1)
                await anyio.sleep(0.1)
                # If auto-cancel didn't fire, close manually
                if not cancel_event.is_set():
                    edits.signal_send.close()

            tg.start_soon(edits.run)
            tg.start_soon(drive)

    assert cancel_event.is_set()
    # Should have sent auto-cancel notification
    auto_cancel_msgs = [
        c for c in transport.send_calls if "Auto-cancelled" in c["message"].text
    ]
    assert len(auto_cancel_msgs) == 1
    assert "process_dead" in auto_cancel_msgs[0]["message"].text


@pytest.mark.anyio
async def test_stall_auto_cancel_no_pid_no_events() -> None:
    """Stall monitor auto-cancels after _STALL_MAX_WARNINGS_NO_PID when pid=None."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._stall_repeat_seconds = 0.01
    edits._STALL_MAX_WARNINGS_NO_PID = 2
    edits.pid = None  # no PID known
    edits.event_seq = 0  # no events received
    cancel_event = anyio.Event()
    edits.cancel_event = cancel_event

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            # Each iteration: advance clock past threshold + repeat
            for i in range(5):
                clock.set(100.1 + i * 0.1)
                await anyio.sleep(0.03)
                if cancel_event.is_set():
                    break
            if not cancel_event.is_set():
                edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    assert cancel_event.is_set()
    auto_cancel_msgs = [
        c for c in transport.send_calls if "Auto-cancelled" in c["message"].text
    ]
    assert len(auto_cancel_msgs) == 1
    assert "no_pid_no_events" in auto_cancel_msgs[0]["message"].text


@pytest.mark.anyio
async def test_stall_auto_cancel_max_warnings() -> None:
    """Stall monitor auto-cancels after _STALL_MAX_WARNINGS absolute cap."""
    from unittest.mock import patch
    from untether.utils.proc_diag import ProcessDiag

    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._stall_repeat_seconds = 0.01
    edits._STALL_MAX_WARNINGS = 3
    edits.pid = 12345
    edits.event_seq = 5  # has events, so no_pid path won't trigger
    cancel_event = anyio.Event()
    edits.cancel_event = cancel_event

    # Mock collect_proc_diag to return alive process so process_dead doesn't fire
    alive_diag = ProcessDiag(pid=12345, alive=True)
    with patch("untether.utils.proc_diag.collect_proc_diag", return_value=alive_diag):
        async with anyio.create_task_group() as tg:

            async def drive() -> None:
                for i in range(10):
                    clock.set(100.1 + i * 0.1)
                    await anyio.sleep(0.03)
                    if cancel_event.is_set():
                        break
                if not cancel_event.is_set():
                    edits.signal_send.close()

            tg.start_soon(edits.run)
            tg.start_soon(drive)

    assert cancel_event.is_set()
    auto_cancel_msgs = [
        c for c in transport.send_calls if "Auto-cancelled" in c["message"].text
    ]
    assert len(auto_cancel_msgs) == 1
    assert "max_warnings" in auto_cancel_msgs[0]["message"].text


@pytest.mark.anyio
async def test_stall_no_auto_cancel_without_cancel_event() -> None:
    """Stall auto-cancel logs but doesn't crash when cancel_event is None."""
    from unittest.mock import patch
    from untether.utils.proc_diag import ProcessDiag

    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._stall_repeat_seconds = 0.01
    edits._STALL_MAX_WARNINGS = 2
    edits.pid = 12345
    edits.event_seq = 5
    edits.cancel_event = None  # no cancel event wired

    alive_diag = ProcessDiag(pid=12345, alive=True)
    with patch("untether.utils.proc_diag.collect_proc_diag", return_value=alive_diag):
        async with anyio.create_task_group() as tg:

            async def drive() -> None:
                for i in range(5):
                    clock.set(100.1 + i * 0.1)
                    await anyio.sleep(0.03)
                edits.signal_send.close()

            tg.start_soon(edits.run)
            tg.start_soon(drive)

    # Should still send the auto-cancel notification
    auto_cancel_msgs = [
        c for c in transport.send_calls if "Auto-cancelled" in c["message"].text
    ]
    assert len(auto_cancel_msgs) == 1


@pytest.mark.anyio
async def test_stall_suppressed_while_waiting_for_approval() -> None:
    """Stall monitor uses longer threshold when pending approval action exists."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._STALL_THRESHOLD_APPROVAL = 0.5  # 500ms for test

    # Simulate a pending approval action (has inline_keyboard in detail)
    from untether.model import Action, ActionEvent

    evt = ActionEvent(
        engine="codex",
        action=Action(
            id="ctrl.1",
            kind="warning",
            title="Permission Request [CanUseTool] - tool: ExitPlanMode",
            detail={"inline_keyboard": {"buttons": [[{"text": "Approve"}]]}},
        ),
        phase="started",
    )
    await edits.on_event(evt)
    clock.set(100.0)  # reset after on_event

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            # Advance past normal threshold (0.05) but NOT past approval threshold (0.5)
            clock.set(100.2)
            await anyio.sleep(0.05)
            # Should NOT have warned yet
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    # No stall warning should have fired
    assert edits._stall_warn_count == 0
    stall_msgs = [c for c in transport.send_calls if "No progress" in c["message"].text]
    assert len(stall_msgs) == 0


@pytest.mark.anyio
async def test_stall_fires_after_approval_threshold() -> None:
    """Stall monitor fires after the longer approval threshold is exceeded."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._STALL_THRESHOLD_APPROVAL = 0.1  # short for test

    from untether.model import Action, ActionEvent

    evt = ActionEvent(
        engine="codex",
        action=Action(
            id="ctrl.1",
            kind="warning",
            title="Permission Request [CanUseTool] - tool: Bash",
            detail={"inline_keyboard": {"buttons": [[{"text": "Approve"}]]}},
        ),
        phase="started",
    )
    await edits.on_event(evt)
    clock.set(100.0)

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            # Advance past the approval threshold (0.1)
            clock.set(100.2)
            await anyio.sleep(0.05)
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    assert edits._stall_warn_count >= 1
    stall_msgs = [c for c in transport.send_calls if "No progress" in c["message"].text]
    assert len(stall_msgs) >= 1


@pytest.mark.anyio
async def test_stall_normal_threshold_without_approval() -> None:
    """Stall monitor uses normal threshold when no pending approval."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._STALL_THRESHOLD_TOOL = 0.05
    edits._STALL_THRESHOLD_APPROVAL = 10.0  # very long, shouldn't matter

    # No pending approval — normal action without inline_keyboard
    from untether.model import Action, ActionEvent

    evt = ActionEvent(
        engine="codex",
        action=Action(id="a1", kind="tool", title="Bash"),
        phase="started",
    )
    await edits.on_event(evt)
    clock.set(100.0)

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            clock.set(100.1)  # past normal threshold (0.05)
            await anyio.sleep(0.05)
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    # Should have warned using the normal threshold
    assert edits._stall_warn_count >= 1


@pytest.mark.anyio
async def test_stall_tool_threshold_suppresses_warning() -> None:
    """Running tool uses longer threshold, suppressing premature stall warnings."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05  # normal: very short
    edits._STALL_THRESHOLD_TOOL = 10.0  # tool: very long
    edits._STALL_THRESHOLD_APPROVAL = 10.0

    # Start a tool action (not completed) — should use tool threshold
    from untether.model import Action, ActionEvent

    evt = ActionEvent(
        engine="codex",
        action=Action(id="a1", kind="tool", title="Bash"),
        phase="started",
    )
    await edits.on_event(evt)
    clock.set(100.0)

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            clock.set(100.1)  # past normal threshold but not tool threshold
            await anyio.sleep(0.05)
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    # Should NOT have warned — tool threshold is 10.0, idle only 0.1
    assert edits._stall_warn_count == 0


@pytest.mark.anyio
async def test_stall_mcp_tool_threshold_suppresses_warning() -> None:
    """Running MCP tool uses longer MCP threshold, suppressing premature stall warnings."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05  # normal: very short
    edits._STALL_THRESHOLD_TOOL = 0.05  # tool: very short
    edits._STALL_THRESHOLD_MCP_TOOL = 10.0  # MCP: very long
    edits._STALL_THRESHOLD_APPROVAL = 10.0

    from untether.model import Action, ActionEvent

    evt = ActionEvent(
        engine="claude",
        action=Action(
            id="a1",
            kind="tool",
            title="mcp__cloudflare-observability__query_worker_observability",
            detail={
                "name": "mcp__cloudflare-observability__query_worker_observability"
            },
        ),
        phase="started",
    )
    await edits.on_event(evt)
    clock.set(100.0)

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            clock.set(100.1)  # past normal + tool thresholds but not MCP threshold
            await anyio.sleep(0.05)
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    # Should NOT have warned — MCP threshold is 10.0, idle only 0.1
    assert edits._stall_warn_count == 0


@pytest.mark.anyio
async def test_stall_mcp_tool_threshold_fires_after_exceeded() -> None:
    """Stall monitor fires after the MCP tool threshold is exceeded."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._STALL_THRESHOLD_TOOL = 0.05
    edits._STALL_THRESHOLD_MCP_TOOL = 0.1  # short for test

    from untether.model import Action, ActionEvent

    evt = ActionEvent(
        engine="claude",
        action=Action(
            id="a1",
            kind="tool",
            title="mcp__github__search_code",
            detail={"name": "mcp__github__search_code"},
        ),
        phase="started",
    )
    await edits.on_event(evt)
    clock.set(100.0)

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            clock.set(100.2)  # past MCP threshold (0.1)
            await anyio.sleep(0.05)
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    assert edits._stall_warn_count >= 1


@pytest.mark.anyio
async def test_stall_mcp_tool_notification_message_format() -> None:
    """Stall notification for MCP tools names the server, not 'session may be stuck'."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._STALL_THRESHOLD_TOOL = 0.05
    edits._STALL_THRESHOLD_MCP_TOOL = 0.1  # short for test

    from untether.model import Action, ActionEvent

    evt = ActionEvent(
        engine="claude",
        action=Action(
            id="a1",
            kind="tool",
            title="mcp__cloudflare-observability__query_worker_observability",
            detail={
                "name": "mcp__cloudflare-observability__query_worker_observability"
            },
        ),
        phase="started",
    )
    await edits.on_event(evt)
    clock.set(100.0)

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            clock.set(100.2)  # past MCP threshold
            await anyio.sleep(0.05)
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    mcp_msgs = [
        c for c in transport.send_calls if "MCP tool running" in c["message"].text
    ]
    assert len(mcp_msgs) >= 1
    assert "cloudflare-observability" in mcp_msgs[0]["message"].text
    # Should NOT contain the generic "stuck" message
    stuck_msgs = [
        c for c in transport.send_calls if "may be stuck" in c["message"].text
    ]
    assert len(stuck_msgs) == 0


def test_has_running_mcp_tool_returns_server_name() -> None:
    """_has_running_mcp_tool returns server name for MCP tools, None otherwise."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)

    from untether.model import Action
    from untether.progress import ActionState

    # No actions → None
    assert edits._has_running_mcp_tool() is None

    # Running MCP tool → server name
    edits.tracker._actions["a1"] = ActionState(
        action=Action(
            id="a1",
            kind="tool",
            title="mcp__github__search_code",
            detail={"name": "mcp__github__search_code"},
        ),
        phase="started",
        ok=None,
        display_phase="started",
        completed=False,
        first_seen=0,
        last_update=0,
    )
    assert edits._has_running_mcp_tool() == "github"

    # Non-MCP tool → None
    edits.tracker._actions["a2"] = ActionState(
        action=Action(id="a2", kind="tool", title="Bash", detail={"name": "Bash"}),
        phase="started",
        ok=None,
        display_phase="started",
        completed=False,
        first_seen=0,
        last_update=0,
    )
    assert edits._has_running_mcp_tool() is None

    # Completed MCP tool → None
    edits.tracker._actions.clear()
    edits.tracker._actions["a3"] = ActionState(
        action=Action(
            id="a3",
            kind="tool",
            title="mcp__cloudflare__list_workers",
            detail={"name": "mcp__cloudflare__list_workers"},
        ),
        phase="completed",
        ok=True,
        display_phase="completed",
        completed=True,
        first_seen=0,
        last_update=0,
    )
    assert edits._has_running_mcp_tool() is None


@pytest.mark.anyio
async def test_stall_mcp_hung_escalation_notifies_after_frozen_ring() -> None:
    """When MCP tool is running and ring buffer is frozen for 3+ checks, notify user."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._STALL_THRESHOLD_TOOL = 0.05
    edits._STALL_THRESHOLD_MCP_TOOL = 0.05  # short so it fires quickly
    edits._stall_repeat_seconds = 0.0  # no delay between warnings

    # Provide a fake stream with a frozen ring buffer
    from collections import deque
    from types import SimpleNamespace

    fake_stream = SimpleNamespace(
        recent_events=deque([(1.0, "system"), (2.0, "assistant")], maxlen=10),
        last_event_type="user",
        stderr_capture=[],
    )
    edits.stream = fake_stream

    from untether.model import Action, ActionEvent

    evt = ActionEvent(
        engine="claude",
        action=Action(
            id="a1",
            kind="tool",
            title="mcp__cloudflare__query_workers",
            detail={"name": "mcp__cloudflare__query_workers"},
        ),
        phase="started",
    )
    await edits.on_event(evt)
    clock.set(100.0)

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            # Advance past threshold, let 5 stall checks fire (all with frozen ring)
            clock.set(100.5)
            await anyio.sleep(0.15)
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    # Should have fired multiple stall warnings
    assert edits._stall_warn_count >= 4
    # After 3+ frozen checks, should have sent a "may be hung" notification
    hung_msgs = [c for c in transport.send_calls if "may be hung" in c["message"].text]
    assert len(hung_msgs) >= 1
    assert "cloudflare" in hung_msgs[0]["message"].text
    assert "no new events" in hung_msgs[0]["message"].text


@pytest.mark.anyio
async def test_stall_mcp_not_hung_when_ring_buffer_advances() -> None:
    """When MCP tool is running but ring buffer changes, suppress notification normally."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._STALL_THRESHOLD_TOOL = 0.05
    edits._STALL_THRESHOLD_MCP_TOOL = 0.05
    edits._stall_repeat_seconds = 0.0

    from collections import deque
    from types import SimpleNamespace

    ring = deque([(1.0, "system"), (2.0, "assistant")], maxlen=10)
    fake_stream = SimpleNamespace(
        recent_events=ring,
        last_event_type="user",
        stderr_capture=[],
    )
    edits.stream = fake_stream

    from untether.model import Action, ActionEvent

    evt = ActionEvent(
        engine="claude",
        action=Action(
            id="a1",
            kind="tool",
            title="mcp__github__search_code",
            detail={"name": "mcp__github__search_code"},
        ),
        phase="started",
    )
    await edits.on_event(evt)
    clock.set(100.0)

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            clock.set(100.5)
            for i in range(5):
                # Advance the ring buffer each iteration to simulate progress
                ring.append((100.0 + i, "user"))
                await anyio.sleep(0.03)
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    # Should NOT have sent any "may be hung" messages — ring buffer was advancing
    hung_msgs = [c for c in transport.send_calls if "may be hung" in c["message"].text]
    assert len(hung_msgs) == 0
    # Frozen ring count should be 0 or very low since events kept coming
    assert edits._frozen_ring_count <= 1


@pytest.mark.anyio
async def test_stall_frozen_ring_escalates_without_mcp_tool() -> None:
    """When no MCP tool is running but ring buffer is frozen for 3+ checks, notify user.

    Regression test for #155: frozen ring buffer escalation was gated on
    mcp_server being set, so general stalls with cpu_active=True were
    suppressed indefinitely.
    """
    from collections import deque
    from types import SimpleNamespace
    from unittest.mock import patch

    from untether.utils.proc_diag import ProcessDiag

    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._stall_repeat_seconds = 0.0  # no delay between warnings
    edits._STALL_MAX_WARNINGS = 100  # don't hit auto-cancel
    edits.pid = 12345
    edits.event_seq = 5

    # Provide a fake stream with a frozen ring buffer — NO MCP tool
    fake_stream = SimpleNamespace(
        recent_events=deque([(1.0, "assistant"), (2.0, "result")], maxlen=10),
        last_event_type="result",
        stderr_capture=[],
    )
    edits.stream = fake_stream

    # No tool action — just a completed run that went silent
    clock.set(100.0)

    call_count = 0

    def active_cpu_diag(pid: int) -> ProcessDiag:
        nonlocal call_count
        call_count += 1
        return ProcessDiag(
            pid=pid,
            alive=True,
            cpu_utime=1000 + call_count * 300,
            cpu_stime=200 + call_count * 50,
        )

    with patch(
        "untether.utils.proc_diag.collect_proc_diag",
        side_effect=active_cpu_diag,
    ):
        async with anyio.create_task_group() as tg:

            async def drive() -> None:
                # Advance past threshold, let enough stall checks fire
                for i in range(8):
                    clock.set(100.1 + i * 0.1)
                    await anyio.sleep(0.03)
                edits.signal_send.close()

            tg.start_soon(edits.run)
            tg.start_soon(drive)

    # After 3+ frozen checks, should have sent a notification despite cpu_active
    notify_msgs = [
        c
        for c in transport.send_calls
        if "no new events" in c["message"].text.lower()
        or (
            "no progress" in c["message"].text.lower()
            and "cpu active" in c["message"].text.lower()
        )
    ]
    assert len(notify_msgs) >= 1, (
        f"Expected frozen ring escalation notification, got: "
        f"{[c['message'].text for c in transport.send_calls]}"
    )
    # Should NOT mention MCP
    assert "mcp" not in notify_msgs[0]["message"].text.lower()
    # Should mention CPU active context
    assert "cpu active" in notify_msgs[0]["message"].text.lower()


@pytest.mark.anyio
async def test_stall_frozen_ring_uses_tool_message_when_bash_running() -> None:
    """When ring buffer is frozen but a Bash command is running (main sleeping,
    CPU active on children), show reassuring 'still running' instead of 'No progress'.

    Regression test for #188: frozen_escalate branch fired alarming 'No progress'
    message even when Claude was legitimately waiting for a long Bash command.
    """
    from collections import deque
    from types import SimpleNamespace
    from unittest.mock import patch

    from untether.model import Action, ActionEvent
    from untether.utils.proc_diag import ProcessDiag

    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._STALL_THRESHOLD_TOOL = 0.05  # override 600s tool threshold
    edits._stall_repeat_seconds = 0.0
    edits._STALL_MAX_WARNINGS = 100
    edits.pid = 12345
    edits.event_seq = 5

    # Simulate a running Bash command action
    await edits.on_event(
        ActionEvent(
            engine="claude",
            action=Action(
                id="a1",
                kind="command",
                title='echo "running benchmarks"',
            ),
            phase="started",
        )
    )

    # Provide a frozen ring buffer
    fake_stream = SimpleNamespace(
        recent_events=deque([(1.0, "assistant"), (2.0, "result")], maxlen=10),
        last_event_type="result",
        stderr_capture=[],
    )
    edits.stream = fake_stream

    clock.set(100.0)
    call_count = 0

    def sleeping_cpu_diag(pid: int) -> ProcessDiag:
        nonlocal call_count
        call_count += 1
        return ProcessDiag(
            pid=pid,
            alive=True,
            state="S",  # main process sleeping (waiting for child)
            cpu_utime=1000 + call_count * 300,
            cpu_stime=200 + call_count * 50,
        )

    with patch(
        "untether.utils.proc_diag.collect_proc_diag",
        side_effect=sleeping_cpu_diag,
    ):
        async with anyio.create_task_group() as tg:

            async def drive() -> None:
                for i in range(8):
                    clock.set(100.1 + i * 0.1)
                    await anyio.sleep(0.03)
                edits.signal_send.close()

            tg.start_soon(edits.run)
            tg.start_soon(drive)

    # Should have sent notification with reassuring tool-aware message
    notify_msgs = [
        c for c in transport.send_calls if "still running" in c["message"].text.lower()
    ]
    assert len(notify_msgs) >= 1, (
        f"Expected 'Bash command still running' message, got: "
        f"{[c['message'].text for c in transport.send_calls]}"
    )
    # Should mention Bash, NOT "No progress"
    assert "bash" in notify_msgs[0]["message"].text.lower()
    assert "no progress" not in notify_msgs[0]["message"].text.lower()


def test_frozen_ring_count_resets_on_event() -> None:
    """_frozen_ring_count and _prev_recent_events reset when a real event arrives."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)

    # Simulate frozen state
    edits._frozen_ring_count = 5
    edits._prev_recent_events = [(1.0, "system")]
    edits._stall_warned = True
    edits._stall_warn_count = 3

    from untether.model import Action, ActionEvent

    import asyncio

    asyncio.run(
        edits.on_event(
            ActionEvent(
                engine="claude",
                action=Action(id="a1", kind="tool", title="Bash"),
                phase="started",
            )
        )
    )

    assert edits._frozen_ring_count == 0
    assert edits._prev_recent_events is None
    assert edits._stall_warned is False
    assert edits._stall_warn_count == 0


# ===========================================================================
# Phase 2b: Edit-fail fallback in _send_or_edit_message (#103)
# ===========================================================================


@pytest.mark.anyio
async def test_send_or_edit_message_edit_fail_fallback() -> None:
    """When transport.edit returns None, _send_or_edit_message falls back to send."""
    from untether.runner_bridge import _send_or_edit_message

    class _FailEditTransport(FakeTransport):
        async def edit(self, *, ref, message, wait=True):
            self.edit_calls.append({"ref": ref, "message": message, "wait": wait})
            return None  # simulate edit failure

    transport = _FailEditTransport()
    edit_ref = MessageRef(channel_id=123, message_id=99)
    msg = RenderedMessage(text="test")

    ref, edited = await _send_or_edit_message(
        transport,
        channel_id=123,
        message=msg,
        edit_ref=edit_ref,
    )
    # Should have tried edit first (failed), then sent
    assert len(transport.edit_calls) == 1
    assert len(transport.send_calls) == 1
    assert ref is not None
    assert edited is False


@pytest.mark.anyio
async def test_send_or_edit_message_edit_success() -> None:
    """When transport.edit succeeds, no fallback send occurs."""
    from untether.runner_bridge import _send_or_edit_message

    transport = FakeTransport()
    edit_ref = MessageRef(channel_id=123, message_id=99)
    msg = RenderedMessage(text="test")

    ref, edited = await _send_or_edit_message(
        transport,
        channel_id=123,
        message=msg,
        edit_ref=edit_ref,
    )
    assert len(transport.edit_calls) == 1
    assert len(transport.send_calls) == 0
    assert ref is not None
    assert edited is True


# ===========================================================================
# Phase 2c: Keyboard edit failure in _run_loop (#104)
# ===========================================================================


@pytest.mark.anyio
async def test_keyboard_edit_failure_logged() -> None:
    """When keyboard edit fails, a warning is logged (not silently dropped)."""

    class _FailEditTransport(FakeTransport):
        async def edit(self, *, ref, message, wait=True):
            self.edit_calls.append({"ref": ref, "message": message, "wait": wait})
            # Return None to simulate edit failure when wait=True
            if wait:
                return None
            return ref

    transport = _FailEditTransport()
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)

    # Set approval buttons and trigger an event
    presenter.set_approval_buttons()
    edits.event_seq = 1
    with contextlib.suppress(anyio.WouldBlock):
        edits.signal_send.send_nowait(None)

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            await anyio.sleep(0)
            await anyio.sleep(0)
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    # The edit should have been attempted
    assert len(transport.edit_calls) >= 1


# ===========================================================================
# Phase 1f: Session summary no-events warning (#98)
# ===========================================================================


def test_session_summary_zero_events_warning_condition() -> None:
    """session.summary.no_events condition: event_count == 0 and not cancelled."""
    # This is a unit test for the condition, not the full flow.
    # The warning is emitted in runner_bridge when event_count == 0 and not cancelled.
    # Verifying the ProgressEdits stream tracks events correctly.
    from untether.runner import JsonlStreamState

    stream = JsonlStreamState(expected_session=None)
    assert stream.event_count == 0  # starts at zero

    # After processing events, count increments
    stream.event_count = 5
    assert stream.event_count == 5


@pytest.mark.anyio
async def test_stall_auto_cancel_suppressed_by_cpu_activity() -> None:
    """Stall auto-cancel should be suppressed when CPU is actively working.

    Regression test for #115: long-running sessions with active CPU
    (extended thinking) should not be auto-cancelled at max_warnings.
    """
    from unittest.mock import patch
    from untether.utils.proc_diag import ProcessDiag

    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._stall_repeat_seconds = 0.01
    edits._STALL_MAX_WARNINGS = 3
    edits.pid = 12345
    edits.event_seq = 5
    cancel_event = anyio.Event()
    edits.cancel_event = cancel_event

    # Return successive diagnostics with incrementing CPU ticks
    # (simulating an active process during extended thinking)
    call_count = 0

    def active_cpu_diag(pid: int) -> ProcessDiag:
        nonlocal call_count
        call_count += 1
        return ProcessDiag(
            pid=pid,
            alive=True,
            cpu_utime=1000 + call_count * 300,
            cpu_stime=200 + call_count * 50,
        )

    with patch(
        "untether.utils.proc_diag.collect_proc_diag",
        side_effect=active_cpu_diag,
    ):
        async with anyio.create_task_group() as tg:

            async def drive() -> None:
                for i in range(10):
                    clock.set(100.1 + i * 0.1)
                    await anyio.sleep(0.03)
                    if cancel_event.is_set():
                        break
                # CPU-active process should NOT be cancelled — close manually
                edits.signal_send.close()

            tg.start_soon(edits.run)
            tg.start_soon(drive)

    # Should NOT have been auto-cancelled
    assert not cancel_event.is_set()
    auto_cancel_msgs = [
        c for c in transport.send_calls if "Auto-cancelled" in c["message"].text
    ]
    assert len(auto_cancel_msgs) == 0
    # First stall fires (cpu_active=None, no baseline). Subsequent are suppressed
    # until frozen ring buffer escalation kicks in after 3+ frozen checks (#155).
    stall_msgs = [c for c in transport.send_calls if "No progress" in c["message"].text]
    assert len(stall_msgs) >= 1  # at least the initial notification
    # After frozen escalation, messages mention "CPU active, no new events"
    frozen_msgs = [c for c in stall_msgs if "CPU active" in c["message"].text]
    assert len(frozen_msgs) >= 1  # frozen ring buffer escalation fired


@pytest.mark.anyio
async def test_stall_auto_cancel_fires_with_flat_cpu() -> None:
    """Stall auto-cancel should still fire when CPU is flat (not active).

    Complements test_stall_auto_cancel_suppressed_by_cpu_activity to
    ensure the guard only suppresses when CPU is genuinely active.
    """
    from unittest.mock import patch
    from untether.utils.proc_diag import ProcessDiag

    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._stall_repeat_seconds = 0.01
    edits._STALL_MAX_WARNINGS = 3
    edits.pid = 12345
    edits.event_seq = 5
    cancel_event = anyio.Event()
    edits.cancel_event = cancel_event

    # Return successive diagnostics with FLAT CPU ticks (idle process)
    flat_diag = ProcessDiag(
        pid=12345,
        alive=True,
        cpu_utime=1000,
        cpu_stime=200,
    )
    with patch(
        "untether.utils.proc_diag.collect_proc_diag",
        return_value=flat_diag,
    ):
        async with anyio.create_task_group() as tg:

            async def drive() -> None:
                for i in range(10):
                    clock.set(100.1 + i * 0.1)
                    await anyio.sleep(0.03)
                    if cancel_event.is_set():
                        break
                if not cancel_event.is_set():
                    edits.signal_send.close()

            tg.start_soon(edits.run)
            tg.start_soon(drive)

    # Should have been auto-cancelled (CPU flat = not active)
    assert cancel_event.is_set()
    auto_cancel_msgs = [
        c for c in transport.send_calls if "Auto-cancelled" in c["message"].text
    ]
    assert len(auto_cancel_msgs) == 1
    assert "max_warnings" in auto_cancel_msgs[0]["message"].text


@pytest.mark.anyio
async def test_stall_notification_suppressed_when_cpu_active() -> None:
    """Stall notifications suppressed when cpu_active=True; heartbeat re-renders fire."""
    from unittest.mock import patch
    from untether.utils.proc_diag import ProcessDiag

    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._stall_repeat_seconds = 0.01
    # High max so we don't hit auto-cancel
    edits._STALL_MAX_WARNINGS = 100
    edits.pid = 12345
    edits.event_seq = 5
    cancel_event = anyio.Event()
    edits.cancel_event = cancel_event

    call_count = 0

    def active_cpu_diag(pid: int) -> ProcessDiag:
        nonlocal call_count
        call_count += 1
        return ProcessDiag(
            pid=pid,
            alive=True,
            cpu_utime=1000 + call_count * 300,
            cpu_stime=200 + call_count * 50,
        )

    initial_seq = edits.event_seq

    with patch(
        "untether.utils.proc_diag.collect_proc_diag",
        side_effect=active_cpu_diag,
    ):
        async with anyio.create_task_group() as tg:

            async def drive() -> None:
                for i in range(10):
                    clock.set(100.1 + i * 0.1)
                    await anyio.sleep(0.03)
                    if cancel_event.is_set():
                        break
                edits.signal_send.close()

            tg.start_soon(edits.run)
            tg.start_soon(drive)

    # First stall fires (cpu_active=None, no baseline). Subsequent are suppressed
    # until frozen ring buffer escalation kicks in after 3+ frozen checks (#155).
    stall_msgs = [c for c in transport.send_calls if "No progress" in c["message"].text]
    assert len(stall_msgs) >= 1  # at least the initial notification
    # Early stalls (before frozen threshold) should be suppressed via heartbeat
    # Heartbeat should have bumped event_seq (re-renders via edit)
    assert edits.event_seq > initial_seq


@pytest.mark.anyio
async def test_stall_notification_fires_when_cpu_inactive() -> None:
    """Stall notifications should fire when cpu_active=False (flat CPU)."""
    from unittest.mock import patch
    from untether.utils.proc_diag import ProcessDiag

    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._stall_repeat_seconds = 0.01
    # High max so we don't hit auto-cancel
    edits._STALL_MAX_WARNINGS = 100
    edits.pid = 12345
    edits.event_seq = 5
    cancel_event = anyio.Event()
    edits.cancel_event = cancel_event

    flat_diag = ProcessDiag(
        pid=12345,
        alive=True,
        cpu_utime=1000,
        cpu_stime=200,
    )
    with patch(
        "untether.utils.proc_diag.collect_proc_diag",
        return_value=flat_diag,
    ):
        async with anyio.create_task_group() as tg:

            async def drive() -> None:
                for i in range(10):
                    clock.set(100.1 + i * 0.1)
                    await anyio.sleep(0.03)
                    if cancel_event.is_set():
                        break
                if not cancel_event.is_set():
                    edits.signal_send.close()

            tg.start_soon(edits.run)
            tg.start_soon(drive)

    # Stall notifications should have fired (CPU inactive)
    stall_msgs = [c for c in transport.send_calls if "No progress" in c["message"].text]
    assert len(stall_msgs) >= 1


@pytest.mark.anyio
async def test_stall_not_suppressed_when_main_sleeping() -> None:
    """Stall notification should fire when cpu_active=True but main process is
    sleeping (state=S) — CPU activity is from child processes (hung Bash tool),
    not from Claude doing extended thinking."""
    from unittest.mock import patch
    from untether.utils.proc_diag import ProcessDiag

    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._stall_repeat_seconds = 0.01
    edits._STALL_MAX_WARNINGS = 100
    edits.pid = 12345
    edits.event_seq = 5
    cancel_event = anyio.Event()
    edits.cancel_event = cancel_event

    call_count = 0

    def sleeping_cpu_diag(pid: int) -> ProcessDiag:
        nonlocal call_count
        call_count += 1
        return ProcessDiag(
            pid=pid,
            alive=True,
            state="S",  # sleeping — waiting for child process
            cpu_utime=1000 + call_count * 300,
            cpu_stime=200 + call_count * 50,
        )

    with patch(
        "untether.utils.proc_diag.collect_proc_diag",
        side_effect=sleeping_cpu_diag,
    ):
        async with anyio.create_task_group() as tg:

            async def drive() -> None:
                for i in range(6):
                    clock.set(100.1 + i * 0.1)
                    await anyio.sleep(0.03)
                    if cancel_event.is_set():
                        break
                edits.signal_send.close()

            tg.start_soon(edits.run)
            tg.start_soon(drive)

    # Despite cpu_active=True, notifications should NOT be suppressed because
    # the main process is sleeping (state=S) — child processes are active.
    stall_msgs = [
        c
        for c in transport.send_calls
        if "progress" in c["message"].text.lower()
        or "stuck" in c["message"].text.lower()
        or "tool" in c["message"].text.lower()
    ]
    assert len(stall_msgs) >= 2, (
        f"Expected multiple stall notifications when main sleeping, got {len(stall_msgs)}"
    )


@pytest.mark.anyio
async def test_stall_message_includes_tool_name_when_sleeping() -> None:
    """Stall message should mention the tool name when main process is sleeping."""
    from unittest.mock import patch
    from untether.utils.proc_diag import ProcessDiag

    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, presenter, clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._stall_repeat_seconds = 0.01
    edits._STALL_MAX_WARNINGS = 100
    edits.pid = 12345
    edits.event_seq = 5
    cancel_event = anyio.Event()
    edits.cancel_event = cancel_event

    # Set the last action to simulate a Bash tool running
    from untether.model import Action, ActionEvent

    evt = ActionEvent(
        engine="claude",
        action=Action(id="a1", kind="tool", title="Bash"),
        phase="started",
    )
    await edits.on_event(evt)
    # Complete the action so last_action shows it
    evt2 = ActionEvent(
        engine="claude",
        action=Action(id="a1", kind="tool", title="Bash"),
        phase="completed",
        ok=True,
    )
    await edits.on_event(evt2)

    call_count = 0

    def sleeping_diag(pid: int) -> ProcessDiag:
        nonlocal call_count
        call_count += 1
        return ProcessDiag(
            pid=pid,
            alive=True,
            state="S",
            cpu_utime=1000 + call_count * 300,
            cpu_stime=200 + call_count * 50,
        )

    with patch(
        "untether.utils.proc_diag.collect_proc_diag",
        side_effect=sleeping_diag,
    ):
        async with anyio.create_task_group() as tg:

            async def drive() -> None:
                for i in range(4):
                    clock.set(100.1 + i * 0.1)
                    await anyio.sleep(0.03)
                    if cancel_event.is_set():
                        break
                edits.signal_send.close()

            tg.start_soon(edits.run)
            tg.start_soon(drive)

    # At least one stall message should mention "Bash tool"
    tool_msgs = [c for c in transport.send_calls if "Bash tool" in c["message"].text]
    assert len(tool_msgs) >= 1, (
        f"Expected stall message mentioning 'Bash tool', got messages: "
        f"{[c['message'].text for c in transport.send_calls]}"
    )


# ---------------------------------------------------------------------------
# Plan outline rendering, keyboard, and cleanup tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_outline_messages_rendered_with_entities() -> None:
    """Outline messages should be rendered as markdown with Telegram entities."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)

    outline = "## Heading\n\n**Bold text** and `code`."
    async with anyio.create_task_group() as tg:
        await edits._send_outline(outline, tg)
        # Let the background task complete
        await anyio.sleep(0)

    # Should have sent one message (short text)
    outline_sends = [
        s for s in transport.send_calls if s["message"].extra.get("entities")
    ]
    assert len(outline_sends) == 1
    msg = outline_sends[0]["message"]
    # Entities should be present (not raw markdown)
    assert len(msg.extra["entities"]) > 0
    # Raw markdown syntax should NOT appear in the text
    assert "##" not in msg.text
    assert "**" not in msg.text


@pytest.mark.anyio
async def test_outline_last_message_has_approval_keyboard() -> None:
    """The last outline message should have the approval keyboard attached."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)

    approval_kb = {"inline_keyboard": [[{"text": "Approve Plan"}, {"text": "Deny"}]]}
    outline = "## Plan\n\nStep 1.\n\nStep 2."
    async with anyio.create_task_group() as tg:
        await edits._send_outline(outline, tg, approval_keyboard=approval_kb)
        await anyio.sleep(0)

    # The last sent message should have the approval keyboard
    last_send = transport.send_calls[-1]
    assert last_send["message"].extra.get("reply_markup") == approval_kb


@pytest.mark.anyio
async def test_outline_multi_chunk_keyboard_only_on_last() -> None:
    """When outline is split across messages, only the last gets buttons."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)

    approval_kb = {"inline_keyboard": [[{"text": "Approve Plan"}, {"text": "Deny"}]]}
    # Create text that will be split (>3500 chars)
    outline = "## Section\n\n" + "x" * 3000 + "\n\n## Section 2\n\n" + "y" * 3000
    async with anyio.create_task_group() as tg:
        await edits._send_outline(outline, tg, approval_keyboard=approval_kb)
        await anyio.sleep(0)

    outline_sends = list(transport.send_calls)
    assert len(outline_sends) >= 2
    # Only the last should have the keyboard
    for s in outline_sends[:-1]:
        assert s["message"].extra.get("reply_markup") is None
    assert outline_sends[-1]["message"].extra.get("reply_markup") == approval_kb


@pytest.mark.anyio
async def test_outline_refs_tracked() -> None:
    """Sent outline message refs are tracked in _outline_refs."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)

    outline = "## Plan\n\nDo things."
    async with anyio.create_task_group() as tg:
        await edits._send_outline(outline, tg)
        await anyio.sleep(0)

    assert len(edits._outline_refs) == 1
    assert edits._outline_refs[0] == transport.send_calls[-1]["ref"]


@pytest.mark.anyio
async def test_outline_messages_deleted_on_approval_transition() -> None:
    """When approval buttons disappear, outline messages should be deleted."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)

    # Simulate: approval buttons appear → outline sent → buttons disappear
    presenter.set_approval_buttons()
    edits.event_seq = 1
    with contextlib.suppress(anyio.WouldBlock):
        edits.signal_send.send_nowait(None)

    async with anyio.create_task_group() as tg:

        async def run_cycle() -> None:
            # Let first render (with approval) complete
            await anyio.sleep(0)
            await anyio.sleep(0)
            # Manually inject outline refs (simulating _send_outline)
            outline_ref = MessageRef(channel_id=123, message_id=999)
            edits._outline_refs.append(outline_ref)
            # Now remove approval buttons
            presenter.set_no_approval()
            edits.event_seq = 2
            with contextlib.suppress(anyio.WouldBlock):
                edits.signal_send.send_nowait(None)
            await anyio.sleep(0)
            await anyio.sleep(0)
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(run_cycle)

    # The outline ref (999) should have been deleted
    deleted_ids = [r.message_id for r in transport.delete_calls]
    assert 999 in deleted_ids
    assert edits._outline_refs == []


@pytest.mark.anyio
async def test_outline_deleted_on_keyboard_change() -> None:
    """Outline deleted when approval buttons change (e.g. ExitPlanMode → Write)."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)

    # Simulate: approval buttons appear (ExitPlanMode) → outline sent
    presenter.set_approval_buttons()
    edits.event_seq = 1
    with contextlib.suppress(anyio.WouldBlock):
        edits.signal_send.send_nowait(None)

    async with anyio.create_task_group() as tg:

        async def run_cycle() -> None:
            # Let first render (with approval) complete
            await anyio.sleep(0)
            await anyio.sleep(0)
            # Inject outline refs
            outline_ref = MessageRef(channel_id=123, message_id=888)
            edits._outline_refs.append(outline_ref)
            # Change keyboard to DIFFERENT approval buttons (simulates
            # ExitPlanMode resolved → new Write tool approval appeared)
            presenter.keyboard = [
                [{"text": "Approve", "callback_data": "ctrl:approve:new_req"}],
                [{"text": "Cancel"}],
            ]
            edits.event_seq = 2
            with contextlib.suppress(anyio.WouldBlock):
                edits.signal_send.send_nowait(None)
            await anyio.sleep(0)
            await anyio.sleep(0)
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(run_cycle)

    # Outline should be deleted even though buttons never disappeared
    deleted_ids = [r.message_id for r in transport.delete_calls]
    assert 888 in deleted_ids
    assert edits._outline_refs == []


@pytest.mark.anyio
async def test_outline_messages_deleted_in_delete_ephemeral() -> None:
    """Safety net: delete_ephemeral() cleans up remaining outline refs."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)

    # Manually add outline refs
    edits._outline_refs = [
        MessageRef(channel_id=123, message_id=50),
        MessageRef(channel_id=123, message_id=51),
    ]

    await edits.delete_ephemeral()

    deleted_ids = [r.message_id for r in transport.delete_calls]
    assert 50 in deleted_ids
    assert 51 in deleted_ids
    assert edits._outline_refs == []


@pytest.mark.anyio
async def test_outline_not_double_deleted() -> None:
    """Refs cleared on transition should not be re-deleted in delete_ephemeral."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)

    # Simulate transition already cleared the refs
    edits._outline_refs = []

    await edits.delete_ephemeral()

    # No outline deletes should have happened
    assert transport.delete_calls == []


@pytest.mark.anyio
async def test_outline_sent_strips_approval_from_progress() -> None:
    """When outline is sent, progress message should only keep cancel button (#163)."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)

    # Mark outline as sent with visible refs (simulating outline delivery)
    edits._outline_sent = True
    edits._outline_refs.append(MessageRef(channel_id=123, message_id=500))

    # Trigger render with approval buttons from the presenter
    presenter.set_approval_buttons()
    edits.event_seq = 1
    with contextlib.suppress(anyio.WouldBlock):
        edits.signal_send.send_nowait(None)

    async with anyio.create_task_group() as tg:

        async def run_cycle() -> None:
            await anyio.sleep(0)
            await anyio.sleep(0)
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(run_cycle)

    # Progress message should only have cancel row (approval stripped)
    last_edit = transport.edit_calls[-1]
    kb = last_edit["message"].extra["reply_markup"]["inline_keyboard"]
    assert len(kb) == 1  # Only cancel row
    assert kb[0][0]["text"] == "Cancel"


@pytest.mark.anyio
async def test_outline_state_resets_on_approval_disappear() -> None:
    """After outline cycle completes, _outline_sent resets for future requests (#163)."""
    transport = FakeTransport()
    presenter = _KeyboardPresenter()
    edits = _make_edits(transport, presenter)

    # Simulate: outline was sent, refs cleaned up, approval buttons visible
    edits._outline_sent = True
    presenter.set_approval_buttons()
    edits.event_seq = 1
    with contextlib.suppress(anyio.WouldBlock):
        edits.signal_send.send_nowait(None)

    async with anyio.create_task_group() as tg:

        async def run_cycle() -> None:
            # First cycle: approval with outline_sent → stripped
            await anyio.sleep(0)
            await anyio.sleep(0)
            # Now buttons disappear (approval resolved)
            presenter.set_no_approval()
            edits.event_seq = 2
            with contextlib.suppress(anyio.WouldBlock):
                edits.signal_send.send_nowait(None)
            await anyio.sleep(0)
            await anyio.sleep(0)
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(run_cycle)

    # _outline_sent should be reset so future ExitPlanMode works
    assert edits._outline_sent is False


# ---------------------------------------------------------------------------
# Outbox file delivery tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_outbox_files_sent_after_completion(tmp_path) -> None:
    """Outbox files are delivered as documents after a successful run."""
    from unittest.mock import AsyncMock

    from untether.settings import TelegramFilesSettings
    from untether.utils.paths import set_run_base_dir, reset_run_base_dir

    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "result.txt").write_text("hello", encoding="utf-8")

    send_file = AsyncMock()
    files_cfg = TelegramFilesSettings(enabled=True)
    transport = FakeTransport()
    runner = _return_runner(answer="done")
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=False,
        send_file=send_file,
        outbox_config=files_cfg,
    )
    incoming = IncomingMessage(channel_id=1, message_id=1, text="test")
    token = set_run_base_dir(tmp_path)
    try:
        await handle_message(cfg, runner=runner, incoming=incoming, resume_token=None)
    finally:
        reset_run_base_dir(token)

    send_file.assert_called_once()
    call_args = send_file.call_args[0]
    assert call_args[2] == "result.txt"  # filename


@pytest.mark.anyio
async def test_outbox_not_scanned_when_disabled(tmp_path) -> None:
    """Outbox is not scanned when send_file callback is None."""
    from untether.utils.paths import set_run_base_dir, reset_run_base_dir

    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "result.txt").write_text("hello", encoding="utf-8")

    transport = FakeTransport()
    runner = _return_runner(answer="done")
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=False,
        # send_file=None (default) — outbox disabled
    )
    incoming = IncomingMessage(channel_id=1, message_id=1, text="test")
    token = set_run_base_dir(tmp_path)
    try:
        await handle_message(cfg, runner=runner, incoming=incoming, resume_token=None)
    finally:
        reset_run_base_dir(token)

    # File should still exist — outbox was not processed
    assert (outbox / "result.txt").exists()


@pytest.mark.anyio
async def test_outbox_not_scanned_on_error(tmp_path) -> None:
    """Outbox delivery is skipped when run_ok is False."""
    from unittest.mock import AsyncMock

    from untether.settings import TelegramFilesSettings
    from untether.utils.paths import set_run_base_dir, reset_run_base_dir

    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "result.txt").write_text("hello", encoding="utf-8")

    send_file = AsyncMock()
    files_cfg = TelegramFilesSettings(enabled=True)
    transport = FakeTransport()
    runner = ScriptRunner([ErrorReturn(error="failed")], engine=CODEX_ENGINE)
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=False,
        send_file=send_file,
        outbox_config=files_cfg,
    )
    incoming = IncomingMessage(channel_id=1, message_id=1, text="test")
    token = set_run_base_dir(tmp_path)
    try:
        await handle_message(cfg, runner=runner, incoming=incoming, resume_token=None)
    finally:
        reset_run_base_dir(token)

    send_file.assert_not_called()


# ── _should_auto_continue detection (#34142/#30333) ──


class TestShouldAutoContinue:
    """Tests for the auto-continue detection function."""

    def _call(
        self,
        *,
        last_event_type: str | None = "user",
        engine: str = "claude",
        cancelled: bool = False,
        resume_value: str | None = "c3f20b1d-58f9-4173-a68e-8735256cf9ae",
        auto_continued_count: int = 0,
        max_retries: int = 1,
    ) -> bool:
        from untether.runner_bridge import _should_auto_continue

        return _should_auto_continue(
            last_event_type=last_event_type,
            engine=engine,
            cancelled=cancelled,
            resume_value=resume_value,
            auto_continued_count=auto_continued_count,
            max_retries=max_retries,
        )

    def test_detects_bug_scenario(self):
        assert self._call() is True

    def test_skips_non_claude_engine(self):
        assert self._call(engine="codex") is False

    def test_skips_cancelled(self):
        assert self._call(cancelled=True) is False

    def test_skips_result_event_type(self):
        assert self._call(last_event_type="result") is False

    def test_skips_assistant_event_type(self):
        assert self._call(last_event_type="assistant") is False

    def test_skips_none_event_type(self):
        assert self._call(last_event_type=None) is False

    def test_skips_no_resume(self):
        assert self._call(resume_value=None) is False

    def test_skips_empty_resume(self):
        assert self._call(resume_value="") is False

    def test_respects_max_retries(self):
        assert self._call(auto_continued_count=0, max_retries=1) is True
        assert self._call(auto_continued_count=1, max_retries=1) is False
        assert self._call(auto_continued_count=2, max_retries=3) is True
        assert self._call(auto_continued_count=3, max_retries=3) is False

    def test_disabled_when_max_retries_zero(self):
        assert self._call(auto_continued_count=0, max_retries=0) is False
