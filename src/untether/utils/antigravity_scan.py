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

- symlinked directories are followed (loops cut by real path), so a plugin
  dir linked from elsewhere is still hashed;
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
HASH_CAP_BYTES = 8 * 1024 * 1024
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
    def __init__(self) -> None:
        self.deadline = time.monotonic() + TIME_BUDGET_S
        self.files = 0
        self.exceeded = False

    def take(self) -> bool:
        self.files += 1
        if self.files > MAX_FILES or time.monotonic() > self.deadline:
            self.exceeded = True
        return not self.exceeded


def _walk_files(base: Path, budget: _Budget) -> Iterator[Path]:
    """Every file under *base*, following symlinked dirs once each."""
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(base, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen:
            dirnames[:] = []
            continue
        seen.add(real)
        if budget.exceeded:
            return
        dirnames.sort()
        for name in sorted(filenames):
            yield Path(dirpath) / name


def _stamp(st: os.stat_result) -> str:
    return f"ctime:{st.st_ctime_ns}"


def _fingerprint(path: Path, *, read: bool) -> Fingerprint | None:
    """None for an empty file. Never raises: an unreadable or vanished path
    still yields an entry, so it can't drop out of the digest."""
    try:
        st = path.stat()
    except OSError:
        try:
            target = os.readlink(path)
        except OSError:
            return (-1, "unstatable")
        return (-1, f"dangling:{target}")
    if st.st_size == 0:
        return None
    if not read:
        return (st.st_size, f"{st.st_mtime_ns}:{_stamp(st)}")
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            remaining = HASH_CAP_BYTES
            while remaining > 0:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
    except OSError:
        return (st.st_size, f"unreadable:{_stamp(st)}")
    if st.st_size > HASH_CAP_BYTES:
        # Content past the cap isn't hashed: the change time catches edits.
        return (st.st_size, f"{digest.hexdigest()}:partial:{_stamp(st)}")
    return (st.st_size, digest.hexdigest())


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _referenced_scripts(manifest: Path, root: Path) -> list[Path]:
    """In-project files that a hooks / MCP manifest's commands name.

    Hook commands run with the manifest's directory as cwd; also try the
    project root. Only existing files inside the root are returned.
    """
    try:
        if manifest.stat().st_size > _REF_PARSE_CAP_BYTES:
            return []
        data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    root_real = root.resolve()
    found: list[Path] = []
    for text in _strings(data):
        for token in _TOKEN_SPLIT.split(text):
            if not token or ("/" not in token and "." not in token):
                continue
            for base in (manifest.parent, root):
                candidate = (
                    (base / token).resolve()
                    if not os.path.isabs(token)
                    else Path(token).resolve()
                )
                try:
                    if candidate.is_file() and candidate.is_relative_to(root_real):
                        found.append(candidate)
                        break
                except OSError:
                    continue
    return found


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.resolve().relative_to(root.resolve()).as_posix()


def _scan_agents_dirs(
    root: Path, cwd: Path, out: dict[str, Fingerprint], budget: _Budget
) -> None:
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
        if not agents.is_dir():
            continue
        for path in _walk_files(agents, budget):
            if not budget.take():
                return
            fp = _fingerprint(path, read=True)
            if fp is None:
                continue
            out[path.relative_to(root).as_posix()] = fp
            if path.name in _COMMAND_MANIFESTS:
                manifests.append(path)
    for manifest in manifests:
        for script in _referenced_scripts(manifest, root):
            rel = _rel(script, root)
            if rel in out:
                continue
            if not budget.take():
                return
            fp = _fingerprint(script, read=True)
            if fp is not None:
                out[rel] = fp


def _scan_user_dir(
    user_dir: Path, out: dict[str, Fingerprint], budget: _Budget
) -> None:
    """Stat only — never ``open()`` anything under ``~/.gemini`` (D33)."""
    if not user_dir.is_dir():
        return
    for path in _walk_files(user_dir, budget):
        rel = path.relative_to(user_dir).as_posix()
        if not _is_user_manifest(rel):
            continue
        if not budget.take():
            return
        fp = _fingerprint(path, read=False)
        if fp is not None:
            out[f"{USER_DISPLAY_PREFIX}{rel}"] = fp


def _scan_cross_engine(
    root: Path, out: dict[str, Fingerprint], budget: _Budget
) -> None:
    candidates: list[Path] = [root / rel for rel in _CROSS_ENGINE_FILES]
    for tree in _CROSS_ENGINE_TREES:
        base = root / tree
        if base.is_dir():
            candidates.extend(_walk_files(base, budget))
    for path in candidates:
        if not (path.is_file() or path.is_symlink()):
            continue
        if not budget.take():
            return
        fp = _fingerprint(path, read=False)
        if fp is not None:
            out[path.relative_to(root).as_posix()] = fp


def scan_workspace_config(cwd: Path) -> ScanResult:
    """Fingerprint both sets for a run in *cwd* (blocking; ≤ 2 s)."""
    root = find_project_root(cwd)
    agy: dict[str, Fingerprint] = {}
    cross: dict[str, Fingerprint] = {}
    agy_budget = _Budget()
    _scan_agents_dirs(root, cwd, agy, agy_budget)
    if not agy_budget.exceeded:
        _scan_user_dir(user_config_dir(), agy, agy_budget)
    cross_budget = _Budget()
    _scan_cross_engine(root, cross, cross_budget)
    if agy_budget.exceeded or cross_budget.exceeded:
        logger.warning(
            "antigravity.workspace_config.scan_truncated",
            root=str(root),
            agy_files=len(agy),
            cross_files=len(cross),
        )
    return ScanResult(
        root=root,
        agy=agy,
        cross=cross,
        truncated=agy_budget.exceeded,
        cross_truncated=cross_budget.exceeded,
    )
