from __future__ import annotations

import stat
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr

from fantasybot.api.models import Match
from fantasybot.external import ExternalData
from fantasybot.external.names import club_key
from fantasybot.external.odds import MatchOdds, fit_goals, match_odds, parse_event, poisson_outcomes
from fantasybot.redact import redact
from fantasybot.scout.difficulty import MAX_FACTOR, MIN_FACTOR, Fixture, FixtureModel
from fantasybot.scout.model import COACH, DEF, FWD, GK, MID

from . import scout_league
from .conftest import NOW as TOKEN_NOW
from .conftest import seed

ODDS_URL = "https://api.the-odds-api.com/v4/sports/soccer_spain_la_liga/odds"
SPORTS_URL = "https://api.the-odds-api.com/v4/sports"
KEY = "0123456789abcdef0123456789abcdef"


# -- club names ------------------------------------------------------------------


@pytest.mark.parametrize("name, key", [
    ("Athletic Bilbao", "athletic"), ("Athletic Club", "athletic"),
    ("Atlético Madrid", "atletico"), ("Club Atlético de Madrid", "atletico"),
    ("Real Madrid", "real-madrid"), ("Real Madrid CF", "real-madrid"),
    ("RCD Espanyol de Barcelona", "espanyol"), ("Espanyol", "espanyol"),
    ("FC Barcelona", "barcelona"), ("Barça", "barcelona"),
    ("Rayo Vallecano de Madrid", "rayo-vallecano"), ("Rayo Vallecano", "rayo-vallecano"),
    ("Real Sociedad de Fútbol", "real-sociedad"), ("Deportivo Alavés", "alaves"),
    ("Alavés", "alaves"), ("RC Celta", "celta"), ("Celta Vigo", "celta"),
    ("Real Betis Balompié", "betis"), ("CA Osasuna", "osasuna"), ("Real Oviedo", "oviedo"),
    ("Elche CF", "elche"), ("Levante UD", "levante"), ("Girona FC", "girona"),
])
def test_every_source_name_maps_to_one_club(name, key):
    assert club_key(name) == key


def test_the_first_identifying_name_wins_and_unknowns_fall_back_to_a_slug():
    assert club_key("RMA", "Real Madrid") == "real-madrid"  # a 3-letter code identifies nothing
    assert club_key("Club 3", "C3") == "club-3"
    assert club_key(None, "") is None


# -- odds --------------------------------------------------------------------------


def priced(probabilities: list[float], margin: float) -> list[float]:
    return [round(1 / (p * margin), 3) for p in probabilities]


def event(home: str, away: str, rates: tuple[float, float], *, commence: str = "2026-11-14T20:00:00Z",
          books: int = 2, line: float = 2.5) -> dict:
    win, draw, loss, over = poisson_outcomes(*rates, line)
    total = win + draw + loss
    bookmakers = []
    for b in range(books):
        margin = 1.04 + 0.02 * b
        h, d, a = priced([win / total, draw / total, loss / total], margin)
        o, u = priced([over, 1 - over], margin)
        bookmakers.append({"key": f"book{b}", "markets": [
            {"key": "h2h", "outcomes": [{"name": home, "price": h}, {"name": "Draw", "price": d},
                                        {"name": away, "price": a}]},
            {"key": "totals", "outcomes": [{"name": "Over", "price": o, "point": line},
                                           {"name": "Under", "price": u, "point": line}]},
        ]})
    return {"id": f"{home}-{away}", "commence_time": commence, "home_team": home, "away_team": away,
            "bookmakers": bookmakers}


def test_margins_are_removed_and_bookmakers_averaged():
    e = parse_event(event("Real Madrid", "Getafe", (2.2, 0.6)))
    assert e is not None
    assert e.p_home + e.p_draw + e.p_away == pytest.approx(1.0)
    assert e.p_home > 0.65 and e.line == 2.5 and 0.5 < e.p_over < 0.7


def test_events_without_a_usable_market_are_skipped():
    broken = event("A", "B", (1, 1))
    broken["bookmakers"][0]["markets"][0]["outcomes"][1]["price"] = 1.0  # impossible price
    broken["bookmakers"] = broken["bookmakers"][:1]
    assert parse_event(broken) is None
    assert parse_event({"home_team": "A", "away_team": "B", "bookmakers": []}) is None


@pytest.mark.parametrize("rates", [(1.3, 1.1), (2.1, 0.7), (0.8, 1.6)])
def test_the_poisson_fit_recovers_each_sides_expected_goals(rates):
    fitted = fit_goals(parse_event(event("A", "B", rates)))
    assert fitted == pytest.approx(rates, abs=0.05)


def test_odds_pair_with_fixtures_by_name_and_date():
    fixtures = [
        Match(id="1", localId="10", visitorId="20", matchDate="2026-11-14T21:00:00+01:00",
              local={"id": "10", "name": "Athletic Club", "shortName": "ATH"},
              visitor={"id": "20", "name": "Club Atlético de Madrid", "shortName": "ATM"}),
        Match(id="2", localId="30", visitorId="40", matchDate="2026-11-15T18:30:00+01:00"),
    ]
    names = {"30": ("Rayo Vallecano de Madrid", "RAY", "rayo-vallecano"),
             "40": ("Real Madrid", "RMA", "real-madrid")}
    events = [
        event("Athletic Bilbao", "Atlético Madrid", (1.2, 1.3)),
        event("Rayo Vallecano", "Real Madrid", (0.9, 2.0), commence="2026-11-15T17:30:00Z"),
        event("Rayo Vallecano", "Real Madrid", (0.9, 2.0), commence="2027-04-20T17:30:00Z"),  # other round
        event("Sevilla", "Betis", (1.4, 1.2)),  # not in these fixtures
    ]
    odds = match_odds(events[:2], fixtures, names)
    assert set(odds) == {("10", "20"), ("30", "40")}
    assert odds[("30", "40")].away_goals == pytest.approx(2.0, abs=0.05)
    assert match_odds(events[2:], fixtures, names) == {}  # months apart, or unknown: no pairing


# -- odds in fixture difficulty --------------------------------------------------


def odds_model(home_goals: float, away_goals: float, p_home: float, p_away: float) -> FixtureModel:
    schedule = {12: [Match(id="m", localId="fav", visitorId="dog", matchDate="2026-11-14T21:00:00+01:00")]}
    odds = {("fav", "dog"): MatchOdds(home_goals, away_goals, p_home, 1 - p_home - p_away, p_away)}
    return FixtureModel({}, schedule, 12, {"fav": "FAV", "dog": "DOG"}, odds=odds)


def test_odds_drive_every_position():
    m = odds_model(2.2, 0.6, 0.70, 0.10)
    home, away = Fixture("fav", "dog", True), Fixture("dog", "fav", False)
    assert m.multiplier(FWD, home) > 1.15 and m.multiplier(MID, home) > 1.15
    assert m.multiplier(FWD, away) < 0.85
    assert m.multiplier(GK, home) > 1.1 and m.multiplier(DEF, away) < 0.85  # clean sheets
    assert m.multiplier(COACH, home) == MAX_FACTOR and m.multiplier(COACH, away) == MIN_FACTOR


def test_an_even_match_by_the_odds_is_neutral():
    m = odds_model(1.3, 1.3, 0.37, 0.37)
    for position in (GK, DEF, MID, FWD, COACH):
        assert m.multiplier(position, Fixture("fav", "dog", True)) == pytest.approx(1.0, abs=0.01)


def test_labels_show_the_win_chance_and_results_fill_in_without_odds():
    m = odds_model(2.2, 0.6, 0.70, 0.10)
    assert m.outlook(scout_league_player("dog")).next_label == "vs FAV (a, 10% win)"
    assert m.win_chance("fav") == pytest.approx(0.70)
    no_odds = FixtureModel({}, {12: [Match(id="m", localId="fav", visitorId="dog")]}, 12, {"dog": "DOG"})
    assert no_odds.outlook(scout_league_player("fav")).next_label == "vs DOG (h)"
    assert no_odds.multiplier(FWD, Fixture("fav", "dog", True)) == pytest.approx(1.05)  # results path


def scout_league_player(club: str):
    from fantasybot.api.models import Player

    return Player.model_validate({"id": "p", "nickname": "P", "positionId": FWD, "teamId": club})


# -- fetching, caching, the briefing ---------------------------------------------


@pytest.fixture
def odds_settings(settings):
    return settings.model_copy(update={"odds_api_key": SecretStr(KEY)})


@pytest.fixture
def odds_calls(router):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, headers={"x-requests-remaining": "480"}, json=[
            event("Club 3", "Club 5", (2.1, 0.8)),
            event("Club 7", "Club 9", (1.9, 1.0), commence="2026-11-15T17:30:00Z"),
        ])

    router.add("GET", ODDS_URL, handler)
    return calls


async def scout(fake, store, settings, tokens, freeze, http, clock, alerter, *, external, now=None,
                guard=False):
    from fantasybot.api import FantasyClient
    from fantasybot.scout.briefing import briefing
    from fantasybot.scout.guard import guard_check

    async def no_sleep(_):
        return None

    seed(store, id_exp=TOKEN_NOW + 3600)
    clock.now = scout_league.NOW.timestamp() - 60
    client = FantasyClient(settings, tokens, freeze, http, sleep=no_sleep, clock=lambda: 0.0)
    now = now or scout_league.NOW
    if guard:
        return (await guard_check(client, settings, alerter, now=now, send=False, external=external,
                                  sleep=no_sleep)).view
    messages, view = await briefing(client, settings, alerter, now=now, send=False, external=external,
                                    sleep=no_sleep)
    return "\n\n".join(messages), view


async def test_the_briefing_uses_the_odds(fake, store, odds_settings, tokens, freeze, http, clock,
                                          alerter, odds_calls):
    scout_league.build(fake)
    external = ExternalData(http, odds_settings, clock=lambda: scout_league.NOW)
    text, view = await scout(fake, store, odds_settings, tokens, freeze, http, clock, alerter,
                             external=external)
    assert len(odds_calls) == 1
    params = odds_calls[0].url.params
    assert params["apiKey"] == KEY and params["markets"] == "h2h,totals" and params["regions"] == "eu"
    assert set(view.odds) == {("3", "5"), ("7", "9")}
    section = text.split("🎲")[1].split("\n\n")[0]
    assert section.startswith(" Jornada 12 by the odds")
    assert section.index("C3") < section.index("C7")  # the bigger favourite first
    assert "C5★" in section  # you own club 5 players
    assert "Isco MID · 1.20M" in text and "next vs C7 (a, " in text and "% win)" in text
    assert KEY not in text


async def test_odds_are_fetched_once_a_day_and_the_guard_never_fetches(
        fake, store, odds_settings, tokens, freeze, http, clock, alerter, odds_calls):
    scout_league.build(fake)
    external = ExternalData(http, odds_settings, clock=lambda: scout_league.NOW)
    view = await scout(fake, store, odds_settings, tokens, freeze, http, clock, alerter,
                       external=external, guard=True)
    assert odds_calls == [] and view.odds == {}  # nothing cached yet, and the guard won't spend
    await scout(fake, store, odds_settings, tokens, freeze, http, clock, alerter, external=external)
    await scout(fake, store, odds_settings, tokens, freeze, http, clock, alerter, external=external)
    assert len(odds_calls) == 1  # the second briefing used today's copy
    view = await scout(fake, store, odds_settings, tokens, freeze, http, clock, alerter,
                       external=external, guard=True)
    assert len(odds_calls) == 1 and set(view.odds) == {("3", "5"), ("7", "9")}


async def test_a_broken_odds_api_falls_back_to_the_last_copy(
        fake, store, odds_settings, tokens, freeze, http, clock, alerter, router):
    scout_league.build(fake)
    router.add("GET", ODDS_URL,
               httpx.Response(200, json=[event("Club 3", "Club 5", (2.1, 0.8))]),
               httpx.Response(401, json={"message": f"bad key {KEY}"}),
               httpx.Response(401, json={"message": f"bad key {KEY}"}))
    now = scout_league.NOW
    external = ExternalData(http, odds_settings, clock=lambda: now)
    await scout(fake, store, odds_settings, tokens, freeze, http, clock, alerter, external=external)
    now = scout_league.NOW + timedelta(hours=7)  # today's copy is stale now
    text, view = await scout(fake, store, odds_settings, tokens, freeze, http, clock, alerter,
                             external=external, now=now)
    assert "odds: HTTP 401: check the key or token (using the last copy)" in text
    assert ("3", "5") in view.odds and KEY not in text

    fresh = odds_settings.model_copy(update={"state_dir": odds_settings.state_dir.parent / "other"})
    text, view = await scout(fake, store, fresh, tokens, freeze, http, clock, alerter,
                             external=ExternalData(http, fresh, clock=lambda: now))
    assert "odds: HTTP 401: check the key or token" in text and "last copy" not in text
    assert view.odds == {} and "🎲" not in text  # results-based difficulty, as before


async def test_without_a_key_nothing_is_fetched(fake, store, settings, tokens, freeze, http, clock,
                                                alerter, odds_calls):
    scout_league.build(fake)
    text, view = await scout(fake, store, settings, tokens, freeze, http, clock, alerter,
                             external=ExternalData(http, settings))
    assert odds_calls == [] and view.odds == {} and "odds:" not in text and "🎲" not in text


# -- health check ------------------------------------------------------------------


async def run_health(settings, tokens, client, clock, http):
    from fantasybot.healthcheck import HealthCheck

    report = await HealthCheck(settings, tokens, client, clock=clock,
                               external=ExternalData(http, settings)).run()
    return report, {r.name: r for r in report.results}


@pytest.fixture
def b2c(router):
    from .conftest import b2c_ok

    router.add("POST", "https://login.laliga.es/", b2c_ok(TOKEN_NOW + 3600))


async def test_health_check_skips_outside_sources_that_arent_set_up(settings, tokens, client, clock,
                                                                    http, store, b2c):
    seed(store, id_exp=TOKEN_NOW + 3600)
    report, results = await run_health(settings, tokens, client, clock, http)
    assert results["odds_api"].status == "skip" and report.healthy


async def test_health_check_reports_the_odds_key_and_credits(odds_settings, tokens, client, clock, http,
                                                             store, router, b2c):
    seed(store, id_exp=TOKEN_NOW + 3600)
    sports = [{"key": "soccer_spain_la_liga", "active": True}, {"key": "soccer_epl", "active": True}]
    router.add("GET", SPORTS_URL + "?",
               httpx.Response(200, headers={"x-requests-remaining": "482"}, json=sports),
               httpx.Response(200, headers={"x-requests-remaining": "12"}, json=sports),
               httpx.Response(401, json={"message": "invalid"}))
    _, results = await run_health(odds_settings, tokens, client, clock, http)
    assert results["odds_api"].status == "ok" and "482" in results["odds_api"].detail
    report, results = await run_health(odds_settings, tokens, client, clock, http)
    assert results["odds_api_credits"].status == "warn" and "12 credits" in results["odds_api_credits"].detail
    report, results = await run_health(odds_settings, tokens, client, clock, http)
    assert results["odds_api"].status == "warn"
    assert results["odds_api"].detail == "The Odds API: HTTP 401: check the key or token"
    assert report.healthy  # outside data never fails the LaLiga health check
    assert "⚠️ The Odds API: HTTP 401" in report.alert_text()


# -- secrets -------------------------------------------------------------------------


def test_secret_set_stores_a_private_file(tmp_path, monkeypatch, capsys):
    import io

    from fantasybot.cli import main

    target = tmp_path / "secrets" / "odds_api_key"
    monkeypatch.setenv("FANTASYBOT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FANTASYBOT_ODDS_API_KEY_FILE", str(target))
    monkeypatch.setattr("sys.stdin", io.StringIO("not a key!\n"))
    assert main(["secret", "set", "odds_api_key"]) == 1
    assert not target.exists()
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    assert main(["secret", "set", "odds_api_key"]) == 0
    assert target.read_text().strip() == KEY
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    out = capsys.readouterr()
    assert KEY not in out.out + out.err

    from fantasybot.config import Settings

    assert Settings().optional_secret("odds_api_key") == KEY
    target.write_text("")
    assert Settings().optional_secret("odds_api_key") is None  # an empty file is "not set up"


def test_keys_in_urls_and_headers_are_redacted():
    text = f"GET {ODDS_URL}?apiKey={KEY}&regions=eu failed; X-Auth-Token: {KEY}"
    assert KEY not in redact(text) and "regions=eu" in redact(text)
