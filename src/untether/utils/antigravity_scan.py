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

- **agy-executable**: manifests under every ``.agents/`` from the cwd up to the
  nearest ancestor holding ``.git`` (inclusive; at most 10 levels; never
  ``$HOME`` or above), content-hashed (size + sha256 of the first 256 KiB),
  plus agy's user-level manifests under ``~/.gemini/config/`` — **stat only**
  (D33: no reads under ``~/.gemini``), excluding ``plugins/untether-gate/``.
  Empty files are skipped: agy's own migration leaves a 0-byte
  ``mcp_config.json`` there, and an empty file defines nothing.
- **cross-engine**: at the project root only, stat only.

The 13 §8 gate precheck reuses this helper; keep it free of runner imports.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..logging import get_logger

logger = get_logger(__name__)

MAX_ANCESTOR_LEVELS = 10
MAX_FILES = 2000
TIME_BUDGET_S = 2.0
HASH_CAP_BYTES = 256 * 1024
USER_DISPLAY_PREFIX = "~/.gemini/config/"
_GATE_PLUGIN = "plugins/untether-gate/"
_AGENTS_DIR = ".agents"
_TOP_MANIFESTS = frozenset(
    {"mcp_config.json", "skills.json", "rules.json", "plugins.json", "agents.json"}
)
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

# (size, sha256 | mtime_ns)
type Fingerprint = tuple[int, str | int]


def user_config_dir() -> Path:
    """agy's user-level customisation dir (tests point it elsewhere)."""
    return Path.home() / ".gemini" / "config"


@dataclass(frozen=True, slots=True)
class ScanResult:
    root: Path
    agy: dict[str, Fingerprint] = field(default_factory=dict)
    cross: dict[str, Fingerprint] = field(default_factory=dict)
    truncated: bool = False

    @property
    def agy_digest(self) -> str:
        return "truncated" if self.truncated else _digest(self.agy)

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


def _is_agy_manifest(rel: str) -> bool:
    """*rel* is relative to an ``.agents/`` (or the user config) dir."""
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


def _walk_files(base: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames.sort()
        out.extend(Path(dirpath) / name for name in sorted(filenames))
    return out


def _hash_file(path: Path, size: int) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read(HASH_CAP_BYTES))
    return f"{digest.hexdigest()}:{min(size, HASH_CAP_BYTES)}"


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
    for directory in dirs:
        agents = directory / _AGENTS_DIR
        if not agents.is_dir():
            continue
        for path in _walk_files(agents):
            rel_in_agents = path.relative_to(agents).as_posix()
            if not _is_agy_manifest(rel_in_agents):
                continue
            if not budget.take():
                return
            try:
                st = path.stat()
                if st.st_size == 0:
                    continue
                fp: Fingerprint = (st.st_size, _hash_file(path, st.st_size))
            except OSError:
                continue
            out[path.relative_to(root).as_posix()] = fp


def _scan_user_dir(
    user_dir: Path, out: dict[str, Fingerprint], budget: _Budget
) -> None:
    """Stat only — never ``open()`` anything under ``~/.gemini`` (D33)."""
    if not user_dir.is_dir():
        return
    for path in _walk_files(user_dir):
        rel = path.relative_to(user_dir).as_posix()
        if not _is_agy_manifest(rel):
            continue
        if not budget.take():
            return
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_size == 0:
            continue
        out[f"{USER_DISPLAY_PREFIX}{rel}"] = (st.st_size, st.st_mtime_ns)


def _scan_cross_engine(
    root: Path, out: dict[str, Fingerprint], budget: _Budget
) -> None:
    candidates: list[Path] = [root / rel for rel in _CROSS_ENGINE_FILES]
    for tree in _CROSS_ENGINE_TREES:
        base = root / tree
        if base.is_dir():
            candidates.extend(_walk_files(base))
    for path in candidates:
        try:
            st = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        if not budget.take():
            return
        out[path.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)


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
    return ScanResult(root=root, agy=agy, cross=cross, truncated=agy_budget.exceeded)
