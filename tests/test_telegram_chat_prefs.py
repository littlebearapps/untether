import pytest

from untether.telegram.chat_prefs import ChatPrefsStore


@pytest.mark.anyio
async def test_chat_prefs_store_roundtrip(tmp_path) -> None:
    path = tmp_path / "telegram_chat_prefs_state.json"
    store = ChatPrefsStore(path)
    await store.set_default_engine(123, "codex")
    await store.set_trigger_mode(123, "mentions")
    await store.set_default_engine(123, "codex")
    await store.clear_default_engine(456)

    assert await store.get_default_engine(123) == "codex"
    assert await store.get_trigger_mode(123) == "mentions"

    store2 = ChatPrefsStore(path)
    assert await store2.get_default_engine(123) == "codex"
    assert await store2.get_trigger_mode(123) == "mentions"

    await store2.clear_default_engine(123)
    assert await store2.get_default_engine(123) is None
    assert await store2.get_trigger_mode(123) == "mentions"

    await store2.clear_trigger_mode(123)
    assert await store2.get_trigger_mode(123) is None


async def _saved_store(path):
    store = ChatPrefsStore(path)
    await store.set_default_engine(123, "codex")
    await store.set_trigger_mode(123, "mentions")
    return path.read_bytes()


@pytest.mark.anyio
@pytest.mark.parametrize("damage", ["truncate", "future_version"])
async def test_1277_unreadable_prefs_file_is_kept_not_wiped(tmp_path, damage) -> None:
    """#1277: one bad read used to rewrite the file as `{"chats": {}}`."""
    import json

    path = tmp_path / "telegram_chat_prefs_state.json"
    good = await _saved_store(path)
    if damage == "truncate":
        bad = good[:-20]
    else:
        data = json.loads(good)
        data["version"] = 99
        bad = json.dumps(data).encode()
    path.write_bytes(bad)

    store = ChatPrefsStore(path)
    assert await store.get_default_engine(123) is None
    backups = list(tmp_path.glob("telegram_chat_prefs_state.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == bad
    # A later write starts fresh beside the backup, never over it.
    await store.set_default_engine(456, "claude")
    assert backups[0].read_bytes() == bad
    assert await ChatPrefsStore(path).get_default_engine(456) == "claude"


@pytest.mark.anyio
async def test_1277_failed_read_keeps_state_and_refuses_saves(
    tmp_path, monkeypatch
) -> None:
    from pathlib import Path

    path = tmp_path / "telegram_chat_prefs_state.json"
    good = await _saved_store(path)
    store = ChatPrefsStore(path)
    assert await store.get_default_engine(123) == "codex"

    real_read = Path.read_bytes

    def failing_read(self):
        if self == path:
            raise OSError(5, "Input/output error")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", failing_read)
    import os

    st = path.stat()  # a new mtime forces a reload, which now fails
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert await store.get_default_engine(123) == "codex"
    # #1373: the change is refused (and says so), not silently dropped.
    from untether.telegram.chat_prefs import ChatPrefsNotSaved

    with pytest.raises(ChatPrefsNotSaved):
        await store.set_default_engine(789, "pi")
    assert await store.get_default_engine(789) is None  # memory untouched
    assert real_read(path) == good
    assert not list(tmp_path.glob("*.corrupt-*"))

    monkeypatch.setattr(Path, "read_bytes", real_read)
    assert await store.get_trigger_mode(123) == "mentions"
