"""Tests for the RunContext dataclass (src/untether/context.py)."""

from __future__ import annotations

from dataclasses import fields, replace

import pytest

from untether.context import RunContext, attended_context


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


# #1120: attended_context is generic, so no trigger-only field (including one
# added to RunContext later) can leak into a human turn.
def test_1120_run_id_carries():
    assert RunContext(trigger_source="cron:x", run_id="abc").run_id == "abc"


@pytest.mark.parametrize(
    "name",
    [f.name for f in fields(RunContext) if f.name not in ("project", "branch")],
)
def test_1120_attended_context_strips_every_trigger_field(name: str):
    ctx = replace(RunContext(project="p", branch="b"), **{name: "x"})
    assert attended_context(ctx) == RunContext(project="p", branch="b")


def test_1120_attended_context_keeps_plain_context():
    plain = RunContext(project="p", branch="b")
    assert attended_context(plain) is plain
    assert attended_context(None) is None
