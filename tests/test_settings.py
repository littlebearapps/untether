from __future__ import annotations

from pathlib import Path

import pytest

from untether.config import ConfigError, read_config
from untether.settings import (
    UntetherSettings,
    load_settings,
    load_settings_if_exists,
    require_telegram,
    validate_settings_data,
)


def test_load_settings_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        'transport = "telegram"\n\n'
        "[transports.telegram]\n"
        'bot_token = "token"\n'
        "chat_id = 123\n\n"
        "allow_any_user = true\n"
        "[codex]\n"
        'model = "gpt-4"\n',
        encoding="utf-8",
    )

    settings, loaded_path = load_settings(config_path)

    assert loaded_path == config_path
    assert settings.transport == "telegram"
    assert settings.transports.telegram.chat_id == 123
    assert settings.engine_config("codex", config_path=config_path)["model"] == "gpt-4"

    token, chat_id = require_telegram(settings, config_path)
    assert token == "token"
    assert chat_id == 123

    # #196: bot_token is SecretStr — unwrap to compare.
    assert settings.transports.telegram.bot_token.get_secret_value() == "token"
    # Verify masking: str()/repr() do not leak the token.
    assert "token" not in str(settings.transports.telegram.bot_token)
    assert "token" not in repr(settings.transports.telegram.bot_token)


def test_env_overrides_toml(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        'default_engine = "codex"\n'
        'transport = "telegram"\n\n'
        "[transports.telegram]\n"
        'bot_token = "token"\n'
        "chat_id = 123\n"
        "allow_any_user = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UNTETHER__DEFAULT_ENGINE", "claude")

    settings, _ = load_settings(config_path)

    assert settings.default_engine == "claude"


def test_legacy_keys_migrated(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        'bot_token = "token"\nchat_id = 123\nallow_any_user = true\n',
        encoding="utf-8",
    )

    settings, loaded_path = load_settings(config_path)

    assert loaded_path == config_path
    assert settings.transports.telegram.chat_id == 123
    raw = read_config(config_path)
    assert "bot_token" not in raw
    assert "chat_id" not in raw
    assert raw["transports"]["telegram"]["bot_token"] == "token"
    assert raw["transports"]["telegram"]["chat_id"] == 123
    assert raw["transport"] == "telegram"


def test_validate_settings_data_rejects_invalid_bot_token_type(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    data = {
        "transport": "telegram",
        "transports": {
            "telegram": {"bot_token": 123, "chat_id": 123, "allow_any_user": True}
        },
    }

    with pytest.raises(ConfigError, match="bot_token"):
        validate_settings_data(data, config_path=config_path)


def test_validate_settings_data_rejects_empty_default_engine(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    data = {
        "default_engine": "   ",
        "transport": "telegram",
        "transports": {
            "telegram": {"bot_token": "token", "chat_id": 123, "allow_any_user": True}
        },
    }

    with pytest.raises(ConfigError, match="default_engine"):
        validate_settings_data(data, config_path=config_path)


def test_validate_settings_data_rejects_empty_default_project(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    data = {
        "default_project": "   ",
        "transports": {
            "telegram": {"bot_token": "token", "chat_id": 123, "allow_any_user": True}
        },
    }

    with pytest.raises(ConfigError, match="default_project"):
        validate_settings_data(data, config_path=config_path)


def test_validate_settings_data_rejects_empty_project_path(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    data = {
        "projects": {"z80": {"path": "   "}},
        "transports": {
            "telegram": {"bot_token": "token", "chat_id": 123, "allow_any_user": True}
        },
    }

    with pytest.raises(ConfigError, match="path"):
        validate_settings_data(data, config_path=config_path)


def test_engine_config_none_and_invalid(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    settings = UntetherSettings.model_validate(
        {
            "transport": "telegram",
            "transports": {
                "telegram": {
                    "bot_token": "token",
                    "chat_id": 123,
                    "allow_any_user": True,
                }
            },
            "codex": None,
        }
    )
    assert settings.engine_config("codex", config_path=config_path) == {}

    settings = UntetherSettings.model_validate(
        {
            "transport": "telegram",
            "transports": {
                "telegram": {
                    "bot_token": "token",
                    "chat_id": 123,
                    "allow_any_user": True,
                }
            },
            "codex": "nope",
        }
    )
    with pytest.raises(ConfigError, match="codex"):
        settings.engine_config("codex", config_path=config_path)


def test_transport_config_telegram_and_extra(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    settings = UntetherSettings.model_validate(
        {
            "transport": "telegram",
            "transports": {
                "telegram": {
                    "bot_token": "token",
                    "chat_id": 123,
                    "allow_any_user": True,
                }
            },
        }
    )
    telegram = settings.transport_config("telegram", config_path=config_path)
    # #196: model_dump() preserves SecretStr wrappers; unwrap to check value.
    assert telegram["bot_token"].get_secret_value() == "token"
    assert telegram["chat_id"] == 123

    settings = UntetherSettings.model_validate(
        {
            "transport": "telegram",
            "transports": {
                "telegram": {
                    "bot_token": "token",
                    "chat_id": 123,
                    "allow_any_user": True,
                },
                "discord": None,
            },
        }
    )
    assert settings.transport_config("discord", config_path=config_path) == {}

    settings = UntetherSettings.model_validate(
        {
            "transport": "telegram",
            "transports": {
                "telegram": {
                    "bot_token": "token",
                    "chat_id": 123,
                    "allow_any_user": True,
                },
                "discord": "nope",
            },
        }
    )
    with pytest.raises(ConfigError, match=r"transports\.discord"):
        settings.transport_config("discord", config_path=config_path)


def test_bot_token_none_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    data = {
        "transport": "telegram",
        "transports": {
            "telegram": {"bot_token": None, "chat_id": 123, "allow_any_user": True}
        },
    }
    with pytest.raises(ConfigError, match="bot_token"):
        validate_settings_data(data, config_path=config_path)


def test_voice_transcription_api_key_is_secret_str(tmp_path: Path) -> None:
    """#378: voice_transcription_api_key must be SecretStr — masks repr()/str()
    and only yields the raw value via .get_secret_value()."""
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        "[transports.telegram]\n"
        'bot_token = "tok"\n'
        "chat_id = 123\n"
        "allow_any_user = true\n"
        "voice_transcription = true\n"
        'voice_transcription_api_key = "sk-supersecret-1234567890ABCDEF"\n',
        encoding="utf-8",
    )
    settings, _ = load_settings(config_path)
    key = settings.transports.telegram.voice_transcription_api_key
    assert key is not None
    assert key.get_secret_value() == "sk-supersecret-1234567890ABCDEF"
    # Masking: str() and repr() must not leak the value.
    assert "supersecret" not in str(key)
    assert "supersecret" not in repr(key)


def test_voice_transcription_api_key_empty_string_normalised_to_none(
    tmp_path: Path,
) -> None:
    """#378: empty/whitespace-only API key round-trips to None so downstream
    truthy / `is not None` checks behave the same as with the prior
    `NonEmptyStr | None` field type."""
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        "[transports.telegram]\n"
        'bot_token = "tok"\n'
        "chat_id = 123\n"
        "allow_any_user = true\n"
        'voice_transcription_api_key = "   "\n',
        encoding="utf-8",
    )
    settings, _ = load_settings(config_path)
    assert settings.transports.telegram.voice_transcription_api_key is None


def test_voice_transcription_api_key_default_none(tmp_path: Path) -> None:
    """#378: default is still None when key is omitted."""
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        '[transports.telegram]\nbot_token = "tok"\nchat_id = 123\n'
        "allow_any_user = true\n",
        encoding="utf-8",
    )
    settings, _ = load_settings(config_path)
    assert settings.transports.telegram.voice_transcription_api_key is None


def test_voice_base_url_private_ip_rejected_at_load(tmp_path: Path) -> None:
    """#381: a voice_transcription_base_url pointing at a private/reserved IP
    literal fails fast at config-load (SSRF)."""
    config_path = tmp_path / "untether.toml"
    data = {
        "transport": "telegram",
        "transports": {
            "telegram": {
                "bot_token": "tok",
                "chat_id": 123,
                "allow_any_user": True,
                "voice_transcription_base_url": "http://169.254.169.254/latest",
            }
        },
    }
    with pytest.raises(ConfigError, match="voice_transcription_base_url"):
        validate_settings_data(data, config_path=config_path)


def test_voice_base_url_allowlisted_private_ip_accepted(tmp_path: Path) -> None:
    """#381: an explicitly allowlisted private range is permitted at load."""
    config_path = tmp_path / "untether.toml"
    data = {
        "transport": "telegram",
        "transports": {
            "telegram": {
                "bot_token": "tok",
                "chat_id": 123,
                "allow_any_user": True,
                "voice_transcription_base_url": "http://10.1.2.3:9000/v1",
                "voice_transcription_url_allowlist": ["10.0.0.0/8"],
            }
        },
    }
    settings = validate_settings_data(data, config_path=config_path)
    tg = settings.transports.telegram
    assert tg.voice_transcription_base_url == "http://10.1.2.3:9000/v1"
    assert tg.voice_transcription_url_allowlist == ["10.0.0.0/8"]


def test_voice_url_allowlist_invalid_entry_rejected(tmp_path: Path) -> None:
    """#381: a malformed allowlist entry is a config error."""
    config_path = tmp_path / "untether.toml"
    data = {
        "transport": "telegram",
        "transports": {
            "telegram": {
                "bot_token": "tok",
                "chat_id": 123,
                "allow_any_user": True,
                "voice_transcription_url_allowlist": ["not-a-cidr"],
            }
        },
    }
    with pytest.raises(ConfigError, match="voice_transcription_url_allowlist"):
        validate_settings_data(data, config_path=config_path)


# ───────────────────────────────────────────────────────────────────────────
# #409 — env allowlist user-extensible config (SecuritySettings extras)
# ───────────────────────────────────────────────────────────────────────────


def test_env_extra_allow_round_trip(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        '[transports.telegram]\nbot_token = "tok"\nchat_id = 123\n'
        "allow_any_user = true\n\n"
        "[security]\n"
        'env_extra_allow = ["OP_SERVICE_ACCOUNT_TOKEN", "DOPPLER_TOKEN"]\n'
        'env_extra_prefix_allow = ["VAULT_", "INFISICAL_"]\n',
        encoding="utf-8",
    )
    settings, _ = load_settings(config_path)
    assert settings.security.env_extra_allow == [
        "OP_SERVICE_ACCOUNT_TOKEN",
        "DOPPLER_TOKEN",
    ]
    assert settings.security.env_extra_prefix_allow == ["VAULT_", "INFISICAL_"]


def test_env_extra_allow_default_empty(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        '[transports.telegram]\nbot_token = "tok"\nchat_id = 123\n'
        "allow_any_user = true\n",
        encoding="utf-8",
    )
    settings, _ = load_settings(config_path)
    assert settings.security.env_extra_allow == []
    assert settings.security.env_extra_prefix_allow == []


def test_env_extra_allow_rejects_empty_string(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        '[transports.telegram]\nbot_token = "tok"\nchat_id = 123\n\n'
        "[security]\n"
        'env_extra_allow = [""]\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="env_extra_allow"):
        load_settings(config_path)


def test_env_extra_allow_rejects_whitespace_only(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        '[transports.telegram]\nbot_token = "tok"\nchat_id = 123\n\n'
        "[security]\n"
        'env_extra_allow = ["   "]\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="env_extra_allow"):
        load_settings(config_path)


def test_env_extra_allow_rejects_lowercase(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        '[transports.telegram]\nbot_token = "tok"\nchat_id = 123\n\n'
        "[security]\n"
        'env_extra_allow = ["my_token"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="env_extra_allow"):
        load_settings(config_path)


def test_env_extra_allow_rejects_leading_digit(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        '[transports.telegram]\nbot_token = "tok"\nchat_id = 123\n\n'
        "[security]\n"
        'env_extra_allow = ["1_BAD"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="env_extra_allow"):
        load_settings(config_path)


def test_env_extra_allow_rejects_spaces(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        '[transports.telegram]\nbot_token = "tok"\nchat_id = 123\n\n'
        "[security]\n"
        'env_extra_allow = ["TOK EN"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="env_extra_allow"):
        load_settings(config_path)


def test_env_extra_prefix_allow_validates_names(tmp_path: Path) -> None:
    """Prefix entries must match the same env-var name shape."""
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        '[transports.telegram]\nbot_token = "tok"\nchat_id = 123\n'
        "allow_any_user = true\n\n"
        "[security]\n"
        'env_extra_prefix_allow = ["bad-prefix"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="env_extra_prefix_allow"):
        load_settings(config_path)


# ───────────────────────────────────────────────────────────────────────────
# #377 — startup-block on empty `allowed_user_ids` (insecure default)
# ───────────────────────────────────────────────────────────────────────────


def test_empty_allowed_users_blocks_startup(tmp_path: Path) -> None:
    """#377: empty allowlist + no opt-out is a hard ConfigError at load time."""
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        '[transports.telegram]\nbot_token = "tok"\nchat_id = 123\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="allowed_user_ids is empty"):
        load_settings(config_path)


def test_allow_any_user_overrides_block(tmp_path: Path) -> None:
    """#377: explicit `allow_any_user = true` lets the empty allowlist load."""
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        '[transports.telegram]\nbot_token = "tok"\nchat_id = 123\n'
        "allow_any_user = true\n",
        encoding="utf-8",
    )
    settings, _ = load_settings(config_path)
    assert settings.transports.telegram.allowed_user_ids == []
    assert settings.transports.telegram.allow_any_user is True


def test_non_empty_allowed_users_loads(tmp_path: Path) -> None:
    """#377: a populated allowlist loads without needing the opt-out."""
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        '[transports.telegram]\nbot_token = "tok"\nchat_id = 123\n'
        "allowed_user_ids = [42, 99]\n",
        encoding="utf-8",
    )
    settings, _ = load_settings(config_path)
    assert settings.transports.telegram.allowed_user_ids == [42, 99]
    assert settings.transports.telegram.allow_any_user is False


def test_allow_any_user_with_populated_allowlist_still_loads(tmp_path: Path) -> None:
    """#377: setting both is fine — the validator is only there to prevent the
    silent insecure default of empty + False."""
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        '[transports.telegram]\nbot_token = "tok"\nchat_id = 123\n'
        "allowed_user_ids = [42]\n"
        "allow_any_user = true\n",
        encoding="utf-8",
    )
    settings, _ = load_settings(config_path)
    assert settings.transports.telegram.allowed_user_ids == [42]
    assert settings.transports.telegram.allow_any_user is True


def test_require_telegram_rejects_non_telegram_transport(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    settings = UntetherSettings.model_validate(
        {
            "transport": "discord",
            "transports": {
                "telegram": {
                    "bot_token": "token",
                    "chat_id": 123,
                    "allow_any_user": True,
                }
            },
        }
    )
    with pytest.raises(ConfigError, match="Unsupported transport"):
        require_telegram(settings, config_path)


def test_load_settings_if_exists_missing(tmp_path: Path) -> None:
    config_path = tmp_path / "missing.toml"
    assert load_settings_if_exists(config_path) is None


def test_load_settings_missing_file(tmp_path: Path) -> None:
    config_path = tmp_path / "missing.toml"
    with pytest.raises(ConfigError, match="Missing config file"):
        load_settings(config_path)


def test_load_settings_if_exists_loads(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        'transport = "telegram"\n\n[transports.telegram]\n'
        'bot_token = "token"\nchat_id = 123\nallow_any_user = true\n',
        encoding="utf-8",
    )

    loaded = load_settings_if_exists(config_path)
    assert loaded is not None
    settings, loaded_path = loaded
    assert loaded_path == config_path


def test_load_settings_if_exists_rejects_non_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config_dir"
    config_path.mkdir()
    with pytest.raises(ConfigError, match="exists but is not a file"):
        load_settings_if_exists(config_path)


def test_load_settings_rejects_non_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config_dir"
    config_path.mkdir()
    with pytest.raises(ConfigError, match="exists but is not a file"):
        load_settings(config_path)


# ---------------------------------------------------------------------------
# FooterSettings tests
# ---------------------------------------------------------------------------


def test_footer_defaults() -> None:
    from untether.settings import FooterSettings

    footer = FooterSettings()
    assert footer.show_api_cost is True
    assert footer.show_subscription_usage is False


def test_footer_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        'transport = "telegram"\n\n'
        "[transports.telegram]\n"
        'bot_token = "token"\n'
        "chat_id = 123\n\n"
        "allow_any_user = true\n"
        "[footer]\n"
        "show_api_cost = false\n"
        "show_subscription_usage = true\n",
        encoding="utf-8",
    )

    settings, _ = load_settings(config_path)
    assert settings.footer.show_api_cost is False
    assert settings.footer.show_subscription_usage is True


def test_footer_rejects_extra_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    data = {
        "transport": "telegram",
        "transports": {
            "telegram": {"bot_token": "token", "chat_id": 123, "allow_any_user": True}
        },
        "footer": {"show_api_cost": True, "bogus_key": True},
    }
    with pytest.raises(ConfigError, match="bogus_key"):
        validate_settings_data(data, config_path=config_path)


# ---------------------------------------------------------------------------
# PreambleSettings tests
# ---------------------------------------------------------------------------


def test_preamble_defaults() -> None:
    from untether.settings import PreambleSettings

    preamble = PreambleSettings()
    assert preamble.enabled is True
    assert preamble.text is None


def test_preamble_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        'transport = "telegram"\n\n'
        "[transports.telegram]\n"
        'bot_token = "token"\n'
        "chat_id = 123\n\n"
        "allow_any_user = true\n"
        "[preamble]\n"
        "enabled = false\n"
        'text = "Custom preamble"\n',
        encoding="utf-8",
    )

    settings, _ = load_settings(config_path)
    assert settings.preamble.enabled is False
    assert settings.preamble.text == "Custom preamble"


def test_preamble_rejects_extra_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    data = {
        "transport": "telegram",
        "transports": {
            "telegram": {"bot_token": "token", "chat_id": 123, "allow_any_user": True}
        },
        "preamble": {"enabled": True, "bogus_key": True},
    }
    with pytest.raises(ConfigError, match="bogus_key"):
        validate_settings_data(data, config_path=config_path)


# ---------------------------------------------------------------------------
# ProgressSettings field validation
# ---------------------------------------------------------------------------


def test_progress_min_render_interval_defaults(tmp_path: Path) -> None:
    data = {
        "transport": "telegram",
        "transports": {
            "telegram": {"bot_token": "tok", "chat_id": 1, "allow_any_user": True}
        },
    }
    settings = validate_settings_data(data, config_path=tmp_path / "c.toml")
    assert settings.progress.min_render_interval == 2.0


def test_progress_group_chat_rps_defaults(tmp_path: Path) -> None:
    data = {
        "transport": "telegram",
        "transports": {
            "telegram": {"bot_token": "tok", "chat_id": 1, "allow_any_user": True}
        },
    }
    settings = validate_settings_data(data, config_path=tmp_path / "c.toml")
    assert settings.progress.group_chat_rps == pytest.approx(20.0 / 60.0)


def test_progress_min_render_interval_custom(tmp_path: Path) -> None:
    data = {
        "transport": "telegram",
        "transports": {
            "telegram": {"bot_token": "tok", "chat_id": 1, "allow_any_user": True}
        },
        "progress": {"min_render_interval": 5.0},
    }
    settings = validate_settings_data(data, config_path=tmp_path / "c.toml")
    assert settings.progress.min_render_interval == 5.0


def test_progress_group_chat_rps_custom(tmp_path: Path) -> None:
    data = {
        "transport": "telegram",
        "transports": {
            "telegram": {"bot_token": "tok", "chat_id": 1, "allow_any_user": True}
        },
        "progress": {"group_chat_rps": 0.5},
    }
    settings = validate_settings_data(data, config_path=tmp_path / "c.toml")
    assert settings.progress.group_chat_rps == 0.5


def test_progress_min_render_interval_rejects_negative(tmp_path: Path) -> None:
    data = {
        "transport": "telegram",
        "transports": {
            "telegram": {"bot_token": "tok", "chat_id": 1, "allow_any_user": True}
        },
        "progress": {"min_render_interval": -1.0},
    }
    with pytest.raises(ConfigError):
        validate_settings_data(data, config_path=tmp_path / "c.toml")


def test_progress_group_chat_rps_rejects_zero(tmp_path: Path) -> None:
    data = {
        "transport": "telegram",
        "transports": {
            "telegram": {"bot_token": "tok", "chat_id": 1, "allow_any_user": True}
        },
        "progress": {"group_chat_rps": 0},
    }
    with pytest.raises(ConfigError):
        validate_settings_data(data, config_path=tmp_path / "c.toml")


# ---------------------------------------------------------------------------
# TelegramFilesSettings outbox config tests
# ---------------------------------------------------------------------------


def test_files_outbox_defaults() -> None:
    from untether.settings import TelegramFilesSettings

    cfg = TelegramFilesSettings()
    assert cfg.outbox_enabled is True
    assert cfg.outbox_dir == ".untether-outbox"
    assert cfg.outbox_max_files == 10
    assert cfg.outbox_cleanup is True


def test_files_outbox_dir_rejects_absolute() -> None:
    from pydantic import ValidationError

    from untether.settings import TelegramFilesSettings

    with pytest.raises(ValidationError, match="relative path"):
        TelegramFilesSettings(outbox_dir="/tmp/outbox")


def test_files_outbox_max_files_range() -> None:
    from pydantic import ValidationError

    from untether.settings import TelegramFilesSettings

    with pytest.raises(ValidationError):
        TelegramFilesSettings(outbox_max_files=0)
    with pytest.raises(ValidationError):
        TelegramFilesSettings(outbox_max_files=51)


# ── AutoContinueSettings ──


def test_auto_continue_settings_defaults() -> None:
    from untether.settings import AutoContinueSettings

    s = AutoContinueSettings()
    assert s.enabled is True
    assert s.max_retries == 1


def test_auto_continue_max_retries_bounds() -> None:
    from pydantic import ValidationError

    from untether.settings import AutoContinueSettings

    with pytest.raises(ValidationError):
        AutoContinueSettings(max_retries=-1)
    with pytest.raises(ValidationError):
        AutoContinueSettings(max_retries=4)
    # Boundary values should pass
    assert AutoContinueSettings(max_retries=0).max_retries == 0
    assert AutoContinueSettings(max_retries=3).max_retries == 3


# ---------------------------------------------------------------------------
# #350 — pre-spawn RAM guard settings
# ---------------------------------------------------------------------------


def test_watchdog_prespawn_ram_defaults() -> None:
    from untether.settings import WatchdogSettings

    ws = WatchdogSettings()
    assert ws.prespawn_ram_warn_mb == 2000
    assert ws.prespawn_ram_block_mb == 500


def test_watchdog_prespawn_ram_ordering_enforced() -> None:
    """warn must sit above block when both are active — otherwise the warn
    tier is unreachable and the config is ambiguous."""
    from pydantic import ValidationError

    from untether.settings import WatchdogSettings

    # warn == block → invalid
    with pytest.raises(ValidationError, match="prespawn_ram_warn_mb must be > "):
        WatchdogSettings(prespawn_ram_warn_mb=500, prespawn_ram_block_mb=500)

    # warn < block → invalid
    with pytest.raises(ValidationError, match="prespawn_ram_warn_mb must be > "):
        WatchdogSettings(prespawn_ram_warn_mb=100, prespawn_ram_block_mb=1000)

    # warn > block → valid
    ws = WatchdogSettings(prespawn_ram_warn_mb=3000, prespawn_ram_block_mb=1000)
    assert ws.prespawn_ram_warn_mb == 3000
    assert ws.prespawn_ram_block_mb == 1000

    # either tier = 0 disables that tier → ordering check skipped
    ws = WatchdogSettings(prespawn_ram_warn_mb=0, prespawn_ram_block_mb=1000)
    assert ws.prespawn_ram_warn_mb == 0
    ws = WatchdogSettings(prespawn_ram_warn_mb=1000, prespawn_ram_block_mb=0)
    assert ws.prespawn_ram_block_mb == 0
    ws = WatchdogSettings(prespawn_ram_warn_mb=0, prespawn_ram_block_mb=0)
    assert ws.prespawn_ram_warn_mb == 0  # both zero = guard disabled


def test_watchdog_prespawn_ram_bounds() -> None:
    from pydantic import ValidationError

    from untether.settings import WatchdogSettings

    with pytest.raises(ValidationError):
        WatchdogSettings(prespawn_ram_warn_mb=-1)
    with pytest.raises(ValidationError):
        WatchdogSettings(prespawn_ram_block_mb=65537)


# ---------------------------------------------------------------------------
# LoopSettings (#289) — Untether-side observation of /loop / ScheduleWakeup


def test_loop_settings_defaults_off() -> None:
    """[loop] is opt-in; default state is exactly the v0.35.3 behaviour."""

    from untether.settings import LoopSettings

    s = LoopSettings()
    assert s.enabled is False
    assert s.inline_threshold_seconds == 300
    assert s.redundancy_check_interval == 30
    assert s.max_iterations == 20
    assert s.max_total_duration_hours == 4
    assert s.min_interval_seconds == 60
    assert s.expiry_days == 7


def test_loop_settings_load_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        'transport = "telegram"\n\n'
        "[transports.telegram]\n"
        'bot_token = "token"\n'
        "chat_id = 123\n"
        "allow_any_user = true\n\n"
        "[loop]\n"
        "enabled = true\n"
        "max_iterations = 50\n"
        "expiry_days = 14\n",
        encoding="utf-8",
    )

    settings, _ = load_settings(config_path)

    assert settings.loop.enabled is True
    assert settings.loop.max_iterations == 50
    assert settings.loop.expiry_days == 14
    # Untouched keys keep defaults:
    assert settings.loop.min_interval_seconds == 60


def test_loop_settings_min_interval_floor() -> None:
    from pydantic import ValidationError

    from untether.settings import LoopSettings

    with pytest.raises(ValidationError):
        LoopSettings(min_interval_seconds=30)  # floor is 60


def test_loop_settings_max_iterations_bounds() -> None:
    from pydantic import ValidationError

    from untether.settings import LoopSettings

    with pytest.raises(ValidationError):
        LoopSettings(max_iterations=0)
    with pytest.raises(ValidationError):
        LoopSettings(max_iterations=10001)


def test_loop_settings_max_duration_bounds() -> None:
    from pydantic import ValidationError

    from untether.settings import LoopSettings

    with pytest.raises(ValidationError):
        LoopSettings(max_total_duration_hours=0)
    with pytest.raises(ValidationError):
        LoopSettings(max_total_duration_hours=169)


def test_loop_settings_expiry_days_bounds() -> None:
    from pydantic import ValidationError

    from untether.settings import LoopSettings

    with pytest.raises(ValidationError):
        LoopSettings(expiry_days=0)
    with pytest.raises(ValidationError):
        LoopSettings(expiry_days=31)


def test_loop_settings_rejects_unknown_keys() -> None:
    from pydantic import ValidationError

    from untether.settings import LoopSettings

    with pytest.raises(ValidationError):
        LoopSettings(budget_per_loop_usd=5.0)  # cost caps live in [cost_budget]


def test_594_voice_key_with_embedded_newline_rejected(tmp_path: Path) -> None:
    """#594: a header-illegal api key (e.g. two keys concatenated with a
    newline by a credential manager) must fail at config load with a
    diagnosable error — not hours later as APIConnectionError("Connection
    error.") on every transcription attempt."""
    config_path = tmp_path / "untether.toml"
    data = {
        "transport": "telegram",
        "transports": {
            "telegram": {
                "bot_token": "tok",
                "chat_id": 123,
                "allow_any_user": True,
                "voice_transcription": True,
                "voice_transcription_api_key": "gsk_aaaa\ngsk_bbbb",
            }
        },
    }
    with pytest.raises(ConfigError, match="control characters"):
        validate_settings_data(data, config_path=config_path)


def test_594_voice_key_with_non_latin1_rejected(tmp_path: Path) -> None:
    """#594: characters that cannot be encoded into an HTTP header are
    rejected at config load."""
    config_path = tmp_path / "untether.toml"
    data = {
        "transport": "telegram",
        "transports": {
            "telegram": {
                "bot_token": "tok",
                "chat_id": 123,
                "allow_any_user": True,
                "voice_transcription": True,
                "voice_transcription_api_key": "gsk_ключ",
            }
        },
    }
    with pytest.raises(ConfigError, match="Authorization header"):
        validate_settings_data(data, config_path=config_path)


def test_594_normal_voice_key_still_accepted(tmp_path: Path) -> None:
    """#594: ordinary ASCII keys are unaffected by the new validation."""
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        "[transports.telegram]\n"
        'bot_token = "tok"\n'
        "chat_id = 123\n"
        "allow_any_user = true\n"
        "voice_transcription = true\n"
        'voice_transcription_api_key = "gsk_normal-Key_1234567890"\n',
        encoding="utf-8",
    )
    settings, _ = load_settings(config_path)
    key = settings.transports.telegram.voice_transcription_api_key
    assert key is not None
    assert key.get_secret_value() == "gsk_normal-Key_1234567890"
