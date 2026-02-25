"""Pydantic models for trigger configuration."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
from pydantic.types import StrictInt

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
    prompt_template: NonEmptyStr
    event_filter: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _require_secret_for_auth(self) -> WebhookConfig:
        if self.auth != "none" and not self.secret:
            raise ValueError(f"secret is required when auth={self.auth!r}")
        return self


class CronConfig(BaseModel):
    """Configuration for a scheduled cron trigger."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: NonEmptyStr
    schedule: NonEmptyStr
    project: NonEmptyStr | None = None
    engine: NonEmptyStr | None = None
    chat_id: StrictInt | None = None
    prompt: NonEmptyStr


class TriggersSettings(BaseModel):
    """Top-level trigger system configuration."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = False
    server: TriggerServerSettings = Field(default_factory=TriggerServerSettings)
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
