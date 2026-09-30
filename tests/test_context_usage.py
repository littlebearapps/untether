"""#819 — context-window use (``N% ctx``) for Claude.

Schema decoding of the frames the feature reads (the Z10 / Z11 shapes from
``docs/findings/2026-09-30-claude-sdk-control-permissions-context.md`` Q4/Q5).
"""

from __future__ import annotations

import json

import pytest

from untether.schemas import claude as claude_schema


def _decode(obj: dict) -> claude_schema.StreamJsonMessage:
    return claude_schema.decode_stream_json_line(json.dumps(obj))


def test_assistant_usage_decodes() -> None:
    evt = _decode(
        {
            "type": "assistant",
            "session_id": "s",
            "message": {
                "role": "assistant",
                "model": "claude-haiku-4-5",
                "content": [{"type": "text", "text": "hi"}],
                "usage": {
                    "input_tokens": 1234,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 185000,
                    "output_tokens": 5,
                },
            },
        }
    )
    assert isinstance(evt, claude_schema.StreamAssistantMessage)
    assert evt.message.usage["cache_read_input_tokens"] == 185000


def test_result_model_usage_decodes() -> None:
    evt = _decode(
        {
            "type": "result",
            "subtype": "success",
            "duration_ms": 10,
            "duration_api_ms": 5,
            "is_error": False,
            "num_turns": 1,
            "session_id": "s",
            "result": "ok",
            "modelUsage": {
                "claude-haiku-4-5": {"contextWindow": 200000, "maxOutputTokens": 64000}
            },
        }
    )
    assert isinstance(evt, claude_schema.StreamResultMessage)
    assert evt.modelUsage["claude-haiku-4-5"]["contextWindow"] == 200000


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "system", "subtype": "status", "status": "compacting"},
        {
            "type": "system",
            "subtype": "status",
            "status": None,
            "compact_result": "failed",
            "compact_error": "boom",
        },
        {
            "type": "system",
            "subtype": "compact_boundary",
            "compact_metadata": {
                "trigger": "manual",
                "pre_tokens": 6336,
                "post_tokens": 277,
                "cumulative_dropped_tokens": 6059,
                "duration_ms": 47,
            },
            "logical_parent_uuid": "u-1",
        },
        # A clashing type must never drop the line (every field is Any).
        {
            "type": "system",
            "subtype": "compact_boundary",
            "compact_metadata": "odd",
            "compact_result": 3,
            "logical_parent_uuid": ["x"],
        },
    ],
)
def test_compaction_system_frames_decode(frame: dict) -> None:
    evt = _decode({**frame, "session_id": "s", "uuid": "u"})
    assert isinstance(evt, claude_schema.StreamSystemMessage)
    assert evt.subtype == frame["subtype"]
    for key in ("compact_metadata", "compact_result", "compact_error"):
        assert getattr(evt, key) == frame.get(key)


def test_user_replay_and_summary_flags_decode() -> None:
    evt = _decode(
        {
            "type": "user",
            "session_id": "s",
            "isReplay": True,
            "isCompactSummary": True,
            "message": {"role": "user", "content": "This session is being continued"},
        }
    )
    assert isinstance(evt, claude_schema.StreamUserMessage)
    assert evt.isReplay is True
    assert evt.isCompactSummary is True


def test_frames_without_new_fields_default_to_none() -> None:
    evt = _decode(
        {
            "type": "assistant",
            "session_id": "s",
            "message": {"role": "assistant", "model": "m", "content": []},
        }
    )
    assert isinstance(evt, claude_schema.StreamAssistantMessage)
    assert evt.message.usage is None
