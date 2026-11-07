"""Alerts to Telegram.

The bot token is part of the Telegram API URL, so it must never be logged:
the CLI keeps httpx's request logging at WARNING, and errors here are logged
by status code or redacted text only.
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from .config import ConfigError, Settings
from .redact import redact

log = logging.getLogger(__name__)

TELEGRAM_MAX_CHARS = 4096


class Alerter(Protocol):
    async def send(self, text: str) -> bool:
        """Deliver an alert. Returns False instead of raising on failure."""
        ...


class LogAlerter:
    """Used when Telegram is not configured: the alert only goes to the log."""

    async def send(self, text: str) -> bool:
        log.warning("ALERT (Telegram not configured): %s", redact(text))
        return False


class TelegramAlerter:
    def __init__(
        self,
        http: httpx.AsyncClient,
        bot_token: str,
        chat_id: str,
        api_base: str = "https://api.telegram.org",
    ) -> None:
        self._http = http
        self._token = bot_token
        self._chat_id = chat_id
        self._api_base = api_base.rstrip("/")

    async def send(self, text: str) -> bool:
        body = redact(text, self._token)
        if len(body) > TELEGRAM_MAX_CHARS:
            body = body[: TELEGRAM_MAX_CHARS - 20] + "\n… (truncated)"
        url = f"{self._api_base}/bot{self._token}/sendMessage"
        try:
            response = await self._http.post(
                url,
                json={"chat_id": self._chat_id, "text": body, "disable_web_page_preview": True},
                timeout=15.0,
            )
        except httpx.HTTPError as exc:
            log.error("Telegram alert failed: %s", redact(f"{type(exc).__name__}: {exc}", self._token))
            return False
        if response.status_code != 200:
            log.error("Telegram alert failed: HTTP %s", response.status_code)
            return False
        return True


def build_alerter(settings: Settings, http: httpx.AsyncClient) -> Alerter:
    # A missing or empty Telegram token turns alerts off; it must not stop the bot.
    try:
        token = settings.resolved_telegram_token()
    except ConfigError as exc:
        log.warning("Telegram alerts are off: %s", exc)
        token = None
    if token and settings.telegram_chat_id:
        return TelegramAlerter(http, token, settings.telegram_chat_id, settings.telegram_api_base)
    if token or settings.telegram_chat_id:
        log.warning("Telegram needs both a bot token and FANTASYBOT_TELEGRAM_CHAT_ID; alerts go to the log")
    return LogAlerter()


async def telegram_recent_chats(http: httpx.AsyncClient, bot_token: str,
                                api_base: str = "https://api.telegram.org") -> list[tuple[str, str]]:
    """Chats that recently messaged the bot, as (chat_id, label). Saves you
    pasting the bot token into a browser to find your chat ID."""
    try:
        response = await http.get(f"{api_base.rstrip('/')}/bot{bot_token}/getUpdates", timeout=15.0)
    except httpx.HTTPError as exc:
        raise RuntimeError(redact(f"Telegram unreachable: {type(exc).__name__}", bot_token)) from None
    if response.status_code != 200:
        raise RuntimeError(f"Telegram answered HTTP {response.status_code} (is the bot token right?)")
    chats: dict[str, str] = {}
    for update in response.json().get("result", []):
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if "id" in chat:
            label = (chat.get("title") or chat.get("username") or chat.get("first_name")
                     or chat.get("type", ""))
            chats[str(chat["id"])] = f"{label} ({chat.get('type', '?')})"
    return list(chats.items())
