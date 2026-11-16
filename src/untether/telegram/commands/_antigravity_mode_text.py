"""Antigravity (agy) permission modes as Telegram shows them (#558, D21).

Single source of truth for the labels, hints and button order used by
``/config`` (home + permission page), ``/planmode`` and the runner's footer
label (``runners/antigravity.py`` imports it lazily). Mirrors
``_permission_mode_text.py`` for Claude.

Honesty rules (REVIEW B2): Workspace is never called read-only or a sandbox.
"Ask me" (#980) needs Untether's Telegram approvals on the host; until they
are installed its button reads ``🔒 Ask me — set up`` and opens the status
page instead of storing anything. "Plan first" is agy's own plan mode (#981).

agy keeps a conversation on the agent it started with (D38), and the modes
use two: Workspace and Plan first run Untether's no-shell agent, Ask me and
Full access agy's default one. Moving between those two groups needs /new
(:func:`needs_new`); moving within a group doesn't.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ...runners.run_options import ANTIGRAVITY_PERMISSION_MODES

if TYPE_CHECKING:
    from ...transport_runtime import TransportRuntime
    from ..chat_prefs import ChatPrefsStore

ENGINE = "antigravity"

ModeSource = Literal["chat", "untether.toml", "default"]


@dataclass(frozen=True, slots=True)
class AgyModeText:
    """How one canonical agy mode is named and described."""

    label: str  # footer / home label (lower case)
    button: str  # /config button text
    hint: str  # home-page micro description


# Keyed by the canonical (effective) value. ``None`` and unknown stored
# values are shown as Workspace: the runner fails closed to it.
ANTIGRAVITY_MODE_TEXT: dict[str, AgyModeText] = {
    "workspace": AgyModeText(
        label="workspace",
        button="Workspace",
        hint="edits files, no shell — not a sandbox",
    ),
    "ask": AgyModeText(
        label="ask me",
        button="Ask me",
        hint="asks before shell, web, MCP",
    ),
    "plan": AgyModeText(
        label="plan first",
        button="Plan first",
        hint="plans, waits for your OK",
    ),
    "full": AgyModeText(
        label="full access",
        button="Full access",
        hint="all tools approved",
    ),
}

# /config callback actions (``config:pm:<action>``). Prefixed ``ag`` so they
# can't collide with Claude ``on/pa/auto/off``, Codex ``fa/safe`` or Gemini
# ``ya/ae/ro``.
ANTIGRAVITY_PM_ACTIONS: dict[str, str] = {
    "agw": "workspace",
    "agf": "full",
    "aga": "ask",
    "agp": "plan",
}
# Button rows, in order.
BUTTON_ROWS: tuple[tuple[str, ...], ...] = (("agw", "agf"), ("aga", "agp"))

# Modes whose conversations start on Untether's no-shell agent (D38); the
# others (Ask me, Full access) start on agy's default agent.
NO_SHELL_AGENT_MODES = frozenset({"workspace", "plan"})

# #1037: agy keeps a conversation on the agent it started with, so a chat
# that began in Workspace has no shell until a new conversation starts.
# R3-3 (2026-10-12): the same goes for Ask me — tell the user, keep no state.
FULL_ACCESS_TOAST = (
    "Permission mode: full access — send /new if you're switching from"
    " Workspace or Plan first (not from Ask me)"
)
ASK_TOAST = (
    "Permission mode: ask me — send /new if you're switching from"
    " Workspace or Plan first (not from Full access)"
)
# #1060 / #981: one that began in Full access (or Ask me) keeps its shell,
# which Workspace then blocks and a plan turn reaches for. Workspace and Plan
# first share the no-shell agent, so moving between those two needs no /new.
WORKSPACE_TOAST = (
    "Permission mode: workspace — send /new if you're switching from Full"
    " access or Ask me (not from Plan first)"
)
PLAN_FIRST_TOAST = (
    "Permission mode: plan first — send /new if you're switching from Full"
    " access or Ask me (not from Workspace)"
)
# ``/planmode`` (#981) when its change crosses the two agent groups.
NEW_NEEDED_TEXT = "send /new to start using it"
# The agy page's own Clear override (``config:pm:agc``, #1060): the shared
# ``clr`` toast is engine-blind, and clearing can cross the agent groups
# when untether.toml sets a mode.
CLEARED_TOAST = (
    "Permission mode: cleared — send /new if this switched between Workspace /"
    " Plan first and Ask me / Full access"
)

# #980: Ask me with no Telegram approvals installed on the host.
ASK_SETUP_BUTTON = "🔒 Ask me — set up"


def needs_new(before: str | None, after: str | None) -> bool:
    """Whether a switch from *before* to *after* (canonical or stored
    values) only applies from the next new conversation: it crosses the
    no-shell agent group and agy's default-agent group."""
    return (canonical_mode(before) in NO_SHELL_AGENT_MODES) != (
        canonical_mode(after) in NO_SHELL_AGENT_MODES
    )


def canonical_mode(value: str | None) -> str:
    """A stored value → the mode agy actually runs in (fail closed)."""
    if value in ANTIGRAVITY_PERMISSION_MODES:
        return value  # type: ignore[return-value]
    return "workspace"


def antigravity_mode_label(value: str | None) -> str:
    """``None`` / unknown → ``workspace``; ``full`` → ``full access`` …"""
    return ANTIGRAVITY_MODE_TEXT[canonical_mode(value)].label


def button_text(action: str, *, gate_ready: bool = True) -> str:
    """The /config button. Ask me reads "🔒 Ask me — set up" while the host
    has no Telegram approvals (the button then opens their status page)."""
    mode_value = ANTIGRAVITY_PM_ACTIONS[action]
    if mode_value == "ask" and not gate_ready:
        return ASK_SETUP_BUTTON
    return ANTIGRAVITY_MODE_TEXT[mode_value].button


def source_suffix(source: ModeSource) -> str:
    if source == "untether.toml":
        return " (from untether.toml)"
    if source == "default":
        return " (default)"
    return ""


def _runner_default(runtime: TransportRuntime | None) -> str | None:
    if runtime is None:
        return None
    try:
        resolved = runtime.resolve_runner(resume_token=None, engine_override=ENGINE)
    except Exception:  # noqa: BLE001 — a label must never break /config
        return None
    value = getattr(resolved.runner, "default_permission_mode", None)
    return value if isinstance(value, str) and value else None


async def antigravity_effective_mode(
    prefs: ChatPrefsStore,
    chat_id: int,
    runtime: TransportRuntime | None,
) -> tuple[str | None, ModeSource]:
    """The stored mode a plain message in this chat would run with.

    Precedence (REVIEW M4): the chat's override, then
    ``[antigravity] permission_mode`` from untether.toml, then nothing
    (Workspace). The runner applies the same order, plus the unattended
    downgrade (08 §10) and the fail-closed mapping.
    """
    override = await prefs.get_engine_override(chat_id, ENGINE)
    if override is not None and override.permission_mode is not None:
        return override.permission_mode, "chat"
    toml_value = _runner_default(runtime)
    if toml_value is not None:
        return toml_value, "untether.toml"
    return None, "default"
