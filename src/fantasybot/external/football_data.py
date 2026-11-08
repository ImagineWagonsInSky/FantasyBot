"""Real fixtures and results from football-data.org, to check LaLiga Fantasy's.

Football data provided by the Football-Data.org API.

LaLiga Fantasy's calendar can lag behind reality: a postponed match still
listed, a kickoff moved, a result missing or wrong. Each of those changes
what the bot recommends (lineups, fixture difficulty), so the briefing
compares the two every morning and says where they disagree.

Matches are paired by both clubs and the jornada (LaLiga Fantasy's jornadas
are the official matchdays, which a rescheduled match keeps), falling back
to the nearest kickoff; dates are what's being checked.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..api.models import Match
from ..scout.analysis import parse_time
from .names import club_key

NOT_PLAYING = {"POSTPONED", "SUSPENDED", "CANCELLED"}
KICKOFF_TOLERANCE = timedelta(minutes=30)
RESULT_DELAY = timedelta(hours=3)  # a match is surely over this long after kickoff
RESULT_WEEKS = 2  # how many recent jornadas' results are compared


@dataclass(frozen=True)
class RealMatch:
    home: str | None  # club keys (names.py)
    away: str | None
    home_name: str
    away_name: str
    kickoff: datetime | None
    status: str
    matchday: int | None = None
    home_goals: int | None = None
    away_goals: int | None = None

    @property
    def time_confirmed(self) -> bool:
        # football-data.org: SCHEDULED = date only, TIMED = kickoff time fixed
        return self.status not in ("SCHEDULED", *NOT_PLAYING)


def season_of(now: datetime) -> int:
    """football-data.org names a season by the year it starts (2026 for 2026-27)."""
    return now.year if now.month >= 7 else now.year - 1


def trim(body: Mapping[str, Any]) -> dict[str, Any]:
    """Only what the check needs: a season's response is ~400 kB, this is ~40 kB."""
    def team(t: Any) -> dict[str, Any]:
        t = t if isinstance(t, Mapping) else {}
        return {k: t.get(k) for k in ("name", "shortName", "tla")}

    out = []
    for m in body.get("matches") or []:
        if not isinstance(m, Mapping):
            continue
        full = ((m.get("score") or {}).get("fullTime") or {})
        out.append({"matchday": m.get("matchday"), "utcDate": m.get("utcDate"), "status": m.get("status"),
                    "home": team(m.get("homeTeam")), "away": team(m.get("awayTeam")),
                    "score": [full.get("home"), full.get("away")]})
    return {"matches": out}


def parse_matches(body: Mapping[str, Any] | None) -> list[RealMatch]:
    def key(t: Mapping[str, Any]) -> str | None:
        return club_key(t.get("name"), t.get("shortName"))

    def label(t: Mapping[str, Any]) -> str:
        return t.get("shortName") or t.get("name") or "?"

    out = []
    for m in (body or {}).get("matches") or []:
        home, away = m.get("home") or {}, m.get("away") or {}
        score = (list(m.get("score") or []) + [None, None])[:2]
        out.append(RealMatch(key(home), key(away), label(home), label(away), parse_time(m.get("utcDate")),
                             str(m.get("status") or ""), m.get("matchday"), score[0], score[1]))
    return out


@dataclass(frozen=True)
class Problem:
    kind: str  # "postponed", "kickoff", "score", "missing"
    jornada: int
    home: str  # LaLiga Fantasy club ids
    away: str
    fantasy: str  # what LaLiga Fantasy says
    real: str  # what football-data.org says


@dataclass
class DataCheck:
    fixtures: int = 0  # upcoming fixtures compared
    results: int = 0  # finished matches compared
    unpaired: int = 0  # fantasy matches with no counterpart (a name the bot doesn't know)
    problems: list[Problem] = field(default_factory=list)

    @property
    def postponed(self) -> set[tuple[str, str]]:
        return {(p.home, p.away) for p in self.problems if p.kind == "postponed"}


def _ids(m: Match) -> tuple[str | None, str | None]:
    home = m.localId if m.localId is not None else (m.local.id if m.local else None)
    away = m.visitorId if m.visitorId is not None else (m.visitor.id if m.visitor else None)
    return (str(home) if home is not None else None), (str(away) if away is not None else None)


def _names(club: str, m: Match, home: bool, aliases: Mapping[str, Sequence[str | None]]) -> list[str | None]:
    ref = m.local if home else m.visitor
    own = [ref.name, getattr(ref, "mainName", None), ref.shortName, ref.slug] if ref else []
    return [*aliases.get(club, ()), *own]


def cross_check(real: Iterable[RealMatch], upcoming: Mapping[int, list[Match]],
                played: Mapping[int, list[Match]], now: datetime,
                aliases: Mapping[str, Sequence[str | None]] | None = None,
                fmt: Any = str) -> DataCheck:
    """upcoming: LaLiga Fantasy's next jornadas. played: earlier jornadas (stored results).
    fmt formats a kickoff for the messages."""
    aliases = aliases or {}
    by_pair: dict[tuple[str, str], list[RealMatch]] = {}
    for r in real:
        if r.home and r.away:
            by_pair.setdefault((r.home, r.away), []).append(r)

    def counterpart(key: tuple[str | None, str | None], week: int, kickoff: datetime | None
                    ) -> RealMatch | None:
        candidates = by_pair.get(key)  # type: ignore[arg-type]  # None keys simply aren't there
        if not candidates:
            return None
        same_week = [r for r in candidates if r.matchday == week]
        if same_week:
            return same_week[0]
        if kickoff is None:
            return candidates[0] if len(candidates) == 1 else None
        return min(candidates, key=lambda r: abs((r.kickoff or kickoff) - kickoff))
    check = DataCheck()
    recent = sorted(w for w, ms in played.items()
                    if any((t := parse_time(m.matchDate)) and t < now - RESULT_DELAY for m in ms))
    weeks = {w: played[w] for w in recent[-RESULT_WEEKS:]}
    for w, ms in upcoming.items():
        weeks.setdefault(w, ms)
    for week in sorted(weeks):
        for m in weeks[week]:
            home, away = _ids(m)
            if home is None or away is None:
                continue
            key = (club_key(*_names(home, m, True, aliases)), club_key(*_names(away, m, False, aliases)))
            kickoff = parse_time(m.matchDate)
            r = counterpart(key, week, kickoff)
            if r is None:
                check.unpaired += 1
                continue
            finished = r.status == "FINISHED" and r.home_goals is not None and r.away_goals is not None
            if (kickoff is not None and kickoff < now - RESULT_DELAY) or m.localScore is not None:
                check.results += 1
                fantasy = (f"{m.localScore}–{m.visitorScore}" if m.localScore is not None
                           and m.visitorScore is not None else None)
                if finished and fantasy is None:
                    check.problems.append(Problem("missing", week, home, away, "no result",
                                                  f"{r.home_goals}–{r.away_goals}"))
                elif finished and fantasy != f"{r.home_goals}–{r.away_goals}":
                    check.problems.append(Problem("score", week, home, away, fantasy or "",
                                                  f"{r.home_goals}–{r.away_goals}"))
                continue
            check.fixtures += 1
            if r.status in NOT_PLAYING:
                check.problems.append(Problem("postponed", week, home, away,
                                              fmt(kickoff) if kickoff else "listed", r.status.lower()))
            elif (r.time_confirmed and kickoff and r.kickoff
                  and abs(kickoff - r.kickoff) > KICKOFF_TOLERANCE):
                check.problems.append(Problem("kickoff", week, home, away, fmt(kickoff), fmt(r.kickoff)))
    return check
