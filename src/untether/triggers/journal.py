"""Machine-readable run journal for cron + webhook runs (#1123).

One JSON line per triggered run in ``triggers_runs.jsonl`` beside
``untether.toml`` (mode 0600, rotated to one ``.1`` generation at
:data:`MAX_BYTES`). Lines carry ids, outcome, cost and throttle counts,
never the prompt or the answer. Writing is best-effort: a failure is logged
and swallowed so the journal can't break a run or the trigger loop.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from ..context import RunContext
from ..logging import get_logger
from ..utils.error_display import redact_url_secrets

logger = get_logger(__name__)

JOURNAL_FILENAME = "triggers_runs.jsonl"
JOURNAL_VERSION = 1
MAX_BYTES = 5 * 1024 * 1024

_path: Path | None = None


def init_journal(config_path: Path) -> None:
    """Point the journal at ``<config dir>/triggers_runs.jsonl``."""
    global _path
    _path = config_path.with_name(JOURNAL_FILENAME)


def reset_journal() -> None:
    """Forget the journal path. Intended for tests."""
    global _path
    _path = None


def _append(path: Path, line: str) -> None:
    try:
        if path.stat().st_size >= MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def record_run(
    context: RunContext | None,
    *,
    outcome: str,
    ok: bool,
    engine: str | None = None,
    error: str | None = None,
    session_id: str | None = None,
    model: str | None = None,
    elapsed_s: float | None = None,
    usage: dict[str, Any] | None = None,
    quiet: bool = False,
    quiet_line: str | None = None,
) -> None:
    """Append one line for a cron/webhook run; a no-op for any other run.

    ``outcome``: ``ok``, ``error``, ``cancelled`` (a run that started), or
    ``refused`` / ``announce_failed`` / ``fetch_aborted`` (nothing ran).
    ``usage`` is the result's usage dict: ``num_turns``, the per-run
    ``total_cost_usd`` and Claude's per-result ``throttle`` delta are read.
    ``quiet`` / ``quiet_line`` (#1220): the run ended quietly, and the
    answer's sentinel line (the only answer text a line ever holds).
    """
    if _path is None or context is None or not context.trigger_source:
        return
    if not context.trigger_source.startswith(("cron:", "webhook:")):
        return
    kind, _, trigger_id = context.trigger_source.partition(":")
    usage = usage or {}
    throttle = usage.get("throttle")
    throttle = throttle if isinstance(throttle, dict) else {}
    finished = time.time()
    record = {
        "v": JOURNAL_VERSION,
        "trigger_kind": kind,
        "trigger_id": trigger_id,
        "run_id": context.run_id,
        "session_id": session_id,
        "project": context.project,
        "engine": engine,
        "model": model,
        "started_at": round(finished - elapsed_s, 3) if elapsed_s is not None else None,
        "finished_at": round(finished, 3),
        "outcome": outcome,
        "ok": ok,
        "error": redact_url_secrets(error)[:500] if error else None,
        "num_turns": usage.get("num_turns"),
        "cost_usd": usage.get("total_cost_usd"),
        "rate_limit_count": throttle.get("rate_limit_count", 0),
        "rate_limit_total_s": throttle.get("rate_limit_total_s", 0.0),
        "api_retry_count": throttle.get("api_retry_count", 0),
        "api_retry_total_s": throttle.get("api_retry_total_s", 0.0),
        "rejected": bool(throttle.get("rejected", False)),
        "quiet": quiet,
        "quiet_line": redact_url_secrets(quiet_line)[:200] if quiet_line else None,
    }
    try:
        _append(_path, json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as exc:
        logger.warning(
            "triggers.journal.write_failed",
            path=str(_path),
            error=str(exc),
            trigger=context.trigger_source,
        )
