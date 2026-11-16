"""#1042: `/config` changes need a group admin, as the text commands do.

Covers the single chokepoint (``classify_config_action`` consulted by both
dispatch paths), the refusal behaviour for taps and typed actions, and a
completeness check that every action the module can dispatch is classified.
"""

from __future__ import annotations

import ast
import inspect
import itertools
import re
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from tests.telegram_fakes import FakeBot, FakeTransport, make_cfg
from untether.telegram.api_schemas import ChatMember
from untether.telegram.commands import _antigravity_mode_text as agy_mode
from untether.telegram.commands import config as config_mod
from untether.telegram.commands import dispatch as dispatch_mod
from untether.telegram.commands.config import (
    _DYNAMIC_ACTION_PAGES,
    _MUTATING_ACTIONS,
    _OPEN_ACTIONS,
    _PAGES,
    BACKEND,
    CONFIG_ADMIN_GATE,
    classify_config_action,
    parse_config_args,
)
from untether.telegram.commands.dispatch import _dispatch_callback, _dispatch_command
from untether.telegram.commands.verbose import _VERBOSE_OVERRIDES
from untether.telegram.types import TelegramCallbackQuery, TelegramIncomingMessage

GROUP = -1001234
ADMIN = 11
MEMBER = 22


@pytest.fixture(autouse=True)
def _clear_verbose():
    _VERBOSE_OVERRIDES.clear()
    yield
    _VERBOSE_OVERRIDES.clear()


# ---------------------------------------------------------------------------
# Enumerating what the module can dispatch
# ---------------------------------------------------------------------------

# Engine ids are only known at runtime, so the Engine page is "every action
# mutates"; a real id stands in for the dynamic value in behaviour tests.
_DYNAMIC_SAMPLES = {"ag": ("claude", "codex", "no-such-engine")}


def _explicit_mutating() -> list[str]:
    out = [
        f"{page}:{action}"
        for page, actions in sorted(_MUTATING_ACTIONS.items())
        for action in sorted(actions)
    ]
    for page in sorted(_DYNAMIC_ACTION_PAGES):
        out.extend(f"{page}:{sample}" for sample in _DYNAMIC_SAMPLES[page])
    return out


MUTATING = _explicit_mutating()
OPEN = [
    "",
    "home",
    "nonsense",
    *sorted(_PAGES),
    *(f"{page}:" for page in sorted(_PAGES)),
    *(f"{p}:{a}" for p, acts in sorted(_OPEN_ACTIONS.items()) for a in sorted(acts)),
]


def _page_functions() -> dict[str, ast.AsyncFunctionDef]:
    tree = ast.parse(inspect.getsource(config_mod))
    by_name = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    }
    return {page: by_name[handler.__name__] for page, handler in _PAGES.items()}


def _const_strings(node: ast.expr) -> list[str] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Set | ast.Tuple | ast.List):
        out: list[str] = []
        for elt in node.elts:
            if not (isinstance(elt, ast.Constant) and isinstance(elt.value, str)):
                return None
            out.append(elt.value)
        return out
    return None


def _module_collection(node: ast.expr) -> list[str] | None:
    """Keys of a module-level dict/set the page tests ``action in`` against."""
    if isinstance(node, ast.Name):
        value = getattr(config_mod, node.id, None)
    elif (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "agy_mode"
    ):
        value = getattr(agy_mode, node.attr, None)
    else:
        return None
    if isinstance(value, dict | set | frozenset | tuple):
        return [str(item) for item in value]
    return None


def _dispatchable_actions() -> tuple[dict[str, set[str]], set[str]]:
    """Every action each page handler branches on, read from the source.

    Returns ``(actions per page, pages with a runtime-only action set)``.
    An unrecognised comparison on ``action`` fails the test, so a new shape
    has to be taught here (and classified) rather than slipping through.
    """
    found: dict[str, set[str]] = {page: set() for page in _PAGES}
    dynamic: set[str] = set()
    for page, func in _page_functions().items():
        assert [a.arg for a in func.args.args] == ["ctx", "action"], page
        prefixes: set[str] = set()
        suffixes: set[str] = set()
        compound = False
        for node in ast.walk(func):
            if isinstance(node, ast.Subscript) and (
                isinstance(node.slice, ast.Name) and node.slice.id == "act"
            ):
                assert isinstance(node.value, ast.Dict), ast.unparse(node)
                suffixes.update(k.value for k in node.value.keys)  # type: ignore[union-attr]
            if not isinstance(node, ast.Compare):
                continue
            left, op, right = node.left, node.ops[0], node.comparators[0]
            if isinstance(left, ast.Name) and left.id == "prefix":
                assert isinstance(op, ast.Eq), ast.unparse(node)
                prefixes.update(_const_strings(right) or [])
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if "action" not in names:
                continue
            shape = f"{page}: {ast.unparse(node)}"
            assert len(node.ops) == 1, shape
            if isinstance(op, ast.Is | ast.IsNot):
                continue
            if isinstance(right, ast.Name) and right.id == "action":
                # `"_" in action` — the Cost & usage page splits prefix_act.
                assert isinstance(op, ast.In) and _const_strings(left) == ["_"], shape
                compound = True
                continue
            assert isinstance(left, ast.Name) and left.id == "action", shape
            assert isinstance(op, ast.Eq | ast.In), shape
            literals = _const_strings(right)
            if literals is None:
                literals = _module_collection(right)
            if literals is None:
                # A local, runtime-built collection (engine ids).
                assert isinstance(right, ast.Name), shape
                dynamic.add(page)
                continue
            found[page].update(literals)
        if compound:
            assert prefixes and suffixes, page
            found[page].update(
                f"{p}_{s}" for p, s in itertools.product(prefixes, suffixes)
            )
        else:
            assert not prefixes and not suffixes, page
    return found, dynamic


# ---------------------------------------------------------------------------
# Classification (the chokepoint) and its completeness
# ---------------------------------------------------------------------------


class TestClassification:
    def test_every_page_has_a_classification_entry(self):
        assert set(_MUTATING_ACTIONS) | _DYNAMIC_ACTION_PAGES == set(_PAGES)
        assert not set(_MUTATING_ACTIONS) & _DYNAMIC_ACTION_PAGES
        assert set(_OPEN_ACTIONS) <= set(_PAGES)

    def test_no_action_is_both_open_and_mutating(self):
        for page, open_actions in _OPEN_ACTIONS.items():
            assert page not in _DYNAMIC_ACTION_PAGES
            assert not open_actions & _MUTATING_ACTIONS.get(page, frozenset())

    def test_every_action_a_handler_branches_on_is_classified(self):
        """Completeness: an action added to a page handler must be listed."""
        found, dynamic = _dispatchable_actions()
        assert dynamic == set(_DYNAMIC_ACTION_PAGES)
        # The extractor really sees the handlers (guards against a no-op test).
        assert sum(len(v) for v in found.values()) >= 50
        for page, actions in found.items():
            if page in _DYNAMIC_ACTION_PAGES:
                continue
            listed = _MUTATING_ACTIONS[page] | _OPEN_ACTIONS.get(page, frozenset())
            assert actions <= listed, (page, sorted(actions - listed))
            # ...and nothing stale is listed for an action that no longer exists.
            assert listed <= actions, (page, sorted(listed - actions))

    def test_every_button_the_module_draws_is_classified(self):
        # String literals only: comments may mention retired callback data.
        literals = [
            node.value
            for node in ast.walk(ast.parse(inspect.getsource(config_mod)))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        drawn = {
            pair
            for text in literals
            for pair in re.findall(r"^config:([a-z]+):([a-z_]+)$", text)
        }
        assert len(drawn) >= 30
        for page, action in drawn:
            assert page in _PAGES, (page, action)
            if page in _DYNAMIC_ACTION_PAGES:
                continue
            listed = _MUTATING_ACTIONS[page] | _OPEN_ACTIONS.get(page, frozenset())
            assert action in listed, (page, action)

    def test_every_toast_action_is_classified(self):
        """The early toast announces a change, so each one must be gated."""
        func = next(
            n
            for n in ast.walk(ast.parse(inspect.getsource(config_mod)))
            if isinstance(n, ast.FunctionDef) and n.name == "early_answer_toast"
        )
        table = next(
            n.value
            for n in ast.walk(func)
            if isinstance(n, ast.AnnAssign)
            and isinstance(n.target, ast.Name)
            and n.target.id == "_TOAST_LABELS"
        )
        assert isinstance(table, ast.Dict)
        seen = 0
        for page_node, actions_node in zip(table.keys, table.values, strict=True):
            page = page_node.value  # type: ignore[union-attr]
            assert isinstance(actions_node, ast.Dict)
            for action_node in actions_node.keys:
                action = action_node.value  # type: ignore[union-attr]
                seen += 1
                assert classify_config_action(f"{page}:{action}") == "mutating"
                if page not in _DYNAMIC_ACTION_PAGES:
                    assert action in _MUTATING_ACTIONS[page], (page, action)
        assert seen >= 50

    @pytest.mark.parametrize("args", MUTATING)
    def test_mutating(self, args: str):
        assert classify_config_action(args) == "mutating"
        assert classify_config_action(f"  {args} ") == "mutating"

    @pytest.mark.parametrize("args", OPEN)
    def test_open(self, args: str):
        assert classify_config_action(args) == "open"

    @pytest.mark.parametrize(
        "args", ["pm:zzz", "tg:anything", "ab:x", "pm:on:extra", "cu:zz_on", "PM:on"]
    )
    def test_an_unlisted_action_on_a_page_fails_closed(self, args: str):
        page, _ = parse_config_args(args)
        expected = "mutating" if page in _PAGES else "open"
        assert classify_config_action(args) == expected

    def test_full_access_and_trigger_pause_are_gated(self):
        for args in ("pm:agf", "tg:pause", "tg:resume", "cu:bc_off", "loop:on"):
            assert classify_config_action(args) == "mutating"

    def test_handle_and_classifier_parse_identically(self):
        assert parse_config_args("") == (None, None)
        assert parse_config_args(" home ") == ("home", None)
        assert parse_config_args("pm") == ("pm", None)
        assert parse_config_args("pm:") == ("pm", None)
        assert parse_config_args("pm:on") == ("pm", "on")
        assert parse_config_args("ag:a:b") == ("ag", "a:b")

    def test_backend_exposes_the_gate_only_for_mutating_actions(self):
        assert BACKEND.admin_gate("pm:on") is CONFIG_ADMIN_GATE  # type: ignore[attr-defined]
        assert BACKEND.admin_gate("pm") is None  # type: ignore[attr-defined]
        assert BACKEND.admin_gate("") is None  # type: ignore[attr-defined]
        assert CONFIG_ADMIN_GATE.denied == (
            "changing settings is restricted to group admins."
        )


# ---------------------------------------------------------------------------
# Dispatch behaviour
# ---------------------------------------------------------------------------


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
    """Stands in for ``ConfigCommand.handle`` while keeping the real gate."""

    def __init__(self, monkeypatch) -> None:
        self.calls: list[str] = []

        async def _handle(_self, ctx):
            self.calls.append(ctx.args_text)
            return

        monkeypatch.setattr(config_mod.ConfigCommand, "handle", _handle)
        monkeypatch.setattr(dispatch_mod, "get_command", lambda *a, **kw: BACKEND)


async def _tap(cfg, args: str, *, chat_id: int = GROUP, sender_id: int | None = MEMBER):
    query = TelegramCallbackQuery(
        transport="telegram",
        chat_id=chat_id,
        message_id=42,
        callback_query_id="cb-1",
        data=f"config:{args}",
        sender_id=sender_id,
    )
    await _dispatch_callback(
        cfg, query, "config", args, None, {}, AsyncMock(), None, False, None, "cb-1"
    )


async def _type(
    cfg,
    args: str,
    *,
    chat_id: int = GROUP,
    sender_id: int | None = MEMBER,
    chat_type: str | None = "supergroup",
    thread_id: int | None = None,
):
    text = f"/config {args}".strip()
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
        cfg, msg, text, "config", args, {}, AsyncMock(), None, False, None, None
    )


def _sent_texts(transport: FakeTransport) -> list[str]:
    return [call["message"].text for call in transport.send_calls]


DENIED = "changing settings is restricted to group admins."
LOOKUP_FAILED = "failed to verify settings permissions."
NO_SENDER = "cannot verify sender for settings changes."


@pytest.mark.anyio
@pytest.mark.parametrize("args", MUTATING)
async def test_non_admin_tap_is_refused(monkeypatch, args: str):
    spy = _Spy(monkeypatch)
    cfg, transport, bot = _cfg(status="member")

    await _tap(cfg, args)

    assert spy.calls == []
    # One answer, the refusal — never the "Permission mode: on" style toast.
    assert [c["text"] for c in bot.callback_calls] == [DENIED]
    assert transport.send_calls == []
    assert transport.edit_calls == []
    bot.get_chat_member.assert_awaited_once_with(GROUP, MEMBER)


@pytest.mark.anyio
@pytest.mark.parametrize("args", MUTATING)
async def test_non_admin_typed_action_is_refused(monkeypatch, args: str):
    spy = _Spy(monkeypatch)
    cfg, transport, bot = _cfg(status="member")

    await _type(cfg, args)

    assert spy.calls == []
    assert _sent_texts(transport) == [DENIED]
    bot.get_chat_member.assert_awaited_once_with(GROUP, MEMBER)


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["creator", "administrator"])
@pytest.mark.parametrize("args", MUTATING)
async def test_group_admin_is_allowed_on_both_paths(monkeypatch, status, args):
    spy = _Spy(monkeypatch)
    cfg, transport, bot = _cfg(status=status)

    await _tap(cfg, args, sender_id=ADMIN)
    await _type(cfg, args, sender_id=ADMIN)

    assert spy.calls == [args, args]
    assert DENIED not in _sent_texts(transport)
    # The tap still gets its early toast, exactly as before.
    assert bot.callback_calls[0]["text"] == BACKEND.early_answer_toast(args)  # type: ignore[attr-defined]


@pytest.mark.anyio
@pytest.mark.parametrize("args", MUTATING)
async def test_private_chat_is_allowed_without_a_lookup(monkeypatch, args: str):
    spy = _Spy(monkeypatch)
    cfg, transport, bot = _cfg(status="member")

    await _tap(cfg, args, chat_id=555, sender_id=555)
    await _type(cfg, args, chat_id=555, sender_id=555, chat_type="private")

    assert spy.calls == [args, args]
    assert transport.send_calls == []
    bot.get_chat_member.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("args", OPEN)
async def test_navigation_is_open_to_any_member(monkeypatch, args: str):
    spy = _Spy(monkeypatch)
    cfg, transport, bot = _cfg(status="member")

    await _tap(cfg, args)
    await _type(cfg, args)

    assert spy.calls == [args, args]
    assert transport.send_calls == []
    bot.get_chat_member.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["member", "restricted", "left", "kicked", "x"])
async def test_only_creator_and_administrator_pass(monkeypatch, status: str):
    spy = _Spy(monkeypatch)
    cfg, transport, bot = _cfg(status=status)

    await _tap(cfg, "pm:agf")
    await _type(cfg, "tg:pause")

    assert spy.calls == []
    assert [c["text"] for c in bot.callback_calls] == [DENIED]
    assert _sent_texts(transport) == [DENIED]


@pytest.mark.anyio
@pytest.mark.parametrize("raises", [False, True])
async def test_admin_lookup_failure_fails_closed(monkeypatch, raises: bool):
    spy = _Spy(monkeypatch)
    cfg, transport, bot = _cfg(status=None, lookup_raises=raises)

    with capture_logs() as logs:
        await _tap(cfg, "pm:agf")
        await _type(cfg, "pm:agf")

    assert spy.calls == []
    assert [c["text"] for c in bot.callback_calls] == [LOOKUP_FAILED]
    assert _sent_texts(transport) == [LOOKUP_FAILED]
    refused = [e for e in logs if e["event"] == "command.admin_required"]
    assert [e["reason"] for e in refused] == ["lookup_failed", "lookup_failed"]


@pytest.mark.anyio
async def test_unknown_sender_is_refused_even_in_a_private_chat(monkeypatch):
    spy = _Spy(monkeypatch)
    cfg, transport, bot = _cfg(status="administrator")

    await _tap(cfg, "vb:on", sender_id=None)
    await _type(cfg, "vb:on", sender_id=None)
    await _type(cfg, "vb:on", chat_id=555, sender_id=None, chat_type="private")

    assert spy.calls == []
    assert [c["text"] for c in bot.callback_calls] == [NO_SENDER]
    assert _sent_texts(transport) == [NO_SENDER, NO_SENDER]
    bot.get_chat_member.assert_not_awaited()


@pytest.mark.anyio
async def test_forum_topic_checks_the_group_not_the_topic(monkeypatch):
    spy = _Spy(monkeypatch)
    cfg, transport, bot = _cfg(status="member")

    await _type(cfg, "pm:on", thread_id=77)

    assert spy.calls == []
    assert _sent_texts(transport) == [DENIED]
    assert transport.send_calls[0]["options"].thread_id == 77
    bot.get_chat_member.assert_awaited_once_with(GROUP, MEMBER)


@pytest.mark.anyio
async def test_refusal_is_logged_with_ids_only(monkeypatch):
    _Spy(monkeypatch)
    cfg, _transport, _bot = _cfg(status="member")

    with capture_logs() as logs:
        await _tap(cfg, "pm:agf")
        await _type(cfg, "tg:pause")

    refused = [e for e in logs if e["event"] == "command.admin_required"]
    assert [(e["source"], e["reason"]) for e in refused] == [
        ("callback", "not_admin"),
        ("command", "not_admin"),
    ]
    for entry in refused:
        assert entry["log_level"] == "warning"
        assert entry["command"] == "config"
        assert entry["chat_id"] == GROUP
        assert entry["sender_id"] == MEMBER


@pytest.mark.anyio
async def test_a_backend_without_a_gate_is_untouched(monkeypatch):
    class _Plain:
        id = "plain"
        description = "x"
        seen = 0

        async def handle(self, ctx):
            self.seen += 1
            return

    backend = _Plain()
    monkeypatch.setattr(dispatch_mod, "get_command", lambda *a, **kw: backend)
    cfg, _transport, bot = _cfg(status="member")
    query = TelegramCallbackQuery(
        transport="telegram",
        chat_id=GROUP,
        message_id=1,
        callback_query_id="cb",
        data="plain:x",
        sender_id=MEMBER,
    )
    await _dispatch_callback(
        cfg, query, "plain", "x", None, {}, AsyncMock(), None, False, None, "cb"
    )
    assert backend.seen == 1
    bot.get_chat_member.assert_not_awaited()


@pytest.mark.anyio
async def test_a_gate_hook_that_raises_fails_closed(monkeypatch):
    spy = _Spy(monkeypatch)

    def _boom(_self, _args):
        raise RuntimeError("boom")

    monkeypatch.setattr(config_mod.ConfigCommand, "admin_gate", _boom)
    cfg, transport, bot = _cfg(status="administrator")

    await _tap(cfg, "pm:on", sender_id=ADMIN)
    await _type(cfg, "pm:on", sender_id=ADMIN)

    assert spy.calls == []
    assert len(bot.callback_calls) == 1
    assert bot.callback_calls[0]["text"]
    assert len(transport.send_calls) == 1


# --- End to end through the real handler: nothing changes when refused -----


@pytest.mark.anyio
async def test_refused_change_leaves_the_setting_alone_end_to_end():
    cfg, transport, bot = _cfg(status="member")

    await _tap(cfg, "vb:on")
    await _type(cfg, "vb:on")

    assert _VERBOSE_OVERRIDES == {}
    assert [c["text"] for c in bot.callback_calls] == [DENIED]
    assert _sent_texts(transport) == [DENIED]


@pytest.mark.anyio
async def test_admin_change_applies_and_logs_the_sender_end_to_end():
    cfg, _transport, bot = _cfg(status="administrator")

    with capture_logs() as logs:
        await _tap(cfg, "vb:on", sender_id=ADMIN)

    assert _VERBOSE_OVERRIDES == {GROUP: "verbose"}
    assert bot.callback_calls[0]["text"] == "Verbose: on"
    entry = next(e for e in logs if e["event"] == "config.verbose.set")
    assert entry["chat_id"] == GROUP
    assert entry["sender_id"] == ADMIN


@pytest.mark.anyio
async def test_non_admin_can_still_open_pages_end_to_end():
    cfg, transport, _bot = _cfg(status="member")

    await _type(cfg, "")
    await _type(cfg, "vb")

    assert len(transport.send_calls) == 2
    assert all(DENIED not in text for text in _sent_texts(transport))


def test_every_settings_change_log_line_carries_the_sender_id():
    """Each INFO ``config.*`` change line in a page handler names the sender."""
    checked = 0
    for page, func in _page_functions().items():
        for node in ast.walk(func):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logger"
            ):
                continue
            event = node.args[0].value  # type: ignore[attr-defined]
            assert event.startswith("config."), (page, event)
            keywords = {kw.arg for kw in node.keywords}
            assert "sender_id" in keywords, (page, event)
            checked += 1
    assert checked >= 35
