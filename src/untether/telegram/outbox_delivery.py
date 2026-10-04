"""Post-run outbox file delivery: scan and send files from .untether-outbox/."""

from __future__ import annotations

import contextlib
import functools
import io
import os
import shutil
import stat
import zipfile
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
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
    # #924: entries that predate the run (name, changed-at epoch seconds),
    # and where they were quarantined (None = left in place).
    stale: list[tuple[str, float]] = field(default_factory=list)
    stale_archived_to: str | None = None
    # #924: fresh files beyond ``max_files`` — surfaced, never auto-sent later.
    overflow: list[str] = field(default_factory=list)
    overflow_archived_to: str | None = None
    overflow_limit: int | None = None
    cleanup: bool = False
    outbox_dir: str = ".untether-outbox"


# #924: an outbox entry is "fresh" for a run when it last changed (or arrived)
# no earlier than this many seconds before the run started. Covers coarse
# filesystem timestamps (HFS+ 1 s, FAT 2 s) and small clock skew.
_OUTBOX_FRESH_GRACE_S = 5.0

SeenKey = tuple[str, int, int]


def entry_changed_at(st: os.stat_result) -> float:
    """#924: when an entry last changed or entered the directory.

    ``st_mtime`` alone is user-settable and survives ``mv`` / ``cp -p`` /
    ``rsync -a``, so a report written last week and moved into the outbox
    today would look old. ``st_ctime`` can't be set from userspace and is
    bumped by ``rename(2)`` and by creating a new inode, so the max of the two
    answers "when did this land here".
    """
    return max(st.st_mtime, st.st_ctime)


def _seen_key(name: str, st: os.stat_result) -> SeenKey:
    return (name, st.st_mtime_ns, st.st_size)


def outbox_dir_unsafe_reason(run_root: Path, outbox_dir: str) -> str | None:
    """#924 review: why the outbox directory must not be scanned, or None.

    Stale archiving ``os.rename``s every older entry of the outbox into
    ``<outbox>/.skipped/``, so the directory itself must be a real directory
    inside the project: no ``..``/absolute path, no symlinked component
    between ``run_root`` and the outbox (a ``.untether-outbox`` symlink to
    ``~/Downloads`` would otherwise quarantine the user's downloads), and it
    must resolve within ``run_root``. ``run_root`` itself may be a symlink.
    """
    rel = Path(outbox_dir)
    if rel.is_absolute() or ".." in rel.parts:
        return "outbox_dir is not a plain relative path"
    current = run_root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            return f"symlinked path component: {current.relative_to(run_root)}"
    if resolve_path_within_root(run_root, rel) is None:
        return "resolves outside the project root"
    return None


@dataclass(slots=True)
class OutboxScan:
    """#924: full classification of one outbox scan."""

    files: list[OutboxFile] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    stale: list[tuple[str, float]] = field(default_factory=list)
    overflow: list[str] = field(default_factory=list)
    keys: dict[str, SeenKey] = field(default_factory=dict)


def classify_outbox(
    run_root: Path,
    *,
    outbox_dir: str,
    deny_globs: Sequence[str],
    max_download_bytes: int,
    max_files: int,
    send_after: float | None = None,
    archive_before: float | None = None,
    seen: set[SeenKey] | None = None,
) -> OutboxScan:
    """Classify every outbox entry. Flat scan only — no recursion.

    #924 cutoffs (epoch seconds, already grace-adjusted by the caller):

    - ``changed < archive_before`` → ``stale`` (predates every active run on
      this project root);
    - ``changed < send_after`` → left alone (another, earlier-started run on
      the same root may still deliver it);
    - otherwise the existing checks, then the ``max_files`` cap applies to the
      deliverable files only (the rest → ``overflow``).

    ``None`` cutoffs = no age classification (legacy / ``send`` policy).
    Entries whose seen-key is in ``seen`` were already handled this run.
    Every entry is ``lstat``-ed once; symlinks are never followed.
    """
    scan = OutboxScan()
    target = run_root / outbox_dir
    if not target.is_dir():
        return scan
    unsafe = outbox_dir_unsafe_reason(run_root, outbox_dir)
    if unsafe is not None:
        # Nothing is sent, reported stale or moved from a directory that
        # isn't really the project's own outbox.
        logger.warning(
            "outbox.outside_root",
            outbox_dir=outbox_dir,
            run_root=str(run_root),
            reason=unsafe,
        )
        return scan

    if (
        archive_before is not None
        and send_after is not None
        and archive_before > send_after
    ):
        archive_before = send_after

    deliverable: list[OutboxFile] = []
    for entry in sorted(target.iterdir(), key=lambda p: p.name):
        name = entry.name
        # #600: never re-report the graveyard of previously-archived skipped
        # entries — and (#924 amendment 2) check it BEFORE freshness so the
        # graveyard can never be classified stale and archived into itself.
        if name == _SKIPPED_GRAVEYARD:
            continue
        try:
            st = entry.lstat()
        except OSError:
            scan.skipped.append((name, "stat failed"))
            continue
        key = _seen_key(name, st)
        if seen is not None and key in seen:
            continue
        changed = entry_changed_at(st)
        if archive_before is not None and changed < archive_before:
            scan.stale.append((name, changed))
            scan.keys[name] = key
            continue
        if send_after is not None and changed < send_after:
            continue  # between the cutoffs: owned by an earlier active run
        scan.keys[name] = key

        if stat.S_ISLNK(st.st_mode):
            scan.skipped.append((name, "symlink"))
            continue
        if stat.S_ISDIR(st.st_mode):
            scan.skipped.append((name, "directory"))
            continue
        if not stat.S_ISREG(st.st_mode):
            scan.keys.pop(name, None)
            continue

        rel_path = Path(outbox_dir) / name

        # Security: resolve within project root
        resolved = resolve_path_within_root(run_root, rel_path)
        if resolved is None:
            scan.skipped.append((name, "outside project root"))
            continue

        # Deny globs
        denied = deny_reason(rel_path, deny_globs)
        if denied is not None:
            scan.skipped.append((name, f"denied by glob: {denied}"))
            continue

        size = st.st_size
        if size > max_download_bytes:
            scan.skipped.append(
                (
                    name,
                    f"too large: {format_bytes(size)} > {format_bytes(max_download_bytes)}",
                )
            )
            continue

        if size == 0:
            scan.skipped.append((name, "empty file"))
            continue

        deliverable.append(OutboxFile(rel_path=rel_path, abs_path=resolved, size=size))

    # #924: classify everything first, then cap the deliverable files only —
    # the old ``break`` left later entries (dirs included) unclassified.
    scan.files = deliverable[:max_files]
    scan.overflow = [f.rel_path.name for f in deliverable[max_files:]]
    return scan


def scan_outbox(
    run_root: Path,
    *,
    outbox_dir: str,
    deny_globs: Sequence[str],
    max_download_bytes: int,
    max_files: int,
) -> tuple[list[OutboxFile], list[tuple[str, str]]]:
    """Scan the outbox directory for files to send (no age classification).

    Returns (files_to_send, skipped_with_reason); files beyond ``max_files``
    are summarised as a ``("...", "N more files exceeded max_files=…")``
    entry. Thin wrapper over :func:`classify_outbox`.
    """
    scan = classify_outbox(
        run_root,
        outbox_dir=outbox_dir,
        deny_globs=deny_globs,
        max_download_bytes=max_download_bytes,
        max_files=max_files,
    )
    skipped = list(scan.skipped)
    if scan.overflow:
        skipped.append(
            ("...", f"{len(scan.overflow)} more files exceeded max_files={max_files}")
        )
    return scan.files, skipped


# ── #924: active-run registry (archive cutoff across same-root runs) ──────

_ACTIVE_RUNS: dict[str, list[float]] = {}


def _registry_key(run_root: Path | str) -> str:
    """One key derivation for registration and lookup (amendment 1): the
    resolved form of the run's cwd, i.e. what ``set_run_base_dir(cwd)``
    stored and ``get_run_base_dir()`` returns at delivery."""
    try:
        return str(Path(run_root).resolve())
    except (OSError, RuntimeError):
        return str(run_root)


@contextmanager
def outbox_run_scope(run_root: Path | None, since: float) -> Iterator[None]:
    """Register an active run on ``run_root`` that started at ``since``.

    While registered, other runs on the same root won't archive outbox
    entries newer than ``since`` — they may be this run's deliverables.
    ``None`` (no cwd) registers nothing.
    """
    if run_root is None:
        yield
        return
    key = _registry_key(run_root)
    _ACTIVE_RUNS.setdefault(key, []).append(since)
    try:
        yield
    finally:
        runs = _ACTIVE_RUNS.get(key)
        if runs is not None:
            with contextlib.suppress(ValueError):
                runs.remove(since)
            if not runs:
                _ACTIVE_RUNS.pop(key, None)


def oldest_active_since(run_root: Path | None, default: float) -> float:
    """The earliest start among active runs on ``run_root`` (or ``default``)."""
    if run_root is None:
        return default
    runs = _ACTIVE_RUNS.get(_registry_key(run_root))
    if not runs:
        return default
    return min(default, *runs)


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


def _graveyard_dest(graveyard: Path, name: str, *, is_dir: bool) -> Path:
    """Collision-free destination: directories get ``name_N``, files keep
    their extension (``plan_1.md``, #924). ``lexists`` so a dangling symlink
    at the destination is never silently replaced."""
    dest = graveyard / name
    suffix = 0
    stem, ext = (name, "") if is_dir else (Path(name).stem, Path(name).suffix)
    while os.path.lexists(dest):
        suffix += 1
        dest = graveyard / f"{stem}_{suffix}{ext}"
    return dest


def _move_to_graveyard(run_root: Path, outbox_dir: str, name: str) -> str | None:
    """Move one outbox entry into ``<outbox>/.skipped/`` (#600, #924).

    ``os.rename`` on the entry itself: a symlink is moved, never followed, and
    nothing is copied. Refuses a graveyard that is itself a symlink (it could
    point outside the project). Raises ``OSError`` on failure; returns the
    relative destination path.
    """
    unsafe = outbox_dir_unsafe_reason(run_root, outbox_dir)
    if unsafe is not None:
        raise OSError(f"{outbox_dir}: {unsafe}; refusing to archive from it")
    target = run_root / outbox_dir
    graveyard = target / _SKIPPED_GRAVEYARD
    src = target / name
    if graveyard.is_symlink():
        raise OSError(f"{graveyard} is a symlink; refusing to archive into it")
    graveyard.mkdir(parents=True, exist_ok=True)
    if graveyard.is_symlink() or not graveyard.is_dir():
        raise OSError(f"{graveyard} is not a directory")
    is_dir = stat.S_ISDIR(src.lstat().st_mode)
    dest = _graveyard_dest(graveyard, name, is_dir=is_dir)
    os.rename(src, dest)
    return str(Path(outbox_dir) / _SKIPPED_GRAVEYARD / dest.name)


def _move_dir_to_graveyard(run_root: Path, outbox_dir: str, name: str) -> str | None:
    """#600: move one skipped directory into ``<outbox>/.skipped/``.

    Returns the relative destination path (for the user-facing notice) or
    None on failure. Name collisions get a ``_1``/``_2`` suffix (mirrors the
    upload-dedup convention in ``telegram/files.py``).
    """
    try:
        rel_dest = _move_to_graveyard(run_root, outbox_dir, name)
    except OSError:
        logger.warning(
            "outbox.skipped_dir_archive_failed",
            directory=name,
            exc_info=True,
        )
        return None
    logger.info(
        "outbox.skipped_dir_archived",
        directory=name,
        moved_to=rel_dest,
    )
    return rel_dest


def _archive_entries(
    run_root: Path, outbox_dir: str, names: Sequence[str]
) -> str | None:
    """#924: quarantine stale / overflow entries in ``<outbox>/.skipped/``.

    Returns the graveyard's relative path (``.untether-outbox/.skipped/``)
    when every move succeeded, else None. A failed move logs
    ``outbox.stale_archive_failed`` and leaves that entry in place.
    """
    ok = True
    for name in names:
        try:
            _move_to_graveyard(run_root, outbox_dir, name)
        except OSError:
            ok = False
            logger.warning("outbox.stale_archive_failed", entry=name, exc_info=True)
    if not ok:
        return None
    return f"{Path(outbox_dir) / _SKIPPED_GRAVEYARD}/"


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
    send_after: float | None = None,
    archive_before: float | None = None,
    seen: set[SeenKey] | None = None,
) -> OutboxResult:
    """Scan outbox, send files as Telegram documents, and optionally clean up.

    #924: ``send_after`` / ``archive_before`` (epoch seconds, grace-adjusted)
    restrict delivery to entries that changed during the run; older entries
    are quarantined once (when ``cleanup``) and reported via
    ``OutboxResult.stale``. Fresh files beyond ``max_files`` are reported as
    ``overflow`` and, with age classification on, quarantined too. ``None``
    cutoffs keep the legacy behaviour. ``seen`` (run-scoped) skips entries a
    previous delivery in the same run already sent, archived or reported.
    """
    scan = classify_outbox(
        run_root,
        outbox_dir=outbox_dir,
        deny_globs=deny_globs,
        max_download_bytes=max_download_bytes,
        max_files=max_files,
        send_after=send_after,
        archive_before=archive_before,
        seen=seen,
    )
    files, skipped = scan.files, scan.skipped

    if not files and not skipped and not scan.stale and not scan.overflow:
        return OutboxResult()

    result = OutboxResult(
        cleanup=cleanup, overflow_limit=max_files, outbox_dir=outbox_dir
    )

    if scan.stale:
        result.stale = list(scan.stale)
        if cleanup:
            result.stale_archived_to = _archive_entries(
                run_root, outbox_dir, [n for n, _ in scan.stale]
            )
        times = [t for _, t in scan.stale]
        logger.info(
            "outbox.stale",
            count=len(scan.stale),
            oldest=datetime.fromtimestamp(min(times)).isoformat(timespec="seconds"),
            newest=datetime.fromtimestamp(max(times)).isoformat(timespec="seconds"),
            archived=result.stale_archived_to is not None,
            cutoff=archive_before,
        )

    if scan.overflow:
        result.overflow = list(scan.overflow)
        # Legacy (no cutoffs / ``send`` policy): overflow stays for a later
        # run, as before. Otherwise quarantine it now so it is never
        # attached to an unrelated later answer (#924's own mechanism).
        if cleanup and send_after is not None:
            result.overflow_archived_to = _archive_entries(
                run_root, outbox_dir, scan.overflow
            )
        logger.info(
            "outbox.overflow",
            count=len(scan.overflow),
            max_files=max_files,
            archived=result.overflow_archived_to is not None,
        )

    if seen is not None:
        for name, _ in scan.stale:
            seen.add(scan.keys[name])
        for name in scan.overflow:
            seen.add(scan.keys[name])
        for name, _ in skipped:
            if name in scan.keys:
                seen.add(scan.keys[name])

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

    result.skipped = skipped

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
            if seen is not None:
                seen.add(scan.keys[f.rel_path.name])
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
