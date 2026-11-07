"""Typed views of LaLiga Fantasy API responses.

No sample responses were available, so these models are built from the
fields LaLigaApp's UI reads. Two rules follow from that:

* every model accepts unknown fields (``extra="allow"``), because the API
  adds and moves fields between seasons;
* a field is required only where LaLigaApp's code relies on it without a
  fallback (mostly IDs). Missing required fields are what the health check
  reports as breakage.

IDs keep whatever type the API sent (int or str), because they are sent back
in request bodies and the API may care about the difference.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

Id = int | str
Number = int | float


def same_id(a: Id | None, b: Id | None) -> bool:
    return a is not None and b is not None and str(a) == str(b)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# -- users, leagues, teams ---------------------------------------------------


class User(ApiModel):
    id: Id | None = None
    userId: Id | None = None
    managerId: Id | None = None
    managerName: str | None = None
    displayName: str | None = None
    username: str | None = None
    name: str | None = None
    firstName: str | None = None
    lastName: str | None = None
    email: str | None = None
    avatar: str | None = None

    @model_validator(mode="after")
    def _has_an_id(self) -> User:
        if self.user_id is None:
            raise ValueError("user has none of id, userId, managerId")
        return self

    @property
    def user_id(self) -> Id | None:
        return next((v for v in (self.id, self.userId, self.managerId) if v is not None), None)


class League(ApiModel):
    id: Id
    name: str | None = None


class LaLigaTeam(ApiModel):
    """A real football club, from /v3/teams-master."""

    id: Id
    name: str | None = None
    shortName: str | None = None
    slug: str | None = None
    badgeColor: str | None = None
    badgeWhite: str | None = None


class TeamRef(ApiModel):
    id: Id | None = None
    name: str | None = None
    shortName: str | None = None
    slug: str | None = None
    badgeColor: str | None = None
    badgeWhite: str | None = None


# -- players -----------------------------------------------------------------


class Player(ApiModel):
    """A player master record (/players, playerMaster objects, /player/{id})."""

    id: Id
    nickname: str | None = None
    name: str | None = None
    positionId: int | None = None
    marketValue: int | None = None
    points: Number | None = None
    averagePoints: Number | None = None
    playerStatus: str | None = None
    teamId: Id | None = None
    team: TeamRef | None = None
    images: dict[str, Any] | None = None
    lastStats: list[Any] | None = None
    playerStats: list[Any] | None = None

    @property
    def display_name(self) -> str:
        return self.nickname or self.name or str(self.id)


class PlayerDetails(Player):
    pass


# -- standings and activity --------------------------------------------------


class StandingManager(ApiModel):
    id: Id | None = None
    managerName: str | None = None


class StandingTeam(ApiModel):
    id: Id
    name: str | None = None
    points: Number | None = None
    teamPoints: Number | None = None
    teamValue: int | None = None
    weekPoints: Number | None = None
    manager: StandingManager | None = None


class StandingEntry(ApiModel):
    """One row of a league standing, after LaLigaApp's normalisation."""

    position: int | None = None
    id: Id | None = None
    name: str | None = None
    points: Number | None = None
    teamValue: int | None = None
    weekPoints: Number | None = None
    userId: Id | None = None
    manager: str | None = None
    team: StandingTeam


class ActivityEntry(ApiModel):
    activityTypeId: int
    id: Id | None = None
    createdAt: str | None = None
    timestamp: str | int | None = None
    amount: int | None = None
    playerMasterId: Id | None = None


# -- a fantasy team ----------------------------------------------------------


class TeamPlayer(ApiModel):
    """A player owned by a fantasy team (a "playerTeam")."""

    playerTeamId: Id | None = None
    id: Id | None = None
    playerMaster: Player
    buyoutClause: int | None = None
    buyoutClauseLockedEndTime: str | None = None
    purchasePrice: int | None = None
    playerMarket: Any = None

    @property
    def player_team_id(self) -> Id | None:
        return self.playerTeamId if self.playerTeamId is not None else self.id


class TeamData(ApiModel):
    players: list[TeamPlayer]
    teamValue: int | None = None
    teamMoney: int | None = None
    manager: Any = None


class TeamMoney(ApiModel):
    """The body is a bare number, {teamMoney}, {money} or {amount}."""

    amount: int

    @model_validator(mode="before")
    @classmethod
    def _accept_every_shape(cls, value: Any) -> Any:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return {"amount": value}
        if isinstance(value, dict):
            for key in ("teamMoney", "money", "amount"):
                if isinstance(value.get(key), (int, float)) and not isinstance(value.get(key), bool):
                    return {**value, "amount": value[key]}
        return value


class LineupFormation(ApiModel):
    goalkeeper: list[Any] = Field(default_factory=list)
    defender: list[Any] = Field(default_factory=list)
    midfield: list[Any] = Field(default_factory=list)
    striker: list[Any] = Field(default_factory=list)
    tacticalFormation: list[int] | None = None


class Lineup(ApiModel):
    formation: LineupFormation | None = None


class PremiumConfigItem(ApiModel):
    key: str
    value: Any = None


# -- market and offers -------------------------------------------------------


class MarketBid(ApiModel):
    id: Id | None = None
    money: int | None = None
    status: str | None = None


class MarketPlayerTeam(ApiModel):
    playerTeamId: Id | None = None
    teamId: Id | None = None


class MarketItem(ApiModel):
    """A market listing. `id` is the marketId the bid endpoints take."""

    id: Id
    discr: str | None = None  # "marketPlayerLeague" (league) or "marketPlayerTeam" (a manager)
    playerMaster: Player
    playerTeam: MarketPlayerTeam | None = None
    salePrice: int | None = None
    expirationDate: str | None = None
    numberOfBids: int | None = None
    numberOfOffers: int | None = None
    bid: MarketBid | None = None
    sellerTeam: dict[str, Any] | None = None


class Offer(ApiModel):
    id: Id
    money: int | None = None
    status: str | None = None
    expirationDate: str | None = None
    bidderTeam: TeamRef | None = None


class ShieldCheck(ApiModel):
    """check-shield answers 2xx when the player can be shielded, 400 when not."""

    shieldable: bool
    message: str | None = None
    data: Any = None


# -- calendar and stats ------------------------------------------------------


class Match(ApiModel):
    id: Id
    local: TeamRef | None = None
    visitor: TeamRef | None = None
    localId: Id | None = None
    visitorId: Id | None = None
    localScore: int | None = None
    visitorScore: int | None = None
    matchState: int | str | None = None
    matchDate: str | None = None


class CurrentWeek(ApiModel):
    weekNumber: int | None = None
    week: int | None = None

    @model_validator(mode="after")
    def _has_a_week(self) -> CurrentWeek:
        if self.number is None:
            raise ValueError("neither weekNumber nor week is present")
        return self

    @property
    def number(self) -> int | None:
        return self.weekNumber if self.weekNumber is not None else self.week


class MatchStats(ApiModel):
    id: Id


# -- writes ------------------------------------------------------------------


class LineupUpdate(BaseModel):
    """Body of PUT /teams/{id}/lineup, as LaLigaApp's LineupEditor sends it."""

    goalkeeper: Id | None
    defender: list[Id]
    midfield: list[Id]
    striker: list[Id]
    tactical_formation: list[int]


class WriteResult(BaseModel):
    dry_run: bool
    method: str
    url: str
    body: dict[str, Any] | None = None
    status_code: int | None = None
    data: Any = None
