import json
import os
import subprocess
import sys
import textwrap

import pytest

import untether.lockfile as lockfile


def test_lockfile_creates_and_stamps(tmp_path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text("ok", encoding="utf-8")

    handle = lockfile.acquire_lock(
        config_path=config_path,
        token_fingerprint="deadbeef",
    )
    try:
        lock_path = lockfile.lock_path_for_config(config_path)
        assert lock_path.exists()
        info = lockfile._read_lock_info(lock_path)
        assert info is not None
        assert info.pid == os.getpid()
        assert info.token_fingerprint == "deadbeef"
    finally:
        handle.release()


def test_lockfile_refuses_while_held(tmp_path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text("ok", encoding="utf-8")

    handle = lockfile.acquire_lock(
        config_path=config_path,
        token_fingerprint="deadbeef",
    )
    try:
        with pytest.raises(lockfile.LockError) as exc:
            lockfile.acquire_lock(
                config_path=config_path,
                token_fingerprint="deadbeef",
            )
        message = str(exc.value).lower()
        assert "already running" in message
        assert exc.value.state == "running"
    finally:
        handle.release()


def test_lockfile_reacquire_after_release(tmp_path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text("ok", encoding="utf-8")

    first = lockfile.acquire_lock(
        config_path=config_path,
        token_fingerprint="deadbeef",
    )
    first.release()

    # Lock file persists (we don't unlink on release) but the flock is gone, so
    # a fresh acquire must succeed.
    assert lockfile.lock_path_for_config(config_path).exists()
    second = lockfile.acquire_lock(
        config_path=config_path,
        token_fingerprint="deadbeef",
    )
    second.release()


def test_lockfile_released_on_holder_death(tmp_path) -> None:
    """#459: the kernel releases the flock when the holding process dies, even
    via a hard exit that never runs release(). A reused PID can no longer make
    the lock look valid forever."""
    config_path = tmp_path / "untether.toml"
    config_path.write_text("ok", encoding="utf-8")

    # Child acquires the lock, signals us, then hard-exits without releasing.
    script = textwrap.dedent(
        f"""
        import os, untether.lockfile as lockfile
        from pathlib import Path
        h = lockfile.acquire_lock(
            config_path=Path({str(config_path)!r}),
            token_fingerprint="child",
        )
        print("LOCKED", flush=True)
        os._exit(0)  # never runs h.release()
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    assert "LOCKED" in proc.stdout

    # Child is dead; kernel released the flock. We must be able to acquire.
    handle = lockfile.acquire_lock(
        config_path=config_path,
        token_fingerprint="parent",
    )
    handle.release()


def test_lockfile_stale_file_with_reused_pid(tmp_path) -> None:
    """A stale lock file naming a live-but-unrelated PID must NOT block acquire
    (the old os.kill(pid, 0) check would have)."""
    config_path = tmp_path / "untether.toml"
    config_path.write_text("ok", encoding="utf-8")
    lock_path = lockfile.lock_path_for_config(config_path)
    # PID 1 is always alive but is not an untether process and holds no flock.
    payload = {"pid": 1, "token_fingerprint": "stale"}
    lock_path.write_text(json.dumps(payload), encoding="utf-8")

    handle = lockfile.acquire_lock(
        config_path=config_path,
        token_fingerprint="deadbeef",
    )
    try:
        info = lockfile._read_lock_info(lock_path)
        assert info is not None
        assert info.pid == os.getpid()
        assert info.token_fingerprint == "deadbeef"
    finally:
        handle.release()
