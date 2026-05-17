from __future__ import annotations

from collections.abc import Container


def is_cancel_command(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    command = stripped.split(maxsplit=1)[0]
    return command == "/cancel" or command.startswith("/cancel@")


def _parse_slash_command(text: str) -> tuple[str | None, str]:
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return None, text
    lines = stripped.splitlines()
    if not lines:
        return None, text
    first_line = lines[0]
    token, _, rest = first_line.partition(" ")
    command = token[1:]
    if not command:
        return None, text
    if "@" in command:
        command = command.split("@", 1)[0]
    args_text = rest
    if len(lines) > 1:
        tail = "\n".join(lines[1:])
        args_text = f"{args_text}\n{tail}" if args_text else tail
    return command.lower(), args_text


# #523: `.new` and similar leading-dot typos used to dispatch a full agent
# subprocess (full OAuth handshake, preamble, MCP catalog probe) before the
# user could cancel — wasting a non-trivial per-run cold-start cost. `.` and
# `/` are adjacent on iOS/Android punctuation rows, and several keyboards
# auto-replace a leading `/` with `.`.
def parse_dot_typo(text: str, known_commands: Container[str]) -> str | None:
    """Return the registered command name if ``text`` looks like a typo of
    ``/<cmd>`` (i.e. begins with ``.<cmd>`` with no whitespace before the
    command and ``<cmd>`` is a known slash command). Else ``None``.

    Only fires on simple shapes ``.cmd`` or ``.cmd args``. Multi-line and
    sentence-shaped inputs are left alone (they'd usually be real prose
    that happens to start with a dot, e.g. ``..wait, what?``).
    """
    if not text:
        return None
    stripped = text.lstrip()
    if not stripped.startswith("."):
        return None
    if stripped.startswith(("..", "./")):
        # Multi-dot ellipsis or a literal path; not a command typo.
        return None
    first_line = stripped.splitlines()[0]
    token, _, _ = first_line.partition(" ")
    cmd = token[1:].lower()
    if not cmd or not cmd.isidentifier():
        return None
    if cmd in known_commands:
        return cmd
    return None
