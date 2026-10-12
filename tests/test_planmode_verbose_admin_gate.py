"""#1042: `/planmode` and `/verbose` need a group admin, like `/config`.

Both commands write the settings `/config pm:*` and `/config vb:*` write, so
a member refused in the menu must not be able to type the command instead.
Reading (`/planmode show`, the usage reply) stays open.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from tests.telegram_fakes import FakeBot, FakeTransport, make_cfg
from untether.telegram.api_schemas import ChatMember
from untether.telegram.commands import dispatch as dispatch_mod
from untether.telegram.commands import planmode as planmode_mod
from untether.telegram.commands import verbose as verbose_mod
from untether.telegram.commands.config import CONFIG_ADMIN_GATE
from untether.telegram.commands.dispatch import _dispatch_command
from untether.telegram.commands.overrides import SETTINGS_ADMIN_GATE
from untether.telegram.commands.planmode import PERMISSION_MODES
from untether.telegram.commands.verbose import _VERBOSE_OVERRIDES
from untether.telegram.types import TelegramIncomingMessage

GROUP = -1001234
ADMIN = 11
MEMBER = 22

DENIED = "changing settings is restricted to group admins."
LOOKUP_FAILED = "failed to verify settings permissions."
NO_SENDER = "cannot verify sender for settings changes."

# Bare `/planmode` toggles the stored mode, so it is a change too.
PLANMODE_MUTATING = ["", *sorted(PERMISSION_MODES), "clear", "ON", "  off  "]
PLANMODE_OPEN = ["show", "SHOW", " show ", "help", "nonsense", "on extra"]
# `/verbose` has no read-only form: anything it doesn't recognise toggles.
VERBOSE_MUTATING = [
    "",
    "on",
    "verbose",
    "off",
    "compact",
    "clear",
    "reset",
    "show",
    "anything",
]

MUTATING = [
    *(("planmode", args) for args in PLANMODE_MUTATING),
    *(("verbose", args) for args in VERBOSE_MUTATING),
]
OPEN = [("planmode", args) for args in PLANMODE_OPEN]

_MODULES = {"planmode": planmode_mod, "verbose": verbose_mod}


@pytest.fixture(autouse=True)
def _clear_verbose():
    _VERBOSE_OVERRIDES.clear()
    yield
    _VERBOSE_OVERRIDES.clear()


def _cfg(*, status: str | None = "member", lookup_raises: bool = False):
    transport = FakeTransport()
    cfg = make_cfg(transport)
    bot: FakeBot = cfg.bot  # type: ignore[assignment]
    if lookup_raises:
        bot.get_chat_member = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    elif status is None:
        bot.get_chat_member = AsyncMock(return_value=None)  # type: ignore[method-assign]
    else:
        bot.get_chat_member = AsyncMock(  # type: ignore[method-assign]
            return_value=ChatMember(status=status)
        )
    return cfg, transport, bot


class _Spy:
    """Stands in for each backend's ``handle`` while keeping the real gate."""

    def __init__(self, monkeypatch) -> None:
        self.calls: list[tuple[str, str]] = []
        spy = self

        def _make(command_id: str):
            async def _handle(_self, ctx):
                spy.calls.append((command_id, ctx.args_text))
                return

            return _handle

        monkeypatch.setattr(planmode_mod.PlanModeCommand, "handle", _make("planmode"))
        monkeypatch.setattr(verbose_mod.VerboseCommand, "handle", _make("verbose"))
        _route(monkeypatch)


def _route(monkeypatch) -> None:
    monkeypatch.setattr(
        dispatch_mod,
        "get_command",
        lambda command_id, **kw: _MODULES[command_id].BACKEND,
    )


async def _type(
    cfg,
    command: str,
    args: str,
    *,
    chat_id: int = GROUP,
    sender_id: int | None = MEMBER,
    chat_type: str | None = "supergroup",
    thread_id: int | None = None,
):
    text = f"/{command} {args}".rstrip()
    msg = TelegramIncomingMessage(
        transport="telegram",
        chat_id=chat_id,
        message_id=7,
        text=text,
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=sender_id,
        chat_type=chat_type,
        thread_id=thread_id,
    )
    await _dispatch_command(
        cfg, msg, text, command, args, {}, AsyncMock(), None, False, None, None
    )


def _sent_texts(transport: FakeTransport) -> list[str]:
    return [call["message"].text for call in transport.send_calls]


class TestClassification:
    def test_same_refusal_wording_as_config(self):
        assert CONFIG_ADMIN_GATE is SETTINGS_ADMIN_GATE
        assert SETTINGS_ADMIN_GATE.denied == DENIED
        assert SETTINGS_ADMIN_GATE.failed_member == LOOKUP_FAILED
        assert SETTINGS_ADMIN_GATE.missing_sender == NO_SENDER

    @pytest.mark.parametrize("args", PLANMODE_MUTATING)
    def test_planmode_mutating(self, args: str):
        assert planmode_mod.BACKEND.admin_gate(args) is SETTINGS_ADMIN_GATE  # type: ignore[attr-defined]

    @pytest.mark.parametrize("args", PLANMODE_OPEN)
    def test_planmode_open(self, args: str):
        assert planmode_mod.BACKEND.admin_gate(args) is None  # type: ignore[attr-defined]

    @pytest.mark.parametrize("args", VERBOSE_MUTATING)
    def test_verbose_always_mutates(self, args: str):
        assert verbose_mod.BACKEND.admin_gate(args) is SETTINGS_ADMIN_GATE  # type: ignore[attr-defined]

    def test_every_planmode_mode_is_gated(self):
        """A mode added to ``PERMISSION_MODES`` is gated without a new entry."""
        for name in PERMISSION_MODES:
            assert planmode_mod.BACKEND.admin_gate(name) is SETTINGS_ADMIN_GATE  # type: ignore[attr-defined]


@pytest.mark.anyio
@pytest.mark.parametrize(("command", "args"), MUTATING)
async def test_non_admin_is_refused(monkeypatch, command: str, args: str):
    spy = _Spy(monkeypatch)
    cfg, transport, bot = _cfg(status="member")

    await _type(cfg, command, args)

    assert spy.calls == []
    assert _sent_texts(transport) == [DENIED]
    bot.get_chat_member.assert_awaited_once_with(GROUP, MEMBER)


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["creator", "administrator"])
@pytest.mark.parametrize(("command", "args"), MUTATING)
async def test_group_admin_is_allowed(monkeypatch, status, command, args):
    spy = _Spy(monkeypatch)
    cfg, transport, _bot = _cfg(status=status)

    await _type(cfg, command, args, sender_id=ADMIN)

    assert spy.calls == [(command, args)]
    assert transport.send_calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(("command", "args"), MUTATING)
async def test_private_chat_is_allowed_without_a_lookup(monkeypatch, command, args):
    spy = _Spy(monkeypatch)
    cfg, transport, bot = _cfg(status="member")

    await _type(cfg, command, args, chat_id=555, sender_id=555, chat_type="private")

    assert spy.calls == [(command, args)]
    assert transport.send_calls == []
    bot.get_chat_member.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(("command", "args"), OPEN)
async def test_read_only_forms_are_open_to_any_member(monkeypatch, command, args):
    spy = _Spy(monkeypatch)
    cfg, transport, bot = _cfg(status="member")

    await _type(cfg, command, args)

    assert spy.calls == [(command, args)]
    assert transport.send_calls == []
    bot.get_chat_member.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("raises", [False, True])
@pytest.mark.parametrize(("command", "args"), [("planmode", "off"), ("verbose", "on")])
async def test_admin_lookup_failure_fails_closed(monkeypatch, raises, command, args):
    spy = _Spy(monkeypatch)
    cfg, transport, _bot = _cfg(status=None, lookup_raises=raises)

    with capture_logs() as logs:
        await _type(cfg, command, args)

    assert spy.calls == []
    assert _sent_texts(transport) == [LOOKUP_FAILED]
    refused = [e for e in logs if e["event"] == "command.admin_required"]
    assert [(e["command"], e["reason"]) for e in refused] == [
        (command, "lookup_failed")
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(("command", "args"), [("planmode", "off"), ("verbose", "on")])
async def test_unknown_sender_is_refused(monkeypatch, command, args):
    spy = _Spy(monkeypatch)
    cfg, transport, bot = _cfg(status="administrator")

    await _type(cfg, command, args, sender_id=None)

    assert spy.calls == []
    assert _sent_texts(transport) == [NO_SENDER]
    bot.get_chat_member.assert_not_awaited()


@pytest.mark.anyio
async def test_refused_verbose_leaves_the_setting_alone_end_to_end(monkeypatch):
    _route(monkeypatch)
    cfg, transport, _bot = _cfg(status="member")

    for args in ("on", "", "clear"):
        await _type(cfg, "verbose", args)

    assert _VERBOSE_OVERRIDES == {}
    assert _sent_texts(transport) == [DENIED, DENIED, DENIED]


@pytest.mark.anyio
async def test_admin_verbose_applies_end_to_end(monkeypatch):
    _route(monkeypatch)
    cfg, transport, _bot = _cfg(status="administrator")

    await _type(cfg, "verbose", "on", sender_id=ADMIN)

    assert _VERBOSE_OVERRIDES == {GROUP: "verbose"}
    assert DENIED not in _sent_texts(transport)
