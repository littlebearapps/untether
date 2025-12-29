from __future__ import annotations

import tomllib
from pathlib import Path

from .constants import DEFAULT_CONFIG_PATHS


class ConfigError(RuntimeError):
    pass


def _display_path(path: Path) -> str:
    try:
        cwd = Path.cwd()
        if path.is_relative_to(cwd):
            return f"./{path.relative_to(cwd).as_posix()}"
        home = Path.home()
        if path.is_relative_to(home):
            return f"~/{path.relative_to(home).as_posix()}"
    except Exception:
        return str(path)
    return str(path)


def _missing_config_message(primary: Path, alternate: Path | None = None) -> str:
    if alternate is None:
        header = f"Missing config file `{_display_path(primary)}`."
    else:
        header = (
            f"Missing config file `{_display_path(primary)}` "
            f"(or `{_display_path(alternate)}`)."
        )
    return "\n".join(
        [
            header,
            "Create it with:",
            '  bot_token = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"',
            "  chat_id = 123456789",
        ]
    )


def _read_config(cfg_path: Path) -> dict:
    try:
        raw = cfg_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(_missing_config_message(cfg_path)) from None
    except OSError as e:
        raise ConfigError(f"Failed to read config file {cfg_path}: {e}") from e
    try:
        return tomllib.loads(raw)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Malformed TOML in {cfg_path}: {e}") from None


def load_telegram_config(path: str | Path | None = None) -> tuple[dict, Path]:
    if path:
        cfg_path = Path(path).expanduser()
        return _read_config(cfg_path), cfg_path

    local_path, home_path = DEFAULT_CONFIG_PATHS
    for candidate in (local_path, home_path):
        if candidate.is_file():
            return _read_config(candidate), candidate

    raise ConfigError(_missing_config_message(home_path, local_path))
