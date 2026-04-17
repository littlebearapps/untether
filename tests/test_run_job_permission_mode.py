"""#330: tests for trigger-level `permission_mode` override in run_job.

The override logic lives in `telegram/loop._apply_trigger_permission_override`
so it's testable in isolation without booting the full bridge / runner stack.
"""

from __future__ import annotations

from untether.context import RunContext
from untether.runners.run_options import EngineRunOptions
from untether.telegram.loop import _apply_trigger_permission_override


def test_no_context_returns_run_options_unchanged():
    ro = EngineRunOptions(permission_mode="plan", model="opus")
    assert _apply_trigger_permission_override(ro, None, engine="claude") is ro


def test_context_without_permission_mode_returns_run_options_unchanged():
    ro = EngineRunOptions(permission_mode="plan")
    ctx = RunContext(trigger_source="cron:x")  # no permission_mode
    assert _apply_trigger_permission_override(ro, ctx, engine="claude") is ro


def test_override_beats_chat_pref():
    """Cron-level 'auto' wins over chat-level 'plan'. Other fields preserved."""
    ro = EngineRunOptions(
        permission_mode="plan",
        model="opus",
        ask_questions=False,
    )
    ctx = RunContext(trigger_source="cron:x", permission_mode="auto")
    out = _apply_trigger_permission_override(ro, ctx, engine="claude")
    assert out is not None
    assert out.permission_mode == "auto"
    assert out.model == "opus"  # preserved
    assert out.ask_questions is False  # preserved


def test_override_builds_run_options_when_none():
    """Chat has no overrides → run_options starts as None → still honours trigger override."""
    ctx = RunContext(trigger_source="cron:y", permission_mode="auto")
    out = _apply_trigger_permission_override(None, ctx, engine="claude")
    assert out is not None
    assert out.permission_mode == "auto"
    assert out.model is None  # other fields stay at defaults


def test_override_is_idempotent_when_matches_current():
    """If trigger matches resolved value, result is equivalent (logger.info still fires — harmless)."""
    ro = EngineRunOptions(permission_mode="auto", model="opus")
    ctx = RunContext(trigger_source="cron:z", permission_mode="auto")
    out = _apply_trigger_permission_override(ro, ctx, engine="claude")
    assert out is not None
    assert out.permission_mode == "auto"
    assert out.model == "opus"
