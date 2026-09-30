"""/planmode wording and the shared permission-mode text table (#747).

The strings asserted here are the user-facing contract: `off` (acceptEdits)
asks before most tools since #749, `on` is a plan checkpoint rather than
per-action approval, and `auto` has no plan phase.  The coupling test ties
the text to ``is_claude_prompting_mode()`` so a reclassification fails CI
until the wording follows.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.telegram_fakes import _empty_projects, _make_router
from tests.test_command_engine_gates import FakeCommandContext
from untether.runners.mock import Return, ScriptRunner
from untether.runners.run_options import (
    VALID_PERMISSION_MODES_BY_ENGINE,
    is_claude_prompting_mode,
)
from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
from untether.telegram.commands._permission_mode_text import (
    CLAUDE_MODE_TEXT,
    PROMPTING_MARKER,
    mode_display,
)
from untether.telegram.commands.planmode import (
    PERMISSION_MODES,
    PLANMODE_USAGE,
    PlanModeCommand,
)
from untether.telegram.engine_overrides import EngineOverrides

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CLAUDE_MODES = sorted(VALID_PERMISSION_MODES_BY_ENGINE["claude"])
_CHAT = 100


async def _run(tmp_path: Path, args: str) -> str:
    ctx = FakeCommandContext(args_text=args, config_path=tmp_path / "untether.toml")
    result = await PlanModeCommand().handle(ctx)  # type: ignore[arg-type]
    assert result is not None
    return result.text


async def _stored_mode(tmp_path: Path) -> str | None:
    prefs = ChatPrefsStore(resolve_prefs_path(tmp_path / "untether.toml"))
    override = await prefs.get_engine_override(_CHAT, "claude")
    return override.permission_mode if override else None


async def _store_mode(tmp_path: Path, mode: str) -> None:
    prefs = ChatPrefsStore(resolve_prefs_path(tmp_path / "untether.toml"))
    await prefs.set_engine_override(
        _CHAT, "claude", EngineOverrides(permission_mode=mode)
    )


# ---------------------------------------------------------------------------
# Shared text table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", _CLAUDE_MODES)
def test_every_claude_mode_has_text(mode: str) -> None:
    """Chains to the #741 drift test: a new CLI mode fails here until described."""
    text = CLAUDE_MODE_TEXT[mode]
    for field in (text.ui_name, text.summary, text.hint):
        assert field
        assert not set(field) & {"<", ">", "&"}


@pytest.mark.parametrize("mode", _CLAUDE_MODES)
def test_prompting_marker_matches_classification(mode: str) -> None:
    """#749: a prompting mode says it asks; an autonomous one doesn't."""
    assert (PROMPTING_MARKER in CLAUDE_MODE_TEXT[mode].summary) == (
        is_claude_prompting_mode(mode)
    )


_FORBIDDEN = (
    "freely",
    "no approval needed",
    "fully autonomous",
    "approve actions",
    "every tool call",
    "agent decides",
)


def test_forbidden_phrases_absent() -> None:
    for text in CLAUDE_MODE_TEXT.values():
        for phrase in _FORBIDDEN:
            assert phrase not in text.summary.lower()
            assert phrase not in text.hint.lower()


def test_only_accept_edits_is_off() -> None:
    offs = [m for m in _CLAUDE_MODES if mode_display(m)[0] == "off"]
    assert offs == ["acceptEdits"]
    assert mode_display("default")[0] == "manual"
    assert mode_display("bogus") == ("bogus", None)


# ---------------------------------------------------------------------------
# /planmode replies
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("arg", "cli"),
    [("on", "plan"), ("plan-auto", "plan"), ("auto", "auto"), ("off", "acceptEdits")],
)
async def test_set_reply_per_mode(tmp_path: Path, arg: str, cli: str) -> None:
    text = await _run(tmp_path, arg)
    assert text.startswith(f"permission mode <b>{arg}</b>")
    assert f"--permission-mode {cli}" in text
    assert "next message" in text
    assert "old mode" in text
    assert "new sessions" not in text
    assert await _stored_mode(tmp_path) == PERMISSION_MODES[arg]


@pytest.mark.anyio
async def test_set_reply_auto_has_no_plan_framing(tmp_path: Path) -> None:
    text = await _run(tmp_path, "auto")
    assert "plan mode" not in text
    assert "classifier" in text
    assert "no plan phase" in text
    assert "falls back" in text


@pytest.mark.anyio
async def test_off_reply_describes_asking(tmp_path: Path) -> None:
    text = await _run(tmp_path, "off")
    assert "acceptEdits" in text
    assert "ask you here first" in text
    assert "allow rules" in text
    assert "freely" not in text


@pytest.mark.anyio
async def test_on_reply_wording(tmp_path: Path) -> None:
    text = await _run(tmp_path, "on")
    assert "without editing files" in text
    assert "approve the plan" in text
    assert "read-only" not in text
    assert "every" not in text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("arg", "ui", "suffix"),
    [
        ("on", "on", " (plan)"),
        ("plan-auto", "plan-auto", " (plan)"),
        ("off", "off", " (acceptEdits)"),
        ("auto", "auto", ""),
    ],
)
async def test_show_after_set(tmp_path: Path, arg: str, ui: str, suffix: str) -> None:
    await _run(tmp_path, arg)
    text = await _run(tmp_path, "show")
    assert text.startswith(f"permission mode: <b>{ui}</b>{suffix}: ")
    if not suffix:
        assert "(" not in text.split(":", 2)[1]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("stored", "ui"),
    [
        ("default", "manual"),
        ("manual", "manual"),
        ("dontAsk", "dontAsk"),
        ("bypassPermissions", "bypassPermissions"),
    ],
)
async def test_show_hand_stored_modes_not_off(
    tmp_path: Path, stored: str, ui: str
) -> None:
    await _store_mode(tmp_path, stored)
    text = await _run(tmp_path, "show")
    assert "<b>off</b>" not in text
    assert f"<b>{ui}</b>" in text
    assert "engine default" not in text
    if stored == "bypassPermissions":
        assert "⚠️" in text


@pytest.mark.anyio
async def test_show_no_override(tmp_path: Path) -> None:
    text = await _run(tmp_path, "show")
    assert "engine default" in text
    assert "no override" in text
    assert "engine config" in text
    assert "no approval buttons" in text


@pytest.mark.anyio
async def test_clear_reply(tmp_path: Path) -> None:
    await _run(tmp_path, "on")
    text = await _run(tmp_path, "clear")
    assert "override cleared" in text
    assert "engine config" in text
    assert await _stored_mode(tmp_path) is None


@pytest.mark.anyio
async def test_bare_toggle_unchanged(tmp_path: Path) -> None:
    await _run(tmp_path, "")
    assert await _stored_mode(tmp_path) == "plan"
    await _run(tmp_path, "")
    assert await _stored_mode(tmp_path) == "acceptEdits"


@pytest.mark.anyio
async def test_usage_unchanged(tmp_path: Path) -> None:
    assert await _run(tmp_path, "bogus") == PLANMODE_USAGE


@pytest.mark.anyio
async def test_no_config_path_framing() -> None:
    result = await PlanModeCommand().handle(FakeCommandContext(args_text="on"))  # type: ignore[arg-type]
    assert result is not None
    assert result.text.startswith("permission mode overrides unavailable")


def test_description() -> None:
    from untether.telegram.commands.menu import build_bot_commands
    from untether.transport_runtime import TransportRuntime

    description = PlanModeCommand.description
    assert "permission mode" in description
    assert "plan-auto" in description
    assert "plan mode" not in description
    assert len(description) <= 256
    runtime = TransportRuntime(
        router=_make_router(ScriptRunner([Return(answer="ok")], engine="claude")),
        projects=_empty_projects(),
    )
    commands = build_bot_commands(runtime)
    assert {"command": "planmode", "description": description} in commands


# ---------------------------------------------------------------------------
# Docs guard
# ---------------------------------------------------------------------------

_DOC_FILES = (
    "docs/how-to/plan-mode.md",
    "docs/tutorials/interactive-control.md",
    "docs/faq/faq.md",
    "docs/reference/glossary.md",
    "docs/how-to/inline-settings.md",
    "README.md",
    "docs/reference/runners/claude/runner.md",
)

_OFF_MARKERS = re.compile(r"acceptEdits|Accept edits|/planmode off|\*\*off\*\*")
_ON_MARKERS = re.compile(r"Plan|/planmode on")


@pytest.mark.parametrize("rel", _DOC_FILES)
def test_docs_have_no_stale_mode_claims(rel: str) -> None:
    lines = (_REPO_ROOT / rel).read_text(encoding="utf-8").splitlines()
    for n, line in enumerate(lines, 1):
        where = f"{rel}:{n}: {line}"
        assert not ("freely" in line and _OFF_MARKERS.search(line)), where
        assert not ("every tool call" in line and _ON_MARKERS.search(line)), where
        assert "approve actions" not in line, where
        assert "agent decides" not in line, where
