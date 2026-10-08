#!/usr/bin/env python3
"""User-facing release announcements for GitHub Discussions (#1008).

Every stable release ships a plain-English post at
``.github/release-announcements/vX.Y.Z.md``. ``/pr-main`` writes it, Nathan
reviews it in the release PR, ``validate_release.py`` refuses a release PR
without a good one, and ``release.yml`` posts it to Discussions → Announcements
once PyPI has the version.

Usage:
  release_announcement.py check  [VERSION]            # validate the post
  release_announcement.py render [VERSION]            # print title + body
  release_announcement.py post   [VERSION] [--dry-run] [--repo OWNER/NAME]

VERSION defaults to the pyproject.toml version. ``post`` needs GITHUB_TOKEN
(discussions: write, contents: write) and is idempotent: an existing
discussion with the same title is reused, never duplicated.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

REPO = "littlebearapps/untether"
REPO_URL = f"https://github.com/{REPO}"
WEBSITE = "https://untether.cc"  # live from v0.36.0
ANNOUNCE_DIR = Path(".github/release-announcements")
CATEGORY_SLUG = "announcements"

REQUIRED_SECTIONS = ("TL;DR", "Upgrade")
# At least one of these must be present — a patch may have no new features.
CONTENT_SECTIONS = ("What's new for you", "Fixes you'll notice", "Under the hood")
HEADS_UP = "Heads up"
KNOWN_SECTIONS = (*REQUIRED_SECTIONS, *CONTENT_SECTIONS, HEADS_UP)
WORD_LIMITS = {"patch": (60, 500), "minor": (150, 1000), "major": (300, 1800)}

STABLE_RE = re.compile(r"^\d+\.\d+\.\d+$")
VERSION_HEADING = re.compile(r"^## v(\d+\.\d+\.\d+)\b(.*)$")
H2 = re.compile(r"^## +(.+?)\s*$")
FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
PLACEHOLDER = re.compile(
    r"\bTODO\b|\bTBD\b|\bXXX\b|\bFIXME\b|<[a-z][a-z0-9 ,.'’/…-]*\s[a-z0-9 ,.'’/…-]*>"
)
DEPRECATION = re.compile(r"deprecat", re.IGNORECASE)


# ── parsing ──────────────────────────────────────────────────────────────


def announcement_path(version: str, root: Path = Path(".")) -> Path:
    return root / ANNOUNCE_DIR / f"v{version}.md"


def section_name(heading: str) -> str:
    """``## ✨ What's new for you`` → ``What's new for you`` (leading emoji dropped)."""
    return re.sub(r"^[^\w]+", "", heading).strip()


@dataclass
class Announcement:
    title: str
    body: str
    sections: list[str] = field(default_factory=list)

    @property
    def words(self) -> int:
        return len(re.findall(r"[A-Za-z0-9][\w'’.-]*", self.body))


def parse(text: str) -> Announcement:
    title = ""
    body = text
    m = FRONT_MATTER.match(text)
    if m:
        body = text[m.end() :]
        for line in m.group(1).splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "title":
                title = value.strip().strip("\"'")
    sections = [section_name(h.group(1)) for h in map(H2.match, body.splitlines()) if h]
    return Announcement(title=title, body=body.strip() + "\n", sections=sections)


def changelog_section(version: str, changelog: str) -> tuple[str, str]:
    """Return (heading line, section text) for ``version`` ("", "") if absent."""
    lines = changelog.splitlines()
    for i, line in enumerate(lines):
        m = VERSION_HEADING.match(line)
        if m and m.group(1) == version:
            end = next(
                (
                    j
                    for j in range(i + 1, len(lines))
                    if VERSION_HEADING.match(lines[j])
                ),
                len(lines),
            )
            return line, "\n".join(lines[i + 1 : end])
    return "", ""


def release_type(version: str, changelog: str) -> str:
    """patch / minor / major, against the newest older stable CHANGELOG heading."""
    cur = tuple(int(p) for p in version.split("."))
    older = [
        v
        for v in (
            tuple(int(p) for p in m.group(1).split("."))
            for m in map(VERSION_HEADING.match, changelog.splitlines())
            if m
        )
        if v < cur
    ]
    if not older:
        return "minor"
    prev = max(older)
    if cur[0] != prev[0]:
        return "major"
    if cur[1] != prev[1]:
        return "minor"
    return "patch"


def needs_heads_up(section: str) -> bool:
    return bool(
        re.search(r"^### breaking\b", section, re.MULTILINE)
        or DEPRECATION.search(section)
    )


# ── check ────────────────────────────────────────────────────────────────


def check(version: str, root: Path = Path(".")) -> list[str]:
    """Return a list of problems (empty = the post is good to publish)."""
    path = announcement_path(version, root)
    if not path.is_file():
        return [
            f"missing {path.relative_to(root)} — write the user-facing announcement "
            f"(see {ANNOUNCE_DIR / 'TEMPLATE.md'})"
        ]
    ann = parse(path.read_text(encoding="utf-8"))
    changelog_path = root / "CHANGELOG.md"
    changelog = (
        changelog_path.read_text(encoding="utf-8") if changelog_path.is_file() else ""
    )
    problems: list[str] = []

    if not ann.title:
        problems.append("front matter has no `title:`")
    elif version not in ann.title:
        problems.append(f"title doesn't mention {version}: {ann.title!r}")

    problems.extend(
        f"missing section `## {name}`"
        for name in REQUIRED_SECTIONS
        if name not in ann.sections
    )
    if not any(name in ann.sections for name in CONTENT_SECTIONS):
        problems.append(
            "needs at least one of: " + ", ".join(f"`## {n}`" for n in CONTENT_SECTIONS)
        )
    unknown = [s for s in ann.sections if s not in KNOWN_SECTIONS]
    if unknown:
        problems.append(
            "unknown section(s) "
            + ", ".join(f"`## {s}`" for s in unknown)
            + f" (allowed: {', '.join(KNOWN_SECTIONS)})"
        )

    _, section = changelog_section(version, changelog)
    if section and needs_heads_up(section) and HEADS_UP not in ann.sections:
        problems.append(
            "the CHANGELOG has breaking or deprecation entries — add `## ⚠️ Heads up` "
            "with what users must do"
        )

    hits = sorted(
        {m.group(0) for m in PLACEHOLDER.finditer(ann.title + "\n" + ann.body)}
    )
    if hits:
        problems.append("placeholder text left in: " + ", ".join(hits))

    kind = release_type(version, changelog)
    lo, hi = WORD_LIMITS[kind]
    if not lo <= ann.words <= hi:
        problems.append(
            f"{ann.words} words — a {kind} release post should be {lo}–{hi}"
        )
    return problems


# ── render ───────────────────────────────────────────────────────────────


def github_anchor(heading: str) -> str:
    text = heading.lstrip("#").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def footer(version: str, changelog: str) -> str:
    heading, _ = changelog_section(version, changelog)
    anchor = github_anchor(heading) if heading else f"v{version.replace('.', '')}"
    d = f"{REPO_URL}/discussions/categories"
    return (
        "\n---\n\n"
        f"**Links:** [Full changelog]({REPO_URL}/blob/master/CHANGELOG.md#{anchor}) · "
        f"[GitHub release]({REPO_URL}/releases/tag/v{version}) · "
        f"[PyPI](https://pypi.org/project/untether/{version}/) · "
        f"[Website]({WEBSITE}) · "
        "[Help centre](https://littlebearapps.com/help/untether/)\n\n"
        f"💬 Questions? Ask in [Q&A]({d}/q-a). Got an idea? Share it in "
        f"[Ideas]({d}/ideas). Built something with Untether? Post it in "
        f"[Show and tell]({d}/show-and-tell). Found a bug? "
        f"[Open an issue]({REPO_URL}/issues/new/choose).\n"
    )


def render(version: str, root: Path = Path(".")) -> tuple[str, str]:
    ann = parse(announcement_path(version, root).read_text(encoding="utf-8"))
    changelog_path = root / "CHANGELOG.md"
    changelog = (
        changelog_path.read_text(encoding="utf-8") if changelog_path.is_file() else ""
    )
    return ann.title, ann.body + footer(version, changelog)


# ── post ─────────────────────────────────────────────────────────────────

Http = Callable[[str, str, dict | None], dict]


def make_http(token: str, api: str = "https://api.github.com") -> Http:
    def http(method: str, path: str, payload: dict | None) -> dict:
        url = path if path.startswith("http") else f"{api}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"_status": 404}
            raise SystemExit(f"GitHub API {method} {path} → HTTP {exc.code}") from exc
        out = json.loads(raw) if raw else {}
        if isinstance(out, dict) and out.get("errors"):
            raise SystemExit(f"GitHub GraphQL error: {out['errors']}")
        return out

    return http


def graphql(http: Http, query: str, **variables: object) -> dict:
    return http("POST", "/graphql", {"query": query, "variables": variables})["data"]


_LOOKUP = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    id
    discussionCategories(first: 25) { nodes { id slug } }
    discussions(first: 50, orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes { title url category { slug } }
    }
  }
}
"""

_CREATE = """
mutation($repo: ID!, $cat: ID!, $title: String!, $body: String!) {
  createDiscussion(input: {repositoryId: $repo, categoryId: $cat, title: $title, body: $body}) {
    discussion { url }
  }
}
"""


def post(
    version: str,
    http: Http,
    *,
    repo: str = REPO,
    root: Path = Path("."),
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> str | None:
    """Create (or reuse) the Announcements discussion and link it from the release."""
    problems = check(version, root)
    if problems:
        raise SystemExit("announcement not publishable:\n  " + "\n  ".join(problems))
    title, body = render(version, root)
    owner, name = repo.split("/", 1)
    data = graphql(http, _LOOKUP, owner=owner, name=name)["repository"]
    cat = next(
        (
            c["id"]
            for c in data["discussionCategories"]["nodes"]
            if c["slug"] == CATEGORY_SLUG
        ),
        None,
    )
    if cat is None:
        raise SystemExit(f"no '{CATEGORY_SLUG}' discussion category in {repo}")

    # Only reuse a post in Announcements, where only maintainers can start
    # discussions — anyone can open a same-titled thread in General or Ideas,
    # and that must never be linked from the official release.
    existing = next(
        (
            d["url"]
            for d in data["discussions"]["nodes"]
            if d["title"] == title
            and (d.get("category") or {}).get("slug") == CATEGORY_SLUG
        ),
        None,
    )
    if existing:
        log(f"already posted: {existing}")
        url = existing
    elif dry_run:
        log(f"[dry-run] would post {title!r} ({len(body)} chars) to {CATEGORY_SLUG}")
        return None
    else:
        url = graphql(http, _CREATE, repo=data["id"], cat=cat, title=title, body=body)[
            "createDiscussion"
        ]["discussion"]["url"]
        log(f"posted: {url}")

    release = http("GET", f"/repos/{repo}/releases/tags/v{version}", None)
    if release.get("_status") == 404 or "id" not in release:
        log(
            f"::warning::no GitHub release v{version} yet — discussion not linked from it"
        )
    elif url not in (release.get("body") or ""):
        if dry_run:
            log(f"[dry-run] would link {url} from release v{version}")
        else:
            new_body = (release.get("body") or "").rstrip() + (
                f"\n\n💬 **Discuss this release:** {url}\n"
            )
            http("PATCH", f"/repos/{repo}/releases/{release['id']}", {"body": new_body})
            log(f"linked from release v{version}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and not dry_run:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"📣 Release announcement: {url}\n")
    return url


# ── CLI ──────────────────────────────────────────────────────────────────


def pyproject_version(root: Path = Path(".")) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("check", "render", "post"))
    ap.add_argument("version", nargs="?")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", REPO))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    version = (args.version or pyproject_version()).removeprefix("v")
    if not STABLE_RE.match(version):
        print(
            f"{version} is a pre-release — announcements are for stable releases only"
        )
        return 0 if args.command == "check" else 1

    if args.command == "check":
        problems = check(version)
        for p in problems:
            print(f"FAIL: {p}")
        if not problems:
            print(f"OK: release announcement for v{version} is ready")
        return 1 if problems else 0
    if args.command == "render":
        title, body = render(version)
        print(f"# {title}\n\n{body}")
        return 0
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("GITHUB_TOKEN (or GH_TOKEN) is required for post", file=sys.stderr)
        return 2
    post(version, make_http(token), repo=args.repo, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
