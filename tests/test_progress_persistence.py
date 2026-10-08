"""Tests for progress message persistence across restarts."""

from __future__ import annotations

from pathlib import Path

import pytest

from untether.telegram.progress_persistence import (
    clear_all_progress,
    load_active_progress,
    register_progress,
    resolve_progress_path,
    unregister_progress,
)


def test_resolve_progress_path() -> None:
    p = resolve_progress_path(Path("/cfg/untether.toml"))
    assert p == Path("/cfg/active_progress.json")


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "active_progress.json"
    assert load_active_progress(path) == {}


def test_load_corrupt_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "active_progress.json"
    path.write_text("NOT JSON!", encoding="utf-8")
    assert load_active_progress(path) == {}


def test_load_non_dict_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "active_progress.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_active_progress(path) == {}


def test_register_and_load(tmp_path: Path) -> None:
    path = tmp_path / "active_progress.json"
    register_progress(path, "123:456", chat_id=123, message_id=456)
    entries = load_active_progress(path)
    assert entries == {"123:456": {"chat_id": 123, "message_id": 456}}


def test_unregister_removes_entry(tmp_path: Path) -> None:
    path = tmp_path / "active_progress.json"
    register_progress(path, "123:456", chat_id=123, message_id=456)
    unregister_progress(path, "123:456")
    assert load_active_progress(path) == {}


def test_unregister_nonexistent_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "active_progress.json"
    register_progress(path, "123:456", chat_id=123, message_id=456)
    unregister_progress(path, "999:999")
    assert "123:456" in load_active_progress(path)


def test_multiple_entries(tmp_path: Path) -> None:
    path = tmp_path / "active_progress.json"
    register_progress(path, "1:10", chat_id=1, message_id=10)
    register_progress(path, "2:20", chat_id=2, message_id=20)
    entries = load_active_progress(path)
    assert len(entries) == 2
    assert entries["1:10"]["chat_id"] == 1
    assert entries["2:20"]["message_id"] == 20


def test_clear_all(tmp_path: Path) -> None:
    path = tmp_path / "active_progress.json"
    register_progress(path, "1:10", chat_id=1, message_id=10)
    register_progress(path, "2:20", chat_id=2, message_id=20)
    clear_all_progress(path)
    assert load_active_progress(path) == {}


def test_clear_nonexistent_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "active_progress.json"
    clear_all_progress(path)  # should not raise


@pytest.mark.anyio
async def test_810_orphan_cleanup_skips_cancelled_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#810: a run cancelled before a restart must not be relabelled
    "interrupted by restart" by the next startup's orphan cleanup, while a
    genuinely unfinished progress message still is."""
    from types import SimpleNamespace

    import anyio

    import untether.runner_bridge as rb
    from untether.markdown import MarkdownPresenter
    from untether.runner_bridge import ExecBridgeConfig, IncomingMessage
    from untether.runners.mock import ScriptRunner, Wait
    from untether.telegram.loop import _cleanup_orphan_progress
    from untether.transport import MessageRef

    config_path = tmp_path / "untether.toml"
    progress_path = resolve_progress_path(config_path)
    monkeypatch.setattr(rb, "_PROGRESS_PERSISTENCE_PATH", progress_path)

    class _Transport:
        def __init__(self) -> None:
            self._next = 100
            self.edits: list[tuple[int, str]] = []

        async def send(self, *, channel_id, message, options=None):
            self._next += 1
            return MessageRef(channel_id=channel_id, message_id=self._next)

        async def edit(self, *, ref, message, wait=True):
            self.edits.append((ref.message_id, message.text))
            return ref

        async def delete(self, *, ref):
            return True

        async def close(self) -> None:
            return None

    transport = _Transport()
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    running_tasks: dict = {}
    runner = ScriptRunner([Wait(anyio.Event())], engine="codex", resume_value="s")

    async def _run() -> None:
        await rb.handle_message(
            cfg,
            runner=runner,
            incoming=IncomingMessage(channel_id=123, message_id=10, text="go"),
            resume_token=None,
            running_tasks=running_tasks,
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(_run)
        for _ in range(100):
            if running_tasks:
                break
            await anyio.lowlevel.checkpoint()
        task = running_tasks[next(iter(running_tasks))]
        with anyio.fail_after(1):
            await task.resume_ready.wait()
        task.cancel_requested.set()

    cancelled_id = 101
    assert "cancelled" in transport.edits[-1][1].lower()
    # A second, still-running progress message from the "prior instance".
    register_progress(progress_path, "123:555", chat_id=123, message_id=555)

    # Simulated restart: the startup orphan cleanup runs against the file.
    edited: list[int] = []

    class _Bot:
        async def edit_message_text(self, *, chat_id, message_id, text):
            edited.append(message_id)

    startup_cfg = SimpleNamespace(
        runtime=SimpleNamespace(config_path=config_path), bot=_Bot()
    )
    await _cleanup_orphan_progress(startup_cfg)  # type: ignore[arg-type]

    assert cancelled_id not in edited
    assert edited == [555]
    assert load_active_progress(progress_path) == {}


# ---------------------------------------------------------------------------
# #746 — orphan cleanup checks the edit result
# ---------------------------------------------------------------------------


def _orphan_cfg(config_path: Path, bot: object) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(runtime=SimpleNamespace(config_path=config_path), bot=bot)


def _events(logs: list[dict], name: str) -> list[dict]:
    return [r for r in logs if r.get("event") == name]


@pytest.mark.anyio
async def test_746_orphan_cleanup_logs_edited_only_on_success(tmp_path: Path) -> None:
    from structlog.testing import capture_logs

    from untether.telegram.loop import _cleanup_orphan_progress

    config_path = tmp_path / "untether.toml"
    progress_path = resolve_progress_path(config_path)
    for mid in (555, 556, 557):
        register_progress(progress_path, f"123:{mid}", chat_id=123, message_id=mid)

    class _Bot:
        def __init__(self) -> None:
            self.pops: list[tuple[object, object]] = []

        async def edit_message_text(self, *, chat_id, message_id, text):
            return object() if message_id == 555 else None

        def pop_edit_error(self, chat_id, message_id):
            self.pops.append((chat_id, message_id))
            return (
                "Bad Request: message to edit not found" if message_id == 556 else None
            )

    bot = _Bot()
    with capture_logs() as logs:
        await _cleanup_orphan_progress(_orphan_cfg(config_path, bot))  # type: ignore[arg-type]

    assert [
        r["message_id"] for r in _events(logs, "startup.orphan_cleanup.edited")
    ] == [555]
    failed = {
        r["message_id"]: r for r in _events(logs, "startup.orphan_cleanup.edit_failed")
    }
    assert set(failed) == {556, 557}
    assert failed[556]["reason"] == "Bad Request: message to edit not found"
    assert failed[557]["reason"] is None
    assert bot.pops == [(123, 556), (123, 557)]
    assert all(type(c) is int and type(m) is int for c, m in bot.pops)
    done = _events(logs, "startup.orphan_cleanup.done")
    assert len(done) == 1
    assert done[0]["log_level"] == "info"
    assert (
        done[0]["count"],
        done[0]["edited"],
        done[0]["failed"],
        done[0]["skipped"],
    ) == (
        3,
        1,
        2,
        0,
    )
    assert load_active_progress(progress_path) == {}


@pytest.mark.anyio
async def test_746_orphan_cleanup_all_fail_still_clears_state(tmp_path: Path) -> None:
    from structlog.testing import capture_logs

    from untether.telegram.loop import _cleanup_orphan_progress

    config_path = tmp_path / "untether.toml"
    progress_path = resolve_progress_path(config_path)
    for mid in (10, 11):
        register_progress(progress_path, f"1:{mid}", chat_id=1, message_id=mid)

    class _Bot:
        async def edit_message_text(self, *, chat_id, message_id, text):
            return None

        def pop_edit_error(self, chat_id, message_id):
            return None

    with capture_logs() as logs:
        await _cleanup_orphan_progress(_orphan_cfg(config_path, _Bot()))  # type: ignore[arg-type]

    done = _events(logs, "startup.orphan_cleanup.done")[0]
    assert (done["edited"], done["failed"]) == (0, 2)
    assert not _events(logs, "startup.orphan_cleanup.edited")
    assert load_active_progress(progress_path) == {}


@pytest.mark.anyio
async def test_746_orphan_cleanup_bot_without_pop(tmp_path: Path) -> None:
    from structlog.testing import capture_logs

    from untether.telegram.loop import _cleanup_orphan_progress

    config_path = tmp_path / "untether.toml"
    progress_path = resolve_progress_path(config_path)
    register_progress(progress_path, "1:10", chat_id=1, message_id=10)

    class _Bot:
        async def edit_message_text(self, *, chat_id, message_id, text):
            return None

    with capture_logs() as logs:
        await _cleanup_orphan_progress(_orphan_cfg(config_path, _Bot()))  # type: ignore[arg-type]

    failed = _events(logs, "startup.orphan_cleanup.edit_failed")
    assert len(failed) == 1 and failed[0]["reason"] is None


@pytest.mark.anyio
async def test_746_orphan_cleanup_corrupt_and_missing_entries(tmp_path: Path) -> None:
    import json

    from structlog.testing import capture_logs

    from untether.telegram.loop import _cleanup_orphan_progress

    config_path = tmp_path / "untether.toml"
    progress_path = resolve_progress_path(config_path)
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path.write_text(
        json.dumps(
            {
                "bad": {"chat_id": "abc", "message_id": 5},
                "missing": {"chat_id": 1},
                "good": {"chat_id": 1, "message_id": 7},
            }
        )
    )
    edited: list[int] = []

    class _Bot:
        async def edit_message_text(self, *, chat_id, message_id, text):
            edited.append(message_id)
            return object()

    with capture_logs() as logs:
        await _cleanup_orphan_progress(_orphan_cfg(config_path, _Bot()))  # type: ignore[arg-type]

    assert edited == [7]
    failed = _events(logs, "startup.orphan_cleanup.edit_failed")
    assert len(failed) == 1 and failed[0]["chat_id"] == "abc"
    assert failed[0].get("exc_info") is True
    done = _events(logs, "startup.orphan_cleanup.done")[0]
    assert (done["edited"], done["failed"], done["skipped"]) == (1, 1, 1)
    assert load_active_progress(progress_path) == {}


@pytest.mark.anyio
async def test_746_orphan_cleanup_http_400_end_to_end(tmp_path: Path) -> None:
    """The #746 regression: a restart that finds deleted/uneditable orphans
    logs no ERROR line, through the real TelegramClient outbox."""
    import json

    import httpx
    from structlog.testing import capture_logs

    from untether.telegram.client import TelegramClient
    from untether.telegram.loop import _cleanup_orphan_progress

    config_path = tmp_path / "untether.toml"
    progress_path = resolve_progress_path(config_path)
    for mid in (555, 556, 557):
        register_progress(progress_path, f"123:{mid}", chat_id=123, message_id=mid)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        mid = body["message_id"]
        if mid == 555:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "message_id": 555,
                        "chat": {"id": 123, "type": "private"},
                    },
                },
                request=request,
            )
        desc = (
            "Bad Request: message to edit not found"
            if mid == 556
            else "Bad Request: message can't be edited"
        )
        return httpx.Response(
            400,
            json={"ok": False, "error_code": 400, "description": desc},
            request=request,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tg = TelegramClient(
        "123:abcDEF_ghij", http_client=http, private_chat_rps=0.0, group_chat_rps=0.0
    )
    try:
        with capture_logs() as logs:
            await _cleanup_orphan_progress(_orphan_cfg(config_path, tg))  # type: ignore[arg-type]
    finally:
        await tg.close()
        await http.aclose()

    assert not [r for r in logs if r.get("log_level") == "error"]
    benign = _events(logs, "telegram.benign_rejection")
    assert sorted(r["reason_class"] for r in benign) == ["not_editable", "target_gone"]
    done = _events(logs, "startup.orphan_cleanup.done")[0]
    assert (done["count"], done["edited"], done["failed"]) == (3, 1, 2)
    assert not _events(logs, "telegram.benign_rejection.burst")
    reasons = {
        r["message_id"]: r["reason"]
        for r in _events(logs, "startup.orphan_cleanup.edit_failed")
    }
    assert reasons == {
        556: "Bad Request: message to edit not found",
        557: "Bad Request: message can't be edited",
    }
    assert load_active_progress(progress_path) == {}
