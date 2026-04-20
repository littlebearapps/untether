"""Tests for src/untether/utils/proc_diag.py."""

from __future__ import annotations

import os
import sys

import pytest

from untether.utils.proc_diag import (
    ProcessDiag,
    _find_descendants,
    collect_proc_diag,
    format_diag,
    is_cpu_active,
    is_tree_cpu_active,
    mem_available_kb,
)

# ---------------------------------------------------------------------------
# format_diag tests
# ---------------------------------------------------------------------------


def test_format_diag_dead() -> None:
    diag = ProcessDiag(pid=1, alive=False)
    assert format_diag(diag) == "dead"


def test_format_diag_alive_minimal() -> None:
    diag = ProcessDiag(pid=1, alive=True, state="S", tcp_established=0, tcp_total=0)
    assert "alive S" in format_diag(diag)
    assert "0/0 TCP" in format_diag(diag)


def test_format_diag_rss_mb() -> None:
    diag = ProcessDiag(pid=1, alive=True, state="R", rss_kb=512 * 1024)
    result = format_diag(diag)
    assert "RSS 512MB" in result


def test_format_diag_rss_gb() -> None:
    diag = ProcessDiag(pid=1, alive=True, state="R", rss_kb=2 * 1024 * 1024)
    result = format_diag(diag)
    assert "RSS 2GB" in result


def test_format_diag_rss_kb() -> None:
    diag = ProcessDiag(pid=1, alive=True, state="R", rss_kb=512)
    result = format_diag(diag)
    assert "RSS 512KB" in result


def test_format_diag_fds() -> None:
    diag = ProcessDiag(pid=1, alive=True, state="S", fd_count=42)
    result = format_diag(diag)
    assert "42 FDs" in result


def test_format_diag_tcp() -> None:
    diag = ProcessDiag(pid=1, alive=True, state="S", tcp_established=2, tcp_total=5)
    result = format_diag(diag)
    assert "2/5 TCP" in result


def test_format_diag_children() -> None:
    diag = ProcessDiag(pid=1, alive=True, state="S", child_pids=[10, 20, 30])
    result = format_diag(diag)
    assert "3 children" in result


def test_format_diag_cpu() -> None:
    diag = ProcessDiag(pid=1, alive=True, state="S", cpu_utime=1000, cpu_stime=200)
    result = format_diag(diag)
    assert "CPU 1000+200" in result


def test_format_diag_full() -> None:
    diag = ProcessDiag(
        pid=42,
        alive=True,
        state="S",
        cpu_utime=14523,
        cpu_stime=892,
        rss_kb=512 * 1024,
        threads=4,
        fd_count=159,
        tcp_established=0,
        tcp_total=3,
        child_pids=[100, 200],
    )
    result = format_diag(diag)
    assert "alive S" in result
    assert "RSS 512MB" in result
    assert "0/3 TCP" in result
    assert "159 FDs" in result
    assert "2 children" in result
    assert "CPU 14523+892" in result


def test_format_diag_unknown_state() -> None:
    diag = ProcessDiag(pid=1, alive=True, state=None)
    result = format_diag(diag)
    assert "alive ?" in result


# ---------------------------------------------------------------------------
# is_cpu_active tests
# ---------------------------------------------------------------------------


def test_is_cpu_active_increasing() -> None:
    prev = ProcessDiag(pid=1, alive=True, cpu_utime=100, cpu_stime=50)
    curr = ProcessDiag(pid=1, alive=True, cpu_utime=150, cpu_stime=50)
    assert is_cpu_active(prev, curr) is True


def test_is_cpu_active_same() -> None:
    prev = ProcessDiag(pid=1, alive=True, cpu_utime=100, cpu_stime=50)
    curr = ProcessDiag(pid=1, alive=True, cpu_utime=100, cpu_stime=50)
    assert is_cpu_active(prev, curr) is False


def test_is_cpu_active_none_prev() -> None:
    curr = ProcessDiag(pid=1, alive=True, cpu_utime=100, cpu_stime=50)
    assert is_cpu_active(None, curr) is None


def test_is_cpu_active_none_curr() -> None:
    prev = ProcessDiag(pid=1, alive=True, cpu_utime=100, cpu_stime=50)
    assert is_cpu_active(prev, None) is None


def test_is_cpu_active_missing_cpu_data() -> None:
    prev = ProcessDiag(pid=1, alive=True, cpu_utime=None, cpu_stime=None)
    curr = ProcessDiag(pid=1, alive=True, cpu_utime=100, cpu_stime=50)
    assert is_cpu_active(prev, curr) is None


def test_is_cpu_active_both_none() -> None:
    assert is_cpu_active(None, None) is None


# ---------------------------------------------------------------------------
# collect_proc_diag tests (Linux only — live /proc reads)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "linux", reason="requires /proc")
def test_collect_self() -> None:
    """Collect diagnostics for our own process — should succeed on Linux."""
    diag = collect_proc_diag(os.getpid())
    assert diag is not None
    assert diag.alive is True
    assert diag.pid == os.getpid()
    assert diag.state is not None
    assert diag.cpu_utime is not None
    assert diag.cpu_stime is not None
    assert diag.rss_kb is not None
    assert diag.threads is not None
    assert diag.fd_count is not None
    assert diag.fd_count > 0


@pytest.mark.skipif(sys.platform != "linux", reason="requires /proc")
def test_collect_dead_process() -> None:
    """Collecting diag for a non-existent PID returns alive=False."""
    diag = collect_proc_diag(99999999)
    assert diag is not None
    assert diag.alive is False


@pytest.mark.skipif(sys.platform != "linux", reason="requires /proc")
def test_collect_self_tcp() -> None:
    """TCP fields should be integers (may be 0 if no connections)."""
    diag = collect_proc_diag(os.getpid())
    assert diag is not None
    assert isinstance(diag.tcp_established, int)
    assert isinstance(diag.tcp_total, int)
    assert diag.tcp_total >= diag.tcp_established


@pytest.mark.skipif(sys.platform != "linux", reason="requires /proc")
def test_collect_self_format_roundtrip() -> None:
    """format_diag should produce a non-empty string for a live process."""
    diag = collect_proc_diag(os.getpid())
    assert diag is not None
    result = format_diag(diag)
    assert "alive" in result
    assert len(result) > 10


# ---------------------------------------------------------------------------
# is_tree_cpu_active tests
# ---------------------------------------------------------------------------


def test_is_tree_cpu_active_increasing() -> None:
    prev = ProcessDiag(pid=1, alive=True, tree_cpu_utime=1000, tree_cpu_stime=500)
    curr = ProcessDiag(pid=1, alive=True, tree_cpu_utime=1200, tree_cpu_stime=500)
    assert is_tree_cpu_active(prev, curr) is True


def test_is_tree_cpu_active_flat() -> None:
    prev = ProcessDiag(pid=1, alive=True, tree_cpu_utime=1000, tree_cpu_stime=500)
    curr = ProcessDiag(pid=1, alive=True, tree_cpu_utime=1000, tree_cpu_stime=500)
    assert is_tree_cpu_active(prev, curr) is False


def test_is_tree_cpu_active_none_prev() -> None:
    curr = ProcessDiag(pid=1, alive=True, tree_cpu_utime=1000, tree_cpu_stime=500)
    assert is_tree_cpu_active(None, curr) is None


def test_is_tree_cpu_active_none_fields() -> None:
    prev = ProcessDiag(pid=1, alive=True, tree_cpu_utime=None, tree_cpu_stime=None)
    curr = ProcessDiag(pid=1, alive=True, tree_cpu_utime=1000, tree_cpu_stime=500)
    assert is_tree_cpu_active(prev, curr) is None


def test_is_tree_cpu_active_child_activity_only() -> None:
    """Tree CPU increases even when main process CPU is flat (child work)."""
    prev = ProcessDiag(
        pid=1,
        alive=True,
        cpu_utime=100,
        cpu_stime=50,
        tree_cpu_utime=1000,
        tree_cpu_stime=500,
    )
    curr = ProcessDiag(
        pid=1,
        alive=True,
        cpu_utime=100,
        cpu_stime=50,
        tree_cpu_utime=1200,
        tree_cpu_stime=600,
    )
    assert is_cpu_active(prev, curr) is False  # main process flat
    assert is_tree_cpu_active(prev, curr) is True  # tree active from children


@pytest.mark.skipif(sys.platform != "linux", reason="requires /proc")
def test_collect_self_tree_cpu_populated() -> None:
    """collect_proc_diag should populate tree CPU fields for live process."""
    diag = collect_proc_diag(os.getpid())
    assert diag is not None
    assert diag.tree_cpu_utime is not None
    assert diag.tree_cpu_stime is not None
    # Tree CPU >= main process CPU (includes children)
    assert diag.tree_cpu_utime >= (diag.cpu_utime or 0)
    assert diag.tree_cpu_stime >= (diag.cpu_stime or 0)


@pytest.mark.skipif(sys.platform != "linux", reason="requires /proc")
def test_find_descendants_self() -> None:
    """_find_descendants for our own process should return a list."""
    descendants = _find_descendants(os.getpid())
    assert isinstance(descendants, list)


def test_find_descendants_nonexistent() -> None:
    """_find_descendants for a non-existent PID returns empty."""
    descendants = _find_descendants(99999999)
    assert descendants == []


@pytest.mark.skipif(sys.platform == "linux", reason="tests non-Linux path")
def test_collect_returns_none_on_non_linux() -> None:
    """On non-Linux platforms, collect_proc_diag returns None."""
    diag = collect_proc_diag(os.getpid())
    assert diag is None


# ---------------------------------------------------------------------------
# ProcessDiag dataclass tests
# ---------------------------------------------------------------------------


def test_process_diag_defaults() -> None:
    diag = ProcessDiag(pid=1, alive=True)
    assert diag.state is None
    assert diag.cpu_utime is None
    assert diag.cpu_stime is None
    assert diag.rss_kb is None
    assert diag.threads is None
    assert diag.fd_count is None
    assert diag.tcp_established == 0
    assert diag.tcp_total == 0
    assert diag.child_pids == []


def test_process_diag_frozen() -> None:
    diag = ProcessDiag(pid=1, alive=True)
    with pytest.raises(AttributeError):
        diag.pid = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# mem_available_kb tests (#350)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_mem_available_kb_reads_procfs() -> None:
    """On Linux, mem_available_kb returns a positive integer."""
    value = mem_available_kb()
    assert value is not None
    assert isinstance(value, int)
    assert value > 0  # any host we run on has some memory available


def test_mem_available_kb_returns_none_on_non_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On non-Linux, mem_available_kb returns None without touching /proc."""
    monkeypatch.setattr(sys, "platform", "darwin")
    assert mem_available_kb() is None


def test_mem_available_kb_handles_missing_meminfo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError from /proc/meminfo → None, not a crash."""
    monkeypatch.setattr(sys, "platform", "linux")
    import builtins

    real_open = builtins.open

    def fake_open(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(path, str) and path == "/proc/meminfo":
            raise FileNotFoundError(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert mem_available_kb() is None


def test_mem_available_kb_handles_malformed_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A /proc/meminfo without a parseable MemAvailable line → None."""
    # Stage a fake meminfo without the expected second field
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:        8000 kB\nMemAvailable:\n")
    monkeypatch.setattr(sys, "platform", "linux")
    import builtins

    real_open = builtins.open

    def fake_open(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(path, str) and path == "/proc/meminfo":
            return real_open(meminfo, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    # "MemAvailable:\n" → parts = ["MemAvailable:"] — len < 2 → returns None
    assert mem_available_kb() is None


def test_mem_available_kb_parses_valid_meminfo(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A well-formed /proc/meminfo → the MemAvailable KB value."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:        8000000 kB\n"
        "MemFree:         1000000 kB\n"
        "MemAvailable:    4200000 kB\n"
        "Buffers:          500000 kB\n"
    )
    monkeypatch.setattr(sys, "platform", "linux")
    import builtins

    real_open = builtins.open

    def fake_open(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(path, str) and path == "/proc/meminfo":
            return real_open(meminfo, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert mem_available_kb() == 4_200_000
