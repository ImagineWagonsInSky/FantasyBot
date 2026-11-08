"""A small Telegram Bot API client: messages with buttons, button taps, long polling.

The bot token is part of every URL, so nothing here logs a URL, and errors
are reported by method and status only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .redact import redact

LONG_POLL_SECONDS = 50


class TelegramError(Exception):
    pass


@dataclass(frozen=True)
class Button:
    text: str
    data: str  # callback data, at most 64 bytes


Keyboard = list[list[Button]]


class TelegramBot:
    def __init__(self, http: httpx.AsyncClient, token: str,
                 api_base: str = "https://api.telegram.org") -> None:
        self._http = http
        self._token = token
        self._base = api_base.rstrip("/")

    async def call(self, method: str, payload: dict[str, Any], timeout: float = 15.0) -> Any:
        url = f"{self._base}/bot{self._token}/{method}"
        try:
            response = await self._http.post(url, json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            raise TelegramError(redact(f"{method}: {type(exc).__name__}", self._token)) from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not body.get("ok"):
            detail = redact(str(body.get("description", "")), self._token)[:200]
            raise TelegramError(f"{method}: HTTP {response.status_code} {detail}".strip())
        return body.get("result")

    @staticmethod
    def _markup(buttons: Keyboard | None) -> dict[str, Any]:
        rows = [[{"text": b.text, "callback_data": b.data} for b in row] for row in buttons or []]
        return {"inline_keyboard": rows}

    async def send(self, chat_id: str, text: str, buttons: Keyboard | None = None) -> int:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": redact(text, self._token)[:4096],
                                   "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = self._markup(buttons)
        result = await self.call("sendMessage", payload)
        return int(result.get("message_id", 0)) if isinstance(result, dict) else 0

    async def clear_buttons(self, chat_id: str, message_id: int) -> None:
        await self.call("editMessageReplyMarkup",
                        {"chat_id": chat_id, "message_id": message_id, "reply_markup": self._markup(None)})

    async def answer(self, callback_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        await self.call("answerCallbackQuery", payload)

    async def updates(self, offset: int | None, timeout: int = LONG_POLL_SECONDS) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        result = await self.call("getUpdates", payload, timeout=timeout + 15)
        return result if isinstance(result, list) else []
