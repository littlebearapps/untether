"""Process diagnostics via /proc (Linux only).

Collects CPU, memory, TCP, FD, and child process info for stall analysis.
Returns None on non-Linux platforms or when /proc is unavailable.
"""

from __future__ import annotations

import contextlib
import os
import sys
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ProcessDiag:
    pid: int
    alive: bool
    state: str | None = None  # R/S/D/Z from /proc/pid/stat
    cpu_utime: int | None = None  # user CPU ticks
    cpu_stime: int | None = None  # system CPU ticks
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
    except (ProcessLookupError, PermissionError):
        return False


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


def _find_descendants(pid: int, *, _depth: int = 0, _max_depth: int = 4) -> list[int]:
    """Find all descendant PIDs recursively (depth-limited)."""
    if _depth >= _max_depth:
        return []
    children = _find_children(pid)
    descendants = list(children)
    for child in children:
        descendants.extend(
            _find_descendants(child, _depth=_depth + 1, _max_depth=_max_depth)
        )
    return descendants


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


def collect_proc_diag(pid: int) -> ProcessDiag | None:
    """Collect process diagnostics from /proc. Returns None on non-Linux."""
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
    descendants = _find_descendants(pid)
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
