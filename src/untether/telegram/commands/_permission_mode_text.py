"""Shared descriptions of Claude Code permission modes (#747).

``/planmode`` and ``/config`` both render from this table so the two surfaces
can't drift apart again (after #741 they disagreed about ``auto``; after #749
``/config`` still called ``acceptEdits`` "run freely").

Every description of a *prompting* mode (``is_claude_prompting_mode()`` true)
contains :data:`PROMPTING_MARKER`; no autonomous one does.
``tests/test_planmode_command.py`` enforces that coupling, so reclassifying a
mode in ``runners/run_options.py`` fails CI until the text follows.

Strings are plain text with no HTML metacharacters; callers wrap them in
``<b>``/``<i>`` themselves.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...runners.run_options import CLAUDE_PLAN_AUTO_MODE, claude_cli_permission_mode

PROMPTING_MARKER = "ask you here first"


@dataclass(frozen=True, slots=True)
class ModeText:
    """How one stored permission mode is named and described in Telegram."""

    ui_name: str
    summary: str
    hint: str


_MANUAL = ModeText(
    ui_name="manual",
    summary=(
        "reads run; other tools ask you here first unless your Claude Code"
        " allow rules cover them"
    ),
    hint="reads run, rest asks",
)

# Keyed by the value stored in chat prefs / untether.toml.  Must cover every
# value in ``VALID_PERMISSION_MODES_BY_ENGINE["claude"]`` (tested).
CLAUDE_MODE_TEXT: dict[str, ModeText] = {
    "plan": ModeText(
        ui_name="on",
        summary=(
            "plan mode: Claude plans without editing files, and you approve"
            " the plan before changes start"
        ),
        hint="approve the plan first",
    ),
    CLAUDE_PLAN_AUTO_MODE: ModeText(
        ui_name="plan-auto",
        summary="plan mode, but the plan is approved for you: no plan buttons",
        hint="auto-approve plans",
    ),
    "auto": ModeText(
        ui_name="auto",
        summary=(
            "Claude Code's auto mode: a classifier approves routine actions and"
            " blocks risky ones; no plan phase. On a model without auto mode,"
            " Claude Code falls back and Untether asks you here instead"
        ),
        hint="classifier-gated",
    ),
    "acceptEdits": ModeText(
        ui_name="off",
        summary=(
            "no plan phase; file edits and common filesystem commands run,"
            " other tools ask you here first unless your Claude Code allow"
            " rules cover them"
        ),
        hint="edits run, others ask",
    ),
    # The CLI accepts ``default`` but advertises ``manual``; Claude Code's own
    # UIs call it Manual, so both display as ``manual``.
    "default": _MANUAL,
    "manual": _MANUAL,
    "dontAsk": ModeText(
        ui_name="dontAsk",
        summary=(
            "only pre-approved tools run; anything that would ask is denied,"
            " questions included"
        ),
        hint="pre-approved only",
    ),
    "bypassPermissions": ModeText(
        ui_name="bypassPermissions",
        summary="⚠️ every tool runs with no permission checks",
        hint="⚠️ no checks",
    ),
}

# The ``/config`` sub-page lists the modes it has buttons for, in button order.
BUTTON_MODES: tuple[str, ...] = ("acceptEdits", "plan", CLAUDE_PLAN_AUTO_MODE, "auto")

# No override stored for the chat.  Labelled "engine default" so it can't be
# confused with a stored CLI ``default`` (which displays as ``manual``).
NO_OVERRIDE_LABEL = "engine default"
NO_OVERRIDE_HINT = "from engine config"
NO_OVERRIDE_TEXT = (
    "uses the engine config (<code>[engines.claude] permission_mode</code> in"
    " untether.toml), or Claude Code's own settings with no approval buttons"
    " if that's unset"
)

# A mode change reaches the CLI on the next message: a live session whose
# options changed is closed and the message resumes a fresh process
# (``live_followup.py``, ``claude.live_session.options_changed``; pinned by
# ``test_live_session_injection.py::
# test_changed_chat_options_close_session_instead_of_injecting``).
APPLY_TIMING_TEXT = (
    "Until then, the current run and any background wake-ups keep the old mode."
)


def mode_display(mode: str) -> tuple[str, ModeText | None]:
    """Return ``(ui_name, text)`` for a stored mode, or ``(mode, None)``.

    Only ``acceptEdits`` is ever labelled ``off``.  An unknown string
    (validators should reject it) is shown raw with no description.
    """
    text = CLAUDE_MODE_TEXT.get(mode)
    if text is None:
        return mode, None
    return text.ui_name, text


def cli_name_suffix(mode: str) -> str:
    """``" (plan)"``-style suffix naming the CLI mode, or ``""``.

    Omitted when the CLI name equals the Telegram label (``auto``,
    ``dontAsk``…), so the parenthesis only appears when it adds information
    (#747 D2: ties the setting to the footer and to Anthropic's docs).
    """
    ui_name, _ = mode_display(mode)
    cli = claude_cli_permission_mode(mode)
    if cli is None or cli == ui_name:
        return ""
    return f" ({cli})"
