from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import anyio

from .. import __version__
from ..backends import EngineBackend
from ..config import read_config
from ..logging import get_logger
from ..markdown import MarkdownFormatter
from ..runner_bridge import ExecBridgeConfig
from ..settings import (
    ProgressSettings,
    TelegramTopicsSettings,
    TelegramTransportSettings,
)
from ..transport_runtime import TransportRuntime
from ..transports import SetupResult, TransportBackend
from .bridge import (
    TelegramBridgeConfig,
    TelegramPresenter,
    TelegramTransport,
    run_main_loop,
)
from .client import TelegramClient
from .onboarding import check_setup, interactive_setup
from .topics import _resolve_topics_scope_raw

logger = get_logger(__name__)


def _load_progress_settings() -> ProgressSettings:
    """Load progress settings from config, returning defaults if unavailable."""
    try:
        from ..settings import load_settings_if_exists

        result = load_settings_if_exists()
        if result is None:
            return ProgressSettings()
        settings, _ = result
        return settings.progress
    except Exception:  # noqa: BLE001
        logger.debug("progress_settings.load_failed", exc_info=True)
        return ProgressSettings()


def _expect_transport_settings(transport_config: object) -> TelegramTransportSettings:
    if isinstance(transport_config, TelegramTransportSettings):
        return transport_config
    raise TypeError("transport_config must be TelegramTransportSettings")


def _detect_cli_version(cmd: str) -> str | None:
    """Run ``<cmd> --version`` and return the version string, or None."""
    try:
        result = subprocess.run(
            [cmd, "--version"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Extract just the version number from output like "claude v1.0.20"
            text = result.stdout.strip().splitlines()[0]
            # Try to find a version-like substring
            for token in text.split():
                cleaned = token.lstrip("vV")
                if cleaned and cleaned[0].isdigit():
                    return cleaned
            return text
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _build_versions_line(engine_ids: tuple[str, ...]) -> str | None:
    """Build a ``py X.Y.Z · engine X.Y.Z`` versions line."""
    py = (
        f"py {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    parts = [py]
    for engine in sorted(engine_ids):
        version = _detect_cli_version(engine)
        if version:
            parts.append(f"{engine} {version}")
    return " · ".join(parts) if len(parts) > 1 else None


def _resolve_mode_label(
    session_mode: str,
    topics_enabled: bool,
) -> str:
    """Derive the workflow mode name from config values."""
    if session_mode == "stateless":
        return "handoff"
    if topics_enabled:
        return "workspace"
    return "assistant"


def _build_startup_message(
    runtime: TransportRuntime,
    *,
    chat_id: int,
    topics: TelegramTopicsSettings,
    session_mode: str = "stateless",
    trigger_config: dict | None = None,
) -> str:
    project_aliases = sorted(set(runtime.project_aliases()), key=str.lower)

    header = f"\N{DOG} **untether is ready** (v{__version__})"

    # engines — separate default and installed lines
    available_engines = list(runtime.available_engine_ids())
    missing_engines = list(runtime.missing_engine_ids())
    misconfigured_engines = list(runtime.engine_ids_with_status("bad_config"))
    failed_engines = list(runtime.engine_ids_with_status("load_error"))
    engine_notes: list[str] = []
    if missing_engines:
        engine_notes.append(f"not installed: {', '.join(missing_engines)}")
    if misconfigured_engines:
        engine_notes.append(f"misconfigured: {', '.join(misconfigured_engines)}")
    if failed_engines:
        engine_notes.append(f"failed to load: {', '.join(failed_engines)}")
    engine_list = ", ".join(available_engines) if available_engines else "none"

    details: list[str] = []
    details.append(f"_default engine:_ `{runtime.default_engine}`")
    if engine_notes:
        details.append(
            f"_installed engines:_ `{engine_list}` ({'; '.join(engine_notes)})"
        )
    else:
        details.append(f"_installed engines:_ `{engine_list}`")

    # mode — derived from session_mode + topics
    mode = _resolve_mode_label(session_mode, topics.enabled)
    details.append(f"_mode:_ `{mode}`")

    # directories — listed by name
    if project_aliases:
        details.append(f"_directories:_ `{', '.join(project_aliases)}`")
    else:
        details.append("_directories:_ `none`")

    # topics — only shown when enabled
    if topics.enabled:
        resolved_scope, _ = _resolve_topics_scope_raw(
            topics.scope, chat_id, runtime.project_chat_ids()
        )
        scope_label = (
            f"auto ({resolved_scope})" if topics.scope == "auto" else resolved_scope
        )
        details.append(f"_topics:_ `enabled (scope={scope_label})`")

    # triggers — only shown when enabled
    if trigger_config and trigger_config.get("enabled"):
        n_wh = len(trigger_config.get("webhooks", []))
        n_cr = len(trigger_config.get("crons", []))
        details.append(f"_triggers:_ `enabled ({n_wh} webhooks, {n_cr} crons)`")

    _DOCS_URL = (
        "https://github.com/littlebearapps/untether?tab=readme-ov-file#-help-guides"
    )
    _ISSUES_URL = (
        "https://github.com/littlebearapps/untether?tab=readme-ov-file#-contributing"
    )
    footer = (
        f"\n\nSend a message to start, or /config for settings."
        f"\n\n\N{OPEN BOOK} [Click here for help]({_DOCS_URL})"
        f" | \N{BUG} [Click here to report a bug]({_ISSUES_URL})"
    )

    return header + "\n\n" + "\n\n".join(details) + footer


class TelegramBackend(TransportBackend):
    id = "telegram"
    description = "Telegram bot"

    def check_setup(
        self,
        engine_backend: EngineBackend,
        *,
        transport_override: str | None = None,
    ) -> SetupResult:
        return check_setup(engine_backend, transport_override=transport_override)

    async def interactive_setup(self, *, force: bool) -> bool:
        return await interactive_setup(force=force)

    def lock_token(self, *, transport_config: object, _config_path: Path) -> str | None:
        settings = _expect_transport_settings(transport_config)
        return settings.bot_token

    def build_and_run(
        self,
        *,
        transport_config: object,
        config_path: Path,
        runtime: TransportRuntime,
        final_notify: bool,
        default_engine_override: str | None,
    ) -> None:
        settings = _expect_transport_settings(transport_config)
        token = settings.bot_token
        chat_id = settings.chat_id

        # Extract trigger config from the raw TOML (optional section).
        trigger_config: dict | None = None
        try:
            raw_toml = read_config(config_path)
            raw_triggers = raw_toml.get("triggers")
            if isinstance(raw_triggers, dict):
                trigger_config = raw_triggers
        except (OSError, ValueError, KeyError) as exc:
            logger.debug("triggers.config.read_skipped", error=str(exc))

        startup_msg = _build_startup_message(
            runtime,
            chat_id=chat_id,
            topics=settings.topics,
            session_mode=settings.session_mode,
            trigger_config=trigger_config,
        )
        progress_cfg = _load_progress_settings()
        bot = TelegramClient(token, group_chat_rps=progress_cfg.group_chat_rps)
        transport = TelegramTransport(bot)
        formatter = MarkdownFormatter(
            max_actions=progress_cfg.max_actions,
            verbosity=progress_cfg.verbosity,
        )
        presenter = TelegramPresenter(
            formatter=formatter,
            message_overflow=settings.message_overflow,
        )
        _files_enabled = settings.files.enabled and settings.files.outbox_enabled

        async def _send_file_via_bot(
            chat_id: int,
            thread_id: int | None,
            filename: str,
            content: bytes,
            reply_to: int | None,
            caption: str | None,
        ) -> None:
            await bot.send_document(
                chat_id=chat_id,
                filename=filename,
                content=content,
                reply_to_message_id=reply_to,
                message_thread_id=thread_id,
                caption=caption,
            )

        exec_cfg = ExecBridgeConfig(
            transport=transport,
            presenter=presenter,
            final_notify=final_notify,
            min_render_interval=progress_cfg.min_render_interval,
            send_file=_send_file_via_bot if _files_enabled else None,
            outbox_config=settings.files if _files_enabled else None,
        )
        cfg = TelegramBridgeConfig(
            bot=bot,
            runtime=runtime,
            chat_id=chat_id,
            startup_msg=startup_msg,
            exec_cfg=exec_cfg,
            session_mode=settings.session_mode,
            show_resume_line=settings.show_resume_line,
            voice_transcription=settings.voice_transcription,
            voice_max_bytes=int(settings.voice_max_bytes),
            voice_transcription_model=settings.voice_transcription_model,
            voice_transcription_base_url=settings.voice_transcription_base_url,
            voice_transcription_api_key=settings.voice_transcription_api_key,
            voice_show_transcription=settings.voice_show_transcription,
            forward_coalesce_s=settings.forward_coalesce_s,
            media_group_debounce_s=settings.media_group_debounce_s,
            allowed_user_ids=tuple(settings.allowed_user_ids),
            topics=settings.topics,
            files=settings.files,
            trigger_config=trigger_config,
        )

        async def run_loop() -> None:
            await run_main_loop(
                cfg,
                watch_config=runtime.watch_config,
                default_engine_override=default_engine_override,
                transport_id=self.id,
                transport_config=settings,
            )

        anyio.run(run_loop)


telegram_backend = TelegramBackend()
BACKEND = telegram_backend
