"""Command backend for headless engine re-authentication via Telegram."""

from __future__ import annotations

import asyncio
import re
import shutil

from ...commands import CommandBackend, CommandContext, CommandResult

# ANSI escape sequence pattern
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Device code extraction patterns (codex login --device-auth output)
_URL_RE = re.compile(r"(https?://\S+)")
_CODE_RE = re.compile(r"([A-Z0-9]{4,6}-[A-Z0-9]{4,6})")

_AUTH_TIMEOUT_SECONDS = 960  # 16 minutes

# Supported engines and their auth commands
_ENGINE_AUTH: dict[str, tuple[str, list[str]]] = {
    "codex": ("codex", ["codex", "login", "--device-auth"]),
}

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


class AuthCommand:
    """Command backend for headless re-authentication."""

    id = "auth"
    description = "Re-authenticate an engine (e.g. /auth codex)"

    async def handle(self, ctx: CommandContext) -> CommandResult:
        global _auth_running

        # Parse args: /auth [engine] or /auth status
        args = ctx.args
        if not args:
            return CommandResult(
                text=(
                    "<b>Usage:</b> /auth &lt;engine&gt;\n"
                    f"<b>Supported:</b> {', '.join(_ENGINE_AUTH.keys())}\n"
                    "<b>Check:</b> /auth status"
                ),
                parse_mode="HTML",
            )

        engine = args[0].lower()

        # Status check
        if engine == "status":
            lines = ["<b>Auth Status</b>\n"]
            for eng, (cli_cmd, _) in _ENGINE_AUTH.items():
                available = shutil.which(cli_cmd) is not None
                status = "\u2705 installed" if available else "\u274c not found"
                lines.append(f"<b>{eng}</b>: {status}")
            return CommandResult(text="\n".join(lines), parse_mode="HTML")

        if engine not in _ENGINE_AUTH:
            return CommandResult(
                text=f"\u274c Unknown engine: {engine}. Supported: {', '.join(_ENGINE_AUTH.keys())}",
            )

        cli_cmd, auth_args = _ENGINE_AUTH[engine]

        # Check CLI availability
        if shutil.which(cli_cmd) is None:
            return CommandResult(
                text=f"\u274c <code>{cli_cmd}</code> not found in PATH",
                parse_mode="HTML",
            )

        # Concurrent guard
        if _auth_running:
            return CommandResult(text="\u26a0\ufe0f Auth already in progress")

        _auth_running = True
        try:
            # Send initial message
            await ctx.executor.send(f"\U0001f510 Starting {engine} device auth\u2026")

            proc = await asyncio.create_subprocess_exec(
                *auth_args,
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

                        # Send device code message as soon as we have both
                        if url and code:
                            await ctx.executor.send(
                                f"\U0001f517 Visit: {url}\n"
                                f"\U0001f511 Code: <code>{code}</code>",
                            )

                rc = await proc.wait()

            except Exception:
                proc.kill()
                raise

            if rc == 0:
                return CommandResult(
                    text=f"\u2705 {engine} auth completed successfully"
                )
            else:
                excerpt = "\n".join(output_lines[-5:]) if output_lines else "no output"
                return CommandResult(
                    text=(
                        f"\u274c {engine} auth failed (exit code {rc})\n"
                        f"<pre>{excerpt}</pre>"
                    ),
                    parse_mode="HTML",
                )

        finally:
            _auth_running = False


BACKEND: CommandBackend = AuthCommand()
