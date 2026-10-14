from __future__ import annotations

from pathlib import Path

import pytest

from untether.config import ConfigError, read_config, write_config


def test_read_write_config_round_trip(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    payload = {
        "default_engine": "codex",
        "projects": {"z80": {"path": "/tmp/repo"}},
    }

    write_config(payload, config_path)
    loaded = read_config(config_path)

    assert loaded == payload


def test_read_config_missing_file(tmp_path: Path) -> None:
    config_path = tmp_path / "missing.toml"
    with pytest.raises(ConfigError, match="Missing config file"):
        read_config(config_path)


def test_read_config_invalid_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text("nope = [", encoding="utf-8")
    with pytest.raises(ConfigError, match="Malformed TOML"):
        read_config(config_path)


def test_read_config_non_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config_dir"
    config_path.mkdir()
    with pytest.raises(ConfigError, match="exists but is not a file"):
        read_config(config_path)


# --- #1049/#1050: atomic writes keep the file's mode; quiet malformed reads ----


def test_write_config_preserves_existing_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text('default_engine = "codex"\n', encoding="utf-8")
    config_path.chmod(0o640)

    write_config({"default_engine": "claude"}, config_path)

    assert config_path.stat().st_mode & 0o777 == 0o640
    assert read_config(config_path) == {"default_engine": "claude"}
    assert not list(tmp_path.glob(".untether.toml.*"))


def test_write_config_new_file_is_owner_only(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    write_config({"default_engine": "codex"}, config_path)
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_read_config_malformed_logs_error_by_default(tmp_path: Path) -> None:
    from structlog.testing import capture_logs

    from untether.config import MalformedConfigError

    config_path = tmp_path / "untether.toml"
    config_path.write_text('a = "unterminated', encoding="utf-8")
    with capture_logs() as logs, pytest.raises(MalformedConfigError) as info:
        read_config(config_path)
    assert "Unterminated string" in info.value.detail
    errors = [r for r in logs if r["event"] == "config.read.toml_error"]
    assert [r["log_level"] for r in errors] == ["error"]


def test_read_config_malformed_quiet_leaves_logging_to_caller(tmp_path: Path) -> None:
    from structlog.testing import capture_logs

    from untether.config import MalformedConfigError

    config_path = tmp_path / "untether.toml"
    config_path.write_text('a = "unterminated', encoding="utf-8")
    with capture_logs() as logs, pytest.raises(MalformedConfigError):
        read_config(config_path, log_toml_errors=False)
    assert not [r for r in logs if r["event"].startswith("config.read")]
