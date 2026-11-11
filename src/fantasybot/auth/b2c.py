"""LaLiga's Azure AD B2C token endpoint.

Ported from LaLigaApp's authService.refreshToken (GPL-3.0-or-later,
(c) 2025–present Externoak). Differences from the original: failures are
classified so the caller can tell a dead refresh token from a network blip.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from enum import Enum
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from ..config import Settings
from ..redact import redact

SCOPE = "openid offline_access"
# The official mobile app's return address. With the app's client ID, the browser sign-in
# (Authorization Code + PKCE, as in LaLigaApp's Google login) ends there with a one-time code.
APP_REDIRECT_URI = "authredirect://com.lfp.laligafantasy"


class TokenResponse(BaseModel):
    """What B2C returns. LaLiga's API takes the id_token as the bearer."""

    model_config = ConfigDict(extra="allow")

    id_token: str
    refresh_token: str | None = None
    access_token: str | None = None
    token_type: str | None = None
    expires_in: int | None = None
    expires_on: int | None = None
    id_token_expires_in: int | None = None
    refresh_token_expires_in: int | None = None

    def __repr__(self) -> str:  # never show token values
        return "TokenResponse(<redacted>)"

    __str__ = __repr__


class RefreshFailure(str, Enum):
    INVALID_GRANT = "invalid_grant"  # refresh token expired, revoked or for another client
    REJECTED = "rejected"  # B2C refused the request for another reason
    POLICY_UNAVAILABLE = "policy_unavailable"  # non-JSON answer: policy retired or moved
    BAD_RESPONSE = "bad_response"  # 2xx but no usable id_token
    TRANSIENT = "transient"  # network error, timeout, 429 or 5xx


class RefreshError(Exception):
    def __init__(self, kind: RefreshFailure, message: str, status: int | None = None,
                 error_code: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.error_code = error_code

    @property
    def transient(self) -> bool:
        return self.kind is RefreshFailure.TRANSIENT


def _json_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None


def _describe(body: dict[str, Any]) -> tuple[str | None, str]:
    error = body.get("error")
    description = str(body.get("error_description") or "")
    # B2C descriptions start with an AADB2C error code; keep the first line only.
    first_line = description.splitlines()[0] if description else ""
    code = first_line.split(":", 1)[0].strip() if first_line.startswith("AADB2C") else None
    return code, redact(first_line[:300]) if first_line else str(error or "unknown error")


async def refresh_tokens(
    http: httpx.AsyncClient, settings: Settings, refresh_token: str, client_id: str
) -> TokenResponse:
    """Redeem a refresh token. Raises RefreshError; never returns partial data."""
    return await _token_request(http, settings, {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "scope": SCOPE,
    }, "refresh")


async def exchange_code(http: httpx.AsyncClient, settings: Settings, client_id: str, code: str,
                        verifier: str, redirect_uri: str = APP_REDIRECT_URI) -> TokenResponse:
    """Swap the one-time code from the browser sign-in for tokens (needs the PKCE verifier)."""
    return await _token_request(http, settings, {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "scope": SCOPE,
    }, "sign-in")


async def _token_request(http: httpx.AsyncClient, settings: Settings, data: dict[str, str],
                         action: str) -> TokenResponse:
    try:
        response = await http.post(
            settings.b2c_token_url,
            params={"p": settings.b2c_policy},
            data=data,
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            timeout=settings.http_timeout,
        )
    except httpx.TimeoutException:
        raise RefreshError(RefreshFailure.TRANSIENT, "timed out talking to login.laliga.es") from None
    except httpx.TransportError as exc:
        raise RefreshError(RefreshFailure.TRANSIENT, f"network error: {type(exc).__name__}") from None

    status = response.status_code
    if status == 429 or status >= 500:
        raise RefreshError(RefreshFailure.TRANSIENT, f"B2C answered HTTP {status}", status)

    body = _json_body(response)
    if not isinstance(body, dict):
        raise RefreshError(
            RefreshFailure.POLICY_UNAVAILABLE,
            f"B2C answered HTTP {status} with a non-JSON body; the sign-in policy "
            f"{settings.b2c_policy} may have been retired",
            status,
        )

    if status >= 400:
        code, description = _describe(body)
        kind = (RefreshFailure.INVALID_GRANT if body.get("error") == "invalid_grant"
                else RefreshFailure.REJECTED)
        raise RefreshError(kind, f"B2C refused the {action} (HTTP {status}): {description}",
                           status, code)

    try:
        return TokenResponse.model_validate(body)
    except ValidationError:
        raise RefreshError(RefreshFailure.BAD_RESPONSE,
                           "B2C answered without an id_token", status) from None


# -- the browser sign-in (Authorization Code + PKCE) ---------------------------------


class LoginError(Exception):
    """What was pasted after the browser sign-in can't be used."""


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge). The verifier never leaves this process."""
    verifier = secrets.token_urlsafe(48)  # 64 characters
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(settings: Settings, client_id: str, challenge: str, state: str,
                  redirect_uri: str = APP_REDIRECT_URI) -> str:
    base = re.sub(r"/token$", "/authorize", settings.b2c_token_url.split("?")[0])
    return base + "?" + urlencode({
        "p": settings.b2c_policy, "client_id": client_id, "response_type": "code",
        "redirect_uri": redirect_uri, "scope": SCOPE, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": state, "nonce": state,
    })


def code_from_redirect(pasted: str, state: str) -> str:
    """The one-time code from the address the browser couldn't open (or the line holding it)."""
    text = pasted.strip()
    found = re.search(r"authredirect://[^\s'\"<>]+", text)
    target = urlsplit(found.group(0) if found else text)
    params = {**parse_qs(target.query), **parse_qs(target.fragment)}
    if not params:  # just "code=...&state=..."
        params = parse_qs(text)
    if "error" in params:
        detail = (params.get("error_description") or params["error"])[0]
        raise LoginError(f"LaLiga's sign-in answered with an error: {redact(detail)[:200]}")
    code = (params.get("code") or [None])[0]
    if not code:
        raise LoginError("that doesn't contain a sign-in code (an address with code=...)")
    if (params.get("state") or [None])[0] != state:
        raise LoginError("that code is from a different sign-in attempt; run `auth login` again")
    return code


def jwt_expiry(token: str) -> int | None:
    """Read the exp claim without verifying the signature.

    Only used to schedule refreshes; nothing trusts the claim for security.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except (ValueError, AttributeError):
        return None
    return int(exp) if isinstance(exp, (int, float)) else None


def id_token_expiry(response: TokenResponse, now: int) -> int:
    """Same precedence as LaLigaApp's calculateTokenExpiration, with the JWT's
    own exp claim first because it is the ground truth."""
    exp = jwt_expiry(response.id_token)
    if exp:
        return exp
    if response.expires_on:
        return response.expires_on
    if response.id_token_expires_in:
        return now + response.id_token_expires_in
    if response.expires_in:
        return now + response.expires_in
    return now + 86400
