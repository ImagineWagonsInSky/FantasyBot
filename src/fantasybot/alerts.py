"""Alerts to Telegram.

The bot token is part of the Telegram API URL, so it must never be logged:
the CLI keeps httpx's request logging at WARNING, and errors here are logged
by status code or redacted text only.
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from .config import Settings
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
    token = settings.resolved_telegram_token()
    if token and settings.telegram_chat_id:
        return TelegramAlerter(http, token, settings.telegram_chat_id, settings.telegram_api_base)
    if token or settings.telegram_chat_id:
        log.warning("Telegram needs both a bot token and FANTASYBOT_TELEGRAM_CHAT_ID; alerts go to the log")
    return LogAlerter()
