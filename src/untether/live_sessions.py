"""Engine-neutral live-session seam (#977).

A *live session* is an engine process kept running after its reply so later
turns (follow-ups, background wake-ups, steers) go into the same process
instead of a fresh ``--resume`` (#776). The bridge, the scheduler hook and the
Telegram loop ask this module for the engine's :class:`LiveSessionBackend`
rather than testing ``engine == "claude"`` and importing the Claude runner's
registry functions themselves.

Claude is the only backend today. Its adapter resolves every function on
``untether.runners.claude`` **at call time** and forwards the caller's
arguments untouched, so behaviour, call shapes and monkeypatches are exactly
what the direct imports gave. This module imports no runner at import time
(``runners/claude.py`` imports ``runner.py``, which imports this).

Log event names stay with the caller: sites build them from
:attr:`LiveSessionBackend.log_ns` (``f"{b.log_ns}.live_session.…"``) and log
through their own module logger.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

__all__ = [
    "LiveSessionBackend",
    "LiveSessionView",
    "idle_live_session_counts",
    "live_backend",
    "supports_live_session",
]


class LiveSessionView(Protocol):
    """What callers may read off a live session without knowing the engine."""

    closing: bool


class LiveSessionBackend(Protocol):
    """One engine's live-session registry, as the engine-neutral code sees it."""

    engine: str
    """Engine id — the key of :func:`live_backend`."""
    display_name: str
    """Name used in user-visible text (``"Claude"``)."""
    log_ns: str
    """Prefix of the engine's ``<ns>.live_session.*`` log events."""
    supports_steer: bool
    """Whether a message can be written into a running turn (#775)."""

    def enabled(self) -> bool:
        """Engine-level kill switch, read on every call."""
        ...

    def get(self, session_id: str) -> LiveSessionView | None: ...

    def is_accepting(self, session_id: str) -> bool:
        """A live process owns ``session_id`` and a follow-up can be written."""
        ...

    def idle_count(self) -> int:
        """Sessions idle between turns that still hold a process (#838)."""
        ...

    def linger_info(self, session_id: str) -> tuple[bool, int] | None:
        """``(post_result, live_bg_count)`` of the owning process, or None."""
        ...

    def live_bg_count(self, session_id: str) -> int:
        """Live background handles held by the owning process (#647)."""
        ...

    def spawn_options(self, session_id: str) -> Any:
        """The run options the live process was spawned with (None: no session)."""
        ...

    def spawn_cwd(self, session_id: str) -> Path | None:
        """The directory the live process was spawned in (#996)."""
        ...

    def pre_inject_close_reason(self, session_id: str) -> str | None:
        """A reason the session must be closed instead of taking a follow-up."""
        ...

    async def inject_when_idle(
        self, session_id: str, text: str, *, command_uuid: str
    ) -> bool: ...

    async def close(
        self,
        session_id: str,
        reason: str,
        *,
        notice: bool = False,
        only_if_idle: bool = False,
    ) -> bool: ...

    def add_listener(
        self, session_id: str, callback: Callable[[str, dict[str, Any]], Any]
    ) -> bool: ...

    async def close_steer_window(self, session_id: str, reason: str) -> bool: ...

    async def steer(self, session_id: str, text: str, **kwargs: Any) -> Any:
        """Write ``text`` into the session now; returns the engine's outcome."""
        ...

    async def wait_for_handoff(self, session_id: str, timeout_s: float) -> Any:
        """Wait for the process owning ``session_id`` to exit (#633)."""
        ...


class _ClaudeLive:
    """Claude's backend: a thin, lazy view of ``untether.runners.claude``."""

    engine = "claude"
    display_name = "Claude"
    log_ns = "claude"
    supports_steer = True

    @staticmethod
    def _claude() -> ModuleType:
        # Looked up per call, never cached: tests replace the module's
        # functions and registries, and the module imports ``runner.py``.
        return importlib.import_module("untether.runners.claude")

    def enabled(self) -> bool:
        # `[watchdog] live_sessions` is read per spawn in the runner; a
        # session that was not kept live is simply absent from the registry.
        return True

    def get(self, session_id: str) -> Any:
        return self._claude().get_live_session(session_id)

    def is_accepting(self, session_id: str) -> bool:
        return self._claude().is_session_accepting(session_id)

    def idle_count(self) -> int:
        return self._claude().idle_live_session_count()

    def linger_info(self, session_id: str) -> tuple[bool, int] | None:
        return self._claude().session_linger_info(session_id)

    def live_bg_count(self, session_id: str) -> int:
        return self._claude().session_live_bg_count(session_id)

    def spawn_options(self, session_id: str) -> Any:
        live = self.get(session_id)
        return None if live is None else live.state.spawn_run_options

    def spawn_cwd(self, session_id: str) -> Path | None:
        live = self.get(session_id)
        return None if live is None else live.state.spawn_cwd

    def pre_inject_close_reason(self, session_id: str) -> str | None:
        # #383: the CLI refused to go back into plan mode.
        live = self.get(session_id)
        if live is not None and live.state.plan_rearm_failed:
            return "plan_rearm_failed"
        return None

    async def inject_when_idle(
        self, session_id: str, text: str, *, command_uuid: str
    ) -> bool:
        return await self._claude().inject_when_idle(
            session_id, text, command_uuid=command_uuid
        )

    async def close(self, session_id: str, reason: str, **kwargs: Any) -> bool:
        # ``notice`` / ``only_if_idle`` pass through exactly as given.
        return await self._claude().close_live_session(session_id, reason, **kwargs)

    def add_listener(
        self, session_id: str, callback: Callable[[str, dict[str, Any]], Any]
    ) -> bool:
        return self._claude().add_live_session_listener(session_id, callback)

    async def close_steer_window(self, session_id: str, reason: str) -> bool:
        return await self._claude().close_steer_window(session_id, reason)

    async def steer(self, session_id: str, text: str, **kwargs: Any) -> Any:
        return await self._claude().steer_into_session(session_id, text, **kwargs)

    async def wait_for_handoff(self, session_id: str, timeout_s: float) -> Any:
        return await self._claude().wait_for_session_handoff(session_id, timeout_s)


# Static on purpose: no entry points until a second engine needs one.
_BACKENDS: dict[str, LiveSessionBackend] = {"claude": _ClaudeLive()}


def live_backend(engine: str | None) -> LiveSessionBackend | None:
    """The live-session backend for ``engine``, or None when it has none."""
    if not engine:
        return None
    return _BACKENDS.get(engine)


def supports_live_session(engine: str | None) -> bool:
    """True when ``engine`` has a backend and its kill switch is on."""
    backend = live_backend(engine)
    return backend is not None and backend.enabled()


def idle_live_session_counts() -> dict[str, int]:
    """Idle live sessions per engine, for the pre-spawn guard (#838).

    A failing backend counts as zero — diagnostics must never block a spawn.
    """
    counts: dict[str, int] = {}
    for engine, backend in _BACKENDS.items():
        try:
            counts[engine] = backend.idle_count()
        except Exception:  # noqa: BLE001
            counts[engine] = 0
    return counts
