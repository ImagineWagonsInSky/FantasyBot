"""Fixture difficulty: how much a player's next opponents should scale his points.

Built from this season's results only (goals scored and conceded per club),
so it needs nothing beyond LaLiga's own calendar:

* attackers (MID, FWD) gain against defences that concede a lot;
* defenders and keepers (DEF, GK) lose against attacks that score a lot;
* coaches follow the overall strength gap between the two clubs;
* playing at home is worth a little.

Early in the season a club has few matches, so its rating is pulled towards
average (shrinkage), and every multiplier is capped, so one hammering can't
dominate the picks. A club with no match in a jornada gets 0.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from ..api.models import Match, Player
from .analysis import parse_time
from .model import COACH, DEF, FWD, GK, MID, NEUTRAL, FixtureOutlook, position_of

HOME = 1.05
AWAY = 0.95
MIN_FACTOR, MAX_FACTOR = 0.75, 1.30
SHRINK_MATCHES = 5  # a club needs about this many matches before its rating counts fully
HORIZON = 3  # jornadas considered when buying or keeping a player
DEFAULT_GOALS = 1.3  # goals per club per match, before any results are in


@dataclass
class ClubRecord:
    played: int = 0
    scored: int = 0
    conceded: int = 0

    @property
    def scored_per_match(self) -> float:
        return self.scored / self.played if self.played else DEFAULT_GOALS

    @property
    def conceded_per_match(self) -> float:
        return self.conceded / self.played if self.played else DEFAULT_GOALS


@dataclass(frozen=True)
class Fixture:
    club: str
    opponent: str
    home: bool


def club_records(matches: Iterable[Match], now: datetime) -> dict[str, ClubRecord]:
    records: dict[str, ClubRecord] = {}
    for m in matches:
        if m.localScore is None or m.visitorScore is None or m.localId is None or m.visitorId is None:
            continue
        played_at = parse_time(m.matchDate)
        if played_at is not None and played_at > now:
            continue
        for club, scored, conceded in ((m.localId, m.localScore, m.visitorScore),
                                       (m.visitorId, m.visitorScore, m.localScore)):
            r = records.setdefault(str(club), ClubRecord())
            r.played += 1
            r.scored += scored
            r.conceded += conceded
    return records


def fixtures_by_club(matches: Iterable[Match]) -> dict[str, list[Fixture]]:
    out: dict[str, list[Fixture]] = {}
    for m in matches:
        local = m.localId if m.localId is not None else (m.local.id if m.local else None)
        visitor = m.visitorId if m.visitorId is not None else (m.visitor.id if m.visitor else None)
        if local is None or visitor is None:
            continue
        out.setdefault(str(local), []).append(Fixture(str(local), str(visitor), True))
        out.setdefault(str(visitor), []).append(Fixture(str(visitor), str(local), False))
    return out


def player_club(player: Player) -> str | None:
    if player.teamId is not None:
        return str(player.teamId)
    if player.team is not None and player.team.id is not None:
        return str(player.team.id)
    return None


class FixtureModel:
    def __init__(self, records: Mapping[str, ClubRecord], schedule: Mapping[int, list[Match]],
                 target_week: int | None, club_names: Mapping[str, str] | None = None,
                 horizon: int = HORIZON) -> None:
        self._records = records
        self._names = club_names or {}
        self._target = target_week
        self._weeks: dict[int, dict[str, list[Fixture]]] = {}
        if target_week is not None:
            for week in range(target_week, target_week + horizon):
                if schedule.get(week):
                    self._weeks[week] = fixtures_by_club(schedule[week])
        played = sum(r.played for r in records.values())
        self._avg = sum(r.scored for r in records.values()) / played if played else DEFAULT_GOALS

    def multiplier(self, position: int | None, fixture: Fixture) -> float:
        opp = self._records.get(fixture.opponent)
        own = self._records.get(fixture.club)
        ratio = 1.0
        if opp is not None and opp.played:
            if position in (MID, FWD):
                ratio = (opp.conceded_per_match / self._avg) ** 0.5
            elif position in (GK, DEF):
                ratio = (self._avg / max(opp.scored_per_match, 0.3)) ** 0.5
            elif position == COACH and own is not None and own.played:
                ratio = (((own.scored + 1) / (own.conceded + 1))
                         / ((opp.scored + 1) / (opp.conceded + 1))) ** 0.5
            ratio = 1 + (ratio - 1) * opp.played / (opp.played + SHRINK_MATCHES)
        ratio *= HOME if fixture.home else AWAY
        return min(MAX_FACTOR, max(MIN_FACTOR, ratio))

    def _label(self, fixture: Fixture) -> str:
        return f"vs {self._names.get(fixture.opponent, fixture.opponent)} ({'h' if fixture.home else 'a'})"

    def outlook(self, player: Player) -> FixtureOutlook:
        club = player_club(player)
        if club is None or self._target not in self._weeks:
            return NEUTRAL  # nothing known: don't guess
        position = position_of(player)
        per_week = []
        for week in sorted(self._weeks):
            per_week.append(sum(self.multiplier(position, f) for f in self._weeks[week].get(club, [])))
        upcoming = self._weeks[self._target].get(club, [])
        label = " & ".join(self._label(f) for f in upcoming) if upcoming else "no match"
        return FixtureOutlook(round(per_week[0], 3), label, round(sum(per_week) / len(per_week), 3))
