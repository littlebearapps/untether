"""Command backend for browsing project files via inline keyboard."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from ...commands import CommandBackend, CommandContext, CommandResult
from ...context import RunContext
from ...logging import get_logger
from ...settings import TelegramFilesSettings
from ...transport import RenderedMessage
from ...utils.paths import get_run_base_dir
from ..files import PathAccess, check_path_access

logger = get_logger(__name__)

# Path registry: (chat_id, short ID) -> absolute (resolved) path string.
# This avoids long paths in 64-byte callback_data. Keyed per chat (#210,
# #389) so an id minted in one chat reads as expired in any other; the
# counter stays global so ids never repeat across chats.
_PATH_REGISTRY: dict[tuple[int | str, int], str] = {}
_PATH_COUNTER: int = 0
_MAX_REGISTRY = 500

# Hidden components that /browse may still show (#389 D2). Deny globs
# always win over this allowlist.
_HIDDEN_ALLOW: frozenset[str] = frozenset({".github", ".gitignore"})

# Listing-only noise filter (not a security control).
_NOISE_DIRS: frozenset[str] = frozenset(
    {"__pycache__", "node_modules", ".git", ".venv", "venv"}
)

_NO_ROOT_TEXT = (
    "No project directory for this chat. Bind the chat to a project "
    "(chat_id under [projects.<alias>]) or set default_project in untether.toml."
)

# Limits
_MAX_ENTRIES = 20  # max items shown in one listing
_FILE_PREVIEW_LINES = 25
_FILE_PREVIEW_CHARS = 2000

# File extension -> code block language tag
_EXT_LANG: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".rs": "rust",
    ".go": "go",
    ".rb": "ruby",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".md": "markdown",
    ".xml": "xml",
    ".dockerfile": "dockerfile",
}


class _Entry(NamedTuple):
    name: str
    """Name as listed (a symlink keeps its own name)."""
    path: Path
    """Resolved absolute path (what a button opens)."""


def _register_path(chat_id: int | str, path: str) -> int:
    """Register a path for *chat_id* and return a short numeric ID."""
    global _PATH_COUNTER
    for (owner, pid), p in _PATH_REGISTRY.items():
        if owner == chat_id and p == path:
            return pid
    _PATH_COUNTER += 1
    pid = _PATH_COUNTER
    _PATH_REGISTRY[(chat_id, pid)] = path
    # Trim the oldest insertion (dicts keep insertion order).
    while len(_PATH_REGISTRY) > _MAX_REGISTRY:
        _PATH_REGISTRY.pop(next(iter(_PATH_REGISTRY)), None)
    return pid


def _resolve_path(chat_id: int | str, pid: int) -> str | None:
    """Look up a path registered by *chat_id*."""
    return _PATH_REGISTRY.get((chat_id, pid))


def _existing_dir(path: Path | None) -> Path | None:
    if path is None:
        return None
    try:
        if path.is_dir():
            return path.resolve()
    except (OSError, RuntimeError):
        return None
    return None


def _get_project_root(ctx: CommandContext | None = None) -> Path | None:
    """Get the (resolved) project root directory, or ``None``.

    Checks in order:
    1. Active run base dir (set during runner execution)
    2. Project bound to the current chat (``[projects.<alias>] chat_id``)
    3. The configured ``default_project``

    There is deliberately no ``Path.cwd()`` fallback (#389): under systemd
    that is ``$HOME``, so an unbound chat used to browse the home directory.
    """
    base = _existing_dir(get_run_base_dir())
    if base is not None:
        return base
    if ctx is None:
        return None
    try:
        chat_id = ctx.message.channel_id
        run_context = ctx.runtime.default_context_for_chat(chat_id)
        if run_context is not None:
            cwd = _existing_dir(ctx.runtime.resolve_run_cwd(run_context))
            if cwd is not None:
                return cwd
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "browse.project_root.error",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
    try:
        default_project = ctx.runtime.default_project
        if isinstance(default_project, str) and default_project:
            cwd = _existing_dir(
                ctx.runtime.resolve_run_cwd(
                    RunContext(project=default_project, branch=None)
                )
            )
            if cwd is not None:
                return cwd
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "browse.project_root.error",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
    return None


def _browse_deny_globs(ctx: CommandContext | None) -> tuple[str, ...]:
    """The live ``files.deny_globs``, or the defaults (fail closed)."""
    globs = getattr(ctx, "file_deny_globs", None)
    if isinstance(globs, (tuple, list)):
        return tuple(str(g) for g in globs)
    return tuple(TelegramFilesSettings().deny_globs)


def _access(root: Path, candidate: Path, deny_globs: Sequence[str]) -> PathAccess:
    return check_path_access(
        root,
        candidate,
        deny_globs,
        deny_hidden=True,
        hidden_allow=_HIDDEN_ALLOW,
    )


def _list_directory(
    dirpath: Path,
    root: Path | None = None,
    deny_globs: Sequence[str] | None = None,
) -> tuple[list[_Entry], list[_Entry]]:
    """List directories and files in a path, sorted alphabetically.

    Every entry goes through the same access check as a click (#389), so no
    button is offered for a path the click would refuse: hidden and
    deny-globbed entries, symlinks that resolve outside the root, and
    symlink loops are all dropped.
    """
    root = dirpath if root is None else root
    globs = (
        tuple(TelegramFilesSettings().deny_globs) if deny_globs is None else deny_globs
    )
    dirs: list[_Entry] = []
    files: list[_Entry] = []
    try:
        entries = sorted(dirpath.iterdir(), key=lambda e: e.name.lower())
    except PermissionError:
        logger.warning("browse.list_dir.permission_denied", path=str(dirpath))
        return dirs, files
    for entry in entries:
        name = entry.name
        if name in _NOISE_DIRS:
            continue
        check = _access(root, entry, globs)
        if not check.ok or check.target is None:
            if check.reason == "unresolvable":
                logger.debug(
                    "browse.list_dir.skipped", reason="unresolvable", name=name
                )
            continue
        target = check.target
        if target.is_dir():
            dirs.append(_Entry(name, target))
        elif target.is_file():
            files.append(_Entry(name, target))
    return dirs, files


def _format_size(size: int) -> str:
    """Format file size compactly."""
    if size < 1024:
        return f"{size}b"
    if size < 1024 * 1024:
        return f"{size // 1024}k"
    return f"{size // (1024 * 1024)}m"


def _count(n: int, noun: str) -> str:
    """``1 file`` / ``2 files`` (#984)."""
    return f"{n} {noun}{'s' if n != 1 else ''}"


def _format_listing(
    dirpath: Path,
    root: Path,
    dirs: list[_Entry],
    files: list[_Entry],
    *,
    chat_id: int | str,
) -> tuple[str, list[list[dict]]]:
    """Format a directory listing with inline keyboard buttons.

    Returns (text, buttons) where buttons is a list of rows. Button ids are
    registered for *chat_id* only (#210).
    """
    rel = dirpath.relative_to(root)
    rel_display = str(rel) if str(rel) != "." else "/"

    text_lines = [f"📁 {rel_display}"]

    buttons: list[list[dict]] = []

    # ".." button if not at root (re-checked like any other id on click)
    if dirpath != root:
        parent_pid = _register_path(chat_id, str(dirpath.parent))
        buttons.append([{"text": "📂 ..", "callback_data": f"browse:d:{parent_pid}"}])

    shown = 0
    truncated_dirs = 0
    truncated_files = 0

    # Collect dir buttons, then pack 2 per row
    dir_buttons: list[dict] = []
    for d in dirs:
        if shown >= _MAX_ENTRIES:
            truncated_dirs += 1
            continue
        pid = _register_path(chat_id, str(d.path))
        dir_buttons.append(
            {"text": f"📂 {d.name}/", "callback_data": f"browse:d:{pid}"}
        )
        shown += 1
    # Pack dir buttons 2 per row
    buttons.extend(dir_buttons[i : i + 2] for i in range(0, len(dir_buttons), 2))

    # Collect file buttons, pack 2 per row for short names, 1 for long
    file_buttons: list[dict] = []
    for f in files:
        if shown >= _MAX_ENTRIES:
            truncated_files += 1
            continue
        try:
            size_str = _format_size(f.path.stat().st_size)
        except OSError:
            size_str = "?"
        pid = _register_path(chat_id, str(f.path))
        file_buttons.append(
            {"text": f"📄 {f.name} ({size_str})", "callback_data": f"browse:f:{pid}"}
        )
        shown += 1
    # Pack file buttons 2 per row
    buttons.extend(file_buttons[i : i + 2] for i in range(0, len(file_buttons), 2))

    counts = []
    if dirs:
        counts.append(_count(len(dirs), "dir"))
    if files:
        counts.append(_count(len(files), "file"))
    if counts:
        text_lines.append(" · ".join(counts))

    if truncated_dirs or truncated_files:
        parts = []
        if truncated_dirs:
            parts.append(_count(truncated_dirs, "dir"))
        if truncated_files:
            parts.append(_count(truncated_files, "file"))
        text_lines.append(f"…and {' + '.join(parts)} not shown")

    return "\n".join(text_lines), buttons


def _read_file_preview(filepath: Path) -> str:
    """Read the first few lines of a file for preview."""
    try:
        size = filepath.stat().st_size
        if size == 0:
            return "(empty file)"
        # Check if likely binary
        with open(filepath, "rb") as f:
            sample = f.read(512)
        if b"\x00" in sample:
            return f"(binary file, {_format_size(size)})"
        # Read text content
        with open(filepath, encoding="utf-8", errors="replace") as f:
            lines = []
            total_chars = 0
            for i, line in enumerate(f):
                if i >= _FILE_PREVIEW_LINES:
                    lines.append(f"\n…({size} bytes total)")
                    break
                total_chars += len(line)
                if total_chars > _FILE_PREVIEW_CHARS:
                    lines.append("…(truncated)")
                    break
                lines.append(line.rstrip())
        return "\n".join(lines)
    except (OSError, UnicodeDecodeError) as exc:
        return f"(cannot read: {exc})"


class BrowseCommand:
    """Browse project files via inline keyboard navigation."""

    id = "browse"
    description = "Browse project files"

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        args = ctx.args_text.strip()
        chat_id = ctx.message.channel_id
        root = _get_project_root(ctx)
        if root is None:
            logger.info("browse.no_project_root", chat_id=chat_id)
            return CommandResult(text=_NO_ROOT_TEXT, notify=True)
        globs = _browse_deny_globs(ctx)

        # Parse args: could be a path, "d:ID", or "f:ID"
        kind: str | None = None
        if args.startswith(("d:", "f:")):
            kind = args[0]
            try:
                pid = int(args[2:])
            except ValueError:
                return CommandResult(text="Invalid path reference.", notify=True)
            path_str = _resolve_path(chat_id, pid)
            if path_str is None:
                return CommandResult(
                    text="Path expired. Use /browse to start over.",
                    notify=True,
                )
            candidate = Path(path_str)
            via = "callback"
        else:
            candidate = Path(args) if args else Path(".")
            via = "arg"

        # #389: containment on the resolved path, deny globs + hidden-path
        # denial on the requested and resolved path — before any existence
        # check, so a denied path reads the same whether or not it exists.
        check = _access(root, candidate, globs)
        if not check.ok or check.target is None:
            return self._refusal(check, candidate, args, via, chat_id)
        target = check.target

        if kind == "d":
            return await self._browse_dir(target, check.root, ctx)
        if kind == "f":
            return await self._view_file(target, check.root, ctx)
        if target.is_file():
            return await self._view_file(target, check.root, ctx)
        if target.is_dir():
            return await self._browse_dir(target, check.root, ctx)
        return CommandResult(text=f"Not found: {args}", notify=True)

    @staticmethod
    def _refusal(
        check: PathAccess,
        candidate: Path,
        args: str,
        via: str,
        chat_id: int | str,
    ) -> CommandResult:
        if check.reason == "outside":
            logger.warning(
                "browse.path_escape_attempted",
                attempted_path=str(candidate),
                root=str(check.root),
                args=args,
                via=via,
            )
            return CommandResult(text="Path outside project.", notify=True)
        logger.info(
            "browse.path_denied",
            reason=check.reason,
            rule=check.rule,
            via=via,
            chat_id=chat_id,
        )
        if check.reason == "denied":
            return CommandResult(text=f"Path denied by rule: {check.rule}", notify=True)
        if check.reason == "hidden":
            return CommandResult(text="Hidden paths can't be browsed.", notify=True)
        return CommandResult(
            text="Path could not be resolved (symlink loop?).", notify=True
        )

    async def _browse_dir(
        self,
        dirpath: Path,
        root: Path,
        ctx: CommandContext,
    ) -> CommandResult | None:
        if not dirpath.is_dir():
            return CommandResult(text="Directory not found.", notify=True)
        # Defensive: callers pass an access-checked, resolved path.
        if not dirpath.is_relative_to(root):
            logger.warning(
                "browse.path_escape_attempted",
                attempted_path=str(dirpath),
                root=str(root),
            )
            return CommandResult(text="Path outside project.", notify=True)

        dirs, files = _list_directory(dirpath, root, _browse_deny_globs(ctx))
        text, buttons = _format_listing(
            dirpath, root, dirs, files, chat_id=ctx.message.channel_id
        )

        if not dirs and not files:
            text += "\n(empty directory)"

        if buttons:
            msg = RenderedMessage(
                text=text,
                extra={
                    "reply_markup": {
                        "inline_keyboard": buttons,
                    },
                },
            )
            await ctx.executor.send(msg, reply_to=ctx.message, notify=True)
            return None  # Already sent
        return CommandResult(text=text, notify=True)

    async def _view_file(
        self, filepath: Path, root: Path, ctx: CommandContext
    ) -> CommandResult | None:
        if not filepath.is_file():
            return CommandResult(text="File not found.", notify=True)
        # Defensive: callers pass an access-checked, resolved path.
        if not filepath.is_relative_to(root):
            logger.warning(
                "browse.path_escape_attempted",
                attempted_path=str(filepath),
                root=str(root),
            )
            return CommandResult(text="Path outside project.", notify=True)

        rel = filepath.relative_to(root)
        preview = _read_file_preview(filepath)
        lang = _EXT_LANG.get(filepath.suffix.lower(), "")
        text = f"📄 {rel}\n\n```{lang}\n{preview}\n```"
        # Truncate to Telegram limit
        if len(text) > 3500:
            text = text[:3500] + "\n…(truncated)```"

        # Back button to parent directory
        parent_pid = _register_path(ctx.message.channel_id, str(filepath.parent))
        buttons = [[{"text": "📂 Back", "callback_data": f"browse:d:{parent_pid}"}]]

        msg = RenderedMessage(
            text=text,
            extra={"reply_markup": {"inline_keyboard": buttons}},
        )
        await ctx.executor.send(msg, reply_to=ctx.message, notify=True)
        return None  # Already sent


BACKEND: CommandBackend = BrowseCommand()
