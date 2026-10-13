"""Antigravity "Ask me": which agy tool calls run on their own, and the
approval card for the rest (#980, phase 14).

Ask me runs agy with ``--dangerously-skip-permissions`` on its default agent,
so Untether's ``PreToolUse`` gate is the only thing that lets a tool run.
This module is the policy half of that gate — pure, no I/O beyond resolving
paths:

- :func:`classify` — reads (``view_file``, ``list_dir`` …) and edits inside
  the project (or the conversation's own artifact folder) pass on Untether's
  safe list; everything else asks: shell, web, MCP, browser, subagents,
  timers, files outside the project, protected files inside it (``.agents/``,
  ``.git/``, ``.claude/``, ``.env`` …), Untether's own code and any tool this
  version doesn't know. ``ask_question`` is the phase 13 question card and is
  never classified here.
- :func:`card_title` — ``Permission Request [agy] - <what>`` plus a diff or
  command preview built from agy's own ``toolCall.args`` (reusing
  :func:`untether.interactive.format_diff_preview`), the subagent it came
  from, and agy's own one-line summary, escaped and last (model text).
- "Always allow this command" grants (:func:`grant` / :func:`granted`) —
  exact ``CommandLine`` + ``Cwd``, per chat and agy conversation, in memory
  only; never used by unattended runs.

Tool names and argument shapes: agy 1.3.3/1.3.4 ``init.tools`` and hook
payloads (probes E2, P-B4, P-B6, P-H8). New or unknown tools ask.
"""

from __future__ import annotations

import os
import sys
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..background_status import escape_markdown
from ..interactive import format_diff_preview

# Never need a card: they read, list, wait or end the turn.
READ_TOOLS = frozenset(
    {
        "view_file",
        "list_dir",
        "find_by_name",
        "grep_search",
        "command_status",
        "list_permissions",
        "wait",
        "wait_5_seconds",
        "finish",
    }
)
# Path-checked: in the project (or the artifact folder) and not protected.
EDIT_TOOLS = frozenset(
    {"write_to_file", "replace_file_content", "multi_replace_file_content"}
)
_EDIT_PATH_KEYS = ("TargetFile", "AbsolutePath")

# How a card names a tool ("Permission Request [agy] - shell command").
_LABELS: dict[str, str] = {
    "run_command": "shell command",
    "send_command_input": "input to a running command",
    "notebook_execution": "run a notebook",
    "manage_task": "manage a background task",
    "schedule": "set a timer",
    "run_workflow": "run a workflow",
    "read_url_content": "fetch a web page",
    "search_web": "web search",
    "search_marketplace": "marketplace search",
    "call_mcp_tool": "MCP tool",
    "list_resources": "list MCP resources",
    "read_resource": "read an MCP resource",
    "list_plugin_accounts": "list plugin accounts",
    "invoke_subagent": "start subagents",
    "browser_subagent": "start a browser subagent",
    "define_subagent": "define a subagent",
    "manage_subagents": "manage subagents",
    "send_message": "message another agent",
    "manage_inbox": "agent inbox",
    "generate_image": "generate an image",
    "delete_knowledge": "delete saved knowledge",
    "write_to_file": "write a file",
    "replace_file_content": "edit a file",
    "multi_replace_file_content": "edit a file",
    "sed_file": "edit a file (sed)",
    "notebook_edit": "edit a notebook",
}

# Inside the project these always ask (REVIEW B2, REVIEW-2 M4): planted agy
# config, git internals, Untether's outbox (it sends files to Telegram), and
# files another engine or tool executes or reads as instructions.
_PROTECTED_DIRS = (
    ".agents",
    ".git",
    ".gemini",
    ".untether-outbox",
    ".claude",
    ".husky",
    ".github/workflows",
)
_PROTECTED_FILES = frozenset(
    {
        ".mcp.json",
        ".envrc",
        ".vscode/tasks.json",
        ".pre-commit-config.yaml",
        "claude.md",
        "agents.md",
        "gemini.md",
    }
)
# Untether's default file deny globs (``[transports.telegram.files]``):
# secrets and credentials, at any depth.
_SECRET_GLOBS = (
    ".env",
    "**/.env",
    "**/.env.*",
    ".envrc",
    "**/.envrc",
    "**/*.pem",
    "**/*.key",
    "**/id_rsa",
    "**/id_ed25519",
    "**/.ssh/**",
    "**/.netrc",
    "**/.npmrc",
    "**/.pypirc",
)

_PREVIEW_PAIRS = 3
_PREVIEW_VALUE_CHARS = 80
_SUMMARY_CHARS = 160
_SECRET_KEY_WORDS = ("token", "secret", "password", "passwd", "apikey", "api_key")
_MASK = "•••"
_MULTI_CHUNKS_SHOWN = 2


@dataclass(frozen=True, slots=True)
class Verdict:
    """``allow``: the safe list lets it run. Otherwise ``why`` says what the
    card should flag (``outside`` / ``protected`` / ``self`` / ``mismatch``
    / ``preview`` for an edit; ``tool`` for everything else)."""

    allow: bool
    why: str = "tool"


ALLOW = Verdict(True, "safe")


def _fold(path: str) -> str:
    # macOS file systems are case-insensitive: ``.Agents/`` is ``.agents/``.
    return path.casefold() if sys.platform == "darwin" else path


def _real(path: str | Path) -> str:
    """``realpath`` that resolves the existing part and appends the rest, so
    a new file under a symlinked folder resolves to where it would land."""
    return os.path.realpath(os.path.expanduser(str(path)))


def _within(child: str, parent: str) -> bool:
    child, parent = _fold(child), _fold(parent)
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def edit_target(args: Any) -> str | None:
    if not isinstance(args, dict):
        return None
    for key in _EDIT_PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def artifact_dir(conversation_id: str) -> Path:
    """agy's own folder for a conversation's ``task.md`` / plans."""
    return Path.home() / ".gemini" / "antigravity-cli" / "brain" / conversation_id


def self_protected_paths() -> tuple[str, ...]:
    """REVIEW-2 B3: Untether's package, its venv and the gate plugin. An
    edit there could switch the gate off, wherever the project is."""
    import untether

    from . import gate_plugin

    return (
        _real(Path(untether.__file__).parent),
        _real(sys.prefix),
        _real(gate_plugin.plugin_dir()),
    )


def _protected_rel(rel: str) -> bool:
    from ..telegram.files import deny_reason

    folded = rel.casefold()
    if folded in _PROTECTED_FILES:
        return True
    for folder in _PROTECTED_DIRS:
        if folded == folder or folded.startswith(folder + "/"):
            return True
    # ``.git`` anywhere (submodules), and the secrets globs.
    return deny_reason(Path(rel), _SECRET_GLOBS) is not None


def classify_edit(
    args: Any,
    *,
    root: Path | None,
    conversation_id: str | None,
    workspace_paths: Any = None,
) -> Verdict:
    """An edit tool's target: inside the project (or the conversation's
    artifact folder) and not protected → allowed; anything else asks."""
    target = edit_target(args)
    if target is None or root is None:
        return Verdict(False, "outside")
    base = _real(root)
    if isinstance(workspace_paths, list) and workspace_paths:
        first = workspace_paths[0]
        if not isinstance(first, str) or _fold(_real(first)) != _fold(base):
            return Verdict(False, "mismatch")
    path = target if os.path.isabs(os.path.expanduser(target)) else str(root / target)
    real = _real(path)
    for protected in self_protected_paths():
        if _within(real, protected):
            return Verdict(False, "self")
    if conversation_id and _within(real, _real(artifact_dir(conversation_id))):
        return ALLOW
    if not _within(real, base):
        return Verdict(False, "outside")
    rel = os.path.relpath(real, base)
    if rel == "." or _protected_rel(PurePosixPath(*Path(rel).parts).as_posix()):
        return Verdict(False, "protected")
    return ALLOW


def classify(
    tool: str,
    args: Any,
    *,
    root: Path | None,
    conversation_id: str | None,
    workspace_paths: Any = None,
    diff_preview: bool | None = None,
) -> Verdict:
    """Ask me's safe list. ``diff_preview`` (the chat's Diff preview toggle
    switched on) asks before in-project edits too, with the diff (Claude's
    C5)."""
    if tool in READ_TOOLS:
        return ALLOW
    if tool in EDIT_TOOLS:
        verdict = classify_edit(
            args,
            root=root,
            conversation_id=conversation_id,
            workspace_paths=workspace_paths,
        )
        if verdict.allow and diff_preview is True:
            return Verdict(False, "preview")
        return verdict
    # Shell, web, MCP, browser, subagents, timers, ``sed_file`` /
    # ``notebook_edit`` (path argument unprobed, P-H13) and every tool this
    # version doesn't know.
    return Verdict(False, "tool")


def tool_label(tool: str, args: Any) -> str:
    label = _LABELS.get(tool)
    if label is None:
        if tool.startswith("browser_") or "browser" in tool:
            label = "browser action"
        else:
            label = f"tool {escape_markdown(tool[:60])}" if tool else "unknown tool"
    if tool == "call_mcp_tool" and isinstance(args, dict):
        server = args.get("ServerName") or args.get("serverName") or args.get("server")
        name = args.get("ToolName") or args.get("toolName") or args.get("name")
        parts = [str(p)[:40] for p in (server, name) if isinstance(p, str) and p]
        if parts:
            label = f"MCP tool {escape_markdown('/'.join(parts))}"
    return label


def _short(value: Any, limit: int = _PREVIEW_VALUE_CHARS) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _masked(key: str, value: Any) -> str:
    folded = key.casefold().replace("-", "_")
    if any(word in folded for word in _SECRET_KEY_WORDS):
        return _MASK
    return _short(value)


def _generic_preview(args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    pairs = [
        (key, value)
        for key, value in args.items()
        if key not in {"toolAction", "toolSummary"} and value not in (None, "")
    ][:_PREVIEW_PAIRS]
    return "\n".join(
        f"• {escape_markdown(str(key)[:40])} = {escape_markdown(_masked(str(key), value))}"
        for key, value in pairs
    )


def _rel_cwd(cwd: Any, root: Path | None) -> str | None:
    if not isinstance(cwd, str) or not cwd or root is None:
        return None
    try:
        if _fold(_real(cwd)) == _fold(_real(root)):
            return None
        rel = os.path.relpath(_real(cwd), _real(root))
    except ValueError:
        return cwd
    return cwd if rel.startswith("..") else rel


def _wait_text(value: Any) -> str | None:
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        return None
    seconds = value / 1000
    shown = f"{seconds:g} s" if seconds < 60 else f"{seconds / 60:.1f} min"
    return f"moves to the background after {shown} if still running"


def preview(tool: str, args: Any, *, root: Path | None) -> str:
    """The diff / command lines under the card's title."""
    if not isinstance(args, dict):
        return ""
    if tool == "run_command":
        lines = [format_diff_preview("Bash", {"command": args.get("CommandLine", "")})]
        notes: list[str] = []
        rel = _rel_cwd(args.get("Cwd"), root)
        if rel is not None:
            notes.append(f"in {escape_markdown(rel)}")
        wait = _wait_text(args.get("WaitMsBeforeAsync"))
        if wait is not None:
            notes.append(wait)
        if notes:
            lines.append(" · ".join(notes))
        if args.get("IsDaemon") is True:
            lines.append(
                "⚠️ starts a daemon: once approved it keeps running after this turn ends"
            )
        return "\n".join(line for line in lines if line)
    target = edit_target(args) or ""
    if tool == "replace_file_content":
        return format_diff_preview(
            "Edit",
            {
                "file_path": target,
                "old_string": str(args.get("TargetContent") or ""),
                "new_string": str(args.get("ReplacementContent") or ""),
            },
        )
    if tool == "multi_replace_file_content":
        chunks = args.get("ReplacementChunks")
        chunks = (
            [c for c in chunks if isinstance(c, dict)]
            if isinstance(chunks, list)
            else []
        )
        blocks = [
            format_diff_preview(
                "Edit",
                {
                    "file_path": target if n == 0 else "",
                    "old_string": str(chunk.get("TargetContent") or ""),
                    "new_string": str(chunk.get("ReplacementContent") or ""),
                },
            )
            for n, chunk in enumerate(chunks[:_MULTI_CHUNKS_SHOWN])
        ]
        more = len(chunks) - _MULTI_CHUNKS_SHOWN
        if more > 0:
            blocks.append(f"…{more} more chunk{'s' if more > 1 else ''}")
        return "\n".join(b for b in blocks if b)
    if tool == "write_to_file":
        text = format_diff_preview(
            "Write",
            {"file_path": target, "content": str(args.get("CodeContent") or "")},
        )
        if args.get("Overwrite") is True and target and os.path.exists(target):
            text += "\n(overwrites an existing file)"
        return text
    if tool == "invoke_subagent":
        return _subagents_preview(args)
    return _generic_preview(args)


_SUBAGENTS_SHOWN = 3


def _subagents_preview(args: dict[str, Any]) -> str:
    """``invoke_subagent``: who starts and what each is told (P-H8: each
    child's own tool calls come back here as their own cards)."""
    raw = args.get("Subagents")
    specs = [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []
    if not specs:
        return _generic_preview(args)
    lines = []
    for spec in specs[:_SUBAGENTS_SHOWN]:
        role = spec.get("Role") or spec.get("TypeName") or "subagent"
        prompt = spec.get("Prompt") or ""
        lines.append(
            f"• {escape_markdown(_short(role, 40))}: {escape_markdown(_short(prompt))}"
        )
    more = len(specs) - _SUBAGENTS_SHOWN
    if more > 0:
        lines.append(f"…{more} more")
    lines.append("Each subagent's own commands and edits ask here too.")
    return "\n".join(lines)


_WHY_NOTE = {
    "outside": "outside this project",
    "protected": "a protected file in this project",
    "self": "Untether's own files — approving lets agy change Telegram approvals",
    "mismatch": "agy's workspace isn't this chat's project",
}


def card_title(
    tool: str,
    args: Any,
    verdict: Verdict,
    *,
    root: Path | None,
    child_role: str | None = None,
    foreign: bool = False,
) -> str:
    """``Permission Request [agy] - <what>`` + the preview. agy's own
    ``toolSummary`` is model-written: shown escaped, after the arguments,
    labelled as agy's words."""
    head = f"Permission Request [agy] - {tool_label(tool, args)}"
    note = _WHY_NOTE.get(verdict.why)
    if note is not None and tool in EDIT_TOOLS:
        head += f" ({note})"
    lines = [head]
    body = preview(tool, args, root=root)
    if body:
        lines.append(body)
    if child_role:
        lines.append(f"from subagent: {escape_markdown(_short(child_role, 60))}")
    elif foreign:
        lines.append("from another agy conversation Untether didn't start")
    if isinstance(args, dict):
        said = args.get("toolSummary") or args.get("toolAction")
        if isinstance(said, str) and said.strip():
            lines.append(f"agy says: {escape_markdown(_short(said, _SUMMARY_CHARS))}")
    return "\n".join(lines)


# ── "Always allow this command" (session-scoped, never persisted) ────────────

_GRANTS_MAX = 50
_CONVERSATIONS_MAX = 64
_GRANTS: OrderedDict[tuple[int | None, str], list[tuple[str, str]]] = OrderedDict()


def _grant_key(args: Any) -> tuple[str, str] | None:
    if not isinstance(args, dict):
        return None
    command = args.get("CommandLine")
    if not isinstance(command, str) or not command.strip():
        return None
    cwd = args.get("Cwd")
    return command.strip(), cwd if isinstance(cwd, str) else ""


def grant(chat_id: int | None, conversation_id: str, args: Any) -> bool:
    key = _grant_key(args)
    if key is None or not conversation_id:
        return False
    slot = (chat_id, conversation_id)
    grants = _GRANTS.setdefault(slot, [])
    _GRANTS.move_to_end(slot)
    if key not in grants:
        grants.append(key)
        del grants[:-_GRANTS_MAX]
    while len(_GRANTS) > _CONVERSATIONS_MAX:
        _GRANTS.popitem(last=False)
    return True


def granted(chat_id: int | None, conversation_id: str | None, args: Any) -> bool:
    key = _grant_key(args)
    if key is None or not conversation_id:
        return False
    return key in _GRANTS.get((chat_id, conversation_id), ())


def forget_grants(conversation_id: str | None) -> None:
    """A run of this conversation in another mode clears its grants."""
    if not conversation_id:
        return
    for slot in [s for s in _GRANTS if s[1] == conversation_id]:
        del _GRANTS[slot]


def _reset_for_tests() -> None:
    _GRANTS.clear()
