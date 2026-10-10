"""One redaction pass for every piece of agy text Untether surfaces (#558).

``redact_agy_text`` is the single choke point between agy's stderr /
``result.error`` / ``AGY_ERROR`` text and a Telegram card or an INFO+ log.
agy prints a Google sign-in URL (authorisation code, state token, PKCE
challenge) when it is signed out, and a terminal, a browser and Telegram each
recognise more URL shapes than any one regex does. So the pass is
conservative and never relies on spotting the URL:

1. cap the input, strip ANSI / control characters, undo ``&amp;``, JSON
   ``\\u0026`` / ``\\u003d`` and percent-encoded separators (so a secret
   can't hide behind an escape)
2. redact whole URLs — any scheme and case, ``//host/…``, scheme-less
   ``host.tld/…``
3. independently redact bearer tokens, bare Google credential shapes (API
   keys, client secrets, JWTs), query-string-shaped runs (``a=1&b=2``) and
   the value of every secret-looking ``key=value`` / ``"key": "value"``
   pair, wherever they sit (a URL split across two stderr lines leaves its
   query on a line with no scheme). A secret's value runs to its closing
   quote or bracket; an ``Authorization`` / ``Cookie`` header loses the
   rest of its line
4. absolute paths, via the shared sanitiser

The shared ``runner._sanitise_stderr`` is deliberately not used on its own
for agy: it redacts paths before URLs, so its generic path pattern eats
``//host/path`` and the query string survives.
"""

from __future__ import annotations

import re

_MAX_CHARS = 16384

# CSI, OSC (BEL- or ST-terminated), then any other escape / control char
# except newline and tab.
_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[@-Z\\-_]?"
    r"|[\x00-\x08\x0b-\x1f\x7f]"
)
_HTML_AMP_RE = re.compile(r"&(?:amp|#38|#x26);", re.IGNORECASE)
# ``&`` and ``=`` as Go's json.Marshal (and a JSON-in-JSON string) writes them.
_JSON_SEP_RE = re.compile(r"\\+u00(26|3d)", re.IGNORECASE)
_JSON_SEP_MAP = {"26": "&", "3d": "="}
_PCT_RE = re.compile(r"%(3a|2f|3f|3d|26|23|40|25)", re.IGNORECASE)
_PCT_MAP = {
    "3a": ":",
    "2f": "/",
    "3f": "?",
    "3d": "=",
    "26": "&",
    "23": "#",
    "40": "@",
    "25": "%",
}

_STOP = r"\s\"'<>"
_SCHEME_URL_RE = re.compile(rf"[a-z][a-z0-9+.-]{{1,20}}://[^{_STOP}]*", re.IGNORECASE)
_RELATIVE_URL_RE = re.compile(rf"(?<![\w:/])//[^{_STOP}/][^{_STOP}]*")
_BARE_HOST_URL_RE = re.compile(
    r"(?<![\w@.-])(?:[a-z0-9][a-z0-9-]{0,62}\.){1,8}[a-z]{2,24}(?::\d{1,5})?"
    rf"[/?#][^{_STOP}]*",
    re.IGNORECASE,
)

_BEARER_RE = re.compile(r"\b(bearer|basic)\s+[^\s\"'<>,;]+", re.IGNORECASE)
_GOOGLE_TOKEN_RE = re.compile(
    r"\bya29\.[\w.~+/=-]+|\b1//[\w-]{10,}|\b4/[\w-]{10,}"
    r"|\bAIza[\w-]{10,}|\bGOCSPX-[\w-]+"
    r"|\beyJ[\w-]{5,}\.[\w-]+(?:\.[\w-]+)?"
)
_KEY = r"[\w.%~+-]{1,64}"
_VALUE = rf"[^{_STOP}&]*"
_QUERY_RUN_RE = re.compile(rf"[?&#]?{_KEY}={_VALUE}(?:&(?:{_KEY}={_VALUE})?)+")
_PAIR_RE = re.compile(
    r"([A-Za-z_][\w.-]{0,63})"
    r"([\"']?\s*[=:]\s*[\"']?)"
    r"([^\s&\"'<>,;)}\]]+)"
)
# Any key containing one of these is a secret, with ``=`` or ``:``.
_SECRET_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "apikey",
    "api_key",
    "api-key",
    "challenge",
    "verifier",
    "signature",
    "assertion",
    "authorization",
    "cookie",
)
# OAuth parameter names that are ordinary words: redacted as ``key=value`` or
# as a quoted JSON key (``"code": "…"``); ``exit code: 3`` and
# ``state: ACTIVE`` stay readable.
_SECRET_EXACT = frozenset(
    {
        "code",
        "state",
        "key",
        "auth",
        "nonce",
        "sig",
        "client_id",
        "session_state",
        "login_hint",
        "authuser",
    }
)


# Header-style keys whose value is several words (``Token abc``, ``a=1; b=2``).
_LINE_VALUE_FRAGMENTS = ("authorization", "cookie")
_CLOSERS = {"[": "]", "{": "}"}


def _value_end(text: str, key: str, sep: str, start: int, default: int) -> int:
    """Where a secret's value ends: the rest of the line for a header, the
    closing quote of a quoted value, the matching bracket of a JSON list or
    object; *default* (the first token) otherwise."""
    line_end = text.find("\n", start)
    if line_end == -1:
        line_end = len(text)
    if any(fragment in key for fragment in _LINE_VALUE_FRAGMENTS):
        return line_end
    if sep[-1] in "\"'":
        close = text.find(sep[-1], start, line_end)
        return line_end if close == -1 else close
    opener = text[start]
    if opener in _CLOSERS:
        depth = 0
        for index in range(start, line_end):
            if text[index] == opener:
                depth += 1
            elif text[index] == _CLOSERS[opener]:
                depth -= 1
                if depth == 0:
                    return index + 1
        return line_end
    return default


def _redact_pairs(text: str) -> str:
    """Redact the value of every secret-looking pair. A non-secret key only
    consumes itself, so ``"status":"code=…"`` still gets its inner pair."""
    out: list[str] = []
    pos = 0
    while (match := _PAIR_RE.search(text, pos)) is not None:
        key = match.group(1).lower()
        sep = match.group(2)
        if any(fragment in key for fragment in _SECRET_FRAGMENTS) or (
            key in _SECRET_EXACT
            # ``"code": "4/0A…"`` is a secret; ``"code": 429`` is a status.
            and (":" not in sep or (sep[0] in "\"'" and sep[-1] in "\"'"))
        ):
            out.append(text[pos : match.end(2)])
            out.append("[redacted]")
            pos = _value_end(text, key, sep, match.end(2), match.end())
        else:
            out.append(text[pos : match.end(1)])
            pos = match.end(1)
    out.append(text[pos:])
    return "".join(out)


def redact_agy_text(text: str) -> str:
    """agy text made safe for a Telegram card or an INFO+ log (see module
    docstring). Redact first, truncate afterwards."""
    from ..runner import _sanitise_stderr

    text = _ANSI_RE.sub("", text[:_MAX_CHARS])
    text = _HTML_AMP_RE.sub("&", text)
    text = _JSON_SEP_RE.sub(lambda m: _JSON_SEP_MAP[m.group(1).lower()], text)
    for _ in range(2):  # single and double encoding
        text = _PCT_RE.sub(lambda m: _PCT_MAP[m.group(1).lower()], text)
    text = _SCHEME_URL_RE.sub("[url]", text)
    text = _RELATIVE_URL_RE.sub("[url]", text)
    text = _BARE_HOST_URL_RE.sub("[url]", text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} [redacted]", text)
    text = _GOOGLE_TOKEN_RE.sub("[redacted]", text)
    text = _QUERY_RUN_RE.sub("[redacted]", text)
    text = _redact_pairs(text)
    return _sanitise_stderr(text)
