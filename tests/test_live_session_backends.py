"""The engine-neutral live-session seam (#977): ``untether.live_sessions``.

Claude, Codex app-server (#964) and Antigravity (#977) are the registered
backends. These pin
the properties the gate sites rely on: an unknown engine has no backend, the Claude adapter
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
    monkeypatch.setattr(claude_mod, "session_turn_pending", _sync("pending", True))
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
    assert backend.turn_pending(SID) is True
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
        ("pending", (SID,), {}),
        ("listen", (SID, listener), {}),
        ("idle", (), {}),
    ]


@pytest.mark.parametrize("engine", [None, "", "opencode", "pi", "gemini", "amp"])
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


def test_codex_backend_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    from untether.runners import codex_app_server as cas

    backend = live_backend("codex")
    assert backend is not None and backend is live_backend("codex")
    assert (backend.engine, backend.display_name, backend.log_ns) == (
        "codex",
        "Codex",
        "codex",
    )
    # Steer and follow-ups exist only on the app-server transport (#964).
    monkeypatch.setattr(cas, "APP_SERVER_ACTIVE", False)
    assert backend.supports_steer is False and backend.enabled() is False
    assert supports_live_session("codex") is False
    monkeypatch.setattr(cas, "APP_SERVER_ACTIVE", True)
    assert backend.supports_steer is True and backend.enabled() is True


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
    assert idle_live_session_counts() == {"claude": 0, "codex": 0, "antigravity": 0}

    live = _install(monkeypatch, idle=True)
    assert idle_live_session_counts() == {"claude": 1, "codex": 0, "antigravity": 0}

    live.closing = True  # closing sessions are not "idle and holding a slot"
    assert idle_live_session_counts() == {"claude": 0, "codex": 0, "antigravity": 0}
    live.closing = False

    monkeypatch.setitem(live_sessions._BACKENDS, "fake", _FakeBackend(idle=2))
    assert idle_live_session_counts() == {
        "claude": 1,
        "codex": 0,
        "antigravity": 0,
        "fake": 2,
    }

    # Diagnostics must never block a spawn: a failing backend counts as zero.
    monkeypatch.setitem(
        live_sessions._BACKENDS, "fake", _FakeBackend(idle=RuntimeError("boom"))
    )
    assert idle_live_session_counts() == {
        "claude": 1,
        "codex": 0,
        "antigravity": 0,
        "fake": 0,
    }


# ── Antigravity (#977) ──────────────────────────────────────────────────────


def _agy_config(tmp_path, monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    config = tmp_path / "untether.toml"
    config.write_text(
        '[transports.telegram]\nbot_token = "tok"\nchat_id = 123\n'
        f"allow_any_user = true\n\n[antigravity]\nlive_sessions = {str(on).lower()}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UNTETHER_CONFIG_PATH", str(config))


@pytest.mark.anyio
async def test_agy_backend_protocol_shape() -> None:
    backend = live_backend("antigravity")
    assert backend is not None and backend is live_backend("antigravity")
    assert (backend.engine, backend.display_name, backend.log_ns) == (
        "antigravity",
        "Antigravity",
        "antigravity",
    )
    # Nothing warm: every read is empty, every write a no-op.
    assert backend.get(SID) is None
    assert backend.is_accepting(SID) is False
    assert backend.idle_count() == 0
    assert backend.linger_info(SID) is None
    assert backend.turn_pending(SID) is False
    assert backend.live_bg_count(SID) == 0
    assert backend.holds_background_jobs(SID) is False
    assert backend.spawn_options(SID) is None
    assert backend.spawn_cwd(SID) is None
    assert backend.pre_inject_close_reason(SID) is None
    assert await backend.close(SID, "cancel", notice=True) is False
    assert backend.add_listener(SID, lambda kind, payload: None) is False
    assert await backend.close_steer_window(SID, "cancel") is False
    assert await backend.interrupt_pending_turn(SID, any_turn=True) is False
    assert await backend.inject_when_idle(SID, "x", command_uuid="u") is False
    assert await backend.steer(SID, "x", command_uuid="u") is None
    assert await backend.wait_for_handoff(SID, 1.0) == "free"
    assert await backend.close_idle_for_continue(None) == 0


def test_supports_live_session_agy_follows_switch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``[antigravity] live_sessions`` (default off), read on every call."""
    assert supports_live_session("antigravity") is False  # no config at all
    _agy_config(tmp_path, monkeypatch, on=True)
    assert supports_live_session("antigravity") is True
    _agy_config(tmp_path, monkeypatch, on=False)
    assert supports_live_session("antigravity") is False
    # Switched off while a process is still warm: it keeps its follow-ups.
    from untether.antigravity import live as agy_live

    monkeypatch.setattr(agy_live, "_ENTRIES", [object()])
    assert supports_live_session("antigravity") is True


@pytest.mark.anyio
async def test_steer_backend_prefers_agy_live_and_delegates_to_gate(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#981 behaviour is unchanged for a one-shot run: no warm process, so
    ``get`` and ``steer`` reach the run's gate through the live backend."""
    from untether.antigravity import gate_server
    from untether.events import EventFactory
    from untether.live_sessions import steer_backend

    backend = live_backend("antigravity")
    assert steer_backend("antigravity") is backend
    monkeypatch.setattr(gate_server.gate_plugin, "installed_ready", lambda: True)
    assert backend.supports_steer is True
    gate = gate_server.GateRun(
        config_path=tmp_path / "u.toml", factory=EventFactory("antigravity")
    )
    gate.set_conversation(SID)
    try:
        assert backend.get(SID) is gate
        assert await backend.steer(SID, "turn left", command_uuid="u1") == "steered"
        assert [i.command_uuid for i in gate.steer_queue] == ["u1"]
        gate.note_result()
        assert await backend.steer(SID, "late", command_uuid="u2") == "window_closed"
    finally:
        await gate.aclose()


def test_idle_counts_include_agy(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from untether.antigravity import live as agy_live

    proc = SimpleNamespace(returncode=None)
    state = SimpleNamespace(
        session_id=SID,
        warm_closing=None,
        warm_proc=proc,
        warm_turns=1,
        warm_turn_open=False,
        warm_checking=False,
        warm_uuids=[],
        warm_expired=0,
    )
    monkeypatch.setattr(agy_live, "_ENTRIES", [agy_live.WarmEntry(None, state)])
    assert idle_live_session_counts()["antigravity"] == 1
    state.warm_turn_open = True  # mid-turn: not idle (#838 counts idle only)
    assert idle_live_session_counts()["antigravity"] == 0


@pytest.mark.anyio
async def test_agy_followup_injected_then_idle_close(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """977-3 exit gate, through the real code path: a warm fake ``agy``
    (``AntigravityRunner.run``), the scheduler's injector
    (``live_followup.inject_live_followup`` → ``_AgyLive`` →
    ``antigravity/live.py`` → ``send_followup``), then the lifecycle's idle
    close. One spawn, two answers, stdin EOF, rc 0."""
    import anyio
    import structlog.testing

    from tests.test_antigravity_live import (
        CID,
        WarmRun,
        _is_completed,
        _runner,
        _turn,
    )
    from untether.antigravity import live as agy_live
    from untether.live_followup import inject_live_followup
    from untether.model import CompletedEvent, ResumeToken, TurnEvent
    from untether.runners import antigravity as agy
    from untether.scheduler import ThreadJob
    from untether.utils.paths import reset_run_base_dir, set_run_base_dir

    monkeypatch.setattr(agy, "_probe_agy_version", agy._run_agy_version)
    monkeypatch.setenv("UNTETHER_FAKE_AGY_WARM", "1")
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "warm_two_turns")
    monkeypatch.setattr(agy_live, "POLL_S", 0.05)
    monkeypatch.setattr(agy_live, "_live_settings", lambda: (0.5, 14400.0))
    _agy_config(tmp_path, monkeypatch, on=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    token = set_run_base_dir(proj)
    job = ThreadJob(
        chat_id=1,
        user_msg_id=2,
        text="Reply TWO",
        resume_token=ResumeToken(engine="antigravity", value=CID),
    )
    run = WarmRun(_runner())
    injected: list[bool] = []
    try:
        with structlog.testing.capture_logs() as logs:
            with anyio.fail_after(30):
                async with anyio.create_task_group() as tg:
                    tg.start_soon(run.consume, "ONE")
                    await run.until(_is_completed)
                    injected.append(await inject_live_followup(job))
                    await run.until(_turn("completed", 2))
                    await run.done.wait()  # nobody closes it but the idle timer
    finally:
        reset_run_base_dir(token)

    assert injected == [True]
    names = [e["event"] for e in logs]
    assert names.count("subprocess.spawn") == 1
    (inj,) = [e for e in logs if e["event"] == "antigravity.live_session.injected"]
    assert inj["session_id"] == CID
    (closed,) = [e for e in logs if e["event"] == "antigravity.live_session.closed"]
    assert closed["reason"] == "idle_no_tasks" and closed["turns"] == 2
    (exit_log,) = [e for e in logs if e["event"] == "subprocess.exit"]
    assert exit_log["rc"] == 0
    assert sum(isinstance(e, CompletedEvent) for e in run.events) == 1
    turns = [e for e in run.events if isinstance(e, TurnEvent)]
    assert [(t.phase, t.turn, t.answer) for t in turns] == [
        ("started", 2, None),
        ("completed", 2, "TWO\n"),
    ]
    assert turns[0].command_uuid == inj["command_uuid"]
    assert live_backend("antigravity").get(CID) is None
