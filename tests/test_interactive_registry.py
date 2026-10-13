"""Engine-neutral approval / question registry (#960).

The registry moved out of ``runners/claude.py`` with no behaviour change. The
~950 test references to its stores go through ``runners.claude``, so the
first guard is identity: every name ``runners.claude`` re-exports must be the
very object ``interactive`` owns (a copy would pass import and split state).
"""

from __future__ import annotations

import untether.runners.claude as claude_mod
from untether.interactive import registry

REGISTRY_NAMES = (
    "CONTROL_REQUEST_TIMEOUT_SECONDS",
    "ControlLookup",
    "ControlRequestSnapshot",
    "ControlRequestStatus",
    "ControlSendResult",
    "HandledControl",
    "InflightClaim",
    "_CANCELLED_DURING_WRITE",
    "_DIRECT_CLAIM_SEQ",
    "_HANDLED_REQUESTS",
    "_HANDLED_REQUESTS_MAX",
    "_INFLIGHT_CONTROL_RESPONSES",
    "_REQUEST_TO_CHANNEL",
    "_REQUEST_TO_CHANNEL_MAX",
    "_REQUEST_TO_INPUT",
    "_REQUEST_TO_ORIGINATOR",
    "_REQUEST_TO_SESSION",
    "_REQUEST_TO_TOOL_NAME",
    "_TERMINAL_CONTROL_OUTCOMES",
    "_bind_request_channel",
    "claim_control_request",
    "classify_control_request",
    "control_request_origin",
    "control_request_originator",
    "mark_request_handled",
    "new_control_claim_owner",
    "release_control_claims",
)


def test_interactive_reexports_are_same_objects() -> None:
    for name in REGISTRY_NAMES:
        assert getattr(claude_mod, name) is getattr(registry, name), name
