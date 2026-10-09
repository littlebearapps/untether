"""Antigravity (agy) redaction choke point (#558 phase 03 follow-up).

Everything agy text passes through on its way to a Telegram card or an
INFO+ log goes through ``redact_agy_text``: ANSI stripped, whole URLs
redacted whatever their shape, then every secret-looking ``key=value``
redacted whether or not a URL was recognised, then absolute paths.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog.testing

from untether.model import CompletedEvent
from untether.runners import antigravity as agy
from untether.runners.antigravity import AntigravityRunner
from untether.utils.antigravity_redact import redact_agy_text
from untether.utils.paths import reset_run_base_dir, set_run_base_dir

FAKE_AGY = Path(__file__).parent / "fake_clis" / "fake_agy.py"
S = "S3CR3Tcanary"  # must never survive
_Q = f"client_id=cid123&code={S}&state={S}x"

_SHAPES: dict[str, str] = {
    "plain": f"visit https://accounts.google.com/o/oauth2/auth?{_Q} now",
    "upper_scheme": f"visit HTTPS://ACCOUNTS.GOOGLE.COM/o/oauth2/auth?{_Q}",
    "mixed_scheme": f"visit HtTpS://accounts.google.com/auth?{_Q}",
    "schemeless": f"visit accounts.google.com/o/oauth2/auth?{_Q}",
    "schemeless_no_path": f"visit accounts.google.com?{_Q}",
    "protocol_relative": f"visit //accounts.google.com/o/oauth2/auth?{_Q}",
    "angle": f"visit <https://accounts.google.com/auth?{_Q}>",
    "double_quoted": f'visit "https://accounts.google.com/auth?{_Q}"',
    "single_quoted": f"visit 'https://accounts.google.com/auth?{_Q}'",
    "brackets": f"visit [https://accounts.google.com/auth?{_Q}] (https://a.b/c?{_Q})",
    "ansi_wrapped": f"\x1b[4;34mhttps://accounts.google.com/auth?{_Q}\x1b[0m",
    "ansi_inside": f"https://accounts.google.com/auth?co\x1b[1mde={S}\x1b[0m&state={S}",
    "osc8_link": f"\x1b]8;;https://a.b/c?code={S}\x1b\\click\x1b]8;;\x1b\\",
    "split_lines": f"https://accounts.google.com/o/oauth2/auth?client_id=cid123\n&code={S}&state={S}",
    "split_mid_query": f"https://accounts.google.com/auth?client_id=c\n  code={S}&scope=x",
    "tail_only": f"oauth2/auth?client_id=cid123&code={S}",
    "percent_encoded": f"redirect=https%3A%2F%2Fa.b%2Fcb%3Fcode%3D{S}%26state%3D{S}",
    "amp_escaped": f"https://a.b/cb?client_id=c&amp;code={S}&amp;state={S}",
    "amp_escaped_bare": f"client_id=c&amp;code={S}",
    "userinfo": f"https://user:{S}@accounts.google.com/auth",
    "fragment_token": f"https://a.b/cb#access_token={S}&token_type=Bearer",
    "bare_code": f"paste code={S} here",
    "bare_state": f"state={S}",
    "bare_access_token": f"access_token={S}",
    "bare_id_token": f"id_token={S}",
    "bare_refresh_token": f"refresh_token={S}",
    "bare_client_secret": f"client_secret={S}",
    "bare_code_challenge": f"code_challenge={S}",
    "colon_token": f"refresh_token: {S}",
    "json_token": f'{{"access_token":"{S}","expires_in":3599}}',
    "json_spaced": f'{{"client_secret" : "{S}"}}',
    "nested_in_plain_key": f'{{"status":"code={S}","detail":"state={S}"}}',
    "upper_key": f"ACCESS_TOKEN={S}",
    "bearer": f"Authorization: Bearer {S}{S}",
    "other_scheme": f"open vscode://auth/callback?code={S}",
    "ya29": f"token ya29.{S}-abc_def",
}


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_redacts_secret_whatever_the_url_shape(shape: str) -> None:
    out = redact_agy_text(_SHAPES[shape])
    assert S not in out, out
    assert "\x1b" not in out
    assert "cid123" not in out, out


def test_keeps_ordinary_error_text() -> None:
    for text in (
        'invalid model selection (--model "gemini-3.1-pro" --effort "max"): '
        'gemini-3.1-pro has no "max" effort (available: low, high)',
        "flags provided but not defined: -print-timeout",
        "antigravity failed (rc=1).",
        'warning: unrecognized --mode value "bogus" (valid: accept-edits, plan)',
        "Available models:\n  Gemini 3.8 Flash (High)",
        "error: /model is answered by the CLI itself and is unavailable with "
        "--input-format stream-json; run it as its own --print /model invocation",
    ):
        assert redact_agy_text(text) == text


def test_redacts_paths_and_is_bounded() -> None:
    assert "/home/someone" not in redact_agy_text("broke in /home/someone/secret")
    big = ("a" * 5000 + f" code={S} ") * 400  # ~2 MB
    out = redact_agy_text(big)
    assert S not in out


# ── no agy surface bypasses the choke point ─────────────────────────────────


def test_runner_module_uses_only_the_choke_point() -> None:
    """A new surface that reaches for the shared sanitiser (paths before
    URLs) or builds its own excerpt fails here."""
    source = Path(agy.__file__).read_text()
    for banned in (r"_sanitise_stderr", r"(?<![a-z])_stderr_excerpt", r"_URL_RE"):
        assert not re.search(banned, source), f"{banned} bypasses redact_agy_text"
    # stderr_lines may only be read by the redacting helpers.
    uses = re.findall(r"^.*\bstderr_lines\b.*$", source, re.MULTILINE)
    allowed = (
        "stderr_lines: list[str] | None",
        "_agy_stderr_excerpt(stderr_lines)",
        "_first_argv_error(stderr_lines)",
    )
    stray = [u.strip() for u in uses if not any(a in u for a in allowed)]
    assert not stray, stray


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    proj = tmp_path / "proj"
    proj.mkdir()
    token = set_run_base_dir(proj)
    try:
        yield proj
    finally:
        reset_run_base_dir(token)


_INIT = (
    '{"event":"init","conversation_id":"90ca6744-e8d4-46d4-b5fc-64a30ae2fa6d",'
    '"init":{"cwd":"<scratch>","tools":[],"permission_mode":"request-review"}}'
)
_LEAK = f"accounts.google.com/o/oauth2/auth?client_id=cid123&code={S} refresh_token={S}"


def _result(status: str, error_json: str) -> str:
    return (
        '{"event":"result","result":{"conversation_id":'
        '"90ca6744-e8d4-46d4-b5fc-64a30ae2fa6d","status":"' + status + '",'
        '"response":"","error":' + error_json + "}}"
    )


_SCENARIOS: dict[str, str] = {
    "rc1_excerpt": f"err:Open {_LEAK}\nrc:1\n",
    "stream_end_excerpt": f"out:{_INIT}\nerr:Open {_LEAK}\nrc:0\n",
    "argv_rejected": f"err:flags provided but not defined: -x {_LEAK}\nrc:2\n",
    "agy_error_rc3": (
        f"out:{_INIT}\n"
        f'err:AGY_ERROR: {{"status":"code={S}","code":"{_LEAK[:60]}","retryable":true}}\n'
        "rc:3\n"
    ),
    "mode_and_timeout_notices": (
        f'err:warning: unrecognized --mode value "{_LEAK}"\n'
        f"err:[agy] print timeout after {_LEAK}\nsleep:0.3\n"
        f"out:{_INIT}\nout:{_result('SUCCESS', 'null')}\n"
    ),
    "result_error_text": f"out:{_INIT}\nout:{_result('ERROR', chr(34) + 'go to ' + _LEAK + chr(34))}\nrc:1\n",
    "result_error_other_status": f"out:{_INIT}\nout:{_result('CANCELED', chr(34) + _LEAK + chr(34))}\n",
    "result_error_structured": (
        f"out:{_INIT}\n"
        "out:"
        + _result("ERROR", '{"message":"x ' + _LEAK + '","detail":"' + _LEAK + '"}')
        + "\nrc:1\n"
    ),
    "result_error_unknown_shape": (
        f"out:{_INIT}\nout:" + _result("ERROR", '["' + _LEAK + '"]') + "\nrc:1\n"
    ),
    "open_tool_row_settled_with_error": (
        f"out:{_INIT}\n"
        'out:{"event":"step_update","step_update":{"conversation_id":'
        '"90ca6744-e8d4-46d4-b5fc-64a30ae2fa6d","step_index":1,"state":"ACTIVE",'
        '"step_type":"tool","tool_name":"wait","tool_info":{"name":"wait","parameters":{}}}}\n'
        f"out:{_result('ERROR', chr(34) + _LEAK + chr(34))}\nrc:1\n"
    ),
}


def _strings(obj: Any) -> Iterator[str]:
    yield repr(obj)


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
@pytest.mark.anyio
async def test_no_surface_leaks_the_canary(
    scenario: str, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every stderr / result.error / AGY_ERROR channel, end to end: the
    canary reaches no event (Telegram) and no INFO+ log."""
    monkeypatch.setattr(agy, "_probe_agy_version", agy._run_agy_version)
    monkeypatch.setenv("UNTETHER_FAKE_AGY_TIME_SCALE", "1")
    path = tmp_path / "s.script"
    path.write_text(_SCENARIOS[scenario])
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCRIPT", str(path))
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    with structlog.testing.capture_logs() as logs:
        events = [evt async for evt in runner.run("hi", None)]
    assert isinstance(events[-1], CompletedEvent)
    for event in events:
        for text in _strings(event):
            assert S not in text and "cid123" not in text, text
    for entry in logs:
        if entry.get("log_level") == "debug":
            continue  # raw stderr pipeline log, DEBUG only (REVIEW m3)
        assert S not in repr(entry) and "cid123" not in repr(entry), entry
