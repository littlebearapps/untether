"""Event factory helpers for runner implementations."""

from __future__ import annotations

from typing import Any

from .logging import get_logger
from .model import (
    Action,
    ActionEvent,
    ActionKind,
    ActionLevel,
    ActionPhase,
    CompletedEvent,
    EngineId,
    ResumeToken,
    StartedEvent,
    TurnEvent,
    TurnReason,
)

logger = get_logger(__name__)


class EventFactory:
    __slots__ = ("_resume", "engine")

    def __init__(self, engine: EngineId) -> None:
        self.engine = engine
        self._resume: ResumeToken | None = None

    @property
    def resume(self) -> ResumeToken | None:
        return self._resume

    def started(
        self,
        token: ResumeToken,
        *,
        title: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> StartedEvent:
        if token.engine != self.engine:
            logger.debug(
                "event.factory.resume_mismatch",
                expected_engine=str(self.engine),
                token_engine=str(token.engine),
            )
            raise RuntimeError(f"resume token is for engine {token.engine!r}")
        if self._resume is not None and self._resume != token:
            logger.debug(
                "event.factory.resume_mismatch",
                existing=self._resume.value[:8],
                incoming=token.value[:8],
            )
            raise RuntimeError(
                f"resume token mismatch: {self._resume.value} vs {token.value}"
            )
        self._resume = token
        return StartedEvent(engine=self.engine, resume=token, title=title, meta=meta)

    def action(
        self,
        *,
        phase: ActionPhase,
        action_id: str,
        kind: ActionKind,
        title: str,
        detail: dict[str, Any] | None = None,
        ok: bool | None = None,
        message: str | None = None,
        level: ActionLevel | None = None,
    ) -> ActionEvent:
        action = Action(
            id=action_id,
            kind=kind,
            title=title,
            detail=detail or {},
        )
        return ActionEvent(
            engine=self.engine,
            action=action,
            phase=phase,
            ok=ok,
            message=message,
            level=level,
        )

    def action_started(
        self,
        *,
        action_id: str,
        kind: ActionKind,
        title: str,
        detail: dict[str, Any] | None = None,
    ) -> ActionEvent:
        return self.action(
            phase="started",
            action_id=action_id,
            kind=kind,
            title=title,
            detail=detail,
        )

    def action_updated(
        self,
        *,
        action_id: str,
        kind: ActionKind,
        title: str,
        detail: dict[str, Any] | None = None,
    ) -> ActionEvent:
        return self.action(
            phase="updated",
            action_id=action_id,
            kind=kind,
            title=title,
            detail=detail,
        )

    def action_completed(
        self,
        *,
        action_id: str,
        kind: ActionKind,
        title: str,
        ok: bool,
        detail: dict[str, Any] | None = None,
        message: str | None = None,
        level: ActionLevel | None = None,
    ) -> ActionEvent:
        return self.action(
            phase="completed",
            action_id=action_id,
            kind=kind,
            title=title,
            detail=detail,
            ok=ok,
            message=message,
            level=level,
        )

    def completed(
        self,
        *,
        ok: bool,
        answer: str,
        resume: ResumeToken | None = None,
        error: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> CompletedEvent:
        resolved_resume = resume if resume is not None else self._resume
        return CompletedEvent(
            engine=self.engine,
            ok=ok,
            answer=answer,
            resume=resolved_resume,
            error=error,
            usage=usage,
        )

    def completed_ok(
        self,
        *,
        answer: str,
        resume: ResumeToken | None = None,
        usage: dict[str, Any] | None = None,
    ) -> CompletedEvent:
        return self.completed(ok=True, answer=answer, resume=resume, usage=usage)

    def completed_error(
        self,
        *,
        error: str,
        answer: str = "",
        resume: ResumeToken | None = None,
        usage: dict[str, Any] | None = None,
    ) -> CompletedEvent:
        return self.completed(
            ok=False,
            answer=answer,
            resume=resume,
            error=error,
            usage=usage,
        )

    def turn_started(
        self,
        *,
        turn: int,
        reason: TurnReason = "unknown",
        command_uuid: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> TurnEvent:
        return TurnEvent(
            engine=self.engine,
            phase="started",
            turn=turn,
            reason=reason,
            resume=self._resume,
            command_uuid=command_uuid,
            detail=detail or {},
        )

    def turn_completed(
        self,
        *,
        turn: int,
        ok: bool,
        answer: str,
        reason: TurnReason = "unknown",
        error: str | None = None,
        usage: dict[str, Any] | None = None,
        command_uuid: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> TurnEvent:
        return TurnEvent(
            engine=self.engine,
            phase="completed",
            turn=turn,
            reason=reason,
            resume=self._resume,
            ok=ok,
            answer=answer,
            error=error,
            usage=usage,
            command_uuid=command_uuid,
            detail=detail or {},
        )
