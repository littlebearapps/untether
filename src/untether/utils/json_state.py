from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from untether.logging import get_logger

logger = get_logger(__name__)


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    indent: int = 2,
    sort_keys: bool = True,
    mode: int | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            if mode is not None:
                os.chmod(tmp_path, mode)  # before any content lands
            json.dump(payload, handle, indent=indent, sort_keys=sort_keys)
            handle.write("\n")
    except (TypeError, ValueError, OSError) as exc:
        logger.error(
            "json_state.write.serialize_failed",
            path=str(path),
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        raise
    try:
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.error(
            "json_state.write.replace_failed",
            path=str(path),
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        raise
