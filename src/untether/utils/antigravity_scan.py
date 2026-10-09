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
  fetches (``npx pkg``) — isn't tracked.
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
- every file under every ``.agents/`` from the cwd up to the project root
  (the nearest ancestor holding ``.git``; with no ``.git``, every ancestor
  below ``$HOME``), except the instruction trees above, hashed in full;
- every file a reference manifest (``hooks.json``, ``mcp_config.json``,
  ``plugin.json``, the 1.2.16 ``*.json`` manifests, the front matter of
  custom agents ``agents/*.md``; names compared case-insensitively) names by
  literal path, resolved like a shell would (JSON-decoded, then shell words)
  against the manifest's folder, the run's cwd and the project root: hashed
  when this user controls it (owns or can write it or any folder above it),
  ignored when it's a system file (``/usr/bin/python3``);
- agy's user-level manifests under ``~/.gemini/config/`` — stat only —
  excluding ``plugins/untether-gate/``.

"Not checked" whenever: a budget (entries, bytes, wall clock) runs out; a
link under ``.agents/`` (or in ``~/.gemini/config``) leads outside both the
project and ``$HOME``; a FIFO, device or socket sits where agy loads files;
an entry or folder can't be stat'd, listed or read; a file changes identity
while read; a reference manifest is too large, not strict JSON (comments,
trailing commas, BOM, duplicate keys, NaN), in another format agy might read
(``hooks.yaml``, ``mcp_config.jsonc`` …), or names a path through a variable,
command substitution or glob.

The cross-engine set (project root only, stat only) never refuses (REVIEW-2
M4); it only feeds the post-run ⚠️ row.

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
_GATE_PLUGIN = "plugins/untether-gate/"
_AGENTS_DIR = ".agents"
# Instruction trees directly under .agents/ (exact names: anything else is
# hashed — failing towards more coverage on case-insensitive filesystems).
_INSTRUCTION_TREES = frozenset({"skills", "rules"})
_TOP_MANIFESTS = frozenset(
    {"mcp_config.json", "skills.json", "rules.json", "plugins.json", "agents.json"}
)
# Manifests whose strings may name a script agy runs (casefolded).
_REFERENCE_MANIFESTS = _TOP_MANIFESTS | {"hooks.json", "plugin.json"}
# Other formats of the same manifests agy might also read: never assume.
_ALT_MANIFEST = re.compile(
    r"^(hooks|mcp_config|plugin|plugins|agents|skills|rules|settings)"
    r"\.(jsonc|json5|ya?ml|toml)$"
)
_RAW_SPLIT = re.compile(r"[\s;&|()<>'\"`=,\[\]{}]+")
_SHELL_DYNAMIC = re.compile(r"[$`*?\[\]{}]")
_SCRIPT_SUFFIX = re.compile(
    r"\.(?:sh|bash|zsh|fish|py|js|mjs|cjs|ts|rb|pl|php|lua|ps1|exe|bin|jar)$"
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

# Problem reasons (shown in the ⚠️ row / refusal text).
TOO_MANY = "too many files to check in time"
OUTSIDE = "links outside the project"
SPECIAL = "a FIFO or device file where agy loads config"
UNREADABLE = "files Untether can't read"
VARIABLE_PATH = "a hook or MCP command path Untether can't resolve"
TOO_LARGE = "a config file too large to check"
CHANGING = "files changing while they were checked"
AMBIGUOUS = "a config file agy might read differently (not strict JSON)"

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


def find_project_root(cwd: Path, *, home: Path | None = None) -> Path:
    """Nearest ancestor of *cwd* (inclusive) holding ``.git``; *cwd* if none.

    Never returns ``$HOME`` or anything above it (a dotfiles repo in ``$HOME``
    must not pull ``~/.claude`` into every project's scan).
    """
    home = (home or Path.home()).resolve()
    start = cwd.resolve()
    current = start
    for _ in range(MAX_ANCESTOR_LEVELS + 1):
        if not _below_home(current, home):
            break
        if (current / ".git").exists():
            return current
        if current.parent == current:
            break
        current = current.parent
    return start


def _agents_parents(cwd: Path, root: Path) -> list[Path]:
    """Directories whose ``.agents/`` agy may load: cwd → root, and — when
    no ``.git`` bounds the walk — every further ancestor below ``$HOME``."""
    home = Path.home().resolve()
    current = cwd.resolve()
    git_bounded = (root / ".git").exists()
    out: list[Path] = []
    for _ in range(MAX_ANCESTOR_LEVELS + 1):
        if not _below_home(current, home):
            break
        out.append(current)
        if (git_bounded and current == root) or current.parent == current:
            break
        current = current.parent
    return out


def _is_user_manifest(rel: str) -> bool:
    """*rel* is relative to ``~/.gemini/config``."""
    if rel.startswith(_GATE_PLUGIN):
        return False  # Untether's own gate plugin (rc3), trusted separately
    folded = _fold(rel)
    name = folded.rsplit("/", 1)[-1]
    if name == "hooks.json" or folded.startswith("plugins/"):
        return True
    if folded in _TOP_MANIFESTS:
        return True
    parts = folded.split("/")
    return len(parts) == 2 and parts[0] == "agents" and name.endswith(".md")


def _is_reference_manifest(path: Path) -> bool:
    name = _fold(path.name)
    return name in _REFERENCE_MANIFESTS or (
        name.endswith(".md") and _fold(path.parent.name) == "agents"
    )


class _Budget:
    """One per set: entries, bytes hashed, wall clock, plus the reasons the
    set couldn't be checked. Every early stop goes through here."""

    def __init__(self) -> None:
        self.deadline = time.monotonic() + TIME_BUDGET_S
        self.entries = 0
        self.bytes = 0
        self.exceeded = False
        self.outside = False
        self.problems: list[str] = []

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


def _strict_json_strings(data: bytes) -> list[str] | None:
    """Every key and string value of a strict-JSON document, decoded (so
    ``\\/`` and ``\\u`` escapes resolve as agy sees them), or None when the
    document isn't strict JSON (BOM, comments, trailing commas, duplicate
    keys, NaN/Infinity, bad UTF-8)."""
    try:
        text = data.decode("utf-8")  # strict; a BOM survives and fails below
        doc = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    out: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                out.append(key)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(doc)
    return out


def _front_matter(data: bytes) -> list[str] | None:
    """The front-matter lines of a custom agent (``---`` … ``---``); None
    when it uses escapes the scan can't decode the way agy's YAML would."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    body: list[str] = []
    for line in lines[1:]:
        if line.strip() in {"---", "..."}:
            break
        body.append(line)
    if any("\\" in line for line in body):
        return None
    return body


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


def _references(
    strings: list[str],
    manifest: Path,
    bases: tuple[str, ...],
    budget: _Budget,
) -> Iterator[str]:
    """Real paths of user-controlled regular files that *strings* name."""
    candidates: set[str] = set()
    for value in strings:
        words, dynamic = _words(value)
        if dynamic:
            budget.flag(VARIABLE_PATH)
        for word in words:
            if _SHELL_DYNAMIC.search(word):
                continue  # can't resolve; already flagged above
            if "/" not in word and not _SCRIPT_SUFFIX.search(word):
                continue  # bare words and PATH commands (known limitation)
            if word.startswith(("http://", "https://")):
                continue
            candidates.add(os.path.expanduser(word))
    for word in sorted(candidates):
        roots = ("",) if os.path.isabs(word) else (str(manifest.parent), *bases)
        for base in roots:
            if not budget.take():
                return
            candidate = os.path.normpath(os.path.join(base, word))
            try:
                st = os.stat(candidate)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            real = os.path.realpath(candidate)
            if _user_controlled(real):
                yield real
            break


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
    # Links may lead anywhere in the project or $HOME (shared agent folders),
    # never into ~/.gemini (D33) or outside both.
    follow = (root_real, home_real)
    no_follow = (gemini_real,)
    seen: set[str] = set()
    pending: list[tuple[Path, bytes]] = []
    for parent in _agents_parents(cwd, root):
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
            if not budget.take():
                return
            if _ALT_MANIFEST.match(_fold(path.name)):
                budget.flag(AMBIGUOUS)
            collect = _is_reference_manifest(path)
            fp, data = _fingerprint(
                path,
                budget,
                read=True,
                contain=here,
                collect=collect,
                exclude=no_follow,
            )
            if budget.exceeded:
                return
            if fp is None:
                continue
            out[os.path.relpath(path, root)] = fp
            if data is not None:
                pending.append((path, data))
    cwd_real = os.path.realpath(cwd)
    for manifest, data in pending:
        if _fold(manifest.name).endswith(".md"):
            strings = _front_matter(data)
        else:
            strings = _strict_json_strings(data)
        if strings is None:
            budget.flag(AMBIGUOUS)
            continue
        for real in _references(strings, manifest, (cwd_real, root_real), budget):
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


def _scan_user_level(out: dict[str, Fingerprint], budget: _Budget) -> None:
    """Stat only — never ``open()`` anything under ``~/.gemini`` (D33).
    Linked folders are followed while they stay in ``$HOME`` (dotfiles)."""
    user_dir = user_config_dir()
    if not os.path.isdir(user_dir):
        return
    follow = (os.path.realpath(Path.home()),)
    seen: set[str] = set()
    for path in _walk(user_dir, budget, follow_within=follow, seen=seen):
        rel = path.relative_to(user_dir).as_posix()
        if rel.startswith(_GATE_PLUGIN):
            continue
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
            candidates.extend(_walk(base, budget, follow_within=(), seen=seen))
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
