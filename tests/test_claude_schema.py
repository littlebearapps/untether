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
