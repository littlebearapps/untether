import re
import textwrap

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


# ---------------------------------------------------------------------------
# #786 — model-emitted <br> renders as a line break, not literally
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tag", ["<br>", "<br/>", "<br />", "<BR>", "<Br />"])
def test_render_markdown_inline_br_becomes_newline(tag: str) -> None:
    text, entities = render_markdown(f"line one{tag}line two")

    assert text == "line one\nline two"
    assert entities == []


def test_render_markdown_br_inside_bold_keeps_entity() -> None:
    text, entities = render_markdown("**a<br>b**")

    assert text == "a\nb"
    assert entities == [{"type": "bold", "offset": 0, "length": 3}]


def test_render_markdown_br_only_paragraph_is_dropped() -> None:
    """The observed #786 shape: a `<br>` spacer paragraph between the
    answer and the footer."""
    text, _ = render_markdown("The agents are still running.\n\n<br>\n\n🏷 footer")

    assert "<br>" not in text
    assert text == "The agents are still running.\n\n🏷 footer"


def test_render_markdown_trailing_br_only_paragraph_dropped() -> None:
    text, _ = render_markdown("answer\n\n<br>")

    assert text == "answer"


def test_render_markdown_br_in_pipe_table_row_becomes_space() -> None:
    """commonmark renders no tables, so a pipe row stays one line of text —
    a space keeps the row readable where a newline would split the cell."""
    text, _ = render_markdown("| a<br>b | c |\n|---|---|\n| d | e |")

    assert "<br>" not in text
    assert "| a b | c |" in text


def test_render_markdown_br_in_code_span_preserved() -> None:
    text, entities = render_markdown("use `a<br>b` here")

    assert text == "use a<br>b here"
    assert {"type": "code", "offset": 4, "length": 6} in entities


def test_render_markdown_br_in_fenced_block_preserved() -> None:
    text, _ = render_markdown("```html\n<p>a<br>b</p>\n```")

    assert text == "<p>a<br>b</p>"


def test_render_markdown_br_in_indented_code_block_preserved() -> None:
    text, _ = render_markdown("para\n\n    a<br>b\n")

    assert text.endswith("a<br>b")


def test_render_markdown_other_html_still_literal() -> None:
    """#713 posture: only a bare <br> is normalised; every other tag —
    including <br> look-alikes and <br> with attributes — stays text."""
    text, _ = render_markdown('x <b>bold</b> <svg onload=1> <brx> <br class="c"> <br>y')

    assert "<b>bold</b>" in text
    assert "<svg onload=1>" in text
    assert "<brx>" in text
    assert '<br class="c">' in text
    assert text.endswith("\ny")


# ---------------------------------------------------------------------------
# #788 — bare filenames whose extension is a TLD (.md/.sh/.py) are rendered
# as inline code, so neither our linkify nor Telegram turns them into links
# ---------------------------------------------------------------------------


def _code_spans(text: str, entities: list[dict]) -> list[str]:
    """Return the text covered by each `code` entity (UTF-16 offsets are
    safe here because the test strings are ASCII)."""
    return [
        text[e["offset"] : e["offset"] + e["length"]]
        for e in entities
        if e.get("type") == "code"
    ]


def _link_urls(entities: list[dict]) -> list[str]:
    return [e["url"] for e in entities if e.get("type") == "text_link"]


@pytest.mark.parametrize(
    "name",
    [
        "CLAUDE.md",
        "AGENTS.md",
        "README.md",
        "setup.sh",
        "main.py",
        "src/untether/telegram/render.py",
        "~/.claude/CLAUDE.md",
        "docs/how-to/troubleshooting.md",
        "NOTES.MD",
    ],
)
def test_render_markdown_bare_filename_becomes_code(name: str) -> None:
    text, entities = render_markdown(f"Update {name} now")

    assert text == f"Update {name} now"
    assert _code_spans(text, entities) == [name]
    assert _link_urls(entities) == []


def test_render_markdown_filename_at_sentence_end_excludes_period() -> None:
    text, entities = render_markdown("Read CLAUDE.md. Then AGENTS.md, then run.sh!")

    assert _code_spans(text, entities) == ["CLAUDE.md", "AGENTS.md", "run.sh"]
    assert _link_urls(entities) == []


@pytest.mark.parametrize(
    ("md", "expected"),
    [
        ("see render.py:86 for it", ["render.py:86"]),
        ("see src/a.py:12:4 now", ["src/a.py:12:4"]),
        ("Edit CLAUDE.md: add a rule", ["CLAUDE.md"]),
        ("run ./scripts/staging.sh install", ["./scripts/staging.sh"]),
        ("(CLAUDE.md) and 'setup.sh'", ["CLAUDE.md", "setup.sh"]),
    ],
)
def test_render_markdown_filename_edge_shapes(md: str, expected: list[str]) -> None:
    text, entities = render_markdown(md)

    assert text == md
    assert _code_spans(text, entities) == expected
    assert _link_urls(entities) == []


def test_render_markdown_filename_in_bold_is_code_not_link() -> None:
    text, entities = render_markdown("**CLAUDE.md** changed")

    assert text == "CLAUDE.md changed"
    assert _code_spans(text, entities) == ["CLAUDE.md"]
    assert _link_urls(entities) == []


def test_render_markdown_filename_already_in_code_span_not_double_wrapped() -> None:
    text, entities = render_markdown("see `CLAUDE.md` here")

    assert text == "see CLAUDE.md here"
    assert entities == [{"type": "code", "offset": 4, "length": 9}]


def test_render_markdown_filename_in_fenced_block_untouched() -> None:
    text, entities = render_markdown("```\ncat CLAUDE.md\n```")

    assert text == "cat CLAUDE.md"
    assert [e["type"] for e in entities] == ["code", "pre"]


def test_render_markdown_raw_url_ending_in_md_stays_link() -> None:
    text, entities = render_markdown("see https://example.com/docs/a.md for more")

    assert text == "see https://example.com/docs/a.md for more"
    assert _link_urls(entities) == ["https://example.com/docs/a.md"]
    assert _code_spans(text, entities) == []


def test_render_markdown_markdown_link_to_md_file_left_alone() -> None:
    text, entities = render_markdown(
        "[CLAUDE.md](https://github.com/o/r/blob/main/CLAUDE.md)"
    )

    assert text == "CLAUDE.md"
    assert _link_urls(entities) == ["https://github.com/o/r/blob/main/CLAUDE.md"]
    assert _code_spans(text, entities) == []


def test_render_markdown_www_host_with_md_tld_stays_link() -> None:
    """An explicit `www.` host is a deliberate web address, not a file."""
    _, entities = render_markdown("visit www.example.md today")

    assert _link_urls(entities) == ["http://www.example.md"]


@pytest.mark.parametrize(
    "md",
    [
        "example.com is a domain",
        "docs.rs hosts crate docs",
        "foo.mdx and a.md5 and archive.md.bak",
    ],
)
def test_render_markdown_non_target_names_unchanged(md: str) -> None:
    text, entities = render_markdown(md)

    assert text == md
    assert _code_spans(text, entities) == []


# ---------------------------------------------------------------------------
# #797 — markdown pipe tables keep one row per line (commonmark has no table
# rule, so rows used to collapse into one run-on line of pipes)
# ---------------------------------------------------------------------------


def _bold_spans(text: str, entities: list[dict]) -> list[str]:
    return [
        text[e["offset"] : e["offset"] + e["length"]]
        for e in entities
        if e.get("type") == "bold"
    ]


def test_render_markdown_pipe_table_rows_stay_on_own_lines() -> None:
    """The issue's local repro."""
    text, entities = render_markdown(
        "Intro line.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n\nAfter."
    )

    assert text == "Intro line.\n\n| A | B |\n| 1 | 2 |\n| 3 | 4 |\n\nAfter."
    # The separator row carries no content; the header is bolded instead.
    assert _bold_spans(text, entities) == ["| A | B |"]


def test_render_markdown_pipe_table_live_example_research() -> None:
    """nsd-research msg 7736 (#797): a 3-column comparison table."""
    md = (
        "| What the report says | With Search Console + DataForSEO "
        "| DataForSEO only |\n"
        "|---|---|---|\n"
        "| Where you show up today | High: Google's own record "
        "| Directional: estimates |\n"
        "| Pages to build (demand with no page) | High | Medium: modelled |\n"
        "| Pages to improve (ranking but not well) | High: exact | Low: sampled |\n"
        "| Measuring results after changes | Possible | Not possible |"
    )
    text, _ = render_markdown(md)

    lines = text.split("\n")
    assert lines == [
        "| What the report says | With Search Console + DataForSEO | DataForSEO only |",
        "| Where you show up today | High: Google's own record "
        "| Directional: estimates |",
        "| Pages to build (demand with no page) | High | Medium: modelled |",
        "| Pages to improve (ranking but not well) | High: exact | Low: sampled |",
        "| Measuring results after changes | Possible | Not possible |",
    ]
    assert "---" not in text


def test_render_markdown_pipe_table_live_example_answers() -> None:
    """nsd-main msg 10234 (#797): 2 columns, underscores/parens in cells."""
    md = (
        "The four answers:\n\n"
        "| Answer | Meaning |\n"
        "|---|---|\n"
        "| Found them (VERIFIED_RELEVANT_PERSON) | A named person in a deciding role |\n"
        "| Role unclear (ROLE_UNRESOLVED) | Someone found, role not confirmed |\n"
        "| Looked, nobody listed (NOT_FOUND) | Searched, no one listed |\n"
        "| Never looked (NOT_CHECKED, now also NOT_RUN) | Not searched yet |\n\n"
        "Pick one."
    )
    text, entities = render_markdown(md)

    assert text == (
        "The four answers:\n\n"
        "| Answer | Meaning |\n"
        "| Found them (VERIFIED_RELEVANT_PERSON) | A named person in a deciding role |\n"
        "| Role unclear (ROLE_UNRESOLVED) | Someone found, role not confirmed |\n"
        "| Looked, nobody listed (NOT_FOUND) | Searched, no one listed |\n"
        "| Never looked (NOT_CHECKED, now also NOT_RUN) | Not searched yet |\n\n"
        "Pick one."
    )
    assert _bold_spans(text, entities) == ["| Answer | Meaning |"]


def test_render_markdown_pipe_table_inline_code_and_formatting_kept() -> None:
    md = "| Cmd | Effect |\n|:--|--:|\n| `a|b` | **bold** and CLAUDE.md |\n| `x` | y |"
    text, entities = render_markdown(md)

    assert text.split("\n") == [
        "| Cmd | Effect |",
        "| a|b | bold and CLAUDE.md |",
        "| x | y |",
    ]
    assert _code_spans(text, entities) == ["a|b", "CLAUDE.md", "x"]
    assert "bold" in _bold_spans(text, entities)


def test_render_markdown_pipe_table_header_already_bold_not_double_wrapped() -> None:
    text, entities = render_markdown("| **A** | B |\n|---|---|\n| 1 | 2 |")

    assert text == "| A | B |\n| 1 | 2 |"
    assert _bold_spans(text, entities) == ["A"]


def test_render_markdown_pipe_table_br_in_cell_stays_space() -> None:
    """#786 kept: a `<br>` in a cell is a space so the row isn't split."""
    text, _ = render_markdown("| a<br>b | c |\n|---|---|\n| d<br/>e | f |")

    assert text == "| a b | c |\n| d e | f |"


def test_render_markdown_pipe_table_directly_after_paragraph() -> None:
    """No blank line: commonmark folds the table into the paragraph."""
    text, _ = render_markdown(
        "Here is the comparison:\n| A | B |\n|---|---|\n| 1 | 2 |\nThat's it."
    )

    assert text == "Here is the comparison:\n| A | B |\n| 1 | 2 |\nThat's it."


def test_render_markdown_br_before_table_in_same_paragraph_single_break() -> None:
    text, _ = render_markdown("Intro<br>\n| A | B |\n|---|---|\n| 1 | 2 |")

    assert text == "Intro\n| A | B |\n| 1 | 2 |"


def test_render_markdown_pipe_table_without_outer_pipes() -> None:
    text, _ = render_markdown("A | B\n--- | ---\n1 | 2\n3 | 4")

    assert text == "A | B\n1 | 2\n3 | 4"


@pytest.mark.parametrize(
    "md",
    [
        # A pipe in prose: no delimiter row, so not a table.
        "Use a | b to pipe\nand then stop.",
        # A lone pipe-led line.
        "| not a table\nnext line",
        # Pipe-led rows with no delimiter row stay as they always were.
        "| a | b |\n| c | d |",
    ],
)
def test_render_markdown_pipe_lines_without_delimiter_not_a_table(md: str) -> None:
    text, entities = render_markdown(md)

    # #870: short lines keep their breaks; still never a table (no bold).
    assert text == md
    assert _bold_spans(text, entities) == []


def test_render_markdown_pipe_table_in_fenced_block_untouched() -> None:
    md = "```\n| A | B |\n|---|---|\n| 1 | 2 |\n```"
    text, entities = render_markdown(md)

    assert text == "| A | B |\n|---|---|\n| 1 | 2 |"
    assert _bold_spans(text, entities) == []


def test_render_markdown_pipe_table_html_stays_escaped() -> None:
    """#713 posture: only `<br>` is normalised, even inside a table."""
    text, _ = render_markdown("| <b>x</b> | <svg onload=1> |\n|---|---|\n| 1 | 2 |")

    assert text.split("\n")[0] == "| <b>x</b> | <svg onload=1> |"


def test_split_markdown_body_keeps_small_table_in_one_chunk() -> None:
    table = "| A | B |\n|---|---|\n" + "".join(f"| r{i} | v{i} |\n" for i in range(5))
    body = ("x" * 60) + "\n\n" + table + "\nafter"

    chunks = split_markdown_body(body, max_chars=len(table) + 10)

    assert any(table.rstrip("\n") in chunk for chunk in chunks)


def test_split_markdown_body_repeats_table_header_when_table_is_split() -> None:
    header = "| Key | Value |\n|---|---|\n"
    rows = "".join(f"| row{i:02d} | value{i:02d} |\n" for i in range(20))
    chunks = split_markdown_body(header + rows, max_chars=120)

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.startswith(header)
    # No row lost or duplicated across chunks.
    seen = [line for chunk in chunks for line in chunk.splitlines() if "row" in line]
    assert seen == rows.splitlines()
    # And each chunk renders as a table.
    for chunk in chunks:
        text, _ = render_markdown(chunk)
        assert "---" not in text
        assert all(line.startswith("|") for line in text.split("\n"))


def test_split_markdown_body_table_in_fence_not_given_header() -> None:
    body = "```\n| A | B |\n|---|---|\n" + "| x | y |\n" * 20 + "```"

    chunks = split_markdown_body(body, max_chars=80)

    assert len(chunks) > 1
    # Continuations reopen the fence but never gain a repeated table header.
    assert sum(chunk.count("| A | B |") for chunk in chunks) == 1


# ---------------------------------------------------------------------------
# #870 — softbreaks: keep author line structure, reflow wrapped prose
# ---------------------------------------------------------------------------
#
# The digest fixtures below are SYNTHETIC: invented, generic content shaped
# like the cron-digest finals described in #870 (short label lines without
# blank lines, ``Key: value`` lines, indented continuation lines, emoji-led
# status lines, lists next to plain lines, and hard-wrapped prose). No real
# user or cron output was copied.

_NB = "\u00a0"

_DIGEST_PROSE = (
    "The import job failed twice overnight because the upstream feed returned "
    "partial data, so the retry window was extended and the job will run again "
    "at the next scheduled slot without any manual intervention from the team "
    "on call this week."
)
SYNTH_NIGHTLY_DIGEST = "\n".join(
    [
        "🗂️ Nightly maintenance digest",
        "Run: 2026-01-15 02:00 UTC",
        "Host: build-01",
        "✅ Backups: 3 of 3 completed",
        "⚠️ Disk: /var at 81% (threshold 85%)",
        "  #101 rotate old log archives",
        "  #102 prune unused container images",
        "🐛 Open bugs (4)",
        "  #2041 login form loses focus on resize",
        "  #2047 export button stays disabled after a refresh",
        textwrap.fill(_DIGEST_PROSE, 74),
        "Next: review the queue depth tomorrow morning",
    ]
)
_STATUS_PROSE = (
    "The queue backlog grew after the deploy changed the batch size, and the "
    "workers have not caught up since then; scaling them out should clear the "
    "backlog within a day without affecting the other services that share the "
    "cluster."
)
SYNTH_SERVICE_STATUS = "\n".join(
    [
        "Service status, weekly summary",
        "Web frontend: healthy (p95 210 ms)",
        "Queue: degraded",
        "    backlog 1,240 jobs",
        "    oldest job 14 min",
        "Database: healthy",
        "🔁 Retries: 12 in the last 24h",
        "→ scale the queue workers from 2 to 4",
        "1. confirm the batch size change",
        "2. watch the backlog for an hour",
        "",
        textwrap.fill(_STATUS_PROSE, 72),
    ]
)
_CHECKLIST_PROSE = (
    "Notes: the migration adds a nullable column and backfills it in batches "
    "of five thousand rows, so it is safe to run during business hours and can "
    "be paused at any point by stopping the worker that drives the backfill loop."
)
SYNTH_RELEASE_CHECKLIST = "\n".join(
    [
        "Release checklist for v2.4.0",
        "[x] changelog updated",
        "[x] version bumped",
        "[ ] tag pushed",
        "(a) staging smoke test passed",
        "(b) production smoke test pending",
        "**Owner:** release team",
        "**Window**: Thursday 10:00 to 12:00",
        textwrap.fill(_CHECKLIST_PROSE, 76),
    ]
)


def test_870_softbreak_digest_keeps_lines() -> None:
    md = "🗂️ Action queue\n🐛 Bugs (47)\n  #3210 a\n  #3239 b\n🧪 Tests (3)\n  #1 one"
    text, _ = render_markdown(md)
    assert text.split("\n") == [
        "🗂️ Action queue",
        "🐛 Bugs (47)",
        f"{_NB * 2}#3210 a",
        f"{_NB * 2}#3239 b",
        "🧪 Tests (3)",
        f"{_NB * 2}#1 one",
    ]


def test_870_synthetic_nightly_digest() -> None:
    text, _ = render_markdown(SYNTH_NIGHTLY_DIGEST)
    assert text.split("\n") == [
        "🗂️ Nightly maintenance digest",
        "Run: 2026-01-15 02:00 UTC",
        "Host: build-01",
        "✅ Backups: 3 of 3 completed",
        "⚠️ Disk: /var at 81% (threshold 85%)",
        f"{_NB * 2}#101 rotate old log archives",
        f"{_NB * 2}#102 prune unused container images",
        "🐛 Open bugs (4)",
        f"{_NB * 2}#2041 login form loses focus on resize",
        f"{_NB * 2}#2047 export button stays disabled after a refresh",
        _DIGEST_PROSE,  # the hard-wrapped paragraph flows, not ragged
        "Next: review the queue depth tomorrow morning",
    ]


def test_870_synthetic_service_status() -> None:
    text, _ = render_markdown(SYNTH_SERVICE_STATUS)
    assert text.split("\n") == [
        "Service status, weekly summary",
        "Web frontend: healthy (p95 210 ms)",
        "Queue: degraded",
        f"{_NB * 4}backlog 1,240 jobs",
        f"{_NB * 4}oldest job 14 min",
        "Database: healthy",
        "🔁 Retries: 12 in the last 24h",
        "→ scale the queue workers from 2 to 4",
        "",  # "1." interrupts the paragraph and starts a real list
        "1. confirm the batch size change",
        "2. watch the backlog for an hour",
        "",
        _STATUS_PROSE,
    ]


def test_870_synthetic_release_checklist() -> None:
    text, entities = render_markdown(SYNTH_RELEASE_CHECKLIST)
    assert text.split("\n") == [
        "Release checklist for v2.4.0",
        "[x] changelog updated",
        "[x] version bumped",
        "[ ] tag pushed",
        "(a) staging smoke test passed",
        "(b) production smoke test pending",
        "Owner: release team",
        "Window: Thursday 10:00 to 12:00",
        _CHECKLIST_PROSE,
    ]
    assert _bold_spans(text, entities) == ["Owner:", "Window"]


def test_870_softbreak_key_value_block() -> None:
    text, _ = render_markdown("Status: green\nVersion: rc16\nNext: rc17")
    assert text == "Status: green\nVersion: rc16\nNext: rc17"


def test_870_softbreak_short_lines_keep_breaks() -> None:
    text, _ = render_markdown("src/a.py\nsrc/b/c.py\ndocs/x.md")
    assert text == "src/a.py\nsrc/b/c.py\ndocs/x.md"
    text, _ = render_markdown("Done.\nTests pass.\nPR opened.")
    assert text == "Done.\nTests pass.\nPR opened."


def test_870_softbreak_unmarked_long_items_case_rule() -> None:
    lines = [
        "Fixed the import order in main and all tests pass now everywhere",
        "Updated the changelog with the new release notes for the team today",
        "Bumped the version to the next release candidate for the next cycle",
        "Opened a pull request against the dev branch for review by the team",
    ]
    text, _ = render_markdown("\n".join(lines))
    assert text.split("\n") == lines


_TCP_PROSE = (
    "The network stack retransmits lost segments after a timeout expires, and "
    "the congestion window shrinks so that the sender backs off politely instead "
    "of flooding an already busy link with even more packets than it can carry, "
    "which keeps the whole path stable for every other flow that shares it, and "
    "once acknowledgements start arriving again the window grows slowly back "
    "towards its previous size until the next loss event or timeout occurs."
)


@pytest.mark.parametrize("width", [60, 72, 80, 100])
def test_870_softbreak_greedy_wrapped_prose_reflows(width: int) -> None:
    wrapped = textwrap.fill(_TCP_PROSE, width)
    assert "\n" in wrapped
    text, _ = render_markdown(wrapped)
    assert text == _TCP_PROSE


def test_870_wrapped_prose_capital_after_punctuation_reflows() -> None:
    prose = (
        "This sentence is long enough to be wrapped by an editor at the margin. "
        "The next sentence starts with a capital letter after a full stop and "
        "keeps going for a while longer."
    )
    text, _ = render_markdown(textwrap.fill(prose, 60))
    assert text == prose


def test_870_wrapped_prose_with_long_url_reflows() -> None:
    url = "https://example.com/a/very/long/path/that/no/wrapper/can/split/index.html"
    prose = (
        "The network stack retransmits lost segments after a timeout expires and "
        "the congestion window shrinks so that the sender backs off politely, see "
        f"{url} for the details of how the algorithm decides when to slow down "
        "and when to speed up again after the loss."
    )
    wrapped = textwrap.fill(prose, 72, break_long_words=False)
    assert any(len(line) > 72 for line in wrapped.split("\n"))
    text, _ = render_markdown(wrapped)
    assert "\n" not in text


def test_870_known_false_break_proper_noun_after_open_clause() -> None:
    # Accepted cost of rule (e) (#870 D1): a wrap that lands between an
    # unpunctuated clause and a capitalised proper noun keeps its break.
    prose = (
        "The pipeline runs on every push to the main branch, which means GitHub "
        "Actions has to start a fresh runner each time and download the cached "
        "dependencies before the tests begin."
    )
    wrapped = textwrap.fill(prose, 72)
    assert wrapped.split("\n")[1].startswith("Actions")
    text, _ = render_markdown(wrapped)
    assert text.count("\n") == 1
    assert text.split("\n")[1].startswith("Actions")


def test_870_softbreak_indent_nbsp_capped() -> None:
    text, _ = render_markdown("a\n" + " " * 12 + "deep")
    assert text == f"a\n{_NB * 8}deep"


def test_870_ordered_item_four_space_continuation_reflows() -> None:
    md = (
        "1. item with a fairly long first line that goes on and on for a while\n"
        "    continuation of the same item that is also long enough to matter"
    )
    text, _ = render_markdown(md)
    assert "\n" not in text
    assert _NB not in text


def test_870_softbreak_in_list_item_and_blockquote() -> None:
    text, _ = render_markdown("- item one\n  short\n  lines")
    assert text.count("\n") == 2
    text, entities = render_markdown("> a\n> b")
    assert text == "a\nb"
    assert [e["type"] for e in entities] == ["blockquote"]


def test_870_softbreak_inside_link_text_stays_space() -> None:
    text, entities = render_markdown(
        "See [the docs\nfor details](https://example.com) now."
    )
    assert text == "See the docs for details now."
    assert [e["type"] for e in entities] == ["text_link"]


def test_870_softbreak_inside_code_span_untouched() -> None:
    text, entities = render_markdown("`a\nb` c")
    assert text == "a b c"
    assert entities == [{"type": "code", "offset": 0, "length": 3}]


def test_870_softbreak_fenced_block_untouched() -> None:
    body = "x " * 60 + "\n" + "y " * 60
    text, entities = render_markdown(f"```\n{body}\n```")
    assert text.rstrip("\n").split("\n")[0] == ("x " * 60)
    assert {e["type"] for e in entities} == {"code", "pre"}


def test_870_pipe_table_after_paragraph_unchanged() -> None:
    text, _ = render_markdown("Intro\n| A | B |\n|---|---|\n| 1 | 2 |")
    assert text == "Intro\n| A | B |\n| 1 | 2 |"


def test_870_br_then_newline_single_break() -> None:
    text, _ = render_markdown("line<br>\nnext")
    assert text == "line\nnext"


def test_870_entity_offsets_after_nbsp_indent() -> None:
    text, entities = render_markdown("🐛 bugs\n  **#1** bold item")
    assert text == f"🐛 bugs\n{_NB * 2}#1 bold item"
    bold = [e for e in entities if e["type"] == "bold"]
    assert len(bold) == 1
    prefix = text[: text.index("#1")]
    assert bold[0]["offset"] == len(prefix.encode("utf-16-le")) // 2
    assert bold[0]["length"] == 2


def test_870_prepare_telegram_multiline_header_keeps_lines() -> None:
    from untether.markdown import MarkdownParts
    from untether.telegram.render import prepare_telegram

    text, _ = prepare_telegram(MarkdownParts(header="a\nb"))
    assert text == "a\nb"


def test_870_progress_multiline_note_title() -> None:
    text, _ = render_markdown("Permission Request [Bash]\n$ rm -rf /tmp/x")
    assert text == "Permission Request [Bash]\n$ rm -rf /tmp/x"


def test_870_code_span_crossing_lines_fallback() -> None:
    text, entities = render_markdown("🐛 one `x\ny` two\n🧪 three")
    assert text == "🐛 one x y two\n🧪 three"
    assert [e["type"] for e in entities] == ["code"]
