"""Planted-config scanner for Antigravity (agy) runs (#558; REVIEW B2 widened
per C10 / D22.5, REVIEW-2 B2 + M4).

agy runs code with no tool call: hook commands of every event, plugins,
custom agents that declare ``hooks:`` and ``.agents/mcp_config.json`` stdio
servers (which start in ``-p`` even when disabled, #1088). A Workspace run may
write those files (in-project writes are allowed) and the *next* run —
possibly an unattended cron — executes them. Another engine's auto-exec and
instruction files (``.claude/``, ``.envrc``, ``.github/workflows/`` …) are the
same risk for the next Claude or Codex run in the project.

Two sets, both relative to the project root:

- **agy-executable**: *every* file under every ``.agents/`` from the cwd up to
  the nearest ancestor holding ``.git`` (inclusive; at most 10 levels; never
  ``$HOME`` or above), plus the in-project scripts that ``hooks.json`` /
  ``mcp_config.json`` commands name, all content-hashed; plus agy's
  user-level manifests under ``~/.gemini/config/`` — **stat only** (D33: no
  reads under ``~/.gemini``), excluding ``plugins/untether-gate/``.
- **cross-engine**: at the project root only, stat only.

Fail-closed rules (security review of 2b4d66c):

- symlinked directories are followed only while they stay inside the
  project root (each real path once — cycle protection); a link out of the
  root, or a manifest naming a script outside it, is recorded but never
  followed and marks the scan ``outside_root`` ("not checked");
- only regular files are ever opened (``stat`` first, then ``O_NONBLOCK`` +
  ``fstat`` on the fd), so a FIFO, ``/dev/zero`` or a device can't wedge it;
- each set has one budget — entries (files and directories), total bytes
  hashed and wall clock; running over it sets ``truncated``;
- a file larger than the hash cap, or one that can't be read, still counts:
  its fingerprint carries the inode change time (``st_ctime_ns``), which a
  writer can't set back the way it can ``mtime``;
- a scan that hits the file or time budget sets ``truncated``; callers must
  treat that as "not checked" (warn every attended run, refuse unattended),
  never as a stable digest.

Empty files are skipped: agy's own migration leaves a 0-byte
``mcp_config.json`` in ``~/.gemini/config``, and an empty file defines and
runs nothing (filling it later changes the digest).

The 13 §8 gate precheck reuses this helper; keep it free of runner imports.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..logging import get_logger

logger = get_logger(__name__)

MAX_ANCESTOR_LEVELS = 10
MAX_FILES = 2000
TIME_BUDGET_S = 2.0
# Per-file hash cap (the rest is covered by the inode change time) and the
# per-set totals: a hostile repo can't make the scan read or walk unbounded.
HASH_CAP_BYTES = 4 * 1024 * 1024
MAX_HASH_BYTES = 32 * 1024 * 1024
_CHUNK_BYTES = 256 * 1024
_MAX_TOKENS = 200
_REF_PARSE_CAP_BYTES = 256 * 1024
USER_DISPLAY_PREFIX = "~/.gemini/config/"
_GATE_PLUGIN = "plugins/untether-gate/"
_AGENTS_DIR = ".agents"
_TOP_MANIFESTS = frozenset(
    {"mcp_config.json", "skills.json", "rules.json", "plugins.json", "agents.json"}
)
# Manifests whose command strings may name an in-project script.
_COMMAND_MANIFESTS = frozenset({"hooks.json", "mcp_config.json"})
_TOKEN_SPLIT = re.compile(r"[\s;&|()<>'\"`=,]+")
# REVIEW-2 M4: files that become code execution or instructions for the
# *next* engine run in the same project.
_CROSS_ENGINE_FILES = (
    ".mcp.json",
    ".envrc",
    ".vscode/tasks.json",
    ".pre-commit-config.yaml",
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
)
_CROSS_ENGINE_TREES = (".claude", ".github/workflows", ".husky", ".git/hooks")

# (size, content hash | stat stamp)
type Fingerprint = tuple[int, str | int]


def user_config_dir() -> Path:
    """agy's user-level customisation dir (tests point it elsewhere)."""
    return Path.home() / ".gemini" / "config"


@dataclass(frozen=True, slots=True)
class ScanResult:
    root: Path
    agy: dict[str, Fingerprint] = field(default_factory=dict)
    cross: dict[str, Fingerprint] = field(default_factory=dict)
    # The agy set hit the file/time budget: not checked. Never compare its
    # digest as if it were complete.
    truncated: bool = False
    cross_truncated: bool = False
    # A link under .agents/ (or a script a manifest names) resolves outside
    # the project root: its target can change unseen, so "not checked".
    outside_root: bool = False

    @property
    def agy_digest(self) -> str:
        return _digest(self.agy)

    @property
    def cross_digest(self) -> str:
        return _digest(self.cross)


def _digest(entries: dict[str, Fingerprint]) -> str:
    payload = json.dumps(sorted(entries.items()), separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def changed_paths(
    before: dict[str, Fingerprint], after: dict[str, Fingerprint]
) -> list[str]:
    """Created, changed or removed paths, sorted."""
    return sorted(
        p for p in before.keys() | after.keys() if before.get(p) != after.get(p)
    )


def find_project_root(cwd: Path, *, home: Path | None = None) -> Path:
    """Nearest ancestor of *cwd* (inclusive) holding ``.git``; *cwd* if none.

    Never returns ``$HOME`` or anything above it (a dotfiles repo in ``$HOME``
    must not pull ``~/.claude`` into every project's scan).
    """
    home = (home or Path.home()).resolve()
    start = cwd.resolve()
    current = start
    for _ in range(MAX_ANCESTOR_LEVELS + 1):
        if current == home or current in home.parents:
            break
        if (current / ".git").exists():
            return current
        if current.parent == current:
            break
        current = current.parent
    return start


def _is_user_manifest(rel: str) -> bool:
    """*rel* is relative to ``~/.gemini/config``."""
    if rel.startswith(_GATE_PLUGIN):
        return False  # Untether's own gate plugin (rc3), trusted separately
    name = rel.rsplit("/", 1)[-1]
    if name == "hooks.json" or rel.startswith("plugins/"):
        return True
    if rel in _TOP_MANIFESTS:
        return True
    parts = rel.split("/")
    return len(parts) == 2 and parts[0] == "agents" and name.endswith(".md")


class _Budget:
    """One budget per set: entries (files + dirs), bytes hashed, wall clock.

    Running over any of them marks the scan ``exceeded`` (→ "not checked":
    a ⚠️ row on attended runs, unattended runs refused)."""

    def __init__(self) -> None:
        self.deadline = time.monotonic() + TIME_BUDGET_S
        self.entries = 0
        self.bytes = 0
        self.exceeded = False
        self.outside = False  # a symlink or script points outside the root

    def _check(self) -> bool:
        if (
            self.entries > MAX_FILES
            or self.bytes > MAX_HASH_BYTES
            or time.monotonic() > self.deadline
        ):
            self.exceeded = True
        return not self.exceeded

    def take(self) -> bool:
        self.entries += 1
        return self._check()

    def spend(self, nbytes: int) -> bool:
        self.bytes += nbytes
        return self._check()


def _contained(path: str | Path, base: str) -> bool:
    real = os.path.realpath(path)
    return real == base or real.startswith(base.rstrip(os.sep) + os.sep)


def _walk_files(
    base: Path,
    budget: _Budget,
    *,
    follow_within: str | None,
    seen: set[str] | None = None,
) -> Iterator[Path]:
    """Every entry under *base* that isn't a directory, bounded by *budget*.

    Symlinked directories are followed only when their real path stays
    inside *follow_within* (and only once each — cycle protection); any
    other symlinked directory is yielded as an entry, never descended.
    """
    seen = set() if seen is None else seen
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        real = os.path.realpath(dirpath)
        if real in seen or not budget.take():
            dirnames[:] = []
            if budget.exceeded:
                return
            continue
        seen.add(real)
        keep: list[str] = []
        for name in sorted(dirnames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                if follow_within is not None and _contained(full, follow_within):
                    # os.walk won't descend a link with followlinks=False;
                    # walk it explicitly (bounded by the same budget).
                    yield from _walk_files(
                        Path(full), budget, follow_within=follow_within, seen=seen
                    )
                else:
                    yield Path(full)
            else:
                keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            yield Path(dirpath) / name


def _stamp(st: os.stat_result) -> str:
    return f"ctime:{st.st_ctime_ns}"


def _hash_regular(path: Path, st: os.stat_result, budget: _Budget) -> Fingerprint:
    """Hash a regular file without ever blocking on it: ``O_NONBLOCK`` and a
    post-open ``fstat`` reject a file swapped for a FIFO or device."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return (st.st_size, f"unreadable:{_stamp(st)}")
    digest = hashlib.sha256()
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or fst.st_ino != st.st_ino:
            return (-1, f"special:{stat.S_IFMT(fst.st_mode)}:{_stamp(fst)}")
        remaining = min(fst.st_size, HASH_CAP_BYTES)
        while remaining > 0:
            chunk = os.read(fd, min(_CHUNK_BYTES, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
            if not budget.spend(len(chunk)):
                return (fst.st_size, f"unfinished:{_stamp(fst)}")
    except OSError:
        return (st.st_size, f"unreadable:{_stamp(st)}")
    finally:
        os.close(fd)
    if fst.st_size > HASH_CAP_BYTES:
        # Content past the cap isn't hashed: the change time catches edits.
        return (fst.st_size, f"{digest.hexdigest()}:partial:{_stamp(fst)}")
    return (fst.st_size, digest.hexdigest())


def _fingerprint(
    path: Path,
    *,
    read: bool,
    budget: _Budget,
    contain: str | None = None,
) -> Fingerprint | None:
    """None for an empty regular file. Never raises, never opens anything but
    a regular file, and never follows a link out of *contain*: such entries
    still count (by stat / link target), so they can't drop out of the
    digest."""
    try:
        lst = os.lstat(path)
    except OSError:
        return (-1, "unstatable")
    if stat.S_ISLNK(lst.st_mode):
        try:
            target = os.readlink(path)
        except OSError:
            target = "?"
        if contain is not None and not _contained(path, contain):
            budget.outside = True
            return (-1, f"outside:{target}")
        try:
            st = os.stat(path)
        except OSError:
            return (-1, f"dangling:{target}")
    else:
        st = lst
    if not stat.S_ISREG(st.st_mode):
        # FIFO, device, socket, directory link: never opened.
        return (-1, f"special:{stat.S_IFMT(st.st_mode)}:{_stamp(st)}")
    if st.st_size == 0:
        return None
    if not read:
        return (st.st_size, f"{st.st_mtime_ns}:{_stamp(st)}")
    return _hash_regular(path, st, budget)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _read_small_regular(path: Path, limit: int) -> str | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, limit).decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _referenced_scripts(manifest: Path, root_real: str) -> list[Path]:
    """In-project regular files that a hooks / MCP manifest's commands name.

    Hook commands run with the manifest's directory as cwd; also try the
    project root. Paths resolving outside the project root (``/dev/zero``,
    ``~``, ``../..``) are never touched.
    """
    text = _read_small_regular(manifest, _REF_PARSE_CAP_BYTES)
    if text is None:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    found: list[Path] = []
    for value in _strings(data):
        for token in _TOKEN_SPLIT.split(value)[:_MAX_TOKENS]:
            if not token or ("/" not in token and "." not in token):
                continue
            for base in (str(manifest.parent), root_real):
                candidate = os.path.normpath(os.path.join(base, token))
                if not _contained(candidate, root_real):
                    continue
                try:
                    if stat.S_ISREG(os.stat(candidate).st_mode):
                        found.append(Path(os.path.realpath(candidate)))
                        break
                except OSError:
                    continue
    return found


def _scan_agents_dirs(
    root: Path, cwd: Path, out: dict[str, Fingerprint], budget: _Budget
) -> None:
    root_real = os.path.realpath(root)
    current = cwd.resolve()
    dirs: list[Path] = []
    for _ in range(MAX_ANCESTOR_LEVELS + 1):
        dirs.append(current)
        if current == root or current.parent == current:
            break
        current = current.parent
    manifests: list[Path] = []
    for directory in dirs:
        agents = directory / _AGENTS_DIR
        if not agents.is_dir() or not _contained(agents, root_real):
            if agents.is_symlink():
                budget.outside = True
                out[agents.relative_to(root).as_posix()] = (-1, "outside")
            continue
        for path in _walk_files(agents, budget, follow_within=root_real):
            if not budget.take():
                return
            fp = _fingerprint(path, read=True, budget=budget, contain=root_real)
            if budget.exceeded:
                return
            if fp is None:
                continue
            out[path.relative_to(root).as_posix()] = fp
            if path.name in _COMMAND_MANIFESTS and fp[0] >= 0:
                manifests.append(path)
    for manifest in manifests:
        for script in _referenced_scripts(manifest, root_real):
            rel = Path(script).relative_to(root_real).as_posix()
            if rel in out:
                continue
            if not budget.take():
                return
            fp = _fingerprint(script, read=True, budget=budget, contain=root_real)
            if budget.exceeded:
                return
            if fp is not None:
                out[rel] = fp


def _scan_user_dir(
    user_dir: Path, out: dict[str, Fingerprint], budget: _Budget
) -> None:
    """Stat only — never ``open()`` anything under ``~/.gemini`` (D33).
    Symlinked dirs are recorded, not followed."""
    if not user_dir.is_dir():
        return
    for path in _walk_files(user_dir, budget, follow_within=None):
        rel = path.relative_to(user_dir).as_posix()
        if not _is_user_manifest(rel) and not path.is_symlink():
            continue
        if not budget.take():
            return
        fp = _fingerprint(path, read=False, budget=budget)
        if fp is not None:
            out[f"{USER_DISPLAY_PREFIX}{rel}"] = fp


def _scan_cross_engine(
    root: Path, out: dict[str, Fingerprint], budget: _Budget
) -> None:
    """Stat only; symlinked dirs are recorded, not followed."""
    candidates: list[Path] = [root / rel for rel in _CROSS_ENGINE_FILES]
    for tree in _CROSS_ENGINE_TREES:
        base = root / tree
        if base.is_dir() and not base.is_symlink():
            candidates.extend(_walk_files(base, budget, follow_within=None))
        elif base.is_symlink():
            candidates.append(base)
        if budget.exceeded:
            return
    for path in candidates:
        if not os.path.lexists(path):
            continue
        if not budget.take():
            return
        fp = _fingerprint(path, read=False, budget=budget)
        if fp is not None:
            out[path.relative_to(root).as_posix()] = fp


def scan_workspace_config(cwd: Path) -> ScanResult:
    """Fingerprint both sets for a run in *cwd*.

    Blocking and bounded (entries, bytes hashed, wall clock); callers run it
    off the event loop (``anyio.to_thread``)."""
    root = find_project_root(cwd)
    agy: dict[str, Fingerprint] = {}
    cross: dict[str, Fingerprint] = {}
    agy_budget = _Budget()
    _scan_agents_dirs(root, cwd, agy, agy_budget)
    if not agy_budget.exceeded:
        _scan_user_dir(user_config_dir(), agy, agy_budget)
    cross_budget = _Budget()
    _scan_cross_engine(root, cross, cross_budget)
    if agy_budget.exceeded or cross_budget.exceeded or agy_budget.outside:
        logger.warning(
            "antigravity.workspace_config.scan_truncated",
            root=str(root),
            agy_files=len(agy),
            cross_files=len(cross),
            over_budget=agy_budget.exceeded or cross_budget.exceeded,
            outside_root=agy_budget.outside,
        )
    return ScanResult(
        root=root,
        agy=agy,
        cross=cross,
        truncated=agy_budget.exceeded,
        cross_truncated=cross_budget.exceeded,
        outside_root=agy_budget.outside,
    )
