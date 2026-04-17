from pathlib import Path

import pytest

from untether.config import ConfigError
from untether.settings import UntetherSettings, validate_settings_data


def test_settings_strips_and_expands_transport_config(tmp_path: Path) -> None:
    settings = UntetherSettings.model_validate(
        {
            "transport": " telegram ",
            "plugins": {"enabled": [" foo "]},
            "transports": {"telegram": {"bot_token": "  token  ", "chat_id": 123}},
        }
    )

    assert settings.transport == "telegram"
    assert settings.plugins.enabled == ["foo"]
    # #196: bot_token is SecretStr
    assert settings.transports.telegram.bot_token.get_secret_value() == "token"


def test_settings_rejects_bool_chat_id(tmp_path: Path) -> None:
    data = {
        "transport": "telegram",
        "transports": {"telegram": {"bot_token": "token", "chat_id": True}},
    }

    with pytest.raises(ConfigError, match="chat_id"):
        validate_settings_data(data, config_path=tmp_path / "untether.toml")
