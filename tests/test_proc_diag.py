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


@pytest.mark.skipif(
    sys.platform in ("linux", "darwin"), reason="tests unsupported-platform path"
)
def test_collect_returns_none_on_unsupported_platform() -> None:
    """On platforms without a backend, collect_proc_diag returns None (#689)."""
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


# --- #590 hardening: pid_starttime ---


def test_pid_starttime_returns_int_for_own_process() -> None:
    from untether.utils.proc_diag import pid_starttime

    st = pid_starttime(os.getpid())
    if sys.platform == "linux":
        assert isinstance(st, int)
        assert st > 0
    else:
        assert st is None


def test_pid_starttime_stable_across_calls() -> None:
    from untether.utils.proc_diag import pid_starttime

    if sys.platform != "linux":
        pytest.skip("Linux-only /proc")
    assert pid_starttime(os.getpid()) == pid_starttime(os.getpid())


def test_pid_starttime_missing_pid_returns_none() -> None:
    from untether.utils.proc_diag import pid_starttime

    # PID 0 is reserved and never has a /proc/0/stat.
    assert pid_starttime(0) is None


# ---------------------------------------------------------------------------
# #689: macOS (darwin) backend — ps-based, fully mocked so it runs on Linux CI
# ---------------------------------------------------------------------------


class TestParsePsTime:
    def test_minutes_seconds_centis(self) -> None:
        from untether.utils.proc_diag import _parse_ps_time

        assert _parse_ps_time("0:00.01") == 1
        assert _parse_ps_time("0:07.5") == 750
        assert _parse_ps_time("12:34.56") == 75456

    def test_hours_format_no_unit_discontinuity(self) -> None:
        from untether.utils.proc_diag import _parse_ps_time

        assert _parse_ps_time("1:02:03") == 372300
        assert _parse_ps_time("123:45:56") == 44555600

    def test_day_prefix(self) -> None:
        from untether.utils.proc_diag import _parse_ps_time

        assert _parse_ps_time("1-02:03:04") == 9378400

    def test_malformed_returns_none(self) -> None:
        from untether.utils.proc_diag import _parse_ps_time

        for bad in ("", "abc", "1:2:3:4", "-5:00", "1:xx.3", "5", "1:-2", "x-1:02:03"):
            assert _parse_ps_time(bad) is None, bad


_PS_OUTPUT = """\
    1     0 Ss    1:00.00  1024
  100     1 S     0:10.00  2048
  200   100 R+    0:05.50  4096
  300   200 U     0:02.00   512
  400   300 S     0:01.00   256
  999     1 Z     0:00.00     0
garbage line without numbers
  500   100 S     bogus     128
"""


def _fake_ps_run(*args, **kwargs):
    class _Out:
        returncode = 0
        stdout = _PS_OUTPUT
        stderr = ""

    return _Out()


class TestDarwinProcessTable:
    def test_parses_valid_rows_and_skips_malformed(self, monkeypatch) -> None:
        from untether.utils import proc_diag

        monkeypatch.setattr(proc_diag.subprocess, "run", _fake_ps_run)
        table = proc_diag._read_darwin_process_table()
        assert table is not None
        assert table[200] == (100, "R", 550, 4096)
        assert table[300] == (200, "U", 200, 512)
        # Malformed TIME parses to None but the row survives.
        assert table[500] == (100, "S", None, 128)
        assert "garbage" not in str(table)

    def test_ps_failure_returns_none(self, monkeypatch) -> None:
        from untether.utils import proc_diag

        def _fail(*args, **kwargs):
            raise OSError("no ps")

        monkeypatch.setattr(proc_diag.subprocess, "run", _fail)
        assert proc_diag._read_darwin_process_table() is None

    def test_nonzero_returncode_returns_none(self, monkeypatch) -> None:
        from untether.utils import proc_diag

        class _Out:
            returncode = 1
            stdout = ""
            stderr = "err"

        monkeypatch.setattr(proc_diag.subprocess, "run", lambda *a, **k: _Out())
        assert proc_diag._read_darwin_process_table() is None


class TestCollectDarwinProcDiag:
    def test_collects_state_cpu_tree_and_children(self, monkeypatch) -> None:
        from untether.utils import proc_diag

        monkeypatch.setattr(proc_diag.subprocess, "run", _fake_ps_run)
        monkeypatch.setattr(proc_diag, "_is_alive", lambda pid: True)
        diag = proc_diag._collect_darwin_proc_diag(100)
        assert diag is not None
        assert diag.alive is True
        assert diag.state == "S"
        assert diag.cpu_utime == 1000
        assert diag.cpu_stime == 0
        assert diag.rss_kb == 2048
        # Direct children only in child_pids (200 and the bogus-TIME 500).
        assert diag.child_pids == [200, 500]
        # Descendant 500 has unparseable CPU → tree stays unknown, not
        # undercounted.
        assert diag.tree_cpu_utime is None
        assert diag.tree_cpu_stime is None

    def test_tree_cpu_sums_descendants(self, monkeypatch) -> None:
        from untether.utils import proc_diag

        monkeypatch.setattr(proc_diag.subprocess, "run", _fake_ps_run)
        monkeypatch.setattr(proc_diag, "_is_alive", lambda pid: True)
        diag = proc_diag._collect_darwin_proc_diag(200)
        assert diag is not None
        # 200 (550) + descendants 300 (200) + 400 (100) = 850.
        assert diag.tree_cpu_utime == 850
        assert diag.child_pids == [300]

    def test_dead_pid_short_circuits(self, monkeypatch) -> None:
        from untether.utils import proc_diag

        monkeypatch.setattr(proc_diag, "_is_alive", lambda pid: False)
        diag = proc_diag._collect_darwin_proc_diag(100)
        assert diag == ProcessDiag(pid=100, alive=False)

    def test_alive_but_missing_from_table_returns_none(self, monkeypatch) -> None:
        from untether.utils import proc_diag

        monkeypatch.setattr(proc_diag.subprocess, "run", _fake_ps_run)
        monkeypatch.setattr(proc_diag, "_is_alive", lambda pid: True)
        assert proc_diag._collect_darwin_proc_diag(7777) is None

    def test_table_failure_returns_none(self, monkeypatch) -> None:
        from untether.utils import proc_diag

        monkeypatch.setattr(proc_diag, "_is_alive", lambda pid: True)
        monkeypatch.setattr(proc_diag, "_read_darwin_process_table", lambda: None)
        assert proc_diag._collect_darwin_proc_diag(100) is None

    def test_cpu_activity_detected_across_snapshots(self, monkeypatch) -> None:
        from untether.utils import proc_diag

        monkeypatch.setattr(proc_diag.subprocess, "run", _fake_ps_run)
        monkeypatch.setattr(proc_diag, "_is_alive", lambda pid: True)
        prev = proc_diag._collect_darwin_proc_diag(200)
        busier = _PS_OUTPUT.replace("0:05.50", "0:05.60")

        def _fake_busier(*args, **kwargs):
            class _Out:
                returncode = 0
                stdout = busier
                stderr = ""

            return _Out()

        monkeypatch.setattr(proc_diag.subprocess, "run", _fake_busier)
        curr = proc_diag._collect_darwin_proc_diag(200)
        assert is_cpu_active(prev, curr) is True
        assert is_tree_cpu_active(prev, curr) is True


def test_collect_dispatches_to_darwin_backend(monkeypatch) -> None:
    from untether.utils import proc_diag

    sentinel = ProcessDiag(pid=1, alive=True, state="R")
    monkeypatch.setattr(proc_diag.sys, "platform", "darwin")
    monkeypatch.setattr(proc_diag, "_collect_darwin_proc_diag", lambda pid: sentinel)
    assert proc_diag.collect_proc_diag(1) is sentinel


def test_is_alive_permission_error_means_alive(monkeypatch) -> None:
    from untether.utils import proc_diag

    def _eperm(pid: int, sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(proc_diag.os, "kill", _eperm)
    assert proc_diag._is_alive(12345) is True


def test_read_cmdline_argv_roundtrip() -> None:
    from untether.utils.proc_diag import read_cmdline_argv

    if sys.platform != "linux":
        pytest.skip("Linux-only /proc")
    argv = read_cmdline_argv(os.getpid())
    assert argv is not None
    assert len(argv) >= 1
    # PID 0 has no /proc entry.
    assert read_cmdline_argv(0) is None


# ── #791: close-grace diagnostic helpers ────────────────────────────────────


def test_describe_process_redacts_secret_bearing_args(monkeypatch) -> None:
    from untether.utils import proc_diag

    argv = [
        "/usr/bin/node",
        "/opt/mcp-remote/index.js",
        "https://mcp.example.com/sse",
        "--header",
        "Authorization:Bearer sk-live-abc",
        "--api-key",
        "plain-value-123",
        "--verbose",
    ]
    monkeypatch.setattr(proc_diag, "read_cmdline_argv", lambda pid: argv)
    out = proc_diag.describe_process(1, max_args=10)
    assert out is not None
    assert out.startswith("node /opt/mcp-remote/index.js https://mcp.example.com/sse")
    assert "sk-live-abc" not in out
    assert "plain-value-123" not in out
    # #800: the header value, --api-key and its value form one contiguous
    # redacted run, collapsed to a single marker.
    assert out == (
        "node /opt/mcp-remote/index.js https://mcp.example.com/sse"
        " --header <redacted> --verbose"
    )


def test_describe_process_truncates_and_counts_extra_args(monkeypatch) -> None:
    from untether.utils import proc_diag

    argv = ["python", "x" * 200, *[f"a{i}" for i in range(10)]]
    monkeypatch.setattr(proc_diag, "read_cmdline_argv", lambda pid: argv)
    out = proc_diag.describe_process(1, max_args=3, max_len=20)
    assert out == f"python {'x' * 19}… a0 a1 (+8 args)"


def test_describe_process_unreadable_returns_none(monkeypatch) -> None:
    from untether.utils import proc_diag

    monkeypatch.setattr(proc_diag, "read_cmdline_argv", lambda pid: None)
    assert proc_diag.describe_process(1) is None


# ── #800: argv[0] process titles and truncation must not leak secrets ────────

_FAKE_HEX = "FAKEFAKEFAKE0123456789abcdef0123"
_FAKE_JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJmYWtlIn0.c2lnbmF0dXJl"


def _describe(monkeypatch, argv: list[str], **kwargs) -> str:
    from untether.utils import proc_diag

    monkeypatch.setattr(proc_diag, "read_cmdline_argv", lambda pid: argv)
    out = proc_diag.describe_process(1, **kwargs)
    assert out is not None
    return out


def _assert_no_prefix_leak(out: str, secret: str, min_prefix: int = 4) -> None:
    """No prefix of ``secret`` of ``min_prefix``+ chars may survive."""
    for n in range(min_prefix, len(secret) + 1):
        assert secret[:n] not in out, f"{secret[:n]!r} leaked in {out!r}"


def test_describe_process_redacts_argv0_process_title(monkeypatch) -> None:
    # The nsd repro: the whole command line lives in argv[0] (setproctitle
    # style), so the old argv[1:]-only scan emitted it verbatim.
    title = (
        "mcp --allow-http --transport http-only "
        f"--header Authorization:Bearer {_FAKE_HEX}"
    )
    out = _describe(monkeypatch, [title], max_args=10)
    _assert_no_prefix_leak(out, _FAKE_HEX)
    assert out == "mcp --allow-http --transport http-only --header <redacted>"
    # Default max_args: the token falls past the cut and is only counted.
    out = _describe(monkeypatch, [title])
    _assert_no_prefix_leak(out, _FAKE_HEX)
    assert out == (
        "mcp --allow-http --transport http-only --header <redacted> (+1 args)"
    )


def test_describe_process_argv0_title_token_early(monkeypatch) -> None:
    out = _describe(monkeypatch, [f"mcp --header Authorization:Bearer {_FAKE_JWT}"])
    _assert_no_prefix_leak(out, _FAKE_JWT)
    assert out == "mcp --header <redacted>"


def test_describe_process_argv0_title_token_late(monkeypatch) -> None:
    # Secret beyond max_args: never shown, only counted.
    title = f"/usr/bin/mcp a b c d e f --header Authorization:Bearer {_FAKE_HEX}"
    out = _describe(monkeypatch, [title])
    _assert_no_prefix_leak(out, _FAKE_HEX)
    assert out == "mcp a b c d e (+4 args)"


def test_describe_process_argv0_title_with_path_in_args(monkeypatch) -> None:
    # basename() used to run over the whole title, so a later "/" in the
    # arguments chopped the executable off and left the tail (header and
    # all) visible — the third nsd child line.
    title = f"node /srv/mcp/index.js --header Authorization:Bearer a/{_FAKE_HEX}"
    out = _describe(monkeypatch, [title])
    _assert_no_prefix_leak(out, _FAKE_HEX)
    assert out == "node /srv/mcp/index.js --header <redacted>"


def test_describe_process_space_separated_bearer_in_one_arg(monkeypatch) -> None:
    out = _describe(
        monkeypatch,
        ["npx", "mcp-remote", "--header", f"Authorization: Bearer {_FAKE_HEX}"],
    )
    _assert_no_prefix_leak(out, _FAKE_HEX)
    assert out == "npx mcp-remote --header <redacted>"


def test_describe_process_redacts_before_truncating(monkeypatch) -> None:
    # A short max_len must not keep the first max_len chars of a secret.
    out = _describe(
        monkeypatch,
        ["srv", f"--token={_FAKE_HEX}", f"Bearer{_FAKE_HEX}", "--verbose"],
        max_len=12,
    )
    _assert_no_prefix_leak(out, _FAKE_HEX)
    assert out == "srv <redacted> --verbose"


def test_describe_process_long_argv0_title_truncation_no_leak(monkeypatch) -> None:
    # Even when the executable token itself is secret-bearing and long.
    out = _describe(monkeypatch, [f"Bearer:{_FAKE_HEX} --x"], max_len=10)
    _assert_no_prefix_leak(out, _FAKE_HEX)
    assert out == "<redacted> --x"


def test_describe_process_redacts_bare_jwt(monkeypatch) -> None:
    out = _describe(monkeypatch, ["mcp-proxy", _FAKE_JWT, "--port", "8080"])
    _assert_no_prefix_leak(out, _FAKE_JWT)
    assert out == "mcp-proxy <redacted> --port 8080"


def test_describe_process_keeps_benign_title(monkeypatch) -> None:
    out = _describe(monkeypatch, ["npm exec firecrawl-mcp"])
    assert out == "npm exec firecrawl-mcp"


def test_read_wchan_missing_pid_returns_none() -> None:
    from untether.utils.proc_diag import read_wchan

    assert read_wchan(2**22 + 12345) is None


# ── #812: hook_script_label ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        # sh -c with a long plugin path — describe_process truncates the
        # token at 80 chars, before its /hooks/ segment.
        (
            [
                "/bin/sh",
                "-c",
                "python3 /home/u/.claude/plugins/cache/claude-plugins-official/"
                "security-guidance/1.0.0/hooks/security_reminder_hook.py",
            ],
            "security_reminder_hook.py",
        ),
        # A hook run directly — describe_process shows only its basename.
        (["/home/u/proj/.claude/hooks/stop.sh"], "stop.sh"),
        (["bash", "-c", "sleep 90; /x/.claude/hooks/review.sh"], "review.sh"),
        # Secret-looking script names never leak.
        (["/x/.claude/hooks/push-token-check.sh"], "<redacted>"),
        (["node", "/srv/mcp/server.js", "--port", "1"], None),
    ],
)
def test_812_hook_script_label(argv: list[str], expected: str | None) -> None:
    from unittest import mock

    from untether.utils import proc_diag

    with mock.patch.object(proc_diag, "read_cmdline_argv", return_value=argv):
        assert proc_diag.hook_script_label(1) == expected


def test_812_describe_process_loses_the_hooks_segment() -> None:
    """Why ``hook_script_label`` reads raw argv: the #800-redacted
    ``describe_process`` line can't be matched on ``/hooks/``."""
    from unittest import mock

    from untether.utils import proc_diag

    for argv in (
        ["/home/u/proj/.claude/hooks/stop.sh"],
        [
            "/bin/sh",
            "-c",
            "python3 /home/u/.claude/plugins/cache/claude-plugins-official/"
            "security-guidance/1.0.0/hooks/security_reminder_hook.py",
        ],
    ):
        with mock.patch.object(proc_diag, "read_cmdline_argv", return_value=argv):
            assert "/hooks/" not in (proc_diag.describe_process(1) or "")


def test_812_hook_script_label_unreadable_pid() -> None:
    from unittest import mock

    from untether.utils import proc_diag

    with mock.patch.object(proc_diag, "read_cmdline_argv", return_value=None):
        assert proc_diag.hook_script_label(1) is None


# ── #812: hook_shell_children ────────────────────────────────────────────────


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc backend")
def test_812_hook_shell_children_finds_sh_c_children_only() -> None:
    """Claude Code runs every command hook as ``/bin/sh -c <command>``; a
    direct non-shell child (an MCP server) and a Bash-tool shell (the
    ``… && pwd -P >| <tmp>/claude-<id>-cwd`` wrapper) are not one."""
    import subprocess

    from untether.utils.proc_diag import hook_shell_children

    hook = subprocess.Popen("sleep 5; true", shell=True)
    tool = subprocess.Popen(
        [
            "/bin/sh",
            "-c",
            "eval 'sleep 5' < /dev/null && pwd -P >| /tmp/claude-ab12-cwd",
        ]
    )
    other = subprocess.Popen(["sleep", "5"])
    try:
        found = hook_shell_children(os.getpid())
        assert found is not None
        assert set(found) & {hook.pid, tool.pid, other.pid} == {hook.pid}
    finally:
        for proc in (hook, tool, other):
            proc.kill()
            proc.wait()
    assert hook.pid not in (hook_shell_children(os.getpid()) or [])


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["/bin/sh", "-c", "moshi-hook claude-hook"], True),
        (["/usr/bin/bash", "-c", "x"], True),
        (["/usr/bin/zsh", "-l", "-c", "x"], True),
        (["npm exec firecrawl-mcp"], False),
        (["node", "/usr/bin/mcp-server-trello"], False),
        (["/bin/sh", "/x/script.sh"], False),
    ],
)
def test_812_is_shell_c(argv: list[str], expected: bool) -> None:
    from untether.utils.proc_diag import _is_shell_c

    assert _is_shell_c(argv) is expected


def test_812_hook_shell_children_unknown_pid_is_none_off_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable table → None: the caller must not assume no hook runs."""
    from untether.utils import proc_diag

    monkeypatch.setattr(proc_diag.sys, "platform", "linux")
    assert proc_diag.hook_shell_children(2**22 + 12345) is None


def test_812_hook_shell_children_darwin_ps(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from untether.utils import proc_diag

    ps_out = (
        "  100     1 /Users/u/.local/bin/claude --output-format stream-json\n"
        "  200   100 /bin/sh -c '/Users/u/.local/bin/moshi-hook' claude-hook\n"
        "  201   100 npm exec firecrawl-mcp\n"
        "  202   100 /bin/zsh -c eval 'ls' && pwd -P >| /tmp/claude-1f-cwd\n"
        "  300   999 /bin/sh -c unrelated\n"
        "garbage\n"
    )
    monkeypatch.setattr(proc_diag.sys, "platform", "darwin")
    monkeypatch.setattr(proc_diag.os.path, "isdir", lambda p: False)
    monkeypatch.setattr(
        proc_diag.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=ps_out, stderr=""),
    )
    assert proc_diag.hook_shell_children(100) == [200]
    monkeypatch.setattr(
        proc_diag.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="x"),
    )
    assert proc_diag.hook_shell_children(100) is None
