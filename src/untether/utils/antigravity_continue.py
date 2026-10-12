"""Which conversation ``/continue`` resumes for one project directory (#1039).

agy's ``--continue`` resumes the most recent conversation of the current
directory *or of a parent or child directory* (probe P-L5, agy 1.3.3), and it
has no flag for "this directory only". Untether's projects nest, so it never
passes ``--continue``. It reads the one file where agy records the last
conversation run from each directory,
``~/.gemini/antigravity-cli/cache/last_conversations.json``
(``{absolute path: conversation id}``), takes the entry for the exact run
directory and passes ``--conversation=<id>``.

Custom code, recorded in the Antigravity runner's "Native vs custom register"
(the narrow read is decision D33). Retire it when agy scopes ``--continue``
itself.

Rules:

- **Exact path.** The run directory is resolved the way the kernel reports it
  to the agy process Untether starts (``os.path.realpath``: absolute, symlinks
  resolved, no trailing slash) and compared with the keys as plain strings.
  Keys are never normalised and case is never folded, so a parent, child,
  sibling, or a key spelt another way never matches.
- **A shared id is not trusted.** A cross-directory resume makes agy write
  the *other* directory into the file too, so the file means "last
  conversation run from this path", not "started here". When the id for this
  directory is also listed under any other path, the entry is refused.
- **The file is untrusted input.** Missing, not a regular file, unreadable,
  too large, not JSON, not an object of strings, or an id that isn't a
  conversation id: no conversation. Nothing here raises, and nothing falls
  back to ``--continue``.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .antigravity_argv import AgyArgvError, check_conversation

# The real file is ~85 bytes a row (33 rows = 2.8 kB on lba-1, agy 1.3.3).
MAX_CACHE_BYTES = 1024 * 1024

FOUND = "found"
# No entry for this directory: there is nothing to continue yet.
MISSING_REASONS = frozenset({"no_cache", "no_entry"})
SHARED_ID = "shared_id"


@dataclass(frozen=True, slots=True)
class ContinueLookup:
    """``conversation_id`` is set only when ``reason == "found"``. The reason
    is a fixed word for the log; it never carries anything from the file."""

    conversation_id: str | None
    reason: str


def last_conversations_path() -> Path:
    """agy's own record of the last conversation per directory (tests point
    it elsewhere)."""
    return (
        Path.home()
        / ".gemini"
        / "antigravity-cli"
        / "cache"
        / "last_conversations.json"
    )


def _miss(reason: str) -> ContinueLookup:
    return ContinueLookup(conversation_id=None, reason=reason)


def _read(path: Path) -> tuple[bytes | None, str]:
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None, "no_cache"
    except (OSError, ValueError):
        return None, "unreadable"
    # Checked before opening: a FIFO here would block the read for ever.
    if not stat.S_ISREG(st.st_mode):
        return None, "not_a_file"
    if st.st_size > MAX_CACHE_BYTES:
        return None, "too_large"
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_CACHE_BYTES + 1)
    except (OSError, ValueError):
        return None, "unreadable"
    if len(raw) > MAX_CACHE_BYTES:
        return None, "too_large"
    return raw, ""


def lookup_conversation(cwd: Path) -> ContinueLookup:
    """The conversation agy last ran from exactly *cwd*, or why there is none
    Untether will resume. Blocking (one small file read); never raises."""
    try:
        target = os.path.realpath(cwd)
    except (OSError, ValueError):
        return _miss("no_run_dir")
    raw, reason = _read(last_conversations_path())
    if raw is None:
        return _miss(reason)
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return _miss("not_json")
    if not isinstance(data, dict) or not all(
        isinstance(value, str) for value in data.values()
    ):
        return _miss("wrong_shape")
    conversation_id = data.get(target)
    if conversation_id is None:
        return _miss("no_entry")
    try:
        check_conversation(conversation_id)
    except AgyArgvError:
        return _miss("invalid_id")
    if any(value == conversation_id for key, value in data.items() if key != target):
        return _miss(SHARED_ID)
    return ContinueLookup(conversation_id=conversation_id, reason=FOUND)
