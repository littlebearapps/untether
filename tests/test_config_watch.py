from pathlib import Path

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
    assert signature is not None


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

    async def fake_awatch(_path: Path):
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

    async def fake_awatch(_path: Path):
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
