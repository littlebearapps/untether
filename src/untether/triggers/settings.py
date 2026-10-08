"""Pydantic models for trigger configuration."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Annotated, Any, Literal, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic.types import StrictInt

from ..logging import get_logger

logger = get_logger(__name__)

_SAFE_PATH_RE = re.compile(r"^/[a-zA-Z0-9/_.-]+$")

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class TriggerServerSettings(BaseModel):
    """HTTP server settings for webhook reception."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    host: str = "127.0.0.1"
    port: StrictInt = Field(default=9876, ge=1, le=65535)
    rate_limit: StrictInt = Field(default=60, ge=1)
    max_body_bytes: StrictInt = Field(default=1_048_576, ge=1024, le=10_485_760)


class WebhookConfig(BaseModel):
    """Configuration for a single webhook endpoint."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: NonEmptyStr
    path: NonEmptyStr
    project: NonEmptyStr | None = None

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("webhook path must start with '/'")
        if v == "/health":
            raise ValueError("webhook path must not be '/health' (reserved)")
        if not _SAFE_PATH_RE.match(v):
            raise ValueError(
                "webhook path must contain only alphanumeric characters, "
                "slashes, underscores, dots, and hyphens"
            )
        return v

    engine: NonEmptyStr | None = None
    chat_id: StrictInt | None = None
    auth: Literal["bearer", "hmac-sha256", "hmac-sha1", "none"] = "bearer"
    secret: NonEmptyStr | None = None
    prompt_template: NonEmptyStr | None = None
    event_filter: NonEmptyStr | None = None

    # --- Multipart file upload fields ---
    accept_multipart: bool = False
    file_destination: NonEmptyStr | None = None
    max_file_size_bytes: StrictInt = Field(default=52_428_800, ge=1024, le=104_857_600)

    # --- Non-agent action fields ---
    action: Literal["agent_run", "file_write", "http_forward", "notify_only"] = (
        "agent_run"
    )
    file_path: NonEmptyStr | None = None
    on_conflict: Literal["overwrite", "append_timestamp", "error"] = "overwrite"
    forward_url: NonEmptyStr | None = None
    forward_headers: dict[str, str] | None = None
    forward_method: Literal["POST", "PUT", "PATCH"] = "POST"
    message_template: NonEmptyStr | None = None
    notify_on_success: bool = False
    notify_on_failure: bool = False

    @model_validator(mode="after")
    def _require_secret_for_auth(self) -> WebhookConfig:
        if self.auth != "none" and not self.secret:
            raise ValueError(f"secret is required when auth={self.auth!r}")
        return self

    @model_validator(mode="after")
    def _validate_action_fields(self) -> WebhookConfig:
        if self.action == "agent_run" and not self.prompt_template:
            raise ValueError("prompt_template is required when action='agent_run'")
        if self.action == "file_write" and not self.file_path:
            raise ValueError("file_path is required when action='file_write'")
        if self.action == "http_forward" and not self.forward_url:
            raise ValueError("forward_url is required when action='http_forward'")
        if self.action == "notify_only" and not self.message_template:
            raise ValueError("message_template is required when action='notify_only'")
        return self


class CronFetchConfig(BaseModel):
    """Configuration for a cron pre-fetch step."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: Literal["http_get", "http_post", "file_read"]
    url: NonEmptyStr | None = None
    headers: dict[str, str] | None = None
    body: NonEmptyStr | None = None
    file_path: NonEmptyStr | None = None
    timeout_seconds: StrictInt = Field(default=15, ge=1, le=60)
    parse_as: Literal["json", "text", "lines"] = "text"
    store_as: NonEmptyStr = "fetch_result"
    on_failure: Literal["abort", "run_with_error"] = "abort"
    max_bytes: StrictInt = Field(default=10_485_760, ge=1024, le=104_857_600)

    @model_validator(mode="after")
    def _validate_fetch_fields(self) -> CronFetchConfig:
        if self.type in ("http_get", "http_post") and not self.url:
            raise ValueError(f"url is required when fetch type={self.type!r}")
        if self.type == "file_read" and not self.file_path:
            raise ValueError("file_path is required when fetch type='file_read'")
        return self


class CronConfig(BaseModel):
    """Configuration for a scheduled cron trigger."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: NonEmptyStr
    schedule: NonEmptyStr
    project: NonEmptyStr | None = None
    engine: NonEmptyStr | None = None
    chat_id: StrictInt | None = None
    prompt: NonEmptyStr | None = None
    prompt_template: NonEmptyStr | None = None
    timezone: NonEmptyStr | None = None
    fetch: CronFetchConfig | None = None
    run_once: bool = False
    permission_mode: NonEmptyStr | None = None
    # #743: this cron's own model / effort (reasoning) for its run only; unset
    # inherits the chat's /model and reasoning, then engine config.
    model: NonEmptyStr | None = None
    reasoning: NonEmptyStr | None = None

    @field_validator("model")
    @classmethod
    def _validate_model(cls, v: str | None) -> str | None:
        # Free-form like /model set (the engine rejects unknown names), but
        # never something that could read as another argv flag or carry
        # whitespace / control characters into argv and logs.
        if v is None:
            return v
        if v.startswith("-") or any(ch.isspace() or not ch.isprintable() for ch in v):
            raise ValueError(
                f"invalid model {v!r}: must not start with '-' or contain "
                "whitespace or control characters"
            )
        return v

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                ZoneInfo(v)
            except (ZoneInfoNotFoundError, KeyError):
                raise ValueError(
                    f"unknown timezone {v!r}; use IANA names like 'Australia/Melbourne'"
                ) from None
        return v

    @model_validator(mode="after")
    def _validate_prompt(self) -> CronConfig:
        if not self.prompt and not self.prompt_template:
            raise ValueError("either prompt or prompt_template is required")
        return self

    @model_validator(mode="after")
    def _validate_permission_mode(self) -> CronConfig:
        # Import lazily to avoid circular import at module load.
        from ..runners.run_options import VALID_PERMISSION_MODES_BY_ENGINE

        if self.permission_mode is None:
            return self
        allowed = VALID_PERMISSION_MODES_BY_ENGINE.get(self.engine or "")
        if allowed is None:
            # Engine absent from the dict (pi/opencode/amp, or engine unset) —
            # accept any non-empty string; the runner will silently no-op if
            # it doesn't recognise the value. Forward-compatible for engines
            # whose permission wiring is still pending (#331, #332).
            return self
        if self.permission_mode not in allowed:
            raise ValueError(
                f"unknown permission_mode {self.permission_mode!r} for engine "
                f"{self.engine!r}; allowed values: {sorted(allowed)}"
            )
        return self

    @model_validator(mode="after")
    def _validate_reasoning(self) -> CronConfig:
        """#743: strict when ``engine`` is explicit, else the global table.

        With no ``engine`` the cron resolves later (project default, #862);
        a level that engine doesn't allow gets the executor's run-time note.
        """
        if self.reasoning is None:
            return self
        # Import lazily to avoid a circular import at module load.
        from ..telegram.engine_overrides import (
            REASONING_LEVELS,
            allowed_reasoning_levels,
            supports_reasoning,
        )

        level = self.reasoning.lower()
        if self.engine is not None:
            if not supports_reasoning(self.engine):
                raise ValueError(
                    f"reasoning is not supported for engine {self.engine!r}"
                )
            allowed = allowed_reasoning_levels(self.engine)
        else:
            allowed = REASONING_LEVELS
        if level not in allowed:
            where = f"engine {self.engine!r}" if self.engine else "any engine"
            raise ValueError(
                f"unknown reasoning {self.reasoning!r} for {where}; "
                f"allowed values: {list(allowed)}"
            )
        self.reasoning = level
        return self


class TriggersSettings(BaseModel):
    """Top-level trigger system configuration."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = False
    default_timezone: NonEmptyStr | None = None
    server: TriggerServerSettings = Field(default_factory=TriggerServerSettings)
    # #382: webhooks with auth="none" on a non-loopback bind are an
    # unauthenticated remote-agent-run (RCE) primitive. The server refuses to
    # bind such routes unless the operator explicitly opts in here (local demos).
    allow_unauthenticated_webhooks: bool = False

    @field_validator("default_timezone")
    @classmethod
    def _validate_default_timezone(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                ZoneInfo(v)
            except (ZoneInfoNotFoundError, KeyError):
                raise ValueError(
                    f"unknown timezone {v!r}; use IANA names like 'Australia/Melbourne'"
                ) from None
        return v

    webhooks: list[WebhookConfig] = Field(default_factory=list)
    crons: list[CronConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> TriggersSettings:
        webhook_ids = [w.id for w in self.webhooks]
        if len(webhook_ids) != len(set(webhook_ids)):
            raise ValueError("webhook ids must be unique")
        webhook_paths = [w.path for w in self.webhooks]
        if len(webhook_paths) != len(set(webhook_paths)):
            raise ValueError("webhook paths must be unique")
        cron_ids = [c.id for c in self.crons]
        if len(cron_ids) != len(set(cron_ids)):
            raise ValueError("cron ids must be unique")
        return self


def parse_trigger_config(raw: dict[str, Any]) -> TriggersSettings:
    """Parse and validate a raw trigger config dict into settings."""
    return TriggersSettings.model_validate(raw)


class TriggerChatFallback(NamedTuple):
    """A trigger whose ``project`` is bound to a chat it will NOT post to."""

    kind: Literal["cron", "webhook"]
    trigger_id: str
    project: str
    project_chat_id: int


def find_trigger_chat_fallbacks(
    settings: TriggersSettings,
    *,
    default_chat_id: int | None,
    project_chat_id: Callable[[str], int | None],
) -> list[TriggerChatFallback]:
    """Return triggers with a ``project`` but no ``chat_id`` (#894).

    Such a trigger posts to the transport's default ``chat_id``, not the
    project's bound chat, which surprises anyone who expects ``project =``
    to route the run. Routing is deliberately unchanged (existing crons may
    rely on it); this only finds the cases worth a warning: the project has
    a bound chat and it differs from the default chat.
    """
    found: list[TriggerChatFallback] = []
    triggers: list[tuple[Literal["cron", "webhook"], CronConfig | WebhookConfig]] = [
        *(("cron", c) for c in settings.crons),
        *(("webhook", w) for w in settings.webhooks),
    ]
    for kind, trigger in triggers:
        if trigger.project is None or trigger.chat_id is not None:
            continue
        bound = project_chat_id(trigger.project)
        if bound is None or bound == default_chat_id:
            continue
        found.append(TriggerChatFallback(kind, trigger.id, trigger.project, bound))
    return found


def warn_trigger_chat_fallbacks(
    settings: TriggersSettings,
    *,
    default_chat_id: int | None,
    project_chat_id: Callable[[str], int | None],
    warned: set[TriggerChatFallback],
) -> list[TriggerChatFallback]:
    """Log ``trigger.cron.chat_fallback`` / ``trigger.webhook.chat_fallback``
    once per finding (#894).

    *warned* carries the findings already logged by this process, so a
    config hot-reload doesn't repeat the warning; a changed finding (new
    trigger, different project or project chat) is logged again. Returns
    the newly logged findings.
    """
    new: list[TriggerChatFallback] = []
    for item in find_trigger_chat_fallbacks(
        settings, default_chat_id=default_chat_id, project_chat_id=project_chat_id
    ):
        if item in warned:
            continue
        warned.add(item)
        new.append(item)
        id_field = "cron_id" if item.kind == "cron" else "webhook_id"
        logger.warning(
            f"trigger.{item.kind}.chat_fallback",
            **{id_field: item.trigger_id},
            project=item.project,
            project_chat_id=item.project_chat_id,
            default_chat_id=default_chat_id,
            hint=(
                f"This {item.kind} has project = {item.project!r} but no "
                "chat_id, so it posts to the transport default chat_id, not "
                "the project's chat. Add chat_id to send it to the project's "
                "chat."
            ),
        )
    return new
