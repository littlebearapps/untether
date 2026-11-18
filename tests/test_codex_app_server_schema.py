"""Codex app-server contract vs the committed 0.169.0 schema fixture (#961).

Runs in CI (no ``codex`` needed). The fixture is the subset of
``codex app-server generate-json-schema [--experimental]`` Untether depends
on, written by ``scripts/codex_app_server_schema_subset.py``; the live check
against the installed CLI is ``tests/test_codex_app_server_drift.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from untether.runners.codex_app_server import schema_subset, unsupported_methods
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
