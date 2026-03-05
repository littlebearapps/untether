"""Command backend for AMP thread management via inline keyboard."""

from __future__ import annotations

import json
import shutil

import anyio

from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger
from ...transport import RenderedMessage

logger = get_logger(__name__)

# Thread registry: short ID -> thread ID string
# Avoids long T-<uuid> values in 64-byte callback_data
_THREAD_REGISTRY: dict[int, str] = {}
_THREAD_COUNTER: int = 0
_MAX_REGISTRY = 200

# Limits
_MAX_THREADS = 15


def _register_thread(thread_id: str) -> int:
    """Register a thread ID and return a short numeric ID."""
    global _THREAD_COUNTER
    for tid, t in _THREAD_REGISTRY.items():
        if t == thread_id:
            return tid
    _THREAD_COUNTER += 1
    tid = _THREAD_COUNTER
    _THREAD_REGISTRY[tid] = thread_id
    if len(_THREAD_REGISTRY) > _MAX_REGISTRY:
        oldest = min(_THREAD_REGISTRY)
        _THREAD_REGISTRY.pop(oldest, None)
    return tid


def _resolve_thread(tid: int) -> str | None:
    """Look up a registered thread by ID."""
    return _THREAD_REGISTRY.get(tid)


async def _run_amp_command(*args: str) -> tuple[int, str, str]:
    """Run an amp CLI command and return (returncode, stdout, stderr)."""
    amp_path = shutil.which("amp")
    if amp_path is None:
        return 1, "", "amp CLI not found in PATH"
    result = await anyio.run_process(
        [amp_path, *args],
        check=False,
    )
    return (
        result.returncode,
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
    )


def _format_thread_list(
    threads: list[dict],
) -> tuple[str, list[list[dict]]]:
    """Format a thread list with inline keyboard buttons."""
    if not threads:
        return "No AMP threads found.", []

    lines = ["<b>AMP threads</b>"]
    buttons: list[list[dict]] = []

    for thread in threads[:_MAX_THREADS]:
        thread_id = thread.get("id", "")
        title = thread.get("title") or thread.get("name") or thread_id
        # Truncate long titles
        if len(title) > 40:
            title = title[:37] + "..."

        tid = _register_thread(thread_id)
        short_id = thread_id[-8:] if len(thread_id) > 8 else thread_id
        buttons.append(
            [{"text": f"{title} ({short_id})", "callback_data": f"threads:v:{tid}"}]
        )

    if len(threads) > _MAX_THREADS:
        lines.append(f"\nShowing {_MAX_THREADS} of {len(threads)} threads.")

    return "\n".join(lines), buttons


def _format_thread_detail(thread_id: str, info: dict) -> tuple[str, list[list[dict]]]:
    """Format a single thread's details with action buttons."""
    title = info.get("title") or info.get("name") or thread_id
    lines = [
        f"<b>{title}</b>",
        f"<code>{thread_id}</code>",
    ]
    if info.get("created_at"):
        lines.append(f"Created: {info['created_at']}")
    if info.get("updated_at"):
        lines.append(f"Updated: {info['updated_at']}")
    if info.get("num_turns"):
        lines.append(f"Turns: {info['num_turns']}")

    tid = _register_thread(thread_id)
    buttons: list[list[dict]] = [
        [
            {"text": "Resume", "callback_data": f"threads:r:{tid}"},
            {"text": "Archive", "callback_data": f"threads:a:{tid}"},
        ],
        [{"text": "Back to list", "callback_data": "threads:list"}],
    ]

    return "\n".join(lines), buttons


class ThreadsCommand:
    """Manage AMP threads via inline keyboard."""

    id = "threads"
    description = "List and manage AMP threads"

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        # Check amp is available
        if shutil.which("amp") is None:
            return CommandResult(
                text="AMP CLI not found. Install with: npm install -g @sourcegraph/amp",
                notify=True,
            )

        args = ctx.args_text.strip()

        # Callback routing
        if args.startswith("v:"):
            return await self._view_thread(args[2:], ctx)
        if args.startswith("r:"):
            return await self._resume_thread(args[2:], ctx)
        if args.startswith("a:"):
            return await self._archive_thread(args[2:], ctx)
        if args == "list" or not args:
            return await self._list_threads(ctx)
        if args.startswith("search "):
            return await self._search_threads(args[7:].strip(), ctx)

        return CommandResult(
            text="Usage: /threads [search <query>]",
            notify=True,
        )

    async def _list_threads(self, ctx: CommandContext) -> CommandResult | None:
        rc, stdout, stderr = await _run_amp_command("threads", "list", "--json")
        if rc != 0:
            error = stderr.strip() or f"amp threads list failed (exit {rc})"
            return CommandResult(text=f"Error: {error}", notify=True)

        try:
            threads = json.loads(stdout) if stdout.strip() else []
        except json.JSONDecodeError:
            # Try line-by-line JSONL
            threads = []
            for line in stdout.strip().splitlines():
                try:
                    threads.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        if not isinstance(threads, list):
            threads = [threads] if isinstance(threads, dict) else []

        text, buttons = _format_thread_list(threads)
        if buttons:
            msg = RenderedMessage(
                text=text,
                extra={
                    "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": buttons},
                },
            )
            await ctx.executor.send(msg, reply_to=ctx.message, notify=True)
            return None
        return CommandResult(text=text, notify=True, parse_mode="HTML")

    async def _search_threads(
        self, query: str, ctx: CommandContext
    ) -> CommandResult | None:
        if not query:
            return CommandResult(text="Usage: /threads search <query>", notify=True)

        rc, stdout, stderr = await _run_amp_command(
            "threads", "search", query, "--json"
        )
        if rc != 0:
            error = stderr.strip() or f"amp threads search failed (exit {rc})"
            return CommandResult(text=f"Error: {error}", notify=True)

        try:
            threads = json.loads(stdout) if stdout.strip() else []
        except json.JSONDecodeError:
            threads = []

        if not isinstance(threads, list):
            threads = [threads] if isinstance(threads, dict) else []

        text, buttons = _format_thread_list(threads)
        if buttons:
            text = f"Search: {query}\n\n{text}"
            msg = RenderedMessage(
                text=text,
                extra={
                    "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": buttons},
                },
            )
            await ctx.executor.send(msg, reply_to=ctx.message, notify=True)
            return None
        return CommandResult(text=text, notify=True, parse_mode="HTML")

    async def _view_thread(
        self, tid_str: str, ctx: CommandContext
    ) -> CommandResult | None:
        try:
            tid = int(tid_str)
        except ValueError:
            return CommandResult(text="Invalid thread reference.", notify=True)
        thread_id = _resolve_thread(tid)
        if thread_id is None:
            return CommandResult(
                text="Thread expired. Use /threads to refresh.", notify=True
            )

        # Build info dict from what we know
        info: dict = {"id": thread_id}
        text, buttons = _format_thread_detail(thread_id, info)
        msg = RenderedMessage(
            text=text,
            extra={
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": buttons},
            },
        )
        await ctx.executor.send(msg, reply_to=ctx.message, notify=True)
        return None

    async def _resume_thread(
        self, tid_str: str, ctx: CommandContext
    ) -> CommandResult | None:
        try:
            tid = int(tid_str)
        except ValueError:
            return CommandResult(text="Invalid thread reference.", notify=True)
        thread_id = _resolve_thread(tid)
        if thread_id is None:
            return CommandResult(
                text="Thread expired. Use /threads to refresh.", notify=True
            )
        return CommandResult(
            text=f"To resume this thread, send:\n<code>amp threads continue {thread_id}</code>",
            notify=True,
            parse_mode="HTML",
        )

    async def _archive_thread(
        self, tid_str: str, ctx: CommandContext
    ) -> CommandResult | None:
        try:
            tid = int(tid_str)
        except ValueError:
            return CommandResult(text="Invalid thread reference.", notify=True)
        thread_id = _resolve_thread(tid)
        if thread_id is None:
            return CommandResult(
                text="Thread expired. Use /threads to refresh.", notify=True
            )

        rc, _stdout, stderr = await _run_amp_command("threads", "archive", thread_id)
        if rc != 0:
            error = stderr.strip() or f"archive failed (exit {rc})"
            return CommandResult(text=f"Error: {error}", notify=True)
        return CommandResult(
            text=f"Thread <code>{thread_id}</code> archived.",
            notify=True,
            parse_mode="HTML",
        )


BACKEND: CommandBackend = ThreadsCommand()
