"""Turns API data into decisions: best XI, readiness, clause targets, market picks.

Pure functions over the API models, so they can be tested without a network.
Game rules relied on here (see README, "The rules that matter"):
  * a negative balance when the jornada starts scores 0 points;
  * the most you can bid is cash + 20% of your team value;
  * a buyout clause can't be paid with debt;
  * raising a clause costs half of the increase;
  * shields (blindaje) exist only in some leagues and plans (FANTASYBOT_SHIELDS);
    a shielded player can't be taken, so the bot always respects the flag.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..api.models import (
    Id,
    Lineup,
    MarketItem,
    Match,
    Player,
    StandingEntry,
    TeamData,
    same_id,
)
from .model import (
    COACH,
    DEF,
    FWD,
    GK,
    MID,
    NEUTRAL,
    FixtureOutlook,
    Projection,
    position_label,
    position_of,
    project,
)

# How a player's upcoming fixtures scale his points (difficulty.FixtureModel.outlook).
OutlookFn = Callable[[Player], FixtureOutlook]

CREDIT_SHARE = 0.20  # max bid = cash + 20% of team value
DEFAULT_FORMATIONS: list[tuple[int, int, int]] = [
    (3, 4, 3), (3, 5, 2), (4, 3, 3), (4, 4, 2), (4, 5, 1), (5, 3, 2), (5, 4, 1)]
LINEUP_KEYS = {"goalkeeper": GK, "defender": DEF, "midfield": MID, "striker": FWD, "coach": COACH}


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def money(amount: int | float | None) -> str:
    if amount is None:
        return "?"
    sign = "−" if amount < 0 else ""
    amount = abs(amount)
    if amount >= 1_000_000:
        return f"{sign}{amount / 1_000_000:.2f}M"
    return f"{sign}{amount / 1000:.0f}k"


def find_my_entry(rows: list[StandingEntry], user_id: Id | None,
                  team_id: Id | None = None) -> StandingEntry | None:
    for row in rows:
        if team_id is not None and same_id(row.team.id, team_id):
            return row
        if team_id is None and (same_id(row.userId, user_id) or (
                row.team.manager is not None and same_id(row.team.manager.id, user_id))):
            return row
    return None


def manager_name(row: StandingEntry) -> str:
    return row.manager or (row.team.manager.managerName if row.team.manager else None) \
        or row.team.name or f"team {row.team.id}"


# -- squads --------------------------------------------------------------------


@dataclass
class SquadPlayer:
    player: Player
    player_team_id: Id | None
    clause: int | None
    locked_until: datetime | None
    shielded: bool
    projection: Projection
    market: dict[str, Any] | None = None  # playerMarket: offers on him, listed or not

    @property
    def position(self) -> int | None:
        return position_of(self.player)

    @property
    def name(self) -> str:
        return self.player.display_name

    @property
    def label(self) -> str:
        return f"{self.name} ({position_label(self.player)})"

    @property
    def value(self) -> int:
        return self.player.marketValue or 0

    @property
    def expected(self) -> float:
        """Next jornada, fixture included: what picks a lineup."""
        return self.projection.expected

    @property
    def outlook(self) -> float:
        """Per jornada over the next few: what makes a player worth buying or keeping."""
        return self.projection.outlook

    def clause_open(self, now: datetime) -> bool:
        return self.locked_until is None or self.locked_until <= now


def build_squad(team: TeamData, details: dict[str, Player], current_week: int | None,
                outlook: OutlookFn | None = None) -> list[SquadPlayer]:
    """Squad of a fantasy team; uses the detailed player record (with weekly
    stats) when one was fetched, so recent form counts."""
    squad = []
    for owned in team.players:
        player = details.get(str(owned.playerMaster.id), owned.playerMaster)
        squad.append(SquadPlayer(
            player=player,
            player_team_id=owned.player_team_id,
            clause=owned.buyoutClause,
            locked_until=parse_time(owned.buyoutClauseLockedEndTime),
            shielded=bool(owned.isShielded),
            projection=project(player, current_week, outlook(player) if outlook else NEUTRAL),
            market=owned.playerMarket if isinstance(owned.playerMarket, dict) else None,
        ))
    return squad


# -- lineups -------------------------------------------------------------------


def parse_formations(raw: Iterable[Any] | None) -> list[tuple[int, int, int]]:
    """Formations from the API in whatever shape they come ([4,4,2], "4-4-2",
    {"tacticalFormation": [...]}); the usual seven if none parse."""
    found = set()
    for item in raw or []:
        shape = item
        if isinstance(item, dict):
            shape = next((item[k] for k in ("tacticalFormation", "formation", "name", "value")
                          if k in item), None)
        if isinstance(shape, str):
            digits = [int(c) for c in re.findall(r"\d", shape)]
        elif isinstance(shape, (list, tuple)) and all(isinstance(n, int) for n in shape):
            digits = list(shape)
        else:
            continue
        if len(digits) == 4 and digits[0] == 1:  # "1-4-4-2" counts the keeper
            digits = digits[1:]
        if len(digits) == 3 and sum(digits) == 10:
            found.add((digits[0], digits[1], digits[2]))
    return sorted(found) or list(DEFAULT_FORMATIONS)


@dataclass
class LineupPlan:
    formation: tuple[int, int, int]
    by_position: dict[int, list[SquadPlayer]]
    coach: SquadPlayer | None
    expected: float

    @property
    def starters(self) -> list[SquadPlayer]:
        return [p for pos in (GK, DEF, MID, FWD) for p in self.by_position.get(pos, [])]

    def formation_label(self) -> str:
        return "-".join(map(str, self.formation))


def best_lineup(squad: list[SquadPlayer], formations: list[tuple[int, int, int]]) -> LineupPlan | None:
    pool = {pos: sorted((p for p in squad if p.position == pos), key=lambda p: -p.expected)
            for pos in (GK, DEF, MID, FWD, COACH)}
    coach = pool[COACH][0] if pool[COACH] else None
    best: LineupPlan | None = None
    for d, m, f in formations:
        need = {GK: 1, DEF: d, MID: m, FWD: f}
        if any(len(pool[pos]) < n for pos, n in need.items()):
            continue
        chosen = {pos: pool[pos][:n] for pos, n in need.items()}
        total = sum(p.expected for group in chosen.values() for p in group)
        total += coach.expected if coach else 0
        if best is None or total > best.expected:
            best = LineupPlan((d, m, f), chosen, coach, round(total, 2))
    return best


@dataclass
class CurrentLineup:
    members: list[SquadPlayer]
    filled: int
    slots: int
    not_owned: int  # slots holding a player no longer in the squad
    coach_slot: bool  # the lineup has a coach entry at all
    coach: SquadPlayer | None
    formation: tuple[int, ...] | None

    @property
    def expected(self) -> float:
        return round(sum(p.expected for p in self.members)
                     + (self.coach.expected if self.coach else 0), 2)


def _entry_ids(entry: Any) -> list[str]:
    if not isinstance(entry, dict):
        return [str(entry)] if isinstance(entry, (int, str)) else []
    ids = [entry.get("playerTeamId"), entry.get("id")]
    master = entry.get("playerMaster")
    if isinstance(master, dict):
        ids.append(master.get("id"))
    return [str(i) for i in ids if i is not None]


def read_lineup(lineup: Lineup | None, squad: list[SquadPlayer]) -> CurrentLineup:
    by_team_id = {str(p.player_team_id): p for p in squad if p.player_team_id is not None}
    by_player_id = {str(p.player.id): p for p in squad}
    raw = lineup.formation.model_dump() if lineup and lineup.formation else {}
    tactical = raw.get("tacticalFormation")
    slots = 1 + sum(tactical) if isinstance(tactical, list) and tactical else 11
    members, filled, not_owned, coach = [], 0, 0, None
    for key, pos in LINEUP_KEYS.items():
        entries = raw.get(key)
        if entries is None:
            continue
        for entry in entries if isinstance(entries, list) else [entries]:
            if entry is None:
                continue
            ids = _entry_ids(entry)
            match = next((by_team_id.get(i) or by_player_id.get(i) for i in ids
                          if i in by_team_id or i in by_player_id), None)
            if pos == COACH:
                coach = match
                continue
            filled += 1
            if match is None:
                not_owned += 1
            else:
                members.append(match)
    return CurrentLineup(members, filled, slots, not_owned, "coach" in raw, coach,
                         tuple(tactical) if isinstance(tactical, list) else None)


# -- readiness -----------------------------------------------------------------


@dataclass
class Kickoff:
    jornada: int
    first_match: datetime
    in_progress: bool


def next_kickoff(matches_by_week: dict[int, list[Match]], current_week: int | None,
                 now: datetime) -> Kickoff | None:
    for week in sorted(matches_by_week):
        if current_week is not None and week < current_week:
            continue
        dates = [d for d in (parse_time(m.matchDate) for m in matches_by_week[week]) if d]
        if not dates:
            continue
        if any(d > now for d in dates):
            return Kickoff(week, min(dates), in_progress=min(dates) <= now)
    return None


@dataclass
class Readiness:
    balance: int | None
    kickoff: Kickoff | None
    problems: list[str] = field(default_factory=list)  # these cost points
    warnings: list[str] = field(default_factory=list)  # these might

    @property
    def ready(self) -> bool:
        return not self.problems


def assess_readiness(balance: int | None, current: CurrentLineup, squad: list[SquadPlayer],
                     kickoff: Kickoff | None) -> Readiness:
    r = Readiness(balance, kickoff)
    if balance is not None and balance < 0:
        r.problems.append(f"Balance is {money(balance)}: you score 0 points this jornada "
                          "unless it's back to 0 or more by kickoff.")
    if current.filled < current.slots:
        r.problems.append(f"Your lineup has {current.filled} of {current.slots} players.")
    if current.not_owned:
        r.problems.append(f"{current.not_owned} lineup slot(s) hold players you no longer own.")
    for p in current.members:
        status = (p.player.playerStatus or "ok").lower()
        if status in ("injured", "suspended"):
            r.problems.append(f"{p.label} is {status} but in your lineup.")
        elif p.projection.note == "no match this jornada":
            r.problems.append(f"{p.label} has no match this jornada but is in your lineup.")
        elif status == "doubt":
            r.warnings.append(f"{p.label} is a doubt.")
        elif p.projection.note == "didn't play last jornada":
            r.warnings.append(f"{p.label} didn't play last jornada.")
    owns_coach = any(p.position == COACH for p in squad)
    if current.coach_slot and owns_coach and current.coach is None:
        r.warnings.append("Your coach slot is empty.")
    return r


def sell_to_cover(deficit: int, squad: list[SquadPlayer], keep: list[SquadPlayer]) -> list[SquadPlayer]:
    """Bench players whose combined value covers a deficit, weakest first."""
    keep_ids = {id(p) for p in keep}
    bench = sorted((p for p in squad if id(p) not in keep_ids), key=lambda p: (p.expected, -p.value))
    picked, total = [], 0
    for p in bench:
        if total >= deficit:
            break
        picked.append(p)
        total += p.value
    return picked if total >= deficit else []


# -- buyout clauses ------------------------------------------------------------


@dataclass
class Rival:
    entry: StandingEntry
    rank: int
    squad: list[SquadPlayer]

    @property
    def name(self) -> str:
        return manager_name(self.entry)


@dataclass
class ClauseTarget:
    rival: Rival
    player: SquadPlayer

    @property
    def cost(self) -> int:
        return self.player.clause if self.player.clause is not None else self.player.value


@dataclass
class ClauseRadar:
    affordable: list[ClauseTarget]  # open now, unshielded, payable from cash
    unlocking_soon: list[ClauseTarget]  # locked, but opening within the window
    out_of_reach: list[ClauseTarget]  # open and unshielded, but more than your cash


def clause_radar(rivals: list[Rival], cash: int | None, now: datetime,
                 window: timedelta = timedelta(hours=24)) -> ClauseRadar:
    affordable, soon, reach = [], [], []
    for rival in rivals:
        for p in rival.squad:
            if p.shielded or p.outlook <= 0:
                continue
            target = ClauseTarget(rival, p)
            if p.clause_open(now):
                (affordable if cash is not None and target.cost <= cash else reach).append(target)
            elif p.locked_until is not None and p.locked_until - now <= window:
                soon.append(target)

    def key(t: ClauseTarget) -> tuple[float, int]:
        return (-t.player.outlook, t.cost)

    return ClauseRadar(sorted(affordable, key=key), sorted(soon, key=lambda t: t.player.locked_until),
                       sorted(reach, key=key))


def exposed(squad: list[SquadPlayer], now: datetime) -> list[SquadPlayer]:
    """Your players any rival could take right now, most valuable to you first."""
    return sorted((p for p in squad if p.clause_open(now) and not p.shielded and p.outlook > 0),
                  key=lambda p: -p.outlook)


# -- market --------------------------------------------------------------------


@dataclass
class MarketPick:
    item: MarketItem
    player: Player
    projection: Projection
    price: int
    change_1d: int | None
    change_3d: int | None
    payable_cash: bool
    payable_credit: bool


def rank_market(items: list[MarketItem], details: dict[str, Player], current_week: int | None,
                cash: int | None, credit_limit: int | None, change_1d: dict[str, int],
                change_3d: dict[str, int], exclude_team: Id | None = None,
                outlook: OutlookFn | None = None) -> list[MarketPick]:
    picks = []
    for item in items:
        if exclude_team is not None and same_id(item.seller_team_id, exclude_team):
            continue  # your own listing
        player = details.get(str(item.playerMaster.id), item.playerMaster)
        price = max(item.salePrice or 0, player.marketValue or 0)  # the minimum bid is the value
        pid = str(player.id)
        projection = project(player, current_week, outlook(player) if outlook else NEUTRAL)
        picks.append(MarketPick(item, player, projection, price,
                                change_1d.get(pid), change_3d.get(pid),
                                cash is not None and price <= cash,
                                credit_limit is not None and price <= credit_limit))
    return sorted(picks, key=lambda p: -p.projection.outlook)


def credit_limit(cash: int | None, team_value: int | None) -> int | None:
    if cash is None or team_value is None:
        return None
    return int(cash + CREDIT_SHARE * team_value)
