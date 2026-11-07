"""Runtime settings, read from FANTASYBOT_* environment variables.

Secrets (the token encryption key and the Telegram bot token) can be given
directly or as a path to a file (``*_FILE``). The file form is preferred: it
keeps secrets out of the process environment, and it is what systemd's
LoadCredential= hands to the service (see deploy/).
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

# B2C client IDs used by LaLiga. A refresh must use the client that issued the
# token: the miliga.laliga.com web login issues tokens to WEB_CLIENT_ID, the
# mobile app / Google PKCE flow to NATIVE_CLIENT_ID.
WEB_CLIENT_ID = "6457fa17-1224-416a-b21a-ee6ce76e9bc0"
NATIVE_CLIENT_ID = "af88bcff-1157-40a0-b579-030728aacf0b"
KNOWN_CLIENT_IDS = (WEB_CLIENT_ID, NATIVE_CLIENT_ID)

BROWSER_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


class ConfigError(Exception):
    """Settings are missing or unusable."""


def _default_state_dir() -> Path:
    # systemd's StateDirectory= exports $STATE_DIRECTORY.
    systemd_dir = os.environ.get("STATE_DIRECTORY")
    if systemd_dir:
        return Path(systemd_dir.split(":")[0])
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "fantasybot"


def _read_secret_file(path: Path, what: str) -> str:
    try:
        info = path.stat()
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"cannot read {what} file {path}: {exc.strerror}") from None
    # Group-readable is the intended setup (root:fantasybot 640); world-readable is not.
    if info.st_mode & stat.S_IRWXO:
        log.warning("%s file %s is readable by every user on this machine; run: chmod o-rwx %s",
                    what, path, path)
    if not value:
        raise ConfigError(f"{what} file {path} is empty")
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FANTASYBOT_", extra="ignore")

    # Which game and league to talk to.
    competition_id: int = 1  # 1 = LaLiga EA Sports
    league_id: str | None = None  # default: the first league on the account
    team_id: str | None = None  # default: discovered from the league standing

    # Every write is simulated unless this is explicitly turned off.
    dry_run: bool = True

    # LaLiga endpoints and the headers LaLigaApp's proxy adds.
    api_base_url: str = "https://fantasy-api.llt-services.com/api"
    stats_base_url: str = "https://fantasy-api.llt-services.com/stats"
    b2c_token_url: str = (
        "https://login.laliga.es/laligadspprob2c.onmicrosoft.com/oauth2/v2.0/token"
    )
    b2c_policy: str = "B2C_1A_5ULAIP_PARAMETRIZED_SIGNIN"
    default_client_id: str = WEB_CLIENT_ID
    lang: str = "es"
    x_app: str = "2"
    user_agent: str = BROWSER_USER_AGENT

    # Behaviour.
    http_timeout: float = 15.0
    refresh_margin_seconds: int = 300  # refresh when the id_token has < 5 min left
    refresh_attempts: int = 3  # tries for transient B2C failures before freezing
    refresh_token_warn_days: float = 3.0
    request_delay: float = 0.5  # pause between health-check calls (API rate-limits)

    # Storage and secrets.
    state_dir: Path = Field(default_factory=_default_state_dir)
    token_key: SecretStr | None = None
    token_key_file: Path | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_bot_token_file: Path | None = None
    telegram_chat_id: str | None = None
    telegram_api_base: str = "https://api.telegram.org"

    @property
    def token_path(self) -> Path:
        return self.state_dir / "tokens.enc"

    @property
    def freeze_path(self) -> Path:
        return self.state_dir / "frozen.json"

    def resolved_token_key(self) -> bytes:
        if self.token_key is not None:
            return self.token_key.get_secret_value().strip().encode()
        if self.token_key_file is not None:
            return _read_secret_file(self.token_key_file, "token key").encode()
        raise ConfigError(
            "no token encryption key: set FANTASYBOT_TOKEN_KEY_FILE (preferred) or "
            "FANTASYBOT_TOKEN_KEY. Generate one with `fantasybot auth gen-key`."
        )

    def resolved_telegram_token(self) -> str | None:
        if self.telegram_bot_token is not None:
            return self.telegram_bot_token.get_secret_value().strip() or None
        if self.telegram_bot_token_file is not None:
            return _read_secret_file(self.telegram_bot_token_file, "Telegram bot token")
        return None
