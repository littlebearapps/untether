"""Engine-neutral registry of control requests awaiting a user answer (#960).

Moved verbatim out of ``runners/claude.py``. A *control request* is anything
an engine is blocked on until the user taps a button or types an answer in
Telegram: a tool approval, a plan approval, an AskUserQuestion. The stores
here are keyed by request id and say which session raised it
(``_REQUEST_TO_SESSION``, the authoritative "pending" set), which chat its
buttons were posted in and who started the run (#388), whether a tap is
writing its answer right now (#685) and how it was resolved (#685, #684).

``runners.claude`` re-exports every name, so the objects are shared, never
copied: ``claude._REQUEST_TO_SESSION is registry._REQUEST_TO_SESSION``.
Nothing here imports ``runners`` or ``telegram`` (pinned by a test).
"""

from __future__ import annotations

import itertools
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NamedTuple

from ..logging import get_logger
from ..utils.paths import get_run_channel_id, get_run_sender_id

logger = get_logger(__name__)

# Phase 2: Global registry mapping request_id -> session_id
# This allows callbacks to find the right runner instance
_REQUEST_TO_SESSION: dict[str, str] = {}

# Phase 2: Global registry mapping request_id -> original tool input
# Claude Code CLI requires updatedInput in can_use_tool responses
_REQUEST_TO_INPUT: dict[str, dict[str, Any]] = {}

# Phase 2: Global registry mapping request_id -> tool_name
# Used by claude_control.py to send tool-specific deny messages
_REQUEST_TO_TOOL_NAME: dict[str, str] = {}

# Recently handled request_ids (prevents duplicate callback warnings).
# #197: previously a plain set cleared wholesale when len > 100, which opened
# a small window where duplicate callbacks could slip through as "not found"
# rather than being recognised as duplicates.  Now an LRU OrderedDict that
# evicts oldest-first at _HANDLED_REQUESTS_MAX entries.
_HANDLED_REQUESTS_MAX = 200


class ControlRequestStatus(StrEnum):
    """Where a control request stands when a button is tapped (#685).

    The user-facing grouping is three-way: **sent** (a ``PENDING`` request we
    answered) / **already handled** (``IN_FLIGHT``, ``ALREADY_HANDLED``,
    ``CANCELLED``) / **not found**. The CLI gives no feedback on a duplicate
    answer, so this status comes from Untether's own registries.
    """

    PENDING = "pending"  # in _REQUEST_TO_SESSION, not claimed
    IN_FLIGHT = "in_flight"  # another tap is writing its answer right now
    ALREADY_HANDLED = "already_handled"  # answered earlier (Telegram / auto)
    CANCELLED = "cancelled"  # the CLI withdrew it (control_cancel_request)
    NOT_FOUND = "not_found"  # unknown: restart, LRU-evicted, session gone, other chat


@dataclass(frozen=True, slots=True)
class HandledControl:
    """How and where a control request was resolved (#685).

    ``action``: approve | deny | discuss | chat | auto | cancelled | timeout |
    superseded (``None`` when unknown). ``outcome``: answered | cancelled |
    expired. ``channel_id``: the chat that resolved it (scopes the memo, #715).
    """

    action: str | None
    outcome: str
    channel_id: int | None
    at: float


# Outcomes that record a request Untether can no longer answer; a later tap
# never overwrites them with "answered" (#685).
_TERMINAL_CONTROL_OUTCOMES = frozenset({"cancelled", "expired"})

# Values are ``HandledControl`` records; ``None`` is tolerated (legacy/tests)
# and reads as "answered, details unknown".
_HANDLED_REQUESTS: OrderedDict[str, HandledControl | None] = OrderedDict()

# #388: request_id -> the chat its buttons were posted in (the run's chat,
# ``get_run_channel_id()`` at registration). A tap from any other chat on a
# pending / in-flight request reads ``NOT_FOUND`` (``channel_mismatch``) and
# writes nothing. Only consulted while the id is still pending or claimed, so
# an entry left behind by any of the deletion paths is inert. Bounded by
# liveness pruning, never by evicting a live id (no fail-open under load).
_REQUEST_TO_CHANNEL: dict[str, int] = {}
_REQUEST_TO_CHANNEL_MAX = 512
# #388 phase 2: request_id -> the Telegram user whose message started the run
# (``get_run_sender_id()`` at registration). Read only when
# ``[transports.telegram] approval_originator_only`` is on; no entry (cron,
# webhook, /at, loop fires) means any allowed user may answer. Same liveness
# rules and pruning as ``_REQUEST_TO_CHANNEL``.
_REQUEST_TO_ORIGINATOR: dict[str, int] = {}


@dataclass(slots=True)
class InflightClaim:
    """A tap that is answering a request right now (#685)."""

    action: str
    owner: str
    channel_id: int | None
    at: float


# #685 §2.4: request_id -> the claim of the tap currently answering it.
# Callbacks are dispatched concurrently, and the gap between "is it pending?"
# and the registry cleanup spans an ``await`` (the stdin lock), so without a
# synchronous claim two taps could both write — and the loser's ``del`` raised
# KeyError. The dispatcher reserves the claim before its early answer, so the
# toast reflects it too. Every writer respects it: the 5-min sweep skips a
# claimed id, and ``_cleanup_session_registries`` drops the session's claims.
_INFLIGHT_CONTROL_RESPONSES: dict[str, InflightClaim] = {}
_DIRECT_CLAIM_SEQ = itertools.count(1)

# #684: request ids the CLI withdrew (``control_cancel_request``) while a tap
# was writing its answer. The cancel handler leaves the ``_REQUEST_TO_*``
# entries to that writer (it still reads them) and parks the id here;
# ``respond_to_control_request`` consumes it in its ``finally`` and records
# ``outcome="cancelled"``. Bounded: the writer always discards its id, and
# session cleanup drops any leftover.
_CANCELLED_DURING_WRITE: set[str] = set()


@dataclass(frozen=True, slots=True)
class ControlRequestSnapshot:
    """One control request still registered for a user answer (#684).

    Read by the bridge's ``control_request.unanswerable`` detector.
    ``kind``: ``tool`` | ``ask`` | ``outline_hold`` | ``synthetic`` (``da:``).
    ``answerable_by_text``: a text reply in the chat routes to it (a pending
    AskUserQuestion). ``writer_ok``: a live stdin writer exists for the
    session. ``input_details``: the tool's key parameters, for a title.
    """

    request_id: str
    session_id: str
    age_s: float
    tool_name: str
    kind: str
    answerable_by_text: bool
    writer_ok: bool
    # #929: the key parameters Approve would allow (``_tool_input_details``),
    # so a rescued request with no tracked action still says what it is.
    input_details: str = ""


# Untether policy, event-driven: swept only when another interactive
# control_request arrives (#684) — a lone request never expires. The CLI has
# no deadline on a permission prompt (findings 2026-09-30 Q1 §5); a request
# that can't be answered is reported by ``control_request.unanswerable``.
CONTROL_REQUEST_TIMEOUT_SECONDS: float = 300.0  # 5 minutes


class ControlLookup(NamedTuple):
    """Result of classifying a control request id (#685)."""

    status: ControlRequestStatus
    prior: HandledControl | None = None
    reason: str | None = None  # NOT_FOUND detail: unknown | channel_mismatch


@dataclass(frozen=True, slots=True)
class ControlSendResult:
    """Outcome of :func:`respond_to_control_request` (#685).

    ``status`` is ``PENDING`` when the request was ours to answer (``sent``
    says whether the response line was actually written). ``prior`` is set
    for ``IN_FLIGHT`` / ``ALREADY_HANDLED`` / ``CANCELLED``. ``reason``:
    unknown | channel_mismatch | no_active_session | write_failed.
    """

    status: ControlRequestStatus
    sent: bool
    session_id: str | None = None
    prior: HandledControl | None = None
    reason: str | None = None
    # #822: the tool the request was for (logged on claude_control.sent).
    tool_name: str | None = None


def mark_request_handled(
    request_id: str,
    *,
    action: str | None = None,
    outcome: str = "answered",
    channel_id: int | None = None,
) -> None:
    """Record *request_id* as resolved so the reconcile loop can retire it.

    The reconcile loop in ``translate`` emits ``action_completed`` for every
    handled request it can resolve to an action id, which is what clears the
    stale inline keyboard (#229) and, since #683, the synthetic
    ``claude.discuss_approve.N`` action from the Pause & Outline hold-open path.

    #685: the record says *how* (``action`` / ``outcome``) and *where*
    (``channel_id``, defaulting to the run's chat) it was resolved, so a later
    tap on the same button can say what actually happened. A ``cancelled`` or
    ``expired`` record is terminal: a later "answered" never overwrites it.

    #197: LRU-evict oldest entries instead of clear()-ing the whole set.
    """
    existing = _HANDLED_REQUESTS.get(request_id)
    if (
        existing is not None
        and existing.outcome in _TERMINAL_CONTROL_OUTCOMES
        and outcome == "answered"
    ):
        logger.debug(
            "control_response.terminal_kept",
            request_id=request_id,
            kept_outcome=existing.outcome,
            kept_action=existing.action,
            ignored_action=action,
        )
        return
    if channel_id is None:
        channel_id = get_run_channel_id()
    _HANDLED_REQUESTS[request_id] = HandledControl(
        action=action, outcome=outcome, channel_id=channel_id, at=time.monotonic()
    )
    _HANDLED_REQUESTS.move_to_end(request_id)
    while len(_HANDLED_REQUESTS) > _HANDLED_REQUESTS_MAX:
        _HANDLED_REQUESTS.popitem(last=False)


def bind_request_channel(request_id: str) -> None:
    """Bind a just-registered request to the run's chat (#388).

    Called right after every ``_REQUEST_TO_SESSION[...] =`` registration
    (pinned by a structural test). No run chat (tests, legacy) → unbound,
    which keeps the pre-#388 behaviour for that id.
    """
    channel = get_run_channel_id()
    _REQUEST_TO_CHANNEL.pop(request_id, None)
    _REQUEST_TO_ORIGINATOR.pop(request_id, None)
    sender = get_run_sender_id()
    if sender is not None:
        _REQUEST_TO_ORIGINATOR[request_id] = sender
    if channel is None:
        return
    _REQUEST_TO_CHANNEL[request_id] = channel
    if len(_REQUEST_TO_CHANNEL) > _REQUEST_TO_CHANNEL_MAX:
        # Liveness pruning: drop entries no longer pending or claimed; a
        # live binding is never evicted.
        for rid in [
            rid
            for rid in _REQUEST_TO_CHANNEL
            if rid not in _REQUEST_TO_SESSION and rid not in _INFLIGHT_CONTROL_RESPONSES
        ]:
            del _REQUEST_TO_CHANNEL[rid]
    if len(_REQUEST_TO_ORIGINATOR) > _REQUEST_TO_CHANNEL_MAX:
        for rid in [
            rid
            for rid in _REQUEST_TO_ORIGINATOR
            if rid not in _REQUEST_TO_SESSION and rid not in _INFLIGHT_CONTROL_RESPONSES
        ]:
            del _REQUEST_TO_ORIGINATOR[rid]


# Pre-#960 name, still used by the Claude runner's registration sites.
_bind_request_channel = bind_request_channel


def control_request_origin(request_id: str) -> int | None:
    """The chat a pending / claimed request is bound to (#388), or None."""
    if request_id in _REQUEST_TO_SESSION or request_id in _INFLIGHT_CONTROL_RESPONSES:
        return _REQUEST_TO_CHANNEL.get(request_id)
    return None


def control_request_originator(request_id: str) -> int | None:
    """The Telegram user who started the run that raised a pending / claimed
    request (#388), or None — no human originator (cron, webhook, /at, loop
    fire) or not pending."""
    if request_id in _REQUEST_TO_SESSION or request_id in _INFLIGHT_CONTROL_RESPONSES:
        return _REQUEST_TO_ORIGINATOR.get(request_id)
    return None


def classify_control_request(
    request_id: str, *, channel_id: int | None = None
) -> ControlLookup:
    """Classify *request_id* for a tap from *channel_id* (#685). Pure and sync.

    Order: an in-flight claim → ``IN_FLIGHT``; registered → ``PENDING``; a
    handled record → ``CANCELLED`` / ``ALREADY_HANDLED``; else ``NOT_FOUND``.
    A handled record from another chat reads ``NOT_FOUND`` (reason
    ``channel_mismatch``) — one chat can't read another's resolution (#715).

    #388: checked first, a pending or in-flight request bound to another chat
    also reads ``NOT_FOUND`` (``channel_mismatch``) — a forged callback from
    a different chat can't answer it, and learns nothing (not even "being
    handled"). ``channel_id=None`` (internal callers) skips the check.
    """
    if channel_id is not None:
        origin = control_request_origin(request_id)
        if origin is not None and origin != channel_id:
            return ControlLookup(
                ControlRequestStatus.NOT_FOUND, reason="channel_mismatch"
            )
    claim = _INFLIGHT_CONTROL_RESPONSES.get(request_id)
    if claim is not None:
        return ControlLookup(
            ControlRequestStatus.IN_FLIGHT,
            HandledControl(
                action=claim.action,
                outcome="answered",
                channel_id=claim.channel_id,
                at=claim.at,
            ),
        )
    if request_id in _REQUEST_TO_SESSION:
        if channel_id is not None and request_id not in _REQUEST_TO_CHANNEL:
            # #388: a registration site that forgot _bind_request_channel
            # (or a run without a chat) would silently reopen the gap.
            logger.debug(
                "claude_control.origin_unbound",
                request_id=request_id,
                channel_id=channel_id,
            )
        return ControlLookup(ControlRequestStatus.PENDING)
    if request_id in _HANDLED_REQUESTS:
        record = _HANDLED_REQUESTS[request_id]
        if (
            record is not None
            and record.channel_id is not None
            and channel_id is not None
            and record.channel_id != channel_id
        ):
            return ControlLookup(
                ControlRequestStatus.NOT_FOUND, reason="channel_mismatch"
            )
        if record is not None and record.outcome == "cancelled":
            return ControlLookup(ControlRequestStatus.CANCELLED, record)
        return ControlLookup(ControlRequestStatus.ALREADY_HANDLED, record)
    return ControlLookup(ControlRequestStatus.NOT_FOUND, reason="unknown")


def claim_control_request(
    request_id: str,
    *,
    action: str,
    owner: str | None,
    channel_id: int | None = None,
) -> ControlLookup:
    """Classify *request_id* and, if it is ``PENDING``, reserve it (#685).

    Synchronous — nothing is awaited between the check and the reservation,
    so two concurrent taps serialise on the event loop: the first gets
    ``PENDING`` and holds the claim, the second sees ``IN_FLIGHT``. A caller
    that already holds the claim (same ``owner``) gets ``PENDING`` again.
    ``owner=None`` only classifies.
    """
    existing = _INFLIGHT_CONTROL_RESPONSES.get(request_id)
    if existing is not None and owner is not None and existing.owner == owner:
        if request_id in _REQUEST_TO_SESSION:
            return ControlLookup(ControlRequestStatus.PENDING)
        # The request vanished under our claim (session cleanup) — drop it
        # and report what the registries say now.
        del _INFLIGHT_CONTROL_RESPONSES[request_id]
    lookup = classify_control_request(request_id, channel_id=channel_id)
    if lookup.status is ControlRequestStatus.PENDING and owner is not None:
        _INFLIGHT_CONTROL_RESPONSES[request_id] = InflightClaim(
            action=action, owner=owner, channel_id=channel_id, at=time.monotonic()
        )
    return lookup


def release_control_claims(owner: str | None) -> None:
    """Drop every in-flight claim *owner* still holds (idempotent, #685)."""
    if owner is None:
        return
    for rid in [
        rid for rid, c in _INFLIGHT_CONTROL_RESPONSES.items() if c.owner == owner
    ]:
        del _INFLIGHT_CONTROL_RESPONSES[rid]


def new_control_claim_owner() -> str:
    """A unique claim owner for a caller without a callback query id (#685)."""
    return f"direct:{next(_DIRECT_CLAIM_SEQ)}"
