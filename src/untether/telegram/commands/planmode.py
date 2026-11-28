"""Command backend for setting Claude Code's permission mode via /planmode."""

from __future__ import annotations

from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger
from ...runners.run_options import CLAUDE_PLAN_AUTO_MODE, claude_cli_permission_mode
from ._permission_mode_text import (
    APPLY_TIMING_TEXT,
    NO_OVERRIDE_LABEL,
    NO_OVERRIDE_TEXT,
    cli_name_suffix,
    mode_display,
)
from .overrides import SETTINGS_ADMIN_GATE, AdminGate

logger = get_logger(__name__)

PLANMODE_USAGE = (
    "usage: `/planmode`, `/planmode on`, `/planmode plan-auto`,"
    " `/planmode auto`, `/planmode off`, `/planmode show`, or"
    " `/planmode clear`"
)

# #741 `plan-auto` is Untether's own sugar (CLI plan mode + auto-approved
# ExitPlanMode).  `auto` is now the CLI's native classifier-gated mode, which
# this command shadowed until 0.35.5rc8.
PERMISSION_MODES = {
    "on": "plan",
    "plan-auto": CLAUDE_PLAN_AUTO_MODE,
    "auto": "auto",
    "off": "acceptEdits",
}

# Modes that mean "planning is active" for the bare `/planmode` toggle.
_PLANNING_MODES = ("plan", CLAUDE_PLAN_AUTO_MODE)

# Engines that support the /planmode command (Claude-style permission modes).
# Antigravity and Codex have their own handlers below; Gemini uses
# /config → Approval policy instead.
_PLANMODE_ENGINES = frozenset({"claude"})


def _summary(mode: str) -> str:
    _, mode_text = mode_display(mode)
    return mode_text.summary if mode_text is not None else mode


class PlanModeCommand:
    """Command backend for toggling Claude Code permission mode."""

    id = "planmode"
    description = "Set Claude Code permission mode: on/plan-auto/auto/off"

    @staticmethod
    def admin_gate(args_text: str) -> AdminGate | None:
        """#1042: changing the chat's permission mode needs a group admin.

        `/planmode` writes the setting `/config pm:*` writes, so it takes the
        same gate (run by the dispatch before ``handle``). A mode name,
        ``clear`` and the bare toggle change it; ``show`` and anything that
        only gets the usage reply do not.
        """
        args = args_text.strip().lower()
        if args == "" or args == "clear" or args in PERMISSION_MODES:
            return SETTINGS_ADMIN_GATE
        return None

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        from ..chat_prefs import shared_prefs_store
        from ..engine_overrides import with_override
        from ._resolve_engine import resolve_effective_engine

        config_path = ctx.config_path
        if config_path is None:
            return CommandResult(
                text="permission mode overrides unavailable (no config path).",
                notify=True,
            )

        current_engine = await resolve_effective_engine(ctx)
        if current_engine == "antigravity":
            return await _handle_antigravity(ctx, config_path)
        if current_engine == "codex":
            return await _handle_codex(ctx, config_path)
        if current_engine not in _PLANMODE_ENGINES:
            hint = ""
            if current_engine == "gemini":
                hint = " Use /config → Approval policy instead."
            return CommandResult(
                text=(
                    f"Plan mode is only available for Claude Code."
                    f" Current engine: <b>{current_engine}</b>.{hint}"
                ),
                notify=True,
                parse_mode="HTML",
            )

        chat_prefs = shared_prefs_store(config_path)
        chat_id = ctx.message.channel_id
        engine = current_engine
        args = ctx.args_text.strip().lower()

        if args == "show":
            current = await chat_prefs.get_engine_override(chat_id, engine)
            mode = current.permission_mode if current else None
            if mode is None:
                text = (
                    f"permission mode: <b>{NO_OVERRIDE_LABEL}</b>: no override"
                    f" for this chat; it {NO_OVERRIDE_TEXT}."
                )
            else:
                # #747: only acceptEdits is "off"; a hand-stored mode that
                # /planmode can't set shows its own name.
                ui_name, mode_text = mode_display(mode)
                text = f"permission mode: <b>{ui_name}</b>{cli_name_suffix(mode)}"
                if mode_text is not None:
                    text += f": {mode_text.summary}."
            return CommandResult(text=text, notify=True, parse_mode="HTML")

        if args == "":
            # Toggle: if currently plan/auto mode, turn off; otherwise turn on
            current = await chat_prefs.get_engine_override(chat_id, engine)
            current_mode = current.permission_mode if current else None
            args = "off" if current_mode in _PLANNING_MODES else "on"

        if args in PERMISSION_MODES:
            mode = PERMISSION_MODES[args]
            current = await chat_prefs.get_engine_override(chat_id, engine)
            updated = with_override(current, permission_mode=mode)
            await chat_prefs.set_engine_override(chat_id, engine, updated)
            cli_mode = claude_cli_permission_mode(mode)
            logger.info(
                "planmode.set",
                chat_id=chat_id,
                sender_id=ctx.message.sender_id,
                mode=args,
                cli_mode=cli_mode,
                command="planmode",
            )
            return CommandResult(
                text=(
                    f"permission mode <b>{args}</b> for this chat:"
                    f" {_summary(mode)}.\n"
                    "applies from your next message"
                    f" (<code>--permission-mode {cli_mode}</code>)."
                    f" {APPLY_TIMING_TEXT}"
                ),
                notify=True,
                parse_mode="HTML",
            )

        if args == "clear":
            current = await chat_prefs.get_engine_override(chat_id, engine)
            updated = with_override(current, permission_mode=None)
            await chat_prefs.set_engine_override(chat_id, engine, updated)
            logger.info(
                "planmode.cleared",
                chat_id=chat_id,
                sender_id=ctx.message.sender_id,
                command="planmode",
            )
            return CommandResult(
                text=(
                    "permission mode <b>override cleared</b>: no override for"
                    f" this chat; it {NO_OVERRIDE_TEXT}."
                ),
                notify=True,
                parse_mode="HTML",
            )

        return CommandResult(text=PLANMODE_USAGE, notify=True)


_AGY_PLAN_ONLY = (
    "<b>{arg}</b> isn't available for Antigravity — use /planmode on / off,"
    " or /config → Permission mode."
)


async def _handle_antigravity(ctx: CommandContext, config_path) -> CommandResult:
    """#981: ``/planmode`` for Antigravity maps onto its permission modes —
    ``on`` = Plan first; ``off`` = drop back to Workspace (or the TOML
    default when that isn't Plan first); ``show``; ``clear``."""
    from ..chat_prefs import shared_prefs_store
    from ..engine_overrides import with_override
    from . import _antigravity_mode_text as agy_mode

    prefs = shared_prefs_store(config_path)
    chat_id = ctx.message.channel_id
    engine = agy_mode.ENGINE
    args = ctx.args_text.strip().lower()

    async def _effective() -> str:
        value, _source = await agy_mode.antigravity_effective_mode(
            prefs, chat_id, ctx.runtime
        )
        return agy_mode.canonical_mode(value)

    before = await _effective()

    async def _describe(prefix: str) -> CommandResult:
        value, source = await agy_mode.antigravity_effective_mode(
            prefs, chat_id, ctx.runtime
        )
        mode = agy_mode.ANTIGRAVITY_MODE_TEXT[agy_mode.canonical_mode(value)]
        if agy_mode.needs_new(before, value):
            # #1060 / #980: a conversation keeps the tools it started with,
            # so a switch to or from Full access or Ask me applies from the
            # next new one.
            prefix = prefix.replace(
                "applies from your next message", agy_mode.NEW_NEEDED_TEXT
            ).replace(
                "override cleared", f"override cleared ({agy_mode.NEW_NEEDED_TEXT})"
            )
        return CommandResult(
            text=(
                f"{prefix}<b>{mode.label}</b>{agy_mode.source_suffix(source)}:"
                f" {mode.hint}."
            ),
            notify=True,
            parse_mode="HTML",
        )

    async def _store(value: str | None) -> None:
        current = await prefs.get_engine_override(chat_id, engine)
        await prefs.set_engine_override(
            chat_id, engine, with_override(current, permission_mode=value)
        )

    if args == "show":
        return await _describe("permission mode: ")
    if args in ("auto", "plan-auto"):
        return CommandResult(
            text=_AGY_PLAN_ONLY.format(arg=args), notify=True, parse_mode="HTML"
        )
    if args == "":
        value, _source = await agy_mode.antigravity_effective_mode(
            prefs, chat_id, ctx.runtime
        )
        args = "off" if agy_mode.canonical_mode(value) == "plan" else "on"
    if args == "on":
        await _store("plan")
        logger.info(
            "planmode.set",
            chat_id=chat_id,
            sender_id=ctx.message.sender_id,
            mode="on",
            engine=engine,
        )
        return await _describe(
            "applies from your next message. permission mode for this chat: "
        )
    if args == "off":
        await _store(None)
        value, _source = await agy_mode.antigravity_effective_mode(
            prefs, chat_id, ctx.runtime
        )
        if agy_mode.canonical_mode(value) == "plan":
            # ``[antigravity] permission_mode = "plan"``: off must still mean off.
            await _store("workspace")
        logger.info(
            "planmode.set",
            chat_id=chat_id,
            sender_id=ctx.message.sender_id,
            mode="off",
            engine=engine,
        )
        return await _describe(
            "applies from your next message. permission mode for this chat: "
        )
    if args == "clear":
        await _store(None)
        logger.info(
            "planmode.cleared",
            chat_id=chat_id,
            sender_id=ctx.message.sender_id,
            engine=engine,
        )
        return await _describe("override cleared. permission mode: ")
    return CommandResult(text=PLANMODE_USAGE, notify=True)


_CODEX_MODES = {"on": "plan", "plan-auto": "plan-auto"}
_CODEX_MODE_TEXT = {
    "plan": (
        "Plan first",
        "Codex plans without changing anything; reply to go ahead",
    ),
    "plan-auto": (
        "Plan first (auto)",
        "Codex plans first and the plan is approved for you",
    ),
}
CODEX_NEEDS_APP_SERVER = (
    "Plan mode needs the app-server transport. Set"
    ' <code>transport = "app-server"</code> under <code>[engines.codex]</code>,'
    " or pick Full auto or Safe in /config → Approval policy."
)


async def _handle_codex(ctx: CommandContext, config_path) -> CommandResult:
    """#965: ``/planmode`` for Codex on app-server: ``on`` = Plan first,
    ``plan-auto``, ``off`` / ``clear`` drop the override (back to Full
    auto), ``show``. Exec has no plan mode."""
    from ..chat_prefs import shared_prefs_store
    from ..engine_overrides import with_override
    from .config import _codex_pm_label, _codex_transport

    args = ctx.args_text.strip().lower()
    if _codex_transport(ctx) != "app-server":
        return CommandResult(
            text=CODEX_NEEDS_APP_SERVER, notify=True, parse_mode="HTML"
        )
    if args not in {"", "show", "off", "clear", *_CODEX_MODES}:
        return CommandResult(
            text=(
                f"<b>{args}</b> isn't a Codex plan mode: use /planmode on,"
                " plan-auto or off, or /config → Approval policy."
            ),
            notify=True,
            parse_mode="HTML",
        )
    prefs = shared_prefs_store(config_path)
    chat_id = ctx.message.channel_id
    current = await prefs.get_engine_override(chat_id, "codex")
    mode = current.permission_mode if current else None
    if args == "":
        args = "off" if mode in _CODEX_MODE_TEXT else "on"
    prefix = "permission mode: "
    if args != "show":
        mode = _CODEX_MODES.get(args)
        await prefs.set_engine_override(
            chat_id, "codex", with_override(current, permission_mode=mode)
        )
        logger.info(
            "planmode.set",
            chat_id=chat_id,
            sender_id=ctx.message.sender_id,
            mode=args,
            engine="codex",
        )
        prefix = "applies from your next message. permission mode for this chat: "
    label, hint = _CODEX_MODE_TEXT.get(
        mode or "", (_codex_pm_label(mode, "app-server"), "no plan phase")
    )
    return CommandResult(
        text=f"{prefix}<b>{label}</b>: {hint}.", notify=True, parse_mode="HTML"
    )


BACKEND: CommandBackend = PlanModeCommand()
