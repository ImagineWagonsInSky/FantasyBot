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


class PlayerWeek(ApiModel):
    """One jornada in playerStats. Each stat is [raw value, points it scored],
    e.g. "saves": [4, 2]; the points add up to totalPoints."""

    weekNumber: int | None = None
    totalPoints: Number | None = None
    stats: dict[str, list[Number]] | None = None
    isInIdealFormation: bool | None = None

    @property
    def minutes(self) -> Number | None:
        played = (self.stats or {}).get("mins_played")
        return played[0] if played else None


class Player(ApiModel):
    """A player master record (/players, playerMaster objects, /player/{id})."""

    id: Id
    nickname: str | None = None
    name: str | None = None
    positionId: int | None = None  # 1 GK, 2 DEF, 3 MID, 4 FWD, 5 coach
    position: str | None = None  # "Portero", "Defensa", … "Entrenador"
    marketValue: int | None = None
    points: Number | None = None
    weekPoints: Number | None = None
    averagePoints: Number | None = None
    lastSeasonPoints: Number | None = None
    playerStatus: str | None = None  # "ok", "doubt", "injured", "suspended"
    teamId: Id | None = None
    team: TeamRef | None = None
    images: dict[str, Any] | None = None
    lastStats: list[Any] | None = None
    playerStats: list[PlayerWeek] | None = None

    @property
    def display_name(self) -> str:
        return self.nickname or self.name or str(self.id)


class ClauseInfo(ApiModel):
    """An owned player's clause state (playerTeam in player details)."""

    playerTeamId: Id | None = None
    buyoutClause: int | None = None
    buyoutClauseLockedEndTime: str | None = None
    isShielded: bool | None = None
    manager: StandingManager | None = None


class MarketPlayer(ApiModel):
    salePrice: int | None = None


class PlayerDetails(ApiModel):
    """/player/{id}/league/{id}: the player wrapped in `playerMaster`, plus its
    market listing in this league (if any) and past seasons."""

    playerMaster: Player
    marketPlayer: MarketPlayer | None = None
    seasons: list[Any] | None = None
    manager: StandingManager | None = None  # the owner in this league, if owned
    playerTeam: ClauseInfo | None = None

    @model_validator(mode="before")
    @classmethod
    def _wrap_a_flat_player(cls, value: Any) -> Any:
        # A bare player record (no playerMaster) is accepted too, as LaLigaApp's
        # name lookups do.
        if isinstance(value, dict) and "playerMaster" not in value and "id" in value:
            return {"playerMaster": value}
        return value


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
    """A league event. Types seen: 1 bought, 4 shielded, 6 jornada earnings,
    7 invalid lineup, 9 joined, 31 signed, 32 clause steal, 33 sold.
    user1 is the manager who acted; user2 the other side (e.g. the seller)."""

    activityTypeId: int
    id: Id | None = None
    createdAt: str | None = None
    timestamp: str | int | None = None
    amount: int | None = None
    playerMasterId: Id | None = None
    playerName: str | None = None
    user1Id: Id | None = None
    user1Name: str | None = None
    user2Id: Id | None = None
    user2Name: str | None = None
    description: str | None = None
    weekNumber: int | None = None


# -- a fantasy team ----------------------------------------------------------


class TeamPlayer(ApiModel):
    """A player owned by a fantasy team (a "playerTeam")."""

    playerTeamId: Id | None = None
    id: Id | None = None
    playerMaster: Player
    buyoutClause: int | None = None
    buyoutClauseLockedEndTime: str | None = None
    isShielded: bool | None = None
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
    buyoutClause: int | None = None
    buyoutClauseLockedEndTime: str | None = None
    isShielded: bool | None = None
    manager: StandingManager | None = None


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
    status: str | None = None
    directOffer: bool | None = None
    sellerTeam: dict[str, Any] | None = None  # id, teamValue, teamPoints; teamMoney is hidden

    @property
    def seller_team_id(self) -> Id | None:
        if self.sellerTeam and self.sellerTeam.get("id") is not None:
            return self.sellerTeam["id"]
        return self.playerTeam.teamId if self.playerTeam else None


class Offer(ApiModel):
    """An offer for one of your players. No bidderTeam: it comes from the market itself."""

    id: Id
    money: int | None = None
    status: str | None = None  # "pending" until you accept or decline
    expirationDate: str | None = None
    bidderTeam: TeamRef | None = None

    @property
    def open(self) -> bool:
        return (self.status or "pending").lower() == "pending"


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


PlayerDetails.model_rebuild()
