#!/usr/bin/env python3
"""Validate release metadata consistency.

Checks:
  - pyproject.toml version has a matching CHANGELOG.md section
  - Changelog date is valid ISO format
  - Changelog entries reference GitHub issues
  - Changelog subsection headings are from the allowed set

Exit codes:
  0 = all checks pass
  1 = one or more checks failed (details on stderr)
"""

from __future__ import annotations

import re
import sys
import tomllib
from datetime import datetime
from pathlib import Path

REPO_URL = "https://github.com/littlebearapps/untether"
ALLOWED_SUBSECTIONS = {"fixes", "changes", "breaking", "docs", "tests"}

# Patterns
VERSION_HEADING = re.compile(r"^## v(\S+) \((\d{4}-\d{2}-\d{2})\)")
SUBSECTION_HEADING = re.compile(r"^### (\w+)")
TOP_LEVEL_ENTRY = re.compile(r"^- .+")
ISSUE_LINK = re.compile(r"\[#\d+\]")


def load_version() -> str:
    """Read version from pyproject.toml."""
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def find_changelog_section(version: str, lines: list[str]) -> tuple[int, str | None]:
    """Find the changelog section matching the given version.

    Returns (line_number, date_string) or (-1, None) if not found.
    """
    for i, line in enumerate(lines):
        m = VERSION_HEADING.match(line)
        if m and m.group(1) == version:
            return i, m.group(2)
    return -1, None


def validate_date(date_str: str) -> bool:
    """Check that the date is valid ISO format."""
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def validate_section(lines: list[str], start: int) -> tuple[list[str], list[str]]:
    """Validate a changelog section starting at `start`.

    Returns (warnings, errors).
    """
    warnings: list[str] = []
    errors: list[str] = []

    i = start + 1
    current_subsection: str | None = None

    while i < len(lines):
        line = lines[i]

        # Stop at the next version heading
        if VERSION_HEADING.match(line):
            break

        # Check subsection headings
        sub_match = SUBSECTION_HEADING.match(line)
        if sub_match:
            current_subsection = sub_match.group(1)
            if current_subsection not in ALLOWED_SUBSECTIONS:
                errors.append(
                    f"  line {i + 1}: unknown subsection '### {current_subsection}' "
                    f"(allowed: {', '.join(sorted(ALLOWED_SUBSECTIONS))})"
                )
            i += 1
            continue

        # Check top-level entries for issue links
        if (
            TOP_LEVEL_ENTRY.match(line)
            and current_subsection
            and not ISSUE_LINK.search(line)
        ):
            errors.append(
                f"  line {i + 1}: entry missing issue link [#N]({REPO_URL}/issues/N)"
            )

        i += 1

    return warnings, errors


def main() -> int:
    passed = 0
    failed = 0

    # 1. Load version
    version = load_version()
    print(f"Version: {version}")

    # 2. Find matching changelog section
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
    lines = changelog.splitlines()
    section_line, date_str = find_changelog_section(version, lines)

    if section_line < 0:
        print(f"FAIL: no changelog section found for v{version}")
        failed += 1
    else:
        print(f"OK: changelog section found at line {section_line + 1}")
        passed += 1

        # 3. Validate date
        if date_str and validate_date(date_str):
            print(f"OK: date {date_str} is valid")
            passed += 1
        else:
            print(f"FAIL: date '{date_str}' is not valid ISO format (YYYY-MM-DD)")
            failed += 1

        # 4. Validate section content
        warnings, errors = validate_section(lines, section_line)
        if errors:
            print(f"FAIL: {len(errors)} issue(s) in changelog section:")
            for err in errors:
                print(err)
            failed += 1
        else:
            print("OK: all changelog entries have issue links and valid subsections")
            passed += 1

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
