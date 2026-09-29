from __future__ import annotations

import importlib.util
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from markdown_it import MarkdownIt
from markdown_it.token import Token
from sulguk import transform_html

from ..markdown import MarkdownParts, assemble_markdown_parts

MAX_BODY_CHARS = 3500

if importlib.util.find_spec("linkify_it"):
    _MD_RENDERER = MarkdownIt("commonmark", {"html": False, "linkify": True}).enable(
        "linkify"
    )
else:
    logging.getLogger(__name__).warning(
        "linkify-it-py not available — URLs will not be auto-linked"
    )
    _MD_RENDERER = MarkdownIt("commonmark", {"html": False})
_BULLET_RE = re.compile(r"(?m)^(\s*)•")
_FENCE_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>[`~]{3,})(?P<info>.*)$")
_ORDERED_ITEM_RE = re.compile(r"^(?P<indent>[ \t]{0,3})(?P<marker>\d+[.)])\s+")
_UNORDERED_ITEM_RE = re.compile(r"^(?P<indent>[ \t]{0,3})[-+*]\s+")


@dataclass(frozen=True, slots=True)
class _FenceState:
    fence: str
    indent: str
    header: str


def _normalize_nested_list_markers(md: str) -> str:
    if not md:
        return md

    lines: list[str] = []
    ordered_indent: str | None = None
    fence_state: _FenceState | None = None

    for raw_line in md.splitlines(keepends=True):
        line, ending = _split_line_ending(raw_line)
        fence_state = _update_fence_state(line, fence_state)
        if fence_state is not None:
            ordered_indent = None
            lines.append(raw_line)
            continue

        if not line.strip():
            ordered_indent = None
            lines.append(raw_line)
            continue

        ordered_match = _ORDERED_ITEM_RE.match(line)
        if ordered_match is not None:
            ordered_indent = ordered_match.group("indent")
            lines.append(raw_line)
            continue

        if ordered_indent is not None:
            unordered_match = _UNORDERED_ITEM_RE.match(line)
            if (
                unordered_match is not None
                and unordered_match.group("indent") == ordered_indent
            ):
                lines.append(f"{ordered_indent}   {line}{ending}")
                continue

            if line.startswith(ordered_indent) and len(line) > len(ordered_indent):
                lines.append(raw_line)
                continue

            ordered_indent = None

        lines.append(raw_line)

    return "".join(lines)


# #786: a bare `<br>` / `<br/>` / `<br />` (any case, no attributes). Models
# use it as a line break or spacer; with `html: False` it would otherwise be
# escaped and shown literally. Nothing else HTML-shaped is let through (#713).
_BR_TAG_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _is_pipe_table_block(content: str) -> bool:
    """True when every non-blank line of an inline block looks like a pipe
    table row. commonmark renders no tables, so such rows stay plain text."""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    return bool(lines) and all(line.startswith("|") for line in lines)


def _text_token(content: str) -> Token:
    return Token("text", "", 0, content=content)


# #797: GFM pipe tables. commonmark has no table rule, so a table parses as
# one paragraph whose row boundaries are softbreaks — rendered as spaces, the
# whole table collapsed into one run-on line of pipes. A table is a line
# holding a `|` followed by a delimiter row (`|---|:-:|`, outer pipes
# optional); body rows are the following lines that hold a `|`. A pipe in
# prose, or pipe-led lines with no delimiter row, are not tables.
_TABLE_DELIM_CELL_RE = re.compile(r"\s*:?-+:?\s*")

_TableRole = Literal["header", "delim", "body"]


def _is_table_delimiter_row(line: str) -> bool:
    stripped = line.strip()
    if "|" not in stripped or "-" not in stripped:
        return False
    stripped = stripped.removeprefix("|")
    stripped = stripped.removesuffix("|")
    return all(_TABLE_DELIM_CELL_RE.fullmatch(cell) for cell in stripped.split("|"))


def _pipe_table_roles(lines: list[str]) -> list[_TableRole | None]:
    """Classify each line as a pipe-table header, delimiter or body row
    (None for anything else)."""
    roles: list[_TableRole | None] = [None] * len(lines)
    idx = 0
    while idx < len(lines) - 1:
        line = lines[idx]
        if (
            "|" in line
            and not _is_table_delimiter_row(line)
            and _is_table_delimiter_row(lines[idx + 1])
        ):
            roles[idx] = "header"
            roles[idx + 1] = "delim"
            idx += 2
            while idx < len(lines) and "|" in lines[idx] and lines[idx].strip():
                roles[idx] = "body"
                idx += 1
            continue
        idx += 1
    return roles


def _format_pipe_tables(inline: Token) -> None:
    """Keep each pipe-table row of an inline block on its own line (#797).

    Row boundaries become ``hardbreak`` s (so do the breaks just before and
    after the table when it shares a paragraph with prose), the delimiter
    row — which carries only column alignment — is dropped, and the header
    row is bolded so it still reads as a header. Cell text, inline code and
    other formatting are kept as they are; a `<br>` inside a row becomes a
    space so the row isn't split mid-cell (#786).

    Rows stay as ``| a | b |`` text rather than a monospace ``<pre>`` grid:
    real tables are usually wider than a phone's ~40 monospace columns, and
    ``<pre>`` would drop the entities (code, bold, links) inside cells.
    """
    children = inline.children
    if not children:
        return
    lines = inline.content.split("\n")
    roles = _pipe_table_roles(lines)
    if not any(roles):
        return
    # One segment per source line, each with the break token that ended it.
    segments: list[tuple[list[Token], Token | None]] = []
    current: list[Token] = []
    for child in children:
        if child.type in ("softbreak", "hardbreak"):
            segments.append((current, child))
            current = []
        else:
            current.append(child)
    segments.append((current, None))
    if len(segments) != len(lines):
        # A code span spanning lines hides a newline from the token stream;
        # rows can't be mapped reliably, so leave the block as it was.
        return

    rewritten: list[Token] = []
    prev: int | None = None
    for idx, (seg, _brk) in enumerate(segments):
        role = roles[idx]
        if role == "delim":
            continue
        if prev is not None:
            if role or roles[prev]:
                rewritten.append(Token("hardbreak", "br", 0))
            else:
                brk = segments[prev][1]
                if brk is not None:
                    rewritten.append(brk)
        if role:
            seg = [
                _text_token(_BR_TAG_RE.sub(" ", t.content))
                if t.type == "text" and _BR_TAG_RE.search(t.content)
                else t
                for t in seg
            ]
        if role == "header" and not any(t.type == "strong_open" for t in seg):
            seg = [
                Token("strong_open", "strong", 1, markup="**"),
                *seg,
                Token("strong_close", "strong", -1, markup="**"),
            ]
        rewritten.extend(seg)
        prev = idx
    inline.children = rewritten


def _split_br(content: str, *, table: bool) -> list[Token]:
    """Split a text token's content on bare `<br>` tags (#786).

    Outside pipe tables each tag becomes a ``hardbreak`` (a real newline in
    Telegram); inside a pipe-table row it becomes a space so the row — which
    renders as one line of text (#797) — isn't split mid-cell.
    """
    if table:
        return [_text_token(_BR_TAG_RE.sub(" ", content))]
    parts = _BR_TAG_RE.split(content)
    out: list[Token] = []
    last = len(parts) - 1
    for idx, part in enumerate(parts):
        if idx > 0:
            part = part.lstrip(" ")
        if idx < last:
            part = part.rstrip(" ")
        if part:
            out.append(_text_token(part))
        if idx < last:
            out.append(Token("hardbreak", "br", 0))
    return out


def _rewrite_br(inline: Token) -> bool:
    """Rewrite `<br>` tags in one inline token's children, in place (#786).

    Only ``text`` children are touched, so code spans (``code_inline``) and
    code blocks (``fence`` / ``code_block``, which are block tokens with no
    inline children) keep their content verbatim.

    Returns True when the block was nothing but `<br>` tags (a spacer
    paragraph) and should be dropped.
    """
    children = inline.children
    if not children or not any(
        c.type == "text" and _BR_TAG_RE.search(c.content) for c in children
    ):
        return False
    table = _is_pipe_table_block(inline.content)
    rewritten: list[Token] = []
    for child in children:
        if child.type == "text" and _BR_TAG_RE.search(child.content):
            rewritten.extend(_split_br(child.content, table=table))
            continue
        if (
            child.type in ("softbreak", "hardbreak")
            and rewritten
            and rewritten[-1].type == "hardbreak"
        ):
            # `text<br>\nmore`: the tag already broke the line (the newline
            # may already be a hardbreak when a table row follows, #797).
            continue
        rewritten.append(child)
    inline.children = rewritten
    return all(
        c.type in ("hardbreak", "softbreak")
        or (c.type == "text" and not c.content.strip())
        for c in rewritten
    )


# #788: a bare filename whose extension is also a country-code TLD. Telegram
# clients (and our own linkify pass) turn `CLAUDE.md` into http://claude.md/,
# `setup.sh` into http://setup.sh/ and so on; a code entity is never linked.
# Only md/sh/py: other source extensions that are TLDs (.rs, .pl, .cc, .ps)
# collide with real, frequently cited domains (docs.rs, allegro.pl), so
# wrapping them would break genuine links.
#
# The lookbehind refuses to start a match mid-token — after a scheme colon,
# a path separator, `@` (e-mail), `=` / `%` / `+` (query strings) — so text
# that is part of a URL is never wrapped. The lookahead refuses a trailing
# path, word char or further `.ext` (`a.md5`, `a.md.bak`, `a.mdx`) but lets
# sentence punctuation (`CLAUDE.md.`, `run.sh!`) end the match. A trailing
# `:line[:col]` (`render.py:86`) stays inside the code span — linkify would
# otherwise read it as a port and link http://render.py:86.
_FILENAME_EXTS = ("md", "sh", "py")
_BARE_FILENAME_RE = re.compile(
    r"(?<![\w./:@~%+=-])"
    r"(~?[\w./-]*[\w-]\.(?:" + "|".join(_FILENAME_EXTS) + r")(?::\d+){0,2})"
    r"(?![\w/-]|\.\w|:\d)",
    re.IGNORECASE,
)


def _code_token(content: str) -> Token:
    return Token("code_inline", "code", 0, content=content, markup="`")


def _split_filenames(content: str) -> list[Token] | None:
    """Split text around bare filenames, wrapping each in a code span.
    Returns None when there is nothing to wrap."""
    matches = list(_BARE_FILENAME_RE.finditer(content))
    if not matches:
        return None
    out: list[Token] = []
    pos = 0
    for match in matches:
        if match.start() > pos:
            out.append(_text_token(content[pos : match.start()]))
        out.append(_code_token(match.group(1)))
        pos = match.end()
    if pos < len(content):
        out.append(_text_token(content[pos:]))
    return out


def _is_fuzzy_filename_link(link_open: Token, text: Token) -> bool:
    """A linkify link produced from a bare filename (`CLAUDE.md` →
    http://CLAUDE.md): no scheme or `www.` was written, and the link text is
    exactly one bare filename."""
    if link_open.markup != "linkify" or text.type != "text":
        return False
    href = str(link_open.attrs.get("href", ""))
    content = text.content
    if content.lower().startswith("www."):
        return False
    if href.lower() != f"http://{content}".lower():
        return False
    return _BARE_FILENAME_RE.fullmatch(content) is not None


def _code_format_filenames(inline: Token) -> None:
    """Render bare `name.md` / `.sh` / `.py` filenames as inline code (#788).

    Works on the token stream, so existing code spans (``code_inline``) and
    code blocks are never touched or double-wrapped, text inside explicit
    markdown links is left alone, and real URLs — which linkify already
    turned into link tokens — keep their links. Linkify links that were
    generated from a bare filename are unwrapped into a code span.
    """
    children = inline.children
    if not children:
        return
    rewritten: list[Token] = []
    link_depth = 0
    idx = 0
    while idx < len(children):
        child = children[idx]
        if (
            child.type == "link_open"
            and idx + 2 < len(children)
            and children[idx + 2].type == "link_close"
            and _is_fuzzy_filename_link(child, children[idx + 1])
        ):
            rewritten.append(_code_token(children[idx + 1].content))
            idx += 3
            continue
        if child.type == "link_open":
            link_depth += 1
        elif child.type == "link_close":
            link_depth = max(0, link_depth - 1)
        elif child.type == "text" and link_depth == 0:
            split = _split_filenames(child.content)
            if split is not None:
                rewritten.extend(split)
                idx += 1
                continue
        rewritten.append(child)
        idx += 1
    inline.children = rewritten


def _normalise_tokens(tokens: list[Token]) -> list[Token]:
    out: list[Token] = []
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        nxt = tokens[idx + 1] if idx + 1 < len(tokens) else None
        if tok.type == "inline":
            _format_pipe_tables(tok)
            drop = _rewrite_br(tok)
            _code_format_filenames(tok)
        else:
            drop = False
        if (
            drop
            and out
            and out[-1].type == "paragraph_open"
            and nxt is not None
            and nxt.type == "paragraph_close"
        ):
            # Drop a paragraph that held only `<br>` spacers (#786).
            out.pop()
            idx += 2
            continue
        out.append(tok)
        idx += 1
    return out


def _render_html(md: str) -> str:
    env: dict[str, Any] = {}
    tokens = _normalise_tokens(_MD_RENDERER.parse(md, env))
    return _MD_RENDERER.renderer.render(tokens, _MD_RENDERER.options, env)


def render_markdown(md: str) -> tuple[str, list[dict[str, Any]]]:
    html = _render_html(_normalize_nested_list_markers(md or ""))
    rendered = transform_html(html)

    text = _BULLET_RE.sub(r"\1-", rendered.text)
    # sulguk adds trailing \n\n after the last paragraph; strip it so
    # post-render appends (cost footer, usage) don't get double-spaced.
    text = text.rstrip("\n")

    # Clamp entities to new text length — sulguk may reference stripped trailing \n
    text_utf16_len = len(text.encode("utf-16-le")) // 2
    entities: list[dict[str, Any]] = []
    for e in rendered.entities:
        ed = dict(e)
        offset = ed.get("offset", 0)
        length = ed.get("length", 0)
        if offset >= text_utf16_len:
            continue
        if offset + length > text_utf16_len:
            ed["length"] = text_utf16_len - offset
        entities.append(ed)
    entities = _sanitise_entities(entities)
    return text, entities


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})  # nosec B104


def _is_telegram_safe_url(url: str) -> bool:
    """Check if a URL is safe for Telegram ``text_link`` entities.

    Telegram rejects localhost, loopback, bare hostnames, file paths,
    and non-HTTP(S) schemes with 400 Bad Request.  (#157)
    """
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname or ""
    if not host:
        return False
    if host in _LOOPBACK_HOSTS:
        return False
    # Bare hostnames (no dot) are rejected by Telegram
    return "." in host


def _sanitise_entities(
    entities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert ``text_link`` entities with invalid URLs to ``code``.

    Telegram's sendMessage API rejects the entire request if any
    ``text_link`` entity has a URL it considers invalid (localhost,
    file paths, bare hostnames).  Converting to ``code`` preserves
    the text visually while avoiding the 400 error.  (#157)
    """
    sanitised: list[dict[str, Any]] = []
    for e in entities:
        if e.get("type") == "text_link" and not _is_telegram_safe_url(e.get("url", "")):
            sanitised.append(
                {
                    "type": "code",
                    "offset": e["offset"],
                    "length": e["length"],
                }
            )
            continue
        sanitised.append(e)
    return sanitised


def _split_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    if line.endswith("\r"):
        return line[:-1], "\r"
    return line, ""


def _split_long_line(line: str, max_chars: int) -> list[str]:
    if len(line) <= max_chars:
        return [line]
    content, ending = _split_line_ending(line)
    parts: list[str] = []
    for idx in range(0, len(content), max_chars):
        chunk = content[idx : idx + max_chars]
        if idx + max_chars >= len(content):
            chunk += ending
        parts.append(chunk)
    if not parts and ending:
        parts.append(ending)
    return parts


def _table_header_prefixes(
    lines: list[str], fence: _FenceState | None
) -> list[str | None]:
    """For each line that is a pipe-table body row outside a code fence,
    the table's header + delimiter lines; None otherwise (#797)."""
    stripped = [_split_line_ending(line)[0] for line in lines]
    in_fence: list[bool] = []
    for line in stripped:
        was_open = fence is not None
        fence = _update_fence_state(line, fence)
        # A fence marker line and everything inside a fence are code.
        in_fence.append(was_open or fence is not None)
    roles = _pipe_table_roles(
        [
            "" if fenced else line
            for line, fenced in zip(stripped, in_fence, strict=True)
        ]
    )
    prefixes: list[str | None] = [None] * len(lines)
    header = ""
    for idx, role in enumerate(roles):
        if role == "header":
            header = stripped[idx] + "\n" + stripped[idx + 1] + "\n"
        elif role == "body":
            prefixes[idx] = header
    return prefixes


def _split_block(
    block: str, max_chars: int, fence: _FenceState | None = None
) -> list[str]:
    if len(block) <= max_chars:
        return [block]
    lines = block.splitlines(keepends=True)
    table_prefixes = _table_header_prefixes(lines, fence)
    pieces: list[str] = []
    current = ""
    for line, table_prefix in zip(lines, table_prefixes, strict=True):
        for part_idx, part in enumerate(_split_long_line(line, max_chars)):
            if not part:
                continue
            if current and len(current) + len(part) > max_chars:
                pieces.append(current)
                current = ""
            if (
                not current
                and pieces
                and part_idx == 0
                and table_prefix
                and len(table_prefix) <= max_chars // 2
                and len(table_prefix) + len(part) <= max_chars
            ):
                # A table split across chunks: repeat its header so the
                # continuation still renders as a table (#797).
                current = table_prefix
            current += part
            if len(current) == max_chars:
                pieces.append(current)
                current = ""
    if current:
        pieces.append(current)
    return pieces


def _update_fence_state(line: str, state: _FenceState | None) -> _FenceState | None:
    match = _FENCE_RE.match(line)
    if match is None:
        return state
    fence = match.group("fence")
    indent = match.group("indent")
    if state is None:
        return _FenceState(fence=fence, indent=indent, header=line)
    if fence[0] == state.fence[0] and len(fence) >= len(state.fence):
        return None
    return state


def _scan_fence_state(text: str, state: _FenceState | None) -> _FenceState | None:
    for line in text.splitlines():
        state = _update_fence_state(line, state)
    return state


def _ensure_trailing_newline(text: str) -> str:
    if text.endswith(("\n", "\r")):
        return text
    return text + "\n"


def _close_fence_chunk(text: str, state: _FenceState) -> str:
    return _ensure_trailing_newline(text) + f"{state.indent}{state.fence}\n"


def _reopen_fence_prefix(state: _FenceState) -> str:
    return f"{state.header}\n"


def split_markdown_body(body: str, max_chars: int) -> list[str]:
    if not body or not body.strip():
        return []
    max_chars = max(1, int(max_chars))
    segments = re.split(r"(\n{2,})", body)
    blocks: list[str] = []
    for idx in range(0, len(segments), 2):
        paragraph = segments[idx]
        separator = segments[idx + 1] if idx + 1 < len(segments) else ""
        block = paragraph + separator
        if block:
            blocks.append(block)

    chunks: list[str] = []
    current = ""
    state: _FenceState | None = None
    for block in blocks:
        for piece in _split_block(block, max_chars, state):
            if not current:
                current = piece
                state = _scan_fence_state(piece, state)
                continue
            if len(current) + len(piece) <= max_chars:
                current += piece
                state = _scan_fence_state(piece, state)
                continue

            if state is not None:
                current = _close_fence_chunk(current, state)
            chunks.append(current)
            current = _reopen_fence_prefix(state) if state is not None else ""
            current += piece
            state = _scan_fence_state(piece, state)

    if current:
        chunks.append(current)

    return [chunk for chunk in chunks if chunk.strip()]


def trim_body(body: str | None, *, max_chars: int = MAX_BODY_CHARS) -> str | None:
    if not body:
        return None
    if len(body) > max_chars:
        body = body[: max_chars - 1] + "…"
    return body if body.strip() else None


def prepare_telegram(parts: MarkdownParts) -> tuple[str, list[dict[str, Any]]]:
    trimmed = MarkdownParts(
        header=parts.header or "",
        body=trim_body(parts.body, max_chars=MAX_BODY_CHARS),
        footer=parts.footer,
    )
    return render_markdown(assemble_markdown_parts(trimmed))


def prepare_telegram_multi(
    parts: MarkdownParts, *, max_body_chars: int = MAX_BODY_CHARS
) -> list[tuple[str, list[dict[str, Any]]]]:
    body = parts.body
    if body is not None and not body.strip():
        body = None
    body_chunks = split_markdown_body(body, max_body_chars) if body is not None else []
    if not body_chunks:
        body_chunks = [""]
    total = len(body_chunks)

    payloads: list[tuple[str, list[dict[str, Any]]]] = []
    for idx, chunk in enumerate(body_chunks, start=1):
        header = parts.header or ""
        if idx > 1:
            if header:
                header = f"{header} · continued ({idx}/{total})"
            else:
                header = f"continued ({idx}/{total})"
        payloads.append(
            render_markdown(
                assemble_markdown_parts(
                    MarkdownParts(
                        header=header,
                        body=chunk,
                        footer=parts.footer if idx == total else None,
                    )
                )
            )
        )
    return payloads
