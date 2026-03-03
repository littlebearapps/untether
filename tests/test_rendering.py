import re

from untether.telegram.render import render_markdown, split_markdown_body


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


def test_split_markdown_body_closes_and_reopens_fence() -> None:
    body = "```py\n" + ("line\n" * 10) + "```\n\npost"

    chunks = split_markdown_body(body, max_chars=40)

    assert len(chunks) > 1
    assert chunks[0].rstrip().endswith("```")
    assert chunks[1].startswith("```py\n")
