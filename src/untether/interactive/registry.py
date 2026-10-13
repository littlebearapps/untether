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
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NamedTuple, Protocol

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
class Decision:
    """The user's answer to a control request, as handed to a responder.

    ``action``: approve | deny | discuss | chat | answer | auto | … (recorded
    on the handled record). ``deny_message`` is the text an engine shows the
    model on a denial. ``rejects_plan`` (#793): a Claude ExitPlanMode denial
    that rejects the plan rather than pausing it; other engines ignore it.
    ``answer`` / ``answers``: a typed answer / the option answers of a
    question, for an engine that words its own reply to the model.
    """

    approved: bool
    action: str
    deny_message: str | None = None
    rejects_plan: bool = True
    answer: str | None = None
    answers: dict[str, str] | None = None


class Responder(Protocol):
    """How one engine delivers a :class:`Decision` (#960).

    ``callback_prefix`` is the command id its buttons carry
    (``claude_control`` for Claude). ``is_alive`` says whether the session can
    still take an answer; ``respond`` delivers it and returns whether it was
    written (``False``: the session went away under the write). Everything
    else — the claim, the handled record, the cancelled-during-write check —
    is the registry's.
    """

    engine: str
    callback_prefix: str

    def is_alive(self, session_key: str) -> bool: ...

    async def respond(
        self, request_id: str, session_key: str, decision: Decision
    ) -> bool: ...


DEFAULT_CALLBACK_PREFIX = "claude_control"

# request_id -> the responder of an engine that registered it with one
# (:func:`register`). A request without an entry is answered by the default
# responder (Claude's, set by ``runners.claude`` at import), so every Claude
# registration path — and a test that writes ``_REQUEST_TO_SESSION``
# directly — routes exactly as before. Claude never adds entries here.
_REQUEST_TO_RESPONDER: dict[str, Responder] = {}


@dataclass(frozen=True, slots=True)
class _RequestMeta:
    kind: str
    tool_name: str
    registered_at: float


# Registration metadata for requests with an explicit responder (the
# engine-neutral :func:`snapshot`); Claude keeps its own on the stream state.
_REQUEST_META: dict[str, _RequestMeta] = {}

_DEFAULT_RESPONDER: list[Responder] = []

# Callbacks run by :func:`drop_session` before it drops the session's
# requests; each returns a label for the cleanup log, or None.
_SESSION_DROPPED_CALLBACKS: list[Callable[[str], str | None]] = []


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


# ── responders (#960) ───────────────────────────────────────────────────────


def set_default_responder(responder: Responder) -> None:
    """Install the responder for requests registered without one (Claude's,
    set when ``runners.claude`` is imported)."""
    _DEFAULT_RESPONDER[:] = [responder]


def responder_for(request_id: str) -> Responder | None:
    """The responder that answers *request_id*: the one it was registered
    with, else the default, else None (no engine has installed one)."""
    responder = _REQUEST_TO_RESPONDER.get(request_id)
    if responder is not None:
        return responder
    return _DEFAULT_RESPONDER[0] if _DEFAULT_RESPONDER else None


def session_alive(session_key: str, *, request_id: str | None = None) -> bool:
    """Whether *session_key* can still take an answer, asked of the
    responder of *request_id* (the default responder without one)."""
    if request_id is not None:
        responder = responder_for(request_id)
    else:
        responder = _DEFAULT_RESPONDER[0] if _DEFAULT_RESPONDER else None
    return responder is not None and responder.is_alive(session_key)


def callback_prefix(request_id: str) -> str:
    """The command id *request_id*'s buttons carry (``claude_control`` for
    Claude and for any request registered without a responder)."""
    responder = _REQUEST_TO_RESPONDER.get(request_id)
    if responder is None:
        return DEFAULT_CALLBACK_PREFIX
    return responder.callback_prefix


def _forget(request_id: str) -> None:
    """Drop the per-request data of a request that is no longer pending."""
    _REQUEST_TO_INPUT.pop(request_id, None)
    _REQUEST_TO_TOOL_NAME.pop(request_id, None)
    _REQUEST_TO_RESPONDER.pop(request_id, None)
    _REQUEST_META.pop(request_id, None)


def register(
    request_id: str,
    *,
    session_key: str,
    responder: Responder | None = None,
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
    kind: str = "tool",
) -> None:
    """Register *request_id* as awaiting a user answer for *session_key*.

    One call does what every registration site must (#388): the pending
    entry, then the chat binding and the run's originator
    (:func:`bind_request_channel`), then — when ``tool_name`` is given — the
    tool input (``{}`` when ``tool_input`` is None) and tool name that a
    responder and the deny wording read. ``responder`` is the engine that
    answers it; None means the default (Claude). ``kind`` (``tool`` | ``ask``
    | ``plan``) is kept for :func:`snapshot` on requests with a responder.
    """
    _REQUEST_TO_SESSION[request_id] = session_key
    bind_request_channel(request_id)
    _REQUEST_TO_RESPONDER.pop(request_id, None)
    _REQUEST_META.pop(request_id, None)
    if responder is not None:
        _REQUEST_TO_RESPONDER[request_id] = responder
        _REQUEST_META[request_id] = _RequestMeta(
            kind=kind, tool_name=tool_name or "", registered_at=time.monotonic()
        )
    if tool_name is not None:
        _REQUEST_TO_INPUT[request_id] = tool_input if tool_input is not None else {}
        _REQUEST_TO_TOOL_NAME[request_id] = tool_name


async def respond(
    request_id: str,
    decision: Decision,
    *,
    channel_id: int | None = None,
    claim_owner: str | None = None,
) -> ControlSendResult:
    """Answer a control request, reporting what actually happened (#685).

    Writes **nothing** unless the request is ``PENDING`` and this caller holds
    (or can take) its in-flight claim. ``claim_owner`` is the callback query id
    whose early toast already reserved the claim; without it a unique owner
    is used. The claim is released in ``finally``. The request's responder
    (:func:`responder_for`) delivers the decision; the claim, the handled
    record and the cancelled-during-write check (#684) are engine-neutral.

    Args:
        request_id: The control request ID
        decision: What the user decided; ``decision.action`` (approve / deny /
            discuss / chat / auto …) is recorded so a later tap can say it
        channel_id: The chat the tap came from (scopes the handled record)
        claim_owner: The dispatcher's callback query id, if it reserved the
            claim before the early answer
    """
    action = decision.action
    owner = claim_owner if claim_owner is not None else new_control_claim_owner()
    lookup = claim_control_request(
        request_id, action=action, owner=owner, channel_id=channel_id
    )
    if lookup.status is not ControlRequestStatus.PENDING:
        if lookup.status is ControlRequestStatus.NOT_FOUND:
            logger.warning(
                "control_response.request_not_found",
                request_id=request_id,
                reason=lookup.reason,
            )
        else:
            # Duplicate callback (a double tap, or Telegram long-polling
            # delivering the same update twice) — nothing is written.
            logger.debug(
                "control_response.already_handled",
                request_id=request_id,
                status=str(lookup.status),
            )
        return ControlSendResult(
            status=lookup.status,
            sent=False,
            prior=lookup.prior,
            reason=lookup.reason,
        )
    # #822: read before write_control_response pops it.
    tool_name = _REQUEST_TO_TOOL_NAME.get(request_id)
    try:
        session_id = _REQUEST_TO_SESSION[request_id]
        responder = responder_for(request_id)
        if responder is None or not responder.is_alive(session_id):
            logger.warning(
                "control_response.no_active_session",
                session_id=session_id,
                request_id=request_id,
            )
            # Clean up stale mappings
            _REQUEST_TO_SESSION.pop(request_id, None)
            _forget(request_id)
            return ControlSendResult(
                status=ControlRequestStatus.PENDING,
                sent=False,
                session_id=session_id,
                reason="no_active_session",
                tool_name=tool_name,
            )

        success = await responder.respond(request_id, session_id, decision)

        # Clean up the mapping after use. ``pop``, not ``del``: the claim
        # makes a concurrent delete impossible, but it is cheap insurance.
        _REQUEST_TO_SESSION.pop(request_id, None)
        _REQUEST_TO_RESPONDER.pop(request_id, None)
        _REQUEST_META.pop(request_id, None)
        if request_id in _CANCELLED_DURING_WRITE:
            # #684: the CLI withdrew the request while we were writing; it
            # ignores our answer, so say so rather than "approved".
            mark_request_handled(
                request_id,
                action="cancelled",
                outcome="cancelled",
                channel_id=channel_id,
            )
            return ControlSendResult(
                status=ControlRequestStatus.CANCELLED,
                sent=success,
                session_id=session_id,
                prior=_HANDLED_REQUESTS.get(request_id),
                tool_name=tool_name,
            )
        # A written *or* attempted write marks it handled (a closed pipe
        # means the session is gone either way).
        mark_request_handled(request_id, action=action, channel_id=channel_id)
        return ControlSendResult(
            status=ControlRequestStatus.PENDING,
            sent=success,
            session_id=session_id,
            reason=None if success else "write_failed",
            tool_name=tool_name,
        )
    finally:
        _CANCELLED_DURING_WRITE.discard(request_id)
        claim = _INFLIGHT_CONTROL_RESPONSES.get(request_id)
        if claim is not None and claim.owner == owner:
            del _INFLIGHT_CONTROL_RESPONSES[request_id]


async def respond_to_control_request(
    request_id: str,
    approved: bool,
    *,
    action: str,
    channel_id: int | None = None,
    deny_message: str | None = None,
    rejects_plan: bool = True,
    claim_owner: str | None = None,
) -> ControlSendResult:
    """:func:`respond` with the decision spelled out (#685).

    Args:
        request_id: The control request ID
        approved: Whether to approve (True) or deny (False) the request
        action: What the tap did (approve / deny / discuss / chat / auto …),
            recorded so a later tap can say it
        channel_id: The chat the tap came from (scopes the handled record)
        deny_message: Custom denial message (used when approved=False)
        rejects_plan: For an ExitPlanMode denial, whether it rejects the plan
            (❌ Deny) or is procedural (Pause & Outline / Let's discuss) — #793
        claim_owner: The dispatcher's callback query id, if it reserved the
            claim before the early answer
    """
    return await respond(
        request_id,
        Decision(
            approved=approved,
            action=action,
            deny_message=deny_message,
            rejects_plan=rejects_plan,
        ),
        channel_id=channel_id,
        claim_owner=claim_owner,
    )


async def answer_control_request(request_id: str, decision: Decision) -> bool:
    """Answer a control request without a tap's claim or chat (#685).

    Returns:
        True if the response was written, or the request was already answered
        (in flight / handled — a benign duplicate). False if it is unknown,
        its session is gone, the write failed, or the engine withdrew it
        (cancelled: a text reply to a withdrawn question must fall through to
        a normal prompt, not be swallowed).
    """
    result = await respond(request_id, decision)
    if result.status is ControlRequestStatus.CANCELLED:
        return False  # #684: even when written — the CLI ignored it
    return result.sent or result.status in (
        ControlRequestStatus.IN_FLIGHT,
        ControlRequestStatus.ALREADY_HANDLED,
    )


def expire(
    request_id: str, *, action: str = "timeout", channel_id: int | None = None
) -> bool:
    """Retire a pending request nobody answered in time (#960).

    For an engine with its own deadline (Claude's 5-min sweep stays in its
    runner). A tap writing its answer right now wins: nothing is retired and
    False is returned, as for an id that is not pending. Otherwise the
    request, its question state and per-request data are dropped and the id
    is recorded ``expired`` (terminal: a later tap reads "This request has
    expired"), and True is returned.
    """
    if request_id in _INFLIGHT_CONTROL_RESPONSES:
        return False
    if _REQUEST_TO_SESSION.pop(request_id, None) is None:
        return False
    _forget(request_id)
    _drop_question_state(request_id)
    mark_request_handled(
        request_id, action=action, outcome="expired", channel_id=channel_id
    )
    return True


def withdraw(
    request_id: str, *, deregister: bool = True, channel_id: int | None = None
) -> bool:
    """The engine no longer needs an answer to *request_id* (#684, #960).

    Claude's ``control_cancel_request``, or an engine's hook connection
    closing before a decision. Pending question state is dropped (a late
    option tap reads "No longer needed"). If a tap is writing its answer
    right now, the id is parked in ``_CANCELLED_DURING_WRITE`` for that
    writer to record and True is returned. Otherwise the request is retired
    — deregistered when ``deregister`` (Claude passes False when the id is
    not registered to its own session) — and recorded ``cancelled`` (in
    *channel_id*, default the run's chat); returns False.
    """
    _drop_question_state(request_id)
    if request_id in _INFLIGHT_CONTROL_RESPONSES:
        # A tap is writing its answer right now: the writer still reads the
        # ``_REQUEST_TO_*`` entries, so leave them to it and let it record
        # the ``cancelled`` outcome (#685 respond path).
        _CANCELLED_DURING_WRITE.add(request_id)
        return True
    if deregister:
        _REQUEST_TO_SESSION.pop(request_id, None)
        _REQUEST_TO_RESPONDER.pop(request_id, None)
        _REQUEST_META.pop(request_id, None)
    _REQUEST_TO_INPUT.pop(request_id, None)
    _REQUEST_TO_TOOL_NAME.pop(request_id, None)
    mark_request_handled(
        request_id,
        action="cancelled",
        outcome="cancelled",
        channel_id=get_run_channel_id() if channel_id is None else channel_id,
    )
    return False


def _drop_question_state(request_id: str) -> None:
    """Retire a question's pending entry and flow; a flow is remembered as
    ``cancelled`` so a late option tap reads "No longer needed" (#684)."""
    from .questions import (
        _ASK_QUESTION_FLOWS,
        _PENDING_ASK_REQUESTS,
        _record_answered_ask_flow,
    )

    _PENDING_ASK_REQUESTS.pop(request_id, None)
    flow = _ASK_QUESTION_FLOWS.pop(request_id, None)
    if flow is not None:
        _record_answered_ask_flow(request_id, flow.channel_id, outcome="cancelled")


def on_session_dropped(callback: Callable[[str], str | None]) -> None:
    """Run *callback(session_key)* on every :func:`drop_session`, before the
    session's requests are dropped. It returns a label for the cleanup log
    (e.g. ``discuss_feedback_ref``) or None. Registering twice is a no-op."""
    if callback not in _SESSION_DROPPED_CALLBACKS:
        _SESSION_DROPPED_CALLBACKS.append(callback)


def drop_session(session_key: str) -> list[str]:
    """Drop everything still registered for *session_key* (run end, cancel).

    Runs the :func:`on_session_dropped` callbacks, then removes every pending
    request of the session with its chat binding, originator, in-flight
    claim, cancelled-during-write mark and question state (and, for a
    request registered with its own responder, its input and tool name).
    Idempotent. Returns the labels for the cleanup log, in that order.
    """
    from .questions import _ASK_QUESTION_FLOWS, _PENDING_ASK_REQUESTS

    labels = [callback(session_key) for callback in tuple(_SESSION_DROPPED_CALLBACKS)]
    cleaned = [label for label in labels if label is not None]
    stale = [k for k, v in _REQUEST_TO_SESSION.items() if v == session_key]
    if stale:
        cleaned.append(f"requests({len(stale)})")
    for k in stale:
        del _REQUEST_TO_SESSION[k]
        _REQUEST_TO_CHANNEL.pop(k, None)  # #388
        _REQUEST_TO_ORIGINATOR.pop(k, None)
        # #685: a claim on a request whose session is gone can never
        # complete — drop it so the id doesn't read "in flight" for ever.
        _INFLIGHT_CONTROL_RESPONSES.pop(k, None)
        _CANCELLED_DURING_WRITE.discard(k)  # #684
        _REQUEST_META.pop(k, None)
        if _REQUEST_TO_RESPONDER.pop(k, None) is not None:
            # Only for a request with its own responder: Claude's session
            # cleanup has always left the input / tool-name entries alone.
            _REQUEST_TO_INPUT.pop(k, None)
            _REQUEST_TO_TOOL_NAME.pop(k, None)
        # Also clean up any pending ask requests and flows for stale requests
        _PENDING_ASK_REQUESTS.pop(k, None)
        _ASK_QUESTION_FLOWS.pop(k, None)
    return cleaned


def snapshot(
    session_key: str,
    *,
    now: float | None = None,
    input_details: Callable[[dict[str, Any] | None], str] | None = None,
) -> list[ControlRequestSnapshot]:
    """The session's pending requests that were registered with a responder,
    with their age — the #684 ``control_request_snapshot`` contract for an
    engine state (Claude builds its own from its stream state).
    ``input_details`` formats a request's tool input for a title."""
    from .questions import _PENDING_ASK_REQUESTS

    now = time.monotonic() if now is None else now
    snaps: list[ControlRequestSnapshot] = []
    for rid, owner in list(_REQUEST_TO_SESSION.items()):
        if owner != session_key:
            continue
        meta = _REQUEST_META.get(rid)
        responder = _REQUEST_TO_RESPONDER.get(rid)
        if meta is None or responder is None:
            continue
        details = ""
        if input_details is not None:
            details = input_details(_REQUEST_TO_INPUT.get(rid))
        snaps.append(
            ControlRequestSnapshot(
                request_id=rid,
                session_id=session_key,
                age_s=max(0.0, now - meta.registered_at),
                tool_name=_REQUEST_TO_TOOL_NAME.get(rid, meta.tool_name),
                kind=meta.kind,
                answerable_by_text=rid in _PENDING_ASK_REQUESTS,
                writer_ok=responder.is_alive(session_key),
                input_details=details,
            )
        )
    return snaps
