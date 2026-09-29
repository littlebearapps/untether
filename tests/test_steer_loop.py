"""#775: the Telegram loop routes prompts through the steer helper.

``maybe_steer`` is replaced by a recorder so these tests pin the *routing*:
what reaches it (text, override, target session), what never does (commands,
uploads) and that a steered prompt is not also queued/run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import untether.telegram.loop as telegram_loop
from tests.telegram_fakes import (
    FakeBot,
    FakeTransport,
    _empty_projects,
    _make_router,
)
from untether.markdown import MarkdownPresenter
from untether.model import ResumeToken
from untether.runner_bridge import ExecBridgeConfig
from untether.runners.mock import Return, ScriptRunner
from untether.telegram.bridge import TelegramBridgeConfig
from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
from untether.telegram.chat_sessions import ChatSessionStore, resolve_sessions_path
from untether.telegram.types import TelegramIncomingMessage
from untether.transport_runtime import TransportRuntime

pytestmark = pytest.mark.anyio

ENGINE = "codex"
RESUME = "resume-xyz"


def _msg(text: str, message_id: int = 5, **kw: Any) -> TelegramIncomingMessage:
    return TelegramIncomingMessage(
        transport="telegram",
        chat_id=123,
        message_id=message_id,
        text=text,
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=123,
        chat_type="private",
        **kw,
    )


async def _loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    messages: list[TelegramIncomingMessage],
    *,
    steer_result: bool = False,
    bot: Any = None,
    projects: Any = None,
    files: Any = None,
) -> tuple[list[dict[str, Any]], ScriptRunner, FakeTransport]:
    state_path = tmp_path / "untether.toml"
    store = ChatSessionStore(resolve_sessions_path(state_path))
    await store.set_session_resume(123, None, ResumeToken(engine=ENGINE, value=RESUME))
    calls: list[dict[str, Any]] = []

    async def fake_maybe_steer(cfg: Any, **kw: Any) -> bool:
        # The target session is only resolved once steer is wanted.
        kw["target"] = await kw["resolve_token"]()
        calls.append(kw)
        return steer_result

    monkeypatch.setattr(telegram_loop, "maybe_steer", fake_maybe_steer)
    transport = FakeTransport()
    runner = ScriptRunner([Return(answer="ok")], engine=ENGINE)
    runtime = TransportRuntime(
        router=_make_router(runner),
        projects=projects or _empty_projects(),
        config_path=state_path,
    )
    extra: dict[str, Any] = {"files": files} if files is not None else {}
    cfg = TelegramBridgeConfig(
        bot=bot or FakeBot(),
        runtime=runtime,
        chat_id=123,
        startup_msg="",
        exec_cfg=ExecBridgeConfig(
            transport=transport, presenter=MarkdownPresenter(), final_notify=True
        ),
        forward_coalesce_s=0.0,
        media_group_debounce_s=0.0,
        session_mode="chat",
        **extra,
    )

    async def poller(_cfg: TelegramBridgeConfig):
        for m in messages:
            yield m

    await telegram_loop.run_main_loop(cfg, poller)
    return calls, runner, transport


async def test_plain_text_goes_through_steer_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, runner, _ = await _loop(tmp_path, monkeypatch, [_msg("do the thing")])
    (call,) = calls
    assert call["prompt_text"] == "do the thing"
    assert call["override"] is None
    assert call["resume_token"] is None
    assert call["target"] == ResumeToken(engine=ENGINE, value=RESUME)
    assert call["user_msg_id"] == 5
    # Not steered → queued/run as before.
    assert len(runner.calls) == 1


async def test_steer_command_with_text_is_a_prompt_with_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, _runner, _ = await _loop(
        tmp_path, monkeypatch, [_msg("/steer also check the logs")]
    )
    (call,) = calls
    assert call["prompt_text"] == "also check the logs"
    assert call["override"] == "steer"


async def test_queue_command_with_text_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, runner, _ = await _loop(tmp_path, monkeypatch, [_msg("/queue later please")])
    (call,) = calls
    assert call["override"] == "queue" and call["prompt_text"] == "later please"
    assert runner.calls[0][0].endswith("later please")


async def test_steered_prompt_is_not_also_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, runner, _ = await _loop(
        tmp_path, monkeypatch, [_msg("/steer go")], steer_result=True
    )
    assert len(calls) == 1
    assert runner.calls == []


async def test_bare_steer_sets_chat_default_and_never_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, runner, transport = await _loop(tmp_path, monkeypatch, [_msg("/steer")])
    assert calls == [] and runner.calls == []
    prefs = ChatPrefsStore(resolve_prefs_path(tmp_path / "untether.toml"))
    assert await prefs.get_followup_mode(123) == "steer"
    assert any("set to steer" in c["message"].text for c in transport.send_calls)


async def test_other_commands_never_reach_steer_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, _runner, _ = await _loop(tmp_path, monkeypatch, [_msg("/listen")])
    assert calls == []


async def test_prompt_upload_always_queues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from untether.config import ProjectConfig, ProjectsConfig
    from untether.settings import TelegramFilesSettings
    from untether.telegram.api_models import File
    from untether.telegram.types import TelegramDocument

    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    class _UploadBot(FakeBot):
        async def get_file(self, file_id: str) -> File | None:
            return File(file_path="files/hello.txt")

        async def download_file(self, file_path: str) -> bytes | None:
            return b"hello"

    projects = ProjectsConfig(
        projects={
            "proj": ProjectConfig(
                alias="proj", path=project_dir, worktrees_dir=Path(".worktrees")
            )
        },
        default_project="proj",
    )
    doc = TelegramDocument(
        file_id="doc-1",
        file_name="hello.txt",
        mime_type="text/plain",
        file_size=5,
        raw={"file_id": "doc-1"},
    )
    calls, runner, _ = await _loop(
        tmp_path,
        monkeypatch,
        [_msg("/steer look at this", document=doc)],
        bot=_UploadBot(),
        projects=projects,
        files=TelegramFilesSettings(
            enabled=True, auto_put=True, auto_put_mode="prompt"
        ),
    )
    assert calls == []
    assert len(runner.calls) == 1


# ── #794 coalescer and #775 overrides ─────────────────────────────────────────


def _pp(message_id: int, text: str, override: str | None) -> Any:
    from untether.telegram.loop import _PendingPrompt

    return _PendingPrompt(
        msg=_msg(text, message_id=message_id),
        text=text,
        ambient_context=None,
        chat_project=None,
        topic_key=None,
        chat_session_key=None,
        reply_ref=None,
        reply_id=None,
        is_voice_transcribed=False,
        forwards=[],
        followup_override=override,
    )


@pytest.mark.parametrize(
    ("first", "second", "merged"),
    [
        (None, "steer", False),
        ("steer", None, False),
        ("steer", "queue", False),
        ("steer", "steer", True),
        (None, None, True),
    ],
)
def test_prompts_with_different_overrides_are_not_merged(
    first: str | None, second: str | None, merged: bool
) -> None:
    from untether.telegram.loop import _merge_block_reason

    reason = _merge_block_reason(_pp(1, "one", first), _pp(2, "two", second))
    assert (reason is None) is merged
    if not merged:
        assert reason == "followup_mode"
