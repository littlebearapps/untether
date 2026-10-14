"""Antigravity "Ask me": which agy tool calls run on their own, and the
approval card for the rest (#980, phase 14).

Ask me runs agy with ``--dangerously-skip-permissions`` on its default agent,
so Untether's ``PreToolUse`` gate is the only thing that lets a tool run.
This module is the policy half of that gate — pure, no I/O beyond resolving
paths:

- :func:`classify` — reads (``view_file``, ``list_dir`` …) and edits inside
  the project (or the conversation's own artifact folder) pass on Untether's
  safe list; everything else asks: shell, web, MCP, browser, subagents,
  timers, files outside the project (a nested checkout or an ``@branch``
  worktree inside it counts as outside), protected files inside it at any
  depth (``.agents/``, ``.git/``, ``.claude/``, ``.env`` …), Untether's own
  code and config, and any tool this version doesn't know. ``ask_question``
  is the phase 13 question card and is never classified here.
- :func:`card_title` — ``Permission Request [agy] - <what>`` plus a diff or
  command preview built from agy's own ``toolCall.args`` (reusing
  :func:`untether.interactive.format_diff_preview`), the subagent it came
  from, and agy's own one-line summary, escaped and last (model text).
- "Always allow this command" grants (:func:`grant` / :func:`granted`) —
  exact ``CommandLine`` + ``Cwd``, per chat and agy conversation, in memory
  only; never used by unattended runs.

Tool names and argument shapes: agy 1.3.3/1.3.4 ``init.tools``, hook
payloads (probes E2, P-B4, P-B6, P-H8) and the Go struct tags in the agy
binary (``json:"CommandLine"``, ``json:"Cwd"`` …). New or unknown tools ask.

Parsing like agy does (security review, 2026-10-13): agy is a Go program and
Go's JSON decoder matches field names case-insensitively, so
:func:`normalise_args` maps every key onto agy's own spelling first and
refuses arguments where two keys could mean the same field (or a key isn't
plain ASCII) — the gate denies those outright. Duplicate JSON keys are
refused earlier, by the hook (``gate_hook``). Only plain absolute paths
(no ``~``, ``.``/``..`` parts, doubled slashes or control characters) can be
on the safe list, so Untether and agy can't resolve a path differently, and
a safe-listed tool may carry only the argument names agy is known to use for
it.
"""

from __future__ import annotations

import itertools
import json
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
# Argument names agy's tools use (hook payloads and the binary's struct tags),
# in agy's spelling: a key that matches one case-insensitively is read as it.
_META_KEYS = frozenset({"toolAction", "toolSummary"})
_RUN_COMMAND_KEYS = frozenset(
    {
        "CommandLine",
        "Cwd",
        "WaitMsBeforeAsync",
        "IsDaemon",
        "Blocking",
        "SafeToAutoRun",
        "BypassSandbox",
        "RunPersistent",
        "RequestedTerminalID",
    }
)
_CHUNK_KEYS = frozenset(
    {"TargetContent", "ReplacementContent", "StartLine", "EndLine", "AllowMultiple"}
)
# Safe-listed tools may carry only these (anything else asks).
_SAFE_KEYS: dict[str, frozenset[str]] = {
    "view_file": frozenset({"AbsolutePath", "StartLine", "EndLine"}),
    "list_dir": frozenset({"DirectoryPath"}),
    "find_by_name": frozenset(
        {
            "SearchDirectory",
            "Pattern",
            "Excludes",
            "Includes",
            "Extensions",
            "Type",
            "MaxDepth",
            "FullPath",
        }
    ),
    "grep_search": frozenset(
        {
            "SearchPath",
            "Query",
            "MatchPerLine",
            "Includes",
            "CaseInsensitive",
            "IsRegex",
        }
    ),
    "write_to_file": frozenset(
        {
            "TargetFile",
            "CodeContent",
            "Overwrite",
            # agy 1.3.4 (live, 2026-10-13): appends instead of replacing.
            "Append",
            "EmptyFile",
            "ArtifactMetadata",
            "Description",
            "Complexity",
        }
    ),
    "replace_file_content": frozenset(
        {
            "TargetFile",
            "TargetContent",
            "ReplacementContent",
            "StartLine",
            "EndLine",
            "AllowMultiple",
            "Instruction",
            "Description",
            "ArtifactMetadata",
            "TargetLintErrorIds",
            "Complexity",
        }
    ),
    "multi_replace_file_content": frozenset(
        {
            "TargetFile",
            "ReplacementChunks",
            "Instruction",
            "Description",
            "ArtifactMetadata",
            "TargetLintErrorIds",
            "Complexity",
        }
    ),
}
_KNOWN_KEYS: frozenset[str] = frozenset(
    {
        *_META_KEYS,
        *_RUN_COMMAND_KEYS,
        *_CHUNK_KEYS,
        *(key for keys in _SAFE_KEYS.values() for key in keys),
        "Subagents",
        "Recipient",
        "Message",
        "Prompt",
        "DurationSeconds",
        "TimerCondition",
        "Action",
        "TaskId",
        "Url",
        "ServerName",
        "ToolName",
        "questions",
        # agy 1.3.4 schemas of asking tools (security review 3 M1).
        "Input",
        "CronExpression",
        "MaxIterations",
        "ConversationIds",
        "query",
        "domain",
        "name",
        "system_prompt",
        "enable_write_tools",
        "enable_mcp_tools",
        "enable_subagent_tools",
    }
)
_KNOWN_BY_FOLD = {key.casefold(): key for key in _KNOWN_KEYS}
# Shown on a card only as agy's words, never as what runs.
_DESCRIPTIVE_KEYS = frozenset(
    {*_META_KEYS, "Description", "Instruction", "explanation", "Complexity"}
)

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

# Inside the project these always ask (REVIEW B2, REVIEW-2 M4, security
# review 3 H1): planted agy config, git internals, and files another engine
# or tool runs, loads or reads as instructions. Matched at ANY depth — a
# nested checkout, a submodule or Untether's own ``@branch`` worktree under
# ``.worktrees/`` loads its own copy of each — and case-folded. Untether's
# outbox (it sends files to Telegram) is protected at the configured
# ``files.outbox_dir`` (:func:`_protected_rel`).
_PROTECTED_DIR_NAMES = frozenset(
    {
        # agy and the other engines Untether drives (hooks, plugins, MCP
        # servers, agents, settings).
        ".agents",
        ".gemini",
        ".claude",
        ".codex",
        ".opencode",
        ".pi",
        ".cursor",
        # git internals (hooks, config: filters and aliases run commands).
        ".git",
        # git hook managers and editors that run project commands.
        ".husky",
        ".lefthook",
        ".vscode",
        ".devcontainer",
    }
)
# Folder pairs at any depth: CI that runs on push.
_PROTECTED_DIR_PAIRS = frozenset({(".github", "workflows"), (".github", "actions")})
_PROTECTED_FILE_NAMES = frozenset(
    {
        # MCP servers / engine config read from the project.
        ".mcp.json",
        "opencode.json",
        "opencode.jsonc",
        # Instructions another agent reads as its own.
        "claude.md",
        "claude.local.md",
        "agents.md",
        "agents.override.md",
        "gemini.md",
        # git hook managers.
        ".pre-commit-config.yaml",
        "lefthook.yml",
        "lefthook.yaml",
        ".lefthook.yml",
        ".lefthook.yaml",
        "lefthook-local.yml",
        "lefthook-local.yaml",
        # Shell environments loaded on ``cd`` (direnv, mise hooks).
        ".envrc",
        "mise.toml",
        ".mise.toml",
        "mise.local.toml",
    }
)
DEFAULT_OUTBOX_DIR = ".untether-outbox"
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


def _has_link(path: str, trusted: str = "/") -> bool:
    """Any existing component of *path* below *trusted* (the project folder
    as configured: system links above it, such as macOS's ``/var``, don't
    count) is a symlink. A safe-listed edit must not go through one: the
    link could be pointed elsewhere between the check and agy's write
    (TOCTOU), so such an edit asks."""
    base = trusted.rstrip("/")
    if base and path.startswith(base + "/"):
        current, rest = base, path[len(base) :]
    else:
        current, rest = "/", path
    for part in rest.strip("/").split("/"):
        current = os.path.join(current, part)
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except FileNotFoundError:
            return False
        except OSError:
            return True
    return False


def _plain_target(path: str, *, write: bool) -> bool:
    """The target as it is now: absent, a regular file with one name, or
    (for a read) a folder. Anything else — a link, a FIFO, a device, a
    hard-linked file — isn't auto-allowed."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if stat.S_ISREG(st.st_mode):
        return st.st_nlink == 1
    return stat.S_ISDIR(st.st_mode) and not write


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


def normalise_args(args: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Tool arguments as agy reads them, or ``(None, why)`` when that is
    ambiguous. Go's JSON decoder matches field names case-insensitively, so
    ``commandline`` *is* ``CommandLine`` to agy; two keys that fold to the
    same name (or any key that isn't plain ASCII, where folding rules
    differ) could mean either value, and are refused."""
    if args is None:
        return {}, None
    if not isinstance(args, dict):
        return None, "the arguments aren't a JSON object"
    out: dict[str, Any] = {}
    seen: dict[str, str] = {}
    for key, value in args.items():
        if not isinstance(key, str) or not key.isascii() or not key.isprintable():
            return None, "an argument name isn't plain text"
        fold = key.casefold()
        if fold in seen:
            return None, f"{seen[fold]!r} and {key!r} name the same argument"
        seen[fold] = key
        out[_KNOWN_BY_FOLD.get(fold, key)] = value
    chunks = out.get("ReplacementChunks")
    if isinstance(chunks, list):
        clean: list[Any] = []
        for chunk in chunks:
            if isinstance(chunk, dict):
                fixed, why = normalise_args(chunk)
                if fixed is None:
                    return None, why
                clean.append(fixed)
            else:
                clean.append(chunk)
        out["ReplacementChunks"] = clean
    return out, None


def plain_absolute(path: Any) -> bool:
    """An absolute path with nothing to interpret: no ``~``, no ``.`` or
    ``..`` parts, no doubled or trailing slash, no control characters. Such
    a path reads the same to Untether and to agy (bar symlinks, which the
    realpath check catches)."""
    if not isinstance(path, str) or not path.startswith("/") or "//" in path:
        return False
    # A lone surrogate (``Cs``) or U+FFFD: Go turns an unpaired ``\\ud800``
    # into U+FFFD, so agy would open another file than Untether checked.
    if any(
        unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Zl", "Zp"} or ch == "�"
        for ch in path
    ):
        return False
    return path == "/" or os.path.normpath(path) == path


def _brain_root() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli" / "brain"


def artifact_dir(conversation_id: str) -> Path:
    """agy's own folder for a conversation's ``task.md`` / plans."""
    return _brain_root() / conversation_id


# What agy writes in a conversation's artifact folder (agy 1.3.4, the brain
# folders on lba-1): top-level ``<name>.md`` artifacts with their
# ``<name>.md.metadata.json``, and anything under ``scratch/``. Its
# ``.system_generated/`` (messages, steps, task logs, transcripts) and
# ``.user_uploaded/`` are agy's own records and never a safe-listed edit
# (security review 3 L2): a forged message or step there is read back as
# the conversation's history.
_ARTIFACT_FILE_RE = re.compile(r"^[^./][^/]*\.md(\.metadata\.json)?$")
_ARTIFACT_SCRATCH = "scratch"


def _in_artifact_dir(
    real: str,
    conversation_id: str | None,
    *,
    write: bool = False,
    artifact_path: Any = None,
) -> bool:
    if not conversation_id or not _CONVERSATION_ID_RE.match(conversation_id):
        return False
    folder = _real(artifact_dir(conversation_id))
    # The folder itself must be agy's, not a link to somewhere else.
    if not _within(folder, _real(_brain_root())) or folder == _real(_brain_root()):
        return False
    if artifact_path is not None:
        # The payload names the folder agy itself uses for this
        # conversation: it must be the one Untether checks.
        if not isinstance(artifact_path, str) or not artifact_path:
            return False
        if _fold(_real(artifact_path)) != _fold(folder):
            return False
    if not _within(real, folder) or real == folder:
        return False
    if not write:
        return True
    rel = os.path.relpath(real, folder)
    if rel.startswith(_ARTIFACT_SCRATCH + os.sep):
        return True
    return _ARTIFACT_FILE_RE.match(rel) is not None


def self_protected_paths(config_path: Path | str | None = None) -> tuple[str, ...]:
    """REVIEW-2 B3: Untether's package, its venv, the Python it runs on
    (``sys.base_prefix``: the gate hook imports its standard library) and
    the gate plugin; and (security review 3 M5) this instance's config file
    and its folder — ``watch_config`` applies an edit there. An edit to any
    of them could switch the gate off, wherever the project is."""
    import untether

    from . import gate_plugin

    paths = [
        _real(Path(untether.__file__).parent),
        _real(sys.prefix),
        _real(sys.base_prefix),
        _real(gate_plugin.plugin_dir()),
    ]
    if config_path is not None:
        config = _real(config_path)
        paths.extend((config, os.path.dirname(config)))
    return tuple(dict.fromkeys(paths))


def _protected_rel(rel: str, outbox_dir: str = DEFAULT_OUTBOX_DIR) -> bool:
    """*rel* (relative to the project, ``/``-separated) is planted config at
    any depth, a secret, or in Untether's outbox."""
    from ..telegram.files import deny_reason

    folded = unicodedata.normalize("NFC", rel).casefold()
    parts = folded.split("/")
    if parts[-1] in _PROTECTED_FILE_NAMES:
        return True
    if any(part in _PROTECTED_DIR_NAMES for part in parts):
        return True
    if any(pair in _PROTECTED_DIR_PAIRS for pair in itertools.pairwise(parts)):
        return True
    outbox = unicodedata.normalize("NFC", outbox_dir.strip("/")).casefold()
    if outbox and (folded == outbox or folded.startswith(outbox + "/")):
        return True
    # The secrets globs.
    return deny_reason(Path(rel), _SECRET_GLOBS) is not None


@dataclass(frozen=True, slots=True)
class _Scope:
    root: Path
    conversation_id: str | None
    workspace_paths: Any
    outbox_dir: str
    # Folders inside the project that count as outside it (the projects'
    # ``@branch`` worktree roots), realpath'd.
    outside: tuple[str, ...]
    config_path: Path | str | None
    artifact_path: Any


def _scope(
    root: Path,
    conversation_id: str | None,
    workspace_paths: Any,
    outbox_dir: str,
    outside: tuple[Path | str, ...],
    config_path: Path | str | None,
    artifact_path: Any,
) -> _Scope:
    return _Scope(
        root=root,
        conversation_id=conversation_id,
        workspace_paths=workspace_paths,
        outbox_dir=outbox_dir or DEFAULT_OUTBOX_DIR,
        outside=tuple(_real(p) for p in outside),
        config_path=config_path,
        artifact_path=artifact_path,
    )


def _nested_checkout(real: str, base: str) -> bool:
    """A folder below the project on the way to *real* (or *real* itself)
    holds a ``.git`` file or folder: another checkout — a submodule, a
    nested repo or a git worktree — with hooks and config of its own
    (security review 3 H1). It isn't this project."""
    rel = os.path.relpath(real, base)
    if rel == "." or rel.startswith(".."):
        return False
    current = base
    for part in rel.split(os.sep):
        current = os.path.join(current, part)
        if os.path.lexists(os.path.join(current, ".git")):
            return True
    return False


def classify_edit(
    args: Any,
    *,
    root: Path | None,
    conversation_id: str | None,
    workspace_paths: Any = None,
    outbox_dir: str = DEFAULT_OUTBOX_DIR,
    outside: tuple[Path | str, ...] = (),
    config_path: Path | str | None = None,
    artifact_path: Any = None,
) -> Verdict:
    """An edit tool's targets: every path it names inside the project (or
    the conversation's artifact folder), not protected, not hard-linked →
    allowed; anything else asks."""
    if edit_target(args) is None or root is None:
        return Verdict(False, "outside")
    paths = path_values(args)
    if not paths:
        return Verdict(False, "outside")
    scope = _scope(
        root,
        conversation_id,
        workspace_paths,
        outbox_dir,
        outside,
        config_path,
        artifact_path,
    )
    return _classify_paths(paths, scope, write=True)


def _classify_paths(paths: list[Any], scope: _Scope, *, write: bool) -> Verdict:
    root = scope.root
    base = _real(root)
    workspace_paths = scope.workspace_paths
    if isinstance(workspace_paths, list) and workspace_paths:
        first = workspace_paths[0]
        if not isinstance(first, str) or _fold(_real(first)) != _fold(base):
            return Verdict(False, "mismatch")
    for target in paths:
        if not plain_absolute(target):
            # Relative, ``~``, ``..`` …: agy may resolve it differently.
            return Verdict(False, "unclear")
        real = _real(target)
        inside = _within(real, base)
        artifact = _in_artifact_dir(
            real,
            scope.conversation_id,
            write=write,
            artifact_path=scope.artifact_path,
        )
        if write:
            for protected in self_protected_paths(scope.config_path):
                if _within(real, protected):
                    return Verdict(False, "self")
        if inside and (
            # Untether's ``@branch`` worktrees and any nested checkout are
            # other projects, wherever they sit (security review 3 H1).
            any(_within(real, other) for other in scope.outside)
            or _nested_checkout(real, base)
        ):
            inside = False
        if not inside and not artifact:
            return Verdict(False, "outside")
        # TOCTOU (security review): agy acts after this check, so the path
        # must not be able to change meaning in between. No component below
        # the trusted folder may be a symlink (a link can be repointed), and
        # an existing target must be a plain file (or, for a read, folder)
        # with one name (a hard link is the same file outside the project).
        trusted = str(root) if inside else str(_brain_root())
        if _has_link(target, trusted) or not _plain_target(target, write=write):
            return Verdict(False, "unclear")
        if artifact or not write:
            continue
        rel = PurePosixPath(*Path(os.path.relpath(real, base)).parts).as_posix()
        if rel == "." or _protected_rel(rel, scope.outbox_dir):
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
    outbox_dir: str = DEFAULT_OUTBOX_DIR,
    outside: tuple[Path | str, ...] = (),
    config_path: Path | str | None = None,
    artifact_path: Any = None,
) -> Verdict:
    """Ask me's safe list: exact tool names, fully resolved paths. Anything
    it can't prove safe — including an error while checking — asks.
    ``diff_preview`` (the chat's Diff preview toggle switched on) asks before
    in-project edits too, with the diff (Claude's C5). ``outbox_dir`` is the
    instance's ``files.outbox_dir``, ``outside`` the ``@branch`` worktree
    roots, ``config_path`` the instance's config file and ``artifact_path``
    the payload's ``artifactDirectoryPath``."""
    try:
        scope = (
            _scope(
                root,
                conversation_id,
                workspace_paths,
                outbox_dir,
                outside,
                config_path,
                artifact_path,
            )
            if root is not None
            else None
        )
        return _classify(tool, args, scope=scope, diff_preview=diff_preview)
    except Exception:  # noqa: BLE001 — never safe-list what can't be checked
        return Verdict(False, "tool")


# Search tools whose values agy hands to a search program (agy 1.3.4, probed:
# ``git grep … -- <Query> <Includes…>`` in a git repo, its own search
# elsewhere). A value that starts with ``-`` could be read as a flag by
# another version or backend (``rg --pre=<script>`` runs a program), so it is
# never on the safe list (security review 3 M4).
_DASH_CHECKED = frozenset({"grep_search", "find_by_name"})


def _dash_value(args: Any) -> bool:
    if not isinstance(args, dict):
        return False
    for key, value in args.items():
        if key in _META_KEYS:
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, str) and item.lstrip().startswith("-"):
                return True
    return False


def _classify(
    tool: str, args: Any, *, scope: _Scope | None, diff_preview: bool | None
) -> Verdict:
    if args is not None and not isinstance(args, dict):
        return Verdict(False, "tool")
    if tool in NO_PATH_TOOLS:
        # ``[]`` only: ``None`` (a non-string path value) asks too.
        return ALLOW if path_values(args) == [] else Verdict(False, "tool")
    allowed = _SAFE_KEYS.get(tool)
    if allowed is not None and not _keys_ok(args, allowed):
        return Verdict(False, "unclear")
    if tool in _DASH_CHECKED and _dash_value(args):
        return Verdict(False, "unclear")
    if tool in READ_TOOLS:
        paths = path_values(args)
        if paths is None or scope is None:
            return Verdict(False, "outside")
        # No path: agy searches its workspace (the project).
        return _classify_paths(paths, scope, write=False)
    if tool in EDIT_TOOLS:
        if edit_target(args) is None or scope is None:
            return Verdict(False, "outside")
        paths = path_values(args)
        if not paths:
            return Verdict(False, "outside")
        verdict = _classify_paths(paths, scope, write=True)
        if verdict.allow and diff_preview is True:
            return Verdict(False, "preview")
        return verdict
    # Shell, web, MCP, browser, subagents, timers, ``sed_file`` /
    # ``notebook_edit`` (path argument unprobed, P-H13) and every tool this
    # version doesn't know.
    return Verdict(False, "tool")


def _keys_ok(args: Any, allowed: frozenset[str]) -> bool:
    """Only argument names agy is known to use for this tool (plus its own
    ``toolAction`` / ``toolSummary``); a chunk only its chunk fields."""
    if not isinstance(args, dict):
        return args is None
    if not set(args) <= allowed | _META_KEYS:
        return False
    chunks = args.get("ReplacementChunks")
    if chunks is not None:
        if not isinstance(chunks, list):
            return False
        return all(isinstance(c, dict) and set(c) <= _CHUNK_KEYS for c in chunks)
    return True


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


def _block(
    text: str,
    *,
    prefix: str,
    lang: str = "",
    what: str = "value",
    verb: str = "does",
    limit: int = _COMMAND_FULL_CHARS,
) -> str:
    """*text* exactly as agy gets it. Short and single-line: inline code
    after *prefix*. Longer: a fenced block, in full up to *limit*
    characters, else its start and end with a visible warning that the
    middle isn't shown (the user must not approve a hidden part)."""
    from ..markdown import backtick_fence, inline_code

    text = visible(text.strip("\n"), keep_newlines=True)
    if not text.strip():
        return f"{prefix} (empty)"
    if "\n" not in text and len(text) <= _COMMAND_INLINE_CHARS:
        return f"{prefix} {inline_code(text)}"
    note = ""
    limit = max(limit, 2 * _MIN_EDGE_CHARS + 1)
    if len(text) > limit:
        edge = min(_COMMAND_EDGE_CHARS, limit // 2)
        hidden = len(text) - 2 * edge
        note = (
            f"\n⚠️ long {what} ({len(text)} characters): {hidden} in the middle "
            f"aren't shown — Deny unless you know what it {verb}"
        )
        text = text[:edge] + f"\n… [{hidden} characters not shown] …\n" + text[-edge:]
    fence = backtick_fence(text, minimum=3)
    return f"{prefix}\n{fence}{lang}\n{text}\n{fence}{note}"


def _command_preview(command: Any) -> str:
    """The command exactly as agy will run it, first on the card (in full
    up to 1,500 characters, see :func:`_block`)."""
    if not isinstance(command, str) or not command.strip():
        return "⚠️ no command line in this call"
    return _block(command, prefix="$", lang="sh", what="command", verb="runs")


# Security review 3 M1: an approval card shows every argument that can
# change what a tool does, in full — values are never cut short silently,
# and a value is never hidden just because the model chose a key name that
# looks like a secret without the card saying so. Each tool's own semantic
# arguments come first (``Url`` before anything the model added), then any
# argument agy's schema for that tool doesn't have, flagged ⚠️.
_MIN_EDGE_CHARS = 100
_PREVIEW_BUDGET_CHARS = 3000
_EXTRA_ARGS_BUDGET_CHARS = 1500
# The arguments agy's own schema has for an asking tool (agy 1.3.4: hook
# payloads and the model's own schema listing, 2026-10-13), in agy's
# spelling after :func:`normalise_args`, the ones that say what runs first.
_TOOL_KEYS: dict[str, tuple[str, ...]] = {
    "read_url_content": ("Url",),
    "schedule": (
        "Prompt",
        "CronExpression",
        "DurationSeconds",
        "TimerCondition",
        "MaxIterations",
        "IsDaemon",
    ),
    "search_web": ("query", "domain"),
    "manage_task": ("Action", "TaskId"),
    "manage_subagents": ("Action", "ConversationIds"),
    "send_message": ("Recipient", "Message"),
    "define_subagent": (
        "system_prompt",
        "enable_write_tools",
        "enable_mcp_tools",
        "enable_subagent_tools",
        "name",
        "Description",
    ),
}
# Tools whose schema isn't known (agy 1.3.4 doesn't offer them to its
# default agent): the arguments that most likely say what runs come first.
_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "open_browser_url": ("Url",),
    "send_command_input": ("Input",),
    "call_mcp_tool": ("ServerName", "ToolName"),
}


def _secret_key(key: str) -> bool:
    folded = key.casefold().replace("-", "_")
    return any(word in folded for word in _SECRET_KEY_WORDS)


def _value_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=repr)
    except (TypeError, ValueError, RecursionError):
        return repr(value)


def _arg_line(
    key: str, value: Any, *, budget: int, flag: bool = False, bullet: str = "•"
) -> str:
    """``• key = value`` with the value in full (see :func:`_block`)."""
    name = escape_markdown(_short(str(key), 60))
    head = f"⚠️ {bullet} {name} =" if flag else f"{bullet} {name} ="
    if _secret_key(str(key)):
        # Kept out of Telegram, but never silently: the card says why.
        return (
            f"{head} {_MASK} (hidden: its name looks like a secret — Deny "
            "unless you expected one here)"
        )
    return _block(_value_text(value), prefix=head, limit=budget)


def _args_lines(
    items: list[tuple[str, Any]],
    *,
    budget: int,
    flagged: frozenset[str] = frozenset(),
    bullet: str = "•",
) -> list[str]:
    """Each argument in full while *budget* (characters) lasts; then a ⚠️
    line naming every argument that isn't shown."""
    lines: list[str] = []
    left = budget
    for n, (key, value) in enumerate(items):
        if left < 2 * _MIN_EDGE_CHARS + 1:
            rest = [str(k) for k, _ in items[n:]]
            names = ", ".join(escape_markdown(_short(k, 40)) for k in rest[:10])
            lines.append(
                f"⚠️ {len(rest)} more argument{'s' if len(rest) > 1 else ''} "
                f"not shown ({names}) — Deny unless you know what they do"
            )
            break
        line = _arg_line(key, value, budget=left, flag=key in flagged, bullet=bullet)
        left -= len(line)
        lines.append(line)
    return lines


def _ordered(
    tool: str, args: dict[str, Any], skip: frozenset[str] = frozenset()
) -> tuple[list[tuple[str, Any]], frozenset[str]]:
    """The arguments to show: the tool's own (semantic first), then those
    its schema doesn't have — returned as the flagged set."""
    items = [
        (key, value)
        for key, value in args.items()
        if key not in _META_KEYS and key not in skip and value is not None
    ]
    known = _TOOL_KEYS.get(tool)
    primary = known or _PRIMARY_KEYS.get(tool, ())
    flagged = frozenset(
        str(key) for key, _ in items if known is not None and key not in known
    )

    def rank(item: tuple[str, Any]) -> tuple[int, int]:
        key = item[0]
        if key in primary:
            return (0, primary.index(key))
        return (1, 0) if key in flagged else (2, 0)

    return sorted(items, key=rank), flagged


def _generic_preview(tool: str, args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    items, flagged = _ordered(tool, args)
    lines: list[str] = []
    if flagged:
        names = ", ".join(escape_markdown(_short(k, 40)) for k in sorted(flagged))
        lines.append(f"⚠️ arguments agy's {escape_markdown(tool)} doesn't have: {names}")
    lines.extend(_args_lines(items, budget=_PREVIEW_BUDGET_CHARS, flagged=flagged))
    if tool == "define_subagent":
        lines.extend(_define_subagent_notes(args))
    return "\n".join(lines)


def _truthy(value: Any) -> bool:
    return value not in (None, False, 0, "", "false", "False")


def _define_subagent_notes(args: dict[str, Any]) -> list[str]:
    """``define_subagent`` (agy 1.3.4 schema: name, description,
    system_prompt and three ``enable_*`` switches; no hooks or commands of
    its own). The subagent's tool calls come back here as their own cards."""
    gives = [
        what
        for key, what in (
            ("enable_write_tools", "file-writing and command tools"),
            ("enable_mcp_tools", "MCP tools"),
            ("enable_subagent_tools", "tools to define and start subagents"),
        )
        if _truthy(args.get(key))
    ]
    lines = []
    if gives:
        lines.append("⚠️ the new subagent gets " + ", ".join(gives))
    lines.append("Its own commands and edits ask here too.")
    return lines


def _other_args(tool: str, args: dict[str, Any], shown: frozenset[str]) -> list[str]:
    """Every argument the preview didn't already show (agy's descriptive
    fields aside), in full, so nothing that changes what runs is hidden.
    One that isn't among the argument names Untether knows agy uses is
    flagged ⚠️."""
    items = [
        (key, value)
        for key, value in args.items()
        if key not in shown and key not in _DESCRIPTIVE_KEYS and value is not None
    ]
    own = _RUN_COMMAND_KEYS if tool == "run_command" else _SAFE_KEYS.get(tool)
    flagged = frozenset(str(key) for key, _ in items if key not in (own or _KNOWN_KEYS))
    items.sort(key=lambda item: 0 if item[0] in flagged else 1)
    # #1080: "⚙️", not "•": the renderer turns a "•" line into "- ", which
    # under a diff's "+ x" read as a removed line.
    return _args_lines(
        items, budget=_EXTRA_ARGS_BUDGET_CHARS, flagged=flagged, bullet=_GEAR
    )


_GEAR = "\N{GEAR}\N{VARIATION SELECTOR-16}"


_COMMAND_SHOWN = frozenset({"CommandLine", "Cwd", "WaitMsBeforeAsync", "IsDaemon"})
_EDIT_SHOWN = frozenset(
    {
        "TargetFile",
        "CodeContent",
        "Overwrite",
        "TargetContent",
        "ReplacementContent",
        "ReplacementChunks",
        "StartLine",
        "EndLine",
    }
)


def _resolves_to(target: str) -> str | None:
    """``→ resolves to …`` when the real file isn't the path as written
    (a symlink, ``..``, a relative path)."""
    if not target:
        return None
    try:
        real = _real(target)
    except (OSError, ValueError):
        return "⚠️ this path can't be resolved"
    if plain_absolute(target) and real == target:
        return None
    return f"→ resolves to {escape_markdown(_short(visible(real), 200))}"


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
    raw = args  # values below go through _block, which makes them visible
    args = _display(args, keep_newlines=True)
    if tool == "run_command":
        lines = [_command_preview(args.get("CommandLine"))]
        notes: list[str] = []
        rel = _rel_cwd(args.get("Cwd"), root)
        if rel is not None:
            notes.append(f"in {escape_markdown(_short(rel, 200))}")
        cwd = args.get("Cwd")
        if cwd is not None and not plain_absolute(cwd):
            notes.append("⚠️ the folder isn't a plain absolute path")
        wait = _wait_text(args.get("WaitMsBeforeAsync"))
        if wait is not None:
            notes.append(wait)
        if notes:
            lines.append(" · ".join(notes))
        if args.get("IsDaemon") not in (None, False, 0, ""):
            lines.append(
                "⚠️ starts a daemon: once approved it keeps running after this turn ends"
            )
        lines.extend(_other_args(tool, raw, _COMMAND_SHOWN))
        return "\n".join(line for line in lines if line)
    target = edit_target(args) or ""
    if tool in EDIT_TOOLS:
        tail = [_resolves_to(target)] if target else ["⚠️ no target file in this call"]
        tail.extend(_other_args(tool, raw, _EDIT_SHOWN))
        body = _edit_preview(tool, args, target)
        return "\n".join(line for line in (body, *tail) if line)
    if tool == "invoke_subagent":
        return _subagents_preview(raw)
    return _generic_preview(tool, raw)


def _edit_preview(tool: str, args: dict[str, Any], target: str) -> str:
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
    return ""


def _subagents_preview(args: dict[str, Any]) -> str:
    """``invoke_subagent``: who starts and what each is told, in full (P-H8:
    each child's own tool calls come back here as their own cards)."""
    raw = args.get("Subagents")
    specs = [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []
    if not specs or len(specs) != len(raw):
        return _generic_preview("invoke_subagent", args)
    lines = []
    left = _PREVIEW_BUDGET_CHARS
    for n, spec in enumerate(specs):
        if left < 2 * _MIN_EDGE_CHARS + 1:
            rest = len(specs) - n
            lines.append(
                f"⚠️ {rest} more subagent{'s' if rest > 1 else ''} not shown — "
                "Deny unless you know what they're told"
            )
            break
        role = spec.get("Role") or spec.get("TypeName") or "subagent"
        head = f"• {escape_markdown(_short(_value_text(role), 40))}:"
        prompt = spec.get("Prompt")
        body = _block(_value_text(prompt or ""), prefix=head, limit=left)
        left -= len(body)
        lines.append(body)
        extra = [
            (str(key), value)
            for key, value in spec.items()
            if key not in {"Role", "Prompt"} and value is not None
        ]
        if extra:
            extra_lines = _args_lines(extra, budget=max(left, 0))
            left -= sum(len(line) for line in extra_lines)
            lines.extend("  " + line for line in extra_lines)
    rest_args = [
        (key, value)
        for key, value in args.items()
        if key not in _META_KEYS and key != "Subagents" and value is not None
    ]
    if rest_args:
        lines.extend(
            _args_lines(
                rest_args,
                budget=_EXTRA_ARGS_BUDGET_CHARS,
                flagged=frozenset(k for k, _ in rest_args),
            )
        )
    lines.append("Each subagent's own commands and edits ask here too.")
    return "\n".join(lines)


_WHY_NOTE = {
    "outside": "outside this project",
    "protected": "a protected file in this project",
    "self": "Untether's own files — approving lets agy change Telegram approvals",
    "mismatch": "agy's workspace isn't this chat's project",
    "unclear": "arguments Untether can't check",
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
    if note is not None and (tool in EDIT_TOOLS or tool in READ_TOOLS):
        # #1083: a read that asks says why too (outside the project, agy's
        # workspace isn't the project, or paths Untether can't check).
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
_GRANTS: OrderedDict[tuple[int | None, str], list[tuple[Any, ...]]] = OrderedDict()

# #1085: ``run_command`` fields agy 1.3.4 decodes into Go zero values (the
# binary's struct tags and proto3 fields without presence: ``run_persistent``,
# ``requested_terminal_id``, ``is_daemon``; ``BypassSandbox,omitempty``), so
# their zero value — or JSON ``null`` — runs exactly as if the field were
# absent. agy's main agent often fills the first two that way. Only the exact
# zero value counts (``0`` or ``"false"`` for a bool isn't one); any other
# value still keeps "Always allow" off and never matches a grant.
_RUN_COMMAND_DEFAULTS: dict[str, Any] = {
    "RunPersistent": False,
    "RequestedTerminalID": "",
    "BypassSandbox": False,
    "IsDaemon": False,
}


def _at_default(key: str, value: Any) -> bool:
    if key not in _RUN_COMMAND_DEFAULTS:
        return False
    if value is None:
        return True
    default = _RUN_COMMAND_DEFAULTS[key]
    return type(value) is type(default) and value == default


def _without_defaults(args: dict[str, Any]) -> dict[str, Any]:
    """*args* minus the ``run_command`` fields that sit at agy's default (the
    card still shows them; see :func:`preview`)."""
    return {k: v for k, v in args.items() if not _at_default(k, v)}


def _grant_key(args: Any) -> tuple[Any, ...] | None:
    """The command, its folder and every other argument that changes what
    runs (``IsDaemon``, ``WaitMsBeforeAsync``, anything new), so a grant
    never covers a call that differs in any of them; agy's own descriptive
    fields, and fields at agy's default (#1085), are left out."""
    if not isinstance(args, dict):
        return None
    args = _without_defaults(args)
    command = args.get("CommandLine")
    if not isinstance(command, str) or not command.strip(_GRANT_TRIM):
        return None
    cwd = args.get("Cwd")
    try:
        # One unambiguous encoding per JSON value (``true`` / ``1`` / ``"1"``
        # differ; non-ASCII escaped): anything that isn't plain JSON — no
        # ``repr`` fallback that two values could share, no NaN — can't be
        # granted (security review 3 M2).
        rest = tuple(
            sorted(
                (key, json.dumps(value, sort_keys=True, allow_nan=False))
                for key, value in args.items()
                if key not in _DESCRIPTIVE_KEYS and key not in {"CommandLine", "Cwd"}
            )
        )
    except (TypeError, ValueError, RecursionError):
        return None
    # Only ASCII spaces and line feeds at the ends are trimmed (security
    # review 3 M2): Python's ``strip()`` also removes Unicode spaces (U+3000)
    # and the U+001C to U+001F separators, so U+3000 + ``npm test`` + U+001F
    # would have matched a grant for ``npm test``.
    return command.strip(_GRANT_TRIM), cwd if isinstance(cwd, str) else "", rest


_GRANT_TRIM = " \n"


def _plain_text(text: str) -> bool:
    """No control, format, private-use, unassigned or surrogate characters
    and no space but the ASCII one (tabs and line feeds are fine): what the
    card shows is exactly what runs."""
    for ch in text:
        if ch in "\n\t":
            continue
        category = unicodedata.category(ch)
        if category in {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"} or ch == "�":
            return False
        if category == "Zs" and ch != " ":
            return False
    return True


def always_allowable(args: Any) -> bool:
    """ "Always allow" is offered only for a command shown in full on the
    card (never one whose middle was cut), in plain text."""
    key = _grant_key(args)
    if key is None or len(key[0]) > _COMMAND_FULL_CHARS:
        return False
    if not _plain_text(args["CommandLine"]) or not _plain_text(key[1]):
        return False
    # Nothing beyond what the card shows (a field at agy's default changes
    # nothing, #1085), and a folder that reads the same to Untether and agy.
    if not set(_without_defaults(args)) <= _COMMAND_SHOWN | _DESCRIPTIVE_KEYS:
        return False
    return not key[1] or plain_absolute(key[1])


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
    # A grant only ever covers a call that could have been granted itself:
    # nothing the card wouldn't show, the command in full, a plain folder.
    if not always_allowable(args):
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
