"""Tests for UNTETHER_CONFIG_PATH env var support."""

from __future__ import annotations

from pathlib import Path

from untether.config import HOME_CONFIG_PATH, load_or_init_config
from untether.settings import _resolve_config_path, load_settings


ENV_VAR = "UNTETHER_CONFIG_PATH"


class TestResolveConfigPath:
    """Tests for settings._resolve_config_path()."""

    def test_explicit_path_wins_over_env(self, tmp_path: Path, monkeypatch) -> None:
        env_config = tmp_path / "env" / "untether.toml"
        explicit = tmp_path / "explicit" / "untether.toml"
        monkeypatch.setenv(ENV_VAR, str(env_config))

        result = _resolve_config_path(str(explicit))

        assert result == explicit

    def test_env_var_used_when_no_explicit_path(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        env_config = tmp_path / "env" / "untether.toml"
        monkeypatch.setenv(ENV_VAR, str(env_config))

        result = _resolve_config_path(None)

        assert result == env_config

    def test_falls_back_to_home_config(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_VAR, raising=False)

        result = _resolve_config_path(None)

        assert result == HOME_CONFIG_PATH

    def test_env_var_tilde_expanded(self, monkeypatch) -> None:
        monkeypatch.setenv(ENV_VAR, "~/.untether-dev/untether.toml")

        result = _resolve_config_path(None)

        assert result == Path.home() / ".untether-dev" / "untether.toml"


class TestLoadOrInitConfigEnv:
    """Tests for config.load_or_init_config() with env var."""

    def test_env_var_used_when_no_path_arg(self, tmp_path: Path, monkeypatch) -> None:
        env_config = tmp_path / "untether.toml"
        env_config.write_text(
            'transport = "telegram"\n\n'
            "[transports.telegram]\n"
            'bot_token = "tok"\n'
            "chat_id = 1\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(ENV_VAR, str(env_config))

        data, cfg_path = load_or_init_config()

        assert cfg_path == env_config
        assert data["transport"] == "telegram"

    def test_explicit_path_wins_over_env(self, tmp_path: Path, monkeypatch) -> None:
        env_config = tmp_path / "env.toml"
        explicit_config = tmp_path / "explicit.toml"
        explicit_config.write_text(
            'transport = "telegram"\n\n'
            "[transports.telegram]\n"
            'bot_token = "tok"\n'
            "chat_id = 2\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(ENV_VAR, str(env_config))

        data, cfg_path = load_or_init_config(str(explicit_config))

        assert cfg_path == explicit_config
        assert data["transports"]["telegram"]["chat_id"] == 2

    def test_env_var_missing_file_returns_empty(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        env_config = tmp_path / "nonexistent.toml"
        monkeypatch.setenv(ENV_VAR, str(env_config))

        data, cfg_path = load_or_init_config()

        assert cfg_path == env_config
        assert data == {}


class TestLoadSettingsEnv:
    """Tests for settings.load_settings() with env var."""

    def test_env_var_loads_config(self, tmp_path: Path, monkeypatch) -> None:
        env_config = tmp_path / "untether.toml"
        env_config.write_text(
            'transport = "telegram"\n\n'
            "[transports.telegram]\n"
            'bot_token = "devtoken"\n'
            "chat_id = 999\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(ENV_VAR, str(env_config))

        settings, cfg_path = load_settings()

        assert cfg_path == env_config
        assert settings.transports.telegram.bot_token == "devtoken"
        assert settings.transports.telegram.chat_id == 999


class TestOnboardingResolveHomeConfig:
    """Tests for onboarding._resolve_home_config()."""

    def test_env_var_overrides_default(self, tmp_path: Path, monkeypatch) -> None:
        from untether.telegram.onboarding import _resolve_home_config

        env_config = tmp_path / "dev" / "untether.toml"
        monkeypatch.setenv(ENV_VAR, str(env_config))

        result = _resolve_home_config()

        assert result == env_config

    def test_falls_back_to_home_config(self, monkeypatch) -> None:
        from untether.telegram.onboarding import _resolve_home_config

        monkeypatch.delenv(ENV_VAR, raising=False)

        result = _resolve_home_config()

        assert result == HOME_CONFIG_PATH
