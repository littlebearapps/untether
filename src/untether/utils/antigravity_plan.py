"""Plan first for Antigravity (#981, shape A: decision R3-4, 2026-10-12).

agy has a native plan mode (``--mode=plan``). Under Untether's no-shell
Workspace agent (``--agent=untether-workspace``) a plan turn writes one plan
artifact into the conversation's own folder (``brain/<conversation id>/``,
with a ``<name>.metadata.json`` sidecar saying ``"requestFeedback": true``)
and then **stops by itself** (agy 1.3.3: P-H3b 5/5 on 2026-10-12, repeated
on 2026-10-13). Approving is a resume of the same conversation with
``--mode=accept-edits`` (P-H3c 5/5); revising is a resume in ``--mode=plan``
with the user's text (probed 2026-10-13: the same file is rewritten and the
turn stops again). No hook gate, hold or bypass is involved.

What agy does **not** give Untether, and this module fills (custom — see
the native-vs-custom register in ``docs/reference/runners/antigravity/``):

- **"A plan is ready."** agy's ``result`` is the same as any turn's, and
  ``init.permission_mode`` is ``request-review`` for plan and accept-edits
  runs alike. The only stream signal is a DONE ``write_to_file`` step whose
  ``TargetFile`` is inside ``brain/<conversation id>/``, and the stream
  carries the path only, so the body and the sidecar are read from that one
  file after ``realpath`` containment (amends D28 for this file only).
- **Approve / Deny / Let's discuss.** A small in-memory store of pending
  plan cards (one per plan turn), bound to the chat it was posted in. It
  could move onto the engine-neutral approval registry (#960) later.

Everything here is pure or touches only this module's state; the runner,
the bridge and the ``agy_plan`` callback backend call into it.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..logging import get_logger

if TYPE_CHECKING:
    from ..context import RunContext

logger = get_logger(__name__)

# agy's ``--mode`` value for a plan turn (allow-listed in antigravity_argv).
PLAN_AGY_MODE = "plan"
# The prompt the Approve button resumes the conversation with (D25, P-H3c).
APPROVE_PROMPT = "The plan is approved. Implement it now."
# D30: in a plan run agy turns the prompt into ``/plan …``, and a slash
# command it handles itself inside stream-json input ends the run (rc 2).
# Plan runs can't pass ``--disable-slash-commands`` (C7: it turns
# ``--mode=plan`` into a no-op), so a ``/``-leading prompt gets this prefix.
SLASH_ESCAPE = "Task: "

CALLBACK_PREFIX = "agy_plan"
ACTIONS = ("approve", "deny", "discuss")

# The write tools agy uses for artifacts (and for project files).
PLAN_WRITE_TOOLS = frozenset(
    {"write_to_file", "replace_file_content", "multi_replace_file_content"}
)
# Artifacts agy writes that are never a plan (P-H3c: ``walkthrough.md`` after
# an approved run; ``task.md`` is agy's own checklist).
_NOT_PLANS = frozenset({"walkthrough.md", "task.md"})
_SIDECAR_SUFFIX = ".metadata.json"
_MAX_WRITES = 32
_MAX_PLAN_BYTES = 256 * 1024
_MAX_SIDECAR_BYTES = 16 * 1024
# The plan as shown in the final; a long final is split across messages.
PLAN_BODY_CHARS = 8000
_TRUNCATED_NOTE = "\n\n…\n\n_(plan truncated — ask Antigravity for the rest)_"

PENDING_TTL_S = 24 * 3600.0
_PENDING_MAX = 256


def brain_root() -> Path:
    """agy's per-conversation artifact folders (``~/.gemini/antigravity-cli/
    brain``; agy 1.3.3 on lba-1). Derived from the home directory only —
    never from a path the stream named."""
    return Path.home() / ".gemini" / "antigravity-cli" / "brain"


def escape_slash(prompt: str) -> str:
    """D30: a plan run's prompt never starts with ``/``."""
    if prompt.lstrip().startswith("/"):
        return SLASH_ESCAPE + prompt
    return prompt


def note_plan_write(writes: list[str], tool_name: str, params: Any) -> None:
    """Remember a finished artifact-capable write's ``TargetFile`` (the
    runner calls this for every DONE tool step of a plan run)."""
    if tool_name not in PLAN_WRITE_TOOLS or not isinstance(params, dict):
        return
    target = params.get("TargetFile")
    if not isinstance(target, str) or not target or len(writes) >= _MAX_WRITES:
        return
    writes.append(target)


@dataclass(frozen=True, slots=True)
class PlanArtifact:
    name: str  # file name inside the conversation's artifact folder
    body: str  # as shown (capped at PLAN_BODY_CHARS)
    summary: str | None
    truncated: bool


def _conversation_dir(conversation_id: str) -> Path | None:
    from .antigravity_argv import AgyArgvError, check_conversation

    try:
        check_conversation(conversation_id)
    except AgyArgvError:
        return None
    try:
        return (brain_root() / conversation_id).resolve()
    except (OSError, RuntimeError):
        return None


def _read_bounded(path: Path, limit: int) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            return fh.read(limit + 1)
    except OSError:
        return None


def _sidecar(path: Path) -> dict[str, Any] | None:
    raw = _read_bounded(path.with_name(path.name + _SIDECAR_SUFFIX), _MAX_SIDECAR_BYTES)
    if raw is None or len(raw) > _MAX_SIDECAR_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def find_plan(conversation_id: str | None, writes: list[str]) -> PlanArtifact | None:
    """The plan this turn wrote, newest write first, or None.

    A candidate must ``realpath`` to a ``.md`` file directly inside
    ``brain/<conversation id>/`` (a symlink out, ``..`` or another
    conversation's folder never counts), must not be ``walkthrough.md`` /
    ``task.md``, and its sidecar — when readable — must say
    ``requestFeedback: true``.
    """
    if not conversation_id or not writes:
        return None
    root = _conversation_dir(conversation_id)
    if root is None:
        return None
    seen: set[Path] = set()
    for target in reversed(writes):
        try:
            path = Path(os.path.expanduser(target)).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if path in seen or path.parent != root:
            continue
        seen.add(path)
        name = path.name
        if not name.endswith(".md") or name in _NOT_PLANS:
            continue
        meta = _sidecar(path)
        if meta is not None and meta.get("requestFeedback") is not True:
            continue
        raw = _read_bounded(path, _MAX_PLAN_BYTES)
        if raw is None:
            logger.warning("antigravity.plan.unreadable", name=name)
            continue
        body = raw[:_MAX_PLAN_BYTES].decode("utf-8", errors="replace").strip()
        if not body:
            continue
        truncated = len(raw) > _MAX_PLAN_BYTES or len(body) > PLAN_BODY_CHARS
        if len(body) > PLAN_BODY_CHARS:
            body = body[:PLAN_BODY_CHARS].rstrip()
        summary = meta.get("summary") if meta is not None else None
        if meta is None:
            logger.info("antigravity.plan.sidecar_missing", name=name)
        return PlanArtifact(
            name=name,
            body=body,
            summary=summary if isinstance(summary, str) and summary else None,
            truncated=truncated,
        )
    return None


# ── what the plan turn's final says ────────────────────────────────────────


def plan_answer(plan: PlanArtifact, response: str) -> str:
    """The plan turn's final: the plan itself, then agy's own reply."""
    body = plan.body + (_TRUNCATED_NOTE if plan.truncated else "")
    parts = [f"📋 **Plan** — `{plan.name}`", body]
    if response.strip():
        parts.extend(["---", response.strip()])
    return "\n\n".join(parts)


NO_PLAN_NOTE = (
    "ℹ️ Plan first: no plan was written this time, so there is nothing to "
    "approve yet. Reply to keep planning, or pick Workspace in /config to let "
    "Antigravity make changes."
)


def approved_header(name: str) -> str:
    return f"📋 Plan (approved) — `{name}`"


# #835: an unattended Plan first run posts its plan and changes nothing.
UNATTENDED_DENIED_LABEL = "plan approval"


def unattended_hint(trigger: str) -> str:
    if trigger.startswith("webhook:"):
        return (
            "webhooks use the chat's permission mode \N{EM DASH} pick Workspace "
            "in /config for webhook runs that should make changes"
        )
    return (
        'set permission_mode = "workspace" on the cron to let it make the '
        "change, or approve plans from the chat"
    )


# ── the card ───────────────────────────────────────────────────────────────

APPROVE_LABEL = "✅ Approve Plan"  # Claude's label (#383)
DENY_LABEL = "❌ Deny"
DISCUSS_LABEL = "💬 Let's discuss"

CARD_CAPTION = (
    "Approving lets Antigravity carry out this plan in Workspace mode: it "
    "edits files in this project and has no shell. Deny changes nothing. "
    "Let's discuss: reply with what to change and it revises the plan."
)


def callback_data(action: str, request_id: str) -> str:
    return f"{CALLBACK_PREFIX}:{action}:{request_id}"


def keyboard(request_id: str) -> list[list[dict[str, str]]]:
    return [
        [
            {
                "text": APPROVE_LABEL,
                "callback_data": callback_data("approve", request_id),
            },
            {"text": DENY_LABEL, "callback_data": callback_data("deny", request_id)},
        ],
        [
            {
                "text": DISCUSS_LABEL,
                "callback_data": callback_data("discuss", request_id),
            }
        ],
    ]


_STATUS_TEXT = {
    "approve": "✅ Plan approved — Antigravity is carrying it out (Workspace).",
    "deny": (
        "❌ Plan denied — nothing was changed. Send a message to keep "
        "planning, or /new to start over."
    ),
    "discuss": (
        "💬 Let's discuss — reply with what to change and Antigravity will "
        "revise the plan."
    ),
    # #1074: a newer plan turn on the conversation replaced this card.
    "superseded": "ℹ️ Replaced by a newer plan — use the latest card.",
}


def card_text(name: str, decision: str | None = None) -> str:
    head = f"📋 Antigravity's plan is ready — {name}"
    if decision is None:
        return f"{head}\n\n{CARD_CAPTION}"
    return f"{head}\n\n{_STATUS_TEXT.get(decision, decision)}"


# ── pending plan cards ──────────────────────────────────────────────────────


@dataclass(slots=True)
class PendingPlan:
    request_id: str
    conversation_id: str
    channel_id: int | None
    thread_id: int | None
    context: RunContext | None
    cwd: str | None
    originator: int | None
    name: str
    created_at: float
    status: str = "pending"  # pending | claimed | approve | deny | discuss | superseded
    claim_owner: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


_LOCK = threading.Lock()
_PENDING: dict[str, PendingPlan] = {}
# conversation id → the plan whose Approve is waiting for its resumed run.
_APPROVED: dict[str, PendingPlan] = {}


def _now() -> float:
    return time.monotonic()


def _sweep(now: float) -> None:
    for rid in [r for r, p in _PENDING.items() if now - p.created_at > PENDING_TTL_S]:
        _PENDING.pop(rid, None)
    for cid in [c for c, p in _APPROVED.items() if now - p.created_at > PENDING_TTL_S]:
        _APPROVED.pop(cid, None)
    while len(_PENDING) > _PENDING_MAX:
        _PENDING.pop(next(iter(_PENDING)))


def register(
    *,
    conversation_id: str,
    name: str,
    channel_id: int | None,
    thread_id: int | None,
    context: RunContext | None,
    cwd: str | None,
    originator: int | None,
) -> PendingPlan:
    """A new plan card; earlier pending cards of the conversation are
    superseded (only the newest plan can be approved)."""
    now = _now()
    with _LOCK:
        _sweep(now)
        superseded = _supersede_locked(conversation_id)
        rid = secrets.token_hex(6)
        while rid in _PENDING:
            rid = secrets.token_hex(6)
        plan = PendingPlan(
            request_id=rid,
            conversation_id=conversation_id,
            channel_id=channel_id,
            thread_id=thread_id,
            context=context,
            cwd=cwd,
            originator=originator,
            name=name,
            created_at=now,
        )
        if superseded:
            plan.extra["superseded"] = superseded
        _PENDING[rid] = plan
    return plan


def _supersede_locked(conversation_id: str) -> list[str]:
    retired: list[str] = []
    for plan in _PENDING.values():
        if plan.conversation_id == conversation_id and plan.status in (
            "pending",
            "discuss",
        ):
            plan.status = "superseded"
            retired.append(plan.request_id)
    return retired


def supersede(conversation_id: str) -> list[str]:
    """A new plan turn on this conversation started: older cards can't be
    approved any more (their plan is being rewritten). Returns their ids."""
    with _LOCK:
        return _supersede_locked(conversation_id)


def retire(request_ids: list[str]) -> list[dict[str, str]]:
    """#1074: ``{"id", "text"}`` for each superseded card still showing its
    buttons, for the bridge to edit (``runner.RETIRED_CARDS_KEY``). Each card
    is handed out once; a tap on it retires it too (``mark_retired``)."""
    out: list[dict[str, str]] = []
    with _LOCK:
        for rid in request_ids:
            plan = _PENDING.get(rid)
            if plan is None or plan.status != "superseded":
                continue
            if plan.extra.get("retired"):
                continue
            plan.extra["retired"] = True
            out.append({"id": rid, "text": card_text(plan.name, "superseded")})
    return out


def mark_retired(request_id: str) -> None:
    with _LOCK:
        plan = _PENDING.get(request_id)
        if plan is not None:
            plan.extra["retired"] = True


def originator(request_id: str) -> int | None:
    with _LOCK:
        plan = _PENDING.get(request_id)
        return plan.originator if plan is not None else None


def _visible(plan: PendingPlan | None, channel_id: int | None) -> PendingPlan | None:
    # #388 / #715 parity: a card only answers in the chat it was posted in.
    if plan is None:
        return None
    if (
        channel_id is not None
        and plan.channel_id is not None
        and plan.channel_id != channel_id
    ):
        return None
    return plan


def peek(request_id: str, channel_id: int | None) -> PendingPlan | None:
    with _LOCK:
        return _visible(_PENDING.get(request_id), channel_id)


def claim(
    request_id: str, channel_id: int | None, owner: str
) -> tuple[str, PendingPlan | None]:
    """``("ok", plan)`` when *owner* now holds the card (or already did),
    else ``(reason, plan_or_None)`` with reason ``expired`` / ``claimed`` /
    ``superseded`` / ``answered``. Synchronous: two taps can't both win."""
    with _LOCK:
        _sweep(_now())
        plan = _visible(_PENDING.get(request_id), channel_id)
        if plan is None:
            return "expired", None
        if plan.status == "claimed":
            return ("ok", plan) if plan.claim_owner == owner else ("claimed", plan)
        if plan.status == "superseded":
            return "superseded", plan
        if plan.status in ("pending", "discuss"):
            plan.extra["prior_status"] = plan.status
            plan.status = "claimed"
            plan.claim_owner = owner
            return "ok", plan
        return "answered", plan


def release(owner: str) -> None:
    """Undo a claim *owner* still holds (its handler failed or never ran)."""
    with _LOCK:
        for plan in _PENDING.values():
            if plan.status == "claimed" and plan.claim_owner == owner:
                plan.status = plan.extra.pop("prior_status", "pending")
                plan.claim_owner = None


def resolve(request_id: str, decision: str) -> PendingPlan | None:
    """Record the decision; ``approve`` also arms the one-shot marker the
    resumed run picks up (its footer and ``📋 Plan (approved)`` header).
    ``discuss`` leaves the card answerable (the user may still approve)."""
    with _LOCK:
        plan = _PENDING.get(request_id)
        if plan is None:
            return None
        plan.status = decision
        plan.claim_owner = None
        plan.extra.pop("prior_status", None)
        if decision == "approve":
            _APPROVED[plan.conversation_id] = plan
        return plan


def take_approved(conversation_id: str) -> PendingPlan | None:
    with _LOCK:
        return _APPROVED.pop(conversation_id, None)


def approve_context(plan: PendingPlan) -> RunContext:
    """The resumed run's context: the plan run's own project/branch, with
    the run's permission mode set to Workspace (applied for that run only,
    like a cron's own ``permission_mode``)."""
    from ..context import RunContext

    base = plan.context if plan.context is not None else RunContext()
    # Not a trigger's run any more: an ``at:`` plan approved from the chat
    # is a person's decision.
    return replace(base, permission_mode="workspace", trigger_source=None)


def _reset_for_tests() -> None:
    with _LOCK:
        _PENDING.clear()
        _APPROVED.clear()
