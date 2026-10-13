"""Untether's no-shell agent for Antigravity Workspace runs (#1037).

In Workspace mode agy's print mode soft-denies a shell command and ends the
turn there, and the default agent often opens with one (``ls -la``), so the
user's whole reply was the blocked notice. agy has no per-run system prompt
and no way to drop a tool from the default agent, but a custom agent's
``tools:`` list is the model's whole toolset (agy 1.3.3, probed 2026-10-11).
A Workspace run therefore names this agent with ``--agent``: no shell, web,
MCP or subagent tool exists for the model to reach for, which is narrower
than Workspace's promise, never wider.

#980 (R3-2): the agent also has ``ask_question``. With Untether's agy plugin
installed the question becomes Telegram buttons; without it agy skips the
unanswered question and the turn carries on (probed on agy 1.3.4,
2026-10-13, n = 2: the model went on to write its file and reply), unlike a
blocked shell call, which ends the turn.

The file lives in agy's documented user-level agent folder
(``~/.gemini/config/agents/<name>/agent.md``) and does nothing unless
``--agent`` names it. It is rewritten whenever it differs from ``AGENT_MD``,
and ``--agent`` is passed only when the file on disk is exactly that text.

agy picks an agent by name and also loads agents from the project's
``.agents/`` folders, so a repo could ship its own ``untether-workspace``.
Untether doesn't try to recognise one (that would mean parsing agent files
the way agy does): ``project_defines_agent`` asks only whether the project
can supply agents at all, and if so the run goes without ``--agent``.

agy keeps a conversation on the agent it started with and ignores
``--agent`` on a resume, so a chat switched to Full access keeps the
no-shell toolset until ``/new``. The agent's own text and ``/config`` say so.
The text also covers cron and webhook runs, which never take Full access
from the chat (#1055): there is one file and no per-run system prompt, so
it gives both routes rather than chat-only advice.
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
  - ask_question
mainAgent: true
subagent: false
---

# System Prompt
You are a coding agent working in the user's project. This conversation runs in Workspace mode: you can read, search, create and edit files inside the project with your file tools. Shell commands, web access and MCP tools are not available, and neither are files outside the project.
When you need the user's choice, ask with `ask_question`. If no answer comes back, carry on with a sensible default and say what you assumed.

# Working without a shell
- To look around, use `list_dir`, `find_by_name`, `grep_search` and `view_file`.
- To create, copy or change a file, use `write_to_file`, `replace_file_content` or `multi_replace_file_content`.
- If a request needs something you cannot do here (running a command, deleting or renaming a file, fetching a URL), do the part you can, then say plainly what was left and that it needs Full access. In a chat: /config, then Permission mode, then Full access, and then /new, because a conversation that started in Workspace mode stays in it. Scheduled and webhook runs don't follow the chat's setting: a cron gets Full access only from `permission_mode = "full"` on the cron itself, and a webhook never does, so it has to be done from the chat.
- Answer simple questions directly without exploring the project first.
"""

# #1055: agy has no per-run system prompt and nothing in a run tells the
# model it was started by a cron or webhook, so with only the agent file's
# two routes it always gave the chat's (/config → Full access → /new),
# which a scheduled run never takes (2/2 live cron fires, rc4 QA). An
# unattended Workspace run's prompt therefore starts with this line.
_SCHEDULED_HEAD = (
    "[Untether] This is a scheduled {kind} run, not a chat: nobody is "
    "reading along, and the chat's /config settings don't apply to it. It "
    "runs in Workspace mode, so it can't run shell commands, delete files or "
    "fetch URLs."
)
_SCHEDULED_CRON = (
    " If the task needs any of that, do the part you can and say that this "
    'cron needs `permission_mode = "full"` on its own entry in untether.toml '
    "(not a chat setting)."
)
_SCHEDULED_WEBHOOK = (
    " If the task needs any of that, do the part you can and say that a "
    "webhook run never gets Full access, so that part has to be done from the "
    "chat in Full access."
)


# #980: a trigger whose own ``permission_mode`` is ``ask`` runs Ask me with
# nobody to approve: the gate refuses everything off its safe list at once.
# Said up front so the model doesn't spend the run on refused calls.
_SCHEDULED_ASK = (
    "[Untether] This is a scheduled {kind} run, not a chat: nobody is "
    "reading along to approve anything. It runs in Ask me mode, so it can "
    "read and edit files in this project, but shell commands, web, MCP "
    "tools, subagents and files outside the project are refused. If the "
    "task needs any of that, do the part you can and say what was left."
)


def scheduled_run_note(trigger: str | None, mode: str | None) -> str | None:
    """#1055: the line an unattended Workspace (or trigger-level Ask me,
    #980) run's prompt starts with, or None (a chat run, Full access, Plan
    first)."""
    if not trigger or not trigger.startswith(("cron:", "webhook:")):
        return None
    kind = "cron" if trigger.startswith("cron:") else "webhook"
    if mode == "ask":
        return _SCHEDULED_ASK.format(kind=kind)
    if mode != "workspace":
        return None
    if trigger.startswith("cron:"):
        return _SCHEDULED_HEAD.format(kind="cron") + _SCHEDULED_CRON
    if trigger.startswith("webhook:"):
        return _SCHEDULED_HEAD.format(kind="webhook") + _SCHEDULED_WEBHOOK
    return None


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


_MAX_ENTRIES = 2000
_NAME_FOLDED = WORKSPACE_AGENT_NAME.casefold()
# The entries of a project's ``.agents/`` through which agy can load an
# agent definition: custom agents, and plugins (which bundle agents).
_AGENT_SOURCES = frozenset({"agents", "agents.json", "plugins", "plugins.json"})


class _Unchecked(Exception):
    """A folder couldn't be checked in full."""


def project_defines_agent(cwd: Path | None) -> bool:
    """True when ``--agent`` might not resolve to Untether's own file (#1037).

    Structural, never content-based, so there is no agent file for Untether
    to read differently from agy: any ``.agents/`` agy loads for *cwd* that
    has an ``agents``, ``agents.json``, ``plugins`` or ``plugins.json`` entry
    (of any kind, empty or not) counts, whatever is inside. Such a project
    runs Workspace on agy's default agent, as before the no-shell agent.

    The user-level agent folder is checked by entry name only (D33: nothing
    under ``~/.gemini`` is opened): a second ``untether-workspace`` there, or
    anything beside our ``agent.md``, also counts.

    Fails closed: an ``.agents`` that is a link or not a folder, one that
    can't be listed or has too many entries, and no project at all."""
    if cwd is None:
        return True
    try:
        parents = antigravity_scan._agents_parents(cwd)
    except OSError:
        _warn("project_unchecked", cwd)
        return True
    budget = _MAX_ENTRIES
    try:
        user_clash, budget = _user_level_clash(budget)
    except (_Unchecked, OSError):
        _warn("user_unchecked", agent_path())
        return True
    if user_clash:
        _warn("user_agent", agent_path())
        return True
    for parent in parents:
        top = parent / ".agents"
        try:
            if not os.path.lexists(top):
                continue
            if top.is_symlink() or not top.is_dir():
                raise _Unchecked
            with os.scandir(top) as entries:
                for entry in entries:
                    budget -= 1
                    if budget < 0:
                        raise _Unchecked
                    if entry.name.casefold() in _AGENT_SOURCES:
                        _warn("project_agents", top)
                        return True
        except (_Unchecked, OSError):
            _warn("project_unchecked", top)
            return True
    return False


def _user_level_clash(budget: int) -> tuple[bool, int]:
    """Entry names only. True for another ``untether-workspace`` in agy's
    user-level agent folder; ``_Unchecked`` when our own folder holds
    anything but a regular ``agent.md``."""
    ours = agent_path()
    agents = ours.parent.parent
    if not os.path.lexists(agents):
        return False, budget
    with os.scandir(agents) as entries:
        for entry in entries:
            budget -= 1
            if budget < 0:
                raise _Unchecked
            folded = entry.name.casefold()
            if folded == f"{_NAME_FOLDED}.md":
                return True, budget
            if folded == _NAME_FOLDED and entry.name != WORKSPACE_AGENT_NAME:
                return True, budget
    if os.path.lexists(ours.parent):
        with os.scandir(ours.parent) as entries:
            for entry in entries:
                if entry.name != ours.name or not entry.is_file(follow_symlinks=False):
                    raise _Unchecked
    return False, budget
