"""Command backend for inline settings menu via /config."""

from __future__ import annotations

from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger
from ...transport import RenderedMessage

logger = get_logger(__name__)

_DOCS_BASE = "https://littlebearapps.com/tools/untether/how-to/"


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


def _toggle_row(
    label: str,
    *,
    current: bool | None,
    default: bool,
    on_data: str,
    off_data: str,
    clr_data: str,
) -> list[dict[str, str]]:
    """Build a 2-button toggle row: [Label: state checkmark] [Clear]."""
    effective = current if current is not None else default
    if effective:
        toggle_text = f"✓ {label}: on"
        toggle_data = off_data  # clicking toggles OFF
    else:
        toggle_text = f"{label}: off"
        toggle_data = on_data  # clicking toggles ON
    return [
        {"text": toggle_text, "callback_data": toggle_data},
        {"text": "Clear", "callback_data": clr_data},
    ]


async def _resolve_effective_engine(
    ctx: CommandContext,
) -> tuple[str, str]:
    """Resolve effective engine and display label for a chat.

    Resolution order: chat override → project default → global default.

    Returns ``(engine_id, label)`` where *label* is e.g. ``"codex"`` or
    ``"claude (default)"`` (annotated when effective == global default).
    """
    from ..chat_prefs import ChatPrefsStore, resolve_prefs_path

    chat_id = ctx.message.channel_id
    global_default = ctx.runtime.default_engine

    chat_override = None
    if ctx.config_path is not None:
        prefs = ChatPrefsStore(resolve_prefs_path(ctx.config_path))
        chat_override = await prefs.get_default_engine(chat_id)

    if chat_override is not None:
        effective = chat_override
    else:
        project_default = None
        context = ctx.runtime.default_context_for_chat(chat_id)
        if context is not None:
            project_default = ctx.runtime.project_default_engine(context)
        effective = project_default if project_default is not None else global_default

    label = f"{effective} (default)" if effective == global_default else effective
    return effective, label


# ---------------------------------------------------------------------------
# Home page
# ---------------------------------------------------------------------------

_HOME_HINTS: dict[str, dict[str, str]] = {
    "pm": {
        "on": "approve actions",
        "off": "run freely",
        "auto": "auto-approve actions",
        "default": "agent decides",
        "full auto": "all tools approved",
        "safe": "untrusted tools blocked",
        "full access": "all tools approved",
        "edit files": "files ok, no shell",
        "read-only": "write tools blocked",
    },
    "aq": {
        "on": "interactive questions",
        "off": "agent guesses",
    },
    "dp": {
        "on": "show code changes",
        "off": "buttons only",
    },
    "vb": {
        "on": "detailed progress",
        "off": "compact progress",
    },
    "tr": {"all": "respond to everything", "mentions": "@mention only"},
    "md": {"default": "from CLI settings"},
    "rs": {"default": "from CLI settings"},
}

# Engine-specific default model hints shown when no model override is set.
_ENGINE_MODEL_HINTS: dict[str, str] = {
    "claude": "from CLI settings",
    "codex": "codex-mini-latest",
    "gemini": "auto (routes Flash ↔ Pro)",
    "amp": "smart mode (Opus 4.6)",
    "opencode": "provider/model (e.g. openai/gpt-4o)",
    "pi": "from provider config",
}

# Map "default" to effective value for settings with known defaults.
_DEFAULT_EFFECTIVE: dict[str, str] = {
    "aq": "on",
    "dp": "off",
    "vb": "off",
}


def _resolve_default(setting: str, value: str) -> str:
    """Replace 'default' with the effective value when known."""
    if value == "default" and setting in _DEFAULT_EFFECTIVE:
        return _DEFAULT_EFFECTIVE[setting]
    return value


def _home_hint(setting: str, value: str) -> str:
    """Return a micro-description suffix for the home page, or empty string."""
    resolved = _resolve_default(setting, value)
    hint = _HOME_HINTS.get(setting, {}).get(resolved, "")
    return f"  · {hint}" if hint else ""


async def _page_home(ctx: CommandContext) -> None:
    from ..chat_prefs import ChatPrefsStore, resolve_prefs_path
    from ..engine_overrides import (
        API_COST_SUPPORTED_ENGINES,
        ASK_QUESTIONS_SUPPORTED_ENGINES,
        DIFF_PREVIEW_SUPPORTED_ENGINES,
        PERMISSION_MODE_SUPPORTED_ENGINES,
        SUBSCRIPTION_USAGE_SUPPORTED_ENGINES,
        supports_reasoning,
    )
    from .verbose import get_verbosity_override

    chat_id = ctx.message.channel_id
    config_path = ctx.config_path

    current_engine, engine_label = await _resolve_effective_engine(ctx)

    pm_label = "—"
    trigger_label = "all"
    model_label = "default"
    reasoning_label = "default"
    aq_label = "default"
    dp_label = "default"
    cu_label = "default"
    _cu_ac: bool | None = None
    _cu_su: bool | None = None
    engine_override = None

    if config_path is not None:
        prefs = ChatPrefsStore(resolve_prefs_path(config_path))
        engine_override = await prefs.get_engine_override(chat_id, current_engine)
        pm = engine_override.permission_mode if engine_override else None
        if current_engine == "claude":
            if pm == "plan":
                pm_label = "on"
            elif pm == "auto":
                pm_label = "auto"
            elif pm is not None:
                pm_label = "off"
            else:
                pm_label = "default"
        elif current_engine == "codex":
            pm_label = "safe" if pm == "safe" else "full auto"
        elif current_engine == "gemini":
            if pm == "yolo":
                pm_label = "full access"
            elif pm == "auto_edit":
                pm_label = "edit files"
            else:
                pm_label = "read-only"

        trig = await prefs.get_trigger_mode(chat_id)
        trigger_label = trig or "all"

        # Model override for current engine
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

        # Cost & usage overrides — resolution deferred until has_api_cost is known
        _cu_ac = engine_override.show_api_cost if engine_override else None
        _cu_su = engine_override.show_subscription_usage if engine_override else None

    verbose = get_verbosity_override(chat_id)
    if verbose == "verbose":
        verbose_label = "on"
    elif verbose == "compact":
        verbose_label = "off"
    else:
        verbose_label = "default"

    show_plan_mode = current_engine in PERMISSION_MODE_SUPPORTED_ENGINES
    show_reasoning = supports_reasoning(current_engine)
    show_ask_questions = current_engine in ASK_QUESTIONS_SUPPORTED_ENGINES
    show_diff_preview = current_engine in DIFF_PREVIEW_SUPPORTED_ENGINES
    show_cost_usage = (
        current_engine in API_COST_SUPPORTED_ENGINES
        or current_engine in SUBSCRIPTION_USAGE_SUPPORTED_ENGINES
    )
    has_api_cost = current_engine in API_COST_SUPPORTED_ENGINES
    has_sub_usage = current_engine in SUBSCRIPTION_USAGE_SUPPORTED_ENGINES

    # Resolve cost & usage label to effective values
    if show_cost_usage:
        from ...settings import FooterSettings, load_settings_if_exists as _load_cu_cfg

        try:
            _cu_result = _load_cu_cfg()
            _footer_cfg = _cu_result[0].footer if _cu_result else FooterSettings()
        except (OSError, ValueError, KeyError):
            _footer_cfg = FooterSettings()

        _eff_ac = _cu_ac if _cu_ac is not None else _footer_cfg.show_api_cost
        _eff_su = _cu_su if _cu_su is not None else _footer_cfg.show_subscription_usage
        parts: list[str] = []
        if has_api_cost:
            parts.append(f"cost {'on' if _eff_ac else 'off'}")
        if has_sub_usage:
            parts.append(f"sub {'on' if _eff_su else 'off'}")
        if parts:
            cu_label = ", ".join(parts)

    lines = [
        "\N{DOG} <b>Untether settings</b>",
        "",
    ]

    # Resolve "default" to effective values where known.
    aq_display = _resolve_default("aq", aq_label)
    dp_display = _resolve_default("dp", dp_label)
    vb_display = _resolve_default("vb", verbose_label)

    # --- Agent controls ---
    if show_plan_mode:
        if current_engine == "claude":
            lines.append("<b>Agent controls</b> <i>(Claude Code)</i>")
            lines.append(f"Plan mode: <b>{pm_label}</b>{_home_hint('pm', pm_label)}")
            if show_ask_questions:
                lines.append(
                    f"Ask mode: <b>{aq_display}</b>{_home_hint('aq', aq_label)}"
                )
            if show_diff_preview:
                lines.append(
                    f"Diff preview: <b>{dp_display}</b>{_home_hint('dp', dp_label)}"
                )
        elif current_engine == "codex":
            lines.append("<b>Agent controls</b> <i>(Codex CLI)</i>")
            lines.append(
                f"Approval policy: <b>{pm_label}</b>{_home_hint('pm', pm_label)}"
            )
        elif current_engine == "gemini":
            lines.append("<b>Agent controls</b> <i>(Gemini CLI)</i>")
            lines.append(
                f"Approval mode: <b>{pm_label}</b>{_home_hint('pm', pm_label)}"
            )
        lines.append("")

    # --- Resume line ---
    _resume_default = True
    try:
        from ...settings import load_settings_if_exists as _load_rl_cfg

        _rl_result = _load_rl_cfg()
        if _rl_result is not None:
            _resume_default = _rl_result[0].transports.telegram.show_resume_line
    except (OSError, ValueError, KeyError):
        pass

    if engine_override and engine_override.show_resume_line is not None:
        rl_label = "on" if engine_override.show_resume_line else "off"
    else:
        rl_label = "on" if _resume_default else "off"

    # --- Display ---
    lines.append("<b>Display</b>")
    if show_cost_usage:
        lines.append(f"Cost & usage: <b>{cu_label}</b>")
    lines.append(f"Verbose: <b>{vb_display}</b>{_home_hint('vb', verbose_label)}")
    lines.append(f"Resume line: <b>{rl_label}</b>")
    lines.append("")

    # --- Routing ---
    lines.append("<b>Routing</b>")
    lines.append(f"Engine: <b>{engine_label}</b>")
    model_hint = _home_hint("md", model_label)
    if model_label == "default":
        engine_hint = _ENGINE_MODEL_HINTS.get(current_engine, "from CLI settings")
        model_hint = f"  · {engine_hint}"
    lines.append(f"Model: <b>{model_label}</b>{model_hint}")
    lines.append(f"Trigger: <b>{trigger_label}</b>{_home_hint('tr', trigger_label)}")
    if show_reasoning:
        lines.append(
            f"Reasoning: <b>{reasoning_label}</b>{_home_hint('rs', reasoning_label)}"
        )

    _DOCS_SETTINGS = f"{_DOCS_BASE}inline-settings/"
    _DOCS_TROUBLE = f"{_DOCS_BASE}troubleshooting/"
    lines.append("")
    lines.append(
        f'📖 <a href="{_DOCS_SETTINGS}">Settings guide</a>'
        f' · <a href="{_DOCS_TROUBLE}">Troubleshooting</a>'
    )

    buttons: list[list[dict[str, str]]] = []

    if current_engine == "claude":
        # Claude Code layout
        buttons.append(
            [
                {"text": "📋 Plan mode", "callback_data": "config:pm"},
                {"text": "❓ Ask mode", "callback_data": "config:aq"},
            ]
        )
        buttons.append(
            [
                {"text": "📝 Diff preview", "callback_data": "config:dp"},
                {"text": "🔍 Verbose", "callback_data": "config:vb"},
            ]
        )
        buttons.append(
            [
                {"text": "💰 Cost & usage", "callback_data": "config:cu"},
                {"text": "↩️ Resume line", "callback_data": "config:rl"},
            ]
        )
        buttons.append(
            [
                {"text": "📡 Trigger", "callback_data": "config:tr"},
                {"text": "⚙️ Engine & model", "callback_data": "config:ag"},
            ]
        )
        buttons.append(
            [
                {"text": "🧠 Reasoning", "callback_data": "config:rs"},
                {"text": "ℹ️ About", "callback_data": "config:ab"},
            ]
        )
    elif current_engine == "codex":
        # Codex layout
        row1 = [{"text": "📋 Approval policy", "callback_data": "config:pm"}]
        if show_cost_usage:
            row1.append({"text": "💰 Cost & usage", "callback_data": "config:cu"})
        buttons.append(row1)
        buttons.append(
            [
                {"text": "🔍 Verbose", "callback_data": "config:vb"},
                {"text": "↩️ Resume line", "callback_data": "config:rl"},
            ]
        )
        buttons.append(
            [
                {"text": "📡 Trigger", "callback_data": "config:tr"},
                {"text": "⚙️ Engine & model", "callback_data": "config:ag"},
            ]
        )
        buttons.append(
            [
                {"text": "🧠 Reasoning", "callback_data": "config:rs"},
                {"text": "ℹ️ About", "callback_data": "config:ab"},
            ]
        )
    elif current_engine == "gemini":
        # Gemini layout
        buttons.append(
            [
                {"text": "📋 Approval mode", "callback_data": "config:pm"},
                {"text": "💰 Cost & usage", "callback_data": "config:cu"},
            ]
        )
        buttons.append(
            [
                {"text": "🔍 Verbose", "callback_data": "config:vb"},
                {"text": "↩️ Resume line", "callback_data": "config:rl"},
            ]
        )
        buttons.append(
            [
                {"text": "📡 Trigger", "callback_data": "config:tr"},
                {"text": "⚙️ Engine & model", "callback_data": "config:ag"},
            ]
        )
        buttons.append([{"text": "ℹ️ About", "callback_data": "config:ab"}])
    else:
        # Other engines
        row1 = []
        if show_cost_usage:
            row1.append({"text": "💰 Cost & usage", "callback_data": "config:cu"})
        row1.append({"text": "↩️ Resume line", "callback_data": "config:rl"})
        buttons.append(row1)
        buttons.append(
            [
                {"text": "🔍 Verbose", "callback_data": "config:vb"},
                {"text": "⚙️ Engine & model", "callback_data": "config:ag"},
            ]
        )
        row3 = [{"text": "📡 Trigger", "callback_data": "config:tr"}]
        if show_reasoning:
            row3.append({"text": "🧠 Reasoning", "callback_data": "config:rs"})
        buttons.append(row3)
        buttons.append([{"text": "ℹ️ About", "callback_data": "config:ab"}])

    await _respond(ctx, "\n".join(lines), buttons)


# ---------------------------------------------------------------------------
# Plan mode
# ---------------------------------------------------------------------------

_PM_MODES: dict[str, str] = {"on": "plan", "auto": "auto", "off": "acceptEdits"}

_CODEX_PM_MODES: dict[str, str] = {"fa": "auto", "safe": "safe"}

_GEMINI_AM_MODES: dict[str, str] = {"ya": "yolo", "ae": "auto_edit"}


async def _page_planmode(ctx: CommandContext, action: str | None = None) -> None:
    from ..chat_prefs import ChatPrefsStore, resolve_prefs_path
    from ..engine_overrides import (
        EngineOverrides,
        PERMISSION_MODE_SUPPORTED_ENGINES,
    )

    config_path = ctx.config_path
    if config_path is None:
        await _respond(
            ctx,
            "<b>📋 Permission mode</b>\n\nUnavailable (no config path).",
            [[{"text": "← Back", "callback_data": "config:home"}]],
        )
        return

    prefs = ChatPrefsStore(resolve_prefs_path(config_path))
    chat_id = ctx.message.channel_id

    current_engine, _ = await _resolve_effective_engine(ctx)
    if current_engine not in PERMISSION_MODE_SUPPORTED_ENGINES:
        await _respond(
            ctx,
            (
                "<b>📋 Permission mode</b>\n\n"
                "Only available for Claude Code, Codex, and Gemini CLI."
            ),
            [[{"text": "← Back", "callback_data": "config:home"}]],
        )
        return

    engine = current_engine

    # --- Codex approval policy actions ---
    if engine == "codex" and action in _CODEX_PM_MODES:
        current = await prefs.get_engine_override(chat_id, engine)
        mode_value = _CODEX_PM_MODES[action]
        updated = EngineOverrides(
            model=current.model if current else None,
            reasoning=current.reasoning if current else None,
            permission_mode=mode_value if mode_value != "auto" else None,
            ask_questions=current.ask_questions if current else None,
            diff_preview=current.diff_preview if current else None,
            show_api_cost=current.show_api_cost if current else None,
            show_subscription_usage=current.show_subscription_usage
            if current
            else None,
            show_resume_line=current.show_resume_line if current else None,
            budget_enabled=current.budget_enabled if current else None,
            budget_auto_cancel=current.budget_auto_cancel if current else None,
        )
        await prefs.set_engine_override(chat_id, engine, updated)
        logger.info("config.approval_policy.set", chat_id=chat_id, mode=action)
        await _page_home(ctx)
        return

    # --- Claude plan mode actions ---
    if engine == "claude" and action in _PM_MODES:
        current = await prefs.get_engine_override(chat_id, engine)
        updated = EngineOverrides(
            model=current.model if current else None,
            reasoning=current.reasoning if current else None,
            permission_mode=_PM_MODES[action],
            ask_questions=current.ask_questions if current else None,
            diff_preview=current.diff_preview if current else None,
            show_api_cost=current.show_api_cost if current else None,
            show_subscription_usage=current.show_subscription_usage
            if current
            else None,
            show_resume_line=current.show_resume_line if current else None,
            budget_enabled=current.budget_enabled if current else None,
            budget_auto_cancel=current.budget_auto_cancel if current else None,
        )
        await prefs.set_engine_override(chat_id, engine, updated)
        logger.info("config.planmode.set", chat_id=chat_id, mode=action)
        await _page_home(ctx)
        return

    # --- Gemini approval mode actions ---
    if engine == "gemini" and action in _GEMINI_AM_MODES:
        current = await prefs.get_engine_override(chat_id, engine)
        updated = EngineOverrides(
            model=current.model if current else None,
            reasoning=current.reasoning if current else None,
            permission_mode=_GEMINI_AM_MODES[action],
            ask_questions=current.ask_questions if current else None,
            diff_preview=current.diff_preview if current else None,
            show_api_cost=current.show_api_cost if current else None,
            show_subscription_usage=current.show_subscription_usage
            if current
            else None,
            show_resume_line=current.show_resume_line if current else None,
            budget_enabled=current.budget_enabled if current else None,
            budget_auto_cancel=current.budget_auto_cancel if current else None,
        )
        await prefs.set_engine_override(chat_id, engine, updated)
        logger.info("config.approval_mode.set", chat_id=chat_id, mode=action)
        await _page_home(ctx)
        return

    if engine == "gemini" and action == "ro":
        current = await prefs.get_engine_override(chat_id, engine)
        updated = EngineOverrides(
            model=current.model if current else None,
            reasoning=current.reasoning if current else None,
            permission_mode=None,
            ask_questions=current.ask_questions if current else None,
            diff_preview=current.diff_preview if current else None,
            show_api_cost=current.show_api_cost if current else None,
            show_subscription_usage=current.show_subscription_usage
            if current
            else None,
            show_resume_line=current.show_resume_line if current else None,
            budget_enabled=current.budget_enabled if current else None,
            budget_auto_cancel=current.budget_auto_cancel if current else None,
        )
        await prefs.set_engine_override(chat_id, engine, updated)
        logger.info("config.approval_mode.set", chat_id=chat_id, mode="ro")
        await _page_home(ctx)
        return

    # --- Clear (all engines) ---
    if action == "clr":
        current = await prefs.get_engine_override(chat_id, engine)
        updated = EngineOverrides(
            model=current.model if current else None,
            reasoning=current.reasoning if current else None,
            permission_mode=None,
            ask_questions=current.ask_questions if current else None,
            diff_preview=current.diff_preview if current else None,
            show_api_cost=current.show_api_cost if current else None,
            show_subscription_usage=current.show_subscription_usage
            if current
            else None,
            show_resume_line=current.show_resume_line if current else None,
            budget_enabled=current.budget_enabled if current else None,
            budget_auto_cancel=current.budget_auto_cancel if current else None,
        )
        await prefs.set_engine_override(chat_id, engine, updated)
        logger.info("config.permission_mode.cleared", chat_id=chat_id, engine=engine)
        await _page_home(ctx)
        return

    # --- Display page ---
    override = await prefs.get_engine_override(chat_id, engine)
    pm = override.permission_mode if override else None

    if engine == "claude":
        if pm == "plan":
            current_label = "on"
        elif pm == "auto":
            current_label = "auto"
        elif pm is not None:
            current_label = "off"
        else:
            current_label = "default"

        lines = [
            "<b>📋 Plan mode</b>",
            "",
            "Review and approve each action before it runs.",
            "",
            "• <b>off</b> — run freely, no approval needed",
            "• <b>on</b> — ask before every action (safest)",
            "• <b>auto</b> — approve actions, ask before finalising plans",
            "",
            "ℹ️ <i>Default: uses Claude Code's own permission mode</i>",
            "",
            f"Current: <b>{current_label}</b>",
            "",
            f'📖 <a href="{_DOCS_BASE}plan-mode/">Learn more</a>',
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
            ],
            [
                {
                    "text": _check("Auto", active=current_label == "auto"),
                    "callback_data": "config:pm:auto",
                },
                {"text": "Clear override", "callback_data": "config:pm:clr"},
            ],
            [{"text": "← Back", "callback_data": "config:home"}],
        ]

    elif engine == "codex":
        current_label = "safe" if pm == "safe" else "full auto"

        lines = [
            "<b>📋 Approval policy</b>",
            "",
            "Control which tools Codex can use.",
            "Codex runs non-interactively — approval is set before the run.",
            "",
            "• <b>full auto</b> — all tools approved (default)",
            "• <b>safe</b> — only trusted commands run, untrusted denied",
            "",
            f"Current: <b>{current_label}</b>",
            "",
            f'📖 <a href="{_DOCS_BASE}inline-settings/">Learn more</a>',
        ]

        buttons = [
            [
                {
                    "text": _check("Full auto", active=current_label == "full auto"),
                    "callback_data": "config:pm:fa",
                },
                {
                    "text": _check("Safe", active=current_label == "safe"),
                    "callback_data": "config:pm:safe",
                },
            ],
            [
                {"text": "Clear override", "callback_data": "config:pm:clr"},
                {"text": "← Back", "callback_data": "config:home"},
            ],
        ]

    elif engine == "gemini":
        if pm == "yolo":
            current_label = "full access"
        elif pm == "auto_edit":
            current_label = "edit files"
        else:
            current_label = "read-only"

        lines = [
            "<b>📋 Approval mode</b>",
            "",
            "Control which tools Gemini can use in non-interactive mode.",
            "",
            "• <b>read-only</b> — research only, no modifications (default)",
            "• <b>edit files</b> — file reads/writes OK, shell commands blocked",
            "• <b>full access</b> — all tools approved",
            "",
            f"Current: <b>{current_label}</b>",
            "",
            f'📖 <a href="{_DOCS_BASE}inline-settings/">Learn more</a>',
        ]

        buttons = [
            [
                {
                    "text": _check("Read-only", active=pm not in {"yolo", "auto_edit"}),
                    "callback_data": "config:pm:ro",
                },
                {
                    "text": _check("Edit files", active=pm == "auto_edit"),
                    "callback_data": "config:pm:ae",
                },
            ],
            [
                {
                    "text": _check("Full access", active=pm == "yolo"),
                    "callback_data": "config:pm:ya",
                },
                {"text": "Clear override", "callback_data": "config:pm:clr"},
            ],
            [{"text": "← Back", "callback_data": "config:home"}],
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
        current_label = "off"

    lines = [
        "<b>🔍 Verbose progress</b>",
        "",
        "Choose how much detail to show while the agent is working.",
        "",
        "• <b>on</b> — show file paths, commands, and search patterns",
        "• <b>off</b> — show action names only (default)",
        "",
        f"Current: <b>{current_label}</b>",
        "",
        f'📖 <a href="{_DOCS_BASE}verbose-progress/">Learn more</a>',
    ]

    is_on = current == "verbose"
    buttons = [
        _toggle_row(
            "Verbose",
            current=True if is_on else (False if current == "compact" else None),
            default=False,
            on_data="config:vb:on",
            off_data="config:vb:off",
            clr_data="config:vb:clr",
        ),
        [{"text": "← Back", "callback_data": "config:home"}],
    ]

    await _respond(ctx, "\n".join(lines), buttons)


# ---------------------------------------------------------------------------
# Default engine
# ---------------------------------------------------------------------------


async def _page_engine(ctx: CommandContext, action: str | None = None) -> None:
    from ..chat_prefs import ChatPrefsStore, resolve_prefs_path
    from ..engine_overrides import EngineOverrides

    config_path = ctx.config_path
    if config_path is None:
        await _respond(
            ctx,
            "<b>⚙️ Engine & model</b>\n\nUnavailable (no config path).",
            [[{"text": "← Back", "callback_data": "config:home"}]],
        )
        return

    prefs = ChatPrefsStore(resolve_prefs_path(config_path))
    chat_id = ctx.message.channel_id
    available = list(ctx.runtime.engine_ids)

    if action == "md_clr":
        # Clear model override (handled here for the merged page)
        current_engine, _ = await _resolve_effective_engine(ctx)
        current = await prefs.get_engine_override(chat_id, current_engine)
        if current and current.model:
            updated = EngineOverrides(
                model=None,
                reasoning=current.reasoning,
                permission_mode=current.permission_mode,
                ask_questions=current.ask_questions,
                diff_preview=current.diff_preview,
                show_api_cost=current.show_api_cost,
                show_subscription_usage=current.show_subscription_usage,
                show_resume_line=current.show_resume_line,
                budget_enabled=current.budget_enabled,
                budget_auto_cancel=current.budget_auto_cancel,
            )
            await prefs.set_engine_override(chat_id, current_engine, updated)
            logger.info("config.model.cleared", chat_id=chat_id)
        await _page_engine(ctx)
        return

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
    current_engine, effective_label = await _resolve_effective_engine(ctx)

    # Model info
    engine_override = await prefs.get_engine_override(chat_id, current_engine)
    model_override = engine_override.model if engine_override else None
    engine_hint = _ENGINE_MODEL_HINTS.get(current_engine, "from CLI settings")
    model_label = model_override or f"default ({engine_hint})"

    lines = [
        "<b>⚙️ Engine & model</b>",
        "",
        "Choose which coding agent runs your tasks in this chat.",
        "The global default is used unless you override it here.",
        "",
        f"Engine: <b>{effective_label}</b>",
        f"Model: <b>{model_label}</b>",
        "",
        "Use <code>/model set &lt;name&gt;</code> to choose a model.",
        "",
        f'📖 <a href="{_DOCS_BASE}switch-engines/">Learn more</a>',
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
            {"text": "Clear engine", "callback_data": "config:ag:clr"},
            {"text": "Clear model", "callback_data": "config:ag:md_clr"},
        ]
    )
    buttons.append([{"text": "← Back", "callback_data": "config:home"}])

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
            "<b>📡 Trigger mode</b>\n\nUnavailable (no config path).",
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
        "<b>📡 Trigger mode</b>",
        "",
        "Control when the bot responds in group chats.",
        "",
        "• <b>all</b> — respond to every message (default)",
        "• <b>mentions</b> — only respond when @mentioned",
        "",
        f"Current: <b>{current_label}</b>",
        "",
        f'📖 <a href="{_DOCS_BASE}group-chat/">Learn more</a>',
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
    """Legacy model page — redirects to the merged engine & model page.

    Kept for backwards compatibility (deep links to ``config:md``).
    The ``clr`` action is still handled here, then redirects to engine page.
    """
    from ..chat_prefs import ChatPrefsStore, resolve_prefs_path
    from ..engine_overrides import EngineOverrides

    if action == "clr":
        config_path = ctx.config_path
        if config_path is not None:
            prefs = ChatPrefsStore(resolve_prefs_path(config_path))
            chat_id = ctx.message.channel_id
            current_engine, _ = await _resolve_effective_engine(ctx)
            current = await prefs.get_engine_override(chat_id, current_engine)
            updated = EngineOverrides(
                model=None,
                reasoning=current.reasoning if current else None,
                permission_mode=current.permission_mode if current else None,
                ask_questions=current.ask_questions if current else None,
                diff_preview=current.diff_preview if current else None,
                show_api_cost=current.show_api_cost if current else None,
                show_subscription_usage=current.show_subscription_usage
                if current
                else None,
                show_resume_line=current.show_resume_line if current else None,
                budget_enabled=current.budget_enabled if current else None,
                budget_auto_cancel=current.budget_auto_cancel if current else None,
            )
            await prefs.set_engine_override(chat_id, current_engine, updated)
            logger.info("config.model.cleared", chat_id=chat_id, engine=current_engine)
        await _page_engine(ctx)
        return

    # For navigation, redirect to the merged engine & model page
    await _page_engine(ctx)


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
    from ..engine_overrides import (
        EngineOverrides,
        allowed_reasoning_levels,
        supports_reasoning,
    )

    config_path = ctx.config_path
    if config_path is None:
        await _respond(
            ctx,
            "<b>🧠 Reasoning</b>\n\nUnavailable (no config path).",
            [[{"text": "← Back", "callback_data": "config:home"}]],
        )
        return

    prefs = ChatPrefsStore(resolve_prefs_path(config_path))
    chat_id = ctx.message.channel_id

    # Reasoning is engine-specific — guard against unsupported engines
    current_engine, _ = await _resolve_effective_engine(ctx)
    if not supports_reasoning(current_engine):
        await _respond(
            ctx,
            "<b>🧠 Reasoning</b>\n\nOnly available for engines that support reasoning levels.",
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
            show_api_cost=current.show_api_cost if current else None,
            show_subscription_usage=current.show_subscription_usage
            if current
            else None,
            show_resume_line=current.show_resume_line if current else None,
            budget_enabled=current.budget_enabled if current else None,
            budget_auto_cancel=current.budget_auto_cancel if current else None,
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
            show_api_cost=current.show_api_cost if current else None,
            show_subscription_usage=current.show_subscription_usage
            if current
            else None,
            show_resume_line=current.show_resume_line if current else None,
            budget_enabled=current.budget_enabled if current else None,
            budget_auto_cancel=current.budget_auto_cancel if current else None,
        )
        await prefs.set_engine_override(chat_id, current_engine, updated)
        logger.info("config.reasoning.cleared", chat_id=chat_id, engine=current_engine)
        await _page_home(ctx)
        return

    override = await prefs.get_engine_override(chat_id, current_engine)
    reasoning = override.reasoning if override else None
    current_label = reasoning or "default (from CLI settings)"

    levels = allowed_reasoning_levels(current_engine)

    level_descriptions: list[str] = []
    if "minimal" in levels:
        level_descriptions.append("• <b>minimal</b> — fastest responses")
    if "low" in levels or "medium" in levels or "high" in levels:
        present = [f"<b>{lv}</b>" for lv in ("low", "medium", "high") if lv in levels]
        level_descriptions.append(f"• {' · '.join(present)} — balanced options")
    if "xhigh" in levels:
        level_descriptions.append("• <b>xhigh</b> — most thorough (slowest)")

    lines = [
        "<b>🧠 Reasoning</b>",
        "",
        "How deeply the model thinks before answering.",
        "Higher = more thorough but slower and costlier.",
        "",
        *level_descriptions,
        "",
        "ℹ️ <i>Default: uses engine's own reasoning level</i>",
        "",
        f"Engine: <b>{current_engine}</b>",
        f"Current: <b>{current_label}</b>",
        "",
        f'📖 <a href="{_DOCS_BASE}model-reasoning/">Learn more</a>',
    ]

    # Build level buttons dynamically based on engine
    _LEVEL_BUTTON_MAP: dict[str, tuple[str, str]] = {
        "minimal": ("Minimal", "min"),
        "low": ("Low", "low"),
        "medium": ("Medium", "med"),
        "high": ("High", "hi"),
        "xhigh": ("Xhigh", "xhi"),
    }
    level_buttons: list[dict[str, str]] = []
    for level in levels:
        label, action_key = _LEVEL_BUTTON_MAP[level]
        level_buttons.append(
            {
                "text": _check(label, active=reasoning == level),
                "callback_data": f"config:rs:{action_key}",
            }
        )
    # Split into rows of 3
    button_rows = [level_buttons[i : i + 3] for i in range(0, len(level_buttons), 3)]

    buttons = [
        *button_rows,
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
            "<b>❓ Ask mode</b>\n\nUnavailable (no config path).",
            [[{"text": "← Back", "callback_data": "config:home"}]],
        )
        return

    prefs = ChatPrefsStore(resolve_prefs_path(config_path))
    chat_id = ctx.message.channel_id

    # Claude Code-only guard
    current_engine, _ = await _resolve_effective_engine(ctx)
    if current_engine not in ASK_QUESTIONS_SUPPORTED_ENGINES:
        await _respond(
            ctx,
            "<b>❓ Ask mode</b>\n\nOnly available for Claude Code.",
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
            show_api_cost=current.show_api_cost if current else None,
            show_subscription_usage=current.show_subscription_usage
            if current
            else None,
            show_resume_line=current.show_resume_line if current else None,
            budget_enabled=current.budget_enabled if current else None,
            budget_auto_cancel=current.budget_auto_cancel if current else None,
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
            show_api_cost=current.show_api_cost if current else None,
            show_subscription_usage=current.show_subscription_usage
            if current
            else None,
            show_resume_line=current.show_resume_line if current else None,
            budget_enabled=current.budget_enabled if current else None,
            budget_auto_cancel=current.budget_auto_cancel if current else None,
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
            show_api_cost=current.show_api_cost if current else None,
            show_subscription_usage=current.show_subscription_usage
            if current
            else None,
            show_resume_line=current.show_resume_line if current else None,
            budget_enabled=current.budget_enabled if current else None,
            budget_auto_cancel=current.budget_auto_cancel if current else None,
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
        current_label = "on"

    lines = [
        "<b>❓ Ask mode</b>",
        "",
        "Let the agent ask you questions mid-task instead of guessing.",
        "Answers appear as tappable buttons.",
        "",
        "• <b>on</b> — questions shown with option buttons (default)",
        "• <b>off</b> — agent makes its best guess and continues",
        "",
        f"Current: <b>{current_label}</b>",
        "",
        f'📖 <a href="{_DOCS_BASE}inline-settings/">Learn more</a>',
    ]

    buttons = [
        _toggle_row(
            "Ask",
            current=aq,
            default=True,
            on_data="config:aq:on",
            off_data="config:aq:off",
            clr_data="config:aq:clr",
        ),
        [{"text": "← Back", "callback_data": "config:home"}],
    ]

    await _respond(ctx, "\n".join(lines), buttons)


async def _page_diff_preview(ctx: CommandContext, action: str | None = None) -> None:
    from ..chat_prefs import ChatPrefsStore, resolve_prefs_path
    from ..engine_overrides import DIFF_PREVIEW_SUPPORTED_ENGINES, EngineOverrides

    config_path = ctx.config_path
    if config_path is None:
        await _respond(
            ctx,
            "<b>📝 Diff preview</b>\n\nUnavailable (no config path).",
            [[{"text": "← Back", "callback_data": "config:home"}]],
        )
        return

    prefs = ChatPrefsStore(resolve_prefs_path(config_path))
    chat_id = ctx.message.channel_id

    # Claude Code-only guard
    current_engine, _ = await _resolve_effective_engine(ctx)
    if current_engine not in DIFF_PREVIEW_SUPPORTED_ENGINES:
        await _respond(
            ctx,
            "<b>📝 Diff preview</b>\n\nOnly available for Claude Code.",
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
            show_api_cost=current.show_api_cost if current else None,
            show_subscription_usage=current.show_subscription_usage
            if current
            else None,
            show_resume_line=current.show_resume_line if current else None,
            budget_enabled=current.budget_enabled if current else None,
            budget_auto_cancel=current.budget_auto_cancel if current else None,
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
            show_api_cost=current.show_api_cost if current else None,
            show_subscription_usage=current.show_subscription_usage
            if current
            else None,
            show_resume_line=current.show_resume_line if current else None,
            budget_enabled=current.budget_enabled if current else None,
            budget_auto_cancel=current.budget_auto_cancel if current else None,
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
            show_api_cost=current.show_api_cost if current else None,
            show_subscription_usage=current.show_subscription_usage
            if current
            else None,
            show_resume_line=current.show_resume_line if current else None,
            budget_enabled=current.budget_enabled if current else None,
            budget_auto_cancel=current.budget_auto_cancel if current else None,
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
        current_label = "off"

    lines = [
        "<b>📝 Diff preview</b>",
        "",
        "See what the agent wants to change before you approve it.",
        "Shows a compact diff of edits and commands.",
        "",
        "• <b>on</b> — show code changes in approval messages",
        "• <b>off</b> — show approval buttons only (default)",
        "",
        f"Current: <b>{current_label}</b>",
        "",
        f'📖 <a href="{_DOCS_BASE}interactive-approval/">Learn more</a>',
    ]

    buttons = [
        _toggle_row(
            "Diff",
            current=dp,
            default=False,
            on_data="config:dp:on",
            off_data="config:dp:off",
            clr_data="config:dp:clr",
        ),
        [{"text": "← Back", "callback_data": "config:home"}],
    ]

    await _respond(ctx, "\n".join(lines), buttons)


# ---------------------------------------------------------------------------
# Cost & usage (merged API cost + subscription usage)
# ---------------------------------------------------------------------------


async def _page_cost_usage(ctx: CommandContext, action: str | None = None) -> None:
    from ..chat_prefs import ChatPrefsStore, resolve_prefs_path
    from ..engine_overrides import (
        API_COST_SUPPORTED_ENGINES,
        EngineOverrides,
        SUBSCRIPTION_USAGE_SUPPORTED_ENGINES,
    )

    config_path = ctx.config_path
    if config_path is None:
        await _respond(
            ctx,
            "<b>💰 Cost & usage</b>\n\nUnavailable (no config path).",
            [[{"text": "← Back", "callback_data": "config:home"}]],
        )
        return

    prefs = ChatPrefsStore(resolve_prefs_path(config_path))
    chat_id = ctx.message.channel_id

    current_engine, _ = await _resolve_effective_engine(ctx)

    has_api_cost = current_engine in API_COST_SUPPORTED_ENGINES
    has_sub_usage = current_engine in SUBSCRIPTION_USAGE_SUPPORTED_ENGINES

    if not has_api_cost and not has_sub_usage:
        await _respond(
            ctx,
            (
                "<b>💰 Cost & usage</b>\n\n"
                f"Not available for <b>{current_engine}</b>.\n"
                "API cost works with Claude Code and OpenCode.\n"
                "Subscription usage works with Claude Code."
            ),
            [[{"text": "← Back", "callback_data": "config:home"}]],
        )
        return

    # --- Actions: ac_on/ac_off/ac_clr, su_on/su_off/su_clr, bg_on/bg_off/bg_clr, bc_on/bc_off/bc_clr ---
    if action and "_" in action:
        prefix, act = action.split("_", 1)
        current = await prefs.get_engine_override(chat_id, current_engine)
        ac_val = current.show_api_cost if current else None
        su_val = current.show_subscription_usage if current else None
        bg_val = current.budget_enabled if current else None
        bc_val = current.budget_auto_cancel if current else None

        if prefix == "ac" and has_api_cost:
            ac_val = {"on": True, "off": False, "clr": None}[act]
            logger.info("config.api_cost.set", chat_id=chat_id, value=ac_val)
        elif prefix == "su" and has_sub_usage:
            su_val = {"on": True, "off": False, "clr": None}[act]
            logger.info("config.subscription_usage.set", chat_id=chat_id, value=su_val)
        elif prefix == "bg":
            bg_val = {"on": True, "off": False, "clr": None}[act]
            logger.info("config.budget_enabled.set", chat_id=chat_id, value=bg_val)
        elif prefix == "bc":
            bc_val = {"on": True, "off": False, "clr": None}[act]
            logger.info("config.budget_auto_cancel.set", chat_id=chat_id, value=bc_val)

        updated = EngineOverrides(
            model=current.model if current else None,
            reasoning=current.reasoning if current else None,
            permission_mode=current.permission_mode if current else None,
            ask_questions=current.ask_questions if current else None,
            diff_preview=current.diff_preview if current else None,
            show_api_cost=ac_val,
            show_subscription_usage=su_val,
            show_resume_line=current.show_resume_line if current else None,
            budget_enabled=bg_val,
            budget_auto_cancel=bc_val,
        )
        await prefs.set_engine_override(chat_id, current_engine, updated)
        await _page_cost_usage(ctx)
        return

    # --- Display ---
    override = await prefs.get_engine_override(chat_id, current_engine)
    ac = override.show_api_cost if override else None
    su = override.show_subscription_usage if override else None

    lines = [
        "<b>💰 Cost & usage</b>",
        "",
    ]

    if has_api_cost:
        ac_label = "on" if ac is True else ("off" if ac is False else "on")
        lines.append(f"<b>API cost</b>: {ac_label}")
        lines.append("  Show cost, tokens, and time after each task.")
        lines.append("")

    if has_sub_usage:
        su_label = "on" if su is True else ("off" if su is False else "off")
        lines.append(f"<b>Subscription usage</b>: {su_label}")
        lines.append("  Show how much of your 5h/weekly quota is used.")
        lines.append("")

    # Budget section
    budget_cfg = None
    try:
        from ...settings import load_settings_if_exists

        result = load_settings_if_exists()
        budget_cfg = result[0].cost_budget if result else None
    except (OSError, ValueError, KeyError):
        pass

    bg = override.budget_enabled if override else None
    bc = override.budget_auto_cancel if override else None

    lines.append("<b>Budget</b>")
    if budget_cfg is not None:
        global_enabled = budget_cfg.enabled
        bg_label = (
            "on"
            if bg is True
            else ("off" if bg is False else ("on" if global_enabled else "off"))
        )
        lines.append(f"  Enabled: {bg_label}")
        if budget_cfg.max_cost_per_run is not None:
            lines.append(f"  Per-run limit: ${budget_cfg.max_cost_per_run:.2f}")
        if budget_cfg.max_cost_per_day is not None:
            lines.append(f"  Daily limit: ${budget_cfg.max_cost_per_day:.2f}")
        global_ac = budget_cfg.auto_cancel
        bc_label = (
            "on"
            if bc is True
            else ("off" if bc is False else ("on" if global_ac else "off"))
        )
        lines.append(f"  Auto-cancel: {bc_label}")
    else:
        bg_label = "on" if bg is True else ("off" if bg is False else "off")
        bc_label = "on" if bc is True else ("off" if bc is False else "off")
        lines.append(f"  Enabled: {bg_label}")
        lines.append(f"  Auto-cancel: {bc_label}")
    lines.append("  Set limits in untether.toml [cost_budget] section.")
    lines.append("")

    lines.append(f'📖 <a href="{_DOCS_BASE}cost-budgets/">Learn more</a>')

    # Determine budget defaults from global config
    budget_default_enabled = budget_cfg.enabled if budget_cfg is not None else False
    budget_default_ac = budget_cfg.auto_cancel if budget_cfg is not None else False

    buttons: list[list[dict[str, str]]] = []

    if has_api_cost:
        buttons.append(
            _toggle_row(
                "Cost",
                current=ac,
                default=True,
                on_data="config:cu:ac_on",
                off_data="config:cu:ac_off",
                clr_data="config:cu:ac_clr",
            )
        )

    if has_sub_usage:
        buttons.append(
            _toggle_row(
                "Sub",
                current=su,
                default=False,
                on_data="config:cu:su_on",
                off_data="config:cu:su_off",
                clr_data="config:cu:su_clr",
            )
        )

    buttons.append(
        _toggle_row(
            "Budget",
            current=bg,
            default=budget_default_enabled,
            on_data="config:cu:bg_on",
            off_data="config:cu:bg_off",
            clr_data="config:cu:bg_clr",
        )
    )
    buttons.append(
        _toggle_row(
            "Auto-cancel",
            current=bc,
            default=budget_default_ac,
            on_data="config:cu:bc_on",
            off_data="config:cu:bc_off",
            clr_data="config:cu:bc_clr",
        )
    )

    buttons.append([{"text": "← Back", "callback_data": "config:home"}])

    await _respond(ctx, "\n".join(lines), buttons)


# ---------------------------------------------------------------------------
# Resume line
# ---------------------------------------------------------------------------


async def _page_resume_line(ctx: CommandContext, action: str | None = None) -> None:
    from ..chat_prefs import ChatPrefsStore, resolve_prefs_path
    from ..engine_overrides import EngineOverrides

    config_path = ctx.config_path
    if config_path is None:
        await _respond(
            ctx,
            "<b>↩️ Resume line</b>\n\nUnavailable (no config path).",
            [[{"text": "← Back", "callback_data": "config:home"}]],
        )
        return

    prefs = ChatPrefsStore(resolve_prefs_path(config_path))
    chat_id = ctx.message.channel_id
    current_engine, _ = await _resolve_effective_engine(ctx)

    if action in ("on", "off", "clr"):
        current = await prefs.get_engine_override(chat_id, current_engine)
        new_val = {"on": True, "off": False, "clr": None}[action]
        updated = EngineOverrides(
            model=current.model if current else None,
            reasoning=current.reasoning if current else None,
            permission_mode=current.permission_mode if current else None,
            ask_questions=current.ask_questions if current else None,
            diff_preview=current.diff_preview if current else None,
            show_api_cost=current.show_api_cost if current else None,
            show_subscription_usage=current.show_subscription_usage
            if current
            else None,
            show_resume_line=new_val,
            budget_enabled=current.budget_enabled if current else None,
            budget_auto_cancel=current.budget_auto_cancel if current else None,
        )
        await prefs.set_engine_override(chat_id, current_engine, updated)
        logger.info("config.resume_line.set", chat_id=chat_id, value=new_val)
        await _page_home(ctx)
        return

    # Display
    override = await prefs.get_engine_override(chat_id, current_engine)
    rl = override.show_resume_line if override else None

    _resume_default = True
    try:
        from ...settings import load_settings_if_exists as _load_rl_cfg

        _rl_result = _load_rl_cfg()
        if _rl_result is not None:
            _resume_default = _rl_result[0].transports.telegram.show_resume_line
    except (OSError, ValueError, KeyError):
        pass

    rl_label = (
        "on"
        if rl is True
        else ("off" if rl is False else ("on" if _resume_default else "off"))
    )

    lines = [
        "<b>↩️ Resume line</b>",
        "",
        f"Current: <b>{rl_label}</b>",
        "",
        "Shows the engine's resume command in message footers.",
        "Reply to continue in Telegram, or copy-paste into your",
        "terminal to pick up the session in CLI.",
        "",
        f'📖 <a href="{_DOCS_BASE}conversation-modes/">Learn more</a>',
    ]

    buttons = [
        _toggle_row(
            "Resume",
            current=rl,
            default=_resume_default,
            on_data="config:rl:on",
            off_data="config:rl:off",
            clr_data="config:rl:clr",
        ),
        [{"text": "← Back", "callback_data": "config:home"}],
    ]

    await _respond(ctx, "\n".join(lines), buttons)


# ---------------------------------------------------------------------------
# About
# ---------------------------------------------------------------------------

_REPO_URL = "https://github.com/littlebearapps/untether"


async def _page_about(ctx: CommandContext, action: str | None = None) -> None:
    from ... import __version__
    from ..backend import _build_versions_line

    lines = [
        "\N{DOG} <b>About Untether</b>",
        "",
        f"Version: <b>{__version__}</b>",
    ]

    versions_line = _build_versions_line(tuple(ctx.runtime.engine_ids))
    if versions_line:
        lines.append(f"<code>{versions_line}</code>")

    lines.append("")
    lines.append(
        f'🔗 <a href="{_REPO_URL}">GitHub</a>'
        f' · <a href="{_REPO_URL}/issues/new?template=bug_report.yml">Report a bug</a>'
        f' · <a href="{_REPO_URL}/issues/new?template=feature_request.yml">Feature request</a>'
    )

    buttons = [[{"text": "← Back", "callback_data": "config:home"}]]
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
    "cu": _page_cost_usage,
    "rl": _page_resume_line,
    "ab": _page_about,
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
                "clr": "Permission mode: cleared",
                "fa": "Approval policy: full auto",
                "ya": "Approval mode: full access",
                "ae": "Approval mode: edit files",
                "ro": "Approval mode: read-only",
                "safe": "Approval policy: safe",
            },
            "vb": {
                "on": "Verbose: on",
                "off": "Verbose: off",
                "clr": "Verbose: cleared",
            },
            "ag": {"clr": "Engine: cleared", "md_clr": "Model: cleared"},
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
            "cu": {
                "ac_on": "API cost: on",
                "ac_off": "API cost: off",
                "ac_clr": "API cost: cleared",
                "su_on": "Sub usage: on",
                "su_off": "Sub usage: off",
                "su_clr": "Sub usage: cleared",
                "bg_on": "Budget: on",
                "bg_off": "Budget: off",
                "bg_clr": "Budget: cleared",
                "bc_on": "Auto-cancel: on",
                "bc_off": "Auto-cancel: off",
                "bc_clr": "Auto-cancel: cleared",
            },
            "rl": {
                "on": "Resume line: on",
                "off": "Resume line: off",
                "clr": "Resume line: cleared",
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
