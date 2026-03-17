"""Tests for outbox file delivery (agent → user via Telegram)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from untether.telegram.outbox_delivery import (
    cleanup_outbox,
    deliver_outbox_files,
    scan_outbox,
)

DENY_GLOBS = (".git/**", ".env", ".envrc", "**/*.pem", "**/.ssh/**")
MAX_BYTES = 50 * 1024 * 1024  # 50 MB


# -- scan_outbox --


def test_scan_missing_outbox_dir(tmp_path: Path) -> None:
    files, skipped = scan_outbox(
        tmp_path,
        outbox_dir=".untether-outbox",
        deny_globs=DENY_GLOBS,
        max_download_bytes=MAX_BYTES,
        max_files=10,
    )
    assert files == []
    assert skipped == []


def test_scan_empty_outbox_dir(tmp_path: Path) -> None:
    (tmp_path / ".untether-outbox").mkdir()
    files, skipped = scan_outbox(
        tmp_path,
        outbox_dir=".untether-outbox",
        deny_globs=DENY_GLOBS,
        max_download_bytes=MAX_BYTES,
        max_files=10,
    )
    assert files == []
    assert skipped == []


def test_scan_single_file(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "plan.md").write_text("# Plan", encoding="utf-8")

    files, skipped = scan_outbox(
        tmp_path,
        outbox_dir=".untether-outbox",
        deny_globs=DENY_GLOBS,
        max_download_bytes=MAX_BYTES,
        max_files=10,
    )
    assert len(files) == 1
    assert files[0].abs_path.name == "plan.md"
    assert files[0].rel_path == Path(".untether-outbox/plan.md")
    assert files[0].size > 0
    assert skipped == []


def test_scan_multiple_files_sorted(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "zebra.txt").write_text("z", encoding="utf-8")
    (outbox / "alpha.txt").write_text("a", encoding="utf-8")

    files, _ = scan_outbox(
        tmp_path,
        outbox_dir=".untether-outbox",
        deny_globs=DENY_GLOBS,
        max_download_bytes=MAX_BYTES,
        max_files=10,
    )
    assert [f.abs_path.name for f in files] == ["alpha.txt", "zebra.txt"]


def test_scan_respects_max_files(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    for i in range(5):
        (outbox / f"file_{i:02d}.txt").write_text(f"content {i}", encoding="utf-8")

    files, skipped = scan_outbox(
        tmp_path,
        outbox_dir=".untether-outbox",
        deny_globs=DENY_GLOBS,
        max_download_bytes=MAX_BYTES,
        max_files=3,
    )
    assert len(files) == 3
    assert any("exceeded max_files=3" in reason for _, reason in skipped)


def test_scan_skips_deny_glob(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "safe.txt").write_text("ok", encoding="utf-8")
    (outbox / "key.pem").write_text("secret", encoding="utf-8")

    files, skipped = scan_outbox(
        tmp_path,
        outbox_dir=".untether-outbox",
        deny_globs=DENY_GLOBS,
        max_download_bytes=MAX_BYTES,
        max_files=10,
    )
    assert len(files) == 1
    assert files[0].abs_path.name == "safe.txt"
    assert any(name == "key.pem" for name, _ in skipped)


def test_scan_skips_env_file(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / ".env").write_text("SECRET=abc", encoding="utf-8")

    files, skipped = scan_outbox(
        tmp_path,
        outbox_dir=".untether-outbox",
        deny_globs=DENY_GLOBS,
        max_download_bytes=MAX_BYTES,
        max_files=10,
    )
    assert len(files) == 0
    assert any(name == ".env" for name, _ in skipped)


def test_scan_skips_oversized_file(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "huge.bin").write_bytes(b"x" * 200)

    files, skipped = scan_outbox(
        tmp_path,
        outbox_dir=".untether-outbox",
        deny_globs=(),
        max_download_bytes=100,
        max_files=10,
    )
    assert len(files) == 0
    assert any("too large" in reason for _, reason in skipped)


def test_scan_skips_empty_file(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "empty.txt").write_text("", encoding="utf-8")

    files, skipped = scan_outbox(
        tmp_path,
        outbox_dir=".untether-outbox",
        deny_globs=(),
        max_download_bytes=MAX_BYTES,
        max_files=10,
    )
    assert len(files) == 0
    assert any(name == "empty.txt" for name, _ in skipped)


def test_scan_skips_symlinks(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    target = tmp_path / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    try:
        (outbox / "link.txt").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")

    files, skipped = scan_outbox(
        tmp_path,
        outbox_dir=".untether-outbox",
        deny_globs=(),
        max_download_bytes=MAX_BYTES,
        max_files=10,
    )
    assert len(files) == 0
    assert any(name == "link.txt" for name, _ in skipped)


def test_scan_skips_subdirectories(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "subdir").mkdir()
    (outbox / "file.txt").write_text("ok", encoding="utf-8")

    files, skipped = scan_outbox(
        tmp_path,
        outbox_dir=".untether-outbox",
        deny_globs=(),
        max_download_bytes=MAX_BYTES,
        max_files=10,
    )
    assert len(files) == 1
    assert any(name == "subdir" for name, _ in skipped)


# -- cleanup_outbox --


def test_cleanup_deletes_sent_files(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    f1 = outbox / "a.txt"
    f1.write_text("content", encoding="utf-8")

    from untether.telegram.outbox_delivery import OutboxFile

    sent = [OutboxFile(rel_path=Path(".untether-outbox/a.txt"), abs_path=f1, size=7)]
    removed = cleanup_outbox(tmp_path, ".untether-outbox", sent)
    assert not f1.exists()
    assert removed is True
    assert not outbox.exists()


def test_cleanup_keeps_dir_if_unsent_files_remain(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    f1 = outbox / "sent.txt"
    f1.write_text("sent", encoding="utf-8")
    f2 = outbox / "unsent.txt"
    f2.write_text("still here", encoding="utf-8")

    from untether.telegram.outbox_delivery import OutboxFile

    sent = [OutboxFile(rel_path=Path(".untether-outbox/sent.txt"), abs_path=f1, size=4)]
    removed = cleanup_outbox(tmp_path, ".untether-outbox", sent)
    assert not f1.exists()
    assert f2.exists()
    assert removed is False
    assert outbox.exists()


def test_cleanup_handles_already_deleted_file(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    abs_path = outbox / "gone.txt"

    from untether.telegram.outbox_delivery import OutboxFile

    sent = [
        OutboxFile(
            rel_path=Path(".untether-outbox/gone.txt"), abs_path=abs_path, size=0
        )
    ]
    # Should not raise — missing_ok=True
    removed = cleanup_outbox(tmp_path, ".untether-outbox", sent)
    assert removed is True


# -- deliver_outbox_files --


@pytest.mark.anyio
async def test_deliver_sends_files(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "report.md").write_text("# Report", encoding="utf-8")

    send_file = AsyncMock()
    result = await deliver_outbox_files(
        send_file=send_file,
        channel_id=123,
        thread_id=456,
        reply_to_msg_id=789,
        run_root=tmp_path,
        outbox_dir=".untether-outbox",
        deny_globs=DENY_GLOBS,
        max_download_bytes=MAX_BYTES,
        max_files=10,
        cleanup=True,
    )

    assert len(result.sent) == 1
    send_file.assert_called_once()
    call_args = send_file.call_args[0]
    assert call_args[0] == 123  # channel_id
    assert call_args[1] == 456  # thread_id
    assert call_args[2] == "report.md"  # filename
    assert call_args[4] == 789  # reply_to_msg_id
    assert "report.md" in call_args[5]  # caption


@pytest.mark.anyio
async def test_deliver_cleans_up_after_send(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "file.txt").write_text("content", encoding="utf-8")

    result = await deliver_outbox_files(
        send_file=AsyncMock(),
        channel_id=1,
        thread_id=None,
        reply_to_msg_id=None,
        run_root=tmp_path,
        outbox_dir=".untether-outbox",
        deny_globs=(),
        max_download_bytes=MAX_BYTES,
        max_files=10,
        cleanup=True,
    )

    assert result.cleaned is True
    assert not outbox.exists()


@pytest.mark.anyio
async def test_deliver_no_cleanup_when_disabled(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "keep.txt").write_text("keep me", encoding="utf-8")

    result = await deliver_outbox_files(
        send_file=AsyncMock(),
        channel_id=1,
        thread_id=None,
        reply_to_msg_id=None,
        run_root=tmp_path,
        outbox_dir=".untether-outbox",
        deny_globs=(),
        max_download_bytes=MAX_BYTES,
        max_files=10,
        cleanup=False,
    )

    assert result.cleaned is False
    assert outbox.exists()
    assert (outbox / "keep.txt").exists()


@pytest.mark.anyio
async def test_deliver_empty_outbox_returns_empty_result(tmp_path: Path) -> None:
    result = await deliver_outbox_files(
        send_file=AsyncMock(),
        channel_id=1,
        thread_id=None,
        reply_to_msg_id=None,
        run_root=tmp_path,
        outbox_dir=".untether-outbox",
        deny_globs=(),
        max_download_bytes=MAX_BYTES,
        max_files=10,
        cleanup=True,
    )
    assert result.sent == []
    assert result.skipped == []
    assert result.cleaned is False


@pytest.mark.anyio
async def test_deliver_continues_on_send_failure(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "a.txt").write_text("aaa", encoding="utf-8")
    (outbox / "b.txt").write_text("bbb", encoding="utf-8")

    call_count = 0

    async def failing_send(*args: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("network error")

    result = await deliver_outbox_files(
        send_file=failing_send,
        channel_id=1,
        thread_id=None,
        reply_to_msg_id=None,
        run_root=tmp_path,
        outbox_dir=".untether-outbox",
        deny_globs=(),
        max_download_bytes=MAX_BYTES,
        max_files=10,
        cleanup=True,
    )
    # First file failed, second succeeded
    assert len(result.sent) == 1
    assert result.sent[0].abs_path.name == "b.txt"
