"""What your opponents are up to, from what LaLiga lets every manager see.

Built from the league activity feed, the price history the bot keeps, the
rivals' squads, the estimated cash and the weekly standings:

* a profile per rival: cash, what positions they're short of, injured
  regulars, how active they are, how much over value they usually pay, the
  clauses they've stolen;
* bid competition: for each market player, which rivals are likely to bid
  (cash, need, activity) and a bid that should beat them. Bids are blind, so
  knowing a friend's habits is the edge;
* clause steals ranked by points swing: what you gain plus what the owner
  loses, counted fully against rivals ahead of you;
* projected points for the next jornada, the recent trend, and a recap of the
  last jornada;
* rivals likely in the red (they score 0 unless they sell) and their listings.

Rivals' bids are blind and their cash is an estimate, so every line here is
a likelihood, not a fact.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..api.models import ActivityEntry, MarketItem, StandingEntry, same_id
from .analysis import (
    ClauseTarget,
    CurrentLineup,
    Rival,
    SquadPlayer,
    best_lineup,
    clause_radar,
    credit_limit,
    manager_name,
    parse_time,
)
from .cash import CashEstimates, is_clause_steal
from .model import COACH, DEF, FWD, GK, MID, POSITION_LABELS

# Fit players a squad needs per position to field most formations.
NEEDS = {GK: 1, DEF: 4, MID: 4, FWD: 2}
DEFAULT_PREMIUM = 0.05  # assumed overpay for a rival whose history doesn't show one yet
PURCHASES = {1, 31}  # bought, signed (clause steals are counted separately)
MOVES = {1, 31, 32, 33}
ACTIVE_DAYS = 14
BEHIND_WEIGHT = 0.5  # a point taken from a rival below you matters less than one from above
UNAVAILABLE = {"injured", "suspended"}


def _round_up(amount: float, step: int = 10_000) -> int:
    return int(math.ceil(amount / step) * step)


@dataclass
class RivalProfile:
    name: str
    team_id: str
    manager_id: str | None
    rank: int
    points: float
    gap: float  # their points minus yours: positive means they're ahead
    cash: int | None  # estimated
    max_bid: int | None  # estimated cash + 20% of team value
    squad: list[SquadPlayer]
    needs: dict[int, int]  # position -> fit players missing
    injured: list[str]  # regulars out (injured or suspended)
    last_move: datetime | None
    moves: int  # in the last ACTIVE_DAYS days
    premiums: list[float]  # what they paid over value, oldest first
    steals_made: int
    steals_from_you: int
    listings: list[MarketItem] = field(default_factory=list)
    best_xi: float | None = None  # expected points next jornada, if they field their best XI
    saved_xi: float | None = None  # their saved lineup's expected points, when LaLiga shows it

    @property
    def overpay(self) -> float | None:
        """Typical premium over value: the median of their last 8 purchases."""
        recent = self.premiums[-8:]
        return statistics.median(recent) if len(recent) >= 2 else None

    @property
    def likely_premium(self) -> float:
        return self.overpay if self.overpay is not None else DEFAULT_PREMIUM

    @property
    def in_red(self) -> bool:
        return self.cash is not None and self.cash < 0

    @property
    def projected(self) -> float | None:
        return self.saved_xi if self.saved_xi is not None else self.best_xi

    def need_text(self) -> str:
        parts = [f"{POSITION_LABELS[p]} ({NEEDS[p] - n} fit)" for p, n in self.needs.items() if n > 0]
        return "short of " + ", ".join(parts) if parts else ""


@dataclass
class Bidder:
    rival: RivalProfile
    reasons: list[str]


@dataclass
class Competition:
    item: MarketItem
    name: str
    position: int | None
    price: int
    bidders: list[Bidder]
    to_win: int | None  # a bid that should beat them (None: nobody likely, or over your max)
    out_of_reach: bool = False  # a likely bidder can go higher than your max bid


@dataclass
class SwingTarget:
    target: ClauseTarget
    your_gain: float  # xP per jornada over the starter he'd replace in your XI
    their_loss: float  # xP per jornada the owner loses, after his replacement
    weight: float

    @property
    def swing(self) -> float:
        return round(self.your_gain + self.weight * self.their_loss, 2)


@dataclass
class Intel:
    profiles: list[RivalProfile]
    competition: dict[str, Competition]  # by market item id
    swings: list[SwingTarget]
    my_projection: float | None
    trend: dict[int, dict[str, float]]  # week -> team id -> points
    recap_week: int | None = None  # a finished jornada not recapped yet

    def profile(self, team_id: object) -> RivalProfile | None:
        return next((p for p in self.profiles if same_id(p.team_id, team_id)), None)


# -- profiles ----------------------------------------------------------------------


def purchase_premiums(entries: Iterable[ActivityEntry],
                      value_near: Callable[[str, date], int | None]) -> dict[str, list[float]]:
    """Per manager id: amount paid over the player's value that day, for each purchase."""
    out: dict[str, list[float]] = {}
    for e in sorted(entries, key=lambda e: e.createdAt or ""):
        if (e.activityTypeId not in PURCHASES or is_clause_steal(e) or not e.amount or e.user1Id is None
                or e.playerMasterId is None):
            continue
        when = parse_time(e.createdAt)
        value = value_near(str(e.playerMasterId), when.date()) if when else None
        if not value:
            continue
        premium = e.amount / value - 1
        if -0.5 <= premium <= 2.0:  # anything else is a data glitch, not a habit
            out.setdefault(str(e.user1Id), []).append(round(premium, 4))
    return out


def _needs(squad: list[SquadPlayer]) -> dict[int, int]:
    fit = {pos: 0 for pos in NEEDS}
    for p in squad:
        status = (p.player.playerStatus or "ok").lower()
        if p.position in fit and status not in UNAVAILABLE:
            fit[p.position] += 1
    return {pos: max(0, NEEDS[pos] - n) for pos, n in fit.items()}


def build_profiles(standing: list[StandingEntry], me: StandingEntry, squads: Mapping[str, list[SquadPlayer]],
                   entries: list[ActivityEntry], cash: CashEstimates | None, market: list[MarketItem],
                   formations: list[tuple[int, int, int]], now: datetime,
                   value_near: Callable[[str, date], int | None],
                   saved: Mapping[str, CurrentLineup] | None = None) -> list[RivalProfile]:
    ordered = sorted(standing, key=lambda e: -(e.points or 0))
    my_manager = str(me.team.manager.id) if me.team.manager else None
    premiums = purchase_premiums(entries, value_near)
    since = now - timedelta(days=ACTIVE_DAYS)
    out = []
    for rank, e in enumerate(ordered, 1):
        if same_id(e.team.id, me.team.id):
            continue
        manager = str(e.team.manager.id) if e.team.manager else None
        mine = [x for x in entries if manager is not None and str(x.user1Id) == manager]
        times = [t for x in mine if x.activityTypeId in MOVES and (t := parse_time(x.createdAt))]
        moves = [t for t in times if t >= since]
        last = max(times, default=None)
        steals = [x for x in mine if is_clause_steal(x)]
        squad = squads.get(str(e.team.id), [])
        est = cash.get(manager) if cash is not None and manager is not None else None
        value = e.teamValue or e.team.teamValue
        plan = best_lineup(squad, formations) if squad else None
        lineup = (saved or {}).get(str(e.team.id))
        injured = [p.name for p in sorted(squad, key=lambda p: -p.projection.season_average)
                   if (p.player.playerStatus or "ok").lower() in UNAVAILABLE
                   and p.projection.season_average >= 3]  # regulars, not fringe players
        out.append(RivalProfile(
            name=manager_name(e), team_id=str(e.team.id), manager_id=manager, rank=rank,
            points=e.points or 0, gap=(e.points or 0) - (me.points or 0), cash=est,
            max_bid=credit_limit(est, value), squad=squad, needs=_needs(squad), injured=injured[:3],
            last_move=last, moves=len(moves), premiums=premiums.get(manager or "", []),
            steals_made=len(steals),
            steals_from_you=sum(1 for x in steals if my_manager is not None and str(x.user2Id) == my_manager),
            listings=[i for i in market if i.discr == "marketPlayerTeam" and not i.directOffer
                      and same_id(i.seller_team_id, e.team.id)],
            best_xi=plan.expected if plan else None,
            saved_xi=lineup.expected if lineup is not None else None,
        ))
    return out


# -- bid competition ------------------------------------------------------------------


def competition(item: MarketItem, name: str, position: int | None, price: int,
                profiles: list[RivalProfile], my_limit: int | None) -> Competition:
    """Who's likely to bid on a market player, and what should beat them."""
    bidders = []
    for r in profiles:
        if r.max_bid is not None and r.max_bid < price:
            continue  # can't afford even the minimum bid
        reasons = []
        if position in r.needs and r.needs[position] > 0:
            reasons.append(f"needs a {POSITION_LABELS[position]}")
        if r.moves >= 2:
            reasons.append("buying lately")
        if not reasons:
            continue
        if r.overpay is not None:
            reasons.append(f"pays {r.overpay:+.0%} usually")
        bidders.append(Bidder(r, reasons))
    if not bidders:
        return Competition(item, name, position, price, [], None)
    target = price * (1 + max(b.rival.likely_premium for b in bidders) + 0.01)
    ceilings = [b.rival.max_bid for b in bidders if b.rival.max_bid is not None]
    if ceilings and len(ceilings) == len(bidders):
        target = min(target, max(ceilings) + 10_000)  # nobody can bid past their own max bid
    bid = _round_up(max(target, price))
    if my_limit is not None and bid > my_limit:
        return Competition(item, name, position, price, bidders, None, out_of_reach=True)
    return Competition(item, name, position, price, bidders, bid)


# -- clause steals by swing -------------------------------------------------------------


def _starters(squad: list[SquadPlayer], formations: list[tuple[int, int, int]]) -> list[SquadPlayer]:
    plan = best_lineup(squad, formations) if squad else None
    return plan.starters if plan else []


def swing_targets(rivals: list[Rival], profiles: list[RivalProfile], my_squad: list[SquadPlayer],
                  cash: int | None, formations: list[tuple[int, int, int]],
                  now: datetime) -> list[SwingTarget]:
    """Clause steals you can pay now, by what they'd change between you and the owner."""
    radar = clause_radar(rivals, cash, now)
    by_team = {r.team_id: r for r in profiles}
    mine = _starters(my_squad, formations)
    out = []
    for t in radar.affordable:
        p = t.player
        pos = p.position
        if pos == COACH:
            continue
        mine_at = [s.outlook for s in mine if s.position == pos]
        your_gain = max(0.0, p.outlook - min(mine_at)) if mine_at else p.outlook
        owner = by_team.get(str(t.rival.entry.team.id))
        if owner is None:
            continue
        theirs = _starters(owner.squad, formations)
        their_loss = 0.0
        if any(s is p for s in theirs):
            bench = [s.outlook for s in owner.squad if s.position == pos and s not in theirs
                     and (s.player.playerStatus or "ok").lower() not in UNAVAILABLE]
            their_loss = max(0.0, p.outlook - max(bench, default=0.0))
        weight = 1.0 if owner.gap > 0 else BEHIND_WEIGHT
        out.append(SwingTarget(t, round(your_gain, 2), round(their_loss, 2), weight))
    return sorted(out, key=lambda s: (-s.swing, s.target.cost))


# -- the whole picture ---------------------------------------------------------------------


def last_finished(trend: Mapping[int, Mapping[str, float]], current_week: int | None) -> int | None:
    done = [w for w in trend if current_week is None or w < current_week]
    return max(done) if done else None


def gap_story(trend: Mapping[int, Mapping[str, float]], week: int, me: str, rival: str,
              gap_now: float) -> tuple[float, float]:
    """(gap before `week`, gap now) between a rival and you; positive means they're ahead."""
    w = trend.get(week, {})
    return gap_now - (w.get(rival, 0.0) - w.get(me, 0.0)), gap_now


def projected_line(intel: Intel) -> str:
    if intel.my_projection is None:
        return ""
    parts = [f"You {intel.my_projection:.1f}"]
    for r in intel.profiles:
        if r.projected is not None:
            parts.append(f"{r.name} {r.projected:.1f}" + ("*" if r.saved_xi is not None else ""))
    return " · ".join(parts)
