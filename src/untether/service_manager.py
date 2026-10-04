"""Which service manager is running this process, and how to restart it (#927).

Restart-required notices used to hardcode the default systemd user unit. That is
wrong on multi-instance hosts (lba-1 runs staging, dev, dev-hf … side by side, so
the default name points at a *different* instance) and fails outright on macOS,
where the job runs under launchd.

This module derives the real restart command from the runtime environment,
stdlib only (same family as :mod:`untether.sdnotify`), and fails closed to
``None`` so callers fall back to generic wording — never a confidently wrong
command.

- **systemd** sets no unit-name variable. The unit comes from the cgroup
  (``/proc/self/cgroup``, as libsystemd's ``sd_pid_get_unit`` does), gated on
  ``INVOCATION_ID`` (we run inside *some* unit) and ``SYSTEMD_EXEC_PID == pid``
  (we are the unit's main process, not a child that inherited its environment,
  e.g. a manual run from an agent shell spawned under staging).
- **launchd** sets ``XPC_SERVICE_NAME`` to the job label. Terminal-launched
  processes see ``0`` or ``application.<bundle-id>.<n>``, both treated as
  unknown.
"""

from __future__ import annotations

import functools
import os
import re
import sys
from collections.abc import Mapping
from typing import Literal

from .logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "detect_restart_command",
    "log_service_detection",
    "parse_cgroup_unit",
    "restart_command",
    "restart_hint",
]

# systemd's unit-name charset; also guarantees no backtick/space can break the
# Markdown code span the command is rendered in.
_UNIT_LEAF_RE = re.compile(r"^[A-Za-z0-9:_.\\@-]+\.service$")
_USER_MANAGER_RE = re.compile(r"/user@\d+\.service/")
_LAUNCHD_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _cgroup_path(cgroup_text: str) -> str | None:
    """Return the systemd cgroup path: the v2 ``0::`` line, else the v1
    ``name=systemd:`` line."""
    v1: str | None = None
    for raw in cgroup_text.splitlines():
        line = raw.strip()
        if line.startswith("0::"):
            return line[3:]
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[1] == "name=systemd":
            v1 = parts[2]
    return v1


def parse_cgroup_unit(
    cgroup_text: str,
) -> tuple[Literal["user", "system"], str] | None:
    """Return ``(kind, unit_name_without_.service)`` for a service cgroup.

    ``None`` for scopes (ssh sessions, tmux, ``systemd-run --scope``), the
    root cgroup (containers), the user manager itself, or any leaf outside the
    unit-name charset.
    """
    path = _cgroup_path(cgroup_text)
    if not path:
        return None
    leaf = path.rstrip("/").rsplit("/", 1)[-1]
    if not leaf or not _UNIT_LEAF_RE.match(leaf) or leaf.startswith("user@"):
        return None
    name = leaf[: -len(".service")]
    kind: Literal["user", "system"] = (
        "user" if _USER_MANAGER_RE.search(path + "/") else "system"
    )
    return kind, name


def _systemd_exec_pid_match(environ: Mapping[str, str], pid: int) -> bool | None:
    """``True``/``False`` when ``SYSTEMD_EXEC_PID`` is set (parse failure →
    ``False``), ``None`` when it is absent (systemd < 248)."""
    raw = environ.get("SYSTEMD_EXEC_PID")
    if raw is None:
        return None
    try:
        return int(raw) == pid
    except ValueError:
        return False


def _launchd_label(environ: Mapping[str, str]) -> str | None:
    label = environ.get("XPC_SERVICE_NAME", "")
    if not label or label == "0" or label.startswith("application."):
        return None
    if not _LAUNCHD_LABEL_RE.match(label):
        return None
    return label


def _detect(
    *,
    environ: Mapping[str, str],
    cgroup_text: str | None,
    pid: int,
    uid: int,
    platform: str,
) -> tuple[str, str | None, str | None]:
    """Return ``(manager, unit_or_label, command)``; manager is ``unknown``
    when nothing was safely detected."""
    if platform.startswith("linux"):
        if not environ.get("INVOCATION_ID"):
            return "unknown", None, None
        if _systemd_exec_pid_match(environ, pid) is False:
            return "unknown", None, None
        if cgroup_text is None:
            return "unknown", None, None
        parsed = parse_cgroup_unit(cgroup_text)
        if parsed is None:
            return "unknown", None, None
        kind, name = parsed
        if kind == "user":
            return "systemd-user", name, f"systemctl --user restart {name}"
        prefix = "" if uid == 0 else "sudo "
        return "systemd-system", name, f"{prefix}systemctl restart {name}"
    if platform == "darwin":
        label = _launchd_label(environ)
        if label is None:
            return "unknown", None, None
        domain = "system" if uid == 0 else f"gui/{uid}"
        return "launchd", label, f"launchctl kickstart -k {domain}/{label}"
    return "unknown", None, None


def detect_restart_command(
    *,
    environ: Mapping[str, str],
    cgroup_text: str | None,
    pid: int,
    uid: int,
    platform: str,
) -> str | None:
    """Pure detection from explicit inputs (testable). ``None`` = unknown."""
    return _detect(
        environ=environ, cgroup_text=cgroup_text, pid=pid, uid=uid, platform=platform
    )[2]


def _read_self_cgroup() -> str | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _detect_live() -> tuple[str, str | None, str | None]:
    try:
        return _detect(
            environ=os.environ,
            cgroup_text=_read_self_cgroup(),
            pid=os.getpid(),
            uid=os.getuid(),
            platform=sys.platform,
        )
    except Exception as exc:  # noqa: BLE001 — never break a notice
        logger.debug("service.detect_failed", error=str(exc))
        return "unknown", None, None


@functools.cache
def restart_command() -> str | None:
    """The restart command for the running service, or ``None`` when unknown.

    Cached for the process lifetime: the unit/label can't change without a
    restart.
    """
    return _detect_live()[2]


def restart_hint() -> str:
    """Markdown-safe "how to apply" phrase used by every restart notice.

    ``/restart`` only stops the process (``request_shutdown``); a supervisor
    relaunches it. So the detected hint offers it unconditionally (every
    detected manager here restarts the job), while the unknown hint must not
    promise it relaunches an unsupervised foreground process.
    """
    cmd = restart_command()
    if cmd:
        return f"run `{cmd}` (or send `/restart`)"
    return (
        "restart Untether's service "
        "(or send `/restart` if a service manager keeps it running)"
    )


def log_service_detection() -> None:
    """Log one INFO line at startup: the rollout evidence for #927."""
    try:
        manager, unit, _cmd = _detect_live()
        exec_pid_match = _systemd_exec_pid_match(os.environ, os.getpid())
        logger.info(
            "service.detected",
            manager=manager,
            unit=unit,
            exec_pid_match=exec_pid_match,
        )
    except Exception as exc:  # noqa: BLE001 — must never block startup
        logger.debug("service.detect_log_failed", error=str(exc))
