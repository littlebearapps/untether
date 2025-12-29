"""First-run setup validation and onboarding."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from .config import ConfigError, HOME_CONFIG_PATH, load_telegram_config

_OCTOPUS = "\N{OCTOPUS}"


@dataclass(slots=True)
class SetupResult:
    """Collected setup issues."""

    missing_codex: bool = False
    missing_or_invalid_config: bool = False
    config_path: Path = HOME_CONFIG_PATH

    @property
    def ok(self) -> bool:
        return not (self.missing_codex or self.missing_or_invalid_config)


def check_setup() -> SetupResult:
    """Check all prerequisites and return collected issues."""
    missing_codex = shutil.which("codex") is None

    try:
        config, config_path = load_telegram_config()
    except ConfigError:
        return SetupResult(
            missing_codex=missing_codex,
            missing_or_invalid_config=True,
            config_path=HOME_CONFIG_PATH,
        )

    token = config.get("bot_token")
    chat_id = config.get("chat_id")

    missing_or_invalid_config = not (isinstance(token, str) and token.strip())
    missing_or_invalid_config |= type(chat_id) is not int

    return SetupResult(
        missing_codex=missing_codex,
        missing_or_invalid_config=missing_or_invalid_config,
        config_path=config_path,
    )


def _config_path_display(path: Path) -> str:
    """Format path for display, using ~ for home directory."""
    home = Path.home()
    try:
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)


def render_setup_guide(result: SetupResult) -> None:
    """Render a friendly setup guide panel to stderr."""
    if result.ok:
        return

    console = Console(stderr=True)
    parts: list[str] = []
    step = 0

    def add_step(title: str, *lines: str) -> None:
        nonlocal step
        step += 1
        parts.append(f"[bold yellow]{step}.[/] [bold]{title}[/]")
        parts.append("")
        parts.extend(lines)
        parts.append("")

    if result.missing_codex:
        add_step(
            "Install the Codex CLI",
            "   [dim]$[/] npm install -g @openai/codex",
        )

    if result.missing_or_invalid_config:
        config_display = _config_path_display(result.config_path)
        add_step(
            "Create a config",
            f"   [dim]{config_display}[/]",
            "",
            '   [cyan]bot_token[/] = [green]"123456789:ABCdef..."[/]',
            "   [cyan]chat_id[/]   = [green]123456789[/]",
            "",
            "[dim]" + ("-" * 56) + "[/]",
            "",
            "[bold]Getting your Telegram credentials:[/]",
            "",
            "   [cyan]bot_token[/]  create a bot with [link=https://t.me/BotFather]@BotFather[/]",
            "   [cyan]chat_id[/]    message [link=https://t.me/myidbot]@myidbot[/] to get your id",
        )

    panel = Panel(
        "\n".join(parts).rstrip(),
        title="[bold]Welcome to Takopi![/]",
        subtitle=f"{_OCTOPUS} setup required",
        border_style="yellow",
        padding=(1, 2),
        expand=False,
    )
    console.print(panel)
