"""Wires settings, HTTP, token storage and alerts together."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from .alerts import Alerter, build_alerter
from .api import FantasyClient
from .auth import FreezeGuard, TokenManager, TokenStore
from .config import Settings
from .external import ExternalData


@dataclass
class Runtime:
    settings: Settings
    http: httpx.AsyncClient
    store: TokenStore
    freeze: FreezeGuard
    alerter: Alerter
    tokens: TokenManager
    client: FantasyClient
    external: ExternalData


@contextlib.asynccontextmanager
async def open_runtime(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> AsyncIterator[Runtime]:
    key = settings.resolved_token_key()
    async with httpx.AsyncClient(follow_redirects=False, transport=transport) as http:
        store = TokenStore(settings.token_path, key)
        freeze = FreezeGuard(settings.freeze_path)
        alerter = build_alerter(settings, http)
        tokens = TokenManager(settings, store, freeze, alerter, http)
        client = FantasyClient(settings, tokens, freeze, http)
        external = ExternalData(http, settings)
        yield Runtime(settings, http, store, freeze, alerter, tokens, client, external)
