from __future__ import annotations

import json
import stat
from datetime import date, timedelta

import pytest

from fantasybot.api.models import Lineup, Player, TeamData
from fantasybot.scout.analysis import (
    Rival,
    assess_readiness,
    best_lineup,
    build_squad,
    clause_radar,
    money,
    next_kickoff,
    parse_formations,
    read_lineup,
    sell_to_cover,
)
from fantasybot.scout.briefing import gather, pack, run_briefing
from fantasybot.scout.guard import run_guard
from fantasybot.scout.history import PriceHistory
from fantasybot.scout.model import COACH, project

from . import scout_league
from .conftest import NOW as TOKEN_NOW
from .conftest import seed
from .scout_league import CET, NOW


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
def league(fake, store):
    seed(store, id_exp=TOKEN_NOW + 3600)
    return scout_league.build(fake)


@pytest.fixture
def scout_client(settings, tokens, freeze, http, clock):
    from fantasybot.api import FantasyClient

    clock.now = NOW.timestamp() - 60  # the token stays valid for the whole test
    return FantasyClient(settings, tokens, freeze, http, sleep=no_sleep, clock=lambda: 0.0)


@pytest.fixture
def seeded_store(store, clock):
    seed(store, id_exp=int(NOW.timestamp()) + 7200)
    return store


# -- expected points -----------------------------------------------------------


def make(**fields) -> Player:
    return Player.model_validate({"id": "X", "positionId": 3, "averagePoints": 4, **fields})


def week(n: int, points: int, minutes: int = 90) -> dict:
    return {"weekNumber": n, "totalPoints": points, "stats": {"mins_played": [minutes, 2]}}


def test_projection_blends_recent_form_with_the_season():
    p = make(playerStats=[week(8, 0), week(9, 6), week(10, 8), week(11, 10), week(12, 0, 0)])
    proj = project(p, current_week=12)  # week 12 isn't finished, so it's ignored
    assert proj.form == 8.0
    assert proj.expected == pytest.approx(0.6 * 8 + 0.4 * 4)


@pytest.mark.parametrize(("status", "factor"), [("injured", 0), ("suspended", 0), ("doubt", 0.5)])
def test_unavailable_players_are_discounted(status, factor):
    assert project(make(playerStatus=status)).expected == pytest.approx(4 * factor)


def test_not_playing_last_jornada_is_a_rotation_risk_but_not_for_coaches():
    played = [week(9, 6), week(10, 6), week(11, 0, minutes=0)]
    assert project(make(playerStats=played), 12).note == "didn't play last jornada"
    assert project(make(playerStats=played, positionId=COACH), 12).note == ""


def test_position_falls_back_to_the_spanish_name():
    from fantasybot.scout.model import position_of

    assert position_of(Player.model_validate({"id": 1, "position": "Entrenador"})) == COACH


# -- lineups and readiness -----------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [
    ([[4, 4, 2], "3-5-2", {"tacticalFormation": [4, 3, 3]}, "1-5-3-2", "433"],
     [(3, 5, 2), (4, 3, 3), (4, 4, 2), (5, 3, 2)]),
    ([], [(3, 4, 3), (3, 5, 2), (4, 3, 3), (4, 4, 2), (4, 5, 1), (5, 3, 2), (5, 4, 1)]),
])
def test_parse_formations(raw, expected):
    assert parse_formations(raw) == expected


def my_squad(fake):
    team = TeamData.model_validate(fake.data["team_data_by_id"]["T1"])
    details = {pid: Player.model_validate(p) for pid, p in scout_league.PLAYERS.items()}
    return build_squad(team, details, current_week=12)


def test_best_lineup_benches_the_injured_and_picks_the_best_formation(fake):
    scout_league.build(fake)
    squad = my_squad(fake)
    plan = best_lineup(squad, parse_formations(fake.data["free_formations"]))
    names = {p.name for p in plan.starters}
    assert "Hurt" not in names and "Def D" in names and "Backup" not in names
    assert plan.coach.name == "Coach"
    assert len(plan.starters) == 11


def test_readiness_finds_what_costs_points(fake):
    scout_league.build(fake)
    squad = my_squad(fake)
    current = read_lineup(Lineup.model_validate(fake.data["current_lineup"]), squad)
    assert (current.filled, current.slots, current.not_owned) == (11, 11, 0)
    assert current.coach.name == "Coach"

    kickoff = next_kickoff({12: [m for m in _matches(fake, "12")]}, 12, NOW)
    ready = assess_readiness(-1_500_000, current, squad, kickoff)
    assert not ready.ready
    assert any("0 points" in p for p in ready.problems)
    assert any("Hurt (DEF) is injured" in p for p in ready.problems)
    assert any("Rested (FWD) didn't play" in w for w in ready.warnings)


def test_a_starter_without_a_match_is_a_problem(fake):
    from fantasybot.scout.difficulty import FixtureModel, club_records

    scout_league.build(fake)
    matches = {w: _matches(fake, str(w)) for w in (12, 13, 14)}
    past = [m for w in range(1, 12) for m in _matches(fake, str(w))]
    model = FixtureModel(club_records(past, NOW), matches, 12, {})
    team = TeamData.model_validate(fake.data["team_data_by_id"]["T1"])
    squad = build_squad(team, {}, 12, model.outlook)
    current = read_lineup(Lineup.model_validate(fake.data["current_lineup"]), squad)
    ready = assess_readiness(100, current, squad, None)
    assert "Mid D (MID) has no match this jornada but is in your lineup." in ready.problems
    mid_d = next(p for p in squad if p.name == "Mid D")
    assert mid_d.expected == 0 and mid_d.outlook > 0  # he plays in later jornadas


def test_a_sold_player_left_in_the_lineup_is_flagged(fake):
    scout_league.build(fake)
    fake.data["current_lineup"]["formation"]["striker"] = [{"playerTeamId": "SOLD"}]
    squad = my_squad(fake)
    current = read_lineup(Lineup.model_validate(fake.data["current_lineup"]), squad)
    ready = assess_readiness(100, current, squad, None)
    assert "Your lineup has 10 of 11 players." in ready.problems
    assert "1 lineup slot(s) hold players you no longer own." in ready.problems


def _matches(fake, week_number):
    from fantasybot.api.models import Match

    return [Match.model_validate(m) for m in fake.data["matchday_by_week"][week_number]]


def test_next_kickoff_skips_finished_jornadas(fake):
    scout_league.build(fake)
    matches = {12: _matches(fake, "12"), 13: _matches(fake, "13")}
    assert next_kickoff(matches, 12, NOW).jornada == 12
    later = NOW + timedelta(days=2)
    assert next_kickoff(matches, 12, later).jornada == 13
    mid = NOW + timedelta(hours=13)  # after Friday's match, before Saturday's
    assert next_kickoff(matches, 12, mid).in_progress


def test_sell_to_cover_uses_bench_players_only(fake):
    scout_league.build(fake)
    squad = my_squad(fake)
    plan = best_lineup(squad, parse_formations([]))
    picked = sell_to_cover(1_500_000, squad, plan.starters + [plan.coach])
    assert picked and sum(p.value for p in picked) >= 1_500_000
    assert not {p.name for p in picked} & {p.name for p in plan.starters}


# -- clauses -------------------------------------------------------------------


def test_clause_radar(fake):
    from fantasybot.api.models import StandingEntry

    scout_league.build(fake)
    rows = {r["team"]["id"]: StandingEntry.model_validate(r) for r in fake.data["league_ranking"]}
    rivals = [Rival(rows[t], rank, build_squad(TeamData.model_validate(
        fake.data["team_data_by_id"][t]), {}, 12)) for rank, t in ((1, "T2"), (2, "T3"), (4, "T4"))]
    radar = clause_radar(rivals, 4_000_000, NOW)
    assert [t.player.name for t in radar.affordable] == ["Star", "Cheap"]  # best first
    assert [t.player.name for t in radar.unlocking_soon] == ["Soon"]
    assert [t.player.name for t in radar.out_of_reach] == ["Big"]
    every = {t.player.name for t in radar.affordable + radar.unlocking_soon + radar.out_of_reach}
    assert "Shielded" not in every and "Locked" not in every


# -- price history -------------------------------------------------------------


def test_price_history_tracks_changes(tmp_path):
    def p(pid, value):
        return Player.model_validate({"id": pid, "nickname": pid, "marketValue": value})

    path = tmp_path / "state" / "history.sqlite"
    with PriceHistory(path) as h:
        h.record(date(2026, 11, 1), [p("A", 100), p("B", 100)])
        h.record(date(2026, 11, 3), [p("A", 150), p("B", 60), p("C", 10)])
        assert h.change(date(2026, 11, 3), 1) == {"A": 50, "B": -40}  # nearest older day
        risers, fallers = h.movers(date(2026, 11, 3), 1, 5)
        assert [m.player_id for m in risers] == ["A"] and fallers[0].delta == -40
        assert h.change(date(2026, 11, 3), 5) == {}  # nothing that old
        h.record(date(2026, 11, 3), [p("A", 160)])  # same day again: replaced, not duplicated
        assert h.change(date(2026, 11, 3), 1)["A"] == 60
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# -- the briefing --------------------------------------------------------------


async def test_briefing_end_to_end(league, scout_client, settings, alerter, seeded_store):
    messages = await run_briefing(scout_client, settings, alerter, now=NOW, sleep=no_sleep)
    text = "\n\n".join(messages)
    assert alerter.messages == messages
    assert all(len(m) <= 4096 for m in messages)

    assert "1. Ana 1100 · 2. Bea 1000 · 3. You 900 · 4. Carl 800 · 200 behind Ana" in text
    assert "Cash 4.00M · max bid 24.00M" in text
    assert "Rivals' cash ≈ Ana 15.00M · Bea 2.00M · Carl 21.00M" in text
    # Readiness and lineup
    assert "Not ready (jornada 12 starts Sat 14 Nov 21:00)" in text
    assert "Hurt (DEF) is injured but in your lineup." in text
    assert "Mid D (MID) has no match this jornada" in text
    assert "Out: Hurt" in text and "Def D" in text.split("In:")[1]
    assert "Isco MID · 1.20M · 3.0 xP/j · next vs C7 (a)" in text
    # Market: own listing excluded, injured filtered, affordability flags
    market = text.split("🛒")[1].split("🎯")[0]
    assert "closes Sat 14 Nov 13:20" in market
    assert "Isco" in market and "✓" in market
    assert "Over your max bid: Pricey (40.00M" in market
    assert market.index("Isco") < market.index("Listed") < market.index("Pricey")
    assert "Listed" in market and "⚠ credit" in market.split("Listed")[1].splitlines()[0]
    assert "Crocked" not in market and "Fwd B" not in market
    # Clauses
    clauses = text.split("🎯")[1].split("🛡️")[0]
    assert "Star (FWD) from Ana (#1) · clause 3.00M" in clauses
    assert "Soon (MID) from Ana" in clauses and "Big (FWD)" in clauses
    assert "Shielded" not in clauses
    exposure = text.split("🛡️")[1]
    assert "Fwd A (FWD)" in exposure  # open clause, not shielded
    assert "within reach of Carl (≈21.00M)" in exposure.split("Fwd A")[1].splitlines()[0]
    assert "within reach of Ana (≈15.00M), Carl (≈21.00M)" in exposure.split("Def D")[1].splitlines()[0]
    assert "Keeper" not in exposure.split("Protect")[0]  # shielded
    assert "Mid A" not in exposure.split("Protect")[0]  # still locked
    assert "raising his clause" in exposure and "Shield" not in exposure  # no shields in this league
    # History and activity
    assert "Price history started today" in text
    assert "Ana clausuló a Fwd X" in text and "Old news" not in text
    assert (settings.state_dir / "history.sqlite").exists()


async def test_second_day_shows_price_moves(league, scout_client, settings, alerter, seeded_store):
    await run_briefing(scout_client, settings, alerter, now=NOW - timedelta(days=1),
                       send=False, sleep=no_sleep)
    for p in league.data["players"]:
        if p["id"] == "P40":
            p["marketValue"] += 1_500_000
    messages = await run_briefing(scout_client, settings, alerter, now=NOW, send=False, sleep=no_sleep)
    text = "\n".join(messages)
    assert "Your players since yesterday: Fwd A +1.50M" in text
    assert alerter.messages == []  # send=False


async def test_briefing_survives_a_broken_endpoint(league, scout_client, settings, alerter,
                                                   seeded_store):
    import httpx

    league.overrides["market"] = httpx.Response(500, json={"message": "down"})
    messages = await run_briefing(scout_client, settings, alerter, now=NOW, sleep=no_sleep)
    text = "\n".join(messages)
    assert "Market: nothing listed, or it couldn't be loaded." in text
    assert "Missing today: market: HTTP 500: down" in text
    assert "Clause steals" in text  # the rest still arrives


def test_pack_splits_long_briefings():
    messages = pack(["a" * 3000, "b" * 3000, "c" * 10])
    assert len(messages) == 2 and messages[1].endswith("c" * 10)


def test_money_formatting():
    assert (money(16_554_348), money(603_150), money(-1_500_000)) == ("16.55M", "603k", "−1.50M")


# -- the kickoff guard ---------------------------------------------------------


async def test_guard_alerts_once_then_again_before_kickoff(league, scout_client, settings,
                                                           alerter, seeded_store):
    league.data["team_money"] = {"teamMoney": -1_500_000}
    first = await run_guard(scout_client, settings, alerter, now=NOW, sleep=no_sleep)
    assert first.startswith("⏰ Jornada 12 starts in 12h")
    assert "0 points" in first and "Hurt (DEF) is injured" in first
    assert "Bench players whose value covers the 1.50M" in first
    assert await run_guard(scout_client, settings, alerter, now=NOW + timedelta(hours=1),
                           sleep=no_sleep) is None  # already told
    final = await run_guard(scout_client, settings, alerter, now=NOW + timedelta(hours=10),
                            sleep=no_sleep)
    assert final.startswith("⏰ Jornada 12 starts in 2h")
    assert len(alerter.messages) == 2
    state = json.loads((settings.state_dir / "guard.json").read_text())
    assert len(state["sent"]) == 2


async def test_guard_is_quiet_when_ready_or_far_away(league, scout_client, settings, alerter,
                                                     seeded_store):
    # Far from kickoff: quiet even with problems.
    assert await run_guard(scout_client, settings, alerter, now=NOW - timedelta(days=2),
                           sleep=no_sleep) is None
    # Ready: swap out the injured defender, and move Mid D to a club that plays.
    league.data["current_lineup"]["formation"]["defender"][0] = {"playerTeamId": "PT24"}
    for owned in league.data["team_data_by_id"]["T1"]["players"]:
        if owned["playerTeamId"] == "PT33":
            owned["playerMaster"]["teamId"] = "5"
    assert await run_guard(scout_client, settings, alerter, now=NOW, sleep=no_sleep) is None
    assert alerter.messages == []


async def test_guard_dry_run_does_not_suppress_the_real_alert(league, scout_client, settings,
                                                              alerter, seeded_store):
    assert await run_guard(scout_client, settings, alerter, now=NOW, send=False, sleep=no_sleep)
    assert not (settings.state_dir / "guard.json").exists()
    assert await run_guard(scout_client, settings, alerter, now=NOW, sleep=no_sleep)
    assert len(alerter.messages) == 1


async def test_gather_reads_only_your_team_for_the_guard(league, scout_client, settings, seeded_store):
    view = await gather(scout_client, settings, NOW, rivals=False, market=False, details=False,
                        activity=False, sleep=no_sleep)
    assert list(view.squads) == ["T1"]
    assert league.get_calls("market") == [] and league.get_calls("player_details") == []
    assert view.balance == 4_000_000 and view.week == 12
    assert view.now.tzinfo == CET


async def test_times_show_in_the_configured_timezone(league, scout_client, settings, alerter,
                                                    seeded_store):
    settings.timezone = "Australia/Sydney"  # 21:00 in Madrid is 07:00 next morning there
    text = "\n".join(await run_briefing(scout_client, settings, alerter, now=NOW, send=False,
                                        sleep=no_sleep))
    assert "jornada 12 starts Sun 15 Nov 07:00" in text
    assert "☀️ FantasyBot · jornada 12 · Sat 14 Nov" in text  # 20:00 on the 14th in Sydney


def test_unknown_timezone_falls_back_to_utc(settings):
    from datetime import timezone

    settings.timezone = "Mars/Olympus"
    assert settings.display_tz() is timezone.utc


def test_cli_briefing_prints_without_sending(tmp_path, monkeypatch, capsys, fake, router):
    import functools

    import httpx
    from cryptography.fernet import Fernet

    import fantasybot.cli as cli
    from fantasybot.auth import TokenStore
    from fantasybot.runtime import open_runtime

    scout_league.build(fake)
    key = Fernet.generate_key()
    monkeypatch.setenv("FANTASYBOT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("FANTASYBOT_TOKEN_KEY", key.decode())
    monkeypatch.setenv("FANTASYBOT_REQUEST_DELAY", "0")
    seed(TokenStore(tmp_path / "tokens.enc", key), id_exp=2_000_000_000)
    monkeypatch.setattr(cli, "open_runtime",
                        functools.partial(open_runtime, transport=httpx.MockTransport(router)))
    assert cli.main(["briefing", "--no-send"]) == 0
    out = capsys.readouterr().out
    assert "Clause steals you can pay now" in out and "Best XI" in out
    assert (tmp_path / "history.sqlite").exists()
    assert cli.main(["guard", "--no-send", "--hours", "0"]) == 0  # nothing within 0h
    assert "Nothing to fix" in capsys.readouterr().out


async def test_activity_is_read_once_then_incrementally(league, scout_client, settings, alerter,
                                                        seeded_store):
    await run_briefing(scout_client, settings, alerter, now=NOW, send=False, sleep=no_sleep)
    assert len(league.get_calls("league_activity")) == 3  # pages 0, 1 and the empty page 2
    await run_briefing(scout_client, settings, alerter, now=NOW, send=False, sleep=no_sleep)
    assert len(league.get_calls("league_activity")) == 4  # page 0 only: nothing new on it


async def test_cash_estimates_flag_unexplained_jumps(league, scout_client, settings, alerter,
                                                     seeded_store):
    await run_briefing(scout_client, settings, alerter, now=NOW - timedelta(days=1), send=False,
                       sleep=no_sleep)
    league.data["team_money"] = {"teamMoney": 9_000_000}  # +5M the feed doesn't explain
    text = "\n".join(await run_briefing(scout_client, settings, alerter, now=NOW, send=False,
                                        sleep=no_sleep))
    assert "Rivals' cash ≈ Ana 20.00M · Bea 7.00M · Carl 26.00M (uncertain today)" in text


# -- selling -------------------------------------------------------------------


async def test_selling_section(league, scout_client, settings, alerter, seeded_store):
    text = "\n".join(await run_briefing(scout_client, settings, alerter, now=NOW, send=False,
                                        sleep=no_sleep))
    selling = text.split("💸 Selling")[1].split("📈")[0]
    lines = [line.strip() for line in selling.strip().splitlines()]
    assert lines[0] == "· squad 15/24"
    # Best premium first: Carl's direct offer for unlisted Mid A, then your listing.
    assert lines[1] == ("• Mid A · not listed (value 10.00M) · best offer 12.00M from Carl "
                        "(+20% vs value) · ⭐ worth taking (in your best XI)")
    assert lines[2].startswith("• Fwd B · listed 6.50M (value 6.00M) · closes Sun 15 Nov 10:00")
    assert "best offer 6.60M from Ana (+10% vs value) · ⭐ worth taking" in lines[2]  # beats the 6.1M
    assert "• Hurt (DEF) · value 8.00M · injured" in selling
    assert "• Backup (GK) · value 1.00M · hardly scores" in selling
    assert "🪟 14 player(s) not listed today" in selling  # everyone except listed Fwd B


async def test_bench_shop_window_and_withdraw_hints(league, scout_client, settings, alerter,
                                                    seeded_store):
    settings.shop_window = "bench"
    text = "\n".join(await run_briefing(scout_client, settings, alerter, now=NOW, send=False,
                                        sleep=no_sleep))
    assert "↩ think about withdrawing: he's in your best XI" in text
    assert "🪟 3 player(s) not listed today" in text  # Backup, Hurt, Mid D


async def test_market_offers_are_recorded_and_summarised(league, scout_client, settings, alerter,
                                                         seeded_store):
    from fantasybot.scout.history import PriceHistory

    with PriceHistory(settings.state_dir / "history.sqlite") as h:
        for i, prem in enumerate((-0.09, -0.02, 0.03, 0.06, 0.10)):
            h.store_offer(NOW.date(), f"old{i}", "P1", int(1_000_000 * (1 + prem)), 1_000_000, True)
    text = "\n".join(await run_briefing(scout_client, settings, alerter, now=NOW, send=False,
                                        sleep=no_sleep))
    # Today's market offer for Fwd B (6.1M on 6.0M) is recorded too: 6 seen.
    assert "LaLiga's offers so far: -9% to +10% of value (average +1.6%, 6 seen)" in text


async def test_negative_balance_puts_covering_sales_first(league, scout_client, settings, alerter,
                                                          seeded_store):
    league.data["team_money"] = {"teamMoney": -500_000}
    text = "\n".join(await run_briefing(scout_client, settings, alerter, now=NOW, send=False,
                                        sleep=no_sleep))
    worth = text.split("Worth selling")[1].split("📈")[0]
    assert worth.strip().splitlines()[1].endswith("covers your negative balance")


def test_withdraw_when_listed_below_current_value():
    from fantasybot.scout.selling import OnSale, should_withdraw

    def squad_player(value):
        from fantasybot.scout.analysis import SquadPlayer
        from fantasybot.scout.model import project

        p = Player.model_validate({"id": "X", "nickname": "X", "marketValue": value, "averagePoints": 3})
        return SquadPlayer(p, "PTX", value, None, False, project(p))

    cheap = OnSale("PTX", squad_player(9_000_000), "M9", 8_000_000, None)
    assert should_withdraw(cheap, []) == "listed at 8.00M but now worth 9.00M"
    fair = OnSale("PTX", squad_player(8_000_000), "M9", 8_500_000, None)
    assert should_withdraw(fair, []) is None
