from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
import pytest
from cryptography.fernet import Fernet

from fantasybot.auth import FreezeGuard, TokenBundle, TokenManager, TokenStore
from fantasybot.config import Settings

NOW = 1_800_000_000


def make_jwt(exp: int, **claims: object) -> str:
    def part(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256', 'typ': 'JWT'})}.{part({'exp': exp, **claims})}.c2lnbmF0dXJlLXNpZw"


class FakeClock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@dataclass
class RecordingAlerter:
    messages: list[str] = field(default_factory=list)

    async def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


Handler = Callable[[httpx.Request], httpx.Response]


class Router:
    """Tiny request router for httpx.MockTransport that records every call."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self._routes: list[tuple[str, str, list[Handler | httpx.Response]]] = []

    def add(self, method: str, url_prefix: str, *responses: Handler | httpx.Response) -> None:
        self._routes.append((method, url_prefix, list(responses)))

    def b2c(self) -> list[httpx.Request]:
        return [c for c in self.calls if c.url.host == "login.laliga.es"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        for method, prefix, queue in self._routes:
            if request.method == method and url.startswith(prefix):
                item = queue[0] if len(queue) == 1 else queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item(request) if callable(item) else item
        return httpx.Response(599, json={"unrouted": url})


def b2c_ok(id_exp: int, refresh_token: str | None = "rt-2", **extra: object) -> httpx.Response:
    body: dict[str, object] = {"id_token": make_jwt(id_exp), "token_type": "Bearer", **extra}
    if refresh_token is not None:
        body["refresh_token"] = refresh_token
    return httpx.Response(200, json=body)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        state_dir=tmp_path / "state",
        token_key=Fernet.generate_key().decode(),
        refresh_attempts=3,
        request_delay=0,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def router() -> Router:
    return Router()


@pytest.fixture
async def http(router: Router):
    async with httpx.AsyncClient(transport=httpx.MockTransport(router)) as client:
        yield client


@pytest.fixture
def store(settings: Settings) -> TokenStore:
    return TokenStore(settings.token_path, settings.resolved_token_key())


@pytest.fixture
def freeze(settings: Settings) -> FreezeGuard:
    return FreezeGuard(settings.freeze_path)


@pytest.fixture
def alerter() -> RecordingAlerter:
    return RecordingAlerter()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def tokens(settings, store, freeze, alerter, http, clock, sleeps) -> TokenManager:
    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return TokenManager(settings, store, freeze, alerter, http, clock=clock, sleep=fake_sleep)


def seed(store: TokenStore, *, id_exp: int, refresh_token: str = "rt-1",
         client_id: str = "6457fa17-1224-416a-b21a-ee6ce76e9bc0") -> TokenBundle:
    bundle = TokenBundle(
        id_token=make_jwt(id_exp, sub="user"),
        refresh_token=refresh_token,
        client_id=client_id,
        id_token_expires_at=id_exp,
        imported_at=NOW - 1000,
        refreshed_at=NOW - 1000,
        rotated_at=NOW - 1000,
    )
    store.save(bundle)
    return bundle
