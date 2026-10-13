"""Planted-config scanner for Antigravity (agy) runs (#558; REVIEW B2 widened
per C10 / D22.5, REVIEW-2 B2 + M4).

This is a **tripwire, not a sandbox**: it notices agy-executable config that
changed since a person last looked, so unattended runs don't execute it
unseen. It can't stop a determined local attacker.

Known limitations (accepted; the rc3 gate's ``agy -p /hooks`` precheck, which
asks agy itself what it loaded, is the stronger control):

- Only *literal* script paths in commands are followed. Code reached
  indirectly — ``python -m pkg``, ``npm run x`` / ``package.json``,
  ``make``, a ``cd`` before a relative path, ``PATH`` lookups, network
  fetches (``npx pkg``), a variable passed as an argument (``sh $X``) —
  isn't tracked. Only a command *word* that is itself dynamic (``$RUNNER``,
  ``$(…)``) is "not checked" (#1043).
- What agy loads is inferred, never asked of agy: whether it reads a
  project-level ``settings.json``, how it resolves a bare folder name, which
  ``.git`` it stops at and which YAML its front-matter reader accepts are
  assumptions, each taken towards more coverage or "not checked" (#1043).
- User-level config under ``~/.gemini`` is stat only (D33: never read): the
  scripts its manifests name aren't followed, and an edit within one
  timestamp tick of the last scan is invisible on coarse-timestamp
  filesystems. agy's own ``settings.json`` (which can carry hooks) isn't in
  the digest: agy rewrites it itself, which would hold every cron on every
  host. Both live outside the workspace (only Full access or the user can
  write them).
- Instruction trees (``.agents/skills/``, ``.agents/rules/``, ``AGENTS.md``,
  ``GEMINI.md``) aren't in the agy-executable set: they steer the model but
  run nothing without a tool call, which the run's mode gates. Root-level
  instruction files are in the cross-engine set (post-run ⚠️ row).
- Empty files are skipped (they define and run nothing; filling one changes
  the digest anyway).
- A writer still running after the pre-spawn scan can change files before
  agy reads them; the post-run re-scan reports it.
- agy's JSON tolerance is assumed strict (every probe manifest is strict
  JSON; no JSONC is documented). Anything else is "not checked".

Threat model
------------
A hostile repo or PR — or a Workspace agy run, which may write in-project and
temp files — plants or edits something agy executes with no tool call (hook
commands of every event, plugins, custom agents with ``hooks:``,
``.agents/mcp_config.json`` stdio servers, which start in ``-p`` even when
disabled, #1088, and the scripts those name), so a later unattended run
executes it unseen; or it tries to wedge the scan.

The scan answers exactly one of: a **digest** that changes when anything it
tracks changes, or **"not checked"** (``unchecked_reason``) — a ⚠️ row on
every attended run, unattended runs refused, nothing recorded as seen.

What's tracked (agy set)
------------------------
- every file under every ``.agents/`` from the cwd up to the *outermost* git
  checkout above it (a ``.git`` folder or ``gitdir:`` file; with none, every
  ancestor below ``$HOME``), except the instruction trees above, hashed in
  full. The project root (``ScanResult.root``) is the nearest checkout;
- every file a reference manifest (``hooks.json``, ``mcp_config.json``,
  ``plugin.json``, ``settings.json``, the 1.2.16 ``*.json`` manifests, the
  front matter of custom agents — any ``.md`` below an ``agents`` folder,
  so both ``agents/x.md`` and ``agents/<name>/agent.md``; names compared
  case-insensitively) names by literal path — as a shell would read it
  (JSON-decoded, then shell words) and as a literal argv entry (the whole
  string) — at *every* one of the manifest's folder, the run's cwd, the
  project root and the outermost checkout where it exists (a decoy in one
  can't hide the file agy loads from another): hashed when this user
  controls it (owns or can write it or any folder above it), ignored when
  it's a system file (``/usr/bin/python3``);
- what a custom agent's ``plugins:`` / ``hooks:`` / ``agents:`` front matter
  or a path string in a declarative manifest (``plugin.json``, the
  ``*.json`` lists) points at: a folder is walked like ``.agents/``, a
  ``.json`` / ``.md`` file is parsed as a manifest in turn. A bare name
  (``"tools"``) is tried at every base only directly under a path key or in
  ``plugins.json`` / ``agents.json``, otherwise beside its manifest only
  (see ``_manifest_paths``). A folder passed to a hook or MCP command
  (``--root ./data``) is an argument, not config, and isn't walked;
- agy's user-level manifests under ``~/.gemini/config/`` — stat only —
  excluding Untether's own Workspace agent file (``_MANAGED_AGENT``) by
  exact path, and Untether's gate plugin files (``_GATE_PLUGIN_DIR``) by
  exact path only while their content is exactly what Untether writes
  (Untether's own files, the only ones this scan reads under ``~/.gemini``).

"Not checked" whenever: a budget (entries, bytes, wall clock) runs out; a
link under ``.agents/`` (or in ``~/.gemini/config``) leads outside both the
project and ``$HOME``; a FIFO, device or socket sits where agy loads files;
an entry or folder can't be stat'd, listed or read; a file changes identity
while read; a reference manifest is too large, not strict JSON (comments,
trailing commas, BOM, duplicate keys, NaN), in another format agy might read
(``hooks.yaml``, ``mcp_config.jsonc`` …), or names a path — or the command
itself — through a variable, command substitution or glob; an agent file
whose front matter agy might delimit differently (BOM, indented or late
``---``, unusual line breaks) or whose path keys this line reader can't
place (quoted, nested, flow mapping, anchor, alias, tag); a
referenced folder that holds the project itself; a cwd so deep that the
project root is out of reach.

The cross-engine set (project root only, stat only) never refuses (REVIEW-2
M4); it only feeds the post-run ⚠️ row. Claude Code's worktrees
(``.claude/worktrees/``) aren't in it: each is its own checkout with its own
config (#1051).

Changing what is tracked changes digests: there is no digest version, so a
project whose set gains an entry after an upgrade differs from the stored
digest like any other change — unattended runs are held until an attended
run shows the first-sight row. That is intended; never re-baseline silently.

The 13 §8 gate precheck reuses this helper; keep it free of runner imports.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import time
import unicodedata
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..logging import get_logger

logger = get_logger(__name__)

MAX_ANCESTOR_LEVELS = 10
# Per-set budgets: entries (dirs + files + reference lookups), bytes hashed,
# wall clock. Running out of any of them is "not checked", never partial.
MAX_FILES = 2000
MAX_HASH_BYTES = 64 * 1024 * 1024
TIME_BUDGET_S = 2.0
# The runner's own bound on the blocking scan thread (a read on a hung
# network mount can't be interrupted from inside): past it the run goes on
# as "not checked" and the thread is abandoned.
SCAN_TIMEOUT_S = 5.0
_CHUNK_BYTES = 256 * 1024
_REF_PARSE_CAP_BYTES = 256 * 1024
USER_DISPLAY_PREFIX = "~/.gemini/config/"
# Untether's gate plugin (#980, ``antigravity/gate_plugin.py``), relative to
# ``~/.gemini/config``: the files ``untether agy-gate install`` puts there
# are left out of the digest only when their *content* is exactly what this
# Untether writes (``gate_plugin.verified_own_files``: byte compare with its
# own templates — Untether's own files, the one read under ``~/.gemini`` this
# scan makes). A changed, extra or linked file is tracked like any other
# plugin file, so nothing planted rides on the exemption.
_GATE_PLUGIN_DIR = "plugins/untether-gate/"
# How those files are named in ``ScanResult.agy`` (#1083: the runner's
# plugin-changed row reports them, so the generic row leaves them out).
GATE_PLUGIN_DISPLAY_PREFIX = USER_DISPLAY_PREFIX + _GATE_PLUGIN_DIR
# Untether's own Workspace agent (``utils/antigravity_agent.py``), relative
# to ``~/.gemini/config``. That exact regular file is left out of the digest:
# Untether rewrites it to its own text before every run that names it, so
# tracking it would hold a cron on each install or upgrade. Nothing else in
# or beside that folder is exempt (#1043).
_MANAGED_AGENT = "agents/untether-workspace/agent.md"


def _own_gate_files() -> frozenset[str]:
    """The gate plugin files (relative to ``~/.gemini/config``) whose
    content is exactly what this Untether writes — none unless all are.
    Lazy import: ``gate_plugin`` imports this module."""
    from ..antigravity import gate_plugin

    return frozenset(_GATE_PLUGIN_DIR + n for n in gate_plugin.verified_own_files())


_AGENTS_DIR = ".agents"
# Instruction trees directly under .agents/ (exact names: anything else is
# hashed — failing towards more coverage on case-insensitive filesystems).
_INSTRUCTION_TREES = frozenset({"skills", "rules"})
_TOP_MANIFESTS = frozenset(
    {"mcp_config.json", "skills.json", "rules.json", "plugins.json", "agents.json"}
)
# Manifests whose strings may name a script agy runs (casefolded).
# ``settings.json``: whether agy reads a project-level one is unverified, so
# it is read as if it does (#1043).
_REFERENCE_MANIFESTS = _TOP_MANIFESTS | {"hooks.json", "plugin.json", "settings.json"}
# Other formats of the same manifests agy might also read: never assume.
_ALT_MANIFEST = re.compile(
    r"^(hooks|mcp_config|plugin|plugins|agents|skills|rules|settings)"
    r"\.(jsonc|json5|ya?ml|toml)$"
)
_RAW_SPLIT = re.compile(r"[\s;&|()<>'\"`=,\[\]{}]+")
_SHELL_DYNAMIC = re.compile(r"[$`*?\[\]{}]")
_SCRIPT_SUFFIX = re.compile(
    r"\.(?:sh|bash|zsh|fish|py|js|mjs|cjs|jsx|ts|mts|cts|tsx|rb|pl|php|lua|go|ps1"
    r"|exe|bin|jar)$"
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
    # core.hooksPath, core.fsmonitor, aliases, filters (#1043). A worktree or
    # submodule keeps its config outside the checkout: not covered.
    ".git/config",
)
_CROSS_ENGINE_TREES = (".claude", ".github/workflows", ".husky", ".git/hooks")
# Folders directly under a cross-engine tree that hold no config for *this*
# project (exact names, real folders only; a link there is still recorded).
# ``.claude/worktrees/<name>/`` is where Claude Code keeps the checkouts it
# makes with ``--worktree`` and for isolated subagents: each is a separate
# copy of the repo with its own config, loaded only by a session whose
# working directory is that worktree (Claude Code 2.1.296 docs, "Run
# parallel sessions with worktrees"). Walking them ran the budget out on
# every run in a repo that uses them (#1051). The agy set never looks here.
_CROSS_ENGINE_SKIP = {".claude": frozenset({"worktrees"})}

# Problem reasons (shown in the ⚠️ row / refusal text).
TOO_MANY = "too many files to check in time"
OUTSIDE = "links outside the project"
SPECIAL = "a FIFO or device file where agy loads config"
UNREADABLE = "files Untether can't read"
VARIABLE_PATH = "a hook or MCP command path Untether can't resolve"
TOO_LARGE = "a config file too large to check"
CHANGING = "files changing while they were checked"
# Each reason is shown inside the row's own parentheses, so none may contain any.
AMBIGUOUS = "a config file that isn't strict JSON, which agy might read differently"
OTHER_FORMAT = "a config file in a format Untether can't check, such as YAML or JSONC"
FRONT_MATTER = (
    "an agent file whose YAML front matter Untether can't read the way agy would"
)
TOO_DEEP = "a project folder too deep to check"

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
    # The agy set ran out of budget.
    truncated: bool = False
    cross_truncated: bool = False
    # A link leads outside both the project and $HOME.
    outside_root: bool = False
    # Every other reason the agy set couldn't be checked.
    problems: tuple[str, ...] = ()

    @property
    def unchecked_reason(self) -> str | None:
        """Why the agy set is "not checked", or None when the digest is
        complete. Callers must never compare or record the digest then."""
        if self.truncated:
            return TOO_MANY
        if self.outside_root:
            return OUTSIDE
        return self.problems[0] if self.problems else None

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


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _below_home(path: Path, home: Path) -> bool:
    return path != home and path not in home.parents


def _git_marker(directory: Path) -> bool:
    """*directory* is the top of a git checkout: its ``.git`` is a folder,
    or a ``gitdir:`` file (worktrees, submodules). An empty or unrelated
    file named ``.git`` isn't one (#1043). Never blocks on a FIFO."""
    marker = os.path.join(directory, ".git")
    try:
        st = os.stat(marker)
    except OSError:
        return False
    if stat.S_ISDIR(st.st_mode):
        return True
    if not stat.S_ISREG(st.st_mode):
        return False
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
    try:
        fd = os.open(marker, flags)
    except OSError:
        return False
    try:
        return os.read(fd, 64).startswith(b"gitdir:")
    except OSError:
        return False
    finally:
        os.close(fd)


def find_project_root(cwd: Path, *, home: Path | None = None) -> Path:
    """Nearest ancestor of *cwd* (inclusive) that is a git checkout
    (``_git_marker``); *cwd* if none.

    Never returns ``$HOME`` or anything above it (a dotfiles repo in ``$HOME``
    must not pull ``~/.claude`` into every project's scan).
    """
    home = (home or Path.home()).resolve()
    start = cwd.resolve()
    current = start
    for _ in range(MAX_ANCESTOR_LEVELS + 1):
        if not _below_home(current, home):
            break
        if _git_marker(current):
            return current
        if current.parent == current:
            break
        current = current.parent
    return start


def _root_search_exhausted(cwd: Path, *, home: Path | None = None) -> bool:
    """``find_project_root`` gave up at the ancestor cap: the real root (and
    any ``.agents/`` above the cap) was never looked at."""
    home = (home or Path.home()).resolve()
    current = cwd.resolve()
    for _ in range(MAX_ANCESTOR_LEVELS + 1):
        if (
            not _below_home(current, home)
            or _git_marker(current)
            or current.parent == current
        ):
            return False
        current = current.parent
    return True


def _agents_parents(cwd: Path) -> list[Path]:
    """Directories whose ``.agents/`` agy may load: cwd → the *outermost*
    git checkout above it (within the ancestor cap), and — when no checkout
    bounds the walk — every ancestor below ``$HOME``.

    The outermost, not the nearest: which ``.git`` agy stops at is its own
    business, and anything that can write in the project can create one in
    a sub-folder (a folder or a ``gitdir:`` file as easily as an empty
    file). A nested checkout therefore never drops the ``.agents/`` of the
    repository around it (#1043)."""
    home = Path.home().resolve()
    current = cwd.resolve()
    out: list[Path] = []
    bound = 0
    for _ in range(MAX_ANCESTOR_LEVELS + 1):
        if not _below_home(current, home):
            break
        out.append(current)
        if _git_marker(current):
            bound = len(out)
        if current.parent == current:
            break
        current = current.parent
    return out[:bound] if bound else out


def _is_user_manifest(rel: str) -> bool:
    """*rel* is relative to ``~/.gemini/config``."""
    folded = _fold(rel)
    name = folded.rsplit("/", 1)[-1]
    if name == "hooks.json" or folded.startswith("plugins/"):
        return True
    if folded in _TOP_MANIFESTS:
        return True
    # Flat (``agents/x.md``) and nested (``agents/<name>/agent.md``) agents.
    return folded.startswith("agents/") and name.endswith(".md")


def _is_reference_manifest(path: Path, base: Path) -> bool:
    """*path* (walked from *base*) may name scripts or folders agy loads: a
    JSON manifest, or a custom agent — any ``.md`` at any depth below an
    ``agents`` folder (``agents/x.md``, ``agents/<name>/agent.md``)."""
    name = _fold(path.name)
    if name in _REFERENCE_MANIFESTS:
        return True
    if not name.endswith(".md"):
        return False
    try:
        folders = path.relative_to(base).parts[:-1]
    except ValueError:
        folders = (path.parent.name,)
    return "agents" in {_fold(part) for part in (base.name, *folders)}


# The wall clock the time budget reads (tests swap it, #1053).
_clock = time.monotonic


class _Budget:
    """One per set: entries, bytes hashed, wall clock, plus the reasons the
    set couldn't be checked. Every early stop goes through here."""

    def __init__(self) -> None:
        self.deadline = _clock() + TIME_BUDGET_S
        self.entries = 0
        self.bytes = 0
        self.exceeded = False
        self.outside = False
        self.problems: list[str] = []

    def _check(self) -> bool:
        if (
            self.entries > MAX_FILES
            or self.bytes > MAX_HASH_BYTES
            or _clock() > self.deadline
        ):
            self.exceeded = True
        return not self.exceeded

    def take(self) -> bool:
        self.entries += 1
        return self._check()

    def alive(self) -> bool:
        """The budget still holds (no entry charged)."""
        return self._check()

    def spend(self, nbytes: int) -> bool:
        self.bytes += nbytes
        return self._check()

    def flag(self, reason: str) -> None:
        if reason not in self.problems:
            self.problems.append(reason)


def _contained(
    path: str | Path, bases: tuple[str, ...], exclude: tuple[str, ...] = ()
) -> bool:
    """*path*'s real path is inside one of *bases* and none of *exclude*."""
    real = os.path.realpath(path)

    def inside(base: str) -> bool:
        return real == base or real.startswith(base.rstrip(os.sep) + os.sep)

    return any(inside(b) for b in bases) and not any(inside(e) for e in exclude)


def _walk(
    base: Path,
    budget: _Budget,
    *,
    follow_within: tuple[str, ...],
    seen: set[str],
    skip_top: frozenset[str] = frozenset(),
    exclude: tuple[str, ...] = (),
) -> Iterator[Path]:
    """Every non-directory entry under *base* (and symlinked directories it
    won't follow), bounded per entry — a huge directory can't stall it.

    Symlinked directories are followed only while their real path stays
    inside *follow_within*, each real path once. Names in *skip_top* (exact)
    are skipped directly under *base*. A folder that can't be listed is
    flagged, never skipped quietly.
    """
    stack = [base]
    while stack:
        directory = stack.pop()
        real = os.path.realpath(directory)
        if real in seen:
            continue
        seen.add(real)
        if not budget.take():
            return
        entries: list[os.DirEntry[str]] = []
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    if not budget.take():
                        return
                    entries.append(entry)
        except OSError:
            budget.flag(UNREADABLE)
            continue
        subdirs: list[Path] = []
        for entry in sorted(entries, key=lambda e: e.name):
            path = Path(entry.path)
            try:
                is_link = entry.is_symlink()
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                budget.flag(UNREADABLE)
                yield path
                continue
            if directory == base and entry.name in skip_top and is_dir:
                continue
            if is_link and os.path.isdir(path):
                if follow_within and _contained(path, follow_within, exclude):
                    subdirs.append(path)
                else:
                    yield path  # recorded (and flagged) by the caller
            elif is_dir:
                subdirs.append(path)
            else:
                yield path
        stack.extend(reversed(subdirs))


def _stamp(st: os.stat_result) -> str:
    return f"{st.st_mtime_ns}:ctime:{st.st_ctime_ns}"


def _hash_regular(
    path: Path, st: os.stat_result, budget: _Budget, *, collect: bool
) -> tuple[Fingerprint | None, bytes | None]:
    """Hash the whole file (to EOF, whatever ``st_size`` claims) without ever
    blocking on a FIFO or device: ``O_NONBLOCK`` plus a post-open ``fstat``
    that must match the file that was stat'd. None for an empty file."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        budget.flag(UNREADABLE)
        return (st.st_size, f"unreadable:{_stamp(st)}"), None
    digest = hashlib.sha256()
    kept = bytearray() if collect else None
    total = 0
    try:
        fst = os.fstat(fd)
        if (
            not stat.S_ISREG(fst.st_mode)
            or fst.st_ino != st.st_ino
            or fst.st_dev != st.st_dev
        ):
            budget.flag(CHANGING)
            return (-1, f"swapped:{_stamp(fst)}"), None
        while True:
            chunk = os.read(fd, _CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if kept is not None:
                if len(kept) + len(chunk) > _REF_PARSE_CAP_BYTES:
                    budget.flag(TOO_LARGE)
                    kept = None
                else:
                    kept.extend(chunk)
            if not budget.spend(len(chunk)):
                return (total, "unfinished"), None
    except OSError:
        budget.flag(UNREADABLE)
        return (st.st_size, f"unreadable:{_stamp(st)}"), None
    finally:
        os.close(fd)
    if total == 0:
        return None, None
    return (total, digest.hexdigest()), (bytes(kept) if kept is not None else None)


def _fingerprint(
    path: Path,
    budget: _Budget,
    *,
    read: bool,
    contain: tuple[str, ...],
    collect: bool = False,
    exclude: tuple[str, ...] = (),
) -> tuple[Fingerprint | None, bytes | None]:
    """Fingerprint one entry. Never raises; never opens anything but a
    regular file; never follows a link out of *contain*. Anything it can't
    vouch for is flagged on *budget* (→ "not checked"), never dropped."""
    try:
        lst = os.lstat(path)
    except OSError:
        budget.flag(UNREADABLE)
        return (-1, "unstatable"), None
    target = ""
    if stat.S_ISLNK(lst.st_mode):
        try:
            target = os.readlink(path)
        except OSError:
            target = "?"
        if contain and not _contained(path, contain, exclude):
            budget.outside = True
            return (-1, f"outside:{target}"), None
        try:
            st = os.stat(path)
        except OSError:
            # Dangling: agy can't load it; creating the target changes this.
            return (-1, f"dangling:{target}"), None
    else:
        st = lst
    link = f":link:{target}" if target else ""
    if stat.S_ISDIR(st.st_mode):
        # A directory link the walk wouldn't follow: its contents are unseen.
        budget.outside = True
        return (-1, f"dir{link}"), None
    if not stat.S_ISREG(st.st_mode):
        budget.flag(SPECIAL)
        return (-1, f"special:{stat.S_IFMT(st.st_mode)}{link}"), None
    if not read:
        if st.st_size == 0:
            return None, None
        return (st.st_size, f"{_stamp(st)}{link}"), None
    fp, data = _hash_regular(path, st, budget, collect=collect)
    if fp is None:
        return None, None
    return (fp[0], f"{fp[1]}{link}"), data


def _user_controlled(path: str) -> bool:
    """This user owns or can write *path* or any folder above it (so could
    replace it). System files (root-owned, read-only chain) are not."""
    uid = os.getuid() if hasattr(os, "getuid") else None
    current = path
    while True:
        try:
            st = os.stat(current)
        except OSError:
            return True  # can't tell: assume it can be changed
        if (uid is not None and st.st_uid == uid) or os.access(current, os.W_OK):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-standard constant {value}")


@dataclass(frozen=True, slots=True)
class _Manifest:
    """What a reference manifest says, as far as the scan reads it."""

    # Every key and string value (JSON) or front-matter line (agent).
    strings: list[str]
    # JSON only — (key, value): a string held directly by a key, or in a
    # list directly under it; the key is folded, ``""`` at the top level.
    keyed: list[tuple[str, str]] = field(default_factory=list)


def _strict_json(data: bytes) -> _Manifest | None:
    """A strict-JSON document's strings, decoded (so ``\\/`` and ``\\u``
    escapes resolve as agy sees them), or None when the document isn't
    strict JSON (BOM, comments, trailing commas, duplicate keys,
    NaN/Infinity, bad UTF-8)."""
    try:
        text = data.decode("utf-8")  # strict; a BOM survives and fails below
        doc = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    out = _Manifest(strings=[])

    def walk(value: Any, key: str, direct: bool) -> None:
        if isinstance(value, str):
            out.strings.append(value)
            if direct:
                out.keyed.append((key, value))
        elif isinstance(value, dict):
            for name, item in value.items():
                out.strings.append(name)
                walk(item, _fold(name), True)
        elif isinstance(value, list):
            for item in value:
                walk(item, key, direct and not isinstance(item, list))

    walk(doc, "", True)
    return out


_LINE_BREAK = re.compile(r"\r\n|\n|\r")
# Breaks ``str.splitlines`` honours and YAML 1.2 doesn't (or the reverse).
_ODD_BREAKS = re.compile("[\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")
# Front-matter keys whose values are paths agy loads config from (skills and
# rules are instruction trees, see the limitations above).
_PATH_KEYS = frozenset({"plugins", "hooks", "agents", "mcp"})
_FRONT_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:(.*)$")
# Manifests whose strings are commands and their arguments, not paths.
_COMMAND_MANIFESTS = frozenset({"hooks.json", "mcp_config.json"})
# Manifests that are nothing but a list of folders to load.
_LISTING_MANIFESTS = frozenset({"plugins.json", "agents.json"})


def _front_matter(data: bytes) -> list[str] | None:
    """The front-matter lines of a custom agent (``---`` … ``---``), ``[]``
    when it has none; None when agy's reader might delimit or decode it
    differently (BOM, an indented or late opener, escapes, odd line breaks)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = _LINE_BREAK.split(text)
    if lines[0].rstrip(" \t") != "---":
        first = next((line for line in lines if line.strip()), "")
        if first.lstrip("\ufeff").strip() == "---":
            return None
        return []
    body: list[str] = []
    for line in lines[1:]:
        # Column 0 only: an indented ``---`` inside a block scalar is text.
        if line.rstrip(" \t") in {"---", "..."}:
            break
        body.append(line)
    if any("\\" in line or _ODD_BREAKS.search(line) for line in body):
        return None
    return body


# A path key named anywhere on a line the reader below can't place.
_PATH_WORD = re.compile(r"(?i)(?<![\w-])(?:plugins|hooks|agents|mcp)(?![\w-])")
# ... or used as a mapping key somewhere other than column 0.
_PATH_KEY_NESTED = re.compile(
    r"(?i)(?<![\w-])[\"']?(?:plugins|hooks|agents|mcp)[\"']?\s*:(?:\s|$)"
)
# A value that starts with a YAML anchor, alias, tag or flow mapping.
_YAML_INDICATOR = re.compile(
    r"^\s*(?:-\s+)*(?:[\"']?[\w.-]+[\"']?\s*:\s+)?\[?\s*[&*!{]"
)
_YAML_COMMENT = re.compile(r"(?:^|\s)#")
_FRONT_COMMAND = re.compile(r"(?i)^\s*(?:-\s+)*[\"']?command[\"']?\s*:\s*(.*)$")


def _front_matter_paths(lines: list[str]) -> tuple[list[str], list[str], bool]:
    """Values under the path keys of a custom agent's front matter (scalar,
    flow list or block list), the hook commands written there, and whether
    the front matter is *ambiguous*: this is a line reader, not a YAML
    parser, so a path key it can't place (quoted, inside a flow mapping,
    nested under another key, an explicit ``? key``) or a value it can't
    read (anchor, alias, tag, flow mapping, a comment beside quotes) makes
    the scan "not checked" instead of quietly reading it as nothing (#1043).
    """
    out: list[str] = []
    commands: list[str] = []
    ambiguous = False
    current: str | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue  # YAML skips these inside a block list too
        match = _FRONT_KEY.match(line)
        if match is not None:
            current = _fold(match.group(1))
            value = match.group(2)
        elif line[:1] in {" ", "\t", "-"}:
            value = line
        else:
            current = None
            ambiguous = ambiguous or bool(_PATH_WORD.search(line))
            continue
        if current not in _PATH_KEYS:
            ambiguous = ambiguous or bool(_PATH_KEY_NESTED.search(value))
            continue
        comment = _YAML_COMMENT.search(value)
        if comment is not None:
            if '"' in value or "'" in value:
                ambiguous = True  # can't tell a comment from quoted text
            value = value[: comment.start()]
        if _YAML_INDICATOR.match(value):
            ambiguous = True
        command = _FRONT_COMMAND.match(value)
        if command is not None:
            commands.append(command.group(1).strip())
        for part in value.strip().strip("[]").split(","):
            part = part.strip().lstrip("-").strip().strip("\"'")
            if part:
                out.append(part)
                out.append(part.rsplit(": ", 1)[-1].strip().strip("\"'"))
    return out, commands, ambiguous


def _manifest_paths(manifest: Path, parsed: _Manifest) -> tuple[list[str], list[str]]:
    """Strings of a JSON *manifest* that may name a folder or manifest agy
    loads: those to try at every base, and bare names to try beside the
    manifest only.

    A bare name (``"tools"``) is tried at every base only where the manifest
    plainly lists config — directly under a ``plugins`` / ``hooks`` /
    ``agents`` / ``mcp`` key, or anywhere in ``plugins.json`` /
    ``agents.json``. Any other bare string (a plugin's ``name``, a keyword)
    would otherwise pull a same-named project folder (``docs/``) into the
    digest and hold the crons on every edit there (#1043)."""
    name = _fold(manifest.name)
    if name in _COMMAND_MANIFESTS:
        return [], []
    listing = name in _LISTING_MANIFESTS
    everywhere: list[str] = []
    beside: list[str] = []
    for value in parsed.strings:
        if "/" in value or _fold(value).endswith((".json", ".md")):
            everywhere.append(value)
        elif _bare_name(value):
            beside.append(value)
    for key, value in parsed.keyed:
        if _bare_name(value) and (listing or key in _PATH_KEYS):
            everywhere.append(value)
    return everywhere, beside


def _bare_name(value: str) -> bool:
    """A single path component that could be a folder's name."""
    return (
        0 < len(value) <= 255
        and "/" not in value
        and "\n" not in value
        and not value.startswith("~")
        and value not in {".", ".."}
    )


_HOME_VAR = re.compile(r"\$(?:HOME|\{HOME\})(?=/)")
_GLOB = re.compile(r"[*?\[\]{}]")


def _words(value: str) -> tuple[list[str], bool]:
    """Shell words of *value* (quotes removed, concatenation resolved) plus
    the raw tokens, and whether the command builds anything dynamically: a
    variable other than ``$HOME``, command substitution or backticks
    anywhere, or a glob / brace in a path-ish word."""
    value = _HOME_VAR.sub("~", value)
    words: list[str] = [t for t in _RAW_SPLIT.split(value) if t]
    try:
        lexer = shlex.shlex(value, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words.extend(lexer)
    except ValueError:
        pass  # unbalanced quotes: the shell refuses it too; raw tokens stand
    pathish = "/" in value or bool(_SCRIPT_SUFFIX.search(value))
    dynamic = pathish and ("$" in value or "`" in value)
    dynamic = dynamic or any(
        _GLOB.search(w) and ("/" in w or _SCRIPT_SUFFIX.search(w)) for w in words
    )
    return words, dynamic


def _dynamic_command(command: str) -> bool:
    """The command word itself is built at run time (``$RUNNER``,
    ``$(cat cmd)``, backticks): nothing the scan could follow (#1043).
    ``$HOME/…`` is resolved; a variable further along (``echo $USER``) is an
    argument, and stays the documented "indirect code" limitation."""
    word = _HOME_VAR.sub("~", command).lstrip().lstrip("\"'")
    return word[:1] in {"$", "`"}


def _references(
    strings: list[str],
    manifest: Path,
    bases: tuple[str, ...],
    budget: _Budget,
) -> Iterator[str]:
    """Real paths of user-controlled regular files that *strings* name, at
    every base where the name resolves."""
    candidates: set[str] = set()
    for value in strings:
        words, dynamic = _words(value)
        if dynamic:
            budget.flag(VARIABLE_PATH)
        if len(value) <= 4096:
            words.append(value)  # an MCP ``args`` entry is literal argv
        for word in words:
            if not word or "\0" in word or "\n" in word:
                continue
            if _SHELL_DYNAMIC.search(word):
                continue  # can't resolve; already flagged above
            if word.startswith(("http://", "https://")):
                continue
            candidates.add(os.path.expanduser(word))
    for word in sorted(candidates):
        # A bare word is nearly always prose or a PATH command: it costs an
        # entry only when a file by that name is really there.
        pathish = "/" in word or bool(_SCRIPT_SUFFIX.search(word))
        roots = ("",) if os.path.isabs(word) else (str(manifest.parent), *bases)
        for base in roots:
            if not (budget.take() if pathish else budget.alive()):
                return
            candidate = os.path.normpath(os.path.join(base, word))
            try:
                st = os.stat(candidate)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if not pathish and not budget.take():
                return
            real = os.path.realpath(candidate)
            if _user_controlled(real):
                yield real


def _holds(folder: str, *paths: str) -> bool:
    """*folder* is one of *paths* or an ancestor of one."""
    prefix = folder.rstrip(os.sep) + os.sep
    return any(p == folder or p.startswith(prefix) for p in paths)


def _display(real: str, root_real: str, home_real: str) -> str:
    if real == root_real or real.startswith(root_real + os.sep):
        return os.path.relpath(real, root_real)
    if real.startswith(home_real + os.sep):
        return "~/" + os.path.relpath(real, home_real)
    return real


def _scan_agents(
    root: Path, cwd: Path, out: dict[str, Fingerprint], budget: _Budget
) -> None:
    root_real = os.path.realpath(root)
    home_real = os.path.realpath(Path.home())
    gemini_real = os.path.realpath(Path.home() / ".gemini")
    cwd_real = os.path.realpath(cwd)
    # Links may lead anywhere in the project or $HOME (shared agent folders),
    # never into ~/.gemini (D33) or outside both.
    follow = (root_real, home_real)
    no_follow = (gemini_real,)
    seen: set[str] = set()
    pending: deque[tuple[Path, bytes]] = deque()

    def track(path: Path, key: str, contain: tuple[str, ...], base: Path) -> bool:
        """Fingerprint one file walked from *base*; False once the budget
        is gone."""
        if not budget.take():
            return False
        if _ALT_MANIFEST.match(_fold(path.name)):
            budget.flag(OTHER_FORMAT)
        fp, data = _fingerprint(
            path,
            budget,
            read=True,
            contain=contain,
            collect=_is_reference_manifest(path, base),
            exclude=no_follow,
        )
        if budget.exceeded:
            return False
        if fp is not None:
            out[key] = fp
            if data is not None:
                pending.append((path, data))
        return True

    parents = _agents_parents(cwd)
    for parent in parents:
        agents = parent / _AGENTS_DIR
        if not os.path.lexists(agents):
            continue
        rel_agents = os.path.relpath(agents, root)
        # An ancestor's .agents/ (no .git bound) may link within its own tree.
        here = (*follow, os.path.realpath(parent))
        if not os.path.isdir(agents) or not _contained(agents, here, no_follow):
            fp, _ = _fingerprint(
                agents, budget, read=False, contain=here, exclude=no_follow
            )
            if fp is not None:
                out[rel_agents] = fp
            continue
        for path in _walk(
            agents,
            budget,
            follow_within=here,
            seen=seen,
            skip_top=_INSTRUCTION_TREES,
            exclude=no_follow,
        ):
            if not track(path, os.path.relpath(path, root), here, agents):
                return

    def follow_path(word: str, manifest: Path, *, everywhere: bool = True) -> bool:
        """Walk the folder, or queue the manifest, that *word* names — at
        the manifest's folder, and (*everywhere*) the run's cwd and the
        project root."""
        if os.path.isabs(word):
            roots: tuple[str, ...] = ("",)
        else:
            roots = (str(manifest.parent), *(bases if everywhere else ()))
        # A bare name is usually prose: like a bare word in a command, it
        # costs an entry only when something by that name is really there.
        pathish = "/" in word
        for base in roots:
            if not (budget.take() if pathish else budget.alive()):
                return False
            try:
                real = os.path.realpath(os.path.join(base, word))
                st = os.stat(real)
            except (OSError, ValueError):
                continue
            if not pathish and not budget.take():
                return False
            if _contained(real, no_follow) or not _user_controlled(real):
                continue  # ~/.gemini is stat only (user-level set); system files
            if stat.S_ISDIR(st.st_mode):
                if _holds(real, root_real, cwd_real, home_real):
                    budget.flag(VARIABLE_PATH)  # would walk the whole project
                    continue
                for path in _walk(
                    Path(real),
                    budget,
                    follow_within=follow,
                    seen=seen,
                    skip_top=_INSTRUCTION_TREES,
                    exclude=no_follow,
                ):
                    key = _display(str(path), root_real, home_real)
                    if not track(path, key, follow, Path(real)):
                        return False
            elif (
                stat.S_ISREG(st.st_mode)
                and real not in parsed
                and _fold(real).endswith((".json", ".md"))
            ):
                fp, data = _fingerprint(
                    Path(real), budget, read=True, contain=(), collect=True
                )
                if budget.exceeded:
                    return False
                if fp is not None:
                    out[_display(real, root_real, home_real)] = fp
                if data is not None:
                    pending.append((Path(real), data))
        return True

    # Where a relative name may resolve: the run's cwd, the project root, and
    # the outermost checkout whose .agents/ is loaded (a nested ``.git`` moves
    # the root, never the place a repo-level hook's ``./script`` lives).
    outer_real = root_real
    if parents and _git_marker(parents[-1]):
        outer_real = os.path.realpath(parents[-1])
    bases = tuple(dict.fromkeys((cwd_real, root_real, outer_real)))
    parsed: set[str] = set()
    while pending:
        manifest, data = pending.popleft()
        manifest_real = os.path.realpath(manifest)
        if manifest_real in parsed:
            continue
        parsed.add(manifest_real)
        is_md = _fold(manifest.name).endswith(".md")
        everywhere: list[str]
        beside: list[str] = []
        if is_md:
            lines = _front_matter(data)
            if lines is None:
                budget.flag(FRONT_MATTER)
                continue
            content = _Manifest(strings=lines)
            everywhere, commands, ambiguous = _front_matter_paths(lines)
            if ambiguous:
                budget.flag(FRONT_MATTER)
        else:
            loaded = _strict_json(data)
            if loaded is None:
                budget.flag(AMBIGUOUS)
                continue
            content = loaded
            everywhere, beside = _manifest_paths(manifest, content)
            commands = [value for key, value in content.keyed if key == "command"]
        if any(_dynamic_command(command) for command in commands):
            budget.flag(VARIABLE_PATH)
        for real in _references(content.strings, manifest, bases, budget):
            if budget.exceeded:
                return
            key = _display(real, root_real, home_real)
            if key in out:
                continue
            under_gemini = _contained(real, no_follow)
            fp, _ = _fingerprint(Path(real), budget, read=not under_gemini, contain=())
            if budget.exceeded:
                return
            if fp is not None:
                out[key] = fp
        if budget.exceeded:
            return
        wide = set(everywhere)
        for word in sorted(wide | set(beside)):
            if not word or "\0" in word or _SHELL_DYNAMIC.search(word):
                continue
            if not follow_path(
                os.path.expanduser(word), manifest, everywhere=word in wide
            ):
                return


def _plain_file(path: Path) -> bool:
    """A regular file that isn't a link (lstat only)."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _scan_user_level(out: dict[str, Fingerprint], budget: _Budget) -> None:
    """Stat only — never ``open()`` anything under ``~/.gemini`` (D33) but
    Untether's own gate plugin files (``_own_gate_files``). Linked folders are followed while
    they stay in ``$HOME`` (dotfiles)."""
    user_dir = user_config_dir()
    if not os.path.isdir(user_dir):
        return
    follow = (os.path.realpath(Path.home()),)
    seen: set[str] = set()
    own_gate = _own_gate_files()
    for path in _walk(user_dir, budget, follow_within=follow, seen=seen):
        rel = path.relative_to(user_dir).as_posix()
        if rel in own_gate and _plain_file(path):
            continue  # Untether's own gate plugin, unchanged: see _GATE_PLUGIN_DIR
        if rel == _MANAGED_AGENT and _plain_file(path):
            continue  # Untether's own agent, see _MANAGED_AGENT
        if not _is_user_manifest(rel) and not path.is_symlink():
            continue
        if not budget.take():
            return
        fp, _ = _fingerprint(path, budget, read=False, contain=follow)
        if fp is not None:
            out[f"{USER_DISPLAY_PREFIX}{rel}"] = fp


def _scan_cross_engine(
    root: Path, out: dict[str, Fingerprint], budget: _Budget
) -> None:
    """Stat only; links recorded, not followed. Its problems never refuse."""
    candidates: list[Path] = [root / rel for rel in _CROSS_ENGINE_FILES]
    seen: set[str] = set()
    for tree in _CROSS_ENGINE_TREES:
        base = root / tree
        if os.path.isdir(base) and not os.path.islink(base):
            candidates.extend(
                _walk(
                    base,
                    budget,
                    follow_within=(),
                    seen=seen,
                    skip_top=_CROSS_ENGINE_SKIP.get(tree, frozenset()),
                )
            )
        elif os.path.lexists(base):
            candidates.append(base)
        if budget.exceeded:
            return
    for path in candidates:
        if not os.path.lexists(path):
            continue
        if not budget.take():
            return
        fp, _ = _fingerprint(path, budget, read=False, contain=())
        if fp is not None:
            out[path.relative_to(root).as_posix()] = fp


def scan_workspace_config(cwd: Path) -> ScanResult:
    """Fingerprint both sets for a run in *cwd*.

    Blocking and bounded (entries, bytes, wall clock); callers run it off the
    event loop under ``SCAN_TIMEOUT_S``."""
    root = find_project_root(cwd)
    agy: dict[str, Fingerprint] = {}
    cross: dict[str, Fingerprint] = {}
    agy_budget = _Budget()
    if _root_search_exhausted(cwd):
        agy_budget.flag(TOO_DEEP)
    _scan_agents(root, cwd, agy, agy_budget)
    if not agy_budget.exceeded:
        _scan_user_level(agy, agy_budget)
    cross_budget = _Budget()
    _scan_cross_engine(root, cross, cross_budget)
    result = ScanResult(
        root=root,
        agy=agy,
        cross=cross,
        truncated=agy_budget.exceeded,
        cross_truncated=cross_budget.exceeded,
        outside_root=agy_budget.outside,
        problems=tuple(agy_budget.problems),
    )
    if result.unchecked_reason is not None or cross_budget.exceeded:
        logger.warning(
            "antigravity.workspace_config.unchecked",
            root=str(root),
            reason=result.unchecked_reason,
            problems=list(result.problems),
            agy_files=len(agy),
            cross_files=len(cross),
            cross_truncated=cross_budget.exceeded,
        )
    return result
