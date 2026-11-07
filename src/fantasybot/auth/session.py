"""Keeps a valid bearer token available, rotating the refresh token as it goes.

Ported from LaLigaApp's authStore.refreshToken (GPL-3.0-or-later,
(c) 2025–present Externoak), with three deliberate changes for an unattended
bot:

* the rotated refresh token is persisted, encrypted, *before* the new
  id_token is handed out, so a crash can never lose the only valid token;
* refreshes are single-flight within the process (asyncio lock) and across
  processes (file lock), and re-read the store first, so two processes never
  fork the token chain;
* where LaLigaApp silently drops a dead refresh token and carries on, this
  freezes writes and sends one alert.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable

import httpx

from ..alerts import Alerter
from ..config import KNOWN_CLIENT_IDS, Settings
from .b2c import (
    RefreshError,
    TokenResponse,
    id_token_expiry,
    jwt_expiry,
    refresh_tokens,
)
from .freeze import FreezeGuard
from .store import TokenBundle, TokenStore, TokenStoreError

log = logging.getLogger(__name__)


class AuthUnavailable(Exception):
    """No usable bearer token: nothing stored, auth frozen, or refresh failed."""


class TokenImportError(Exception):
    """A pasted token could not be imported."""


def parse_token_paste(text: str) -> dict[str, str | int | None]:
    """Accept the B2C token JSON (LaLigaApp's manual method) or a bare refresh token."""
    text = text.strip()
    if not text:
        raise TokenImportError("nothing was pasted")
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise TokenImportError(
                "that is not valid JSON; copy the whole response from { to }") from None
        if not isinstance(data, dict) or not isinstance(data.get("refresh_token"), str):
            raise TokenImportError(
                'the JSON has no "refresh_token"; copy the token response, not the request')
        return {
            "refresh_token": data["refresh_token"],
            "id_token": data.get("id_token") if isinstance(data.get("id_token"), str) else None,
            "client_id": data.get("client_id") if isinstance(data.get("client_id"), str) else None,
            "refresh_token_expires_in": data.get("refresh_token_expires_in")
            if isinstance(data.get("refresh_token_expires_in"), int) else None,
        }
    if any(ch.isspace() for ch in text):
        raise TokenImportError("expected the token JSON or a single refresh token")
    return {"refresh_token": text, "id_token": None, "client_id": None, "refresh_token_expires_in": None}


def _fmt_time(epoch: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch))


class TokenManager:
    def __init__(
        self,
        settings: Settings,
        store: TokenStore,
        freeze: FreezeGuard,
        alerter: Alerter,
        http: httpx.AsyncClient,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._store = store
        self._freeze = freeze
        self._alerter = alerter
        self._http = http
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._bundle: TokenBundle | None = None

    # -- public API -----------------------------------------------------

    async def bearer(self) -> str:
        """A bearer token valid for at least the refresh margin, if possible."""
        bundle = self._bundle or await self._load()
        self._bundle = bundle
        if self._remaining(bundle) > self._settings.refresh_margin_seconds:
            return bundle.id_token
        return await self._refresh(stale=bundle.id_token, mode="expiring")

    async def refresh_after_rejection(self, rejected_token: str) -> str:
        """Called after a 401. Returns a different token or raises."""
        return await self._refresh(stale=rejected_token, mode="rejected")

    async def force_refresh(self) -> TokenBundle:
        """Always redeem the refresh token (health check, `auth refresh`)."""
        await self._refresh(stale=None, mode="forced")
        assert self._bundle is not None
        return self._bundle

    async def import_token(
        self,
        refresh_token: str,
        *,
        client_id: str | None = None,
        id_token: str | None = None,
        refresh_token_expires_in: int | None = None,
        verify: bool = True,
    ) -> TokenBundle:
        """Store a pasted token and lift any freeze.

        With verify (the default) the token is redeemed once straight away,
        which proves it works and rotates it. If no client ID was given, the
        known LaLiga clients are tried in turn.
        """
        now = self._now()
        if verify:
            candidates = [client_id] if client_id else [
                self._settings.default_client_id,
                *(c for c in KNOWN_CLIENT_IDS if c != self._settings.default_client_id),
            ]
            response: TokenResponse | None = None
            last_error: RefreshError | None = None
            used_client = candidates[0]
            for used_client in candidates:
                try:
                    response = await self._redeem(refresh_token, used_client)
                    break
                except RefreshError as exc:
                    last_error = exc
                    if exc.transient:
                        break
            if response is None:
                assert last_error is not None
                raise TokenImportError(f"LaLiga did not accept the token: {last_error}")
            bundle = TokenBundle(
                id_token=response.id_token,
                refresh_token=response.refresh_token or refresh_token,
                client_id=used_client,
                id_token_expires_at=id_token_expiry(response, now),
                refresh_token_expires_at=self._rt_expiry(
                    response.refresh_token_expires_in or refresh_token_expires_in, now),
                imported_at=now,
                refreshed_at=now,
                rotated_at=now,
            )
        else:
            if not id_token:
                raise TokenImportError("--no-verify needs the full token JSON including id_token")
            bundle = TokenBundle(
                id_token=id_token,
                refresh_token=refresh_token,
                client_id=client_id or self._settings.default_client_id,
                # Unknown expiry counts as expired, so the first call refreshes.
                id_token_expires_at=jwt_expiry(id_token) or now,
                refresh_token_expires_at=self._rt_expiry(refresh_token_expires_in, now),
                imported_at=now,
                refreshed_at=now,
                rotated_at=now,
            )
        async with self._lock:
            async with self._store.lock():
                self._store.save(bundle)
                self._bundle = bundle
        if self._freeze.clear():
            log.info("write freeze lifted by token import")
        return bundle

    # -- internals ------------------------------------------------------

    def _now(self) -> int:
        return int(self._clock())

    def _remaining(self, bundle: TokenBundle) -> int:
        return bundle.id_token_expires_at - self._now()

    @staticmethod
    def _rt_expiry(expires_in: int | None, now: int) -> int | None:
        return now + expires_in if expires_in else None

    async def _load(self) -> TokenBundle:
        try:
            bundle = self._store.load()
        except TokenStoreError as exc:
            await self._freeze_and_alert("store", str(exc), None)
            raise AuthUnavailable(str(exc)) from None
        if bundle is None:
            raise AuthUnavailable("no token stored yet; run `fantasybot auth import`")
        return bundle

    async def _redeem(self, refresh_token: str, client_id: str) -> TokenResponse:
        attempts = max(1, self._settings.refresh_attempts)
        for attempt in range(1, attempts + 1):
            try:
                return await refresh_tokens(self._http, self._settings, refresh_token, client_id)
            except RefreshError as exc:
                if not exc.transient or attempt == attempts:
                    raise
                delay = 2 ** (attempt - 1)
                log.warning("token refresh failed (%s); retry %d/%d in %ds",
                            exc, attempt, attempts - 1, delay)
                await self._sleep(delay)
        raise AssertionError("unreachable")

    async def _refresh(self, *, stale: str | None, mode: str) -> str:
        margin = self._settings.refresh_margin_seconds
        async with self._lock:
            async with self._store.lock():
                current = await self._load()
                self._bundle = current
                now = self._now()

                # Another task or process already refreshed while we waited.
                if mode != "forced" and current.id_token != stale and self._remaining(current) > margin:
                    return current.id_token

                frozen = self._freeze.state()
                if frozen is not None:
                    if mode == "expiring" and self._remaining(current) > 0:
                        return current.id_token
                    raise AuthUnavailable(f"auth is frozen: {frozen.reason}")

                try:
                    response = await self._redeem(current.refresh_token, current.client_id)
                except RefreshError as exc:
                    await self._freeze_and_alert(exc.kind.value, str(exc), current)
                    if mode == "expiring" and self._remaining(current) > 0:
                        log.warning("refresh failed; using the current token until it expires")
                        return current.id_token
                    raise AuthUnavailable(str(exc)) from None

                rotated = bool(response.refresh_token) and response.refresh_token != current.refresh_token
                if not rotated:
                    log.info("B2C did not rotate the refresh token; keeping the current one")
                updated = current.model_copy(update={
                    "id_token": response.id_token,
                    "refresh_token": response.refresh_token or current.refresh_token,
                    "id_token_expires_at": id_token_expiry(response, now),
                    "refresh_token_expires_at": (
                        self._rt_expiry(response.refresh_token_expires_in, now)
                        if response.refresh_token_expires_in
                        else (None if rotated else current.refresh_token_expires_at)
                    ),
                    "refreshed_at": now,
                    "rotated_at": now if rotated else current.rotated_at,
                })
                # Persist before use: if this fails, keep the token in memory so
                # this process stays alive, and freeze so a human looks.
                self._bundle = updated
                try:
                    self._store.save(updated)
                except OSError as exc:
                    await self._freeze_and_alert(
                        "store", f"could not save the rotated token: {exc.strerror}", updated)
                log.info("token refreshed; id_token valid until %s", _fmt_time(updated.id_token_expires_at))
                return updated.id_token

    async def _freeze_and_alert(self, kind: str, reason: str, bundle: TokenBundle | None) -> None:
        newly = self._freeze.freeze(kind, reason, self._now())
        log.error("writes frozen: %s", reason)
        if not newly:
            return
        lines = ["⚠️ FantasyBot: writes are frozen.", f"Reason: {reason}"]
        if bundle is not None and self._remaining(bundle) > 0:
            lines.append(f"Reads keep working until the current session expires at "
                         f"{_fmt_time(bundle.id_token_expires_at)}.")
        else:
            lines.append("The current session has expired, so reads will fail too.")
        lines.append("Fix: run `fantasybot auth import` with a fresh token.")
        await self._alerter.send("\n".join(lines))
