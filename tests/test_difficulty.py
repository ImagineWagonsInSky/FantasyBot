from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fantasybot.api.models import Match, Player
from fantasybot.scout.difficulty import (
    MAX_FACTOR,
    MIN_FACTOR,
    Fixture,
    FixtureModel,
    club_records,
)
from fantasybot.scout.model import COACH, DEF, FWD, GK, MID, project

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)


def match(mid, home, away, hs=None, as_=None, days_ago=7):
    return Match(id=mid, localId=home, visitorId=away, localScore=hs, visitorScore=as_,
                 matchDate=(NOW - timedelta(days=days_ago)).isoformat())


def season(n: int) -> list[Match]:
    """n played jornadas: 'leaky' concedes 4 a match, 'wall' concedes 0 and scores 0."""
    out = []
    for w in range(n):
        out.append(match(f"a{w}", "strong", "leaky", 4, 1))
        out.append(match(f"b{w}", "wall", "mid", 0, 0))
    return out


def model(n_played: int = 10, schedule=None) -> FixtureModel:
    schedule = schedule or {12: [match("n1", "strong", "leaky", days_ago=-1),
                                 match("n2", "mid", "wall", days_ago=-1)]}
    return FixtureModel(club_records(season(n_played), NOW), schedule, 12, {"leaky": "LKY"})


def test_records_skip_unplayed_and_future_matches():
    recs = club_records([match("x", "a", "b", 2, 1), match("y", "a", "b"),  # no score
                         match("z", "a", "b", 5, 0, days_ago=-2)], NOW)  # in the future
    assert (recs["a"].played, recs["a"].scored, recs["b"].conceded) == (1, 2, 2)


def test_attackers_gain_against_a_leaky_defence_and_defenders_lose_against_a_strong_attack():
    m = model()
    assert m.multiplier(FWD, Fixture("strong", "leaky", True)) > 1.05
    assert m.multiplier(MID, Fixture("strong", "leaky", True)) > 1.05
    assert m.multiplier(DEF, Fixture("leaky", "strong", False)) < 0.95
    assert m.multiplier(GK, Fixture("mid", "wall", True)) > 1.05  # wall never scores


def test_home_beats_away_and_coaches_follow_the_strength_gap():
    m = model()
    assert m.multiplier(FWD, Fixture("x", "unknown", True)) == pytest.approx(1.05)
    assert m.multiplier(FWD, Fixture("x", "unknown", False)) == pytest.approx(0.95)
    assert m.multiplier(COACH, Fixture("strong", "leaky", True)) > m.multiplier(
        COACH, Fixture("leaky", "strong", True))


def test_ratings_are_shrunk_early_and_always_capped():
    early, late = model(n_played=1), model(n_played=30)
    fx = Fixture("strong", "leaky", True)
    assert 1 < early.multiplier(FWD, fx) < late.multiplier(FWD, fx) <= MAX_FACTOR
    assert late.multiplier(DEF, Fixture("leaky", "strong", False)) >= MIN_FACTOR


def test_outlook_blank_jornada_and_horizon():
    sched = {12: [match("n1", "strong", "leaky", days_ago=-1)],  # 'mid' doesn't play in 12
             13: [match("n2", "mid", "leaky", days_ago=-8)],
             14: [match("n3", "leaky", "mid", days_ago=-15)]}
    m = model(schedule=sched)
    mid_player = Player(id=1, positionId=FWD, teamId="mid", averagePoints=6)
    outlook = m.outlook(mid_player)
    assert outlook.next_factor == 0 and outlook.next_label == "no match"
    assert 0 < outlook.horizon_factor < 1  # two of three jornadas, both decent
    p = project(mid_player, 12, outlook)
    assert p.expected == 0 and p.outlook > 0 and p.note == "no match this jornada"

    striker = Player(id=2, positionId=FWD, teamId="strong", averagePoints=6)
    s = m.outlook(striker)
    assert s.next_label == "vs LKY (h)" and s.next_factor > 1


def test_unknown_schedule_or_club_is_neutral():
    m = FixtureModel({}, {}, 12)
    assert m.outlook(Player(id=1, positionId=MID, teamId="x")).next_factor == 1.0
    assert model().outlook(Player(id=1, positionId=MID)).next_factor == 1.0  # no club
