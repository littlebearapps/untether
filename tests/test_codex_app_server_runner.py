"""Codex app-server pre-spawn support gate (#961), against a fake ``codex``.

The fake answers ``--version`` and ``app-server generate-json-schema
[--experimental] --out DIR`` by copying a hand-built bundle, and logs each
call so the per-binary cache can be checked. The transport runner itself
(#962) adds its tests here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from untether.model import ResumeToken
from untether.runner import prespawn_blocked_reason
from untether.runner_bridge import _resume_failure_clears_session
from untether.runners import codex_app_server as gate
from untether.schemas.codex_app_server import (
    CODEX_APP_SERVER_USED,
    MIN_TESTED_APP_SERVER_VERSION,
)

_UNION = {
    "client_request": "ClientRequest",
    "client_notification": "ClientNotification",
    "server_notification": "ServerNotification",
    "server_request": "ServerRequest",
}


def _bundle(*, skip: frozenset[str] = frozenset()) -> dict:
    defs: dict = {name: {"oneOf": []} for name in _UNION.values()}
    for kind, method in CODEX_APP_SERVER_USED:
        if method not in skip:
            defs[_UNION[kind]]["oneOf"].append(
                {"properties": {"method": {"enum": [method]}}}
            )
    return {"definitions": defs}


def _fake_codex(
    tmp_path: Path,
    *,
    stable: dict | None = None,
    experimental: dict | None = None,
    fail: bool = False,
) -> Path:
    stable_path = tmp_path / "stable.json"
    exp_path = tmp_path / "exp.json"
    stable_path.write_text(json.dumps(stable or _bundle()))
    exp_path.write_text(json.dumps(experimental or _bundle()))
    script = tmp_path / "codex"
    script.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, shutil, sys\n"
        "args = sys.argv[1:]\n"
        f"with open({str(tmp_path / 'calls.log')!r}, 'a') as fh:\n"
        "    fh.write(' '.join(args) + '\\n')\n"
        "if args == ['--version']:\n"
        "    print('codex-cli 9.9.9'); sys.exit(0)\n"
        f"if {fail!r}:\n"
        "    print(\"error: unrecognized subcommand 'app-server'\", file=sys.stderr)\n"
        "    sys.exit(2)\n"
        "out = pathlib.Path(args[args.index('--out') + 1])\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        f"src = {str(exp_path)!r} if '--experimental' in args else {str(stable_path)!r}\n"
        "shutil.copy(src, out / 'codex_app_server_protocol.schemas.json')\n"
    )
    script.chmod(0o755)
    return script


def _schema_calls(tmp_path: Path) -> int:
    log = tmp_path / "calls.log"
    lines = log.read_text().splitlines() if log.exists() else []
    return sum("generate-json-schema" in line for line in lines)


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "_SUPPORT_CACHE", {})


def test_supported_cli_passes_logs_ready_once_and_caches(tmp_path: Path) -> None:
    cmd = str(_fake_codex(tmp_path))
    with capture_logs() as logs:
        first = gate.codex_app_server_support(cmd)
        second = gate.codex_app_server_support(cmd)
    assert first.ok and first.version == "9.9.9"
    assert second is first
    assert _schema_calls(tmp_path) == 2  # stable + experimental, once
    ready = [e for e in logs if e["event"] == "codex.app_server.ready"]
    assert [e["version"] for e in ready] == ["9.9.9"]


def test_binary_change_reprobes(tmp_path: Path) -> None:
    script = _fake_codex(tmp_path)
    gate.codex_app_server_support(str(script))
    st = script.stat()
    os.utime(script, (st.st_atime, st.st_mtime + 10))
    gate.codex_app_server_support(str(script))
    assert _schema_calls(tmp_path) == 4


def test_missing_method_is_unsupported(tmp_path: Path) -> None:
    gone = frozenset({"turn/interrupt"})
    cmd = str(
        _fake_codex(
            tmp_path, stable=_bundle(skip=gone), experimental=_bundle(skip=gone)
        )
    )
    with capture_logs() as logs:
        support = gate.codex_app_server_support(cmd)
    assert support.problems == ("`turn/interrupt` missing",)
    assert any(e["event"] == "codex.app_server.unsupported" for e in logs)


def test_stable_method_moved_to_experimental_is_unsupported(tmp_path: Path) -> None:
    cmd = str(_fake_codex(tmp_path, stable=_bundle(skip=frozenset({"thread/resume"}))))
    support = gate.codex_app_server_support(cmd)
    assert support.problems == ("`thread/resume` is experimental-only",)


def test_cli_without_generate_json_schema_is_unsupported_and_cached(
    tmp_path: Path,
) -> None:
    cmd = str(_fake_codex(tmp_path, fail=True))
    support = gate.codex_app_server_support(cmd)
    assert not support.ok
    assert "schema unavailable" in support.problems[0]
    gate.codex_app_server_support(cmd)
    assert _schema_calls(tmp_path) == 1


def test_probe_that_cannot_run_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd = str(_fake_codex(tmp_path))

    def boom(_cmd: str) -> dict:
        raise subprocess.TimeoutExpired(cmd=_cmd, timeout=30)

    monkeypatch.setattr(gate, "probe_schema_subset", boom)
    with capture_logs() as logs:
        support = gate.codex_app_server_support(cmd)
    assert support.problems == ("schema probe could not run",)
    assert any(e["event"] == "codex.app_server.probe_failed" for e in logs)
    assert gate._SUPPORT_CACHE == {}


def test_missing_binary_is_unsupported(tmp_path: Path) -> None:
    support = gate.codex_app_server_support("untether-no-such-codex-binary")
    assert not support.ok and "not found" in support.problems[0]


@pytest.mark.anyio
async def test_prespawn_refusal_keeps_the_saved_session(tmp_path: Path) -> None:
    gone = frozenset({"turn/start"})
    cmd = str(
        _fake_codex(
            tmp_path, stable=_bundle(skip=gone), experimental=_bundle(skip=gone)
        )
    )
    resume = ResumeToken(engine="codex", value="00000000-0000-0000-0000-000000000000")
    event = await gate.app_server_unsupported_event(cmd, resume)
    assert event is not None and not event.ok
    assert event.resume == resume
    assert prespawn_blocked_reason(event.usage) == gate.APP_SERVER_UNSUPPORTED_BLOCK
    assert not _resume_failure_clears_session("codex", event.usage, event.error)
    assert "9.9.9" in event.error and "`turn/start` missing" in event.error
    assert MIN_TESTED_APP_SERVER_VERSION in event.error
    assert 'transport = "exec"' in event.error


@pytest.mark.anyio
async def test_supported_cli_yields_no_refusal(tmp_path: Path) -> None:
    assert (
        await gate.app_server_unsupported_event(str(_fake_codex(tmp_path)), None)
        is None
    )
