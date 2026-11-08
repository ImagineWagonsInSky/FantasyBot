"""Typed client for the LaLiga Fantasy API.

Every function in LaLigaApp's src/services/api.js (GPL-3.0-or-later,
(c) 2025–present Externoak) has a method here with the same path, method and
body, except:

* scrapeTeamLineup: it calls LaLigaApp's own local scraper for
  futbolfantasy.com, not LaLiga's API (external/lineups.py reads those pages
  directly, with LaLigaApp's parser ported);
* shieldPlayer: its body tells LaLiga a rewarded ad was watched
  (rewardedAd: 1). The read-only check_player_shield is kept.

Writes are simulated unless settings.dry_run is False, are refused while the
write freeze is on, and are never retried on failure: a retried bid could go
through twice. The only automatic retry for a write is after a 401, when
the request was rejected before doing anything.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar

import httpx
from pydantic import TypeAdapter, ValidationError

from ..auth import FreezeGuard, TokenManager
from ..config import Settings
from ..redact import redact
from . import adapters
from .adapters import ResponseShapeError
from .models import (
    ActivityEntry,
    CurrentWeek,
    Id,
    LaLigaTeam,
    League,
    Lineup,
    LineupUpdate,
    MarketItem,
    Match,
    MatchStats,
    Offer,
    Player,
    PlayerDetails,
    PremiumConfigItem,
    ShieldCheck,
    StandingEntry,
    TeamData,
    TeamMoney,
    User,
    WriteResult,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

READ_ATTEMPTS = 3
MAX_RETRY_AFTER = 30.0
TEAMS_MASTER_TTL = 600  # seconds, as in LaLigaApp
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

ResponseHook = Callable[[str, str, Any], None]


class ApiError(Exception):
    def __init__(self, endpoint: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{endpoint}: {message}")
        self.endpoint = endpoint
        self.message = message
        self.status = status


def _seg(value: Id) -> str:
    """Path segments come from API data or config; refuse anything that could
    change the path (``/``, ``..``, ``?``)."""
    text = str(value)
    if not _SAFE_SEGMENT.match(text):
        raise ValueError(f"unsafe path segment {text!r}")
    return text


def _validate(endpoint: str, adapter: TypeAdapter[T], data: Any) -> T:
    try:
        return adapter.validate_python(data)
    except ValidationError as exc:
        # include_input=False: error text must not carry response values (PII).
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or '<root>'}: {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False)[:5]
        )
        more = f" (+{exc.error_count() - 5} more)" if exc.error_count() > 5 else ""
        raise ResponseShapeError(endpoint, problems + more) from None


def _adapter(tp: Any) -> TypeAdapter[Any]:
    return TypeAdapter(tp)


_USER = _adapter(User)
_LEAGUES = _adapter(list[League])
_STANDING = _adapter(list[StandingEntry])
_ACTIVITY = _adapter(list[ActivityEntry])
_TEAM = _adapter(TeamData)
_LINEUP = _adapter(Lineup)
_ANY_LIST = _adapter(list[Any])
_PREMIUM = _adapter(list[PremiumConfigItem])
_MARKET = _adapter(list[MarketItem])
_PLAYERS = _adapter(list[Player])
_MATCHES = _adapter(list[Match])
_WEEK = _adapter(CurrentWeek)
_STATS = _adapter(list[MatchStats])
_MONEY = _adapter(TeamMoney)
_OFFERS = _adapter(list[Offer])
_DETAILS = _adapter(PlayerDetails)
_TEAMS_MASTER = _adapter(list[LaLigaTeam])


class FantasyClient:
    def __init__(
        self,
        settings: Settings,
        tokens: TokenManager,
        freeze: FreezeGuard,
        http: httpx.AsyncClient,
        *,
        on_response: ResponseHook | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._tokens = tokens
        self._freeze = freeze
        self._http = http
        self._on_response = on_response
        self._sleep = sleep
        self._clock = clock
        self._cmp = f"/v1/competition/{_seg(settings.competition_id)}"
        self._teams_master: dict[str, Mapping[str, Any]] | None = None
        self._teams_master_at = 0.0

    @property
    def dry_run(self) -> bool:
        return self._settings.dry_run

    # -- transport -------------------------------------------------------

    def _url(self, path: str, base: str = "api") -> str:
        root = self._settings.api_base_url if base == "api" else self._settings.stats_base_url
        return root.rstrip("/") + path

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "x-app": self._settings.x_app,
            "x-lang": self._settings.lang,
            "User-Agent": self._settings.user_agent,
        }

    async def _send(self, method: str, url: str, params: dict[str, Any] | None,
                    body: dict[str, Any] | None) -> httpx.Response:
        query = {**(params or {}), "x-lang": self._settings.lang}
        token = await self._tokens.bearer()
        for attempt in (1, 2):
            kwargs: dict[str, Any] = {"params": query, "headers": self._headers(token),
                                      "timeout": self._settings.http_timeout}
            if body is not None:
                kwargs["json"] = body  # sets Content-Type; bodiless requests send none
            response = await self._http.request(method, url, **kwargs)
            if response.status_code != 401 or attempt == 2:
                return response
            log.info("%s %s answered 401; refreshing the session once", method, url)
            token = await self._tokens.refresh_after_rejection(token)
        raise AssertionError("unreachable")

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        message = ""
        try:
            data = response.json()
            if isinstance(data, Mapping):
                message = str(data.get("message") or data.get("error") or "")
        except ValueError:
            pass
        return redact(f"HTTP {response.status_code}" + (f": {message[:200]}" if message else ""))

    async def _get(self, endpoint: str, path: str, *, params: dict[str, Any] | None = None,
                   base: str = "api") -> Any:
        url = self._url(path, base)
        response: httpx.Response | None = None
        for attempt in range(1, READ_ATTEMPTS + 1):
            try:
                response = await self._send("GET", url, params, None)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == READ_ATTEMPTS:
                    raise ApiError(endpoint, f"network error: {type(exc).__name__}") from None
                await self._sleep(2 ** (attempt - 1))
                continue
            if (response.status_code == 429 or response.status_code >= 500) and attempt < READ_ATTEMPTS:
                await self._sleep(self._retry_delay(response, attempt))
                continue
            break
        assert response is not None
        if not response.is_success:
            raise ApiError(endpoint, self._error_message(response), response.status_code)
        if not response.content.strip():
            data: Any = None
        else:
            try:
                data = response.json()
            except ValueError:
                raise ApiError(endpoint, f"HTTP {response.status_code} with a non-JSON body",
                               response.status_code) from None
        if self._on_response is not None:
            self._on_response(endpoint, path, data)
        return data

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        header = response.headers.get("retry-after", "")
        if header.isdigit():
            return min(float(header), MAX_RETRY_AFTER)
        return float(2 ** (attempt - 1))

    async def _write(self, endpoint: str, method: str, path: str,
                     body: dict[str, Any] | None = None) -> WriteResult:
        url = self._url(path)
        if self._settings.dry_run:
            log.info("[dry run] %s %s %s", method, path, body if body is not None else "(no body)")
            return WriteResult(dry_run=True, method=method, url=url, body=body)
        await self._tokens.bearer()  # may freeze if the session can't be renewed
        self._freeze.check()
        response = await self._send(method, url, None, body)
        if not response.is_success:
            raise ApiError(endpoint, self._error_message(response), response.status_code)
        data: Any = None
        if response.content.strip():
            try:
                data = response.json()
            except ValueError:
                data = response.text[:500]
        log.info("%s %s -> HTTP %s", method, path, response.status_code)
        return WriteResult(dry_run=False, method=method, url=url, body=body,
                           status_code=response.status_code, data=data)

    # -- teams master (used by the adapters) -----------------------------

    async def get_teams_master(self) -> list[LaLigaTeam]:
        teams = adapters.extract_teams_master(await self._get("teams_master", "/v3/teams-master"))
        result = _validate("teams_master", _TEAMS_MASTER, teams)
        self._teams_master = adapters.teams_by_id(teams)
        self._teams_master_at = self._clock()
        return result

    async def _teams_map(self) -> Mapping[str, Mapping[str, Any]]:
        fresh = self._clock() - self._teams_master_at < TEAMS_MASTER_TTL
        if self._teams_master is None or not fresh:
            try:
                await self.get_teams_master()
            except (ApiError, ResponseShapeError) as exc:
                # Same graceful degradation as LaLigaApp: enrich nothing, retry next time.
                log.warning("teams-master unavailable, players and matches stay un-enriched: %s", exc)
                return {}
        return self._teams_master or {}

    # -- reads -----------------------------------------------------------

    async def get_current_user(self) -> User:
        return _validate("current_user", _USER, await self._get("current_user", "/v4/user/me"))

    async def get_leagues(self) -> list[League]:
        data = await self._get("leagues", f"{self._cmp}/leagues")
        return _validate("leagues", _LEAGUES, adapters.extract_list("leagues", data))

    async def get_league_ranking(self, league_id: Id) -> list[StandingEntry]:
        data = await self._get("league_ranking", f"{self._cmp}/leagues/{_seg(league_id)}/standing")
        rows = [adapters.normalize_standing_entry(e) for e in adapters.extract_list("league_ranking", data)]
        return _validate("league_ranking", _STANDING, rows)

    async def get_league_ranking_by_week(self, league_id: Id, week: int) -> list[StandingEntry]:
        endpoint = "league_ranking_by_week"
        data = await self._get(endpoint, f"{self._cmp}/leagues/{_seg(league_id)}/standing/{_seg(week)}")
        rows = [adapters.normalize_standing_entry(e) for e in adapters.extract_list(endpoint, data)]
        return _validate(endpoint, _STANDING, rows)

    async def get_league_activity(self, league_id: Id, index: int = 0) -> list[ActivityEntry]:
        data = await self._get("league_activity",
                               f"{self._cmp}/leagues/{_seg(league_id)}/activity/{_seg(index)}")
        return _validate("league_activity", _ACTIVITY, adapters.extract_list("league_activity", data))

    async def get_team_data(self, league_id: Id, team_id: Id) -> TeamData:
        data = await self._get("team_data", f"{self._cmp}/leagues/{_seg(league_id)}/teams/{_seg(team_id)}")
        return _validate("team_data", _TEAM, data)

    async def get_team_lineup(self, team_id: Id, week: int) -> Lineup:
        data = await self._get("team_lineup",
                               f"{self._cmp}/teams/{_seg(team_id)}/lineup/week/{_seg(week)}")
        return _validate("team_lineup", _LINEUP, data)

    async def get_current_lineup(self, team_id: Id) -> Lineup:
        data = await self._get("current_lineup", f"{self._cmp}/teams/{_seg(team_id)}/lineup")
        return _validate("current_lineup", _LINEUP, data)

    async def get_free_formations(self) -> list[Any]:
        data = await self._get("free_formations", "/v4/teams/lineup/formations", params={"option": "free"})
        return _validate("free_formations", _ANY_LIST, adapters.extract_list("free_formations", data))

    async def get_premium_formations(self) -> list[Any]:
        data = await self._get("premium_formations", "/v4/teams/lineup/formations",
                               params={"option": "premium"})
        return _validate("premium_formations", _ANY_LIST,
                         adapters.extract_list("premium_formations", data))

    async def get_premium_configuration(self) -> list[PremiumConfigItem]:
        data = await self._get("premium_configuration", "/v4/leagues/premium-configuration")
        return _validate("premium_configuration", _PREMIUM,
                         adapters.extract_list("premium_configuration", data))

    async def get_market(self, league_id: Id) -> list[MarketItem]:
        data = await self._get("market", f"{self._cmp}/league/{_seg(league_id)}/market")
        return _validate("market", _MARKET, adapters.extract_list("market", data))

    async def get_all_players(self) -> list[Player]:
        players = adapters.extract_list("players", await self._get("players", f"{self._cmp}/players"))
        teams = await self._teams_map()
        return _validate("players", _PLAYERS, [adapters.normalize_player(p, teams) for p in players])

    async def get_matchday(self, week: int) -> list[Match]:
        data = await self._get("matchday", f"{self._cmp}/calendar", params={"weekNumber": week})
        matches = adapters.extract_list("matchday", data)
        if adapters.calendar_needs_teams(matches):
            teams = await self._teams_map()
            matches = [adapters.resolve_match_teams(m, teams) for m in matches]
        return _validate("matchday", _MATCHES, matches)

    async def get_current_week(self) -> CurrentWeek:
        data = await self._get("current_week", f"{self._cmp}/week/current")
        if isinstance(data, Mapping) and isinstance(data.get("data"), Mapping):
            data = data["data"]  # LaLigaApp's extractWeekNumber also looks one level down
        return _validate("current_week", _WEEK, data)

    async def get_match_stats(self, week: int) -> list[MatchStats]:
        data = await self._get("match_stats", f"/v1/competition/{_seg(self._settings.competition_id)}"
                               f"/stats/week/{_seg(week)}", base="stats")
        return _validate("match_stats", _STATS, adapters.extract_list("match_stats", data))

    async def get_team_money(self, team_id: Id) -> TeamMoney:
        data = await self._get("team_money", f"{self._cmp}/teams/{_seg(team_id)}/money")
        return _validate("team_money", _MONEY, data)

    async def get_player_offer(self, league_id: Id, player_team_id: Id) -> list[Offer]:
        data = await self._get("player_offer", f"{self._cmp}/league/{_seg(league_id)}"
                               f"/playerTeam/{_seg(player_team_id)}/offer")
        if data is None:
            return []
        return _validate("player_offer", _OFFERS, adapters.extract_list("player_offer", data))

    async def get_player_details(self, player_id: Id, league_id: Id) -> PlayerDetails:
        data = await self._get("player_details",
                               f"{self._cmp}/player/{_seg(player_id)}/league/{_seg(league_id)}")
        return _validate("player_details", _DETAILS, data)

    async def check_player_shield(self, league_id: Id, player_team_id: Id) -> ShieldCheck:
        """2xx: can be shielded. 400: can't right now (a normal answer, not an error)."""
        try:
            data = await self._get("check_player_shield", f"{self._cmp}/league/{_seg(league_id)}"
                                   f"/player-team/{_seg(player_team_id)}/check-shield")
        except ApiError as exc:
            if exc.status == 400:
                return ShieldCheck(shieldable=False, message=exc.message)
            raise
        return ShieldCheck(shieldable=True, data=data)

    # -- writes ----------------------------------------------------------

    async def update_lineup(self, team_id: Id, lineup: LineupUpdate) -> WriteResult:
        return await self._write("update_lineup", "PUT", f"{self._cmp}/teams/{_seg(team_id)}/lineup",
                                 lineup.model_dump())

    async def make_bid(self, league_id: Id, market_id: Id, money: int) -> WriteResult:
        return await self._write("make_bid", "POST",
                                 f"{self._cmp}/league/{_seg(league_id)}/market/{_seg(market_id)}/bid",
                                 {"money": money})

    async def cancel_bid(self, league_id: Id, market_id: Id, bid_id: Id) -> WriteResult:
        return await self._write("cancel_bid", "DELETE", f"{self._cmp}/league/{_seg(league_id)}"
                                 f"/market/{_seg(market_id)}/bid/{_seg(bid_id)}/cancel")

    async def modify_bid(self, league_id: Id, market_id: Id, bid_id: Id, money: int) -> WriteResult:
        return await self._write("modify_bid", "PUT", f"{self._cmp}/league/{_seg(league_id)}"
                                 f"/market/{_seg(market_id)}/bid/{_seg(bid_id)}", {"money": money})

    async def accept_offer(self, league_id: Id, market_id: Id, offer_id: Id,
                           offer_money: int) -> WriteResult:
        # The API requires the amount as confirmation; without it it answers 400.
        return await self._write("accept_offer", "POST", f"{self._cmp}/league/{_seg(league_id)}"
                                 f"/market/{_seg(market_id)}/offer/{_seg(offer_id)}/accept",
                                 {"offerMoney": offer_money})

    async def decline_offer(self, league_id: Id, market_id: Id, offer_id: Id) -> WriteResult:
        # No body and no Content-Type: the API answers 400 to an empty JSON body.
        return await self._write("decline_offer", "POST", f"{self._cmp}/league/{_seg(league_id)}"
                                 f"/market/{_seg(market_id)}/offer/{_seg(offer_id)}/reject")

    async def increase_buyout_clause(self, league_id: Id, player_team_id: Id, factor: Any,
                                     value_to_increase: int) -> WriteResult:
        """Raise one of your clauses. LaLigaApp sends the playerTeamId as playerId,
        factor 2.0, and the clause increase; you pay half of the increase."""
        return await self._write("increase_buyout_clause", "PUT",
                                 f"{self._cmp}/league/{_seg(league_id)}/buyout/player",
                                 {"factor": factor, "playerId": player_team_id,
                                  "valueToIncrease": value_to_increase})

    async def pay_buyout_clause(self, league_id: Id, player_team_id: Id,
                                buyout_clause_to_pay: int) -> WriteResult:
        """Take a rival's player by paying his clause. The path takes the
        playerTeamId (as LaLigaApp's Clauses page sends it)."""
        return await self._write("pay_buyout_clause", "POST",
                                 f"{self._cmp}/league/{_seg(league_id)}/buyout/{_seg(player_team_id)}/pay",
                                 {"buyoutClauseToPay": buyout_clause_to_pay})

    async def sell_player_to_market(self, league_id: Id, player_team_id: Id, sale_price: int) -> WriteResult:
        """List one of your players. LaLigaApp sends the playerTeamId as playerId."""
        return await self._write("sell_player_to_market", "POST",
                                 f"{self._cmp}/league/{_seg(league_id)}/market/sell",
                                 {"playerId": player_team_id, "salePrice": sale_price})

    async def withdraw_player_from_market(self, league_id: Id, market_id: Id) -> WriteResult:
        return await self._write("withdraw_player_from_market", "DELETE",
                                 f"{self._cmp}/league/{_seg(league_id)}/market/{_seg(market_id)}/delete")

    async def make_direct_offer(self, league_id: Id, player_id: Id, money: int) -> WriteResult:
        return await self._write("make_direct_offer", "POST",
                                 f"{self._cmp}/league/{_seg(league_id)}/market/direct-offer",
                                 {"playerId": player_id, "money": money})

    async def cancel_offer(self, league_id: Id, market_id: Id, offer_id: Id) -> WriteResult:
        return await self._write("cancel_offer", "DELETE", f"{self._cmp}/league/{_seg(league_id)}"
                                 f"/market/{_seg(market_id)}/offer/{_seg(offer_id)}/cancel")


# Every function in LaLigaApp's api.js, and where it lives here.
# None means deliberately not ported (see the module docstring).
PORTED_FROM_API_JS: dict[str, str | None] = {
    "getCurrentUser": "get_current_user",
    "getLeagues": "get_leagues",
    "getLeagueRanking": "get_league_ranking",
    "getLeagueRankingByWeek": "get_league_ranking_by_week",
    "getLeagueActivity": "get_league_activity",
    "getTeamData": "get_team_data",
    "getTeamLineup": "get_team_lineup",
    "getCurrentLineup": "get_current_lineup",
    "updateLineup": "update_lineup",
    "getFreeFormations": "get_free_formations",
    "getPremiumFormations": "get_premium_formations",
    "getPremiumConfiguration": "get_premium_configuration",
    "getMarket": "get_market",
    "getAllPlayers": "get_all_players",
    "getMatchday": "get_matchday",
    "getCurrentWeek": "get_current_week",
    "getMatchStats": "get_match_stats",
    "getTeamMoney": "get_team_money",
    "getPlayerOffer": "get_player_offer",
    "makeBid": "make_bid",
    "cancelBid": "cancel_bid",
    "modifyBid": "modify_bid",
    "acceptOffer": "accept_offer",
    "declineOffer": "decline_offer",
    "getPlayerDetails": "get_player_details",
    "scrapeTeamLineup": None,
    "increaseBuyoutClause": "increase_buyout_clause",
    "payBuyoutClause": "pay_buyout_clause",
    "sellPlayerToMarket": "sell_player_to_market",
    "withdrawPlayerFromMarket": "withdraw_player_from_market",
    "makeDirectOffer": "make_direct_offer",
    "cancelOffer": "cancel_offer",
    "checkPlayerShield": "check_player_shield",
    "shieldPlayer": None,
}
