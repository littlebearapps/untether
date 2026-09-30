"""Every `/config` help link maps to one synced doc file and heading (#296).

The help centre (littlebearapps.com ``scripts/docs-sync.config.ts``) publishes
every top-level page of docs/{tutorials,how-to,reference,explanation,faq}
flat at ``/help/untether/<file-stem>/``, synced from ``master``, and uses
GitHub-slugger heading ids.  It does NOT honour Zensical ``{#id}`` attr_list
ids (checked live 2026-09-30), so such headings are rejected as targets.

Run ``uv run python -m tests.test_config_help_links`` to print every URL the
pages render (R15-12-1 curls that list).
"""

from __future__ import annotations

import re
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

from untether.telegram.commands import config as config_mod
from untether.telegram.commands.config import _HELP_BASE, _PAGES, ConfigCommand

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SYNCED_DIRS = ("tutorials", "how-to", "reference", "explanation", "faq")
_SLUG_RE = re.compile(r"^[a-z0-9-]+$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_ENGINES = ("claude", "codex", "opencode", "gemini")
_CHAT = 123

_REPO_URL = "https://github.com/littlebearapps/untether"
ALLOWED_EXTERNAL_HREFS = frozenset(
    {
        _HELP_BASE,  # help-centre index (home footer "Help guides")
        _REPO_URL,
        f"{_REPO_URL}/issues/new?template=bug_report.yml",
        f"{_REPO_URL}/issues/new?template=feature_request.yml",
    }
)


# ---------------------------------------------------------------------------
# Help-centre model
# ---------------------------------------------------------------------------


def _help_doc_index(root: Path = _REPO_ROOT) -> dict[str, list[Path]]:
    """Stem → files, for every doc the sync publishes (top level only).

    Nested reference files are renamed by the sync, so they're not
    addressable by stem; ``index.md`` is skipped (docs-sync ``skip``).
    """
    index: dict[str, list[Path]] = {}
    for sub in _SYNCED_DIRS:
        for path in sorted((root / "docs" / sub).glob("*.md")):
            if path.name == "index.md":
                continue
            index.setdefault(path.stem, []).append(path)
    return index


def _slugify(text: str) -> str:
    """GitHub-slugger: lowercase, drop punctuation, spaces → hyphens."""
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _heading_anchors(path: Path) -> set[str]:
    """Anchors a help-centre link may target in *path*.

    Headings with a ``{#id}`` attr_list are rejected (the help centre renders
    the attribute as text), and so is every variant of a duplicated slug.
    """
    counts: dict[str, int] = {}
    rejected: set[str] = set()
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        text = match.group(2)
        slug = _slugify(re.sub(r"\{#[^}]*\}", "", text))
        if "{#" in text:
            rejected.add(slug)
            continue
        counts[slug] = counts.get(slug, 0) + 1
    return {slug for slug, n in counts.items() if n == 1 and slug not in rejected}


def _resolve(url: str, root: Path = _REPO_ROOT) -> Path | None:
    """The one doc file a help URL lands on, or None when it would 404."""
    if not url.startswith(_HELP_BASE):
        return None
    rest = url[len(_HELP_BASE) :]
    path_part, _, anchor = rest.partition("#")
    if not path_part.endswith("/"):
        return None
    slug = path_part[:-1]
    if not _SLUG_RE.match(slug):
        return None
    files = _help_doc_index(root).get(slug, [])
    if len(files) != 1:
        return None
    if anchor and anchor not in _heading_anchors(files[0]):
        return None
    return files[0]


# ---------------------------------------------------------------------------
# Render driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rendered:
    page: str
    engine: str
    variant: str
    text: str
    keyboard: list[list[dict[str, str]]]

    @property
    def hrefs(self) -> list[str]:
        return re.findall(r'href="([^"]+)"', self.text)

    @property
    def help_hrefs(self) -> set[str]:
        """The page's 📖 links (help-centre pages, excluding the index)."""
        return {h for h in self.hrefs if h.startswith(_HELP_BASE) and h != _HELP_BASE}

    @property
    def callbacks(self) -> list[str]:
        return [b["callback_data"] for row in self.keyboard for b in row]


def _trigger_variants() -> dict[str, Any]:
    from untether.triggers.manager import TriggerManager
    from untether.triggers.settings import parse_trigger_config

    one_cron = TriggerManager(
        parse_trigger_config(
            {
                "enabled": True,
                "crons": [
                    {
                        "id": "morning",
                        "schedule": "0 9 * * *",
                        "prompt": "x",
                        "chat_id": _CHAT,
                    }
                ],
            }
        )
    )
    return {"one_cron": one_cron, "empty": TriggerManager(), "none": None}


def _ctx(
    args_text: str, text: str, engine: str, config_path: Path, mgr: Any
) -> MagicMock:
    ctx = MagicMock()
    ctx.args_text = args_text
    ctx.text = text
    ctx.message.channel_id = _CHAT
    ctx.config_path = config_path
    ctx.runtime.engine_ids = _ENGINES
    ctx.runtime.default_engine = engine
    ctx.runtime.default_context_for_chat.return_value = None
    ctx.runtime.project_default_engine.return_value = None
    ctx.executor = AsyncMock()
    ctx.executor.send = AsyncMock(return_value=None)
    ctx.executor.edit = AsyncMock(return_value=None)
    ctx.trigger_manager = mgr
    ctx.default_chat_id = None
    return ctx


def _message(ctx: MagicMock) -> Any:
    if ctx.executor.edit.call_args is not None:
        return ctx.executor.edit.call_args[0][1]
    return ctx.executor.send.call_args[0][0]


async def _render_all(tmp_dir: Path) -> list[Rendered]:
    out: list[Rendered] = []
    config_path = tmp_dir / "prefs.json"
    with patch("untether.telegram.backend._detect_cli_version", return_value=None):
        for variant, mgr in _trigger_variants().items():
            for engine in _ENGINES:
                targets = [("home", "", "/config"), ("home", "home", "config:home")]
                targets += [(key, key, f"config:{key}") for key in _PAGES]
                for page, args_text, text in targets:
                    ctx = _ctx(args_text, text, engine, config_path, mgr)
                    await ConfigCommand().handle(ctx)
                    msg = _message(ctx)
                    keyboard = msg.extra.get("reply_markup", {}).get(
                        "inline_keyboard", []
                    )
                    out.append(Rendered(page, engine, variant, msg.text, keyboard))
    return out


@pytest.fixture
def rendered(tmp_path: Path) -> list[Rendered]:
    return anyio.run(_render_all, tmp_path)


# ---------------------------------------------------------------------------
# Expected 📖 links per (page, engine) — also the R15-12-1 reference table.
# Every (page, engine) the driver renders must be classified here.
# ---------------------------------------------------------------------------


def _u(slug: str, anchor: str | None = None) -> str:
    return f"{_HELP_BASE}{slug}/" + (f"#{anchor}" if anchor else "")


_ENGINE_LINKS = frozenset({_u("switch-engines"), _u("model-reasoning")})
_NONE: frozenset[str] = frozenset()


def _expected() -> dict[tuple[str, str], frozenset[str]]:
    table: dict[tuple[str, str], frozenset[str]] = {}
    for engine in _ENGINES:
        table[("home", engine)] = _NONE
        table[("ab", engine)] = _NONE
        table[("vb", engine)] = frozenset({_u("verbose-progress")})
        table[("ag", engine)] = _ENGINE_LINKS
        table[("md", engine)] = _ENGINE_LINKS
        table[("tr", engine)] = frozenset(
            {_u("group-chat", "set-listen-mode-for-groups")}
        )
        table[("rl", engine)] = frozenset(
            {_u("conversation-modes", "resume-lines-in-chat-mode")}
        )
        table[("tg", engine)] = frozenset({_u("webhooks-and-cron")})
        table[("fu", engine)] = frozenset({_u("steer-follow-ups")})
        # Claude-only pages render an "only available for …" notice elsewhere.
        table[("aq", engine)] = _NONE
        table[("dp", engine)] = _NONE
        table[("loop", engine)] = _NONE
        table[("rs", engine)] = _NONE
        table[("cu", engine)] = _NONE
        table[("pm", engine)] = _NONE
    table[("pm", "claude")] = frozenset({_u("plan-mode")})
    table[("pm", "codex")] = frozenset(
        {_u("interactive-approval", "codex-cli--approval-policy")}
    )
    table[("pm", "gemini")] = frozenset(
        {_u("interactive-approval", "gemini-cli--approval-mode")}
    )
    table[("aq", "claude")] = frozenset(
        {_u("interactive-approval", "answering-questions")}
    )
    table[("dp", "claude")] = frozenset({_u("interactive-approval", "diff-previews")})
    table[("loop", "claude")] = frozenset({_u("schedule-tasks", "loop-mode")})
    for engine in ("claude", "codex"):
        table[("rs", engine)] = frozenset(
            {_u("model-reasoning", "set-reasoning-level")}
        )
    for engine in ("claude", "opencode", "gemini"):
        table[("cu", engine)] = frozenset({_u("cost-budgets")})
    return table


EXPECTED_HELP_URLS = _expected()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_every_help_link_resolves(rendered: list[Rendered]) -> None:
    for r in rendered:
        for href in r.help_hrefs:
            assert _resolve(href) is not None, (r.page, r.engine, r.variant, href)


def test_help_urls_exact(rendered: list[Rendered]) -> None:
    seen: set[tuple[str, str]] = set()
    for r in rendered:
        key = (r.page, r.engine)
        seen.add(key)
        assert key in EXPECTED_HELP_URLS, f"unclassified page {key}"
        assert r.help_hrefs == EXPECTED_HELP_URLS[key], (key, r.variant)
    assert seen == set(EXPECTED_HELP_URLS)


def test_no_unexpected_hrefs(rendered: list[Rendered]) -> None:
    for r in rendered:
        for href in r.hrefs:
            ok = href.startswith(_HELP_BASE) or href in ALLOWED_EXTERNAL_HREFS
            assert ok, (r.page, r.engine, href)


def test_every_callback_targets_a_real_page(rendered: list[Rendered]) -> None:
    """Unknown page keys fall back to home silently, so catch them here."""
    for r in rendered:
        for cb in r.callbacks:
            assert cb.startswith("config:"), (r.page, cb)
            key = cb.split(":")[1]
            assert key == "home" or key in _PAGES, (r.page, r.engine, cb)


def test_all_config_callback_data_within_64_bytes(rendered: list[Rendered]) -> None:
    for r in rendered:
        for cb in r.callbacks:
            assert len(cb.encode()) <= 64, cb


def test_triggers_button_reachable_from_home(rendered: list[Rendered]) -> None:
    homes = [r for r in rendered if r.page == "home"]
    for r in homes:
        assert ("config:tg" in r.callbacks) == (r.variant != "none"), (
            r.engine,
            r.variant,
        )


def test_source_learn_more_calls_resolve() -> None:
    """Covers branches the driver doesn't reach."""
    source = Path(config_mod.__file__).read_text(encoding="utf-8")
    calls = re.findall(
        r'_(?:learn_more|help_url)\(\s*[\'"]([^\'"]+)[\'"]'
        r'(?:\s*,\s*[\'"]([^\'"]+)[\'"])?',
        source,
    )
    assert calls
    for slug, anchor in calls:
        url = f"{_HELP_BASE}{slug}/" + (f"#{anchor}" if anchor else "")
        assert _resolve(url) is not None, url


def test_no_dead_docs_bases_in_src() -> None:
    for path in (_REPO_ROOT / "src" / "untether").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "tools/untether/how-to" not in text, path
        assert "untether.littlebearapps.com" not in text, path


def test_resolver_rejects_bad_targets(tmp_path: Path) -> None:
    assert _resolve(_u("plan-mode")) is not None
    assert _resolve(f"{_HELP_BASE}how-to/conversation-modes/") is None
    assert _resolve(_u("inline-settings", "answering-questions")) is None
    assert _resolve(_u("inline-settings", "triggers-page")) is None
    assert _resolve(_u("does-not-exist")) is None
    assert _resolve(_u("runner")) is None
    assert _resolve(f"{_HELP_BASE}plan-mode") is None

    how_to = tmp_path / "docs" / "how-to"
    how_to.mkdir(parents=True)
    (how_to / "dup.md").write_text("# Dup\n\n## Usage\n\n## Usage\n\n## Other\n")
    assert _resolve(_u("dup", "other"), root=tmp_path) is not None
    assert _resolve(_u("dup", "usage"), root=tmp_path) is None
    assert _resolve(_u("dup", "usage-1"), root=tmp_path) is None


def test_slugify_matches_help_centre() -> None:
    assert _slugify("Codex CLI — Approval policy") == "codex-cli--approval-policy"
    assert _slugify("Resume lines in chat mode") == "resume-lines-in-chat-mode"


def _satellite_trigger_files() -> Iterator[Path]:
    for sub in _SYNCED_DIRS:
        yield from sorted((_REPO_ROOT / "docs" / sub).glob("*.md"))
    yield _REPO_ROOT / "README.md"
    yield _REPO_ROOT / "CLAUDE.md"
    yield Path(config_mod.__file__)


def test_no_satellite_triggers_in_docs() -> None:
    """📡 means Listen only; Triggers are ⏰ (history files excluded)."""
    for path in _satellite_trigger_files():
        if path.name == "changelog.md":
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            assert "📡 Trigger" not in line, f"{path}:{n}: {line}"


def _all_urls(rendered: list[Rendered]) -> list[str]:
    return sorted({h for r in rendered for h in r.hrefs})


def test_dump_rendered_help_urls(rendered: list[Rendered]) -> None:
    urls = _all_urls(rendered)
    assert urls
    assert _HELP_BASE in urls


if __name__ == "__main__":  # R15-12-1: print every rendered URL for curl
    import structlog

    # Keep stdout to URLs only (structlog prints to stdout by default).
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))
    with tempfile.TemporaryDirectory() as tmp:
        for url in _all_urls(anyio.run(_render_all, Path(tmp))):
            sys.stdout.write(url + "\n")
