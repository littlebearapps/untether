"""Tests for the /browse file browser command."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

import untether.telegram.commands.browse as browse_mod
from untether.config import ConfigError
from untether.telegram.commands.browse import (
    _MAX_ENTRIES,
    _PATH_REGISTRY,
    BrowseCommand,
    _format_listing,
    _format_size,
    _get_project_root,
    _list_directory,
    _read_file_preview,
    _register_path,
    _resolve_path,
)

CHAT = 1
ROOT_PATCH = "untether.telegram.commands.browse._get_project_root"


@pytest.fixture(autouse=True)
def _clear_registry():
    yield
    _PATH_REGISTRY.clear()


def _link(link: Path, target: Path | str) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")


def _make_ctx(args_text: str, *, chat_id: int | str = CHAT, globs=None):
    """Build a minimal CommandContext-like object for testing."""
    ctx = MagicMock()
    ctx.args_text = args_text
    ctx.executor = AsyncMock()
    ctx.executor.send = AsyncMock(return_value=None)
    ctx.message = MagicMock()
    ctx.message.channel_id = chat_id
    if globs is not None:
        ctx.file_deny_globs = globs
    return ctx


def _root_ctx(
    *,
    chat_root: Path | None = None,
    default_root: Path | None = None,
    chat_error: Exception | None = None,
):
    """A ctx whose runtime resolves a chat-bound and/or default project."""
    ctx = _make_ctx("")

    def default_context_for_chat(chat_id):
        if chat_error is not None:
            raise chat_error
        if chat_root is None:
            return None
        return SimpleNamespace(project="chat", branch=None)

    def resolve_run_cwd(run_context):
        if run_context.project == "chat":
            return chat_root
        if run_context.project == "dflt":
            return default_root
        return None

    ctx.runtime = SimpleNamespace(
        default_context_for_chat=default_context_for_chat,
        resolve_run_cwd=resolve_run_cwd,
        default_project="dflt" if default_root is not None else None,
    )
    return ctx


def _sent_text(ctx) -> str:
    return ctx.executor.send.call_args[0][0].text


def _buttons(ctx) -> list[dict]:
    msg = ctx.executor.send.call_args[0][0]
    return [b for row in msg.extra["reply_markup"]["inline_keyboard"] for b in row]


class TestPathRegistry:
    def test_register_returns_id(self):
        pid = _register_path(CHAT, "/some/path")
        assert isinstance(pid, int)
        assert pid > 0

    def test_same_path_same_id(self):
        pid1 = _register_path(CHAT, "/some/path")
        pid2 = _register_path(CHAT, "/some/path")
        assert pid1 == pid2

    def test_different_paths_different_ids(self):
        pid1 = _register_path(CHAT, "/path/a")
        pid2 = _register_path(CHAT, "/path/b")
        assert pid1 != pid2

    def test_resolve_returns_path(self):
        pid = _register_path(CHAT, "/some/path")
        assert _resolve_path(CHAT, pid) == "/some/path"

    def test_resolve_missing_returns_none(self):
        assert _resolve_path(CHAT, 99999) is None


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
        text, buttons = _format_listing(tmp_path, tmp_path, dirs, files, chat_id=CHAT)
        assert "/" in text
        assert "1 dirs" in text
        assert "1 files" in text
        # No ".." button at root
        assert not any("📂 .." in b[0]["text"] for b in buttons if buttons)

    def test_parent_button_in_subdir(self, tmp_path):
        subdir = tmp_path / "src"
        subdir.mkdir()
        dirs, files = _list_directory(subdir, tmp_path)
        text, buttons = _format_listing(subdir, tmp_path, dirs, files, chat_id=CHAT)
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

    def test_no_cwd_fallback(self, tmp_path, monkeypatch):
        # #389: was ``Path.cwd()`` ($HOME under systemd).
        monkeypatch.chdir(tmp_path)
        with patch(
            "untether.telegram.commands.browse.get_run_base_dir", return_value=None
        ):
            assert _get_project_root() is None
            assert _get_project_root(_root_ctx()) is None


class TestRegistryTrimming:
    def test_trims_old_entries(self):
        import untether.telegram.commands.browse as mod

        old_max = mod._MAX_REGISTRY
        mod._MAX_REGISTRY = 3  # type: ignore[assignment]
        try:
            first = _register_path(CHAT, "/a")
            _register_path(CHAT, "/b")
            _register_path(CHAT, "/c")
            newest = _register_path(CHAT, "/d")  # Should trigger trim
            assert len(_PATH_REGISTRY) <= 3
            # The first-inserted key is evicted, the newest remains.
            assert _resolve_path(CHAT, first) is None
            assert _resolve_path(CHAT, newest) == "/d"
        finally:
            mod._MAX_REGISTRY = old_max


class TestFormatListingTruncation:
    def test_truncates_many_entries(self, tmp_path):
        # Create more entries than _MAX_ENTRIES
        for i in range(_MAX_ENTRIES + 5):
            (tmp_path / f"file_{i:03d}.py").write_text(f"content {i}")
        dirs, files = _list_directory(tmp_path)
        text, buttons = _format_listing(tmp_path, tmp_path, dirs, files, chat_id=CHAT)
        assert "not shown" in text

    def test_dirs_only_listing(self, tmp_path):
        (tmp_path / "dirA").mkdir()
        (tmp_path / "dirB").mkdir()
        dirs, files = _list_directory(tmp_path)
        text, buttons = _format_listing(tmp_path, tmp_path, dirs, files, chat_id=CHAT)
        assert "2 dirs" in text
        assert "files" not in text

    def test_empty_dir(self, tmp_path):
        dirs, files = _list_directory(tmp_path)
        text, buttons = _format_listing(tmp_path, tmp_path, dirs, files, chat_id=CHAT)
        assert len(buttons) == 0


class TestBrowseCommandHandle:
    @pytest.fixture
    def cmd(self):
        return BrowseCommand()

    def _make_ctx(self, args_text: str, *, chat_id: int = CHAT):
        return _make_ctx(args_text, chat_id=chat_id)

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
        # File preview now sent via executor with inline keyboard
        assert result is None
        ctx.executor.send.assert_called_once()
        sent_msg = ctx.executor.send.call_args[0][0]
        assert "Hello World" in sent_msg.text
        # Language detection: .md -> markdown
        assert "```markdown" in sent_msg.text
        # Back button present
        keyboard = sent_msg.extra["reply_markup"]["inline_keyboard"]
        assert any("Back" in btn["text"] for row in keyboard for btn in row)

    @pytest.mark.anyio
    async def test_browse_dir_by_id(self, cmd, tmp_path):
        (tmp_path / "item.txt").write_text("stuff")
        pid = _register_path(CHAT, str(tmp_path))
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
        pid = _register_path(CHAT, str(f))
        ctx = self._make_ctx(f"f:{pid}")
        with patch(
            "untether.telegram.commands.browse._get_project_root", return_value=tmp_path
        ):
            result = await cmd.handle(ctx)
        # File preview sent via executor
        assert result is None
        ctx.executor.send.assert_called_once()
        sent_msg = ctx.executor.send.call_args[0][0]
        assert "secret data" in sent_msg.text

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
        assert "No project directory for this chat" in result.text

    @pytest.mark.anyio
    async def test_path_outside_project(self, cmd, tmp_path):
        ctx = self._make_ctx("../../etc/passwd")
        with patch(
            "untether.telegram.commands.browse._get_project_root", return_value=tmp_path
        ):
            result = await cmd.handle(ctx)
        assert result is not None
        assert result.text == "Path outside project."

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
    async def test_file_preview_python_language(self, cmd, tmp_path):
        f = tmp_path / "app.py"
        f.write_text("print('hello')")
        ctx = self._make_ctx("app.py")
        with patch(
            "untether.telegram.commands.browse._get_project_root", return_value=tmp_path
        ):
            await cmd.handle(ctx)
        sent_msg = ctx.executor.send.call_args[0][0]
        assert "```python" in sent_msg.text

    @pytest.mark.anyio
    async def test_file_preview_no_lang_for_unknown_ext(self, cmd, tmp_path):
        f = tmp_path / "data.csv"
        f.write_text("a,b,c")
        ctx = self._make_ctx("data.csv")
        with patch(
            "untether.telegram.commands.browse._get_project_root", return_value=tmp_path
        ):
            await cmd.handle(ctx)
        sent_msg = ctx.executor.send.call_args[0][0]
        # Unknown extension -> bare code block
        assert "```\n" in sent_msg.text

    @pytest.mark.anyio
    async def test_file_preview_back_button_points_to_parent(self, cmd, tmp_path):
        sub = tmp_path / "src"
        sub.mkdir()
        f = sub / "main.py"
        f.write_text("code")
        pid = _register_path(CHAT, str(f))
        ctx = self._make_ctx(f"f:{pid}")
        with patch(
            "untether.telegram.commands.browse._get_project_root", return_value=tmp_path
        ):
            await cmd.handle(ctx)
        sent_msg = ctx.executor.send.call_args[0][0]
        keyboard = sent_msg.extra["reply_markup"]["inline_keyboard"]
        back_btn = keyboard[0][0]
        assert "Back" in back_btn["text"]
        assert back_btn["callback_data"].startswith("browse:d:")

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


# ---------------------------------------------------------------------------
# #389 group C — root resolution (explicit only)
# ---------------------------------------------------------------------------


class TestProjectRootExplicit:
    @pytest.fixture(autouse=True)
    def _no_run_base(self):
        with patch(
            "untether.telegram.commands.browse.get_run_base_dir", return_value=None
        ):
            yield

    def test_root_from_chat_bound_project(self, tmp_path):
        assert _get_project_root(_root_ctx(chat_root=tmp_path)) == tmp_path.resolve()

    def test_root_from_default_project(self, tmp_path):
        assert _get_project_root(_root_ctx(default_root=tmp_path)) == tmp_path.resolve()

    def test_root_chat_binding_beats_default_project(self, tmp_path):
        chat = tmp_path / "chat"
        dflt = tmp_path / "dflt"
        chat.mkdir()
        dflt.mkdir()
        ctx = _root_ctx(chat_root=chat, default_root=dflt)
        assert _get_project_root(ctx) == chat.resolve()

    def test_root_resolve_error_falls_through(self):
        with capture_logs() as logs:
            root = _get_project_root(_root_ctx(chat_error=ConfigError("boom")))
        assert root is None
        assert any(e["event"] == "browse.project_root.error" for e in logs)

    def test_root_missing_dir_returns_none(self, tmp_path):
        assert _get_project_root(_root_ctx(chat_root=tmp_path / "gone")) is None

    def test_root_is_resolved(self, tmp_path):
        (tmp_path / "real").mkdir()
        _link(tmp_path / "link", Path("real"))
        root = _get_project_root(_root_ctx(chat_root=tmp_path / "link"))
        assert root == (tmp_path / "real").resolve()

    @pytest.mark.anyio
    async def test_handle_no_root_message(self):
        ctx = _root_ctx()
        ctx.args_text = ""
        with capture_logs() as logs:
            result = await BrowseCommand().handle(ctx)
        assert result is not None
        assert "default_project" in result.text
        assert "chat_id" in result.text
        ctx.executor.send.assert_not_called()
        assert any(e["event"] == "browse.no_project_root" for e in logs)


# ---------------------------------------------------------------------------
# #389 group D — direct path arguments
# ---------------------------------------------------------------------------


async def _handle(args: str, root: Path, **kwargs):
    ctx = _make_ctx(args, **kwargs)
    with patch(ROOT_PATCH, return_value=root):
        result = await BrowseCommand().handle(ctx)
    return ctx, result


class TestDirectArgs:
    @pytest.mark.anyio
    async def test_arg_env_denied(self, tmp_path):
        (tmp_path / ".env").write_text("SECRET=hunter2")
        with capture_logs() as logs:
            ctx, result = await _handle(".env", tmp_path)
        assert result is not None
        assert result.text.startswith("Path denied by rule")
        ctx.executor.send.assert_not_called()
        assert "hunter2" not in result.text
        assert "hunter2" not in repr(logs)
        assert any(
            e["event"] == "browse.path_denied" and e["reason"] == "denied" for e in logs
        )

    @pytest.mark.anyio
    async def test_arg_untether_config_hidden(self, tmp_path):
        (tmp_path / ".untether").mkdir()
        (tmp_path / ".untether" / "untether.toml").write_text('bot_token = "FAKE"')
        ctx, result = await _handle(".untether/untether.toml", tmp_path)
        assert result is not None
        assert result.text == "Hidden paths can't be browsed."
        ctx.executor.send.assert_not_called()

    @pytest.mark.anyio
    @pytest.mark.parametrize("arg", [".ssh/id_rsa", "key.pem", "src/../.env"])
    async def test_arg_secret_paths_denied(self, tmp_path, arg):
        (tmp_path / ".ssh").mkdir()
        (tmp_path / ".ssh" / "id_rsa").write_text("PRIVATE")
        (tmp_path / "key.pem").write_text("-----BEGIN FAKE-----")
        (tmp_path / "src").mkdir()
        (tmp_path / ".env").write_text("x")
        ctx, result = await _handle(arg, tmp_path)
        assert result is not None
        assert result.text.startswith("Path denied by rule")
        ctx.executor.send.assert_not_called()

    @pytest.mark.anyio
    async def test_denied_reply_same_when_missing(self, tmp_path):
        _, missing = await _handle(".env", tmp_path)
        (tmp_path / ".env").write_text("x")
        _, present = await _handle(".env", tmp_path)
        assert missing is not None and present is not None
        assert missing.text == present.text

    @pytest.mark.anyio
    async def test_arg_github_allowed(self, tmp_path):
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text("on: push")
        ctx, result = await _handle(".github/workflows/ci.yml", tmp_path)
        assert result is None
        assert "on: push" in _sent_text(ctx)

    @pytest.mark.anyio
    async def test_arg_symlink_escape_logs_warning(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        (tmp_path / "outside").mkdir()
        (tmp_path / "outside" / "x.txt").write_text("leak")
        _link(root / "link", tmp_path / "outside")
        with capture_logs() as logs:
            ctx, result = await _handle("link/x.txt", root)
        assert result is not None
        assert result.text == "Path outside project."
        escapes = [e for e in logs if e["event"] == "browse.path_escape_attempted"]
        assert escapes and escapes[0]["via"] == "arg"
        ctx.executor.send.assert_not_called()

    @pytest.mark.anyio
    async def test_arg_symlink_loop_clean_error(self, tmp_path):
        _link(tmp_path / "loop1", Path("loop2"))
        _link(tmp_path / "loop2", Path("loop1"))
        with capture_logs() as logs:
            ctx, result = await _handle("loop1/x", tmp_path)
        assert result is not None
        assert result.text == "Path could not be resolved (symlink loop?)."
        ctx.executor.send.assert_not_called()
        assert any(
            e["event"] == "browse.path_denied" and e["reason"] == "unresolvable"
            for e in logs
        )

    @pytest.mark.anyio
    async def test_arg_custom_deny_globs_from_context(self, tmp_path):
        (tmp_path / "notes.txt").write_text("n")
        (tmp_path / ".env").write_text("x")
        _, result = await _handle("notes.txt", tmp_path, globs=("**/*.txt",))
        assert result is not None
        assert result.text == "Path denied by rule: `**/*.txt`"
        _, result = await _handle(".env", tmp_path, globs=("**/*.txt",))
        assert result is not None
        assert result.text == "Hidden paths can't be browsed."

    @pytest.mark.anyio
    async def test_arg_defaults_when_context_lacks_field(self, tmp_path):
        (tmp_path / ".env").write_text("x")
        ctx = SimpleNamespace(
            args_text=".env",
            message=SimpleNamespace(channel_id=CHAT),
            executor=AsyncMock(),
        )
        with patch(ROOT_PATCH, return_value=tmp_path):
            result = await BrowseCommand().handle(ctx)  # type: ignore[arg-type]
        assert result is not None
        assert result.text == "Path denied by rule: `.env`"


# ---------------------------------------------------------------------------
# #389 group E — listings and callbacks
# ---------------------------------------------------------------------------


class TestListingAndCallbacks:
    @pytest.mark.anyio
    async def test_listing_hides_denied_and_hidden(self, tmp_path):
        (tmp_path / "key.pem").write_text("k")
        (tmp_path / ".env").write_text("e")
        (tmp_path / ".env.example").write_text("x")
        (tmp_path / ".github").mkdir()
        (tmp_path / "src").mkdir()
        ctx, result = await _handle("", tmp_path)
        assert result is None
        texts = [b["text"] for b in _buttons(ctx)]
        assert texts == ["📂 .github/", "📂 src/"]

    def test_listing_drops_escaping_symlink(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        (tmp_path / "outside").mkdir()
        _link(root / "home", tmp_path / "outside")
        (root / "ok").mkdir()
        dirs, files = _list_directory(root.resolve(), root.resolve())
        assert [d.name for d in dirs] == ["ok"]
        assert files == []

    def test_listing_skips_symlink_loop(self, tmp_path):
        _link(tmp_path / "loop1", Path("loop2"))
        _link(tmp_path / "loop2", Path("loop1"))
        (tmp_path / "a.txt").write_text("a")
        dirs, files = _list_directory(tmp_path.resolve(), tmp_path.resolve())
        assert dirs == []
        assert [f.name for f in files] == ["a.txt"]

    def test_listing_env_example_hidden(self, tmp_path):
        # #389 Decision 6 pin: root .env.example gets no button.
        (tmp_path / ".env.example").write_text("X=1")
        dirs, files = _list_directory(tmp_path.resolve(), tmp_path.resolve())
        assert dirs == [] and files == []

    @pytest.mark.anyio
    async def test_listing_keeps_in_root_symlink(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("a")
        _link(tmp_path / "docs-link", Path("src"))
        ctx, _ = await _handle("", tmp_path)
        buttons = {b["text"]: b["callback_data"] for b in _buttons(ctx)}
        assert "📂 docs-link/" in buttons
        pid = int(buttons["📂 docs-link/"].rsplit(":", 1)[1])
        assert _resolve_path(CHAT, pid) == str((tmp_path / "src").resolve())

    @pytest.mark.anyio
    async def test_callback_dir_symlink_escape_rejected(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        (tmp_path / "outside").mkdir()
        (tmp_path / "outside" / "x.txt").write_text("leak")
        _link(root / "link", tmp_path / "outside")
        pid = _register_path(CHAT, str(root.resolve() / "link"))
        with capture_logs() as logs:
            ctx, result = await _handle(f"d:{pid}", root)
        assert result is not None
        assert result.text == "Path outside project."
        ctx.executor.send.assert_not_called()
        escapes = [e for e in logs if e["event"] == "browse.path_escape_attempted"]
        assert escapes and escapes[0]["via"] == "callback"

    @pytest.mark.anyio
    async def test_callback_file_denied_rechecked(self, tmp_path):
        (tmp_path / ".env").write_text("SECRET")
        pid = _register_path(CHAT, str(tmp_path.resolve() / ".env"))
        ctx, result = await _handle(f"f:{pid}", tmp_path)
        assert result is not None
        assert result.text == "Path denied by rule: `.env`"
        ctx.executor.send.assert_not_called()

    @pytest.mark.anyio
    async def test_callback_parent_of_root_rejected(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        pid = _register_path(CHAT, str(tmp_path.resolve()))
        _, result = await _handle(f"d:{pid}", root)
        assert result is not None
        assert result.text == "Path outside project."

    @pytest.mark.anyio
    async def test_listing_no_parent_button_at_root(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        ctx, _ = await _handle("", tmp_path)
        assert not any(b["text"] == "📂 .." for b in _buttons(ctx))


# ---------------------------------------------------------------------------
# #389 group F — per-chat registry (#210)
# ---------------------------------------------------------------------------


class TestPerChatRegistry:
    @pytest.mark.anyio
    async def test_registry_id_from_other_chat_expired(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("a")
        pid = _register_path(1, str(f.resolve()))
        _, result = await _handle(f"f:{pid}", tmp_path, chat_id=2)
        assert result is not None
        assert result.text == "Path expired. Use /browse to start over."
        ctx, result = await _handle(f"f:{pid}", tmp_path, chat_id=1)
        assert result is None
        assert "a.txt" in _sent_text(ctx)

    def test_registry_same_path_different_chats_different_ids(self):
        assert _register_path(1, "/p") != _register_path(2, "/p")

    def test_registry_dedupes_within_chat(self):
        assert _register_path(1, "/p") == _register_path(1, "/p")

    def test_callback_data_fits_64_bytes(self, monkeypatch):
        monkeypatch.setattr(browse_mod, "_PATH_COUNTER", 10**12)
        pid = _register_path(-1003953881142, "/some/very/long/path" * 10)
        assert len(f"browse:d:{pid}".encode()) <= 64
