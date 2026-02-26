from __future__ import annotations

from untether.markdown import (
    HARD_BREAK,
    MarkdownFormatter,
    _short_model_name,
    format_meta_line,
)
from untether.model import ResumeToken, StartedEvent
from untether.progress import ProgressTracker


class TestShortModelName:
    def test_sonnet_full_id(self) -> None:
        assert _short_model_name("claude-sonnet-4-5-20250929") == "sonnet"

    def test_opus_full_id(self) -> None:
        assert _short_model_name("claude-opus-4-6") == "opus"

    def test_haiku_full_id(self) -> None:
        assert _short_model_name("claude-haiku-4-5-20251001") == "haiku"

    def test_already_short(self) -> None:
        assert _short_model_name("sonnet") == "sonnet"

    def test_non_claude_with_date_suffix(self) -> None:
        assert _short_model_name("gpt-4o-20240513") == "gpt-4o"

    def test_non_claude_no_date(self) -> None:
        assert _short_model_name("gemini-pro") == "gemini-pro"

    def test_case_insensitive(self) -> None:
        assert _short_model_name("Claude-Sonnet-4-5") == "sonnet"


class TestFormatMetaLine:
    def test_full_model_and_permission(self) -> None:
        result = format_meta_line(
            {"model": "claude-sonnet-4-5-20250929", "permissionMode": "plan"}
        )
        assert result == "\N{LABEL} sonnet \N{MIDDLE DOT} plan"

    def test_already_short_model(self) -> None:
        result = format_meta_line({"model": "opus", "permissionMode": "default"})
        assert result == "\N{LABEL} opus \N{MIDDLE DOT} default"

    def test_model_only(self) -> None:
        result = format_meta_line({"model": "claude-haiku-4-5-20251001"})
        assert result == "\N{LABEL} haiku"

    def test_permission_only(self) -> None:
        result = format_meta_line({"permissionMode": "plan"})
        assert result == "\N{LABEL} plan"

    def test_empty_meta(self) -> None:
        assert format_meta_line({}) is None

    def test_none_values(self) -> None:
        assert format_meta_line({"model": None, "permissionMode": None}) is None

    def test_empty_string_values(self) -> None:
        assert format_meta_line({"model": "", "permissionMode": ""}) is None

    def test_non_string_values_ignored(self) -> None:
        assert format_meta_line({"model": 42, "permissionMode": True}) is None


class TestProgressTrackerMeta:
    """Test that ProgressTracker stores meta from StartedEvent."""

    def test_note_event_stores_meta(self) -> None:
        tracker = ProgressTracker(engine="claude")
        meta = {"model": "claude-sonnet-4-5-20250929", "permissionMode": "plan"}
        evt = StartedEvent(
            engine="claude",
            resume=ResumeToken(engine="claude", value="sess-1"),
            meta=meta,
        )
        tracker.note_event(evt)
        assert tracker.meta == meta

    def test_note_event_no_meta(self) -> None:
        tracker = ProgressTracker(engine="claude")
        evt = StartedEvent(
            engine="claude",
            resume=ResumeToken(engine="claude", value="sess-1"),
        )
        tracker.note_event(evt)
        assert tracker.meta is None

    def test_snapshot_with_meta_formatter(self) -> None:
        tracker = ProgressTracker(engine="claude")
        meta = {"model": "claude-sonnet-4-5-20250929", "permissionMode": "plan"}
        evt = StartedEvent(
            engine="claude",
            resume=ResumeToken(engine="claude", value="sess-1"),
            meta=meta,
        )
        tracker.note_event(evt)
        state = tracker.snapshot(meta_formatter=format_meta_line)
        assert state.meta_line == "\N{LABEL} sonnet \N{MIDDLE DOT} plan"

    def test_snapshot_without_meta_formatter(self) -> None:
        tracker = ProgressTracker(engine="claude")
        meta = {"model": "sonnet"}
        evt = StartedEvent(
            engine="claude",
            resume=ResumeToken(engine="claude", value="sess-1"),
            meta=meta,
        )
        tracker.note_event(evt)
        state = tracker.snapshot()
        assert state.meta_line is None

    def test_snapshot_no_meta_with_formatter(self) -> None:
        tracker = ProgressTracker(engine="codex")
        evt = StartedEvent(
            engine="codex",
            resume=ResumeToken(engine="codex", value="sess-1"),
        )
        tracker.note_event(evt)
        state = tracker.snapshot(meta_formatter=format_meta_line)
        assert state.meta_line is None


class TestFooterWithMetaLine:
    """Test that _format_footer renders meta_line between context and resume."""

    def test_footer_ordering_ctx_meta_resume(self) -> None:
        tracker = ProgressTracker(engine="claude")
        meta = {"model": "claude-sonnet-4-5-20250929", "permissionMode": "plan"}
        evt = StartedEvent(
            engine="claude",
            resume=ResumeToken(engine="claude", value="sess-1"),
            meta=meta,
        )
        tracker.note_event(evt)
        state = tracker.snapshot(
            resume_formatter=lambda t: f"`claude --resume {t.value}`",
            context_line="ctx: untether @master",
            meta_formatter=format_meta_line,
        )
        formatter = MarkdownFormatter()
        parts = formatter.render_final_parts(
            state, elapsed_s=10.0, status="done", answer="hello"
        )
        assert parts.footer is not None
        lines = parts.footer.split(HARD_BREAK)
        assert lines[0] == "ctx: untether @master"
        assert lines[1] == "\N{LABEL} sonnet \N{MIDDLE DOT} plan"
        assert lines[2] == "`claude --resume sess-1`"

    def test_footer_meta_only(self) -> None:
        tracker = ProgressTracker(engine="claude")
        meta = {"model": "opus"}
        evt = StartedEvent(
            engine="claude",
            resume=ResumeToken(engine="claude", value="sess-1"),
            meta=meta,
        )
        tracker.note_event(evt)
        state = tracker.snapshot(meta_formatter=format_meta_line)
        formatter = MarkdownFormatter()
        parts = formatter.render_final_parts(
            state, elapsed_s=5.0, status="done", answer="ok"
        )
        assert parts.footer == "\N{LABEL} opus"

    def test_footer_no_meta_unchanged(self) -> None:
        """Without meta, footer behaves exactly as before."""
        tracker = ProgressTracker(engine="codex")
        evt = StartedEvent(
            engine="codex",
            resume=ResumeToken(engine="codex", value="t-1"),
        )
        tracker.note_event(evt)
        state = tracker.snapshot(
            resume_formatter=lambda t: f"`codex resume {t.value}`",
            context_line="ctx: proj @main",
        )
        formatter = MarkdownFormatter()
        parts = formatter.render_final_parts(
            state, elapsed_s=5.0, status="done", answer="ok"
        )
        assert parts.footer is not None
        lines = parts.footer.split(HARD_BREAK)
        assert len(lines) == 2
        assert lines[0] == "ctx: proj @main"
        assert lines[1] == "`codex resume t-1`"
