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


def _is_agent(task: Any) -> bool:
    return getattr(task, "task_type", None) == "local_agent"


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def task_label(task: Any, width: int = DESC_WIDTH) -> str:
    raw = (
        getattr(task, "description", None)
        or getattr(task, "task_type", None)
        or "background task"
    )
    return shorten(_one_line(raw), width) or "background task"


def task_elapsed(task: Any, now: float) -> float:
    started = getattr(task, "started_at", None)
    if not isinstance(started, (int, float)):
        return 0.0
    ended = getattr(task, "ended_at", None)
    end = ended if isinstance(ended, (int, float)) else now
    return max(0.0, end - started)


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
    """``✅ verifier done · 4m20s · 61k tok`` (❌ failed, ⏹ stopped)."""
    mark, word = _end_mark(task)
    parts = [f"{mark} {task_label(task)} {word}"]
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
    live = sorted(live_top_level(tasks), key=_start_key)
    if not live:
        return None
    lines = [f"{HOURGLASS} background ({len(live)})"]
    lines.extend(_live_rows(live, now, max(1, max_rows), markdown=True))
    return HARD_BREAK.join(lines)


def _start_key(task: Any) -> float:
    started = getattr(task, "started_at", None)
    return started if isinstance(started, (int, float)) else 0.0


_CLOSE_REASONS = {
    "max_hold": "background hold limit reached",
    "abs_cap": "session time limit reached",
    "cancel": "cancelled",
    "new": "new session started",
    "drain": "Untether restarting",
    "options_changed": "chat settings changed",
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

    # ── model ────────────────────────────────────────────────────────────
    def track(self, tasks: Iterable[Any]) -> bool:
        """Add live top-level background tasks not yet on the panel."""
        added = False
        for task in live_top_level(tasks):
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
        lines.extend(_live_rows(live, now, self.max_rows, markdown=False))
        shown = done[-self.max_rows :]
        hidden = len(done) - len(shown)
        if hidden > 0:
            lines.append(f"+{hidden} more ended")
        lines.extend(format_done_row(t, now) for t in shown)
        text = "\n".join(lines)
        if len(text) > STATUS_MAX_CHARS:
            text = text[: STATUS_MAX_CHARS - 1] + "…"
        return text

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
        return len(live_top_level(self._tasks()))

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
            live = live_top_level(tasks)
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

    async def run(self) -> None:
        await self._opened.wait()
        while True:
            await self._sleep(self._poll_s)
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 — never break the run
                logger.debug("background_status.poll_failed", exc_info=True)

    async def aclose(self, reason: str | None) -> None:
        async with self._lock:
            panel = self.active
            if panel is None:
                return
            panel.track(self._tasks())
            await panel.finalise(reason)


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
            continue
    return total
