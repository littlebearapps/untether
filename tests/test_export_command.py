"""Tests for the /export command."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from untether.telegram.commands.export import (
    _SESSION_HISTORY,
    ExportCommand,
    _format_export_json,
    _format_export_markdown,
    record_session_event,
    record_session_usage,
)


def _reset():
    _SESSION_HISTORY.clear()


CHAT_A = 111
CHAT_B = 222


class TestRecordSessionEvent:
    def setup_method(self):
        _reset()

    def test_records_event(self):
        record_session_event("sess1", {"type": "started"}, channel_id=CHAT_A)
        assert (CHAT_A, "sess1") in _SESSION_HISTORY
        _, events, _ = _SESSION_HISTORY[(CHAT_A, "sess1")]
        assert len(events) == 1
        assert events[0]["type"] == "started"

    def test_accumulates_events(self):
        record_session_event("sess1", {"type": "started"}, channel_id=CHAT_A)
        record_session_event(
            "sess1", {"type": "action", "phase": "started"}, channel_id=CHAT_A
        )
        _, events, _ = _SESSION_HISTORY[(CHAT_A, "sess1")]
        assert len(events) == 2

    def test_records_usage(self):
        record_session_event("sess1", {"type": "started"}, channel_id=CHAT_A)
        record_session_usage("sess1", {"total_cost_usd": 0.15}, channel_id=CHAT_A)
        _, _, usage = _SESSION_HISTORY[(CHAT_A, "sess1")]
        assert usage is not None
        assert usage["total_cost_usd"] == 0.15

    def test_trims_old_sessions(self):
        for i in range(25):
            record_session_event(f"sess{i}", {"type": "started"}, channel_id=CHAT_A)
        assert len(_SESSION_HISTORY) <= 20

    def test_same_session_id_different_chats_are_separate(self):
        record_session_event("sess1", {"type": "started"}, channel_id=CHAT_A)
        record_session_event("sess1", {"type": "started"}, channel_id=CHAT_B)
        assert len(_SESSION_HISTORY) == 2
        assert (CHAT_A, "sess1") in _SESSION_HISTORY
        assert (CHAT_B, "sess1") in _SESSION_HISTORY

    def test_default_channel_id_zero(self):
        record_session_event("sess1", {"type": "started"})
        assert (0, "sess1") in _SESSION_HISTORY


class TestExportChatIsolation:
    """Verify /export only returns sessions from the requesting chat."""

    def setup_method(self):
        _reset()

    @pytest.mark.anyio
    async def test_export_returns_own_chat_session(self):
        import time

        record_session_event(
            "sess_a",
            {"type": "started", "engine": "claude", "title": "opus"},
            channel_id=CHAT_A,
        )
        time.sleep(0.01)
        record_session_event(
            "sess_b",
            {"type": "started", "engine": "opencode", "title": "opencode"},
            channel_id=CHAT_B,
        )

        cmd = ExportCommand()

        @dataclass
        class FakeMessage:
            channel_id: int = CHAT_A
            message_id: int = 1

        @dataclass
        class FakeCtx:
            args_text: str = "json"
            message: FakeMessage = None  # type: ignore[assignment]

            def __post_init__(self):
                if self.message is None:
                    self.message = FakeMessage()

        # Chat A should get sess_a (claude), not sess_b (opencode)
        ctx_a = FakeCtx(message=FakeMessage(channel_id=CHAT_A))
        result_a = await cmd.handle(ctx_a)  # type: ignore[arg-type]
        assert result_a is not None
        assert "claude" in result_a.text.lower() or "sess_a" in result_a.text

        # Chat B should get sess_b (opencode)
        ctx_b = FakeCtx(message=FakeMessage(channel_id=CHAT_B))
        result_b = await cmd.handle(ctx_b)  # type: ignore[arg-type]
        assert result_b is not None
        assert "opencode" in result_b.text.lower() or "sess_b" in result_b.text

    @pytest.mark.anyio
    async def test_export_no_sessions_for_chat(self):
        record_session_event("sess_a", {"type": "started"}, channel_id=CHAT_A)

        cmd = ExportCommand()

        @dataclass
        class FakeMessage:
            channel_id: int = CHAT_B
            message_id: int = 1

        @dataclass
        class FakeCtx:
            args_text: str = "md"
            message: FakeMessage = None  # type: ignore[assignment]

            def __post_init__(self):
                if self.message is None:
                    self.message = FakeMessage()

        ctx = FakeCtx()
        result = await cmd.handle(ctx)  # type: ignore[arg-type]
        assert result is not None
        assert "no session history" in result.text.lower()


class TestFormatExportMarkdown:
    def test_basic_export(self):
        events = [
            {"type": "started", "engine": "claude", "title": "opus"},
            {
                "type": "action",
                "phase": "completed",
                "ok": True,
                "action": {"id": "1", "kind": "tool", "title": "Read file.py"},
            },
            {
                "type": "completed",
                "ok": True,
                "answer": "Done!",
                "error": None,
            },
        ]
        md = _format_export_markdown("test-session", events, None)
        assert "test-session" in md
        assert "Session Started" in md
        assert "Read file.py" in md
        assert "Completed" in md
        assert "Done!" in md

    def test_with_usage(self):
        md = _format_export_markdown(
            "s1",
            [{"type": "completed", "ok": True, "answer": "ok", "error": None}],
            {"total_cost_usd": 0.5, "num_turns": 3, "duration_ms": 10000},
        )
        assert "$0.5" in md
        assert "3 turns" in md

    def test_with_token_counts_no_cost(self):
        """Codex-style usage with only token counts shows token summary."""
        md = _format_export_markdown(
            "s1",
            [{"type": "completed", "ok": True, "answer": "ok", "error": None}],
            {"input_tokens": 5000, "output_tokens": 1200, "cached_input_tokens": 3000},
        )
        assert "5000 in / 1200 out tokens" in md
        assert "Usage:" in md

    def test_with_cost_and_tokens_cost_wins(self):
        """When both cost and tokens are present, show cost not token fallback."""
        md = _format_export_markdown(
            "s1",
            [{"type": "completed", "ok": True, "answer": "ok", "error": None}],
            {"total_cost_usd": 0.05, "input_tokens": 5000, "output_tokens": 1200},
        )
        assert "$0.05" in md
        assert "tokens" not in md

    def test_with_input_tokens_only(self):
        """Token fallback works with only input_tokens (no output)."""
        md = _format_export_markdown(
            "s1",
            [{"type": "completed", "ok": True, "answer": "ok", "error": None}],
            {"input_tokens": 3000},
        )
        assert "3000 in tokens" in md
        assert "out" not in md

    def test_duplicate_started_events_deduplicated(self):
        """Resume runs with same session_id produce duplicate started events;
        only the first should be rendered."""
        events = [
            {"type": "started", "engine": "codex", "title": "Codex"},
            {
                "type": "action",
                "phase": "started",
                "ok": None,
                "action": {"id": "t0", "kind": "turn", "title": "turn started"},
            },
            {"type": "started", "engine": "codex", "title": "Codex"},
            {
                "type": "action",
                "phase": "started",
                "ok": None,
                "action": {"id": "t1", "kind": "turn", "title": "turn started"},
            },
            {"type": "completed", "ok": True, "answer": "done", "error": None},
        ]
        md = _format_export_markdown("codex-sess", events, None)
        assert md.count("Session Started") == 1

    def test_error_export(self):
        events = [
            {
                "type": "completed",
                "ok": False,
                "answer": "",
                "error": "timeout",
            },
        ]
        md = _format_export_markdown("s1", events, None)
        assert "Failed" in md
        assert "timeout" in md


class TestFormatExportJson:
    def test_produces_valid_json(self):
        import json

        events = [{"type": "started", "engine": "claude", "title": "opus"}]
        result = _format_export_json("s1", events, {"cost": 0.1})
        parsed = json.loads(result)
        assert parsed["session_id"] == "s1"
        assert len(parsed["events"]) == 1
        assert parsed["usage"]["cost"] == 0.1
