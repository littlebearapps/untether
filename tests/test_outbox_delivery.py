"""Tests for outbox file delivery (agent → user via Telegram)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from untether.telegram import outbox_delivery as od
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


# ---------------------------------------------------------------------------
# #524 — surface skipped outbox entries (directories, oversized, etc.) so the
# agent's "I've prepared the guides folder for you" final message doesn't
# become a silent lie.
# ---------------------------------------------------------------------------


def test_format_outbox_skipped_notice_directory_uses_trailing_slash() -> None:
    from untether.runner_bridge import _format_outbox_skipped_notice

    text = _format_outbox_skipped_notice([("guides", "directory")])
    assert text.startswith("\U0001f4ce Outbox skipped")
    assert "guides/ — directory" in text


def test_format_outbox_skipped_notice_multiple_items_sorted() -> None:
    from untether.runner_bridge import _format_outbox_skipped_notice

    text = _format_outbox_skipped_notice(
        [
            ("z.txt", "too large: 60 MB > 50 MB"),
            ("guides", "directory"),
            ("key.pem", "denied by glob: **/*.pem"),
        ]
    )
    lines = text.splitlines()
    # Headline + 3 items, sorted alphabetically by name
    assert len(lines) == 4
    assert "guides/ — directory" in lines[1]
    assert "key.pem" in lines[2]
    assert "z.txt" in lines[3]


def test_format_outbox_skipped_notice_caps_at_ten_with_overflow() -> None:
    from untether.runner_bridge import _format_outbox_skipped_notice

    items = [(f"file_{i:02d}.txt", "denied") for i in range(15)]
    text = _format_outbox_skipped_notice(items)
    lines = text.splitlines()
    # Headline + 10 visible + overflow line = 12
    assert len(lines) == 12
    assert lines[-1] == "- … and 5 more"


def test_telegram_files_settings_default_notify_skipped() -> None:
    """#524: the new setting defaults to True so the surface fires
    automatically on upgrade without users opting in."""
    from untether.settings import TelegramFilesSettings

    cfg = TelegramFilesSettings()
    assert cfg.outbox_notify_skipped is True


def test_telegram_files_settings_can_disable_notify_skipped() -> None:
    from untether.settings import TelegramFilesSettings

    cfg = TelegramFilesSettings(outbox_notify_skipped=False)
    assert cfg.outbox_notify_skipped is False


# -- #600: skipped-directory graveyard --


def _deliver_kwargs(tmp_path: Path, **overrides):
    kwargs = {
        "send_file": AsyncMock(),
        "channel_id": 1,
        "thread_id": None,
        "reply_to_msg_id": None,
        "run_root": tmp_path,
        "outbox_dir": ".untether-outbox",
        "deny_globs": DENY_GLOBS,
        "max_download_bytes": MAX_BYTES,
        "max_files": 10,
        "cleanup": True,
    }
    kwargs.update(overrides)
    return kwargs


@pytest.mark.anyio
async def test_600_skipped_dir_archived_to_graveyard(tmp_path: Path) -> None:
    """A skipped directory is moved into .skipped/ and the skip reason
    tells the user where it went; sendable files are unaffected."""
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "guides").mkdir()
    (outbox / "guides" / "one.md").write_text("g", encoding="utf-8")
    (outbox / "report.md").write_text("# R", encoding="utf-8")

    result = await deliver_outbox_files(**_deliver_kwargs(tmp_path))

    assert len(result.sent) == 1
    assert not (outbox / "guides").exists()
    assert (outbox / ".skipped" / "guides" / "one.md").is_file()
    reasons = dict(result.skipped)
    assert "moved aside" in reasons["guides"]


@pytest.mark.anyio
async def test_600_second_run_does_not_rereport_archived_dir(
    tmp_path: Path,
) -> None:
    """After archival, subsequent scans see neither the directory nor the
    graveyard — the per-run noise stops."""
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "guides").mkdir()

    await deliver_outbox_files(**_deliver_kwargs(tmp_path))

    files, skipped = scan_outbox(
        tmp_path,
        outbox_dir=".untether-outbox",
        deny_globs=DENY_GLOBS,
        max_download_bytes=MAX_BYTES,
        max_files=10,
    )
    assert files == []
    assert skipped == []


@pytest.mark.anyio
async def test_600_only_skipped_dir_outbox_still_archives(tmp_path: Path) -> None:
    """An outbox containing ONLY a directory (zero sendable files) must
    still archive it — this was the #600 core gap (cleanup only ran when
    something was sent)."""
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "onboarding").mkdir()

    result = await deliver_outbox_files(**_deliver_kwargs(tmp_path))

    assert result.sent == []
    assert not (outbox / "onboarding").exists()
    assert (outbox / ".skipped" / "onboarding").is_dir()


@pytest.mark.anyio
async def test_600_cleanup_false_leaves_dir_in_place(tmp_path: Path) -> None:
    """outbox_cleanup=false keeps the outbox untouched — no archival."""
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "guides").mkdir()

    result = await deliver_outbox_files(**_deliver_kwargs(tmp_path, cleanup=False))

    assert (outbox / "guides").is_dir()
    assert not (outbox / ".skipped").exists()
    assert ("guides", "directory") in result.skipped


@pytest.mark.anyio
async def test_600_graveyard_name_collision_gets_suffix(tmp_path: Path) -> None:
    """A second directory with the same name archives to guides_1."""
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "guides").mkdir()
    await deliver_outbox_files(**_deliver_kwargs(tmp_path))

    (outbox / "guides").mkdir()  # agent makes the same mistake again
    await deliver_outbox_files(**_deliver_kwargs(tmp_path))

    assert (outbox / ".skipped" / "guides").is_dir()
    assert (outbox / ".skipped" / "guides_1").is_dir()


def test_600_scan_ignores_graveyard(tmp_path: Path) -> None:
    """The .skipped graveyard itself never appears in scan results."""
    outbox = tmp_path / ".untether-outbox"
    (outbox / ".skipped" / "old").mkdir(parents=True)
    (outbox / "file.txt").write_text("x", encoding="utf-8")

    files, skipped = scan_outbox(
        tmp_path,
        outbox_dir=".untether-outbox",
        deny_globs=DENY_GLOBS,
        max_download_bytes=MAX_BYTES,
        max_files=10,
    )
    assert [f.rel_path.name for f in files] == ["file.txt"]
    assert skipped == []


# -- #628: deliver skipped directories as zip --


def _read_sent_zip(send_file_mock):
    """Extract (filename, sorted namelist, caption) from a captured send_file
    call whose payload is a zip document."""
    import io
    import zipfile

    args = send_file_mock.call_args[0]
    fname, payload, caption = args[2], args[3], args[5]
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = sorted(zf.namelist())
    return fname, names, caption


@pytest.mark.anyio
async def test_628_zip_delivers_directory(tmp_path: Path) -> None:
    """deliver_directories='zip' bundles a skipped directory into <name>.zip,
    sends it, removes the source, and does NOT archive to .skipped/."""
    outbox = tmp_path / ".untether-outbox"
    (outbox / "screenshots").mkdir(parents=True)
    (outbox / "screenshots" / "a.png").write_bytes(b"PNGDATA-A")
    (outbox / "screenshots" / "b.png").write_bytes(b"PNGDATA-B")

    send_file = AsyncMock()
    result = await deliver_outbox_files(
        **_deliver_kwargs(tmp_path, send_file=send_file, deliver_directories="zip")
    )

    send_file.assert_called_once()
    fname, names, caption = _read_sent_zip(send_file)
    assert fname == "screenshots.zip"
    assert names == ["screenshots/a.png", "screenshots/b.png"]
    assert "screenshots.zip" in caption
    assert "2 files" in caption
    assert not (outbox / "screenshots").exists()
    assert not (outbox / ".skipped").exists()
    reasons = dict(result.skipped)
    assert "delivered as screenshots.zip" in reasons["screenshots"]


@pytest.mark.anyio
async def test_628_zip_excludes_denied_and_symlink_members(tmp_path: Path) -> None:
    """Recursive deny-glob + symlink filtering: secrets nested in a delivered
    directory are NEVER bundled."""
    outbox = tmp_path / ".untether-outbox"
    (outbox / "bundle").mkdir(parents=True)
    (outbox / "bundle" / "ok.txt").write_text("safe", encoding="utf-8")
    (outbox / "bundle" / "secret.pem").write_text("PRIVATE KEY", encoding="utf-8")
    (outbox / "bundle" / ".env").write_text("TOKEN=abc", encoding="utf-8")
    (outbox / "bundle" / "sub").mkdir()
    (outbox / "bundle" / "sub" / "nested.pem").write_text("KEY2", encoding="utf-8")
    # A symlink pointing outside the project must not be followed/bundled.
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET-OUTSIDE", encoding="utf-8")
    try:
        (outbox / "bundle" / "link.txt").symlink_to(outside)
        symlinks_supported = True
    except OSError:
        symlinks_supported = False

    send_file = AsyncMock()
    await deliver_outbox_files(
        **_deliver_kwargs(tmp_path, send_file=send_file, deliver_directories="zip")
    )

    _, names, _ = _read_sent_zip(send_file)
    assert names == ["bundle/ok.txt"]
    assert not any(".pem" in n for n in names)
    assert not any(".env" in n for n in names)
    if symlinks_supported:
        assert not any("link" in n for n in names)


@pytest.mark.anyio
async def test_628_empty_or_all_denied_dir_falls_back_to_archive(
    tmp_path: Path,
) -> None:
    """A directory with no deliverable members (all denied) is NOT sent; it
    falls back to the #600 archive so it stops re-scanning."""
    outbox = tmp_path / ".untether-outbox"
    (outbox / "secrets").mkdir(parents=True)
    (outbox / "secrets" / "id.pem").write_text("KEY", encoding="utf-8")

    send_file = AsyncMock()
    result = await deliver_outbox_files(
        **_deliver_kwargs(tmp_path, send_file=send_file, deliver_directories="zip")
    )

    send_file.assert_not_called()
    assert not (outbox / "secrets").exists()
    assert (outbox / ".skipped" / "secrets").is_dir()
    reasons = dict(result.skipped)
    assert "moved aside" in reasons["secrets"]


@pytest.mark.anyio
async def test_628_oversize_zip_falls_back_to_archive(tmp_path: Path) -> None:
    """A directory whose zip would exceed the size cap is archived, not sent."""
    outbox = tmp_path / ".untether-outbox"
    (outbox / "big").mkdir(parents=True)
    # Incompressible-ish content bigger than a tiny cap.
    (outbox / "big" / "data.bin").write_bytes(b"X" * 4096)

    send_file = AsyncMock()
    result = await deliver_outbox_files(
        **_deliver_kwargs(
            tmp_path,
            send_file=send_file,
            deliver_directories="zip",
            max_download_bytes=100,  # tiny cap → member exceeds it
        )
    )

    send_file.assert_not_called()
    assert (outbox / ".skipped" / "big").is_dir()
    reasons = dict(result.skipped)
    assert "moved aside" in reasons["big"]


@pytest.mark.anyio
async def test_628_off_keeps_archive_behaviour(tmp_path: Path) -> None:
    """Default deliver_directories='off' preserves #600 archive behaviour —
    no zip is sent."""
    outbox = tmp_path / ".untether-outbox"
    (outbox / "guides").mkdir(parents=True)
    (outbox / "guides" / "one.md").write_text("g", encoding="utf-8")

    send_file = AsyncMock()
    await deliver_outbox_files(
        **_deliver_kwargs(tmp_path, send_file=send_file, deliver_directories="off")
    )

    send_file.assert_not_called()
    assert (outbox / ".skipped" / "guides" / "one.md").is_file()


@pytest.mark.anyio
async def test_628_zip_respects_max_members(tmp_path: Path) -> None:
    """Member count is capped at max_files; the excess is reported skipped."""
    outbox = tmp_path / ".untether-outbox"
    (outbox / "many").mkdir(parents=True)
    for i in range(5):
        (outbox / "many" / f"f{i}.txt").write_text(str(i), encoding="utf-8")

    send_file = AsyncMock()
    await deliver_outbox_files(
        **_deliver_kwargs(
            tmp_path, send_file=send_file, deliver_directories="zip", max_files=3
        )
    )

    _, names, _ = _read_sent_zip(send_file)
    assert len(names) == 3


@pytest.mark.anyio
async def test_628_directory_count_capped(tmp_path: Path) -> None:
    """#628: the number of directory zip attachments is bounded (max_files) so
    a pathological outbox can't flood the chat; the excess is archived."""
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    for i in range(4):
        d = outbox / f"d{i}"
        d.mkdir()
        (d / "f.txt").write_text("x", encoding="utf-8")

    send_file = AsyncMock()
    result = await deliver_outbox_files(
        **_deliver_kwargs(
            tmp_path, send_file=send_file, deliver_directories="zip", max_files=2
        )
    )

    assert send_file.call_count == 2
    limited = [n for n, r in result.skipped if "attachment limit" in r]
    assert len(limited) == 2


@pytest.mark.anyio
async def test_628_zip_build_error_falls_back_to_archive(
    tmp_path: Path, monkeypatch
) -> None:
    """#628: an unexpected error building the zip falls back to the #600
    archive (and does not abort the whole delivery)."""
    outbox = tmp_path / ".untether-outbox"
    (outbox / "boom").mkdir(parents=True)
    (outbox / "boom" / "x.txt").write_text("x", encoding="utf-8")

    def _raise(*a, **k):
        raise OSError("zip blew up")

    monkeypatch.setattr("untether.telegram.outbox_delivery._zip_skipped_dir", _raise)
    send_file = AsyncMock()
    result = await deliver_outbox_files(
        **_deliver_kwargs(tmp_path, send_file=send_file, deliver_directories="zip")
    )

    send_file.assert_not_called()
    assert (outbox / ".skipped" / "boom").is_dir()
    reasons = dict(result.skipped)
    assert "zip failed" in reasons["boom"]


# -- #924: run-scoped freshness, stale quarantine, overflow --


def _classify(tmp_path: Path, **overrides) -> od.OutboxScan:
    kwargs = {
        "outbox_dir": ".untether-outbox",
        "deny_globs": DENY_GLOBS,
        "max_download_bytes": MAX_BYTES,
        "max_files": 10,
        "send_after": None,
        "archive_before": None,
        "seen": None,
    }
    kwargs.update(overrides)
    return od.classify_outbox(tmp_path, **kwargs)


def _future() -> float:
    """A cutoff after every file the test just wrote (ctime can't be
    backdated, so "stale" means "before the cutoff")."""
    return time.time() + 10


def test_924_entry_changed_at_uses_max_of_mtime_and_ctime() -> None:
    def _st(mtime: float, ctime: float) -> os.stat_result:
        return os.stat_result((0o100644, 0, 0, 1, 0, 0, 5, 0, mtime, ctime))

    assert od.entry_changed_at(_st(100.0, 200.0)) == 200.0
    assert od.entry_changed_at(_st(300.0, 200.0)) == 300.0


def test_924_preexisting_files_classified_stale_and_not_sent(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    for n in range(3):
        (outbox / f"old-{n}.md").write_text("old", encoding="utf-8")
    cutoff = _future()
    scan = _classify(tmp_path, send_after=cutoff, archive_before=cutoff)
    assert scan.files == []
    assert sorted(n for n, _ in scan.stale) == ["old-0.md", "old-1.md", "old-2.md"]
    assert scan.skipped == []


def test_924_moved_in_file_with_old_mtime_is_fresh(tmp_path: Path) -> None:
    """`mv`/`cp -p` keep mtime; ctime is "now", so the file still counts."""
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    p = outbox / "report.md"
    p.write_text("report", encoding="utf-8")
    os.utime(p, (0, 0))  # mtime 1970, ctime now
    since = time.time() - 60
    scan = _classify(tmp_path, send_after=since, archive_before=since)
    assert [f.abs_path.name for f in scan.files] == ["report.md"]
    assert scan.stale == []


@pytest.mark.anyio
async def test_924_stale_archived_to_skipped_once(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "old.md").write_text("old", encoding="utf-8")
    send_file = AsyncMock()
    cutoff = _future()
    result = await deliver_outbox_files(
        **_deliver_kwargs(
            tmp_path, send_file=send_file, send_after=cutoff, archive_before=cutoff
        )
    )
    send_file.assert_not_called()
    assert [n for n, _ in result.stale] == ["old.md"]
    assert result.stale_archived_to == ".untether-outbox/.skipped/"
    assert (outbox / ".skipped" / "old.md").is_file()
    assert not (outbox / "old.md").exists()

    again = await deliver_outbox_files(
        **_deliver_kwargs(
            tmp_path, send_file=send_file, send_after=cutoff, archive_before=cutoff
        )
    )
    assert again.stale == []
    assert again.skipped == []
    send_file.assert_not_called()


@pytest.mark.anyio
async def test_924_cleanup_false_leaves_stale_in_place(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "old.md").write_text("old", encoding="utf-8")
    cutoff = _future()
    result = await deliver_outbox_files(
        **_deliver_kwargs(
            tmp_path, cleanup=False, send_after=cutoff, archive_before=cutoff
        )
    )
    assert [n for n, _ in result.stale] == ["old.md"]
    assert result.stale_archived_to is None
    assert (outbox / "old.md").is_file()
    assert not (outbox / ".skipped").exists()


@pytest.mark.anyio
async def test_924_policy_send_none_cutoffs_is_legacy(tmp_path: Path) -> None:
    """Kill switch: no cutoffs = no age classification (old files sent)."""
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    p = outbox / "old.md"
    p.write_text("old", encoding="utf-8")
    os.utime(p, (0, 0))
    send_file = AsyncMock()
    result = await deliver_outbox_files(
        **_deliver_kwargs(tmp_path, send_file=send_file)
    )
    send_file.assert_called_once()
    assert result.stale == []


def test_924_between_cutoffs_neither_sent_nor_archived(tmp_path: Path) -> None:
    """Concurrent-run guard: newer than the oldest active same-root run's
    start (archive cutoff) but older than this run's own start (send
    cutoff) → left alone for the run that owns it."""
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "theirs.md").write_text("x", encoding="utf-8")
    scan = _classify(tmp_path, send_after=_future(), archive_before=time.time() - 60)
    assert scan.files == []
    assert scan.stale == []
    assert scan.skipped == []
    assert scan.overflow == []


@pytest.mark.anyio
async def test_924_cap_applies_to_fresh_only_and_dirs_still_classified(
    tmp_path: Path,
) -> None:
    """Regression for the old `break` at max_files: entries sorting after
    the 10th file (here `zz/`) were never classified or archived."""
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    for n in range(12):
        (outbox / f"f{n:02d}.md").write_text("x", encoding="utf-8")
    (outbox / "zz").mkdir()
    send_file = AsyncMock()
    since = time.time() - 60
    result = await deliver_outbox_files(
        **_deliver_kwargs(
            tmp_path, send_file=send_file, send_after=since, archive_before=since
        )
    )
    assert send_file.call_count == 10
    assert result.overflow == ["f10.md", "f11.md"]
    assert result.overflow_limit == 10
    assert (outbox / ".skipped" / "zz").is_dir()


@pytest.mark.anyio
async def test_924_overflow_archived_when_cleanup(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    for n in range(3):
        (outbox / f"f{n}.md").write_text("x", encoding="utf-8")
    since = time.time() - 60
    result = await deliver_outbox_files(
        **_deliver_kwargs(tmp_path, max_files=1, send_after=since, archive_before=since)
    )
    assert result.overflow == ["f1.md", "f2.md"]
    assert result.overflow_archived_to == ".untether-outbox/.skipped/"
    assert (outbox / ".skipped" / "f1.md").is_file()
    assert (outbox / ".skipped" / "f2.md").is_file()


@pytest.mark.anyio
async def test_924_stale_dir_archived_not_zipped_in_zip_mode(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    (outbox / "shots").mkdir(parents=True)
    (outbox / "shots" / "a.png").write_bytes(b"png")
    send_file = AsyncMock()
    cutoff = _future()
    result = await deliver_outbox_files(
        **_deliver_kwargs(
            tmp_path,
            send_file=send_file,
            deliver_directories="zip",
            send_after=cutoff,
            archive_before=cutoff,
        )
    )
    send_file.assert_not_called()
    assert [n for n, _ in result.stale] == ["shots"]
    assert (outbox / ".skipped" / "shots" / "a.png").is_file()


def test_924_graveyard_file_collision_keeps_extension(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    (outbox / ".skipped").mkdir(parents=True)
    (outbox / ".skipped" / "plan.md").write_text("first", encoding="utf-8")
    (outbox / "plan.md").write_text("second", encoding="utf-8")
    rel = od._move_to_graveyard(tmp_path, ".untether-outbox", "plan.md")
    assert rel == ".untether-outbox/.skipped/plan_1.md"
    assert (outbox / ".skipped" / "plan_1.md").read_text(encoding="utf-8") == "second"


def test_924_graveyard_never_archived_into_itself(tmp_path: Path) -> None:
    """Amendment 2: the graveyard is checked before freshness, so `.skipped/`
    can never be classified stale and moved into itself."""
    outbox = tmp_path / ".untether-outbox"
    (outbox / ".skipped").mkdir(parents=True)
    (outbox / ".skipped" / "old.md").write_text("x", encoding="utf-8")
    cutoff = _future()
    scan = _classify(tmp_path, send_after=cutoff, archive_before=cutoff)
    assert scan.stale == []
    assert scan.skipped == []


def test_924_stale_symlink_moved_not_followed(tmp_path: Path) -> None:
    """Amendment 2: a stale symlink is renamed itself — its target is never
    followed, copied or moved."""
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "link.txt").symlink_to(outside)
    cutoff = _future()
    scan = _classify(tmp_path, send_after=cutoff, archive_before=cutoff)
    assert [n for n, _ in scan.stale] == ["link.txt"]
    rel = od._archive_entries(tmp_path, ".untether-outbox", ["link.txt"])
    assert rel == ".untether-outbox/.skipped/"
    moved = outbox / ".skipped" / "link.txt"
    assert moved.is_symlink()
    assert outside.read_text(encoding="utf-8") == "secret"


def test_924_symlinked_graveyard_refused(tmp_path: Path) -> None:
    """A `.skipped` that is a symlink would move files outside the project."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / ".skipped").symlink_to(elsewhere, target_is_directory=True)
    (outbox / "old.md").write_text("x", encoding="utf-8")
    assert od._archive_entries(tmp_path, ".untether-outbox", ["old.md"]) is None
    assert (outbox / "old.md").is_file()
    assert list(elsewhere.iterdir()) == []


@pytest.mark.anyio
async def test_924_seen_set_prevents_resend_with_cleanup_false(tmp_path: Path) -> None:
    outbox = tmp_path / ".untether-outbox"
    outbox.mkdir()
    (outbox / "a.md").write_text("a", encoding="utf-8")
    (outbox / "dir").mkdir()
    send_file = AsyncMock()
    seen: set = set()
    since = time.time() - 60
    first = await deliver_outbox_files(
        **_deliver_kwargs(
            tmp_path,
            send_file=send_file,
            cleanup=False,
            send_after=since,
            archive_before=since,
            seen=seen,
        )
    )
    assert len(first.sent) == 1
    assert [n for n, _ in first.skipped] == ["dir"]
    second = await deliver_outbox_files(
        **_deliver_kwargs(
            tmp_path,
            send_file=send_file,
            cleanup=False,
            send_after=since,
            archive_before=since,
            seen=seen,
        )
    )
    assert second.sent == []
    assert second.skipped == []
    assert send_file.call_count == 1


def test_924_registry_oldest_active_since(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    assert od.oldest_active_since(root_a, 500.0) == 500.0
    with od.outbox_run_scope(root_a, 100.0):
        with od.outbox_run_scope(root_a, 200.0):
            assert od.oldest_active_since(root_a, 300.0) == 100.0
            assert od.oldest_active_since(root_b, 300.0) == 300.0
        assert od.oldest_active_since(root_a, 300.0) == 100.0
    assert od.oldest_active_since(root_a, 300.0) == 300.0

    with pytest.raises(RuntimeError), od.outbox_run_scope(root_a, 50.0):
        raise RuntimeError("boom")
    assert od.oldest_active_since(root_a, 300.0) == 300.0

    with od.outbox_run_scope(None, 10.0):  # no cwd → registers nothing
        assert od.oldest_active_since(root_a, 300.0) == 300.0


def test_924_registry_key_matches_delivery_root(tmp_path: Path) -> None:
    """Amendment 1: the executor registers with the run's cwd and delivery
    queries with ``get_run_base_dir()`` (the same cwd). A symlinked project
    path resolves to the same key; a separate worktree (`@branch` cwd) stays
    isolated from the main checkout."""
    from untether.utils.paths import (
        get_run_base_dir,
        reset_run_base_dir,
        set_run_base_dir,
    )

    real = tmp_path / "project"
    real.mkdir()
    link = tmp_path / "project-link"
    link.symlink_to(real, target_is_directory=True)
    worktree = tmp_path / "worktrees" / "feature"
    worktree.mkdir(parents=True)

    token = set_run_base_dir(link)
    try:
        with od.outbox_run_scope(get_run_base_dir(), 100.0):
            assert od.oldest_active_since(real, 300.0) == 100.0
            assert od.oldest_active_since(get_run_base_dir(), 300.0) == 100.0
            assert od.oldest_active_since(worktree, 300.0) == 300.0
    finally:
        reset_run_base_dir(token)


# -- #924 review: the outbox directory itself must stay inside the project --


def _make_outside_dir(tmp_path: Path) -> tuple[Path, Path]:
    """A project root plus an unrelated directory (think ~/Downloads)."""
    project = tmp_path / "project"
    project.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    for n in range(3):
        (downloads / f"personal-{n}.pdf").write_text("mine", encoding="utf-8")
    return project, downloads


@pytest.mark.anyio
async def test_924_symlinked_outbox_dir_never_scanned_or_archived(
    tmp_path: Path,
) -> None:
    from structlog.testing import capture_logs

    project, downloads = _make_outside_dir(tmp_path)
    (project / ".untether-outbox").symlink_to(downloads, target_is_directory=True)
    send_file = AsyncMock()
    cutoff = _future()
    with capture_logs() as logs:
        result = await deliver_outbox_files(
            **_deliver_kwargs(
                project, send_file=send_file, send_after=cutoff, archive_before=cutoff
            )
        )
    send_file.assert_not_called()
    assert result.stale == []
    assert result.sent == []
    assert not (downloads / ".skipped").exists()
    assert sorted(p.name for p in downloads.iterdir()) == [
        "personal-0.pdf",
        "personal-1.pdf",
        "personal-2.pdf",
    ]
    warns = [e for e in logs if e["event"] == "outbox.outside_root"]
    assert warns and warns[0]["log_level"] == "warning"


def test_924_symlinked_outbox_dir_inside_project_also_refused(tmp_path: Path) -> None:
    """Any symlink on the way to the outbox is refused, even one that
    resolves inside the project — `.skipped` moves must stay put."""
    real = tmp_path / "real-outbox"
    real.mkdir()
    (real / "old.md").write_text("x", encoding="utf-8")
    (tmp_path / ".untether-outbox").symlink_to(real, target_is_directory=True)
    cutoff = _future()
    scan = _classify(tmp_path, send_after=cutoff, archive_before=cutoff)
    assert scan.stale == []
    assert scan.files == []


def test_924_symlinked_parent_of_outbox_dir_refused(tmp_path: Path) -> None:
    project, downloads = _make_outside_dir(tmp_path)
    (downloads / "out").mkdir()
    (downloads / "out" / "old.md").write_text("x", encoding="utf-8")
    (project / "link").symlink_to(downloads, target_is_directory=True)
    cutoff = _future()
    scan = _classify(
        project, outbox_dir="link/out", send_after=cutoff, archive_before=cutoff
    )
    assert scan.stale == []
    assert scan.files == []


def test_924_dotdot_outbox_dir_refused(tmp_path: Path) -> None:
    project, downloads = _make_outside_dir(tmp_path)
    cutoff = _future()
    scan = _classify(
        project, outbox_dir="../Downloads", send_after=cutoff, archive_before=cutoff
    )
    assert scan.stale == []
    assert scan.files == []


def test_924_move_to_graveyard_refuses_symlinked_outbox(tmp_path: Path) -> None:
    project, downloads = _make_outside_dir(tmp_path)
    (project / ".untether-outbox").symlink_to(downloads, target_is_directory=True)
    with pytest.raises(OSError, match="refusing to archive"):
        od._move_to_graveyard(project, ".untether-outbox", "personal-0.pdf")
    assert od._archive_entries(project, ".untether-outbox", ["personal-1.pdf"]) is None
    assert not (downloads / ".skipped").exists()
    assert (downloads / "personal-0.pdf").is_file()
    assert (downloads / "personal-1.pdf").is_file()


def test_924_nested_real_outbox_dir_still_works(tmp_path: Path) -> None:
    outbox = tmp_path / "out" / "box"
    outbox.mkdir(parents=True)
    (outbox / "old.md").write_text("x", encoding="utf-8")
    cutoff = _future()
    scan = _classify(
        tmp_path, outbox_dir="out/box", send_after=cutoff, archive_before=cutoff
    )
    assert [n for n, _ in scan.stale] == ["old.md"]
