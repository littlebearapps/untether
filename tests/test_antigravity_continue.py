"""Antigravity ``/continue`` resumes only this project's conversation (#1039).

agy's ``--continue`` also picks up a conversation from a parent or child
directory (probe P-L5, agy 1.3.3), so Untether never passes it. It looks the
run directory up in agy's ``cache/last_conversations.json`` and passes
``--conversation=<id>``; anything it can't read or trust means no run.
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
from typing import Any

import anyio
import pytest
import structlog.testing

from tests.telegram_fakes import FakeTransport
from untether.markdown import MarkdownPresenter
from untether.model import CompletedEvent, ResumeToken, StartedEvent
from untether.runner import PRESPAWN_BLOCKED_KEY
from untether.runner_bridge import (
    _RESUME_FAILURE_RE,
    ExecBridgeConfig,
    IncomingMessage,
    handle_message,
)
from untether.runners import antigravity as agy
from untether.runners.antigravity import AntigravityRunner
from untether.utils import antigravity_continue as cont
from untether.utils.antigravity_argv import AgyArgvError
from untether.utils.paths import reset_run_base_dir, set_run_base_dir

FAKE_AGY = Path(__file__).parent / "fake_clis" / "fake_agy.py"
ENGINE = "antigravity"
# The id the ``continue_workspace`` capture answers with.
CONV = "5bcf649b-0dfc-44b4-b691-2f3314724abf"
OTHER = "90ca6744-e8d4-46d4-b5fc-64a30ae2fa6d"
# Bound at import, before conftest points the lookup at a tmp path.
_REAL_PATH = cont.last_conversations_path


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "agy-home" / "cache" / "last_conversations.json"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(cont, "last_conversations_path", lambda: path)
    return path


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    """outer/proj/child plus a sibling whose name starts with ``proj``."""
    outer = tmp_path / "outer"
    dirs = {
        "outer": outer,
        "proj": outer / "proj",
        "child": outer / "proj" / "child",
        "sibling": outer / "proj2",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def _write(cache: Path, entries: Any) -> None:
    cache.write_text(json.dumps(entries), encoding="utf-8")


# -- the lookup --------------------------------------------------------------


def test_default_path_is_agys_cache_file() -> None:
    assert _REAL_PATH() == (
        Path.home() / ".gemini/antigravity-cli/cache/last_conversations.json"
    )
    # conftest points every test away from the host's real file.
    assert cont.last_conversations_path() != _REAL_PATH()
    assert not cont.last_conversations_path().exists()


def test_exact_path_is_found(cache: Path, tree: dict[str, Path]) -> None:
    _write(cache, {str(tree["proj"]): CONV, str(tree["sibling"]): OTHER})
    found = cont.lookup_conversation(tree["proj"])
    assert found.conversation_id == CONV
    assert found.reason == "found"


@pytest.mark.parametrize("where", ["outer", "child", "sibling"])
def test_parent_child_and_sibling_do_not_match(
    cache: Path, tree: dict[str, Path], where: str
) -> None:
    _write(cache, {str(tree["proj"]): CONV})
    found = cont.lookup_conversation(tree[where])
    assert found.conversation_id is None
    assert found.reason == "no_entry"


def test_trailing_slash_on_the_run_dir_still_matches(
    cache: Path, tree: dict[str, Path]
) -> None:
    _write(cache, {str(tree["proj"]): CONV})
    assert cont.lookup_conversation(Path(f"{tree['proj']}/")).conversation_id == CONV
    dotted = tree["child"] / ".."
    assert cont.lookup_conversation(dotted).conversation_id == CONV


@pytest.mark.parametrize("suffix", ["/", "/.", "/child/.."])
def test_keys_are_never_normalised(
    cache: Path, tree: dict[str, Path], suffix: str
) -> None:
    # agy writes plain absolute paths; a key in any other form isn't one of
    # its own, so it never matches.
    _write(cache, {f"{tree['proj']}{suffix}": CONV})
    assert cont.lookup_conversation(tree["proj"]).reason == "no_entry"


def test_case_is_never_folded(cache: Path, tree: dict[str, Path]) -> None:
    _write(cache, {str(tree["proj"]).replace("proj", "PROJ"): CONV})
    assert cont.lookup_conversation(tree["proj"]).reason == "no_entry"


def test_symlinked_run_dir_matches_its_real_path(
    cache: Path, tree: dict[str, Path], tmp_path: Path
) -> None:
    link = tmp_path / "link"
    link.symlink_to(tree["proj"], target_is_directory=True)
    _write(cache, {str(tree["proj"]): CONV})
    assert cont.lookup_conversation(link).conversation_id == CONV


def test_key_spelt_through_a_symlink_does_not_match(
    cache: Path, tree: dict[str, Path], tmp_path: Path
) -> None:
    # Fails closed: Untether's agy runs in the real directory.
    link = tmp_path / "link"
    link.symlink_to(tree["proj"], target_is_directory=True)
    _write(cache, {str(link): CONV})
    assert cont.lookup_conversation(link).reason == "no_entry"


def test_same_id_under_another_path_is_not_trusted(
    cache: Path, tree: dict[str, Path]
) -> None:
    # What an earlier bare `--continue` from the child left behind.
    _write(cache, {str(tree["proj"]): CONV, str(tree["child"]): CONV})
    for where in ("proj", "child"):
        found = cont.lookup_conversation(tree[where])
        assert found.conversation_id is None
        assert found.reason == "shared_id"
    assert cont.lookup_conversation(tree["outer"]).reason == "no_entry"


def test_missing_file_and_missing_folder(cache: Path, tree: dict[str, Path]) -> None:
    assert cont.lookup_conversation(tree["proj"]).reason == "no_cache"
    cache.parent.rmdir()
    assert cont.lookup_conversation(tree["proj"]).reason == "no_cache"
    cache.parent.write_text("a file where the folder should be")
    assert cont.lookup_conversation(tree["proj"]).reason == "no_cache"


def test_directory_in_place_of_the_file(cache: Path, tree: dict[str, Path]) -> None:
    cache.mkdir()
    assert cont.lookup_conversation(tree["proj"]).reason == "not_a_file"


def test_fifo_is_never_opened(cache: Path, tree: dict[str, Path]) -> None:
    os.mkfifo(cache)  # opening it for reading would block for ever
    assert cont.lookup_conversation(tree["proj"]).reason == "not_a_file"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_unreadable_file(cache: Path, tree: dict[str, Path]) -> None:
    _write(cache, {str(tree["proj"]): CONV})
    cache.chmod(0o000)
    try:
        assert cont.lookup_conversation(tree["proj"]).reason == "unreadable"
    finally:
        cache.chmod(0o600)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{",
        b"not json",
        b"\xff\xfe\x00garbage",
        b'{"a": "b"} trailing',
        b"[" * 100_000,
    ],
)
def test_not_json(cache: Path, tree: dict[str, Path], raw: bytes) -> None:
    cache.write_bytes(raw)
    found = cont.lookup_conversation(tree["proj"])
    assert found.conversation_id is None
    assert found.reason == "not_json"


@pytest.mark.parametrize("top", [[], ["x"], "text", 7, None, True])
def test_top_level_must_be_an_object(
    cache: Path, tree: dict[str, Path], top: Any
) -> None:
    _write(cache, top)
    assert cont.lookup_conversation(tree["proj"]).reason == "wrong_shape"


@pytest.mark.parametrize("bad", [None, 7, True, 1.5, ["x"], {"id": CONV}])
def test_non_string_values_anywhere(
    cache: Path, tree: dict[str, Path], bad: Any
) -> None:
    _write(cache, {str(tree["proj"]): bad})
    assert cont.lookup_conversation(tree["proj"]).reason == "wrong_shape"
    # A file that isn't the shape Untether knows isn't trusted for any row.
    _write(cache, {str(tree["proj"]): CONV, str(tree["sibling"]): bad})
    found = cont.lookup_conversation(tree["proj"])
    assert found.conversation_id is None
    assert found.reason == "wrong_shape"


def test_oversize_file(cache: Path, tree: dict[str, Path]) -> None:
    entries = {str(tree["proj"]): CONV, "/pad": "x" * cont.MAX_CACHE_BYTES}
    _write(cache, entries)
    found = cont.lookup_conversation(tree["proj"])
    assert found.conversation_id is None
    assert found.reason == "too_large"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "--dangerously-skip-permissions",
        "--continue",
        "-x",
        "id with space",
        "id\nnewline",
        "a" * 129,
        "../../etc/passwd",
    ],
)
def test_value_must_be_a_conversation_id(
    cache: Path, tree: dict[str, Path], value: str
) -> None:
    _write(cache, {str(tree["proj"]): value})
    found = cont.lookup_conversation(tree["proj"])
    assert found.conversation_id is None
    assert found.reason == "invalid_id"


def test_run_dir_that_cannot_be_a_path(cache: Path) -> None:
    _write(cache, {"/x": CONV})
    assert cont.lookup_conversation(Path("/x\x00y")).reason == "no_run_dir"


# -- the runner --------------------------------------------------------------


@pytest.fixture
def record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "record.json"
    monkeypatch.setenv("UNTETHER_FAKE_AGY_RECORD", str(path))
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "continue_workspace")
    return path


async def _continue(base: Path) -> list[Any]:
    """``/continue`` with *base* as the run directory (set in the test's own
    context, as the bridge does)."""
    assert os.access(FAKE_AGY, os.X_OK), f"chmod +x {FAKE_AGY}"
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    resume = ResumeToken(engine=ENGINE, value="", is_continue=True)
    token = set_run_base_dir(base)
    try:
        return [evt async for evt in runner.run("quote it", resume)]
    finally:
        reset_run_base_dir(token)


def _refused(events: list[Any], text: str, record: Path) -> CompletedEvent:
    assert len(events) == 1
    done = events[0]
    assert isinstance(done, CompletedEvent)
    assert done.ok is False
    assert done.error == text
    assert done.resume is None
    assert done.usage == {PRESPAWN_BLOCKED_KEY: agy.CONTINUE_BLOCK}
    assert not record.exists(), "agy was started"
    return done


@pytest.mark.anyio
async def test_continue_passes_this_projects_conversation(
    cache: Path, record: Path, tree: dict[str, Path]
) -> None:
    proj = tree["proj"]
    _write(cache, {str(proj): CONV, str(proj.parent): OTHER})
    with structlog.testing.capture_logs() as logs:
        events = await _continue(proj)
    assert isinstance(events[0], StartedEvent)
    assert events[0].resume == ResumeToken(engine=ENGINE, value=CONV)
    done = events[-1]
    assert isinstance(done, CompletedEvent) and done.ok is True
    assert done.resume == events[0].resume
    rec = json.loads(record.read_text())
    assert f"--conversation={CONV}" in rec["argv"]
    assert "--continue" not in rec["argv"]
    assert os.path.realpath(rec["cwd"]) == os.path.realpath(proj)
    assert [e for e in logs if e["event"] == "antigravity.continue.resolved"]


@pytest.mark.anyio
@pytest.mark.parametrize("where", ["outer", "child", "sibling"])
async def test_continue_from_another_directory_starts_nothing(
    cache: Path, record: Path, tree: dict[str, Path], where: str
) -> None:
    _write(cache, {str(tree["proj"]): CONV})
    with structlog.testing.capture_logs() as logs:
        events = await _continue(tree[where])
    _refused(events, agy.NO_CONVERSATION_TEXT, record)
    refused = [e for e in logs if e["event"] == "antigravity.continue.refused"]
    assert [e["reason"] for e in refused] == ["no_entry"]


@pytest.mark.anyio
async def test_continue_without_a_cache_starts_nothing(
    cache: Path, record: Path, tree: dict[str, Path]
) -> None:
    events = await _continue(tree["proj"])
    _refused(events, agy.NO_CONVERSATION_TEXT, record)


@pytest.mark.anyio
async def test_continue_with_a_shared_id_starts_nothing(
    cache: Path, record: Path, tree: dict[str, Path]
) -> None:
    _write(cache, {str(tree["proj"]): CONV, str(tree["child"]): CONV})
    events = await _continue(tree["child"])
    _refused(events, agy.CONTINUE_SHARED_TEXT, record)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "raw",
    [b"not json", b"[]", b'{"%s": 7}', b'{"%s": "--dangerously-skip-permissions"}'],
)
async def test_continue_with_a_bad_cache_starts_nothing(
    cache: Path, record: Path, tree: dict[str, Path], raw: bytes
) -> None:
    proj = tree["proj"]
    cache.write_bytes(raw.replace(b"%s", str(proj).encode()))
    with structlog.testing.capture_logs() as logs:
        events = await _continue(proj)
    _refused(events, agy.CONTINUE_UNREADABLE_TEXT, record)
    # The value from the file is never echoed.
    assert "dangerously" not in json.dumps(logs, default=str)


@pytest.mark.anyio
async def test_continue_lookup_crash_starts_nothing(
    cache: Path,
    record: Path,
    tree: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(cwd: Path) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cont, "lookup_conversation", _boom)
    events = await _continue(tree["proj"])
    _refused(events, agy.CONTINUE_UNREADABLE_TEXT, record)


def test_user_texts_are_plain_and_distinct() -> None:
    texts = {
        agy.NO_CONVERSATION_TEXT,
        agy.CONTINUE_SHARED_TEXT,
        agy.CONTINUE_UNREADABLE_TEXT,
    }
    assert len(texts) == 3
    assert agy.NO_CONVERSATION_TEXT.startswith(
        "No Antigravity conversation for this project yet"
    )
    for text in texts:
        assert "last_conversations" not in text and ".gemini" not in text


def test_build_args_never_emits_a_bare_continue() -> None:
    runner = AntigravityRunner(antigravity_cmd="agy")
    token = ResumeToken(engine=ENGINE, value="", is_continue=True)
    with pytest.raises(AgyArgvError) as err:
        runner.build_args("p", token, state=None)
    assert err.value.reason == "unresolved_continue"
    # Nothing in the runner can put the flag into an argv.
    assert '"--continue"' not in inspect.getsource(agy)


# -- /continue finds a conversation agy has since deleted --------------------

# The id the ``unknown_conversation`` capture's stderr warning names.
GONE = "00000000-1111-2222-3333-444444444444"
SAVED = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.mark.anyio
async def test_continue_to_a_deleted_conversation_says_nothing_was_resumed(
    cache: Path, tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "unknown_conversation")
    _write(cache, {str(tree["proj"]): GONE})
    events = await _continue(tree["proj"])
    (done,) = events
    assert isinstance(done, CompletedEvent) and done.ok is False
    assert done.error == agy.CONTINUE_GONE_TEXT
    # No resume line for the dead id, and nothing the bridge reads as "the
    # chat's saved session failed".
    assert done.resume is None
    assert not _RESUME_FAILURE_RE.search(done.error or "")


def test_continue_gone_text_claims_nothing_was_cleared() -> None:
    text = agy.CONTINUE_GONE_TEXT
    assert text.startswith(
        "The conversation Antigravity had recorded for this project no longer"
        " exists, so nothing was resumed."
    )
    assert "cleared" not in text
    assert "last_conversations" not in text and ".gemini" not in text
    assert text != agy.CONVERSATION_GONE_TEXT


async def _bridge_run(
    base: Path, resume: ResumeToken
) -> tuple[dict[str, str], list[ResumeToken], list[str], list[Any]]:
    """One run through ``handle_message`` in a chat whose saved Antigravity
    session is ``SAVED``. ``on_resume_failed`` clears by engine, as the
    Telegram loop's does."""
    saved = {ENGINE: SAVED}
    failed: list[ResumeToken] = []

    async def on_resume_failed(token: ResumeToken) -> None:
        failed.append(token)
        saved.pop(token.engine, None)

    transport = FakeTransport()
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    token = set_run_base_dir(base)
    try:
        with structlog.testing.capture_logs() as logs, anyio.fail_after(30):
            await handle_message(
                cfg,
                runner=AntigravityRunner(antigravity_cmd=str(FAKE_AGY)),
                incoming=IncomingMessage(
                    channel_id=4243, message_id=10, text="carry on"
                ),
                resume_token=resume,
                running_tasks={},
                on_resume_failed=on_resume_failed,
            )
    finally:
        reset_run_base_dir(token)
    texts = [call["message"].text for call in transport.send_calls]
    texts += [call["message"].text for call in transport.edit_calls]
    return saved, failed, texts, logs


@pytest.mark.anyio
async def test_continue_to_a_deleted_conversation_keeps_the_saved_session(
    cache: Path, tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chat's saved conversation is healthy; agy's file lists another one
    for this directory, which was deleted. /continue must leave the saved one
    alone and must not say anything was cleared."""
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "unknown_conversation")
    _write(cache, {str(tree["proj"]): GONE})

    saved, failed, texts, logs = await _bridge_run(
        tree["proj"], ResumeToken(engine=ENGINE, value="", is_continue=True)
    )

    assert saved == {ENGINE: SAVED}
    assert failed == []
    final = "\n".join(texts)
    assert "no longer exists" in final and "nothing was resumed" in final
    assert "was cleared" not in final
    assert "couldn't be resumed" not in final
    assert GONE not in final
    assert not [e for e in logs if e["event"] == "session.auto_cleared"]
    (skipped,) = [e for e in logs if e["event"] == "session.auto_clear_skipped"]
    assert skipped["reason"] == "continue_token"


@pytest.mark.anyio
async def test_resume_by_id_to_a_deleted_conversation_still_clears(
    cache: Path, tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unchanged: a resume that names the conversation (the saved session or
    a reply to its resume line) clears it and says so."""
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "unknown_conversation")

    saved, failed, texts, logs = await _bridge_run(
        tree["proj"], ResumeToken(engine=ENGINE, value=GONE)
    )

    assert saved == {}
    assert failed == [ResumeToken(engine=ENGINE, value=GONE)]
    final = "\n".join(texts)
    assert "That Antigravity conversation no longer exists" in final
    assert "couldn't be resumed, so it was cleared" in final
    assert [e for e in logs if e["event"] == "session.auto_cleared"]


# -- #1047: a refused /continue is a plain reply -------------------------------


async def _seed_quota_footer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cache a ``/usage`` quota for the fake agy and switch the quota line
    on, so any Antigravity final that gets the footer shows it."""
    from untether.settings import FooterSettings
    from untether.utils import antigravity_quota

    async def seed(runner: Any, command: str, **kwargs: Any) -> dict[str, Any]:
        bucket = {"window": "5h", "remaining_fraction": 0.5, "reset_time": None}
        return {"groups": [{"name": "Gemini Models", "buckets": [bucket]}]}

    monkeypatch.setattr(antigravity_quota, "run_agy_slash", seed)
    await antigravity_quota.get_quota(AntigravityRunner(antigravity_cmd=str(FAKE_AGY)))
    monkeypatch.setattr(
        "untether.runner_bridge._load_footer_settings",
        lambda: FooterSettings(show_api_cost=False, show_subscription_usage=True),
    )


async def _continue_in_chat(
    base: Path, *, final_notify: bool = True
) -> tuple[FakeTransport, list[Any]]:
    transport = FakeTransport()
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=final_notify
    )
    token = set_run_base_dir(base)
    try:
        with structlog.testing.capture_logs() as logs, anyio.fail_after(30):
            await handle_message(
                cfg,
                runner=AntigravityRunner(antigravity_cmd=str(FAKE_AGY)),
                incoming=IncomingMessage(
                    channel_id=4243, message_id=10, text="carry on"
                ),
                resume_token=ResumeToken(engine=ENGINE, value="", is_continue=True),
                running_tasks={},
            )
    finally:
        reset_run_base_dir(token)
    return transport, logs


def _cache_for(case: str, cache: Path, tree: dict[str, Path]) -> str:
    if case == "no_entry":
        _write(cache, {str(tree["outer"]): CONV})
        return agy.NO_CONVERSATION_TEXT
    if case == "shared_id":
        _write(cache, {str(tree["proj"]): CONV, str(tree["child"]): CONV})
        return agy.CONTINUE_SHARED_TEXT
    cache.write_bytes(b"not json")
    return agy.CONTINUE_UNREADABLE_TEXT


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["no_entry", "shared_id", "unreadable"])
async def test_refused_continue_is_a_plain_reply(
    cache: Path,
    record: Path,
    tree: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """The refusal text alone replaces the progress card: no error header,
    no code block, no quota line, and no WARNING for a run that was never
    started."""
    text = _cache_for(case, cache, tree)
    await _seed_quota_footer(monkeypatch)

    transport, logs = await _continue_in_chat(tree["proj"])

    assert not record.exists(), "agy was started"
    progress, final = transport.send_calls
    assert final["message"].text == text
    assert final["options"].replace == progress["ref"]
    assert final["message"].extra.get("reply_markup") == {"inline_keyboard": []}
    assert transport.edit_calls == []
    warnings = [e for e in logs if e["log_level"] in {"warning", "error"}]
    assert [e["event"] for e in warnings] == ["antigravity.continue.refused"]
    (summary,) = [e for e in logs if e["event"] == "session.summary.no_events"]
    assert summary["log_level"] == "info"
    assert summary["prespawn_blocked"] == agy.CONTINUE_BLOCK


@pytest.mark.anyio
async def test_refused_continue_without_final_notify_edits_the_card(
    cache: Path, record: Path, tree: dict[str, Path]
) -> None:
    """With final notifications off the final is an edit of the progress
    card: the same plain text, and the card's Cancel button is cleared."""
    transport, _ = await _continue_in_chat(tree["proj"], final_notify=False)
    assert not record.exists(), "agy was started"
    (progress,) = transport.send_calls
    final = transport.edit_calls[-1]
    assert final["ref"] == progress["ref"]
    assert final["message"].text == agy.NO_CONVERSATION_TEXT
    assert final["message"].extra.get("reply_markup") == {"inline_keyboard": []}


@pytest.mark.anyio
async def test_a_continue_that_ran_and_failed_keeps_the_error_card(
    cache: Path, tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: agy was started (the recorded conversation was deleted), so
    the run failed for real and keeps the error card and its quota line."""
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "unknown_conversation")
    _write(cache, {str(tree["proj"]): GONE})
    await _seed_quota_footer(monkeypatch)

    transport, _ = await _continue_in_chat(tree["proj"])

    final = transport.send_calls[-1]["message"].text
    assert final.startswith("error · antigravity")
    assert "nothing was resumed" in final
    assert "Gemini Models" in final
