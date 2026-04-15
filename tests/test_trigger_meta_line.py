"""Tests for trigger source rendering in the meta footer (#271)."""

from __future__ import annotations

from untether.markdown import format_meta_line


class TestTriggerInFooter:
    def test_trigger_only(self):
        out = format_meta_line({"trigger": "\u23f0 cron:daily-review"})
        assert out == "\u23f0 cron:daily-review"

    def test_trigger_with_model(self):
        out = format_meta_line(
            {"trigger": "\u23f0 cron:daily-review", "model": "claude-opus-4-6"}
        )
        assert out is not None
        assert "\u23f0 cron:daily-review" in out
        assert "opus" in out.lower()
        # Model must come before trigger in the part order.
        parts = out.split(" \u00b7 ")
        assert parts.index("\u23f0 cron:daily-review") == len(parts) - 1

    def test_trigger_webhook(self):
        out = format_meta_line({"trigger": "\u26a1 webhook:github-push"})
        assert out == "\u26a1 webhook:github-push"

    def test_no_trigger_ignored(self):
        out = format_meta_line({"model": "claude-opus-4-6"})
        assert out is not None
        assert "cron" not in out
        assert "webhook" not in out

    def test_empty_trigger_ignored(self):
        out = format_meta_line({"trigger": "", "model": "claude-opus-4-6"})
        assert out is not None
        assert "opus" in out.lower()

    def test_non_string_trigger_ignored(self):
        out = format_meta_line({"trigger": 42, "model": "claude-opus-4-6"})
        assert out is not None
        assert "42" not in out
