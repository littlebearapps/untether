"""Confined, size-bounded reads from a local Telegram Bot API cache."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from untether.telegram.client_api import HttpBotClient


@pytest.mark.anyio
async def test_local_read_requires_explicit_data_directory(tmp_path: Path) -> None:
    target = tmp_path / "document.bin"
    target.write_bytes(b"must not be read")
    async with httpx.AsyncClient() as http:
        client = HttpBotClient(
            "token", base_url="http://localhost:8081", http_client=http
        )
        assert await client.download_file(str(target)) is None


@pytest.mark.anyio
@pytest.mark.parametrize("size", [0, 7, 8])
async def test_local_read_accepts_files_up_to_limit(tmp_path: Path, size: int) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    target = root / "document.bin"
    target.write_bytes(b"x" * size)
    async with httpx.AsyncClient() as http:
        client = HttpBotClient(
            "token",
            base_url="http://localhost:8081",
            bot_api_local_dir=root,
            max_download_bytes=8,
            http_client=http,
        )
        assert await client.download_file(str(target)) == b"x" * size


@pytest.mark.anyio
async def test_local_read_checks_size_before_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    target = root / "oversized.bin"
    target.write_bytes(b"x" * 9)

    def no_read(*args, **kwargs):
        pytest.fail("oversized file must be rejected before opening it")

    monkeypatch.setattr(Path, "open", no_read)
    async with httpx.AsyncClient() as http:
        client = HttpBotClient(
            "token",
            base_url="http://localhost:8081",
            bot_api_local_dir=root,
            max_download_bytes=8,
            http_client=http,
        )
        assert await client.download_file(str(target)) is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "escape", ["outside", "prefix", "traversal", "symlink", "parent_symlink"]
)
async def test_local_read_rejects_cache_escapes(
    tmp_path: Path, escape: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    sibling = tmp_path / "cache-other"
    sibling.mkdir()
    outside = sibling / "document.bin"
    outside.write_bytes(b"outside the allowed directory")
    if escape == "outside":
        target = outside
    elif escape == "prefix":
        target = sibling / "document.bin"
    elif escape == "traversal":
        target = root / ".." / "cache-other" / "document.bin"
    elif escape == "symlink":
        target = root / "linked.bin"
        target.symlink_to(outside)
    else:
        (root / "linked-dir").symlink_to(sibling, target_is_directory=True)
        target = root / "linked-dir" / "document.bin"

    def no_read(*args, **kwargs):
        pytest.fail("a path outside the configured cache must never be read")

    monkeypatch.setattr(Path, "open", no_read)
    async with httpx.AsyncClient() as http:
        client = HttpBotClient(
            "token",
            base_url="http://localhost:8081",
            bot_api_local_dir=root,
            http_client=http,
        )
        assert await client.download_file(str(target)) is None


@pytest.mark.anyio
async def test_local_read_allows_symlink_resolving_inside_cache(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    target = root / "document.bin"
    target.write_bytes(b"safe")
    link = root / "linked.bin"
    link.symlink_to(target)
    async with httpx.AsyncClient() as http:
        client = HttpBotClient(
            "token",
            base_url="http://localhost:8081",
            bot_api_local_dir=root,
            http_client=http,
        )
        assert await client.download_file(str(link)) == b"safe"


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["missing", "directory", "symlink_loop", "null_byte"])
async def test_local_read_fails_closed_on_invalid_paths(
    tmp_path: Path, kind: str
) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    target = root / "invalid"
    if kind == "directory":
        target.mkdir()
    elif kind == "symlink_loop":
        target.symlink_to(target)
    file_path = str(target) + ("\x00" if kind == "null_byte" else "")
    async with httpx.AsyncClient() as http:
        client = HttpBotClient(
            "token",
            base_url="http://localhost:8081",
            bot_api_local_dir=root,
            http_client=http,
        )
        assert await client.download_file(file_path) is None


@pytest.mark.anyio
async def test_public_api_never_reads_absolute_paths_even_with_cache(
    tmp_path: Path,
) -> None:
    target = tmp_path / "document.bin"
    target.write_bytes(b"local file")
    async with httpx.AsyncClient() as http:
        client = HttpBotClient("token", bot_api_local_dir=tmp_path, http_client=http)
        assert await client.download_file(str(target)) is None


@pytest.mark.anyio
async def test_local_read_rejects_file_growing_after_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "growing.bin"
    target.write_bytes(b"small")
    original_open = Path.open

    def grow_before_read(path, *args, **kwargs):
        if path == target and args == ("rb",):
            with original_open(path, "ab") as stream:
                stream.write(b"larger than the limit")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", grow_before_read)
    async with httpx.AsyncClient() as http:
        client = HttpBotClient(
            "token",
            base_url="http://localhost:8081",
            bot_api_local_dir=tmp_path,
            max_download_bytes=8,
            http_client=http,
        )
        assert await client.download_file(str(target)) is None


@pytest.mark.anyio
async def test_local_read_errors_do_not_log_token_bearing_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    errors: list[dict] = []
    from untether.telegram import client_api

    monkeypatch.setattr(
        client_api,
        "logger",
        SimpleNamespace(error=lambda event, **kw: errors.append(kw)),
    )
    target = tmp_path / "bot-token-example" / "missing.bin"
    async with httpx.AsyncClient() as http:
        client = HttpBotClient(
            "token",
            base_url="http://localhost:8081",
            bot_api_local_dir=tmp_path,
            http_client=http,
        )
        assert await client.download_file(str(target)) is None
    assert errors
    assert "bot-token-example" not in str(errors)


def test_local_cache_settings_require_absolute_directory(tmp_path: Path) -> None:
    from pydantic import ValidationError

    from untether.settings import TelegramTransportSettings

    cfg = TelegramTransportSettings.model_validate(
        {
            "bot_token": "token",
            "chat_id": 1,
            "allowed_user_ids": [1],
            "bot_api_local_dir": str(tmp_path),
        }
    )
    assert cfg.bot_api_local_dir == tmp_path
    assert (
        TelegramTransportSettings(
            bot_token=SecretStr("token"), chat_id=1, allowed_user_ids=[1]
        ).bot_api_local_dir
        is None
    )
    with pytest.raises(ValidationError, match="absolute directory"):
        TelegramTransportSettings.model_validate(
            {
                "bot_token": "token",
                "chat_id": 1,
                "allowed_user_ids": [1],
                "bot_api_local_dir": "cache",
            }
        )


def test_backend_applies_local_read_limit_and_hot_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.telegram_fakes import FakeTransport, make_cfg
    from untether.settings import TelegramFilesSettings, TelegramTransportSettings
    from untether.telegram import backend

    root = tmp_path / "cache"
    root.mkdir()
    target = root / "document.bin"
    target.write_bytes(b"data")
    config_path = tmp_path / "untether.toml"
    config_path.write_text("", encoding="utf-8")
    settings = TelegramTransportSettings(
        bot_token=SecretStr("token"),
        bot_api_base_url="http://localhost:8081",
        bot_api_local_dir=root,
        chat_id=1,
        allowed_user_ids=[1],
        files=TelegramFilesSettings(max_download_bytes=4),
    )
    checked = []

    async def check_loop(cfg, **kwargs):
        try:
            assert await cfg.bot.download_file(str(target)) == b"data"
            cfg.update_from(
                settings.model_copy(
                    update={"files": TelegramFilesSettings(max_download_bytes=3)}
                )
            )
            assert await cfg.bot.download_file(str(target)) is None
            cfg.update_from(
                settings.model_copy(
                    update={"files": TelegramFilesSettings(max_download_bytes=4)}
                )
            )
            assert await cfg.bot.download_file(str(target)) == b"data"
            checked.append(True)
        finally:
            await cfg.bot.close()

    monkeypatch.setattr(backend, "run_main_loop", check_loop)
    backend.TelegramBackend().build_and_run(
        transport_config=settings,
        config_path=config_path,
        runtime=make_cfg(FakeTransport()).runtime,
        final_notify=False,
        default_engine_override=None,
    )
    assert checked == [True]
