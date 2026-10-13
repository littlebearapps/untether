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
import re
import stat
import sys
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..background_status import escape_markdown
from ..interactive import format_diff_preview

# Read-only tools, matched by exact name. Every path they name must resolve
# inside the project (or the conversation's artifact folder); a read
# elsewhere asks.
READ_TOOLS = frozenset({"view_file", "list_dir", "find_by_name", "grep_search"})
# Name no path and change nothing: status of a command agy already ran,
# agy's own permission list, a pause, the end of the turn.
NO_PATH_TOOLS = frozenset(
    {"command_status", "list_permissions", "wait", "wait_5_seconds", "finish"}
)
# Path-checked: in the project (or the artifact folder) and not protected.
EDIT_TOOLS = frozenset(
    {"write_to_file", "replace_file_content", "multi_replace_file_content"}
)
_EDIT_PATH_KEYS = ("TargetFile", "AbsolutePath")
# Argument keys that name a path (agy 1.3.x: ``TargetFile``, ``AbsolutePath``,
# ``DirectoryPath``, ``SearchPath``, ``SearchDirectory``). Every one present
# is checked, never only the first.
_PATH_KEY_RE = re.compile(r"(path|file|dir|directory|folder|cwd)s?$", re.IGNORECASE)
# Conversation ids are UUIDs; anything else never names an artifact folder.
_CONVERSATION_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{7,127}$")

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
    # macOS file systems are case-insensitive and normalise Unicode:
    # ``.Agents/`` is ``.agents/`` there.
    if sys.platform == "darwin":
        return unicodedata.normalize("NFC", path).casefold()
    return path


def _real(path: str | Path) -> str:
    """``realpath`` that resolves the existing part and appends the rest, so
    a new file under a symlinked folder resolves to where it would land.
    Raises ``ValueError`` for a path with a NUL byte (callers ask)."""
    text = str(path)
    if "\x00" in text:
        raise ValueError("NUL in path")
    return os.path.realpath(os.path.expanduser(text))


def _hard_linked(real: str) -> bool:
    """An existing file with more than one name: a write through the name
    inside the project changes the other one too, wherever it is."""
    try:
        st = os.lstat(real)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_nlink > 1


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


def path_values(args: Any) -> list[Any] | None:
    """Every value under a path-like key (lists flattened). ``None`` when a
    path-like key holds something that isn't a string (callers ask)."""
    if not isinstance(args, dict):
        return []
    found: list[Any] = []
    for key, value in args.items():
        if not isinstance(key, str) or not _PATH_KEY_RE.search(key):
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            if item is None or item == "":
                continue
            if not isinstance(item, str):
                return None
            found.append(item)
    return found


def _brain_root() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli" / "brain"


def artifact_dir(conversation_id: str) -> Path:
    """agy's own folder for a conversation's ``task.md`` / plans."""
    return _brain_root() / conversation_id


def _in_artifact_dir(real: str, conversation_id: str | None) -> bool:
    if not conversation_id or not _CONVERSATION_ID_RE.match(conversation_id):
        return False
    folder = _real(artifact_dir(conversation_id))
    # The folder itself must be agy's, not a link to somewhere else.
    if not _within(folder, _real(_brain_root())) or folder == _real(_brain_root()):
        return False
    return _within(real, folder) and real != folder


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

    folded = unicodedata.normalize("NFC", rel).casefold()
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
    """An edit tool's targets: every path it names inside the project (or
    the conversation's artifact folder), not protected, not hard-linked →
    allowed; anything else asks."""
    if edit_target(args) is None or root is None:
        return Verdict(False, "outside")
    paths = path_values(args)
    if not paths:
        return Verdict(False, "outside")
    return _classify_paths(
        paths,
        root=root,
        conversation_id=conversation_id,
        workspace_paths=workspace_paths,
        write=True,
    )


def _classify_paths(
    paths: list[Any],
    *,
    root: Path,
    conversation_id: str | None,
    workspace_paths: Any,
    write: bool,
) -> Verdict:
    base = _real(root)
    if isinstance(workspace_paths, list) and workspace_paths:
        first = workspace_paths[0]
        if not isinstance(first, str) or _fold(_real(first)) != _fold(base):
            return Verdict(False, "mismatch")
    for target in paths:
        path = (
            target if os.path.isabs(os.path.expanduser(target)) else str(root / target)
        )
        real = _real(path)
        if write:
            for protected in self_protected_paths():
                if _within(real, protected):
                    return Verdict(False, "self")
        if _in_artifact_dir(real, conversation_id):
            if write and _hard_linked(real):
                return Verdict(False, "outside")
            continue
        if not _within(real, base):
            return Verdict(False, "outside")
        if not write:
            continue
        rel = os.path.relpath(real, base)
        if rel == "." or _protected_rel(PurePosixPath(*Path(rel).parts).as_posix()):
            return Verdict(False, "protected")
        if _hard_linked(real):
            return Verdict(False, "outside")
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
    """Ask me's safe list: exact tool names, fully resolved paths. Anything
    it can't prove safe — including an error while checking — asks.
    ``diff_preview`` (the chat's Diff preview toggle switched on) asks before
    in-project edits too, with the diff (Claude's C5)."""
    try:
        return _classify(
            tool,
            args,
            root=root,
            conversation_id=conversation_id,
            workspace_paths=workspace_paths,
            diff_preview=diff_preview,
        )
    except (OSError, ValueError, TypeError):
        return Verdict(False, "tool")


def _classify(
    tool: str,
    args: Any,
    *,
    root: Path | None,
    conversation_id: str | None,
    workspace_paths: Any,
    diff_preview: bool | None,
) -> Verdict:
    if args is not None and not isinstance(args, dict):
        return Verdict(False, "tool")
    if tool in NO_PATH_TOOLS:
        return ALLOW if not path_values(args) else Verdict(False, "tool")
    if tool in READ_TOOLS:
        paths = path_values(args)
        if paths is None or root is None:
            return Verdict(False, "outside")
        # No path: agy searches its workspace (the project).
        return _classify_paths(
            paths,
            root=root,
            conversation_id=conversation_id,
            workspace_paths=workspace_paths,
            write=False,
        )
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


_COMMAND_INLINE_CHARS = 200
_COMMAND_FULL_CHARS = 1500
_COMMAND_EDGE_CHARS = 600


def visible(text: str, *, keep_newlines: bool = False) -> str:
    """Model-controlled text as the card shows it: control and invisible
    format characters (bidi overrides, zero-width spaces, BOM) become a
    visible ``⟨U+XXXX⟩`` so nothing can reorder or hide what is approved.
    Newlines stay only where the caller fences the text line by line."""
    out: list[str] = []
    for ch in text:
        if ch == "\n" and keep_newlines:
            out.append(ch)
        elif ch == "\t":
            out.append("    " if keep_newlines else " ")
        elif unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"}:
            out.append(f"⟨U+{ord(ch):04X}⟩")
        else:
            out.append(ch)
    return "".join(out)


def _display(value: Any, *, keep_newlines: bool = False) -> Any:
    """A copy of tool arguments with every string made :func:`visible`."""
    if isinstance(value, str):
        return visible(value, keep_newlines=keep_newlines)
    if isinstance(value, list):
        return [_display(v, keep_newlines=keep_newlines) for v in value]
    if isinstance(value, dict):
        return {
            k: _display(v, keep_newlines=keep_newlines and k in _MULTILINE_KEYS)
            for k, v in value.items()
        }
    return value


# Shown line by line in a fenced block (every line prefixed), so their
# newlines can't fake a line of the card.
_MULTILINE_KEYS = frozenset(
    {
        "CommandLine",
        "CodeContent",
        "TargetContent",
        "ReplacementContent",
        "ReplacementChunks",
    }
)


def _short(value: Any, limit: int = _PREVIEW_VALUE_CHARS) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = " ".join(visible(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _command_preview(command: Any) -> str:
    """The command exactly as agy will run it, first on the card. Short and
    single-line: inline code. Longer: a fenced block, in full up to 1,500
    characters, else its start and end with a visible warning that the
    middle isn't shown (the user must not approve a hidden tail)."""
    from ..markdown import backtick_fence, inline_code

    if not isinstance(command, str) or not command.strip():
        return "⚠️ no command line in this call"
    text = visible(command.strip("\n"), keep_newlines=True)
    if "\n" not in text and len(text) <= _COMMAND_INLINE_CHARS:
        return f"$ {inline_code(text)}"
    note = ""
    if len(text) > _COMMAND_FULL_CHARS:
        hidden = len(text) - 2 * _COMMAND_EDGE_CHARS
        note = (
            f"\n⚠️ long command ({len(text)} characters): {hidden} in the middle "
            "aren't shown — Deny unless you know what it runs"
        )
        text = (
            text[:_COMMAND_EDGE_CHARS]
            + f"\n… [{hidden} characters not shown] …\n"
            + text[-_COMMAND_EDGE_CHARS:]
        )
    fence = backtick_fence(text, minimum=3)
    return f"$\n{fence}sh\n{text}\n{fence}{note}"


def _masked(key: str, value: Any) -> str:
    folded = key.casefold().replace("-", "_")
    if any(word in folded for word in _SECRET_KEY_WORDS):
        return _MASK
    return _short(value)


def _generic_preview(args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    shown = [
        (key, value)
        for key, value in args.items()
        if key not in {"toolAction", "toolSummary"} and value not in (None, "")
    ]
    lines = [
        f"• {escape_markdown(_short(str(key), 40))} = "
        f"{escape_markdown(_masked(str(key), value))}"
        for key, value in shown[:_PREVIEW_PAIRS]
    ]
    more = len(shown) - _PREVIEW_PAIRS
    if more > 0:
        lines.append(f"…{more} more argument{'s' if more > 1 else ''} not shown")
    return "\n".join(lines)


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
    """The diff / command lines under the card's title, built from agy's own
    arguments (what will run), every string made :func:`visible`."""
    if not isinstance(args, dict):
        return ""
    args = _display(args, keep_newlines=True)
    if tool == "run_command":
        lines = [_command_preview(args.get("CommandLine"))]
        notes: list[str] = []
        rel = _rel_cwd(args.get("Cwd"), root)
        if rel is not None:
            notes.append(f"in {escape_markdown(_short(rel, 200))}")
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
    head = f"Permission Request [agy] - {tool_label(tool, _display(args))}"
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


def always_allowable(args: Any) -> bool:
    """ "Always allow" is offered only for a command shown in full on the
    card (never one whose middle was cut)."""
    key = _grant_key(args)
    return key is not None and len(key[0]) <= _COMMAND_FULL_CHARS


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
