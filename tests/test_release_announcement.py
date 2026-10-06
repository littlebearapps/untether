"""#1008: user-facing release announcements (scripts/release_announcement.py)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ra = _load("release_announcement")

CHANGELOG = """# changelog

## v1.3.0 (2026-10-10)

### changes

- new thing [#1](https://github.com/littlebearapps/untether/issues/1)

## v1.2.1 (2026-09-01)

### fixes

- a fix [#2](https://github.com/littlebearapps/untether/issues/2)

## v1.2.0 (2026-08-01)

### breaking

- removed X [#3](https://github.com/littlebearapps/untether/issues/3)
"""

WORDS = " ".join(["useful"] * 160)

GOOD = f"""---
title: "Untether 1.3.0 — smoother approvals"
---

## TL;DR
Approvals are smoother. {WORDS}

## ✨ What's new for you
- **Faster buttons** — tap once and you're done.

## ⬆️ Upgrade
`uv tool upgrade untether`, then restart Untether.
"""


def _repo(tmp_path: Path, version: str, text: str | None, changelog: str = CHANGELOG):
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    if text is not None:
        d = tmp_path / ".github" / "release-announcements"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"v{version}.md").write_text(text, encoding="utf-8")
    return tmp_path


def test_good_announcement_passes(tmp_path: Path) -> None:
    assert ra.check("1.3.0", _repo(tmp_path, "1.3.0", GOOD)) == []


def test_missing_file_fails_with_template_hint(tmp_path: Path) -> None:
    (problem,) = ra.check("1.3.0", _repo(tmp_path, "1.3.0", None))
    assert "missing" in problem and "TEMPLATE.md" in problem


def test_title_must_exist_and_name_the_version(tmp_path: Path) -> None:
    no_title = GOOD.replace('title: "Untether 1.3.0 — smoother approvals"', "")
    assert any(
        "title" in p for p in ra.check("1.3.0", _repo(tmp_path, "1.3.0", no_title))
    )
    wrong = GOOD.replace("1.3.0 —", "1.2.9 —")
    assert any(
        "doesn't mention" in p
        for p in ra.check("1.3.0", _repo(tmp_path, "1.3.0", wrong))
    )


def test_required_and_content_sections(tmp_path: Path) -> None:
    text = GOOD.replace("## ⬆️ Upgrade", "## Upgrading").replace(
        "## ✨ What's new for you", "## Stuff"
    )
    problems = ra.check("1.3.0", _repo(tmp_path, "1.3.0", text))
    assert any("`## Upgrade`" in p for p in problems)
    assert any("needs at least one of" in p for p in problems)
    assert any("unknown section" in p for p in problems)


@pytest.mark.parametrize("marker", ["TODO", "TBD", "<short benefit-led subtitle>"])
def test_placeholders_fail(tmp_path: Path, marker: str) -> None:
    text = GOOD.replace("Approvals are smoother.", f"Approvals are smoother {marker}.")
    problems = ra.check("1.3.0", _repo(tmp_path, "1.3.0", text))
    assert any("placeholder" in p for p in problems)


def test_autolinks_and_html_are_not_placeholders(tmp_path: Path) -> None:
    text = GOOD.replace(
        "Approvals are smoother.",
        'See <https://example.com> and <a href="https://x.y">this</a>.',
    )
    assert ra.check("1.3.0", _repo(tmp_path, "1.3.0", text)) == []


def test_breaking_or_deprecation_requires_heads_up(tmp_path: Path) -> None:
    text = GOOD.replace("1.3.0", "1.2.0")
    problems = ra.check("1.2.0", _repo(tmp_path, "1.2.0", text))
    assert any("Heads up" in p for p in problems)

    deprecating = CHANGELOG.replace("- new thing", "- Gemini CLI is deprecated")
    problems = ra.check("1.3.0", _repo(tmp_path, "1.3.0", GOOD, deprecating))
    assert any("Heads up" in p for p in problems)

    with_heads_up = (
        GOOD + "\n## ⚠️ Heads up\n- Gemini CLI still ships but is unsupported.\n"
    )
    assert ra.check("1.3.0", _repo(tmp_path, "1.3.0", with_heads_up, deprecating)) == []


def test_release_type_against_previous_stable() -> None:
    assert ra.release_type("1.3.0", CHANGELOG) == "minor"
    assert ra.release_type("1.2.1", CHANGELOG) == "patch"
    assert ra.release_type("2.0.0", CHANGELOG) == "major"
    assert ra.release_type("0.1.0", "") == "minor"


def test_word_limits_by_release_type(tmp_path: Path) -> None:
    short = GOOD.replace(WORDS, "")
    problems = ra.check("1.3.0", _repo(tmp_path, "1.3.0", short))
    assert any("minor release post should be" in p for p in problems)


def test_render_appends_links_footer(tmp_path: Path) -> None:
    root = _repo(tmp_path, "1.3.0", GOOD)
    title, body = ra.render("1.3.0", root)
    assert title == "Untether 1.3.0 — smoother approvals"
    assert "title:" not in body
    assert "CHANGELOG.md#v130-2026-10-10" in body
    assert "/releases/tag/v1.3.0" in body
    assert "pypi.org/project/untether/1.3.0/" in body
    assert "/discussions/categories/q-a" in body
    assert "(https://untether.cc)" in body


class FakeGitHub:
    def __init__(self, discussions=(), release=None) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.discussions = list(discussions)
        self.release = release

    def __call__(self, method: str, path: str, payload: dict | None) -> dict:
        self.calls.append((method, path, payload))
        if path == "/graphql" and "createDiscussion" in payload["query"]:
            url = "https://github.com/o/r/discussions/9"
            self.discussions.append(
                {"title": payload["variables"]["title"], "url": url}
            )
            return {"data": {"createDiscussion": {"discussion": {"url": url}}}}
        if path == "/graphql":
            return {
                "data": {
                    "repository": {
                        "id": "R1",
                        "discussionCategories": {
                            "nodes": [
                                {"id": "C0", "slug": "general"},
                                {"id": "C1", "slug": "announcements"},
                            ]
                        },
                        "discussions": {"nodes": self.discussions},
                    }
                }
            }
        if method == "GET":
            return self.release or {"_status": 404}
        return {}


def test_post_creates_discussion_and_links_release(tmp_path: Path) -> None:
    gh = FakeGitHub(release={"id": 7, "body": "auto notes"})
    url = ra.post(
        "1.3.0", gh, repo="o/r", root=_repo(tmp_path, "1.3.0", GOOD), log=lambda _: None
    )
    assert url == "https://github.com/o/r/discussions/9"
    create = next(
        p for m, path, p in gh.calls if p and "createDiscussion" in p["query"]
    )
    assert create["variables"]["cat"] == "C1"
    patch = next(p for m, path, p in gh.calls if m == "PATCH")
    assert patch["body"].endswith(f"💬 **Discuss this release:** {url}\n")


def test_post_is_idempotent(tmp_path: Path) -> None:
    root = _repo(tmp_path, "1.3.0", GOOD)
    url = "https://github.com/o/r/discussions/9"
    gh = FakeGitHub(
        discussions=[{"title": "Untether 1.3.0 — smoother approvals", "url": url}],
        release={"id": 7, "body": f"notes\n\n💬 **Discuss this release:** {url}\n"},
    )
    assert ra.post("1.3.0", gh, repo="o/r", root=root, log=lambda _: None) == url
    assert not any(
        p and "createDiscussion" in p["query"]
        for _, _, p in gh.calls
        if p and "query" in p
    )
    assert not any(m == "PATCH" for m, _, _ in gh.calls)


def test_post_dry_run_writes_nothing(tmp_path: Path) -> None:
    gh = FakeGitHub(release={"id": 7, "body": ""})
    root = _repo(tmp_path, "1.3.0", GOOD)
    assert (
        ra.post("1.3.0", gh, repo="o/r", root=root, dry_run=True, log=lambda _: None)
        is None
    )
    assert all(
        m == "POST" and "createDiscussion" not in p["query"] for m, _, p in gh.calls
    )


def test_post_refuses_an_unpublishable_post(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="not publishable"):
        ra.post("1.3.0", FakeGitHub(), repo="o/r", root=_repo(tmp_path, "1.3.0", None))


def test_cli_skips_prereleases(capsys: pytest.CaptureFixture[str]) -> None:
    assert ra.main(["check", "0.36.0rc3"]) == 0
    assert "pre-release" in capsys.readouterr().out


def test_template_is_not_mistaken_for_a_release() -> None:
    template = REPO_ROOT / ".github" / "release-announcements" / "TEMPLATE.md"
    assert template.is_file()
    assert "title:" in template.read_text(encoding="utf-8")


def test_shipped_announcements_are_publishable() -> None:
    """Every committed vX.Y.Z.md must pass the same check the release PR runs."""
    for path in sorted((REPO_ROOT / ".github" / "release-announcements").glob("v*.md")):
        version = path.stem.removeprefix("v")
        assert ra.check(version, REPO_ROOT) == [], path.name
