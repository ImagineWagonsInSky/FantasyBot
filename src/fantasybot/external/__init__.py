"""Data from outside LaLiga Fantasy: match odds, real fixtures, probable lineups.

Each source is optional and cached per day in the state DB, so the hourly
guard never spends API credits: only the morning run fetches.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..config import Settings
from ..redact import redact
from ..scout.history import PriceHistory
from .football_data import trim

log = logging.getLogger(__name__)


class ExternalError(Exception):
    def __init__(self, source: str, message: str) -> None:
        super().__init__(f"{source}: {message}")
        self.source = source
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ExternalData:
    def __init__(self, http: httpx.AsyncClient, settings: Settings, *,
                 clock: Callable[[], datetime] = _now) -> None:
        self._http = http
        self._settings = settings
        self._clock = clock
        self.credits_left: str | None = None  # The Odds API's x-requests-remaining

    def credits(self) -> float | None:
        try:
            return float(self.credits_left) if self.credits_left else None
        except ValueError:
            return None

    @property
    def odds_enabled(self) -> bool:
        return self._settings.optional_secret("odds_api_key") is not None

    @property
    def results_enabled(self) -> bool:
        return self._settings.optional_secret("football_data_token") is not None

    @property
    def lineups_enabled(self) -> bool:
        return self._settings.lineups

    async def cached(self, source: str, fetch: Callable[[], Awaitable[Any]], *, refresh: bool,
                     max_age: timedelta) -> Any:
        """Today's copy if fresh enough; otherwise fetch (only when refresh is allowed)."""
        now = self._clock()
        today = now.astimezone(self._settings.display_tz()).date()
        with PriceHistory(self._settings.state_dir / "history.sqlite") as store:
            hit = store.cached(source, today) or (None if refresh else store.latest_cached(source))
        if hit is not None:
            fetched_at, body = hit
            if not refresh or now - datetime.fromisoformat(fetched_at) < max_age:
                return json.loads(body)
        if not refresh:
            return None
        data = await fetch()
        with PriceHistory(self._settings.state_dir / "history.sqlite") as store:
            store.put_cached(source, today, now.isoformat(), json.dumps(data))
        return data

    async def _get(self, source: str, url: str, *, params: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None, secret: str | None = None,
                   as_json: bool = True) -> Any:
        try:
            response = await self._http.get(url, params=params, headers=headers,
                                            timeout=self._settings.http_timeout)
        except httpx.HTTPError as exc:
            raise ExternalError(source, redact(f"network error: {type(exc).__name__}", secret)) from None
        if response.status_code == 401:
            raise ExternalError(source, "HTTP 401: check the key or token")
        if response.status_code == 403:
            raise ExternalError(source, "HTTP 403: the key, or its free plan, doesn't allow this")
        if response.status_code == 429:
            raise ExternalError(source, "HTTP 429: over the free plan's limit")
        if response.status_code != 200:
            raise ExternalError(source, f"HTTP {response.status_code}")
        if source == "odds":
            self.credits_left = response.headers.get("x-requests-remaining")
        if not as_json:
            return response.text
        try:
            return response.json()
        except ValueError:
            raise ExternalError(source, "the answer wasn't JSON") from None

    # -- The Odds API ----------------------------------------------------------

    async def odds_events(self, *, refresh: bool = True) -> list[dict[str, Any]] | None:
        key = self._settings.optional_secret("odds_api_key")
        if key is None:
            return None

        async def fetch() -> list[dict[str, Any]]:
            s = self._settings
            data = await self._get("odds", f"{s.odds_api_base.rstrip('/')}/v4/sports/{s.odds_sport}/odds",
                                   params={"apiKey": key, "regions": s.odds_regions, "markets": "h2h,totals",
                                           "oddsFormat": "decimal", "dateFormat": "iso"}, secret=key)
            if not isinstance(data, list):
                raise ExternalError("odds", "expected a list of matches")
            return data

        return await self.cached("odds", fetch, refresh=refresh, max_age=timedelta(hours=6))

    async def odds_status(self) -> str:
        """For the health check: the key works and LaLiga is listed. /v4/sports is free."""
        key = self._settings.optional_secret("odds_api_key")
        if key is None:
            raise ExternalError("odds", "no key set up")
        s = self._settings
        sports = await self._get("odds", f"{s.odds_api_base.rstrip('/')}/v4/sports",
                                 params={"apiKey": key}, secret=key)
        sport = next((x for x in sports if isinstance(x, dict) and x.get("key") == s.odds_sport), None) \
            if isinstance(sports, list) else None
        if sport is None:
            raise ExternalError("odds", f"{s.odds_sport} isn't offered (check FANTASYBOT_ODDS_SPORT)")
        if not sport.get("active"):
            raise ExternalError("odds", "no LaLiga matches are listed right now")
        return f"credits left this month: {self.credits_left}" if self.credits_left else "key works"

    # -- football-data.org -------------------------------------------------------

    def _football_data(self) -> tuple[str, str, dict[str, str]] | None:
        token = self._settings.optional_secret("football_data_token")
        if token is None:
            return None
        s = self._settings
        base = f"{s.football_data_base.rstrip('/')}/v4/competitions/{s.football_data_competition}"
        return token, base, {"X-Auth-Token": token}

    async def real_matches(self, season: int, *, refresh: bool = True) -> dict[str, Any] | None:
        """The season's fixtures and results, trimmed (see football_data.trim)."""
        setup = self._football_data()
        if setup is None:
            return None
        token, base, headers = setup

        async def fetch() -> dict[str, Any]:
            data = await self._get("football-data.org", f"{base}/matches", params={"season": season},
                                   headers=headers, secret=token)
            if not isinstance(data, dict) or not isinstance(data.get("matches"), list):
                raise ExternalError("football-data.org", "expected a list of matches")
            return trim(data)

        return await self.cached("football-data", fetch, refresh=refresh, max_age=timedelta(hours=6))

    async def football_data_status(self) -> str:
        """For the health check: the token works and the competition is covered (1 call)."""
        setup = self._football_data()
        if setup is None:
            raise ExternalError("football-data.org", "no token set up")
        token, base, headers = setup
        data = await self._get("football-data.org", base, headers=headers, secret=token)
        season = (data.get("currentSeason") if isinstance(data, dict) else None) or {}
        if not season:
            raise ExternalError("football-data.org", "the competition has no current season")
        return f"season from {season.get('startDate')}, matchday {season.get('currentMatchday')}"
