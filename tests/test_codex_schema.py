from __future__ import annotations

import json
from pathlib import Path

import pytest

from untether.schemas import codex as codex_schema


def _fixture_path(name: str) -> Path:
    return Path(__file__).parent / "fixtures" / name


def _decode_fixture(name: str) -> list[str]:
    path = _fixture_path(name)
    errors: list[str] = []

    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        # Capture fixtures (#419) carry ``#`` header/separator lines.
        if not line.strip() or line.startswith("#"):
            continue
        try:
            json.loads(line)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"line {lineno}: invalid JSON ({exc})")
            continue
        try:
            codex_schema.decode_event(line)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"line {lineno}: {exc.__class__.__name__}: {exc}")

    return errors


@pytest.mark.parametrize(
    "fixture",
    [
        "codex_exec_json_all_formats.jsonl",
        "codex_exec_json_phase_and_unknown.jsonl",
        "codex_exec_json_0157.jsonl",
        "codex_0157_web_search.jsonl",
        "codex_0157_resume_usage.jsonl",
    ],
)
def test_codex_schema_parses_fixture(fixture: str) -> None:
    errors = _decode_fixture(fixture)

    assert not errors, f"{fixture} had {len(errors)} errors: " + "; ".join(errors[:5])


def test_codex_schema_decodes_unknown_item_type() -> None:
    event = codex_schema.decode_event(
        '{"type":"item.completed","item":{"id":"item_99","type":"future_item",'
        '"foo":"bar","count":2}}'
    )
    assert isinstance(event, codex_schema.ItemCompleted)
    assert isinstance(event.item, codex_schema.UnknownItem)
    assert event.item.item_type == "future_item"
    assert event.item.payload == {"foo": "bar", "count": 2}


# --- #419: 0.157.1 Usage fields + lenient web_search ------------------------


def test_usage_decodes_all_five_fields() -> None:
    event = codex_schema.decode_event(
        '{"type":"turn.completed","usage":{"input_tokens":10,'
        '"cached_input_tokens":2,"cache_write_input_tokens":3,'
        '"output_tokens":5,"reasoning_output_tokens":4}}'
    )
    assert isinstance(event, codex_schema.TurnCompleted)
    usage = event.usage
    assert (
        usage.input_tokens,
        usage.cached_input_tokens,
        usage.cache_write_input_tokens,
        usage.output_tokens,
        usage.reasoning_output_tokens,
    ) == (10, 2, 3, 5, 4)
    assert set(codex_schema.CODEX_USAGE_FIELDS) == {
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    }


def test_usage_legacy_three_field_line_defaults_zero() -> None:
    event = codex_schema.decode_event(
        '{"type":"turn.completed","usage":{"input_tokens":10,'
        '"cached_input_tokens":2,"output_tokens":5}}'
    )
    assert isinstance(event, codex_schema.TurnCompleted)
    assert event.usage.cache_write_input_tokens == 0
    assert event.usage.reasoning_output_tokens == 0


def test_web_search_duplicate_id_keeps_last() -> None:
    """Regression guard (D11): the wire carries ``item_N`` then the raw id
    (verified on 0.157.1, fixture codex_0157_web_search.jsonl). msgspec keeps
    the last key, which is the id started/completed share."""
    event = codex_schema.decode_event(
        '{"type":"item.started","item":{"id":"item_0","type":"web_search",'
        '"id":"ws_abc","query":"","action":{"type":"other"}}}'
    )
    assert isinstance(event, codex_schema.ItemStarted)
    assert isinstance(event.item, codex_schema.WebSearchItem)
    assert event.item.id == "ws_abc"


def test_web_search_wire_capture_pairs_started_and_completed() -> None:
    lines = [
        ln
        for ln in _fixture_path("codex_0157_web_search.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    ids: dict[str, list[str]] = {}
    for line in lines:
        event = codex_schema.decode_event(line)
        item = getattr(event, "item", None)
        if isinstance(item, codex_schema.WebSearchItem):
            ids.setdefault(item.id, []).append(type(event).__name__)
    assert ids
    for phases in ids.values():
        assert phases == ["ItemStarted", "ItemCompleted"]


@pytest.mark.parametrize(
    "action",
    [
        {"type": "search", "query": "q"},
        {"type": "search", "queries": ["a", "b"]},
        {"type": "open_page", "url": "https://example.com"},
        {"type": "find_in_page", "url": "https://example.com", "pattern": "p"},
        {"type": "other"},
        None,
    ],
)
def test_web_search_action_variants(action: object) -> None:
    item: dict[str, object] = {"id": "ws", "type": "web_search", "query": ""}
    if action is not None:
        item["action"] = action
    event = codex_schema.decode_event(
        json.dumps({"type": "item.completed", "item": item})
    )
    assert isinstance(event, codex_schema.ItemCompleted)
    assert isinstance(event.item, codex_schema.WebSearchItem)
    assert event.item.action == action


def test_web_search_unknown_action_type_does_not_fail_line() -> None:
    """Regression guard for the rejected closed union (plan §4.6-1)."""
    event = codex_schema.decode_event(
        '{"type":"item.completed","item":{"id":"ws","type":"web_search",'
        '"query":"q","action":{"type":"brand_new","extra":1}}}'
    )
    assert isinstance(event, codex_schema.ItemCompleted)
    assert isinstance(event.item, codex_schema.WebSearchItem)


@pytest.mark.parametrize(
    "item",
    [
        {"query": "", "action": {"type": "search", "queries": ["a", None]}},
        {"query": "", "action": {"type": 7}},
        {"query": "", "action": "search"},
        {"query": None, "action": {"type": "search", "query": "q"}},
        {"query": "q", "results": {"x": 1}},
    ],
)
def test_web_search_malformed_action_does_not_fail_line(item: dict) -> None:
    from untether.events import EventFactory
    from untether.model import ActionEvent
    from untether.runners.codex import translate_codex_event

    payload = {"id": "ws", "type": "web_search", **item}
    event = codex_schema.decode_event(
        json.dumps({"type": "item.completed", "item": payload})
    )
    assert isinstance(event, codex_schema.ItemCompleted)
    out = translate_codex_event(event, title="Codex", factory=EventFactory("codex"))
    assert len(out) == 1
    assert isinstance(out[0], ActionEvent)
    assert out[0].action.title


@pytest.mark.parametrize(
    "results",
    [
        [
            {"url": "https://a", "content": "text"},
            {"url": "https://b", "error": {"message": "boom"}},
        ],
        None,
    ],
)
def test_web_search_results_opaque(results: object) -> None:
    event = codex_schema.decode_event(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "ws",
                    "type": "web_search",
                    "query": "q",
                    "results": results,
                },
            }
        )
    )
    assert isinstance(event, codex_schema.ItemCompleted)
    assert isinstance(event.item, codex_schema.WebSearchItem)
    assert event.item.results == results


def test_resume_capture_usage_is_thread_cumulative() -> None:
    """#419 step-0 U2 (GO): every field of runs 2 and 3 is >= the previous
    run's, and thread.started carries the same id on both resumes."""
    usages: list[codex_schema.Usage] = []
    threads: list[str] = []
    for line in (
        _fixture_path("codex_0157_resume_usage.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ):
        if not line.strip() or line.startswith("#"):
            continue
        event = codex_schema.decode_event(line)
        if isinstance(event, codex_schema.TurnCompleted):
            usages.append(event.usage)
        elif isinstance(event, codex_schema.ThreadStarted):
            threads.append(event.thread_id)
    assert len(usages) == 3
    assert len(threads) == 3 and len(set(threads)) == 1
    for prev, cur in zip(usages, usages[1:], strict=False):
        for name in codex_schema.CODEX_USAGE_FIELDS:
            assert getattr(cur, name) >= getattr(prev, name)
    assert usages[1].input_tokens > usages[0].input_tokens
