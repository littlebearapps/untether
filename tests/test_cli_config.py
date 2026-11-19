import tomllib
from pathlib import Path

from typer.testing import CliRunner

from untether import cli


def _write_min_config(path: Path) -> None:
    path.write_text(
        'transport = "telegram"\n'
        "\n"
        "[transports.telegram]\n"
        'bot_token = "token"\n'
        "chat_id = 123\n"
        "allow_any_user = true\n",
        encoding="utf-8",
    )


def test_config_list_outputs_flattened(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        'transport = "telegram"\n'
        "watch_config = true\n"
        "\n"
        "[transports.telegram]\n"
        'bot_token = "token"\n'
        "chat_id = 123\n"
        "allow_any_user = true\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli.create_app(),
        ["config", "list", "--config-path", str(config_path)],
    )

    assert result.exit_code == 0
    lines = [line.strip() for line in result.output.splitlines() if line.strip()]
    assert 'transport = "telegram"' in lines
    assert "watch_config = true" in lines
    assert any(
        line.startswith('transports.telegram.bot_token = "***"  # redacted, 5 chars')
        for line in lines
    )
    assert '"token"' not in result.output
    assert "transports.telegram.chat_id = 123" in lines
    assert not any(line.startswith("default_engine") for line in lines)


def test_config_get_outputs_literal_and_table_error(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    _write_min_config(config_path)

    runner = CliRunner()
    result = runner.invoke(
        cli.create_app(),
        [
            "config",
            "get",
            "transports.telegram.chat_id",
            "--config-path",
            str(config_path),
        ],
    )

    assert result.exit_code == 0
    assert result.output.strip() == "123"

    result = runner.invoke(
        cli.create_app(),
        [
            "config",
            "get",
            "transports.telegram",
            "--config-path",
            str(config_path),
        ],
    )

    assert result.exit_code == 2
    assert "table" in result.output


def test_config_get_missing_key(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    _write_min_config(config_path)

    runner = CliRunner()
    result = runner.invoke(
        cli.create_app(),
        ["config", "get", "nope", "--config-path", str(config_path)],
    )

    assert result.exit_code == 1
    assert result.output == ""


def test_config_set_parses_and_writes(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    _write_min_config(config_path)

    runner = CliRunner()
    result = runner.invoke(
        cli.create_app(),
        ["config", "set", "watch_config", "true", "--config-path", str(config_path)],
    )
    assert result.exit_code == 0

    result = runner.invoke(
        cli.create_app(),
        [
            "config",
            "set",
            "default_engine",
            "openai",
            "--config-path",
            str(config_path),
        ],
    )
    assert result.exit_code == 0

    result = runner.invoke(
        cli.create_app(),
        [
            "config",
            "set",
            "watch_config",
            "False",
            "--config-path",
            str(config_path),
        ],
    )
    assert result.exit_code == 0

    result = runner.invoke(
        cli.create_app(),
        [
            "config",
            "set",
            "transports.telegram.chat_id",
            "456",
            "--config-path",
            str(config_path),
        ],
    )
    assert result.exit_code == 0

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert data["watch_config"] is False
    assert data["default_engine"] == "openai"
    assert data["transports"]["telegram"]["chat_id"] == 456


def test_config_unset_prunes_tables(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        'transport = "telegram"\n'
        "\n"
        "[transports.telegram]\n"
        'bot_token = "token"\n'
        "chat_id = 123\n"
        "allow_any_user = true\n"
        "\n"
        "[projects.foo]\n"
        'path = "/tmp/repo"\n',
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli.create_app(),
        ["config", "unset", "projects.foo", "--config-path", str(config_path)],
    )

    assert result.exit_code == 0
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert "projects" not in data


def test_config_set_schema_validation_error(tmp_path: Path) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        'transport = "telegram"\n'
        "\n"
        "[transports.telegram]\n"
        'bot_token = "token"\n'
        "chat_id = 123\n"
        "allow_any_user = true\n"
        "\n"
        "[projects.foo]\n"
        'path = "/tmp/repo"\n',
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli.create_app(),
        [
            "config",
            "set",
            "projects.foo.extra",
            "nope",
            "--config-path",
            str(config_path),
        ],
    )

    assert result.exit_code == 2
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert "extra" not in data.get("projects", {}).get("foo", {})


def test_1268_config_list_masks_secrets_and_lists_arrays_of_tables(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        'transport = "telegram"\n'
        "\n"
        "[transports.telegram]\n"
        'bot_token = "123456:dummy-bot-token"\n'
        "chat_id = 123\n"
        'voice_transcription_api_key = "sk-dummy-key"\n'
        "\n"
        "[engines.codex.env]\n"
        'GITHUB_TOKEN = "ghp_dummy"\n'
        "max_tokens = 4096\n"
        "\n"
        "[[triggers.webhooks]]\n"
        'id = "gh"\n'
        'secret = "whsec-dummy"\n'
        "\n"
        "[[triggers.crons]]\n"
        'id = "daily"\n'
        'schedule = "0 9 * * *"\n',
        encoding="utf-8",
    )
    app = cli.create_app()
    result = CliRunner().invoke(
        app, ["config", "list", "--config-path", str(config_path)]
    )

    assert result.exit_code == 0, result.output
    for secret in ("dummy-bot-token", "sk-dummy-key", "ghp_dummy", "whsec-dummy"):
        assert secret not in result.output
    lines = result.output.splitlines()
    assert 'triggers.webhooks[0].id = "gh"' in lines
    assert any(line.startswith('triggers.webhooks[0].secret = "***"') for line in lines)
    assert 'triggers.crons[0].schedule = "0 9 * * *"' in lines
    assert "engines.codex.env.max_tokens = 4096" in lines

    shown = CliRunner().invoke(
        app, ["config", "list", "--show-secrets", "--config-path", str(config_path)]
    )
    assert 'transports.telegram.bot_token = "123456:dummy-bot-token"' in shown.output

    got = CliRunner().invoke(
        app,
        [
            "config",
            "get",
            "transports.telegram.bot_token",
            "--config-path",
            str(config_path),
        ],
    )
    assert got.exit_code == 0 and "dummy-bot-token" not in got.output


def test_1268_config_commands_honour_untether_config_path(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "env.toml"
    _write_min_config(config_path)
    monkeypatch.setenv("UNTETHER_CONFIG_PATH", str(config_path))
    app = cli.create_app()

    assert CliRunner().invoke(app, ["config", "path"]).output.strip() == str(
        config_path
    )
    got = CliRunner().invoke(app, ["config", "get", "transports.telegram.chat_id"])
    assert got.exit_code == 0 and got.output.strip() == "123"
