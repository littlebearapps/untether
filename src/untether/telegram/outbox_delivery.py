"""Post-run outbox file delivery: scan and send files from .untether-outbox/."""

from __future__ import annotations

import functools
import io
import os
import shutil
import stat
import zipfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio

from ..logging import get_logger
from .files import deny_reason, format_bytes, resolve_path_within_root

logger = get_logger(__name__)

# #600: graveyard subdirectory for skipped outbox directories. Directories
# can't be sent as Telegram documents; leaving them in place meant they were
# re-scanned, re-skipped, and re-logged on every run forever. One-time move
# into this dot-dir stops the noise while preserving the content.
_SKIPPED_GRAVEYARD = ".skipped"

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
        # #600: never re-report the graveyard of previously-archived
        # skipped directories.
        if name == _SKIPPED_GRAVEYARD:
            continue
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


def _move_dir_to_graveyard(run_root: Path, outbox_dir: str, name: str) -> str | None:
    """#600: move one skipped directory into ``<outbox>/.skipped/``.

    Returns the relative destination path (for the user-facing notice) or
    None on failure. Name collisions get a ``_1``/``_2`` suffix (mirrors the
    upload-dedup convention in ``telegram/files.py``).
    """
    target = run_root / outbox_dir
    graveyard = target / _SKIPPED_GRAVEYARD
    src = target / name
    try:
        graveyard.mkdir(parents=True, exist_ok=True)
        dest = graveyard / name
        suffix = 0
        while dest.exists():
            suffix += 1
            dest = graveyard / f"{name}_{suffix}"
        src.rename(dest)
    except OSError:
        logger.warning(
            "outbox.skipped_dir_archive_failed",
            directory=name,
            exc_info=True,
        )
        return None
    rel_dest = str(Path(outbox_dir) / _SKIPPED_GRAVEYARD / dest.name)
    logger.info(
        "outbox.skipped_dir_archived",
        directory=name,
        moved_to=rel_dest,
    )
    return rel_dest


def _archive_skipped_dirs(
    run_root: Path,
    outbox_dir: str,
    skipped: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """#600: move skipped directories into ``<outbox>/.skipped/`` once.

    Directories can never be delivered as Telegram documents, so leaving them
    in the outbox meant per-run log/notice noise forever and the agent's
    intended deliverable silently stranded. Content is preserved (moved, not
    deleted). Returns the skipped list with archived entries' reasons
    rewritten so the user-facing notice says where the directory went. Move
    failures keep the original entry.
    """
    updated: list[tuple[str, str]] = []
    for name, reason in skipped:
        if reason != "directory":
            updated.append((name, reason))
            continue
        rel_dest = _move_dir_to_graveyard(run_root, outbox_dir, name)
        if rel_dest is None:
            updated.append((name, reason))
            continue
        updated.append((name, f"directory, moved aside to {rel_dest}"))
    return updated


@dataclass(slots=True)
class _DirZip:
    """#628: result of zipping a skipped outbox directory's members."""

    data: bytes
    included: list[str]
    excluded: list[tuple[str, str]]
    oversize: bool = False


def _copy_member_into_zip(
    zf: zipfile.ZipFile,
    fpath: Path,
    arcname: str,
    *,
    max_member_bytes: int,
    remaining_budget: int,
) -> int | str:
    """#628: open a member with O_NOFOLLOW, verify it's a regular file, and
    stream at most its fstat'd size (bounded by the per-member cap and the
    directory's remaining byte budget) into the zip.

    Opening the descriptor ONCE with O_NOFOLLOW and validating via ``fstat``
    closes the TOCTOU window between a stat() check and ``ZipFile.write``
    reopening the path — a background/orphan process cannot swap the file for
    a symlink to escape ``run_root`` or grow it past the size cap. Returns the
    number of bytes written, or a string status: ``"skip"`` (non-regular /
    symlink race / empty / unreadable), ``"too_large"``, or ``"over_budget"``.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(fpath, flags)
    except OSError:
        # ELOOP (symlink), ENOENT (raced away), EACCES, …
        return "skip"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return "skip"
        size = st.st_size
        if size == 0:
            return "skip"
        if size > max_member_bytes:
            return "too_large"
        if size > remaining_budget:
            return "over_budget"
        written = 0
        with zf.open(arcname, "w") as dest:
            while written < size:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                if written + len(chunk) > size:
                    # File grew after fstat — truncate to the validated size.
                    chunk = chunk[: size - written]
                dest.write(chunk)
                written += len(chunk)
        return written if written else "skip"
    except OSError:
        return "skip"
    finally:
        os.close(fd)


def _zip_skipped_dir(
    dir_path: Path,
    *,
    run_root: Path,
    outbox_dir: str,
    deny_globs: Sequence[str],
    max_bytes: int,
    max_members: int,
) -> _DirZip | None:
    """#628: build an in-memory zip of a skipped directory's deliverable files.

    Security: walks WITHOUT following symlinks (``os.walk(followlinks=False)``),
    prunes symlinked AND deny-globbed subdirectories (e.g. ``.git``, ``.ssh``)
    from the descent, skips symlinked files, and reads every member through an
    O_NOFOLLOW descriptor (see ``_copy_member_into_zip``) after a project-root
    containment check and a per-member ``deny_globs`` check — so nested secrets
    are never bundled and no symlink can escape ``run_root``. Per-member size,
    total uncompressed input, member count, total traversal work, and the final
    compressed zip size are all capped.

    Returns None when the directory has no deliverable members (empty, or every
    member denied/oversize/empty). Returns a ``_DirZip`` with ``oversize=True``
    when the built zip exceeds ``max_bytes`` (caller falls back to archiving).

    Synchronous + CPU/IO-heavy — call via ``anyio.to_thread`` off the loop.
    """
    included: list[str] = []
    excluded: list[tuple[str, str]] = []
    total_input = 0
    visited = 0
    # Bound total traversal work independently of deliverable count: a tree
    # full of denied/empty/symlink members never increments `included`, so
    # without this a pathological directory could spin os.walk unbounded.
    max_visited = max(max_members * 10, 200)
    truncated = False
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(dir_path, followlinks=False):
            # Deterministic order; never descend into symlinked or deny-globbed
            # subdirs (the latter keeps .git/.ssh trees off the traversal budget
            # AND out of the archive).
            kept_dirs = []
            for d in sorted(dirs):
                dpath = Path(root) / d
                if dpath.is_symlink():
                    continue
                probe = (dpath / "__probe__").relative_to(run_root)
                if deny_reason(probe, deny_globs) is not None:
                    continue
                kept_dirs.append(d)
            dirs[:] = kept_dirs
            for fname in sorted(files):
                if len(included) >= max_members:
                    excluded.append(("…", f"exceeded max_files={max_members}"))
                    truncated = True
                    break
                visited += 1
                if visited > max_visited:
                    excluded.append(("…", "exceeded traversal limit"))
                    truncated = True
                    break
                fpath = Path(root) / fname
                member_rel = str(fpath.relative_to(dir_path))
                if fpath.is_symlink():
                    excluded.append((member_rel, "symlink"))
                    continue
                # Defence-in-depth: confirm the member resolves inside run_root.
                proj_rel = fpath.relative_to(run_root)
                if resolve_path_within_root(run_root, proj_rel) is None:
                    excluded.append((member_rel, "outside project root"))
                    continue
                denied = deny_reason(proj_rel, deny_globs)
                if denied is not None:
                    excluded.append((member_rel, f"denied by glob: {denied}"))
                    continue
                # arcname keeps the top directory as the zip's root folder.
                arcname = str(fpath.relative_to(dir_path.parent))
                added = _copy_member_into_zip(
                    zf,
                    fpath,
                    arcname,
                    max_member_bytes=max_bytes,
                    remaining_budget=max_bytes - total_input,
                )
                if added == "skip":
                    continue
                if added == "too_large":
                    excluded.append((member_rel, "member too large"))
                    continue
                if added == "over_budget":
                    excluded.append((member_rel, "directory total too large"))
                    truncated = True
                    break
                total_input += added  # bytes written
                included.append(member_rel)
            if truncated:
                break
    if not included:
        return None
    data = buf.getvalue()
    if len(data) > max_bytes:
        return _DirZip(data=b"", included=included, excluded=excluded, oversize=True)
    return _DirZip(data=data, included=included, excluded=excluded)


def _fallback_archive(
    run_root: Path,
    outbox_dir: str,
    name: str,
    reason: str,
    why: str,
) -> tuple[str, str]:
    """#628: move a non-deliverable directory to the #600 graveyard and build
    its user-facing skip reason. Keeps the original reason if the move fails."""
    rel_dest = _move_dir_to_graveyard(run_root, outbox_dir, name)
    if rel_dest is None:
        return (name, reason)
    return (name, f"directory ({why}), moved aside to {rel_dest}")


async def _deliver_skipped_dirs_as_zip(
    *,
    send_file: SendFileFunc,
    channel_id: int,
    thread_id: int | None,
    reply_to_msg_id: int | None,
    run_root: Path,
    outbox_dir: str,
    skipped: list[tuple[str, str]],
    deny_globs: Sequence[str],
    max_bytes: int,
    max_members: int,
) -> list[tuple[str, str]]:
    """#628: zip each skipped directory and send it as one Telegram document,
    then remove the delivered directory. Directories with no deliverable
    members, an oversize zip, a build error, or a send failure fall back to the
    #600 archive so they don't re-scan forever. The number of directory
    attachments is capped (``max_members``) so a pathological outbox with
    thousands of directories can't flood the chat. Returns the skipped list
    with reasons rewritten for the user-facing notice.
    """
    target = run_root / outbox_dir
    updated: list[tuple[str, str]] = []
    dirs_attempted = 0
    for name, reason in skipped:
        if reason != "directory":
            updated.append((name, reason))
            continue
        if dirs_attempted >= max_members:
            # Attachment-count cap: archive the remainder instead of flooding.
            updated.append(
                _fallback_archive(
                    run_root, outbox_dir, name, reason, "attachment limit reached"
                )
            )
            continue
        dirs_attempted += 1
        dir_path = target / name
        # Compression + descriptor IO is CPU/memory-heavy — keep it off the loop.
        try:
            result = await anyio.to_thread.run_sync(
                functools.partial(
                    _zip_skipped_dir,
                    dir_path,
                    run_root=run_root,
                    outbox_dir=outbox_dir,
                    deny_globs=deny_globs,
                    max_bytes=max_bytes,
                    max_members=max_members,
                )
            )
        except Exception:  # noqa: BLE001
            logger.warning("outbox.dir_zip_failed", directory=name, exc_info=True)
            updated.append(
                _fallback_archive(run_root, outbox_dir, name, reason, "zip failed")
            )
            continue
        if result is None or result.oversize:
            why = "no deliverable files" if result is None else "too large to zip"
            updated.append(_fallback_archive(run_root, outbox_dir, name, reason, why))
            continue
        zip_name = f"{name}.zip"
        caption = (
            f"\U0001f4ce {zip_name} "
            f"({len(result.included)} files, {format_bytes(len(result.data))})"
        )
        try:
            await send_file(
                channel_id,
                thread_id,
                zip_name,
                result.data,
                reply_to_msg_id,
                caption,
            )
        except Exception:  # noqa: BLE001
            logger.warning("outbox.dir_zip_send_failed", directory=name, exc_info=True)
            updated.append(
                _fallback_archive(run_root, outbox_dir, name, reason, "delivery failed")
            )
            continue
        # Delivered — remove the source directory so it isn't re-scanned.
        try:
            shutil.rmtree(dir_path)
        except OSError:
            logger.warning("outbox.dir_cleanup_failed", directory=name, exc_info=True)
        logger.info(
            "outbox.dir_delivered_zip",
            directory=name,
            files=len(result.included),
            excluded=len(result.excluded),
            bytes=len(result.data),
        )
        note = f"directory delivered as {zip_name} ({len(result.included)} files)"
        if result.excluded:
            note += f", {len(result.excluded)} member(s) skipped"
        updated.append((name, note))
    return updated


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
    deliver_directories: str = "off",
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
        # Gated on the same ``cleanup`` flag as sent-file deletion
        # (``outbox_cleanup = false`` keeps the outbox untouched).
        if cleanup:
            if deliver_directories == "zip":
                # #628: bundle each skipped directory into a <name>.zip and
                # send it; empty/oversize dirs fall back to the #600 archive.
                skipped = await _deliver_skipped_dirs_as_zip(
                    send_file=send_file,
                    channel_id=channel_id,
                    thread_id=thread_id,
                    reply_to_msg_id=reply_to_msg_id,
                    run_root=run_root,
                    outbox_dir=outbox_dir,
                    skipped=skipped,
                    deny_globs=deny_globs,
                    max_bytes=max_download_bytes,
                    max_members=max_files,
                )
            else:
                # #600: archive skipped directories once so they don't re-log
                # and re-notify on every subsequent run.
                skipped = _archive_skipped_dirs(run_root, outbox_dir, skipped)

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
