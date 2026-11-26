"""Zero-token reads from a short-lived ``codex app-server`` (#966, #846, #423).

Codex reports plan quota (``account/rateLimits/read``), the model catalogue
(``model/list``), the user's config (``config/read``) and the provider's
web-search capability only over app-server JSON-RPC (stable tier). Untether's
``/usage``, ``/config`` pages and exec runs have no Codex process to ask, so
one read spawns ``codex app-server`` for a single batch of requests and
closes it (DEC-11; gap row "short-lived read" in the Codex register).

Never sends ``thread/start`` or ``turn/start``: no model tokens. Two caches:
usage (60 s, failures 30 s) and catalogue (1 h, failures 5 min). Finals and
the run-time reasoning drop only ``peek`` them; they never spawn.

Codex's account ids, emails and config origins are never logged or kept:
the cache records the auth kind and plan only.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio
from anyio.streams.buffered import BufferedByteReceiveStream

from .. import __version__
from ..logging import get_logger
from ..utils.env_policy import apply_run_env
from ..utils.error_display import redact_url_secrets
from ..utils.streams import _MAX_LINE_BYTES
from ..utils.subprocess import manage_subprocess
from .codex_rpc import (
    CodexRpcClosed,
    CodexRpcPeer,
    CodexRpcWriteTimeout,
    Response,
    RpcError,
)

if TYPE_CHECKING:
    from .codex import CodexRunner

logger = get_logger(__name__)

READ_TIMEOUT_S = 10.0
_EXIT_WAIT_S = 2.0
USAGE_TTL_S = 60.0
USAGE_FAILURE_TTL_S = 30.0
CATALOGUE_TTL_S = 3600.0
CATALOGUE_FAILURE_TTL_S = 300.0
# #1412: the /config Reasoning page re-reads a catalogue older than this
# (about 1 s, no tokens); runs keep the 1 h cache through peek_levels.
REASONING_PAGE_MAX_AGE_S = 60.0
# The run footer accepts a usage snapshot this old; it never fetches.
FOOTER_MAX_AGE_S = 900.0
WARMUP_INTERVAL_S = 3600.0
# Tests switch this off so a fake `codex` never gets an unexpected spawn.
WARMUP_ENABLED = True

# Codex's reasoning vocabulary Untether has buttons for, in order (#846).
# `model_reasoning_effort` documents low … ultra (config reference, 0.171.0).
CODEX_REASONING_VOCAB: tuple[str, ...] = (
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)

# Same allow-list as `find_non_app_server_args`: the only extra_args a read
# passes on (a `--profile` has no app-server equivalent, DEC-19).
_PAIR_FLAGS = frozenset({"-c", "--config", "--enable", "--disable"})
_AUTH_REQUIRED = "authentication required"


class CodexReadError(Exception):
    """A read failed. ``kind``: unsupported / timeout / spawn / exited /
    error / not_signed_in."""

    def __init__(self, kind: str, *, code: int | None = None, detail: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.code = code
        self.detail = redact_url_secrets(detail)[:200]


class CodexReadUnavailable(CodexReadError):
    """The CLI can't run app-server (the #961 gate said no)."""

    def __init__(self, version: str | None) -> None:
        super().__init__("unsupported")
        self.version = version


class CodexNotSignedIn(CodexReadError):
    def __init__(self) -> None:
        super().__init__("not_signed_in")


def read_args(extra_args: list[str]) -> list[str]:
    """The ``-c`` / ``--enable`` / ``--disable`` / ``--strict-config`` part
    of *extra_args* (the app-server allow-list); everything else is dropped."""
    out: list[str] = []
    i = 0
    while i < len(extra_args):
        tok = extra_args[i]
        name, eq, _ = tok.partition("=")
        if tok in _PAIR_FLAGS and i + 1 < len(extra_args):
            out += [tok, extra_args[i + 1]]
            i += 2
            continue
        if (
            (eq and name in _PAIR_FLAGS)
            or tok == "--strict-config"
            or (tok.startswith("-c") and not tok.startswith("--") and len(tok) > 2)
        ):
            out.append(tok)
        i += 1
    return out


async def app_server_read(
    runner: CodexRunner,
    calls: list[tuple[str, dict[str, Any] | None]],
    *,
    timeout_s: float | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Spawn ``codex app-server``, send *calls* (method, params) after the
    handshake, return ``(cli_version, {method: result | RpcError})``.

    Runs in a fresh empty temp dir (no project config, hooks or MCP), with the
    runner's filtered env. Raises ``CodexReadError``."""
    from .codex_app_server import OPT_OUT_NOTIFICATIONS, codex_app_server_support

    cmd = runner.command()
    support = await anyio.to_thread.run_sync(codex_app_server_support, cmd)
    if not support.ok:
        raise CodexReadUnavailable(support.version)
    # #838: a read is a spawn too — refuse on a host short of memory or at
    # its run limit, like every engine run.
    if runner._check_prespawn_ram_guard(None) is not None:
        raise CodexReadError("prespawn_blocked")
    argv = [cmd, *read_args(runner.extra_args), "app-server", "--listen", "stdio://"]
    env = apply_run_env(runner.env(state=None), None)
    tmp_dir = tempfile.mkdtemp(prefix="untether-codex-read-")
    results: dict[str, Any] = {}
    try:
        with anyio.fail_after(timeout_s or READ_TIMEOUT_S):
            async with manage_subprocess(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                cwd=tmp_dir,
            ) as proc:
                peer = CodexRpcPeer(proc.stdin.send)
                reader = BufferedByteReceiveStream(proc.stdout)
                await peer.submit(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "untether",
                            "title": "Untether",
                            "version": __version__,
                        },
                        "capabilities": {
                            "experimentalApi": False,
                            "optOutNotificationMethods": list(OPT_OUT_NOTIFICATIONS),
                        },
                    },
                )
                waiting: dict[Any, str] = {}
                while True:
                    line = await reader.receive_until(b"\n", _MAX_LINE_BYTES)
                    msg = await peer.feed(line)
                    if not isinstance(msg, Response):
                        continue  # notifications and answered server requests
                    if msg.method == "initialize":
                        if msg.error is not None:
                            raise CodexReadError(
                                "error", code=msg.error.code, detail=msg.error.message
                            )
                        await peer.notify("initialized", {})
                        for method, params in calls:
                            waiting[await peer.submit(method, params)] = method
                        if not waiting:
                            break
                        continue
                    method = waiting.pop(msg.id, None)
                    if method is not None:
                        results[method] = msg.error if msg.error else msg.result
                    if not waiting:
                        break
                await proc.stdin.aclose()
                with anyio.move_on_after(_EXIT_WAIT_S):
                    await proc.wait()
    except TimeoutError as exc:
        raise CodexReadError("timeout") from exc
    except (
        anyio.IncompleteRead,
        anyio.EndOfStream,
        anyio.ClosedResourceError,
        anyio.BrokenResourceError,
        CodexRpcClosed,
        CodexRpcWriteTimeout,
    ) as exc:
        raise CodexReadError("exited") from exc
    except OSError as exc:
        raise CodexReadError("spawn", detail=type(exc).__name__) from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return support.version, results


# --- parsed shapes -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Window:
    label: str  # "5h", "7d", …
    pct: int
    resets_at: int | None  # epoch seconds


@dataclass(frozen=True, slots=True)
class Bucket:
    name: str
    windows: tuple[Window, ...]
    reached_type: str | None = None


@dataclass(frozen=True, slots=True)
class CodexUsage:
    auth_kind: str | None  # chatgpt / apiKey / amazonBedrock / gcp / None
    plan: str | None
    allowed: bool | None  # ordinaryUsageAllowed: never inferred from %
    buckets: tuple[Bucket, ...] = ()
    has_credits: bool = False
    unlimited: bool = False
    credits_balance: str | None = None
    reset_credits: int | None = None

    @property
    def reached_type(self) -> str | None:
        return next((b.reached_type for b in self.buckets if b.reached_type), None)


@dataclass(frozen=True, slots=True)
class ModelInfo:
    levels: tuple[str, ...]
    descriptions: dict[str, str]
    default_level: str | None
    hidden: bool = False


@dataclass(frozen=True, slots=True)
class ConfigInfo:
    model: str | None
    effort: str | None
    web_search: str | None


@dataclass(frozen=True, slots=True)
class CodexCatalogue:
    models: dict[str, ModelInfo]
    default_model: str | None
    config: ConfigInfo | None
    web_search_supported: bool | None
    # configRequirements: None = no managed restriction.
    web_search_allowed: tuple[str, ...] | None
    auth_kind: str | None
    plan: str | None


def window_label(mins: Any) -> str:
    if not isinstance(mins, int) or mins <= 0:
        return "limit"
    if mins % 1440 == 0:
        return f"{mins // 1440}d"
    if mins % 60 == 0:
        return f"{mins // 60}h"
    return f"{mins}m"


def time_until(resets_at: int, now: float | None = None) -> str:
    """Epoch seconds → ``2d 4h`` / ``1h 14m`` / ``9m`` from now."""
    left = max(0, int(resets_at - (time.time() if now is None else now)))
    hours, rem = divmod(left, 3600)
    if hours >= 24:
        return f"{hours // 24}d {hours % 24}h"
    if hours:
        return f"{hours}h {rem // 60}m"
    return f"{rem // 60}m"


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _account(result: Any) -> tuple[str | None, str | None]:
    """(auth kind, plan) from account/read. Email and ids are dropped here."""
    account = result.get("account") if isinstance(result, dict) else None
    if not isinstance(account, dict):
        return None, None
    return _str(account.get("type")), _str(account.get("planType"))


def _parse_bucket(snapshot: dict[str, Any]) -> Bucket:
    windows: list[Window] = []
    for slot in ("primary", "secondary"):
        raw = snapshot.get(slot)
        if not isinstance(raw, dict) or not isinstance(
            raw.get("usedPercent"), int | float
        ):
            continue
        resets = raw.get("resetsAt")
        windows.append(
            Window(
                label=window_label(raw.get("windowDurationMins")),
                pct=int(raw["usedPercent"]),
                resets_at=resets if isinstance(resets, int) else None,
            )
        )
    name = _str(snapshot.get("limitName")) or _str(snapshot.get("limitId")) or "codex"
    return Bucket(
        name=name,
        windows=tuple(windows),
        reached_type=_str(snapshot.get("rateLimitReachedType")),
    )


def _snapshots(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every bucket: ``rateLimitsByLimitId``, else the single ``rateLimits``."""
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict) and by_id:
        return {str(k): dict(v) for k, v in by_id.items() if isinstance(v, dict)}
    single = result.get("rateLimits")
    if isinstance(single, dict):
        return {_str(single.get("limitId")) or "codex": dict(single)}
    return {}


@dataclass(slots=True)
class _UsageRaw:
    """The last rate-limit read, kept raw so sparse pushes can merge in."""

    auth_kind: str | None
    plan: str | None
    allowed: bool | None
    snapshots: dict[str, dict[str, Any]]
    reset_credits: int | None

    def build(self) -> CodexUsage:
        buckets = tuple(_parse_bucket(s) for s in self.snapshots.values())
        credits: dict[str, Any] = {}
        plan = self.plan
        for snap in self.snapshots.values():
            if isinstance(snap.get("credits"), dict) and not credits:
                credits = snap["credits"]
            plan = plan or _str(snap.get("planType"))
        return CodexUsage(
            auth_kind=self.auth_kind,
            plan=plan,
            allowed=self.allowed,
            buckets=buckets,
            has_credits=credits.get("hasCredits") is True,
            unlimited=credits.get("unlimited") is True,
            credits_balance=_str(credits.get("balance")),
            reset_credits=self.reset_credits,
        )


def parse_usage(account: Any, limits: Any) -> _UsageRaw:
    """account/read + account/rateLimits/read → raw usage. Raises
    ``CodexNotSignedIn`` / ``CodexReadError``."""
    if isinstance(account, RpcError):
        raise CodexReadError("error", code=account.code, detail=account.message)
    auth_kind, plan = _account(account)
    if auth_kind is None:
        raise CodexNotSignedIn()
    if auth_kind != "chatgpt":
        # API key / Bedrock / GCP: no ChatGPT plan quota to show.
        return _UsageRaw(auth_kind, None, None, {}, None)
    if isinstance(limits, RpcError):
        if _AUTH_REQUIRED in limits.message:
            raise CodexNotSignedIn()
        raise CodexReadError("error", code=limits.code, detail=limits.message)
    if not isinstance(limits, dict):
        raise CodexReadError("error", detail="no rate limits")
    allowed = limits.get("ordinaryUsageAllowed")
    resets = limits.get("rateLimitResetCredits")
    count = resets.get("availableCount") if isinstance(resets, dict) else None
    return _UsageRaw(
        auth_kind=auth_kind,
        plan=plan,
        allowed=allowed if isinstance(allowed, bool) else None,
        snapshots=_snapshots(limits),
        reset_credits=count if isinstance(count, int) else None,
    )


_LOGGED_UNKNOWN_EFFORTS: set[str] = set()


def parse_catalogue(results: dict[str, Any]) -> CodexCatalogue:
    """The catalogue reads → ``CodexCatalogue``. ``model/list`` must answer;
    the others may fail (older CLI, no provider capability) and read as
    unknown."""
    listed = results.get("model/list")
    if isinstance(listed, RpcError) or not isinstance(listed, dict):
        detail = listed.message if isinstance(listed, RpcError) else "no model list"
        raise CodexReadError("error", detail=detail)
    models: dict[str, ModelInfo] = {}
    default_model: str | None = None
    for raw in listed.get("data") or ():
        if not isinstance(raw, dict) or not _str(raw.get("id")):
            continue
        descriptions: dict[str, str] = {}
        for effort in raw.get("supportedReasoningEfforts") or ():
            name = (
                _str(effort.get("reasoningEffort"))
                if isinstance(effort, dict)
                else None
            )
            if name is None:
                continue
            if name not in CODEX_REASONING_VOCAB:
                if name not in _LOGGED_UNKNOWN_EFFORTS:
                    _LOGGED_UNKNOWN_EFFORTS.add(name)
                    logger.info("codex.reads.unknown_effort", effort=name[:40])
                continue
            descriptions[name] = (_str(effort.get("description")) or "")[:80]
        model_id = raw["id"]
        models[model_id] = ModelInfo(
            levels=tuple(v for v in CODEX_REASONING_VOCAB if v in descriptions),
            descriptions=descriptions,
            default_level=_str(raw.get("defaultReasoningEffort")),
            hidden=raw.get("hidden") is True,
        )
        if raw.get("isDefault") is True:
            default_model = model_id
    config: ConfigInfo | None = None
    read = results.get("config/read")
    cfg = read.get("config") if isinstance(read, dict) else None
    if isinstance(cfg, dict):
        config = ConfigInfo(
            model=_str(cfg.get("model")),
            effort=_str(cfg.get("model_reasoning_effort")),
            web_search=_str(cfg.get("web_search")),
        )
    caps = results.get("modelProvider/capabilities/read")
    web_search = caps.get("webSearch") if isinstance(caps, dict) else None
    reqs = results.get("configRequirements/read")
    reqs = reqs.get("requirements") if isinstance(reqs, dict) else None
    allowed = reqs.get("allowedWebSearchModes") if isinstance(reqs, dict) else None
    auth_kind, plan = _account(results.get("account/read"))
    return CodexCatalogue(
        models=models,
        default_model=default_model,
        config=config,
        web_search_supported=web_search if isinstance(web_search, bool) else None,
        web_search_allowed=(
            tuple(v for v in allowed if isinstance(v, str))
            if isinstance(allowed, list)
            else None
        ),
        auth_kind=auth_kind,
        plan=plan,
    )


# --- caches ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReadStats:
    last_success_wall: float | None
    age_s: float | None
    last_error_kind: str | None
    version: str | None
    auth_kind: str | None


class _ReadCache[V]:
    """TTL cache with a failure TTL and one lock per key (two callers, one
    spawn). Keys start with the codex command; ``peek`` never fetches."""

    def __init__(self, *, ttl_s: float, failure_ttl_s: float) -> None:
        self.ttl_s = ttl_s
        self.failure_ttl_s = failure_ttl_s
        self.reset()

    def reset(self) -> None:
        self.values: dict[tuple[Any, ...], tuple[float, V]] = {}
        self._failures: dict[tuple[Any, ...], tuple[float, CodexReadError]] = {}
        self._locks: dict[tuple[Any, ...], anyio.Lock] = {}
        self.last_success_wall: float | None = None
        self.last_error_kind: str | None = None
        self.last_version: str | None = None

    def _fresh(
        self, key: tuple[Any, ...], max_age_s: float | None = None
    ) -> tuple[V, float] | None:
        hit = self.values.get(key)
        if hit is None:
            return None
        age = time.monotonic() - hit[0]
        limit = self.ttl_s if max_age_s is None else min(self.ttl_s, max_age_s)
        return (hit[1], age) if age < limit else None

    def peek(
        self, match: Callable[[tuple[Any, ...]], bool], max_age_s: float
    ) -> tuple[V, float] | None:
        """The freshest value whose key matches, if no older than *max_age_s*."""
        best: tuple[float, V] | None = None
        for key, (at, value) in self.values.items():
            if match(key) and (best is None or at > best[0]):
                best = (at, value)
        if best is None:
            return None
        age = time.monotonic() - best[0]
        return (best[1], age) if age < max_age_s else None

    async def get(
        self,
        key: tuple[Any, ...],
        fetch: Callable[[], Awaitable[V]],
        *,
        max_age_s: float | None = None,
    ) -> tuple[V, float]:
        hit = self._fresh(key, max_age_s)
        if hit is not None:
            return hit
        lock = self._locks.setdefault(key, anyio.Lock())
        async with lock:
            hit = self._fresh(key, max_age_s)
            if hit is not None:
                return hit
            failed = self._failures.get(key)
            if failed is not None and time.monotonic() - failed[0] < self.failure_ttl_s:
                raise failed[1]
            try:
                value = await fetch()
            except CodexReadError as exc:
                self._failures[key] = (time.monotonic(), exc)
                self.last_error_kind = exc.kind
                raise
            self._failures.pop(key, None)
            self.values[key] = (time.monotonic(), value)
            self.last_success_wall = time.time()
            self.last_error_kind = None
            return value, 0.0

    def stats(self, auth_kind: str | None) -> ReadStats:
        ages = [time.monotonic() - at for at, _ in self.values.values()]
        return ReadStats(
            last_success_wall=self.last_success_wall,
            age_s=min(ages) if ages else None,
            last_error_kind=self.last_error_kind,
            version=self.last_version,
            auth_kind=auth_kind,
        )


_USAGE: _ReadCache[_UsageRaw] = _ReadCache(
    ttl_s=USAGE_TTL_S, failure_ttl_s=USAGE_FAILURE_TTL_S
)
_CATALOGUE: _ReadCache[CodexCatalogue] = _ReadCache(
    ttl_s=CATALOGUE_TTL_S, failure_ttl_s=CATALOGUE_FAILURE_TTL_S
)
# cmd → monotonic time of the last app-server push merged into the usage cache.
_PUSHED_AT: dict[str, float] = {}
_LAST_WARM: dict[tuple[str, str | None], float] = {}


def reset_caches() -> None:
    _USAGE.reset()
    _CATALOGUE.reset()
    _PUSHED_AT.clear()
    _LAST_WARM.clear()


async def _version(cmd: str) -> str | None:
    from .codex_app_server import codex_app_server_support

    support = await anyio.to_thread.run_sync(codex_app_server_support, cmd)
    if not support.ok:
        raise CodexReadUnavailable(support.version)
    return support.version


async def get_usage(runner: CodexRunner) -> tuple[CodexUsage, float]:
    """Quota from the 60 s cache or one read: ``(usage, age_s)``."""
    cmd = runner.command()
    version = await _version(cmd)
    _USAGE.last_version = version

    async def fetch() -> _UsageRaw:
        start = time.monotonic()
        _, results = await app_server_read(
            runner,
            [
                ("account/read", {"refreshToken": False}),
                ("account/rateLimits/read", {"excludeResetCreditDetails": True}),
            ],
        )
        raw = parse_usage(
            results.get("account/read"), results.get("account/rateLimits/read")
        )
        logger.info(
            "codex.reads.usage",
            elapsed_ms=int((time.monotonic() - start) * 1000),
            auth=raw.auth_kind,
            plan=raw.plan,
            buckets=len(raw.snapshots),
            allowed=raw.allowed,
        )
        return raw

    try:
        raw, age = await _USAGE.get((cmd, version), fetch)
    except CodexReadError as exc:
        logger.warning("codex.reads.usage_failed", kind=exc.kind, code=exc.code)
        raise
    return raw.build(), age


def peek_usage(cmd: str, *, max_age_s: float = FOOTER_MAX_AGE_S) -> CodexUsage | None:
    """What a read or an app-server push last cached for *cmd*. Never spawns."""
    hit = _USAGE.peek(lambda key: key[0] == cmd, float("inf"))
    if hit is None:
        return None
    raw, age = hit
    pushed = _PUSHED_AT.get(cmd)
    if pushed is not None:
        age = min(age, time.monotonic() - pushed)
    return raw.build() if age < max_age_s else None


def merge_push(cmd: str, params: Any) -> None:
    """``account/rateLimits/updated``: merge the sparse snapshot into the
    cached read. Non-null fields overwrite; null never clears (schema)."""
    snapshot = params.get("rateLimits") if isinstance(params, dict) else None
    if not isinstance(snapshot, dict):
        return
    hit = _USAGE.peek(lambda key: key[0] == cmd, float("inf"))
    if hit is None:
        # No read yet: keep it, aged so `/usage` still reads (no credits or
        # allowed flag in a push).
        raw = _UsageRaw("chatgpt", None, None, {}, None)
        key: tuple[Any, ...] = (cmd, _USAGE.last_version)
        _USAGE.values[key] = (time.monotonic() - USAGE_TTL_S, raw)
    else:
        raw = hit[0]
    limit_id = _str(snapshot.get("limitId")) or next(iter(raw.snapshots), "codex")
    current = raw.snapshots.setdefault(limit_id, {})
    fields = [name for name, value in snapshot.items() if value is not None]
    for field_name in fields:
        current[field_name] = snapshot[field_name]
    _PUSHED_AT[cmd] = time.monotonic()
    logger.info("codex.reads.quota_push_merged", limit_id=limit_id, fields=fields)


def usage_stats() -> ReadStats:
    hit = _USAGE.peek(lambda _key: True, float("inf"))
    return _USAGE.stats(hit[0].auth_kind if hit else None)


async def get_catalogue(
    runner: CodexRunner, cwd: str | None, *, max_age_s: float | None = None
) -> CodexCatalogue:
    """Models, levels, the user's config for *cwd* and web-search capability,
    from the 1 h cache (or one no older than *max_age_s*) or one read."""
    cmd = runner.command()
    version = await _version(cmd)
    _CATALOGUE.last_version = version

    async def fetch() -> CodexCatalogue:
        start = time.monotonic()
        config_params: dict[str, Any] = {"includeLayers": False}
        if cwd:
            config_params["cwd"] = cwd
        _, results = await app_server_read(
            runner,
            [
                ("account/read", {"refreshToken": False}),
                ("model/list", {"limit": 100, "includeHidden": True}),
                ("config/read", config_params),
                ("modelProvider/capabilities/read", {}),
                ("configRequirements/read", None),
            ],
        )
        catalogue = parse_catalogue(results)
        logger.info(
            "codex.reads.catalogue",
            elapsed_ms=int((time.monotonic() - start) * 1000),
            models=len(catalogue.models),
            default_model=catalogue.default_model,
            auth=catalogue.auth_kind,
        )
        return catalogue

    try:
        catalogue, _ = await _CATALOGUE.get(
            (cmd, version, cwd), fetch, max_age_s=max_age_s
        )
    except CodexReadError as exc:
        logger.warning("codex.reads.catalogue_failed", kind=exc.kind, code=exc.code)
        raise
    return catalogue


def peek_catalogue(cmd: str, cwd: str | None = None) -> CodexCatalogue | None:
    """The cached catalogue for *cwd* (None: the freshest for any cwd)."""
    hit = _CATALOGUE.peek(
        lambda key: key[0] == cmd and (cwd is None or key[2] == cwd),
        CATALOGUE_TTL_S,
    )
    return hit[0] if hit else None


def effective_model(catalogue: CodexCatalogue) -> str | None:
    """Codex's own resolution: the config's model, else ``isDefault``."""
    if catalogue.config is not None and catalogue.config.model:
        return catalogue.config.model
    return catalogue.default_model


def peek_effective_model(cmd: str, cwd: str | None) -> str | None:
    """The model Codex runs with no chat override, if a read for *cwd* is
    cached (None: the freshest read). Never a guess (#1360)."""
    catalogue = peek_catalogue(cmd, cwd)
    if catalogue is None or catalogue.config is None:
        return None
    return effective_model(catalogue)


def peek_levels(cmd: str, model: str | None) -> tuple[str, ...] | None:
    """The cached levels of *model* (None: the effective model), or None when
    the catalogue isn't cached or doesn't list it."""
    catalogue = peek_catalogue(cmd)
    if catalogue is None:
        return None
    model = model or effective_model(catalogue)
    info = catalogue.models.get(model) if model else None
    return info.levels if info is not None else None


def maybe_warm_up(runner: CodexRunner, cwd: str | None) -> None:
    """DEC-27: on the first Codex run per process for *cwd*, then at most
    hourly, read the catalogue and usage in the background. Never awaited
    by the run; failures are logged by the caches and dropped."""
    if not WARMUP_ENABLED:
        return
    cmd = runner.command()
    key = (cmd, cwd)
    now = time.monotonic()
    last = _LAST_WARM.get(key)
    if last is not None and now - last < WARMUP_INTERVAL_S:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _LAST_WARM[key] = now

    async def warm() -> None:
        for read in (get_catalogue(runner, cwd), get_usage(runner)):
            try:
                await read
            except CodexReadError:
                pass  # logged by the cache
            except Exception as exc:  # noqa: BLE001 — a warm-up never breaks a run
                logger.warning("codex.reads.warmup_failed", error=type(exc).__name__)

    task = loop.create_task(warm())
    _WARM_TASKS.add(task)
    task.add_done_callback(_WARM_TASKS.discard)


_WARM_TASKS: set[asyncio.Task[None]] = set()


# --- run footer and cap-hit reset (#966) ------------------------------------

_FOOTER_WARN_PCT = 70  # Claude's footer thresholds (runner_bridge)
_FOOTER_CRITICAL_PCT = 90
_FOOTER_NAME_CHARS = 40


def limit_resets_at(usage: CodexUsage) -> int | None:
    """When the limit that stops Codex resets: the earliest reset of a window
    at 100 % or more, else the reset of the fullest window."""
    windows = [w for b in usage.buckets for w in b.windows if w.resets_at]
    full = [w.resets_at for w in windows if w.pct >= 100 and w.resets_at]
    if full:
        return min(full)
    if not windows:
        return None
    return max(windows, key=lambda w: w.pct).resets_at


def _window_text(window: Window, now: float | None) -> str:
    reset = (
        f" ({time_until(window.resets_at, now)})"
        if window.resets_at and window.pct >= 50
        else ""
    )
    return f"{window.label}: {window.pct}%{reset}"


def format_usage_footer(
    usage: CodexUsage, *, always_show: bool, now: float | None = None
) -> list[str]:
    """Plain-text footer lines from a cached read (never a fetch).

    Blocked (``ordinaryUsageAllowed`` false) → one 🛑 line with the reset.
    *always_show* → a compact ``⚡ 7d: 8%`` line per bucket. Otherwise only
    buckets with a window at 70 % or more (⚠️ from 90 %)."""
    if usage.auth_kind not in (None, "chatgpt"):
        return []
    if usage.allowed is False:
        resets = limit_resets_at(usage)
        reset = f" — resets in {time_until(resets, now)}" if resets else ""
        return [f"\U0001f6d1 Codex usage limit reached{reset}"]
    many = len(usage.buckets) > 1
    lines: list[str] = []
    for bucket in usage.buckets:
        if not bucket.windows:
            continue
        worst = max(w.pct for w in bucket.windows)
        if not always_show and worst < _FOOTER_WARN_PCT:
            continue
        icon = "⚡"
        if not always_show and worst >= _FOOTER_CRITICAL_PCT:
            icon = "⚠️"
        name = " ".join(bucket.name.replace("`", "").split())[:_FOOTER_NAME_CHARS]
        prefix = f"{name} " if many else ""
        parts = " | ".join(_window_text(w, now) for w in bucket.windows)
        lines.append(f"{icon} {prefix}{parts}")
    return lines
