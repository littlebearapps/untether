from __future__ import annotations

import tomllib
from pathlib import Path

LOCAL_CONFIG_NAME = Path(".codex") / "takopi.toml"
HOME_CONFIG_PATH = Path.home() / ".codex" / "takopi.toml"


class ConfigError(RuntimeError):
    pass


def _config_candidates() -> list[Path]:
    candidates = [Path.cwd() / LOCAL_CONFIG_NAME, HOME_CONFIG_PATH]
    if candidates[0] == candidates[1]:
        return [candidates[0]]
    return candidates


def _read_config(cfg_path: Path) -> dict:
    try:
        raw = cfg_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"Missing config file {cfg_path}.") from None
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

    candidates = _config_candidates()
    for candidate in candidates:
        if candidate.is_file():
            return _read_config(candidate), candidate

    if len(candidates) == 1:
        raise ConfigError("Missing takopi config.")
    raise ConfigError("Missing takopi config.")
