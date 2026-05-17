from pathlib import Path

import pytest

import untether.runtime_loader as runtime_loader
from untether.config import ConfigError
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
