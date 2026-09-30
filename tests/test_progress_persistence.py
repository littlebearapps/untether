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
