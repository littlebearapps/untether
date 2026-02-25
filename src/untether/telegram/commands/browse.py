"""Command backend for browsing project files via inline keyboard."""

from __future__ import annotations

from pathlib import Path

from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger
from ...transport import RenderedMessage
from ...utils.paths import get_run_base_dir

logger = get_logger(__name__)

# Path registry: short ID -> absolute path string
# This avoids long paths in 64-byte callback_data
_PATH_REGISTRY: dict[int, str] = {}
_PATH_COUNTER: int = 0
_MAX_REGISTRY = 500

# Limits
_MAX_ENTRIES = 20  # max items shown in one listing
_FILE_PREVIEW_LINES = 25
_FILE_PREVIEW_CHARS = 2000


def _register_path(path: str) -> int:
    """Register a path and return a short numeric ID."""
    global _PATH_COUNTER
    # Check if already registered
    for pid, p in _PATH_REGISTRY.items():
        if p == path:
            return pid
    _PATH_COUNTER += 1
    pid = _PATH_COUNTER
    _PATH_REGISTRY[pid] = path
    # Trim old entries
    if len(_PATH_REGISTRY) > _MAX_REGISTRY:
        oldest = min(_PATH_REGISTRY)
        _PATH_REGISTRY.pop(oldest, None)
    return pid


def _resolve_path(pid: int) -> str | None:
    """Look up a registered path by ID."""
    return _PATH_REGISTRY.get(pid)


def _get_project_root(ctx: CommandContext | None = None) -> Path | None:
    """Get the project root directory.

    Checks in order:
    1. Active run base dir (set during runner execution)
    2. Project path for the current chat (from config)
    3. Process CWD as fallback
    """
    base = get_run_base_dir()
    if base is not None:
        return base
    # Try to resolve from chat's project config
    if ctx is not None:
        try:
            chat_id = ctx.message.channel_id
            run_context = ctx.runtime.default_context_for_chat(chat_id)
            if run_context is not None:
                cwd = ctx.runtime.resolve_run_cwd(run_context)
                if cwd is not None and cwd.is_dir():
                    return cwd
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "browse.project_root.error",
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
    return Path.cwd()


def _list_directory(dirpath: Path) -> tuple[list[Path], list[Path]]:
    """List directories and files in a path, sorted alphabetically."""
    dirs: list[Path] = []
    files: list[Path] = []
    try:
        for entry in sorted(dirpath.iterdir(), key=lambda e: e.name.lower()):
            name = entry.name
            # Skip hidden files and common noise
            if name.startswith(".") and name not in {".env.example"}:
                continue
            if name in {"__pycache__", "node_modules", ".git", ".venv", "venv"}:
                continue
            if entry.is_dir():
                dirs.append(entry)
            elif entry.is_file():
                files.append(entry)
    except PermissionError:
        logger.warning("browse.list_dir.permission_denied", path=str(dirpath))
    return dirs, files


def _format_size(size: int) -> str:
    """Format file size compactly."""
    if size < 1024:
        return f"{size}b"
    if size < 1024 * 1024:
        return f"{size // 1024}k"
    return f"{size // (1024 * 1024)}m"


def _format_listing(
    dirpath: Path,
    root: Path,
    dirs: list[Path],
    files: list[Path],
) -> tuple[str, list[list[dict]]]:
    """Format a directory listing with inline keyboard buttons.

    Returns (text, buttons) where buttons is a list of rows.
    """
    rel = dirpath.relative_to(root)
    rel_display = str(rel) if str(rel) != "." else "/"

    text_lines = [f"📁 {rel_display}"]

    buttons: list[list[dict]] = []

    # ".." button if not at root
    if dirpath != root:
        parent_pid = _register_path(str(dirpath.parent))
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
        pid = _register_path(str(d))
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
            size_str = _format_size(f.stat().st_size)
        except OSError:
            size_str = "?"
        pid = _register_path(str(f))
        file_buttons.append(
            {"text": f"📄 {f.name} ({size_str})", "callback_data": f"browse:f:{pid}"}
        )
        shown += 1
    # Pack file buttons 2 per row
    buttons.extend(file_buttons[i : i + 2] for i in range(0, len(file_buttons), 2))

    counts = []
    if dirs:
        counts.append(f"{len(dirs)} dirs")
    if files:
        counts.append(f"{len(files)} files")
    if counts:
        text_lines.append(" · ".join(counts))

    if truncated_dirs or truncated_files:
        parts = []
        if truncated_dirs:
            parts.append(f"{truncated_dirs} dirs")
        if truncated_files:
            parts.append(f"{truncated_files} files")
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
        root = _get_project_root(ctx)
        if root is None:
            return CommandResult(text="No project directory set.", notify=True)

        # Parse args: could be a path, "d:ID", or "f:ID"
        if args.startswith("d:"):
            # Directory navigation callback
            try:
                pid = int(args[2:])
            except ValueError:
                return CommandResult(text="Invalid path reference.", notify=True)
            path_str = _resolve_path(pid)
            if path_str is None:
                return CommandResult(
                    text="Path expired. Use /browse to start over.",
                    notify=True,
                )
            return await self._browse_dir(Path(path_str), root, ctx)
        if args.startswith("f:"):
            # File view callback
            try:
                pid = int(args[2:])
            except ValueError:
                return CommandResult(text="Invalid path reference.", notify=True)
            path_str = _resolve_path(pid)
            if path_str is None:
                return CommandResult(
                    text="Path expired. Use /browse to start over.",
                    notify=True,
                )
            return self._view_file(Path(path_str), root)
        # Direct path argument
        if args:
            target = (root / args).resolve()
            if not target.is_relative_to(root):
                return CommandResult(text="Path outside project.", notify=True)
        else:
            target = root

        if target.is_file():
            return self._view_file(target, root)
        if target.is_dir():
            return await self._browse_dir(target, root, ctx)
        return CommandResult(text=f"Not found: {args}", notify=True)

    async def _browse_dir(
        self,
        dirpath: Path,
        root: Path,
        ctx: CommandContext,
    ) -> CommandResult | None:
        if not dirpath.is_dir():
            return CommandResult(text="Directory not found.", notify=True)
        if not dirpath.is_relative_to(root):
            return CommandResult(text="Path outside project.", notify=True)

        dirs, files = _list_directory(dirpath)
        text, buttons = _format_listing(dirpath, root, dirs, files)

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

    def _view_file(self, filepath: Path, root: Path) -> CommandResult:
        if not filepath.is_file():
            return CommandResult(text="File not found.", notify=True)
        if not filepath.is_relative_to(root):
            return CommandResult(text="Path outside project.", notify=True)

        rel = filepath.relative_to(root)
        preview = _read_file_preview(filepath)
        text = f"📄 {rel}\n\n```\n{preview}\n```"
        # Truncate to Telegram limit
        if len(text) > 3500:
            text = text[:3500] + "\n…(truncated)```"
        return CommandResult(text=text, notify=True)


BACKEND: CommandBackend = BrowseCommand()
