"""Zero-token ``agy -p /<command>`` probes (#558).

rc1 ships the shared runner (``run_agy_slash``), the cached ``-p /config``
sanity check (phase 02, REVIEW-2 M5) and the on-demand ``/usage`` quota
(phase 04: ``get_quota`` → private 60 s cache → ``parse_quota`` /
``format_quota_html``). 05's effort probe and 13's ``-p /hooks`` precheck
reuse ``run_agy_slash``. Nothing here runs before, during or after an agy
run: the quota is fetched only when someone sends ``/usage`` (D5, REVIEW M8).

``run_agy_slash`` runs the guard first (#838), then agy through
``manage_subprocess`` (#590: new session + descendant-aware kill) with stdin
from ``/dev/null`` (no inherited TTY for agy's "paste the authorization code"
prompt), the runner's filtered env minus any ``UNTETHER_AGY_GATE*`` variable
(REVIEW-2 m10) and — unless a ``cwd`` is given — a fresh empty temp dir, so no project ``.agents/`` hooks or MCP servers load. Probe
P-H16 (whether ``-p /config`` in the project cwd starts project MCP servers)
hasn't run, so the config check stays in the temp dir: it sees agy's
user-level settings only.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio

from ..logging import get_logger
from .subprocess import manage_subprocess

if TYPE_CHECKING:
    from ..runners.antigravity import AntigravityRunner

logger = get_logger(__name__)

_MAX_OUTPUT_BYTES = 1024 * 1024
# rc3's hook gate passes its socket/token in these; a slash probe must never
# inherit them (REVIEW-2 m10).
_GATE_ENV_PREFIX = "UNTETHER_AGY_GATE"
_STDERR_LINE_MAX_BYTES = 8192
CONFIG_TTL_S = 3600.0
# A failed check is remembered briefly so a host where it keeps failing
# (timeout, signed out) doesn't pay the probe before every run.
CONFIG_FAILURE_TTL_S = 300.0


class AgySlashError(Exception):
    """A ``-p /<command>`` probe failed; ``kind`` is a short log tag."""

    def __init__(self, kind: str, detail: str | None = None) -> None:
        super().__init__(detail or kind)
        self.kind = kind


class AntigravityNotSignedIn(AgySlashError):
    def __init__(self) -> None:
        super().__init__("not_signed_in")


def _stderr_line_is_auth(line: str) -> bool:
    from ..runners.antigravity import stderr_kill_kind

    return stderr_kill_kind(line) == "auth"


async def _read_all(stream: Any, sink: bytearray) -> None:
    if stream is None:
        return
    async for chunk in stream:
        if len(sink) < _MAX_OUTPUT_BYTES:
            sink.extend(chunk[: _MAX_OUTPUT_BYTES - len(sink)])


async def _read_stderr(
    stream: Any, sink: bytearray, on_auth: anyio.CancelScope, flag: list[bool]
) -> None:
    """Collect stderr; cancel *on_auth* as soon as a line says agy wants a
    sign-in (D15: 1.2.x waited for a pasted code; the probe must not)."""
    if stream is None:
        return
    pending = b""
    overlong = False  # the current line outgrew the buffer: not an auth line
    async for chunk in stream:
        if len(sink) < _MAX_OUTPUT_BYTES:
            sink.extend(chunk[: _MAX_OUTPUT_BYTES - len(sink)])
        *lines, rest = (pending + chunk).split(b"\n")
        for line in lines:
            if not overlong and _stderr_line_is_auth(
                line.decode("utf-8", errors="replace")
            ):
                flag[0] = True
                on_auth.cancel()
                return
            overlong = False
        if len(rest) > _STDERR_LINE_MAX_BYTES:
            rest, overlong = b"", True
        pending = rest


def _command_data(stdout: bytes) -> dict[str, Any] | None:
    fallback: dict[str, Any] | None = None
    for raw in stdout.decode("utf-8", errors="replace").splitlines():
        start = raw.find("{")
        if start < 0:
            continue
        try:
            obj = json.loads(raw[start:])
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("event") == "command_result":
            data = (obj.get("command") or {}).get("data")
            if isinstance(data, dict):
                return data
        if obj.get("event") == "result" and fallback is None:
            data = ((obj.get("result") or {}).get("command") or {}).get("data")
            if isinstance(data, dict):
                fallback = data
    return fallback


async def run_agy_slash(
    runner: AntigravityRunner,
    command: str,
    *,
    extra_args: tuple[str, ...] = (),
    timeout_s: float = 15.0,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Run ``agy -p <command> --output-format stream-json``; return its data.

    Never passes ``--conversation`` (account-level commands only). Raises
    ``AgySlashError`` (``prespawn_blocked`` / ``timeout`` / ``spawn`` /
    ``unparseable``) or ``AntigravityNotSignedIn``.
    """
    blocked = runner._check_prespawn_ram_guard(None)
    if blocked is not None:
        raise AgySlashError("prespawn_blocked")
    tmp_dir: str | None = None
    if cwd is None:
        tmp_dir = tempfile.mkdtemp(prefix="untether-agy-")
    run_cwd = str(cwd) if cwd is not None else tmp_dir
    argv = [
        runner.command(),
        "-p",
        command,
        "--output-format",
        "stream-json",
        *extra_args,
    ]
    env = runner.env(state=None)
    if env is not None:
        env = {k: v for k, v in env.items() if not k.startswith(_GATE_ENV_PREFIX)}
    stdout = bytearray()
    stderr = bytearray()
    rc: int | None = None
    auth_required = [False]
    try:
        with anyio.fail_after(timeout_s):
            # Leaving manage_subprocess with agy still running terminates
            # its whole process group (#590), on the auth cancel and on
            # the timeout alike.
            async with manage_subprocess(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=run_cwd,
            ) as proc:
                with anyio.CancelScope() as auth_scope:
                    async with anyio.create_task_group() as tg:
                        tg.start_soon(_read_all, proc.stdout, stdout)
                        tg.start_soon(
                            _read_stderr, proc.stderr, stderr, auth_scope, auth_required
                        )
                    rc = await proc.wait()
    except TimeoutError as exc:
        raise AgySlashError("timeout") from exc
    except OSError as exc:
        raise AgySlashError("spawn", exc.__class__.__name__) from exc
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    if auth_required[0] or any(
        _stderr_line_is_auth(line)
        for line in stderr.decode("utf-8", errors="replace").splitlines()
    ):
        raise AntigravityNotSignedIn()
    data = _command_data(bytes(stdout))
    if data is None:
        raise AgySlashError("unparseable", f"rc={rc}")
    return data


# ── `-p /config` sanity check (REVIEW-2 M5) ─────────────────────────────────


@dataclass(frozen=True, slots=True)
class AgyConfig:
    """The permission-relevant keys of agy's ``-p /config`` output."""

    tool_permission: str | None
    allow_non_workspace_access: bool
    allow_rules_count: int
    model_provider: str
    # 12 hex over the keys above (minus model_provider): one ⚠️ row per
    # (project, digest).
    digest: str


def parse_agy_config(data: dict[str, Any]) -> AgyConfig:
    tool_permission = data.get("toolPermission")
    permissions = data.get("permissions")
    allow = permissions.get("allow") if isinstance(permissions, dict) else None
    allow_rules = (
        [r for r in allow if isinstance(r, str)] if isinstance(allow, list) else []
    )
    non_workspace = data.get("allowNonWorkspaceAccess") is True
    provider = data.get("modelProvider")
    material = json.dumps(
        [tool_permission, non_workspace, sorted(allow_rules)], separators=(",", ":")
    )
    return AgyConfig(
        tool_permission=tool_permission if isinstance(tool_permission, str) else None,
        allow_non_workspace_access=non_workspace,
        allow_rules_count=len(allow_rules),
        model_provider=provider if isinstance(provider, str) else "",
        digest=hashlib.sha256(material.encode()).hexdigest()[:12],
    )


async def _run_agy_config(runner: AntigravityRunner) -> AgyConfig:
    data = await run_agy_slash(runner, "/config")
    config = data.get("config")
    if not isinstance(config, dict):
        raise AgySlashError("unparseable", "no config object")
    return parse_agy_config(config)


# Indirection so tests stub the probe (``tests/conftest.py``) without losing
# the real implementation.
_probe_agy_config = _run_agy_config


def agy_settings_path() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


_CONFIG_CACHE: dict[tuple[Any, ...], tuple[float, AgyConfig]] = {}
_CONFIG_FAILED: dict[tuple[Any, ...], float] = {}
_CONFIG_LOCKS: dict[str, anyio.Lock] = {}


def clear_config_cache() -> None:
    _CONFIG_CACHE.clear()
    _CONFIG_FAILED.clear()
    _CONFIG_LOCKS.clear()


def _cached(key: tuple[Any, ...]) -> tuple[bool, AgyConfig | None]:
    now = time.monotonic()
    hit = _CONFIG_CACHE.get(key)
    if hit is not None and now - hit[0] < CONFIG_TTL_S:
        return True, hit[1]
    failed_at = _CONFIG_FAILED.get(key)
    if failed_at is not None and now - failed_at < CONFIG_FAILURE_TTL_S:
        return True, None
    return False, None


def _config_cache_key(runner: AntigravityRunner) -> tuple[Any, ...]:
    from ..runners import antigravity as agy_runner

    cmd = runner.command()
    try:
        st = agy_settings_path().stat()
        settings_stat: tuple[int, int] | None = (st.st_size, st.st_mtime_ns)
    except OSError:
        settings_stat = None  # stat only (D33): never read the file
    return (cmd, agy_runner.cached_agy_version(cmd), settings_stat)


async def agy_config(runner: AntigravityRunner) -> AgyConfig | None:
    """agy's own permission settings, cached per (agy version, settings
    file stat) for an hour. ``None`` when the check failed: callers fail
    open (the ``init.permission_mode`` cross-check still applies)."""
    key = _config_cache_key(runner)
    hit, value = _cached(key)
    if hit:
        return value
    lock = _CONFIG_LOCKS.setdefault(runner.command(), anyio.Lock())
    async with lock:
        hit, value = _cached(key)
        if hit:
            return value
        try:
            config = await _probe_agy_config(runner)
        except AgySlashError as exc:
            logger.warning("antigravity.config_check.failed", kind=exc.kind)
            if exc.kind != "prespawn_blocked":  # transient: retry next run
                _CONFIG_FAILED[key] = time.monotonic()
            return None
        except Exception as exc:  # noqa: BLE001 — never block a run on this
            logger.warning(
                "antigravity.config_check.failed",
                kind="error",
                error_type=exc.__class__.__name__,
            )
            _CONFIG_FAILED[key] = time.monotonic()
            return None
        for stale in [k for k in _CONFIG_CACHE if k[0] == key[0]]:
            _CONFIG_CACHE.pop(stale, None)
        _CONFIG_FAILED.pop(key, None)
        _CONFIG_CACHE[key] = (time.monotonic(), config)
        logger.info(
            "antigravity.config_check.fetched",
            tool_permission=config.tool_permission,
            allow_rules=config.allow_rules_count,
            allow_non_workspace_access=config.allow_non_workspace_access,
            digest=config.digest,
        )
        return config


def auth_route(config: AgyConfig | None, environ: Any = None) -> str:
    """``api_key`` / ``adc`` / ``oauth`` (a failed check counts as OAuth,
    for the ToS notice only)."""
    env = os.environ if environ is None else environ
    if config is not None and config.model_provider == "gemini":
        return "api_key"
    if str(env.get("AGY_ADC_AUTH", "")).strip().lower() == "true":
        return "adc"
    return "oauth"


# ── `/usage` quota (phase 04, D5) ───────────────────────────────────────────
# Parser and formatter ported from PR #766 (Manuel Naranjo), minus its
# cross-group "worst bucket" (audit R5: the groups are separate pools) and
# with every agy string escaped and capped.

QUOTA_TTL_S = 60.0
QUOTA_TIMEOUT_S = 15.0
MAX_GROUPS = 8
MAX_BUCKETS = 8
MAX_NAME_CHARS = 80
_MAX_RESET_CHARS = 40
_WINDOW_LABELS = {"5h": "5-hour", "weekly": "Weekly"}
_WINDOW_ORDER = {"5h": 0, "weekly": 1}
_BAR_WIDTH = 10


@dataclass(frozen=True, slots=True)
class QuotaBucket:
    id: str
    name: str
    window: str
    utilisation: float  # percent used, 0 to 100
    resets_at: str | None  # ISO 8601 (agy's piped output keeps UTC)


@dataclass(frozen=True, slots=True)
class QuotaGroup:
    name: str
    buckets: tuple[QuotaBucket, ...]


def _short_str(value: Any, limit: int = MAX_NAME_CHARS) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _parse_reset(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > _MAX_RESET_CHARS:
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value


def _parse_bucket(raw: Any) -> QuotaBucket | None:
    if not isinstance(raw, dict):
        return None
    fraction = raw.get("remaining_fraction")
    if (
        isinstance(fraction, bool)
        or not isinstance(fraction, int | float)
        or not math.isfinite(fraction)
    ):
        return None
    return QuotaBucket(
        id=_short_str(raw.get("id")),
        name=_short_str(raw.get("name")),
        window=_short_str(raw.get("window"), 16),
        utilisation=max(0.0, min(100.0, (1.0 - float(fraction)) * 100.0)),
        resets_at=_parse_reset(raw.get("reset_time")),
    )


def parse_quota(data: Any) -> list[QuotaGroup]:
    """agy ``/usage`` ``command.data`` → quota groups, each with its own
    buckets (5-hour first). Malformed entries are skipped, never raised."""
    raw_groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(raw_groups, list):
        return []
    groups: list[QuotaGroup] = []
    for raw in raw_groups[:MAX_GROUPS]:
        if not isinstance(raw, dict):
            continue
        raw_buckets = raw.get("buckets")
        buckets = [
            b
            for b in (
                _parse_bucket(item)
                for item in (
                    raw_buckets[:MAX_BUCKETS] if isinstance(raw_buckets, list) else []
                )
            )
            if b is not None
        ]
        buckets.sort(key=lambda b: _WINDOW_ORDER.get(b.window, len(_WINDOW_ORDER)))
        groups.append(
            QuotaGroup(name=_short_str(raw.get("name")), buckets=tuple(buckets))
        )
    return groups


def _time_until(iso_ts: str, now: datetime | None = None) -> str:
    reset = datetime.fromisoformat(iso_ts)
    if reset.tzinfo is None:
        reset = reset.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    total = max(0, int((reset - current).total_seconds()))
    hours, rem = divmod(total, 3600)
    if hours >= 24:
        return f"{hours // 24}d {hours % 24}h"
    if hours:
        return f"{hours}h {rem // 60}m"
    return f"{rem // 60}m"


def _bar(pct: float) -> str:
    filled = max(0, min(_BAR_WIDTH, round(pct / 100 * _BAR_WIDTH)))
    return "█" * filled + "░" * (_BAR_WIDTH - filled)


def format_quota_html(
    groups: list[QuotaGroup], *, now: datetime | None = None
) -> list[str]:
    """Telegram HTML lines, one block per group. Every agy-provided string
    is escaped; errors are rendered by the caller."""
    if not groups:
        return ["agy returned no quota groups."]
    lines: list[str] = []
    for group in groups:
        lines.append(f"<b>{html.escape(group.name or 'Quota')}</b>")
        if not group.buckets:
            lines.append("• no limits reported")
        for bucket in group.buckets:
            label = _WINDOW_LABELS.get(bucket.window) or html.escape(
                bucket.name or bucket.window or "Limit"
            )
            row = (
                f"• {label}: {_bar(bucket.utilisation)} {bucket.utilisation:.0f}% used"
            )
            # An untouched window hasn't started, so its reset time means
            # nothing (PR #766 behaviour).
            if bucket.resets_at and bucket.utilisation > 0:
                row += f" · resets in {_time_until(bucket.resets_at, now)}"
            lines.append(row)
    return lines


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    groups: list[QuotaGroup]
    age_s: float


@dataclass(frozen=True, slots=True)
class QuotaCacheStats:
    last_success_wall: float | None
    age_s: float | None
    last_error_kind: str | None


def _redact(text: str) -> str:
    """URLs first (whole, query string included), then paths (R10)."""
    from ..runner import _URL_RE, _sanitise_stderr

    return _sanitise_stderr(_URL_RE.sub("[url]", text))


class _QuotaCache:
    """Private 60 s cache for ``agy -p /usage``. Its own lazily created lock
    — never Claude's ``usage_cache`` lock, so a slow agy can't hold up
    Claude's footer. Failures are not cached (a fresh sign-in shows at once)."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._lock: anyio.Lock | None = None
        self._value: tuple[str, float, list[QuotaGroup]] | None = None
        self.last_success_wall: float | None = None
        self.last_error_kind: str | None = None

    def _fresh(self, cmd: str) -> QuotaSnapshot | None:
        if self._value is None or self._value[0] != cmd:
            return None
        age = time.monotonic() - self._value[1]
        if age >= QUOTA_TTL_S:
            return None
        return QuotaSnapshot(groups=self._value[2], age_s=age)

    def stats(self) -> QuotaCacheStats:
        age = time.monotonic() - self._value[1] if self._value is not None else None
        return QuotaCacheStats(
            last_success_wall=self.last_success_wall,
            age_s=age,
            last_error_kind=self.last_error_kind,
        )

    async def get_or_fetch(self, runner: AntigravityRunner) -> QuotaSnapshot:
        cmd = runner.command()
        hit = self._fresh(cmd)
        if hit is not None:
            return hit
        if self._lock is None:
            self._lock = anyio.Lock()
        async with self._lock:
            hit = self._fresh(cmd)
            if hit is not None:
                return hit
            start = time.monotonic()
            try:
                data = await run_agy_slash(runner, "/usage", timeout_s=QUOTA_TIMEOUT_S)
            except AgySlashError as exc:
                self.last_error_kind = exc.kind
                logger.warning(
                    "antigravity.quota.failed",
                    kind=exc.kind,
                    detail=_redact(str(exc)[:200]),
                )
                raise
            except Exception as exc:
                self.last_error_kind = exc.__class__.__name__
                logger.warning(
                    "antigravity.quota.failed",
                    kind="error",
                    error_type=exc.__class__.__name__,
                )
                raise
            groups = parse_quota(data)
            now = time.monotonic()
            self._value = (cmd, now, groups)
            self.last_success_wall = time.time()
            self.last_error_kind = None
            logger.info(
                "antigravity.quota.fetched",
                duration_ms=int((now - start) * 1000),
                groups=len(groups),
            )
            return QuotaSnapshot(groups=groups, age_s=0.0)


_QUOTA_CACHE = _QuotaCache()


async def get_quota(runner: AntigravityRunner) -> QuotaSnapshot:
    """agy's quota groups, from the 60 s cache or one ``-p /usage`` probe.
    Only ``/usage`` calls this. Raises ``AgySlashError`` /
    ``AntigravityNotSignedIn``."""
    return await _QUOTA_CACHE.get_or_fetch(runner)


def quota_cache_stats() -> QuotaCacheStats:
    return _QUOTA_CACHE.stats()


def reset_quota_cache() -> None:
    _QUOTA_CACHE.reset()
