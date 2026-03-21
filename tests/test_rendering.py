import re

import pytest

from untether.telegram.render import (
    _is_telegram_safe_url,
    _sanitise_entities,
    render_markdown,
    split_markdown_body,
)


def test_render_markdown_basic_entities() -> None:
    text, entities = render_markdown("**bold** and `code`")

    assert text == "bold and code"
    assert entities == [
        {"type": "bold", "offset": 0, "length": 4},
        {"type": "code", "offset": 9, "length": 4},
    ]


def test_render_markdown_code_fence_language_is_string() -> None:
    text, entities = render_markdown("```py\nprint('x')\n```")

    assert text == "print('x')"
    assert entities is not None
    assert any(e.get("type") == "pre" and e.get("language") == "py" for e in entities)
    assert any(e.get("type") == "code" for e in entities)


def test_render_markdown_keeps_ordered_numbering_with_unindented_sub_bullets() -> None:
    md = (
        "1. Tune maker\n"
        "- Sweep\n"
        "- Keep data\n"
        "1. Increase\n"
        "- Raise target\n"
        "- Keep\n"
        "1. Train\n"
        "- Start\n"
        "1. Add\n"
        "- Keep exposure\n"
        "1. Run\n"
        "- Target pnl\n"
    )

    text, _ = render_markdown(md)
    numbered = [line for line in text.splitlines() if re.match(r"^\d+\.\s", line)]

    assert numbered == [
        "1. Tune maker",
        "2. Increase",
        "3. Train",
        "4. Add",
        "5. Run",
    ]


def test_render_markdown_clamps_entities_after_strip() -> None:
    """The voice-disabled hint ends with a code block; sulguk entities must
    not overflow the text after rstrip('\\n')."""
    from untether.telegram.voice import VOICE_TRANSCRIPTION_DISABLED_HINT

    text, entities = render_markdown(VOICE_TRANSCRIPTION_DISABLED_HINT)
    text_utf16_len = len(text.encode("utf-16-le")) // 2
    for e in entities:
        end = e.get("offset", 0) + e.get("length", 0)
        assert end <= text_utf16_len, (
            f"entity {e} overflows text (len={text_utf16_len})"
        )


def test_render_markdown_code_block_at_end() -> None:
    """Any markdown ending with a fenced code block should have valid entities."""
    md = "intro\n```\nsome code\n```"
    text, entities = render_markdown(md)
    text_utf16_len = len(text.encode("utf-16-le")) // 2
    for e in entities:
        end = e.get("offset", 0) + e.get("length", 0)
        assert end <= text_utf16_len


def test_render_markdown_preserves_inner_entities() -> None:
    """Code block NOT at the end — entities should be unchanged."""
    md = "```\ncode\n```\n\nafter"
    text, entities = render_markdown(md)
    assert "after" in text
    code_entities = [e for e in entities if e.get("type") in ("pre", "code")]
    assert len(code_entities) > 0
    text_utf16_len = len(text.encode("utf-16-le")) // 2
    for e in entities:
        end = e.get("offset", 0) + e.get("length", 0)
        assert end <= text_utf16_len


def test_prepare_telegram_multi_footer_only_on_last() -> None:
    """Continued messages should NOT repeat the footer — only the last chunk gets it."""
    from untether.telegram.render import MarkdownParts, prepare_telegram_multi

    footer = "\N{LABEL} dir: test | sonnet"
    body = "word " * 200  # Long enough to split
    parts = MarkdownParts(header="done", body=body, footer=footer)

    payloads = prepare_telegram_multi(parts, max_body_chars=200)

    assert len(payloads) > 1, "body should split into multiple messages"
    # Only the last message should contain the footer
    for i, (text, _entities) in enumerate(payloads[:-1]):
        assert "sonnet" not in text, f"message {i + 1} should not have footer"
    last_text, _ = payloads[-1]
    assert "sonnet" in last_text, "last message should have footer"


def test_prepare_telegram_multi_single_message_has_footer() -> None:
    """A single-chunk message should still include the footer."""
    from untether.telegram.render import MarkdownParts, prepare_telegram_multi

    footer = "\N{LABEL} dir: test"
    parts = MarkdownParts(header="done", body="short answer", footer=footer)

    payloads = prepare_telegram_multi(parts)

    assert len(payloads) == 1
    text, _ = payloads[0]
    assert "dir: test" in text


def test_split_markdown_body_closes_and_reopens_fence() -> None:
    body = "```py\n" + ("line\n" * 10) + "```\n\npost"

    chunks = split_markdown_body(body, max_chars=40)

    assert len(chunks) > 1
    assert chunks[0].rstrip().endswith("```")
    assert chunks[1].startswith("```py\n")


def test_render_markdown_linkifies_raw_urls() -> None:
    """Raw URLs should become clickable text_link entities."""
    text, entities = render_markdown("Check https://example.com for details")
    assert "example.com" in text
    link_entities = [e for e in entities if e.get("type") == "text_link"]
    assert len(link_entities) == 1
    assert link_entities[0]["url"] == "https://example.com"


# ---------------------------------------------------------------------------
# URL safety and entity sanitisation tests (#157)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/path",
        "http://example.com",
        "https://sub.domain.co.uk/page?q=1",
        "https://api.github.com/repos/owner/repo",
    ],
)
def test_is_telegram_safe_url_accepts_valid(url: str) -> None:
    assert _is_telegram_safe_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080",
        "http://localhost",
        "http://127.0.0.1:3000",
        "http://127.0.0.1",
        "http://0.0.0.0:5000",
        "http://::1/path",
        "/Users/foo/docs/file.md",
        "file:///etc/passwd",
        "ftp://example.com/file",
        "http://myserver/path",
        "",
        "not-a-url",
    ],
)
def test_is_telegram_safe_url_rejects_invalid(url: str) -> None:
    assert _is_telegram_safe_url(url) is False


def test_sanitise_entities_preserves_valid_text_link() -> None:
    entities = [
        {"type": "text_link", "offset": 0, "length": 4, "url": "https://example.com"}
    ]
    assert _sanitise_entities(entities) == entities


def test_sanitise_entities_converts_localhost_to_code() -> None:
    entities = [
        {"type": "text_link", "offset": 0, "length": 4, "url": "http://localhost:8080"}
    ]
    result = _sanitise_entities(entities)
    assert result == [{"type": "code", "offset": 0, "length": 4}]


def test_sanitise_entities_converts_file_path_to_code() -> None:
    entities = [
        {"type": "text_link", "offset": 0, "length": 10, "url": "/Users/foo/file.md"}
    ]
    result = _sanitise_entities(entities)
    assert result == [{"type": "code", "offset": 0, "length": 10}]


def test_sanitise_entities_leaves_non_link_entities() -> None:
    entities = [
        {"type": "bold", "offset": 0, "length": 4},
        {"type": "code", "offset": 5, "length": 3},
    ]
    assert _sanitise_entities(entities) == entities


def test_sanitise_entities_empty_list() -> None:
    assert _sanitise_entities([]) == []


def test_render_markdown_sanitises_localhost_link() -> None:
    """Markdown link to localhost should become code, not text_link (#157)."""
    text, entities = render_markdown("[my app](http://localhost:8080)")
    assert "my app" in text
    link_entities = [e for e in entities if e.get("type") == "text_link"]
    assert len(link_entities) == 0
    code_entities = [e for e in entities if e.get("type") == "code"]
    assert len(code_entities) >= 1


def test_render_markdown_keeps_valid_link() -> None:
    """Markdown link to a valid URL should remain a text_link."""
    text, entities = render_markdown("[docs](https://docs.example.com)")
    link_entities = [e for e in entities if e.get("type") == "text_link"]
    assert len(link_entities) == 1
    assert link_entities[0]["url"] == "https://docs.example.com"
