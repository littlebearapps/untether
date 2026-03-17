"""Post-run outbox file delivery: scan and send files from .untether-outbox/."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..logging import get_logger
from .files import deny_reason, format_bytes, resolve_path_within_root

logger = get_logger(__name__)

SendFileFunc = Callable[
    [int, int | None, str, bytes, int | None, str | None],
    Awaitable[Any],
]


@dataclass(frozen=True, slots=True)
class OutboxFile:
    """A validated file ready to be sent from the outbox."""

    rel_path: Path
    abs_path: Path
    size: int


@dataclass(slots=True)
class OutboxResult:
    """Outcome of an outbox delivery attempt."""

    sent: list[OutboxFile] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    cleaned: bool = False


def scan_outbox(
    run_root: Path,
    *,
    outbox_dir: str,
    deny_globs: Sequence[str],
    max_download_bytes: int,
    max_files: int,
) -> tuple[list[OutboxFile], list[tuple[str, str]]]:
    """Scan the outbox directory for files to send.

    Returns (files_to_send, skipped_with_reason). Flat scan only — no recursion.
    """
    target = run_root / outbox_dir
    if not target.is_dir():
        return [], []

    files: list[OutboxFile] = []
    skipped: list[tuple[str, str]] = []

    entries = sorted(target.iterdir(), key=lambda p: p.name)
    for entry in entries:
        name = entry.name
        if not entry.is_file() or entry.is_symlink():
            if entry.is_symlink():
                skipped.append((name, "symlink"))
            elif entry.is_dir():
                skipped.append((name, "directory"))
            continue

        rel_path = Path(outbox_dir) / name

        # Security: resolve within project root
        resolved = resolve_path_within_root(run_root, rel_path)
        if resolved is None:
            skipped.append((name, "outside project root"))
            continue

        # Deny globs
        denied = deny_reason(rel_path, deny_globs)
        if denied is not None:
            skipped.append((name, f"denied by glob: {denied}"))
            continue

        # Size check
        try:
            size = entry.stat().st_size
        except OSError:
            skipped.append((name, "stat failed"))
            continue

        if size > max_download_bytes:
            skipped.append(
                (
                    name,
                    f"too large: {format_bytes(size)} > {format_bytes(max_download_bytes)}",
                )
            )
            continue

        if size == 0:
            skipped.append((name, "empty file"))
            continue

        files.append(OutboxFile(rel_path=rel_path, abs_path=resolved, size=size))

        if len(files) >= max_files:
            # Skip remaining entries
            remaining = len(entries) - (entries.index(entry) + 1)
            if remaining > 0:
                skipped.append(
                    ("...", f"{remaining} more files exceeded max_files={max_files}")
                )
            break

    return files, skipped


def cleanup_outbox(
    run_root: Path,
    outbox_dir: str,
    sent_files: Sequence[OutboxFile],
) -> bool:
    """Delete sent files and remove the outbox directory if empty.

    Returns True if the directory was removed.
    """
    for f in sent_files:
        try:
            f.abs_path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "outbox.cleanup.unlink_failed", file=str(f.rel_path), exc_info=True
            )

    target = run_root / outbox_dir
    try:
        if target.is_dir() and not any(target.iterdir()):
            target.rmdir()
            return True
    except OSError:
        logger.debug("outbox.cleanup.rmdir_failed", exc_info=True)
    return False


async def deliver_outbox_files(
    *,
    send_file: SendFileFunc,
    channel_id: int,
    thread_id: int | None,
    reply_to_msg_id: int | None,
    run_root: Path,
    outbox_dir: str,
    deny_globs: Sequence[str],
    max_download_bytes: int,
    max_files: int,
    cleanup: bool,
) -> OutboxResult:
    """Scan outbox, send files as Telegram documents, and optionally clean up."""
    files, skipped = scan_outbox(
        run_root,
        outbox_dir=outbox_dir,
        deny_globs=deny_globs,
        max_download_bytes=max_download_bytes,
        max_files=max_files,
    )

    if not files and not skipped:
        return OutboxResult()

    if skipped:
        logger.info("outbox.skipped", skipped=skipped)

    result = OutboxResult(skipped=skipped)

    for f in files:
        try:
            payload = f.abs_path.read_bytes()
            caption = f"\U0001f4ce {f.abs_path.name} ({format_bytes(f.size)})"
            await send_file(
                channel_id,
                thread_id,
                f.abs_path.name,
                payload,
                reply_to_msg_id,
                caption,
            )
            result.sent.append(f)
            logger.info(
                "outbox.sent",
                file=str(f.rel_path),
                size=f.size,
            )
        except Exception:  # noqa: BLE001
            logger.warning("outbox.send_failed", file=str(f.rel_path), exc_info=True)

    if cleanup and result.sent:
        result.cleaned = cleanup_outbox(run_root, outbox_dir, result.sent)

    if result.sent:
        logger.info(
            "outbox.delivered",
            sent=len(result.sent),
            skipped=len(result.skipped),
            cleaned=result.cleaned,
        )

    return result
