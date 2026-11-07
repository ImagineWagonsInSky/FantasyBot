"""LaLiga's Azure AD B2C token endpoint.

Ported from LaLigaApp's authService.refreshToken (GPL-3.0-or-later,
(c) 2025–present Externoak). Differences from the original: failures are
classified so the caller can tell a dead refresh token from a network blip.
"""

from __future__ import annotations

import base64
import json
from enum import Enum
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from ..config import Settings
from ..redact import redact

SCOPE = "openid offline_access"


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
    try:
        response = await http.post(
            settings.b2c_token_url,
            params={"p": settings.b2c_policy},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "scope": SCOPE,
            },
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
        raise RefreshError(kind, f"B2C refused the refresh (HTTP {status}): {description}",
                           status, code)

    try:
        return TokenResponse.model_validate(body)
    except ValidationError:
        raise RefreshError(RefreshFailure.BAD_RESPONSE,
                           "B2C answered without an id_token", status) from None


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
