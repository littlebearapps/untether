"""Trust current config… on Antigravity's Ask me refusal (#1032).

Ask me refuses while agy loads hooks, plugins, agents or MCP servers
Untether doesn't manage. When the runner could fingerprint that whole set
for an attended run, the refusal carries ``🔓 Trust current config…``:

- ``agy_trust:ask:<token>`` edits the refusal into the disclosure (what will
  be trusted, the short digest, what the gate no longer covers) with
  **✅ Trust** and **Cancel**. Nothing is written.
- ``agy_trust:yes:<token>`` claims the offer once and stores the *offer's*
  fingerprint (what was shown, never a recomputation) in
  ``antigravity_trust.json``. Only after ``ask`` showed the disclosure: no
  one-tap trust.
- ``agy_trust:no:<token>`` uses the offer up and writes nothing.

Every action is admin-gated in groups (#1042, ``admin_gate``: dispatch
refuses before any toast) and the offer is bound to the chat it was posted
in. No automatic re-run (D9): the user sends the message again.
"""

from __future__ import annotations

import html
import os
import time
from pathlib import Path

from ...antigravity import gate_trust
from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger
from ...transport import MessageRef, RenderedMessage
from ...utils import antigravity_scan
from ...utils.antigravity_state import TRUST_FILENAME, TrustRecord, TrustStore
from .overrides import SETTINGS_ADMIN_GATE, AdminGate

logger = get_logger(__name__)

EXPIRED_TOAST = "This button has expired"
EXPIRED_TEXT = (
    "⚠️ This Trust offer has expired (or Untether restarted). Send your "
    "message again to get a new one."
)
CANCELLED_TEXT = (
    "Not trusted. Ask me stays off in this project while this agy config is "
    "loaded; Workspace and Full access still work."
)
SAVE_FAILED_TEXT = "⚠️ Couldn't save the trust record. Nothing was trusted."
_ACTIONS = frozenset({"ask", "yes", "no"})
_SHOWN_MAX = 10


def _parse(args_text: str) -> tuple[str, str] | None:
    action, _, token = (args_text or "").partition(":")
    if action not in _ACTIONS or not token or len(token) > 16:
        return None
    return action, token


def trust_store(config_path: Path | None) -> TrustStore | None:
    """The store the runner reads (beside ``untether.toml``)."""
    if config_path is None:
        return None
    return TrustStore(Path(config_path).with_name(TRUST_FILENAME))


def record_root(cwd: Path | None) -> str | None:
    """The key a run in *cwd* uses: the realpath of its project root."""
    if cwd is None:
        return None
    return os.path.realpath(antigravity_scan.find_project_root(Path(cwd)))


def revoke(
    config_path: Path | None, chat_id: int, root: str, *, sender_id: int | None
) -> bool | None:
    """``/config`` → 🔐 → Revoke trust. None when nothing was trusted; else
    whether the removal was saved (the record is gone in memory either way,
    and the runner re-reads the file)."""
    store = trust_store(config_path)
    record = store.get(chat_id, root) if store is not None else None
    if store is None or record is None:
        return None
    saved = store.revoke(chat_id, root)
    logger.warning(
        "antigravity.config_trust_revoked",
        reason="user",
        chat_id=chat_id,
        sender_id=sender_id,
        root=root,
        fp=record.fp[:12],
        saved=saved,
    )
    return saved


def _items(fp: gate_trust.Fingerprint) -> str:
    items = [f"<code>{html.escape(p, quote=False)}</code>" for p in fp.paths]
    items += [
        f"hook group <code>{html.escape(g, quote=False)}</code>" for g in fp.groups
    ]
    shown = ", ".join(items[:_SHOWN_MAX])
    more = len(items) - _SHOWN_MAX
    return f"{shown} and {more} more" if more > 0 else shown


def disclosure_html(fp: gate_trust.Fingerprint) -> str:
    return "\n".join(
        [
            "<b>Trust this agy config for Ask me in this project?</b>",
            _items(fp),
            f"Digest <code>{fp.short}</code>",
            "",
            "Ask me will run here with these loaded. Their own commands run"
            " <b>without asking in Telegram</b>, and they can see each tool call"
            " and add instructions for agy. agy's tool calls still need your"
            " approval as usual.",
            "Any change to this config, or an agy update, asks again. Scheduled"
            " runs never use it. Revoke any time in /config → 🔐 Telegram"
            " approvals.",
        ]
    )


def trusted_html(fp: gate_trust.Fingerprint) -> str:
    return (
        "🔓 Trusted for Ask me in this project (digest "
        f"<code>{fp.short}</code>). Send your message again."
    )


class AntigravityTrustCommand:
    """Callback backend for the Ask me refusal's Trust button."""

    id = "agy_trust"
    description = "Trust this project's agy config for Ask me"
    answer_early = True
    callback_only = True

    @staticmethod
    def admin_gate(args_text: str) -> AdminGate | None:
        # Every tap, the disclosure and Cancel included: trust is a chat
        # setting, and failing closed costs a group member nothing.
        return SETTINGS_ADMIN_GATE

    @staticmethod
    def early_answer_toast(
        args_text: str,
        *,
        channel_id: int | None = None,
        claim_owner: str | None = None,
    ) -> str | None:
        parsed = _parse(args_text)
        if parsed is None or channel_id is None:
            return None
        action, token = parsed
        offer = gate_trust.peek_offer(token, chat_id=channel_id)
        if offer is None:
            return EXPIRED_TOAST
        if action == "yes" and not gate_trust.is_confirmed(token):
            return EXPIRED_TOAST
        return None

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        parsed = _parse(ctx.args_text)
        if parsed is None:
            logger.warning("antigravity.config_trust.invalid_callback")
            return None
        action, token = parsed
        chat_id = ctx.message.channel_id
        ref = MessageRef(
            channel_id=chat_id,
            message_id=ctx.message.message_id,
            thread_id=ctx.message.thread_id,
        )
        if action == "ask":
            offer = gate_trust.confirm_offer(token, chat_id=chat_id)
            if offer is None:
                await _edit(ctx, ref, html.escape(EXPIRED_TEXT, quote=False))
                return None
            buttons = [
                [
                    {
                        "text": "✅ Trust",
                        "callback_data": gate_trust.callback_data("yes", token),
                    },
                    {
                        "text": "Cancel",
                        "callback_data": gate_trust.callback_data("no", token),
                    },
                ]
            ]
            await _edit(ctx, ref, disclosure_html(offer.fingerprint), buttons)
            return None
        if action == "no":
            offer = gate_trust.claim_offer(token, chat_id=chat_id)
            text = CANCELLED_TEXT if offer is not None else EXPIRED_TEXT
            if offer is not None:
                logger.info(
                    "antigravity.config_trust_cancelled",
                    chat_id=chat_id,
                    sender_id=ctx.message.sender_id,
                    fp=offer.fingerprint.short,
                )
            await _edit(ctx, ref, html.escape(text, quote=False))
            return None
        offer = gate_trust.claim_offer(token, chat_id=chat_id, confirmed=True)
        if offer is None:
            await _edit(ctx, ref, html.escape(EXPIRED_TEXT, quote=False))
            return None
        fp = offer.fingerprint
        store = trust_store(ctx.config_path)
        record = TrustRecord(
            fp=fp.digest,
            paths=fp.paths,
            groups=fp.groups,
            agy=fp.agy_version,
            at=time.time(),
            by=ctx.message.sender_id,
        )
        if store is None or not store.set(chat_id, offer.root, record):
            logger.warning(
                "antigravity.config_trust_save_failed",
                chat_id=chat_id,
                root=offer.root,
                fp=fp.short,
                has_path=store is not None,
            )
            await _edit(ctx, ref, html.escape(SAVE_FAILED_TEXT, quote=False))
            return None
        logger.info(
            "antigravity.config_trusted",
            chat_id=chat_id,
            sender_id=ctx.message.sender_id,
            root=offer.root,
            fp=fp.short,
            paths=list(fp.paths)[:20],
            groups=list(fp.groups)[:20],
        )
        await _edit(ctx, ref, trusted_html(fp))
        return None


async def _edit(
    ctx: CommandContext,
    ref: MessageRef,
    text_html: str,
    buttons: list[list[dict[str, str]]] | None = None,
) -> None:
    try:
        await ctx.executor.edit(
            ref,
            RenderedMessage(
                text=text_html,
                extra={
                    "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": buttons or []},
                },
            ),
        )
    except Exception:  # noqa: BLE001 — the decision stands either way
        logger.debug("antigravity.config_trust.edit_failed", exc_info=True)


BACKEND: CommandBackend = AntigravityTrustCommand()
