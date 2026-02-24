from __future__ import annotations

from typing import Any

from untether.model import (
    Action,
    ActionEvent,
    ActionKind,
    EngineId,
    ResumeToken,
    StartedEvent,
    UntetherEvent,
)


def session_started(engine: str, value: str, title: str = "Codex") -> UntetherEvent:
    engine_id: EngineId = engine
    return StartedEvent(
        engine=engine_id,
        resume=ResumeToken(engine=engine_id, value=value),
        title=title,
    )


def action_started(
    action_id: str,
    kind: ActionKind,
    title: str,
    detail: dict[str, Any] | None = None,
    engine: str = "codex",
) -> UntetherEvent:
    engine_id: EngineId = engine
    return ActionEvent(
        engine=engine_id,
        action=Action(
            id=action_id,
            kind=kind,
            title=title,
            detail=detail or {},
        ),
        phase="started",
    )


def action_completed(
    action_id: str,
    kind: ActionKind,
    title: str,
    ok: bool,
    detail: dict[str, Any] | None = None,
    engine: str = "codex",
) -> UntetherEvent:
    engine_id: EngineId = engine
    return ActionEvent(
        engine=engine_id,
        action=Action(
            id=action_id,
            kind=kind,
            title=title,
            detail=detail or {},
        ),
        phase="completed",
        ok=ok,
    )
