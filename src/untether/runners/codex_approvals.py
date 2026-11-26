"""Codex app-server approval cards (#963).

Codex asks with three server requests (``item/commandExecution|fileChange|
permissions/requestApproval``); the runner defers each (``codex_rpc.DEFER``),
this module turns it into a Telegram card and maps each button to Codex's own
decision, and :class:`CodexResponder` writes the answer from the callback
task. Everything shown is Codex's data; only the rendering is Untether's.

Button → decision (plan §5.4, D2, D3; P-A on 0.171.0: an unlisted
``decline`` is accepted and the turn carries on):

========================  ==================  ==============  =================
button (``ia:`` action)   command             file change     permissions
========================  ==================  ==============  =================
✅ Approve (approve)      accept              accept          grant, turn
🔁 Allow for session      acceptForSession    acceptForSess.  grant, session
📌 Always allow (always)  amendment object    —               —
❌ Deny (deny)            decline             decline         grant nothing
🛑 Stop (stop)            cancel              cancel          —
========================  ==================  ==============  =================
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any

from ..background_status import escape_markdown
from ..interactive.preview import (
    format_command_preview,
    format_diff_preview,
    format_unified_diff_preview,
)
from ..interactive.registry import Decision
from ..logging import get_logger
from ..markdown import inline_code
from ..schemas import codex_app_server as cas
from ..utils.error_display import redact_url_secrets
from ..utils.paths import relativize_path
from .codex_rpc import CodexRpcClosed, CodexRpcPeer, CodexRpcWriteTimeout

logger = get_logger(__name__)

ENGINE = "codex"
CALLBACK_PREFIX = "ia"
COMMAND_APPROVAL = "item/commandExecution/requestApproval"
FILE_APPROVAL = "item/fileChange/requestApproval"
PERMISSIONS_APPROVAL = "item/permissions/requestApproval"
APPROVAL_METHODS = frozenset({COMMAND_APPROVAL, FILE_APPROVAL, PERMISSIONS_APPROVAL})
_PREFIX_MAX = 40

# Closing row per answer (the card's title after the tap).
ANSWER_TITLES: dict[str, str] = {
    "approve": "✅ Approved",
    "session": "🔁 Allowed for session",
    "always": "📌 Always allowed",
    "deny": "❌ Denied",
    "stop": "🛑 Stopped",
}
_KIND_NAMES = {
    COMMAND_APPROVAL: "command",
    FILE_APPROVAL: "file change",
    PERMISSIONS_APPROVAL: "permissions",
}


@dataclass(slots=True)
class Card:
    title: str
    # Short label for the closing row ("curl -sI https://…").
    label: str
    # ia action -> the JSON-RPC result Codex gets for it.
    decisions: dict[str, Any]
    buttons: list[list[dict[str, str]]]


@dataclass(slots=True)
class Pending:
    rpc_id: int | str
    method: str
    action_id: str
    label: str
    decisions: dict[str, Any]
    answer: str | None = None
    # #964 §16 item 5: the main turn the card belongs to.
    turn_id: str | None = None


@dataclass(slots=True)
class CodexApprovals:
    """One run's open cards, keyed by Untether's request id."""

    session_key: str
    peer: CodexRpcPeer
    pending: dict[str, Pending] = field(default_factory=dict)
    by_rpc: dict[int | str, str] = field(default_factory=dict)


# session_key ("codex:<thread id>") -> the run's approvals (Antigravity's
# ``_RUNS`` pattern); removed with ``registry.drop_session`` at run end.
_RUNS: dict[str, CodexApprovals] = {}


def kind_name(method: str) -> str:
    return _KIND_NAMES.get(method, method)


def new_request_id() -> str:
    # ``ia:session:cx_<12 hex>`` stays far under Telegram's 64-byte limit.
    return f"cx_{secrets.token_hex(6)}"


def _command_actions(params: cas.CommandApprovalParams) -> str:
    """Codex's parsed summary (``commandActions``): an extra line only, never
    a stand-in for the command that actually runs."""
    actions = [
        a.get("command")
        for a in params.command_actions or ()
        if isinstance(a, dict) and isinstance(a.get("command"), str)
    ]
    return "; ".join(actions)


def _short(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _code(text: str, limit: int = 60) -> str:
    """A value from Codex on a card (command, host, path, tool, kind) as a
    code span its own backticks or markdown can't break out of."""
    return inline_code(_short(text, limit)) or '""'


def _command_decisions(params: cas.CommandApprovalParams) -> list[Any]:
    """Codex's decision list: ``availableDecisions`` when the server sends it
    (an experimental field it sends on stable connections, findings H2),
    else Codex's own ``default_available_decisions`` (approvals.rs L379)."""
    if params.available_decisions:
        return list(params.available_decisions)
    if params.network_approval_context is not None:
        listed: list[Any] = ["accept", "acceptForSession"]
        allow = next(
            (
                a
                for a in params.proposed_network_policy_amendments or ()
                if isinstance(a, dict) and a.get("action") == "allow"
            ),
            None,
        )
        if allow is not None:
            listed.append(
                {"applyNetworkPolicyAmendment": {"network_policy_amendment": allow}}
            )
        return [*listed, "cancel"]
    listed = ["accept"]
    if params.proposed_execpolicy_amendment:
        listed.append(
            {
                "acceptWithExecpolicyAmendment": {
                    "execpolicy_amendment": params.proposed_execpolicy_amendment
                }
            }
        )
    return [*listed, "cancel"]


def _always_label(decision: dict[str, Any]) -> str | None:
    """Button text for an object decision, or None for one Untether doesn't
    know (skipped, never rendered)."""
    if "acceptWithExecpolicyAmendment" in decision:
        inner = decision["acceptWithExecpolicyAmendment"]
        prefix = inner.get("execpolicy_amendment") if isinstance(inner, dict) else None
        if not isinstance(prefix, list) or not prefix:
            return None
        text = " ".join(str(p) for p in prefix)
        if "\n" in text:
            return None  # the TUI never offers a multi-line prefix either
        return f'📌 Always allow "{_short(redact_url_secrets(text), _PREFIX_MAX)}" (saved on this host)'
    if "applyNetworkPolicyAmendment" in decision:
        inner = decision["applyNetworkPolicyAmendment"]
        rule = (
            inner.get("network_policy_amendment") if isinstance(inner, dict) else None
        )
        if not isinstance(rule, dict) or rule.get("action") != "allow":
            return None
        host = rule.get("host")
        if not isinstance(host, str) or not host:
            return None
        return f'📌 Always allow "{_short(host, _PREFIX_MAX)}" (saved on this host)'
    return None


def _keyboard(
    request_id: str, decisions: dict[str, Any], labels: dict[str, str]
) -> list[list[dict[str, str]]]:
    # Built before the request is registered, so the prefix is spelled out
    # (approval_keyboard would read it from the registry).
    rows = [
        [
            {
                "text": "✅ Approve",
                "callback_data": f"{CALLBACK_PREFIX}:approve:{request_id}",
            },
            {
                "text": "❌ Deny",
                "callback_data": f"{CALLBACK_PREFIX}:deny:{request_id}",
            },
        ]
    ]
    for action, default in (
        ("session", "🔁 Allow for session"),
        ("always", ""),
        ("stop", "🛑 Stop"),
    ):
        if action in decisions:
            rows.append(
                [
                    {
                        "text": labels.get(action, default),
                        "callback_data": f"{CALLBACK_PREFIX}:{action}:{request_id}",
                    }
                ]
            )
    return rows


def _file_change_lines(changes: list[Any], *, diff: bool) -> list[str]:
    lines: list[str] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        path = str(change.get("path") or "")
        kind = change.get("kind") if isinstance(change.get("kind"), dict) else {}
        kind_type = kind.get("type")
        text = redact_url_secrets(str(change.get("diff") or ""))
        shown = _code(relativize_path(path), 200)
        if kind_type == "delete":
            lines.append(f"🗑 {shown}")
        elif kind_type == "add":
            preview = format_diff_preview("Write", {"file_path": path, "content": text})
            lines.append(preview if diff and preview else f"📝 {shown} (new)")
        else:
            move = kind.get("move_path")
            if isinstance(move, str) and move:
                lines.append(f"📝 {shown} → {_code(relativize_path(move), 200)}")
                path = move
            preview = format_unified_diff_preview(path, text) if diff else ""
            lines.append(preview or f"📝 {shown}")
    return lines


def _entry_text(entry: Any) -> str | None:
    """One ``FileSystemSandboxEntry`` as a line, or None when Untether can't
    show it (then it isn't granted either)."""
    if not isinstance(entry, dict) or entry.get("access") not in {
        "read",
        "write",
        "deny",
    }:
        return None
    path = entry.get("path")
    if not isinstance(path, dict):
        return None
    if path.get("type") == "path" and isinstance(path.get("path"), str):
        where = _code(relativize_path(path["path"]), 4000)
    elif path.get("type") == "glob_pattern" and isinstance(path.get("pattern"), str):
        where = f"pattern {_code(path['pattern'], 4000)}"
    elif path.get("type") == "special" and isinstance(path.get("value"), str):
        where = f"special {_code(path['value'], 4000)}"
    else:
        return None
    return f"{entry['access'].title()}: {where}"


def permission_view(permissions: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """(card lines, the grant) for ``item/permissions/requestApproval``.

    The grant is built only from what the lines show (Codex takes a granted
    subset, ``GrantedPermissionProfile``), so a tap never grants something
    the user didn't see. Anything Untether can't show is listed as not
    granted. Every path is shown: nothing is trimmed."""
    lines: list[str] = []
    grant: dict[str, Any] = {}
    skipped: list[str] = []
    for key, value in permissions.items():
        if value is None:
            continue
        if key == "network" and isinstance(value, dict):
            enabled = value.get("enabled")
            if isinstance(enabled, bool):
                lines.append(f"Network: {'on' if enabled else 'off'}")
                grant["network"] = {"enabled": enabled}
            skipped += [
                f"network.{k}"
                for k, v in value.items()
                if k != "enabled" and v is not None
            ]
        elif key == "fileSystem" and isinstance(value, dict):
            fs: dict[str, Any] = {}
            for name, item in value.items():
                if item is None:
                    continue
                if (
                    name in ("read", "write")
                    and isinstance(item, list)
                    and all(isinstance(p, str) for p in item)
                ):
                    if item:
                        shown = ", ".join(_code(relativize_path(p), 4000) for p in item)
                        lines.append(f"{name.title()}: {shown}")
                        fs[name] = list(item)
                elif name == "entries" and isinstance(item, list):
                    kept = []
                    for entry in item:
                        text = _entry_text(entry)
                        if text is None:
                            skipped.append("fileSystem.entries")
                        else:
                            lines.append(text)
                            kept.append(entry)
                    if kept:
                        fs["entries"] = kept
                elif name == "globScanMaxDepth" and isinstance(item, int):
                    lines.append(f"Glob scan depth: {item}")
                    fs[name] = item
                else:
                    skipped.append(f"fileSystem.{name}")
            if fs:
                grant["fileSystem"] = fs
        else:
            skipped.append(key)
    if skipped:
        lines.append(
            "Not granted from Telegram (Untether can't show it): "
            + escape_markdown(", ".join(sorted(set(skipped))))
        )
    return lines, grant


def build_card(
    request_id: str,
    method: str,
    params: Any,
    *,
    subagent: bool,
    file_changes: list[Any] | None,
    diff_preview: bool | None,
) -> Card:
    """The card for one decoded approval request (plan §5.3)."""
    labels: dict[str, str] = {}
    body: list[str] = []
    decisions: dict[str, Any]
    if isinstance(params, cas.CommandApprovalParams):
        # Always Codex's own command string, the one that runs (redacted);
        # the parsed commandActions only as an extra summary line.
        command = redact_url_secrets(params.command or "")
        summary = redact_url_secrets(_command_actions(params))
        net = params.network_approval_context or {}
        label = _code(summary or command)
        if params.network_approval_context is not None:
            host = str(net.get("host") or "?")
            protocol = net.get("protocol")
            what = f"Network access to {_code(host)}" + (
                f" ({escape_markdown(str(protocol))})" if protocol else ""
            )
            label = _code(host)
        elif params.kind == "writeStdin":
            what = "Send input to a running command"
        elif params.kind == "humanCommand":
            what = "Command needs your approval"
        else:
            what = "Run command"
        body.append(format_command_preview(command))
        if summary and summary != command:
            body.append(f"Codex reads it as: {inline_code(_short(summary, 200))}")
        if params.cwd and params.network_approval_context is None:
            body.append(f"in {_code(relativize_path(params.cwd), 200)}")
        decisions = {"deny": "decline"}  # D2: always offered
        for raw in _command_decisions(params):
            if raw == "accept":
                decisions["approve"] = "accept"
            elif raw == "acceptForSession":
                decisions["session"] = "acceptForSession"
            elif raw == "cancel":
                decisions["stop"] = "cancel"
            elif raw == "decline":
                decisions["deny"] = "decline"
            elif isinstance(raw, dict) and (text := _always_label(raw)) is not None:
                decisions["always"] = raw
                labels["always"] = text
            # anything else (e.g. a future decision) is skipped, never shown
        decisions.setdefault("approve", "accept")
    elif isinstance(params, cas.FileChangeApprovalParams):
        what = "Edit files"
        lines = _file_change_lines(file_changes or [], diff=diff_preview is not False)
        body.extend(lines)
        label = next(
            (
                _code(relativize_path(str(c.get("path"))))
                for c in file_changes or ()
                if isinstance(c, dict) and c.get("path")
            ),
            "files",
        )
        if params.grant_root:
            body.append(
                f"Asks to write under {_code(relativize_path(params.grant_root), 4000)}"
            )
        decisions = {
            "approve": "accept",
            "session": "acceptForSession",
            "deny": "decline",
            "stop": "cancel",
        }
    else:
        what = "Extra permissions"
        lines, grant = permission_view(params.permissions)
        body.extend(lines)
        label = "extra permissions"
        decisions = {
            "approve": {"permissions": grant, "scope": "turn"},
            "session": {"permissions": grant, "scope": "session"},
            "deny": {"permissions": {}, "scope": "turn"},
        }
    if not isinstance(params, cas.PermissionsApprovalParams):
        # Command and file answers are ``{"decision": …}``.
        decisions = {k: {"decision": v} for k, v in decisions.items()}
    reason = getattr(params, "reason", None)
    if reason:
        body.append(f"Reason: {escape_markdown(redact_url_secrets(reason))}")
    title = f"Permission Request [codex] - {what}" + (
        " (sub-agent)" if subagent else ""
    )
    text = "\n".join([title, *(line for line in body if line)])
    return Card(
        title=text,
        label=label,
        decisions=decisions,
        buttons=_keyboard(request_id, decisions, labels),
    )


_SHIELD = "\N{SHIELD}\N{VARIATION SELECTOR-16}"
# review.status → (title verb, ok, warning level).
_REVIEW_OUTCOMES: dict[str, tuple[str, bool, bool]] = {
    "approved": ("Auto-review allowed", True, False),
    "denied": ("Auto-review denied", False, True),
    "timedOut": ("Auto-review timed out on", False, True),
    "aborted": ("Auto-review stopped on", False, False),
}


def review_action_text(action: Any) -> str:
    """What an auto-review looked at, from the ``[UNSTABLE]`` action payload
    (read as a dict: a missing key gives the bare type, never an error)."""
    if not isinstance(action, dict):
        return "an action"
    kind = action.get("type")
    if kind == "command" and isinstance(action.get("command"), str):
        return _code(redact_url_secrets(action["command"]))
    if kind == "execve" and isinstance(action.get("argv"), list):
        return _code(redact_url_secrets(" ".join(map(str, action["argv"]))))
    if kind == "applyPatch" and isinstance(action.get("files"), list):
        count = len(action["files"])
        return f"an edit to {count} file{'s' if count != 1 else ''}"
    if kind == "networkAccess" and isinstance(action.get("host"), str):
        return f"network access to {_code(action['host'])}"
    if kind == "mcpToolCall" and isinstance(action.get("toolName"), str):
        return f"MCP tool {_code(action['toolName'])}"
    if kind == "requestPermissions":
        return "extra permissions"
    if kind == "writeStdin":
        return "input to a running command"
    return _code(kind) if isinstance(kind, str) and kind else "an action"


def review_row(phase: str, params: dict[str, Any]) -> tuple[str, str, bool, bool]:
    """``item/autoApprovalReview/started|completed`` → (row id, title, ok,
    warning). The reviewer decides; nobody is asked (#963 Auto-review)."""
    review_id = params.get("reviewId")
    row_id = f"codex.review.{review_id if isinstance(review_id, str) else 'x'}"
    what = review_action_text(params.get("action"))
    if phase == "started":
        return row_id, f"{_SHIELD} Auto-review: {what}…", True, False
    review = params.get("review") if isinstance(params.get("review"), dict) else {}
    status = review.get("status")
    verb, ok, warn = _REVIEW_OUTCOMES.get(status, ("Auto-review", True, False))
    title = f"{_SHIELD} {verb} {what}"
    rationale = review.get("rationale")
    if not ok and isinstance(rationale, str) and rationale:
        title += f": {escape_markdown(_short(redact_url_secrets(rationale), 200))}"
    return row_id, title, ok, warn


class CodexResponder:
    """Delivers a Telegram answer to the Codex request a card stands for."""

    engine = ENGINE
    callback_prefix = CALLBACK_PREFIX

    def is_alive(self, session_key: str) -> bool:
        run = _RUNS.get(session_key)
        return run is not None and run.peer.closed_reason is None

    async def respond(
        self, request_id: str, session_key: str, decision: Decision
    ) -> bool:
        run = _RUNS.get(session_key)
        pending = run.pending.get(request_id) if run is not None else None
        if run is None or pending is None:
            return False
        if decision.action in pending.decisions:
            action = decision.action
        else:
            # A tap Untether didn't offer for this request: fail closed.
            action = "approve" if decision.approved else "deny"
            if action not in pending.decisions:
                return False
        try:
            written = await run.peer.reply(pending.rpc_id, pending.decisions[action])
        except (CodexRpcClosed, CodexRpcWriteTimeout):
            return False
        if written:
            pending.answer = action
            logger.info(
                "codex.app_server.approval.answered",
                request_id=request_id,
                method=pending.method,
                action=action,
                decision=wire_name(pending.decisions[action]),
            )
        return written


def wire_name(result: Any) -> str:
    """A decision for logs: the string, an object decision's key, or a
    permissions answer's scope (``deny`` when it grants nothing)."""
    if isinstance(result, dict) and "decision" in result:
        result = result["decision"]
    if isinstance(result, str):
        return result
    if isinstance(result, dict) and "permissions" in result:
        return f"grant:{result.get('scope')}" if result["permissions"] else "deny"
    if isinstance(result, dict) and result:
        return str(next(iter(result)))
    return "?"


RESPONDER = CodexResponder()
