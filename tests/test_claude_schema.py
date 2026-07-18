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
