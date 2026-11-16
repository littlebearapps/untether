"""Callback data of the buttons that answer a control request (#960).

Claude's buttons carry ``claude_control:<action>:<request_id>`` (approvals and
plan buttons) and ``aq:`` (AskUserQuestion options); those strings never
change. ``ia:`` is the engine-neutral approval prefix reserved for engines
that register a request with their own responder (Antigravity's hook gate,
#980); its command backend arrives with the first such responder.
"""

from __future__ import annotations

from .registry import callback_prefix

# #684: callback-data prefixes of buttons that can answer a control request
# (approval / plan buttons, question options).
CONTROL_CALLBACK_PREFIXES: tuple[str, ...] = ("claude_control:", "aq:", "ia:")

# Command ids whose ``<action>:<request_id>`` callbacks answer an approval
# directly by request id (the #388 originator check reads the id from them).
APPROVAL_COMMAND_IDS: frozenset[str] = frozenset({"claude_control", "ia"})


def approval_keyboard(request_id: str) -> list[list[dict[str, str]]]:
    """Approve / Deny for *request_id*, under the prefix of the engine that
    registered it (``claude_control`` for Claude)."""
    prefix = callback_prefix(request_id)
    return [
        [
            {
                "text": "✅ Approve",
                "callback_data": f"{prefix}:approve:{request_id}",
            },
            {"text": "❌ Deny", "callback_data": f"{prefix}:deny:{request_id}"},
        ]
    ]
