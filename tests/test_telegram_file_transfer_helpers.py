from dataclasses import replace
from pathlib import Path

import pytest

from tests.telegram_fakes import DEFAULT_ENGINE_ID, FakeBot, FakeTransport, make_cfg
from untether.config import ProjectConfig, ProjectsConfig
from untether.context import RunContext
from untether.router import AutoRouter, RunnerEntry
from untether.runners.mock import Return, ScriptRunner
from untether.settings import TelegramFilesSettings
from untether.telegram.api_models import ChatMember, File
from untether.telegram.commands import file_transfer as transfer
from untether.telegram.types import TelegramDocument, TelegramIncomingMessage
from untether.transport_runtime import ResolvedMessage, TransportRuntime


class _FileBot(FakeBot):
    def __init__(self, *, file_info: File | None, payload: bytes | None) -> None:
        super().__init__()
        self._file_info = file_info
        self._payload = payload

    async def get_file(self, file_id: str) -> File | None:
        _ = file_id
        return self._file_info

    async def download_file(self, file_path: str) -> bytes | None:
        _ = file_path
        return self._payload


def _document(
    *,
    file_id: str = "file",
    file_name: str | None = "upload.bin",
    file_size: int | None = 1,
) -> TelegramDocument:
    return TelegramDocument(
        file_id=file_id,
        file_name=file_name,
        mime_type="application/octet-stream",
        file_size=file_size,
        raw={},
    )


def _msg(
    text: str,
    *,
    message_id: int = 1,
    chat_id: int = 123,
    sender_id: int | None = 1,
    chat_type: str | None = None,
    document: TelegramDocument | None = None,
) -> TelegramIncomingMessage:
    return TelegramIncomingMessage(
        transport="telegram",
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=sender_id,
        chat_type=chat_type,
        document=document,
    )


def _runtime(tmp_path: Path) -> TransportRuntime:
    runner = ScriptRunner([Return(answer="ok")], engine=DEFAULT_ENGINE_ID)
    router = AutoRouter(
        entries=[RunnerEntry(engine=runner.engine, runner=runner)],
        default_engine=runner.engine,
    )
    projects = ProjectsConfig(
        projects={
            "proj": ProjectConfig(
                alias="proj",
                path=tmp_path,
                worktrees_dir=Path(".worktrees"),
            )
        },
        default_project="proj",
    )
    return TransportRuntime(router=router, projects=projects)


def _resolved() -> ResolvedMessage:
    return ResolvedMessage(
        prompt="",
        resume_token=None,
        engine_override=None,
        context=None,
        context_source="none",
    )


def _plan(tmp_path: Path, *, path_value: str | None) -> transfer._FilePutPlan:
    return transfer._FilePutPlan(
        resolved=_resolved(),
        run_root=tmp_path,
        path_value=path_value,
        force=False,
    )


@pytest.mark.anyio
async def test_save_document_payload_rejects_large_file(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        bot=_FileBot(file_info=None, payload=None),
    )
    document = _document(file_size=TelegramFilesSettings.max_upload_bytes + 1)

    result = await transfer._save_document_payload(
        cfg,
        document=document,
        run_root=tmp_path,
        rel_path=None,
        base_dir=None,
        force=False,
    )

    assert result.error == "file is too large to upload."


@pytest.mark.anyio
async def test_save_document_payload_denied_path(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        bot=_FileBot(file_info=File(file_path="files/x.bin"), payload=None),
    )
    document = _document(file_name="x.bin")

    result = await transfer._save_document_payload(
        cfg,
        document=document,
        run_root=tmp_path,
        rel_path=None,
        base_dir=Path(".git"),
        force=False,
    )

    assert result.error == "path denied by rule: `.git/**`"


@pytest.mark.anyio
async def test_save_document_payload_existing_file_deduplicates(tmp_path: Path) -> None:
    transport = FakeTransport()
    payload = b"new content"
    cfg = replace(
        make_cfg(transport),
        bot=_FileBot(file_info=File(file_path="files/report.txt"), payload=payload),
    )
    document = _document(file_name="report.txt")
    target = tmp_path / cfg.files.uploads_dir / "report.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("existing", encoding="utf-8")

    result = await transfer._save_document_payload(
        cfg,
        document=document,
        run_root=tmp_path,
        rel_path=None,
        base_dir=None,
        force=False,
    )

    assert result.error is None
    assert result.name == "report_1.txt"
    assert result.rel_path is not None
    assert result.rel_path.name == "report_1.txt"
    saved = tmp_path / result.rel_path
    assert saved.read_bytes() == payload
    assert target.read_text(encoding="utf-8") == "existing"  # original untouched


@pytest.mark.anyio
async def test_save_document_payload_success(tmp_path: Path) -> None:
    transport = FakeTransport()
    payload = b"hello"
    cfg = replace(
        make_cfg(transport),
        bot=_FileBot(file_info=File(file_path="files/report.txt"), payload=payload),
    )
    document = _document(file_name="report.txt")

    result = await transfer._save_document_payload(
        cfg,
        document=document,
        run_root=tmp_path,
        rel_path=None,
        base_dir=None,
        force=False,
    )

    assert result.error is None
    assert result.rel_path is not None
    assert (tmp_path / result.rel_path).read_bytes() == payload


@pytest.mark.anyio
async def test_save_document_payload_missing_metadata(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        bot=_FileBot(file_info=None, payload=None),
    )
    document = _document()

    result = await transfer._save_document_payload(
        cfg,
        document=document,
        run_root=tmp_path,
        rel_path=None,
        base_dir=None,
        force=False,
    )

    assert result.error == "failed to fetch file metadata."


@pytest.mark.anyio
async def test_save_document_payload_download_failed(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        bot=_FileBot(file_info=File(file_path="files/report.txt"), payload=None),
    )
    document = _document(file_name="report.txt")

    result = await transfer._save_document_payload(
        cfg,
        document=document,
        run_root=tmp_path,
        rel_path=None,
        base_dir=None,
        force=False,
    )

    assert result.error == "failed to download file."


@pytest.mark.anyio
async def test_save_document_payload_target_is_dir(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        bot=_FileBot(file_info=File(file_path="files/report.txt"), payload=b"payload"),
    )
    document = _document(file_name="report.txt")
    target = tmp_path / "uploads"
    target.mkdir()

    result = await transfer._save_document_payload(
        cfg,
        document=document,
        run_root=tmp_path,
        rel_path=Path("uploads"),
        base_dir=None,
        force=False,
    )

    assert result.error == "upload target is a directory."


def test_resolve_file_put_paths_invalid_dir(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    plan = _plan(tmp_path, path_value="../escape/")

    base_dir, rel_path, error = transfer.resolve_file_put_paths(
        plan,
        cfg=cfg,
        require_dir=True,
    )

    assert base_dir is None
    assert rel_path is None
    assert error == "invalid upload path."


def test_resolve_file_put_paths_denied_rule(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    plan = _plan(tmp_path, path_value=".env/")

    base_dir, rel_path, error = transfer.resolve_file_put_paths(
        plan,
        cfg=cfg,
        require_dir=True,
    )

    assert base_dir is None
    assert rel_path is None
    assert error == "path denied by rule: `.env`"


def test_resolve_file_put_paths_target_is_file(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    target = tmp_path / "uploads"
    target.write_text("data", encoding="utf-8")
    plan = _plan(tmp_path, path_value="uploads/")

    base_dir, rel_path, error = transfer.resolve_file_put_paths(
        plan,
        cfg=cfg,
        require_dir=True,
    )

    assert base_dir is None
    assert rel_path is None
    assert error == "upload path is a file."


def test_resolve_file_put_paths_invalid_rel_path(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    plan = _plan(tmp_path, path_value="~/secret.txt")

    base_dir, rel_path, error = transfer.resolve_file_put_paths(
        plan,
        cfg=cfg,
        require_dir=False,
    )

    assert base_dir is None
    assert rel_path is None
    assert error == "invalid upload path."


@pytest.mark.anyio
async def test_check_file_permissions_requires_sender(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    msg = _msg("/file put", sender_id=None)

    allowed = await transfer._check_file_permissions(cfg, msg)

    assert allowed is False
    assert transport.send_calls
    assert "cannot verify sender" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_check_file_permissions_denies_unlisted_user(tmp_path: Path) -> None:
    transport = FakeTransport()
    files = TelegramFilesSettings(allowed_user_ids=[42])
    cfg = replace(make_cfg(transport), files=files)
    msg = _msg("/file put", sender_id=1)

    allowed = await transfer._check_file_permissions(cfg, msg)

    assert allowed is False
    assert transport.send_calls
    assert "file transfer is not allowed" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_check_file_permissions_denies_non_admin(tmp_path: Path) -> None:
    class _MemberBot(FakeBot):
        async def get_chat_member(self, chat_id: int, user_id: int):
            _ = chat_id
            _ = user_id
            return ChatMember(status="member")

    transport = FakeTransport()
    cfg = replace(make_cfg(transport), bot=_MemberBot())
    msg = _msg("/file put", chat_id=-123, chat_type="group")

    allowed = await transfer._check_file_permissions(cfg, msg)

    assert allowed is False
    assert transport.send_calls
    assert (
        "file transfer is restricted to group admins"
        in transport.send_calls[-1]["message"].text
    )


@pytest.mark.anyio
async def test_prepare_file_put_plan_denied_user(tmp_path: Path) -> None:
    transport = FakeTransport()
    files = TelegramFilesSettings(allowed_user_ids=[42])
    cfg = replace(make_cfg(transport), files=files, runtime=_runtime(tmp_path))
    msg = _msg("/file put", sender_id=1)

    plan = await transfer._prepare_file_put_plan(
        cfg,
        msg,
        "note.txt",
        ambient_context=None,
        topic_store=None,
    )

    assert plan is None
    assert transport.send_calls
    assert "file transfer is not allowed" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_prepare_file_put_plan_directive_error(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path))
    msg = _msg("/file put")

    plan = await transfer._prepare_file_put_plan(
        cfg,
        msg,
        "/proj /proj note.txt",
        ambient_context=None,
        topic_store=None,
    )

    assert plan is None
    assert transport.send_calls
    assert "multiple project directives" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_prepare_file_put_plan_requires_context(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    msg = _msg("/file put")

    plan = await transfer._prepare_file_put_plan(
        cfg,
        msg,
        "note.txt",
        ambient_context=None,
        topic_store=None,
    )

    assert plan is None
    assert transport.send_calls
    assert (
        "no project context available for file upload"
        in transport.send_calls[-1]["message"].text
    )


@pytest.mark.anyio
async def test_prepare_file_put_plan_rejects_unknown_flag(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path))
    msg = _msg("/file put")

    plan = await transfer._prepare_file_put_plan(
        cfg,
        msg,
        "--bogus note.txt",
        ambient_context=None,
        topic_store=None,
    )

    assert plan is None
    assert transport.send_calls
    assert "unknown flag" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_save_file_put_group_requires_documents(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path))
    msg = _msg("/file put")

    result = await transfer._save_file_put_group(
        cfg,
        msg,
        "",
        [],
        ambient_context=None,
        topic_store=None,
    )

    assert result is None
    assert transport.send_calls
    assert "usage: /file put <path>" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_save_file_put_group_saves_documents(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        runtime=_runtime(tmp_path),
        bot=_FileBot(file_info=File(file_path="files/doc.bin"), payload=b"payload"),
    )
    msg = _msg(
        "/file put uploads/",
        document=_document(file_id="a", file_name="a.txt"),
    )
    extra = _msg(
        "/file put uploads/",
        message_id=2,
        document=_document(file_id="b", file_name="b.txt"),
    )

    result = await transfer._save_file_put_group(
        cfg,
        msg,
        "uploads/",
        [msg, extra],
        ambient_context=None,
        topic_store=None,
    )

    assert result is not None
    assert result.base_dir == Path("uploads")
    assert [item.name for item in result.saved] == ["a.txt", "b.txt"]
    assert result.failed == []
    assert (tmp_path / "uploads" / "a.txt").read_bytes() == b"payload"
    assert (tmp_path / "uploads" / "b.txt").read_bytes() == b"payload"


@pytest.mark.anyio
async def test_handle_file_put_saves_and_replies(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        runtime=_runtime(tmp_path),
        bot=_FileBot(file_info=File(file_path="files/note.txt"), payload=b"hello"),
    )
    msg = _msg("/file put note.txt", document=_document(file_name="note.txt"))

    await transfer._handle_file_put(
        cfg,
        msg,
        "note.txt",
        ambient_context=None,
        topic_store=None,
    )

    assert (tmp_path / "note.txt").read_bytes() == b"hello"
    assert transport.send_calls
    assert "saved note.txt" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_handle_file_put_default_delegates(monkeypatch) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    msg = _msg("/file put")
    called: dict[str, int] = {"count": 0}

    async def _fake_handle(*_args, **_kwargs) -> None:
        called["count"] += 1

    monkeypatch.setattr(transfer, "_handle_file_put", _fake_handle)

    await transfer._handle_file_put_default(
        cfg,
        msg,
        ambient_context=None,
        topic_store=None,
    )

    assert called["count"] == 1


@pytest.mark.anyio
async def test_handle_file_command_routes(monkeypatch) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    msg = _msg("/file")
    calls: dict[str, int] = {"put": 0, "get": 0}

    async def _fake_put(*_args, **_kwargs) -> None:
        calls["put"] += 1

    async def _fake_get(*_args, **_kwargs) -> None:
        calls["get"] += 1

    monkeypatch.setattr(transfer, "_handle_file_put", _fake_put)
    monkeypatch.setattr(transfer, "_handle_file_get", _fake_get)

    await transfer._handle_file_command(
        cfg,
        msg,
        "put uploads/",
        ambient_context=None,
        topic_store=None,
    )
    await transfer._handle_file_command(
        cfg,
        msg,
        "get downloads/report.txt",
        ambient_context=None,
        topic_store=None,
    )

    assert calls["put"] == 1
    assert calls["get"] == 1


@pytest.mark.anyio
async def test_handle_file_command_invalid_usage() -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    msg = _msg("/file")

    await transfer._handle_file_command(
        cfg,
        msg,
        "unknown arg",
        ambient_context=None,
        topic_store=None,
    )

    assert transport.send_calls
    assert "usage: /file put <path>" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_handle_file_put_group_formats_failures(
    tmp_path: Path, monkeypatch
) -> None:
    transport = FakeTransport()
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path))
    msg = _msg("/file put uploads/")
    saved_group = transfer._SavedFilePutGroup(
        context=RunContext(project="proj", branch=None),
        base_dir=Path("uploads"),
        saved=[
            transfer._FilePutResult(
                name="a.txt",
                rel_path=Path("uploads/a.txt"),
                size=1,
                error=None,
            )
        ],
        failed=[
            transfer._FilePutResult(
                name="b.txt",
                rel_path=None,
                size=None,
                error="boom",
            )
        ],
    )

    async def _fake_save(*_args, **_kwargs):
        return saved_group

    monkeypatch.setattr(transfer, "_save_file_put_group", _fake_save)

    await transfer._handle_file_put_group(
        cfg,
        msg,
        "uploads/",
        [msg],
        ambient_context=None,
        topic_store=None,
    )

    assert transport.send_calls
    text = transport.send_calls[-1]["message"].text
    assert "saved a.txt to uploads/" in text
    assert "failed:" in text


@pytest.mark.anyio
async def test_handle_file_get_requires_path(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path))
    msg = _msg("/file get")

    await transfer._handle_file_get(
        cfg,
        msg,
        "",
        ambient_context=None,
        topic_store=None,
    )

    assert transport.send_calls
    assert "usage: /file get <path>" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_handle_file_get_invalid_path(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path))
    msg = _msg("/file get")

    await transfer._handle_file_get(
        cfg,
        msg,
        "../secret.txt",
        ambient_context=None,
        topic_store=None,
    )

    assert transport.send_calls
    assert "invalid download path" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_handle_file_get_missing_file(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path))
    msg = _msg("/file get")

    await transfer._handle_file_get(
        cfg,
        msg,
        "missing.txt",
        ambient_context=None,
        topic_store=None,
    )

    assert transport.send_calls
    assert "file does not exist" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_handle_file_get_sends_file(tmp_path: Path) -> None:
    transport = FakeTransport()
    bot = FakeBot()
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path), bot=bot)
    target = tmp_path / "notes.txt"
    target.write_bytes(b"hello")
    msg = _msg("/file get")

    await transfer._handle_file_get(
        cfg,
        msg,
        "notes.txt",
        ambient_context=None,
        topic_store=None,
    )

    assert bot.document_calls
    call = bot.document_calls[-1]
    assert call["filename"] == "notes.txt"
    assert call["content"] == b"hello"


@pytest.mark.anyio
async def test_handle_file_get_sends_directory_zip(tmp_path: Path) -> None:
    transport = FakeTransport()
    bot = FakeBot()
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path), bot=bot)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "file.txt").write_text("data", encoding="utf-8")
    msg = _msg("/file get")

    await transfer._handle_file_get(
        cfg,
        msg,
        "bundle",
        ambient_context=None,
        topic_store=None,
    )

    assert bot.document_calls
    call = bot.document_calls[-1]
    assert call["filename"] == "bundle.zip"
    assert call["content"][:2] == b"PK"


@pytest.mark.anyio
async def test_save_document_payload_rejects_large_payload(
    tmp_path: Path, monkeypatch
) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        bot=_FileBot(file_info=File(file_path="files/report.txt"), payload=b"xx"),
    )
    document = _document(file_name="report.txt", file_size=None)
    monkeypatch.setattr(TelegramFilesSettings, "max_upload_bytes", 1)

    result = await transfer._save_document_payload(
        cfg,
        document=document,
        run_root=tmp_path,
        rel_path=None,
        base_dir=None,
        force=False,
    )

    assert result.error == "file is too large to upload."


@pytest.mark.anyio
async def test_save_document_payload_write_error(tmp_path: Path, monkeypatch) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        bot=_FileBot(file_info=File(file_path="files/report.txt"), payload=b"data"),
    )
    document = _document(file_name="report.txt")

    def _raise(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(transfer, "write_bytes_atomic", _raise)

    result = await transfer._save_document_payload(
        cfg,
        document=document,
        run_root=tmp_path,
        rel_path=None,
        base_dir=None,
        force=False,
    )

    assert result.error == "failed to write file: boom"


@pytest.mark.anyio
async def test_check_file_permissions_missing_member(tmp_path: Path) -> None:
    class _NoMemberBot(FakeBot):
        async def get_chat_member(self, chat_id: int, user_id: int):
            _ = chat_id
            _ = user_id
            return

    transport = FakeTransport()
    cfg = replace(make_cfg(transport), bot=_NoMemberBot())
    msg = _msg("/file put", chat_id=-123, chat_type="group")

    allowed = await transfer._check_file_permissions(cfg, msg)

    assert allowed is False
    assert transport.send_calls
    assert (
        "failed to verify file transfer permissions"
        in transport.send_calls[-1]["message"].text
    )


@pytest.mark.anyio
async def test_check_file_permissions_allows_admin(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    msg = _msg("/file put", chat_id=-123, chat_type="group")

    allowed = await transfer._check_file_permissions(cfg, msg)

    assert allowed is True
    assert transport.send_calls == []


@pytest.mark.anyio
async def test_save_file_put_requires_document(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path))
    msg = _msg("/file put")

    result = await transfer._save_file_put(
        cfg,
        msg,
        "note.txt",
        ambient_context=None,
        topic_store=None,
    )

    assert result is None
    assert transport.send_calls
    assert "usage: /file put <path>" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_handle_file_put_skips_when_no_save(monkeypatch) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    msg = _msg("/file put")

    async def _fake_save(*_args, **_kwargs):
        return None

    monkeypatch.setattr(transfer, "_save_file_put", _fake_save)

    await transfer._handle_file_put(
        cfg,
        msg,
        "note.txt",
        ambient_context=None,
        topic_store=None,
    )

    assert transport.send_calls == []


@pytest.mark.anyio
async def test_handle_file_put_group_skips_when_no_save(monkeypatch) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    msg = _msg("/file put")

    async def _fake_save(*_args, **_kwargs):
        return None

    monkeypatch.setattr(transfer, "_save_file_put_group", _fake_save)

    await transfer._handle_file_put_group(
        cfg,
        msg,
        "uploads/",
        [msg],
        ambient_context=None,
        topic_store=None,
    )

    assert transport.send_calls == []


@pytest.mark.anyio
async def test_handle_file_put_group_infers_dir(tmp_path: Path, monkeypatch) -> None:
    transport = FakeTransport()
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path))
    msg = _msg("/file put")
    saved_group = transfer._SavedFilePutGroup(
        context=RunContext(project="proj", branch=None),
        base_dir=None,
        saved=[
            transfer._FilePutResult(
                name="a.txt",
                rel_path=Path("incoming/a.txt"),
                size=1,
                error=None,
            )
        ],
        failed=[],
    )

    async def _fake_save(*_args, **_kwargs):
        return saved_group

    monkeypatch.setattr(transfer, "_save_file_put_group", _fake_save)

    await transfer._handle_file_put_group(
        cfg,
        msg,
        "",
        [msg],
        ambient_context=None,
        topic_store=None,
    )

    assert transport.send_calls
    text = transport.send_calls[-1]["message"].text
    assert "saved a.txt to incoming/" in text


@pytest.mark.anyio
async def test_handle_file_get_permission_denied(tmp_path: Path) -> None:
    transport = FakeTransport()
    files = TelegramFilesSettings(allowed_user_ids=[42])
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path), files=files)
    msg = _msg("/file get", sender_id=1)

    await transfer._handle_file_get(
        cfg,
        msg,
        "notes.txt",
        ambient_context=None,
        topic_store=None,
    )

    assert transport.send_calls
    assert "file transfer is not allowed" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_handle_file_get_send_failure(tmp_path: Path) -> None:
    class _NoSendBot(FakeBot):
        async def send_document(self, *args, **kwargs):
            _ = args
            _ = kwargs
            return

    transport = FakeTransport()
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path), bot=_NoSendBot())
    target = tmp_path / "notes.txt"
    target.write_text("data", encoding="utf-8")
    msg = _msg("/file get")

    await transfer._handle_file_get(
        cfg,
        msg,
        "notes.txt",
        ambient_context=None,
        topic_store=None,
    )

    assert transport.send_calls
    assert "failed to send file" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_save_file_put_reports_invalid_path(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        runtime=_runtime(tmp_path),
        bot=_FileBot(file_info=File(file_path="files/note.txt"), payload=b"hi"),
    )
    msg = _msg("/file put", document=_document(file_name="note.txt"))

    result = await transfer._save_file_put(
        cfg,
        msg,
        "../bad/path",
        ambient_context=None,
        topic_store=None,
    )

    assert result is None
    assert transport.send_calls
    assert "invalid upload path" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_save_file_put_reports_document_error(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        runtime=_runtime(tmp_path),
        bot=_FileBot(file_info=None, payload=None),
    )
    msg = _msg("/file put", document=_document(file_name="note.txt"))

    result = await transfer._save_file_put(
        cfg,
        msg,
        "note.txt",
        ambient_context=None,
        topic_store=None,
    )

    assert result is None
    assert transport.send_calls
    assert "failed to fetch file metadata" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_save_file_put_reports_missing_path(tmp_path: Path, monkeypatch) -> None:
    transport = FakeTransport()
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path))
    msg = _msg("/file put", document=_document(file_name="note.txt"))

    async def _fake_save(*_args, **_kwargs):
        return transfer._FilePutResult(
            name="note.txt",
            rel_path=None,
            size=None,
            error=None,
        )

    monkeypatch.setattr(transfer, "_save_document_payload", _fake_save)

    result = await transfer._save_file_put(
        cfg,
        msg,
        "note.txt",
        ambient_context=None,
        topic_store=None,
    )

    assert result is None
    assert transport.send_calls
    assert "failed to save file" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_handle_file_get_requires_context(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    msg = _msg("/file get")

    await transfer._handle_file_get(
        cfg,
        msg,
        "note.txt",
        ambient_context=None,
        topic_store=None,
    )

    assert transport.send_calls
    assert (
        "no project context available for file download"
        in transport.send_calls[-1]["message"].text
    )


@pytest.mark.anyio
async def test_handle_file_get_denies_path(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path))
    msg = _msg("/file get")

    await transfer._handle_file_get(
        cfg,
        msg,
        ".env",
        ambient_context=None,
        topic_store=None,
    )

    assert transport.send_calls
    assert "path denied by rule: .env" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_handle_file_get_escape_root(tmp_path: Path, monkeypatch) -> None:
    transport = FakeTransport()
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path))
    msg = _msg("/file get")
    # A real escape (was a mock of the removed resolve_path_within_root call,
    # #390): an in-root symlink pointing outside the project.
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        (tmp_path / "note.txt").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")

    await transfer._handle_file_get(
        cfg,
        msg,
        "note.txt",
        ambient_context=None,
        topic_store=None,
    )

    assert transport.send_calls
    assert (
        "download path escapes the repo root"
        in transport.send_calls[-1]["message"].text
    )


@pytest.mark.anyio
async def test_handle_file_get_zip_too_large(tmp_path: Path, monkeypatch) -> None:
    transport = FakeTransport()
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path))
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "file.txt").write_text("data", encoding="utf-8")
    msg = _msg("/file get")

    def _raise(*_args, **_kwargs):
        raise transfer.ZipTooLargeError()

    monkeypatch.setattr(transfer, "zip_directory", _raise)

    await transfer._handle_file_get(
        cfg,
        msg,
        "bundle",
        ambient_context=None,
        topic_store=None,
    )

    assert transport.send_calls
    assert "file is too large to send" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_handle_file_get_file_too_large(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        runtime=_runtime(tmp_path),
        files=TelegramFilesSettings(max_download_bytes=1),
    )
    target = tmp_path / "notes.txt"
    target.write_bytes(b"data")
    msg = _msg("/file get")

    await transfer._handle_file_get(
        cfg,
        msg,
        "notes.txt",
        ambient_context=None,
        topic_store=None,
    )

    assert transport.send_calls
    assert "file is too large to send" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_handle_file_get_oversize_detected_on_read(
    tmp_path: Path,
) -> None:
    """#211: streaming read caps at max+1 bytes — TOCTOU between stat() and
    read() can no longer slip an over-sized file through."""
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        runtime=_runtime(tmp_path),
        files=TelegramFilesSettings(max_download_bytes=50),
    )
    target = tmp_path / "notes.txt"
    target.write_bytes(b"x" * 100)
    msg = _msg("/file get")

    await transfer._handle_file_get(
        cfg,
        msg,
        "notes.txt",
        ambient_context=None,
        topic_store=None,
    )

    # Oversize is rejected regardless of which code path detected it.
    assert transport.send_calls
    assert "file is too large to send" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_handle_file_get_at_size_limit_succeeds(
    tmp_path: Path,
) -> None:
    """#211: file exactly at the cap is delivered (read returns max bytes)."""
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        runtime=_runtime(tmp_path),
        files=TelegramFilesSettings(max_download_bytes=50),
    )
    target = tmp_path / "notes.txt"
    target.write_bytes(b"x" * 50)
    msg = _msg("/file get")

    await transfer._handle_file_get(
        cfg,
        msg,
        "notes.txt",
        ambient_context=None,
        topic_store=None,
    )

    # Document send should fire (no "too large" reply).
    too_large = any(
        "file is too large" in call["message"].text for call in transport.send_calls
    )
    assert not too_large


# ---------------------------------------------------------------------------
# #390 — deny globs hold for the symlink-resolved path (F1-F12)
# ---------------------------------------------------------------------------


def _link(link: Path, target: Path | str) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")


def _put_cfg(tmp_path: Path, name: str = "x.bin", payload: bytes = b"PAYLOAD"):
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        runtime=_runtime(tmp_path),
        bot=_FileBot(file_info=File(file_path=f"files/{name}"), payload=payload),
    )
    return transport, cfg


@pytest.mark.anyio
async def test_save_document_payload_denies_symlink_into_git(tmp_path: Path) -> None:
    # F1
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    _link(tmp_path / "docs" / "x", Path("../.git/hooks"))
    _, cfg = _put_cfg(tmp_path, "pre-commit")

    result = await transfer._save_document_payload(
        cfg,
        document=_document(file_name="pre-commit"),
        run_root=tmp_path,
        rel_path=Path("docs/x/pre-commit"),
        base_dir=None,
        force=False,
    )

    assert result.error is not None
    assert result.error.startswith("path denied by rule: `.git/**`")
    assert "resolves to `.git/hooks/pre-commit`" in result.error
    assert not (tmp_path / ".git" / "hooks" / "pre-commit").exists()


@pytest.mark.anyio
async def test_save_document_payload_uploads_dir_symlinked_to_git_hooks(
    tmp_path: Path,
) -> None:
    # F2: the malicious-repo vector — ``incoming -> .git/hooks``.
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    _link(tmp_path / "incoming", Path(".git/hooks"))
    _, cfg = _put_cfg(tmp_path, "pre-commit")

    result = await transfer._save_document_payload(
        cfg,
        document=_document(file_name="pre-commit"),
        run_root=tmp_path,
        rel_path=None,
        base_dir=None,
        force=False,
    )

    assert result.error is not None
    assert result.error.startswith("path denied by rule: `.git/**`")
    assert not (tmp_path / ".git" / "hooks" / "pre-commit").exists()


@pytest.mark.anyio
async def test_save_document_payload_force_via_symlink_to_env_denied(
    tmp_path: Path,
) -> None:
    # F3
    (tmp_path / ".env").write_text("SECRET", encoding="utf-8")
    _link(tmp_path / "cfg.txt", Path(".env"))
    _, cfg = _put_cfg(tmp_path, "cfg.txt")

    result = await transfer._save_document_payload(
        cfg,
        document=_document(file_name="cfg.txt"),
        run_root=tmp_path,
        rel_path=Path("cfg.txt"),
        base_dir=None,
        force=True,
    )

    assert result.error == "path denied by rule: `.env` (resolves to `.env`)"
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "SECRET"


@pytest.mark.anyio
async def test_save_document_payload_benign_symlink_reports_resolved_path(
    tmp_path: Path,
) -> None:
    # F4
    (tmp_path / "data" / "inbox").mkdir(parents=True)
    _link(tmp_path / "inbox", Path("data/inbox"))
    _, cfg = _put_cfg(tmp_path, "a.txt", b"hello")

    result = await transfer._save_document_payload(
        cfg,
        document=_document(file_name="a.txt"),
        run_root=tmp_path,
        rel_path=Path("inbox/a.txt"),
        base_dir=None,
        force=False,
    )

    assert result.error is None
    assert result.rel_path == Path("data/inbox/a.txt")
    assert (tmp_path / "data" / "inbox" / "a.txt").read_bytes() == b"hello"


@pytest.mark.anyio
async def test_save_document_payload_symlinked_run_root_dedup_no_crash(
    tmp_path: Path,
) -> None:
    # F5: ``target.relative_to(run_root)`` raised ValueError on a symlinked
    # project path and took the bot down.
    real = tmp_path / "real"
    (real / "incoming").mkdir(parents=True)
    (real / "incoming" / "report.txt").write_text("existing", encoding="utf-8")
    _link(tmp_path / "link", Path("real"))
    _, cfg = _put_cfg(tmp_path, "report.txt", b"new")

    result = await transfer._save_document_payload(
        cfg,
        document=_document(file_name="report.txt"),
        run_root=tmp_path / "link",
        rel_path=None,
        base_dir=None,
        force=False,
    )

    assert result.error is None
    assert result.rel_path == Path("incoming/report_1.txt")
    assert (real / "incoming" / "report.txt").read_text(encoding="utf-8") == (
        "existing"
    )
    assert (real / "incoming" / "report_1.txt").read_bytes() == b"new"


@pytest.mark.anyio
async def test_save_document_payload_symlink_loop_returns_error(
    tmp_path: Path,
) -> None:
    # F6
    _link(tmp_path / "loop1", Path("loop2"))
    _link(tmp_path / "loop2", Path("loop1"))
    _, cfg = _put_cfg(tmp_path, "x.txt")

    result = await transfer._save_document_payload(
        cfg,
        document=_document(file_name="x.txt"),
        run_root=tmp_path,
        rel_path=Path("loop1/x.txt"),
        base_dir=None,
        force=False,
    )

    assert result.error is not None
    assert "could not be resolved" in result.error


def test_resolve_file_put_paths_symlinked_dir_into_git_denied(
    tmp_path: Path,
) -> None:
    # F7
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    _link(tmp_path / "docs" / "x", Path("../.git/hooks"))
    _, cfg = _put_cfg(tmp_path)

    result = transfer.resolve_file_put_paths(
        _plan(tmp_path, path_value="docs/x/"), cfg=cfg, require_dir=False
    )

    assert result == (
        None,
        None,
        "path denied by rule: `.git/**` (resolves to `.git/hooks`)",
    )


@pytest.mark.anyio
async def test_save_file_put_group_symlinked_ssh_dir_denies_each_file(
    tmp_path: Path,
) -> None:
    # F8: the dir-level check passes (``r/.ssh`` has no ancestor matching
    # ``**/.ssh``); the per-file check on ``r/.ssh/<name>`` denies.
    (tmp_path / "r" / ".ssh").mkdir(parents=True)
    _link(tmp_path / "keys", Path("r/.ssh"))
    _, cfg = _put_cfg(tmp_path)
    msg = _msg("/file put keys/", document=_document(file_id="a", file_name="a.txt"))
    extra = _msg(
        "/file put keys/",
        message_id=2,
        document=_document(file_id="b", file_name="b.txt"),
    )

    result = await transfer._save_file_put_group(
        cfg, msg, "keys/", [msg, extra], ambient_context=None, topic_store=None
    )

    assert result is not None
    assert result.saved == []
    assert len(result.failed) == 2
    for item in result.failed:
        assert item.error is not None
        assert "**/.ssh/**" in item.error
    assert list((tmp_path / "r" / ".ssh").iterdir()) == []


@pytest.mark.anyio
async def test_handle_file_get_denies_symlink_to_env(tmp_path: Path) -> None:
    # F9
    (tmp_path / ".env").write_text("SECRET", encoding="utf-8")
    _link(tmp_path / "cfg.txt", Path(".env"))
    transport = FakeTransport()
    bot = FakeBot()
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path), bot=bot)

    await transfer._handle_file_get(
        cfg, _msg("/file get"), "cfg.txt", ambient_context=None, topic_store=None
    )

    assert "path denied by rule: .env" in transport.send_calls[-1]["message"].text
    assert bot.document_calls == []


@pytest.mark.anyio
async def test_handle_file_get_denies_dir_symlink_into_git(tmp_path: Path) -> None:
    # F10
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    _link(tmp_path / "docs" / "x", Path("../.git"))
    transport = FakeTransport()
    bot = FakeBot()
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path), bot=bot)

    await transfer._handle_file_get(
        cfg, _msg("/file get"), "docs/x", ambient_context=None, topic_store=None
    )

    text = transport.send_calls[-1]["message"].text
    assert "path denied by rule: .git/**" in text
    assert "resolves to .git" in text
    assert bot.document_calls == []


@pytest.mark.anyio
async def test_handle_file_get_symlinked_dir_zip_denies_on_real_path(
    tmp_path: Path,
) -> None:
    # F11: members are deny-checked on the real path but keep the requested
    # ``bench/`` prefix in the archive.
    import io
    import zipfile

    (tmp_path / "benchmarks").mkdir()
    (tmp_path / "benchmarks" / "ok.txt").write_text("ok", encoding="utf-8")
    (tmp_path / "benchmarks" / "secret.txt").write_text("no", encoding="utf-8")
    _link(tmp_path / "bench", Path("benchmarks"))
    transport = FakeTransport()
    bot = FakeBot()
    base = make_cfg(transport)
    files = base.files.model_copy(
        update={
            "deny_globs": [*base.files.deny_globs, "benchmarks/secret.txt"],
        }
    )
    cfg = replace(base, runtime=_runtime(tmp_path), bot=bot, files=files)

    await transfer._handle_file_get(
        cfg, _msg("/file get"), "bench", ambient_context=None, topic_store=None
    )

    assert len(bot.document_calls) == 1
    call = bot.document_calls[0]
    assert call["filename"] == "bench.zip"
    with zipfile.ZipFile(io.BytesIO(call["content"])) as archive:
        assert archive.namelist() == ["bench/ok.txt"]


@pytest.mark.anyio
async def test_handle_file_get_symlinked_file_keeps_requested_name(
    tmp_path: Path,
) -> None:
    (tmp_path / "data.txt").write_text("r15 ok", encoding="utf-8")
    _link(tmp_path / "link.txt", Path("data.txt"))
    transport = FakeTransport()
    bot = FakeBot()
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path), bot=bot)

    await transfer._handle_file_get(
        cfg, _msg("/file get"), "link.txt", ambient_context=None, topic_store=None
    )

    assert bot.document_calls[-1]["filename"] == "link.txt"
    assert bot.document_calls[-1]["content"] == b"r15 ok"


@pytest.mark.anyio
async def test_path_denied_log_fields(tmp_path: Path) -> None:
    # F12
    from structlog.testing import capture_logs

    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    _link(tmp_path / "docs" / "x", Path("../.git/hooks"))
    _, cfg = _put_cfg(tmp_path, "pre-commit")

    with capture_logs() as logs:
        await transfer._save_document_payload(
            cfg,
            document=_document(file_name="pre-commit"),
            run_root=tmp_path,
            rel_path=Path("docs/x/pre-commit"),
            base_dir=None,
            force=False,
        )

    denied = [e for e in logs if e["event"] == "file_transfer.path_denied"]
    assert len(denied) == 1
    entry = denied[0]
    assert entry["direction"] == "put"
    assert entry["via_symlink"] is True
    assert entry["rule"] == ".git/**"
    assert entry["requested"] == "docs/x/pre-commit"
    assert entry["resolved"] == ".git/hooks/pre-commit"
    assert entry["log_level"] == "warning"
    for value in entry.values():
        assert not (isinstance(value, str) and value.startswith("/"))


@pytest.mark.anyio
async def test_path_denied_log_info_without_symlink(tmp_path: Path) -> None:
    from structlog.testing import capture_logs

    transport = FakeTransport()
    cfg = replace(make_cfg(transport), runtime=_runtime(tmp_path))
    with capture_logs() as logs:
        await transfer._handle_file_get(
            cfg, _msg("/file get"), "key.pem", ambient_context=None, topic_store=None
        )

    assert "path denied by rule: **/*.pem" in transport.send_calls[-1]["message"].text
    denied = [e for e in logs if e["event"] == "file_transfer.path_denied"]
    assert len(denied) == 1
    assert denied[0]["direction"] == "get"
    assert denied[0]["via_symlink"] is False
    assert denied[0]["log_level"] == "info"


def test_path_denied_reply_keeps_glob_stars_when_rendered() -> None:
    # Replies go through Markdown; a bare ``**/.ssh/**`` used to render as a
    # bold ``/.ssh/`` (rc15 integration finding, R15-3c).
    from untether.markdown import MarkdownParts
    from untether.telegram.files import PathAccess
    from untether.telegram.render import prepare_telegram

    check = PathAccess(
        root=Path("/repo"),
        target=None,
        rel=None,
        reason="denied",
        rule="**/.ssh/**",
        via_symlink=True,
        resolved=Path("r15/real/.ssh/key.txt"),
    )
    text = transfer._path_access_error(
        "put", Path("r15/keys/key.txt"), check, kind="upload"
    )
    rendered, _entities = prepare_telegram(MarkdownParts(header=text))
    assert rendered == (
        "path denied by rule: **/.ssh/** (resolves to r15/real/.ssh/key.txt)"
    )
