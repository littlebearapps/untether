from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from .model import Action, ActionEvent, ResumeToken, StartedEvent, UntetherEvent


@dataclass(frozen=True, slots=True)
class ActionState:
    action: Action
    phase: str
    ok: bool | None
    display_phase: str
    completed: bool
    first_seen: int
    last_update: int
    # #481: wall-clock timestamps for elapsed-time rendering and
    # heartbeat-driven "action older than 60s" suppression checks.
    # 0.0 = unset (backward-compat for tests that build ActionState
    # without a clock). Populated by ProgressTracker.note_event when a
    # clock callable is configured.
    started_at: float = 0.0
    last_update_at: float = 0.0


@dataclass(frozen=True, slots=True)
class ProgressState:
    engine: str
    action_count: int
    actions: tuple[ActionState, ...]
    resume: ResumeToken | None
    resume_line: str | None
    context_line: str | None
    meta_line: str | None = None
    # #777: pre-result background-task block (markdown), set by the bridge.
    background: str | None = None


class ProgressTracker:
    def __init__(
        self,
        *,
        engine: str,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.engine = engine
        # #481: clock callable for ActionState wall-clock timestamps.
        # Defaults to time.monotonic so production callers don't need to
        # plumb anything new. Tests can pass a fake clock for deterministic
        # elapsed-time assertions.
        self._clock: Callable[[], float] = clock or time.monotonic
        self.resume: ResumeToken | None = None
        self.meta: dict[str, Any] | None = None
        self.action_count = 0
        self._actions: dict[str, ActionState] = {}
        self._seq = 0

    def note_event(self, event: UntetherEvent) -> bool:
        match event:
            case StartedEvent(resume=resume, meta=meta):
                self.resume = resume
                if meta:
                    # Merge rather than replace so that dispatcher-seeded
                    # keys (e.g. "trigger" from RunContext, #271) survive
                    # the engine's own StartedEvent.meta.
                    if self.meta is None:
                        self.meta = dict(meta)
                    else:
                        self.meta = {**self.meta, **meta}
                return True
            case ActionEvent(action=action, phase=phase, ok=ok):
                if action.kind == "turn":
                    return False
                action_id = str(action.id or "")
                if not action_id:
                    return False
                completed = phase == "completed"
                existing = self._actions.get(action_id)
                has_open = existing is not None and not existing.completed
                is_update = phase == "updated" or (phase == "started" and has_open)
                display_phase = "updated" if is_update and not completed else phase

                self._seq += 1
                seq = self._seq
                now = self._clock()

                if existing is None:
                    self.action_count += 1
                    first_seen = seq
                    started_at = now
                else:
                    first_seen = existing.first_seen
                    started_at = existing.started_at or now
                self._actions[action_id] = ActionState(
                    action=action,
                    phase=phase,
                    ok=ok,
                    display_phase=display_phase,
                    completed=completed,
                    first_seen=first_seen,
                    last_update=seq,
                    started_at=started_at,
                    last_update_at=now,
                )
                return True
            case _:
                return False

    def action_detail(self, action_id: str) -> dict[str, Any] | None:
        """The tracked action's current ``detail``, or None when unknown."""
        existing = self._actions.get(action_id)
        return None if existing is None else existing.action.detail

    def update_action(
        self,
        action_id: str,
        *,
        title: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> bool:
        """Replace a tracked action's ``title`` / ``detail`` in place.

        #709: some actions are advanced by a *callback handler* rather than by
        a new engine event — most notably a multi-question AskUserQuestion
        flow, where answering Q1 edits the progress message to show Q2. Without
        this the tracker keeps the intercept-time title and keyboard, and the
        next heartbeat re-render regenerates the message from that stale model
        and wins: the user is shown Q1's text and Q1's option labels while Q2
        is outstanding.

        ``detail`` REPLACES the action's detail dict wholesale (callers pass a
        merged dict) so a key can be removed as well as changed — clearing
        ``inline_keyboard`` is how the flow's final keyboard strip becomes a
        model change rather than an edit racing the renderer.

        Returns False when *action_id* is unknown, so callers can fall back to
        a plain edit rather than assume the model was updated.
        """
        existing = self._actions.get(action_id)
        if existing is None:
            return False
        action = existing.action
        self._seq += 1
        self._actions[action_id] = replace(
            existing,
            action=replace(
                action,
                title=action.title if title is None else title,
                detail=action.detail if detail is None else detail,
            ),
            last_update=self._seq,
            last_update_at=self._clock(),
        )
        return True

    def set_resume(self, resume: ResumeToken | None) -> None:
        if resume is not None:
            self.resume = resume

    def snapshot(
        self,
        *,
        resume_formatter: Callable[[ResumeToken], str] | None = None,
        context_line: str | None = None,
        meta_formatter: Callable[[dict[str, Any]], str | None] | None = None,
    ) -> ProgressState:
        resume_line: str | None = None
        if self.resume is not None and resume_formatter is not None:
            resume_line = resume_formatter(self.resume)
        meta_line: str | None = None
        if self.meta is not None and meta_formatter is not None:
            meta_line = meta_formatter(self.meta)
        actions = tuple(
            sorted(self._actions.values(), key=lambda item: item.first_seen)
        )
        return ProgressState(
            engine=self.engine,
            action_count=self.action_count,
            actions=actions,
            resume=self.resume,
            resume_line=resume_line,
            context_line=context_line,
            meta_line=meta_line,
        )
