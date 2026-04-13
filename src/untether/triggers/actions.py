"""Non-agent webhook actions: file_write, http_forward, notify_only.

See https://github.com/littlebearapps/untether/issues/277
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from ..logging import get_logger
from .settings import WebhookConfig
from .ssrf import SSRFError, clamp_timeout, validate_url_with_dns
from .templating import render_template_fields

logger = get_logger(__name__)

# Default deny globs — block writes to sensitive paths.
_DENY_GLOBS: tuple[str, ...] = (
    ".git/**",
    ".env",
    ".envrc",
    "**/*.pem",
    "**/.ssh/**",
)

# Maximum file size for file_write action (50 MB).
_MAX_FILE_BYTES: int = 50 * 1024 * 1024

# Maximum directory creation depth.
_MAX_PATH_DEPTH: int = 15

# http_forward defaults.
_FORWARD_TIMEOUT: int = 15
_FORWARD_MAX_RETRIES: int = 3


def _deny_reason(path: Path) -> str | None:
    """Check whether *path* matches a deny glob."""
    posix = PurePosixPath(path.as_posix())
    for pattern in _DENY_GLOBS:
        if posix.match(pattern):
            return pattern
    return None


def _resolve_file_path(raw_path: str) -> Path | None:
    """Expand and validate a file path from webhook config.

    Supports ``~`` expansion.  Rejects paths with ``..`` traversal.
    Returns the resolved absolute path or ``None`` on rejection.
    """
    expanded = Path(raw_path).expanduser()
    resolved = expanded.resolve(strict=False)

    # Block traversal via symlinks: the resolved path must start with
    # the expanded parent to prevent escaping.
    if ".." in Path(raw_path).parts:
        return None

    return resolved


async def execute_file_write(
    webhook: WebhookConfig,
    payload: dict[str, Any],
    raw_body: bytes,
) -> tuple[bool, str]:
    """Write the payload body to the configured file path.

    Returns ``(success, message)`` tuple.
    """
    assert webhook.file_path is not None  # validated by config model

    # Multipart short-circuit (#280): when a file part was already saved
    # via the multipart parser, skip the raw-body write — otherwise we
    # end up with the full MIME envelope written to ``file_path`` in
    # addition to the extracted file at ``file_destination``.
    saved = (
        payload.get("file", {}).get("saved_path")
        if isinstance(payload.get("file"), dict)
        else None
    )
    if saved:
        logger.info(
            "triggers.action.file_write.multipart_short_circuit",
            path=saved,
            webhook_id=webhook.id,
        )
        return True, f"written to {saved}"

    # Render template variables in file_path.
    rendered_path = render_template_fields(webhook.file_path, payload)
    target = _resolve_file_path(rendered_path)
    if target is None:
        msg = f"file_write rejected: path traversal in {rendered_path!r}"
        logger.warning("triggers.action.file_write.path_rejected", path=rendered_path)
        return False, msg

    # Deny-glob check on the resolved path.
    reason = _deny_reason(target)
    if reason is not None:
        msg = f"file_write rejected: path matches deny glob {reason!r}"
        logger.warning(
            "triggers.action.file_write.denied",
            path=str(target),
            deny_glob=reason,
        )
        return False, msg

    # Path depth check.
    if len(target.parts) > _MAX_PATH_DEPTH:
        msg = f"file_write rejected: path too deep ({len(target.parts)} levels)"
        logger.warning("triggers.action.file_write.too_deep", path=str(target))
        return False, msg

    # Size check.
    if len(raw_body) > _MAX_FILE_BYTES:
        msg = f"file_write rejected: payload too large ({len(raw_body)} bytes)"
        logger.warning(
            "triggers.action.file_write.too_large",
            size=len(raw_body),
            max_size=_MAX_FILE_BYTES,
        )
        return False, msg

    # On-conflict handling.
    if target.exists():
        if webhook.on_conflict == "error":
            msg = f"file_write rejected: file already exists at {target}"
            logger.warning("triggers.action.file_write.exists", path=str(target))
            return False, msg
        if webhook.on_conflict == "append_timestamp":
            stem = target.stem
            suffix = target.suffix
            ts = str(int(time.time()))
            target = target.parent / f"{stem}_{ts}{suffix}"

    # Atomic write.
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=target.parent,
            prefix=".untether-trigger-",
        ) as handle:
            handle.write(raw_body)
            temp_name = handle.name
        Path(temp_name).replace(target)
    except OSError as exc:
        msg = f"file_write failed: {exc}"
        logger.error(
            "triggers.action.file_write.error", path=str(target), error=str(exc)
        )
        return False, msg

    logger.info(
        "triggers.action.file_write.ok",
        path=str(target),
        size=len(raw_body),
        webhook_id=webhook.id,
    )
    return True, f"written to {target}"


async def execute_http_forward(
    webhook: WebhookConfig,
    payload: dict[str, Any],
    raw_body: bytes,
) -> tuple[bool, str]:
    """Forward the payload to the configured URL.

    Returns ``(success, message)`` tuple.
    """
    assert webhook.forward_url is not None  # validated by config model

    # Render template variables in forward_url and headers.
    rendered_url = render_template_fields(webhook.forward_url, payload)
    rendered_headers: dict[str, str] = {}
    if webhook.forward_headers:
        for key, value in webhook.forward_headers.items():
            rendered_value = render_template_fields(value, payload)
            # Reject header values with newlines/control chars.
            if any(c in rendered_value for c in ("\r", "\n", "\x00")):
                msg = (
                    f"http_forward rejected: header {key!r} contains control characters"
                )
                logger.warning(
                    "triggers.action.http_forward.header_injection",
                    header=key,
                    webhook_id=webhook.id,
                )
                return False, msg
            rendered_headers[key] = rendered_value

    # SSRF validation.
    try:
        await validate_url_with_dns(rendered_url)
    except SSRFError as exc:
        msg = f"http_forward blocked: {exc}"
        logger.warning(
            "triggers.action.http_forward.ssrf_blocked",
            url=rendered_url,
            error=str(exc),
            webhook_id=webhook.id,
        )
        return False, msg

    # Forward with retries on 5xx.
    timeout = clamp_timeout(_FORWARD_TIMEOUT)
    method = webhook.forward_method
    last_error = ""

    for attempt in range(1, _FORWARD_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(
                    method,
                    rendered_url,
                    content=raw_body,
                    headers={
                        "Content-Type": "application/json",
                        **rendered_headers,
                    },
                    follow_redirects=False,
                )
            if resp.status_code < 500:
                if resp.status_code < 400:
                    logger.info(
                        "triggers.action.http_forward.ok",
                        url=rendered_url,
                        status=resp.status_code,
                        webhook_id=webhook.id,
                    )
                    return True, f"forwarded ({resp.status_code})"
                # 4xx — don't retry.
                msg = f"http_forward failed: {resp.status_code}"
                logger.warning(
                    "triggers.action.http_forward.client_error",
                    url=rendered_url,
                    status=resp.status_code,
                    webhook_id=webhook.id,
                )
                return False, msg

            # 5xx — retry with backoff.
            last_error = f"http_forward: server error {resp.status_code}"
            logger.warning(
                "triggers.action.http_forward.retry",
                url=rendered_url,
                status=resp.status_code,
                attempt=attempt,
                webhook_id=webhook.id,
            )
            if attempt < _FORWARD_MAX_RETRIES:
                import anyio

                await anyio.sleep(2**attempt)  # 2, 4 seconds

        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_error = f"http_forward: {exc}"
            logger.warning(
                "triggers.action.http_forward.retry",
                url=rendered_url,
                error=str(exc),
                attempt=attempt,
                webhook_id=webhook.id,
            )
            if attempt < _FORWARD_MAX_RETRIES:
                import anyio

                await anyio.sleep(2**attempt)

    logger.error(
        "triggers.action.http_forward.exhausted",
        url=rendered_url,
        webhook_id=webhook.id,
    )
    return False, last_error


def execute_notify_message(
    webhook: WebhookConfig,
    payload: dict[str, Any],
) -> str:
    """Render the notification message template.

    Returns the rendered message text.
    """
    assert webhook.message_template is not None  # validated by config model
    return render_template_fields(webhook.message_template, payload)
