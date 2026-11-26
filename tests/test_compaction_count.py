"""#1182: the session compaction count — the bridge's stream wrapper.

``_with_compaction_counts`` wraps every runner stream at the bridge's single
``runner.run`` call site. It passes every event through unchanged, bumps the
session ledger on a completed ``ActionEvent`` flagged
``detail["compaction"] is True`` (once per action id per run), and adds a
``telemetry`` event ``{"compactions": n}`` after each flagged completion and
after each ``StartedEvent`` / ``TurnEvent(started)`` when ``n > 0``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from untether import runner_bridge as rb
from untether.events import EventFactory
from untether.model import (
    ActionEvent,
    CompletedEvent,
    ResumeToken,
    StartedEvent,
    TurnEvent,
    UntetherEvent,
)
from untether.progress import ProgressTracker
from untether.session_costs import (
    SessionCostLedger,
    get_session_cost_ledger,
    set_session_cost_ledger,
)

pytestmark = pytest.mark.anyio

ENGINE = "claude"
SID = "sess-1182"


def _factory(sid: str = SID) -> EventFactory:
    factory = EventFactory(engine=ENGINE)
    factory.started(ResumeToken(engine=ENGINE, value=sid), title="claude")
    return factory


def _boundary(action_id: str = "claude.compaction.1") -> ActionEvent:
    return EventFactory(engine=ENGINE).action_completed(
        action_id=action_id,
        kind="note",
        title="🗜️ Context compacted",
        ok=True,
        detail={"trigger": "manual", "compaction": True},
    )


def _run(sid: str = SID, *middle: UntetherEvent) -> list[UntetherEvent]:
    factory = _factory(sid)
    token = ResumeToken(engine=ENGINE, value=sid)
    return [
        factory.started(token, title="claude"),
        *middle,
        factory.completed_ok(answer="ok", resume=token),
    ]


async def _stream(events: Iterable[UntetherEvent]) -> AsyncIterator[UntetherEvent]:
    for evt in events:
        yield evt


async def _wrapped(
    events: Iterable[UntetherEvent], resume: ResumeToken | None = None
) -> list[UntetherEvent]:
    return [
        evt
        async for evt in rb._with_compaction_counts(
            _stream(events), engine=ENGINE, resume=resume
        )
    ]


def _counts(events: list[UntetherEvent]) -> list[int]:
    return [
        e.action.detail["compactions"]
        for e in events
        if isinstance(e, ActionEvent)
        and e.action.kind == "telemetry"
        and "compactions" in e.action.detail
    ]


async def test_flagged_completion_bumps_ledger_and_yields_telemetry() -> None:
    boundary = _boundary()
    with capture_logs() as logs:
        out = await _wrapped(_run(SID, boundary))
    assert get_session_cost_ledger().compactions(ENGINE, SID) == 1
    idx = out.index(boundary)
    tel = out[idx + 1]
    assert isinstance(tel, ActionEvent)
    assert tel.action.kind == "telemetry"
    assert tel.action.detail == {"compactions": 1}
    [log] = [e for e in logs if e["event"] == "session.compaction_counted"]
    assert log["engine"] == ENGINE
    assert log["session_id"] == SID
    assert log["count"] == 1
    assert log["action_id"] == "claude.compaction.1"
    assert log["log_level"] == "info"


async def test_two_runs_same_session_count_1_then_2() -> None:
    first = await _wrapped(_run(SID, _boundary()))
    assert _counts(first) == [1]
    resume = ResumeToken(engine=ENGINE, value=SID)
    second = await _wrapped(_run(SID, _boundary()), resume=resume)
    # seeded with 1 at the StartedEvent, then 2 after the boundary
    assert _counts(second) == [1, 2]
    assert get_session_cost_ledger().compactions(ENGINE, SID) == 2
    tracker = ProgressTracker(engine=ENGINE)
    for evt in second:
        tracker.note_event(evt)
    assert tracker.compactions == 2


async def test_new_session_starts_at_zero() -> None:
    await _wrapped(_run(SID, _boundary()))
    out = await _wrapped(_run("sess-new"))
    assert _counts(out) == []
    assert get_session_cost_ledger().compactions(ENGINE, "sess-new") == 0


async def test_unflagged_or_started_events_ignored() -> None:
    factory = EventFactory(engine=ENGINE)
    events = [
        # the status ``success`` row: same kind, no flag
        factory.action_completed(
            action_id="claude.compaction.1", kind="note", title="🗜️", ok=True
        ),
        # a flag on a non-completed phase never counts
        factory.action_started(
            action_id="x", kind="note", title="x", detail={"compaction": True}
        ),
        # only ``True`` counts
        factory.action_completed(
            action_id="y", kind="note", title="y", ok=True, detail={"compaction": 1}
        ),
    ]
    out = await _wrapped(_run(SID, *events))
    assert _counts(out) == []
    assert get_session_cost_ledger().compactions(ENGINE, SID) == 0


async def test_duplicate_action_id_counted_once() -> None:
    out = await _wrapped(_run(SID, _boundary("c1"), _boundary("c1"), _boundary("c2")))
    assert _counts(out) == [1, 2]
    assert get_session_cost_ledger().compactions(ENGINE, SID) == 2


async def test_started_event_seeds_count() -> None:
    get_session_cost_ledger().bump_compactions(ENGINE, SID)
    get_session_cost_ledger().bump_compactions(ENGINE, SID)
    out = await _wrapped(_run(SID))
    assert isinstance(out[0], StartedEvent)
    assert _counts(out[1:2]) == [2]
    assert isinstance(out[-1], CompletedEvent)


async def test_turn_started_reseeds_count() -> None:
    """A live follow-up turn renders from a fresh tracker: re-send the count."""
    get_session_cost_ledger().bump_compactions(ENGINE, SID)
    factory = _factory()
    turn_open = factory.turn_started(turn=1)
    turn_done = factory.turn_completed(turn=1, ok=True, answer="ok")
    out = await _wrapped(_run(SID) + [turn_open, _boundary("t1"), turn_done])
    idx = out.index(turn_open)
    assert _counts(out[idx + 1 : idx + 2]) == [1]
    assert _counts(out[idx:]) == [1, 2]
    assert isinstance(out[-1], TurnEvent)


async def test_restart_keeps_count(tmp_path: Path) -> None:
    path = tmp_path / "session_costs.json"
    set_session_cost_ledger(SessionCostLedger.load(path))
    await _wrapped(_run(SID, _boundary()))
    set_session_cost_ledger(SessionCostLedger.load(path))  # "restart"
    out = await _wrapped(_run(SID), resume=ResumeToken(engine=ENGINE, value=SID))
    assert _counts(out) == [1]


async def test_resume_token_used_before_started_event() -> None:
    out = await _wrapped(
        [_boundary()], resume=ResumeToken(engine=ENGINE, value="from-resume")
    )
    assert _counts(out) == [1]
    assert get_session_cost_ledger().compactions(ENGINE, "from-resume") == 1


async def test_no_session_id_counts_nothing() -> None:
    out = await _wrapped([_boundary()])
    assert out == [_boundary()]


async def test_ledger_error_never_breaks_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> int:
        raise OSError("disk full")

    ledger = get_session_cost_ledger()
    monkeypatch.setattr(ledger, "bump_compactions", boom)
    monkeypatch.setattr(ledger, "compactions", boom)
    events = _run(SID, _boundary())
    with capture_logs() as logs:
        out = await _wrapped(events)
    assert out == events
    assert any(e["event"] == "session.compaction_count_failed" for e in logs)


async def test_events_pass_through_unchanged() -> None:
    events = _run(SID, _boundary())
    out = await _wrapped(events)
    assert [e for e in out if e in events] == events
    # every added event is a count telemetry, never a step
    extra = [e for e in out if e not in events]
    assert extra and all(
        isinstance(e, ActionEvent) and e.action.kind == "telemetry" for e in extra
    )


async def test_closing_wrapper_closes_runner_stream() -> None:
    closed: list[bool] = []

    async def gen() -> AsyncIterator[UntetherEvent]:
        try:
            for evt in _run(SID):
                yield evt
        finally:
            closed.append(True)

    wrapped = rb._with_compaction_counts(gen(), engine=ENGINE, resume=None)
    await wrapped.__anext__()
    await wrapped.aclose()
    assert closed == [True]
