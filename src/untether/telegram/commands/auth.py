"""Command backend for headless Codex re-authentication via Telegram.

Only Codex supports device auth (codex login --device-auth). For other
engines, run their login command directly in the terminal.
"""

from __future__ import annotations

import asyncio
import re
import shutil

from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger

logger = get_logger(__name__)

# ANSI escape sequence pattern
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Device code extraction patterns (codex login --device-auth output)
_URL_RE = re.compile(r"(https?://\S+)")
_CODE_RE = re.compile(r"([A-Z0-9]{4,6}-[A-Z0-9]{4,6})")

_AUTH_TIMEOUT_SECONDS = 960  # 16 minutes

# Only Codex supports device auth. Claude Code has an open feature request
# (anthropics/claude-code#22992) but no implementation. OpenCode and Pi
# use API key env vars for headless operation.
_CODEX_CLI = "codex"
_CODEX_AUTH_ARGS = ["codex", "login", "--device-auth"]

_auth_running = False


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


def parse_device_code(text: str) -> tuple[str | None, str | None]:
    """Extract verification URL and device code from auth output.

    Returns (url, code) tuple — either may be None.
    """
    clean = strip_ansi(text)
    url_match = _URL_RE.search(clean)
    code_match = _CODE_RE.search(clean)
    return (
        url_match.group(1) if url_match else None,
        code_match.group(1) if code_match else None,
    )


def codex_cli_available() -> bool:
    """Check if the codex CLI is installed."""
    return shutil.which(_CODEX_CLI) is not None


class AuthCommand:
    """Command backend for headless Codex re-authentication."""

    id = "auth"
    description = "Re-authenticate Codex (device auth)"

    async def handle(self, ctx: CommandContext) -> CommandResult:
        global _auth_running

        args = ctx.args
        engine = args[0].lower() if args else ""

        # Only codex is supported
        if engine != "codex":
            return CommandResult(
                text=(
                    "<b>/auth codex</b> \u2014 re-authenticate Codex via device code\n\n"
                    "Only Codex supports remote device auth.\n"
                    "For other engines, run their login command "
                    "directly in the terminal."
                ),
                parse_mode="HTML",
            )

        # Check CLI availability
        if not codex_cli_available():
            return CommandResult(
                text="\u274c <code>codex</code> not found in PATH",
                parse_mode="HTML",
            )

        # Concurrent guard
        if _auth_running:
            return CommandResult(text="\u26a0\ufe0f Auth already in progress")

        _auth_running = True
        try:
            await ctx.executor.send("\U0001f510 Starting Codex device auth\u2026")

            proc = await asyncio.create_subprocess_exec(
                *_CODEX_AUTH_ARGS,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
            )

            output_lines: list[str] = []
            url: str | None = None
            code: str | None = None

            try:
                assert proc.stdout is not None
                while True:
                    try:
                        line_bytes = await asyncio.wait_for(
                            proc.stdout.readline(),
                            timeout=_AUTH_TIMEOUT_SECONDS,
                        )
                    except TimeoutError:
                        logger.warning(
                            "auth.timeout", timeout_seconds=_AUTH_TIMEOUT_SECONDS
                        )
                        proc.kill()
                        return CommandResult(
                            text=f"\u23f0 Auth timed out after {_AUTH_TIMEOUT_SECONDS // 60} minutes"
                        )

                    if not line_bytes:
                        break

                    line = strip_ansi(
                        line_bytes.decode("utf-8", errors="replace").rstrip()
                    )
                    output_lines.append(line)

                    # Try to extract device code
                    if url is None or code is None:
                        found_url, found_code = parse_device_code(line)
                        if found_url:
                            url = found_url
                        if found_code:
                            code = found_code

                        # Send device code message with security warning
                        if url and code:
                            await ctx.executor.send(
                                f"\U0001f517 Open this link and sign in:\n"
                                f"{url}\n\n"
                                f"\U0001f511 Enter this one-time code:\n"
                                f"<code>{code}</code>\n\n"
                                f"\u26a0\ufe0f <b>Security:</b> Device codes are a "
                                f"common phishing target. Never share this code "
                                f"with anyone. It expires in 15 minutes.",
                            )

                rc = await proc.wait()

            except Exception:
                proc.kill()
                raise

            if rc == 0:
                return CommandResult(text="\u2705 Codex auth completed successfully")
            else:
                excerpt = "\n".join(output_lines[-5:]) if output_lines else "no output"
                return CommandResult(
                    text=(
                        f"\u274c Codex auth failed (exit code {rc})\n"
                        f"<pre>{excerpt}</pre>"
                    ),
                    parse_mode="HTML",
                )

        finally:
            _auth_running = False


BACKEND: CommandBackend = AuthCommand()
