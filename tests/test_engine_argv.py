"""#1034: a user-influenced model or resume value can never add a flag to an
engine CLI's argv (Claude, Codex, OpenCode, Pi; Gemini and AMP share the base
refusal). Antigravity has its own suite: ``tests/test_antigravity_argv.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import structlog

from tests.telegram_fakes import FakeTransport, make_cfg
from untether.config import ConfigError
from untether.model import CompletedEvent, ResumeToken
from untether.runner import PRESPAWN_BLOCKED_KEY, JsonlSubprocessRunner
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.telegram.chat_prefs import ChatPrefsStore
from untether.telegram.commands.model import _handle_model_command
from untether.telegram.types import TelegramIncomingMessage
from untether.utils import engine_argv
from untether.utils.engine_argv import EngineArgvError

ENGINES = ["claude", "codex", "opencode", "pi", "gemini"]
# AMP is deprecated and its build_args is left alone (#722); the base
# refusal still covers it before anything spawns.
REFUSAL_ENGINES = [*ENGINES, "amp"]

# Each would have been parsed as a flag by at least one CLI, or smuggles
# whitespace / control characters into argv and logs.
HOSTILE = [
    "--version",
    "--dangerously-skip-permissions",
    "--dangerously-bypass-approvals-and-sandbox",
    "-p",
    "-",
    "--",
]
HOSTILE_MODELS = [*HOSTILE, "a b", "a\tb", "a\nb", "a\x00b", "a\x1bb", "x" * 257]
HOSTILE_RESUMES = [*HOSTILE, "a\nb", "a\x00b", "a\x1bb", "x" * 4097]

VALID_MODELS = {
    "claude": [
        "opus",
        "sonnet[1m]",
        "claude-opus-5-5",
        "claude-opus-5-5[1m]",
        "us.anthropic.claude-sonnet-5-v1:0",
        "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-opus-5-5-v1:0",
    ],
    "codex": ["gpt-6.1-sol", "o3", "codex-mini-latest"],
    "opencode": [
        "anthropic/claude-sonnet-5",
        "openrouter/meta-llama/llama-3.1-8b-instruct:free",
        "cloudflare-workers-ai/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        "ollama/qwen2.5-coder:32b",
    ],
    "pi": ["openai/gpt-4o", "gemini-3.8-flash:high", "sonnet"],
    "gemini": ["gemini-3.8-pro"],
}
VALID_RESUMES = {
    "claude": "0c1f6a52-6c1e-4b8e-9a5d-1f2e3d4c5b6a",
    "codex": "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b",
    "opencode": "ses_AbC123xyz",
    # What Untether saves after a Pi run: the session id's first segment
    # (#1064 — a session *path* must be under Pi's sessions directory).
    "pi": "019e81c8",
    "gemini": "abc123_DEF-456",
}


def _runner(engine: str, *, model: str | None = None) -> Any:
    if engine == "claude":
        from untether.runners.claude import ClaudeRunner

        return ClaudeRunner(claude_cmd="claude", model=model)
    if engine == "codex":
        from untether.runners.codex import CodexRunner

        return CodexRunner(codex_cmd="codex", extra_args=[])
    if engine == "opencode":
        from untether.runners.opencode import OpenCodeRunner

        return OpenCodeRunner(model=model)
    if engine == "pi":
        from untether.runners.pi import PiRunner

        return PiRunner(extra_args=[], model=model, provider=None)
    if engine == "gemini":
        from untether.runners.gemini import GeminiRunner

        return GeminiRunner(model=model)
    if engine == "amp":
        from untether.runners.amp import AmpRunner

        return AmpRunner()
    raise AssertionError(engine)


def _args(
    engine: str,
    *,
    model: str | None = None,
    config_model: str | None = None,
    resume: str | None = None,
) -> list[str]:
    runner = _runner(engine, model=config_model)
    token = ResumeToken(engine=engine, value=resume) if resume is not None else None
    options = EngineRunOptions(model=model) if model is not None else None
    with apply_run_options(options):
        return runner.build_args("hello", token, state=runner.new_state("hello", token))


# ── the shared checks ───────────────────────────────────────────────────────


@pytest.mark.parametrize("value", HOSTILE_MODELS + ["", None, 7])
def test_check_model_refuses(value: Any) -> None:
    with pytest.raises(EngineArgvError) as err:
        engine_argv.check_model(value)
    assert err.value.field == "model"
    # The raw value never rides along in the exception text.
    if isinstance(value, str) and len(value) > 2:
        assert value not in str(err.value)
    assert engine_argv.model_problem(value)


@pytest.mark.parametrize(
    "value", sorted({m for models in VALID_MODELS.values() for m in models})
)
def test_check_model_accepts_real_ids(value: str) -> None:
    assert engine_argv.check_model(value) == value
    assert engine_argv.model_problem(value) is None


@pytest.mark.parametrize("value", HOSTILE_RESUMES + ["", None])
def test_check_resume_refuses(value: Any) -> None:
    with pytest.raises(EngineArgvError) as err:
        engine_argv.check_resume(value)
    assert err.value.field == "resume"


@pytest.mark.parametrize("value", sorted(VALID_RESUMES.values()))
def test_check_resume_accepts_real_ids(value: str) -> None:
    assert engine_argv.check_resume(value) == value


def test_refusal_texts_are_plain_and_actionable() -> None:
    assert "/model" in engine_argv.INVALID_MODEL_TEXT
    # #1069: the refusal keeps the chat's saved session, so the text says
    # so rather than sending the user to /new.
    assert "/new" not in engine_argv.INVALID_RESUME_TEXT
    assert "saved session isn't affected" in engine_argv.INVALID_RESUME_TEXT


# ── build_args: no value becomes a flag ─────────────────────────────────────


@pytest.mark.parametrize("via", ["chat_model", "config_model"])
@pytest.mark.parametrize("hostile", HOSTILE_MODELS)
@pytest.mark.parametrize("engine", ENGINES)
def test_hostile_model_never_reaches_argv(engine: str, via: str, hostile: str) -> None:
    if engine == "codex" and via == "config_model":
        pytest.skip("codex has no config model in argv")
    kwargs = {"model": hostile} if via == "chat_model" else {"config_model": hostile}
    with pytest.raises(EngineArgvError) as err:
        _args(engine, **kwargs)
    assert err.value.field == "model"


@pytest.mark.parametrize("hostile", HOSTILE_RESUMES)
@pytest.mark.parametrize("engine", ENGINES)
def test_hostile_resume_never_reaches_argv(engine: str, hostile: str) -> None:
    with pytest.raises(EngineArgvError) as err:
        _args(engine, resume=hostile)
    assert err.value.field == "resume"


@pytest.mark.parametrize(
    ("engine", "model"),
    [(engine, model) for engine, models in VALID_MODELS.items() for model in models],
)
def test_valid_model_and_resume_still_pass(engine: str, model: str) -> None:
    resume = VALID_RESUMES[engine]
    args = _args(engine, model=model, resume=resume)
    if engine == "pi":
        # Pi's own parser has no ``--flag=value`` form for these two flags;
        # it takes the next token as the value unconditionally.
        assert args[args.index("--model") + 1] == model
        assert args[args.index("--session") + 1] == resume
    elif engine == "codex":
        assert f"--model={model}" in args
        # ``exec resume <id> -`` is positional.
        assert args[args.index("resume") + 1 :] == [resume, "-"]
    elif engine == "claude":
        assert f"--model={model}" in args
        assert f"--resume={resume}" in args
    elif engine == "opencode":
        assert f"--model={model}" in args
        assert f"--session={resume}" in args
    else:  # gemini: deprecated, validated only (#722)
        assert args[args.index("--model") + 1] == model
        assert args[args.index("--resume") + 1] == resume


@pytest.mark.parametrize("engine", ["claude", "codex", "opencode"])
def test_joined_form_leaves_no_bare_value_flag(engine: str) -> None:
    model = VALID_MODELS[engine][0]
    args = _args(engine, model=model, resume=VALID_RESUMES[engine])
    assert "--model" not in args
    assert "--resume" not in args
    assert "--session" not in args
    assert model not in args


@pytest.mark.parametrize("engine", ENGINES)
def test_continue_carries_no_value(engine: str) -> None:
    runner = _runner(engine)
    token = ResumeToken(engine=engine, value="", is_continue=True)
    args = runner.build_args("hello", token, state=runner.new_state("hello", token))
    assert not any(a.startswith(("--resume=", "--session=")) for a in args)


# ── #998: after `/model clear`, a resumed run names the configured default ──


@pytest.mark.parametrize("engine", ["claude", "opencode", "pi"])
def test_resumed_run_passes_the_configured_model(engine: str) -> None:
    """With the chat override gone (or emptied), `[<engine>] model` is passed
    on a resumed run too — the engine would otherwise keep the session's
    model (Claude 2.1.296 and Pi 0.78.0 do)."""
    model = VALID_MODELS[engine][0]
    for override in (None, ""):
        args = _args(
            engine, model=override, config_model=model, resume=VALID_RESUMES[engine]
        )
        if engine == "pi":
            assert args[args.index("--model") + 1] == model
            assert "--session" in args
        else:
            assert f"--model={model}" in args
            assert any(a.startswith(("--resume=", "--session=")) for a in args)


@pytest.mark.parametrize("engine", ["claude", "codex", "opencode", "pi"])
def test_resumed_run_without_any_model_passes_none(engine: str) -> None:
    """Nothing configured: Untether has no model to name, so the engine
    decides (the `/model clear` reply then points at `/new`). Codex has no
    `[codex] model` key at all — `codex exec resume` uses its own config."""
    args = _args(engine, resume=VALID_RESUMES[engine])
    assert not any(a == "--model" or a.startswith("--model=") for a in args)


# ── the run is refused before anything spawns ───────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize("via", ["chat_model", "resume"])
@pytest.mark.parametrize("hostile", ["--version", "--dangerously-skip-permissions"])
@pytest.mark.parametrize("engine", REFUSAL_ENGINES)
async def test_run_refused_before_spawn(
    monkeypatch: pytest.MonkeyPatch, engine: str, via: str, hostile: str
) -> None:
    runner = _runner(engine)
    cls = type(runner)
    spawned: list[str] = []

    def _command(self: Any) -> str:
        spawned.append("command")
        return "/nonexistent/untether-test-engine"

    monkeypatch.setattr(cls, "command", _command)
    monkeypatch.setattr(
        JsonlSubprocessRunner, "_check_prespawn_ram_guard", lambda self, resume: None
    )
    if engine == "opencode":

        async def _supported(self: Any, resume: Any) -> None:
            return None

        monkeypatch.setattr(cls, "_unsupported_version_event", _supported)

    options = EngineRunOptions(model=hostile) if via == "chat_model" else None
    resume = ResumeToken(engine=engine, value=hostile) if via == "resume" else None
    with apply_run_options(options), structlog.testing.capture_logs() as logs:
        events = [evt async for evt in runner.run("Reply OK", resume)]

    assert spawned == [], "the engine must not be spawned"
    (done,) = events
    assert isinstance(done, CompletedEvent) and done.ok is False
    assert done.usage == {PRESPAWN_BLOCKED_KEY: engine_argv.INVALID_ARGUMENT_BLOCK}
    assert done.resume is None
    assert done.error == (
        engine_argv.INVALID_RESUME_TEXT
        if via == "resume"
        else engine_argv.INVALID_MODEL_TEXT
    )
    assert hostile not in (done.error or "")
    (line,) = [e for e in logs if e["event"] == "runner.argv.invalid_value"]
    assert line["field"] == ("resume" if via == "resume" else "model")
    assert line["engine"] == engine and line["log_level"] == "warning"
    assert hostile not in json.dumps(line)
    assert "runner.start" not in [e["event"] for e in logs]


# ── /model set ──────────────────────────────────────────────────────────────


def _model_msg(text: str) -> TelegramIncomingMessage:
    return TelegramIncomingMessage(
        transport="telegram",
        chat_id=123,
        message_id=10,
        text=text,
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=456,
        chat_type="private",
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "hostile", ["--dangerously-skip-permissions", "-p", "a\x1bb", "x" * 257]
)
async def test_model_set_refuses_a_hostile_value(tmp_path: Path, hostile: str) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    chat_prefs = ChatPrefsStore(tmp_path / "telegram_chat_prefs_state.json")
    await _handle_model_command(
        cfg,
        _model_msg(f"/model set codex {hostile}"),
        f"set codex {hostile}",
        ambient_context=None,
        topic_store=None,
        chat_prefs=chat_prefs,
    )
    assert await chat_prefs.get_engine_override(123, "codex") is None
    text = transport.send_calls[-1]["message"].text
    assert "model" in text and "override set" not in text
    assert hostile not in text


@pytest.mark.anyio
@pytest.mark.parametrize("model", ["claude-opus-5-5[1m]", "anthropic/claude-sonnet-5"])
async def test_model_set_still_accepts_real_ids(tmp_path: Path, model: str) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    chat_prefs = ChatPrefsStore(tmp_path / "telegram_chat_prefs_state.json")
    await _handle_model_command(
        cfg,
        _model_msg(f"/model set codex {model}"),
        f"set codex {model}",
        ambient_context=None,
        topic_store=None,
        chat_prefs=chat_prefs,
    )
    override = await chat_prefs.get_engine_override(123, "codex")
    assert override is not None and override.model == model


# ── config load ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("hostile", ["--dangerously-skip-permissions", "a b", "-p"])
# Codex reads no ``[codex] model`` (its model comes from /model or the CLI's
# own config), so there is nothing to validate at load for it.
@pytest.mark.parametrize("engine", ["claude", "opencode", "pi"])
def test_config_load_refuses_a_hostile_model(
    tmp_path: Path, engine: str, hostile: str
) -> None:
    import importlib

    module = importlib.import_module(f"untether.runners.{engine}")
    path = tmp_path / "untether.toml"
    with pytest.raises(ConfigError) as err:
        module.build_runner({"model": hostile}, path)
    assert f"{engine}.model" in str(err.value)
    assert hostile not in str(err.value)


@pytest.mark.parametrize(
    ("engine", "model"),
    [
        ("claude", "claude-opus-5-5[1m]"),
        ("opencode", "anthropic/claude-sonnet-5"),
        ("pi", "openai/gpt-4o"),
    ],
)
def test_config_load_accepts_real_ids(tmp_path: Path, engine: str, model: str) -> None:
    import importlib

    module = importlib.import_module(f"untether.runners.{engine}")
    runner = module.build_runner({"model": model}, tmp_path / "untether.toml")
    assert runner.model == model


# ── #1064 Pi / #1065 Codex: only values the CLI treats as a session ─────────

_PI_UUID = "019e81c8-5c5c-76d3-afa8-ca92a049c4d6"


@pytest.fixture
def pi_agent_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    agent = tmp_path / "pi-agent"
    (agent / "sessions" / "--home-me-proj--").mkdir(parents=True)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    return agent


def _pi_refused(value: str) -> None:
    from untether.runners.pi import check_pi_resume

    with pytest.raises(EngineArgvError) as err:
        check_pi_resume(value)
    assert err.value.field == "resume"
    assert value not in str(err.value)


@pytest.mark.parametrize(
    "value",
    [
        _PI_UUID,
        "019e81c8",  # Untether's saved short id (``_short_session_id``)
        "ccd569e0",
        "my-session_1.2",  # Pi's own grammar (``--session-id``)
    ],
)
def test_pi_accepts_session_ids(pi_agent_dir: Path, value: str) -> None:
    from untether.runners.pi import check_pi_resume

    assert check_pi_resume(value) == value


def test_pi_accepts_session_files_under_its_sessions_dir(pi_agent_dir: Path) -> None:
    from untether.runners.pi import check_pi_resume

    inside = pi_agent_dir / "sessions" / "--home-me-proj--" / f"2026_{_PI_UUID}.jsonl"
    assert check_pi_resume(str(inside)) == str(inside)
    # Not created yet (a new run's path) is fine too, and so is a space.
    spaced = pi_agent_dir / "sessions" / "--home-me-My Projects--" / "s.jsonl"
    assert check_pi_resume(str(spaced)) == str(spaced)


def test_pi_accepts_a_tilde_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from untether.runners.pi import check_pi_resume

    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    value = "~/.pi/agent/sessions/--home-me-proj--/s.jsonl"
    assert check_pi_resume(value) == value
    _pi_refused("~/.pi/agent/s.jsonl")


def test_pi_accepts_its_session_dir_env(
    tmp_path: Path, pi_agent_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from untether.runners.pi import check_pi_resume

    other = tmp_path / "pi-sessions"
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(other))
    value = str(other / "--p--" / "s.jsonl")
    assert check_pi_resume(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "/tmp/qa/qa-pi-escape.jsonl",  # the #1064 repro
        "https://example.invalid/cb?token=abc123&state=xyz",  # the other one
        "http:/example.invalid/x.jsonl",
        "qa-pi-escape.jsonl",  # relative: Pi resolves it against the cwd
        "notes/escape.jsonl",
        "../escape.jsonl",
        "a b",
        "id?x",
        "id:x",
        ".hidden",
        "trailing-",
        "~",
        "~/escape.jsonl",
        "a\\b",
    ],
)
def test_pi_refuses_anything_else(pi_agent_dir: Path, value: str) -> None:
    _pi_refused(value)


def test_pi_refuses_paths_that_escape_its_sessions_dir(
    tmp_path: Path, pi_agent_dir: Path
) -> None:
    sessions = pi_agent_dir / "sessions"
    _pi_refused(str(sessions / ".." / "escape.jsonl"))
    _pi_refused(str(sessions / "--p--" / ".." / ".." / "escape.jsonl"))
    _pi_refused(str(sessions / "--p--" / "notes.txt"))  # not a session file
    _pi_refused(str(pi_agent_dir / "auth.jsonl"))
    _pi_refused(str(tmp_path / "pi-agent-sessions" / "x.jsonl"))  # prefix twin
    outside = tmp_path / "outside"
    outside.mkdir()
    (sessions / "link").symlink_to(outside, target_is_directory=True)
    _pi_refused(str(sessions / "link" / "x.jsonl"))


def test_pi_new_run_path_and_saved_id_still_resume(
    tmp_path: Path, pi_agent_dir: Path
) -> None:
    """The path Untether hands a new Pi run, and the id it saves from the run
    (what a later message resumes), both pass the narrowed check."""
    from untether.runners.pi import PiRunner, _short_session_id, check_pi_resume
    from untether.utils.paths import reset_run_base_dir, set_run_base_dir

    runner = PiRunner(extra_args=[], model=None, provider=None)
    token = set_run_base_dir(tmp_path / "proj")
    try:
        state = runner.new_state("hello", None)
        args = runner.build_args("hello", None, state=state)
    finally:
        reset_run_base_dir(token)
    new_path = args[args.index("--session") + 1]
    assert new_path.startswith(str(pi_agent_dir / "sessions"))
    saved = _short_session_id(_PI_UUID)
    assert check_pi_resume(saved) == saved
    resumed = ResumeToken(engine="pi", value=saved)
    args = runner.build_args("again", resumed, state=runner.new_state("again", resumed))
    assert args[args.index("--session") + 1] == saved


def test_pi_continue_is_untouched(pi_agent_dir: Path) -> None:
    from untether.runners.pi import PiRunner

    runner = PiRunner(extra_args=[], model=None, provider=None)
    token = ResumeToken(engine="pi", value="", is_continue=True)
    args = runner.build_args("hi", token, state=runner.new_state("hi", token))
    assert "--continue" in args and "--session" not in args


@pytest.mark.parametrize(
    "value",
    ["019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b", "019A2B3C-4D5E-7F60-8A9B-0C1D2E3F4A5B"],
)
def test_codex_accepts_thread_ids(value: str) -> None:
    runner = _runner("codex")
    assert runner.check_resume_value(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "https://example.invalid/cb?token=X&state=Y",  # the #1065 repro
        "my-thread-name",  # a name: unknown → Codex silently starts a new thread
        "019a2b3c",
        "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b-extra",
        "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5g",
    ],
)
def test_codex_refuses_non_thread_ids(value: str) -> None:
    runner = _runner("codex")
    with pytest.raises(EngineArgvError) as err:
        runner.check_resume_value(value)
    assert value not in str(err.value)
    with pytest.raises(EngineArgvError):
        _args("codex", resume=value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("engine", "value"),
    [
        ("pi", "/tmp/qa/qa-pi-escape.jsonl"),
        ("pi", "https://example.invalid/cb?token=abc123"),
        ("codex", "https://example.invalid/cb?token=X&state=Y"),
        ("codex", "my-thread-name"),
    ],
)
async def test_narrowed_resume_refused_before_spawn(
    monkeypatch: pytest.MonkeyPatch, pi_agent_dir: Path, engine: str, value: str
) -> None:
    runner = _runner(engine)
    spawned: list[str] = []

    def _command(self: Any) -> str:
        spawned.append("command")
        return "/nonexistent/untether-test-engine"

    monkeypatch.setattr(type(runner), "command", _command)
    monkeypatch.setattr(
        JsonlSubprocessRunner, "_check_prespawn_ram_guard", lambda self, resume: None
    )
    resume = ResumeToken(engine=engine, value=value)
    with structlog.testing.capture_logs() as logs:
        events = [evt async for evt in runner.run("Reply OK", resume)]

    assert spawned == []
    (done,) = events
    assert isinstance(done, CompletedEvent) and done.ok is False
    assert done.error == engine_argv.INVALID_RESUME_TEXT
    assert done.usage == {PRESPAWN_BLOCKED_KEY: engine_argv.INVALID_ARGUMENT_BLOCK}
    assert done.resume is None
    (line,) = [e for e in logs if e["event"] == "runner.argv.invalid_value"]
    assert line["reason"] == "not_a_session"
    # Only the inert, cut preview (#1034) — never a URL or a path.
    assert line["preview"] == engine_argv.safe_preview(value)
    assert "/" not in line["preview"]
