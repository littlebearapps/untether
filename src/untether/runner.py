"""Runner protocol and shared runner definitions."""

from __future__ import annotations

import json
import re
import signal
import subprocess
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, cast
from weakref import WeakValueDictionary

import anyio

from .logging import get_logger, log_pipeline
from .model import (
    Action,
    ActionEvent,
    CompletedEvent,
    EngineId,
    ResumeToken,
    StartedEvent,
    UntetherEvent,
)
from .utils.paths import get_run_base_dir
from .utils.streams import drain_stderr, iter_bytes_lines
from .utils.subprocess import manage_subprocess

_lock_logger = get_logger(__name__)


class ResumeTokenMixin:
    engine: EngineId
    resume_re: re.Pattern[str]

    def format_resume(self, token: ResumeToken) -> str:
        if token.engine != self.engine:
            raise RuntimeError(f"resume token is for engine {token.engine!r}")
        return f"`{self.engine} resume {token.value}`"

    def is_resume_line(self, line: str) -> bool:
        return bool(self.resume_re.match(line))

    def extract_resume(self, text: str | None) -> ResumeToken | None:
        if not text:
            return None
        found: str | None = None
        for match in self.resume_re.finditer(text):
            token = match.group("token")
            if token:
                found = token
        if not found:
            return None
        _lock_logger.debug(
            "session.resume_token.found", engine=str(self.engine), session_id=found[:8]
        )
        return ResumeToken(engine=self.engine, value=found)


class SessionLockMixin:
    engine: EngineId
    session_locks: WeakValueDictionary[str, anyio.Semaphore] | None = None

    def lock_for(self, token: ResumeToken) -> anyio.Semaphore:
        locks = self.session_locks
        if locks is None:
            locks = WeakValueDictionary()
            self.session_locks = locks
        key = f"{token.engine}:{token.value}"
        lock = locks.get(key)
        if lock is None:
            lock = anyio.Semaphore(1)
            locks[key] = lock
        return lock

    async def run_with_resume_lock(
        self,
        prompt: str,
        resume: ResumeToken | None,
        run_fn: Callable[[str, ResumeToken | None], AsyncIterator[UntetherEvent]],
    ) -> AsyncIterator[UntetherEvent]:
        resume_token = resume
        if resume_token is not None and resume_token.engine != self.engine:
            raise RuntimeError(
                f"resume token is for engine {resume_token.engine!r}, not {self.engine!r}"
            )
        if resume_token is None:
            async for evt in run_fn(prompt, resume_token):
                yield evt
            return
        lock = self.lock_for(resume_token)
        async with lock:
            async for evt in run_fn(prompt, resume_token):
                yield evt


def _rc_label(rc: int) -> str:
    """Format exit code, adding signal name for negative rc values."""
    if rc < 0:
        try:
            name = signal.Signals(-rc).name
        except (ValueError, AttributeError):
            name = f"signal {-rc}"
        return f"rc={rc} ({name})"
    return f"rc={rc}"


_ABS_PATH_RE = re.compile(r"(/[\w./-]{3,}/[\w.-]+)")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")


_TOOL_RESULT_EVENT_KIND = "tool_result"
_ASSISTANT_EVENT_KIND = "assistant"
_OTHER_EVENT_KIND = "other"

# Engine-agnostic classification of raw JSONL events for the
# stuck-after-tool_result detector (#322). See docs/reference/runners/*/
# for each engine's event shape.
_CODEX_TOOL_ITEM_TYPES = frozenset(
    {"mcp_tool_call", "command_execution", "file_change", "web_search"}
)
_OPENCODE_TOOL_STATUSES = frozenset({"completed", "error"})


def _classify_jsonl_event(raw: Any) -> str:
    """Return "tool_result" | "assistant" | "other" for a decoded JSONL event.

    Engine-agnostic: handles Claude, Codex, OpenCode, Pi, Gemini, AMP.
    Conservative — unknown shapes return "other".
    """
    if not isinstance(raw, dict):
        return _OTHER_EVENT_KIND
    t = raw.get("type")
    if not isinstance(t, str):
        return _OTHER_EVENT_KIND
    # Claude / AMP: role=user message whose content contains a tool_result block
    if t == "user":
        msg = raw.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        return _TOOL_RESULT_EVENT_KIND
        return _OTHER_EVENT_KIND
    # Pi direct tool_result events
    if t in {"tool_result", "ToolExecutionEnd"}:
        return _TOOL_RESULT_EVENT_KIND
    # Codex: item.completed (and item.updated with terminal status) for tool items
    if t in {"item.completed", "item.updated"}:
        item = raw.get("item")
        if isinstance(item, dict) and item.get("type") in _CODEX_TOOL_ITEM_TYPES:
            status = item.get("status")
            if t == "item.completed" or status in {"completed", "failed"}:
                return _TOOL_RESULT_EVENT_KIND
        # Codex agent_message completion is an assistant signal
        if (
            isinstance(item, dict)
            and item.get("type") == "agent_message"
            and t == "item.completed"
        ):
            return _ASSISTANT_EVENT_KIND
        return _OTHER_EVENT_KIND
    # OpenCode: ToolUse event (or message.part.updated) carrying a part with
    # terminal status. Normalised ToolUse shape first, then raw shape.
    if t == "ToolUse":
        state_block = raw.get("state")
        if (
            isinstance(state_block, dict)
            and state_block.get("status") in _OPENCODE_TOOL_STATUSES
        ):
            return _TOOL_RESULT_EVENT_KIND
        return _OTHER_EVENT_KIND
    if t == "message.part.updated":
        props = raw.get("properties")
        part = props.get("part") if isinstance(props, dict) else raw.get("part")
        if isinstance(part, dict) and part.get("type") == "tool":
            state_block = part.get("state")
            if (
                isinstance(state_block, dict)
                and state_block.get("status") in _OPENCODE_TOOL_STATUSES
            ):
                return _TOOL_RESULT_EVENT_KIND
        return _OTHER_EVENT_KIND
    # Assistant-turn signals (clear the tool_result latch so the detector
    # correctly sees "recovered" if the engine resumes).
    if t in {"assistant", "message.updated", "agent_message"}:
        return _ASSISTANT_EVENT_KIND
    return _OTHER_EVENT_KIND


def _sanitise_stderr(text: str) -> str:
    """Redact absolute paths and URLs from stderr before exposing to users."""
    text = _ABS_PATH_RE.sub("[path]", text)
    text = _URL_RE.sub("[url]", text)
    return text


def _stderr_excerpt(lines: list[str] | None, max_chars: int = 300) -> str | None:
    """First ~max_chars of captured stderr, sanitised for user display."""
    if not lines:
        return None
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return _sanitise_stderr(text)


def _session_label(
    found_session: ResumeToken | None,
    resume: ResumeToken | None,
) -> str | None:
    """Short session ID (8 chars) with resumed/new indicator."""
    token = found_session or resume
    if token is None:
        return None
    sid = token.value[:8]
    status = "resumed" if resume is not None else "new"
    return f"{sid} · {status}"


class BaseRunner(SessionLockMixin):
    engine: EngineId

    def run(
        self, prompt: str, resume: ResumeToken | None
    ) -> AsyncIterator[UntetherEvent]:
        return self.run_locked(prompt, resume)

    async def run_locked(
        self, prompt: str, resume: ResumeToken | None
    ) -> AsyncIterator[UntetherEvent]:
        if resume is not None:
            async for evt in self.run_with_resume_lock(prompt, resume, self.run_impl):
                yield evt
            return

        lock: anyio.Semaphore | None = None
        acquired = False
        try:
            async for evt in self.run_impl(prompt, None):
                if lock is None and isinstance(evt, StartedEvent):
                    lock = self.lock_for(evt.resume)
                    await lock.acquire()
                    acquired = True
                    _lock_logger.debug(
                        "session_lock.acquired",
                        session_id=evt.resume.value,
                        engine=str(self.engine),
                    )
                yield evt
        finally:
            if acquired and lock is not None:
                lock.release()
                _lock_logger.debug("session_lock.released", engine=str(self.engine))

    async def run_impl(
        self, prompt: str, resume: ResumeToken | None
    ) -> AsyncIterator[UntetherEvent]:
        if False:
            yield  # pragma: no cover
        raise NotImplementedError


@dataclass(slots=True)
class JsonlRunState:
    note_seq: int = 0


@dataclass(slots=True)
class JsonlStreamState:
    expected_session: ResumeToken | None
    found_session: ResumeToken | None = None
    did_emit_completed: bool = False
    ignored_after_completed: bool = False
    jsonl_seq: int = 0
    # Activity tracking for stall diagnostics
    last_stdout_at: float = 0.0
    last_event_type: str | None = None
    last_event_tool: str | None = None
    event_count: int = 0
    recent_events: deque[tuple[float, str]] = field(
        default_factory=lambda: deque(maxlen=10)
    )
    stderr_capture: list[str] = field(default_factory=list)
    proc_returncode: int | None = None
    # Stuck-after-tool_result detector (#322). Engine-agnostic signal:
    # set when a tool_result-equivalent event arrives, cleared when an
    # assistant-turn-start event arrives. When non-zero and elapsed > threshold,
    # indicates Claude (or any engine) received a tool result but has not
    # emitted a follow-up assistant turn.
    last_event_kind: str = "other"
    last_tool_result_at: float = 0.0
    # #346 Engine-specific state handle for detectors that need deeper
    # signals (e.g. Claude's background-task tracking from #347). The
    # wedge detector duck-types against this — if the engine state exposes
    # `has_live_background_work()`-style info it can gate SIGTERM. Engines
    # without background-task awareness leave this None.
    engine_state: Any = None


class JsonlSubprocessRunner(BaseRunner):
    # Exposed for diagnostics — set during run_impl, cleared on exit
    current_stream: JsonlStreamState | None = None
    last_pid: int | None = None

    def get_logger(self) -> Any:
        return getattr(self, "logger", get_logger(__name__))

    def command(self) -> str:
        raise NotImplementedError

    def tag(self) -> str:
        return str(self.engine)

    def build_args(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: Any,
    ) -> list[str]:
        raise NotImplementedError

    def stdin_payload(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: Any,
    ) -> bytes | None:
        return prompt.encode()

    def env(self, *, state: Any) -> dict[str, str] | None:
        return None

    def new_state(self, prompt: str, resume: ResumeToken | None) -> Any:
        return JsonlRunState()

    def start_run(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: Any,
    ) -> None:
        return None

    def pipes_error_message(self) -> str:
        return f"{self.tag()} failed to open subprocess pipes"

    def next_note_id(self, state: Any) -> str:
        try:
            note_seq = state.note_seq
        except AttributeError as exc:
            raise RuntimeError(
                "state must define note_seq or override next_note_id"
            ) from exc
        state.note_seq = note_seq + 1
        return f"{self.tag()}.note.{state.note_seq}"

    def note_event(
        self,
        message: str,
        *,
        state: Any,
        ok: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> UntetherEvent:
        note_id = self.next_note_id(state)
        action = Action(
            id=note_id,
            kind="warning",
            title=message,
            detail=detail or {},
        )
        return ActionEvent(
            engine=self.engine,
            action=action,
            phase="completed",
            ok=ok,
            message=message,
            level="info" if ok else "warning",
        )

    def invalid_json_events(
        self,
        *,
        raw: str,
        line: str,
        state: Any,
    ) -> list[UntetherEvent]:
        message = f"invalid JSON from {self.tag()}; ignoring line"
        return [self.note_event(message, state=state, detail={"line": line})]

    @staticmethod
    def sanitize_prompt(prompt: str) -> str:
        """Prevent flag injection by prepending a space to flag-like prompts.

        If a user prompt starts with ``-``, CLI argument parsers may interpret
        it as a flag.  Prepending a space neutralises this without altering the
        prompt semantics for the engine.
        """
        if prompt.startswith("-"):
            return f" {prompt}"
        return prompt

    def decode_jsonl(self, *, line: bytes) -> Any | None:
        text = line.decode("utf-8", errors="replace")
        try:
            return cast(dict[str, Any], json.loads(text))
        except json.JSONDecodeError:
            # Some CLIs (e.g. Gemini) mix non-JSON warnings with JSONL on
            # stdout.  Try to extract the first JSON object from the line.
            brace = text.find("{")
            if brace > 0:
                try:
                    return cast(dict[str, Any], json.loads(text[brace:]))
                except json.JSONDecodeError:
                    pass
            self.get_logger().warning(
                "runner.jsonl.decode_failed",
                engine=self.engine,
                line=text[:200],
            )
            return None

    async def iter_json_lines(
        self,
        stream: Any,
    ) -> AsyncIterator[bytes]:
        async for raw_line in iter_bytes_lines(stream):
            yield raw_line.rstrip(b"\n")

    def decode_error_events(
        self,
        *,
        raw: str,
        line: str,
        error: Exception,
        state: Any,
    ) -> list[UntetherEvent]:
        message = f"invalid event from {self.tag()}; ignoring line"
        detail = {"line": line, "error": str(error)}
        return [self.note_event(message, state=state, detail=detail)]

    def translate_error_events(
        self,
        *,
        data: Any,
        error: Exception,
        state: Any,
    ) -> list[UntetherEvent]:
        message = f"{self.tag()} translation error; ignoring event"
        detail: dict[str, Any] = {"error": str(error)}
        if isinstance(data, dict):
            detail["type"] = data.get("type")
            item = data.get("item")
            if isinstance(item, dict):
                detail["item_type"] = item.get("type") or item.get("item_type")
        return [self.note_event(message, state=state, detail=detail)]

    def process_error_events(
        self,
        rc: int,
        *,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
        state: Any,
        stderr_lines: list[str] | None = None,
    ) -> list[UntetherEvent]:
        parts = [f"{self.tag()} failed ({_rc_label(rc)})."]
        session = _session_label(found_session, resume)
        if session:
            parts.append(f"session: {session}")
        excerpt = _stderr_excerpt(stderr_lines)
        if excerpt:
            parts.append(excerpt)
        message = "\n".join(parts)
        resume_for_completed = found_session or resume
        return [
            self.note_event(message, state=state),
            CompletedEvent(
                engine=self.engine,
                ok=False,
                answer="",
                resume=resume_for_completed,
                error=message,
            ),
        ]

    def stream_end_events(
        self,
        *,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
        state: Any,
    ) -> list[UntetherEvent]:
        parts = [f"{self.tag()} finished without a result event"]
        session = _session_label(found_session, resume)
        if session:
            parts.append(f"session: {session}")
        message = "\n".join(parts)
        resume_for_completed = found_session or resume
        return [
            CompletedEvent(
                engine=self.engine,
                ok=False,
                answer="",
                resume=resume_for_completed,
                error=message,
            )
        ]

    def translate(
        self,
        data: Any,
        *,
        state: Any,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
    ) -> list[UntetherEvent]:
        raise NotImplementedError

    def handle_started_event(
        self,
        event: StartedEvent,
        *,
        expected_session: ResumeToken | None,
        found_session: ResumeToken | None,
    ) -> tuple[ResumeToken | None, bool]:
        if event.engine != self.engine:
            raise RuntimeError(
                f"{self.tag()} emitted session token for engine {event.engine!r}"
            )
        if (
            expected_session is not None
            and not expected_session.is_continue
            and event.resume != expected_session
        ):
            message = (
                f"{self.tag()} emitted session id {event.resume.value} "
                f"but expected {expected_session.value}"
            )
            raise RuntimeError(message)
        if found_session is None:
            return event.resume, True
        if event.resume != found_session:
            message = (
                f"{self.tag()} emitted session id {event.resume.value} "
                f"but expected {found_session.value}"
            )
            raise RuntimeError(message)
        # #225: when the event carries meta, treat it as a supplementary
        # StartedEvent — engines emit these to propagate late-arriving
        # metadata (e.g. pi.py ships the model from message_end once known).
        # ProgressTracker.note_event merges meta idempotently, so re-emission
        # is safe. True duplicates (no meta) continue to be dropped.
        if event.meta:
            return found_session, True
        return found_session, False

    async def _send_payload(
        self,
        proc: Any,
        payload: bytes | None,
        *,
        logger: Any,
        resume: ResumeToken | None,
    ) -> None:
        if payload is not None:
            assert proc.stdin is not None
            await proc.stdin.send(payload)
            await proc.stdin.aclose()
            logger.info(
                "subprocess.stdin.send",
                pid=proc.pid,
                resume=resume.value if resume else None,
                bytes=len(payload),
            )
        elif proc.stdin is not None:
            await proc.stdin.aclose()

    def _decode_jsonl_events(
        self,
        *,
        raw_line: bytes,
        line: bytes,
        jsonl_seq: int,
        state: Any,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
        logger: Any,
        pid: int,
    ) -> list[UntetherEvent]:
        raw_text = raw_line.decode("utf-8", errors="replace")
        line_text = line.decode("utf-8", errors="replace")
        try:
            decoded = self.decode_jsonl(line=line)
        except Exception as exc:  # noqa: BLE001
            log_pipeline(
                logger,
                "jsonl.parse.error",
                pid=pid,
                jsonl_seq=jsonl_seq,
                line=line_text,
                error=str(exc),
            )
            return self.decode_error_events(
                raw=raw_text,
                line=line_text,
                error=exc,
                state=state,
            )
        if decoded is None:
            log_pipeline(
                logger,
                "jsonl.parse.invalid",
                pid=pid,
                jsonl_seq=jsonl_seq,
                line=line_text,
            )
            logger.info(
                "runner.jsonl.invalid",
                pid=pid,
                jsonl_seq=jsonl_seq,
                line=line_text,
            )
            return self.invalid_json_events(
                raw=raw_text,
                line=line_text,
                state=state,
            )
        try:
            return self.translate(
                decoded,
                state=state,
                resume=resume,
                found_session=found_session,
            )
        except Exception as exc:  # noqa: BLE001
            log_pipeline(
                logger,
                "runner.translate.error",
                pid=pid,
                jsonl_seq=jsonl_seq,
                error=str(exc),
            )
            return self.translate_error_events(
                data=decoded,
                error=exc,
                state=state,
            )

    def _process_started_event(
        self,
        event: StartedEvent,
        *,
        expected_session: ResumeToken | None,
        found_session: ResumeToken | None,
        logger: Any,
        pid: int,
        jsonl_seq: int,
    ) -> tuple[ResumeToken | None, bool]:
        prior_found = found_session
        try:
            found_session, emit = self.handle_started_event(
                event,
                expected_session=expected_session,
                found_session=found_session,
            )
        except Exception as exc:
            log_pipeline(
                logger,
                "runner.started.error",
                pid=pid,
                jsonl_seq=jsonl_seq,
                resume=event.resume.value,
                expected_session=expected_session.value if expected_session else None,
                found_session=prior_found.value if prior_found else None,
                error=str(exc),
            )
            raise
        if prior_found is None and emit:
            reason = (
                "matched_expected" if expected_session is not None else "first_seen"
            )
        elif prior_found is not None and not emit:
            reason = "duplicate"
        else:
            reason = "unknown"
        log_pipeline(
            logger,
            "runner.started.seen",
            pid=pid,
            jsonl_seq=jsonl_seq,
            resume=event.resume.value,
            expected_session=expected_session.value if expected_session else None,
            found_session=found_session.value if found_session else None,
            emit=emit,
            reason=reason,
        )
        return found_session, emit

    def _log_completed_event(
        self,
        *,
        logger: Any,
        pid: int,
        event: CompletedEvent,
        jsonl_seq: int | None = None,
        source: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "pid": pid,
            "ok": event.ok,
            "has_answer": bool(event.answer.strip()),
            "emit": True,
        }
        if jsonl_seq is not None:
            payload["jsonl_seq"] = jsonl_seq
        if source is not None:
            payload["source"] = source
        log_pipeline(logger, "runner.completed.seen", **payload)

    def _handle_jsonl_line(
        self,
        *,
        raw_line: bytes,
        stream: JsonlStreamState,
        state: Any,
        resume: ResumeToken | None,
        logger: Any,
        pid: int,
    ) -> list[UntetherEvent]:
        if stream.did_emit_completed:
            if not stream.ignored_after_completed:
                log_pipeline(
                    logger,
                    "runner.drop.jsonl_after_completed",
                    pid=pid,
                )
                stream.ignored_after_completed = True
            return []
        line = raw_line.strip()
        if not line:
            return []
        # Track raw I/O activity
        now = time.monotonic()
        stream.last_stdout_at = now
        stream.event_count += 1
        stream.jsonl_seq += 1
        seq = stream.jsonl_seq
        events = self._decode_jsonl_events(
            raw_line=raw_line,
            line=line,
            jsonl_seq=seq,
            state=state,
            resume=resume,
            found_session=stream.found_session,
            logger=logger,
            pid=pid,
        )
        # Peek at raw JSON for event timeline (engine-agnostic)
        try:
            raw_dict = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            raw_dict = None
        if isinstance(raw_dict, dict):
            etype = str(raw_dict.get("type", "unknown"))
            etool = None
            # Cover common engine conventions for tool name
            for key in ("tool_name", "tool", "name"):
                val = raw_dict.get(key)
                if isinstance(val, str) and val:
                    etool = val
                    break
            # Also check nested item.type for Codex-style events
            item = raw_dict.get("item")
            if etool is None and isinstance(item, dict):
                itype = item.get("type")
                if isinstance(itype, str) and itype:
                    etool = itype
            stream.last_event_type = etype
            stream.last_event_tool = etool
            label = f"tool:{etool}" if etool else etype
            stream.recent_events.append((now, label))
            # Stuck-after-tool_result tracking (#322). The latch persists across
            # intervening "other" events (attachments, system hooks) and is
            # cleared only by an assistant-turn-start event so the detector
            # sees a true "tool_result arrived, no follow-up" signal.
            kind = _classify_jsonl_event(raw_dict)
            stream.last_event_kind = kind
            if kind == _TOOL_RESULT_EVENT_KIND:
                stream.last_tool_result_at = now
            elif kind == _ASSISTANT_EVENT_KIND:
                stream.last_tool_result_at = 0.0
        output: list[UntetherEvent] = []
        for evt in events:
            if isinstance(evt, StartedEvent):
                # Inject subprocess PID into meta for diagnostics
                meta = dict(evt.meta) if evt.meta else {}
                meta["pid"] = pid
                evt = replace(evt, meta=meta)
                stream.found_session, emit = self._process_started_event(
                    evt,
                    expected_session=stream.expected_session,
                    found_session=stream.found_session,
                    logger=logger,
                    pid=pid,
                    jsonl_seq=seq,
                )
                if not emit:
                    continue
            if isinstance(evt, CompletedEvent):
                stream.did_emit_completed = True
                self._log_completed_event(
                    logger=logger,
                    pid=pid,
                    event=evt,
                    jsonl_seq=seq,
                )
                output.append(evt)
                break
            output.append(evt)
        return output

    async def _iter_jsonl_events(
        self,
        *,
        stdout: Any,
        stream: JsonlStreamState,
        state: Any,
        resume: ResumeToken | None,
        logger: Any,
        pid: int,
    ) -> AsyncIterator[UntetherEvent]:
        async for raw_line in self.iter_json_lines(stdout):
            for evt in self._handle_jsonl_line(
                raw_line=raw_line,
                stream=stream,
                state=state,
                resume=resume,
                logger=logger,
                pid=pid,
            ):
                yield evt

    _WATCHDOG_GRACE_SECONDS: float = 5.0

    _WATCHDOG_POLL_SECONDS: float = 0.5

    _LIVENESS_TIMEOUT_SECONDS: float = 600.0

    _stall_auto_kill: bool = False

    async def _subprocess_watchdog(
        self,
        proc: Any,
        stream: JsonlStreamState,
        reader_done: anyio.Event,
        logger: Any,
        pid: int,
    ) -> None:
        """Kill orphan children if stdout outlives the process.

        When a subprocess dies but child processes (e.g. MCP servers) inherit the
        stdout pipe FD, the JSONL reader blocks forever.  This watchdog polls for
        process death (``proc.wait()`` blocks until pipes drain, so we use
        ``os.kill(pid, 0)``), then after a grace period kills the process group
        to terminate orphan children and unblock the readers.

        Also detects liveness stalls: process alive but no stdout for
        ``_LIVENESS_TIMEOUT_SECONDS``.
        """
        import os as _os

        from .utils.proc_diag import collect_proc_diag, is_cpu_active

        liveness_warned = False
        prev_diag = None

        # Poll until the process is dead or the reader finishes.
        while not reader_done.is_set():
            try:
                _os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                break  # process exited

            # Liveness stall detection
            if (
                not liveness_warned
                and stream.last_stdout_at > 0
                and not stream.did_emit_completed
            ):
                idle = time.monotonic() - stream.last_stdout_at
                if idle >= self._LIVENESS_TIMEOUT_SECONDS:
                    liveness_warned = True
                    diag = collect_proc_diag(pid)
                    cpu_active = is_cpu_active(prev_diag, diag)
                    recent = list(stream.recent_events)[-5:]
                    logger.warning(
                        "subprocess.liveness_stall",
                        pid=pid,
                        idle_seconds=round(idle, 1),
                        event_count=stream.event_count,
                        last_event_type=stream.last_event_type,
                        tcp_established=diag.tcp_established if diag else None,
                        rss_kb=diag.rss_kb if diag else None,
                        cpu_active=cpu_active,
                        recent_events=[(round(t, 1), lbl) for t, lbl in recent],
                    )
                    # Auto-kill: config enabled + zero TCP + CPU NOT active
                    if (
                        self._stall_auto_kill
                        and diag is not None
                        and diag.tcp_established == 0
                        and diag.alive
                        and cpu_active is not True
                    ):
                        logger.warning(
                            "subprocess.liveness_kill",
                            pid=pid,
                            reason="zero_tcp_zero_cpu",
                        )
                        try:
                            _os.killpg(pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError, OSError) as e:
                            logger.debug(
                                "subprocess.watchdog.suppressed",
                                pid=pid,
                                error=str(e),
                                error_type=e.__class__.__name__,
                                context="liveness_kill",
                            )
                    prev_diag = diag

            await anyio.sleep(self._WATCHDOG_POLL_SECONDS)
        if stream.did_emit_completed or reader_done.is_set():
            return
        # Process is dead but reader hasn't finished — wait grace period.
        with anyio.move_on_after(self._WATCHDOG_GRACE_SECONDS):
            await reader_done.wait()
        if stream.did_emit_completed or reader_done.is_set():
            return
        # Reader still blocked — pipes likely held open by orphan children.
        logger.warning(
            "subprocess.died_without_completion",
            pid=pid,
        )
        # Kill the process group to terminate orphan children holding pipes open.
        # manage_subprocess uses start_new_session=True, so the process group
        # matches the subprocess PID.
        try:
            _os.killpg(pid, signal.SIGKILL)
            logger.warning("subprocess.killed_orphan_group", pid=pid)
        except (ProcessLookupError, PermissionError) as e:
            logger.debug(
                "subprocess.watchdog.suppressed",
                pid=pid,
                error=str(e),
                error_type=e.__class__.__name__,
                context="orphan_killpg",
            )
        except OSError:
            logger.debug("subprocess.killpg_failed", pid=pid, exc_info=True)

    async def run_impl(
        self, prompt: str, resume: ResumeToken | None
    ) -> AsyncIterator[UntetherEvent]:
        state = self.new_state(prompt, resume)
        self.start_run(prompt, resume, state=state)

        tag = self.tag()
        logger = self.get_logger()
        cmd = [self.command(), *self.build_args(prompt, resume, state=state)]
        payload = self.stdin_payload(prompt, resume, state=state)
        env = self.env(state=state)
        logger.info(
            "runner.start",
            engine=self.engine,
            resume=resume.value if resume else None,
            prompt=prompt[:100] + "…" if len(prompt) > 100 else prompt,
            prompt_len=len(prompt),
            args=cmd[1:],
        )

        cwd = get_run_base_dir()

        async with manage_subprocess(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
        ) as proc:
            if proc.stdout is None or proc.stderr is None:
                logger.error(
                    "subprocess.create.failed",
                    engine=self.engine,
                    reason="missing stdout/stderr pipes",
                    pid=proc.pid,
                )
                raise RuntimeError(self.pipes_error_message())
            if payload is not None and proc.stdin is None:
                logger.error(
                    "subprocess.create.failed",
                    engine=self.engine,
                    reason="missing stdin pipe for payload",
                    pid=proc.pid,
                )
                raise RuntimeError(self.pipes_error_message())

            self.last_pid = proc.pid
            logger.info(
                "subprocess.spawn",
                cmd=cmd[0] if cmd else None,
                args=cmd[1:],
                pid=proc.pid,
            )

            await self._send_payload(proc, payload, logger=logger, resume=resume)

            stream = JsonlStreamState(expected_session=resume)
            self.current_stream = stream
            reader_done = anyio.Event()

            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    drain_stderr,
                    proc.stderr,
                    logger,
                    tag,
                    stream.stderr_capture,
                )
                tg.start_soon(
                    self._subprocess_watchdog,
                    proc,
                    stream,
                    reader_done,
                    logger,
                    proc.pid,
                )
                async for evt in self._iter_jsonl_events(
                    stdout=proc.stdout,
                    stream=stream,
                    state=state,
                    resume=resume,
                    logger=logger,
                    pid=proc.pid,
                ):
                    yield evt
                reader_done.set()

            rc = await proc.wait()
            stream.proc_returncode = rc
            logger.info("subprocess.exit", pid=proc.pid, rc=rc)
            if stream.did_emit_completed:
                return
            found_session = stream.found_session
            if rc != 0:
                events = self.process_error_events(
                    rc,
                    resume=resume,
                    found_session=found_session,
                    state=state,
                    stderr_lines=stream.stderr_capture or None,
                )
                for evt in events:
                    if isinstance(evt, CompletedEvent):
                        self._log_completed_event(
                            logger=logger,
                            pid=proc.pid,
                            event=evt,
                            source="process_error",
                        )
                    yield evt
                return

            events = self.stream_end_events(
                resume=resume,
                found_session=found_session,
                state=state,
            )
            for evt in events:
                if isinstance(evt, CompletedEvent):
                    self._log_completed_event(
                        logger=logger,
                        pid=proc.pid,
                        event=evt,
                        source="stream_end",
                    )
                yield evt


class Runner(Protocol):
    engine: str

    def is_resume_line(self, line: str) -> bool: ...

    def format_resume(self, token: ResumeToken) -> str: ...

    def extract_resume(self, text: str | None) -> ResumeToken | None: ...

    def run(
        self,
        prompt: str,
        resume: ResumeToken | None,
    ) -> AsyncIterator[UntetherEvent]: ...
