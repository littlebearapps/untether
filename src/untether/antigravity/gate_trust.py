""" "Trust current config" for Ask me (#1032).

Ask me refuses while agy loads hook groups, plugins, agents or MCP servers
that Untether doesn't manage (``gate_foreign_config``). A *trust record*
(``utils/antigravity_state.TrustStore``) lets an attended Ask me run in one
chat and project go ahead while that exact set is unchanged. This module
computes the set's fingerprint and holds the one-shot offers a refusal
makes. :func:`runners.antigravity.decide_antigravity_mode` stays the only
place that grants the bypass flag.

Fails closed: whatever can't be fingerprinted completely is
:class:`Untrustable` (no offer, a stored record isn't honoured):

- the planted-config scan is unfinished (``scan_incomplete``);
- ``agy mcp list`` failed (``mcp_unknown``) or agy's version is unknown
  (``version_unknown``; every agy update asks again, D2);
- another group has a ``PreToolUse`` action, enabled or not: agy runs
  same-event hooks in sequence and a PreToolUse answer can approve or
  rewrite (``overwrite``) the call after Untether's gate (D1);
- a custom agent's front matter has a ``hooks`` key or is ambiguous
  (``agent_hooks``), or the agent can't be read (``agent_unreadable``, every
  user-level agent: D33 never reads ``~/.gemini``). agy's ``-p /hooks`` never
  lists an agent's hooks (probe 1032-0 P-T1, agy 1.5.18), so nothing
  vouches for them.

No native agy feature pins loaded config (agy's own "workspace trust" is a
TUI prompt that decides whether a folder's config loads at all), so this is
custom (register row in ``docs/reference/runners/antigravity/runner.md``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..logging import get_logger
from ..utils import antigravity_scan
from .gate_precheck import GatePrecheck

logger = get_logger(__name__)

FINGERPRINT_VERSION = 1
SCAN_INCOMPLETE = "scan_incomplete"
MCP_UNKNOWN = "mcp_unknown"
VERSION_UNKNOWN = "version_unknown"
FOREIGN_PRETOOLUSE = "foreign_pretooluse"
AGENT_HOOKS = "agent_hooks"
AGENT_UNREADABLE = "agent_unreadable"
_LIST_MAX = 50
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class Fingerprint:
    digest: str  # sha256 hex
    paths: tuple[str, ...]  # the foreign config, as the refusal names it
    groups: tuple[str, ...]  # "<name> (<source>)" of each other hook group
    agy_version: str

    @property
    def short(self) -> str:
        return self.digest[:12]


@dataclass(frozen=True, slots=True)
class Untrustable:
    reason: str
    detail: str = ""


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(
    *,
    cwd: Path | None,
    scan: antigravity_scan.ScanResult | None,
    precheck: GatePrecheck,
    foreign: tuple[str, ...],
    agy_version: str | None,
    mcp: str | None,
    agent_problem: tuple[str, str] | None,
) -> Fingerprint | Untrustable:
    """Pure. ``mcp`` is :func:`gate_precheck.user_mcp_digest`'s answer
    (``"none"`` when agy has no user-level MCP servers, None when the listing
    failed); ``agent_problem`` is :func:`agent_problem`'s."""
    if cwd is None or scan is None or scan.unchecked_reason is not None:
        why = scan.unchecked_reason if scan is not None else None
        return Untrustable(SCAN_INCOMPLETE, why or "")
    if mcp is None:
        return Untrustable(MCP_UNKNOWN)
    if not agy_version:
        return Untrustable(VERSION_UNKNOWN)
    if precheck.foreign_pretooluse:
        return Untrustable(FOREIGN_PRETOOLUSE, ", ".join(precheck.foreign_pretooluse))
    if agent_problem is not None:
        return Untrustable(*agent_problem)
    hooks = sorted(
        _canon(
            {
                "name": g.name,
                "enabled": g.enabled,
                "source": g.source,
                "actions": list(g.actions),
            }
        )
        for g in precheck.foreign_groups
    )
    payload = {
        "v": FINGERPRINT_VERSION,
        "cwd": os.path.realpath(cwd),
        "root": os.path.realpath(scan.root),
        "agy_version": agy_version,
        "agy_digest": scan.agy_digest,
        "hooks": _sha(_canon(hooks)),
        "mcp": mcp,
    }
    groups = sorted({f"{g.name} ({g.source})" for g in precheck.foreign_groups})
    return Fingerprint(
        digest=_sha(_canon(payload)),
        paths=tuple(foreign)[:_LIST_MAX],
        groups=tuple(groups)[:_LIST_MAX],
        agy_version=agy_version,
    )


def _read_regular(path: str) -> bytes | None:
    """The whole file, never blocking on a FIFO or device; None when it
    isn't a readable regular file or is over the scan's parse cap."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
    cap = antigravity_scan._REF_PARSE_CAP_BYTES
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = bytearray()
        while len(data) <= cap:
            chunk = os.read(fd, 65536)
            if not chunk:
                return bytes(data)
            data.extend(chunk)
        return None
    except OSError:
        return None
    finally:
        os.close(fd)


def _has_hooks(data: bytes) -> bool:
    lines = antigravity_scan._front_matter(data)
    if lines is None:
        return True  # agy's reader might see a hooks key this one doesn't
    for line in lines:
        match = antigravity_scan._FRONT_KEY.match(line)
        if match is not None and antigravity_scan._fold(match.group(1)) == "hooks":
            return True
    return antigravity_scan._front_matter_paths(lines)[2]


def agent_problem(scan: antigravity_scan.ScanResult) -> tuple[str, str] | None:
    """The first custom agent (any ``.md`` in the scan's agy set) that rules
    trust out, as ``(reason, path)``. Blocking (reads project agent files):
    run it off the event loop. The bytes read must hash to what the scan
    recorded, so a file swapped since the scan never passes."""
    root_real = os.path.realpath(scan.root)
    home_real = os.path.realpath(Path.home())
    for key in sorted(scan.agy):
        if not antigravity_scan._fold(key).endswith(".md"):
            continue
        if key.startswith(antigravity_scan.USER_DISPLAY_PREFIX):
            return (AGENT_UNREADABLE, key)
        if key.startswith("~/"):
            path = os.path.join(home_real, key[2:])
        elif os.path.isabs(key):
            path = key
        else:
            path = os.path.join(root_real, key)
        recorded = str(scan.agy[key][1]).split(":link:", 1)[0]
        data = _read_regular(path) if _SHA256.fullmatch(recorded) else None
        if data is None or hashlib.sha256(data).hexdigest() != recorded:
            return (AGENT_UNREADABLE, key)
        if _has_hooks(data):
            return (AGENT_HOOKS, key)
    return None


# ── one-shot offers (``budget_gate.register_pending_run`` shape) ────────────

OFFER_MAX = 64
OFFER_TTL_S = 3600.0


@dataclass(frozen=True, slots=True)
class Offer:
    chat_id: int
    root: str
    fingerprint: Fingerprint
    created: float


_OFFERS: dict[str, Offer] = {}


def _prune(now: float) -> None:
    for token in [t for t, o in _OFFERS.items() if now - o.created > OFFER_TTL_S]:
        del _OFFERS[token]
    while len(_OFFERS) >= OFFER_MAX:
        del _OFFERS[next(iter(_OFFERS))]


def register_offer(chat_id: int, root: str, fp: Fingerprint) -> str:
    """Remember what a refusal showed; returns the token for its button."""
    now = time.monotonic()
    _prune(now)
    token = secrets.token_hex(4)
    while token in _OFFERS:
        token = secrets.token_hex(4)
    _OFFERS[token] = Offer(chat_id=chat_id, root=root, fingerprint=fp, created=now)
    return token


def _live(token: str, chat_id: int) -> Offer | None:
    offer = _OFFERS.get(token)
    if offer is None:
        return None
    if offer.chat_id != chat_id:
        logger.warning(
            "antigravity.config_trust.offer_foreign_chat",
            bound_chat_id=offer.chat_id,
            chat_id=chat_id,
        )
        return None
    if time.monotonic() - offer.created > OFFER_TTL_S:
        del _OFFERS[token]
        return None
    return offer


def peek_offer(token: str, *, chat_id: int) -> Offer | None:
    """The offer, left in place (the confirm step)."""
    return _live(token, chat_id)


def claim_offer(token: str, *, chat_id: int) -> Offer | None:
    """Take the offer once. Another chat's tap reads as expired and leaves
    it in place (callbacks are bound to their chat, #388)."""
    offer = _live(token, chat_id)
    if offer is not None:
        del _OFFERS[token]
    return offer


def _reset_for_tests() -> None:
    _OFFERS.clear()
