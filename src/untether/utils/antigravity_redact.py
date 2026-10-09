"""One redaction pass for every piece of agy text Untether surfaces (#558).

``redact_agy_text`` is the single choke point between agy's stderr /
``result.error`` / ``AGY_ERROR`` text and a Telegram card or an INFO+ log.
agy prints a Google sign-in URL (authorisation code, state token, PKCE
challenge) when it is signed out, and a terminal, a browser and Telegram each
recognise more URL shapes than any one regex does. So the pass is
conservative and never relies on spotting the URL:

1. cap the input, strip ANSI / control characters, undo ``&amp;`` and
   percent-encoded separators (so a secret can't hide behind an escape)
2. redact whole URLs — any scheme and case, ``//host/…``, scheme-less
   ``host.tld/…``
3. independently redact bearer tokens, query-string-shaped runs
   (``a=1&b=2``) and the value of every secret-looking ``key=value`` /
   ``"key": "value"`` pair, wherever they sit (a URL split across two
   stderr lines leaves its query on a line with no scheme)
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
_GOOGLE_TOKEN_RE = re.compile(r"\bya29\.[\w.~+/=-]+|\b1//[\w-]{10,}|\b4/[\w-]{10,}")
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
# OAuth parameter names that are ordinary words: redacted only as ``key=value``
# (``exit code: 3`` and ``state: ACTIVE`` stay readable).
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


def _redact_pairs(text: str) -> str:
    """Redact the value of every secret-looking pair. A non-secret key only
    consumes itself, so ``"status":"code=…"`` still gets its inner pair."""
    out: list[str] = []
    pos = 0
    while (match := _PAIR_RE.search(text, pos)) is not None:
        key = match.group(1).lower()
        sep = match.group(2)
        if any(fragment in key for fragment in _SECRET_FRAGMENTS) or (
            key in _SECRET_EXACT and ":" not in sep
        ):
            out.append(text[pos : match.end(2)])
            out.append("[redacted]")
            pos = match.end()
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
