"""Screenshot references in the docs resolve to real files (#783).

Guards the class of regression a screenshot rename or removal can cause: a
dangling ``<img>`` in a published page, or a ``CAPTURES.md`` entry ticked for
a file that no longer exists.  References inside HTML comments (the
``<!-- TODO: capture screenshot -->`` placeholders) are ignored.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCREENSHOTS = _REPO_ROOT / "docs" / "assets" / "screenshots"
_REF_RE = re.compile(r"assets/screenshots/([A-Za-z0-9._-]+\.(?:jpg|png))")
_TICKED_RE = re.compile(r"^- \[x\] `([^`]+)`", re.MULTILINE)
_SKIP_DIRS = ("docs/plans/", "docs/findings/")


def _live_refs(text: str) -> list[str]:
    """Screenshot file names referenced outside HTML comments."""
    stripped = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return [m.group(1) for m in _REF_RE.finditer(stripped)]


def _doc_files() -> list[Path]:
    files = [
        p
        for p in sorted((_REPO_ROOT / "docs").rglob("*.md"))
        if not any(skip in p.as_posix() for skip in _SKIP_DIRS)
    ]
    return [*files, _REPO_ROOT / "README.md"]


def test_live_screenshot_refs_exist() -> None:
    missing = [
        (str(path.relative_to(_REPO_ROOT)), name)
        for path in _doc_files()
        for name in _live_refs(path.read_text(encoding="utf-8"))
        if not (_SCREENSHOTS / name).is_file()
    ]
    assert missing == []


def test_captures_ticked_entries_exist() -> None:
    text = (_SCREENSHOTS / "CAPTURES.md").read_text(encoding="utf-8")
    ticked = _TICKED_RE.findall(text)
    assert ticked
    assert [name for name in ticked if not (_SCREENSHOTS / name).is_file()] == []


def test_commented_todo_refs_are_ignored() -> None:
    commented = (
        "<!-- TODO: capture screenshot -->"
        '<!-- <img src="../assets/screenshots/nope.jpg"> -->'
    )
    assert _live_refs(commented) == []
    assert _live_refs('<img src="../assets/screenshots/nope.jpg">') == ["nope.jpg"]
