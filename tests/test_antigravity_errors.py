"""Antigravity (agy) error paths, stderr kill classes and status mapping
(#558 phase 03, D27).

Every way an agy run can fail ends in one clear message within seconds:
signed-out host, account/Terms-of-Service block, quota or AI credits used up,
a stale ``--conversation`` id, a rejected flag, a turn-level ``AGY_ERROR``,
non-SUCCESS statuses. The stderr hook (opt-in on the base runner) kills agy
on the hang-prone classes, escalating to SIGKILL after 3 s.

Real subprocess through ``run()`` over ``tests/fake_clis/fake_agy.py``; the
1.2.14-style signed-out wait and the quota/credits/account strings are
synthetic scripts written per test (P19: replace them with real captures on
first sight). No network, no Google quota.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog.testing

from untether.error_hints import get_error_hint
from untether.model import CompletedEvent, ResumeToken, StartedEvent
from untether.runner_bridge import _RESUME_FAILURE_RE
from untether.runners import antigravity as agy
from untether.runners.antigravity import (
    ACCOUNT_BLOCKED_TEXT,
    AUTH_TEXT,
    CONVERSATION_GONE_TEXT,
    CREDITS_TEXT,
    QUOTA_TEXT,
    AntigravityRunner,
)
from untether.schemas import antigravity as schema
from untether.utils.paths import reset_run_base_dir, set_run_base_dir

FAKE_AGY = Path(__file__).parent / "fake_clis" / "fake_agy.py"
ENGINE = "antigravity"
OK_ID = "90ca6744-e8d4-46d4-b5fc-64a30ae2fa6d"
GONE_ID = "00000000-1111-2222-3333-444444444444"
_INIT = (
    '{"event":"init","conversation_id":"' + OK_ID + '","init":{"cwd":"<scratch>",'
    '"tools":["run_command"],"permission_mode":"request-review"}}'
)
_DELTA = (
    '{"event":"step_update","step_update":{"conversation_id":"' + OK_ID + '",'
    '"step_index":1,"state":"ACTIVE","step_type":"agent_response",'
    '"text_delta":"Partial work"}}'
)
# agy 1.2.14's signed-out wait (Z2): URL + "Waiting for authentication
# (timeout 60s)…", then nothing until the 60 s timeout. 1.3.2 errors in
# ~6 s instead (P17), but a reworded/regressed agy must still be bounded.
_OAUTH_URL = (
    "https://accounts.google.com/o/oauth2/auth?client_id=x&code_challenge="
    "SECRETPKCECHALLENGE&state=abc"
)
_UNAUTH_WAIT = (
    "err:Authentication required. Please visit the URL to log in:\n"
    f"err:{_OAUTH_URL}\n"
    "err:Waiting for authentication (timeout 60s)...\n"
    "err:Or, paste the authorization code here and press Enter:\n"
    "rc:1\n"
)


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    proj = tmp_path / "proj"
    proj.mkdir()
    token = set_run_base_dir(proj)
    try:
        yield proj
    finally:
        reset_run_base_dir(token)


@pytest.fixture(autouse=True)
def _real_version_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agy, "_probe_agy_version", agy._run_agy_version)


def _script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    path = tmp_path / "scenario.script"
    path.write_text(body)
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCRIPT", str(path))


async def _run(resume: ResumeToken | None = None) -> list[Any]:
    assert os.access(FAKE_AGY, os.X_OK), f"chmod +x {FAKE_AGY}"
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    return [evt async for evt in runner.run("Reply with exactly: OK", resume)]


def _done(events: list[Any]) -> CompletedEvent:
    assert isinstance(events[-1], CompletedEvent)
    assert sum(isinstance(e, CompletedEvent) for e in events) == 1
    return events[-1]


def _replay(
    lines: list[str], resume: ResumeToken | None = None
) -> tuple[list[Any], agy.AntigravityStreamState]:
    runner = AntigravityRunner(antigravity_cmd="agy")
    state = runner.new_state("prompt", resume)
    runner.start_run("prompt", resume, state=state)
    events: list[Any] = []
    for line in lines:
        events.extend(
            runner.translate(
                schema.decode_event(line),
                state=state,
                resume=resume,
                found_session=None,
            )
        )
    return events, state


def _result(status: str, error: str | None = None, response: str = "") -> str:
    err = "" if error is None else f',"error":"{error}"'
    return (
        '{"event":"result","result":{"conversation_id":"' + OK_ID + '",'
        f'"status":"{status}","response":"{response}"{err}}}}}'
    )


# ── auth (D27) ─────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_auth_required_killed_fast(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(tmp_path, monkeypatch, _UNAUTH_WAIT)
    monkeypatch.setenv("UNTETHER_FAKE_AGY_HOLD_S", "60")
    start = time.monotonic()
    with structlog.testing.capture_logs() as logs:
        events = await _run()
    elapsed = time.monotonic() - start
    assert elapsed < 5, f"signed-out agy took {elapsed:.1f}s to stop"
    done = _done(events)
    assert done.ok is False
    assert done.error == AUTH_TEXT
    assert "accounts.google.com" not in (done.error or "")
    names = [e["event"] for e in logs]
    assert "antigravity.auth.required" in names
    (killed,) = [e for e in logs if e["event"] == "antigravity.auth.killed"]
    assert killed["elapsed_ms"] < 5000


@pytest.mark.anyio
async def test_auth_required_killed_fast_on_1_3_2_signature(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P17 (1.3.2): stderr ``Error: authentication required…`` then an ERROR
    result; a hold after it must not keep the run open."""
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "unauth")
    monkeypatch.setenv("UNTETHER_FAKE_AGY_HOLD_S", "60")
    start = time.monotonic()
    events = await _run()
    assert time.monotonic() - start < 5
    done = _done(events)
    assert done.ok is False and done.error == AUTH_TEXT
    assert not any(isinstance(e, StartedEvent) for e in events)


@pytest.mark.anyio
async def test_auth_kill_escalates_to_sigkill(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(tmp_path, monkeypatch, _UNAUTH_WAIT)
    monkeypatch.setenv("UNTETHER_FAKE_AGY_HOLD_S", "60")
    monkeypatch.setenv("UNTETHER_FAKE_AGY_IGNORE_TERM", "1")
    start = time.monotonic()
    with structlog.testing.capture_logs() as logs:
        events = await _run()
    elapsed = time.monotonic() - start
    assert elapsed < 5, f"SIGTERM-proof agy took {elapsed:.1f}s to die"
    assert elapsed >= 2.5  # SIGKILL only after the 3 s grace
    done = _done(events)
    assert done.ok is False and done.error == AUTH_TEXT
    assert any(e["event"] == "subprocess.kill_escalated" for e in logs)


def test_auth_result_text_maps_even_without_stderr() -> None:
    lines = [
        '{"event":"result","result":{"conversation_id":"","status":"ERROR",'
        '"response":"","error":"authentication failed or timed out"}}'
    ]
    with structlog.testing.capture_logs() as logs:
        events, _ = _replay(lines)
    done = _done(events)
    assert done.ok is False and done.error == AUTH_TEXT
    assert any(e["event"] == "antigravity.auth.failed" for e in logs)
    hint = get_error_hint(AUTH_TEXT)
    assert hint is not None and "GEMINI_API_KEY" in hint


@pytest.mark.anyio
async def test_stderr_never_logged_unsanitised_above_debug(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(tmp_path, monkeypatch, _UNAUTH_WAIT)
    monkeypatch.setenv("UNTETHER_FAKE_AGY_HOLD_S", "60")
    with structlog.testing.capture_logs() as logs:
        await _run()
    loud = [e for e in logs if e.get("log_level") in {"warning", "error", "info"}]
    assert loud
    for entry in loud:
        assert "https://" not in repr(entry), entry
        assert "SECRETPKCECHALLENGE" not in repr(entry), entry


# ── stale --conversation id ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_conversation_gone_killed_on_stderr_warning(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The warning comes at 0.57 s, the new conversation's init at 2.24 s:
    the stderr kill must beat the init (real timing replayed)."""
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "unknown_conversation")
    monkeypatch.setenv("UNTETHER_FAKE_AGY_TIME_SCALE", "1")
    resume = ResumeToken(engine=ENGINE, value=GONE_ID)
    start = time.monotonic()
    with structlog.testing.capture_logs() as logs:
        events = await _run(resume)
    assert time.monotonic() - start < 2
    (done,) = events
    assert isinstance(done, CompletedEvent) and done.ok is False
    assert done.error == CONVERSATION_GONE_TEXT
    assert _RESUME_FAILURE_RE.search(done.error or "")
    assert done.resume == resume
    (missing,) = [e for e in logs if e["event"] == "antigravity.conversation.missing"]
    assert missing["detected_by"] == "stderr"


# ── quota / credits / account (D27) ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("stderr", "text", "log_event"),
    [
        (
            "Individual quota reached for gemini-3.8-flash, retrying in 30s",
            QUOTA_TEXT,
            "antigravity.quota.exhausted",
        ),
        (
            "Error: Your AI credits balance is too low to continue.",
            CREDITS_TEXT,
            "antigravity.credits.low",
        ),
        (
            "Error: Google needs you to verify your account before continuing.",
            ACCOUNT_BLOCKED_TEXT,
            "antigravity.account.blocked",
        ),
        (
            "Error: this account was blocked for violating the Terms of Service;"
            " appeal at https://support.google.com/x",
            ACCOUNT_BLOCKED_TEXT,
            "antigravity.account.blocked",
        ),
    ],
)
@pytest.mark.anyio
async def test_quota_credits_and_account_blocked_each_distinct(
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
    text: str,
    log_event: str,
) -> None:
    _script(tmp_path, monkeypatch, f"out:{_INIT}\nout:{_DELTA}\nerr:{stderr}\n")
    monkeypatch.setenv("UNTETHER_FAKE_AGY_HOLD_S", "60")
    start = time.monotonic()
    with structlog.testing.capture_logs() as logs:
        events = await _run()
    assert time.monotonic() - start < 5
    done = _done(events)
    assert done.ok is False and done.error == text
    assert "Partial work" in done.answer
    assert done.resume == ResumeToken(engine=ENGINE, value=OK_ID)
    assert any(e["event"] == log_event for e in logs)
    assert not _RESUME_FAILURE_RE.search(done.error or "")


def test_kill_texts_are_distinct() -> None:
    texts = {AUTH_TEXT, ACCOUNT_BLOCKED_TEXT, QUOTA_TEXT, CREDITS_TEXT}
    assert len(texts) == 4
    for text in texts:
        assert not _RESUME_FAILURE_RE.search(text)
    assert "/usage" in (get_error_hint(QUOTA_TEXT) or "")
    assert "/usage" in (get_error_hint(CREDITS_TEXT) or "")


def test_unrelated_stderr_never_kills() -> None:
    runner = AntigravityRunner(antigravity_cmd="agy")
    state = runner.new_state("prompt", None)

    class _Proc:
        pid = 1
        returncode = None

        def terminate(self) -> None:  # pragma: no cover - must not run
            raise AssertionError("killed on an unrelated line")

    for line in (
        "jetski: no output produced",
        "warning: ignoring unsupported stream input message event",
        "some MCP server: terms and conditions apply",
        "github-mcp: Authentication required for this repository",
        "[mcp] Terms of Service updated",
        "x" * 100_000,
    ):
        runner.on_stderr_line(line, state=state, proc=_Proc())
    assert state.kill_reason is None


# ── result vs late stderr (R1), AGY_ERROR, argv ─────────────────────────────


@pytest.mark.anyio
async def test_result_then_late_agy_error_result_wins(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "synthetic_result_then_agy_error")
    events = await _run()
    done = _done(events)
    assert done.ok is True and done.answer == "OK\n" and done.error is None


@pytest.mark.anyio
async def test_agy_error_rc3_partial_answer(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "synthetic_agy_error_rc3")
    with structlog.testing.capture_logs() as logs:
        events = await _run()
    done = _done(events)
    assert done.ok is False
    assert (done.error or "").startswith(
        "agy reported a model/agent error (INTERNAL, retryable=unknown"
    )
    assert "rc=3" in (done.error or "")
    assert done.answer == "OK"
    assert done.resume == ResumeToken(engine=ENGINE, value=OK_ID)
    (logged,) = [e for e in logs if e["event"] == "antigravity.agy_error"]
    assert logged["keys"] == ["code", "message"]
    assert logged["code"] == "INTERNAL"


@pytest.mark.anyio
async def test_agy_error_unparseable_json_tolerated(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(
        tmp_path,
        monkeypatch,
        f"out:{_INIT}\nout:{_DELTA}\nerr:AGY_ERROR: {{not json\n"
        f"err:AGY_ERROR: [1, 2]\nerr:AGY_ERROR: {'{' * 50_000}\nrc:3\n",
    )
    events = await _run()
    done = _done(events)
    assert done.ok is False
    assert "model/agent error (unknown, retryable=unknown" in (done.error or "")
    assert done.answer == "Partial work"


@pytest.mark.anyio
async def test_argv_rejected_logs_and_errors(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(
        tmp_path,
        monkeypatch,
        "err:flags provided but not defined: -print-timeout\nerr:Usage of agy:\nrc:2\n",
    )
    with structlog.testing.capture_logs() as logs:
        events = await _run()
    done = _done(events)
    assert done.ok is False
    assert "flags provided but not defined" in (done.error or "")
    (rejected,) = [e for e in logs if e["event"] == "antigravity.argv.rejected"]
    assert rejected["rc"] == 2
    assert rejected["first_error_line"].startswith("flags provided but not defined")
    assert "--input-format" in rejected["args"]
    hint = get_error_hint(done.error or "")
    assert hint is not None and "rejected a command-line flag" in hint


# ── status mapping ──────────────────────────────────────────────────────────


def test_status_canceled_maps_to_error_with_hint() -> None:
    events, _ = _replay([_INIT, _DELTA, _result("CANCELED", "upstream cancelled")])
    done = _done(events)
    assert done.ok is False
    assert done.error == "antigravity ended with status CANCELED: upstream cancelled"
    assert done.answer == "Partial work"
    assert "antigravity-cli#902" in (get_error_hint(done.error) or "")


def test_status_waiting_maps_to_error() -> None:
    events, _ = _replay([_INIT, _result("WAITING")])
    done = _done(events)
    assert done.ok is False
    assert done.error == "antigravity ended with status WAITING"


def test_status_error_long_text_trimmed_to_three_lines() -> None:
    error = "first\\nsecond\\nthird\\nfourth\\nfifth"
    events, _ = _replay([_INIT, _result("ERROR", error)])
    assert _done(events).error == "first\nsecond\nthird\n…"


def test_invalid_model_error_trimmed_with_hint() -> None:
    lines = (
        (Path(__file__).parent / "fixtures" / "antigravity" / "invalid_model.jsonl")
        .read_text()
        .splitlines()
    )
    events, _ = _replay(lines)
    error = _done(events).error or ""
    assert error.startswith("invalid model selection")
    assert error.count("\n") == 3 and error.endswith("…")
    assert "agy models" in (get_error_hint(error) or "")


def test_effort_errors_get_effort_hint_not_model_hint() -> None:
    for error in (
        'invalid model selection (--model "gemini-3.1-pro" --effort "max"): '
        'gemini-3.1-pro has no "max" effort (available: low, high)',
        'invalid model selection (--model "" --effort "bogus"): invalid --effort '
        '"bogus" (valid: low, medium, high, xhigh, max)',
        'invalid model selection (--model "gemini-3.1-pro-high" --effort "max"): '
        "--model gemini-3.1-pro-high conflicts with --effort=max",
    ):
        hint = get_error_hint(error) or ""
        assert "effort level isn't available" in hint, error


@pytest.mark.anyio
async def test_mode_rejected_and_print_timeout_warnings_logged(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The sleep lets the drain task read stderr before the result closes it.
    monkeypatch.setenv("UNTETHER_FAKE_AGY_TIME_SCALE", "1")
    _script(
        tmp_path,
        monkeypatch,
        'err:warning: unrecognized --mode value "bogus" (valid: accept-edits, plan)\n'
        "err:[agy] print timeout after 1s\nsleep:0.3\n"
        f"out:{_INIT}\nout:{_result('SUCCESS', response='OK')}\n",
    )
    with structlog.testing.capture_logs() as logs:
        events = await _run()
    assert _done(events).ok is True
    names = {e["event"]: e for e in logs}
    assert names["antigravity.mode.rejected"]["log_level"] == "warning"
    assert names["antigravity.print_timeout"]["log_level"] == "warning"
