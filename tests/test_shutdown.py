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
    def test_sole_run_uses_short_timeout(self) -> None:
        # #559: a single active run (self-restart deadlock) drains fast.
        assert select_drain_timeout(1) == SELF_RESTART_DRAIN_TIMEOUT_S

    def test_multiple_runs_use_full_timeout(self) -> None:
        assert select_drain_timeout(2) == DRAIN_TIMEOUT_S
        assert select_drain_timeout(5) == DRAIN_TIMEOUT_S

    def test_short_timeout_is_smaller(self) -> None:
        assert SELF_RESTART_DRAIN_TIMEOUT_S < DRAIN_TIMEOUT_S
