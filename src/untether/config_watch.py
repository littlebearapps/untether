from __future__ import annotations

import hashlib
import os
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from watchfiles import awatch

from .config import ConfigError
from .ids import RESERVED_CHAT_COMMANDS
from .logging import get_logger
from .runtime_loader import RuntimeSpec, build_runtime_spec
from .settings import UntetherSettings, load_settings
from .transport_runtime import TransportRuntime

logger = get_logger(__name__)

__all__ = [
    "ConfigReload",
    "config_status",
    "watch_config",
]


@dataclass(frozen=True, slots=True)
class ConfigReload:
    settings: UntetherSettings
    runtime_spec: RuntimeSpec
    config_path: Path


def _snapshot(path: Path) -> tuple[str, bytes | None, int | None]:
    """#839: ``(status, sha256 digest, st_mtime_ns)`` of the config file.

    The digest is of the bytes on disk, so a same-size edit inside one coarse
    timestamp tick (kernels < 6.13) is still seen as a change. A read error
    (incl. a file deleted between stat and read) counts as ``missing``.
    """
    try:
        stat = path.stat()
    except OSError:
        return "missing", None, None
    if not path.is_file():
        return "invalid", None, None
    try:
        data = path.read_bytes()
    except OSError:
        return "missing", None, None
    return "ok", hashlib.sha256(data).digest(), stat.st_mtime_ns


def config_status(path: Path) -> tuple[str, bytes | None]:
    """``(status, content digest)``; the digest is ``None`` unless ``ok``."""
    status, digest, _ = _snapshot(path)
    return status, digest


def _digest_label(digest: bytes | None) -> str | None:
    return digest.hex()[:12] if digest is not None else None


def _resolve(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path


def _event_matches(candidate: str, link_path: Path, target: Path) -> bool:
    """#839: an event is for the config if it names the (possibly symlinked)
    config path itself, or a path that resolves to its current target."""
    try:
        candidate_path = Path(candidate)
        if candidate_path == link_path:
            return True
        return candidate_path.resolve(strict=False) == target
    except OSError:
        return False


def _reload_config(
    config_path: Path,
    default_engine_override: str | None,
    reserved: tuple[str, ...],
    *,
    report_path: Path | None = None,
) -> ConfigReload:
    """Load + validate *config_path* into a runtime spec.

    #839: the watcher loads (and migrates) through the resolved target, so a
    migration rewrite never replaces a symlinked config with a regular file,
    but builds the spec with ``report_path`` (the configured, unresolved path)
    so ``runtime.config_path`` and relative project paths match startup.
    """
    settings, resolved_path = load_settings(config_path)
    spec_path = report_path if report_path is not None else resolved_path
    spec = build_runtime_spec(
        settings=settings,
        config_path=spec_path,
        default_engine_override=default_engine_override,
        reserved=reserved,
        audit_reason="reload",
    )
    return ConfigReload(
        settings=settings,
        runtime_spec=spec,
        config_path=spec_path,
    )


async def watch_config(
    *,
    config_path: Path,
    runtime: TransportRuntime,
    default_engine_override: str | None = None,
    reserved: Iterable[str] = RESERVED_CHAT_COMMANDS,
    on_reload: Callable[[ConfigReload], Awaitable[None]] | None = None,
) -> None:
    # Mutmut sets MUTANT_UNDER_TEST; disable watchers to avoid native crashes.
    if os.environ.get("MUTANT_UNDER_TEST"):
        return
    reserved_tuple = tuple(reserved)
    # #839: keep the configured path (it may be a symlink) next to its
    # resolved target. Load through the target; report the link.
    link_path = config_path.expanduser().absolute()
    target = _resolve(link_path)
    roots = sorted({link_path.parent, target.parent})
    status, current, _ = _snapshot(target)
    # Content we last applied successfully. Survives missing/invalid, so a
    # restore with identical bytes doesn't reload a config already in place.
    seen: bytes | None = current if status == "ok" else None
    # A failed reload is keyed on content AND mtime, so an identical event
    # batch doesn't re-log the failure but ``touch`` still retries (the error
    # may depend on state outside the file, e.g. an engine missing on PATH).
    failed: tuple[bytes, int | None] | None = None
    last_status = status
    if status != "ok":
        logger.warning("config.watch.unavailable", path=str(target), status=status)

    # Only the directories' direct entries matter, so no recursive watch.
    async for changes in awatch(*roots, recursive=False):
        if not any(_event_matches(path, link_path, target) for _, path in changes):
            continue

        new_target = _resolve(link_path)
        if new_target != target:
            logger.info(
                "config.watch.target_changed",
                old=str(target),
                new=str(new_target),
            )
            if new_target.parent not in roots:
                logger.warning(
                    "config.watch.target_unwatched",
                    path=str(new_target),
                    hint="later in-place edits of this file need a restart",
                )
            target = new_target

        status, current, mtime_ns = _snapshot(target)
        if status != "ok" or current is None:
            if status != last_status:
                logger.warning(
                    "config.watch.unavailable",
                    path=str(target),
                    status=status,
                )
            last_status = status
            continue

        if last_status != "ok":
            logger.info("config.watch.available", path=str(target))
        last_status = status

        if current == seen or (current, mtime_ns) == failed:
            logger.debug("config.watch.unchanged", digest=_digest_label(current))
            continue

        try:
            reload = _reload_config(
                target,
                default_engine_override,
                reserved_tuple,
                report_path=link_path,
            )
        except ConfigError as exc:
            logger.warning("config.reload.failed", error=str(exc))
            failed = (current, mtime_ns)
            continue
        except Exception as exc:  # pragma: no cover - safety net
            logger.exception(
                "config.reload.crashed",
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            failed = (current, mtime_ns)
            continue

        # Record the content acted on BEFORE on_reload runs: an edit that
        # lands while the callback awaits arrives as a new event with a new
        # digest and is reloaded, never silently marked as applied (#839).
        seen = current
        failed = None
        reload.runtime_spec.apply(runtime, config_path=reload.config_path)
        logger.info(
            "config.reload.applied",
            path=str(reload.config_path),
            digest=_digest_label(current),
        )
        if on_reload is not None:
            try:
                await on_reload(reload)
            except Exception as exc:  # pragma: no cover - safety net
                logger.exception(
                    "config.reload.callback_failed",
                    error=str(exc),
                    error_type=exc.__class__.__name__,
                )
