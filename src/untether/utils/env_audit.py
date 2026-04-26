"""Runtime audit of an engine subprocess's actual ``/proc/<pid>/environ``
against :mod:`untether.utils.env_policy` (#361).

Defensive instrumentation: even though :func:`~untether.utils.env_policy.filtered_env`
strips disallowed names at spawn time, downstream tooling (the engine CLI
itself, an MCP wrapper script, PAM /etc/environment, login shells) can
re-introduce host vars before the first tool runs. This module samples
the live process env and returns the disallowed names so the runner can
warn (one structured log per session per leaked name).

Linux-only — non-Linux platforms return an empty result silently. Best-
effort — read errors (process gone, EPERM, missing /proc) return empty
without raising.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable

from .env_policy import is_allowed_with_extras


def read_proc_environ(pid: int) -> dict[str, str] | None:
    """Parse ``/proc/<pid>/environ`` into a name→value mapping.

    Returns None on non-Linux platforms or any read error (process exited,
    permission denied, missing file). Never raises.
    """
    if sys.platform != "linux":
        return None
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    out: dict[str, str] = {}
    for chunk in raw.split(b"\x00"):
        if not chunk or b"=" not in chunk:
            continue
        key, _, value = chunk.partition(b"=")
        out[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return out


def audit_proc_env(
    pid: int,
    *,
    expected_extras: Iterable[str] = (),
    user_extra_exact: Iterable[str] = (),
    user_extra_prefix: Iterable[str] = (),
) -> list[str]:
    """Return sorted names present in ``/proc/<pid>/environ`` that aren't
    in the env_policy allowlist.

    Empty list = clean (or non-Linux / unreadable). The caller decides
    whether to log a warning per leaked name.

    ``expected_extras`` lets the caller permit per-engine vars that
    aren't in the global allowlist (e.g. a runner sets a specific
    ``X_INTERNAL_TOKEN`` itself).

    ``user_extra_exact`` / ``user_extra_prefix`` (#409) thread per-
    deployment user extras through so audit doesn't false-flag names the
    user opted into via ``[security] env_extra_allow``.
    """
    env = read_proc_environ(pid)
    if not env:
        return []
    runner_extras = frozenset(expected_extras)
    return sorted(
        name
        for name in env
        if name not in runner_extras
        and not is_allowed_with_extras(
            name,
            extra_exact=user_extra_exact,
            extra_prefix=user_extra_prefix,
        )
    )


__all__ = ["audit_proc_env", "read_proc_environ"]
