"""``untether agy-gate install | uninstall | status`` (#980).

Not ``untether antigravity …``: every engine id is already a top-level
command that starts Untether with that engine. Host-side only — Telegram
shows the status read-only (``/config`` → Permission mode → 🔐).
"""

from __future__ import annotations

from pathlib import Path

import anyio
import typer

from ..antigravity import gate_plugin

agy_gate_app = typer.Typer(
    help=(
        "Untether's agy plugin: Telegram questions and mid-turn messages for "
        "Antigravity runs."
    ),
    no_args_is_help=True,
)


def _agy_cmd(agy: str | None) -> str:
    """``--agy``, else ``[antigravity] cmd`` from untether.toml, else agy on
    PATH (the runner's own order)."""
    if agy:
        return agy
    import tomllib

    from .. import config as untether_config

    try:
        data = tomllib.loads(untether_config.HOME_CONFIG_PATH.read_text())
    except (OSError, ValueError):
        data = {}
    section = data.get("antigravity") if isinstance(data, dict) else None
    cmd = section.get("cmd") if isinstance(section, dict) else None
    if isinstance(cmd, str) and cmd.strip():
        return str(Path(cmd).expanduser())
    from ..runners.antigravity import default_antigravity_cmd

    return default_antigravity_cmd()


_AGY_OPTION = typer.Option(None, "--agy", help="Path to agy (default: agy on PATH).")
_PROJECT_OPTION = typer.Option(
    None,
    "--project",
    help="Also run agy's own check (agy -p /hooks, no model turn) in this project folder.",
)


@agy_gate_app.command("install")
def install_cmd(
    wait_s: int = typer.Option(
        gate_plugin.DEFAULT_WAIT_S,
        "--wait-s",
        help=(
            "How long a question or an Ask me approval waits for an answer in"
            " Telegram (seconds)."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Replace a plugin folder Untether didn't install, or one edited since.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the files and the target; write nothing."
    ),
    agy: str | None = _AGY_OPTION,
) -> None:
    """Install the plugin with `agy plugin install` (host-wide: every agy on this HOME)."""
    try:
        plan = gate_plugin.plan_install(wait_s)
    except gate_plugin.GateInstallError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    if dry_run:
        typer.echo(f"Target: {plan.target}  (via `agy plugin install`)")
        for name, data in plan.files.items():
            typer.echo(f"\n--- {name}")
            typer.echo(data.decode().rstrip())
        typer.echo(f"\n--- {gate_plugin.MANIFEST_FILE}  (version, wait, file hashes)")
        return
    try:
        state = gate_plugin.install(
            agy_cmd=_agy_cmd(agy), wait_s=wait_s, force=force, echo=typer.echo
        )
    except gate_plugin.GateInstallError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Installed {gate_plugin.PLUGIN_NAME} at {state.path}.")
    typer.echo(
        "It does nothing in your own agy sessions. Untether checks it before "
        "each Antigravity run (agy -p /hooks); restart nothing."
    )


@agy_gate_app.command("uninstall")
def uninstall_cmd(
    force: bool = typer.Option(
        False,
        "--force",
        help="Remove it even if Untether didn't install it or it was edited.",
    ),
    agy: str | None = _AGY_OPTION,
) -> None:
    """Remove the plugin with `agy plugin uninstall untether-gate`."""
    try:
        removed = gate_plugin.uninstall(agy_cmd=_agy_cmd(agy), force=force)
    except gate_plugin.GateInstallError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo("Uninstalled." if removed else "Not installed — nothing to do.")


@agy_gate_app.command("status")
def status_cmd(
    project: Path | None = _PROJECT_OPTION,
    canary: bool = typer.Option(
        False,
        "--canary",
        help="Also send one trivial agy turn (≈13k tokens) and check its hooks reach Untether.",
    ),
    agy: str | None = _AGY_OPTION,
) -> None:
    """What is installed, and optionally whether agy loads and runs it."""
    state = gate_plugin.inspect_install()
    typer.echo(f"Plugin folder: {state.path}")
    typer.echo(f"State: {state.summary()}")
    if state.present:
        manifest = state.manifest or {}
        typer.echo(f"Installed by Untether: {manifest.get('untether_version', '—')}")
        typer.echo(f"Approval and question wait: {state.wait_s or '—'} s")
        for name, status in sorted(state.files.items()):
            typer.echo(f"  {name}: {status}")
    ok = state.ready
    if project is not None:
        ok = anyio.run(_project_check, _agy_cmd(agy), project.resolve()) and ok
    if canary:
        ok = anyio.run(_canary, _agy_cmd(agy)) and ok
    if not ok:
        raise typer.Exit(code=1)


def _runner(agy_cmd: str) -> object:
    from ..runners.antigravity import AntigravityRunner

    return AntigravityRunner(antigravity_cmd=agy_cmd)


async def _project_check(agy_cmd: str, project: Path) -> bool:
    from ..antigravity import gate_precheck

    result = await gate_precheck.check(_runner(agy_cmd), project, use_cache=False)  # type: ignore[arg-type]
    if result.ok:
        typer.echo(f"agy -p /hooks in {project}: the gate is loaded.")
    else:
        typer.echo(
            f"agy -p /hooks in {project}: not ready — "
            f"{gate_precheck.reason_text(result.reason)}"
            + (f" ({result.detail})" if result.detail else "")
        )
    if result.foreign_sources:
        typer.echo("Other agy hooks in scope: " + ", ".join(result.foreign_sources))
    if result.self_protect:
        typer.echo(f"This project contains Untether's own code: {result.self_protect}")
    return result.ok


async def _canary(agy_cmd: str) -> bool:
    from ..antigravity import gate_canary

    outcome = await gate_canary.run_canary(agy_cmd)
    typer.echo(f"Canary: {outcome.summary}")
    return outcome.ok
