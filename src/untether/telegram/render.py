from __future__ import annotations

import importlib.util
import logging
import re
from dataclasses import dataclass
from typing import Any
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


def _split_br(content: str, *, table: bool) -> list[Token]:
    """Split a text token's content on bare `<br>` tags (#786).

    Outside pipe tables each tag becomes a ``hardbreak`` (a real newline in
    Telegram); inside a pipe-table row it becomes a space so the row — which
    renders as a single line of text — isn't split mid-cell.
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


def _normalise_inline_children(inline: Token) -> bool:
    """Rewrite one inline token's children in place.

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
            child.type == "softbreak"
            and rewritten
            and rewritten[-1].type == "hardbreak"
        ):
            # `text<br>\nmore`: the tag already broke the line.
            continue
        rewritten.append(child)
    inline.children = rewritten
    return all(
        c.type in ("hardbreak", "softbreak")
        or (c.type == "text" and not c.content.strip())
        for c in rewritten
    )


def _normalise_tokens(tokens: list[Token]) -> list[Token]:
    out: list[Token] = []
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        nxt = tokens[idx + 1] if idx + 1 < len(tokens) else None
        if (
            tok.type == "inline"
            and _normalise_inline_children(tok)
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


def _split_block(block: str, max_chars: int) -> list[str]:
    if len(block) <= max_chars:
        return [block]
    pieces: list[str] = []
    current = ""
    for line in block.splitlines(keepends=True):
        for part in _split_long_line(line, max_chars):
            if not part:
                continue
            if current and len(current) + len(part) > max_chars:
                pieces.append(current)
                current = ""
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
        for piece in _split_block(block, max_chars):
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
