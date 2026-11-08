"""Match odds from The Odds API, turned into each team's expected goals.

Bookmakers' 1X2 and over/under prices, with their margin removed and
averaged, give the probabilities of a home win, draw, away win and of more
than N goals. A Poisson model with two scoring rates (home and away goals
per match) is fitted to those probabilities: that's each team's expected
goals, which then drive fixture difficulty (see scout/difficulty.py).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..api.models import Match
from ..scout.analysis import parse_time
from .names import club_key

MAX_GOALS = 10


@dataclass(frozen=True)
class MatchOdds:
    home_goals: float  # expected goals (Poisson rate)
    away_goals: float
    p_home: float
    p_draw: float
    p_away: float

    def win_chance(self, home: bool) -> float:
        return self.p_home if home else self.p_away


@dataclass(frozen=True)
class EventOdds:
    home: str
    away: str
    commence: datetime | None
    p_home: float
    p_draw: float
    p_away: float
    line: float | None
    p_over: float | None


def _fair(prices: list[float]) -> list[float] | None:
    """Implied probabilities with the bookmaker's margin removed."""
    if not prices or any(p <= 1.0 for p in prices):
        return None
    raw = [1 / p for p in prices]
    total = sum(raw)
    return [r / total for r in raw]


def parse_event(event: Mapping[str, Any]) -> EventOdds | None:
    home, away = event.get("home_team"), event.get("away_team")
    if not home or not away:
        return None
    one_x_two: list[list[float]] = []
    totals: dict[float, list[float]] = {}
    for book in event.get("bookmakers") or []:
        for market in book.get("markets") or []:
            outcomes = market.get("outcomes") or []
            if market.get("key") == "h2h":
                price = {o.get("name"): o.get("price") for o in outcomes}
                fair = _fair([price.get(home) or 0, price.get("Draw") or 0, price.get(away) or 0])
                if fair:
                    one_x_two.append(fair)
            elif market.get("key") == "totals":
                over = next((o for o in outcomes if o.get("name") == "Over"), None)
                under = next((o for o in outcomes if o.get("name") == "Under"), None)
                if over and under and over.get("point") == under.get("point"):
                    fair = _fair([over.get("price") or 0, under.get("price") or 0])
                    if fair:
                        totals.setdefault(float(over["point"]), []).append(fair[0])
    if not one_x_two:
        return None
    p_home, p_draw, p_away = (sum(x[i] for x in one_x_two) / len(one_x_two) for i in range(3))
    line = p_over = None
    if totals:  # the line most bookmakers quote, usually 2.5
        line = max(totals, key=lambda k: (len(totals[k]), -abs(k - 2.5)))
        p_over = sum(totals[line]) / len(totals[line])
    return EventOdds(home, away, parse_time(event.get("commence_time")), p_home, p_draw, p_away,
                     line, p_over)


def _pmf(rate: float) -> list[float]:
    return [math.exp(-rate) * rate ** k / math.factorial(k) for k in range(MAX_GOALS + 1)]


def poisson_outcomes(home: float, away: float, line: float = 2.5) -> tuple[float, float, float, float]:
    ph, pa = _pmf(home), _pmf(away)
    win = draw = loss = over = 0.0
    for i, a in enumerate(ph):
        for j, b in enumerate(pa):
            p = a * b
            if i > j:
                win += p
            elif i == j:
                draw += p
            else:
                loss += p
            if i + j > line:
                over += p
    return win, draw, loss, over


def fit_goals(odds: EventOdds) -> tuple[float, float]:
    """Expected goals for each side that best reproduce the market's probabilities."""
    line = odds.line if odds.line is not None else 2.5

    def error(h: float, a: float) -> float:
        win, draw, loss, over = poisson_outcomes(h, a, line)
        e = (win - odds.p_home) ** 2 + (draw - odds.p_draw) ** 2 + (loss - odds.p_away) ** 2
        if odds.p_over is not None:
            e += (over - odds.p_over) ** 2
        return e

    best = (1.3, 1.1)
    best_err = error(*best)
    coarse = [round(0.2 + 0.1 * k, 3) for k in range(39)]  # 0.2 .. 4.0
    for fine in (False, True):  # a coarse sweep, then a finer one around the best point
        if fine:
            grid_h = [round(best[0] + 0.02 * k, 3) for k in range(-5, 6)]
            grid_a = [round(best[1] + 0.02 * k, 3) for k in range(-5, 6)]
        else:
            grid_h = grid_a = coarse
        for h in grid_h:
            for a in grid_a:
                if h <= 0 or a <= 0:
                    continue
                e = error(h, a)
                if e < best_err:
                    best, best_err = (h, a), e
    return best


def _side(m: Match, home: bool) -> tuple[str | None, list[str | None]]:
    ref = m.local if home else m.visitor
    club = m.localId if home else m.visitorId
    if club is None and ref is not None:
        club = ref.id
    names = [ref.name, getattr(ref, "mainName", None), ref.shortName, ref.slug] if ref else []
    return (str(club) if club is not None else None), names


def match_odds(events: Iterable[Mapping[str, Any]], matches: Iterable[Match],
               club_names: Mapping[str, Sequence[str | None]] | None = None
               ) -> dict[tuple[str, str], MatchOdds]:
    """Odds per LaLiga Fantasy fixture, keyed (home club id, away club id).

    club_names: every name LaLiga uses for a club id (name, short name, slug).
    Events are paired with fixtures by both clubs' keys, and only when the
    kickoffs are within a few days of each other."""
    names = club_names or {}
    fixtures: dict[tuple[str | None, str | None], tuple[str, str, datetime | None]] = {}
    for m in matches:
        (home, home_names), (away, away_names) = _side(m, True), _side(m, False)
        if home is None or away is None:
            continue
        key = (club_key(*names.get(home, ()), *home_names), club_key(*names.get(away, ()), *away_names))
        fixtures[key] = (home, away, parse_time(m.matchDate))
    out: dict[tuple[str, str], MatchOdds] = {}
    for raw in events:
        event = parse_event(raw)
        if event is None:
            continue
        found = fixtures.get((club_key(event.home), club_key(event.away)))
        if found is None:
            continue
        home, away, kickoff = found
        if kickoff and event.commence and abs(kickoff - event.commence) > timedelta(days=3):
            continue
        h, a = fit_goals(event)
        out[(home, away)] = MatchOdds(h, a, event.p_home, event.p_draw, event.p_away)
    return out
