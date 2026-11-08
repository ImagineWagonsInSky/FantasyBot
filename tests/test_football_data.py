from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from pydantic import SecretStr

from fantasybot.api.models import Match
from fantasybot.external import ExternalData
from fantasybot.external.football_data import cross_check, parse_matches, season_of, trim
from fantasybot.scout.analysis import parse_time
from fantasybot.scout.difficulty import POSTPONED, Fixture, FixtureModel
from fantasybot.scout.model import FWD

from . import scout_league
from .test_external import scout

MATCHES_URL = "https://api.football-data.org/v4/competitions/PD/matches"
COMPETITION_URL = "https://api.football-data.org/v4/competitions/PD"
TOKEN = "fedcba9876543210fedcba9876543210"
NOW = datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc)


def real(home: str, away: str, when: str, status: str = "TIMED", score=(None, None), matchday=1) -> dict:
    """One match as football-data.org's v4 API returns it."""
    return {"id": hash((home, away)) % 10**6, "utcDate": when, "status": status, "matchday": matchday,
            "stage": "REGULAR_SEASON", "homeTeam": {"id": 1, "name": home, "shortName": home, "tla": "AAA"},
            "awayTeam": {"id": 2, "name": away, "shortName": away, "tla": "BBB"},
            "score": {"winner": None, "duration": "REGULAR", "fullTime": {"home": score[0], "away": score[1]},
                      "halfTime": {"home": None, "away": None}},
            "referees": [], "odds": {"msg": "Activate Odds-Package in User-Panel to retrieve odds."}}


def utc(text: str) -> str:
    return parse_time(text).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fantasy(mid, home, away, when, hs=None, as_=None) -> Match:
    return Match(id=mid, localId=home, visitorId=away, localScore=hs, visitorScore=as_, matchDate=when,
                 local={"id": home, "name": f"Club {home}"}, visitor={"id": away, "name": f"Club {away}"})


def test_trim_keeps_what_the_check_needs():
    body = {"filters": {"season": "2026"}, "resultSet": {"count": 1},
            "matches": [real("Athletic Club", "Club Atlético de Madrid", "2026-11-14T20:00:00Z",
                             "FINISHED", (2, 1), 12), "junk"]}
    trimmed = trim(body)
    assert len(str(trimmed)) < len(str(body)) / 2
    [m] = parse_matches(trimmed)
    assert (m.home, m.away, m.status, m.matchday, m.home_goals, m.away_goals) == (
        "athletic", "atletico", "FINISHED", 12, 2, 1)
    assert m.kickoff == datetime(2026, 11, 14, 20, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("now, season", [(datetime(2026, 11, 14), 2026), (datetime(2027, 3, 1), 2026),
                                         (datetime(2026, 8, 15), 2026), (datetime(2026, 6, 30), 2025)])
def test_seasons_are_named_by_their_first_year(now, season):
    assert season_of(now) == season


def test_cross_check_finds_each_kind_of_disagreement():
    upcoming = {12: [fantasy("a", "1", "2", "2026-11-14T21:00:00+01:00"),  # postponed in reality
                     fantasy("b", "3", "4", "2026-11-15T18:30:00+01:00"),  # moved a day
                     fantasy("c", "5", "6", "2026-11-15T16:15:00+01:00"),  # time not fixed yet: fine
                     fantasy("d", "7", "8", "2026-11-15T21:00:00+01:00"),  # agrees
                     fantasy("e", "98", "99", "2026-11-15T21:00:00+01:00")]}  # unknown clubs
    played = {
        9: [fantasy("w", "1", "4", "2026-10-31T16:00:00+01:00", 9, 9)],  # too old to check
        10: [fantasy("x", "2", "3", "2026-11-01T16:00:00+01:00", 1, 0)],  # agrees
        11: [fantasy("y", "4", "5", "2026-11-08T16:00:00+01:00", 2, 2),  # differs
             fantasy("z", "6", "7", "2026-11-08T18:00:00+01:00")],  # missing in LaLiga Fantasy
    }
    body = trim({"matches": [
        real("Club 1", "Club 2", "2026-11-14T20:00:00Z", "POSTPONED"),
        real("Club 3", "Club 4", "2026-11-16T20:00:00Z"),
        real("Club 5", "Club 6", "2026-11-15T00:00:00Z", "SCHEDULED"),
        real("Club 7", "Club 8", "2026-11-15T20:10:00Z"),  # 10 minutes off: within tolerance
        real("Club 1", "Club 4", "2026-10-31T15:00:00Z", "FINISHED", (0, 0)),
        real("Club 2", "Club 3", "2026-11-01T15:00:00Z", "FINISHED", (1, 0)),
        real("Club 4", "Club 5", "2026-11-08T15:00:00Z", "FINISHED", (2, 1)),
        real("Club 6", "Club 7", "2026-11-08T17:00:00Z", "FINISHED", (0, 3)),
    ]})
    check = cross_check(parse_matches(body), upcoming, played, NOW, fmt=lambda t: t.strftime("%d %H:%M"))
    found = {(p.kind, p.home, p.away): (p.fantasy, p.real) for p in check.problems}
    assert found == {
        ("postponed", "1", "2"): ("14 21:00", "postponed"),
        ("kickoff", "3", "4"): ("15 18:30", "16 20:00"),
        ("score", "4", "5"): ("2–2", "2–1"),
        ("missing", "6", "7"): ("no result", "0–3"),
    }
    assert (check.fixtures, check.results, check.unpaired) == (4, 3, 1)
    assert check.postponed == {("1", "2")}


def test_a_postponed_fixture_counts_half_and_says_so():
    schedule = {12: [Match(id="m", localId="a", visitorId="b")]}
    model = FixtureModel({}, schedule, 12, {"a": "AAA", "b": "BBB"}, postponed={("a", "b")})
    assert model.multiplier(FWD, Fixture("b", "a", False)) == POSTPONED
    player = __import__("fantasybot.api.models", fromlist=["Player"]).Player.model_validate(
        {"id": "p", "nickname": "P", "positionId": FWD, "teamId": "b"})
    assert model.outlook(player).next_label == "vs AAA (a, postponed)"


# -- in the briefing ---------------------------------------------------------------


def season_body(*, postpone: bool = True, moved: bool = True, wrong_score: bool = True) -> dict:
    """football-data.org's view of the test league's season."""
    out = []
    for week, matches in scout_league.schedule().items():
        for m in matches:
            home, away = f"Club {m['localId']}", f"Club {m['visitorId']}"
            when = utc(m["matchDate"])
            if "localScore" in m:
                score = (m["localScore"], m["visitorScore"])
                if wrong_score and m["id"] == "R11b":
                    score = (3, 3)
                out.append(real(home, away, when, "FINISHED", score, int(week)))
            elif postpone and m["id"] == "G1":
                out.append(real(home, away, when, "POSTPONED", matchday=int(week)))
            elif moved and m["id"] == "G2":
                out.append(real(home, away, "2026-11-16T20:00:00Z", matchday=int(week)))
            else:
                out.append(real(home, away, when, matchday=int(week)))
    return {"filters": {"season": "2026"}, "matches": out}


@pytest.fixture
def fd_settings(settings):
    return settings.model_copy(update={"football_data_token": SecretStr(TOKEN)})


async def test_the_briefing_reports_what_football_data_disagrees_with(
        fake, store, fd_settings, tokens, freeze, http, clock, alerter, router):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=season_body())

    router.add("GET", MATCHES_URL, handler)
    scout_league.build(fake)
    external = ExternalData(http, fd_settings, clock=lambda: scout_league.NOW)
    text, view = await scout(fake, store, fd_settings, tokens, freeze, http, clock, alerter,
                             external=external)
    assert len(calls) == 1
    assert calls[0].headers["X-Auth-Token"] == TOKEN and calls[0].url.params["season"] == "2026"
    section = text.split("🔎")[1].split("\n\n")[0]
    assert section.startswith(" LaLiga Fantasy vs football-data.org:")
    assert ("Not being played: C3 v C5 (jornada 12): postponed. Points only come at a later recierre, "
            "so they count half here. Yours: ") in section
    assert "Def A" in section.split("Yours:")[1].splitlines()[0]
    assert ("Kickoff moved: C7 v C9 (jornada 12): LaLiga Fantasy Sun 15 Nov 18:30, "
            "football-data.org Mon 16 Nov 21:00. Check the lineup deadline in the app") in section
    assert "Result differs: C7 v C9 (jornada 11): LaLiga Fantasy 3–1, football-data.org 3–3" in section
    fwd_b = next(p for p in view.my_squad if p.name == "Fwd B")  # club 5
    assert "postponed" in fwd_b.projection.fixture
    assert TOKEN not in text


async def test_a_clean_check_is_one_line(fake, store, fd_settings, tokens, freeze, http, clock, alerter,
                                         router):
    router.add("GET", MATCHES_URL,
               httpx.Response(200, json=season_body(postpone=False, moved=False, wrong_score=False)))
    scout_league.build(fake)
    text, view = await scout(fake, store, fd_settings, tokens, freeze, http, clock, alerter,
                             external=ExternalData(http, fd_settings, clock=lambda: scout_league.NOW))
    assert ("🔎 football-data.org agrees with LaLiga Fantasy: 6 upcoming fixtures, 4 recent results."
            in text)
    assert view.data_check is not None and not view.data_check.problems


async def test_a_refused_token_is_reported_and_nothing_else_changes(
        fake, store, fd_settings, tokens, freeze, http, clock, alerter, router):
    router.add("GET", MATCHES_URL, httpx.Response(403, json={"message": "restricted"}))
    scout_league.build(fake)
    text, view = await scout(fake, store, fd_settings, tokens, freeze, http, clock, alerter,
                             external=ExternalData(http, fd_settings, clock=lambda: scout_league.NOW))
    assert "real fixtures: HTTP 403: the key, or its free plan, doesn't allow this" in text
    assert view.data_check is None and "🔎" not in text


async def test_health_check_covers_football_data(fd_settings, tokens, client, clock, http, store, router):
    from fantasybot.healthcheck import HealthCheck

    from .conftest import NOW as TOKEN_NOW
    from .conftest import b2c_ok, seed

    seed(store, id_exp=TOKEN_NOW + 3600)
    router.add("POST", "https://login.laliga.es/", b2c_ok(TOKEN_NOW + 3600))
    router.add("GET", COMPETITION_URL,
               httpx.Response(200, json={"id": 2014, "code": "PD", "currentSeason": {
                   "id": 2400, "startDate": "2026-08-14", "endDate": "2027-05-23", "currentMatchday": 12}}),
               httpx.Response(429, json={"message": "slow down"}))
    external = ExternalData(http, fd_settings)
    report = await HealthCheck(fd_settings, tokens, client, clock=clock, external=external).run()
    result = {r.name: r for r in report.results}["football_data"]
    assert result.status == "ok" and result.detail == "season from 2026-08-14, matchday 12"
    report = await HealthCheck(fd_settings, tokens, client, clock=clock, external=external).run()
    result = {r.name: r for r in report.results}["football_data"]
    assert result.status == "warn" and "429" in result.detail and report.healthy
