from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import anyio

from ..backends import EngineBackend
from ..config import read_config
from ..logging import get_logger
from ..runner_bridge import ExecBridgeConfig
from ..settings import TelegramTopicsSettings, TelegramTransportSettings
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


def _expect_transport_settings(transport_config: object) -> TelegramTransportSettings:
    if isinstance(transport_config, TelegramTransportSettings):
        return transport_config
    raise TypeError("transport_config must be TelegramTransportSettings")


def _build_startup_message(
    runtime: TransportRuntime,
    *,
    startup_pwd: str,
    chat_id: int,
    session_mode: Literal["stateless", "chat"],
    show_resume_line: bool,
    topics: TelegramTopicsSettings,
    trigger_config: dict | None = None,
) -> str:
    available_engines = list(runtime.available_engine_ids())
    missing_engines = list(runtime.missing_engine_ids())
    misconfigured_engines = list(runtime.engine_ids_with_status("bad_config"))
    failed_engines = list(runtime.engine_ids_with_status("load_error"))

    engine_list = ", ".join(available_engines) if available_engines else "none"

    notes: list[str] = []
    if missing_engines:
        notes.append(f"not installed: {', '.join(missing_engines)}")
    if misconfigured_engines:
        notes.append(f"misconfigured: {', '.join(misconfigured_engines)}")
    if failed_engines:
        notes.append(f"failed to load: {', '.join(failed_engines)}")
    if notes:
        engine_list = f"{engine_list} ({'; '.join(notes)})"
    project_aliases = sorted(set(runtime.project_aliases()), key=str.lower)
    project_list = ", ".join(project_aliases) if project_aliases else "none"
    resume_label = "shown" if show_resume_line else "hidden"
    topics_label = "disabled"
    if topics.enabled:
        resolved_scope, _ = _resolve_topics_scope_raw(
            topics.scope, chat_id, runtime.project_chat_ids()
        )
        scope_label = (
            f"auto ({resolved_scope})" if topics.scope == "auto" else resolved_scope
        )
        topics_label = f"enabled (scope={scope_label})"
    triggers_label = "disabled"
    if trigger_config and trigger_config.get("enabled"):
        n_wh = len(trigger_config.get("webhooks", []))
        n_cr = len(trigger_config.get("crons", []))
        triggers_label = f"enabled ({n_wh} webhooks, {n_cr} crons)"
    return (
        f"\N{OCTOPUS} **untether is ready**\n\n"
        f"default: `{runtime.default_engine}`  \n"
        f"engines: `{engine_list}`  \n"
        f"projects: `{project_list}`  \n"
        f"mode: `{session_mode}`  \n"
        f"topics: `{topics_label}`  \n"
        f"triggers: `{triggers_label}`  \n"
        f"resume lines: `{resume_label}`  \n"
        f"working in: `{startup_pwd}`"
    )


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
            startup_pwd=os.getcwd(),
            chat_id=chat_id,
            session_mode=settings.session_mode,
            show_resume_line=settings.show_resume_line,
            topics=settings.topics,
            trigger_config=trigger_config,
        )
        bot = TelegramClient(token)
        transport = TelegramTransport(bot)
        presenter = TelegramPresenter(message_overflow=settings.message_overflow)
        exec_cfg = ExecBridgeConfig(
            transport=transport,
            presenter=presenter,
            final_notify=final_notify,
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
