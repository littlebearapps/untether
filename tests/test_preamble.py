"""Tests for prompt preamble injection."""

from __future__ import annotations

from unittest.mock import patch

from untether.runner_bridge import (
    _DEFAULT_PREAMBLE,
    _apply_preamble,
)
from untether.runners.claude import _prepend_exitplanmode_plan
from untether.settings import PreambleSettings


def test_default_preamble_prepended() -> None:
    """Default preamble is prepended when settings use defaults."""
    result = _apply_preamble("fix the bug")
    assert result.startswith("[Untether]")
    assert result.endswith("fix the bug")
    assert "\n\n---\n\n" in result
    assert _DEFAULT_PREAMBLE in result


def test_preamble_disabled() -> None:
    """Prompt is returned unchanged when preamble is disabled."""
    cfg = PreambleSettings(enabled=False)
    with patch("untether.runner_bridge._load_preamble_settings", return_value=cfg):
        result = _apply_preamble("fix the bug")
    assert result == "fix the bug"


def test_custom_preamble_text() -> None:
    """Custom preamble text overrides the default."""
    cfg = PreambleSettings(text="Custom context for this agent.")
    with patch("untether.runner_bridge._load_preamble_settings", return_value=cfg):
        result = _apply_preamble("fix the bug")
    assert result.startswith("Custom context for this agent.")
    assert result.endswith("fix the bug")
    assert _DEFAULT_PREAMBLE not in result


def test_preamble_empty_text() -> None:
    """Empty text string effectively disables the preamble."""
    cfg = PreambleSettings(text="")
    with patch("untether.runner_bridge._load_preamble_settings", return_value=cfg):
        result = _apply_preamble("fix the bug")
    assert result == "fix the bug"


def test_preamble_settings_defaults() -> None:
    """PreambleSettings defaults to enabled with no custom text."""
    cfg = PreambleSettings()
    assert cfg.enabled is True
    assert cfg.text is None


def test_default_preamble_includes_outbox_instructions() -> None:
    """Default preamble tells agents about the .untether-outbox/ delivery mechanism."""
    assert ".untether-outbox/" in _DEFAULT_PREAMBLE
    assert "/file get" in _DEFAULT_PREAMBLE


# ───── #508 — plan-mode preamble clauses ───────────────────────────────


def test_default_preamble_has_exitplanmode_plan_body_clause() -> None:
    """A1: ExitPlanMode plan body must be substantive bullets, never just
    a file path. Plan-mode users are on Telegram and cannot open files."""
    assert "ExitPlanMode" in _DEFAULT_PREAMBLE
    assert "3–5 bullet" in _DEFAULT_PREAMBLE
    assert "never just a file path" in _DEFAULT_PREAMBLE


def test_default_preamble_has_post_approval_substantive_clause() -> None:
    """A2: After ExitPlanMode is approved, the next assistant message
    (the final Telegram message) must repeat the substantive findings.
    The plan-body messages disappear after approval."""
    assert "After `ExitPlanMode` is approved" in _DEFAULT_PREAMBLE
    assert "post-approval text is the only thing the user retains" in _DEFAULT_PREAMBLE


def test_default_preamble_plan_document_section_inlines_findings() -> None:
    """A3: Plan/Document Created bullet asks for inline key findings, not
    just a path pointer."""
    assert "key findings inline" in _DEFAULT_PREAMBLE
    assert "do not require the user to open the file" in _DEFAULT_PREAMBLE


# ───── #508 Layer E — _prepend_exitplanmode_plan helper ────────────────


def test_prepend_exitplanmode_plan_when_final_answer_short() -> None:
    """When the post-approval final answer is brief (the load-bearing
    repro case from #508), the plan body is prepended with a header and
    separator so the user sees the substantive findings in chat."""
    plan = "- Finding 1\n- Finding 2\n- Recommend X"
    short_final = "Plan approved — research is complete. See file."

    result = _prepend_exitplanmode_plan(short_final, plan)

    assert "📋 Plan (approved):" in result
    assert plan in result
    assert short_final in result
    # Plan body comes before the brief acknowledgement (separator)
    assert result.index(plan) < result.index(short_final)


def test_prepend_exitplanmode_plan_skipped_when_already_substring() -> None:
    """When the final answer already contains the plan body verbatim
    (preamble guidance caused Claude to repeat it), do not prepend —
    avoid duplication."""
    plan = "- Finding 1\n- Finding 2"
    final = "Here is what I found:\n- Finding 1\n- Finding 2\n\nNext steps: ..."

    result = _prepend_exitplanmode_plan(final, plan)

    assert result == final
    assert "📋 Plan (approved):" not in result


def test_prepend_exitplanmode_plan_skipped_when_no_plan_body() -> None:
    """No plan body captured → return the final answer unchanged."""
    final = "ok"
    assert _prepend_exitplanmode_plan(final, None) == final
    assert _prepend_exitplanmode_plan(final, "") == final
    assert _prepend_exitplanmode_plan(final, "   \n\t") == final


def test_prepend_exitplanmode_plan_handles_empty_final_answer() -> None:
    """If the post-approval result yields an entirely empty final answer
    (no fallback text either), the plan body becomes the full answer."""
    plan = "- Finding 1"
    result = _prepend_exitplanmode_plan("", plan)
    assert result.startswith("📋 Plan (approved):")
    assert plan in result


def test_prepend_exitplanmode_plan_handles_none_final_answer() -> None:
    """None ``final_answer`` is handled the same as empty string."""
    plan = "- Finding 1"
    result = _prepend_exitplanmode_plan(None, plan)
    assert "📋 Plan (approved):" in result
    assert plan in result
