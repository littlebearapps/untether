"""#927 — restart hints name the service that is actually running.

The restart-required notices used to hardcode the default systemd unit, which
pointed multi-instance hosts (lba-1: staging + dev + dev-hf …) at the wrong
instance and failed outright on macOS (launchd). ``service_manager`` derives the
real unit (cgroup + ``INVOCATION_ID``/``SYSTEMD_EXEC_PID``) or launchd label
(``XPC_SERVICE_NAME``) and fails closed to ``None`` → generic wording.
"""

from __future__ import annotations

import pytest

from untether import service_manager
from untether.service_manager import (
    detect_restart_command,
    parse_cgroup_unit,
    restart_command,
    restart_hint,
)

_USER_PREFIX = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"


@pytest.fixture(autouse=True)
def _clear_cache():
    restart_command.cache_clear()
    yield
    restart_command.cache_clear()


def _linux(
    cgroup: str | None,
    *,
    pid: int = 4242,
    exec_pid: int | None = 4242,
    invocation: bool = True,
    uid: int = 1000,
) -> str | None:
    env: dict[str, str] = {}
    if invocation:
        env["INVOCATION_ID"] = "0123456789abcdef0123456789abcdef"
    if exec_pid is not None:
        env["SYSTEMD_EXEC_PID"] = str(exec_pid)
    return detect_restart_command(
        environ=env, cgroup_text=cgroup, pid=pid, uid=uid, platform="linux"
    )


def _darwin(label: str | None, *, uid: int = 501) -> str | None:
    env = {} if label is None else {"XPC_SERVICE_NAME": label}
    return detect_restart_command(
        environ=env, cgroup_text=None, pid=4204, uid=uid, platform="darwin"
    )


# ── systemd ──────────────────────────────────────────────────────────────


def test_user_unit_cgroup_v2() -> None:
    cmd = _linux(_USER_PREFIX + "untether-dev.service\n")
    assert cmd == "systemctl --user restart untether-dev"


@pytest.mark.parametrize("unit", ["untether-dev-hf", "untether"])
def test_user_unit_dev_hf_and_staging(unit: str) -> None:
    assert _linux(f"{_USER_PREFIX}{unit}.service") == (
        f"systemctl --user restart {unit}"
    )


def test_system_unit_uses_sudo() -> None:
    assert _linux("0::/system.slice/untether.service") == (
        "sudo systemctl restart untether"
    )


def test_system_unit_as_root_no_sudo() -> None:
    assert _linux("0::/system.slice/untether.service", uid=0) == (
        "systemctl restart untether"
    )


def test_template_unit_kept() -> None:
    assert _linux(_USER_PREFIX + "untether@bot1.service") == (
        "systemctl --user restart untether@bot1"
    )


@pytest.mark.parametrize(
    "leaf",
    [
        "session-3.scope",
        "tmux-spawn-ab12.scope",
        "run-u12.scope",
    ],
)
def test_scope_is_unknown(leaf: str) -> None:
    assert _linux(_USER_PREFIX + leaf) is None
    assert _linux(f"0::/user.slice/user-1000.slice/{leaf}") is None


def test_user_manager_leaf_rejected() -> None:
    assert _linux("0::/user.slice/user-1000.slice/user@1000.service") is None


def test_missing_invocation_id_is_unknown() -> None:
    assert _linux(_USER_PREFIX + "untether-dev.service", invocation=False) is None


def test_exec_pid_mismatch_is_unknown() -> None:
    """Regression for the inherited-environment case: a manual run from an
    agent shell spawned under staging inherits staging's INVOCATION_ID and
    cgroup, but its PID is not the unit's main process."""
    cmd = _linux(_USER_PREFIX + "untether.service", pid=999, exec_pid=433334)
    assert cmd is None


def test_exec_pid_garbage_is_unknown() -> None:
    env = {"INVOCATION_ID": "x", "SYSTEMD_EXEC_PID": "not-a-pid"}
    assert (
        detect_restart_command(
            environ=env,
            cgroup_text=_USER_PREFIX + "untether.service",
            pid=1,
            uid=1000,
            platform="linux",
        )
        is None
    )


def test_exec_pid_absent_trusts_cgroup() -> None:
    """systemd < 248 doesn't set SYSTEMD_EXEC_PID: trust the cgroup."""
    cmd = _linux(_USER_PREFIX + "untether-dev.service", exec_pid=None)
    assert cmd == "systemctl --user restart untether-dev"


def test_cgroup_v1_name_systemd_line() -> None:
    text = (
        "12:memory:/user.slice\n"
        "3:cpu,cpuacct:/user.slice\n"
        "1:name=systemd:/user.slice/user-1000.slice/user@1000.service/"
        "app.slice/untether.service\n"
    )
    assert _linux(text) == "systemctl --user restart untether"


def test_cgroup_v2_line_preferred_over_v1() -> None:
    text = (
        "1:name=systemd:/system.slice/other.service\n"
        + _USER_PREFIX
        + "untether-dev.service\n"
    )
    assert _linux(text) == "systemctl --user restart untether-dev"


def test_container_root_cgroup_unknown() -> None:
    assert _linux("0::/") is None
    assert _linux("") is None


@pytest.mark.parametrize("leaf", ["bad`name.service", "has space.service"])
def test_invalid_unit_chars_rejected(leaf: str) -> None:
    assert _linux(_USER_PREFIX + leaf) is None


def test_unreadable_cgroup_unknown() -> None:
    assert _linux(None) is None


def test_parse_cgroup_unit_kinds() -> None:
    assert parse_cgroup_unit(_USER_PREFIX + "untether-dev.service") == (
        "user",
        "untether-dev",
    )
    assert parse_cgroup_unit("0::/system.slice/untether.service") == (
        "system",
        "untether",
    )
    assert parse_cgroup_unit("0::/user.slice/user-1000.slice/session-2.scope") is None


# ── launchd ──────────────────────────────────────────────────────────────


def test_launchd_label() -> None:
    assert _darwin("com.littlebearapps.untether") == (
        "launchctl kickstart -k gui/501/com.littlebearapps.untether"
    )


def test_launchd_root_system_domain() -> None:
    assert _darwin("com.littlebearapps.untether", uid=0) == (
        "launchctl kickstart -k system/com.littlebearapps.untether"
    )


@pytest.mark.parametrize(
    "label",
    ["0", "application.com.apple.Terminal.123", "", None, "bad label", "a`b"],
)
def test_launchd_terminal_values_unknown(label: str | None) -> None:
    assert _darwin(label) is None


def test_platform_isolation() -> None:
    env = {
        "INVOCATION_ID": "x",
        "SYSTEMD_EXEC_PID": "7",
        "XPC_SERVICE_NAME": "com.littlebearapps.untether",
    }
    # darwin never reads the cgroup
    assert (
        detect_restart_command(
            environ={k: v for k, v in env.items() if k != "XPC_SERVICE_NAME"},
            cgroup_text=_USER_PREFIX + "untether.service",
            pid=7,
            uid=501,
            platform="darwin",
        )
        is None
    )
    # linux never reads XPC_SERVICE_NAME
    assert (
        detect_restart_command(
            environ={"XPC_SERVICE_NAME": "com.littlebearapps.untether"},
            cgroup_text=None,
            pid=7,
            uid=501,
            platform="linux",
        )
        is None
    )
    # other platforms → unknown
    assert (
        detect_restart_command(
            environ=env,
            cgroup_text=_USER_PREFIX + "untether.service",
            pid=7,
            uid=0,
            platform="win32",
        )
        is None
    )


# ── hint wording ─────────────────────────────────────────────────────────


def test_restart_hint_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service_manager,
        "restart_command",
        lambda: "systemctl --user restart untether-dev",
    )
    assert restart_hint() == (
        "run `systemctl --user restart untether-dev` (or send `/restart`)"
    )


def test_restart_hint_generic(monkeypatch: pytest.MonkeyPatch) -> None:
    """§13 amendment 1: /restart only stops the process; it's relaunched by a
    supervisor. The unknown hint must not promise /restart restarts an
    unsupervised (foreground) process."""
    monkeypatch.setattr(service_manager, "restart_command", lambda: None)
    hint = restart_hint()
    assert hint == (
        "restart Untether's service "
        "(or send `/restart` if a service manager keeps it running)"
    )
    assert "systemctl" not in hint


def test_restart_command_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> str:
        raise RuntimeError("cgroup exploded")

    monkeypatch.setattr(service_manager, "_read_self_cgroup", boom)
    assert restart_command() is None


def test_restart_command_reads_real_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_manager.sys, "platform", "linux")
    monkeypatch.setattr(service_manager.os, "getpid", lambda: 31337)
    monkeypatch.setenv("INVOCATION_ID", "abc")
    monkeypatch.setenv("SYSTEMD_EXEC_PID", "31337")
    monkeypatch.setattr(
        service_manager,
        "_read_self_cgroup",
        lambda: _USER_PREFIX + "untether-dev-hf.service\n",
    )
    assert restart_command() == "systemctl --user restart untether-dev-hf"
    # cached for the process lifetime
    monkeypatch.setattr(service_manager, "_read_self_cgroup", lambda: None)
    assert restart_command() == "systemctl --user restart untether-dev-hf"


def test_log_service_detection_logs_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    from structlog.testing import capture_logs

    monkeypatch.setattr(service_manager.sys, "platform", "linux")
    monkeypatch.setattr(service_manager.os, "getpid", lambda: 10)
    monkeypatch.setenv("INVOCATION_ID", "abc")
    monkeypatch.setenv("SYSTEMD_EXEC_PID", "10")
    monkeypatch.setattr(
        service_manager,
        "_read_self_cgroup",
        lambda: _USER_PREFIX + "untether-dev.service",
    )
    with capture_logs() as logs:
        service_manager.log_service_detection()
    entry = next(e for e in logs if e["event"] == "service.detected")
    assert entry["manager"] == "systemd-user"
    assert entry["unit"] == "untether-dev"
    assert entry["exec_pid_match"] is True


def test_log_service_detection_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    from structlog.testing import capture_logs

    monkeypatch.setattr(service_manager.sys, "platform", "linux")
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("SYSTEMD_EXEC_PID", raising=False)
    monkeypatch.setattr(service_manager, "_read_self_cgroup", lambda: None)
    with capture_logs() as logs:
        service_manager.log_service_detection()
    entry = next(e for e in logs if e["event"] == "service.detected")
    assert entry["manager"] == "unknown"
    assert entry["unit"] is None


def test_log_service_detection_launchd(monkeypatch: pytest.MonkeyPatch) -> None:
    from structlog.testing import capture_logs

    monkeypatch.setattr(service_manager.sys, "platform", "darwin")
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.littlebearapps.untether")
    with capture_logs() as logs:
        service_manager.log_service_detection()
    entry = next(e for e in logs if e["event"] == "service.detected")
    assert entry["manager"] == "launchd"
    assert entry["unit"] == "com.littlebearapps.untether"
