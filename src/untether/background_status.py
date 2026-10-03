"""Live background-task status for Claude live sessions (#777).

Renders over the runner's native task map (``ClaudeStreamState.tasks``, built
from ``system/task_*`` events in #776) — nothing here parses CLI events. Two
surfaces:

- **Pre-result:** a ``⏳ background (N)`` block appended to the run's progress
  message, one row per live top-level background task. It rides the existing
  progress edits (and their rate limit); the heartbeat tick refreshes it.
- **Post-result:** one *status message* per batch of background work, sent
  silently after the run's answer (or a later turn's) and edited in place —
  throttled, with an early edit when a task finishes — then finalised when
  the set empties or the live session closes. Its ref is registered with
  ``progress_persistence`` so a crash/restart edits it to "interrupted"
  rather than leaving it saying "running" forever.

Tasks are duck-typed (``task_id``, ``task_type``, ``description``,
``is_backgrounded``, ``owned_by_subagent``, ``status``, ``started_at``,
``ended_at``, ``last_usage``, ``last_step``, ``last_tool_name``) so the bridge
stays engine-agnostic and tests can use plain objects.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

import anyio

from .logging import get_logger
from .markdown import HARD_BREAK, shorten
from .transport import MessageRef, RenderedMessage, SendOptions, Transport

logger = get_logger(__name__)

AGENT_ICON = "\N{ROBOT FACE}"
SHELL_ICON = "\N{SPIRAL SHELL}"
HOURGLASS = "\N{HOURGLASS WITH FLOWING SAND}"
DONE_MARK = "\N{WHITE HEAVY CHECK MARK}"
FAIL_MARK = "\N{CROSS MARK}"
STOP_MARK = "\N{BLACK SQUARE FOR STOP}\N{VARIATION SELECTOR-16}"
NOTE_MARK = "\N{SPEECH BALLOON}"
# #892: appended to a continued (revived, #801) task's label when its next
# finish is announced, so the second "🔔 … finished" isn't read as a repeat.
CONTINUED_SUFFIX = " (continued)"

LIVE_STATUSES = frozenset({"running", "pending"})
_DONE_STATUSES = frozenset({"completed", "ended", "done", "success"})
_FAIL_STATUSES = frozenset({"failed", "error", "errored"})

DESC_WIDTH = 60
STEP_WIDTH = 40
# The post-result status message is refreshed (elapsed / tokens) at most this
# often; a task finishing (or a folded ack, #785) edits sooner, bounded only by
# ``STATUS_MIN_EDIT_S`` and the outbox's own per-chat rate limit.
STATUS_THROTTLE_S = 30.0
STATUS_MIN_EDIT_S = 2.0
STATUS_POLL_S = 1.0
# #785: a batch whose wake turns all folded (no pushed message) gets one
# pushed "done" notice once its status message has finalised and the session
# has sat idle this long — long enough for the report turn the CLI usually
# opens right after the last task ends to break out (and push) instead.
QUIET_NOTICE_IDLE_S = 5.0
# Headroom under Telegram's 4096-char limit for the plain-text status message.
STATUS_MAX_CHARS = 3500

# Characters that would otherwise be read as markdown by the progress
# renderer (markdown-it → Telegram entities). Backslash-escaping them keeps a
# task description literal; HTML-shaped text is already escaped at that
# boundary (html disabled, #713).
_MD_SPECIAL = frozenset("\\`*_[]()~|<>#!&")


def escape_markdown(text: str) -> str:
    return "".join(f"\\{ch}" if ch in _MD_SPECIAL else ch for ch in text)


def format_tokens(value: float | int) -> str:
    """``950`` · ``9.5k`` · ``52k`` · ``1.2M`` (tokens, never cost)."""
    n = max(0, int(value))
    if n < 1000:
        return str(n)
    if n < 999_500:
        k = n / 1000
        if k < 10:
            return f"{k:.1f}".rstrip("0").rstrip(".") + "k"
        return f"{round(k)}k"
    m = n / 1_000_000
    if m < 10:
        return f"{m:.1f}".rstrip("0").rstrip(".") + "M"
    return f"{round(m)}M"


def format_bg_elapsed(seconds: float) -> str:
    """``45s`` · ``3m12s`` · ``1h05m``."""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def is_top_level_background(task: Any) -> bool:
    """A background task the session launched itself — not a subagent's own
    (foreground / nested) tool."""
    return bool(getattr(task, "is_backgrounded", False)) and not getattr(
        task, "owned_by_subagent", False
    )


def is_live(task: Any) -> bool:
    return getattr(task, "status", None) in LIVE_STATUSES


def live_top_level(tasks: Iterable[Any]) -> list[Any]:
    return [t for t in tasks if is_top_level_background(t) and is_live(t)]


def live_shown(tasks: Iterable[Any]) -> list[Any]:
    """The live background tasks to list: everything that holds the live
    session open (#801 ``holds_session`` — backgrounded and live, whoever
    launched it), except a subagent's own task while the agent that spawned
    it is itself listed — that agent's row (tokens, tools, current step)
    already covers it, and listing both double-counts one piece of work. Once
    the agent has ended, or its owner is unknown, the task is listed on its
    own: the panel must never say nothing is running while the session is
    held open (the resumed-agent ``sleep 75`` orphan, #801)."""
    held = [
        t for t in tasks if bool(getattr(t, "is_backgrounded", False)) and is_live(t)
    ]
    top = [t for t in held if not getattr(t, "owned_by_subagent", False)]
    covering = {getattr(t, "tool_use_id", None) for t in top} - {None}
    shown = list(top)
    for task in held:
        if not getattr(task, "owned_by_subagent", False):
            continue
        owner = getattr(task, "owner_tool_use_id", None)
        if owner is None or owner not in covering:
            shown.append(task)
    return shown


def _is_agent(task: Any) -> bool:
    return getattr(task, "task_type", None) == "local_agent"


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _ack_plain(text: str) -> str:
    """#891: a folded ack is the model's markdown, but the status message is
    plain text — show what the rendered answer would read, not ``**`` and
    backticks. Best-effort: on any renderer failure the raw text is kept."""
    from .telegram.render import markdown_to_plain

    try:
        return markdown_to_plain(text)
    except Exception:  # noqa: BLE001 — never lose an ack to the renderer
        logger.warning("background_status.ack_plain_failed", exc_info=True)
        return text


def task_label(task: Any, width: int = DESC_WIDTH) -> str:
    raw = (
        getattr(task, "description", None)
        or getattr(task, "task_type", None)
        or "background task"
    )
    return shorten(_one_line(raw), width) or "background task"


def task_elapsed(task: Any, now: float) -> float:
    """The task's total active time: its current run plus, for a continued
    (revived, #801) agent, the runs before it (``prior_active_s``, #892) —
    never the last leg alone beside lifetime tokens."""
    started = getattr(task, "started_at", None)
    if not isinstance(started, (int, float)):
        return 0.0
    ended = getattr(task, "ended_at", None)
    end = ended if isinstance(ended, (int, float)) else now
    prior = getattr(task, "prior_active_s", 0.0)
    if isinstance(prior, bool) or not isinstance(prior, (int, float)):
        prior = 0.0
    return max(0.0, end - started) + max(0.0, prior)


def _usage(task: Any, key: str) -> int | None:
    usage = getattr(task, "last_usage", None)
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _task_step(task: Any) -> str | None:
    step = getattr(task, "last_step", None) or getattr(task, "last_tool_name", None)
    if not step:
        return None
    return shorten(_one_line(step), STEP_WIDTH) or None


def format_live_row(task: Any, now: float, *, markdown: bool = False) -> str:
    """``🤖 verifier · 3m12s · 52k tok · 7 tools · Running tests`` /
    ``🐚 gh run watch · 1m05s``."""
    esc = escape_markdown if markdown else (lambda text: text)
    agent = _is_agent(task)
    parts = [
        f"{AGENT_ICON if agent else SHELL_ICON} {esc(task_label(task))}",
        format_bg_elapsed(task_elapsed(task, now)),
    ]
    if agent:
        tokens = _usage(task, "total_tokens")
        if tokens is not None:
            parts.append(f"{format_tokens(tokens)} tok")
        tools = _usage(task, "tool_uses")
        if tools is not None:
            parts.append(f"{tools} tool{'s' if tools != 1 else ''}")
        step = _task_step(task)
        if step:
            parts.append(esc(step))
    return " · ".join(parts)


def _end_mark(task: Any) -> tuple[str, str]:
    status = getattr(task, "status", None)
    if status in _DONE_STATUSES:
        return DONE_MARK, "done"
    if status in _FAIL_STATUSES:
        return FAIL_MARK, "failed"
    # killed / stopped — or still "running" in the map because the process
    # went away before the CLI could say (session closed / died).
    return STOP_MARK, "stopped"


def format_done_row(task: Any, now: float) -> str:
    """``✅ verifier · 4m20s · 61k tok`` / ``❌ verifier failed · …`` /
    ``⏹️ verifier stopped · …`` — ✅ already says "done", so a description
    ending in "done" doesn't read "done done"."""
    mark, word = _end_mark(task)
    label = task_label(task)
    parts = [f"{mark} {label}" if word == "done" else f"{mark} {label} {word}"]
    parts.append(format_bg_elapsed(task_elapsed(task, now)))
    tokens = _usage(task, "total_tokens")
    if tokens is not None:
        parts.append(f"{format_tokens(tokens)} tok")
    return " · ".join(parts)


def _live_rows(
    tasks: list[Any], now: float, max_rows: int, *, markdown: bool
) -> list[str]:
    rows = [format_live_row(t, now, markdown=markdown) for t in tasks[:max_rows]]
    extra = len(tasks) - len(rows)
    if extra > 0:
        rows.append(f"+{extra} more")
    return rows


def render_background_block(
    tasks: Iterable[Any], *, now: float, max_rows: int = 5
) -> str | None:
    """The pre-result progress block (markdown; descriptions escaped)."""
    live = sorted(live_shown(tasks), key=_start_key)
    if not live:
        return None
    lines = [f"{HOURGLASS} background ({len(live)})"]
    lines.extend(_live_rows(live, now, max(1, max_rows), markdown=True))
    return HARD_BREAK.join(lines)


def _start_key(task: Any) -> float:
    started = getattr(task, "started_at", None)
    return started if isinstance(started, (int, float)) else 0.0


# #785 part 2: a wake turn whose whole answer fits in this many characters
# is an "ack" ("The Trello sweep is back — waiting on the other three.") and
# can fold into the status message. nsd's acks were ~60-150 chars; the one
# compiled report was 6826. 300 is two or three sentences — the whole answer
# is shown on one line, so nothing is lost — and keeps a 5-row message well
# under Telegram's limit. The owner's suggested threshold.
FOLD_MAX_CHARS = 300
# Reasons a turn can fold for: a background finish (or a turn the CLI opened
# on one before naming it), a Monitor tick, a ScheduleWakeup firing.
FOLDABLE_REASONS = frozenset(
    {"task_finished", "unknown", "scheduled_wakeup", "monitor_event"}
)
# #813: read-only tools a wake turn uses to *collect* a finished task's
# result. Since CLI 2.1.277 (TaskOutput removed) the model reads the task's
# ``output_file`` with ``Read``; ``TaskOutput`` stays for older CLIs. A short
# ack whose only tools are these is still an ack — it folds.
COLLECTION_TOOLS = frozenset({"Read", "Glob", "Grep", "TaskOutput"})
# More collection calls than this is an investigation, not an ack.
COLLECTION_FOLD_MAX = 3
_NON_COLLECTION_KINDS = frozenset({"file_change", "command", "note"})


def is_collection_action(action: Any) -> bool:
    """#813: a read-only result-collection tool call (``Read`` of a task's
    output file, ``Glob``/``Grep``, legacy ``TaskOutput``). Writes, shell
    commands, approvals and questions never qualify."""
    if getattr(action, "kind", None) in _NON_COLLECTION_KINDS:
        return False
    detail = getattr(action, "detail", None)
    name = detail.get("name") if isinstance(detail, dict) else None
    return isinstance(name, str) and name in COLLECTION_TOOLS


def count_substantive_actions(actions: Iterable[Any]) -> int:
    """#785/#813: the actions that make a wake turn more than an ack —
    every non-note action except up to ``COLLECTION_FOLD_MAX`` read-only
    collection calls (beyond that, all of them count)."""
    tools = [a for a in actions if getattr(a, "kind", None) != "note"]
    collection = sum(1 for a in tools if is_collection_action(a))
    other = len(tools) - collection
    return other + (collection if collection > COLLECTION_FOLD_MAX else 0)


def wake_fold_decision(
    *,
    reason: str,
    ok: bool,
    answer: str,
    substantive_actions: int,
    already_announced: bool,
    live_tasks_remaining: int,
    batch_announced: bool = True,
) -> str:
    """``"fold"`` or why the turn breaks out as its own (pushed) message.

    Decided on content first — tools / approvals / questions (see
    ``count_substantive_actions``: a few read-only result-collection calls
    don't count, #813), a substantive answer, an error — because the CLI often opens the
    compiled-report turn as ``unknown`` before the last task's end event
    lands, so "is this the last task?" can't be known when the turn opens.
    At completion, though, a task_finished / unknown turn that left no
    background work running is treated as the report (``last_task``): it
    still breaks out, so the finish the user was waiting for pushes — unless
    it only repeats a finish already announced *and* this batch of background
    work already had a pushed wake message (``batch_announced``). That keeps
    one push per batch even when the report turn raced the task's end and
    folded as a short unattributed ack.
    """
    if reason not in FOLDABLE_REASONS:
        return "not_wake"
    if not ok:
        return "error"
    if substantive_actions > 0:
        return "tools"
    if len((answer or "").strip()) > FOLD_MAX_CHARS:
        return "long_answer"
    if live_tasks_remaining == 0 and (
        # a genuinely new finish (the report) — or any finish-ish turn in a
        # batch that hasn't pushed yet — breaks out;
        (reason == "task_finished" and not already_announced)
        or (reason in ("task_finished", "unknown") and not batch_announced)
    ):
        # an unnamed ``unknown`` turn after the batch already pushed is the
        # CLI's no-op follow-up ("nothing new since the report") and folds.
        return "last_task"
    return "fold"


_CLOSE_REASONS = {
    "max_hold": "background hold limit reached",
    "abs_cap": "session time limit reached",
    "cancel": "cancelled",
    "new": "new session started",
    "drain": "Untether restarting",
    "options_changed": "chat settings changed",
    "budget_stop": "cost budget reached",  # #896
    "idle_no_tasks": "session closed",
}


class BackgroundStatusPanel:
    """One post-result status message, edited in place (#777).

    Plain text (no entities / parse mode), so task descriptions can never be
    read as formatting. All writes go through the transport (the outbox).
    """

    def __init__(
        self,
        *,
        transport: Transport,
        channel_id: Any,
        thread_id: Any = None,
        clock: Callable[[], float] = time.monotonic,
        max_rows: int = 5,
        throttle_s: float = STATUS_THROTTLE_S,
        min_edit_s: float = STATUS_MIN_EDIT_S,
        persistence_path: Path | None = None,
    ) -> None:
        self._transport = transport
        self._channel_id = channel_id
        self._thread_id = thread_id
        self._clock = clock
        self.max_rows = max(1, int(max_rows))
        self._throttle_s = throttle_s
        self._min_edit_s = min_edit_s
        self._persistence_path = persistence_path
        self.tasks: dict[str, Any] = {}
        self.ref: MessageRef | None = None
        self.finalised = False
        self.close_reason: str | None = None
        self.last_text: str | None = None
        self._last_edit_at = 0.0
        self._last_sig: tuple[Any, ...] | None = None
        self._edits = 0
        # #785 part 2: short wake-turn acks folded in instead of pushed —
        # per task row, or unattributed (a turn no task event named).
        self.acks: dict[str, list[str]] = {}
        self.notes: list[str] = []
        # #813: unattributed acks by the wake turn that produced them, so a
        # task whose end the runner paired with that turn claims exactly
        # that note — never merely the latest one.
        self._note_turns: dict[int, str] = {}
        self.folds = 0
        # Wake turns delivered as their own pushed message while this was the
        # run's status message (see ``wake_fold_decision``'s batch rule).
        self.breakouts = 0
        self.reply_to: MessageRef | None = None
        self.quiet_notice_sent = False

    # ── model ────────────────────────────────────────────────────────────
    def track(self, tasks: Iterable[Any]) -> bool:
        """Add the listed live background tasks not yet on the panel."""
        added = False
        for task in live_shown(tasks):
            tid = str(getattr(task, "task_id", "") or id(task))
            if tid not in self.tasks:
                self.tasks[tid] = task
                added = True
        return added

    def live(self) -> list[Any]:
        return [t for t in self.tasks.values() if is_live(t)]

    def _signature(self) -> tuple[Any, ...]:
        return (
            tuple((tid, getattr(t, "status", None)) for tid, t in self.tasks.items()),
            self.finalised,
            self.close_reason,
            self.folds,
        )

    def _header(self, now: float) -> str:
        live = self.live()
        done = len(self.tasks) - len(live)
        if not self.finalised:
            head = f"{HOURGLASS} background ({len(live)})"
            return f"{head} · {done} done" if done else head
        n = len(self.tasks)
        marks = [_end_mark(t)[1] for t in self.tasks.values()]
        if marks and all(m == "done" for m in marks):
            if n == 1:
                return f"{DONE_MARK} background task done"
            return f"{DONE_MARK} all {n} background tasks done"
        counts = [
            f"{marks.count(word)} {word}"
            for word in ("done", "failed", "stopped")
            if marks.count(word)
        ]
        head = f"{STOP_MARK} background tasks ended · " + " · ".join(counts)
        if self.close_reason:
            why = _CLOSE_REASONS.get(self.close_reason, "session ended")
            head += f" ({why})"
        return head

    def render(self, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        ordered = sorted(self.tasks.values(), key=_start_key)
        live = [t for t in ordered if is_live(t) and not self.finalised]
        live_ids = {id(t) for t in live}
        done = [t for t in ordered if id(t) not in live_ids]
        lines = [self._header(now)]
        live_rows = _live_rows(live, now, self.max_rows, markdown=False)
        for task, row in zip(live, live_rows, strict=False):
            lines.append(row)
            lines.extend(self._ack_lines(task))
        lines.extend(live_rows[len(live) :])  # "+N more"
        # Rows carrying a folded ack are always shown — folding must never
        # lose content (#785); the rest collapse beyond the row cap.
        acked = [t for t in done if self.acks.get(self._tid(t))]
        plain = [t for t in done if not self.acks.get(self._tid(t))]
        keep = max(0, self.max_rows - len(acked))
        kept_ids = {id(t) for t in acked} | {
            id(t) for t in (plain[-keep:] if keep else [])
        }
        hidden = len(done) - len(kept_ids)
        if hidden > 0:
            lines.append(f"+{hidden} more ended")
        for task in done:
            if id(task) in kept_ids:
                lines.append(format_done_row(task, now))
                lines.extend(self._ack_lines(task))
        lines.extend(f"{NOTE_MARK} {note}" for note in self.notes)
        text = "\n".join(lines)
        if len(text) > STATUS_MAX_CHARS:
            text = text[: STATUS_MAX_CHARS - 1] + "…"
        return text

    def _tid(self, task: Any) -> str:
        return str(getattr(task, "task_id", "") or id(task))

    def _ack_lines(self, task: Any) -> list[str]:
        return [f"   ↳ {ack}" for ack in self.acks.get(self._tid(task), [])]

    def _claim_turn_notes(self, target: str, turns: Iterable[int]) -> bool:
        """#813: move the unattributed acks of ``turns`` onto ``target``'s
        row — the CLI answered one finish twice, first in a turn no task
        event named (the runner paired the task's end with it), then in the
        task's own turn. With no turn key nothing is claimed: the note stays
        unattributed, which is never misfiled and never lost."""
        if target not in self.tasks:
            return False
        claimed = False
        for turn in turns:
            note = self._note_turns.pop(turn, None)
            if note is None:
                continue
            if note in self.notes and note not in self._note_turns.values():
                self.notes.remove(note)
            bucket = self.acks.setdefault(target, [])
            if note not in bucket:
                bucket.append(note)
            claimed = True
        return claimed

    async def attribute_turn_notes(
        self, task_ids: Iterable[str], announced_turns: Iterable[int]
    ) -> None:
        """A task's own (already-announced) turn broke out as a message:
        still file the ack of the turn its end was paired with under that
        task's row."""
        target = next((tid for tid in task_ids if tid in self.tasks), None)
        if target is not None and self._claim_turn_notes(target, announced_turns):
            await self._edit(self.render())

    async def fold(
        self,
        text: str,
        *,
        task_ids: Iterable[str] = (),
        already_announced: bool = False,
        turn: int | None = None,
        announced_turns: Iterable[int] = (),
    ) -> bool:
        """#785 part 2: record a short wake-turn answer on this message
        instead of a new pushed one. False (nothing changed) when it can't be
        shown in full — the caller then delivers the turn normally."""
        if self.ref is None:
            return False
        saved = (
            {k: list(v) for k, v in self.acks.items()},
            list(self.notes),
            dict(self._note_turns),
        )
        ack = _one_line(_ack_plain(text))
        target = next((tid for tid in task_ids if tid in self.tasks), None)
        if target is not None and already_announced:
            self._claim_turn_notes(target, announced_turns)
        if ack:
            if target is not None:
                bucket = self.acks.setdefault(target, [])
                if ack not in bucket:
                    bucket.append(ack)
            else:
                if ack not in self.notes:
                    self.notes.append(ack)
                if turn is not None:
                    self._note_turns[turn] = ack
        if len(self.render()) >= STATUS_MAX_CHARS:  # would be truncated
            self.acks, self.notes, self._note_turns = saved
            return False
        self.folds += 1
        if not self.finalised and not self.live():
            await self.finalise()
        else:
            await self._edit(self.render())
        logger.info(
            "background_status.folded",
            channel_id=self._channel_id,
            message_id=self.ref.message_id,
            task_id=target,
            ack_len=len(ack),
            finalised=self.finalised,
        )
        return True

    # ── transport ────────────────────────────────────────────────────────
    async def open(self, reply_to: MessageRef | None) -> bool:
        text = self.render()
        try:
            ref = await self._transport.send(
                channel_id=self._channel_id,
                message=RenderedMessage(text=text),
                options=SendOptions(
                    reply_to=reply_to, notify=False, thread_id=self._thread_id
                ),
            )
        except Exception:  # noqa: BLE001 — a status panel is best-effort
            logger.warning("background_status.send_failed", exc_info=True)
            return False
        if ref is None:
            return False
        self.ref = ref
        self.reply_to = reply_to
        self.last_text = text
        self._last_edit_at = self._clock()
        self._last_sig = self._signature()
        self._persist(register=True)
        logger.info(
            "background_status.opened",
            channel_id=self._channel_id,
            message_id=ref.message_id,
            tasks=len(self.tasks),
        )
        return True

    async def _edit(self, text: str) -> None:
        if self.ref is None or text == self.last_text:
            self._last_sig = self._signature()
            return
        try:
            await self._transport.edit(ref=self.ref, message=RenderedMessage(text=text))
        except Exception:  # noqa: BLE001
            logger.debug("background_status.edit_failed", exc_info=True)
            return
        self.last_text = text
        self._last_edit_at = self._clock()
        self._last_sig = self._signature()
        self._edits += 1

    async def sync(self, tasks: Iterable[Any], *, force: bool = False) -> None:
        """Poll step: pick up new tasks, finalise when the set empties, and
        edit when something changed (early) or the throttle elapsed."""
        if self.finalised or self.ref is None:
            return
        self.track(tasks)
        if not self.live():
            await self.finalise()
            return
        now = self._clock()
        since = now - self._last_edit_at
        changed = self._signature() != self._last_sig
        if (
            force
            or (changed and since >= self._min_edit_s)
            or since >= self._throttle_s
        ):
            await self._edit(self.render())

    async def finalise(self, reason: str | None = None) -> None:
        if self.finalised:
            return
        if reason is not None and any(
            _end_mark(t)[1] != "done" for t in self.tasks.values()
        ):
            self.close_reason = reason
        self.finalised = True
        await self._edit(self.render())
        self._persist(register=False)
        logger.info(
            "background_status.finalised",
            channel_id=self._channel_id,
            message_id=self.ref.message_id if self.ref else None,
            tasks=len(self.tasks),
            reason=self.close_reason,
            edits=self._edits,
        )

    @property
    def owes_quiet_notice(self) -> bool:
        """Finalised on its own (not closed early) after folding acks, and no
        wake turn of this batch ever pushed."""
        return (
            self.finalised
            and self.close_reason is None
            and self.folds > 0
            and self.breakouts == 0
            and not self.quiet_notice_sent
        )

    async def send_quiet_notice(self) -> bool:
        self.quiet_notice_sent = True
        text = self._header(self._clock())
        try:
            await self._transport.send(
                channel_id=self._channel_id,
                message=RenderedMessage(text=text),
                options=SendOptions(
                    reply_to=self.reply_to, notify=True, thread_id=self._thread_id
                ),
            )
        except Exception:  # noqa: BLE001
            logger.warning("background_status.quiet_notice_failed", exc_info=True)
            return False
        logger.info(
            "background_status.quiet_notice",
            channel_id=self._channel_id,
            message_id=self.ref.message_id if self.ref else None,
            folds=self.folds,
        )
        return True

    def _persist(self, *, register: bool) -> None:
        if self._persistence_path is None or self.ref is None:
            return
        try:
            from .telegram.progress_persistence import (
                register_progress,
                unregister_progress,
            )

            key = f"{self._channel_id}:{self.ref.message_id}"
            if register:
                register_progress(
                    self._persistence_path,
                    key,
                    int(self._channel_id),
                    int(self.ref.message_id),
                )
            else:
                unregister_progress(self._persistence_path, key)
        except Exception:  # noqa: BLE001
            logger.debug("background_status.persist_failed", exc_info=True)


class BackgroundStatusManager:
    """Owns a live run's post-result status messages (#777).

    ``after_turn`` is called once the run's answer (and each later turn's) has
    been delivered: with live background tasks and no active panel it opens
    one. ``run`` polls the active panel; ``aclose`` finalises it when the run
    ends (cancel / close / drain / process death).
    """

    def __init__(
        self,
        *,
        transport: Transport,
        channel_id: Any,
        thread_id: Any = None,
        tasks_source: Callable[[], list[Any]],
        anchor_for: Callable[[list[Any]], MessageRef | None],
        settings_source: Callable[[], Any],
        clock: Callable[[], float] = time.monotonic,
        persistence_path: Path | None = None,
        poll_s: float = STATUS_POLL_S,
        idle_source: Callable[[], bool] | None = None,
        quiet_notice_idle_s: float = QUIET_NOTICE_IDLE_S,
        throttle_s: float = STATUS_THROTTLE_S,
        min_edit_s: float = STATUS_MIN_EDIT_S,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        self._transport = transport
        self._channel_id = channel_id
        self._thread_id = thread_id
        self._tasks_source = tasks_source
        self._anchor_for = anchor_for
        self._settings_source = settings_source
        self._clock = clock
        self._persistence_path = persistence_path
        self._poll_s = poll_s
        # True while the live session sits between turns (no turn running).
        self._idle_source = idle_source
        self._quiet_notice_idle_s = quiet_notice_idle_s
        self._idle_since: float | None = None
        self._throttle_s = throttle_s
        self._min_edit_s = min_edit_s
        self._sleep = sleep
        self.panel: BackgroundStatusPanel | None = None
        self.panels_opened = 0
        self._lock = anyio.Lock()
        # The poll loop sleeps until a status message exists (most runs
        # never open one).
        self._opened = anyio.Event()

    def settings(self) -> Any:
        try:
            return self._settings_source()
        except Exception:  # noqa: BLE001
            logger.debug("background_status.settings_failed", exc_info=True)
            return None

    def _tasks(self) -> list[Any]:
        try:
            return list(self._tasks_source())
        except Exception:  # noqa: BLE001
            return []

    def live_count(self) -> int:
        return len(live_shown(self._tasks()))

    @property
    def active(self) -> BackgroundStatusPanel | None:
        panel = self.panel
        return panel if panel is not None and not panel.finalised else None

    async def after_turn(self) -> None:
        async with self._lock:
            settings = self.settings()
            if settings is not None and not getattr(
                settings, "show_background_tasks", True
            ):
                return
            tasks = self._tasks()
            if (panel := self.active) is not None:
                panel.max_rows = max(
                    1, int(getattr(settings, "background_tasks_max_rows", 5))
                )
                await panel.sync(tasks, force=panel.track(tasks))
                return
            live = live_shown(tasks)
            if not live:
                return
            panel = BackgroundStatusPanel(
                transport=self._transport,
                channel_id=self._channel_id,
                thread_id=self._thread_id,
                clock=self._clock,
                max_rows=getattr(settings, "background_tasks_max_rows", 5),
                throttle_s=self._throttle_s,
                min_edit_s=self._min_edit_s,
                persistence_path=self._persistence_path,
            )
            panel.track(live)
            if await panel.open(self._anchor_for(live)):
                self.panel = panel
                self.panels_opened += 1
                self._opened.set()

    async def poll_once(self) -> None:
        async with self._lock:
            panel = self.active
            if panel is not None:
                await panel.sync(self._tasks())
            await self._maybe_quiet_notice()

    def _session_idle(self) -> bool:
        if self._idle_source is None:
            return True
        try:
            return bool(self._idle_source())
        except Exception:  # noqa: BLE001
            return False

    async def _maybe_quiet_notice(self, *, now_if_owed: bool = False) -> None:
        """#785: never let a batch whose acks all folded end without a push.
        Waits for the session to stay idle a moment after the status message
        finalised: the report turn usually follows the last task's end, and
        if it breaks out it pushes (and cancels this)."""
        panel = self.panel
        if panel is None or not panel.owes_quiet_notice:
            self._idle_since = None
            return
        if now_if_owed:
            await panel.send_quiet_notice()
            return
        if not self._session_idle():
            self._idle_since = None
            return
        now = self._clock()
        if self._idle_since is None:
            self._idle_since = now
        if now - self._idle_since >= self._quiet_notice_idle_s:
            await panel.send_quiet_notice()

    async def run(self) -> None:
        await self._opened.wait()
        while True:
            await self._sleep(self._poll_s)
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 — never break the run
                logger.debug("background_status.poll_failed", exc_info=True)

    @property
    def fold_target(self) -> BackgroundStatusPanel | None:
        """The run's latest status message (active or finalised) — where a
        short wake-turn ack folds (#785 part 2)."""
        panel = self.panel
        return panel if panel is not None and panel.ref is not None else None

    def note_breakout(self) -> None:
        if (panel := self.fold_target) is not None:
            panel.breakouts += 1

    async def attribute_turn_notes(
        self, task_ids: Iterable[str], announced_turns: Iterable[int]
    ) -> None:
        async with self._lock:
            if (panel := self.fold_target) is not None:
                await panel.attribute_turn_notes(
                    [str(t) for t in task_ids], list(announced_turns)
                )

    async def fold(
        self,
        text: str,
        *,
        task_ids: Iterable[str] = (),
        already_announced: bool = False,
        turn: int | None = None,
        announced_turns: Iterable[int] = (),
    ) -> bool:
        async with self._lock:
            panel = self.fold_target
            if panel is None:
                return False
            if not panel.finalised:
                # Pick up tasks launched since the last poll first, or a fold
                # could finalise a message while new work still runs.
                panel.track(self._tasks())
            ids = [str(t) for t in task_ids]
            by_id = {
                str(getattr(t, "task_id", "")): t
                for t in self._tasks()
                if getattr(t, "is_backgrounded", False)
            }
            for tid in ids:
                if tid not in panel.tasks and tid in by_id:
                    panel.tasks[tid] = by_id[tid]
            return await panel.fold(
                text,
                task_ids=ids,
                already_announced=already_announced,
                turn=turn,
                announced_turns=list(announced_turns),
            )

    async def aclose(self, reason: str | None) -> None:
        async with self._lock:
            panel = self.active
            if panel is not None:
                panel.track(self._tasks())
                await panel.finalise(reason)
            if reason in (None, "idle_no_tasks"):
                # The run ended normally before the idle wait ran out.
                await self._maybe_quiet_notice(now_if_owed=True)


# ── /ping: live background count per chat (#777) ─────────────────────────────

_COUNT_SOURCES: dict[int, tuple[Any, Callable[[], int]]] = {}
_next_token = 0


def register_live_count_source(channel_id: Any, source: Callable[[], int]) -> int:
    global _next_token
    _next_token += 1
    _COUNT_SOURCES[_next_token] = (channel_id, source)
    return _next_token


def unregister_live_count_source(token: int) -> None:
    _COUNT_SOURCES.pop(token, None)


def chat_live_background_count(channel_id: Any) -> int:
    total = 0
    for chan, source in list(_COUNT_SOURCES.values()):
        if chan != channel_id:
            continue
        try:
            total += int(source())
        except Exception:  # noqa: BLE001
            # A run that is tearing down can't be counted; /ping must not fail.
            logger.debug("background_status.count_source_failed", exc_info=True)
    return total
