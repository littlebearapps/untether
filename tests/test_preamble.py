"""Tests for prompt preamble injection."""

from __future__ import annotations

from unittest.mock import patch

from untether.runner_bridge import (
    _DEFAULT_PREAMBLE,
    _apply_preamble,
)
from untether.runners.claude import (
    _PREPEND_BODY_CAP,
    _PREPEND_LENGTH_GATE,
    _prepend_exitplanmode_plan,
)
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


def test_default_preamble_warns_against_systemctl_restart() -> None:
    """#547 axis 1: agents routinely follow ``edit untether.toml`` with
    ``systemctl --user restart untether`` because their training data is
    full of "restart the service after config changes". Untether already
    hot-reloads the file; the restart drops the agent's own final answer
    (drain timeout + outbox.fail_pending). The preamble must tell agents
    explicitly NOT to restart after editing config."""
    # Headline: hot-reload mentioned
    assert "hot-reload" in _DEFAULT_PREAMBLE.lower()
    # Explicit "do NOT" framing
    assert "Do NOT" in _DEFAULT_PREAMBLE
    assert "systemctl" in _DEFAULT_PREAMBLE
    assert "restart untether" in _DEFAULT_PREAMBLE
    # Consequence spelled out so agents understand why
    assert "drop" in _DEFAULT_PREAMBLE.lower() or "lost" in _DEFAULT_PREAMBLE.lower()
    # Restart-only keys mentioned so agents know the exception
    assert "bot_token" in _DEFAULT_PREAMBLE
    assert "chat_id" in _DEFAULT_PREAMBLE


# ───── #508 / #515 — plan-mode preamble clauses ────────────────────────


def test_default_preamble_has_exitplanmode_plan_body_clause() -> None:
    """A1 (#515 tuning): ExitPlanMode plan body must be a concise 3-5
    bullet summary - never just a file path, but also not an expanded
    substantive summary (rc11 over-fire). The plan is shown for
    approval, not as the final deliverable."""
    assert "ExitPlanMode" in _DEFAULT_PREAMBLE
    assert "concise 3–5 bullet" in _DEFAULT_PREAMBLE
    assert "never just a file path" in _DEFAULT_PREAMBLE
    assert "shown to the user for approval, not as the final deliverable" in (
        _DEFAULT_PREAMBLE
    )


def test_default_preamble_has_post_approval_brief_summary_clause() -> None:
    """A2 (#515 tuning): After ExitPlanMode is approved, the final
    Telegram message should be a brief CLI-style summary (3-7 bullets
    or 1-2 short paragraphs, ~500-1500 chars). Do NOT re-paste the full
    plan content - rc11 told Claude to "repeat substantive findings"
    which produced 30k-char finals."""
    assert "After `ExitPlanMode` is approved" in _DEFAULT_PREAMBLE
    assert "brief CLI-style summary" in _DEFAULT_PREAMBLE
    assert "3–7 bullets" in _DEFAULT_PREAMBLE
    assert "Do NOT re-paste the full plan content" in _DEFAULT_PREAMBLE
    assert "~500–1500 characters" in _DEFAULT_PREAMBLE


def test_default_preamble_summary_block_asks_for_headline_summary() -> None:
    """A3 (#515 tuning): the ## Summary block's Plan/Document Created
    bullet asks for a pointer + 3-5 bullet headline summary, not a
    re-paste of the full plan content. The user already saw the plan
    during approval."""
    assert "3–5 bullet headline summary" in _DEFAULT_PREAMBLE
    assert "not a re-paste of the full content" in _DEFAULT_PREAMBLE


def test_default_preamble_does_not_drive_verbose_post_approval_text() -> None:
    """Regression for #515: ensure the rc11 verbosity-driving phrases
    that produced 42k-char Telegram finals are no longer present."""
    # rc11 A2 phrase that told Claude to repeat the full content
    assert "MUST repeat the substantive findings or decisions" not in (
        _DEFAULT_PREAMBLE
    )
    # rc11 A1 phrase that told Claude to expand bullets into a
    # substantive summary for research/audit tasks
    assert "expand the bullets into a substantive summary" not in _DEFAULT_PREAMBLE
    # rc11 A3 phrase that told Claude to put full findings inline
    assert "do not require the user to open the file" not in _DEFAULT_PREAMBLE


# ───── #508 / #515 Layer E — _prepend_exitplanmode_plan helper ─────────


def test_prepend_exitplanmode_plan_when_final_answer_short() -> None:
    """The original #508 repro: post-approval result is brief (584
    chars in the live capture). Plan body must be prepended so the user
    sees the substantive findings in chat."""
    plan = "- Finding 1\n- Finding 2\n- Recommend X"
    short_final = "Plan approved — research is complete. See file."
    assert len(short_final) < _PREPEND_LENGTH_GATE

    result = _prepend_exitplanmode_plan(short_final, plan)

    assert "📋 Plan (approved):" in result
    assert plan in result
    assert short_final in result
    # Plan body comes before the brief acknowledgement (separator)
    assert result.index(plan) < result.index(short_final)


def test_prepend_exitplanmode_plan_skipped_when_answer_substantive() -> None:
    """#515: when the post-approval text is ≥ ``_PREPEND_LENGTH_GATE``
    chars (Claude wrote a real CLI-style summary), do NOT prepend the
    plan body — the post-approval text is doing the job. This is the
    load-bearing change vs rc11/rc12 where the substring check failed
    on paraphrased summaries and double-shipped content."""
    plan = "- Finding 1\n- Finding 2\n- Recommend X"
    substantive_final = (
        "I investigated the issue and here is what I found:\n\n"
        "- Headline 1: module X had a regression introduced in commit abc123\n"
        "- Headline 2: the root cause was a missing null guard in the parser\n"
        "- Headline 3: rolled back commit abc123 and added a regression test\n"
        "- Headline 4: next step is to backfill the affected rows Monday\n\n"
        "Decisions made: kept the legacy code path for one more release cycle to\n"
        "give downstream consumers time to migrate; full removal scheduled for the\n"
        "next minor version once telemetry confirms zero active callers.\n\n"
        "Next steps: open a follow-up issue for the backfill, send a heads-up in\n"
        "the team channel, and re-run the daily-audit cron tomorrow morning to\n"
        "confirm the regression is gone from the verification window.\n"
    )
    assert len(substantive_final) >= _PREPEND_LENGTH_GATE

    result = _prepend_exitplanmode_plan(substantive_final, plan)

    assert result == substantive_final
    assert "📋 Plan (approved):" not in result


def test_prepend_exitplanmode_plan_caps_long_plan_body() -> None:
    """#515: when Layer E does fire and the captured plan body is
    longer than ``_PREPEND_BODY_CAP``, truncate it to avoid runaway
    finals. Live staging captures had 5,000-char plan bodies that got
    prepended in full."""
    plan = "x" * (_PREPEND_BODY_CAP + 1000)
    short_final = "ok"

    result = _prepend_exitplanmode_plan(short_final, plan)

    assert "📋 Plan (approved):" in result
    assert "plan truncated" in result
    # Plan body in the result should be ~_PREPEND_BODY_CAP chars (plus
    # the truncation suffix), not the full 2500-char original.
    assert "x" * (_PREPEND_BODY_CAP + 100) not in result


def test_prepend_exitplanmode_plan_skipped_when_already_substring() -> None:
    """Secondary skip rule: when the plan body is a literal substring
    of the final answer (rare with rc13 wording, but a cheap belt-and-
    braces check), do not prepend."""
    plan = "- Finding 1\n- Finding 2"
    final = "Here is what I found:\n- Finding 1\n- Finding 2\n\nNext steps: ..."
    assert len(final) < _PREPEND_LENGTH_GATE

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
