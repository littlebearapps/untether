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

from ..interactive.buttons import approval_keyboard
from ..interactive.preview import format_diff_preview, format_unified_diff_preview
from ..interactive.registry import Decision
from ..logging import get_logger
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


def _command_text(params: cas.CommandApprovalParams) -> str:
    """The friendly command (``commandActions``), else Codex's own string
    (often the ``zsh -lc '…'`` wrapper)."""
    actions = [
        a.get("command")
        for a in params.command_actions or ()
        if isinstance(a, dict) and isinstance(a.get("command"), str)
    ]
    if actions:
        return "\n".join(actions)
    return params.command or ""


def _short(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


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
        return f"📌 Always allow `{_short(redact_url_secrets(text), _PREFIX_MAX)}` (saved on this host)"
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
        return f"📌 Always allow `{_short(host, _PREFIX_MAX)}` (saved on this host)"
    return None


def _keyboard(
    request_id: str, decisions: dict[str, Any], labels: dict[str, str]
) -> list[list[dict[str, str]]]:
    rows = approval_keyboard(request_id)  # ✅ Approve / ❌ Deny
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
        shown = relativize_path(path)
        if kind_type == "delete":
            lines.append(f"🗑 {shown}")
        elif kind_type == "add":
            preview = format_diff_preview("Write", {"file_path": path, "content": text})
            lines.append(preview if diff and preview else f"📝 {shown} (new)")
        else:
            move = kind.get("move_path")
            if isinstance(move, str) and move:
                lines.append(f"📝 {shown} → {relativize_path(move)}")
                path = move
            preview = format_unified_diff_preview(path, text) if diff else ""
            lines.append(preview or f"📝 {shown}")
    return lines


def _permission_lines(permissions: dict[str, Any]) -> list[str]:
    """TUI ``format_additional_permissions_rule`` wording, loosely."""
    lines: list[str] = []
    network = permissions.get("network")
    if isinstance(network, dict) and network.get("enabled") is not None:
        lines.append(f"Network: {'on' if network['enabled'] else 'off'}")
    fs = permissions.get("fileSystem")
    if isinstance(fs, dict):
        for key, name in (("read", "Read"), ("write", "Write")):
            paths = fs.get(key)
            if isinstance(paths, list) and paths:
                shown = ", ".join(relativize_path(str(p)) for p in paths[:5])
                lines.append(f"{name}: {shown}")
    return lines


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
        command = redact_url_secrets(_command_text(params))
        net = params.network_approval_context or {}
        if params.network_approval_context is not None:
            host = str(net.get("host") or "?")
            protocol = net.get("protocol")
            what = f"Network access to `{host}`" + (
                f" ({protocol})" if protocol else ""
            )
            label = host
        elif params.kind == "writeStdin":
            what = "Send input to a running command"
            body.append(format_diff_preview("Bash", {"command": command}))
            label = _short(command)
        elif params.kind == "humanCommand":
            what = "Command needs your approval"
            body.append(format_diff_preview("Bash", {"command": command}))
            label = _short(command)
        else:
            what = "Run command"
            body.append(format_diff_preview("Bash", {"command": command}))
            label = _short(command)
        if params.cwd and params.network_approval_context is None:
            body.append(f"in {relativize_path(params.cwd)}")
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
                relativize_path(str(c.get("path")))
                for c in file_changes or ()
                if isinstance(c, dict) and c.get("path")
            ),
            "files",
        )
        if params.grant_root:
            body.append(f"Asks to write under {relativize_path(params.grant_root)}")
        decisions = {
            "approve": "accept",
            "session": "acceptForSession",
            "deny": "decline",
            "stop": "cancel",
        }
    else:
        what = "Extra permissions"
        body.extend(_permission_lines(params.permissions))
        label = "extra permissions"
        decisions = {
            "approve": {"permissions": params.permissions, "scope": "turn"},
            "session": {"permissions": params.permissions, "scope": "session"},
            "deny": {"permissions": {}, "scope": "turn"},
        }
    if not isinstance(params, cas.PermissionsApprovalParams):
        # Command and file answers are ``{"decision": …}``.
        decisions = {k: {"decision": v} for k, v in decisions.items()}
    reason = getattr(params, "reason", None)
    if reason:
        body.append(f"Reason: {redact_url_secrets(reason)}")
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
