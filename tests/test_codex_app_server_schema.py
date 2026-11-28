"""Codex app-server contract vs the committed schema fixture (#961, #963).

Runs in CI (no ``codex`` needed). The fixture is the subset of
``codex app-server generate-json-schema [--experimental]`` Untether depends
on, written by ``scripts/codex_app_server_schema_subset.py``; the live check
against the installed CLI is ``tests/test_codex_app_server_drift.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import msgspec
import pytest

from untether.runners.codex_app_server import schema_subset, unsupported_methods
from untether.schemas import codex_app_server as cas
from untether.schemas.codex_app_server import (
    CODEX_APP_SERVER_SENDS,
    CODEX_APP_SERVER_TYPES,
    CODEX_APP_SERVER_USED,
    PROBED_APP_SERVER_VERSION,
)

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "codex_app_server"
    / f"schema-subset-{PROBED_APP_SERVER_VERSION}.json"
)


@pytest.fixture(scope="module")
def fixture() -> dict:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["codex_version"] == PROBED_APP_SERVER_VERSION
    return data["subset"]


def test_every_used_method_is_in_the_fixture_at_its_tier(fixture: dict) -> None:
    assert unsupported_methods(fixture) == []
    for (kind, method), tier in CODEX_APP_SERVER_USED.items():
        assert fixture[f"{kind} {method}"]["tier"] == tier, (kind, method)


def test_every_value_untether_sends_is_stable(fixture: dict) -> None:
    for enum_name, values in CODEX_APP_SERVER_SENDS.items():
        names = fixture[f"enum {enum_name}"]["names"]
        for value in values:
            assert names.get(value) == "S", (enum_name, value)


def test_every_read_type_is_in_the_fixture(fixture: dict) -> None:
    for type_name, variants in CODEX_APP_SERVER_TYPES.items():
        keys = [f"type {type_name}.{v}" for v in variants] or [f"type {type_name}"]
        for key in keys:
            assert fixture[key]["tier"] == "S", key
            assert fixture[key]["names"], key


def test_fixture_pins_the_fields_rc1_relies_on(fixture: dict) -> None:
    """Spot checks from the plan: % ctx inputs, the final-answer phase, the
    interrupt ids, the developer-instructions preamble route (DEC-15)."""
    assert {"last", "modelContextWindow"} <= fixture["type ThreadTokenUsage"][
        "names"
    ].keys()
    assert "phase" in fixture["type ThreadItem.agentMessage"]["names"]
    assert fixture["client_request turn/interrupt"]["names"] == {
        "params.threadId": "S",
        "params.turnId": "S",
    }
    start = fixture["client_request thread/start"]["names"]
    assert start["params.developerInstructions"] == "S"
    assert start["result.model"] == "S"
    # Sent on every resume (#962 QA): must stay stable.
    resume = fixture["client_request thread/resume"]["names"]
    assert resume["params.excludeTurns"] == "S"
    # Plan mode is experimental-only on turn/start (#965, DEC-3).
    assert (
        fixture["client_request turn/start"]["names"]["params.collaborationMode"] == "E"
    )


# --- schema_subset / unsupported_methods on hand-built bundles ---------------


def _variant(method: str, params: str | None = None) -> dict:
    props: dict = {"method": {"enum": [method]}}
    if params is not None:
        props["params"] = {"$ref": f"#/definitions/v2/{params}"}
    return {"properties": props}


def _bundle(*, experimental: bool) -> dict:
    v2: dict = {
        "TurnInterruptParams": {
            "title": "TurnInterruptParams",
            "properties": {"threadId": {}, "turnId": {}},
        },
        "TurnInterruptResponse": {"properties": {}},
        "TurnStartParams": {
            "title": "TurnStartParams",
            "properties": {"threadId": {}, "input": {}},
        },
        "TurnStartResponse": {"properties": {"turn": {}}},
        "ThreadItem": {
            "oneOf": [
                {"properties": {"type": {"enum": ["agentMessage"]}, "text": {}}},
                {"properties": {"type": {"enum": ["somethingElse"]}, "x": {}}},
            ]
        },
    }
    defs: dict = {
        "v2": v2,
        "ClientRequest": {
            "oneOf": [
                _variant("turn/start", "TurnStartParams"),
                _variant("not/used"),
            ]
        },
        "AskForApproval": {
            "oneOf": [{"enum": ["never", "on-request"]}, {"type": "object"}]
        },
    }
    if experimental:
        defs["ClientRequest"]["oneOf"].append(
            _variant("turn/interrupt", "TurnInterruptParams")
        )
        v2["TurnStartParams"]["properties"]["collaborationMode"] = {}
    return {"definitions": defs}


def test_schema_subset_tiers_methods_fields_unions_and_enums() -> None:
    subset = schema_subset(_bundle(experimental=False), _bundle(experimental=True))
    assert subset["client_request turn/start"] == {
        "tier": "S",
        "names": {
            "params.collaborationMode": "E",
            "params.input": "S",
            "params.threadId": "S",
            "result.turn": "S",
        },
    }
    assert subset["client_request turn/interrupt"]["tier"] == "E"
    assert "client_request not/used" not in subset
    assert subset["type ThreadItem.agentMessage"]["names"] == {"text": "S", "type": "S"}
    assert not any(k.startswith("type ThreadItem.somethingElse") for k in subset)
    assert subset["enum AskForApproval"]["names"] == {"never": "S", "on-request": "S"}


def test_unsupported_methods_names_missing_and_demoted() -> None:
    subset = schema_subset(_bundle(experimental=False), _bundle(experimental=True))
    problems = unsupported_methods(subset)
    assert "`turn/interrupt` is experimental-only" in problems
    assert "`thread/start` missing" in problems
    assert not any("turn/start`" in p for p in problems)


# --- #962 msgspec structs ----------------------------------------------------

LIVE_FIXTURES = sorted(FIXTURE.parent.glob("live-*.jsonl"))


def _wire_names(cls: type) -> set[str]:
    return {f.encode_name for f in msgspec.structs.fields(cls)}


# struct → (fixture key, name prefix): every field must exist in the schema.
_STRUCT_KEYS: list[tuple[type, str, str]] = [
    (cas.TokenUsageBreakdown, "type TokenUsageBreakdown", ""),
    (cas.ThreadTokenUsage, "type ThreadTokenUsage", ""),
    (cas.TurnError, "type TurnError", ""),
    (cas.Turn, "type Turn", ""),
    *(
        (cls, f"type ThreadItem.{tag}", "")
        for tag, cls in cas.THREAD_ITEM_TYPES.items()
    ),
    (cas.InitializeResult, "client_request initialize", "result."),
    (cas.ThreadOpenResult, "client_request thread/start", "result."),
    (cas.ThreadResumeResult, "client_request thread/resume", "result."),
    (cas.ThreadListResult, "client_request thread/list", "result."),
    (cas.TurnStartResult, "client_request turn/start", "result."),
    *(
        (cls, f"server_notification {method}", "params.")
        for method, cls in cas.NOTIFICATION_TYPES.items()
    ),
    *(
        (cls, f"server_request {method}", "params.")
        for method, cls in cas.REQUEST_PARAM_TYPES.items()
    ),
]


@pytest.mark.parametrize(("cls", "key", "prefix"), _STRUCT_KEYS)
def test_every_struct_field_exists_in_the_fixture(
    fixture: dict, cls: type, key: str, prefix: str
) -> None:
    names = fixture[key]["names"]
    missing = {n for n in _wire_names(cls) if f"{prefix}{n}" not in names}
    assert missing == set(), (cls.__name__, key)
    # Stable connection only: never read an experimental-only field, bar the
    # recorded exception (#963 availableDecisions, with a stable fallback).
    stable = _wire_names(cls) - cas.EXPERIMENTAL_FIELDS_READ
    assert all(names[f"{prefix}{n}"] == "S" for n in stable)


def test_every_used_notification_and_item_has_a_struct() -> None:
    used = {m for (k, m) in CODEX_APP_SERVER_USED if k == "server_notification"}
    assert used == set(cas.NOTIFICATION_TYPES) | cas.DICT_NOTIFICATIONS | {
        cas.SETTINGS_NOTIFICATION
    }
    requests = {m for (k, m) in CODEX_APP_SERVER_USED if k == "server_request"}
    assert set(cas.REQUEST_PARAM_TYPES) <= requests
    assert set(cas.THREAD_ITEM_TYPES) == set(CODEX_APP_SERVER_TYPES["ThreadItem"])


def test_live_transcripts_decode() -> None:
    assert len(LIVE_FIXTURES) == 5
    decoded: dict[str, int] = {}
    for path in LIVE_FIXTURES:
        sent: dict = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            msg = rec["msg"]
            if rec["dir"] == "c2s" and "id" in msg and "method" in msg:
                sent[msg["id"]] = msg["method"]
            elif rec["dir"] == "s2c" and "result" in msg and msg["id"] in sent:
                method = sent[msg["id"]]
                assert cas.decode_result(method, msg["result"]) is not None
                decoded[method] = decoded.get(method, 0) + 1
            elif rec["dir"] == "s2c" and "method" in msg and "id" not in msg:
                params = cas.decode_notification(msg["method"], msg.get("params"))
                if params is not None:
                    decoded[msg["method"]] = decoded.get(msg["method"], 0) + 1
                if isinstance(params, cas.ItemParams):
                    cas.decode_thread_item(params.item)
                if isinstance(params, cas.TurnParams):
                    for item in params.turn.items:
                        cas.decode_thread_item(item)
    # The transcripts exercise the core of the stable surface.
    for method in (
        "initialize",
        "thread/start",
        "turn/start",
        "thread/started",
        "turn/completed",
        "item/started",
        "item/completed",
        "thread/tokenUsage/updated",
        "configWarning",
        "warning",
    ):
        assert decoded.get(method), method


def test_thread_open_result_keeps_the_sandbox_policy_object() -> None:
    result = cas.decode_result(
        "thread/start",
        {
            "thread": {"id": "t1", "source": "vscode"},
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "sandbox": {"type": "readOnly", "networkAccess": False},
            "model": "m",
        },
    )
    assert isinstance(result, cas.ThreadOpenResult)
    assert result.sandbox == {"type": "readOnly", "networkAccess": False}
    assert result.thread.id == "t1" and result.thread.source == "vscode"


def test_unknown_item_type_decodes_to_the_generic_struct() -> None:
    item = cas.decode_thread_item({"type": "hookPrompt", "id": "i1", "x": 1})
    assert isinstance(item, cas.GenericItem)
    assert (item.type, item.id, item.raw["x"]) == ("hookPrompt", "i1", 1)
    # A known type whose shape no longer fits is generic too, never an error.
    bad = cas.decode_thread_item({"type": "agentMessage", "id": 5})
    assert isinstance(bad, cas.GenericItem) and bad.id is None
    assert isinstance(cas.decode_thread_item(None), cas.GenericItem)


@pytest.mark.parametrize(
    "info",
    [
        "contextWindowExceeded",
        {"httpConnectionFailed": {"httpStatusCode": 502}},
        {"somethingNew": {"a": [1, 2]}},
        None,
    ],
)
def test_unknown_codex_error_info_decodes(info: object) -> None:
    params = cas.decode_notification(
        "error",
        {
            "error": {"message": "boom", "codexErrorInfo": info},
            "willRetry": False,
            "threadId": "t",
            "turnId": "u",
        },
    )
    assert isinstance(params, cas.ErrorParams)
    assert params.error.codex_error_info == info


def test_extra_top_level_fields_are_ignored() -> None:
    params = cas.decode_notification(
        "turn/completed",
        {
            "threadId": "t",
            "turn": {"id": "u", "status": "completed", "items": [], "new": 1},
            "brandNew": {"x": 1},
        },
    )
    assert isinstance(params, cas.TurnParams) and params.turn.status == "completed"


@pytest.mark.parametrize(
    "display",
    [
        # 0.169.0
        {"action": "show", "blockType": None, "cyberTrustedAccess": None},
        # 0.170.0: a versioned union on `mode`
        {"mode": "hide", "version": 1, "replacement": {"kind": "x"}},
        {"mode": "block", "version": 1, "reason": {"kind": "cyber"}},
    ],
)
def test_both_moderation_display_shapes_decode(display: dict) -> None:
    item = cas.decode_thread_item(
        {
            "type": "agentMessage",
            "id": "m1",
            "text": "hi",
            "phase": "final_answer",
            "moderation": {"moderated": True, "display": display},
        }
    )
    assert isinstance(item, cas.AgentMessageItem)
    assert item.moderation is not None and item.moderation["display"] == display
