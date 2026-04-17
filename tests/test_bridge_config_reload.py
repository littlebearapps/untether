"""Tests for TelegramBridgeConfig hot-reload (#286)."""

from __future__ import annotations

import dataclasses

import pytest

from tests.telegram_fakes import FakeBot, FakeTransport, make_cfg
from untether.settings import (
    TelegramFilesSettings,
    TelegramTopicsSettings,
    TelegramTransportSettings,
)
from untether.telegram.bridge import TelegramBridgeConfig


def _settings(**overrides) -> TelegramTransportSettings:
    base = {
        "bot_token": "abc",
        "chat_id": 123,
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
            show_resume_line=False,
            forward_coalesce_s=3.5,
            media_group_debounce_s=2.5,
        )
        cfg.update_from(new_settings)
        assert cfg.allowed_user_ids == (111, 222)
        assert cfg.voice_transcription is True
        assert cfg.voice_max_bytes == 1 * 1024 * 1024
        assert cfg.voice_transcription_model == "whisper-1"
        assert cfg.voice_transcription_base_url == "https://x/v1"
        assert cfg.voice_transcription_api_key == "sk-new"
        assert cfg.voice_show_transcription is False
        assert cfg.show_resume_line is False
        assert cfg.forward_coalesce_s == 3.5
        assert cfg.media_group_debounce_s == 2.5

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
        assert cfg.voice_transcription_api_key == "sk-before"
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
