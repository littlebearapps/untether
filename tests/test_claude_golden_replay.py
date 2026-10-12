"""Claude golden replay harness (#977, #960; REVIEW-2 M11).

The live-session seam (#977) and the approval-registry extraction (#960) are
both behaviour-preserving refactors of the Claude runner. This file is their
oracle: it replays recorded stream-json sessions through the REAL
``handle_message`` + ``ClaudeRunner`` + control channel + Telegram callback
dispatch and compares everything observable with committed snapshots.

How a replay runs
-----------------
- ``tests/fixtures/claude_golden/<name>.jsonl`` is the session: stream-json
  lines the CLI printed, with ``{"_do": ...}`` lines for what the user (or
  Untether) did in between — a button tap, a typed answer, a follow-up, a
  steer, ``/cancel``, a drain.
- The CLI is ``tests/fake_clis/fake_claude_puppet.py``: a real process with
  no behaviour of its own. The test tells it which line to print and it
  reports every line the runner writes to its stdin. Nothing sleeps for
  effect: each line is a handshake (printed → translated → bridge settled).
- ``tests/fixtures/claude_golden/<name>.json`` is the snapshot. Per step it
  holds the Untether events the runner emitted, the lines written back to the
  CLI (control responses, follow-ups, steers, re-arms), every Telegram call
  (send / edit / delete / callback answer, with text, entities and
  ``callback_data``), the structlog events (name, key set, and the values of
  a fixed list of stable keys) and the registry state whenever it changed.

Fixture lines
-------------
The first line is ``{"_do": "config", ...}``: ``permission_mode``, ``prompt``,
and optionally ``run_options``, ``watchdog``, ``progress``, ``auto_continue``,
``approval_originator_only``. Every other line is either a stream-json line
(``session_id`` is added when absent; ``"$stdin:<path>"`` echoes a value from
the last line the runner wrote, e.g. a follow-up's uuid) or a directive:
``tap`` (through ``_dispatch_callback``), ``typed_answer``, ``followup``
(``inject_live_followup``), ``steer`` (``maybe_steer``), ``cancel``
(``handle_cancel``), ``drain`` (``close_idle_live_sessions``),
``queued_note``, ``advance_time``, ``message`` (a second ``handle_message``),
``eof``, ``exit``. ``_stdin`` / ``_eof`` / ``_telegram`` on a line name what
the step must wait for before it is recorded. All content is synthetic.

Known gaps (not replayed): the Telegram loop's own typed-reply
branch (its calls are made here, in the same order), two taps racing one
request (in-flight claim, cancel-during-write), the follow-up settle timeout,
the pre-spawn guard's message text, the daily-budget and refused-re-arm
closes in ``inject_live_followup``, and ``orphan_approvals.synth_keyboard``.

Regenerating (a deliberate behaviour change only)
-------------------------------------------------
    UNTETHER_GOLDEN_UPDATE=1 uv run pytest tests/test_claude_golden_replay.py --no-cov

rewrites the snapshots; ``test_golden_snapshots_not_regenerated_in_refactor_commits``
then fails and prints the new ``GOLDEN_SHA256`` block to paste below. A
regenerated snapshot is therefore always two visible diffs (the ``.json`` and
the pin), never a silent rewrite. A refactor commit must not need either.
"""

from __future__ import annotations

import dataclasses
import difflib
import functools
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time as real_time
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest
from structlog.testing import capture_logs

from tests.telegram_fakes import FakeBot, FakeTransport, make_cfg
from untether import background_status as background_status_mod
from untether import runner as runner_mod
from untether import runner_bridge as bridge_mod
from untether.live_followup import inject_live_followup
from untether.model import ResumeToken
from untether.runner_bridge import (
    ExecBridgeConfig,
    IncomingMessage,
    RunningTask,
    close_idle_live_sessions,
    handle_message,
)
from untether.runners import claude as claude_mod
from untether.runners.claude import ClaudeRunner
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.scheduler import ThreadJob
from untether.settings import (
    AutoContinueSettings,
    ProgressSettings,
    WatchdogSettings,
)
from untether.telegram import steer as steer_mod
from untether.telegram.bridge import TelegramPresenter
from untether.telegram.commands import cancel as cancel_mod
from untether.telegram.commands import claude_control as claude_control_mod
from untether.telegram.commands import export as export_mod
from untether.telegram.commands.cancel import handle_cancel
from untether.telegram.commands.dispatch import (
    _dispatch_callback,
    _parse_callback_data,
)
from untether.telegram.loop import _queued_wait_note
from untether.telegram.steer import maybe_steer
from untether.telegram.types import TelegramCallbackQuery, TelegramIncomingMessage
from untether.transport import MessageRef, RenderedMessage
from untether.utils.paths import (
    reset_run_channel_id,
    reset_run_sender_id,
    set_run_channel_id,
    set_run_sender_id,
)

pytestmark = pytest.mark.anyio

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "claude_golden"
PUPPET = Path(__file__).parent / "fake_clis" / "fake_claude_puppet.py"
REPO_ROOT = Path(__file__).resolve().parent.parent
UPDATE_ENV = "UNTETHER_GOLDEN_UPDATE"
SOCK_ENV = "CLAUDE_GOLDEN_PUPPET_SOCK"

CHAT_ID = 123
USER_MSG_ID = 10
SENDER_ID = 7001
STEP_TIMEOUT_S = float(os.environ.get("UNTETHER_GOLDEN_STEP_TIMEOUT", "20"))
POLL_S = 0.02  # the runner's live-session poll interval in a replay
QUIET_S = 0.05
REPLAY_TIMEOUT_S = 120.0  # one whole replay
# How long the runner waits for the CLI to exit after closing its stdin
# before it signals it. In a replay the CLI exits when the script says so
# (``exit``), so this wall-clock timer must never be what ends a session:
# it outlasts the whole replay. A grace that did expire would still be
# recorded (``claude.live_session.close_grace_expired``) and fail the diff.
CLOSE_GRACE_S = 2 * REPLAY_TIMEOUT_S

# sha256 of every file in tests/fixtures/claude_golden/. Pinned so a snapshot
# cannot be regenerated without a second, reviewable diff in this file.
GOLDEN_SHA256: dict[str, str] = {
    "approval_pending_across_result.json": "9af1f7a5a05e478023424fe760c22de072e5f09feadb0a3f7791f4a918102445",
    "approval_pending_across_result.jsonl": "cb679ff7082bf4677a39a9ded3fb4c752fbbdf18f4c1acadc87f2a23cdfcf201",
    "ask_question_flows.json": "28246b0ea73dbd3ddf30e1785f8aefed8a27766a80a3a66bfcaeae41c7fc0a90",
    "ask_question_flows.jsonl": "6d6e093854ecf8b0855f31564a73b35b2a4006716ff5a9356788f219da3e0738",
    "auto_handled_requests.json": "d641cc5bde75a6bf4916b0ddff2ffeb7d931a65344d8eb8a02c4102dd673686e",
    "auto_handled_requests.jsonl": "33d316117774e476785d8b039c68b912cdb0467ab49ad5bdde7f600904ac6894",
    "background_task_cancel.json": "83243a017346e5007628fee259344414daeae6531996887a06294e3583123cd0",
    "background_task_cancel.jsonl": "26ed30a0304b75169f34cdf94d9a745060a957538ba30b8e95846084c3ce88b8",
    "background_task_context_changed.json": "bda2cdcb1cfa10670ce0f6287c15fe138cbeb0f9f8104f9836dcc1a22d03e7a3",
    "background_task_context_changed.jsonl": "16554d2ff8979659f3fe4c2f3043af8dda4f8d4b27537f3fd65c60e15fd0289c",
    "background_task_drain_notice.json": "b63f1e2bf09623d5f64ec0d112ebf2dd0aa631163ed9d8fba68e843ce505b970",
    "background_task_drain_notice.jsonl": "cef1c04c6f27e7206596703628ec00ac0e6a27b1713c5bc52f25b49b1fa5a97d",
    "background_task_options_changed.json": "bf4f687c44414e6932a63492f7b4b351c6b1fecf11b254408bb19af3361bdb84",
    "background_task_options_changed.jsonl": "dda79cb9df8721ec285ecb43ad5ce683a300d6dec5f8d5359a8f9e14cdfcd4a3",
    "cancel_with_pending_approval.json": "f114a62cf583c3a5097f92eb9b1b8636ee3c669eb53998e4259c80c000b12550",
    "cancel_with_pending_approval.jsonl": "f72dfa5b9cc8af2e0d4ff9ef6a41d22d856a2946c2c7a9092f5811438eb6a674",
    "idle_agent_approval.json": "bdb8caa47fa1479afc29b54798187c2c11f29ea1cbd4ff80b0b1117c2fc817d5",
    "idle_agent_approval.jsonl": "06a22dd133572a3534b268d15cd75f444e2e37f3eaa45df3de8fc60f27a4efb8",
    "idle_close_no_tasks.json": "5988aa36a7fcd86b475b127f564e064a430cf913921af9f580bb596ecbd32bf1",
    "idle_close_no_tasks.jsonl": "518075134a3dac8b7750956f3675d23e6610e2a41004812eae1e1ba759ffe6d6",
    "live_followup_steer_resume.json": "a4b1e6ac5f4c9535c3f1da2bbf0036f2a53f06fb122d37b0e317fce9f9c153ea",
    "live_followup_steer_resume.jsonl": "71c65a6286c3f34e5bf5118bf503395f666782bdaec799806a7f4c25d6dd1975",
    "originator_only.json": "55d7b19cb3abbcbbebaff71ecd341dba59adad40e35c21f6cf93b3612aba525a",
    "originator_only.jsonl": "3fbc1f3471305aa008e512c7fa3e46615f671acbef0f450ce2fdd97ab2d7c7d8",
    "plan_approve_followup_cancel.json": "964c0a63c895ee3dbb6650b2075a18e8aeb2a2fa26535510fe6f03920aaf277e",
    "plan_approve_followup_cancel.jsonl": "28d44a364010b367ebaad282c016c087d1fbc1468aefe7f3fd83d79b61d50610",
    "plan_outline_abandoned.json": "c07a16eb83c784b88a3c12075dc1729acb67940ed1669237569996de3842a3fb",
    "plan_outline_abandoned.jsonl": "de15f0161a7628efc3517c608f84f64f2f62b4cce9f9448990fc14327118e424",
    "plan_outline_guard.json": "2cf2042a4668b6c6a94fa4e772a4c90252ace6ecafd57257f3a28305d3be9da7",
    "plan_outline_guard.jsonl": "0d7f070a1acb8ca69125f79dcc2692a25f5dce31d2f905351936a63a7aae2bf8",
    "plan_outline_hold.json": "e1bd37a9b97905d1a6eac45b693b78da223b22bbe5e26d5df7524d5cdcb8f545",
    "plan_outline_hold.jsonl": "4920222856240ed72e4bff0a7696f99d145edb6138be3d78bd1dddc2e8cc8cee",
    "plan_rearm_refused.json": "cc86ea4a3d06e7910de80e5b8e019c791f1414d8c19f6fcc148c22ff4f5f7b76",
    "plan_rearm_refused.jsonl": "070ed57af149ff77c66a13b3c4925af6a142c99b966ddce1dc1f8d2ba52f8a92",
    "request_timeout_sweep.json": "99fb9741607bdd7c85d0acdabeba2e2b87e4ea78c77f3aea35e15ad4175b9b7c",
    "request_timeout_sweep.jsonl": "d8f2aadb4a0fd74677aeb66868acb097f3f5272074f2511cafca43fe8c0dc80e",
    "resume_waits_for_background_owner.json": "1c035f2d158fec8549214891792b101e40f69aa6a910e5ae904130bb461ac333",
    "resume_waits_for_background_owner.jsonl": "2a52fd6cb208882b289c27d8a73411cf02b2874ed7daa6a0429a54339c4a9acf",
    "tool_approve_deny.json": "b60ceb48a869cbcf72cd7c073b1bfcacb8e722b84907871eead9e858a374bee5",
    "tool_approve_deny.jsonl": "88e09163371958159ebae06eee84d937790786147989be76ec9a4756c12646ed",
    "unattended_denies.json": "ce9bd995bc7ce645ddc3cf16dd55e6bda1cf9cc29062b6df358d361ae9843ed1",
    "unattended_denies.jsonl": "3e9b8e31c3208dd0574fb3e9b140d9962f77212fb03c56cc7e8ae2782476ddac",
}


def _updating() -> bool:
    return os.environ.get(UPDATE_ENV) == "1"


def _scenario_names() -> list[str]:
    return sorted(p.stem for p in GOLDEN_DIR.glob("*.jsonl"))


# ── normalisation ───────────────────────────────────────────────────────────

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_INIT_ID_RE = re.compile(r"\binit_\d+\b")
_ELAPSED_RE = re.compile(r"· \d+(?:h \d+m|m \d+s|[smh])\b")


class _Normaliser:
    """Replace what legitimately differs between two runs of one replay:
    random uuids (numbered in order of first appearance), ``id()``-derived
    request ids, and host paths."""

    def __init__(self, tmp_dirs: list[Path]) -> None:
        self._uuids: dict[str, str] = {}
        self._paths: list[tuple[str, str]] = [
            (str(p), "<tmp>") for p in sorted(tmp_dirs, key=lambda p: -len(str(p)))
        ]
        self._paths.append((str(REPO_ROOT), "<repo>"))
        self._paths.append((str(Path.home()), "<home>"))
        self._paths.append((sys.executable, "<python>"))

    def text(self, value: str) -> str:
        for path, label in self._paths:
            if path and path in value:
                value = value.replace(path, label)

        def _uuid(match: re.Match[str]) -> str:
            key = match.group(0).lower()
            if key not in self._uuids:
                self._uuids[key] = f"<uuid-{len(self._uuids) + 1}>"
            return self._uuids[key]

        value = _UUID_RE.sub(_uuid, value)
        return _INIT_ID_RE.sub("init_<id>", value)

    def value(self, obj: Any) -> Any:
        """A JSON-safe, normalised copy of ``obj``."""
        if isinstance(obj, str):
            return self.text(obj)
        if obj is None or isinstance(obj, (bool, int)):
            return obj
        if isinstance(obj, float):
            return round(obj, 6)
        if isinstance(obj, Enum):
            return self.value(obj.value)
        if isinstance(obj, Path):
            return self.text(str(obj))
        if isinstance(obj, MessageRef):
            return f"msg:{obj.message_id}"
        if isinstance(obj, dict):
            return {
                self.text(str(k)): "<pid>" if k == "pid" else self.value(v)
                for k, v in obj.items()
            }
        if isinstance(obj, (list, tuple)):
            return [self.value(v) for v in obj]
        if isinstance(obj, (set, frozenset)):
            return sorted((self.value(v) for v in obj), key=repr)
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return {
                f.name: self.value(getattr(obj, f.name))
                for f in dataclasses.fields(obj)
            }
        return f"<{type(obj).__name__}>"


# ── observation: Telegram, logs, registries ─────────────────────────────────


def _keyboard(extra: dict[str, Any] | None) -> list[list[str]] | None:
    markup = (extra or {}).get("reply_markup")
    if not isinstance(markup, dict):
        return None
    return [
        [f"{b.get('text')} => {b.get('callback_data')}" for b in row]
        for row in markup.get("inline_keyboard", [])
    ]


def _entities(extra: dict[str, Any] | None) -> list[str] | None:
    entities = (extra or {}).get("entities")
    if not entities:
        return None
    out = []
    for ent in entities:
        get = ent.get if isinstance(ent, dict) else lambda k, e=ent: getattr(e, k, None)
        out.append(f"{get('type')}@{get('offset')}+{get('length')}")
    return out


class _RecordingTransport(FakeTransport):
    """FakeTransport plus one ordered log of every call."""

    def __init__(self, norm: _Normaliser) -> None:
        super().__init__()
        self._norm = norm
        self.log: list[dict[str, Any]] = []
        # message id -> the keyboard it currently shows (for taps).
        self.keyboards: dict[int, list[list[str]] | None] = {}

    def _message(self, message: RenderedMessage) -> dict[str, Any]:
        extra = message.extra or {}
        # Elapsed times are wall-clock (a background row's ``· 1s``): the
        # replay pins what was said, not how long the host took to say it.
        text = _ELAPSED_RE.sub("· <t>", self._norm.text(message.text))
        out: dict[str, Any] = {"text": text}
        if (kb := _keyboard(extra)) is not None:
            out["keyboard"] = kb
        if (ents := _entities(extra)) is not None:
            out["entities"] = ents
        other = sorted(k for k in extra if k not in ("reply_markup", "entities"))
        if other:
            out["extra"] = {k: self._norm.value(extra[k]) for k in other}
        return out

    async def send(self, *, channel_id, message, options=None):  # type: ignore[override]
        ref = await super().send(
            channel_id=channel_id, message=message, options=options
        )
        entry: dict[str, Any] = {
            "op": "send",
            "id": ref.message_id,
            "chat": channel_id,
            **self._message(message),
        }
        if options is not None:
            entry["notify"] = options.notify
            if options.reply_to is not None:
                entry["reply_to"] = options.reply_to.message_id
            if options.replace is not None:
                entry["replace"] = options.replace.message_id
            if options.thread_id is not None:
                entry["thread"] = options.thread_id
        self.keyboards[ref.message_id] = entry.get("keyboard")
        self.log.append(entry)
        return ref

    async def edit(self, *, ref, message, wait=True):  # type: ignore[override]
        entry = {"op": "edit", "id": ref.message_id, **self._message(message)}
        self.keyboards[ref.message_id] = entry.get("keyboard")
        # A heartbeat re-render repaints identical content: one call, not two.
        last = next(
            (e for e in reversed(self.log) if e.get("id") == ref.message_id), None
        )
        content = ("text", "keyboard", "entities", "extra")
        if last is None or any(last.get(k) != entry.get(k) for k in content):
            self.log.append(entry)
        return await super().edit(ref=ref, message=message, wait=wait)

    async def delete(self, *, ref):  # type: ignore[override]
        self.keyboards.pop(ref.message_id, None)
        self.log.append({"op": "delete", "id": ref.message_id})
        return await super().delete(ref=ref)

    def message_with_button(self, data: str) -> int | None:
        for message_id in sorted(self.keyboards, reverse=True):
            rows = self.keyboards[message_id] or []
            if any(cell.endswith(f"=> {data}") for row in rows for cell in row):
                return message_id
        return None


# Log values worth pinning: decisions and identities, never timings or pids.
_LOG_VALUE_KEYS = frozenset(
    {
        "action",
        "agent_id",
        "approved",
        "behavior",
        "callback_id",
        "cancelled",
        "channel",
        "cleaned",
        "close_reason",
        "closed",
        "closing",
        "command",
        "decision",
        "decision_reason_type",
        "early",
        "engine",
        "explicit",
        "first_action",
        "from",
        "had_action",
        "has_toast",
        "kind",
        "live",
        "mid_turn",
        "mode",
        "notice",
        "ok",
        "outcome",
        "permission_mode",
        "phase",
        "prior",
        "quarantined",
        "rc",
        "reason",
        "request_id",
        "request_type",
        "session_id",
        "source",
        "status",
        "subtype",
        "to",
        "tool_name",
        "turn",
        "turn_idle",
        "unattended",
    }
)

# Events whose presence or count depends on wall time, the host or the
# process table rather than on the replayed session. Everything else is kept.
_LOG_NOISE_PREFIXES: tuple[str, ...] = ()
_LOG_NOISE: frozenset[str] = frozenset(
    {
        # Cached per CLI binary: logged by the first spawn in a process only.
        "claude.hook_events.probe",
    }
)


def _log_line(entry: dict[str, Any], norm: _Normaliser) -> str | None:
    event = str(entry.get("event"))
    if event in _LOG_NOISE or event.startswith(_LOG_NOISE_PREFIXES):
        return None
    pinned = []
    others = []
    for key in sorted(entry):
        if key in ("event", "log_level"):
            continue
        value = entry[key]
        if key in _LOG_VALUE_KEYS and (
            value is None or isinstance(value, (str, bool, int, list, tuple, dict))
        ):
            pinned.append(f"{key}={json.dumps(norm.value(value), sort_keys=True)}")
        else:
            others.append(key)
    line = f"{entry.get('log_level')} {event}"
    if pinned:
        line += " " + " ".join(pinned)
    if others:
        line += " +[" + ",".join(others) + "]"
    return line


_TIME_FIELDS = frozenset({"at", "age_s"})


def _registry_snapshot(norm: _Normaliser) -> dict[str, Any]:
    """Every registry the two refactors move or read, timestamps removed."""
    c = claude_mod

    def strip(obj: Any) -> Any:
        value = norm.value(obj)
        if isinstance(value, dict):
            return {k: v for k, v in value.items() if k not in _TIME_FIELDS}
        return value

    snap: dict[str, Any] = {
        "request_to_session": norm.value(dict(c._REQUEST_TO_SESSION)),
        "request_to_input": norm.value(dict(c._REQUEST_TO_INPUT)),
        "request_to_tool_name": norm.value(dict(c._REQUEST_TO_TOOL_NAME)),
        "request_to_channel": norm.value(dict(c._REQUEST_TO_CHANNEL)),
        "request_to_originator": norm.value(dict(c._REQUEST_TO_ORIGINATOR)),
        # Insertion order is the LRU order (#197): keep it as a list.
        "handled": [[norm.text(k), strip(v)] for k, v in c._HANDLED_REQUESTS.items()],
        "inflight": {
            norm.text(k): {"action": v.action, "channel_id": v.channel_id}
            for k, v in c._INFLIGHT_CONTROL_RESPONSES.items()
        },
        "cancelled_during_write": norm.value(set(c._CANCELLED_DURING_WRITE)),
        "pending_ask": norm.value(dict(c._PENDING_ASK_REQUESTS)),
        "ask_flows": {
            norm.text(k): {
                "channel_id": v.channel_id,
                "questions": len(v.questions),
                "current_index": v.current_index,
                "answers": norm.value(v.answers),
                "awaiting_text": v.awaiting_text,
            }
            for k, v in c._ASK_QUESTION_FLOWS.items()
        },
        "answered_ask_flows": {
            norm.text(k): [v[0], v[2]] for k, v in c._ANSWERED_ASK_FLOWS.items()
        },
        "discuss_approved": norm.value(set(c._DISCUSS_APPROVED)),
        "discuss_carry": norm.value(set(c._DISCUSS_CARRY)),
        "plan_exit_approved": norm.value(set(c._PLAN_EXIT_APPROVED)),
        "outline_pending": norm.value(set(c._OUTLINE_PENDING)),
        "discuss_feedback_refs": norm.value(
            dict(claude_control_mod._DISCUSS_FEEDBACK_REFS)
        ),
        "active_runners": sorted(norm.text(k) for k in c._ACTIVE_RUNNERS),
        "session_stdin": sorted(norm.text(k) for k in c._SESSION_STDIN),
        "session_bg_state": sorted(norm.text(k) for k in c._SESSION_BG_STATE),
        "live_sessions": {},
        "states": {},
    }
    states: dict[str, Any] = dict(c._SESSION_BG_STATE)
    for sid, live in c._LIVE_SESSIONS.items():
        states.setdefault(sid, live.state)
        snap["live_sessions"][norm.text(sid)] = {
            "closing": live.closing,
            "close_reason": live.close_reason,
            "idle": live.idle,
            "accepting": c.is_session_accepting(sid),
            "accepting_steer": live.accepting_steer,
            "listeners": len(live.listeners),
        }
    for sid, state in states.items():
        snap["states"][norm.text(sid)] = {
            "live_mode": state.live_mode,
            "turn": state.turn,
            "turn_open": state.turn_open,
            "completed_turns": state.completed_turns,
            "live_close_reason": state.live_close_reason,
            "effective_permission_mode": state.effective_permission_mode,
            "prompting_mode": state.prompting_mode,
            "plan_rearm_pending": state.plan_rearm_pending,
            "plan_rearm_failed": state.plan_rearm_failed,
            "awaiting_injected": state.awaiting_injected,
            "pending_control_requests": sorted(
                norm.text(k) for k in state.pending_control_requests
            ),
            "request_to_action": norm.value(dict(state.request_to_action)),
            "control_action_for_tool": norm.value(dict(state.control_action_for_tool)),
            "control_registered": sorted(
                norm.text(k) for k in state.control_registered_at
            ),
            "auto_approve_queue": norm.value(list(state.auto_approve_queue)),
            "auto_deny_queue": norm.value(list(state.auto_deny_queue)),
            "exitplanmode_plans": norm.value(dict(state.exitplanmode_plans)),
            "awaiting_user_approval": state.awaiting_user_approval(),
            "control_request_snapshot": [
                strip(s) for s in state.control_request_snapshot()
            ],
        }
    snap["idle_live_session_count"] = runner_mod._idle_claude_live_session_count()
    return snap


_EMPTY: Any = object()
REMOVED = "<removed>"


def _diff(before: Any, after: Any) -> Any:
    """What changed from ``before`` to ``after``: for dicts only the keys
    that differ (recursively; a dropped key reads ``<removed>``), otherwise
    the new value. ``{}`` means no change."""
    if not (isinstance(before, dict) and isinstance(after, dict)):
        return after
    out: dict[str, Any] = {}
    for key, value in after.items():
        old = before.get(key, _EMPTY)
        if old is _EMPTY:
            out[key] = value
        elif old != value:
            out[key] = _diff(old, value)
    for key in before:
        if key not in after:
            out[key] = REMOVED
    return out


def _clear_registries() -> None:
    c = claude_mod
    for registry in (
        c._REQUEST_TO_SESSION,
        c._REQUEST_TO_INPUT,
        c._REQUEST_TO_TOOL_NAME,
        c._REQUEST_TO_CHANNEL,
        c._REQUEST_TO_ORIGINATOR,
        c._HANDLED_REQUESTS,
        c._INFLIGHT_CONTROL_RESPONSES,
        c._CANCELLED_DURING_WRITE,
        c._PENDING_ASK_REQUESTS,
        c._ASK_QUESTION_FLOWS,
        c._ANSWERED_ASK_FLOWS,
        c._DISCUSS_APPROVED,
        c._DISCUSS_CARRY,
        c._PLAN_EXIT_APPROVED,
        c._OUTLINE_PENDING,
        c._ACTIVE_RUNNERS,
        c._SESSION_STDIN,
        c._SESSION_BG_STATE,
        c._LIVE_SESSIONS,
        claude_control_mod._DISCUSS_FEEDBACK_REFS,
    ):
        registry.clear()
    # Process-wide memos and once-per-process log latches. Left alone, what a
    # replay records would depend on which tests ran before it (an ephemeral
    # message another test registered for the same chat, a "logged once"
    # line that an earlier test already used up).
    for module, names in _PROCESS_STATE:
        for name in names:
            getattr(module, name).clear()


_PROCESS_STATE: tuple[tuple[Any, tuple[str, ...]], ...] = (
    (
        bridge_mod,
        (
            "_EPHEMERAL_MSGS",
            "_EPHEMERAL_MSGS_TS",
            "_ASK_ACTION_MODEL",
            "_ASK_ACTION_MODEL_TS",
            "_OUTLINE_REGISTRY",
            "_OUTLINE_REGISTRY_TS",
            "_SAFEGUARD_HINTED",
            "_UNATTENDED_HINTED",
            "_FOLLOWUP_ANCHORS",
            "_FOLLOWUP_IN_FLIGHT",
        ),
    ),
    (
        claude_mod,
        (
            "_UNKNOWN_RATE_LIMIT_STATUSES_LOGGED",
            "_RATE_LIMIT_RESET_LATCH",
            "_RATE_LIMIT_ACTION_LATCH",
            "_CONTEXT_WINDOWS",
            "_CONTEXT_WINDOW_MISSES",
            "_CONTEXT_OVER_WINDOW_WARNED",
            "_PROMPTING_MODE_ALLOWLIST_LOGGED",
            "_CATALOG_STALENESS_WARNED",
        ),
    ),
    (steer_mod, ("_NOTICED",)),
    (cancel_mod, ("_RECENT_CANCELS",)),
    (export_mod, ("_SESSION_HISTORY",)),
    (background_status_mod, ("_COUNT_SOURCES",)),
)


# ── the puppet side ─────────────────────────────────────────────────────────


class _Puppet:
    """One connected puppet process: what it was started with and what the
    runner has written to it."""

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.argv: list[str] | None = None
        self.stdin: list[str] = []
        self.taken = 0
        self.eof = False
        self.gone = False

    async def pump(self) -> None:
        buf = b""
        try:
            async for chunk in self.stream:
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    msg = json.loads(raw)
                    if "hello" in msg:
                        self.argv = msg["hello"]["argv"]
                    elif "stdin" in msg:
                        self.stdin.append(msg["stdin"])
                    elif msg.get("eof"):
                        self.eof = True
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            pass
        finally:
            self.gone = True

    async def send(self, obj: dict[str, Any]) -> None:
        await self.stream.send((json.dumps(obj) + "\n").encode())


class _Lines:
    """Counts the stream lines the runner has translated, with their events."""

    def __init__(self) -> None:
        self.seen = 0
        self.events: list[Any] = []


class _GoldenRunner(ClaudeRunner):
    """The real runner; only the spawn environment and one observation tap
    differ. ``golden_sock`` / ``golden_lines`` live in the instance dict (the
    subclass is not a slots dataclass)."""

    def env(self, *, state: Any) -> dict[str, str] | None:
        base = super().env(state=state) or {}
        base[SOCK_ENV] = self.golden_sock  # type: ignore[attr-defined]
        return base

    def _handle_jsonl_line(self, **kwargs: Any) -> list[Any]:
        events = list(super()._handle_jsonl_line(**kwargs))
        lines: _Lines = self.golden_lines  # type: ignore[attr-defined]
        lines.events.extend(events)
        lines.seen += 1
        return events


class _TimeShim:
    """``time`` for ``runners.claude`` with a movable wall clock, so the
    5-minute control-request sweep can be replayed without waiting."""

    def __init__(self) -> None:
        self.offset = 0.0

    def time(self) -> float:
        return real_time.time() + self.offset

    def __getattr__(self, name: str) -> Any:
        return getattr(real_time, name)


# ── the replay ──────────────────────────────────────────────────────────────


def _label(line: dict[str, Any]) -> str:
    if "_do" in line:
        parts = [f"do:{line['_do']}"]
        parts.extend(
            f"{key}={line[key]}"
            for key in ("data", "text", "reason", "s", "rc")
            if key in line
        )
        return " ".join(parts)
    parts = [str(line.get("type"))]
    if line.get("subtype"):
        parts.append(str(line["subtype"]))
    request = line.get("request")
    if isinstance(request, dict):
        parts.append(str(request.get("subtype")))
        if request.get("tool_name"):
            parts.append(str(request["tool_name"]))
    message = line.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), list):
        for block in message["content"]:
            if isinstance(block, dict):
                kind = block.get("type")
                name = block.get("name") or block.get("tool_use_id") or ""
                parts.append(f"{kind}:{name}" if name else str(kind))
    if "request_id" in line:
        parts.append(str(line["request_id"]))
    return "emit:" + "/".join(parts)


class _Replay:
    def __init__(
        self, name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.name = name
        self.monkeypatch = monkeypatch
        self.tmp_path = tmp_path
        raw = (GOLDEN_DIR / f"{name}.jsonl").read_text(encoding="utf-8")
        script = [json.loads(line) for line in raw.splitlines() if line.strip()]
        assert script and script[0].get("_do") == "config", (
            f"{name}.jsonl must start with a config line"
        )
        self.config: dict[str, Any] = script[0]
        self.script = script[1:]
        self.sock_dir = Path(tempfile.mkdtemp(prefix="cg"))
        self.norm = _Normaliser([tmp_path, self.sock_dir])
        self.transport = _RecordingTransport(self.norm)
        self.lines = _Lines()
        self.puppets: list[_Puppet] = []
        self.current: _Puppet | None = None
        self.running_tasks: dict[MessageRef, RunningTask] = {}
        self.runs_done: list[anyio.Event] = []
        self.steps: list[dict[str, Any]] = []
        self.logs: list[dict[str, Any]] = []
        self.time = _TimeShim()
        self._tap_seq = 0
        self._marks = {"telegram": 0, "logs": 0, "events": 0, "bot": 0}
        self._last_registry: dict[str, Any] = {}
        self._threads = 0
        self.session = self.config.get("session", "golden-session-1")
        self.run_options = (
            EngineRunOptions(**self.config["run_options"])
            if "run_options" in self.config
            else None
        )

    # -- setup ---------------------------------------------------------------

    def _setup(self) -> None:
        mp = self.monkeypatch
        progress = ProgressSettings(
            **{"min_render_interval": 0.0, **self.config.get("progress", {})}
        )
        mp.setattr(bridge_mod, "_load_progress_settings", lambda: progress)
        watchdog = WatchdogSettings.model_construct(
            **{
                **WatchdogSettings().model_dump(),
                **self.config.get("watchdog", {}),
            }
        )
        mp.setattr(
            claude_mod,
            "load_settings_if_exists",
            lambda *a, **k: (SimpleNamespace(watchdog=watchdog), Path("x")),
        )
        mp.setattr(claude_mod, "time", self.time)
        # The background-status message finalises on its own 1 s poll; at
        # that pace the edit would land in whichever step happened to be
        # open. Same code, replay-sized interval.
        mp.setattr(
            bridge_mod,
            "BackgroundStatusManager",
            functools.partial(bridge_mod.BackgroundStatusManager, poll_s=POLL_S),
        )
        if "auto_continue" in self.config:
            auto_continue = AutoContinueSettings(**self.config["auto_continue"])
            mp.setattr(
                bridge_mod, "_load_auto_continue_settings", lambda: auto_continue
            )
        real_run_sync = anyio.to_thread.run_sync

        async def tracked_run_sync(*args: Any, **kwargs: Any) -> Any:
            # A worker thread is invisible to wait_all_tasks_blocked: count
            # them so a step is not closed while one is still out.
            self._threads += 1
            try:
                return await real_run_sync(*args, **kwargs)
            finally:
                self._threads -= 1

        mp.setattr(anyio.to_thread, "run_sync", tracked_run_sync)
        self.exec_cfg = ExecBridgeConfig(
            transport=self.transport,
            presenter=TelegramPresenter(),
            final_notify=True,
        )
        self.tg_cfg = make_cfg(self.transport)
        self.tg_cfg.exec_cfg = self.exec_cfg
        self.tg_cfg.approval_originator_only = bool(
            self.config.get("approval_originator_only", False)
        )
        self.bot: FakeBot = self.tg_cfg.bot  # type: ignore[assignment]
        runner = _GoldenRunner(
            claude_cmd=str(PUPPET),
            permission_mode=self.config.get("permission_mode", "plan"),
        )
        runner.golden_sock = str(self.sock_dir / "s")  # type: ignore[attr-defined]
        runner.golden_lines = self.lines  # type: ignore[attr-defined]
        # Slots-dataclass timing knobs: set on the instance.
        runner._live_poll_s = POLL_S
        # Not a replay-sized grace: the steps between a close and the
        # scripted exit settle in wall-clock time, and on a slow host (CI,
        # Python 3.14 under coverage) a 2 s grace expired between them, so
        # the runner SIGINTed the puppet and the next emit hit a broken pipe.
        runner._live_close_grace_s = CLOSE_GRACE_S
        self.runner = runner

    # -- waiting -------------------------------------------------------------

    async def _until(self, what: str, predicate: Any) -> None:
        try:
            with anyio.fail_after(STEP_TIMEOUT_S):
                while not predicate():
                    await anyio.sleep(0.002)
        except TimeoutError:
            raise AssertionError(
                f"{self.name}: timed out waiting for {what} "
                f"(step {len(self.steps) + 1})"
            ) from None

    def _activity(self) -> tuple[Any, ...]:
        return (
            len(self.logs),
            len(self.transport.log),
            len(self.bot.callback_calls),
            self.lines.seen,
            sum(len(p.stdin) for p in self.puppets),
            json.dumps(_registry_snapshot(self.norm), sort_keys=True),
        )

    async def _settle(self) -> None:
        """Close a step only once its consequences are in: every task
        blocked, no worker thread out, and nothing observable has moved for
        two quiet periods (each longer than the poll intervals the replay
        sets, so a poll-driven reaction lands in the step that caused it).
        A period in which this task itself woke late does not count: on a
        starved host the polls are late too, so the step waits longer rather
        than being recorded short."""
        quiet = 0
        last: tuple[Any, ...] | None = None
        with anyio.fail_after(STEP_TIMEOUT_S):
            while quiet < 2:
                await anyio.wait_all_tasks_blocked()
                if self._threads:
                    quiet = 0
                    await anyio.sleep(0.002)
                    continue
                before = self._activity()
                slept_at = real_time.monotonic()
                await anyio.sleep(QUIET_S)
                on_time = real_time.monotonic() - slept_at < QUIET_S * 1.5
                now = self._activity()
                unchanged = now == before and (last is None or now == last)
                quiet = quiet + 1 if unchanged and on_time else 0
                last = now

    # -- run contexts --------------------------------------------------------

    async def _message(
        self, text: str, msg_id: int, resume: ResumeToken | None, done: anyio.Event
    ) -> None:
        channel_token = set_run_channel_id(CHAT_ID)
        sender_token = set_run_sender_id(self.config.get("sender_id", SENDER_ID))
        try:
            with apply_run_options(self.run_options):
                await handle_message(
                    self.exec_cfg,
                    runner=self.runner,
                    incoming=IncomingMessage(
                        channel_id=CHAT_ID, message_id=msg_id, text=text
                    ),
                    resume_token=resume,
                    running_tasks=self.running_tasks,
                    # A frozen clock: elapsed-time text and stall timers
                    # never depend on how fast the host is.
                    clock=lambda: 1000.0,
                )
        finally:
            reset_run_sender_id(sender_token)
            reset_run_channel_id(channel_token)
            done.set()

    def _start_message(
        self, tg: Any, text: str, msg_id: int, resume: ResumeToken | None
    ) -> None:
        done = anyio.Event()
        self.runs_done.append(done)
        tg.start_soon(self._message, text, msg_id, resume, done)

    async def _accept(self) -> None:
        count = len(self.puppets)
        await self._until(
            "the CLI to start",
            lambda: len(self.puppets) > count and self.puppets[-1].argv is not None,
        )
        self.current = self.puppets[-1]

    # -- directives ----------------------------------------------------------

    async def _do(self, tg: Any, line: dict[str, Any]) -> Any:
        kind = line["_do"]
        token = ResumeToken(engine="claude", value=line.get("session", self.session))
        if kind == "tap":
            data = line["data"]
            message_id = line.get("message_id")
            if message_id is None:
                message_id = self.transport.message_with_button(data)
            assert message_id is not None, f"no message shows the button {data!r}"
            self._tap_seq += 1
            query_id = f"cb-{self._tap_seq}"
            command_id, args_text = _parse_callback_data(data)
            await _dispatch_callback(
                self.tg_cfg,
                TelegramCallbackQuery(
                    transport="telegram",
                    chat_id=line.get("chat", CHAT_ID),
                    message_id=message_id,
                    callback_query_id=query_id,
                    data=data,
                    sender_id=line.get("sender", SENDER_ID),
                ),
                command_id,
                args_text,
                None,
                self.running_tasks,
                AsyncMock(),
                None,
                False,
                None,
                query_id,
            )
            return None
        if kind == "typed_answer":
            # The Telegram loop's typed-reply branch (telegram/loop.py, "A1")
            # for a pending AskUserQuestion, call for call.
            text = line["text"]
            flow = claude_mod.get_ask_question_flow(channel_id=CHAT_ID)
            if flow is not None and flow.awaiting_text:
                flow.awaiting_text = False
                current = flow.questions[flow.current_index]
                key = current.get("question", f"Question {flow.current_index + 1}")
                flow.answers[key] = text
                flow.current_index += 1
                if flow.current_index < len(flow.questions):
                    from untether.telegram.commands.ask_question import (
                        send_next_ask_question_message,
                    )

                    await send_next_ask_question_message(
                        self.transport,
                        chat_id=CHAT_ID,
                        user_msg_id=line.get("msg_id", 40),
                        thread_id=None,
                        flow=flow,
                    )
                    return "next_question"
                return await claude_mod.answer_ask_question_with_options(
                    flow.request_id
                )
            pending = claude_mod.get_pending_ask_request(channel_id=CHAT_ID)
            if pending is None:
                return "no_pending_question"
            return await claude_mod.answer_ask_question(pending[0], text)
        if kind == "followup":
            options = (
                EngineRunOptions(**line["options"])
                if "options" in line
                else self.run_options
            )

            async def options_for(_job: ThreadJob) -> Any:
                return options

            async def cwd_for(_job: ThreadJob) -> Path | None:
                # The run's own cwd is unset (None); "cwd" simulates /ctx.
                return Path(line["cwd"]) if "cwd" in line else None

            return await inject_live_followup(
                ThreadJob(
                    chat_id=CHAT_ID,
                    user_msg_id=line.get("msg_id", 20),
                    text=line["text"],
                    resume_token=token,
                ),
                options_for=options_for,
                cwd_for=cwd_for,
            )
        if kind == "steer":
            return await maybe_steer(
                self.tg_cfg,
                chat_id=CHAT_ID,
                user_msg_id=line.get("msg_id", 30),
                thread_id=None,
                topic_thread_id=None,
                prompt_text=line["text"],
                engine=line.get("engine", "claude"),
                # "engine" = a chat on another engine: no Claude session.
                resume_token=None if "engine" in line else token,
                override="steer",
                running_tasks=self.running_tasks,
                chat_prefs=None,
                topic_store=None,
            )
        if kind == "queued_note":
            return _queued_wait_note(token)
        if kind == "cancel":
            await handle_cancel(
                self.tg_cfg,
                TelegramIncomingMessage(
                    transport="telegram",
                    chat_id=CHAT_ID,
                    message_id=line.get("msg_id", 50),
                    text="/cancel",
                    reply_to_message_id=line.get("reply_to"),
                    reply_to_text=None,
                    sender_id=SENDER_ID,
                ),
                self.running_tasks,
            )
            if line.get("gone"):
                # A cancel mid-turn kills the CLI: wait for it and the run.
                puppet = self.current
                assert puppet is not None
                await self._until("the CLI to be killed", lambda: puppet.gone)
                for done in self.runs_done:
                    await self._until("the run to end", done.is_set)
            return None
        if kind == "drain":
            return await close_idle_live_sessions(
                self.running_tasks, line.get("reason", "drain")
            )
        if kind == "advance_time":
            self.time.offset += float(line["s"])
            return None
        if kind == "message":
            self._start_message(
                tg,
                line["text"],
                line.get("msg_id", 60),
                token if line.get("resume") else None,
            )
            return None
        if kind == "accept":
            await self._accept()
            return self.norm.value(self.current.argv)  # type: ignore[union-attr]
        if kind == "eof":
            puppet = self.current
            assert puppet is not None
            await self._until("the runner to close stdin", lambda: puppet.eof)
            return None
        if kind == "exit":
            puppet = self.current
            assert puppet is not None
            await puppet.send({"exit": int(line.get("rc", 0))})
            await self._until("the CLI to exit", lambda: puppet.gone)
            if line.get("then_accept"):
                # A queued resume spawns once the old process is gone. How
                # soon is a poll in the bridge, so both belong to one step.
                await self._accept()
                return self.norm.value(self.current.argv)  # type: ignore[union-attr]
            return None
        if kind == "end":
            for done in self.runs_done:
                await self._until("the run to end", done.is_set)
            return None
        raise AssertionError(f"unknown directive {kind!r}")

    def _fill(self, obj: Any, puppet: _Puppet) -> Any:
        """Resolve ``"$stdin:<dotted.path>"``: a value the CLI echoes back
        from the last line the runner wrote to it (a follow-up's random
        ``uuid``, a parent-initiated request id)."""
        if isinstance(obj, dict):
            return {k: self._fill(v, puppet) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._fill(v, puppet) for v in obj]
        if isinstance(obj, str) and obj.startswith("$stdin:"):
            value: Any = json.loads(puppet.stdin[-1])
            for part in obj.removeprefix("$stdin:").split("."):
                value = value[part]
            return value
        return obj

    async def _emit(self, line: dict[str, Any]) -> None:
        puppet = self.current
        assert puppet is not None, "no CLI process to print on"
        seen = self.lines.seen
        await puppet.send(
            {"emit": {"session_id": self.session, **self._fill(line, puppet)}}
        )
        await self._until(
            "the runner to translate the line", lambda: self.lines.seen > seen
        )

    # -- recording -----------------------------------------------------------

    def _record(self, label: str, result: Any, stdin: list[str]) -> None:
        step: dict[str, Any] = {"step": label}
        if result is not None:
            step["result"] = self.norm.value(result)
        events = self.lines.events[self._marks["events"] :]
        self._marks["events"] = len(self.lines.events)
        if events:
            step["events"] = [self._event(e) for e in events]
        if stdin:
            step["stdin"] = [self.norm.value(json.loads(raw)) for raw in stdin]
        telegram = self.transport.log[self._marks["telegram"] :]
        self._marks["telegram"] = len(self.transport.log)
        answers = self.bot.callback_calls[self._marks["bot"] :]
        self._marks["bot"] = len(self.bot.callback_calls)
        # Within one step, calls are grouped per message (ids are handed out
        # in send order, so the order of sends is kept; so is every
        # message's own send → edit → delete sequence). Only the interleaving
        # of two messages' edits inside a step is dropped: at run end that
        # is decided by which finaliser task wakes first.
        ordered = sorted(telegram, key=lambda e: e["id"])
        calls = [dict(e) for e in ordered] + [
            {"op": "answer_callback", **self.norm.value(a)} for a in answers
        ]
        if calls:
            step["telegram"] = calls
        logs = self.logs[self._marks["logs"] :]
        self._marks["logs"] = len(self.logs)
        log_lines = sorted(
            line for e in logs if (line := _log_line(e, self.norm)) is not None
        )
        if log_lines:
            step["logs"] = log_lines
        registry = _registry_snapshot(self.norm)
        before = self._last_registry
        changed = _diff(before, registry)
        if changed:
            # Only what this step changed: the snapshot reads as transitions.
            step["registry"] = changed
        self._last_registry = registry
        self.steps.append(step)

    def _event(self, event: Any) -> dict[str, Any]:
        out = self.norm.value(event)
        out.pop("engine", None)
        if out.get("started_ago_s") is not None:
            out["started_ago_s"] = "<s>"
        return {k: v for k, v in out.items() if v is not None and v != {}}

    # -- the run -------------------------------------------------------------

    async def run(self) -> dict[str, Any]:
        _clear_registries()
        self._setup()
        try:
            with capture_logs() as logs:
                self.logs = logs
                await self._run()
        except BaseException:
            # Debugging aid: what was recorded before the replay broke.
            partial = self.tmp_path / f"{self.name}.partial.json"
            partial.write_text(_dump({"steps": self.steps}), encoding="utf-8")
            print(f"golden replay {self.name}: partial snapshot at {partial}")
            raise
        finally:
            _clear_registries()
            shutil.rmtree(self.sock_dir, ignore_errors=True)
        return {
            "scenario": self.name,
            "config": {k: v for k, v in self.config.items() if k != "_do"},
            "steps": self.steps,
        }

    async def _serve(self, listener: Any, tg: Any) -> None:
        async def handle(stream: Any) -> None:
            puppet = _Puppet(stream)
            self.puppets.append(puppet)
            await puppet.pump()

        await listener.serve(handle, tg)

    async def _run(self) -> None:
        listener = await anyio.create_unix_listener(self.sock_dir / "s")
        async with listener, anyio.create_task_group() as tg:
            tg.start_soon(self._serve, listener, tg)
            try:
                with anyio.fail_after(REPLAY_TIMEOUT_S):
                    self._start_message(
                        tg, self.config.get("prompt", "go"), USER_MSG_ID, None
                    )
                    await self._step(tg, {"_do": "accept", "_stdin": 2})
                    for line in self.script:
                        await self._step(tg, dict(line))
                    for done in self.runs_done:
                        await self._until("the run to end", done.is_set)
                    await self._settle()
                    self._record("end", None, self._take_stdin(None))
            finally:
                for puppet in self.puppets:
                    if not puppet.gone:
                        with anyio.CancelScope(shield=True), anyio.move_on_after(2):
                            await puppet.send({"exit": 0})
                tg.cancel_scope.cancel()

    def _take_stdin(self, count: int | None) -> list[str]:
        puppet = self.current
        if puppet is None:
            return []
        end = len(puppet.stdin) if count is None else puppet.taken + count
        taken = puppet.stdin[puppet.taken : end]
        puppet.taken = end
        return taken

    async def _step(self, tg: Any, line: dict[str, Any]) -> None:
        # ``_stdin``: how many lines this step makes the runner write to the
        # CLI. ``_eof``: the step ends with the runner closing the CLI's
        # stdin (a close it decides on, or a timed idle close). ``_telegram``:
        # the step ends with a message containing that text (one sent after a
        # timeout). All are waited for, so what a step caused is recorded
        # with it however slow the host is.
        expect = int(line.pop("_stdin", 0))
        wait_eof = bool(line.pop("_eof", False))
        wait_text: str | None = line.pop("_telegram", None)
        result: Any = None
        label = _label(line)
        if "_do" in line:
            result = await self._do(tg, line)
        else:
            await self._emit(line)
        puppet = self.current
        if expect and puppet is not None:
            want = puppet.taken + expect
            await self._until(
                f"{expect} line(s) on the CLI's stdin",
                lambda: len(puppet.stdin) >= want,
            )
        if wait_eof and puppet is not None:
            await self._until("the runner to close stdin", lambda: puppet.eof)
        if wait_text is not None:
            await self._until(
                f"a message containing {wait_text!r}",
                lambda: any(wait_text in e.get("text", "") for e in self.transport.log),
            )
        await self._settle()
        self._record(label, result, self._take_stdin(expect))


def _dump(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


async def _replay(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    return await _Replay(name, tmp_path, monkeypatch).run()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize("name", _scenario_names())
async def test_claude_golden_replay(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = _dump(await _replay(name, tmp_path, monkeypatch))
    golden_path = GOLDEN_DIR / f"{name}.json"
    if _updating():
        golden_path.write_text(actual, encoding="utf-8")
        return
    assert golden_path.exists(), (
        f"no snapshot for {name}: run with {UPDATE_ENV}=1 to record it"
    )
    expected = golden_path.read_text(encoding="utf-8")
    if actual != expected:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"golden/{name}.json",
                tofile="this run",
                n=3,
            )
        )
        pytest.fail(
            f"Claude golden replay {name!r} changed. A refactor must not change "
            f"it; a deliberate behaviour change regenerates it with "
            f"{UPDATE_ENV}=1 and updates GOLDEN_SHA256.\n{diff[:12000]}",
            pytrace=False,
        )


def _golden_hashes() -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(GOLDEN_DIR.iterdir())
        if p.is_file()
    }


def test_golden_snapshots_not_regenerated_in_refactor_commits() -> None:
    """The fixture set and every snapshot's content are pinned here, so a
    regenerated (or added, or dropped) snapshot is always a visible diff in
    this file too."""
    actual = _golden_hashes()
    if actual != GOLDEN_SHA256:
        block = "GOLDEN_SHA256: dict[str, str] = {\n"
        block += "".join(f'    "{k}": "{v}",\n' for k, v in actual.items())
        block += "}"
        pytest.fail(
            "tests/fixtures/claude_golden/ no longer matches the pinned hashes. "
            "If this commit is a refactor, revert the snapshot change. If the "
            "behaviour change is deliberate, replace the pin with:\n" + block,
            pytrace=False,
        )


def test_every_scenario_has_a_snapshot() -> None:
    names = _scenario_names()
    assert names, "no golden scenarios found"
    missing = [n for n in names if not (GOLDEN_DIR / f"{n}.json").exists()]
    assert not missing, f"scenarios without a snapshot: {missing}"


_HOST_LEAKS = (
    re.compile(r"/home/[a-z]"),
    re.compile(r"/tmp/"),
    re.compile(r"/Users/"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}"),  # a Telegram bot token
    re.compile(r"sk-[A-Za-z0-9-]{10,}"),
    re.compile(r"-100\d{8,}"),  # a supergroup chat id
)


def test_golden_fixtures_hold_no_host_data() -> None:
    """Scripts and snapshots are synthetic: no host path, token or real chat
    id may reach either."""
    for path in sorted(GOLDEN_DIR.iterdir()):
        text = path.read_text(encoding="utf-8")
        for pattern in _HOST_LEAKS:
            assert not pattern.search(text), f"{path.name} matches {pattern.pattern}"
