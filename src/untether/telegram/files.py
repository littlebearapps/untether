from __future__ import annotations

import io
import os
import shlex
import stat
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Literal

from ..logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "AccessReason",
    "PathAccess",
    "ZipTooLargeError",
    "check_path_access",
    "deduplicate_target",
    "default_upload_name",
    "default_upload_path",
    "deny_reason",
    "file_usage",
    "format_bytes",
    "normalize_relative_path",
    "parse_file_command",
    "parse_file_prompt",
    "resolve_path_within_root",
    "split_command_args",
    "write_bytes_atomic",
    "zip_directory",
]


def split_command_args(text: str) -> tuple[str, ...]:
    if not text.strip():
        return ()
    try:
        return tuple(shlex.split(text))
    except ValueError:
        return tuple(text.split())


def file_usage() -> str:
    return "usage: `/file put <path>` or `/file get <path>`"


def parse_file_command(args_text: str) -> tuple[str | None, str, str | None]:
    tokens = split_command_args(args_text)
    if not tokens:
        return None, "", file_usage()
    command = tokens[0].lower()
    rest = " ".join(tokens[1:]).strip()
    if command not in {"put", "get"}:
        return None, rest, file_usage()
    return command, rest, None


def parse_file_prompt(
    prompt: str, *, allow_empty: bool
) -> tuple[str | None, bool, str | None]:
    tokens = split_command_args(prompt)
    force = False
    parts: list[str] = []
    for token in tokens:
        if token == "--force":
            force = True
            continue
        if token.startswith("--"):
            return None, force, f"unknown flag: {token}"
        parts.append(token)
    path = " ".join(parts).strip()
    if not path and not allow_empty:
        return None, force, "missing path"
    return (path or None), force, None


def normalize_relative_path(value: str) -> Path | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.startswith("~"):
        return None
    path = Path(cleaned)
    if path.is_absolute():
        return None
    parts = [part for part in path.parts if part not in {"", "."}]
    if not parts:
        return None
    if ".." in parts:
        return None
    if ".git" in parts:
        return None
    return Path(*parts)


def resolve_path_within_root(root: Path, rel_path: Path) -> Path | None:
    root_resolved = root.resolve(strict=False)
    target = (root / rel_path).resolve(strict=False)
    if not target.is_relative_to(root_resolved):
        return None
    return target


def _full_match_parts(parts: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    """Segment-wise glob match with recursive ``**`` (Python 3.13 ``full_match``).

    A ``**`` segment matches zero or more path segments, except a trailing
    ``**``, which matches one or more (``.git/**`` matches ``.git/x`` but not
    ``.git`` itself) — the same semantics as ``PurePath.full_match`` on 3.13+.
    """
    if not pattern:
        return not parts
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        if not rest:
            return len(parts) >= 1
        return any(_full_match_parts(parts[i:], rest) for i in range(len(parts) + 1))
    if not parts:
        return False
    return fnmatchcase(parts[0], head) and _full_match_parts(parts[1:], rest)


def _glob_matches(posix: PurePosixPath, pattern: str) -> bool:
    """Return True when *posix* matches deny glob *pattern* (#831).

    ``PurePosixPath.match`` is right-anchored and treats ``**`` as a single
    segment on Python 3.12-3.14, so ``**/*.pem`` never matched a root-level
    ``key.pem`` and ``**/.ssh/**`` never matched ``.ssh/config`` or
    ``a/b/.ssh/x/y``. This keeps the legacy right-anchored match (bare names
    such as ``.env`` still match at any depth), adds a recursive ``**`` match
    that emulates 3.13 ``full_match``, and for a trailing ``/**`` also accepts
    any proper ancestor that matches the head (so ``secrets/**`` keeps
    covering ``a/secrets/...`` at every depth, not just one level down).
    Strictly more denying than the legacy match — never less.
    """
    if not posix.parts:
        return False
    if posix.match(pattern):
        return True
    if _full_match_parts(posix.parts, PurePosixPath(pattern).parts):
        return True
    if pattern.endswith("/**") and len(pattern) > 3:
        head = pattern[:-3]
        parts = posix.parts
        for i in range(1, len(parts)):
            if _glob_matches(PurePosixPath(*parts[:i]), head):
                return True
    return False


def deny_reason(rel_path: Path, deny_globs: Sequence[str]) -> str | None:
    # Casefolded so ``.GIT`` on a case-insensitive filesystem (macOS APFS)
    # is caught too (#390 D4).
    if any(part.casefold() == ".git" for part in rel_path.parts):
        return ".git/**"
    posix = PurePosixPath(rel_path.as_posix())
    for pattern in deny_globs:
        if _glob_matches(posix, pattern):
            return pattern
    return None


AccessReason = Literal["outside", "denied", "hidden", "unresolvable"]


@dataclass(frozen=True, slots=True)
class PathAccess:
    """Result of :func:`check_path_access` (#389, #390)."""

    root: Path
    """The root, resolved (``root.resolve(strict=False)``)."""
    target: Path | None
    """Fully resolved absolute target; ``None`` unless allowed."""
    rel: Path | None
    """``target.relative_to(root)`` (never raises); ``None`` unless allowed."""
    reason: AccessReason | None
    """``None`` means allowed."""
    rule: str | None
    """The deny glob that matched when ``reason == "denied"``."""
    via_symlink: bool
    """True when the resolved relative path differs from the requested one."""
    resolved: Path | None = None
    """Resolved root-relative path whenever resolution stayed inside the root.

    Unlike ``rel`` it is also set when the *resolved* path was denied, so
    callers can say where a request led (``resolves to .git/hooks/x``)
    without resolving twice. Relative, so safe to log.
    """

    @property
    def ok(self) -> bool:
        return self.reason is None


def _is_hidden(rel: Path, hidden_allow: frozenset[str]) -> bool:
    return any(part.startswith(".") and part not in hidden_allow for part in rel.parts)


def _has_symlink_component(root_r: Path, target: Path) -> bool:
    """``lstat``-walk *target* and its parents up to *root_r* for a symlink.

    A resolved path never contains a symlink, except when resolution gave up
    (a symlink loop on Python 3.13+, where ``resolve(strict=False)`` returns
    the unresolved path instead of raising). Missing components are skipped;
    ``lstat`` never follows links, so the walk cannot loop.
    """
    current = target
    while current != root_r:
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except OSError:
            pass
        parent = current.parent
        if parent == current:
            break
        current = parent
    return False


def check_path_access(
    root: Path,
    candidate: Path,
    deny_globs: Sequence[str],
    *,
    deny_hidden: bool = False,
    hidden_allow: frozenset[str] = frozenset(),
) -> PathAccess:
    """Check *candidate* (relative to *root*, or absolute) for access.

    Shared by ``/file put``/``/file get`` (#390) and ``/browse`` (#389). The
    first failing check wins:

    1. lexical containment (``os.path.normpath``) → ``outside``;
    2. deny globs on the requested root-relative path → ``denied``;
    3. hidden components (only with ``deny_hidden``) → ``hidden``;
    4. resolve the raw path with OS semantics (``a/link/..`` follows
       ``link``); a symlink loop → ``unresolvable`` on every Python;
    5. resolved containment → ``outside``;
    6. deny globs / hidden on the resolved root-relative path.

    Existence is never checked, so callers must run this *before* any
    ``exists()``/``is_file()`` to avoid an existence oracle.

    TOCTOU: a process with write access inside the root could swap a parent
    directory for a symlink between this check and the caller's read/write.
    Such a process can already write anywhere in the root, so the residual
    grants no new capability; accepted and documented (#390 D2).
    """

    def _deny(
        reason: AccessReason,
        root_r: Path,
        *,
        rule: str | None = None,
        via_symlink: bool = False,
        resolved: Path | None = None,
    ) -> PathAccess:
        return PathAccess(
            root=root_r,
            target=None,
            rel=None,
            reason=reason,
            rule=rule,
            via_symlink=via_symlink,
            resolved=resolved,
        )

    try:
        root_r = root.resolve(strict=False)
    except (RuntimeError, OSError):
        return _deny("unresolvable", root)
    raw = root_r / candidate
    lexical = Path(os.path.normpath(raw))
    if not lexical.is_relative_to(root_r):
        return _deny("outside", root_r)
    lex_rel = lexical.relative_to(root_r)
    rule = deny_reason(lex_rel, deny_globs)
    if rule is not None:
        return _deny("denied", root_r, rule=rule)
    if deny_hidden and _is_hidden(lex_rel, hidden_allow):
        return _deny("hidden", root_r)
    try:
        target = raw.resolve(strict=False)
    except (RuntimeError, OSError):
        return _deny("unresolvable", root_r)
    if _has_symlink_component(root_r, target):
        return _deny("unresolvable", root_r)
    if not target.is_relative_to(root_r):
        return _deny("outside", root_r, via_symlink=True)
    rel = target.relative_to(root_r)
    via_symlink = rel != lex_rel
    rule = deny_reason(rel, deny_globs)
    if rule is not None:
        return _deny("denied", root_r, rule=rule, via_symlink=via_symlink, resolved=rel)
    if deny_hidden and _is_hidden(rel, hidden_allow):
        return _deny("hidden", root_r, via_symlink=via_symlink, resolved=rel)
    return PathAccess(
        root=root_r,
        target=target,
        rel=rel,
        reason=None,
        rule=None,
        via_symlink=via_symlink,
        resolved=rel,
    )


def format_bytes(value: int) -> str:
    size = max(0.0, float(value))
    units = ("b", "kb", "mb", "gb", "tb")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "b":
                return f"{int(size)} b"
            if size < 10:
                return f"{size:.1f} {unit}"
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{int(size)} B"


def default_upload_name(filename: str | None, file_path: str | None) -> str:
    name = Path(filename or "").name
    if not name and file_path:
        name = Path(file_path).name
    if not name:
        name = "upload.bin"
    return name


def default_upload_path(
    uploads_dir: str, filename: str | None, file_path: str | None
) -> Path:
    return Path(uploads_dir) / default_upload_name(filename, file_path)


def deduplicate_target(target: Path) -> Path:
    """Return *target* if it doesn't exist, otherwise append ``_1``, ``_2``, … before the extension."""
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    for i in range(1, 1000):
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            logger.info(
                "file.deduplicate",
                original=str(target),
                renamed=str(candidate),
            )
            return candidate
    raise FileExistsError(f"no available deduplicated path for {target}")


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", delete=False, dir=path.parent, prefix=".untether-upload-"
    ) as handle:
        handle.write(payload)
        temp_name = handle.name
    Path(temp_name).replace(path)


class ZipTooLargeError(Exception):
    pass


def zip_directory(
    root: Path,
    rel_path: Path,
    deny_globs: Sequence[str],
    *,
    max_bytes: int | None = None,
    arc_prefix: Path | None = None,
) -> bytes:
    """Zip ``root / rel_path``, skipping symlinks and deny-globbed members.

    Deny checks run on the real ``rel_path / member``. Member names use
    ``arc_prefix`` when given (``/file get`` passes the *requested* path so a
    download through an in-root symlink keeps its names, #390), else
    ``rel_path``.
    """
    target = root / rel_path
    prefix = rel_path if arc_prefix is None else arc_prefix
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for dirpath, _, filenames in os.walk(target, followlinks=False):
            dir_path = Path(dirpath)
            for filename in filenames:
                item = dir_path / filename
                if item.is_symlink():
                    continue
                if not item.is_file():
                    continue
                member = item.relative_to(target)
                rel_item = rel_path / member
                if deny_reason(rel_item, deny_globs) is not None:
                    continue
                archive.write(item, arcname=(prefix / member).as_posix())
                if max_bytes is not None and buffer.tell() > max_bytes:
                    logger.debug(
                        "file.zip_too_large",
                        max_bytes=max_bytes,
                        actual_bytes=buffer.tell(),
                        path=str(rel_path),
                    )
                    raise ZipTooLargeError()
    payload = buffer.getvalue()
    if max_bytes is not None and len(payload) > max_bytes:
        logger.debug(
            "file.zip_too_large",
            max_bytes=max_bytes,
            actual_bytes=len(payload),
            path=str(rel_path),
        )
        raise ZipTooLargeError()
    return payload
