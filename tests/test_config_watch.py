import os
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

import untether.config_watch as config_watch
from untether.config import ProjectsConfig
from untether.config_watch import ConfigReload, config_status, watch_config
from untether.router import AutoRouter, RunnerEntry
from untether.runners.mock import Return, ScriptRunner
from untether.runtime_loader import RuntimeSpec
from untether.settings import UntetherSettings
from untether.transport_runtime import TransportRuntime


def test_config_status_variants(tmp_path: Path) -> None:
    missing = tmp_path / "missing.toml"
    status, signature = config_status(missing)
    assert status == "missing"
    assert signature is None

    directory = tmp_path / "config.d"
    directory.mkdir()
    status, signature = config_status(directory)
    assert status == "invalid"
    assert signature is None

    config_file = tmp_path / "untether.toml"
    config_file.write_text(
        'transport = "telegram"\n\n[transports.telegram]\n'
        'bot_token = "token"\nchat_id = 123\n',
        encoding="utf-8",
    )
    status, signature = config_status(config_file)
    assert status == "ok"
    # #839: a SHA-256 content digest, not a (mtime, size) stat tuple
    assert isinstance(signature, bytes) and len(signature) == 32


@pytest.mark.anyio
async def test_watch_config_applies_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text('default_engine = "codex"\n', encoding="utf-8")
    resolved_path = config_path.resolve()

    codex_runner = ScriptRunner([Return(answer="ok")], engine="codex")
    router = AutoRouter(
        entries=[RunnerEntry(engine=codex_runner.engine, runner=codex_runner)],
        default_engine=codex_runner.engine,
    )
    runtime = TransportRuntime(
        router=router,
        projects=ProjectsConfig(projects={}, default_project=None),
        config_path=resolved_path,
    )

    pi_runner = ScriptRunner([Return(answer="ok")], engine="pi")
    new_router = AutoRouter(
        entries=[RunnerEntry(engine=pi_runner.engine, runner=pi_runner)],
        default_engine=pi_runner.engine,
    )
    new_spec = RuntimeSpec(
        router=new_router,
        projects=ProjectsConfig(projects={}, default_project=None),
        allowlist=None,
        plugin_configs=None,
    )
    reload = ConfigReload(
        settings=UntetherSettings.model_validate(
            {
                "transport": "telegram",
                "transports": {
                    "telegram": {
                        "bot_token": "token",
                        "chat_id": 123,
                        "allow_any_user": True,
                    }
                },
            }
        ),
        runtime_spec=new_spec,
        config_path=resolved_path,
    )

    ready = anyio.Event()
    watching = anyio.Event()

    async def fake_awatch(*_paths: Path, **_kwargs: object):
        watching.set()
        await ready.wait()
        yield {(None, str(resolved_path))}

    monkeypatch.setattr(config_watch, "awatch", fake_awatch)
    monkeypatch.setattr(
        config_watch, "_reload_config", lambda *_args, **_kwargs: reload
    )

    reloaded = anyio.Event()

    async def on_reload(_payload: ConfigReload) -> None:
        reloaded.set()

    async with anyio.create_task_group() as tg:

        async def run_watch() -> None:
            await watch_config(
                config_path=resolved_path,
                runtime=runtime,
                on_reload=on_reload,
            )

        tg.start_soon(run_watch)
        with anyio.fail_after(2):
            await watching.wait()
        config_path.write_text('default_engine = "pi"\n', encoding="utf-8")
        ready.set()
        with anyio.fail_after(2):
            await reloaded.wait()
        tg.cancel_scope.cancel()

    assert runtime.default_engine == "pi"


# --- #209: a blocked extra_args flag never takes effect on reload -----------

_BLOCKED_TOML = (
    'transport = "telegram"\ndefault_engine = "codex"\n\n'
    "[transports.telegram]\n"
    'bot_token = "token"\nchat_id = 123\nallow_any_user = true\n\n'
    "[engines.codex]\n"
)


def test_209_real_reload_rejects_blocked_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import untether.runtime_loader as runtime_loader
    from untether.config import ConfigError

    monkeypatch.setattr(runtime_loader.shutil, "which", lambda _cmd: "/bin/echo")
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        _BLOCKED_TOML + 'extra_args = ["-c", "notify=[]", "--yolo"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="--yolo"):
        config_watch._reload_config(config_path, None, ())

    # Negative control: the same file without the flag reloads normally.
    config_path.write_text(
        _BLOCKED_TOML + 'extra_args = ["-c", "notify=[]"]\n', encoding="utf-8"
    )
    reload = config_watch._reload_config(config_path, None, ())
    runner = reload.runtime_spec.router.entry_for_engine("codex").runner
    assert runner.extra_args == ["-c", "notify=[]"]


@pytest.mark.anyio
async def test_209_watch_keeps_previous_runtime_on_blocked_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from structlog.testing import capture_logs

    import untether.runtime_loader as runtime_loader

    monkeypatch.setattr(runtime_loader.shutil, "which", lambda _cmd: "/bin/echo")
    config_path = tmp_path / "untether.toml"
    config_path.write_text('default_engine = "codex"\n', encoding="utf-8")
    resolved_path = config_path.resolve()

    codex_runner = ScriptRunner([Return(answer="ok")], engine="codex")
    router = AutoRouter(
        entries=[RunnerEntry(engine=codex_runner.engine, runner=codex_runner)],
        default_engine=codex_runner.engine,
    )
    runtime = TransportRuntime(
        router=router,
        projects=ProjectsConfig(projects={}, default_project=None),
        config_path=resolved_path,
    )
    original_router = runtime._router

    ready = anyio.Event()
    watching = anyio.Event()
    delivered = anyio.Event()

    async def fake_awatch(*_paths: Path, **_kwargs: object):
        watching.set()
        await ready.wait()
        yield {(None, str(resolved_path))}
        delivered.set()
        await anyio.sleep_forever()

    monkeypatch.setattr(config_watch, "awatch", fake_awatch)

    async def on_reload(_payload: ConfigReload) -> None:  # pragma: no cover
        raise AssertionError("a blocked flag must never apply")

    with capture_logs() as logs:
        async with anyio.create_task_group() as tg:

            async def run_watch() -> None:
                await watch_config(
                    config_path=resolved_path, runtime=runtime, on_reload=on_reload
                )

            tg.start_soon(run_watch)
            with anyio.fail_after(2):
                await watching.wait()
            config_path.write_text(
                _BLOCKED_TOML + 'extra_args = ["-c", "notify=[]", "--yolo"]\n',
                encoding="utf-8",
            )
            ready.set()
            with anyio.fail_after(2):
                await delivered.wait()
            tg.cancel_scope.cancel()

    assert runtime._router is original_router
    failed = [e for e in logs if e.get("event") == "config.reload.failed"]
    assert len(failed) == 1
    assert "--yolo" in failed[0]["error"]
    assert not [e for e in logs if e.get("event") == "config.reload.applied"]


def test_751_reload_passes_reason_reload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A reload that gains an `auto` cron re-emits with reason=reload."""
    from structlog.testing import capture_logs

    import untether.runtime_loader as runtime_loader
    from untether.permission_audit import reset_permission_audit_state

    monkeypatch.setattr(runtime_loader.shutil, "which", lambda _cmd: "/bin/echo")
    reset_permission_audit_state()
    path = tmp_path / "untether.toml"
    base = (
        'default_engine = "claude"\ntransport = "telegram"\n'
        "[transports.telegram]\n"
        'bot_token = "token"\nchat_id = 123\nallow_any_user = true\n'
    )
    path.write_text(base, encoding="utf-8")
    try:
        with capture_logs() as logs:
            config_watch._reload_config(path, None, ())
            path.write_text(
                base + "[triggers]\nenabled = true\n"
                '[[triggers.crons]]\nid = "a"\nschedule = "0 0 1 1 *"\n'
                'prompt = "p"\npermission_mode = "auto"\n',
                encoding="utf-8",
            )
            config_watch._reload_config(path, None, ())
    finally:
        reset_permission_audit_state()
    auto = [
        e for e in logs if e["event"] == "claude.permission_mode.auto_semantics_changed"
    ]
    assert [(e["reason"], e["entries"]) for e in auto] == [
        ("reload", ["triggers.crons[a]"])
    ]


# --- #839: content-keyed change detection -----------------------------------


def test_839_config_status_unreadable_is_missing(tmp_path: Path) -> None:
    if sys.platform == "win32" or os.geteuid() == 0:
        pytest.skip("chmod 000 doesn't block root / Windows")
    path = tmp_path / "untether.toml"
    path.write_text("x = 1\n", encoding="utf-8")
    path.chmod(0)
    try:
        assert config_status(path) == ("missing", None)
    finally:
        path.chmod(0o600)


class _FakeWatch:
    """A controllable stand-in for ``watchfiles.awatch``.

    ``push`` delivers one batch and waits until the watcher loop has finished
    processing it (the generator is resumed only when the loop body is done).
    """

    def __init__(self) -> None:
        self.send, self.recv = anyio.create_memory_object_stream(100)
        self.paths: tuple[object, ...] = ()
        self.kwargs: dict[str, object] = {}
        self.started = anyio.Event()
        self.done = 0

    async def awatch(self, *paths: object, **kwargs: object):
        self.paths = paths
        self.kwargs = kwargs
        self.started.set()
        async for batch in self.recv:
            yield batch
            self.done += 1

    async def push(self, *paths: Path) -> None:
        target = self.done + 1
        await self.send.send({(None, str(p)) for p in paths})
        with anyio.fail_after(2):
            while self.done < target:
                await anyio.sleep(0.005)


class _Recorder:
    """A stand-in ``_reload_config`` recording what each reload read."""

    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[tuple[Path, Path | None, bytes]] = []
        self.fail_times = fail_times

    def __call__(self, config_path, _override, _reserved, *, report_path=None):
        from untether.config import ConfigError

        if self.fail_times:
            self.fail_times -= 1
            raise ConfigError("Default engine 'x' unavailable: x not found on PATH")
        self.calls.append((config_path, report_path, config_path.read_bytes()))
        return ConfigReload(
            settings=None,  # type: ignore[arg-type]
            runtime_spec=SimpleNamespace(apply=lambda *_a, **_k: None),  # type: ignore[arg-type]
            config_path=report_path or config_path,
        )

    @property
    def contents(self) -> list[bytes]:
        return [c[2] for c in self.calls]


async def _run_watch(watch: _FakeWatch, config_path: Path, body, *, on_reload=None):
    async with anyio.create_task_group() as tg:

        async def run() -> None:
            await watch_config(
                config_path=config_path,
                runtime=None,  # type: ignore[arg-type]
                on_reload=on_reload,
            )

        tg.start_soon(run)
        with anyio.fail_after(2):
            await watch.started.wait()
        await body()
        tg.cancel_scope.cancel()


def _events(logs: list[dict], name: str) -> list[dict]:
    return [e for e in logs if e.get("event") == name]


def _install(monkeypatch: pytest.MonkeyPatch, recorder=None) -> _FakeWatch:
    watch = _FakeWatch()
    monkeypatch.setattr(config_watch, "awatch", watch.awatch)
    if recorder is not None:
        monkeypatch.setattr(config_watch, "_reload_config", recorder)
    return watch


@pytest.mark.anyio
async def test_839_same_stat_signature_edit_reloads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "untether.toml"
    path.write_bytes(b"x = 0\n")
    recorder = _Recorder()
    watch = _install(monkeypatch, recorder)

    async def body() -> None:
        path.write_bytes(b"a = 1\n")
        st_a = path.stat()
        await watch.push(path)
        path.write_bytes(b"b = 2\n")  # same length, different bytes
        os.utime(path, ns=(st_a.st_atime_ns, st_a.st_mtime_ns))
        st_b = path.stat()
        # precondition: the old (mtime, size) key can't tell A from B
        assert (st_a.st_mtime_ns, st_a.st_size) == (st_b.st_mtime_ns, st_b.st_size)
        await watch.push(path)

    await _run_watch(watch, path, body)
    assert recorder.contents == [b"a = 1\n", b"b = 2\n"]


@pytest.mark.anyio
async def test_839_unchanged_content_event_does_not_reload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from structlog.testing import capture_logs

    path = tmp_path / "untether.toml"
    path.write_bytes(b"a = 1\n")
    recorder = _Recorder()
    watch = _install(monkeypatch, recorder)
    callbacks: list[ConfigReload] = []

    async def on_reload(reload: ConfigReload) -> None:
        callbacks.append(reload)

    async def body() -> None:
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        await watch.push(path)  # (a) touch: new mtime, same bytes
        path.write_bytes(b"a = 1\n")
        await watch.push(path)  # (b) rewrite identical bytes

    with capture_logs() as logs:
        await _run_watch(watch, path, body, on_reload=on_reload)
    assert recorder.calls == []
    assert callbacks == []
    assert not _events(logs, "config.reload.applied")


@pytest.mark.anyio
async def test_839_edit_during_reload_is_not_lost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "untether.toml"
    path.write_bytes(b"x = 0\n")
    recorder = _Recorder()
    watch = _install(monkeypatch, recorder)

    async def on_reload(_reload: ConfigReload) -> None:
        if len(recorder.calls) == 1:
            # an edit landing while the callback (Bot API calls) awaits
            path.write_bytes(b"c = 'a longer value'\n")

    async def body() -> None:
        path.write_bytes(b"a = 1\n")
        await watch.push(path)
        await watch.push(path)  # watchfiles' queued event for the C write

    await _run_watch(watch, path, body, on_reload=on_reload)
    assert recorder.contents == [b"a = 1\n", b"c = 'a longer value'\n"]


@pytest.mark.anyio
async def test_839_atomic_rename_detected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "untether.toml"
    path.write_bytes(b"x = 0\n")
    recorder = _Recorder()
    watch = _install(monkeypatch, recorder)

    async def body() -> None:
        tmp = tmp_path / ".untether.toml.tmp"
        tmp.write_bytes(b"y = 1\n")
        os.replace(tmp, path)
        await watch.push(path)

    await _run_watch(watch, path, body)
    assert recorder.contents == [b"y = 1\n"]


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
async def test_839_symlink_swap_same_dir_detected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from structlog.testing import capture_logs

    a = tmp_path / "a.toml"
    b = tmp_path / "b.toml"
    a.write_bytes(b"which = 'a'\n")
    b.write_bytes(b"which = 'b'\n")
    link = tmp_path / "untether.toml"
    link.symlink_to("a.toml")
    recorder = _Recorder()
    watch = _install(monkeypatch, recorder)

    async def body() -> None:
        tmp = tmp_path / ".link.tmp"
        os.symlink("b.toml", tmp)
        os.replace(tmp, link)  # ln -sfn
        await watch.push(link)
        b.write_bytes(b"which = 'B'\n")
        await watch.push(b)  # an in-place edit of the new target

    with capture_logs() as logs:
        await _run_watch(watch, link, body)
    assert recorder.contents == [b"which = 'b'\n", b"which = 'B'\n"]
    # loads go through the resolved target; the reload reports the link
    assert recorder.calls[0][0] == b.resolve()
    assert recorder.calls[0][1] == link.absolute()
    changed = _events(logs, "config.watch.target_changed")
    assert len(changed) == 1
    assert changed[0]["new"] == str(b.resolve())
    assert not _events(logs, "config.watch.target_unwatched")


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
async def test_839_symlink_target_other_dir_watched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()
    target = d2 / "a.toml"
    target.write_bytes(b"x = 0\n")
    link = d1 / "untether.toml"
    link.symlink_to(target)
    recorder = _Recorder()
    watch = _install(monkeypatch, recorder)

    async def body() -> None:
        target.write_bytes(b"x = 1\n")
        await watch.push(target)

    await _run_watch(watch, link, body)
    assert set(watch.paths) == {d1.absolute(), d2.resolve()}
    assert watch.kwargs.get("recursive") is False
    assert recorder.contents == [b"x = 1\n"]


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
async def test_839_swap_to_unwatched_dir_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from structlog.testing import capture_logs

    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()
    (d1 / "a.toml").write_bytes(b"x = 0\n")
    (d2 / "b.toml").write_bytes(b"x = 1\n")
    link = d1 / "untether.toml"
    link.symlink_to("a.toml")
    recorder = _Recorder()
    watch = _install(monkeypatch, recorder)

    async def body() -> None:
        link.unlink()
        link.symlink_to(d2 / "b.toml")
        await watch.push(link)

    with capture_logs() as logs:
        await _run_watch(watch, link, body)
    assert recorder.contents == [b"x = 1\n"]
    assert len(_events(logs, "config.watch.target_unwatched")) == 1


def test_839_runtime_config_path_keeps_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import untether.runtime_loader as runtime_loader

    if sys.platform == "win32":
        pytest.skip("symlinks")
    monkeypatch.setattr(runtime_loader.shutil, "which", lambda _cmd: "/bin/echo")
    target = tmp_path / "real.toml"
    target.write_text(_BLOCKED_TOML, encoding="utf-8")
    link = tmp_path / "untether.toml"
    link.symlink_to(target)
    reload = config_watch._reload_config(target, None, (), report_path=link)
    assert reload.config_path == link


def test_839_migration_rewrite_keeps_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import untether.runtime_loader as runtime_loader

    if sys.platform == "win32":
        pytest.skip("symlinks")
    monkeypatch.setattr(runtime_loader.shutil, "which", lambda _cmd: "/bin/echo")
    target = tmp_path / "real.toml"
    # legacy top-level telegram keys -> migrate_config_file rewrites the file
    target.write_text(
        'default_engine = "codex"\nbot_token = "token"\nchat_id = 123\n'
        "allow_any_user = true\n",
        encoding="utf-8",
    )
    link = tmp_path / "untether.toml"
    link.symlink_to("real.toml")
    reload = config_watch._reload_config(
        config_watch._resolve(link), None, (), report_path=link
    )
    assert link.is_symlink()
    assert "[transports.telegram]" in target.read_text(encoding="utf-8")
    assert reload.config_path == link


@pytest.mark.anyio
async def test_839_missing_then_restored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from structlog.testing import capture_logs

    path = tmp_path / "untether.toml"
    path.write_bytes(b"a = 1\n")
    recorder = _Recorder()
    watch = _install(monkeypatch, recorder)

    async def body() -> None:
        path.unlink()
        await watch.push(path)
        await watch.push(path)  # still missing: warned once
        path.write_bytes(b"a = 1\n")  # identical bytes restored
        await watch.push(path)
        path.write_bytes(b"a = 2\n")
        await watch.push(path)

    with capture_logs() as logs:
        await _run_watch(watch, path, body)
    unavailable = _events(logs, "config.watch.unavailable")
    assert [e["status"] for e in unavailable] == ["missing"]
    assert len(_events(logs, "config.watch.available")) == 1
    assert recorder.contents == [b"a = 2\n"]


@pytest.mark.anyio
async def test_839_invalid_config_not_retried_for_same_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from structlog.testing import capture_logs

    import untether.runtime_loader as runtime_loader

    monkeypatch.setattr(runtime_loader.shutil, "which", lambda _cmd: "/bin/echo")
    path = tmp_path / "untether.toml"
    path.write_text(_BLOCKED_TOML, encoding="utf-8")
    codex_runner = ScriptRunner([Return(answer="ok")], engine="codex")
    runtime = TransportRuntime(
        router=AutoRouter(
            entries=[RunnerEntry(engine=codex_runner.engine, runner=codex_runner)],
            default_engine=codex_runner.engine,
        ),
        projects=ProjectsConfig(projects={}, default_project=None),
        config_path=path,
    )
    watch = _install(monkeypatch)

    async def body() -> None:
        path.write_text(_BLOCKED_TOML + "this is not toml\n", encoding="utf-8")
        await watch.push(path)
        await watch.push(path)  # same bytes, same mtime: not retried
        path.write_text(_BLOCKED_TOML + "# fixed\n", encoding="utf-8")
        await watch.push(path)

    with capture_logs() as logs:
        async with anyio.create_task_group() as tg:

            async def run() -> None:
                await watch_config(config_path=path, runtime=runtime)

            tg.start_soon(run)
            with anyio.fail_after(2):
                await watch.started.wait()
            await body()
            tg.cancel_scope.cancel()
    assert len(_events(logs, "config.reload.failed")) == 1
    applied = _events(logs, "config.reload.applied")
    assert len(applied) == 1
    assert len(applied[0]["digest"]) == 12


@pytest.mark.anyio
async def test_839_failed_reload_retried_on_touch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#839 A1: a failure that depends on state outside the file (an engine
    missing on PATH) is retried by ``touch`` once the operator fixes it."""
    from structlog.testing import capture_logs

    path = tmp_path / "untether.toml"
    path.write_bytes(b"x = 0\n")
    recorder = _Recorder(fail_times=1)
    watch = _install(monkeypatch, recorder)

    async def body() -> None:
        path.write_bytes(b"x = 1\n")
        await watch.push(path)  # fails
        await watch.push(path)  # identical batch: no re-log, no retry
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        await watch.push(path)  # touch: retried, succeeds

    with capture_logs() as logs:
        await _run_watch(watch, path, body)
    assert len(_events(logs, "config.reload.failed")) == 1
    assert recorder.contents == [b"x = 1\n"]


@pytest.mark.anyio
async def test_839_real_awatch_keeps_event_during_slow_callback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#839 review A3: real watchfiles keeps a change that lands while the
    consumer is busy in ``on_reload``, and the watcher reloads it."""
    import functools

    import watchfiles

    monkeypatch.setattr(
        config_watch,
        "awatch",
        functools.partial(watchfiles.awatch, debounce=50, step=10),
    )
    path = tmp_path / "untether.toml"
    path.write_bytes(b"x = 0\n")
    recorder = _Recorder()
    monkeypatch.setattr(config_watch, "_reload_config", recorder)
    second = anyio.Event()

    async def on_reload(_reload: ConfigReload) -> None:
        if len(recorder.calls) == 1:
            path.write_bytes(b"x = 'during the callback'\n")
            await anyio.sleep(0.5)  # the consumer is busy
        else:
            second.set()

    async with anyio.create_task_group() as tg:

        async def run() -> None:
            await watch_config(
                config_path=path,
                runtime=None,  # type: ignore[arg-type]
                on_reload=on_reload,
            )

        tg.start_soon(run)
        with anyio.fail_after(10):
            # the inotify watch starts on first iteration; poke until seen
            i = 0
            while not recorder.calls:
                i += 1
                path.write_bytes(f"x = {i}\n".encode())
                await anyio.sleep(0.2)
            await second.wait()
        tg.cancel_scope.cancel()
    assert recorder.contents[-1] == b"x = 'during the callback'\n"
