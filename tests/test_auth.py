from __future__ import annotations

import asyncio
import stat
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.fernet import Fernet

from fantasybot.alerts import TelegramAlerter
from fantasybot.auth import (
    AuthUnavailable,
    TokenImportError,
    TokenManager,
    TokenStore,
    TokenStoreError,
    parse_token_paste,
)
from fantasybot.cli import PasteCollector
from fantasybot.config import NATIVE_CLIENT_ID, WEB_CLIENT_ID
from fantasybot.redact import redact

from .conftest import NOW, b2c_ok, make_jwt, seed

TOKEN_URL = "https://login.laliga.es/laligadspprob2c.onmicrosoft.com/oauth2/v2.0/token"


# -- the refresh call itself -------------------------------------------------


async def test_refresh_request_matches_laligaapp(tokens, store, router):
    seed(store, id_exp=NOW + 60)  # inside the 5-minute margin
    router.add("POST", TOKEN_URL, b2c_ok(NOW + 3600))

    await tokens.bearer()

    (call,) = router.b2c()
    assert call.url.params["p"] == "B2C_1A_5ULAIP_PARAMETRIZED_SIGNIN"
    form = parse_qs(call.content.decode())
    assert form == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["rt-1"],
        "client_id": [WEB_CLIENT_ID],
        "scope": ["openid offline_access"],
    }
    assert call.headers["content-type"] == "application/x-www-form-urlencoded"
    assert call.headers["user-agent"].startswith("Mozilla/5.0")


async def test_refresh_uses_the_issuing_client(tokens, store, router):
    seed(store, id_exp=NOW + 60, client_id=NATIVE_CLIENT_ID)
    router.add("POST", TOKEN_URL, b2c_ok(NOW + 3600))
    await tokens.bearer()
    assert parse_qs(router.b2c()[0].content.decode())["client_id"] == [NATIVE_CLIENT_ID]


async def test_fresh_token_is_used_without_refreshing(tokens, store, router):
    bundle = seed(store, id_exp=NOW + 3600)
    assert await tokens.bearer() == bundle.id_token
    assert router.b2c() == []


# -- rotation and storage ----------------------------------------------------


async def test_rotated_refresh_token_is_persisted(tokens, store, router):
    seed(store, id_exp=NOW + 60)
    new_id = make_jwt(NOW + 3600)
    router.add("POST", TOKEN_URL, httpx.Response(200, json={
        "id_token": new_id, "refresh_token": "rt-2", "refresh_token_expires_in": 1209600}))

    assert await tokens.bearer() == new_id

    on_disk = store.load()
    assert on_disk.id_token == new_id
    assert on_disk.refresh_token == "rt-2"
    assert on_disk.id_token_expires_at == NOW + 3600  # from the JWT exp claim
    assert on_disk.refresh_token_expires_at == NOW + 1209600
    assert on_disk.rotated_at == NOW


async def test_missing_refresh_token_keeps_the_old_one(tokens, store, router):
    seed(store, id_exp=NOW + 60)
    router.add("POST", TOKEN_URL, b2c_ok(NOW + 3600, refresh_token=None))
    await tokens.bearer()
    on_disk = store.load()
    assert on_disk.refresh_token == "rt-1"
    assert on_disk.rotated_at == NOW - 1000  # unchanged


def test_token_file_is_encrypted_and_private(store, settings):
    bundle = seed(store, id_exp=NOW + 3600, refresh_token="super-secret-refresh-token")
    raw = settings.token_path.read_bytes()
    assert b"super-secret-refresh-token" not in raw
    assert bundle.id_token.encode() not in raw
    assert stat.S_IMODE(settings.token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings.state_dir.stat().st_mode) == 0o700
    assert "super-secret" not in repr(store.load())


def test_wrong_key_cannot_read_the_store(store, settings):
    seed(store, id_exp=NOW + 3600)
    other = TokenStore(settings.token_path, Fernet.generate_key())
    with pytest.raises(TokenStoreError, match="wrong key"):
        other.load()


def test_invalid_key_is_rejected(settings):
    with pytest.raises(TokenStoreError, match="gen-key"):
        TokenStore(settings.token_path, b"not-a-key")


async def test_unreadable_store_freezes_and_alerts(settings, store, freeze, alerter, http, clock):
    seed(store, id_exp=NOW + 3600)
    wrong = TokenStore(settings.token_path, Fernet.generate_key())
    manager = TokenManager(settings, wrong, freeze, alerter, http, clock=clock)
    with pytest.raises(AuthUnavailable):
        await manager.bearer()
    assert freeze.state().kind == "store"
    assert len(alerter.messages) == 1


# -- failure handling: freeze and alert --------------------------------------


async def test_invalid_grant_freezes_once_and_keeps_reads_alive(tokens, store, freeze, alerter, router):
    bundle = seed(store, id_exp=NOW + 120)  # still valid, but inside the margin
    router.add("POST", TOKEN_URL, httpx.Response(400, json={
        "error": "invalid_grant",
        "error_description": "AADB2C90080: The provided grant has expired.\nCorrelation ID: x"}))

    # The refresh fails, but the current token still works for reads.
    assert await tokens.bearer() == bundle.id_token
    assert await tokens.bearer() == bundle.id_token

    state = freeze.state()
    assert state.kind == "invalid_grant"
    assert "AADB2C90080" in state.reason
    assert len(router.b2c()) == 1  # frozen: no second attempt against B2C
    assert len(alerter.messages) == 1  # one alert per freeze
    assert "writes are frozen" in alerter.messages[0]
    assert "fantasybot auth import" in alerter.messages[0]
    assert bundle.id_token not in alerter.messages[0]


async def test_expired_token_with_failed_refresh_raises(tokens, store, freeze, router):
    seed(store, id_exp=NOW - 10)
    router.add("POST", TOKEN_URL, httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(AuthUnavailable):
        await tokens.bearer()
    assert freeze.is_frozen()


async def test_html_answer_means_the_policy_is_gone(tokens, store, freeze, router):
    seed(store, id_exp=NOW - 10)
    router.add("POST", TOKEN_URL, httpx.Response(
        404, text="<html>The resource you are looking for has been removed</html>"))
    with pytest.raises(AuthUnavailable, match="policy"):
        await tokens.bearer()
    assert freeze.state().kind == "policy_unavailable"


async def test_success_without_id_token_freezes(tokens, store, freeze, router):
    seed(store, id_exp=NOW - 10)
    router.add("POST", TOKEN_URL, httpx.Response(200, json={"access_token": "x"}))
    with pytest.raises(AuthUnavailable):
        await tokens.bearer()
    assert freeze.state().kind == "bad_response"


async def test_transient_errors_are_retried(tokens, store, freeze, router, sleeps):
    seed(store, id_exp=NOW - 10)
    new_id = make_jwt(NOW + 3600)
    router.add("POST", TOKEN_URL,
               httpx.Response(503), httpx.ConnectError("boom"),
               httpx.Response(200, json={"id_token": new_id, "refresh_token": "rt-2"}))
    assert await tokens.bearer() == new_id
    assert sleeps == [1, 2]
    assert not freeze.is_frozen()


@pytest.mark.parametrize("failure", [httpx.Response(502), httpx.Response(429), httpx.ConnectError("offline"),
                                     httpx.ReadTimeout("slow")])
async def test_network_trouble_never_freezes(tokens, store, freeze, alerter, router, failure):
    # A laptop waking up without Wi-Fi, or a B2C outage, says nothing about the token.
    seed(store, id_exp=NOW - 10)
    router.add("POST", TOKEN_URL, failure)
    with pytest.raises(AuthUnavailable, match="nothing is frozen and the next run tries again"):
        await tokens.bearer()
    assert len(router.b2c()) == 3  # retried first
    assert not freeze.is_frozen() and alerter.messages == []


async def test_after_a_network_blip_the_next_run_just_works(tokens, store, freeze, router):
    seed(store, id_exp=NOW - 10)
    new_id = make_jwt(NOW + 3600)
    router.add("POST", TOKEN_URL, httpx.ConnectError("offline"), httpx.ConnectError("offline"),
               httpx.ConnectError("offline"),
               httpx.Response(200, json={"id_token": new_id, "refresh_token": "rt-2"}))
    with pytest.raises(AuthUnavailable):
        await tokens.bearer()
    assert await tokens.bearer() == new_id and not freeze.is_frozen()


async def test_a_network_blip_keeps_a_still_valid_session(tokens, store, freeze, router):
    seed(store, id_exp=NOW + 60)  # inside the refresh margin, but not expired
    router.add("POST", TOKEN_URL, httpx.ConnectError("offline"))
    assert await tokens.bearer() == store.load().id_token
    assert not freeze.is_frozen()


async def test_a_dead_token_after_a_blip_still_freezes(tokens, store, freeze, alerter, router):
    seed(store, id_exp=NOW - 10)
    router.add("POST", TOKEN_URL, httpx.ConnectError("offline"), httpx.ConnectError("offline"),
               httpx.ConnectError("offline"),
               httpx.Response(400, json={"error": "invalid_grant",
                                         "error_description": "AADB2C90080: The grant has expired."}))
    with pytest.raises(AuthUnavailable):
        await tokens.bearer()
    assert not freeze.is_frozen()
    with pytest.raises(AuthUnavailable):
        await tokens.bearer()
    assert freeze.state().kind == "invalid_grant" and len(alerter.messages) == 1


async def test_frozen_session_does_not_call_b2c(tokens, store, freeze, router):
    seed(store, id_exp=NOW - 10)
    freeze.freeze("invalid_grant", "test")
    with pytest.raises(AuthUnavailable, match="frozen"):
        await tokens.bearer()
    assert router.b2c() == []


# -- single flight -----------------------------------------------------------


async def test_concurrent_callers_share_one_refresh(tokens, store, router):
    seed(store, id_exp=NOW + 60)

    async def slow_ok(request):
        await asyncio.sleep(0.02)  # make sure every caller is waiting before it answers
        return b2c_ok(NOW + 3600)

    router.add("POST", TOKEN_URL, slow_ok)

    async def caller(i: int) -> str:
        await asyncio.sleep(0.001 * i)  # stagger, so later callers arrive mid-refresh
        return await tokens.bearer()

    results = await asyncio.gather(*(caller(i) for i in range(5)))
    assert len(set(results)) == 1
    assert len(router.b2c()) == 1


async def test_a_second_process_reuses_the_rotated_token(settings, store, freeze, alerter, http,
                                                         clock, router):
    old = seed(store, id_exp=NOW + 60)
    router.add("POST", TOKEN_URL, b2c_ok(NOW + 3600))
    proc_a = TokenManager(settings, store, freeze, alerter, http, clock=clock)
    proc_b = TokenManager(settings, TokenStore(settings.token_path, settings.resolved_token_key()),
                          freeze, alerter, http, clock=clock)
    assert await proc_b.bearer()  # b caches the old bundle... and refreshes it
    # a still holds nothing; b's rotation is on disk. A 401 on the old token in
    # a third process must pick up the stored token instead of redeeming again.
    fresh = await proc_a.refresh_after_rejection(old.id_token)
    assert fresh == store.load().id_token
    assert len(router.b2c()) == 1


async def test_401_with_current_token_forces_a_refresh(tokens, store, router):
    bundle = seed(store, id_exp=NOW + 3600)
    router.add("POST", TOKEN_URL, b2c_ok(NOW + 7200))
    fresh = await tokens.refresh_after_rejection(bundle.id_token)
    assert fresh != bundle.id_token
    assert len(router.b2c()) == 1


# -- import ------------------------------------------------------------------


async def test_import_verifies_rotates_and_lifts_the_freeze(tokens, store, freeze, router):
    freeze.freeze("invalid_grant", "old token died")
    router.add("POST", TOKEN_URL, b2c_ok(NOW + 3600, refresh_token="rt-rotated"))

    bundle = await tokens.import_token("rt-pasted")

    assert bundle.refresh_token == "rt-rotated"
    assert bundle.client_id == WEB_CLIENT_ID
    assert store.load().refresh_token == "rt-rotated"
    assert not freeze.is_frozen()


async def test_import_tries_the_native_client_when_web_refuses(tokens, router):
    def by_client(request):
        if parse_qs(request.content.decode())["client_id"] == [NATIVE_CLIENT_ID]:
            return b2c_ok(NOW + 3600)
        return httpx.Response(400, json={"error": "invalid_grant"})

    router.add("POST", TOKEN_URL, by_client)
    bundle = await tokens.import_token("rt-from-google-login")
    assert bundle.client_id == NATIVE_CLIENT_ID


async def test_import_failure_stores_nothing(tokens, store, router):
    router.add("POST", TOKEN_URL, httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(TokenImportError):
        await tokens.import_token("rt-dead")
    assert not store.exists()


def test_parse_token_paste():
    pasted = '{"id_token": "a.b.c", "refresh_token": "rt", "refresh_token_expires_in": 100}'
    assert parse_token_paste(pasted)["refresh_token"] == "rt"
    assert parse_token_paste("  bare-token\n")["refresh_token"] == "bare-token"
    with pytest.raises(TokenImportError, match="refresh_token"):
        parse_token_paste('{"id_token": "x"}')
    with pytest.raises(TokenImportError, match="JSON"):
        parse_token_paste('{"id_token": ')


def test_paste_collector_stops_when_the_json_closes():
    collector = PasteCollector()
    assert not collector.feed('\n {"a": "has } and { inside", ')
    assert collector.feed('"b": {"c": "\\"}"}}trailing')
    assert collector.text == '{"a": "has } and { inside", "b": {"c": "\\"}"}}'

    line = PasteCollector()
    assert line.feed("bare-token\n")
    assert line.text == "bare-token"


# -- alerts and redaction ----------------------------------------------------


def test_redact_scrubs_tokens():
    jwt = make_jwt(NOW)
    bot = "123456789:AAH-abcdefghijklmnopqrstuvwxyz012345"
    text = f"id={jwt} auth=Bearer abcdefghijklmnopqrstuvwxyz bot={bot}"
    out = redact(text, "my-own-secret-value")
    assert jwt not in out
    assert "abcdefghijklmnopqrstuvwxyz" not in out
    assert "AAH-" not in out
    assert redact("x my-own-secret-value y", "my-own-secret-value") == "x [redacted] y"


async def test_telegram_alert_is_redacted_and_never_raises(router, http):
    bot_token = "123456789:AAH-abcdefghijklmnopqrstuvwxyz012345"
    router.add("POST", "https://api.telegram.org/", httpx.Response(200, json={"ok": True}))
    alerter = TelegramAlerter(http, bot_token, "42")
    jwt = make_jwt(NOW)
    assert await alerter.send(f"leak {jwt}")
    sent = router.calls[-1]
    assert jwt not in sent.content.decode()
    assert b'"chat_id":"42"' in sent.content

    failing = TelegramAlerter(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))), bot_token, "42")
    assert await failing.send("hello") is False
