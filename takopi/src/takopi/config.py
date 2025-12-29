import tomllib
from pathlib import Path

from .constants import TELEGRAM_CONFIG_PATH


def load_telegram_config(path=None):
    cfg_path = Path(path) if path else TELEGRAM_CONFIG_PATH
    return tomllib.loads(cfg_path.read_text(encoding="utf-8"))
