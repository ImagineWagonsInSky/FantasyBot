"""Scrub secrets from text before it reaches a log line or a Telegram message."""

from __future__ import annotations

import re

# JWTs and B2C's JWE refresh tokens all start with a base64url "{" header.
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]*){0,4}")
# Telegram bot tokens: <bot id>:<35 chars>.
_TELEGRAM = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")
# Bearer headers that slipped into an error message.
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")

# API keys in a URL's query string (The Odds API's apiKey=) or a token header.
_QUERY_KEY = re.compile(r"(?i)\b(api_?key|x-auth-token|token|key)([=:]\s*)[A-Za-z0-9._~+/-]{12,}")

REDACTED = "[redacted]"


def redact(text: str, *secrets: str | None) -> str:
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, REDACTED)
    text = _BEARER.sub(f"Bearer {REDACTED}", text)
    text = _QUERY_KEY.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _JWT.sub(REDACTED, text)
    return _TELEGRAM.sub(REDACTED, text)
