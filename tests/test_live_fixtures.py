"""Real, anonymised API responses captured from a live league (./fb fixtures)."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from fantasybot.api.models import MarketItem, PlayerDetails, same_id

LIVE = Path(__file__).parent / "fixtures" / "live"


def load(name: str):
    return json.loads((LIVE / f"{name}.json").read_text())


def test_player_details_parse_with_weekly_points_and_clause():
    details = PlayerDetails.model_validate(load("player_details"))
    player = details.playerMaster
    assert player.position == "Portero" and player.playerStatus == "ok"
    week1 = player.playerStats[0]
    assert week1.weekNumber == 1 and week1.minutes == 90
    # Each stat is [raw, points]; the points add up to the jornada total.
    assert sum(points for _, points in week1.stats.values()) == week1.totalPoints
    assert details.playerTeam.buyoutClause == player.marketValue
    assert details.playerTeam.isShielded is False
    assert details.manager is not None


def test_market_parses_league_and_manager_listings():
    items = TypeAdapter(list[MarketItem]).validate_python(load("market"))
    league = [i for i in items if i.discr == "marketPlayerLeague"]
    listed = [i for i in items if i.discr == "marketPlayerTeam"]
    assert len(league) == 13 and len(listed) == 1
    assert {i.playerMaster.position for i in items} >= {"Entrenador", "Delantero", "Defensa"}
    mine = listed[0]
    assert mine.playerTeam.buyoutClause == 10177264 and mine.numberOfOffers == 1
    assert same_id(mine.seller_team_id, "id80")
    assert mine.sellerTeam["teamMoney"] is None  # rivals' cash is hidden
