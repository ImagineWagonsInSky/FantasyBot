from __future__ import annotations

import json

import httpx
import pytest

from fantasybot.api import ApiError, FantasyClient, LineupUpdate, ResponseShapeError
from fantasybot.api.client import PORTED_FROM_API_JS
from fantasybot.auth import WritesFrozen

from .conftest import NOW, b2c_ok
from .fakeapi import API, LEAGUE, MY_TEAM, STATS

C1 = f"{API}/v1/competition/1"

READS = [
    ("get_current_user", (), f"{API}/v4/user/me?x-lang=es"),
    ("get_leagues", (), f"{C1}/leagues?x-lang=es"),
    ("get_league_ranking", (LEAGUE,), f"{C1}/leagues/L1/standing?x-lang=es"),
    ("get_league_ranking_by_week", (LEAGUE, 12), f"{C1}/leagues/L1/standing/12?x-lang=es"),
    ("get_league_activity", (LEAGUE, 0), f"{C1}/leagues/L1/activity/0?x-lang=es"),
    ("get_team_data", (LEAGUE, MY_TEAM), f"{C1}/leagues/L1/teams/T1?x-lang=es"),
    ("get_team_lineup", (MY_TEAM, 12), f"{C1}/teams/T1/lineup/week/12?x-lang=es"),
    ("get_current_lineup", (MY_TEAM,), f"{C1}/teams/T1/lineup?x-lang=es"),
    ("get_free_formations", (), f"{API}/v4/teams/lineup/formations?option=free&x-lang=es"),
    ("get_premium_formations", (), f"{API}/v4/teams/lineup/formations?option=premium&x-lang=es"),
    ("get_premium_configuration", (), f"{API}/v4/leagues/premium-configuration?x-lang=es"),
    ("get_market", (LEAGUE,), f"{C1}/league/L1/market?x-lang=es"),
    ("get_all_players", (), f"{C1}/players?x-lang=es"),
    ("get_matchday", (12,), f"{C1}/calendar?weekNumber=12&x-lang=es"),
    ("get_current_week", (), f"{C1}/week/current?x-lang=es"),
    ("get_match_stats", (12,), f"{STATS}/v1/competition/1/stats/week/12?x-lang=es"),
    ("get_team_money", (MY_TEAM,), f"{C1}/teams/T1/money?x-lang=es"),
    ("get_player_offer", (LEAGUE, "PT1"), f"{C1}/league/L1/playerTeam/PT1/offer?x-lang=es"),
    ("get_player_details", ("P1", LEAGUE), f"{C1}/player/P1/league/L1?x-lang=es"),
    ("check_player_shield", (LEAGUE, "PT1"), f"{C1}/league/L1/player-team/PT1/check-shield?x-lang=es"),
    ("get_teams_master", (), f"{API}/v3/teams-master?x-lang=es"),
]

LINEUP = LineupUpdate(goalkeeper="PT2", defender=["PT3", "PT4"], midfield=["PT1"], striker=[],
                      tactical_formation=[4, 4, 2])

WRITES = [
    ("update_lineup", (MY_TEAM, LINEUP), "PUT", f"{C1}/teams/T1/lineup",
     {"goalkeeper": "PT2", "defender": ["PT3", "PT4"], "midfield": ["PT1"], "striker": [],
      "tactical_formation": [4, 4, 2]}),
    ("make_bid", (LEAGUE, "M1", 1_500_000), "POST", f"{C1}/league/L1/market/M1/bid",
     {"money": 1_500_000}),
    ("cancel_bid", (LEAGUE, "M1", "B1"), "DELETE", f"{C1}/league/L1/market/M1/bid/B1/cancel", None),
    ("modify_bid", (LEAGUE, "M1", "B1", 2_000_000), "PUT", f"{C1}/league/L1/market/M1/bid/B1",
     {"money": 2_000_000}),
    ("accept_offer", (LEAGUE, "M2", "O1", 13_000_000), "POST",
     f"{C1}/league/L1/market/M2/offer/O1/accept", {"offerMoney": 13_000_000}),
    ("decline_offer", (LEAGUE, "M2", "O1"), "POST", f"{C1}/league/L1/market/M2/offer/O1/reject", None),
    ("increase_buyout_clause", (LEAGUE, "P1", 1, 1_000_000), "PUT", f"{C1}/league/L1/buyout/player",
     {"factor": 1, "playerId": "P1", "valueToIncrease": 1_000_000}),
    ("pay_buyout_clause", (LEAGUE, 4242, 30_000_000), "POST", f"{C1}/league/L1/buyout/4242/pay",
     {"buyoutClauseToPay": 30_000_000}),
    ("sell_player_to_market", (LEAGUE, "P1", 50_000_000), "POST", f"{C1}/league/L1/market/sell",
     {"playerId": "P1", "salePrice": 50_000_000}),
    ("withdraw_player_from_market", (LEAGUE, "M2"), "DELETE", f"{C1}/league/L1/market/M2/delete", None),
    ("make_direct_offer", (LEAGUE, 77, 20_000_000), "POST", f"{C1}/league/L1/market/direct-offer",
     {"playerId": 77, "money": 20_000_000}),
    ("cancel_offer", (LEAGUE, "M3", "O7"), "DELETE", f"{C1}/league/L1/market/M3/offer/O7/cancel", None),
]


def test_every_api_js_function_is_accounted_for():
    assert len(PORTED_FROM_API_JS) == 34
    ported = {m for m in PORTED_FROM_API_JS.values() if m}
    assert {name for name, _, _ in READS} - {"get_teams_master"} | {name for name, *_ in WRITES} == ported
    for method in ported:
        assert callable(getattr(FantasyClient, method))
    assert [k for k, v in PORTED_FROM_API_JS.items() if v is None] == ["scrapeTeamLineup", "shieldPlayer"]


@pytest.mark.parametrize(("method", "args", "url"), READS, ids=[r[0] for r in READS])
async def test_read_hits_the_same_url_as_laligaapp(client, store, fake, method, args, url):
    await getattr(client, method)(*args)
    first = next(c for c in fake.calls if c.url.path != "/api/v3/teams-master"
                 or method == "get_teams_master")
    assert first.method == "GET"
    assert str(first.url) == url
    assert first.headers["authorization"] == f"Bearer {store.load().id_token}"
    assert first.headers["x-app"] == "2"
    assert first.headers["x-lang"] == "es"
    assert first.headers["user-agent"].startswith("Mozilla/5.0")
    assert "content-type" not in first.headers


@pytest.mark.parametrize(("method", "args", "http_method", "url", "body"), WRITES,
                         ids=[w[0] for w in WRITES])
async def test_write_sends_the_same_request_as_laligaapp(client, settings, fake, method, args,
                                                         http_method, url, body):
    settings.dry_run = False
    result = await getattr(client, method)(*args)
    (sent,) = fake.writes
    assert sent.method == http_method
    assert str(sent.url) == url + "?x-lang=es"
    if body is None:
        assert sent.content == b""
        assert "content-type" not in sent.headers
    else:
        assert json.loads(sent.content) == body
        assert sent.headers["content-type"] == "application/json"
    assert not result.dry_run and result.status_code == 200


@pytest.mark.parametrize(("method", "args", "http_method", "url", "body"), WRITES,
                         ids=[w[0] for w in WRITES])
async def test_dry_run_is_the_default_and_sends_nothing(client, fake, method, args, http_method,
                                                        url, body):
    result = await getattr(client, method)(*args)
    assert fake.writes == []
    assert result.dry_run
    assert (result.method, result.url, result.body) == (http_method, url, body)


async def test_frozen_writes_are_refused(client, settings, freeze, fake):
    settings.dry_run = False
    freeze.freeze("invalid_grant", "refresh token expired")
    with pytest.raises(WritesFrozen, match="refresh token expired"):
        await client.make_bid(LEAGUE, "M1", 1_000_000)
    assert fake.writes == []
    assert (await client.get_market(LEAGUE))[0].id == "M1"  # reads still work


async def test_failed_writes_are_not_retried(client, settings, fake):
    settings.dry_run = False
    fake.write_response = httpx.Response(503, json={"message": "busy"})
    with pytest.raises(ApiError, match="HTTP 503: busy"):
        await client.make_bid(LEAGUE, "M1", 1_000_000)
    assert len(fake.writes) == 1


async def test_401_refreshes_once_and_retries(client, fake, router, store):
    old = store.load().id_token
    fake.overrides["current_user"] = httpx.Response(401)
    router.add("POST", "https://login.laliga.es/", b2c_ok(NOW + 7200))

    with pytest.raises(ApiError) as excinfo:
        await client.get_current_user()
    assert excinfo.value.status == 401
    calls = fake.get_calls("current_user")
    assert len(calls) == 2  # one retry, not a loop
    assert calls[0].headers["authorization"] == f"Bearer {old}"
    assert calls[1].headers["authorization"] == f"Bearer {store.load().id_token}" != f"Bearer {old}"


async def test_reads_retry_429_and_5xx(client, fake, sleeps):
    responses = iter([httpx.Response(429, headers={"retry-after": "7"}), httpx.Response(502)])

    def flaky(request):
        try:
            return next(responses)
        except StopIteration:
            return httpx.Response(200, json={"weekNumber": 3})

    original = fake.__call__
    fake_handler = lambda r: flaky(r) if fake.endpoint_for(r) == "current_week" else original(r)  # noqa: E731
    client._http._transport = httpx.MockTransport(fake_handler)
    assert (await client.get_current_week()).number == 3
    assert sleeps == [7.0, 2.0]


async def test_competition_id_comes_from_config(settings, tokens, freeze, http, store, fake):
    from .conftest import seed

    settings.competition_id = 2
    seed(store, id_exp=NOW + 3600)
    await FantasyClient(settings, tokens, freeze, http).get_market(LEAGUE)
    assert fake.calls[0].url.path == "/api/v1/competition/2/league/L1/market"


async def test_unsafe_ids_never_reach_the_url(client, fake):
    with pytest.raises(ValueError, match="unsafe"):
        await client.get_team_data(LEAGUE, "../../v4/user/me")
    assert fake.calls == []


# -- shapes and adapters -----------------------------------------------------


async def test_players_are_enriched_from_teams_master(client, fake):
    fake.data["players"][1]["image"] = "https://assets.example/p2.png"
    players = await client.get_all_players()
    assert players[0].team.name == "FC Barcelona"
    assert players[0].name == "Pedri"
    assert players[0].images == {"transparent": {"256x256": "https://assets.example/p1.png"}}
    assert players[1].images["player"] == "https://assets.example/p2.png"


async def test_calendar_ids_are_resolved_to_teams(client):
    (match,) = await client.get_matchday(12)
    assert match.local.name == "FC Barcelona" and match.visitor.name == "Real Betis"


async def test_standing_is_normalised_like_laligaapp(client):
    first = (await client.get_league_ranking(LEAGUE))[0]
    assert first.points == 120 and first.team.points == 120
    assert first.userId == 101 and first.manager == "Me Myself"


async def test_wrapped_lists_are_unwrapped(client):
    assert [lg.id for lg in await client.get_leagues()] == [LEAGUE]
    assert len(await client.get_league_activity(LEAGUE)) == 1


@pytest.mark.parametrize("body", [2_500_000, {"teamMoney": 2_500_000}, {"money": 2_500_000}])
async def test_team_money_accepts_every_known_shape(client, fake, body):
    fake.data["team_money"] = body
    assert (await client.get_team_money(MY_TEAM)).amount == 2_500_000


async def test_shield_400_means_not_shieldable(client, fake):
    fake.overrides["check_player_shield"] = httpx.Response(400, json={"message": "Ya está blindado"})
    check = await client.check_player_shield(LEAGUE, "PT1")
    assert not check.shieldable and "Ya está blindado" in check.message


async def test_missing_required_field_is_a_shape_error_without_values(client, fake):
    fake.data["market"] = [{"salePrice": 1, "playerMaster": {"id": "P2", "nickname": "Secret Name"}}]
    with pytest.raises(ResponseShapeError) as excinfo:
        await client.get_market(LEAGUE)
    assert "0.id: Field required" in str(excinfo.value)
    assert "Secret Name" not in str(excinfo.value)


async def test_unexpected_envelope_is_a_shape_error(client, fake):
    fake.data["market"] = {"result": "maintenance"}
    with pytest.raises(ResponseShapeError, match="expected a list"):
        await client.get_market(LEAGUE)


async def test_non_json_body_is_an_api_error(client, fake):
    fake.overrides["current_week"] = httpx.Response(200, text="<html>maintenance</html>")
    with pytest.raises(ApiError, match="non-JSON"):
        await client.get_current_week()


async def test_teams_master_failure_degrades_gracefully(client, fake):
    fake.overrides["teams_master"] = httpx.Response(500)
    players = await client.get_all_players()
    assert players[0].team.id == "3" and players[0].team.name is None


async def test_player_details_are_wrapped_in_player_master(client, fake):
    details = await client.get_player_details("P1", LEAGUE)
    assert details.playerMaster.nickname == "Pedri" and details.seasons == []

    fake.data["player_details"] = {"id": "P1", "nickname": "Pedri"}  # flat shape still accepted
    assert (await client.get_player_details("P1", LEAGUE)).playerMaster.id == "P1"

    fake.data["player_details"] = {"seasons": []}  # neither shape: a real breakage
    with pytest.raises(ResponseShapeError, match="playerMaster: Field required"):
        await client.get_player_details("P1", LEAGUE)
