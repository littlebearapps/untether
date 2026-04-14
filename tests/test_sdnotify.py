"""Tests for the stdlib sd_notify client (#287)."""

from __future__ import annotations

import socket as socket_mod
from typing import Any

from untether import sdnotify


class FakeSocket:
    """Minimal AF_UNIX SOCK_DGRAM stand-in — records sendto() calls."""

    calls: list[tuple[bytes, Any]]

    def __init__(self, family: int, kind: int, *args: Any, **kwargs: Any) -> None:
        assert family == socket_mod.AF_UNIX
        assert kind == socket_mod.SOCK_DGRAM
        self.calls = []

    def sendto(self, data: bytes, addr: Any) -> int:
        self.calls.append((data, addr))
        return len(data)

    def __enter__(self) -> FakeSocket:
        return self

    def __exit__(self, *exc: Any) -> None:
        pass


class TestNotify:
    def test_notify_absent_socket_returns_false(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        assert sdnotify.notify("READY=1") is False

    def test_notify_empty_socket_returns_false(self, monkeypatch):
        monkeypatch.setenv("NOTIFY_SOCKET", "")
        assert sdnotify.notify("READY=1") is False

    def test_notify_with_filesystem_socket(self, monkeypatch):
        created: list[FakeSocket] = []

        def _socket_factory(*args, **kwargs):
            sock = FakeSocket(*args, **kwargs)
            created.append(sock)
            return sock

        monkeypatch.setenv("NOTIFY_SOCKET", "/run/user/1000/systemd/notify")
        monkeypatch.setattr(socket_mod, "socket", _socket_factory)
        assert sdnotify.notify("READY=1") is True
        assert len(created) == 1
        assert created[0].calls == [(b"READY=1", "/run/user/1000/systemd/notify")]

    def test_notify_with_abstract_namespace(self, monkeypatch):
        """Leading '@' in NOTIFY_SOCKET translates to a leading null byte."""
        created: list[FakeSocket] = []

        def _socket_factory(*args, **kwargs):
            sock = FakeSocket(*args, **kwargs)
            created.append(sock)
            return sock

        monkeypatch.setenv("NOTIFY_SOCKET", "@systemd-notify-abs")
        monkeypatch.setattr(socket_mod, "socket", _socket_factory)
        assert sdnotify.notify("STOPPING=1") is True
        assert created[0].calls == [(b"STOPPING=1", b"\0systemd-notify-abs")]

    def test_notify_swallows_send_errors(self, monkeypatch):
        class FailingSocket(FakeSocket):
            def sendto(self, data: bytes, addr: Any) -> int:
                raise OSError(111, "Connection refused")

        monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/nope")
        monkeypatch.setattr(socket_mod, "socket", FailingSocket)
        # Must not raise.
        assert sdnotify.notify("READY=1") is False

    def test_notify_swallows_socket_creation_errors(self, monkeypatch):
        def _socket_factory(*args, **kwargs):
            raise OSError(13, "Permission denied")

        monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/nope")
        monkeypatch.setattr(socket_mod, "socket", _socket_factory)
        assert sdnotify.notify("READY=1") is False

    def test_notify_encodes_utf8_messages(self, monkeypatch):
        created: list[FakeSocket] = []

        def _socket_factory(*args, **kwargs):
            sock = FakeSocket(*args, **kwargs)
            created.append(sock)
            return sock

        monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/sock")
        monkeypatch.setattr(socket_mod, "socket", _socket_factory)
        assert sdnotify.notify("STATUS=running — idle") is True
        assert created[0].calls[0][0] == b"STATUS=running \xe2\x80\x94 idle"
