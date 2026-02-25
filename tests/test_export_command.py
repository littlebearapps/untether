"""Tests for the /export command."""

from __future__ import annotations

from untether.telegram.commands.export import (
    _SESSION_HISTORY,
    _format_export_json,
    _format_export_markdown,
    record_session_event,
    record_session_usage,
)


def _reset():
    _SESSION_HISTORY.clear()


class TestRecordSessionEvent:
    def setup_method(self):
        _reset()

    def test_records_event(self):
        record_session_event("sess1", {"type": "started"})
        assert "sess1" in _SESSION_HISTORY
        _, events, _ = _SESSION_HISTORY["sess1"]
        assert len(events) == 1
        assert events[0]["type"] == "started"

    def test_accumulates_events(self):
        record_session_event("sess1", {"type": "started"})
        record_session_event("sess1", {"type": "action", "phase": "started"})
        _, events, _ = _SESSION_HISTORY["sess1"]
        assert len(events) == 2

    def test_records_usage(self):
        record_session_event("sess1", {"type": "started"})
        record_session_usage("sess1", {"total_cost_usd": 0.15})
        _, _, usage = _SESSION_HISTORY["sess1"]
        assert usage["total_cost_usd"] == 0.15

    def test_trims_old_sessions(self):
        for i in range(25):
            record_session_event(f"sess{i}", {"type": "started"})
        assert len(_SESSION_HISTORY) <= 20


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
