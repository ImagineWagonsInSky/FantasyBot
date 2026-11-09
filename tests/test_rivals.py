from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from fantasybot.api.models import ActivityEntry, MarketItem, Player, StandingEntry
from fantasybot.scout.analysis import Rival, SquadPlayer
from fantasybot.scout.cash import CashEstimates
from fantasybot.scout.model import COACH, DEF, FWD, GK, MID, Projection
from fantasybot.scout.rivals import (
    DEFAULT_PREMIUM,
    build_profiles,
    competition,
    gap_story,
    purchase_premiums,
    swing_targets,
)

from . import scout_league
from .test_external import scout

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)
M = 1_000_000
FORMATIONS = [(4, 4, 2)]


def sp(pid: str, pos: int, outlook: float, *, status: str = "ok", season: float | None = None,
       value: int = 5 * M, clause: int | None = None, shielded: bool = False) -> SquadPlayer:
    player = Player.model_validate({"id": pid, "nickname": pid, "positionId": pos, "marketValue": value,
                                    "playerStatus": status})
    available = 0.0 if status in ("injured", "suspended") else 1.0
    average = season if season is not None else outlook
    projection = Projection(outlook * available, outlook * available, average, None, available, "")
    clause = clause if clause is not None else value
    return SquadPlayer(player, f"PT-{pid}", clause, None, shielded, projection)


def full_squad(prefix: str, defs: list[float], extra: list[SquadPlayer] = ()) -> list[SquadPlayer]:
    return ([sp(f"{prefix}GK", GK, 4), sp(f"{prefix}C", COACH, 3)]
            + [sp(f"{prefix}D{i}", DEF, o) for i, o in enumerate(defs)]
            + [sp(f"{prefix}M{i}", MID, 4) for i in range(4)]
            + [sp(f"{prefix}F{i}", FWD, 5) for i in range(2)] + list(extra))


def entry(team: str, manager: int, name: str, points: float, value: int = 100 * M) -> StandingEntry:
    team_data = {"id": team, "manager": {"id": manager, "managerName": name}}
    return StandingEntry.model_validate({"points": points, "teamValue": value, "team": team_data})


def act(type_id: int, actor: int, amount: int, player: str | None = None, days_ago: float = 1,
        other: int | None = None, text: str | None = None) -> ActivityEntry:
    return ActivityEntry(activityTypeId=type_id, user1Id=actor, user2Id=other, amount=amount,
                         playerMasterId=player, description=text,
                         createdAt=(NOW - timedelta(days=days_ago)).isoformat())


def values(table: dict[str, int]):
    return lambda pid, day: table.get(pid)


# -- what friends pay -------------------------------------------------------------


def test_premiums_count_purchases_only_and_skip_glitches():
    feed = [act(1, 202, 11 * M, "P1"), act(31, 202, 12 * M, "P2"),
            act(32, 202, 20 * M, "P3"),  # clause steal: the price is the clause, not a choice
            act(1, 202, 20 * M, "P4", text="Ana ha clausulado a X"),  # older feeds' clause steals
            act(1, 202, 100 * M, "P5"),  # 10x the value: a data glitch
            act(1, 202, 10 * M, "P6"),  # no recorded value: skipped
            act(1, 303, 10 * M, "P1")]
    premiums = purchase_premiums(feed, values({"P1": 10 * M, "P2": 10 * M, "P3": 10 * M,
                                               "P4": 10 * M, "P5": 10 * M}))
    assert premiums == {"202": [0.1, 0.2], "303": [0.0]}


# -- profiles ---------------------------------------------------------------------


def profiles(feed=(), cash=None, market=(), squads=None):
    me, ana, bea = entry("T1", 101, "Me", 900), entry("T2", 202, "Ana", 1100), entry("T3", 303, "Bea", 800)
    squads = squads or {
        "T1": full_squad("me", [3, 4, 5, 6]),
        "T2": full_squad("ana", [2, 2], [sp("aHurt", DEF, 6, status="injured", season=6),
                                        sp("aFringe", DEF, 1, status="injured", season=1)]),
        "T3": full_squad("bea", [3, 3, 3, 3]),
    }
    estimates = CashEstimates(cash or {"202": -2 * M, "303": 8 * M}, 0, 0)
    return build_profiles([me, ana, bea], me, squads, list(feed), estimates, list(market), FORMATIONS, NOW,
                          values({"P1": 10 * M, "P2": 10 * M}))


def test_a_profile_shows_needs_injuries_habits_and_trouble():
    feed = [act(1, 202, 11 * M, "P1", days_ago=2), act(1, 202, 12 * M, "P2", days_ago=3),
            act(33, 202, 4 * M, days_ago=5), act(1, 202, 1 * M, days_ago=30),
            act(32, 202, 9 * M, days_ago=6, other=101), act(32, 202, 9 * M, days_ago=7, other=303)]
    listing = MarketItem.model_validate({"id": "MK", "discr": "marketPlayerTeam", "salePrice": 7 * M,
                                         "sellerTeam": {"id": "T2"},
                                         "playerMaster": {"id": "X", "nickname": "Listed"}})
    ana, bea = profiles(feed, market=[listing])
    assert (ana.name, ana.rank, ana.gap) == ("Ana", 1, 200)
    assert ana.needs == {GK: 0, DEF: 2, MID: 0, FWD: 0} and ana.need_text() == "short of DEF (2 fit)"
    assert ana.injured == ["aHurt"]  # a regular, not the fringe player
    assert ana.moves == 5 and ana.last_move == NOW - timedelta(days=2)
    assert ana.overpay == pytest.approx(0.15) and ana.likely_premium == pytest.approx(0.15)
    assert (ana.steals_made, ana.steals_from_you) == (2, 1)
    assert [i.id for i in ana.listings] == ["MK"]
    assert ana.in_red and ana.max_bid == -2 * M + 20 * M  # cash + 20% of team value
    assert bea.overpay is None and bea.likely_premium == DEFAULT_PREMIUM and not bea.in_red
    assert bea.last_move is None and bea.best_xi is not None


# -- bid competition -------------------------------------------------------------------


def contest(price: int, position: int, my_limit: int | None, **overrides):
    feed = [act(1, 202, 11 * M, "P1"), act(1, 202, 12 * M, "P2")]  # Ana: +15%, active
    rivals = profiles(feed, cash=overrides.get("cash", {"202": 20 * M, "303": 1 * M}))
    item = MarketItem.model_validate({"id": "M1", "discr": "marketPlayerLeague", "salePrice": price,
                                      "playerMaster": {"id": "T", "nickname": "Target"}})
    return competition(item, "Target", position, price, rivals, my_limit)


def test_likely_bidders_need_the_position_or_be_buying_and_be_able_to_pay():
    c = contest(10 * M, DEF, 30 * M)
    assert [b.rival.name for b in c.bidders] == ["Ana"]  # Bea's max bid (1M + 20M) covers it, but no need
    assert c.bidders[0].reasons == ["needs a DEF", "buying lately", "pays +15% usually"]
    assert c.to_win == 11_600_000  # her usual premium, plus 1%
    nobody = contest(10 * M, MID, 30 * M, cash={"202": -15 * M, "303": 1 * M})  # Ana can't reach 10M
    assert nobody.bidders == [] and nobody.to_win is None


def test_the_winning_bid_stops_at_what_rivals_can_pay_and_at_your_max():
    capped = contest(10 * M, DEF, 30 * M, cash={"202": -9_500_000, "303": 1 * M})  # Ana's max bid 10.5M
    assert capped.to_win == 10_510_000
    over = contest(10 * M, DEF, 11 * M)
    assert over.to_win is None and over.out_of_reach


# -- clause steals by swing -------------------------------------------------------------


def test_swing_counts_your_gain_and_the_owners_loss():
    star = sp("aStar", DEF, 7, clause=3 * M)
    squads = {"T1": full_squad("me", [3, 4, 5, 6]),
              "T2": full_squad("ana", [2, 2, 2], [star, sp("aBench", DEF, 1)]),  # ahead of you
              "T3": full_squad("bea", [2, 2, 2], [sp("bStar", DEF, 7, clause=3 * M), sp("bBench", DEF, 1)])}
    rivals_ = profiles(squads=squads)
    me_ana_bea = [entry("T2", 202, "Ana", 1100), entry("T3", 303, "Bea", 800)]
    rivals = [Rival(e, i + 1, squads[str(e.team.id)]) for i, e in enumerate(me_ana_bea)]
    swings = swing_targets(rivals, rivals_, squads["T1"], 10 * M, FORMATIONS, NOW)
    top = {s.target.player.name: s for s in swings}
    assert (top["aStar"].your_gain, top["aStar"].their_loss, top["aStar"].swing) == (4.0, 6.0, 10.0)
    assert top["bStar"].swing == 7.0  # Bea is behind you: her loss counts half
    assert swings[0].target.player.name == "aStar"


def test_the_gap_story_before_and_after_a_jornada():
    trend = {11: {"T1": 50, "T2": 60}}
    assert gap_story(trend, 11, "T1", "T2", 200) == (190, 200)


# -- in the briefing -----------------------------------------------------------------------


def weekly(points: dict[str, float]) -> list[dict]:
    return [{"team": {"id": t}, "points": p} for t, p in points.items()]


@pytest.fixture
def intel_league(fake):
    scout_league.build(fake)
    fake.data["ranking_by_week"] = {str(w): weekly({"T1": 50 + w, "T2": 60, "T3": 40, "T4": 45})
                                    for w in range(1, 12)}
    fake.data["current_lineup_by_id"] = {"T1": fake.data["current_lineup"]}  # rivals' are hidden
    return fake


async def test_the_briefing_shows_the_rivals_and_recaps_each_jornada_once(
        intel_league, store, settings, tokens, freeze, http, clock, alerter):
    from fantasybot.actions.core import suggest
    from fantasybot.api import FantasyClient
    from fantasybot.scout.briefing import briefing

    from .conftest import NOW as TOKEN_NOW
    from .conftest import seed

    async def no_sleep(_):
        return None

    text, view = await scout(intel_league, store, settings, tokens, freeze, http, clock, alerter,
                             external=None)
    weeks = [c.url.path.split("/")[-1] for c in intel_league.get_calls("league_ranking_by_week")]
    assert weeks == ["7", "8", "9", "10", "11"]
    assert "lineup" not in " ".join(view.errors)  # rivals' hidden lineups aren't an error
    assert "📊 Jornada 11 recap: You 61 · Ana 60 · Bea 40 · Carl 45" in text
    assert "Gap (their points minus yours): Ana +201 → +200" in text
    section = text.split("🕵️ Rivals")[1].split("\n\n")[0]
    assert "Last 3 jornadas, their points minus yours: Ana +" in section
    assert "• Ana: #1 (+200 vs you) · ≈15.00M" in section and "1 clause steal" in section
    assert "• Bea: #2 (+100 vs you) · ≈2.00M" in section and "selling Listed 10.00M" in section
    assert "Bidding on Isco" in section and "to beat them" in section

    # The bid button carries the amount that should beat the likely bidders.
    settings.actions = True
    bids = [s for s in suggest(view, settings, scout_league.NOW) if s.kind == "bid"]
    isco = next(s for s in bids if s.params["name"] == "Isco")
    to_win = view.intel.competition[next(k for k, c in view.intel.competition.items()
                                         if c.name == "Isco")].to_win
    assert to_win in isco.params["amounts"] and any(o.endswith("⚔") for o in isco.params["options"])
    steals = [s.params["name"] for s in suggest(view, settings, scout_league.NOW) if s.kind == "steal"]
    assert steals == [w.target.player.name for w in view.intel.swings][:3]

    # Sent once: the recap is gone, and only the last jornada's standing is fetched again.
    seed(store, id_exp=TOKEN_NOW + 3600)
    client = FantasyClient(settings, tokens, freeze, http, sleep=no_sleep, clock=lambda: 0.0)
    await briefing(client, settings, alerter, now=scout_league.NOW, send=True, sleep=no_sleep)
    messages, _ = await briefing(client, settings, alerter, now=scout_league.NOW, send=False,
                                 sleep=no_sleep)
    assert "recap" not in "\n".join(messages)
    weeks = [c.url.path.split("/")[-1] for c in intel_league.get_calls("league_ranking_by_week")]
    assert weeks[5:] == ["11", "11"]


def test_history_finds_a_value_near_a_day(tmp_path):
    from fantasybot.scout.history import PriceHistory

    def p(value):
        return Player.model_validate({"id": "P", "nickname": "P", "marketValue": value})

    with PriceHistory(tmp_path / "h.sqlite") as h:
        h.record(date(2026, 11, 1), [p(10 * M)])
        h.record(date(2026, 11, 6), [p(12 * M)])
        assert h.value_near("P", date(2026, 11, 4)) == 10 * M  # latest up to 3 days before
        assert h.value_near("P", date(2026, 11, 5)) == 12 * M  # nothing before in range: a day after
        assert h.value_near("P", date(2026, 11, 20)) is None
        assert h.value_near("other", date(2026, 11, 3)) is None
