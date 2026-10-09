"""Antigravity (agy) permission modes as Telegram shows them (#558, D21).

Single source of truth for the labels, hints and button order used by
``/config`` (home + permission page), ``/planmode`` and the runner's footer
label (``runners/antigravity.py`` imports it lazily). Mirrors
``_permission_mode_text.py`` for Claude.

Honesty rules (REVIEW B2): Workspace is never called read-only or a sandbox;
"Ask me" and "Plan first" need Untether's approval gate, so rc1 shows them as
``· soon`` buttons that only answer a toast.
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
    available: bool  # False → greyed until the gate ships (rc3)


# Keyed by the canonical (effective) value. ``None`` and unknown stored
# values are shown as Workspace: the runner fails closed to it.
ANTIGRAVITY_MODE_TEXT: dict[str, AgyModeText] = {
    "workspace": AgyModeText(
        label="workspace",
        button="Workspace",
        hint="edits files, no shell — not a sandbox",
        available=True,
    ),
    "ask": AgyModeText(
        label="ask me",
        button="Ask me",
        hint="asks before shell, web, MCP",
        available=False,
    ),
    "plan": AgyModeText(
        label="plan first",
        button="Plan first",
        hint="plans, waits for your OK",
        available=False,
    ),
    "full": AgyModeText(
        label="full access",
        button="Full access",
        hint="all tools approved",
        available=True,
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

GATE_SOON_SUFFIX = " · soon"
GATE_SOON_TOAST = (
    "Needs Untether's Telegram approval gate — coming in a later 0.36.1 release"
)


def canonical_mode(value: str | None) -> str:
    """A stored value → the mode agy actually runs in (fail closed)."""
    if value in ANTIGRAVITY_PERMISSION_MODES:
        return value  # type: ignore[return-value]
    return "workspace"


def antigravity_mode_label(value: str | None) -> str:
    """``None`` / unknown → ``workspace``; ``full`` → ``full access`` …"""
    return ANTIGRAVITY_MODE_TEXT[canonical_mode(value)].label


def button_text(action: str) -> str:
    mode = ANTIGRAVITY_MODE_TEXT[ANTIGRAVITY_PM_ACTIONS[action]]
    return mode.button if mode.available else f"{mode.button}{GATE_SOON_SUFFIX}"


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
