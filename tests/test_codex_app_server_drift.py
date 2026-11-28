"""Zero-token drift checks of the installed Codex ``app-server`` (#961).

Regenerates both schema bundles (``generate-json-schema`` and
``--experimental``) and compares the subset Untether depends on with the
committed fixture: a removed or tier-moved method, field, union variant or
enum value fails. Then one JSON-RPC handshake: ``initialize`` (stable),
``collaborationMode/list`` must be refused without ``experimentalApi``, and
closing stdin must end the process within 5 s. No ``thread/start`` (it
starts MCP servers and may touch the network) and no turns.

Same conventions as ``tests/test_codex_cli_schema_drift.py``: fresh
``CODEX_HOME`` and cwd under ``tmp_path``, bounded waits (a timeout fails),
skips when ``codex`` is absent (CI). Run on lba-1 before every rc:
``uv run pytest tests/test_codex_app_server_drift.py -v -rs``.
"""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from untether.runners.codex_app_server import (
    codex_app_server_support,
    probe_schema_subset,
)
from untether.schemas.codex_app_server import (
    CODEX_APP_SERVER_FIELDS,
    PROBED_APP_SERVER_VERSION,
)

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None, reason="codex CLI not installed"
)

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "codex_app_server"
    / f"schema-subset-{PROBED_APP_SERVER_VERSION}.json"
)
_RPC_TIMEOUT_S = 30.0
_EXIT_TIMEOUT_S = 5.0
_REDERIVE = (
    "re-derive with scripts/codex_app_server_schema_subset.py and review "
    f"the app-server transport (last green on codex-cli {PROBED_APP_SERVER_VERSION})"
)


@pytest.fixture
def codex_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "codex_home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    return home


def test_schema_subset_has_not_drifted(codex_env: Path) -> None:
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))["subset"]
    live = probe_schema_subset("codex")
    drift: list[str] = []
    for key, want in expected.items():
        got = live.get(key)
        if got is None:
            drift.append(f"{key}: removed")
            continue
        if got["tier"] != want["tier"]:
            drift.append(f"{key}: tier {want['tier']} → {got['tier']}")
        for name, tier in want["names"].items():
            if name not in got["names"]:
                drift.append(f"{key}: {name} removed")
            elif got["names"][name] != tier:
                drift.append(f"{key}: {name} tier {tier} → {got['names'][name]}")
    assert not drift, f"app-server schema drift: {drift} — {_REDERIVE}"


def test_plan_mode_field_tiers(codex_env: Path) -> None:
    """#965: ``turn/start.collaborationMode`` stays experimental (so plan
    mode needs ``experimentalApi``) and the resume echo stays stable."""
    live = probe_schema_subset("codex")
    drift = [
        f"{key} {name}: want {tier}, got {live.get(key, {}).get('names', {}).get(name)}"
        for (key, name), tier in CODEX_APP_SERVER_FIELDS.items()
        if live.get(key, {}).get("names", {}).get(name) != tier
    ]
    assert not drift, f"plan-mode field drift: {drift} — {_REDERIVE}"


def test_gate_passes_on_the_installed_cli(codex_env: Path) -> None:
    support = codex_app_server_support("codex")
    assert support.ok, f"{support.problems} — {_REDERIVE}"


class _Rpc:
    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self.proc = proc
        self.lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def send(self, msg: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def response(self, req_id: int) -> dict:
        deadline = time.monotonic() + _RPC_TIMEOUT_S
        while (left := deadline - time.monotonic()) > 0:
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                break
            if line is None:
                pytest.fail(f"app-server exited before answering id {req_id}")
            msg = json.loads(line)
            if msg.get("id") == req_id and "method" not in msg:
                return msg
        pytest.fail(f"no app-server response to id {req_id} in {_RPC_TIMEOUT_S}s")


def test_handshake_experimental_refusal_and_clean_exit(codex_env: Path) -> None:
    proc = subprocess.Popen(
        ["codex", "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        rpc = _Rpc(proc)
        rpc.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "untether_probe",
                        "title": "Untether probe",
                        "version": "0",
                    },
                    "capabilities": {"experimentalApi": False},
                },
            }
        )
        init = rpc.response(1)
        assert "result" in init, f"initialize failed: {init} — {_REDERIVE}"
        assert init["result"]["userAgent"].startswith("untether_probe/")
        rpc.send({"jsonrpc": "2.0", "method": "initialized"})
        rpc.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "collaborationMode/list",
                "params": {},
            }
        )
        refused = rpc.response(2)
        assert "requires experimentalApi capability" in json.dumps(
            refused.get("error")
        ), f"experimental gating changed: {refused} — {_REDERIVE}"
        assert proc.stdin is not None
        proc.stdin.close()
        try:
            proc.wait(timeout=_EXIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            pytest.fail(
                f"app-server still running {_EXIT_TIMEOUT_S}s after stdin closed"
            )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
