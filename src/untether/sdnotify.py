"""Minimal sd_notify client (stdlib only).

systemd's ``Type=notify`` services use the ``$NOTIFY_SOCKET`` environment
variable to signal readiness and state changes. This module sends datagrams
to that socket with no external dependency.

Messages of interest:
- ``READY=1`` — sent after the bot has finished startup and is serving
  updates. Until this is sent, systemd keeps the unit in "activating".
- ``STOPPING=1`` — sent at the start of the drain sequence so that
  ``systemctl status`` shows "Deactivating" rather than "Active".

When ``NOTIFY_SOCKET`` is absent (non-systemd runs, dev containers,
pytest), ``notify()`` is a no-op returning ``False`` and does not raise.
"""

from __future__ import annotations

import os
import socket

from .logging import get_logger

logger = get_logger(__name__)

__all__ = ["notify"]


def notify(message: str) -> bool:
    """Send ``message`` to the systemd notify socket.

    Returns ``True`` when the datagram was sent, ``False`` otherwise
    (no socket configured, send failed). Never raises — a failure to
    notify systemd must not break the bot.
    """
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return False

    # Abstract sockets on Linux use a leading null byte — systemd
    # encodes this as a leading '@' in the NOTIFY_SOCKET env var.
    addr: str | bytes
    if sock_path.startswith("@"):
        addr = b"\0" + sock_path[1:].encode("utf-8")
    else:
        addr = sock_path

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode("utf-8"), addr)
    except OSError as exc:
        logger.debug(
            "sdnotify.send_failed",
            message=message,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return False

    return True
