"""Zero-token ``agy -p /<command>`` probes (#558).

rc1 ships the shared runner (``run_agy_slash``) and the cached ``-p /config``
sanity check (phase 02, REVIEW-2 M5). Phase 04 adds the ``/usage`` quota
parser and formatter here; 05's effort probe and 13's ``-p /hooks`` precheck
reuse ``run_agy_slash``.

``run_agy_slash`` runs the guard first (#838), then agy through
``manage_subprocess`` (#590: new session + descendant-aware kill) with stdin
from ``/dev/null`` (no inherited TTY for agy's "paste the authorization code"
prompt), the runner's filtered env and — unless a ``cwd`` is given — a fresh
empty temp dir, so no project ``.agents/`` hooks or MCP servers load. Probe
P-H16 (whether ``-p /config`` in the project cwd starts project MCP servers)
hasn't run, so the config check stays in the temp dir: it sees agy's
user-level settings only.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio

from ..logging import get_logger
from .subprocess import manage_subprocess

if TYPE_CHECKING:
    from ..runners.antigravity import AntigravityRunner

logger = get_logger(__name__)

_MAX_OUTPUT_BYTES = 1024 * 1024
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


async def _read_all(stream: Any, sink: bytearray) -> None:
    if stream is None:
        return
    async for chunk in stream:
        if len(sink) < _MAX_OUTPUT_BYTES:
            sink.extend(chunk[: _MAX_OUTPUT_BYTES - len(sink)])


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
    stdout = bytearray()
    stderr = bytearray()
    rc: int | None = None
    try:
        with anyio.fail_after(timeout_s):
            async with manage_subprocess(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=runner.env(state=None),
                cwd=run_cwd,
            ) as proc:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(_read_all, proc.stdout, stdout)
                    tg.start_soon(_read_all, proc.stderr, stderr)
                rc = await proc.wait()
    except TimeoutError as exc:
        raise AgySlashError("timeout") from exc
    except OSError as exc:
        raise AgySlashError("spawn", exc.__class__.__name__) from exc
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    if "authentication required" in stderr.decode("utf-8", errors="replace").lower():
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
