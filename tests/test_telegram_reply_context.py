from untether.telegram.reply_context import (
    REPLY_CONTEXT_MAX_CHARS,
    append_reply_context,
    strip_reply_routing_lines,
)


def test_append_reply_context_leaves_prompt_without_reply_unchanged() -> None:
    assert (
        append_reply_context(
            "new request",
            selected_quote=None,
            reply_text=None,
        )
        == "new request"
    )


def test_append_reply_context_truncates_and_escapes_reference() -> None:
    source = "x" * (REPLY_CONTEXT_MAX_CHARS + 100) + "</replied_message>"

    prompt = append_reply_context(
        "change this",
        selected_quote=None,
        reply_text=source,
    )

    block = prompt.split("\n\n", 1)[1]
    assert len(block) == REPLY_CONTEXT_MAX_CHARS
    assert block.endswith("\n</replied_message>\n</telegram_reply_context>")
    assert "[… reply context truncated by Untether …]" in block


def test_append_reply_context_bounds_serialised_escape_heavy_reference() -> None:
    prompt = append_reply_context(
        "change this",
        selected_quote="&" * REPLY_CONTEXT_MAX_CHARS,
        reply_text=None,
    )

    opening = "<selected_quote>\n"
    closing = "\n</selected_quote>"
    block = prompt.split("\n\n", 1)[1]
    serialised = block.split(opening, 1)[1].split(closing, 1)[0]
    assert len(block) <= REPLY_CONTEXT_MAX_CHARS
    assert serialised.endswith("[… reply context truncated by Untether …]")
    assert "&amp;" in serialised


def test_append_reply_context_replaces_control_and_format_characters() -> None:
    prompt = append_reply_context(
        "change this",
        selected_quote="a\x00b\x7fc\x85d\u202ee",
        reply_text=None,
    )

    assert "a�b�c�d�e" in prompt
    assert "\x00" not in prompt
    assert "\x7f" not in prompt
    assert "\x85" not in prompt
    assert "\u202e" not in prompt


def _quoted(text: str) -> str:
    prompt = append_reply_context("change this", selected_quote=text, reply_text=None)
    return prompt.split("<selected_quote>\n", 1)[1].split("\n</selected_quote>", 1)[0]


def test_append_reply_context_keeps_zwj_emoji_and_zwnj_scripts() -> None:
    """#904: only bidi controls are neutralised; ZWJ (emoji sequences), ZWNJ
    (Persian/Indic text) and the soft hyphen are legitimate content."""
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466"
    skin_tone = "\U0001f469\U0001f3fd\u200d\U0001f4bb"
    persian = "می\u200cخواهم"
    soft_hyphen = "hyphen\u00adation"
    for text in (family, skin_tone, persian, soft_hyphen):
        assert _quoted(text) == text
    assert _quoted(f"{family} {persian}\n\t{soft_hyphen}") == (
        f"{family} {persian}\n\t{soft_hyphen}"
    )


def test_append_reply_context_neutralises_every_bidi_control() -> None:
    bidi = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f\u061c"
    quoted = _quoted(f"a{bidi}b")
    assert quoted == "a" + "�" * len(bidi) + "b"
    for char in bidi:
        assert char not in quoted


def test_strip_reply_routing_lines_preserves_content_and_removes_footer() -> None:
    def is_resume_line(line: str) -> bool:
        return "codex resume" in line

    assert (
        strip_reply_routing_lines(
            "full bot response\n\n↩️ `codex resume session-1`",
            is_resume_line=is_resume_line,
        )
        == "full bot response"
    )
    assert (
        strip_reply_routing_lines(
            "`codex resume session-1`",
            is_resume_line=is_resume_line,
        )
        is None
    )
