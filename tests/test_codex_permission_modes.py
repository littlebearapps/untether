"""Codex permission tiers (#421) on both transports, and the #1394 guard.

App-server: each tier → the exact ``thread/start`` / ``thread/resume``
fields, the trust rule for writable tiers (DEC-7, D1), unattended runs
(``never``, D4), unknown modes (read-only, DEC-5), the footer label and the
echo check. Exec: the prompting tiers are refused before spawn. Plus the
trigger validator, the Diff preview capability and ``--remote``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from tests.test_codex_app_server_runner import (
    THREAD,
    Fake,
    _completed,
    _run,
    _runner,
)
from untether.model import ActionEvent, ResumeToken, StartedEvent
from untether.runner import prespawn_blocked_reason
from untether.runner_bridge import _resume_failure_clears_session
from untether.runners import codex_app_server as gate
from untether.runners.codex import (
    CODEX_MODE_NEEDS_APP_SERVER_BLOCK,
    CodexRunner,
    find_blocked_codex_args,
)
from untether.runners.run_options import (
    CODEX_PERMISSION_MODES_APP_SERVER,
    VALID_PERMISSION_MODES_BY_ENGINE,
    EngineRunOptions,
)
from untether.telegram.engine_overrides import diff_preview_supported
from untether.triggers.settings import CronConfig, WebhookConfig
from untether.utils.paths import reset_run_base_dir, set_run_base_dir


@pytest.fixture
def project(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    token = set_run_base_dir(proj)
    try:
        yield proj
    finally:
        reset_run_base_dir(token)


@pytest.fixture
def gate_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def supported(cmd: str, resume: ResumeToken | None) -> None:
        calls.append(cmd)

    monkeypatch.setattr(gate, "app_server_unsupported_event", supported)
    return calls


def _trusted(project: Path) -> dict:
    return {
        "sandbox_mode": "workspace-write",
        "projects": {str(project): {"trust_level": "trusted"}},
    }


_TIERS = [
    # mode, approvalPolicy, sandbox (None = omitted), reviewer, config/read?
    (None, "never", None, "user", True),
    ("auto", "never", None, "user", True),
    ("safe", "never", "read-only", "user", False),
    ("ask", "on-request", "workspace-write", "user", True),
    ("ask-writes", "on-request", "read-only", "user", False),
    ("review", "on-request", "workspace-write", "auto_review", True),
]


@pytest.mark.anyio
@pytest.mark.parametrize(("mode", "policy", "sandbox", "reviewer", "reads"), _TIERS)
async def test_tier_params(
    tmp_path: Path,
    project: Path,
    gate_calls: list[str],
    mode: str | None,
    policy: str,
    sandbox: str | None,
    reviewer: str,
    reads: bool,
) -> None:
    fake = Fake(tmp_path, config=_trusted(project))
    with capture_logs() as logs:
        events = await _run(
            _runner(fake), options=EngineRunOptions(permission_mode=mode)
        )
    assert _completed(events).ok
    expected = {"cwd": str(project), "approvalPolicy": policy}
    expected["approvalsReviewer"] = reviewer
    if sandbox is not None:
        expected["sandbox"] = sandbox
    assert fake.requests("thread/start") == [expected]
    assert bool(fake.requests("config/read")) is reads
    logged = [e for e in logs if e["event"] == "codex.app_server.permissions"]
    assert logged[0]["requested"]["approvalPolicy"] == policy
    assert logged[0]["requested"]["approvalsReviewer"] == reviewer
    # Matching echo: no mismatch row.
    assert not [e for e in events if isinstance(e, ActionEvent)]


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["ask", "review"])
async def test_resume_sends_the_same_tier(
    tmp_path: Path, project: Path, gate_calls: list[str], mode: str
) -> None:
    fake = Fake(tmp_path, config=_trusted(project))
    resume = ResumeToken(engine="codex", value=THREAD)
    events = await _run(
        _runner(fake), resume=resume, options=EngineRunOptions(permission_mode=mode)
    )
    assert _completed(events).ok
    params = fake.requests("thread/resume")[0]
    assert params["approvalPolicy"] == "on-request"
    assert params["sandbox"] == "workspace-write"
    assert params["approvalsReviewer"] == (
        "auto_review" if mode == "review" else "user"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["ask", "review"])
async def test_writable_tier_in_untrusted_project_runs_read_only_with_row(
    tmp_path: Path, project: Path, gate_calls: list[str], mode: str
) -> None:
    # D1(a): no [projects] entry → read-only + the DEC-7 row, never trust.
    fake = Fake(tmp_path, config={"projects": {}})
    events = await _run(_runner(fake), options=EngineRunOptions(permission_mode=mode))
    start = fake.requests("thread/start")[0]
    assert start["sandbox"] == "read-only"
    assert start["approvalPolicy"] == "on-request"
    rows = [e.action.id for e in events if isinstance(e, ActionEvent)]
    assert rows == ["codex.untrusted_read_only"]
    assert _completed(events).ok


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mode", "sandbox"),
    [
        ("ask", "workspace-write"),
        ("review", "workspace-write"),
        ("ask-writes", "read-only"),
    ],
)
async def test_unattended_runs_never_ask(
    tmp_path: Path, project: Path, gate_calls: list[str], mode: str, sandbox: str
) -> None:
    # #835 / D4: cron and webhook runs send `never` and the user reviewer.
    fake = Fake(tmp_path, config=_trusted(project))
    options = EngineRunOptions(permission_mode=mode, unattended_trigger="cron:c")
    events = await _run(_runner(fake), options=options)
    assert _completed(events).ok
    start = fake.requests("thread/start")[0]
    assert start["approvalPolicy"] == "never"
    assert start["approvalsReviewer"] == "user"
    assert start["sandbox"] == sandbox


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["untrusted", "on-request", "Ask", "weird"])
async def test_unknown_mode_runs_read_only(
    tmp_path: Path, project: Path, gate_calls: list[str], mode: str
) -> None:
    fake = Fake(tmp_path)
    events = await _run(_runner(fake), options=EngineRunOptions(permission_mode=mode))
    start = fake.requests("thread/start")[0]
    assert (start["approvalPolicy"], start["sandbox"]) == ("never", "read-only")
    started = events[0]
    assert isinstance(started, StartedEvent)
    assert started.meta["permissionMode"] == "safe"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mode", "label"),
    [
        (None, None),
        ("safe", "safe"),
        ("ask", "ask me"),
        ("ask-writes", "ask before writes"),
        ("review", "auto-review"),
    ],
)
async def test_footer_label_per_tier(
    tmp_path: Path,
    project: Path,
    gate_calls: list[str],
    mode: str | None,
    label: str | None,
) -> None:
    fake = Fake(tmp_path, config=_trusted(project))
    events = await _run(_runner(fake), options=EngineRunOptions(permission_mode=mode))
    started = events[0]
    assert isinstance(started, StartedEvent)
    assert started.meta.get("permissionMode") == label


@pytest.mark.anyio
async def test_echo_mismatch_per_tier(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(
        tmp_path,
        config=_trusted(project),
        echo_sandbox={"type": "readOnly", "networkAccess": False},
    )
    with capture_logs() as logs:
        events = await _run(
            _runner(fake), options=EngineRunOptions(permission_mode="ask")
        )
    rows = [e for e in events if isinstance(e, ActionEvent)]
    assert [r.action.id for r in rows] == ["codex.permission_mismatch"]
    assert "workspace-write" in rows[0].action.title
    mismatch = [e for e in logs if e["event"] == "codex.permission_mode.mismatch"]
    assert mismatch[0]["fields"] == ["sandbox"]


# --- exec: prompting tiers refused before spawn ---------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["ask", "ask-writes", "review"])
async def test_exec_refuses_app_server_only_modes_before_spawn(
    tmp_path: Path, project: Path, mode: str
) -> None:
    script = tmp_path / "codex"
    marker = tmp_path / "spawned"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    resume = ResumeToken(engine="codex", value=THREAD)
    runner = CodexRunner(codex_cmd=str(script), extra_args=[])
    events = await _run(
        runner, resume=resume, options=EngineRunOptions(permission_mode=mode)
    )
    done = _completed(events)
    assert len(events) == 1 and not done.ok
    assert prespawn_blocked_reason(done.usage) == CODEX_MODE_NEEDS_APP_SERVER_BLOCK
    assert done.resume == resume
    assert not _resume_failure_clears_session("codex", done.usage, done.error)
    assert 'transport = "app-server"' in done.error
    assert not marker.exists()


@pytest.mark.anyio
async def test_exec_webhook_with_an_app_server_mode_is_refused_not_downgraded(
    tmp_path: Path, project: Path
) -> None:
    script = tmp_path / "codex"
    script.write_text("#!/bin/sh\nexit 1\n")
    script.chmod(0o755)
    options = EngineRunOptions(permission_mode="ask", unattended_trigger="webhook:w")
    events = await _run(
        CodexRunner(codex_cmd=str(script), extra_args=[]), options=options
    )
    done = _completed(events)
    assert prespawn_blocked_reason(done.usage) == CODEX_MODE_NEEDS_APP_SERVER_BLOCK


def test_transport_attribute() -> None:
    assert CodexRunner.transport == "exec"
    assert gate.CodexAppServerRunner.transport == "app-server"


def test_diff_preview_supported_by_transport() -> None:
    assert diff_preview_supported("codex", transport="app-server") is True
    assert diff_preview_supported("codex", transport="exec") is False
    assert diff_preview_supported("codex") is False
    assert diff_preview_supported("claude") is True


# --- validator ------------------------------------------------------------------


def test_validator_has_the_five_codex_modes() -> None:
    assert VALID_PERMISSION_MODES_BY_ENGINE["codex"] == frozenset(
        {"auto", "safe", "ask", "ask-writes", "review"}
    )
    assert set(gate.TIERS) == CODEX_PERMISSION_MODES_APP_SERVER


@pytest.mark.parametrize("mode", sorted(CODEX_PERMISSION_MODES_APP_SERVER))
def test_cron_accepts_codex_modes(mode: str) -> None:
    c = CronConfig(
        id="cr", schedule="* * * * *", engine="codex", prompt="Hi", permission_mode=mode
    )
    assert c.permission_mode == mode


@pytest.mark.parametrize("mode", ["untrusted", "on-request", "Ask", "full-auto"])
def test_cron_rejects_codex_typos(mode: str) -> None:
    with pytest.raises(ValidationError, match="unknown permission_mode"):
        CronConfig(
            id="cr",
            schedule="* * * * *",
            engine="codex",
            prompt="Hi",
            permission_mode=mode,
        )


def test_webhook_accepts_a_codex_tier_and_rejects_a_typo() -> None:
    base: dict[str, Any] = {
        "id": "w",
        "path": "/hooks/w",
        "auth": "none",
        "prompt_template": "x",
        "engine": "codex",
    }
    assert WebhookConfig(**base, permission_mode="ask").permission_mode == "ask"
    with pytest.raises(ValidationError, match="unknown permission_mode"):
        WebhookConfig(**base, permission_mode="untrusted")


# --- #1394: --remote ------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["--remote", "ws://127.0.0.1:9"],
        ["--remote=ws://127.0.0.1:9"],
        ["--remote-auth-token-env", "TOKEN"],
        ["--remote-auth-token-env=TOKEN"],
    ],
)
def test_remote_flags_are_blocked(args: list[str]) -> None:
    hits = find_blocked_codex_args(args)
    assert [h.category for h in hits] == ["workspace"]
    assert "another app server" in hits[0].hint
    assert "127.0.0.1" not in hits[0].hint and "TOKEN" not in hits[0].hint
