"""Pydantic models for trigger configuration."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal, NamedTuple, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.types import StrictBool, StrictInt

from ..logging import get_logger
from .cron_expr import CronExpressionError, parse_cron_expression

logger = get_logger(__name__)

_SAFE_PATH_RE = re.compile(r"^/[a-zA-Z0-9/_.-]+$")

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class TriggerServerSettings(BaseModel):
    """HTTP server settings for webhook reception."""

    # #1117: never echo raw input (secrets, auth headers) into errors.
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True
    )

    host: str = "127.0.0.1"
    port: StrictInt = Field(default=9876, ge=1, le=65535)
    rate_limit: StrictInt = Field(default=60, ge=1)
    max_body_bytes: StrictInt = Field(default=1_048_576, ge=1024, le=10_485_760)
    # #1119: the server is reachable from outside this host even though it
    # binds loopback (a tunnel or reverse proxy in front). Turns the
    # ``auth = "none"`` refusal back on, whatever the bind address.
    exposed: StrictBool = False
    # #1119: header the proxy sets to the real client address (e.g.
    # ``CF-Connecting-IP``). Honoured only when ``exposed`` is on and the TCP
    # peer is loopback, for the failed-auth lockout bucket and for logs.
    trusted_proxy_header: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9-]{1,64}$"
    )


class TriggerRunOptions(BaseModel):
    """Per-trigger run options shared by crons and webhooks (#1122).

    One definition, so every option, its validator and its default exist
    once and a webhook forwards exactly what a cron does.
    """

    # #1117: never echo raw input (secrets, auth headers) into errors.
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True
    )

    engine: NonEmptyStr | None = None
    permission_mode: NonEmptyStr | None = None
    # #743: this trigger's own model / effort (reasoning) for its run only;
    # unset inherits the chat's /model and reasoning, then engine config.
    model: NonEmptyStr | None = None
    reasoning: NonEmptyStr | None = None
    # #1118: Claude's pre-approved tools for this trigger's runs only. Replaces
    # `[engines.claude] allowed_tools`; `[]` pre-approves nothing.
    allowed_tools: list[str] | None = None

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

    @field_validator("allowed_tools")
    @classmethod
    def _validate_allowed_tools(cls, v: list[str] | None) -> list[str] | None:
        # Joined with ',' into the one argv value after --allowedTools.
        if v is None:
            return v
        tools = [tool.strip() for tool in v]
        for tool in tools:
            if (
                not tool
                or tool.startswith("-")
                or "," in tool
                or not tool.isprintable()
            ):
                raise ValueError(
                    f"invalid allowed_tools entry {tool!r}: must be non-empty, "
                    "not start with '-', and contain no ',' or control characters"
                )
        return tools

    @model_validator(mode="after")
    def _validate_permission_mode(self) -> Self:
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
    def _validate_reasoning(self) -> Self:
        """#743: strict when ``engine`` is explicit, else the global table.

        With no ``engine`` the trigger resolves later (project default, #862);
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

    @model_validator(mode="after")
    def _validate_claude_only_keys(self) -> Self:
        # #1118 (D4): an engine-specific key needs an explicit engine, so it
        # is never dropped silently when the project's default engine turns
        # out to be another one.
        if self.allowed_tools is not None and self.engine != "claude":
            raise ValueError('allowed_tools needs engine = "claude"')
        return self


# #1118: modes a webhook runs in only with `allow_unsafe_permission_mode =
# true` (they skip every approval, and a webhook's prompt is built from its
# request payload).
# shortcut: Codex modes are the exec runner's (`safe`, `auto` = full auto);
# add the app-server mode names when #962/#963 land.
UNSAFE_WEBHOOK_PERMISSION_MODES: dict[str, frozenset[str]] = {
    "claude": frozenset({"bypassPermissions"}),
    "antigravity": frozenset({"full"}),
    "codex": frozenset({"auto"}),
}
# #1118: a webhook's own `permission_mode` values for engines missing from
# `VALID_PERMISSION_MODES_BY_ENGINE`. Other engines have none to set.
WEBHOOK_EXTRA_PERMISSION_MODES: dict[str, frozenset[str]] = {
    "codex": frozenset({"safe", "auto"}),
}


class WebhookConfig(TriggerRunOptions):
    """Configuration for a single webhook endpoint."""

    # #1117: never echo raw input (secrets, auth headers) into errors.
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True
    )

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

    chat_id: StrictInt | None = None
    auth: Literal["bearer", "hmac-sha256", "hmac-sha1", "none"] = "bearer"
    # #1117: SecretStr so the value never reaches a repr, a log line or a
    # validation error; read it with ``get_secret_value()`` at the auth check.
    secret: SecretStr | None = Field(default=None, min_length=1)
    # #1219: read the secret from an environment variable or an owner-only
    # file instead (one of the three); resolved into ``secret`` at parse.
    secret_env: NonEmptyStr | None = Field(
        default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    )
    secret_file: NonEmptyStr | None = None
    prompt_template: NonEmptyStr | None = None
    event_filter: NonEmptyStr | None = None

    @model_validator(mode="before")
    @classmethod
    def _resolve_secret_source(cls, data: Any) -> Any:
        """#1219: ``secret_env`` / ``secret_file`` → ``secret``. Errors name
        the variable or path and the fix, never the value."""
        if not isinstance(data, dict):
            return data
        sources = [k for k in ("secret", "secret_env", "secret_file") if data.get(k)]
        if len(sources) > 1:
            raise ValueError(f"set only one of {', '.join(sources)}")
        if sources == ["secret_env"]:
            name = str(data["secret_env"]).strip()
            value = os.environ.get(name, "").strip()
            if not value:
                raise ValueError(
                    f"secret_env: environment variable {name} is not set or empty "
                    "in Untether's environment"
                )
            return {**data, "secret": value}
        if sources == ["secret_file"]:
            return {**data, "secret": _read_secret_file(str(data["secret_file"]))}
        return data

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
    # #1118: lets this webhook's own permission_mode skip approvals (Claude
    # bypassPermissions, Antigravity full, Codex auto).
    allow_unsafe_permission_mode: StrictBool = False

    @model_validator(mode="after")
    def _validate_webhook_run_options(self) -> WebhookConfig:
        """#1118/#1122: run options apply to ``agent_run`` only; a webhook's
        own mode needs an explicit engine, an unsafe one an explicit opt-in."""
        set_options = [
            name
            for name in ("permission_mode", "model", "reasoning", "allowed_tools")
            if getattr(self, name) is not None
        ]
        if self.allow_unsafe_permission_mode:
            set_options.append("allow_unsafe_permission_mode")
        if self.action != "agent_run":
            if set_options:
                raise ValueError(
                    f"{', '.join(set_options)} only applies when action='agent_run'"
                )
            return self
        mode = self.permission_mode
        if mode is not None:
            if self.engine is None:
                raise ValueError(
                    "permission_mode on a webhook needs an explicit engine"
                )
            from ..runners.run_options import VALID_PERMISSION_MODES_BY_ENGINE

            allowed = VALID_PERMISSION_MODES_BY_ENGINE.get(
                self.engine
            ) or WEBHOOK_EXTRA_PERMISSION_MODES.get(self.engine)
            if allowed is None:
                raise ValueError(
                    f"engine {self.engine!r} has no permission_mode a webhook can set"
                )
            if mode not in allowed:
                raise ValueError(
                    f"unknown permission_mode {mode!r} for engine "
                    f"{self.engine!r}; allowed values: {sorted(allowed)}"
                )
        unsafe = mode is not None and mode in UNSAFE_WEBHOOK_PERMISSION_MODES.get(
            self.engine or "", frozenset()
        )
        if unsafe and not self.allow_unsafe_permission_mode:
            raise ValueError(
                f"permission_mode {mode!r} skips every approval for a prompt built "
                "from the request payload; set allow_unsafe_permission_mode = true "
                "to allow it"
            )
        if self.allow_unsafe_permission_mode and not unsafe:
            raise ValueError(
                "allow_unsafe_permission_mode needs an explicit engine and an "
                "unsafe permission_mode (Claude bypassPermissions, Antigravity "
                "full, Codex auto)"
            )
        return self

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


def _read_secret_file(raw_path: str) -> str:
    """#1219: a webhook secret from an owner-only regular file (stripped)."""
    path = Path(raw_path.strip()).expanduser()
    if not path.is_absolute():
        raise ValueError(f"secret_file must be an absolute path: {path}")
    try:
        info = path.stat()
    except OSError as exc:
        raise ValueError(f"secret_file {path} can't be read: {exc.strerror}") from None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"secret_file {path} is not a regular file")
    if info.st_uid != os.getuid():
        raise ValueError(
            f"secret_file {path} must be owned by the user Untether runs as"
        )
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError(
            f"secret_file {path} is readable by other users; run `chmod 600 {path}`"
        )
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        raise ValueError(f"secret_file {path} can't be read as text") from None
    if not value:
        raise ValueError(f"secret_file {path} is empty")
    return value


class CronFetchConfig(BaseModel):
    """Configuration for a cron pre-fetch step."""

    # #1117: never echo raw input (secrets, auth headers) into errors.
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True
    )

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


class CronConfig(TriggerRunOptions):
    """Configuration for a scheduled cron trigger."""

    # #1117: never echo raw input (secrets, auth headers) into errors.
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True
    )

    id: NonEmptyStr
    schedule: NonEmptyStr
    project: NonEmptyStr | None = None
    chat_id: StrictInt | None = None
    prompt: NonEmptyStr | None = None
    prompt_template: NonEmptyStr | None = None
    timezone: NonEmptyStr | None = None
    fetch: CronFetchConfig | None = None
    run_once: bool = False

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

    @field_validator("schedule")
    @classmethod
    def _validate_schedule(cls, v: str) -> str:
        # #1144: the matcher's own grammar, so a schedule that loads is one
        # that can fire; a bad one is skipped and reported per entry (#1117).
        try:
            parse_cron_expression(v)
        except CronExpressionError as exc:
            raise ValueError(f"invalid cron schedule {v!r}: {exc}") from None
        return v

    @model_validator(mode="after")
    def _validate_prompt(self) -> CronConfig:
        if not self.prompt and not self.prompt_template:
            raise ValueError("either prompt or prompt_template is required")
        return self


class TriggersSettings(BaseModel):
    """Top-level trigger system configuration."""

    # #1117: never echo raw input (secrets, auth headers) into errors.
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True
    )

    enabled: bool = False
    default_timezone: NonEmptyStr | None = None
    server: TriggerServerSettings = Field(default_factory=TriggerServerSettings)
    # #382: webhooks with auth="none" on a non-loopback bind are an
    # unauthenticated remote-agent-run (RCE) primitive. The server refuses to
    # bind such routes unless the operator explicitly opts in here (local demos).
    allow_unauthenticated_webhooks: bool = False
    # #1118 (D1 option B): "strict" gives agent_run webhooks safe defaults
    # (see `strict_webhook_problem` and `_apply_trigger_overrides`); "compat"
    # keeps 0.36.1 behaviour and warns. The default flips in 0.37.0.
    webhook_defaults: Literal["compat", "strict"] = "compat"

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


#: #1117: bearer / HMAC secrets shorter than this get a warning; with
#: ``[triggers] webhook_defaults = "strict"`` the webhook is skipped (#1118).
MIN_WEBHOOK_SECRET_LENGTH = 32

#: #1118: the release where ``webhook_defaults = "strict"`` becomes the default.
STRICT_WEBHOOK_DEFAULTS_RELEASE = "0.37.0"


def _short_secret(webhook: WebhookConfig) -> bool:
    return (
        webhook.auth != "none"
        and webhook.secret is not None
        and len(webhook.secret.get_secret_value()) < MIN_WEBHOOK_SECRET_LENGTH
    )


def strict_webhook_problem(webhook: WebhookConfig) -> str | None:
    """Why strict ``webhook_defaults`` skips *webhook*, or ``None`` (#1118)."""
    if webhook.action == "agent_run" and webhook.project is None:
        return (
            "project is required for an agent_run webhook "
            '([triggers] webhook_defaults = "strict")'
        )
    if _short_secret(webhook):
        return (
            f"secret must be at least {MIN_WEBHOOK_SECRET_LENGTH} characters "
            '([triggers] webhook_defaults = "strict"); use e.g. '
            "`openssl rand -hex 32`"
        )
    return None


def strict_webhook_changes(webhook: WebhookConfig) -> tuple[str, ...]:
    """What ``webhook_defaults = "strict"`` would change for *webhook* (#1118).

    Static (no chat or engine lookup), so it may list a change that makes no
    difference at run time, e.g. a missing ``allowed_tools`` in a chat whose
    mode already prompts. Order is stable for dedupe.
    """
    changes: list[str] = []
    if webhook.action == "agent_run":
        if webhook.project is None:
            changes.append("skipped: no project")
        # Pi and OpenCode have no mode to change (and can't set one).
        if webhook.permission_mode is None and webhook.engine in (
            None,
            "claude",
            "codex",
            "antigravity",
        ):
            changes.append(
                "runs in the engine's safe mode (Claude default, Codex safe, "
                "Antigravity Workspace) instead of the chat's"
            )
        if webhook.allowed_tools is None and webhook.engine in (None, "claude"):
            changes.append("no tools pre-approved for Claude")
    if _short_secret(webhook):
        changes.append(f"skipped: secret under {MIN_WEBHOOK_SECRET_LENGTH} characters")
    return tuple(changes)


class StrictWebhookChange(NamedTuple):
    """A webhook that ``webhook_defaults = "strict"`` would change (#1118)."""

    webhook_id: str
    changes: tuple[str, ...]


def warn_strict_webhook_changes(
    settings: TriggersSettings, *, warned: set[StrictWebhookChange]
) -> list[StrictWebhookChange]:
    """#1118 compat mode: log ``trigger.webhook.strict_defaults_change`` once
    per webhook and change set; returns the newly logged ones. Nothing in
    strict mode."""
    if settings.webhook_defaults == "strict":
        return []
    new: list[StrictWebhookChange] = []
    for webhook in settings.webhooks:
        changes = strict_webhook_changes(webhook)
        if not changes:
            continue
        item = StrictWebhookChange(webhook.id, changes)
        if item in warned:
            continue
        warned.add(item)
        new.append(item)
        logger.warning(
            "trigger.webhook.strict_defaults_change",
            webhook_id=webhook.id,
            changes=list(changes),
            changes_in=STRICT_WEBHOOK_DEFAULTS_RELEASE,
            hint=(
                'Set [triggers] webhook_defaults = "strict" now, and give the '
                "webhook its own project, permission_mode and allowed_tools."
            ),
        )
    return new


class InvalidTrigger(NamedTuple):
    """One ``[[triggers.crons]]`` / ``[[triggers.webhooks]]`` entry that
    failed validation and was skipped (#1117)."""

    kind: Literal["cron", "webhook"]
    trigger_id: str
    error: str


class ParsedTriggerConfig(NamedTuple):
    """The valid trigger settings plus the entries that were skipped."""

    settings: TriggersSettings
    invalid: tuple[InvalidTrigger, ...]


def format_trigger_error(exc: BaseException) -> str:
    """A one-line, secret-free summary of a trigger config error (#1117).

    Built from pydantic's structured errors without their ``input`` (the
    raw TOML values, which can hold a webhook secret or an auth header), so
    it is safe for logs, the issue-watcher and Telegram.
    """
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors(include_url=False, include_input=False):
            loc = ".".join(str(part) for part in err.get("loc", ()))
            msg = err.get("msg", "invalid value")
            parts.append(f"{loc}: {msg}" if loc else msg)
        return "; ".join(parts) or "invalid trigger config"
    return str(exc)


def _entry_label(kind: str, index: int, entry: object) -> str:
    raw_id = entry.get("id") if isinstance(entry, dict) else None
    if isinstance(raw_id, str) and raw_id.strip():
        return raw_id.strip()[:64]
    return f"{kind}s[{index}]"


def parse_trigger_config_isolated(raw: dict[str, Any]) -> ParsedTriggerConfig:
    """Parse trigger config, isolating each cron / webhook entry (#1117).

    Each ``[[triggers.crons]]`` / ``[[triggers.webhooks]]`` entry is
    validated on its own: a bad entry is skipped and reported in
    ``invalid``, the rest still load. Block-level problems still raise
    (``[triggers.server]``, unknown ``[triggers]`` keys, duplicate ids or
    paths across the valid entries), as one bad value there affects every
    trigger.
    """
    data = dict(raw)
    # A bad value fails the block below; only the exact "strict" applies here.
    strict = data.get("webhook_defaults") == "strict"
    invalid: list[InvalidTrigger] = []
    models: tuple[tuple[Literal["cron", "webhook"], str, type[BaseModel]], ...] = (
        ("cron", "crons", CronConfig),
        ("webhook", "webhooks", WebhookConfig),
    )
    for kind, key, model in models:
        entries = data.get(key)
        if not isinstance(entries, list):
            continue  # wrong type → the block-level parse reports it
        valid: list[BaseModel] = []
        for index, entry in enumerate(entries):
            try:
                parsed = model.model_validate(entry)
            except ValidationError as exc:
                invalid.append(
                    InvalidTrigger(
                        kind,
                        _entry_label(kind, index, entry),
                        format_trigger_error(exc),
                    )
                )
                continue
            # #1118: strict webhook rules skip only that entry too.
            problem = (
                strict_webhook_problem(parsed)
                if strict and isinstance(parsed, WebhookConfig)
                else None
            )
            if problem is not None:
                invalid.append(InvalidTrigger(kind, parsed.id, problem))
                continue
            valid.append(parsed)
        data[key] = valid
    return ParsedTriggerConfig(TriggersSettings.model_validate(data), tuple(invalid))


def parse_trigger_config(raw: dict[str, Any]) -> TriggersSettings:
    """Parse trigger config into settings, skipping invalid entries (#1117).

    Use :func:`parse_trigger_config_isolated` to learn which entries were
    skipped. Block-level errors still raise ``ValidationError``.
    """
    return parse_trigger_config_isolated(raw).settings


def log_invalid_triggers(
    invalid: Sequence[InvalidTrigger], *, phase: Literal["startup", "reload"]
) -> None:
    """Log ``triggers.trigger_invalid`` (ERROR) per skipped entry (#1117).

    ``error`` is the secret-free :func:`format_trigger_error` summary.
    """
    for item in invalid:
        logger.error(
            "triggers.trigger_invalid",
            kind=item.kind,
            trigger_id=item.trigger_id,
            error=item.error,
            phase=phase,
        )


def find_short_webhook_secrets(settings: TriggersSettings) -> list[str]:
    """Ids of bearer / HMAC webhooks whose secret is under
    :data:`MIN_WEBHOOK_SECRET_LENGTH` characters (#1117)."""
    return [wh.id for wh in settings.webhooks if _short_secret(wh)]


def warn_short_webhook_secrets(settings: TriggersSettings) -> list[str]:
    """Log ``triggers.webhook.short_secret`` for each short secret (#1117).

    A warning only in 0.36.1 (the config still loads); never logs the
    secret or its length. Returns the flagged webhook ids.
    """
    short = find_short_webhook_secrets(settings)
    for webhook_id in short:
        logger.warning(
            "triggers.webhook.short_secret",
            webhook_id=webhook_id,
            min_length=MIN_WEBHOOK_SECRET_LENGTH,
            hint=(
                f"Use a random secret of at least {MIN_WEBHOOK_SECRET_LENGTH} "
                "characters (e.g. `openssl rand -hex 32`). Short secrets are "
                'skipped with [triggers] webhook_defaults = "strict", the '
                f"default from {STRICT_WEBHOOK_DEFAULTS_RELEASE}."
            ),
        )
    return short


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
