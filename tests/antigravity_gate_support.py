"""Shared helpers for the agy gate tests (#980, #981). Not a test module."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any

from untether.antigravity import gate_plugin


def install_plugin(wait_s: int = gate_plugin.DEFAULT_WAIT_S) -> Path:
    """Write the plugin exactly as ``agy plugin install`` leaves it (the
    staged folder, copied), under the tests' ``user_config_dir``."""
    folder = gate_plugin.plugin_dir()
    folder.mkdir(parents=True, exist_ok=True)
    files = gate_plugin.template_files(wait_s)
    for name, data in files.items():
        (folder / name).write_bytes(data)
    (folder / gate_plugin.MANIFEST_FILE).write_bytes(
        gate_plugin._manifest_bytes(files, wait_s)
    )
    return folder


def hooks_listing(
    wait_s: int = gate_plugin.DEFAULT_WAIT_S,
    *,
    enabled: bool = True,
    extra_groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """``agy -p /hooks`` data for the installed plugin (the 1.3.3 shape:
    ``timeout_seconds`` per action, matcher on PreToolUse only)."""
    actions = [
        {
            "event": "PreInvocation",
            "type": "command",
            "command": gate_plugin.hook_command("PreInvocation"),
            "timeout_seconds": gate_plugin.SHORT_HOOK_TIMEOUT_S,
        },
        {
            "event": "PreToolUse",
            "matcher": "*",
            "type": "command",
            "command": gate_plugin.hook_command("PreToolUse"),
            "timeout_seconds": wait_s + gate_plugin.HOOK_GRACE_S,
        },
        {
            "event": "Stop",
            "type": "command",
            "command": gate_plugin.hook_command("Stop"),
            "timeout_seconds": gate_plugin.SHORT_HOOK_TIMEOUT_S,
        },
    ]
    groups = [
        {
            "name": gate_plugin.PLUGIN_NAME,
            "enabled": enabled,
            "source": "~/.gemini/config/plugins/untether-gate/hooks.json",
            "actions": actions,
        }
    ]
    return {"hooks": groups + list(extra_groups or [])}


def short_dir() -> Path:
    """A short folder for Unix sockets (``sun_path`` is ≤ 108 bytes; pytest's
    tmp_path can be longer than that on its own)."""
    return Path(tempfile.mkdtemp(prefix="ut-"))


class FakeGateServer:
    """A one-shot Unix socket server for the hook tests: records the request
    line and answers with *reply* (bytes, sent as is; None: close)."""

    def __init__(self, path: Path, reply: bytes | None) -> None:
        self.path = path
        self.reply = reply
        self.requests: list[dict[str, Any]] = []
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        self._sock.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            line = buf.split(b"\n", 1)[0]
            if line:
                self.requests.append(json.loads(line))
            if self.reply is not None:
                conn.sendall(self.reply)

    def close(self) -> None:
        self._sock.close()
        self._thread.join(timeout=2)
