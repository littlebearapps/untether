"""Tests for the /browse file browser command."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from untether.telegram.commands.browse import (
    BrowseCommand,
    _MAX_ENTRIES,
    _PATH_REGISTRY,
    _format_listing,
    _format_size,
    _get_project_root,
    _list_directory,
    _read_file_preview,
    _register_path,
    _resolve_path,
)


@pytest.fixture(autouse=True)
def _clear_registry():
    yield
    _PATH_REGISTRY.clear()


class TestPathRegistry:
    def test_register_returns_id(self):
        pid = _register_path("/some/path")
        assert isinstance(pid, int)
        assert pid > 0

    def test_same_path_same_id(self):
        pid1 = _register_path("/some/path")
        pid2 = _register_path("/some/path")
        assert pid1 == pid2

    def test_different_paths_different_ids(self):
        pid1 = _register_path("/path/a")
        pid2 = _register_path("/path/b")
        assert pid1 != pid2

    def test_resolve_returns_path(self):
        pid = _register_path("/some/path")
        assert _resolve_path(pid) == "/some/path"

    def test_resolve_missing_returns_none(self):
        assert _resolve_path(99999) is None


class TestFormatSize:
    def test_bytes(self):
        assert _format_size(100) == "100b"

    def test_kilobytes(self):
        assert _format_size(2048) == "2k"

    def test_megabytes(self):
        assert _format_size(1048576) == "1m"


class TestListDirectory:
    def test_lists_files_and_dirs(self, tmp_path):
        (tmp_path / "subdir").mkdir()
        (tmp_path / "file.py").write_text("hello")
        dirs, files = _list_directory(tmp_path)
        assert len(dirs) == 1
        assert dirs[0].name == "subdir"
        assert len(files) == 1
        assert files[0].name == "file.py"

    def test_skips_hidden(self, tmp_path):
        (tmp_path / ".hidden").mkdir()
        (tmp_path / ".secret").write_text("x")
        (tmp_path / "visible").write_text("y")
        dirs, files = _list_directory(tmp_path)
        assert len(dirs) == 0
        assert len(files) == 1
        assert files[0].name == "visible"

    def test_skips_pycache(self, tmp_path):
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "real_dir").mkdir()
        dirs, files = _list_directory(tmp_path)
        assert len(dirs) == 1
        assert dirs[0].name == "real_dir"

    def test_sorted_alphabetically(self, tmp_path):
        (tmp_path / "zebra.py").write_text("z")
        (tmp_path / "alpha.py").write_text("a")
        (tmp_path / "beta.py").write_text("b")
        _, files = _list_directory(tmp_path)
        names = [f.name for f in files]
        assert names == ["alpha.py", "beta.py", "zebra.py"]


class TestFormatListing:
    def test_basic_listing(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "README.md").write_text("# Hello")
        dirs, files = _list_directory(tmp_path)
        text, buttons = _format_listing(tmp_path, tmp_path, dirs, files)
        assert "/" in text
        assert "1 dirs" in text
        assert "1 files" in text
        # No ".." button at root
        assert not any("📂 .." in b[0]["text"] for b in buttons if buttons)

    def test_parent_button_in_subdir(self, tmp_path):
        subdir = tmp_path / "src"
        subdir.mkdir()
        dirs, files = _list_directory(subdir)
        text, buttons = _format_listing(subdir, tmp_path, dirs, files)
        assert any("📂 .." in b[0]["text"] for b in buttons)


class TestReadFilePreview:
    def test_reads_text_file(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("line 1\nline 2\nline 3\n")
        preview = _read_file_preview(f)
        assert "line 1" in preview
        assert "line 2" in preview

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty"
        f.write_text("")
        assert _read_file_preview(f) == "(empty file)"

    def test_binary_file(self, tmp_path):
        f = tmp_path / "bin"
        f.write_bytes(b"\x00\x01\x02binary")
        preview = _read_file_preview(f)
        assert "binary file" in preview

    def test_truncates_long_files(self, tmp_path):
        f = tmp_path / "long.txt"
        f.write_text("\n".join(f"line {i}" for i in range(100)))
        preview = _read_file_preview(f)
        assert "total" in preview or "truncated" in preview


class TestGetProjectRoot:
    def test_returns_run_base_dir_when_set(self, tmp_path):
        with patch(
            "untether.telegram.commands.browse.get_run_base_dir", return_value=tmp_path
        ):
            assert _get_project_root() == tmp_path

    def test_falls_back_to_cwd(self):
        with patch(
            "untether.telegram.commands.browse.get_run_base_dir", return_value=None
        ):
            root = _get_project_root()
            assert root == Path.cwd()


class TestRegistryTrimming:
    def test_trims_old_entries(self):
        import untether.telegram.commands.browse as mod

        old_max = mod._MAX_REGISTRY
        mod._MAX_REGISTRY = 3
        try:
            _register_path("/a")
            _register_path("/b")
            _register_path("/c")
            _register_path("/d")  # Should trigger trim
            assert len(_PATH_REGISTRY) <= 3
        finally:
            mod._MAX_REGISTRY = old_max


class TestFormatListingTruncation:
    def test_truncates_many_entries(self, tmp_path):
        # Create more entries than _MAX_ENTRIES
        for i in range(_MAX_ENTRIES + 5):
            (tmp_path / f"file_{i:03d}.py").write_text(f"content {i}")
        dirs, files = _list_directory(tmp_path)
        text, buttons = _format_listing(tmp_path, tmp_path, dirs, files)
        assert "not shown" in text

    def test_dirs_only_listing(self, tmp_path):
        (tmp_path / "dirA").mkdir()
        (tmp_path / "dirB").mkdir()
        dirs, files = _list_directory(tmp_path)
        text, buttons = _format_listing(tmp_path, tmp_path, dirs, files)
        assert "2 dirs" in text
        assert "files" not in text

    def test_empty_dir(self, tmp_path):
        dirs, files = _list_directory(tmp_path)
        text, buttons = _format_listing(tmp_path, tmp_path, dirs, files)
        assert len(buttons) == 0


class TestBrowseCommandHandle:
    @pytest.fixture()
    def cmd(self):
        return BrowseCommand()

    def _make_ctx(self, args_text: str):
        """Build a minimal CommandContext-like object for testing."""
        from unittest.mock import AsyncMock, MagicMock

        ctx = MagicMock()
        ctx.args_text = args_text
        ctx.executor = AsyncMock()
        ctx.executor.send = AsyncMock(return_value=None)
        ctx.message = MagicMock()
        return ctx

    @pytest.mark.anyio
    async def test_browse_root_sends_keyboard(self, cmd, tmp_path):
        (tmp_path / "hello.py").write_text("x")
        ctx = self._make_ctx("")
        with patch(
            "untether.telegram.commands.browse._get_project_root", return_value=tmp_path
        ):
            result = await cmd.handle(ctx)
        # Dir with files -> sent via executor (returns None)
        assert result is None
        ctx.executor.send.assert_called_once()
        sent_msg = ctx.executor.send.call_args[0][0]
        assert "inline_keyboard" in sent_msg.extra.get("reply_markup", {})

    @pytest.mark.anyio
    async def test_browse_subdir_by_path(self, cmd, tmp_path):
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "main.py").write_text("code")
        ctx = self._make_ctx("src")
        with patch(
            "untether.telegram.commands.browse._get_project_root", return_value=tmp_path
        ):
            result = await cmd.handle(ctx)
        assert result is None  # Sent via executor
        ctx.executor.send.assert_called_once()

    @pytest.mark.anyio
    async def test_browse_file_by_path(self, cmd, tmp_path):
        f = tmp_path / "readme.md"
        f.write_text("# Hello World")
        ctx = self._make_ctx("readme.md")
        with patch(
            "untether.telegram.commands.browse._get_project_root", return_value=tmp_path
        ):
            result = await cmd.handle(ctx)
        assert result is not None
        assert "Hello World" in result.text

    @pytest.mark.anyio
    async def test_browse_dir_by_id(self, cmd, tmp_path):
        (tmp_path / "item.txt").write_text("stuff")
        pid = _register_path(str(tmp_path))
        ctx = self._make_ctx(f"d:{pid}")
        with patch(
            "untether.telegram.commands.browse._get_project_root", return_value=tmp_path
        ):
            result = await cmd.handle(ctx)
        assert result is None  # Dir with files, sent via executor

    @pytest.mark.anyio
    async def test_browse_file_by_id(self, cmd, tmp_path):
        f = tmp_path / "data.txt"
        f.write_text("secret data")
        pid = _register_path(str(f))
        ctx = self._make_ctx(f"f:{pid}")
        with patch(
            "untether.telegram.commands.browse._get_project_root", return_value=tmp_path
        ):
            result = await cmd.handle(ctx)
        assert result is not None
        assert "secret data" in result.text

    @pytest.mark.anyio
    async def test_expired_path_id(self, cmd, tmp_path):
        ctx = self._make_ctx("d:99999")
        with patch(
            "untether.telegram.commands.browse._get_project_root", return_value=tmp_path
        ):
            result = await cmd.handle(ctx)
        assert result is not None
        assert "expired" in result.text.lower()

    @pytest.mark.anyio
    async def test_not_found(self, cmd, tmp_path):
        ctx = self._make_ctx("nonexistent_thing")
        with patch(
            "untether.telegram.commands.browse._get_project_root", return_value=tmp_path
        ):
            result = await cmd.handle(ctx)
        assert result is not None
        assert "Not found" in result.text

    @pytest.mark.anyio
    async def test_no_project_root(self, cmd):
        ctx = self._make_ctx("")
        with patch(
            "untether.telegram.commands.browse._get_project_root", return_value=None
        ):
            result = await cmd.handle(ctx)
        assert result is not None
        assert "No project directory" in result.text

    @pytest.mark.anyio
    async def test_path_outside_project(self, cmd, tmp_path):
        ctx = self._make_ctx("../../etc/passwd")
        with patch(
            "untether.telegram.commands.browse._get_project_root", return_value=tmp_path
        ):
            result = await cmd.handle(ctx)
        assert result is not None
        assert "outside" in result.text.lower() or "Not found" in result.text

    @pytest.mark.anyio
    async def test_invalid_dir_id(self, cmd, tmp_path):
        ctx = self._make_ctx("d:abc")
        with patch(
            "untether.telegram.commands.browse._get_project_root", return_value=tmp_path
        ):
            result = await cmd.handle(ctx)
        assert result is not None
        assert "Invalid" in result.text

    @pytest.mark.anyio
    async def test_invalid_file_id(self, cmd, tmp_path):
        ctx = self._make_ctx("f:abc")
        with patch(
            "untether.telegram.commands.browse._get_project_root", return_value=tmp_path
        ):
            result = await cmd.handle(ctx)
        assert result is not None
        assert "Invalid" in result.text

    @pytest.mark.anyio
    async def test_empty_root_dir_returns_result(self, cmd, tmp_path):
        """Empty root dir has no buttons, returns CommandResult directly."""
        empty_root = tmp_path / "empty_root"
        empty_root.mkdir()
        ctx = self._make_ctx("")
        with patch(
            "untether.telegram.commands.browse._get_project_root",
            return_value=empty_root,
        ):
            result = await cmd.handle(ctx)
        assert result is not None
        assert "empty directory" in result.text
