"""Is Untether's agy gate loaded for this run? (#980, D22.1 amended).

Two layers, cheapest first:

1. :func:`gate_plugin.inspect_install` — the plugin folder holds exactly the
   files this Untether version writes (byte compare against its own
   templates, never against ``.untether-managed``) and agy hasn't disabled it.
2. ``agy -p /hooks`` in the run's cwd (0 tokens, 3-12 s; P-H16: starts no
   planted MCP server and runs no hook) — agy itself lists the
   ``untether-gate`` group as enabled, sourced from the plugin folder, with
   our three commands byte for byte and a long enough ``PreToolUse`` timeout.
   It also lists every *other* hook group with its source: those run shell
   outside the gate (REVIEW-2 B2), which phase 14's gated modes refuse.

Cached for an hour per (agy binary + version, plugin files, cwd, the planted
config digest of the cwd, the stat of agy's user-level hook / plugin state
files). A failed check is cached for five minutes. ``-p /hooks`` proves the
hooks are *loaded*; the runner's liveness check (a ``PreInvocation`` before
the first model step) proves they *run*.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio

from ..logging import get_logger
from ..utils import antigravity_quota, antigravity_scan
from . import gate_plugin

if TYPE_CHECKING:
    from ..runners.antigravity import AntigravityRunner

logger = get_logger(__name__)

CACHE_TTL_S = 3600.0
FAILURE_TTL_S = 300.0
HOOKS_TIMEOUT_S = 30.0
# agy shows the user-level plugin folder as `~/.gemini/config/…` (P-H0).
_GROUP_SOURCE = (
    f"~/.gemini/config/plugins/{gate_plugin.PLUGIN_NAME}/{gate_plugin.HOOKS_FILE}"
)

# Reasons (logged as ``antigravity.gate.precheck_failed reason=…``).
MISSING = "missing"
EDITED = "edited"
OUTDATED = "outdated"
DISABLED = "disabled"
NOT_LISTED = "not_listed"
MISMATCH = "mismatch"
NOT_SIGNED_IN = "not_signed_in"
CHECK_FAILED = "check_failed"
SELF_PROTECT = "self_protect"

_REASON_TEXT = {
    MISSING: "the plugin isn't installed (run `untether agy-gate install`)",
    EDITED: "the plugin was edited since install (run `untether agy-gate install --force`)",
    OUTDATED: "the plugin is from another Untether version — run `untether agy-gate install` on the host to update it",
    DISABLED: "agy has the plugin disabled (`agy plugin enable untether-gate`)",
    NOT_LISTED: "agy doesn't list the plugin's hooks for this project",
    MISMATCH: "agy loads different hook commands than Untether wrote (run `untether agy-gate install`)",
    NOT_SIGNED_IN: "agy isn't signed in, so the hooks couldn't be listed",
    CHECK_FAILED: "the `agy -p /hooks` check failed",
    SELF_PROTECT: "this project contains Untether's own code, which agy could edit to switch the gate off",
}


def reason_text(reason: str | None) -> str:
    return _REASON_TEXT.get(reason or "", reason or "unknown")


@dataclass(frozen=True, slots=True)
class HookGroup:
    name: str
    enabled: bool
    source: str
    actions: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class GatePrecheck:
    ok: bool
    reason: str | None = None
    detail: str | None = None
    # Other hook groups agy lists for this cwd, by source (phase 14 refuses
    # its gated modes while any is present; phase 13 only records them).
    foreign_sources: tuple[str, ...] = ()
    wait_s: int = gate_plugin.DEFAULT_WAIT_S
    checked_at: float = field(default_factory=time.time)
    cwd: str | None = None
    # REVIEW-2 B3: the project holds Untether's own code (that path). Only a
    # mode whose safety rests on the gate refuses on it (phase 14).
    self_protect: str | None = None


def parse_hooks(data: dict[str, Any]) -> list[HookGroup]:
    """``command_result.command.data`` of ``agy -p /hooks``."""
    groups: list[HookGroup] = []
    raw = data.get("hooks")
    if not isinstance(raw, list):
        return groups
    for item in raw:
        if not isinstance(item, dict):
            continue
        actions = item.get("actions")
        groups.append(
            HookGroup(
                name=str(item.get("name") or ""),
                enabled=item.get("enabled") is True,
                source=str(item.get("source") or ""),
                actions=tuple(a for a in actions if isinstance(a, dict))
                if isinstance(actions, list)
                else (),
            )
        )
    return groups


def _timeout(action: dict[str, Any]) -> int:
    # Absent when it is agy's 30 s default.
    value = action.get("timeout_seconds")
    return value if isinstance(value, int) and not isinstance(value, bool) else 30


def _is_our_source(source: str) -> bool:
    """Exactly the user-level plugin's ``hooks.json`` — never a project's
    ``.agents/plugins/untether-gate/`` that merely ends the same way."""
    if source == _GROUP_SOURCE:
        return True
    try:
        return (
            Path(source).expanduser().resolve()
            == (gate_plugin.plugin_dir() / gate_plugin.HOOKS_FILE).resolve()
        )
    except (OSError, ValueError):
        return False


def evaluate_hooks(groups: list[HookGroup], *, wait_s: int) -> GatePrecheck:
    """Pure: does agy's own listing carry our gate exactly?"""
    ours = [g for g in groups if g.name == gate_plugin.PLUGIN_NAME]
    foreign = tuple(
        sorted(
            {g.source or g.name for g in groups if g.name != gate_plugin.PLUGIN_NAME}
        )
    )
    if len(ours) != 1:
        return GatePrecheck(
            ok=False,
            reason=NOT_LISTED,
            detail=f"{len(ours)} {gate_plugin.PLUGIN_NAME} groups",
            foreign_sources=foreign,
            wait_s=wait_s,
        )
    group = ours[0]
    if not group.enabled:
        return GatePrecheck(
            ok=False, reason=DISABLED, foreign_sources=foreign, wait_s=wait_s
        )
    if not _is_our_source(group.source):
        return GatePrecheck(
            ok=False,
            reason=MISMATCH,
            detail="source",
            foreign_sources=foreign,
            wait_s=wait_s,
        )
    by_event: dict[str, list[dict[str, Any]]] = {}
    for action in group.actions:
        by_event.setdefault(str(action.get("event")), []).append(action)
    for event in ("PreToolUse", "PreInvocation", "Stop"):
        actions = by_event.get(event, [])
        if len(actions) != 1:
            return GatePrecheck(
                ok=False,
                reason=MISMATCH,
                detail=f"{event}: {len(actions)} actions",
                foreign_sources=foreign,
                wait_s=wait_s,
            )
        action = actions[0]
        if action.get("command") != gate_plugin.hook_command(event):
            return GatePrecheck(
                ok=False,
                reason=MISMATCH,
                detail=f"{event}: command",
                foreign_sources=foreign,
                wait_s=wait_s,
            )
        if event == "PreToolUse":
            if action.get("matcher") != "*":
                return GatePrecheck(
                    ok=False,
                    reason=MISMATCH,
                    detail="PreToolUse: matcher",
                    foreign_sources=foreign,
                    wait_s=wait_s,
                )
            if _timeout(action) < wait_s + gate_plugin.HOOK_GRACE_S:
                return GatePrecheck(
                    ok=False,
                    reason=MISMATCH,
                    detail="PreToolUse: timeout",
                    foreign_sources=foreign,
                    wait_s=wait_s,
                )
    extra = set(by_event) - {"PreToolUse", "PreInvocation", "Stop"}
    if extra:
        return GatePrecheck(
            ok=False,
            reason=MISMATCH,
            detail="extra events: " + ", ".join(sorted(extra)),
            foreign_sources=foreign,
            wait_s=wait_s,
        )
    return GatePrecheck(ok=True, foreign_sources=foreign, wait_s=wait_s)


def self_protect_path(
    cwd: Path, root: Path | None = None, config_path: Path | None = None
) -> Path | None:
    """REVIEW-2 B3: a project that contains Untether's own package, its
    venv, the Python it runs on (``sys.base_prefix``), the plugin folder or
    (security review 3 M5) this instance's config file and its folder could
    have its gate edited by the agent it guards. Returns the offending
    path, or None."""
    import untether

    project = (root or cwd).resolve()
    candidates = [
        Path(untether.__file__).resolve().parent,
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
        gate_plugin.plugin_dir().resolve(),
    ]
    if config_path is not None:
        candidates.append(config_path.expanduser().resolve().parent)
    for candidate in candidates:
        if candidate == project or project in candidate.parents:
            return candidate
    return None


def _stat_key(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _cache_key(
    runner: AntigravityRunner, cwd: Path, scan_digest: str | None
) -> tuple[Any, ...]:
    from ..runners import antigravity as agy_runner

    cmd = runner.command()
    user = antigravity_scan.user_config_dir()
    folder = gate_plugin.plugin_dir()
    plugin_stats = tuple(
        _stat_key(folder / name)
        for name in (*gate_plugin.PLUGIN_FILES, gate_plugin.MANIFEST_FILE)
    )
    return (
        cmd,
        _stat_key(Path(cmd)) if os.path.isabs(cmd) else None,
        agy_runner.cached_agy_version(cmd),
        str(cwd),
        scan_digest,
        plugin_stats,
        _stat_key(user / "config.json"),
        _stat_key(user / "import_manifest.json"),
        _stat_key(user / "hooks.json"),
        _stat_key(antigravity_quota.agy_settings_path()),
    )


_CACHE: dict[tuple[Any, ...], GatePrecheck] = {}
_LATEST: dict[str, GatePrecheck] = {}
_LOCKS: dict[str, anyio.Lock] = {}


def clear_cache(cwd: Path | None = None) -> None:
    """``/config → 🔐 → Re-check`` (one project) or everything."""
    if cwd is None:
        _CACHE.clear()
        _LATEST.clear()
        _MCP_CACHE.clear()
        return
    key = str(cwd)
    for k in [k for k in _CACHE if k[3] == key]:
        _CACHE.pop(k, None)
    _LATEST.pop(key, None)


def latest(cwd: Path | None) -> GatePrecheck | None:
    """The newest result for *cwd* (any age), for ``/config`` and the
    ask-capability check. Never runs agy."""
    if cwd is None:
        return None
    return _LATEST.get(str(cwd))


async def _run_hooks(runner: AntigravityRunner, cwd: Path) -> dict[str, Any]:
    return await antigravity_quota.run_agy_slash(
        runner, "/hooks", timeout_s=HOOKS_TIMEOUT_S, cwd=cwd
    )


# Indirection so tests stub the probe without a real agy.
_probe_hooks: Callable[[AntigravityRunner, Path], Awaitable[dict[str, Any]]] = (
    _run_hooks
)


def _remember(cwd: Path, result: GatePrecheck) -> GatePrecheck:
    _LATEST[str(cwd)] = result
    if not result.ok:
        logger.warning(
            "antigravity.gate.precheck_failed",
            reason=result.reason,
            detail=result.detail,
            cwd=str(cwd),
        )
    else:
        logger.info(
            "antigravity.gate.precheck",
            ok=True,
            foreign=len(result.foreign_sources),
            cwd=str(cwd),
        )
    return result


USER_MCP_KEY = f"{antigravity_scan.USER_DISPLAY_PREFIX}mcp_config.json"
USER_HOOKS_KEY = f"{antigravity_scan.USER_DISPLAY_PREFIX}hooks.json"
MCP_LIST_TIMEOUT_S = 15.0
_NO_MCP_SERVERS = "No MCP servers configured."
_MCP_CACHE: dict[tuple[Any, ...], bool] = {}


async def _run_mcp_list(runner: AntigravityRunner) -> str | None:
    """``agy mcp list`` (agy's own listing of user-level MCP servers; no
    model call, ≈ 0.5 s). Its stdout, or None when it failed."""
    import shutil
    import subprocess
    import tempfile

    from ..utils.subprocess import manage_subprocess

    if runner._check_prespawn_ram_guard(None) is not None:
        # #838: no spawn under memory pressure; unknown counts as "has some".
        return None
    env = runner.env(state=None)
    if env is not None:
        env = {k: v for k, v in env.items() if not k.startswith("UNTETHER_AGY_GATE")}
    folder = tempfile.mkdtemp(prefix="untether-agy-")
    out = bytearray()
    try:
        with anyio.fail_after(MCP_LIST_TIMEOUT_S):
            async with manage_subprocess(
                [runner.command(), "mcp", "list"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                cwd=folder,
            ) as proc:
                assert proc.stdout is not None
                async for chunk in proc.stdout:
                    out.extend(chunk[: max(0, 65536 - len(out))])
                rc = await proc.wait()
    except (TimeoutError, OSError):
        return None
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return out.decode("utf-8", errors="replace") if rc == 0 else None


# Indirection so tests stub it without a real agy.
_probe_mcp: Callable[[AntigravityRunner], Awaitable[str | None]] = _run_mcp_list


async def user_mcp_configured(runner: AntigravityRunner) -> bool:
    """Whether agy has user-level MCP servers. The scanner only stats
    ``~/.gemini/config/mcp_config.json`` (D33: never read), and an existing
    file says nothing about servers (lba-1 has an empty one), so agy's own
    ``agy mcp list`` decides. Fails closed: an error counts as "has some"."""
    from ..runners import antigravity as agy_runner

    cmd = runner.command()
    key = (
        cmd,
        agy_runner.cached_agy_version(cmd),
        _stat_key(antigravity_scan.user_config_dir() / "mcp_config.json"),
    )
    hit = _MCP_CACHE.get(key)
    if hit is not None:
        return hit
    out = await _probe_mcp(runner)
    configured = out is None or out.strip() != _NO_MCP_SERVERS
    if out is not None:
        _MCP_CACHE[key] = configured
    logger.info(
        "antigravity.gate.user_mcp", configured=configured, checked=out is not None
    )
    return configured


async def foreign_config(
    runner: AntigravityRunner,
    result: GatePrecheck,
    scan: antigravity_scan.ScanResult | None,
) -> tuple[str, ...]:
    """Config that runs commands outside the gate and that Untether doesn't
    manage, in scope for this run (REVIEW-2 B2, D34): every other hook group
    agy lists (``-p /hooks``, any event), and every file of the planted-config
    scan — the project's ``.agents/`` hooks, plugins, MCP servers and agents,
    and agy's user-level plugins and agents. Two user-level files are judged
    by agy itself instead of by their presence: ``hooks.json`` (its groups
    are in the ``-p /hooks`` listing) and ``mcp_config.json``
    (:func:`user_mcp_configured`)."""
    # #1080: agy names a hook group's source by its absolute path, the scan
    # relative to the project (or ``~/``); one file must be listed once.
    home_real = os.path.realpath(os.path.expanduser("~"))
    root_real = os.path.realpath(scan.root) if scan is not None else None

    def shown(source: str) -> str:
        if root_real is None or not os.path.isabs(source):
            return source
        return antigravity_scan._display(os.path.realpath(source), root_real, home_real)

    found = {shown(source) for source in result.foreign_sources}
    for path in scan.agy if scan is not None else ():
        if path == USER_HOOKS_KEY:
            continue
        if path == USER_MCP_KEY and not await user_mcp_configured(runner):
            continue
        found.add(path)
    return tuple(sorted(found))


async def check(
    runner: AntigravityRunner,
    cwd: Path,
    *,
    scan_digest: str | None = None,
    root: Path | None = None,
    use_cache: bool = True,
) -> GatePrecheck:
    """The gate's readiness for a run in *cwd*. Never raises."""
    install = gate_plugin.inspect_install()
    if not install.present:
        return GatePrecheck(ok=False, reason=MISSING, cwd=str(cwd))
    if not install.managed or install.edited or install.problem:
        return _remember(
            cwd,
            GatePrecheck(
                ok=False,
                reason=EDITED,
                detail=install.summary(),
                cwd=str(cwd),
            ),
        )
    if not install.current:
        # Only a byte-exact earlier version is "outdated"; files changed
        # together with their record read as edited.
        reason = OUTDATED if install.previous_version is not None else EDITED
        return _remember(
            cwd,
            GatePrecheck(
                ok=False, reason=reason, detail=install.summary(), cwd=str(cwd)
            ),
        )
    if install.disabled:
        return _remember(cwd, GatePrecheck(ok=False, reason=DISABLED, cwd=str(cwd)))
    wait_s = install.wait_s or gate_plugin.DEFAULT_WAIT_S
    key = _cache_key(runner, cwd, scan_digest)
    now = time.time()
    hit = _CACHE.get(key)
    if use_cache and hit is not None:
        ttl = CACHE_TTL_S if hit.ok else FAILURE_TTL_S
        if now - hit.checked_at < ttl:
            _LATEST[str(cwd)] = hit
            return hit
    lock = _LOCKS.setdefault(str(cwd), anyio.Lock())
    async with lock:
        hit = _CACHE.get(key)
        if (
            use_cache
            and hit is not None
            and now - hit.checked_at < (CACHE_TTL_S if hit.ok else FAILURE_TTL_S)
        ):
            return hit
        try:
            data = await _probe_hooks(runner, cwd)
        except antigravity_quota.AntigravityNotSignedIn:
            result = GatePrecheck(ok=False, reason=NOT_SIGNED_IN, cwd=str(cwd))
        except antigravity_quota.AgySlashError as exc:
            result = GatePrecheck(
                ok=False, reason=CHECK_FAILED, detail=exc.kind, cwd=str(cwd)
            )
        except Exception as exc:  # noqa: BLE001 — never block a run on this
            result = GatePrecheck(
                ok=False,
                reason=CHECK_FAILED,
                detail=exc.__class__.__name__,
                cwd=str(cwd),
            )
        else:
            evaluated = evaluate_hooks(parse_hooks(data), wait_s=wait_s)
            result = GatePrecheck(
                ok=evaluated.ok,
                reason=evaluated.reason,
                detail=evaluated.detail,
                foreign_sources=evaluated.foreign_sources,
                wait_s=wait_s,
                cwd=str(cwd),
            )
            protected = self_protect_path(
                cwd, root, getattr(runner, "config_path", None)
            )
            if protected is not None:
                result = replace(result, self_protect=str(protected))
                logger.info(
                    "antigravity.gate.self_protect", cwd=str(cwd), path=str(protected)
                )
        _CACHE[key] = result
        return _remember(cwd, result)
