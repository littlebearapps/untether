"""Tests for the RunContext dataclass (src/untether/context.py)."""

from __future__ import annotations

from untether.context import RunContext


def test_run_context_defaults_are_all_none():
    ctx = RunContext()
    assert ctx.project is None
    assert ctx.branch is None
    assert ctx.trigger_source is None
    assert ctx.permission_mode is None


def test_run_context_project_branch_carry():
    ctx = RunContext(project="scout", branch="feature/x")
    assert ctx.project == "scout"
    assert ctx.branch == "feature/x"


def test_run_context_trigger_source_carries():
    ctx = RunContext(trigger_source="cron:daily-review")
    assert ctx.trigger_source == "cron:daily-review"


# #330
def test_run_context_permission_mode_carries():
    ctx = RunContext(trigger_source="cron:x", permission_mode="auto")
    assert ctx.permission_mode == "auto"
    assert ctx.trigger_source == "cron:x"


def test_run_context_is_frozen():
    """RunContext is a frozen dataclass — mutation must raise."""
    import dataclasses

    ctx = RunContext(project="p")
    try:
        ctx.project = "other"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("expected FrozenInstanceError on attribute assignment")
