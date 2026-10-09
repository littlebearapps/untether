"""Antigravity argv safety (#558): no user-influenced value can become an
agy flag.

agy 1.3.2 parses a dash-leading token after ``--model`` as a flag
(``--model --version`` prints the version) while ``--model=--version`` is
read as a value. So every value that reaches agy's argv goes through one
validator (``utils/antigravity_argv.py``) and is emitted joined
(``--flag=value``); an invalid one refuses the run before anything spawns.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog.testing

from untether.config import ConfigError
from untether.model import CompletedEvent, ResumeToken
from untether.runner import PRESPAWN_BLOCKED_KEY
from untether.runners import antigravity as agy
from untether.runners.antigravity import ENGINE, AntigravityRunner
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.utils import antigravity_argv as argv_mod
from untether.utils import antigravity_quota as quota
from untether.utils.antigravity_argv import AgyArgvError, agy_flag
from untether.utils.paths import reset_run_base_dir, set_run_base_dir

FAKE_AGY = Path(__file__).parent / "fake_clis" / "fake_agy.py"
FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"

HOSTILE = [
    agy.BYPASS_FLAG,
    "--mode=plan",
    "--mode",
    "--continue",
    "--project=/etc",
    "-p",
    "-",
    "",
    " ",
    "gemini 3.8 flash",
    " gemini-3.8-flash",
    "gemini-3.8-flash ",
    "a\nb",
    "a\rb",
    "a\tb",
    "a\x00b",
    "a\x1b[31mb",
    "a b",  # no-break space
    "a b",
    "—model",  # em dash
    "x=--continue y",
    "x" * 129,
    "a;b",
    "a$(id)",
    "a'b",
    'a"b',
]
# Flags Untether itself may add; nothing else dash-leading may appear.
_OWN_FLAGS = {
    "--input-format",
    "--output-format",
    "--print-timeout",
    "--disable-slash-commands",
    "--continue",
    agy.BYPASS_FLAG,
}
_VALUE_FLAGS = ("--model", "--conversation", "--effort")


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    proj = tmp_path / "proj"
    proj.mkdir()
    token = set_run_base_dir(proj)
    try:
        yield proj
    finally:
        reset_run_base_dir(token)


@pytest.fixture
def record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "record.json"
    monkeypatch.setenv("UNTETHER_FAKE_AGY_RECORD", str(path))
    return path


def _runner(**kwargs: Any) -> AntigravityRunner:
    kwargs.setdefault("antigravity_cmd", "agy")
    return AntigravityRunner(**kwargs)


def _assert_no_injected_flag(args: list[str], hostile: str) -> None:
    for token in args:
        if not token.startswith("-"):
            continue
        name, sep, _ = token.partition("=")
        if sep:
            assert name in _VALUE_FLAGS, token
        else:
            assert token in _OWN_FLAGS, token
    assert hostile not in args or hostile in ("", "0")


# ── the validator ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", HOSTILE)
@pytest.mark.parametrize("flag", ["--model", "--conversation", "--effort"])
def test_agy_flag_rejects_hostile_values(flag: str, value: str) -> None:
    with pytest.raises(AgyArgvError) as err:
        agy_flag(flag, value)
    assert err.value.field == flag.removeprefix("--")
    # The error never carries the raw value.
    if len(value) > 3:
        assert value not in str(err.value)


@pytest.mark.parametrize("value", [None, 7, b"gemini", ["gemini"], {"a": 1}, True, 1.5])
def test_agy_flag_rejects_non_strings(value: Any) -> None:
    with pytest.raises(AgyArgvError):
        agy_flag("--model", value)


@pytest.mark.parametrize(
    "model",
    [
        "gemini-3.8-flash",
        "gemini-3.8-flash-high",
        "gemini-3.1-pro",
        "claude-opus-5-5",
        "claude-opus-5-5-medium",
        "gpt-oss-120b",
        "provider/model_name:tag@1+x",
        "x" * 128,
    ],
)
def test_agy_flag_accepts_real_model_ids(model: str) -> None:
    assert agy_flag("--model", model) == f"--model={model}"


def test_agy_flag_accepts_every_captured_conversation_id() -> None:
    ids: set[str] = set()
    for path in FIXTURES.glob("*.jsonl"):
        for line in path.read_text().splitlines():
            if not line.strip().startswith("{"):
                continue
            obj = json.loads(line)
            for holder in (obj, obj.get("step_update"), obj.get("result")):
                if isinstance(holder, dict) and holder.get("conversation_id"):
                    ids.add(holder["conversation_id"])
    assert len(ids) >= 10
    for cid in ids:
        assert agy_flag("--conversation", cid) == f"--conversation={cid}"


def test_agy_flag_conversation_is_stricter_than_model() -> None:
    for value in ("a/b", "a.b", "a:b", "a@b", "a+b"):
        assert agy_flag("--model", value)
        with pytest.raises(AgyArgvError):
            agy_flag("--conversation", value)


def test_agy_flag_effort_is_the_cli_vocabulary_only() -> None:
    for level in ("low", "medium", "high", "xhigh", "max"):
        assert agy_flag("--effort", level) == f"--effort={level}"
    for bad in ("minimal", "HIGH", "bogus", "high-1"):
        with pytest.raises(AgyArgvError):
            agy_flag("--effort", bad)


@pytest.mark.parametrize(
    "flag", ["--project", "--mode", agy.BYPASS_FLAG, "-p", "model", "--model=x", ""]
)
def test_agy_flag_refuses_flags_it_does_not_know(flag: str) -> None:
    with pytest.raises(AgyArgvError):
        agy_flag(flag, "value")


def test_safe_preview_is_short_and_inert() -> None:
    preview = argv_mod.safe_preview(
        "--mode=plan\n\x00\x1b[31m" + "https://x/?code=S3CR3T" * 9
    )
    assert len(preview) <= argv_mod.PREVIEW_CHARS + 1
    assert "\n" not in preview and "\x00" not in preview and "\x1b" not in preview
    assert "=" not in preview and "/" not in preview and ":" not in preview
    assert not preview.startswith("-")
    assert argv_mod.safe_preview("--continue") == "??continue"
    assert argv_mod.safe_preview("gemini-3.8-flash") == "gemini-3.8-flash"
    assert argv_mod.safe_preview(None) == "<NoneType>"


# ── build_args ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("hostile", HOSTILE)
@pytest.mark.parametrize("via", ["chat_model", "toml_model", "resume"])
def test_build_args_never_lets_a_value_add_a_flag(via: str, hostile: str) -> None:
    options = EngineRunOptions(model=hostile) if via == "chat_model" else None
    runner = _runner(model=hostile if via == "toml_model" else None)
    resume = ResumeToken(engine=ENGINE, value=hostile) if via == "resume" else None
    if hostile == "" and via != "resume":
        # No model set at all: a plain run, and no --model.
        with apply_run_options(options):
            args = runner.build_args("p", None, state=runner.new_state("p", None))
        assert not any(a.startswith("--model") for a in args)
        _assert_no_injected_flag(args, hostile)
        return
    with apply_run_options(options):
        state = runner.new_state("p", resume)
        with pytest.raises(AgyArgvError):
            runner.build_args("p", resume, state=state)


@pytest.mark.parametrize("hostile", HOSTILE)
def test_build_args_hostile_effort_is_never_emitted(hostile: str) -> None:
    runner = _runner()
    with apply_run_options(EngineRunOptions(reasoning=hostile, model="m")):
        args = runner.build_args("p", None, state=runner.new_state("p", None))
    assert not any(a.startswith("--effort") for a in args)
    _assert_no_injected_flag(args, hostile)


def test_build_args_never_emits_the_two_token_form() -> None:
    runner = _runner(model="cfg-model")
    cid = "90ca6744-e8d4-46d4-b5fc-64a30ae2fa6d"
    resume = ResumeToken(engine=ENGINE, value=cid)
    options = EngineRunOptions(
        model="gemini-3.8-flash", reasoning="high", permission_mode="full"
    )
    with apply_run_options(options):
        args = runner.build_args("p", resume, state=runner.new_state("p", resume))
    for flag in _VALUE_FLAGS:
        assert flag not in args, f"{flag} emitted as its own token"
    assert f"--conversation={cid}" in args
    assert "--model=gemini-3.8-flash" in args
    assert "--effort=high" in args
    # The values never appear as tokens of their own either.
    for value in (cid, "gemini-3.8-flash", "high"):
        assert value not in args
    _assert_no_injected_flag(args, "")
    # /continue still has no value at all.
    cont = ResumeToken(engine=ENGINE, value="", is_continue=True)
    args = runner.build_args("p", cont, state=runner.new_state("p", cont))
    assert "--continue" in args
    assert not any(a.startswith("--conversation") for a in args)


# ── the run is refused before spawn ─────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    "hostile",
    [agy.BYPASS_FLAG, "--mode=plan", "--continue", "-p", "a b", "a\nb", "a\x00b"],
)
@pytest.mark.parametrize("via", ["chat_model", "toml_model", "resume"])
async def test_run_refused_before_spawn_for_an_invalid_value(
    project: Path,
    record: Path,
    monkeypatch: pytest.MonkeyPatch,
    via: str,
    hostile: str,
) -> None:
    assert os.access(FAKE_AGY, os.X_OK)
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    options = EngineRunOptions(model=hostile) if via == "chat_model" else None
    runner = AntigravityRunner(
        antigravity_cmd=str(FAKE_AGY), model=hostile if via == "toml_model" else None
    )
    resume = ResumeToken(engine=ENGINE, value=hostile) if via == "resume" else None
    with apply_run_options(options), structlog.testing.capture_logs() as logs:
        events = [evt async for evt in runner.run("Reply OK", resume)]
    assert not record.exists(), "agy must not be spawned"
    (done,) = events
    assert isinstance(done, CompletedEvent) and done.ok is False
    assert done.usage == {PRESPAWN_BLOCKED_KEY: agy.INVALID_ARGUMENT_BLOCK}
    field = "conversation" if via == "resume" else "model"
    expected = (
        agy.INVALID_CONVERSATION_TEXT if via == "resume" else agy.INVALID_MODEL_TEXT
    )
    assert done.error == expected
    assert hostile not in (done.error or "")
    (line,) = [e for e in logs if e["event"] == "antigravity.argv.invalid_value"]
    assert line["field"] == field and line["log_level"] == "warning"
    assert hostile not in json.dumps(line) or len(hostile) <= 2
    assert "runner.start" not in [e["event"] for e in logs]
    assert "subprocess.spawn" not in [e["event"] for e in logs]


def test_refusal_texts_are_plain_and_short() -> None:
    for text in (agy.INVALID_MODEL_TEXT, agy.INVALID_CONVERSATION_TEXT):
        assert text.startswith("🛑 ") and len(text) < 320
        assert "<" not in text and "`" not in text
    assert "/model" in agy.INVALID_MODEL_TEXT
    assert "/new" in agy.INVALID_CONVERSATION_TEXT


@pytest.mark.anyio
async def test_valid_values_still_run_and_reach_agy_joined(
    project: Path, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "resume_after_interrupt")
    cid = next(
        json.loads(line)["conversation_id"]
        for line in (FIXTURES / "resume_after_interrupt.jsonl").read_text().splitlines()
        if '"event":"init"' in line
    )
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    resume = ResumeToken(engine=ENGINE, value=cid)
    with apply_run_options(EngineRunOptions(model="gemini-3.8-flash")):
        events = [evt async for evt in runner.run("go on", resume)]
    assert isinstance(events[-1], CompletedEvent) and events[-1].ok is True
    argv = json.loads(record.read_text())["argv"]
    assert f"--conversation={cid}" in argv and "--model=gemini-3.8-flash" in argv
    assert "--conversation" not in argv and "--model" not in argv


def test_build_runner_rejects_an_invalid_toml_model(tmp_path: Path) -> None:
    for bad in (agy.BYPASS_FLAG, "gemini 3.8 flash", "a\nb", ""):
        with pytest.raises(ConfigError, match=r"antigravity\.model"):
            agy.build_runner({"model": bad}, tmp_path / "untether.toml")
    runner = agy.build_runner({"model": "gemini-3.8-flash"}, tmp_path / "untether.toml")
    assert runner.model == "gemini-3.8-flash"  # type: ignore[attr-defined]


# ── the slash probes ────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize("hostile", [h for h in HOSTILE if h])
async def test_effort_probe_never_spawns_for_an_invalid_model(
    record: Path, monkeypatch: pytest.MonkeyPatch, hostile: str
) -> None:
    monkeypatch.setattr(quota, "_probe_model_efforts", quota._run_model_efforts)
    monkeypatch.setenv(
        "UNTETHER_FAKE_AGY_SLASH_DATA", json.dumps({"available": ["low"]})
    )
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    with structlog.testing.capture_logs() as logs:
        assert await quota.agy_model_efforts(runner, hostile) is None
    assert not record.exists(), "agy must not be spawned"
    (failed,) = [e for e in logs if e["event"] == "antigravity.effort.probe_failed"]
    assert failed["kind"] == "invalid_model"
    assert hostile not in json.dumps(failed) or len(hostile) <= 2
    assert quota.peek_model_efforts(runner.command(), hostile) is None


@pytest.mark.anyio
@pytest.mark.parametrize("hostile", HOSTILE)
async def test_run_agy_slash_flags_are_validated_before_spawn(
    record: Path, hostile: str
) -> None:
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    with pytest.raises(quota.AgySlashError) as err:
        await quota.run_agy_slash(runner, "/effort", flags=(("--model", hostile),))
    assert err.value.kind == "invalid_argument"
    assert not record.exists()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "command",
    [
        "--version",
        "/usage --model x",
        "usage",
        "/",
        "",
        "/usage\n",
        "/../x",
        "-p",
        "/Usage",
    ],
)
async def test_run_agy_slash_command_is_a_plain_slash_word(
    record: Path, command: str
) -> None:
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    with pytest.raises(quota.AgySlashError) as err:
        await quota.run_agy_slash(runner, command)
    assert err.value.kind == "invalid_argument"
    assert not record.exists()


@pytest.mark.anyio
async def test_run_agy_slash_takes_no_free_form_arguments(record: Path) -> None:
    """The old ``extra_args`` tuple is gone: only named, validated flags."""
    import inspect

    params = inspect.signature(quota.run_agy_slash).parameters
    assert "extra_args" not in params
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    with pytest.raises(quota.AgySlashError):
        await quota.run_agy_slash(runner, "/effort", flags=(("--project", "x"),))
    assert not record.exists()


@pytest.mark.anyio
async def test_slash_probe_argv_is_joined(
    record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "UNTETHER_FAKE_AGY_SLASH_DATA", json.dumps({"available": ["low"]})
    )
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    await quota.run_agy_slash(runner, "/effort", flags=(("--model", "gemini-3.1-pro"),))
    assert json.loads(record.read_text())["argv"] == [
        "-p",
        "/effort",
        "--output-format",
        "stream-json",
        "--model=gemini-3.1-pro",
    ]


def test_every_agy_argv_site_goes_through_the_validator() -> None:
    """I-4 sweep, kept honest: outside ``agy_flag`` no source line builds a
    value-taking agy flag by hand."""
    import re

    src = Path(agy.__file__).parent.parent
    offenders: list[str] = []
    pattern = re.compile(r"""["']--(model|conversation|effort|project|mode)["=]""")
    for path in [*src.rglob("antigravity*.py"), *src.rglob("_antigravity*.py")]:
        for number, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            # ``flags=((flag, value),)`` is run_agy_slash's validated input.
            if stripped.startswith("#") or "agy_flag(" in line or "flags=((" in line:
                continue
            if pattern.search(line) and path.name != "antigravity_argv.py":
                offenders.append(f"{path.name}:{number}: {stripped}")
    assert offenders == []
