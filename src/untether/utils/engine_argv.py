"""Engine-neutral guard for user-influenced argv values (#1034).

A model id (``/model set``, ``[engine] model``, a cron) or a resume value (a
resume line typed in chat, a saved session) used to reach each engine CLI as
a separate token after its flag. Probed on the installed CLIs (2026-10-11):

| CLI | ``--model -x`` | resume ``-x`` | ``--flag=value`` |
|---|---|---|---|
| claude 2.1.296 | value | **flag** (``--resume --version`` prints the version) | accepted |
| codex 0.160.1 | clap error | **flag** (``exec resume <id>`` is positional) | accepted for ``--model`` |
| opencode 1.14.33 | **flag** | **flag** | accepted |
| pi 0.78.0 | value | value | **not parsed** (own parser) |
| gemini 0.40.1 | **flag** | **flag** | accepted |

So a resume line such as ``claude --resume --dangerously-skip-permissions``
could add a flag to a run. Two defences:

1. ``check_model`` / ``check_resume`` refuse a value that is empty, too long,
   starts with ``-`` or carries whitespace / control characters. Every runner
   calls them in ``build_args`` (raising ``EngineArgvError``), and
   ``JsonlSubprocessRunner`` turns the same check into one plain refusal
   before anything spawns. A runner narrows the resume check to what its CLI
   really accepts through ``check_resume_value``: Pi only takes its own
   session ids or ``.jsonl`` files under its sessions directory (#1064),
   Codex only a thread UUID (#1065).
2. ``joined`` emits ``--flag=value`` for the CLIs that parse it, so the value
   can never be re-read as a flag even if a check is loosened later.

Model ids stay free-form otherwise (``provider/model``, ``opus[1m]``,
``@cf/…``, ``name:tag``): the engine rejects unknown names itself.
Antigravity keeps its own stricter per-flag allow-list in
``utils/antigravity_argv.py``.
"""

from __future__ import annotations

from typing import Any

from .antigravity_argv import safe_preview

__all__ = [
    "INVALID_ARGUMENT_BLOCK",
    "INVALID_MODEL_TEXT",
    "INVALID_RESUME_TEXT",
    "MAX_MODEL_CHARS",
    "MAX_RESUME_CHARS",
    "EngineArgvError",
    "check_model",
    "check_resume",
    "joined",
    "model_problem",
    "safe_preview",
]

MAX_MODEL_CHARS = 256
# Pi resumes by session-file path, so this is a path-sized bound.
MAX_RESUME_CHARS = 4096

# ``usage[PRESPAWN_BLOCKED_KEY]`` value: the run was refused, the chat's saved
# session is kept (same key Antigravity uses).
INVALID_ARGUMENT_BLOCK = "invalid_argument"

INVALID_MODEL_TEXT = (
    "🛑 That model id isn't valid, so nothing was started. A model id can't "
    "start with a dash or contain spaces or control characters. Set one with "
    "/model set, or go back to the default with /model clear."
)
# #1069: the refusal keeps the chat's saved session (``PRESPAWN_BLOCKED_KEY``),
# so the text says so instead of pointing at /new.
INVALID_RESUME_TEXT = (
    "🛑 That session id isn't valid, so nothing was started. This chat's "
    "saved session isn't affected."
)

_REASON_TEXT = {
    "not_a_string": "must be text",
    "empty": "must not be empty",
    "too_long": "is too long",
    "leading_dash": "must not start with a dash",
    "whitespace_or_control": "must not contain spaces or control characters",
    "control": "must not contain control characters",
    # #1064 / #1065: the engine-specific narrowing (``Runner.check_resume_value``).
    "not_a_session": "is not one of the engine's session ids or session files",
}


class EngineArgvError(ValueError):
    """A value isn't safe to pass to an engine CLI. Never carries the raw
    value (it may be hostile, and it ends up in logs and chat)."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"invalid {field}: {_REASON_TEXT.get(reason, reason)}")
        self.field = field
        self.reason = reason


def _reason(value: Any, *, max_chars: int, allow_spaces: bool) -> str | None:
    if not isinstance(value, str):
        return "not_a_string"
    if not value:
        return "empty"
    if len(value) > max_chars:
        return "too_long"
    if value.startswith("-"):
        return "leading_dash"
    if allow_spaces:
        if any(not ch.isprintable() for ch in value):
            return "control"
    elif any(ch.isspace() or not ch.isprintable() for ch in value):
        return "whitespace_or_control"
    return None


def model_problem(value: Any) -> str | None:
    """Why *value* can't be a model id, as a short phrase for a message
    (``"must not start with a dash"``); None when it can."""
    reason = _reason(value, max_chars=MAX_MODEL_CHARS, allow_spaces=False)
    return None if reason is None else _REASON_TEXT[reason]


def check_model(value: Any) -> str:
    """*value* if it is a usable model id, else ``EngineArgvError``."""
    reason = _reason(value, max_chars=MAX_MODEL_CHARS, allow_spaces=False)
    if reason is not None:
        raise EngineArgvError("model", reason)
    return value


def check_resume(value: Any) -> str:
    """*value* if it is a usable session id / session path, else
    ``EngineArgvError``. Spaces are allowed (Pi resumes by file path, and
    argv is never shell-parsed); a leading dash and control characters are
    not."""
    reason = _reason(value, max_chars=MAX_RESUME_CHARS, allow_spaces=True)
    if reason is not None:
        raise EngineArgvError("resume", reason)
    return value


def joined(flag: str, value: str) -> str:
    """``--flag=value`` — one token, for CLIs that parse the joined form.
    *value* must already have been through ``check_model`` / ``check_resume``."""
    return f"{flag}={value}"
