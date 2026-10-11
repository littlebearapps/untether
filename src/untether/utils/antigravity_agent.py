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

agy picks an agent by name and also loads agents from the project's
``.agents/`` folders, so a repo could ship its own ``untether-workspace``.
``project_defines_agent`` looks for one and the run then goes without
``--agent`` rather than risk running the project's copy under our name.

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


_MAX_WALK_ENTRIES = 2000
_MAX_WALK_DEPTH = 8
_MAX_FILE_BYTES = 256 * 1024
_MAX_TOTAL_BYTES = 2 * 1024 * 1024
_NAME_FOLDED = WORKSPACE_AGENT_NAME.casefold()
_NAME_BYTES = WORKSPACE_AGENT_NAME.encode()
# Where agy reads an agent's name from: a Markdown front matter or a JSON
# manifest (``agents.json``, a plugin's manifest).
_DEFINITION_SUFFIXES = (".md", ".json")


class _Unchecked(Exception):
    """The project's ``.agents/`` couldn't be checked in full."""


def project_defines_agent(cwd: Path | None) -> bool:
    """True when the project may carry its own agent called
    ``untether-workspace`` (#1037): under any ``.agents/`` agy loads for
    *cwd* (plugins included), an entry of that name, or a ``.md`` / ``.json``
    file that mentions it (the name lives in the front matter, not the file
    name).

    Fails closed: a link anywhere under ``.agents/``, an unreadable entry, or
    a tree past the entry, depth or byte bounds counts as a match, so the
    flag is dropped. The bounds are enforced entry by entry, before anything
    more is listed or read. With no project there is nothing to run in."""
    if cwd is None:
        return True
    try:
        root = antigravity_scan.find_project_root(cwd)
        parents = antigravity_scan._agents_parents(cwd, root)
    except OSError:
        _warn("project_unchecked", cwd)
        return True
    budget = {"entries": _MAX_WALK_ENTRIES, "bytes": _MAX_TOTAL_BYTES}
    for parent in parents:
        top = parent / ".agents"
        try:
            if not os.path.lexists(top):
                continue
            if top.is_symlink() or not top.is_dir():
                raise _Unchecked
            found = _tree_names_agent(top, budget, depth=0)
        except (_Unchecked, OSError):
            _warn("project_unchecked", top)
            return True
        if found:
            _warn("project_agent", top)
            return True
    return False


def _tree_names_agent(folder: Path, budget: dict[str, int], *, depth: int) -> bool:
    if depth > _MAX_WALK_DEPTH:
        raise _Unchecked
    subfolders: list[Path] = []
    with os.scandir(folder) as entries:
        for entry in entries:
            budget["entries"] -= 1
            if budget["entries"] < 0:
                raise _Unchecked
            if entry.is_symlink():
                raise _Unchecked
            folded = entry.name.casefold()
            if folded in (_NAME_FOLDED, f"{_NAME_FOLDED}.md"):
                return True
            if entry.is_dir(follow_symlinks=False):
                subfolders.append(Path(entry.path))
            elif folded.endswith(_DEFINITION_SUFFIXES):
                if not entry.is_file(follow_symlinks=False):
                    raise _Unchecked
                if _file_names_agent(entry.path, budget):
                    return True
    return any(_tree_names_agent(sub, budget, depth=depth + 1) for sub in subfolders)


def _file_names_agent(path: str, budget: dict[str, int]) -> bool:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(_MAX_FILE_BYTES + 1)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise
    budget["bytes"] -= len(data)
    if len(data) > _MAX_FILE_BYTES or budget["bytes"] < 0:
        raise _Unchecked
    return _NAME_BYTES in data.lower()
