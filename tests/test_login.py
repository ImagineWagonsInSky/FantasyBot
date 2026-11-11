from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from fantasybot.auth import TokenImportError
from fantasybot.auth.b2c import (
    APP_REDIRECT_URI,
    LoginError,
    authorize_url,
    code_from_redirect,
    pkce_pair,
)
from fantasybot.config import NATIVE_CLIENT_ID

from .conftest import NOW, make_jwt

TOKEN_URL = "https://login.laliga.es/"


def test_pkce_challenge_is_the_s256_of_the_verifier():
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert pkce_pair()[0] != verifier  # fresh each time


def test_the_authorize_address_is_the_apps_sign_in(settings):
    url = urlsplit(authorize_url(settings, NATIVE_CLIENT_ID, "CHAL", "STATE"))
    assert url.netloc == "login.laliga.es" and url.path.endswith("/oauth2/v2.0/authorize")
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert q == {"p": "B2C_1A_5ULAIP_PARAMETRIZED_SIGNIN", "client_id": NATIVE_CLIENT_ID,
                 "response_type": "code", "redirect_uri": APP_REDIRECT_URI,
                 "scope": "openid offline_access", "code_challenge": "CHAL",
                 "code_challenge_method": "S256", "state": "STATE", "nonce": "STATE"}


@pytest.mark.parametrize("pasted", [
    "authredirect://com.lfp.laligafantasy?state=S1&code=THE-CODE",
    "  authredirect://com.lfp.laligafantasy/?code=THE-CODE&state=S1\n",
    "Failed to launch 'authredirect://com.lfp.laligafantasy?state=S1&code=THE-CODE' because the scheme "
    "does not have a registered handler.",
    "location: authredirect://com.lfp.laligafantasy#state=S1&code=THE-CODE",
    "code=THE-CODE&state=S1",
])
def test_the_code_is_found_however_it_was_copied(pasted):
    assert code_from_redirect(pasted, "S1") == "THE-CODE"


@pytest.mark.parametrize("pasted, problem", [
    ("authredirect://com.lfp.laligafantasy?state=S1&error=access_denied&error_description=AADB2C90091"
     "%3A+The+user+has+cancelled", "AADB2C90091: The user has cancelled"),
    ("authredirect://com.lfp.laligafantasy?state=OTHER&code=X", "different sign-in attempt"),
    ("https://miliga.laliga.com/", "doesn't contain a sign-in code"),
])
def test_unusable_pastes_say_why(pasted, problem):
    with pytest.raises(LoginError, match=problem):
        code_from_redirect(pasted, "S1")


async def test_the_code_is_swapped_for_tokens_with_the_verifier(tokens, store, freeze, router):
    new_id = make_jwt(NOW + 86400)
    router.add("POST", TOKEN_URL, httpx.Response(200, json={
        "id_token": new_id, "refresh_token": "rt-app", "refresh_token_expires_in": 1209600}))
    freeze.freeze("invalid_grant", "test")
    bundle = await tokens.login_with_code("THE-CODE", "VERIFIER", client_id=NATIVE_CLIENT_ID)
    (request,) = router.b2c()
    form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
    assert form == {"grant_type": "authorization_code", "client_id": NATIVE_CLIENT_ID, "code": "THE-CODE",
                    "redirect_uri": APP_REDIRECT_URI, "code_verifier": "VERIFIER",
                    "scope": "openid offline_access"}
    assert request.url.params["p"] == "B2C_1A_5ULAIP_PARAMETRIZED_SIGNIN"
    assert (bundle.client_id, bundle.refresh_token, bundle.id_token) == (NATIVE_CLIENT_ID, "rt-app", new_id)
    assert store.load().refresh_token == "rt-app" and not freeze.is_frozen()


async def test_a_refused_code_stores_nothing(tokens, store, router):
    router.add("POST", TOKEN_URL, httpx.Response(400, json={
        "error": "invalid_grant", "error_description": "AADB2C90080: The provided grant has expired."}))
    with pytest.raises(TokenImportError, match="didn't accept the sign-in code.*AADB2C90080"):
        await tokens.login_with_code("OLD", "VERIFIER", client_id=NATIVE_CLIENT_ID)
    assert store.load() is None


# -- the command ------------------------------------------------------------------------------


def run_login(tmp_path, monkeypatch, router, capsys, reply, extra=()):
    import functools

    from cryptography.fernet import Fernet

    import fantasybot.cli as cli
    from fantasybot.runtime import open_runtime

    monkeypatch.setenv("FANTASYBOT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("FANTASYBOT_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("FANTASYBOT_REQUEST_DELAY", "0")
    monkeypatch.setattr(cli, "open_runtime",
                        functools.partial(open_runtime, transport=httpx.MockTransport(router)))
    seen = {}

    def paste(prompt):
        printed = capsys.readouterr().out
        seen["printed"] = printed
        url = next(line.strip() for line in printed.splitlines() if "/authorize?" in line)
        seen["state"] = parse_qs(urlsplit(url).query)["state"][0]
        return reply(seen["state"])

    monkeypatch.setattr(cli, "read_paste", paste)
    code = cli.main(["auth", "login", *extra])
    out = capsys.readouterr()
    return code, seen, out.out + out.err


def test_auth_login_end_to_end(tmp_path, monkeypatch, router, fake, capsys):
    router.add("POST", TOKEN_URL, httpx.Response(200, json={
        "id_token": make_jwt(2_000_000_000), "refresh_token": "rt-app"}))
    code, seen, out = run_login(tmp_path, monkeypatch, router, capsys,
                                lambda state: f"authredirect://com.lfp.laligafantasy?state={state}&code=C1")
    assert code == 0, out
    assert "Sign in the way the official LaLiga Fantasy app does" in seen["printed"]
    assert "✅ LaLiga Fantasy accepts it: 1 league(s)" in out and "native/Google" in out
    assert "rt-app" not in out and "C1" not in out  # no tokens or codes on screen


def test_auth_login_says_when_laliga_still_refuses(tmp_path, monkeypatch, router, fake, capsys):
    router.add("POST", TOKEN_URL, httpx.Response(200, json={
        "id_token": make_jwt(2_000_000_000), "refresh_token": "rt-app"}))
    fake.overrides["current_user"] = httpx.Response(401, json={"message": "Invalid credentials"})
    code, _, out = run_login(tmp_path, monkeypatch, router, capsys,
                             lambda state: f"authredirect://com.lfp.laligafantasy?state={state}&code=C1")
    assert code == 1 and "❌ LaLiga Fantasy still refuses it (HTTP 401: Invalid credentials)" in out


def test_auth_login_rejects_a_paste_from_another_attempt(tmp_path, monkeypatch, router, fake, capsys):
    code, _, out = run_login(tmp_path, monkeypatch, router, capsys,
                             lambda state: "authredirect://com.lfp.laligafantasy?state=stale&code=C1")
    assert code == 1 and "different sign-in attempt" in out and router.b2c() == []


def test_handoff_explains_when_the_browser_never_sends_the_address(tmp_path, monkeypatch, router, fake,
                                                                  capsys):
    code, seen, out = run_login(tmp_path, monkeypatch, router, capsys, lambda state: "", ["--handoff"])
    assert code == 1 and "choose Open" in seen["printed"]
    assert "The browser never handed over the sign-in address" in out and router.b2c() == []
