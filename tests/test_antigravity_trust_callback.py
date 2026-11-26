"""#1032 phase 2: the ``agy_trust`` callback-only backend — Trust current
config… (disclosure), ✅ Trust (writes the offer's fingerprint), Cancel.

Fail closed throughout: two taps (a ``yes`` that never went through the
disclosure writes nothing), offers bound to their chat and claimed once, a
group non-admin refused by dispatch before any toast, and a failed save
reports that nothing was trusted."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from tests.telegram_fakes import FakeTransport, make_cfg
from untether.antigravity import gate_trust
from untether.antigravity.gate_trust import Fingerprint
from untether.commands import get_command, is_callback_only
from untether.telegram.api_schemas import ChatMember
from untether.telegram.commands import antigravity_trust as trust_mod
from untether.telegram.commands import dispatch as dispatch_mod
from untether.telegram.commands.antigravity_trust import BACKEND
from untether.telegram.commands.dispatch import _dispatch_callback
from untether.telegram.types import TelegramCallbackQuery
from untether.transport import RenderedMessage
from untether.utils.antigravity_state import TRUST_FILENAME, TrustStore

CHAT = 4242
OTHER = 777
ROOT = "/work/demo"
FP = Fingerprint(
    digest="ab" * 32,
    paths=(".agents/hooks.json",),
    groups=("lint (/work/demo/.agents/hooks.json)",),
    agy_version="1.5.18",
)


@pytest.fixture(autouse=True)
def _fresh() -> None:
    gate_trust._reset_for_tests()


class _Executor:
    def __init__(self) -> None:
        self.edits: list[tuple[Any, RenderedMessage]] = []

    async def edit(self, ref: Any, message: RenderedMessage) -> Any:
        self.edits.append((ref, message))
        return ref

    async def send(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("the refusal message is edited, never answered anew")


def _ctx(
    args: str,
    tmp_path: Path | None,
    *,
    chat: int = CHAT,
    executor: _Executor | None = None,
) -> Any:
    return SimpleNamespace(
        args_text=args,
        message=SimpleNamespace(
            channel_id=chat, message_id=555, thread_id=None, sender_id=11
        ),
        callback_query_id="q-1",
        executor=executor or _Executor(),
        config_path=(tmp_path / "untether.toml") if tmp_path is not None else None,
    )


def _store(tmp_path: Path) -> TrustStore:
    return TrustStore(tmp_path / TRUST_FILENAME)


def _keyboard(message: RenderedMessage) -> list[list[dict[str, str]]]:
    return message.extra["reply_markup"]["inline_keyboard"]


async def _tap(action: str, token: str, tmp_path: Path, **kw: Any) -> _Executor:
    executor = kw.pop("executor", None) or _Executor()
    await BACKEND.handle(_ctx(f"{action}:{token}", tmp_path, executor=executor, **kw))
    return executor


@pytest.mark.anyio
async def test_trust_ask_shows_disclosure_and_keeps_offer(tmp_path: Path) -> None:
    token = gate_trust.register_offer(CHAT, ROOT, FP)
    executor = await _tap("ask", token, tmp_path)
    ((ref, msg),) = executor.edits
    assert ref.message_id == 555 and ref.channel_id == CHAT
    assert "Trust this agy config for Ask me in this project?" in msg.text
    assert ".agents/hooks.json" in msg.text and "lint" in msg.text
    assert FP.short in msg.text
    assert "without asking in Telegram" in msg.text
    assert "Scheduled runs never use it" in msg.text
    assert (
        "every file edit in this project will ask for approval while trust is on"
        in msg.text
    )
    assert len(msg.text) <= 1000
    assert _keyboard(msg) == [
        [
            {"text": "✅ Trust", "callback_data": f"agy_trust:yes:{token}"},
            {"text": "Cancel", "callback_data": f"agy_trust:no:{token}"},
        ]
    ]
    # Peeked, not claimed; nothing written.
    assert gate_trust.peek_offer(token, chat_id=CHAT) is not None
    assert _store(tmp_path).get(CHAT, ROOT) is None


@pytest.mark.anyio
async def test_trust_yes_writes_record_from_offer_fingerprint(tmp_path: Path) -> None:
    token = gate_trust.register_offer(CHAT, ROOT, FP)
    await _tap("ask", token, tmp_path)
    with capture_logs() as logs:
        executor = await _tap("yes", token, tmp_path)
    record = _store(tmp_path).get(CHAT, ROOT)
    assert record is not None
    assert record.fp == FP.digest and record.paths == FP.paths
    assert record.groups == FP.groups and record.agy == "1.5.18" and record.by == 11
    ((_ref, msg),) = executor.edits
    assert "Trusted for Ask me in this project" in msg.text and FP.short in msg.text
    assert "Send your message again" in msg.text
    assert _keyboard(msg) == []
    (log,) = [r for r in logs if r["event"] == "antigravity.config_trusted"]
    assert log["chat_id"] == CHAT and log["sender_id"] == 11
    assert log["fp"] == FP.short and log["root"] == ROOT
    # Claimed once: a second tap writes nothing more and says it expired.
    _store(tmp_path).revoke(CHAT, ROOT)
    again = await _tap("yes", token, tmp_path)
    assert "expired" in again.edits[0][1].text
    assert _store(tmp_path).get(CHAT, ROOT) is None


@pytest.mark.anyio
async def test_trust_yes_without_the_disclosure_writes_nothing(tmp_path: Path) -> None:
    """No one-tap trust: ``yes`` only after ``ask`` showed the disclosure."""
    token = gate_trust.register_offer(CHAT, ROOT, FP)
    with capture_logs() as logs:
        executor = await _tap("yes", token, tmp_path)
    assert _store(tmp_path).get(CHAT, ROOT) is None
    assert "expired" in executor.edits[0][1].text
    assert _keyboard(executor.edits[0][1]) == []
    assert not [r for r in logs if r["event"] == "antigravity.config_trusted"]
    assert [r for r in logs if r["event"] == "antigravity.config_trust.unconfirmed"]
    # The offer is still there for the proper two taps.
    await _tap("ask", token, tmp_path)
    await _tap("yes", token, tmp_path)
    assert _store(tmp_path).get(CHAT, ROOT) is not None


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["other_chat", "expired", "unknown"])
async def test_trust_yes_expired_or_other_chat_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    now = [1000.0]
    monkeypatch.setattr(gate_trust.time, "monotonic", lambda: now[0])
    token = gate_trust.register_offer(CHAT, ROOT, FP)
    await _tap("ask", token, tmp_path)
    chat = CHAT
    if case == "other_chat":
        chat = OTHER
        # Not even the disclosure opens in another chat.
        other = await _tap("ask", token, tmp_path, chat=OTHER)
        assert "expired" in other.edits[0][1].text
    elif case == "expired":
        now[0] += gate_trust.OFFER_TTL_S + 1
    else:
        token = "deadbeef"
    assert BACKEND.early_answer_toast(f"yes:{token}", channel_id=chat) == (
        trust_mod.EXPIRED_TOAST
    )
    executor = await _tap("yes", token, tmp_path, chat=chat)
    assert "expired" in executor.edits[0][1].text
    assert _store(tmp_path).get(CHAT, ROOT) is None
    assert _store(tmp_path).get(OTHER, ROOT) is None
    if case == "other_chat":
        # The bound chat can still use it.
        await _tap("yes", token, tmp_path)
        assert _store(tmp_path).get(CHAT, ROOT) is not None


@pytest.mark.anyio
async def test_trust_yes_save_failure_reports_nothing_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from untether.utils import antigravity_state

    def boom(path: Path, payload: Any) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(antigravity_state, "atomic_write_json", boom)
    token = gate_trust.register_offer(CHAT, ROOT, FP)
    await _tap("ask", token, tmp_path)
    with capture_logs() as logs:
        executor = await _tap("yes", token, tmp_path)
    assert executor.edits[0][1].text == trust_mod.SAVE_FAILED_TEXT
    assert not [r for r in logs if r["event"] == "antigravity.config_trusted"]
    monkeypatch.undo()
    assert _store(tmp_path).get(CHAT, ROOT) is None
    # No config path at all (ad-hoc runtime): nothing can be saved either.
    token = gate_trust.register_offer(CHAT, ROOT, FP)
    executor = _Executor()
    await BACKEND.handle(_ctx(f"ask:{token}", None, executor=executor))
    await BACKEND.handle(_ctx(f"yes:{token}", None, executor=executor))
    assert executor.edits[-1][1].text == trust_mod.SAVE_FAILED_TEXT


@pytest.mark.anyio
async def test_trust_no_restores_refusal_without_button(tmp_path: Path) -> None:
    token = gate_trust.register_offer(CHAT, ROOT, FP)
    await _tap("ask", token, tmp_path)
    executor = await _tap("no", token, tmp_path)
    ((_ref, msg),) = executor.edits
    assert "Not trusted" in msg.text and _keyboard(msg) == []
    assert _store(tmp_path).get(CHAT, ROOT) is None
    # Cancel used the offer up.
    assert gate_trust.peek_offer(token, chat_id=CHAT) is None


@pytest.mark.anyio
async def test_trust_invalid_callback_does_nothing(tmp_path: Path) -> None:
    for args in ("", "yes", "maybe:abc", "yes:", "ask:" + "x" * 80):
        executor = await _tap(*(args.split(":", 1) + [""])[:2], tmp_path)
        assert executor.edits == [] or "expired" in executor.edits[0][1].text
    assert _store(tmp_path).get(CHAT, ROOT) is None


def test_trust_backend_is_callback_only() -> None:
    backend = get_command("agy_trust")
    assert backend is not None and is_callback_only(backend)
    assert BACKEND.id == "agy_trust"
    # Every action needs a group admin (#1042), the confirm step included.
    for action in ("ask", "yes", "no", "nonsense"):
        assert BACKEND.admin_gate(f"{action}:abc") is not None


def _group_cfg(status: str | None):
    cfg = make_cfg(FakeTransport())
    if status is None:
        cfg.bot.get_chat_member = AsyncMock(return_value=None)  # type: ignore[method-assign]
    else:
        cfg.bot.get_chat_member = AsyncMock(  # type: ignore[method-assign]
            return_value=ChatMember(status=status)
        )
    return cfg


async def _dispatch_tap(cfg: Any, args: str, *, chat_id: int, sender_id: int | None):
    query = TelegramCallbackQuery(
        transport="telegram",
        chat_id=chat_id,
        message_id=42,
        callback_query_id="cb-1",
        data=f"agy_trust:{args}",
        sender_id=sender_id,
    )
    await _dispatch_callback(
        cfg, query, "agy_trust", args, None, {}, AsyncMock(), None, False, None, "cb-1"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["ask", "yes", "no"])
@pytest.mark.parametrize(
    ("status", "sender"), [("member", 22), (None, 22), ("administrator", None)]
)
async def test_trust_admin_gate_refuses_group_non_admin_before_toast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    status: str | None,
    sender: int | None,
) -> None:
    """A group member (or an unknown sender, or a failed lookup) is refused
    by dispatch before the early toast; ``handle`` never runs."""
    group = -1001234
    token = gate_trust.register_offer(group, ROOT, FP)
    gate_trust.confirm_offer(token, chat_id=group)
    handled: list[str] = []

    async def spy(_self: Any, ctx: Any) -> None:
        handled.append(ctx.args_text)

    monkeypatch.setattr(trust_mod.AntigravityTrustCommand, "handle", spy)
    monkeypatch.setattr(dispatch_mod, "get_command", lambda *a, **kw: BACKEND)
    cfg = _group_cfg(status)
    with capture_logs() as logs:
        await _dispatch_tap(cfg, f"{action}:{token}", chat_id=group, sender_id=sender)
    assert handled == []
    answers = [c["text"] for c in cfg.bot.callback_calls]
    assert len(answers) == 1 and answers[0] != trust_mod.EXPIRED_TOAST
    assert "restricted to group admins" in answers[0] or "verify" in answers[0]
    assert not [r for r in logs if r["event"] == "antigravity.config_trusted"]
    # The offer is untouched for an admin.
    assert gate_trust.peek_offer(token, chat_id=group) is not None


@pytest.mark.anyio
async def test_trust_admin_in_group_passes_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    group = -1001234
    token = gate_trust.register_offer(group, ROOT, FP)
    handled: list[str] = []

    async def spy(_self: Any, ctx: Any) -> None:
        handled.append(ctx.args_text)

    monkeypatch.setattr(trust_mod.AntigravityTrustCommand, "handle", spy)
    monkeypatch.setattr(dispatch_mod, "get_command", lambda *a, **kw: BACKEND)
    cfg = _group_cfg("administrator")
    await _dispatch_tap(cfg, f"ask:{token}", chat_id=group, sender_id=11)
    assert handled == [f"ask:{token}"]
