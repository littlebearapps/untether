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
# #790 — the real rate_limit_event is a quota-status snapshot (CLI 2.1.283)
# ---------------------------------------------------------------------------

# Captured 2026-09-28 on lba-1 from a one-turn Haiku probe (uuid/session_id
# redacted). Every key below was silently dropped by the pre-#790 schema.
REAL_ALLOWED_RATE_LIMIT_EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed",
        "resetsAt": 1790578200,
        "rateLimitType": "five_hour",
        "overageStatus": "rejected",
        "overageDisabledReason": "out_of_credits",
        "isUsingOverage": False,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.09, "resetsAt": 1790578200},
            "seven_day": {"utilization": 0.15, "resetsAt": 1791036000},
        },
    },
    "uuid": "00000000-0000-0000-0000-000000000000",
    "session_id": "11111111-1111-1111-1111-111111111111",
}


def test_decode_real_allowed_rate_limit_event() -> None:
    decoded = claude_schema.decode_stream_json_line(
        json.dumps(REAL_ALLOWED_RATE_LIMIT_EVENT).encode()
    )
    assert isinstance(decoded, claude_schema.StreamRateLimitMessage)
    assert decoded.uuid == "00000000-0000-0000-0000-000000000000"
    assert decoded.session_id == "11111111-1111-1111-1111-111111111111"
    info = decoded.rate_limit_info
    assert info is not None
    assert info.status == "allowed"
    assert info.resets_at == 1790578200
    assert info.rate_limit_type == "five_hour"
    assert info.overage_status == "rejected"
    assert info.overage_disabled_reason == "out_of_credits"
    assert info.is_using_overage is False
    assert info.unified_windows is not None
    assert info.unified_windows.five_hour is not None
    assert info.unified_windows.five_hour.utilization == 0.09
    assert info.unified_windows.five_hour.resets_at == 1790578200
    assert info.unified_windows.seven_day is not None
    assert info.unified_windows.seven_day.utilization == 0.15
    assert info.unified_windows.seven_day_overage_included is None
    # Legacy fields stay available and simply absent.
    assert info.retry_after_ms is None
    assert info.requests_reset is None


def test_decode_allowed_warning_rate_limit_event() -> None:
    payload = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "allowed_warning",
            "resetsAt": 1790578200,
            "rateLimitType": "seven_day_opus",
            "utilization": 0.82,
            "surpassedThreshold": 0.8,
        },
        "uuid": "u",
        "session_id": "s",
    }
    decoded = claude_schema.decode_stream_json_line(json.dumps(payload).encode())
    assert isinstance(decoded, claude_schema.StreamRateLimitMessage)
    info = decoded.rate_limit_info
    assert info is not None
    assert info.status == "allowed_warning"
    assert info.rate_limit_type == "seven_day_opus"
    assert info.utilization == 0.82


def test_decode_rejected_rate_limit_event_with_error_code() -> None:
    payload = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "rejected",
            "rateLimitType": "overage",
            "overageStatus": "rejected",
            "isUsingOverage": True,
            "errorCode": "credits_required",
            "unifiedWindows": {
                "seven_day_overage_included": {
                    "utilization": 1.0,
                    "resetsAt": 1791036000,
                }
            },
        },
        "uuid": "u",
        "session_id": "s",
    }
    decoded = claude_schema.decode_stream_json_line(json.dumps(payload).encode())
    assert isinstance(decoded, claude_schema.StreamRateLimitMessage)
    info = decoded.rate_limit_info
    assert info is not None
    assert info.status == "rejected"
    assert info.error_code == "credits_required"
    assert info.is_using_overage is True
    assert info.resets_at is None
    assert info.unified_windows is not None
    window = info.unified_windows.seven_day_overage_included
    assert window is not None
    assert window.utilization == 1.0


def test_decode_rate_limit_event_unknown_status_and_extra_keys() -> None:
    """Upstream enum growth must degrade to "unknown value", never a dropped
    line — status is a plain str, and unknown keys are ignored."""
    payload = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "throttled_v2",
            "rateLimitType": "one_hour",
            "limitScope": "group_pool",
            "brandNewKey": {"nested": [1, 2, 3]},
            "unifiedWindows": {"one_hour": {"utilization": 0.5, "resetsAt": 1}},
        },
        "uuid": "u",
        "session_id": "s",
    }
    decoded = claude_schema.decode_stream_json_line(json.dumps(payload).encode())
    assert isinstance(decoded, claude_schema.StreamRateLimitMessage)
    assert decoded.rate_limit_info is not None
    assert decoded.rate_limit_info.status == "throttled_v2"
    assert decoded.rate_limit_info.rate_limit_type == "one_hour"


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


# ---------------------------------------------------------------------------
# #501 — tool_result.content / advisor_tool_result.content as a single dict
# ---------------------------------------------------------------------------


def test_decode_tool_result_block_with_dict_content() -> None:
    """Claude Code may emit `tool_result.content` as a single content block
    object (e.g. {"type": "text", "text": "..."}), not just str / list /
    null. Schema must accept the dict shape so msgspec doesn't drop the
    line with ValidationError."""
    payload = {
        "type": "user",
        "uuid": "uuid-501a",
        "session_id": "sess-501",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_501",
                    "content": {"type": "text", "text": "ok"},
                    "is_error": False,
                },
            ],
        },
    }
    decoded = claude_schema.decode_stream_json_line(json.dumps(payload).encode())
    assert isinstance(decoded, claude_schema.StreamUserMessage)
    assert isinstance(decoded.message.content, list)
    block = decoded.message.content[0]
    assert isinstance(block, claude_schema.StreamToolResultBlock)
    assert block.tool_use_id == "tu_501"
    assert block.content == {"type": "text", "text": "ok"}
    assert block.is_error is False


def test_decode_advisor_tool_result_block_with_dict_content() -> None:
    """advisor_tool_result with the same dict-content shape as #501."""
    payload = {
        "type": "user",
        "uuid": "uuid-501b",
        "session_id": "sess-501",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "advisor_tool_result",
                    "tool_use_id": "adv_501",
                    "content": {"type": "text", "text": "advice"},
                },
            ],
        },
    }
    decoded = claude_schema.decode_stream_json_line(json.dumps(payload).encode())
    block = decoded.message.content[0]
    assert isinstance(block, claude_schema.StreamAdvisorToolResultBlock)
    assert block.tool_use_id == "adv_501"
    assert block.content == {"type": "text", "text": "advice"}


# ---------------------------------------------------------------------------
# #597 — image + document content blocks (Read on binary media echoes these
# back inside user-role messages; x23 jsonl.msgspec.invalid on nsd)
# ---------------------------------------------------------------------------


def test_decode_image_block_in_user_message() -> None:
    """A `Read` on an image echoes an image content block in the user-role
    message. Schema must parse it instead of dropping the whole line."""
    payload = {
        "type": "user",
        "uuid": "uuid-img",
        "session_id": "sess-1",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": "aGVsbG8=",
                    },
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_img",
                    "content": "read 1 image",
                },
            ],
        },
    }
    decoded = claude_schema.decode_stream_json_line(json.dumps(payload).encode())
    assert isinstance(decoded, claude_schema.StreamUserMessage)
    assert isinstance(decoded.message.content, list)
    block = decoded.message.content[0]
    assert isinstance(block, claude_schema.StreamImageBlock)
    assert block.source is not None
    assert block.source["media_type"] == "image/jpeg"
    assert isinstance(decoded.message.content[1], claude_schema.StreamToolResultBlock)


def test_decode_document_block_in_user_message() -> None:
    """PDF reads echo a document content block — same #489-family shape."""
    payload = {
        "type": "user",
        "uuid": "uuid-doc",
        "session_id": "sess-1",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": "JVBERi0=",
                    },
                    "title": "report.pdf",
                }
            ],
        },
    }
    decoded = claude_schema.decode_stream_json_line(json.dumps(payload).encode())
    assert isinstance(decoded, claude_schema.StreamUserMessage)
    assert isinstance(decoded.message.content, list)
    block = decoded.message.content[0]
    assert isinstance(block, claude_schema.StreamDocumentBlock)
    assert block.title == "report.pdf"


def test_decode_image_block_in_assistant_message() -> None:
    """Assistant-role messages can carry image blocks too (vision replies);
    the union addition covers both bodies for free."""
    payload = {
        "type": "assistant",
        "uuid": "uuid-img2",
        "session_id": "sess-1",
        "message": {
            "role": "assistant",
            "model": "claude-fable-5",
            "content": [
                {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}
            ],
        },
    }
    decoded = claude_schema.decode_stream_json_line(json.dumps(payload).encode())
    assert isinstance(decoded, claude_schema.StreamAssistantMessage)
    assert isinstance(decoded.message.content[0], claude_schema.StreamImageBlock)


# #637 — top-level `tool_progress` heartbeat emitted while a long-running
# tool is in flight. Payload below is the verbatim shape captured from
# Claude Code CLI 2.1.214 by running a >30s Bash command. Before the fix
# msgspec raised: Invalid value 'tool_progress' - at `$.type`.
def test_decode_tool_progress_heartbeat() -> None:
    payload = {
        "type": "tool_progress",
        "tool_use_id": "toolu_011cbTyUrSBE4D28tMCVqRSt-heartbeat-0",
        "tool_name": "Bash",
        "parent_tool_use_id": "toolu_011cbTyUrSBE4D28tMCVqRSt",
        "elapsed_time_seconds": 30,
        "heartbeat": True,
        "session_id": "8e8245e8-952c-4b70-9c6f-4c1cb4d4a687",
        "uuid": "a9786562-4e78-418e-b48a-b14e57a1076d",
    }
    decoded = claude_schema.decode_stream_json_line(json.dumps(payload).encode())
    assert isinstance(decoded, claude_schema.StreamToolProgressMessage)
    assert decoded.tool_name == "Bash"
    assert decoded.heartbeat is True
    assert decoded.elapsed_time_seconds == 30
    assert decoded.session_id == "8e8245e8-952c-4b70-9c6f-4c1cb4d4a687"


def test_decode_tool_progress_minimal_and_unknown_fields() -> None:
    """Every field is optional and unknown fields are tolerated, so an
    upstream shape change cannot reintroduce the dropped-line regression."""
    decoded = claude_schema.decode_stream_json_line(
        json.dumps({"type": "tool_progress", "some_future_field": {"a": 1}}).encode()
    )
    assert isinstance(decoded, claude_schema.StreamToolProgressMessage)
    assert decoded.tool_name is None
    assert decoded.heartbeat is None


def test_tool_progress_translates_to_no_events() -> None:
    """The heartbeat must decode *and* be ignored — it carries no progress
    detail Untether renders (elapsed time comes from Untether's own clock,
    #481), so translate must not emit a spurious action."""
    from untether.runners.claude import ClaudeStreamState, translate_claude_event

    decoded = claude_schema.decode_stream_json_line(
        json.dumps(
            {"type": "tool_progress", "tool_name": "Bash", "heartbeat": True}
        ).encode()
    )
    state = ClaudeStreamState()
    assert (
        translate_claude_event(
            decoded, title="claude", state=state, factory=state.factory
        )
        == []
    )


# ---------------------------------------------------------------------------
# #792 — system/api_retry (CLI 2.1.283 zod: SDKAPIRetryMessage)
# ---------------------------------------------------------------------------


def test_decode_api_retry_with_http_status() -> None:
    payload = {
        "type": "system",
        "subtype": "api_retry",
        "attempt": 2,
        "max_retries": 10,
        "retry_delay_ms": 8000,
        "error_status": 529,
        "error": "overloaded",
        "uuid": "u",
        "session_id": "s",
    }
    decoded = claude_schema.decode_stream_json_line(json.dumps(payload).encode())
    assert isinstance(decoded, claude_schema.StreamSystemMessage)
    assert decoded.subtype == "api_retry"
    assert decoded.attempt == 2
    assert decoded.max_retries == 10
    assert decoded.retry_delay_ms == 8000
    assert decoded.error_status == 529
    assert decoded.error == "overloaded"
    assert decoded.no_response is None


def test_decode_api_retry_no_response_and_null_status() -> None:
    payload = {
        "type": "system",
        "subtype": "api_retry",
        "attempt": 1,
        "max_retries": 1,
        "retry_delay_ms": 2000,
        "error_status": None,
        "error": "unknown",
        "no_response": {"waited_ms": 45000, "retry_wait_ms": 90000},
        "uuid": "u",
        "session_id": "s",
    }
    decoded = claude_schema.decode_stream_json_line(json.dumps(payload).encode())
    assert isinstance(decoded, claude_schema.StreamSystemMessage)
    assert decoded.error_status is None
    assert decoded.no_response is not None
    assert decoded.no_response.waited_ms == 45000
    assert decoded.no_response.retry_wait_ms == 90000


def test_decode_api_retry_tolerates_drift() -> None:
    """Unknown keys, an object-shaped ``error`` and extra ``no_response``
    keys must never drop the line."""
    payload = {
        "type": "system",
        "subtype": "api_retry",
        "attempt": 3,
        "max_retries": 10,
        "retry_delay_ms": 16000,
        "error_status": 500,
        "error": {"message": "boom", "formatted": "API Error: 500"},
        "no_response": {"waited_ms": 1, "retry_wait_ms": 2, "new_key": True},
        "brand_new_field": [1, 2],
        "uuid": "u",
        "session_id": "s",
    }
    decoded = claude_schema.decode_stream_json_line(json.dumps(payload).encode())
    assert isinstance(decoded, claude_schema.StreamSystemMessage)
    assert decoded.attempt == 3
    assert isinstance(decoded.error, dict)
