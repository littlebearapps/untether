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
        assert _short_model_name("claude-sonnet-4-5-20250929") == "sonnet 4.5"

    def test_opus_full_id(self) -> None:
        assert _short_model_name("claude-opus-4-6") == "opus 4.6"

    def test_haiku_full_id(self) -> None:
        assert _short_model_name("claude-haiku-4-5-20251001") == "haiku 4.5"

    def test_already_short(self) -> None:
        assert _short_model_name("sonnet") == "sonnet"

    def test_bare_opus(self) -> None:
        assert _short_model_name("opus") == "opus"

    def test_non_claude_with_date_suffix(self) -> None:
        assert _short_model_name("gpt-4o-20240513") == "gpt-4o"

    def test_non_claude_no_date(self) -> None:
        assert _short_model_name("gemini-pro") == "gemini-pro"

    def test_case_insensitive(self) -> None:
        assert _short_model_name("Claude-Sonnet-4-5") == "sonnet 4.5"

    def test_gemini_unchanged(self) -> None:
        assert _short_model_name("gemini-2.5-pro") == "gemini-2.5-pro"

    def test_auto_gemini_stripped(self) -> None:
        assert _short_model_name("auto-gemini-3") == "gemini-3"

    def test_auto_gemini_with_variant(self) -> None:
        assert _short_model_name("auto-gemini-2.5-pro") == "gemini-2.5-pro"

    def test_auto_claude_stripped(self) -> None:
        assert _short_model_name("auto-claude-sonnet-4-5") == "sonnet 4.5"

    def test_opus_with_1m_context(self) -> None:
        assert _short_model_name("claude-opus-4-6[1m]") == "opus 4.6 (1M)"

    def test_sonnet_with_1m_context(self) -> None:
        assert _short_model_name("claude-sonnet-4-5[1m]") == "sonnet 4.5 (1M)"

    def test_opus_with_date_and_1m(self) -> None:
        assert _short_model_name("claude-opus-4-6-20260101[1m]") == "opus 4.6 (1M)"

    def test_unknown_context_suffix(self) -> None:
        assert _short_model_name("claude-opus-4-6[500k]") == "opus 4.6 (500K)"

    def test_no_bracket_unchanged(self) -> None:
        assert _short_model_name("claude-opus-4-6") == "opus 4.6"


class TestFormatMetaLine:
    def test_full_model_and_permission(self) -> None:
        result = format_meta_line(
            {"model": "claude-sonnet-4-5-20250929", "permissionMode": "plan"}
        )
        assert result == "sonnet 4.5 \N{MIDDLE DOT} plan"

    def test_already_short_model(self) -> None:
        result = format_meta_line({"model": "opus", "permissionMode": "default"})
        assert result == "opus \N{MIDDLE DOT} default"

    def test_model_only(self) -> None:
        result = format_meta_line({"model": "claude-haiku-4-5-20251001"})
        assert result == "haiku 4.5"

    def test_permission_only(self) -> None:
        result = format_meta_line({"permissionMode": "plan"})
        assert result == "plan"

    def test_empty_meta(self) -> None:
        assert format_meta_line({}) is None

    def test_none_values(self) -> None:
        assert format_meta_line({"model": None, "permissionMode": None}) is None

    def test_empty_string_values(self) -> None:
        assert format_meta_line({"model": "", "permissionMode": ""}) is None

    def test_non_string_values_ignored(self) -> None:
        assert format_meta_line({"model": 42, "permissionMode": True}) is None

    def test_effort_between_model_and_permission(self) -> None:
        result = format_meta_line(
            {
                "model": "claude-opus-4-6",
                "effort": "medium",
                "permissionMode": "plan",
            }
        )
        assert result == "opus 4.6 \N{MIDDLE DOT} medium \N{MIDDLE DOT} plan"

    def test_effort_with_model_only(self) -> None:
        result = format_meta_line({"model": "claude-opus-4-6", "effort": "low"})
        assert result == "opus 4.6 \N{MIDDLE DOT} low"

    def test_effort_only(self) -> None:
        result = format_meta_line({"effort": "high"})
        assert result == "high"

    def test_effort_ignored_when_empty(self) -> None:
        result = format_meta_line({"model": "opus", "effort": ""})
        assert result == "opus"

    def test_effort_ignored_when_non_string(self) -> None:
        result = format_meta_line({"model": "opus", "effort": 42})
        assert result == "opus"

    def test_1m_model_with_permission(self) -> None:
        result = format_meta_line(
            {"model": "claude-opus-4-6[1m]", "permissionMode": "plan"}
        )
        assert result == "opus 4.6 (1M) \N{MIDDLE DOT} plan"


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
        assert state.meta_line == "sonnet 4.5 \N{MIDDLE DOT} plan"

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
    """Test that _format_footer combines context + meta into a single 🏷 info line."""

    def test_footer_combined_dir_meta_resume(self) -> None:
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
            context_line="dir: untether @master",
            meta_formatter=format_meta_line,
        )
        formatter = MarkdownFormatter()
        parts = formatter.render_final_parts(
            state, elapsed_s=10.0, status="done", answer="hello"
        )
        assert parts.footer is not None
        lines = parts.footer.split(HARD_BREAK)
        assert (
            lines[0]
            == "\N{LABEL} dir: untether @master | sonnet 4.5 \N{MIDDLE DOT} plan"
        )
        assert lines[1] == ""  # blank line for visual separation
        assert lines[2] == "\u21a9\ufe0f `claude --resume sess-1`"

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

    def test_footer_dir_only(self) -> None:
        """Context line without meta still gets 🏷 prefix."""
        tracker = ProgressTracker(engine="codex")
        evt = StartedEvent(
            engine="codex",
            resume=ResumeToken(engine="codex", value="t-1"),
        )
        tracker.note_event(evt)
        state = tracker.snapshot(context_line="dir: proj")
        formatter = MarkdownFormatter()
        parts = formatter.render_final_parts(
            state, elapsed_s=5.0, status="done", answer="ok"
        )
        assert parts.footer == "\N{LABEL} dir: proj"

    def test_progress_footer_combined(self) -> None:
        """Progress messages show combined 🏷 dir + model line."""
        tracker = ProgressTracker(engine="gemini")
        meta = {"model": "gemini-2.5-pro"}
        evt = StartedEvent(
            engine="gemini",
            resume=ResumeToken(engine="gemini", value="abc123"),
            meta=meta,
        )
        tracker.note_event(evt)
        state = tracker.snapshot(
            context_line="dir: my-project",
            meta_formatter=format_meta_line,
        )
        formatter = MarkdownFormatter()
        parts = formatter.render_progress_parts(state, elapsed_s=3.0)
        assert parts.footer is not None
        assert parts.footer == "\N{LABEL} dir: my-project | gemini-2.5-pro"

    def test_footer_no_info_with_resume(self) -> None:
        """Resume line without context or meta — no 🏷 line, just resume."""
        tracker = ProgressTracker(engine="codex")
        evt = StartedEvent(
            engine="codex",
            resume=ResumeToken(engine="codex", value="t-1"),
        )
        tracker.note_event(evt)
        state = tracker.snapshot(
            resume_formatter=lambda t: f"`codex resume {t.value}`",
        )
        formatter = MarkdownFormatter()
        parts = formatter.render_final_parts(
            state, elapsed_s=5.0, status="done", answer="ok"
        )
        lines = parts.footer.split(HARD_BREAK)
        assert lines[0] == ""  # blank line for visual separation
        assert lines[1] == "\u21a9\ufe0f `codex resume t-1`"

    def test_footer_dir_and_resume_no_meta(self) -> None:
        """Dir + resume but no model info."""
        tracker = ProgressTracker(engine="codex")
        evt = StartedEvent(
            engine="codex",
            resume=ResumeToken(engine="codex", value="t-1"),
        )
        tracker.note_event(evt)
        state = tracker.snapshot(
            resume_formatter=lambda t: f"`codex resume {t.value}`",
            context_line="dir: proj @main",
        )
        formatter = MarkdownFormatter()
        parts = formatter.render_final_parts(
            state, elapsed_s=5.0, status="done", answer="ok"
        )
        assert parts.footer is not None
        lines = parts.footer.split(HARD_BREAK)
        assert len(lines) == 3
        assert lines[0] == "\N{LABEL} dir: proj @main"
        assert lines[1] == ""  # blank line for visual separation
        assert lines[2] == "\u21a9\ufe0f `codex resume t-1`"


class TestCrossEngineFooter:
    """Verify the combined 🏷 footer format across all engine types."""

    def _render_footer(
        self,
        engine: str,
        *,
        meta: dict | None = None,
        context_line: str | None = None,
        resume_fmt: str | None = None,
    ) -> str | None:
        tracker = ProgressTracker(engine=engine)
        evt = StartedEvent(
            engine=engine,
            resume=ResumeToken(engine=engine, value="tok-1"),
            meta=meta,
        )
        tracker.note_event(evt)
        state = tracker.snapshot(
            resume_formatter=(lambda t: resume_fmt) if resume_fmt else None,
            context_line=context_line,
            meta_formatter=format_meta_line,
        )
        return MarkdownFormatter().render_progress_parts(state, elapsed_s=1.0).footer

    def test_claude_model_and_permission(self) -> None:
        footer = self._render_footer(
            "claude",
            meta={"model": "claude-sonnet-4-5-20250929", "permissionMode": "plan"},
            context_line="dir: untether @master",
        )
        assert (
            footer == "\N{LABEL} dir: untether @master | sonnet 4.5 \N{MIDDLE DOT} plan"
        )

    def test_gemini_model(self) -> None:
        footer = self._render_footer(
            "gemini",
            meta={"model": "gemini-2.5-pro"},
            context_line="dir: gemini-test",
        )
        assert footer == "\N{LABEL} dir: gemini-test | gemini-2.5-pro"

    def test_amp_with_mode(self) -> None:
        footer = self._render_footer(
            "amp",
            meta={"model": "deep"},
            context_line="dir: amp-test",
        )
        assert footer == "\N{LABEL} dir: amp-test | deep"

    def test_amp_with_model_fallback(self) -> None:
        footer = self._render_footer(
            "amp",
            meta={"model": "claude-sonnet-4-6"},
            context_line="dir: amp-test",
        )
        assert footer == "\N{LABEL} dir: amp-test | sonnet 4.6"

    def test_pi_model(self) -> None:
        footer = self._render_footer(
            "pi",
            meta={"model": "gpt-4o", "provider": "openai"},
            context_line="dir: pi-test",
        )
        assert footer == "\N{LABEL} dir: pi-test | gpt-4o"

    def test_codex_model(self) -> None:
        footer = self._render_footer(
            "codex",
            meta={"model": "o3"},
            context_line="dir: codex-test",
        )
        assert footer == "\N{LABEL} dir: codex-test | o3"

    def test_opencode_model(self) -> None:
        footer = self._render_footer(
            "opencode",
            meta={"model": "claude-sonnet-4-5-20250929"},
            context_line="dir: oc-test",
        )
        assert footer == "\N{LABEL} dir: oc-test | sonnet 4.5"

    def test_no_model_dir_only(self) -> None:
        footer = self._render_footer(
            "amp",
            meta=None,
            context_line="dir: amp-test",
        )
        assert footer == "\N{LABEL} dir: amp-test"

    def test_no_dir_model_only(self) -> None:
        footer = self._render_footer(
            "claude",
            meta={"model": "opus"},
            context_line=None,
        )
        assert footer == "\N{LABEL} opus"

    def test_claude_1m_model(self) -> None:
        footer = self._render_footer(
            "claude",
            meta={"model": "claude-opus-4-6[1m]", "permissionMode": "plan"},
            context_line="dir: untether @master",
        )
        assert (
            footer
            == "\N{LABEL} dir: untether @master | opus 4.6 (1M) \N{MIDDLE DOT} plan"
        )

    def test_gemini_auto_model(self) -> None:
        footer = self._render_footer(
            "gemini",
            meta={"model": "auto-gemini-3"},
            context_line="dir: gemini-test",
        )
        assert footer == "\N{LABEL} dir: gemini-test | gemini-3"

    def test_neither_dir_nor_model(self) -> None:
        footer = self._render_footer("codex", meta=None, context_line=None)
        assert footer is None
