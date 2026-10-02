"""#330 / #743: tests for trigger-level overrides applied in run_job.

The override logic lives in `telegram/loop._apply_trigger_overrides` (the
single applier for every trigger-level option) so it's testable in isolation
without booting the full bridge / runner stack.
"""

from __future__ import annotations

from untether.context import RunContext
from untether.runners.run_options import EngineRunOptions
from untether.telegram.loop import _apply_trigger_overrides


def test_no_context_returns_run_options_unchanged():
    ro = EngineRunOptions(permission_mode="plan", model="opus")
    assert _apply_trigger_overrides(ro, None, engine="claude") is ro


def test_context_without_permission_mode_returns_run_options_unchanged():
    ro = EngineRunOptions(permission_mode="plan")
    ctx = RunContext(trigger_source="at:x")  # no permission_mode, attended
    assert _apply_trigger_overrides(ro, ctx, engine="claude") is ro


def test_835_cron_without_permission_mode_only_marks_unattended():
    """#835: a cron with no permission_mode keeps the chat's mode but is
    marked unattended (the runner denies anything that would wait)."""
    ro = EngineRunOptions(permission_mode="plan", model="opus")
    ctx = RunContext(trigger_source="cron:x")
    out = _apply_trigger_overrides(ro, ctx, engine="claude")
    assert out is not None
    assert out.permission_mode == "plan"
    assert out.model == "opus"
    assert out.unattended_trigger == "cron:x"


def test_override_beats_chat_pref():
    """Cron-level 'auto' wins over chat-level 'plan'. Other fields preserved."""
    ro = EngineRunOptions(
        permission_mode="plan",
        model="opus",
        ask_questions=False,
    )
    ctx = RunContext(trigger_source="cron:x", permission_mode="auto")
    out = _apply_trigger_overrides(ro, ctx, engine="claude")
    assert out is not None
    assert out.permission_mode == "auto"
    assert out.model == "opus"  # preserved
    assert out.ask_questions is False  # preserved


def test_override_builds_run_options_when_none():
    """Chat has no overrides → run_options starts as None → still honours trigger override."""
    ctx = RunContext(trigger_source="cron:y", permission_mode="auto")
    out = _apply_trigger_overrides(None, ctx, engine="claude")
    assert out is not None
    assert out.permission_mode == "auto"
    assert out.model is None  # other fields stay at defaults


def test_override_is_idempotent_when_matches_current():
    """If trigger matches resolved value, result is equivalent (no log)."""
    ro = EngineRunOptions(permission_mode="auto", model="opus")
    ctx = RunContext(trigger_source="cron:z", permission_mode="auto")
    out = _apply_trigger_overrides(ro, ctx, engine="claude")
    assert out is not None
    assert out.permission_mode == "auto"
    assert out.model == "opus"


def test_743_log_false_never_logs_and_field_names_unchanged():
    """The comparison sites pass log=False; run_job logs with the #330
    event and field names byte-for-byte (docs and #751 monitors grep them)."""
    from structlog.testing import capture_logs

    ro = EngineRunOptions(permission_mode="plan")
    ctx = RunContext(trigger_source="cron:q", permission_mode="auto")
    with capture_logs() as quiet:
        out = _apply_trigger_overrides(ro, ctx, engine="claude", log=False)
    assert out is not None and out.permission_mode == "auto"
    assert quiet == []

    with capture_logs() as logs:
        _apply_trigger_overrides(ro, ctx, engine="claude")
    assert len(logs) == 1
    rec = logs[0]
    assert rec["event"] == "trigger.cron.permission_mode_override"
    assert rec["trigger_source"] == "cron:q"
    assert rec["chat_permission_mode"] == "plan"
    assert rec["trigger_permission_mode"] == "auto"
    assert rec["engine"] == "claude"


def test_743_no_log_when_value_unchanged():
    from structlog.testing import capture_logs

    ro = EngineRunOptions(permission_mode="auto")
    ctx = RunContext(trigger_source="cron:q", permission_mode="auto")
    with capture_logs() as logs:
        _apply_trigger_overrides(ro, ctx, engine="claude")
    assert logs == []
