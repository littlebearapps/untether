"""Command backend for inline settings menu via /config."""

from __future__ import annotations

from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger
from ...transport import RenderedMessage

logger = get_logger(__name__)


def _is_callback(ctx: CommandContext) -> bool:
    """Detect if this invocation is a callback (edit in-place) vs text command."""
    return ctx.text.startswith("config:")


async def _respond(
    ctx: CommandContext,
    text: str,
    buttons: list[list[dict[str, str]]],
) -> None:
    """Send a new message or edit the existing one with inline keyboard."""
    msg = RenderedMessage(
        text=text,
        extra={
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": buttons},
        },
    )
    if _is_callback(ctx):
        await ctx.executor.edit(ctx.message, msg)
    else:
        await ctx.executor.send(msg, reply_to=ctx.message, notify=True)


def _check(label: str, *, active: bool) -> str:
    """Add checkmark prefix if active."""
    return f"✓ {label}" if active else label


# ---------------------------------------------------------------------------
# Home page
# ---------------------------------------------------------------------------


async def _page_home(ctx: CommandContext) -> None:
    from ..chat_prefs import ChatPrefsStore, resolve_prefs_path
    from .verbose import get_verbosity_override

    chat_id = ctx.message.channel_id
    config_path = ctx.config_path

    pm_label = "—"
    engine_label = ctx.runtime.default_engine
    trigger_label = "all"

    if config_path is not None:
        prefs = ChatPrefsStore(resolve_prefs_path(config_path))
        override = await prefs.get_engine_override(chat_id, "claude")
        pm = override.permission_mode if override else None
        if pm == "plan":
            pm_label = "on"
        elif pm == "auto":
            pm_label = "auto"
        elif pm is not None:
            pm_label = "off"
        else:
            pm_label = "default"

        eng = await prefs.get_default_engine(chat_id)
        engine_label = eng if eng else f"{ctx.runtime.default_engine} (global)"

        trig = await prefs.get_trigger_mode(chat_id)
        trigger_label = trig or "all"

    verbose = get_verbosity_override(chat_id)
    if verbose == "verbose":
        verbose_label = "on"
    elif verbose == "compact":
        verbose_label = "off"
    else:
        verbose_label = "default"

    lines = [
        "<b>⚙️ Settings</b>",
        "",
        f"Plan mode: <b>{pm_label}</b>",
        f"Verbose: <b>{verbose_label}</b>",
        f"Engine: <b>{engine_label}</b>",
        f"Trigger: <b>{trigger_label}</b>",
    ]

    buttons = [
        [
            {"text": "Plan mode", "callback_data": "config:pm"},
            {"text": "Verbose", "callback_data": "config:vb"},
        ],
        [
            {"text": "Engine", "callback_data": "config:ag"},
            {"text": "Trigger", "callback_data": "config:tr"},
        ],
    ]

    await _respond(ctx, "\n".join(lines), buttons)


# ---------------------------------------------------------------------------
# Plan mode
# ---------------------------------------------------------------------------

_PM_MODES: dict[str, str] = {"on": "plan", "auto": "auto", "off": "acceptEdits"}


async def _page_planmode(ctx: CommandContext, action: str | None = None) -> None:
    from ..chat_prefs import ChatPrefsStore, resolve_prefs_path
    from ..engine_overrides import EngineOverrides

    config_path = ctx.config_path
    if config_path is None:
        await _respond(
            ctx,
            "<b>⚙️ Plan mode</b>\n\nUnavailable (no config path).",
            [[{"text": "← Back", "callback_data": "config:home"}]],
        )
        return

    prefs = ChatPrefsStore(resolve_prefs_path(config_path))
    chat_id = ctx.message.channel_id
    engine = "claude"

    if action in _PM_MODES:
        current = await prefs.get_engine_override(chat_id, engine)
        updated = EngineOverrides(
            model=current.model if current else None,
            reasoning=current.reasoning if current else None,
            permission_mode=_PM_MODES[action],
        )
        await prefs.set_engine_override(chat_id, engine, updated)
        logger.info("config.planmode.set", chat_id=chat_id, mode=action)
    elif action == "clr":
        current = await prefs.get_engine_override(chat_id, engine)
        updated = EngineOverrides(
            model=current.model if current else None,
            reasoning=current.reasoning if current else None,
            permission_mode=None,
        )
        await prefs.set_engine_override(chat_id, engine, updated)
        logger.info("config.planmode.cleared", chat_id=chat_id)

    override = await prefs.get_engine_override(chat_id, engine)
    pm = override.permission_mode if override else None
    if pm == "plan":
        current_label = "on"
    elif pm == "auto":
        current_label = "auto"
    elif pm is not None:
        current_label = "off"
    else:
        current_label = "default"

    lines = [
        "<b>⚙️ Plan mode</b>",
        "",
        "Controls Claude Code permission prompt behaviour.",
        "• <b>off</b> — no tool approval needed",
        "• <b>on</b> — approve every tool call",
        "• <b>auto</b> — approve, auto-accept ExitPlanMode",
        "",
        f"Current: <b>{current_label}</b>",
    ]

    buttons = [
        [
            {
                "text": _check("Off", active=current_label == "off"),
                "callback_data": "config:pm:off",
            },
            {
                "text": _check("On", active=current_label == "on"),
                "callback_data": "config:pm:on",
            },
            {
                "text": _check("Auto", active=current_label == "auto"),
                "callback_data": "config:pm:auto",
            },
        ],
        [
            {"text": "Clear override", "callback_data": "config:pm:clr"},
            {"text": "← Back", "callback_data": "config:home"},
        ],
    ]

    await _respond(ctx, "\n".join(lines), buttons)


# ---------------------------------------------------------------------------
# Verbose
# ---------------------------------------------------------------------------


async def _page_verbose(ctx: CommandContext, action: str | None = None) -> None:
    from .verbose import _VERBOSE_OVERRIDES, get_verbosity_override

    chat_id = ctx.message.channel_id

    if action == "on":
        _VERBOSE_OVERRIDES[chat_id] = "verbose"
        logger.info("config.verbose.set", chat_id=chat_id, verbosity="verbose")
    elif action == "off":
        _VERBOSE_OVERRIDES[chat_id] = "compact"
        logger.info("config.verbose.set", chat_id=chat_id, verbosity="compact")
    elif action == "clr":
        _VERBOSE_OVERRIDES.pop(chat_id, None)
        logger.info("config.verbose.cleared", chat_id=chat_id)

    current = get_verbosity_override(chat_id)
    if current == "verbose":
        current_label = "on"
    elif current == "compact":
        current_label = "off"
    else:
        current_label = "default"

    lines = [
        "<b>⚙️ Verbose progress</b>",
        "",
        "Controls detail level in progress messages.",
        "• <b>on</b> — show file paths, commands, patterns",
        "• <b>off</b> — compact action names only",
        "",
        f"Current: <b>{current_label}</b>",
    ]

    buttons = [
        [
            {
                "text": _check("Off", active=current_label == "off"),
                "callback_data": "config:vb:off",
            },
            {
                "text": _check("On", active=current_label == "on"),
                "callback_data": "config:vb:on",
            },
        ],
        [
            {"text": "Clear override", "callback_data": "config:vb:clr"},
            {"text": "← Back", "callback_data": "config:home"},
        ],
    ]

    await _respond(ctx, "\n".join(lines), buttons)


# ---------------------------------------------------------------------------
# Default engine
# ---------------------------------------------------------------------------


async def _page_engine(ctx: CommandContext, action: str | None = None) -> None:
    from ..chat_prefs import ChatPrefsStore, resolve_prefs_path

    config_path = ctx.config_path
    if config_path is None:
        await _respond(
            ctx,
            "<b>⚙️ Default engine</b>\n\nUnavailable (no config path).",
            [[{"text": "← Back", "callback_data": "config:home"}]],
        )
        return

    prefs = ChatPrefsStore(resolve_prefs_path(config_path))
    chat_id = ctx.message.channel_id
    available = list(ctx.runtime.engine_ids)

    if action == "clr":
        await prefs.clear_default_engine(chat_id)
        logger.info("config.engine.cleared", chat_id=chat_id)
    elif action and action in available:
        await prefs.set_default_engine(chat_id, action)
        logger.info("config.engine.set", chat_id=chat_id, engine=action)

    current = await prefs.get_default_engine(chat_id)
    global_default = ctx.runtime.default_engine
    current_label = current if current else f"{global_default} (global default)"

    lines = [
        "<b>⚙️ Default engine</b>",
        "",
        "Sets the default engine for new messages in this chat.",
        f"Global default: <b>{global_default}</b>",
        "",
        f"Current: <b>{current_label}</b>",
    ]

    engine_buttons = [
        {
            "text": _check(eid, active=current == eid),
            "callback_data": f"config:ag:{eid}",
        }
        for eid in available
    ]

    buttons: list[list[dict[str, str]]] = [
        engine_buttons[i : i + 2] for i in range(0, len(engine_buttons), 2)
    ]

    buttons.append(
        [
            {"text": "Clear override", "callback_data": "config:ag:clr"},
            {"text": "← Back", "callback_data": "config:home"},
        ]
    )

    await _respond(ctx, "\n".join(lines), buttons)


# ---------------------------------------------------------------------------
# Trigger mode
# ---------------------------------------------------------------------------


async def _page_trigger(ctx: CommandContext, action: str | None = None) -> None:
    from ..chat_prefs import ChatPrefsStore, resolve_prefs_path

    config_path = ctx.config_path
    if config_path is None:
        await _respond(
            ctx,
            "<b>⚙️ Trigger mode</b>\n\nUnavailable (no config path).",
            [[{"text": "← Back", "callback_data": "config:home"}]],
        )
        return

    prefs = ChatPrefsStore(resolve_prefs_path(config_path))
    chat_id = ctx.message.channel_id

    if action == "all":
        await prefs.clear_trigger_mode(chat_id)
        logger.info("config.trigger.set", chat_id=chat_id, mode="all")
    elif action == "men":
        await prefs.set_trigger_mode(chat_id, "mentions")
        logger.info("config.trigger.set", chat_id=chat_id, mode="mentions")
    elif action == "clr":
        await prefs.clear_trigger_mode(chat_id)
        logger.info("config.trigger.cleared", chat_id=chat_id)

    current = await prefs.get_trigger_mode(chat_id)
    current_label = current or "all"

    lines = [
        "<b>⚙️ Trigger mode</b>",
        "",
        "Controls how the bot responds in group chats.",
        "• <b>all</b> — respond to every message",
        "• <b>mentions</b> — only respond when @mentioned",
        "",
        f"Current: <b>{current_label}</b>",
    ]

    buttons = [
        [
            {
                "text": _check("All", active=current_label == "all"),
                "callback_data": "config:tr:all",
            },
            {
                "text": _check("Mentions", active=current_label == "mentions"),
                "callback_data": "config:tr:men",
            },
        ],
        [
            {"text": "Clear override", "callback_data": "config:tr:clr"},
            {"text": "← Back", "callback_data": "config:home"},
        ],
    ]

    await _respond(ctx, "\n".join(lines), buttons)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

_PAGES: dict[str, object] = {
    "pm": _page_planmode,
    "vb": _page_verbose,
    "ag": _page_engine,
    "tr": _page_trigger,
}


class ConfigCommand:
    """Inline settings menu with navigable sub-pages."""

    id = "config"
    description = "Interactive settings menu"
    answer_early = True

    def early_answer_toast(self, args_text: str) -> str | None:
        """Return None for silent feedback — the in-place edit is the response."""
        return None

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        args = ctx.args_text.strip()

        if not args or args == "home":
            await _page_home(ctx)
            return None

        parts = args.split(":", 1)
        page = parts[0]
        action = parts[1] if len(parts) > 1 else None

        handler = _PAGES.get(page)
        if handler is None:
            await _page_home(ctx)
            return None

        await handler(ctx, action)
        return None


BACKEND: CommandBackend = ConfigCommand()
