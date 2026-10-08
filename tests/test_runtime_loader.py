from pathlib import Path

import pytest

import untether.runtime_loader as runtime_loader
from untether.config import ConfigError
from untether.model import ResumeToken
from untether.settings import UntetherSettings


def test_build_runtime_spec_minimal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime_loader.shutil, "which", lambda _cmd: "/bin/echo")
    settings = UntetherSettings.model_validate(
        {
            "transport": "telegram",
            "watch_config": True,
            "transports": {
                "telegram": {
                    "bot_token": "token",
                    "chat_id": 123,
                    "allow_any_user": True,
                }
            },
        }
    )
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        'transport = "telegram"\n\n[transports.telegram]\n'
        'bot_token = "token"\nchat_id = 123\n',
        encoding="utf-8",
    )

    spec = runtime_loader.build_runtime_spec(
        settings=settings,
        config_path=config_path,
    )

    assert spec.router.default_engine == settings.default_engine
    runtime = spec.to_runtime(config_path=config_path)
    assert runtime.default_engine == settings.default_engine
    assert runtime.watch_config is True


def test_resolve_default_engine_unknown(tmp_path: Path) -> None:
    settings = UntetherSettings.model_validate(
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
    )
    with pytest.raises(ConfigError, match="Unknown default engine"):
        runtime_loader.resolve_default_engine(
            override="unknown",
            settings=settings,
            config_path=tmp_path / "untether.toml",
            engine_ids=["codex"],
        )


# ---------------------------------------------------------------------------
# #532: setup.summary consolidation + focused setup.warning for user-configured
# engines only. Previously each missing engine fired its own WARN on every
# config.reload — 5xN spam on single-engine hosts.
# ---------------------------------------------------------------------------


def _settings_with_engines(engine_configs: dict[str, dict] | None = None):
    base = {
        "transport": "telegram",
        "transports": {
            "telegram": {
                "bot_token": "token",
                "chat_id": 123,
                "allow_any_user": True,
            }
        },
    }
    if engine_configs is not None:
        base["engines"] = engine_configs
    return UntetherSettings.model_validate(base)


def test_setup_summary_emitted_once_with_found_and_missing_lists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A typical single-engine host (channelo: claude-only) should emit ONE
    setup.summary line listing claude as found and the rest as
    missing_on_path — no per-engine WARN spam.
    """
    from structlog.testing import capture_logs

    config_path = tmp_path / "untether.toml"
    config_path.touch()

    # Only claude is on PATH; the rest are missing.
    def fake_which(cmd: str) -> str | None:
        return "/usr/bin/claude" if cmd == "claude" else None

    monkeypatch.setattr(runtime_loader.shutil, "which", fake_which)

    settings = _settings_with_engines()  # no user-level [engines] block
    backends = runtime_loader.load_backends(
        engine_ids=["claude", "codex", "gemini", "opencode", "pi"],
        allowlist=None,
        default_engine="claude",
    )

    with capture_logs() as logs:
        runtime_loader.build_router(
            settings=settings,
            config_path=config_path,
            backends=backends,
            default_engine="claude",
        )

    summary = [e for e in logs if e.get("event") == "setup.summary"]
    assert len(summary) == 1
    s = summary[0]
    assert "claude" in s["found"]
    assert set(s["missing_on_path"]) == {"codex", "gemini", "opencode", "pi"}
    assert s["bad_config"] == []
    assert s["default_engine"] == "claude"

    # No WARN should fire for engines the user didn't configure.
    warnings = [e for e in logs if e.get("event") == "setup.warning"]
    assert warnings == [], (
        f"expected zero setup.warning lines for unconfigured engines, got: {warnings}"
    )


def test_setup_warning_fires_for_user_configured_engine_missing_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the user has put ``[engines.gemini]`` in their TOML but ``gemini``
    isn't on PATH, that IS noteworthy — fire one focused WARN. The summary
    line still emits alongside.
    """
    from structlog.testing import capture_logs

    config_path = tmp_path / "untether.toml"
    config_path.touch()

    def fake_which(cmd: str) -> str | None:
        return "/usr/bin/claude" if cmd == "claude" else None

    monkeypatch.setattr(runtime_loader.shutil, "which", fake_which)

    settings = _settings_with_engines(
        {"gemini": {"model": "gemini-pro"}}
    )  # user configured gemini despite it missing
    backends = runtime_loader.load_backends(
        engine_ids=["claude", "gemini"],
        allowlist=None,
        default_engine="claude",
    )

    with capture_logs() as logs:
        runtime_loader.build_router(
            settings=settings,
            config_path=config_path,
            backends=backends,
            default_engine="claude",
        )

    summary = [e for e in logs if e.get("event") == "setup.summary"]
    assert len(summary) == 1
    assert "gemini" in summary[0]["missing_on_path"]

    warnings = [e for e in logs if e.get("event") == "setup.warning"]
    assert len(warnings) == 1
    assert warnings[0]["engine"] == "gemini"
    assert "not found on PATH" in warnings[0]["issue"]


def test_setup_summary_all_engines_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When every engine is on PATH, summary lists all under ``found`` with
    empty missing/bad lists; no warnings.
    """
    from structlog.testing import capture_logs

    config_path = tmp_path / "untether.toml"
    config_path.touch()
    monkeypatch.setattr(runtime_loader.shutil, "which", lambda _cmd: "/bin/echo")

    settings = _settings_with_engines()
    backends = runtime_loader.load_backends(
        engine_ids=["claude", "codex"],
        allowlist=None,
        default_engine="claude",
    )

    with capture_logs() as logs:
        runtime_loader.build_router(
            settings=settings,
            config_path=config_path,
            backends=backends,
            default_engine="claude",
        )

    summary = [e for e in logs if e.get("event") == "setup.summary"]
    assert len(summary) == 1
    assert set(summary[0]["found"]) == {"claude", "codex"}
    assert summary[0]["missing_on_path"] == []
    assert summary[0]["bad_config"] == []
    assert [e for e in logs if e.get("event") == "setup.warning"] == []


# --- #209: extra_args deny-list at config load (D15) -------------------------


def _build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engines: dict):
    config_path = tmp_path / "untether.toml"
    config_path.touch()
    monkeypatch.setattr(runtime_loader.shutil, "which", lambda _cmd: "/bin/echo")
    backends = runtime_loader.load_backends(
        engine_ids=["claude", "codex"], allowlist=None, default_engine="claude"
    )
    return runtime_loader.build_router(
        settings=_settings_with_engines(engines),
        config_path=config_path,
        backends=backends,
        default_engine="claude",
    )


def test_209_default_engine_with_bypass_flag_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ConfigError, match="--dangerously-skip-permissions"):
        _build(
            monkeypatch,
            tmp_path,
            {"claude": {"extra_args": ["--dangerously-skip-permissions"]}},
        )


def test_209_non_default_engine_with_bypass_flag_is_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from structlog.testing import capture_logs

    from untether.router import RunnerUnavailableError

    with capture_logs() as logs:
        router = _build(
            monkeypatch,
            tmp_path,
            {"codex": {"extra_args": ["-s", "read-only", "--yolo"]}},
        )
    entry = router.entry_for_engine("codex")
    # D15: disabled (load_error), NOT rebuilt from `{}` as bad_config.
    assert entry.status == "load_error"
    assert not entry.available
    assert "codex" not in [e.engine for e in router.available_entries]
    with pytest.raises(RunnerUnavailableError, match="--yolo"):
        router.runner_for(ResumeToken(engine="codex", value="t"))
    summary = next(e for e in logs if e.get("event") == "setup.summary")
    assert "codex" not in summary["found"]
    warn = next(e for e in logs if e.get("event") == "setup.warning")
    assert warn["engine"] == "codex"
    assert "--yolo" in warn["issue"]
    assert "read-only" not in warn["issue"]


def test_209_other_config_errors_keep_bad_config_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    router = _build(monkeypatch, tmp_path, {"codex": {"extra_args": "not-a-list"}})
    entry = router.entry_for_engine("codex")
    assert entry.status == "bad_config"
    assert entry.available
    assert entry.runner.extra_args == ["-c", "notify=[]"]


def test_209_load_error_shows_in_startup_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from untether.config import ProjectsConfig
    from untether.transport_runtime import TransportRuntime

    router = _build(monkeypatch, tmp_path, {"codex": {"extra_args": ["--yolo"]}})
    runtime = TransportRuntime(
        router=router,
        projects=ProjectsConfig(projects={}, default_project=None),
        config_path=tmp_path / "untether.toml",
    )
    assert runtime.engine_ids_with_status("load_error") == ("codex",)
    assert "codex" not in runtime.available_engine_ids()


# ---------------------------------------------------------------------------
# #751: the permission audit runs from build_runtime_spec (startup + reload)
# ---------------------------------------------------------------------------


def _751_config(tmp_path: Path, triggers: str) -> Path:
    path = tmp_path / "untether.toml"
    path.write_text(
        'default_engine = "claude"\ntransport = "telegram"\n'
        "[transports.telegram]\n"
        'bot_token = "token"\nchat_id = 123\nallow_any_user = true\n'
        '[claude]\npermission_mode = "auto"\n' + triggers,
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _751_reset():
    from untether.permission_audit import reset_permission_audit_state

    reset_permission_audit_state()
    yield
    reset_permission_audit_state()


def test_751_build_runtime_spec_emits_exactly_one_auto_warn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from structlog.testing import capture_logs

    from untether.settings import load_settings

    monkeypatch.setattr(runtime_loader.shutil, "which", lambda _cmd: "/bin/echo")
    path = _751_config(
        tmp_path,
        "[triggers]\nenabled = true\n"
        '[[triggers.crons]]\nid = "c1"\nschedule = "0 0 1 1 *"\nprompt = "p"\n'
        'permission_mode = "auto"\n'
        '[[triggers.crons]]\nid = "c2"\nengine = "claude"\nschedule = "0 0 1 1 *"\n'
        'prompt = "p"\npermission_mode = "default"\n'
        '[[triggers.crons]]\nid = "c3"\nschedule = "0 0 1 1 *"\nprompt = "p"\n'
        'permission_mode = "bogus-typo"\n',
    )
    settings, resolved = load_settings(path)
    with capture_logs() as logs:
        runtime_loader.build_runtime_spec(settings=settings, config_path=resolved)
        runtime_loader.build_runtime_spec(settings=settings, config_path=resolved)
    auto = [
        e for e in logs if e["event"] == "claude.permission_mode.auto_semantics_changed"
    ]
    assert len(auto) == 1
    assert auto[0]["entries"] == ["engines.claude", "triggers.crons[c1]"]
    assert auto[0]["reason"] == "startup"
    risk = [e for e in logs if e["event"] == "trigger.unattended_approval_risk"]
    assert [r["entries"] for r in risk] == [
        [{"trigger": "cron:c2", "mode": "default", "waits_for": "tool approval"}]
    ]
    invalid = [e for e in logs if e["event"] == "trigger.cron.permission_mode_invalid"]
    assert [(e["trigger"], e["mode"]) for e in invalid] == [("cron:c3", "bogus-typo")]


def test_751_unparseable_triggers_do_not_break_spec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from structlog.testing import capture_logs

    from untether.settings import load_settings

    monkeypatch.setattr(runtime_loader.shutil, "which", lambda _cmd: "/bin/echo")
    path = _751_config(
        tmp_path,
        '[triggers]\nenabled = true\ndefault_timezone = "Mars/Olympus"\n'
        '[[triggers.crons]]\nid = "c1"\nschedule = "0 0 1 1 *"\nprompt = "p"\n'
        'permission_mode = "auto"\n',
    )
    settings, resolved = load_settings(path)
    with capture_logs() as logs:
        spec = runtime_loader.build_runtime_spec(
            settings=settings, config_path=resolved
        )
    assert spec.router.default_engine == "claude"
    auto = [
        e for e in logs if e["event"] == "claude.permission_mode.auto_semantics_changed"
    ]
    assert [e["entries"] for e in auto] == [["engines.claude"]]
    assert not [e for e in logs if e["event"] == "permission_audit.failed"]
