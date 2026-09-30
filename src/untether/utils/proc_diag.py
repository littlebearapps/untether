"""Process diagnostics via platform process APIs.

Collects CPU, memory, TCP, FD, and child process info for stall analysis.
Linux reads /proc; macOS shells out to `ps` (#689 — a partial backend
supplying state + CPU ticks, enough to drive the liveness gates). Other
platforms return None.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ProcessDiag:
    pid: int
    alive: bool
    state: str | None = None  # R/S/D/Z (/proc) or R/S/I/T/U/Z (BSD ps)
    cpu_utime: int | None = None  # user CPU ticks (Darwin: combined user+sys)
    cpu_stime: int | None = None  # system CPU ticks (Darwin: always 0)
    rss_kb: int | None = None  # VmRSS
    threads: int | None = None  # thread count
    fd_count: int | None = None  # open file descriptors
    tcp_established: int = 0
    tcp_total: int = 0
    child_pids: list[int] = field(default_factory=list)
    tree_cpu_utime: int | None = None  # sum of utime for pid + descendants
    tree_cpu_stime: int | None = None  # sum of stime for pid + descendants


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # #689: EPERM from kill(pid, 0) means the process exists but we may
        # not signal it — that's alive, not dead.
        return True


def _read_stat(pid: int) -> tuple[str | None, int | None, int | None]:
    """Parse /proc/pid/stat for state, utime, stime."""
    try:
        data = open(f"/proc/{pid}/stat", encoding="utf-8").read()  # noqa: SIM115
    except (OSError, FileNotFoundError, PermissionError):
        return None, None, None
    # Fields after the comm (which may contain spaces/parens)
    close_paren = data.rfind(")")
    if close_paren < 0:
        return None, None, None
    fields = data[close_paren + 2 :].split()
    # field 0 = state, field 11 = utime, field 12 = stime (0-indexed after comm)
    state = fields[0] if len(fields) > 0 else None
    utime = int(fields[11]) if len(fields) > 12 else None
    stime = int(fields[12]) if len(fields) > 13 else None
    return state, utime, stime


def pid_starttime(pid: int) -> int | None:
    """#590: return a process's start time (clock ticks since boot,
    /proc/pid/stat field 22) — a stable birth-identity token that survives
    setpgid/setsid/reparenting but NOT PID reuse. Used to verify a recorded
    orphan PID still refers to the same process before signalling it. Returns
    None on non-Linux or any /proc read/parse error.
    """
    if sys.platform != "linux":
        return None
    try:
        data = open(f"/proc/{pid}/stat", encoding="utf-8").read()  # noqa: SIM115
    except (OSError, FileNotFoundError, PermissionError):
        return None
    close_paren = data.rfind(")")
    if close_paren < 0:
        return None
    fields = data[close_paren + 2 :].split()
    # field 22 (starttime) → index 19 after the comm field.
    if len(fields) <= 19:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def _read_status(pid: int) -> tuple[int | None, int | None]:
    """Parse /proc/pid/status for VmRSS and Threads."""
    rss_kb = None
    threads = None
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        rss_kb = int(parts[1])
                elif line.startswith("Threads:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        threads = int(parts[1])
    except (OSError, FileNotFoundError, PermissionError, ValueError):
        pass
    return rss_kb, threads


def _count_fds(pid: int) -> int | None:
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except (OSError, FileNotFoundError, PermissionError):
        return None


def _count_tcp(pid: int) -> tuple[int, int]:
    """Count TCP connections from /proc/pid/net/tcp{,6}."""
    established = 0
    total = 0
    for suffix in ("tcp", "tcp6"):
        try:
            with open(f"/proc/{pid}/net/{suffix}", encoding="utf-8") as f:
                next(f, None)  # skip header
                for line in f:
                    total += 1
                    fields = line.split()
                    # field 3 is connection state; 01 = ESTABLISHED
                    if len(fields) > 3 and fields[3] == "01":
                        established += 1
        except (OSError, FileNotFoundError, PermissionError):
            continue
    return established, total


def mem_available_kb() -> int | None:
    """Return /proc/meminfo MemAvailable in KB, or None on non-Linux / parse fail.

    Used by the pre-spawn RAM guard (#350) to decide whether to allow, warn
    on, or refuse spawning a new engine subprocess. Deliberately uncached —
    callers must read a fresh value on each spawn to catch the near-OOM
    window where a prior heavy run is still consuming memory. Cheap: one
    file open and a single-line grep.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1])
                    return None
    except (OSError, FileNotFoundError, PermissionError, ValueError):
        return None
    return None


def read_cmdline(pid: int) -> str | None:
    """Return /proc/<pid>/cmdline as a space-separated string, or None.

    Used by the stuck-after-tool_result recovery path (#322) to identify
    MCP-adapter child processes like `npx mcp-remote`. Returns None on
    non-Linux platforms, missing PIDs, or permission errors.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except (OSError, FileNotFoundError, PermissionError):
        return None
    if not raw:
        return None
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def read_cmdline_argv(pid: int) -> list[str] | None:
    """Return /proc/<pid>/cmdline as an argv list, or None.

    #690: the self-restart drain evidence scan matches parsed argv tokens
    (executable basename + exact verb + unit name), not substrings — a
    space-joined string (``read_cmdline``) can't distinguish
    ``systemctl restart untether`` from ``echo "systemctl restart untether"``.
    Returns None on non-Linux platforms, missing PIDs, or permission errors.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except (OSError, FileNotFoundError, PermissionError):
        return None
    if not raw:
        return None
    return [
        part.decode("utf-8", errors="replace") for part in raw.split(b"\x00") if part
    ]


def read_wchan(pid: int) -> str | None:
    """Return the kernel wait channel (``/proc/<pid>/wchan``) or None.

    #791: names what a sleeping process is blocked on (``ep_poll``,
    ``do_wait``, ``futex_wait_queue``…) for the live-session close-grace
    diagnostic. None on non-Linux platforms, missing PIDs, or when the
    kernel hides it ("0").
    """
    try:
        with open(f"/proc/{pid}/wchan") as f:
            raw = f.read().strip()
    except (OSError, FileNotFoundError, PermissionError):
        return None
    return raw if raw and raw != "0" else None


_SECRETISH = (
    "token",
    "secret",
    "key",
    "auth",
    "bearer",
    "password",
    "passwd",
    "credential",
    "cookie",
)
# Flags whose value is a header line ("Name: value"): the value is hidden
# whatever the header name, the flag itself stays visible.
_HEADER_FLAGS = frozenset({"-H", "--header", "--headers", "--http-header"})
# Auth schemes that precede the credential proper ("Bearer <token>").
_AUTH_SCHEMES = ("bearer", "basic", "token", "digest")
_REDACTED = "<redacted>"


def _is_secret_token(token: str) -> bool:
    lowered = token.lower()
    if any(word in lowered for word in _SECRETISH):
        return True
    # A JWT (base64url JSON header) needs no keyword to be a credential.
    return token.strip("\"'").startswith("eyJ") and len(token) >= 16


def _opens_secret_value(token: str) -> bool:
    """Does a redacted ``token`` introduce a value that must also go?

    ``--api-key`` (bare flag), ``Authorization:`` / ``X-Api-Key=`` (a name
    awaiting its value) and ``Bearer`` (a scheme awaiting its credential).
    """
    if token.startswith("-") and "=" not in token:
        return True
    lowered = token.lower().strip("\"'")
    return lowered.endswith((":", "=")) or lowered.endswith(_AUTH_SCHEMES)


def _argv_tokens(argv: list[str]) -> list[str]:
    """Whitespace-split every argv element.

    #800: some processes (setproctitle-style MCP servers) carry their whole
    command line in ``argv[0]``, and a single element can hold a full
    header line (``"Authorization: Bearer …"``). Splitting first means the
    executable is just the first word — not ``basename()`` of the title,
    which a later ``/`` in the arguments would cut mid-string — and every
    word goes through the redaction scan.
    """
    tokens: list[str] = []
    for arg in argv:
        tokens.extend(arg.split())
    return tokens


def describe_process(pid: int, *, max_args: int = 5, max_len: int = 80) -> str | None:
    """A short, redacted one-line command description for diagnostics.

    #791: child processes of a wedged Claude CLI are usually MCP servers
    whose argv can carry credentials (``--header Authorization:Bearer …``,
    ``--api-key …``). Keeps the executable basename plus the first
    ``max_args`` arguments and replaces any argument that looks
    secret-bearing — and the value a secret-looking flag, header name or
    auth scheme introduces — with ``<redacted>`` (a contiguous run collapses
    to one marker). #800: ``argv[0]`` is scanned too, elements are split on
    whitespace first, and redaction is decided on the whole token *before*
    truncating to ``max_len``, so no prefix of a secret survives the cut.
    Returns None when the cmdline is unreadable.
    """
    argv = read_cmdline_argv(pid)
    if not argv:
        return None
    tokens = _argv_tokens(argv)
    if not tokens:
        return None

    def _shown(token: str) -> str:
        return token if len(token) <= max_len else token[: max_len - 1] + "…"

    exe = os.path.basename(tokens[0]) or tokens[0]
    out: list[str] = []
    redact_next = False
    if _is_secret_token(tokens[0]):
        out.append(_REDACTED)
        redact_next = _opens_secret_value(tokens[0])
    else:
        out.append(_shown(exe))
    rest = tokens[1:]
    for token in rest[:max_args]:
        if redact_next or _is_secret_token(token):
            if out[-1] != _REDACTED:
                out.append(_REDACTED)
            redact_next = _opens_secret_value(token)
            continue
        out.append(_shown(token))
        redact_next = token in _HEADER_FLAGS
    if len(rest) > max_args:
        out.append(f"(+{len(rest) - max_args} args)")
    return " ".join(out)


_HOOK_PATH_MARKER = "/hooks/"


def hook_script_label(pid: int) -> str | None:
    """#812: is ``pid`` running a Claude Code hook script? Returns a short,
    safe label (the script's basename) or None.

    Matches ``/hooks/`` anywhere in the *raw* argv — not in
    ``describe_process`` output, which shows only the executable's basename
    (``/x/.claude/hooks/stop.sh`` → ``stop.sh``) and cuts tokens at 80 chars
    (plugin hook paths under ``~/.claude/plugins/cache/…`` lose their
    ``/hooks/`` segment), so it can't be matched reliably. Only the matched
    token's basename is returned, and it goes through the #800
    secret-token check first.
    """
    argv = read_cmdline_argv(pid)
    if not argv:
        return None
    for token in _argv_tokens(argv):
        if _HOOK_PATH_MARKER not in token:
            continue
        name = os.path.basename(token.strip("\"';&|()")) or "hook"
        if _is_secret_token(name):
            return _REDACTED
        return name if len(name) <= 60 else name[:59] + "…"
    return None


# #812: Claude Code runs command hooks as ``/bin/sh -c <command>`` direct
# children (Node ``spawn(..., {shell: true})``, observed on CLI 2.1.285). Any
# POSIX shell counts, so a CLI change of hook shell fails safe (a longer
# hold), never early.
_HOOK_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash"})


def _is_shell_c(argv: list[str]) -> bool:
    return (
        len(argv) >= 2
        and os.path.basename(argv[0]) in _HOOK_SHELLS
        and "-c" in argv[1:3]
    )


# #812: the Bash tool (incl. ``run_in_background`` / Monitor) also runs as
# ``<shell> -c …`` under the CLI; its wrapper always ends by recording the cwd
# (``… && eval <cmd> && pwd -P >| <tmp>/claude-<id>-cwd``, CLI 2.1.285). Hook
# commands never carry it. If the wrapper changes, a tool shell counts as a
# hook shell — a longer hold, never an early release.
_TOOL_SHELL_MARKER = " && pwd -P >| "


def _is_tool_shell(command: str) -> bool:
    return _TOOL_SHELL_MARKER in command


# #812: a live session's hook-process view is only as good as its child
# list. Untether spawns ``env -i … claude`` (env execs, so the spawned pid IS
# the CLI) and a native/Node CLI; a user wrapper script that forks instead of
# exec'ing would put the CLI — and every hook — one level down. A spawned pid
# whose argv[0] is a shell or ``env`` with exactly one live child is treated
# as such a wrapper and resolved to that child (at most 3 levels).
_WRAPPER_EXES = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash", "env"})
_WRAPPER_MAX_DEPTH = 3

# /proc start times are in clock ticks (10 ms); macOS ``etime`` in whole
# seconds. A start time is only ever used as "the latest moment this process
# can have started", so the resolution is added on.
_LINUX_START_RESOLUTION_S = 0.02
_DARWIN_START_RESOLUTION_S = 1.0

try:
    _CLK_TCK: int | None = os.sysconf("SC_CLK_TCK")
except (AttributeError, ValueError, OSError):  # pragma: no cover — non-POSIX
    _CLK_TCK = None

# #812: hook frames and child start times are compared on a clock that keeps
# counting through system sleep — a process's age does, and
# ``time.monotonic()`` doesn't (a Mac asleep mid-hook would make the hook
# look older than its frame). CLOCK_BOOTTIME on Linux (the /proc starttime
# clock); on macOS CLOCK_MONOTONIC counts during sleep.
if sys.platform.startswith("linux") and hasattr(time, "CLOCK_BOOTTIME"):
    _HOOK_CLOCK_ID: int | None = time.CLOCK_BOOTTIME
elif sys.platform == "darwin" and hasattr(time, "CLOCK_MONOTONIC"):
    _HOOK_CLOCK_ID = time.CLOCK_MONOTONIC
else:  # pragma: no cover — other platforms have no hook-process backend
    _HOOK_CLOCK_ID = None


def hook_clock() -> float:
    """#812: now, on the clock ``CliChild.started_by`` and a pending hook's
    ``started_clock`` share (counts through system sleep)."""
    if _HOOK_CLOCK_ID is None:  # pragma: no cover
        return time.monotonic()
    return time.clock_gettime(_HOOK_CLOCK_ID)


@dataclass(frozen=True, slots=True)
class CliChild:
    """#812: one live direct child of the Claude CLI."""

    argv: list[str]  # [] when unreadable
    pgid: int | None  # process group (None: unknown)
    # Stable birth token for PID-reuse checks: /proc starttime ticks (Linux),
    # ``lstart`` epoch seconds (macOS). None: unknown.
    start_id: int | None
    # The latest ``hook_clock()`` moment it can have started (None: unknown).
    started_by: float | None


@dataclass(frozen=True, slots=True)
class CliScan:
    """#812: the CLI's live direct children at one moment."""

    cli_pid: int  # the CLI itself (the spawned pid, or a wrapper's child)
    cli_pgid: int | None
    children: dict[int, CliChild]

    def in_cli_group(self, pid: int) -> bool:
        """The child shares the CLI's process group — spawned without
        ``detached`` — which no command hook is (see ``hook_evidence``)."""
        child = self.children.get(pid)
        return (
            child is not None
            and child.pgid is not None
            and self.cli_pgid is not None
            and child.pgid == self.cli_pgid
        )


def _stat_fields(pid: int) -> list[str] | None:
    """/proc/<pid>/stat fields after ``(comm)``: [0] state, [1] ppid,
    [2] pgrp, [3] session, …, [19] starttime. None when unreadable."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            data = f.read()
    except (OSError, ValueError):
        return None
    close_paren = data.rfind(")")
    if close_paren < 0:
        return None
    fields = data[close_paren + 2 :].split()
    return fields or None


def _int_field(fields: list[str] | None, index: int) -> int | None:
    if fields is None or len(fields) <= index:
        return None
    try:
        return int(fields[index])
    except ValueError:
        return None


def _read_uptime() -> float | None:
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _children_from_task_files(pid: int) -> list[int] | None:
    """Children via /proc/<pid>/task/<tid>/children. None when that file is
    missing or unreadable for a thread that still exists — a kernel built
    without CONFIG_PROC_CHILDREN has no such file, and "no children" must
    never be inferred from its absence (#812 review)."""
    task_dir = f"/proc/{pid}/task"
    try:
        tids = os.listdir(task_dir)
    except OSError:
        return None
    children: list[int] = []
    for tid in tids:
        try:
            with open(f"{task_dir}/{tid}/children", encoding="utf-8") as f:
                data = f.read()
        except OSError:
            if os.path.isdir(f"{task_dir}/{tid}"):
                return None  # the thread is there; its children file isn't
            continue  # the thread exited meanwhile
        for tok in data.split():
            with contextlib.suppress(ValueError):
                children.append(int(tok))
    return children


def _children_from_ppid_scan(pid: int) -> list[int] | None:
    """Children via a /proc/*/stat parent-pid scan (the fallback). None when
    /proc can't be listed."""
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    children: list[int] = []
    for name in entries:
        if not name.isdigit():
            continue
        if _int_field(_stat_fields(int(name)), 1) == pid:
            children.append(int(name))
    return children


def direct_children(pid: int) -> list[int] | None:
    """Direct child PIDs of ``pid`` (Linux /proc). Reads the per-thread
    ``children`` files and falls back to a /proc parent-pid scan when they
    are unavailable. None when neither source can be read (or ``pid`` has
    no /proc entry)."""
    children = _children_from_task_files(pid)
    if children is None and os.path.isdir(f"/proc/{pid}"):
        children = _children_from_ppid_scan(pid)
    return children


def _live_children_linux(pid: int) -> list[int] | None:
    children = direct_children(pid)
    if children is None:
        return None
    live: list[int] = []
    for child in children:
        state = (_stat_fields(child) or [None])[0]
        if state is None or state.startswith("Z"):
            continue  # gone, or exited and not reaped yet
        live.append(child)
    return live


def _resolve_wrapper(
    pid: int,
    argv_of: Callable[[int], list[str] | None],
    live_children_of: Callable[[int], list[int] | None],
) -> int:
    cur = pid
    for _ in range(_WRAPPER_MAX_DEPTH):
        argv = argv_of(cur)
        if not argv or os.path.basename(argv[0]) not in _WRAPPER_EXES:
            break
        kids = live_children_of(cur)
        if not kids or len(kids) != 1:
            break
        cur = kids[0]
    return cur


def _cli_children_linux(pid: int) -> CliScan | None:
    cli = _resolve_wrapper(pid, read_cmdline_argv, _live_children_linux)
    children = direct_children(cli)
    if children is None:
        return None
    uptime = _read_uptime()
    now = hook_clock()
    found: dict[int, CliChild] = {}
    for child in children:
        fields = _stat_fields(child)
        if fields is None or fields[0].startswith("Z"):
            continue  # gone, or exited and not reaped yet
        start = _int_field(fields, 19)
        started_by: float | None = None
        if start is not None and uptime is not None and _CLK_TCK:
            age = max(0.0, uptime - start / _CLK_TCK)
            started_by = now - age + _LINUX_START_RESOLUTION_S
        found[child] = CliChild(
            argv=read_cmdline_argv(child) or [],
            pgid=_int_field(fields, 2),
            start_id=start,
            started_by=started_by,
        )
    return CliScan(
        cli_pid=cli, cli_pgid=_int_field(_stat_fields(cli), 2), children=found
    )


def _parse_lstart(tokens: list[str]) -> int | None:
    """``lstart`` under LC_ALL=C (``Wed Sep 30 17:06:50 2026``) → epoch
    seconds. Only an identity token (same process → same string): timing
    uses ``etime``, which has no DST ambiguity."""
    try:
        parsed = time.strptime(" ".join(tokens[1:5]), "%b %d %H:%M:%S %Y")
        return int(time.mktime(parsed))
    except (ValueError, OverflowError, IndexError):
        return None


def _parse_etime(token: str) -> int | None:
    """``etime`` (``[[dd-]hh:]mm:ss``) → elapsed seconds."""
    days, _, rest = token.rpartition("-")
    parts = rest.split(":")
    if not 2 <= len(parts) <= 3:
        return None
    try:
        secs = 0
        for part in parts:
            secs = secs * 60 + int(part)
        return secs + (int(days) * 86400 if days else 0)
    except ValueError:
        return None


def _cli_children_darwin(pid: int) -> CliScan | None:
    try:
        out = subprocess.run(  # nosec B603 — fixed argv, no shell
            ["/bin/ps", "-axo", "pid=,ppid=,pgid=,stat=,etime=,lstart=,command="],
            capture_output=True,
            text=True,
            timeout=2.0,
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    now = hook_clock()
    # pid -> (ppid, pgid, stat, etime s, lstart epoch, argv)
    rows: dict[int, tuple[int, int, str, int | None, int | None, list[str]]] = {}
    for line in out.stdout.splitlines():
        fields = line.split(None, 10)
        if len(fields) < 10:
            continue
        try:
            row_pid, row_ppid, row_pgid = (int(f) for f in fields[:3])
        except ValueError:
            continue
        rows[row_pid] = (
            row_ppid,
            row_pgid,
            fields[3],
            _parse_etime(fields[4]),
            _parse_lstart(fields[5:10]),
            # ``command=`` joins argv with spaces: split on whitespace.
            fields[10].split() if len(fields) == 11 else [],
        )

    def live_children(parent: int) -> list[int]:
        return [
            p for p, r in rows.items() if r[0] == parent and not r[2].startswith("Z")
        ]

    cli = _resolve_wrapper(
        pid,
        lambda p: rows[p][5] if p in rows else None,
        live_children,
    )
    found: dict[int, CliChild] = {}
    for child in live_children(cli):
        _, pgid, _, etime, lstart, argv = rows[child]
        found[child] = CliChild(
            argv=argv,
            pgid=pgid,
            start_id=lstart,
            started_by=(
                None if etime is None else now - etime + _DARWIN_START_RESOLUTION_S
            ),
        )
    return CliScan(
        cli_pid=cli,
        cli_pgid=rows[cli][1] if cli in rows else None,
        children=found,
    )


def cli_children(pid: int) -> CliScan | None:
    """#812: the Claude CLI's live (non-zombie) direct children — argv,
    process group, start time — plus the CLI's own process group. ``pid`` is
    the spawned process; a forking shell/``env`` wrapper is resolved to the
    CLI beneath it. Returns None when the process table can't be read (then
    the caller can't tell, and must not assume no hook is running).
    Blocking (``ps`` on macOS): call from a thread."""
    if os.path.isdir(f"/proc/{pid}"):
        return _cli_children_linux(pid)
    if sys.platform != "darwin":
        return None
    return _cli_children_darwin(pid)


# #812: long-lived CLI children that appear after the session baseline and
# are not hooks — an MCP server (re)started on reconnect, a language server.
# A token-boundary match so a path like ``/home/alsparks`` doesn't count; a
# ``/hooks/`` token always wins (plugin and user hook scripts live there).
_SERVICE_TOKEN = re.compile(
    r"(?<![a-z0-9])(?:mcp|lsp)(?![a-z0-9])|modelcontextprotocol"
    r"|language-?server|langserver"
)


def looks_like_service(argv: list[str]) -> bool:
    lowered = [t.lower() for t in argv]
    if any(_HOOK_PATH_MARKER in t for t in lowered):
        return False
    return any(_SERVICE_TOKEN.search(t) for t in lowered)


# #812: the CLI emits ``hook_started`` and only then spawns the hook
# (CLI 2.1.285: ``QQ(id,name,event); let r = await xU(…)``), so a hook's
# process can't have started before its frame — give or take how late
# Untether read the frame. A child that started more than this long before
# the oldest unpaired hook's frame was read is not any unpaired hook.
HOOK_START_SLACK_S = 5.0

BaselineId = tuple[int, int | None]  # (pid, start_id)


def baseline_ids(scan: CliScan) -> frozenset[BaselineId]:
    """#812: identities of the children a session baseline may exempt — only
    those in the CLI's own process group. The CLI spawns every command hook
    ``detached`` (its own session and process group — CLI 2.1.285
    ``detached: !isWindows``); MCP servers are not detached. So a hook that
    happens to be alive at ``system/init`` (a ``UserPromptSubmit`` hook
    starts before it) is never baselined. (pid, start) guards PID reuse."""
    return frozenset(
        (pid, child.start_id)
        for pid, child in scan.children.items()
        if scan.in_cli_group(pid)
    )


def hook_evidence_children(
    scan: CliScan,
    baseline: frozenset[BaselineId] | None,
    *,
    since: float | None = None,
) -> list[int]:
    """#812: the CLI children that may be a running command hook.

    Hooks run as ``/bin/sh -c <command>``, but a shell that execs a single
    command (bash — macOS ``/bin/sh`` — and zsh do; Linux dash doesn't)
    leaves only the command itself (``bash …/sg-python.sh …``). So any
    direct child counts, except:

    - a Bash-tool shell (``… && pwd -P >| <tmp>/claude-<id>-cwd``);
    - one that started more than ``HOOK_START_SLACK_S`` before ``since``
      (when the oldest unpaired hook's ``hook_started`` was read, on
      ``hook_clock()``) — no unpaired hook's process can predate its frame;
    - one in the CLI's own process group (hooks never are — they are
      spawned detached) that is in the session ``baseline`` (same pid and
      start time: MCP servers up at ``system/init``) or whose argv looks
      like an MCP / LSP server. A ``/hooks/`` argv token always counts.

    Unknown argv / start time / group counts. ``baseline`` None (never
    captured) exempts nothing. Wrong guesses err towards holding (bounded by
    ``async_hook_max_hold``)."""
    found: list[int] = []
    for pid, child in scan.children.items():
        argv = child.argv
        if argv and _is_shell_c(argv) and _is_tool_shell(" ".join(argv[1:])):
            continue
        if (
            since is not None
            and child.started_by is not None
            and child.started_by < since - HOOK_START_SLACK_S
        ):
            continue
        if any(_HOOK_PATH_MARKER in t for t in argv):
            found.append(pid)  # a hook script
            continue
        if scan.in_cli_group(pid) and (
            (baseline is not None and (pid, child.start_id) in baseline)
            or (argv and looks_like_service(argv))
        ):
            continue
        found.append(pid)
    return found


def _find_children(pid: int) -> list[int]:
    """Direct child PIDs (``[]`` when unreadable) — see ``direct_children``."""
    return direct_children(pid) or []


def find_descendants(pid: int, *, _depth: int = 0, _max_depth: int = 4) -> list[int]:
    """Find all descendant PIDs recursively (depth-limited).

    Public helper used by subprocess cleanup (#275) to capture a snapshot of
    the process tree before signalling the parent — grandchildren in separate
    process groups (e.g. vitest → workerd) are only reachable this way.

    Depth-limited to 4 levels to bound the recursion on pathological trees;
    the typical Claude Code → Bash → vitest → workerd chain is 3 deep with
    margin.
    """
    if _depth >= _max_depth:
        return []
    children = _find_children(pid)
    descendants = list(children)
    for child in children:
        descendants.extend(
            find_descendants(child, _depth=_depth + 1, _max_depth=_max_depth)
        )
    return descendants


# Private alias kept for back-compat with existing test imports.
_find_descendants = find_descendants


def _collect_tree_cpu(
    utime: int | None, stime: int | None, descendants: list[int]
) -> tuple[int | None, int | None]:
    """Sum CPU ticks across process + all descendants."""
    if utime is None or stime is None:
        return None, None
    tree_utime = utime
    tree_stime = stime
    for desc_pid in descendants:
        _, d_utime, d_stime = _read_stat(desc_pid)
        if d_utime is not None:
            tree_utime += d_utime
        if d_stime is not None:
            tree_stime += d_stime
    return tree_utime, tree_stime


def _parse_ps_time(raw: str) -> int | None:
    """Parse BSD ps TIME (``M:SS.cc``, ``H:MM:SS``, ``D-HH:MM:SS``) into
    centiseconds. Integer arithmetic only — the format switch from
    minutes/centiseconds to hours/whole-seconds must not introduce a unit
    discontinuity. Returns None on any malformed value."""
    raw = raw.strip()
    if not raw:
        return None
    days = 0
    if "-" in raw:
        day_part, _, raw = raw.partition("-")
        if not day_part.isdigit():
            return None
        days = int(day_part)
    parts = raw.split(":")
    if not 2 <= len(parts) <= 3:
        return None
    sec_part = parts[-1]
    centis = 0
    if "." in sec_part:
        sec_str, _, frac = sec_part.partition(".")
        if not frac.isdigit():
            return None
        centis = int(frac[:2].ljust(2, "0"))
    else:
        sec_str = sec_part
    if not sec_str.isdigit() or not all(p.isdigit() for p in parts[:-1]):
        return None
    secs = int(sec_str)
    if len(parts) == 3:
        hours, minutes = int(parts[0]), int(parts[1])
    else:
        hours, minutes = 0, int(parts[0])
    total_s = ((days * 24 + hours) * 60 + minutes) * 60 + secs
    return total_s * 100 + centis


def _read_darwin_process_table() -> (
    dict[int, tuple[int, str | None, int | None, int | None]] | None
):
    """One ``ps`` call for the whole process table (#689).

    Returns ``pid -> (ppid, state, cpu_centis, rss_kb)`` or None when ps
    itself fails. A malformed row is skipped rather than disabling
    diagnostics for every target. Darwin's ``rss`` keyword is KiB; ``time``
    is accumulated user+system CPU.
    """
    try:
        out = subprocess.run(  # nosec B603 — fixed argv, no shell
            ["/bin/ps", "-axo", "pid=,ppid=,state=,time=,rss="],
            capture_output=True,
            text=True,
            timeout=2.0,
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    table: dict[int, tuple[int, str | None, int | None, int | None]] = {}
    for line in out.stdout.splitlines():
        fields = line.split()
        if len(fields) != 5:
            continue
        try:
            row_pid = int(fields[0])
            row_ppid = int(fields[1])
        except ValueError:
            continue
        state = fields[2][:1] or None
        cpu = _parse_ps_time(fields[3])
        try:
            rss: int | None = int(fields[4])
        except ValueError:
            rss = None
        table[row_pid] = (row_ppid, state, cpu, rss)
    return table or None


def _collect_darwin_proc_diag(pid: int) -> ProcessDiag | None:
    """macOS backend for collect_proc_diag (#689) — state + CPU ticks + tree,
    the fields the #653/#655 liveness gates need. fd/TCP stay unknown."""
    if not _is_alive(pid):
        return ProcessDiag(pid=pid, alive=False)
    table = _read_darwin_process_table()
    if table is None:
        return None
    row = table.get(pid)
    if row is None:
        # Died between the liveness probe and the ps snapshot — or ps hid it.
        if not _is_alive(pid):
            return ProcessDiag(pid=pid, alive=False)
        return None
    _, state, cpu, rss_kb = row
    children_by_ppid: dict[int, list[int]] = {}
    for row_pid, (row_ppid, *_rest) in table.items():
        children_by_ppid.setdefault(row_ppid, []).append(row_pid)
    children = sorted(children_by_ppid.get(pid, []))
    # Depth-capped descendant walk matching find_descendants (levels 1-4).
    descendants: list[int] = []
    seen: set[int] = {pid, *children}
    frontier = list(children)
    depth = 1
    while frontier and depth <= 4:
        descendants.extend(frontier)
        if depth == 4:
            break
        next_frontier: list[int] = []
        for child in frontier:
            for grandchild in sorted(children_by_ppid.get(child, [])):
                if grandchild not in seen:
                    seen.add(grandchild)
                    next_frontier.append(grandchild)
        frontier = next_frontier
        depth += 1
    tree_cpu: int | None = None
    if cpu is not None:
        tree_cpu = cpu
        for desc_pid in descendants:
            desc_row = table.get(desc_pid)
            desc_cpu = desc_row[2] if desc_row is not None else None
            if desc_cpu is None:
                # Prefer unknown over an undercounted tree total.
                tree_cpu = None
                break
            tree_cpu += desc_cpu
    return ProcessDiag(
        pid=pid,
        alive=True,
        state=state,
        cpu_utime=cpu,
        cpu_stime=0 if cpu is not None else None,
        rss_kb=rss_kb,
        child_pids=children,
        tree_cpu_utime=tree_cpu,
        tree_cpu_stime=0 if tree_cpu is not None else None,
    )


def collect_proc_diag(pid: int) -> ProcessDiag | None:
    """Collect process diagnostics from the platform backend.

    Linux reads /proc; macOS uses one ``ps`` call (#689). Returns None on
    other platforms or when the backend fails.
    """
    if sys.platform == "darwin":
        return _collect_darwin_proc_diag(pid)
    if sys.platform != "linux":
        return None

    alive = _is_alive(pid)
    if not alive:
        return ProcessDiag(pid=pid, alive=False)

    state, utime, stime = _read_stat(pid)
    rss_kb, threads = _read_status(pid)
    fd_count = _count_fds(pid)
    tcp_est, tcp_total = _count_tcp(pid)
    children = _find_children(pid)
    descendants = find_descendants(pid)
    tree_utime, tree_stime = _collect_tree_cpu(utime, stime, descendants)

    return ProcessDiag(
        pid=pid,
        alive=True,
        state=state,
        cpu_utime=utime,
        cpu_stime=stime,
        rss_kb=rss_kb,
        threads=threads,
        fd_count=fd_count,
        tcp_established=tcp_est,
        tcp_total=tcp_total,
        child_pids=children,
        tree_cpu_utime=tree_utime,
        tree_cpu_stime=tree_stime,
    )


def format_diag(diag: ProcessDiag) -> str:
    """Format diagnostics as a compact one-line summary."""
    if not diag.alive:
        return "dead"

    parts: list[str] = []
    parts.append(f"alive {diag.state or '?'}")

    if diag.rss_kb is not None:
        if diag.rss_kb >= 1024 * 1024:
            parts.append(f"RSS {diag.rss_kb // (1024 * 1024)}GB")
        elif diag.rss_kb >= 1024:
            parts.append(f"RSS {diag.rss_kb // 1024}MB")
        else:
            parts.append(f"RSS {diag.rss_kb}KB")

    parts.append(f"{diag.tcp_established}/{diag.tcp_total} TCP")

    if diag.fd_count is not None:
        parts.append(f"{diag.fd_count} FDs")

    if diag.child_pids:
        parts.append(f"{len(diag.child_pids)} children")

    if diag.cpu_utime is not None and diag.cpu_stime is not None:
        parts.append(f"CPU {diag.cpu_utime}+{diag.cpu_stime}")

    return ", ".join(parts)


def is_cpu_active(prev: ProcessDiag | None, curr: ProcessDiag | None) -> bool | None:
    """True if CPU ticks increased between two snapshots.

    Returns None if either snapshot lacks CPU data.
    """
    if prev is None or curr is None:
        return None
    if (
        prev.cpu_utime is None
        or prev.cpu_stime is None
        or curr.cpu_utime is None
        or curr.cpu_stime is None
    ):
        return None
    prev_total = prev.cpu_utime + prev.cpu_stime
    curr_total = curr.cpu_utime + curr.cpu_stime
    return curr_total > prev_total


def is_tree_cpu_active(
    prev: ProcessDiag | None, curr: ProcessDiag | None
) -> bool | None:
    """True if aggregate CPU ticks across pid + descendants increased.

    Returns None if either snapshot lacks tree CPU data.
    """
    if prev is None or curr is None:
        return None
    if (
        prev.tree_cpu_utime is None
        or prev.tree_cpu_stime is None
        or curr.tree_cpu_utime is None
        or curr.tree_cpu_stime is None
    ):
        return None
    prev_total = prev.tree_cpu_utime + prev.tree_cpu_stime
    curr_total = curr.tree_cpu_utime + curr.tree_cpu_stime
    return curr_total > prev_total
