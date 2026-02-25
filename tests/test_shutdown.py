"""Tests for the graceful shutdown module."""

from __future__ import annotations

from untether.shutdown import is_shutting_down, request_shutdown, reset_shutdown


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
