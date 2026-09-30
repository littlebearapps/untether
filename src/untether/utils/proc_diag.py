"""Process diagnostics via platform process APIs.

Collects CPU, memory, TCP, FD, and child process info for stall analysis.
Linux reads /proc; macOS shells out to `ps` (#689 — a partial backend
supplying state + CPU ticks, enough to drive the liveness gates). Other
platforms return None.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
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


def hook_shell_children(pid: int) -> list[int] | None:
    """#812: direct children of ``pid`` running ``<shell> -c …`` — the shape
    every Claude Code command hook runs as. Returns None when the process
    table can't be read (then the caller can't tell, and must not assume
    no hook is running). Blocking (``ps`` on macOS): call from a thread."""
    if os.path.isdir(f"/proc/{pid}"):
        found: list[int] = []
        for child in _find_children(pid):
            argv = read_cmdline_argv(child)
            if argv and _is_shell_c(argv):
                found.append(child)
        return found
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(  # nosec B603 — fixed argv, no shell
            ["/bin/ps", "-axo", "pid=,ppid=,command="],
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
    found = []
    for line in out.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) != 3:
            continue
        try:
            row_pid, row_ppid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        # ``command=`` joins argv with spaces; the shell and ``-c`` are the
        # leading tokens.
        if row_ppid == pid and _is_shell_c(fields[2].split()[:3]):
            found.append(row_pid)
    return found


def _find_children(pid: int) -> list[int]:
    """Find child PIDs via /proc/pid/task/*/children."""
    children: list[int] = []
    try:
        task_dir = f"/proc/{pid}/task"
        for tid in os.listdir(task_dir):
            try:
                data = open(  # noqa: SIM115
                    f"{task_dir}/{tid}/children", encoding="utf-8"
                ).read()
                for tok in data.split():
                    with contextlib.suppress(ValueError):
                        children.append(int(tok))
            except (OSError, FileNotFoundError, PermissionError):
                continue
    except (OSError, FileNotFoundError, PermissionError):
        pass
    return children


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
