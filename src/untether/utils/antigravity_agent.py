"""Untether's no-shell agent for Antigravity Workspace runs (#1037).

In Workspace mode agy's print mode soft-denies a shell command and ends the
turn there, and the default agent often opens with one (``ls -la``), so the
user's whole reply was the blocked notice. agy has no per-run system prompt
and no way to drop a tool from the default agent, but a custom agent's
``tools:`` list is the model's whole toolset (agy 1.3.3, probed 2026-10-11).
A Workspace run therefore names this agent with ``--agent``: no shell, web,
MCP or subagent tool exists for the model to reach for, which is narrower
than Workspace's promise, never wider.

The file lives in agy's documented user-level agent folder
(``~/.gemini/config/agents/<name>/agent.md``) and does nothing unless
``--agent`` names it. It is rewritten whenever it differs from ``AGENT_MD``,
and ``--agent`` is passed only when the file on disk is exactly that text.

agy keeps a conversation on the agent it started with and ignores
``--agent`` on a resume, so a chat switched to Full access keeps the
no-shell toolset until ``/new``. The agent's own text and ``/config`` say so.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

from ..logging import get_logger
from . import antigravity_scan

logger = get_logger(__name__)

WORKSPACE_AGENT_NAME = "untether-workspace"

AGENT_MD = """\
---
name: untether-workspace
description: Untether's Workspace permission mode. Reads and edits files in the project; no shell, web or MCP tools. Managed by Untether - edits are overwritten.
tools:
  - view_file
  - list_dir
  - find_by_name
  - grep_search
  - write_to_file
  - replace_file_content
  - multi_replace_file_content
mainAgent: true
subagent: false
---

# System Prompt
You are a coding agent working in the user's project. This conversation runs in Workspace mode: you can read, search, create and edit files inside the project with your file tools. Shell commands, web access and MCP tools are not available, and neither are files outside the project.

# Working without a shell
- To look around, use `list_dir`, `find_by_name`, `grep_search` and `view_file`.
- To create, copy or change a file, use `write_to_file`, `replace_file_content` or `multi_replace_file_content`.
- If a request needs something you cannot do here (running a command, deleting or renaming a file, fetching a URL), do the part you can, then say plainly what was left. Tell the user that this needs Full access: /config, then Permission mode, then Full access, and then /new, because a conversation that started in Workspace mode stays in it.
- Answer simple questions directly without exploring the project first.
"""

_warned: set[str] = set()


def agent_path() -> Path:
    return (
        antigravity_scan.user_config_dir()
        / "agents"
        / WORKSPACE_AGENT_NAME
        / "agent.md"
    )


def _warn(reason: str, path: Path) -> None:
    if reason in _warned:
        return
    _warned.add(reason)
    logger.warning(
        "antigravity.workspace_agent.unavailable", reason=reason, path=str(path)
    )


def ensure_workspace_agent() -> bool:
    """Make sure the agent file is in place; True when ``--agent`` may be
    passed. Never raises: on any problem the run goes ahead without the
    agent (the old behaviour) and a WARNING says why, once per reason."""
    path = agent_path()
    wanted = AGENT_MD.encode()
    try:
        folder = path.parent
        # Never write through a link someone else put there.
        for part in (folder.parent, folder, path):
            if part.is_symlink():
                _warn("symlink", path)
                return False
        if path.is_file() and path.read_bytes() == wanted:
            return True
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".agent.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(wanted)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError as exc:
        _warn(type(exc).__name__, path)
        return False
    logger.info("antigravity.workspace_agent.installed", path=str(path))
    return True
