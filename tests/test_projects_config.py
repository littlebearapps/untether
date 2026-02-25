from pathlib import Path

from typer.testing import CliRunner

from untether import cli
from untether.config import read_config
from untether.ids import RESERVED_CHAT_COMMANDS
from untether.settings import UntetherSettings


def _base_config() -> dict:
    return {"transports": {"telegram": {"bot_token": "token", "chat_id": 123}}}


def test_parse_projects_skips_engine_alias() -> None:
    config = {**_base_config(), "projects": {"codex": {"path": "/tmp/repo"}}}
    settings = UntetherSettings.model_validate(config)
    result = settings.to_projects_config(
        config_path=Path("untether.toml"),
        engine_ids=["codex"],
        reserved=RESERVED_CHAT_COMMANDS,
    )
    assert "codex" not in result.projects


def test_parse_projects_default_project_cleared_when_missing() -> None:
    config = {**_base_config(), "default_project": "z80", "projects": {}}
    settings = UntetherSettings.model_validate(config)
    result = settings.to_projects_config(
        config_path=Path("untether.toml"),
        engine_ids=["codex"],
        reserved=RESERVED_CHAT_COMMANDS,
    )
    assert result.default_project is None


def test_init_writes_project(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        'transport = "telegram"\n\n[transports.telegram]\n'
        'bot_token = "token"\nchat_id = 123\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("untether.config.HOME_CONFIG_PATH", config_path)
    monkeypatch.setattr(cli, "resolve_default_base", lambda _: "main")
    monkeypatch.setattr(cli, "_load_settings_optional", lambda: (None, None))

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    monkeypatch.chdir(repo_path)

    runner = CliRunner()
    result = runner.invoke(cli.create_app(), ["init", "z80"])
    assert result.exit_code == 0

    saved = config_path.read_text(encoding="utf-8")
    assert "[projects.z80]" in saved
    assert 'worktrees_dir = ".worktrees"' in saved
    assert 'default_engine = "codex"' in saved
    assert 'worktree_base = "main"' in saved


def test_init_migrates_legacy_config(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text('bot_token = "token"\nchat_id = 123\n', encoding="utf-8")
    monkeypatch.setattr("untether.config.HOME_CONFIG_PATH", config_path)
    monkeypatch.setattr(cli, "resolve_default_base", lambda _: "main")
    monkeypatch.setattr(cli, "_load_settings_optional", lambda: (None, None))

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    monkeypatch.chdir(repo_path)

    runner = CliRunner()
    result = runner.invoke(cli.create_app(), ["init", "z80"])
    assert result.exit_code == 0

    raw = read_config(config_path)
    assert "bot_token" not in raw
    assert "chat_id" not in raw
    assert raw["transport"] == "telegram"
    assert raw["transports"]["telegram"]["bot_token"] == "token"
    assert raw["transports"]["telegram"]["chat_id"] == 123
    assert "z80" in raw.get("projects", {})


def test_projects_skips_unknown_engine() -> None:
    config = {
        **_base_config(),
        "projects": {"z80": {"path": "/tmp/repo", "default_engine": "nope"}},
    }
    settings = UntetherSettings.model_validate(config)
    result = settings.to_projects_config(
        config_path=Path("untether.toml"),
        engine_ids=["codex"],
        reserved=RESERVED_CHAT_COMMANDS,
    )
    assert "z80" not in result.projects


def test_projects_skips_chat_id_matching_transport() -> None:
    config = {
        "transports": {"telegram": {"bot_token": "token", "chat_id": 123}},
        "projects": {"z80": {"path": "/tmp/repo", "chat_id": 123}},
    }
    settings = UntetherSettings.model_validate(config)
    result = settings.to_projects_config(
        config_path=Path("untether.toml"),
        engine_ids=["codex"],
        reserved=RESERVED_CHAT_COMMANDS,
    )
    assert "z80" not in result.projects


def test_projects_skips_duplicate_chat_id() -> None:
    config = {
        "transports": {"telegram": {"bot_token": "token", "chat_id": 123}},
        "projects": {
            "a": {"path": "/tmp/a", "chat_id": -10},
            "b": {"path": "/tmp/b", "chat_id": -10},
        },
    }
    settings = UntetherSettings.model_validate(config)
    result = settings.to_projects_config(
        config_path=Path("untether.toml"),
        engine_ids=["codex"],
        reserved=RESERVED_CHAT_COMMANDS,
    )
    # First project loads, second is skipped
    assert "a" in result.projects
    assert "b" not in result.projects


def test_projects_relative_path_resolves(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    settings = UntetherSettings.model_validate(
        {**_base_config(), "projects": {"z80": {"path": "repo"}}}
    )
    projects = settings.to_projects_config(
        config_path=config_path,
        engine_ids=["codex"],
        reserved=RESERVED_CHAT_COMMANDS,
    )
    assert projects.projects["z80"].path == config_path.parent / "repo"
