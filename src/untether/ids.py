from __future__ import annotations

import re

ID_PATTERN = r"^[a-z0-9_]{1,32}$"
_ID_RE = re.compile(ID_PATTERN)

RESERVED_CLI_COMMANDS = frozenset({"doctor", "init", "plugins"})
RESERVED_CHAT_COMMANDS = frozenset(
    {
        "cancel",
        "continue",
        "file",
        "new",
        "agent",
        "model",
        "reasoning",
        "trigger",
        "topic",
        "ctx",
    }
)
RESERVED_ENGINE_IDS = (
    RESERVED_CLI_COMMANDS | RESERVED_CHAT_COMMANDS | frozenset({"config"})
)
RESERVED_COMMAND_IDS = RESERVED_CLI_COMMANDS | RESERVED_CHAT_COMMANDS

# Engines that still load and run but are no longer supported. Surfaced in
# `/config` and the docs; no removal is scheduled, but they may be removed in a
# future release (#722, #947). Deliberately a simple
# id set rather than an `EngineBackend.status` field; richer registry metadata
# may become an `EngineBackend.status` field later. The Antigravity engine
# (#558) ships as a separate, supported `antigravity` id and doesn't need it.
#
# NOT a blacklist: a third-party entry point supplying one of these ids is still
# honoured. This only drives presentation.
DEPRECATED_ENGINES = frozenset({"gemini", "amp"})


def is_valid_id(value: str) -> bool:
    return bool(_ID_RE.fullmatch(value))
