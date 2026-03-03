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
    from ..engine_overrides import (
        ASK_QUESTIONS_SUPPORTED_ENGINES,
        DIFF_PREVIEW_SUPPORTED_ENGINES,
        supports_reasoning,
    )
    from .verbose import get_verbosity_override

    chat_id = ctx.message.channel_id
    config_path = ctx.config_path

    pm_label = "—"
    engine_label = ctx.runtime.default_engine
    current_engine = ctx.runtime.default_engine
    trigger_label = "all"
    model_label = "default"
    reasoning_label = "default"
    aq_label = "default"
    dp_label = "default"

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
        current_engine = eng if eng else ctx.runtime.default_engine
        engine_label = eng if eng else f"{ctx.runtime.default_engine} (global)"

        trig = await prefs.get_trigger_mode(chat_id)
        trigger_label = trig or "all"

        # Model override for current engine
        engine_override = await prefs.get_engine_override(chat_id, current_engine)
        if engine_override and engine_override.model:
            model_label = engine_override.model

        # Reasoning override for current engine
        if engine_override and engine_override.reasoning:
            reasoning_label = engine_override.reasoning

        # Ask questions override for current engine
        if engine_override and engine_override.ask_questions is not None:
            aq_label = "on" if engine_override.ask_questions else "off"

        # Diff preview override for current engine
        if engine_override and engine_override.diff_preview is not None:
            dp_label = "on" if engine_override.diff_preview else "off"

    verbose = get_verbosity_override(chat_id)
    if verbose == "verbose":
        verbose_label = "on"
    elif verbose == "compact":
        verbose_label = "off"
    else:
        verbose_label = "default"

    show_plan_mode = current_engine == "claude"
    show_reasoning = supports_reasoning(current_engine)
    show_ask_questions = current_engine in ASK_QUESTIONS_SUPPORTED_ENGINES
    show_diff_preview = current_engine in DIFF_PREVIEW_SUPPORTED_ENGINES

    lines = [
        "<b>⚙️ Settings</b>",
        "",
    ]
    if show_plan_mode:
        lines.append(f"Plan mode: <b>{pm_label}</b>")
    if show_ask_questions:
        lines.append(f"Ask mode: <b>{aq_label}</b>")
    if show_diff_preview:
        lines.append(f"Diff preview: <b>{dp_label}</b>")
    lines.extend(
        [
            f"Verbose: <b>{verbose_label}</b>",
            f"Engine: <b>{engine_label}</b>",
            f"Model: <b>{model_label}</b>",
            f"Trigger: <b>{trigger_label}</b>",
        ]
    )
    if show_reasoning:
        lines.append(f"Reasoning: <b>{reasoning_label}</b>")

    buttons: list[list[dict[str, str]]] = []

    # Row 1
    if show_plan_mode:
        buttons.append(
            [
                {"text": "Plan mode", "callback_data": "config:pm"},
                {"text": "Ask mode", "callback_data": "config:aq"},
            ]
        )
    else:
        buttons.append(
            [
                {"text": "Verbose", "callback_data": "config:vb"},
                {"text": "Model", "callback_data": "config:md"},
            ]
        )

    # Row 2
    if show_plan_mode:
        row2 = []
        if show_diff_preview:
            row2.append({"text": "Diff preview", "callback_data": "config:dp"})
        row2.append({"text": "Verbose", "callback_data": "config:vb"})
        buttons.append(row2)
    elif show_reasoning:
        buttons.append(
            [
                {"text": "Engine", "callback_data": "config:ag"},
                {"text": "Reasoning", "callback_data": "config:rs"},
            ]
        )
    else:
        buttons.append(
            [
                {"text": "Engine", "callback_data": "config:ag"},
                {"text": "Trigger", "callback_data": "config:tr"},
            ]
        )

    # Row 3
    if show_plan_mode:
        buttons.append(
            [
                {"text": "Model", "callback_data": "config:md"},
                {"text": "Engine", "callback_data": "config:ag"},
            ]
        )
    elif show_reasoning:
        buttons.append(
            [
                {"text": "Trigger", "callback_data": "config:tr"},
            ]
        )

    # Row 4
    if show_plan_mode:
        buttons.append(
            [
                {"text": "Trigger", "callback_data": "config:tr"},
            ]
        )
    elif show_reasoning:
        pass  # already handled

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

    # Plan mode is Claude-only — guard against non-Claude engines
    eng = await prefs.get_default_engine(chat_id)
    current_engine = eng if eng else ctx.runtime.default_engine
    if current_engine != "claude":
        await _respond(
            ctx,
            "<b>⚙️ Plan mode</b>\n\nOnly available for Claude Code.",
            [[{"text": "← Back", "callback_data": "config:home"}]],
        )
        return

    engine = "claude"

    if action in _PM_MODES:
        current = await prefs.get_engine_override(chat_id, engine)
        updated = EngineOverrides(
            model=current.model if current else None,
            reasoning=current.reasoning if current else None,
            permission_mode=_PM_MODES[action],
            ask_questions=current.ask_questions if current else None,
            diff_preview=current.diff_preview if current else None,
        )
        await prefs.set_engine_override(chat_id, engine, updated)
        logger.info("config.planmode.set", chat_id=chat_id, mode=action)
        await _page_home(ctx)
        return
    elif action == "clr":
        current = await prefs.get_engine_override(chat_id, engine)
        updated = EngineOverrides(
            model=current.model if current else None,
            reasoning=current.reasoning if current else None,
            permission_mode=None,
            ask_questions=current.ask_questions if current else None,
            diff_preview=current.diff_preview if current else None,
        )
        await prefs.set_engine_override(chat_id, engine, updated)
        logger.info("config.planmode.cleared", chat_id=chat_id)
        await _page_home(ctx)
        return

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
        await _page_home(ctx)
        return
    elif action == "off":
        _VERBOSE_OVERRIDES[chat_id] = "compact"
        logger.info("config.verbose.set", chat_id=chat_id, verbosity="compact")
        await _page_home(ctx)
        return
    elif action == "clr":
        _VERBOSE_OVERRIDES.pop(chat_id, None)
        logger.info("config.verbose.cleared", chat_id=chat_id)
        await _page_home(ctx)
        return

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
        await _page_home(ctx)
        return
    elif action and action in available:
        await prefs.set_default_engine(chat_id, action)
        logger.info("config.engine.set", chat_id=chat_id, engine=action)
        await _page_home(ctx)
        return

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
        await _page_home(ctx)
        return
    elif action == "men":
        await prefs.set_trigger_mode(chat_id, "mentions")
        logger.info("config.trigger.set", chat_id=chat_id, mode="mentions")
        await _page_home(ctx)
        return
    elif action == "clr":
        await prefs.clear_trigger_mode(chat_id)
        logger.info("config.trigger.cleared", chat_id=chat_id)
        await _page_home(ctx)
        return

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
# Model
# ---------------------------------------------------------------------------


async def _page_model(ctx: CommandContext, action: str | None = None) -> None:
    from ..chat_prefs import ChatPrefsStore, resolve_prefs_path
    from ..engine_overrides import EngineOverrides

    config_path = ctx.config_path
    if config_path is None:
        await _respond(
            ctx,
            "<b>⚙️ Model</b>\n\nUnavailable (no config path).",
            [[{"text": "← Back", "callback_data": "config:home"}]],
        )
        return

    prefs = ChatPrefsStore(resolve_prefs_path(config_path))
    chat_id = ctx.message.channel_id

    # Resolve current engine
    eng = await prefs.get_default_engine(chat_id)
    current_engine = eng if eng else ctx.runtime.default_engine

    if action == "clr":
        current = await prefs.get_engine_override(chat_id, current_engine)
        updated = EngineOverrides(
            model=None,
            reasoning=current.reasoning if current else None,
            permission_mode=current.permission_mode if current else None,
            ask_questions=current.ask_questions if current else None,
            diff_preview=current.diff_preview if current else None,
        )
        await prefs.set_engine_override(chat_id, current_engine, updated)
        logger.info("config.model.cleared", chat_id=chat_id, engine=current_engine)
        await _page_home(ctx)
        return

    override = await prefs.get_engine_override(chat_id, current_engine)
    model = override.model if override else None
    current_label = model or "default"

    lines = [
        "<b>⚙️ Model</b>",
        "",
        "Per-engine model override for this chat.",
        f"Engine: <b>{current_engine}</b>",
        f"Current: <b>{current_label}</b>",
        "",
        "Use <code>/model set &lt;name&gt;</code> to set a specific model.",
    ]

    buttons = [
        [
            {"text": "Clear override", "callback_data": "config:md:clr"},
            {"text": "← Back", "callback_data": "config:home"},
        ],
    ]

    await _respond(ctx, "\n".join(lines), buttons)


# ---------------------------------------------------------------------------
# Reasoning
# ---------------------------------------------------------------------------

_RS_ACTIONS: dict[str, str] = {
    "min": "minimal",
    "low": "low",
    "med": "medium",
    "hi": "high",
    "xhi": "xhigh",
}

_RS_LABELS: dict[str, str] = {v: k for k, v in _RS_ACTIONS.items()}


async def _page_reasoning(ctx: CommandContext, action: str | None = None) -> None:
    from ..chat_prefs import ChatPrefsStore, resolve_prefs_path
    from ..engine_overrides import EngineOverrides, supports_reasoning

    config_path = ctx.config_path
    if config_path is None:
        await _respond(
            ctx,
            "<b>⚙️ Reasoning</b>\n\nUnavailable (no config path).",
            [[{"text": "← Back", "callback_data": "config:home"}]],
        )
        return

    prefs = ChatPrefsStore(resolve_prefs_path(config_path))
    chat_id = ctx.message.channel_id

    # Reasoning is engine-specific — guard against unsupported engines
    eng = await prefs.get_default_engine(chat_id)
    current_engine = eng if eng else ctx.runtime.default_engine
    if not supports_reasoning(current_engine):
        await _respond(
            ctx,
            "<b>⚙️ Reasoning</b>\n\nOnly available for engines that support reasoning levels.",
            [[{"text": "← Back", "callback_data": "config:home"}]],
        )
        return

    if action in _RS_ACTIONS:
        level = _RS_ACTIONS[action]
        current = await prefs.get_engine_override(chat_id, current_engine)
        updated = EngineOverrides(
            model=current.model if current else None,
            reasoning=level,
            permission_mode=current.permission_mode if current else None,
            ask_questions=current.ask_questions if current else None,
            diff_preview=current.diff_preview if current else None,
        )
        await prefs.set_engine_override(chat_id, current_engine, updated)
        logger.info(
            "config.reasoning.set",
            chat_id=chat_id,
            engine=current_engine,
            level=level,
        )
        await _page_home(ctx)
        return
    elif action == "clr":
        current = await prefs.get_engine_override(chat_id, current_engine)
        updated = EngineOverrides(
            model=current.model if current else None,
            reasoning=None,
            permission_mode=current.permission_mode if current else None,
            ask_questions=current.ask_questions if current else None,
            diff_preview=current.diff_preview if current else None,
        )
        await prefs.set_engine_override(chat_id, current_engine, updated)
        logger.info("config.reasoning.cleared", chat_id=chat_id, engine=current_engine)
        await _page_home(ctx)
        return

    override = await prefs.get_engine_override(chat_id, current_engine)
    reasoning = override.reasoning if override else None
    current_label = reasoning or "default"

    lines = [
        "<b>⚙️ Reasoning</b>",
        "",
        "Controls reasoning effort level.",
        "• <b>minimal</b> — fastest, least reasoning",
        "• <b>low</b> / <b>medium</b> / <b>high</b>",
        "• <b>xhigh</b> — most thorough reasoning",
        "",
        f"Engine: <b>{current_engine}</b>",
        f"Current: <b>{current_label}</b>",
    ]

    buttons = [
        [
            {
                "text": _check("Minimal", active=reasoning == "minimal"),
                "callback_data": "config:rs:min",
            },
            {
                "text": _check("Low", active=reasoning == "low"),
                "callback_data": "config:rs:low",
            },
            {
                "text": _check("Medium", active=reasoning == "medium"),
                "callback_data": "config:rs:med",
            },
        ],
        [
            {
                "text": _check("High", active=reasoning == "high"),
                "callback_data": "config:rs:hi",
            },
            {
                "text": _check("Xhigh", active=reasoning == "xhigh"),
                "callback_data": "config:rs:xhi",
            },
        ],
        [
            {"text": "Clear override", "callback_data": "config:rs:clr"},
            {"text": "← Back", "callback_data": "config:home"},
        ],
    ]

    await _respond(ctx, "\n".join(lines), buttons)


# ---------------------------------------------------------------------------
# Ask questions
# ---------------------------------------------------------------------------


async def _page_ask_questions(ctx: CommandContext, action: str | None = None) -> None:
    from ..chat_prefs import ChatPrefsStore, resolve_prefs_path
    from ..engine_overrides import ASK_QUESTIONS_SUPPORTED_ENGINES, EngineOverrides

    config_path = ctx.config_path
    if config_path is None:
        await _respond(
            ctx,
            "<b>⚙️ Ask questions</b>\n\nUnavailable (no config path).",
            [[{"text": "← Back", "callback_data": "config:home"}]],
        )
        return

    prefs = ChatPrefsStore(resolve_prefs_path(config_path))
    chat_id = ctx.message.channel_id

    # Claude-only guard
    eng = await prefs.get_default_engine(chat_id)
    current_engine = eng if eng else ctx.runtime.default_engine
    if current_engine not in ASK_QUESTIONS_SUPPORTED_ENGINES:
        await _respond(
            ctx,
            "<b>⚙️ Ask questions</b>\n\nOnly available for Claude Code.",
            [[{"text": "← Back", "callback_data": "config:home"}]],
        )
        return

    engine = current_engine

    if action == "on":
        current = await prefs.get_engine_override(chat_id, engine)
        updated = EngineOverrides(
            model=current.model if current else None,
            reasoning=current.reasoning if current else None,
            permission_mode=current.permission_mode if current else None,
            ask_questions=True,
            diff_preview=current.diff_preview if current else None,
        )
        await prefs.set_engine_override(chat_id, engine, updated)
        logger.info("config.ask_questions.set", chat_id=chat_id, value=True)
        await _page_home(ctx)
        return
    elif action == "off":
        current = await prefs.get_engine_override(chat_id, engine)
        updated = EngineOverrides(
            model=current.model if current else None,
            reasoning=current.reasoning if current else None,
            permission_mode=current.permission_mode if current else None,
            ask_questions=False,
            diff_preview=current.diff_preview if current else None,
        )
        await prefs.set_engine_override(chat_id, engine, updated)
        logger.info("config.ask_questions.set", chat_id=chat_id, value=False)
        await _page_home(ctx)
        return
    elif action == "clr":
        current = await prefs.get_engine_override(chat_id, engine)
        updated = EngineOverrides(
            model=current.model if current else None,
            reasoning=current.reasoning if current else None,
            permission_mode=current.permission_mode if current else None,
            ask_questions=None,
            diff_preview=current.diff_preview if current else None,
        )
        await prefs.set_engine_override(chat_id, engine, updated)
        logger.info("config.ask_questions.cleared", chat_id=chat_id)
        await _page_home(ctx)
        return

    override = await prefs.get_engine_override(chat_id, engine)
    aq = override.ask_questions if override else None
    if aq is True:
        current_label = "on"
    elif aq is False:
        current_label = "off"
    else:
        current_label = "default (on)"

    lines = [
        "<b>⚙️ Ask mode</b>",
        "",
        "When enabled, Claude Code can ask interactive",
        "questions with option buttons instead of guessing.",
        "• <b>on</b> — show questions with option buttons",
        "• <b>off</b> — Claude proceeds with defaults",
        "",
        f"Current: <b>{current_label}</b>",
    ]

    buttons = [
        [
            {
                "text": _check("Off", active=aq is False),
                "callback_data": "config:aq:off",
            },
            {
                "text": _check("On", active=aq is True),
                "callback_data": "config:aq:on",
            },
        ],
        [
            {"text": "Clear override", "callback_data": "config:aq:clr"},
            {"text": "← Back", "callback_data": "config:home"},
        ],
    ]

    await _respond(ctx, "\n".join(lines), buttons)


async def _page_diff_preview(ctx: CommandContext, action: str | None = None) -> None:
    from ..chat_prefs import ChatPrefsStore, resolve_prefs_path
    from ..engine_overrides import DIFF_PREVIEW_SUPPORTED_ENGINES, EngineOverrides

    config_path = ctx.config_path
    if config_path is None:
        await _respond(
            ctx,
            "<b>⚙️ Diff preview</b>\n\nUnavailable (no config path).",
            [[{"text": "← Back", "callback_data": "config:home"}]],
        )
        return

    prefs = ChatPrefsStore(resolve_prefs_path(config_path))
    chat_id = ctx.message.channel_id

    # Claude-only guard
    eng = await prefs.get_default_engine(chat_id)
    current_engine = eng if eng else ctx.runtime.default_engine
    if current_engine not in DIFF_PREVIEW_SUPPORTED_ENGINES:
        await _respond(
            ctx,
            "<b>⚙️ Diff preview</b>\n\nOnly available for Claude Code.",
            [[{"text": "← Back", "callback_data": "config:home"}]],
        )
        return

    engine = current_engine

    if action == "on":
        current = await prefs.get_engine_override(chat_id, engine)
        updated = EngineOverrides(
            model=current.model if current else None,
            reasoning=current.reasoning if current else None,
            permission_mode=current.permission_mode if current else None,
            ask_questions=current.ask_questions if current else None,
            diff_preview=True,
        )
        await prefs.set_engine_override(chat_id, engine, updated)
        logger.info("config.diff_preview.set", chat_id=chat_id, value=True)
        await _page_home(ctx)
        return
    elif action == "off":
        current = await prefs.get_engine_override(chat_id, engine)
        updated = EngineOverrides(
            model=current.model if current else None,
            reasoning=current.reasoning if current else None,
            permission_mode=current.permission_mode if current else None,
            ask_questions=current.ask_questions if current else None,
            diff_preview=False,
        )
        await prefs.set_engine_override(chat_id, engine, updated)
        logger.info("config.diff_preview.set", chat_id=chat_id, value=False)
        await _page_home(ctx)
        return
    elif action == "clr":
        current = await prefs.get_engine_override(chat_id, engine)
        updated = EngineOverrides(
            model=current.model if current else None,
            reasoning=current.reasoning if current else None,
            permission_mode=current.permission_mode if current else None,
            ask_questions=current.ask_questions if current else None,
            diff_preview=None,
        )
        await prefs.set_engine_override(chat_id, engine, updated)
        logger.info("config.diff_preview.cleared", chat_id=chat_id)
        await _page_home(ctx)
        return

    override = await prefs.get_engine_override(chat_id, engine)
    dp = override.diff_preview if override else None
    if dp is True:
        current_label = "on"
    elif dp is False:
        current_label = "off"
    else:
        current_label = "default (on)"

    lines = [
        "<b>⚙️ Diff preview</b>",
        "",
        "Shows compact diffs in tool approval messages.",
        "• <b>on</b> — show Edit/Write diffs and Bash commands",
        "• <b>off</b> — approval buttons only, no preview",
        "",
        f"Current: <b>{current_label}</b>",
    ]

    buttons = [
        [
            {
                "text": _check("Off", active=dp is False),
                "callback_data": "config:dp:off",
            },
            {
                "text": _check("On", active=dp is True),
                "callback_data": "config:dp:on",
            },
        ],
        [
            {"text": "Clear override", "callback_data": "config:dp:clr"},
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
    "md": _page_model,
    "rs": _page_reasoning,
    "aq": _page_ask_questions,
    "dp": _page_diff_preview,
}


class ConfigCommand:
    """Inline settings menu with navigable sub-pages."""

    id = "config"
    description = "Interactive settings menu"
    answer_early = True

    @staticmethod
    def early_answer_toast(args_text: str) -> str | None:
        """Return a confirmation toast for toggle actions, None for navigation."""
        parts = args_text.split(":")
        if len(parts) < 2:
            return None  # Home page navigation
        page = parts[0]
        action = parts[1] if len(parts) > 1 else None
        if action is None:
            return None  # Sub-page navigation only
        _TOAST_LABELS: dict[str, dict[str, str]] = {
            "pm": {
                "on": "Plan mode: on",
                "off": "Plan mode: off",
                "auto": "Plan mode: auto",
                "clr": "Plan mode: cleared",
            },
            "vb": {
                "on": "Verbose: on",
                "off": "Verbose: off",
                "clr": "Verbose: cleared",
            },
            "ag": {"clr": "Engine: cleared"},
            "tr": {
                "all": "Trigger: all",
                "men": "Trigger: mentions",
                "clr": "Trigger: cleared",
            },
            "md": {"clr": "Model: cleared"},
            "rs": {
                "min": "Reasoning: minimal",
                "low": "Reasoning: low",
                "med": "Reasoning: medium",
                "hi": "Reasoning: high",
                "xhi": "Reasoning: xhigh",
                "clr": "Reasoning: cleared",
            },
            "aq": {
                "on": "Ask mode: on",
                "off": "Ask mode: off",
                "clr": "Ask mode: cleared",
            },
            "dp": {
                "on": "Diff preview: on",
                "off": "Diff preview: off",
                "clr": "Diff preview: cleared",
            },
        }
        page_labels = _TOAST_LABELS.get(page, {})
        if action in page_labels:
            return page_labels[action]
        if page == "ag" and action != "clr":
            return f"Engine: {action}"
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
