"""#1186: Codex plugin background jobs owned by a Claude session.

The Codex plugin for Claude Code (``codex@openai-codex``) kills a session's
``queued`` / ``running`` ``--background`` jobs from its ``SessionEnd`` hook,
which fires on every Claude process exit — including Untether's idle close of
a live session the user will resume. The hook ignores ``SessionEnd.reason``
and there is no host-side signal, so the live session is held open while such
a job runs.

shortcut: reads the plugin's private 1.0.6 state layout
(``<root>/<workspace-slug>-<sha16>/state.json`` → ``jobs[]`` with ``id``,
``sessionId``, ``status``, ``pid``) — read-only, never written or deleted.
Remove when openai/codex-plugin-cc#822 ships (``SessionEnd`` honours
``reason``), or re-pin if the layout changes. Anything missing, unreadable or
untrusted yields no jobs, i.e. today's behaviour.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterable, Iterator
from pathlib import Path

from .subprocess import _pid_alive

_ACTIVE_STATUSES = frozenset({"queued", "running"})
_MAX_STATE_BYTES = 1024 * 1024


def default_state_roots() -> tuple[Path, ...]:
    """The plugin's ``$CLAUDE_PLUGIN_DATA/state``. Claude Code always sets
    ``CLAUDE_PLUGIN_DATA`` for plugin hooks and commands, so the plugin's
    shared-tmp fallback (``$TMPDIR/codex-companion``, used only when run
    outside Claude Code) is deliberately not read: it is world-writable."""
    return (
        Path.home() / ".claude" / "plugins" / "data" / "codex-openai-codex" / "state",
    )


def _owned_dir(path: Path) -> bool:
    """A real directory (not a symlink) owned by us. Group-writable is allowed:
    under ``~/.claude`` with a umask of 002 it is the user's private group."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()


def _read_state(path: Path) -> object:
    """``state.json`` parsed, or None unless it is a regular file we own of at
    most 1 MiB. ``O_NOFOLLOW`` refuses a symlink; ``O_NONBLOCK`` keeps a FIFO
    from hanging the open (``fstat`` then rejects it)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.getuid()
                or st.st_size > _MAX_STATE_BYTES
            ):
                return None
            raw = fh.read(_MAX_STATE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_STATE_BYTES:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def session_plugin_jobs(
    session_id: str, roots: Iterable[Path] | None = None
) -> list[str]:
    """Ids of this session's queued/running Codex plugin jobs whose process is
    alive. Matched by ``sessionId`` across every workspace dir, so the
    plugin's workspace hash never has to be reproduced."""
    return [job["id"] for job in _session_jobs(session_id, roots)]


def session_plugin_job_labels(
    session_id: str, roots: Iterable[Path] | None = None
) -> list[str]:
    """``"<title> <id>"`` for each job ``session_plugin_jobs`` finds, as
    ``/codex:status`` names it, for the close notice (#1186 step 3). Never
    the prompt ``summary`` (user content)."""
    labels = []
    for job in _session_jobs(session_id, roots):
        title = job.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 40:
            title = "Codex job"
        labels.append(f"{title} {job['id']}")
    return labels


def _session_jobs(session_id: str, roots: Iterable[Path] | None) -> Iterator[dict]:
    for root in default_state_roots() if roots is None else roots:
        if not _owned_dir(root):
            continue
        try:
            state_files = sorted(root.glob("*/state.json"))
        except OSError:
            continue
        for path in state_files:
            data = _read_state(path)
            jobs = data.get("jobs") if isinstance(data, dict) else None
            for job in jobs if isinstance(jobs, list) else ():
                if not isinstance(job, dict):
                    continue
                pid = job.get("pid")
                job_id = job.get("id")
                if (
                    job.get("sessionId") == session_id
                    and job.get("status") in _ACTIVE_STATUSES
                    and isinstance(job_id, str)
                    and type(pid) is int
                    and pid > 0
                    and _pid_alive(pid)
                ):
                    yield job
