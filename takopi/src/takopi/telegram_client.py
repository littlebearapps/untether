from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .logging import RedactTokenFilter

logger = logging.getLogger(__name__)
logger.addFilter(RedactTokenFilter())


class TelegramAPIError(RuntimeError):
    def __init__(
        self, method: str, payload: dict[str, Any], status_code: int | None
    ) -> None:
        desc = payload.get("description") or str(payload)
        super().__init__(f"{method} failed: {desc}")
        self.payload = payload
        self.status_code = status_code


class TelegramClient:
    """
    Minimal Telegram Bot API client.
    """

    def __init__(
        self,
        token: str,
        timeout_s: float = 120,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not token:
            raise ValueError("Telegram token is empty")
        self._base = f"https://api.telegram.org/bot{token}"
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None
        self._sleep = sleep

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post(self, method: str, json_data: dict[str, Any]) -> Any:
        try:
            logger.debug("[telegram] request %s: %s", method, json_data)
            resp = await self._client.post(f"{self._base}/{method}", json=json_data)
            payload: dict[str, Any] | None = None
            try:
                payload = resp.json()
            except Exception:
                resp.raise_for_status()
                raise
            if not payload.get("ok"):
                params = payload.get("parameters") or {}
                retry_after = params.get("retry_after")
                if resp.status_code == 429 and isinstance(retry_after, int):
                    logger.warning(
                        "[telegram] 429 retry_after=%s method=%s", retry_after, method
                    )
                    await self._sleep(retry_after)
                    return await self._post(method, json_data)
                raise TelegramAPIError(method, payload, resp.status_code)
            logger.debug("[telegram] response %s: %s", method, payload)
            return payload["result"]
        except httpx.HTTPError as e:
            logger.error("Telegram network error: %s", e)
            raise

    async def get_updates(
        self,
        offset: int | None,
        timeout_s: int = 50,
        allowed_updates: list[str] | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {"timeout": timeout_s}
        if offset is not None:
            params["offset"] = offset
        if allowed_updates is not None:
            params["allowed_updates"] = allowed_updates
        return await self._post("getUpdates", params)  # type: ignore[return-value]

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        disable_notification: bool | None = False,
        entities: list[dict] | None = None,
        parse_mode: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        if disable_notification is not None:
            params["disable_notification"] = disable_notification
        if reply_to_message_id is not None:
            params["reply_to_message_id"] = reply_to_message_id
        if entities is not None:
            params["entities"] = entities
        if parse_mode is not None:
            params["parse_mode"] = parse_mode
        return await self._post("sendMessage", params)  # type: ignore[return-value]

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        entities: list[dict] | None = None,
        parse_mode: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if entities is not None:
            params["entities"] = entities
        if parse_mode is not None:
            params["parse_mode"] = parse_mode
        return await self._post("editMessageText", params)  # type: ignore[return-value]

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        res = await self._post(
            "deleteMessage",
            {
                "chat_id": chat_id,
                "message_id": message_id,
            },
        )
        return bool(res)
