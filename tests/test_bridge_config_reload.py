"""Tests for TelegramBridgeConfig hot-reload (#286)."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from tests.telegram_fakes import FakeBot, FakeTransport, make_cfg
from untether.config import ProjectsConfig
from untether.runner_bridge import RunningTask
from untether.settings import (
    TelegramFilesSettings,
    TelegramTopicsSettings,
    TelegramTransportSettings,
)
from untether.telegram.bridge import TelegramBridgeConfig
from untether.telegram.loop import _notify_reload_applied
from untether.transport import MessageRef
from untether.transport_runtime import TransportRuntime


def _settings(**overrides) -> TelegramTransportSettings:
    base = {
        "bot_token": "abc",
        "chat_id": 123,
        # #377: tests don't care about user allowlisting; opt in to the
        # explicit "open bot" so the model_validator doesn't reject these
        # fixtures. Tests that do care set allowed_user_ids via overrides.
        "allow_any_user": True,
    }
    base.update(overrides)
    return TelegramTransportSettings.model_validate(base)


@pytest.fixture
def cfg() -> TelegramBridgeConfig:
    return make_cfg(FakeTransport())


# ── Unfreezing ─────────────────────────────────────────────────────────


class TestUnfrozen:
    def test_cfg_is_unfrozen(self, cfg: TelegramBridgeConfig):
        """Direct attribute assignment no longer raises FrozenInstanceError."""
        cfg.voice_transcription = True
        assert cfg.voice_transcription is True

    def test_cfg_keeps_slots(self, cfg: TelegramBridgeConfig):
        """slots=True still prevents creating arbitrary new attributes."""
        with pytest.raises(AttributeError):
            cfg.not_a_real_field = 42  # type: ignore[attr-defined]

    def test_dataclass_is_unfrozen(self):
        """dataclasses.is_dataclass confirms the @dataclass decorator remained."""
        assert dataclasses.is_dataclass(TelegramBridgeConfig)
        # Frozen dataclasses expose __setattr__ that raises;
        # unfrozen ones use the default.
        cfg_inst = make_cfg(FakeTransport())
        cfg_inst.show_resume_line = False  # must not raise


# ── update_from ────────────────────────────────────────────────────────


class TestUpdateFrom:
    def test_update_from_all_fields(self, cfg: TelegramBridgeConfig):
        new_settings = _settings(
            allowed_user_ids=[111, 222],
            voice_transcription=True,
            voice_max_bytes=1 * 1024 * 1024,
            voice_transcription_model="whisper-1",
            voice_transcription_base_url="https://x/v1",
            voice_transcription_api_key="sk-new",
            voice_show_transcription=False,
            voice_transcription_language="EN",
            voice_transcription_prompt=" Trello, Untether ",
            show_resume_line=False,
            forward_coalesce_s=3.5,
            media_group_debounce_s=2.5,
            voice_transcription_url_allowlist=["10.0.0.0/8"],
        )
        cfg.update_from(new_settings)
        assert cfg.allowed_user_ids == (111, 222)
        assert cfg.voice_transcription_url_allowlist == ("10.0.0.0/8",)
        assert cfg.voice_transcription is True
        assert cfg.voice_max_bytes == 1 * 1024 * 1024
        assert cfg.voice_transcription_model == "whisper-1"
        assert cfg.voice_transcription_base_url == "https://x/v1"
        # #638: hot-reloadable, normalised to lowercase at parse time
        assert cfg.voice_transcription_language == "en"
        # #691: hot-reloadable, stripped at parse time
        assert cfg.voice_transcription_prompt == "Trello, Untether"
        # #378: SecretStr — compare via .get_secret_value() since equality
        # against a bare str returns False.
        assert cfg.voice_transcription_api_key is not None
        assert cfg.voice_transcription_api_key.get_secret_value() == "sk-new"
        assert cfg.voice_show_transcription is False
        assert cfg.show_resume_line is False
        assert cfg.forward_coalesce_s == 3.5
        assert cfg.media_group_debounce_s == 2.5

    def test_388_approval_originator_only_hot_reloads(self, cfg: TelegramBridgeConfig):
        """#388: opt-in, off by default, and not restart-required."""
        assert _settings().approval_originator_only is False
        assert cfg.approval_originator_only is False
        cfg.update_from(_settings(approval_originator_only=True))
        assert cfg.approval_originator_only is True
        cfg.update_from(_settings())
        assert cfg.approval_originator_only is False
        assert (
            "approval_originator_only"
            not in TelegramTransportSettings.RESTART_REQUIRED_FIELDS
        )

    def test_update_from_swaps_files_object(self, cfg: TelegramBridgeConfig):
        original = cfg.files
        new_files = TelegramFilesSettings(
            enabled=True,
            auto_put=False,
            uploads_dir="uploads",
        )
        cfg.update_from(_settings(files=new_files))
        assert cfg.files is not original
        assert cfg.files.enabled is True
        assert cfg.files.auto_put is False
        assert cfg.files.uploads_dir == "uploads"

    def test_update_from_preserves_identity_fields(self, cfg: TelegramBridgeConfig):
        """bot, runtime, chat_id, exec_cfg, session_mode, topics are not reloaded."""
        original_bot = cfg.bot
        original_runtime = cfg.runtime
        original_chat_id = cfg.chat_id
        original_exec = cfg.exec_cfg
        original_session_mode = cfg.session_mode
        original_topics = cfg.topics

        cfg.update_from(
            _settings(
                chat_id=999,
                session_mode="chat",
                topics=TelegramTopicsSettings(enabled=True, scope="main"),
            )
        )

        # These architectural fields must not move even if the TOML changed.
        assert cfg.bot is original_bot
        assert cfg.runtime is original_runtime
        assert cfg.chat_id == original_chat_id
        assert cfg.exec_cfg is original_exec
        assert cfg.session_mode == original_session_mode
        assert cfg.topics is original_topics

    def test_update_from_clears_voice_api_key(self, cfg: TelegramBridgeConfig):
        """Removing voice_transcription_api_key from config resets it to None."""
        cfg.update_from(_settings(voice_transcription_api_key="sk-before"))
        # #378: SecretStr — equality is by SecretStr identity, not raw string.
        assert cfg.voice_transcription_api_key is not None
        assert cfg.voice_transcription_api_key.get_secret_value() == "sk-before"
        cfg.update_from(_settings())  # no voice_transcription_api_key
        assert cfg.voice_transcription_api_key is None

    def test_update_from_allowed_user_ids_stored_as_tuple(
        self, cfg: TelegramBridgeConfig
    ):
        cfg.update_from(_settings(allowed_user_ids=[1, 2, 3]))
        assert isinstance(cfg.allowed_user_ids, tuple)
        assert cfg.allowed_user_ids == (1, 2, 3)

    def test_update_from_empty_allowed_user_ids(self, cfg: TelegramBridgeConfig):
        cfg.update_from(_settings(allowed_user_ids=[]))
        assert cfg.allowed_user_ids == ()


class TestTriggerManagerField:
    def test_trigger_manager_defaults_to_none(self):
        """New field added for rc4 — default must stay None to avoid breakage."""
        cfg = TelegramBridgeConfig(
            bot=FakeBot(),
            runtime=make_cfg(FakeTransport()).runtime,
            chat_id=1,
            startup_msg="",
            exec_cfg=make_cfg(FakeTransport()).exec_cfg,
        )
        assert cfg.trigger_manager is None

    def test_trigger_manager_assignable_after_construction(self):
        """Since the dataclass is unfrozen, post-construction assignment works."""
        cfg = make_cfg(FakeTransport())
        from untether.triggers.manager import TriggerManager

        mgr = TriggerManager()
        cfg.trigger_manager = mgr
        assert cfg.trigger_manager is mgr


# ── #318: RESTART_REQUIRED_FIELDS on settings models ───────────────────


class TestRestartRequiredFields:
    """#318: the set of fields that require a process restart is authoritative
    on the settings model, not scattered across loop.py / /config / docs. This
    locks the invariant in so future edits to the model stay in sync."""

    def test_classvar_exists_and_is_frozen(self):
        assert hasattr(TelegramTransportSettings, "RESTART_REQUIRED_FIELDS")
        assert isinstance(TelegramTransportSettings.RESTART_REQUIRED_FIELDS, frozenset)

    def test_expected_members(self):
        """v0.35.2 baseline — update with care. Additions need matching
        handle_reload() logic, config.md docs, and `/config` 🔄 annotations.
        Removals need hot-reload wiring in TelegramBridgeConfig.update_from."""
        assert (
            frozenset(
                {
                    "bot_token",
                    "chat_id",
                    "session_mode",
                    "topics",
                    "message_overflow",
                }
            )
            == TelegramTransportSettings.RESTART_REQUIRED_FIELDS
        )

    def test_every_listed_field_exists_on_model(self):
        """Every entry must be a real field. Typos would silently neuter the
        restart warning (membership test would never hit)."""
        field_names = set(TelegramTransportSettings.model_fields.keys())
        stray = TelegramTransportSettings.RESTART_REQUIRED_FIELDS - field_names
        assert stray == set(), (
            f"RESTART_REQUIRED_FIELDS references unknown fields: {stray}"
        )

    def test_loop_consumes_the_classvar(self):
        """loop.py:handle_reload should read the ClassVar, not inline a copy.
        This grep-style test prevents silent drift between the two."""
        from pathlib import Path

        loop_src = Path("src/untether/telegram/loop.py").read_text()
        # The legacy inline set is gone
        assert "RESTART_ONLY_KEYS" not in loop_src, (
            "handle_reload still has an inline RESTART_ONLY_KEYS set — "
            "consume TelegramTransportSettings.RESTART_REQUIRED_FIELDS instead"
        )
        # The ClassVar is actually referenced
        assert "TelegramTransportSettings.RESTART_REQUIRED_FIELDS" in loop_src, (
            "handle_reload should reference "
            "TelegramTransportSettings.RESTART_REQUIRED_FIELDS"
        )


# ── #1030: who gets the reload notice ─────────────────────────────────

_TOML = Path("/tmp/untether.toml")


def _with_project_chats(cfg: TelegramBridgeConfig, *chat_ids: int) -> None:
    projects = ProjectsConfig(
        projects={}, chat_map={chat_id: f"p{n}" for n, chat_id in enumerate(chat_ids)}
    )
    cfg.runtime = TransportRuntime(router=cfg.runtime._router, projects=projects)


def _run(
    chat_id: int, *, message_id: int = 1, thread_id: int | None = None
) -> dict[MessageRef, RunningTask]:
    ref = MessageRef(channel_id=chat_id, message_id=message_id)
    return {ref: RunningTask(thread_id=thread_id)}


def _targets(transport: FakeTransport) -> list[tuple[int, int | None]]:
    return sorted(
        (
            (call["channel_id"], call["options"].thread_id)
            for call in transport.send_calls
        ),
        key=lambda target: (target[0], target[1] or 0),
    )


class TestNotifyReloadApplied:
    """#1030: a hot-reload confirmation is not a broadcast. It goes to the
    owner DMs plus the chats with a run in flight; only a pending restart
    (#318) still reaches every project chat, as one message."""

    @pytest.mark.anyio
    async def test_hot_reload_skips_idle_project_chats(self):
        transport = FakeTransport()
        cfg = make_cfg(transport)
        cfg.allowed_user_ids = (555, 777)
        _with_project_chats(cfg, -100, -200)
        await _notify_reload_applied(
            cfg, path=_TOML, hot_keys=["voice_transcription_api_key"], restart_keys=[]
        )
        assert _targets(transport) == [(555, None), (777, None)]
        for call in transport.send_calls:
            assert "No restart needed" in call["message"].text
            assert call["options"].notify is False
            assert call["message"].extra.get("parse_mode") == "Markdown"

    @pytest.mark.anyio
    async def test_hot_reload_falls_back_to_chat_id_without_allowed_users(self):
        transport = FakeTransport()
        cfg = make_cfg(transport)
        cfg.allowed_user_ids = ()
        _with_project_chats(cfg, -100)
        await _notify_reload_applied(
            cfg, path=_TOML, hot_keys=["show_resume_line"], restart_keys=[]
        )
        assert _targets(transport) == [(cfg.chat_id, None)]

    @pytest.mark.anyio
    async def test_hot_reload_reaches_a_chat_with_a_run_in_flight(self):
        """#547/#548: the agent that made the edit still sees the cue."""
        transport = FakeTransport()
        cfg = make_cfg(transport)
        cfg.allowed_user_ids = (555,)
        _with_project_chats(cfg, -100, -200)
        await _notify_reload_applied(
            cfg,
            path=_TOML,
            hot_keys=["show_resume_line"],
            restart_keys=[],
            running_tasks=_run(-200),
        )
        assert _targets(transport) == [(-200, None), (555, None)]

    @pytest.mark.anyio
    async def test_busy_owner_dm_gets_one_notice(self):
        transport = FakeTransport()
        cfg = make_cfg(transport)
        cfg.allowed_user_ids = (555,)
        await _notify_reload_applied(
            cfg,
            path=_TOML,
            hot_keys=["show_resume_line"],
            restart_keys=[],
            running_tasks=_run(555),
        )
        assert _targets(transport) == [(555, None)]

    @pytest.mark.anyio
    async def test_busy_topic_gets_the_notice_in_its_thread(self):
        """A forum run is addressed by the task's thread (#826), once per
        topic, and never doubled into the chat's General topic."""
        transport = FakeTransport()
        cfg = make_cfg(transport)
        cfg.allowed_user_ids = (555,)
        _with_project_chats(cfg, -100)
        running = {
            **_run(-100, message_id=1, thread_id=37),
            **_run(-100, message_id=2, thread_id=37),
            **_run(-100, message_id=3, thread_id=41),
        }
        await _notify_reload_applied(
            cfg,
            path=_TOML,
            hot_keys=["show_resume_line"],
            restart_keys=[],
            running_tasks=running,
        )
        assert _targets(transport) == [(-100, 37), (-100, 41), (555, None)]

    @pytest.mark.anyio
    async def test_finished_and_idle_live_runs_are_not_busy(self):
        transport = FakeTransport()
        cfg = make_cfg(transport)
        cfg.allowed_user_ids = (555,)
        finished = RunningTask()
        finished.done.set()
        idle = RunningTask()
        idle.edits = SimpleNamespace(  # type: ignore[assignment]
            stream=SimpleNamespace(
                engine_state=SimpleNamespace(
                    live_mode=True, completed_turns=1, turn_open=False
                )
            )
        )
        running = {
            MessageRef(channel_id=-100, message_id=1): finished,
            MessageRef(channel_id=-200, message_id=2): idle,
        }
        await _notify_reload_applied(
            cfg,
            path=_TOML,
            hot_keys=["show_resume_line"],
            restart_keys=[],
            running_tasks=running,
        )
        assert _targets(transport) == [(555, None)]

    @pytest.mark.anyio
    async def test_restart_required_reaches_project_chats_and_admins(self, monkeypatch):
        """#318 follow-up: a pending restart affects every chat, so it keeps
        the wide audience, and it pings (#927 names the running unit)."""
        from untether import service_manager

        monkeypatch.setattr(
            service_manager,
            "restart_command",
            lambda: "systemctl --user restart untether-dev",
        )
        transport = FakeTransport()
        cfg = make_cfg(transport)
        cfg.allowed_user_ids = (555,)
        _with_project_chats(cfg, -100, -200)
        await _notify_reload_applied(
            cfg,
            path=_TOML,
            hot_keys=[],
            restart_keys=["session_mode", "topics"],
            running_tasks=_run(-100, thread_id=37),
        )
        # One message per chat: the busy topic stands in for its chat.
        assert _targets(transport) == [(-200, None), (-100, 37), (555, None)]
        for call in transport.send_calls:
            text = call["message"].text
            assert "Restart required" in text
            assert "`session_mode`" in text
            assert "`topics`" in text
            assert "run `systemctl --user restart untether-dev`" in text
            assert call["options"].notify is True

    @pytest.mark.anyio
    async def test_partial_reload_uses_the_restart_audience(self):
        transport = FakeTransport()
        cfg = make_cfg(transport)
        cfg.allowed_user_ids = (555,)
        _with_project_chats(cfg, -100)
        await _notify_reload_applied(
            cfg,
            path=_TOML,
            hot_keys=["show_resume_line"],
            restart_keys=["message_overflow"],
        )
        assert _targets(transport) == [(-100, None), (555, None)]
        assert "Partial reload" in transport.send_calls[0]["message"].text

    @pytest.mark.anyio
    async def test_restart_required_falls_back_to_chat_id_when_no_targets(self):
        transport = FakeTransport()
        cfg = make_cfg(transport)
        cfg.allowed_user_ids = ()
        await _notify_reload_applied(
            cfg, path=_TOML, hot_keys=[], restart_keys=["bot_token"]
        )
        assert _targets(transport) == [(cfg.chat_id, None)]

    @pytest.mark.anyio
    async def test_no_changed_keys_sends_nothing(self):
        transport = FakeTransport()
        cfg = make_cfg(transport)
        cfg.allowed_user_ids = (555,)
        await _notify_reload_applied(cfg, path=_TOML, hot_keys=[], restart_keys=[])
        assert transport.send_calls == []

    @pytest.mark.anyio
    async def test_continues_past_send_failures(self):
        class FlakyTransport(FakeTransport):
            async def send(self, **kw):  # type: ignore[override]
                if kw["channel_id"] == 1:
                    raise RuntimeError("telegram api error")
                return await super().send(**kw)

        transport = FlakyTransport()
        cfg = make_cfg(transport)
        cfg.allowed_user_ids = (1, 2, 3)
        # Must not raise — per-chat failures are logged and skipped.
        with capture_logs() as logs:
            await _notify_reload_applied(
                cfg, path=_TOML, hot_keys=["show_resume_line"], restart_keys=[]
            )
        assert _targets(transport) == [(2, None), (3, None)]
        sent = [e for e in logs if e["event"] == "config.reload.applied_notify.sent"]
        assert sent[0]["sent_count"] == 2
        assert sent[0]["targets"] == [[1, None], [2, None], [3, None]]
