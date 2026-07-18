from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal

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
from pydantic.types import StrictInt
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import TomlConfigSettingsSource

from .config import (
    HOME_CONFIG_PATH,
    ConfigError,
    ProjectConfig,
    ProjectsConfig,
)
from .config_migrations import migrate_config_file
from .logging import get_logger

logger = get_logger(__name__)


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _normalize_project_path(value: str, *, config_path: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    return path


class TelegramTopicsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = False
    scope: Literal["auto", "main", "projects", "all"] = "auto"


class TelegramFilesSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    max_upload_bytes: ClassVar[int] = 20 * 1024 * 1024
    max_download_bytes: ClassVar[int] = 50 * 1024 * 1024

    enabled: bool = False
    auto_put: bool = True
    auto_put_mode: Literal["upload", "prompt"] = "upload"
    uploads_dir: NonEmptyStr = "incoming"
    allowed_user_ids: list[StrictInt] = Field(default_factory=list)
    # Secret/credential patterns never delivered from the outbox. Broadened
    # for #628 recursive directory (zip) delivery, which makes shipping a
    # nested secret far easier than the old flat scan — matched right-to-left
    # by ``deny_reason`` (PurePosixPath.match), so bare names like ``.env``
    # also match nested members.
    deny_globs: list[NonEmptyStr] = Field(
        default_factory=lambda: [
            ".git/**",
            ".env",
            "**/.env",
            "**/.env.*",
            ".envrc",
            "**/.envrc",
            "**/*.pem",
            "**/*.key",
            "**/id_rsa",
            "**/id_ed25519",
            "**/.ssh/**",
            "**/.netrc",
            "**/.npmrc",
            "**/.pypirc",
        ]
    )

    # Outbox: agent-initiated file delivery
    outbox_enabled: bool = True
    outbox_dir: NonEmptyStr = ".untether-outbox"
    outbox_max_files: int = Field(default=10, ge=1, le=50)
    outbox_cleanup: bool = True
    # #524: surface "skipped" outbox entries (directories, oversized files,
    # deny-globbed files, …) to the user in chat. Previously these were
    # logged as ``outbox.skipped`` only — the agent's "I've prepared the
    # guides folder for you" final message became a silent lie.
    outbox_notify_skipped: bool = True
    # #628: deliver skipped outbox DIRECTORIES (e.g. an agent's screenshots/
    # folder). "off" (default) keeps the #600 archive-to-.skipped/ behaviour;
    # "zip" bundles each directory's deliverable members into a single
    # <name>.zip Telegram document. Recursive deny-glob, symlink, per-member
    # size, member-count, and total-zip-size caps are all applied to the
    # contents. Recurse-and-send-each-file is intentionally NOT offered
    # (flooding risk on large trees); zip keeps delivery to one bounded
    # attachment per directory. Directories with no deliverable members (all
    # denied/empty) or an oversize zip fall back to the #600 archive.
    outbox_deliver_directories: Literal["off", "zip"] = "off"

    @field_validator("uploads_dir")
    @classmethod
    def _validate_uploads_dir(cls, value: str) -> str:
        if Path(value).is_absolute():
            raise ValueError("files.uploads_dir must be a relative path")
        return value

    @field_validator("outbox_dir")
    @classmethod
    def _validate_outbox_dir(cls, value: str) -> str:
        if Path(value).is_absolute():
            raise ValueError("files.outbox_dir must be a relative path")
        return value


class TelegramTransportSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # #318: fields in this set require a process restart to take effect.
    # Everything else hot-reloads via `TelegramBridgeConfig.update_from()`
    # (#286).  The hot-reload path in `telegram/loop.py:handle_reload` reads
    # this ClassVar rather than duplicating the list inline, and the
    # `/config` menu suffixes restart-required settings with 🔄 so agents
    # and users can tell which edits need a restart before they try them.
    RESTART_REQUIRED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "bot_token",
            "chat_id",
            "session_mode",
            "topics",
            "message_overflow",
        }
    )

    # #196: SecretStr masks the value in repr()/str()/tracebacks and any
    # accidental structlog serialisation.  Access the raw value via
    # bot_token.get_secret_value() at the transport boundary.  The token is
    # additionally redacted from log URLs by _redact_event_dict (#190).
    bot_token: SecretStr
    chat_id: StrictInt
    allowed_user_ids: list[StrictInt] = Field(default_factory=list)
    # #377: opt-in escape hatch for demos/dev. When the allowlist is
    # empty AND this flag is False, startup fails with a ConfigError so
    # accidentally-public bots can't slip into production. Setting this
    # to True is logged at INFO on every boot so the deviation is
    # visible in journalctl.
    allow_any_user: bool = False
    message_overflow: Literal["trim", "split"] = "split"
    voice_transcription: bool = False
    voice_max_bytes: StrictInt = 10 * 1024 * 1024
    voice_transcription_model: NonEmptyStr = "gpt-4o-mini-transcribe"
    voice_transcription_base_url: NonEmptyStr | None = None
    # #378: SecretStr (parity with bot_token from #196) — masks repr()/str()/
    # tracebacks/structlog. Access the raw value via .get_secret_value() at the
    # transport boundary (telegram/loop.py before passing to OpenAI SDK).
    voice_transcription_api_key: SecretStr | None = None
    voice_show_transcription: bool = True
    # #381: optional SSRF allowlist (CIDR / bare-IP strings) for
    # voice_transcription_base_url — lets operators opt in to private endpoints
    # (e.g. an Azure private-link range) that would otherwise be blocked.
    voice_transcription_url_allowlist: list[str] = Field(default_factory=list)
    session_mode: Literal["stateless", "chat"] = "stateless"
    show_resume_line: bool = True
    forward_coalesce_s: float = Field(default=1.0, ge=0)
    media_group_debounce_s: float = Field(default=1.0, ge=0)
    topics: TelegramTopicsSettings = Field(default_factory=TelegramTopicsSettings)
    files: TelegramFilesSettings = Field(default_factory=TelegramFilesSettings)

    @field_validator("bot_token", mode="after")
    @classmethod
    def _validate_bot_token_not_empty(cls, v: SecretStr) -> SecretStr:
        """Preserve the pre-#196 NonEmptyStr contract.  SecretStr bypasses
        str_strip_whitespace, so whitespace-only values would otherwise pass
        the schema and fail at connect time with a less-helpful error.
        Returns the *stripped* value so accidental padding (`" token "`) doesn't
        reach the Telegram API as `https://api.telegram.org/bot %20token%20/`."""
        token = v.get_secret_value().strip()
        if not token:
            raise ValueError("bot_token must not be empty")
        return SecretStr(token)

    @field_validator("voice_transcription_api_key", mode="after")
    @classmethod
    def _validate_voice_key_not_empty(cls, v: SecretStr | None) -> SecretStr | None:
        """#378: preserve the pre-SecretStr `NonEmptyStr | None` contract.
        Empty / whitespace-only strings round-trip to None so downstream code
        can use a simple `is not None` (or truthy) check at the call site.

        #594: additionally reject header-illegal characters at config load.
        An embedded newline (e.g. two concatenated keys emitted by a
        credential manager) or other control character reaches httpx as an
        illegal ``Authorization`` header and surfaces hours later as a
        misleading ``APIConnectionError("Connection error.")`` on every
        transcription attempt — fail fast with a diagnosable error instead
        (same fail-fast precedent as the #381 base-url SSRF validator)."""
        if v is None:
            return None
        key = v.get_secret_value().strip()
        if not key:
            return None
        if not key.isprintable():
            raise ValueError(
                "voice_transcription_api_key contains control characters "
                "(embedded newline/tab?) — check for concatenated or "
                "line-wrapped key material"
            )
        try:
            key.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise ValueError(
                "voice_transcription_api_key contains characters that cannot "
                "be sent in an HTTP Authorization header"
            ) from exc
        return SecretStr(key)

    @model_validator(mode="after")
    def _validate_voice_base_url_ssrf(self) -> TelegramTransportSettings:
        """#381: fast-fail at config load for an obviously-unsafe voice
        transcription endpoint (non-http scheme, or a private/reserved IP
        literal) and for a malformed allowlist. Hostname-based URLs that
        resolve to a private IP are caught later (async, with DNS) at the
        chokepoint in ``transcribe_voice``."""
        # Lazy import to avoid any import-time cycle through the triggers pkg.
        from .triggers.ssrf import SSRFError, parse_networks, validate_url

        try:
            networks = parse_networks(self.voice_transcription_url_allowlist)
        except ValueError as exc:
            raise ValueError(
                "[transports.telegram] voice_transcription_url_allowlist has an "
                f"invalid CIDR/IP entry: {exc}"
            ) from exc

        if self.voice_transcription_base_url is not None:
            try:
                validate_url(self.voice_transcription_base_url, allowlist=networks)
            except SSRFError as exc:
                raise ValueError(
                    "[transports.telegram] voice_transcription_base_url is not "
                    f"permitted: {exc}"
                ) from exc
        return self

    @model_validator(mode="after")
    def _validate_allowed_user_ids_or_optin(self) -> TelegramTransportSettings:
        """#377: refuse to start with no user allowlist unless the operator
        explicitly opts out.

        ``allowed_user_ids = []`` previously degraded to "any Telegram user
        who knows the bot username can send commands" with only a runtime
        warning. That's an insecure default — it shipped real production
        bots that were silently public. The fix promotes the warning to a
        hard ConfigError at config-load time. Operators who actually want
        an open bot (demos, hackathons, dev) opt in by setting
        ``allow_any_user = true``.
        """
        if not self.allowed_user_ids and not self.allow_any_user:
            raise ValueError(
                "[transports.telegram] allowed_user_ids is empty — bot would "
                "accept commands from anyone who knows its username. Set a "
                "non-empty list of Telegram user IDs, or pass "
                "`allow_any_user = true` to opt in to an open bot (dev/demo "
                "only)."
            )
        return self


class TransportsSettings(BaseModel):
    telegram: TelegramTransportSettings

    model_config = ConfigDict(extra="allow")


class PluginsSettings(BaseModel):
    enabled: list[NonEmptyStr] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class ProjectSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    path: NonEmptyStr
    worktrees_dir: NonEmptyStr = ".worktrees"
    default_engine: NonEmptyStr | None = None
    worktree_base: NonEmptyStr | None = None
    chat_id: StrictInt | None = None


class CostBudgetSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = False
    max_cost_per_run: float | None = Field(default=None, ge=0)
    max_cost_per_day: float | None = Field(default=None, ge=0)
    warn_at_pct: int = Field(default=70, ge=0, le=100)
    auto_cancel: bool = False


class LoopSettings(BaseModel):
    """Untether-side observation of Claude Code's session-scoped scheduling
    tools (CronCreate, ScheduleWakeup) so /loop and dynamic-mode wakeups
    keep firing after the subprocess exits.  Off by default — opt-in
    per-chat via /config → 🔁 Loop mode (#289).

    Cost limits are NOT in [loop]; they live in [cost_budget] and apply
    to loop fires automatically.  The caps below are runaway-safety
    only.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = False
    inline_threshold_seconds: int = Field(default=300, ge=0)
    redundancy_check_interval: int = Field(default=30, ge=1)
    max_iterations: int = Field(default=20, ge=1, le=10000)
    max_total_duration_hours: int = Field(default=4, ge=1, le=168)
    min_interval_seconds: int = Field(default=60, ge=60)
    expiry_days: int = Field(default=7, ge=1, le=30)


class FooterSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    show_api_cost: bool = True
    show_subscription_usage: bool = False


class PreambleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = True
    text: str | None = None


class AutoContinueSettings(BaseModel):
    """Mitigate Claude Code bug #34142/#30333: session exits after receiving
    tool results without letting Claude process them.  When detected, Untether
    auto-resumes the session so the user doesn't have to manually continue."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = True
    max_retries: int = Field(default=1, ge=0, le=3)
    # #596: an empty-result no-op resume (0 turns / $0 / empty answer, ok=True)
    # is the same upstream turn-state family — resuming a session whose prior
    # turn ended on a tool_result (last_event_type=user) can return an
    # immediate empty result on the first attempt, forcing the user to
    # manually re-nudge. When enabled, Untether auto-resends the original
    # prompt once (same session) instead of asking the user to resend.
    # Single-shot: if the retry is also empty, the user-visible resend notice
    # is shown and no further retry fires.
    resend_empty_resume: bool = True
    # #631 (W1): when empty-result retry is exhausted, retry as a FRESH session
    # instead of same-session to circumvent the poisoned-session state.
    empty_resume_fresh: bool = True
    # #631 (W2): mark sessions force-killed after a tool result as unsafe to
    # resume — quarantine them so resuming on the next message spins up a new
    # session rather than re-entering the poisoned state.
    quarantine_on_forced_teardown: bool = True
    # #633 (W4): never resume a session whose previous subprocess is still
    # alive. rc7's quarantine-and-fresh recovers AFTER a session is poisoned;
    # this prevents the poisoning. Before spawning a `--resume`, wait (bounded)
    # for the prior owner to exit; if it does not, quarantine and start fresh
    # rather than racing it. Set False for exact pre-rc8 behaviour.
    serialize_session_owner: bool = True
    # Upper bound on that wait. Condition-based, so it resolves the instant the
    # prior subprocess exits — this is only the give-up point. Kept comfortably
    # above the post-result SIGTERM grace so a normal teardown wins the race.
    session_handoff_timeout_s: float = Field(default=30.0, ge=0.0, le=300.0)


class WatchdogSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    liveness_timeout: float = Field(default=600.0, ge=60, le=3600)
    stall_auto_kill: bool = False
    stall_repeat_seconds: float = Field(default=180.0, ge=30, le=600)
    # #590: after an engine subprocess exits (including clean rc=0), sweep
    # surviving process-group members and captured descendant PIDs — the
    # Claude CLI leaks MCP node children on exit fleet-wide (1/run on sl,
    # 11-37/day on nsd, driving the #589 OOM kills). Killing everything the
    # session spawned at session end is the #573 lifecycle policy (OWASP
    # ASI08/ASI10); it also terminates user-backgrounded Bash tasks that
    # outlive the run. Set false to keep survivors.
    reap_orphans: bool = True
    tool_timeout: float = Field(default=600.0, ge=60, le=7200)
    mcp_tool_timeout: float = Field(default=900.0, ge=60, le=7200)
    subagent_timeout: float = Field(default=900.0, ge=60, le=7200)

    # Engine-agnostic "stuck after tool_result" detector (issue #322).
    # Default threshold of 300s matches undici's non-configurable 5-min
    # idle-body timeout, which is the root-cause mechanism behind mcp-remote
    # wedges talking to Cloudflare MCPs.
    detect_stuck_after_tool_result: bool = False
    stuck_after_tool_result_timeout: float = Field(default=300.0, ge=60, le=1800)
    stuck_after_tool_result_recovery_enabled: bool = True
    stuck_after_tool_result_recovery_delay: float = Field(default=60.0, ge=10, le=600)

    # MCP catalog observability + proactive refresh (#365).
    # ``detect_catalog_staleness`` is a zero-risk logging hook: when Claude
    # Code's ``system.init`` event reports any configured MCP server with a
    # non-``connected`` status, Untether emits a ``catalog_staleness.detected``
    # structlog warning once per (session, server) pair so operators can
    # measure the "MCPs flapping" UX independent of real #322 watchdog fires.
    # Default ON — observability only, no recovery action.
    detect_catalog_staleness: bool = True
    # ``notify_catalog_refresh`` is opt-in experimental: after each
    # ``tool_result`` Untether posts an ``mcp_status`` control_request to
    # Claude Code's stdin. This is the parent→CLI primitive documented in
    # Anthropic's ``claude-agent-sdk-python`` (``get_mcp_status()``); whether
    # it causes Claude Code to re-probe its catalog is empirical, so default
    # OFF until staging measurement confirms the UX benefit.
    notify_catalog_refresh: bool = False
    # #497 debounce: minimum seconds between consecutive
    # ``catalog.refresh_sent`` fires per session. Without this, a session
    # with many rapid ``tool_result`` batches (observed: 183 fires in a
    # single 'scout' run) generates a flood of ``mcp_status`` requests on
    # the runner's stdin. Set to 0 to disable the debounce and restore the
    # pre-#497 behaviour of one fire per ``tool_result`` batch.
    catalog_refresh_min_interval_s: float = Field(default=5.0, ge=0.0, le=60.0)

    # Pre-spawn RAM guard (#350) — refuse or warn on new engine subprocesses
    # when the host is near-OOM. 0 disables that tier; set both to 0 to
    # disable the guard entirely. Warn threshold MUST be > block threshold
    # when both are set, enforced by a model_validator below (see #350).
    prespawn_ram_warn_mb: int = Field(default=2000, ge=0, le=65536)
    prespawn_ram_block_mb: int = Field(default=500, ge=0, le=65536)

    # #589: the guard above is per-spawn and count-blind, so N concurrent chats
    # can each individually pass the free-RAM check and then collectively
    # exhaust the host. On nsd the OOM killer struck untether.service 5x in one
    # evening and killed two live Claude runs (rc=-9), each holding 10-17 MCP
    # node children.
    #
    # Reserve headroom for the runs already in flight: the effective block
    # threshold becomes
    #     block_mb + prespawn_ram_per_run_reserve_mb * live_engine_subprocesses
    # so the bar rises as concurrency does. 0 disables the scaling and restores
    # the flat pre-0.35.4rc8 threshold.
    prespawn_ram_per_run_reserve_mb: int = Field(default=750, ge=0, le=65536)
    # Hard ceiling on concurrent engine subprocesses. Independent of free RAM —
    # useful on small VPS hosts where the leak rate matters more than the
    # instantaneous reading. 0 = unlimited (default; no behaviour change).
    max_concurrent_engine_runs: int = Field(default=0, ge=0, le=64)

    # #438: user-configurable Claude SSE-stream watchdog. Sets
    # ``CLAUDE_STREAM_IDLE_TIMEOUT_MS`` for the Claude subprocess (via
    # ``setdefault`` — shell-set values still win). Default 300000 ms (5 min)
    # matches the upstream undici idle-body timeout and #342's reasoning.
    # Long-form opus 4.7 1M plan-mode generations can legitimately idle the
    # SSE stream past 5 min; deployments that hit upstream Anthropic API
    # stalls (#438) can raise this to 600000-900000 to ride out longer
    # silences before Untether reports the run failed. Range 30s-30min.
    claude_stream_idle_timeout_ms: int = Field(default=300_000, ge=30_000, le=1_800_000)

    # #333: post-result idle timeout for Claude bidirectional sessions.
    # Claude Code in stream-json + permission-mode keeps stdin open after
    # emitting a `result` event so multi-turn sessions don't re-spawn. In
    # practice this leaves a 400 MB RSS subprocess + ~200 TCP sockets
    # idling for tens of minutes between user prompts. After
    # `post_result_idle_timeout` seconds with no new event we close the
    # subprocess's stdin so the CLI exits gracefully (rc=0). The auto-
    # continue safety gate already excludes ``last_event_type == "result"``
    # so the clean exit will not phantom-resume the session. Pause/resume
    # via Telegram is unaffected — the resume token is preserved on the
    # progress tracker. Set ``post_result_idle_enabled = false`` to keep
    # the legacy "stay alive forever" behaviour (e.g. for users who pipe
    # successive turns within seconds and want to skip the spawn cost).
    # Range 30s-1h.
    post_result_idle_enabled: bool = True
    post_result_idle_timeout: float = Field(default=600.0, ge=30, le=3600)

    # #592: pre-first-result silence cap. A run whose stream goes silent
    # forever BEFORE any result event was previously never auto-cancelled —
    # the post-result watchdog only arms after a result, and the liveness
    # machinery never escalates an alive-but-idle process (8-day zombie
    # Claude subprocess on mac). After this many seconds with zero stream
    # output, no pending permission/ask requests, and no live background
    # work, the subprocess is killed (descendant-aware) and the run ends
    # with an explanatory error. Default 1h accommodates subscription
    # rate-limit waits; 0 disables. Range 0-24h.
    pre_result_silence_timeout: float = Field(default=3600.0, ge=0, le=86400)

    # #591: short-circuit grace for the post-result subcountdown. Once the
    # JSONL reader is done and nothing references the session (no pending
    # control/ask requests, no live background work), the subprocess is only
    # being held open by lingering MCP children — SIGTERM after this grace
    # instead of the full post_result_idle_timeout so the process (and its
    # RSS/TCP) is released promptly. 0 disables the shortcut. Range 0-600s.
    post_result_limbo_grace: float = Field(default=60.0, ge=0, le=600)

    # #481: grace window for fresh Bash/BashOutput tool calls. When the most
    # recent action is Bash/BashOutput/KillShell and its age is less than
    # bash_grace_seconds, ProgressEdits._stall_monitor suppresses the Telegram
    # stall warning (the command may still be in its startup phase / first
    # poll cycle). Range 5s-300s. Logged as
    # ``progress_edits.stall_bash_grace_suppressed`` per suppression.
    bash_grace_seconds: float = Field(default=60.0, ge=5, le=300)

    @model_validator(mode="after")
    def _validate_prespawn_ram_ordering(self) -> WatchdogSettings:
        # When both tiers are active, warn must sit above block — otherwise
        # the warn tier is unreachable (spawn would hit block first).
        # Using 0 disables a tier, which is a legitimate config.
        if (
            self.prespawn_ram_warn_mb > 0
            and self.prespawn_ram_block_mb > 0
            and self.prespawn_ram_warn_mb <= self.prespawn_ram_block_mb
        ):
            raise ValueError(
                "prespawn_ram_warn_mb must be > prespawn_ram_block_mb when both are active "
                f"(got warn={self.prespawn_ram_warn_mb}, block={self.prespawn_ram_block_mb})"
            )
        return self


class ProgressSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    verbosity: Literal["compact", "verbose"] = "compact"
    max_actions: int = Field(default=5, ge=0, le=50)
    min_render_interval: float = Field(default=2.0, ge=0, le=30)
    group_chat_rps: float = Field(default=20.0 / 60.0, gt=0, le=10)
    # #481: heartbeat tick cadence for the long-running-action elapsed-time
    # tail and the post-result closing-message poller. Distinct from the
    # stall-monitor cadence (60s) because lowering that would silently
    # break stall_repeat_seconds=180 ≈ 3-tick math and the wider stall test
    # corpus. The stall monitor's loop sleeps min(heartbeat_interval,
    # stall_check_interval) and only runs the threshold check at the slower
    # cadence. Range 5s-120s.
    heartbeat_interval: float = Field(default=30.0, ge=5, le=120)


_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class SecuritySettings(BaseModel):
    """Runtime security knobs (#361, #409).

    ``env_audit`` enables a one-shot ``/proc/<pid>/environ`` sample on
    Claude session start. Disallowed names emit a structured warning so
    the operator can see when host env leaks past
    :func:`utils.env_policy.filtered_env`.

    ``env_extra_allow`` / ``env_extra_prefix_allow`` (#409) extend the
    built-in subprocess-env allowlist with per-deployment names so users
    can thread credential-manager tokens (1Password, Doppler, Vault,
    Infisical, …) without forking ``utils/env_policy.py``.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    env_audit: bool = True
    # #409: user-extensible engine-subprocess env allowlist. Each entry
    # must look like a POSIX env var name (uppercase, digits, underscore;
    # must not start with a digit). Empty/whitespace strings are rejected
    # so a stray TOML edit doesn't silently widen the allowlist.
    env_extra_allow: list[str] = Field(default_factory=list)
    env_extra_prefix_allow: list[str] = Field(default_factory=list)

    @field_validator("env_extra_allow", "env_extra_prefix_allow", mode="after")
    @classmethod
    def _validate_env_names(cls, v: list[str]) -> list[str]:
        """Each entry must look like a POSIX env-var name.

        Trailing wildcards / glob chars are NOT supported — prefix matches
        already cover families (``VAULT_*`` is configured as ``"VAULT_"``).
        """
        cleaned: list[str] = []
        for entry in v:
            if not isinstance(entry, str):
                raise ValueError(
                    f"env allowlist entries must be strings (got {type(entry).__name__})"
                )
            stripped = entry.strip()
            if not stripped:
                raise ValueError("env allowlist entries must not be empty")
            if not _ENV_NAME_RE.match(stripped):
                raise ValueError(
                    f"invalid env name {entry!r} — must match [A-Z_][A-Z0-9_]* "
                    "(uppercase letters, digits, underscores; cannot start with a digit)"
                )
            cleaned.append(stripped)
        return cleaned


class UntetherSettings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="allow",
        env_prefix="UNTETHER__",
        env_nested_delimiter="__",
        str_strip_whitespace=True,
    )

    watch_config: bool = False
    default_engine: NonEmptyStr = "codex"
    default_project: NonEmptyStr | None = None
    projects: dict[str, ProjectSettings] = Field(default_factory=dict)

    transport: NonEmptyStr = "telegram"
    transports: TransportsSettings

    plugins: PluginsSettings = Field(default_factory=PluginsSettings)
    cost_budget: CostBudgetSettings = Field(default_factory=CostBudgetSettings)
    loop: LoopSettings = Field(default_factory=LoopSettings)
    footer: FooterSettings = Field(default_factory=FooterSettings)
    preamble: PreambleSettings = Field(default_factory=PreambleSettings)
    progress: ProgressSettings = Field(default_factory=ProgressSettings)
    watchdog: WatchdogSettings = Field(default_factory=WatchdogSettings)
    auto_continue: AutoContinueSettings = Field(default_factory=AutoContinueSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_telegram_keys(cls, data: Any) -> Any:
        if isinstance(data, dict) and ("bot_token" in data or "chat_id" in data):
            raise ValueError(
                "Move bot_token/chat_id under [transports.telegram] "
                'and set transport = "telegram".'
            )
        return data

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    def engine_config(self, engine_id: str, *, config_path: Path) -> dict[str, Any]:
        extra = self.model_extra or {}
        # Support both [engines.claude] (nested) and [claude] (flat) TOML layouts
        engines = extra.get("engines")
        if isinstance(engines, dict):
            raw = engines.get(engine_id)
        else:
            raw = extra.get(engine_id)
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise ConfigError(
                f"Invalid `{engine_id}` config in {config_path}; expected a table."
            )
        return raw

    def transport_config(
        self, transport_id: str, *, config_path: Path
    ) -> dict[str, Any]:
        if transport_id == "telegram":
            return self.transports.telegram.model_dump()
        extra = self.transports.model_extra or {}
        raw = extra.get(transport_id)
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise ConfigError(
                f"Invalid `transports.{transport_id}` in {config_path}; "
                "expected a table."
            )
        return raw

    def to_projects_config(
        self,
        *,
        config_path: Path,
        engine_ids: Iterable[str],
        reserved: Iterable[str] = ("cancel",),
    ) -> ProjectsConfig:
        default_project = self.default_project
        default_chat_id = self.transports.telegram.chat_id

        reserved_lower = {value.lower() for value in reserved}
        engine_map = {engine.lower(): engine for engine in engine_ids}
        projects: dict[str, ProjectConfig] = {}
        chat_map: dict[int, str] = {}

        skipped: list[str] = []
        for raw_alias, entry in self.projects.items():
            alias = raw_alias
            alias_key = alias.lower()
            if alias_key in engine_map or alias_key in reserved_lower:
                logger.error(
                    "project.skipped.reserved_alias",
                    alias=alias,
                    config_path=str(config_path),
                    reason="aliases must not match engine ids or reserved commands",
                )
                skipped.append(alias)
                continue
            if alias_key in projects:
                logger.error(
                    "project.skipped.duplicate_alias",
                    alias=alias,
                    config_path=str(config_path),
                )
                skipped.append(alias)
                continue

            path = _normalize_project_path(entry.path, config_path=config_path)

            worktrees_dir = Path(entry.worktrees_dir).expanduser()

            default_engine = None
            if entry.default_engine is not None:
                engine_map_lower = {e.lower(): e for e in engine_ids}
                resolved = engine_map_lower.get(entry.default_engine.lower())
                if resolved is None:
                    available = ", ".join(sorted(engine_map_lower.values()))
                    logger.error(
                        "project.skipped.unknown_engine",
                        alias=alias,
                        engine=entry.default_engine,
                        available=available,
                        config_path=str(config_path),
                    )
                    skipped.append(alias)
                    continue
                default_engine = resolved

            worktree_base = entry.worktree_base

            chat_id = entry.chat_id
            if chat_id is not None:
                if chat_id == default_chat_id:
                    logger.error(
                        "project.skipped.chat_id_matches_transport",
                        alias=alias,
                        chat_id=chat_id,
                        config_path=str(config_path),
                        reason="must not match transports.telegram.chat_id",
                    )
                    skipped.append(alias)
                    continue
                if chat_id in chat_map:
                    existing = chat_map[chat_id]
                    logger.error(
                        "project.skipped.duplicate_chat_id",
                        alias=alias,
                        chat_id=chat_id,
                        existing_alias=existing,
                        config_path=str(config_path),
                    )
                    skipped.append(alias)
                    continue
                chat_map[chat_id] = alias_key

            projects[alias_key] = ProjectConfig(
                alias=alias,
                path=path,
                worktrees_dir=worktrees_dir,
                default_engine=default_engine,
                worktree_base=worktree_base,
                chat_id=chat_id,
            )

        if skipped:
            logger.warning(
                "projects.config.skipped_projects",
                skipped=skipped,
                loaded=len(projects),
                config_path=str(config_path),
            )

        if default_project is not None:
            default_key = default_project.lower()
            if default_key not in projects:
                logger.error(
                    "projects.config.invalid_default_project",
                    default_project=default_project,
                    config_path=str(config_path),
                    reason="no matching project alias found (may have been skipped)",
                )
                default_project = None
            else:
                default_project = default_key

        return ProjectsConfig(
            projects=projects,
            default_project=default_project,
            chat_map=chat_map,
        )


def load_settings(path: str | Path | None = None) -> tuple[UntetherSettings, Path]:
    cfg_path = _resolve_config_path(path)
    _ensure_config_file(cfg_path)
    migrate_config_file(cfg_path)
    return _load_settings_from_path(cfg_path), cfg_path


def load_settings_if_exists(
    path: str | Path | None = None,
) -> tuple[UntetherSettings, Path] | None:
    cfg_path = _resolve_config_path(path)
    if cfg_path.exists():
        if not cfg_path.is_file():
            raise ConfigError(
                f"Config path {cfg_path} exists but is not a file."
            ) from None
        migrate_config_file(cfg_path)
        return _load_settings_from_path(cfg_path), cfg_path
    return None


def validate_settings_data(
    data: dict[str, Any], *, config_path: Path
) -> UntetherSettings:
    try:
        return UntetherSettings.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid config in {config_path}: {exc}") from exc


def require_telegram(settings: UntetherSettings, config_path: Path) -> tuple[str, int]:
    if settings.transport != "telegram":
        raise ConfigError(
            f"Unsupported transport {settings.transport!r} in {config_path} "
            "(telegram only for now)."
        )
    tg = settings.transports.telegram
    # #196: unwrap SecretStr at the transport boundary.
    return tg.bot_token.get_secret_value(), tg.chat_id


def _resolve_config_path(path: str | Path | None) -> Path:
    if path:
        return Path(path).expanduser()
    env_path = os.environ.get("UNTETHER_CONFIG_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return HOME_CONFIG_PATH


def _ensure_config_file(cfg_path: Path) -> None:
    if cfg_path.exists() and not cfg_path.is_file():
        raise ConfigError(f"Config path {cfg_path} exists but is not a file.") from None
    if not cfg_path.exists():
        raise ConfigError(f"Missing config file {cfg_path}.") from None


def _load_settings_from_path(cfg_path: Path) -> UntetherSettings:
    cfg = dict(UntetherSettings.model_config)
    cfg["toml_file"] = cfg_path
    Bound = type(
        "UntetherSettingsBound",
        (UntetherSettings,),
        {"model_config": SettingsConfigDict(**cfg)},
    )
    try:
        settings = Bound()
        # #498 — fires per-helper load (footer/watchdog/progress/auto_continue/
        # preamble/budget) by design (#269 hot-reload); too noisy at INFO.
        # See v0.35.4 issue for caching settings within handle_message.
        logger.debug("config.loaded", path=str(cfg_path))
        return settings
    except ValidationError as exc:
        raise ConfigError(f"Invalid config in {cfg_path}: {exc}") from exc
    except Exception as exc:  # pragma: no cover - safety net
        raise ConfigError(f"Failed to load config {cfg_path}: {exc}") from exc
