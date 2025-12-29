from __future__ import annotations

from pathlib import Path

TELEGRAM_HARD_LIMIT = 4096
DEFAULT_CONFIG_PATHS = (
    Path.cwd() / "codex" / "takopi.toml",
    Path.home() / ".codex" / "takopi.toml",
)
