"""Tests for the graceful shutdown module."""

from __future__ import annotations

from untether.shutdown import (
    DRAIN_TIMEOUT_S,
    SELF_RESTART_DRAIN_TIMEOUT_S,
    get_shutdown_origin_chat_id,
    is_shutting_down,
    request_shutdown,
    reset_shutdown,
    select_drain_timeout,
)


class TestShutdownState:
    def setup_method(self) -> None:
        reset_shutdown()

    def teardown_method(self) -> None:
        reset_shutdown()

    def test_initially_not_shutting_down(self) -> None:
        assert is_shutting_down() is False

    def test_request_shutdown_sets_state(self) -> None:
        request_shutdown()
        assert is_shutting_down() is True

    def test_double_request_is_idempotent(self) -> None:
        request_shutdown()
        request_shutdown()
        assert is_shutting_down() is True

    def test_reset_clears_state(self) -> None:
        request_shutdown()
        assert is_shutting_down() is True
        reset_shutdown()
        assert is_shutting_down() is False


class TestShutdownOrigin:
    def setup_method(self) -> None:
        reset_shutdown()

    def teardown_method(self) -> None:
        reset_shutdown()

    def test_origin_defaults_to_none(self) -> None:
        request_shutdown()
        assert get_shutdown_origin_chat_id() is None

    def test_origin_chat_id_recorded(self) -> None:
        request_shutdown(origin_chat_id=4242)
        assert get_shutdown_origin_chat_id() == 4242

    def test_origin_not_overwritten_by_later_request(self) -> None:
        request_shutdown(origin_chat_id=4242)
        request_shutdown(origin_chat_id=9999)  # idempotent — first wins
        assert get_shutdown_origin_chat_id() == 4242

    def test_reset_clears_origin(self) -> None:
        request_shutdown(origin_chat_id=4242)
        reset_shutdown()
        assert get_shutdown_origin_chat_id() is None


class TestDrainTimeoutSelection:
    def test_sole_run_with_evidence_uses_short_timeout(self) -> None:
        # #559: a confirmed self-restart deadlock drains fast.
        assert select_drain_timeout(1, self_restart=True) == (
            SELF_RESTART_DRAIN_TIMEOUT_S
        )

    def test_sole_run_without_evidence_gets_full_grace(self) -> None:
        # #690: an external restart (fleet rollout — origin None, no
        # systemctl descendant) landing on a lone healthy run must NOT
        # take the destructive fast path.
        assert select_drain_timeout(1, self_restart=False) == DRAIN_TIMEOUT_S

    def test_multiple_runs_use_full_timeout(self) -> None:
        assert select_drain_timeout(2, self_restart=False) == DRAIN_TIMEOUT_S
        assert select_drain_timeout(5, self_restart=False) == DRAIN_TIMEOUT_S

    def test_multiple_runs_ignore_self_restart_evidence(self) -> None:
        # #690: evidence only ever narrows the SOLE-run case.
        assert select_drain_timeout(2, self_restart=True) == DRAIN_TIMEOUT_S

    def test_short_timeout_is_smaller(self) -> None:
        assert SELF_RESTART_DRAIN_TIMEOUT_S < DRAIN_TIMEOUT_S


class TestSelfRestartArgvMatcher:
    def test_systemctl_restart_untether_matches(self) -> None:
        from untether.shutdown import is_self_restart_argv

        assert is_self_restart_argv(["systemctl", "--user", "restart", "untether"])
        assert is_self_restart_argv(
            ["/usr/bin/systemctl", "--user", "restart", "untether.service"]
        )
        assert is_self_restart_argv(["systemctl", "--user", "stop", "untether-dev"])

    def test_non_blocking_or_unrelated_commands_do_not_match(self) -> None:
        from untether.shutdown import is_self_restart_argv

        # status/kill return immediately — not the deadlock.
        assert not is_self_restart_argv(["systemctl", "--user", "status", "untether"])
        assert not is_self_restart_argv(["systemctl", "--user", "kill", "untether"])
        # Other units.
        assert not is_self_restart_argv(["systemctl", "--user", "restart", "nginx"])
        # Words inside a shell string must not count (argv-token matching).
        assert not is_self_restart_argv(
            ["bash", "-c", "echo systemctl restart untether"]
        )
        assert not is_self_restart_argv(
            ["grep", "systemctl restart untether", "notes.md"]
        )
        assert not is_self_restart_argv([])

    def test_launchctl_disruptive_verbs_match(self) -> None:
        from untether.shutdown import is_self_restart_argv

        assert is_self_restart_argv(
            ["launchctl", "kickstart", "-k", "gui/501/com.littlebearapps.untether"]
        )
        assert is_self_restart_argv(
            ["launchctl", "stop", "com.littlebearapps.untether"]
        )
        assert not is_self_restart_argv(
            ["launchctl", "print", "gui/501/com.littlebearapps.untether"]
        )


class TestScanSelfRestartEvidence:
    def test_none_pid_returns_none(self) -> None:
        from untether.shutdown import scan_self_restart_evidence

        assert scan_self_restart_evidence(None) is None

    def test_matching_descendant_returns_label(self, monkeypatch) -> None:
        from untether import shutdown
        from untether.utils import proc_diag

        monkeypatch.setattr(proc_diag, "find_descendants", lambda pid: [111, 222])
        monkeypatch.setattr(
            proc_diag,
            "read_cmdline_argv",
            lambda pid: (
                ["systemctl", "--user", "restart", "untether"] if pid == 222 else None
            ),
        )
        assert shutdown.scan_self_restart_evidence(42) == "cmdline:systemctl"

    def test_no_match_and_scan_error_fail_closed(self, monkeypatch) -> None:
        from untether import shutdown
        from untether.utils import proc_diag

        monkeypatch.setattr(proc_diag, "find_descendants", lambda pid: [111])
        monkeypatch.setattr(proc_diag, "read_cmdline_argv", lambda pid: ["sleep", "60"])
        assert shutdown.scan_self_restart_evidence(42) is None

        def _boom(pid: int) -> list[int]:
            raise RuntimeError("proc walk failed")

        monkeypatch.setattr(proc_diag, "find_descendants", _boom)
        assert shutdown.scan_self_restart_evidence(42) is None
