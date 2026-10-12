"""The engine-neutral live-session seam (#977): ``untether.live_sessions``.

Claude is the only registered backend. These tests pin the properties the
gate sites rely on: an unknown engine has no backend, the Claude adapter
looks its functions up on ``untether.runners.claude`` at call time (so the
existing suites' monkeypatches and direct registry pokes keep working), and
the per-engine idle count never raises.
"""

from __future__ import annotations

from typing import Any

import pytest

from untether import live_sessions
from untether.live_sessions import (
    idle_live_session_counts,
    live_backend,
    supports_live_session,
)
from untether.runners import claude as claude_mod
from untether.runners.claude import ClaudeStreamState, LiveSession

SID = "sid-seam"


def _install(monkeypatch: pytest.MonkeyPatch, *, idle: bool = True) -> LiveSession:
    state = ClaudeStreamState()
    state.completed_turns = 1
    state.turn_open = not idle
    live = LiveSession(session_id=SID, state=state, stdin=None)
    monkeypatch.setitem(claude_mod._LIVE_SESSIONS, SID, live)
    return live


class _FakeBackend:
    engine = "fake"
    display_name = "Fake"
    log_ns = "fake"
    supports_steer = False

    def __init__(self, *, enabled: bool = True, idle: int | Exception = 0) -> None:
        self._enabled = enabled
        self._idle = idle

    def enabled(self) -> bool:
        return self._enabled

    def idle_count(self) -> int:
        if isinstance(self._idle, Exception):
            raise self._idle
        return self._idle


@pytest.mark.anyio
async def test_claude_adapter_resolves_functions_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = live_backend("claude")
    assert backend is not None
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _sync(name: str, result: Any):
        def fake(*args: Any, **kwargs: Any) -> Any:
            calls.append((name, args, kwargs))
            return result

        return fake

    def _async(name: str, result: Any):
        async def fake(*args: Any, **kwargs: Any) -> Any:
            calls.append((name, args, kwargs))
            return result

        return fake

    # Patched AFTER the backend was resolved: the adapter must still call them.
    monkeypatch.setattr(claude_mod, "inject_when_idle", _async("inject", True))
    monkeypatch.setattr(claude_mod, "close_live_session", _async("close", True))
    monkeypatch.setattr(claude_mod, "close_steer_window", _async("steer_close", True))
    monkeypatch.setattr(claude_mod, "steer_into_session", _async("steer", "steered"))
    monkeypatch.setattr(
        claude_mod, "wait_for_session_handoff", _async("handoff", "exited")
    )
    monkeypatch.setattr(claude_mod, "session_live_bg_count", _sync("bg", 3))
    monkeypatch.setattr(claude_mod, "session_linger_info", _sync("linger", (True, 2)))
    monkeypatch.setattr(claude_mod, "is_session_accepting", _sync("accepting", True))
    monkeypatch.setattr(claude_mod, "add_live_session_listener", _sync("listen", True))
    monkeypatch.setattr(claude_mod, "idle_live_session_count", _sync("idle", 4))

    def listener(kind: str, payload: dict[str, Any]) -> None:
        return None

    assert await backend.inject_when_idle(SID, "hi", command_uuid="u1") is True
    assert await backend.close(SID, "cancel", notice=True) is True
    assert (
        await backend.close(SID, "options_changed", notice=True, only_if_idle=True)
        is True
    )
    assert await backend.close_steer_window(SID, "cancel") is True
    assert await backend.steer(SID, "now", command_uuid="u2", cwd=None) == "steered"
    assert await backend.wait_for_handoff(SID, 30.0) == "exited"
    assert backend.live_bg_count(SID) == 3
    assert backend.linger_info(SID) == (True, 2)
    assert backend.is_accepting(SID) is True
    assert backend.add_listener(SID, listener) is True
    assert backend.idle_count() == 4

    # Arguments reach Claude's functions exactly as the call sites wrote them
    # (existing suites assert on these call shapes): no keyword is added.
    assert calls == [
        ("inject", (SID, "hi"), {"command_uuid": "u1"}),
        ("close", (SID, "cancel"), {"notice": True}),
        ("close", (SID, "options_changed"), {"notice": True, "only_if_idle": True}),
        ("steer_close", (SID, "cancel"), {}),
        ("steer", (SID, "now"), {"command_uuid": "u2", "cwd": None}),
        ("handoff", (SID, 30.0), {}),
        ("bg", (SID,), {}),
        ("linger", (SID,), {}),
        ("accepting", (SID,), {}),
        ("listen", (SID, listener), {}),
        ("idle", (), {}),
    ]


@pytest.mark.parametrize(
    "engine", [None, "", "codex", "opencode", "pi", "antigravity", "gemini", "amp"]
)
def test_live_backend_unknown_engine_is_none(engine: str | None) -> None:
    assert live_backend(engine) is None
    assert supports_live_session(engine) is False


def test_claude_backend_identity() -> None:
    backend = live_backend("claude")
    assert backend is not None
    assert backend is live_backend("claude")
    # User-visible text and log event names are built from these.
    assert (backend.engine, backend.display_name, backend.log_ns) == (
        "claude",
        "Claude",
        "claude",
    )
    assert backend.supports_steer is True
    assert backend.enabled() is True


def test_claude_adapter_reads_live_registry_directly(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    backend = live_backend("claude")
    assert backend is not None
    assert backend.get(SID) is None
    assert backend.is_accepting(SID) is False
    assert backend.spawn_options(SID) is None
    assert backend.spawn_cwd(SID) is None
    assert backend.pre_inject_close_reason(SID) is None

    live = _install(monkeypatch)
    options = object()
    live.state.spawn_run_options = options
    live.state.spawn_cwd = tmp_path

    assert backend.get(SID) is live
    assert backend.is_accepting(SID) is True
    assert backend.spawn_options(SID) is options
    assert backend.spawn_cwd(SID) == tmp_path
    assert backend.pre_inject_close_reason(SID) is None

    live.state.plan_rearm_failed = True
    assert backend.pre_inject_close_reason(SID) == "plan_rearm_failed"

    live.closing = True
    assert backend.is_accepting(SID) is False


def test_supports_live_session_respects_watchdog_and_engine_kill_switches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Claude's kill switch (`[watchdog] live_sessions`) is read per spawn in
    # the runner, not here: the backend always reports enabled, and a session
    # that was never kept live simply isn't in the registry.
    assert supports_live_session("claude") is True

    # A backend with its own switch is honoured, and read on every call.
    fake = _FakeBackend(enabled=False)
    monkeypatch.setitem(live_sessions._BACKENDS, "fake", fake)
    assert live_backend("fake") is fake
    assert supports_live_session("fake") is False
    fake._enabled = True
    assert supports_live_session("fake") is True


def test_idle_counts_per_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    assert idle_live_session_counts() == {"claude": 0}

    live = _install(monkeypatch, idle=True)
    assert idle_live_session_counts() == {"claude": 1}

    live.closing = True  # closing sessions are not "idle and holding a slot"
    assert idle_live_session_counts() == {"claude": 0}
    live.closing = False

    monkeypatch.setitem(live_sessions._BACKENDS, "fake", _FakeBackend(idle=2))
    assert idle_live_session_counts() == {"claude": 1, "fake": 2}

    # Diagnostics must never block a spawn: a failing backend counts as zero.
    monkeypatch.setitem(
        live_sessions._BACKENDS, "fake", _FakeBackend(idle=RuntimeError("boom"))
    )
    assert idle_live_session_counts() == {"claude": 1, "fake": 0}
