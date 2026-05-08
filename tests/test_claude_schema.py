from __future__ import annotations

import json
from pathlib import Path

import pytest

from untether.schemas import claude as claude_schema


def _fixture_path(name: str) -> Path:
    return Path(__file__).parent / "fixtures" / name


def _decode_fixture(name: str) -> list[str]:
    path = _fixture_path(name)
    errors: list[str] = []

    for lineno, line in enumerate(path.read_bytes().splitlines(), 1):
        if not line.strip():
            continue
        try:
            decoded = claude_schema.decode_stream_json_line(line)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"line {lineno}: {exc.__class__.__name__}: {exc}")
            continue

        _ = decoded

    return errors


@pytest.mark.parametrize(
    "fixture",
    [
        "claude_stream_json_session.jsonl",
    ],
)
def test_claude_schema_parses_fixture(fixture: str) -> None:
    errors = _decode_fixture(fixture)

    assert not errors, f"{fixture} had {len(errors)} errors: " + "; ".join(errors[:5])


def test_decode_rate_limit_event_full() -> None:
    payload = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "requests_limit": 1000,
            "requests_remaining": 0,
            "requests_reset": "2026-01-01T00:01:00Z",
            "tokens_limit": 50000,
            "tokens_remaining": 0,
            "tokens_reset": "2026-01-01T00:01:00Z",
            "retry_after_ms": 60000,
        },
    }
    decoded = claude_schema.decode_stream_json_line(json.dumps(payload).encode())
    assert isinstance(decoded, claude_schema.StreamRateLimitMessage)
    assert decoded.rate_limit_info is not None
    assert decoded.rate_limit_info.requests_limit == 1000
    assert decoded.rate_limit_info.retry_after_ms == 60000


def test_decode_rate_limit_event_bare() -> None:
    payload = {"type": "rate_limit_event"}
    decoded = claude_schema.decode_stream_json_line(json.dumps(payload).encode())
    assert isinstance(decoded, claude_schema.StreamRateLimitMessage)
    assert decoded.rate_limit_info is None


# ---------------------------------------------------------------------------
# #489 — server_tool_use + advisor_tool_result content blocks
# ---------------------------------------------------------------------------


def test_decode_server_tool_use_block() -> None:
    """Anthropic server-side tools (web_search, code_execution, …) emit
    `server_tool_use` content blocks. Schema must parse them as
    StreamServerToolUseBlock instead of raising ValidationError."""
    payload = {
        "type": "assistant",
        "uuid": "uuid-1",
        "session_id": "sess-1",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-7",
            "content": [
                {
                    "type": "server_tool_use",
                    "id": "stu_01",
                    "name": "web_search",
                    "input": {"query": "untether telegram"},
                }
            ],
        },
    }
    decoded = claude_schema.decode_stream_json_line(json.dumps(payload).encode())
    assert isinstance(decoded, claude_schema.StreamAssistantMessage)
    assert len(decoded.message.content) == 1
    block = decoded.message.content[0]
    assert isinstance(block, claude_schema.StreamServerToolUseBlock)
    assert block.id == "stu_01"
    assert block.name == "web_search"
    assert block.input == {"query": "untether telegram"}


def test_decode_advisor_tool_result_block() -> None:
    """Result of the parent agent's `advisor()` meta-tool. Schema must parse
    it as StreamAdvisorToolResultBlock instead of raising ValidationError."""
    payload = {
        "type": "user",
        "uuid": "uuid-2",
        "session_id": "sess-1",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "advisor_tool_result",
                    "tool_use_id": "adv_01",
                    "content": "Reviewer said: looks good.",
                    "is_error": False,
                }
            ],
        },
    }
    decoded = claude_schema.decode_stream_json_line(json.dumps(payload).encode())
    assert isinstance(decoded, claude_schema.StreamUserMessage)
    assert isinstance(decoded.message.content, list)
    assert len(decoded.message.content) == 1
    block = decoded.message.content[0]
    assert isinstance(block, claude_schema.StreamAdvisorToolResultBlock)
    assert block.tool_use_id == "adv_01"
    assert block.content == "Reviewer said: looks good."
    assert block.is_error is False


def test_decode_advisor_tool_result_block_minimal() -> None:
    """advisor_tool_result with optional fields omitted (content/is_error default)."""
    payload = {
        "type": "user",
        "uuid": "uuid-3",
        "session_id": "sess-1",
        "message": {
            "role": "user",
            "content": [
                {"type": "advisor_tool_result", "tool_use_id": "adv_02"},
            ],
        },
    }
    decoded = claude_schema.decode_stream_json_line(json.dumps(payload).encode())
    assert isinstance(decoded, claude_schema.StreamUserMessage)
    assert isinstance(decoded.message.content, list)
    block = decoded.message.content[0]
    assert isinstance(block, claude_schema.StreamAdvisorToolResultBlock)
    assert block.tool_use_id == "adv_02"
    assert block.content is None
    assert block.is_error is None
