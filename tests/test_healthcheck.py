from __future__ import annotations

import json
import stat

import httpx
import pytest

from fantasybot.fixtures import Anonymiser, FixtureRecorder
from fantasybot.healthcheck import HealthCheck, run_and_alert

from .conftest import NOW, b2c_ok, seed

TOKEN_URL = "https://login.laliga.es/"

ALL_CHECKS = [
    "auth_refresh", "current_user", "leagues", "current_week", "league_ranking",
    "league_ranking_by_week", "league_activity", "team_data", "team_money", "current_lineup",
    "team_lineup", "free_formations", "premium_formations", "premium_configuration", "market",
    "players", "teams_master", "matchday", "match_stats", "player_offer", "player_details",
    "check_player_shield",
]


@pytest.fixture
def health(settings, tokens, client, clock):
    return HealthCheck(settings, tokens, client, clock=clock)


@pytest.fixture
def b2c(router):
    router.add("POST", TOKEN_URL, b2c_ok(NOW + 3600, refresh_token_expires_in=14 * 86400))


def by_name(report):
    return {r.name: r for r in report.results}


async def test_healthy_api_sends_no_alert(health, alerter, fake, b2c):
    fake.data["market"][1]["numberOfOffers"] = 1  # one of my listings has an offer
    report = await run_and_alert(health, alerter)

    assert [r.name for r in report.checks] == ALL_CHECKS
    assert [r.name for r in report.results if r.status != "ok"] == []
    assert report.healthy
    assert alerter.messages == []
    # IDs were discovered, not hard-coded: my team, my player, last week's standing.
    assert fake.get_calls("team_data")[0].url.path.endswith("/leagues/L1/teams/T1")
    assert fake.get_calls("player_details")[0].url.path.endswith("/player/P1/league/L1")
    assert fake.get_calls("league_ranking_by_week")[0].url.path.endswith("/standing/11")
    assert fake.get_calls("player_offer")[0].url.path.endswith("/playerTeam/PT1/offer")


async def test_offers_are_skipped_when_there_are_none(health, alerter, b2c):
    report = await run_and_alert(health, alerter)
    assert by_name(report)["player_offer"].status == "skip"
    assert report.healthy and alerter.messages == []


async def test_a_broken_endpoint_is_reported_and_the_rest_still_run(health, alerter, fake, b2c):
    fake.overrides["team_data"] = httpx.Response(500, json={"message": "Internal error"})
    report = await run_and_alert(health, alerter)

    results = by_name(report)
    assert results["team_data"].status == "fail"
    assert results["team_data"].detail == "HTTP 500: Internal error"
    assert results["player_details"].status == "skip"  # needed a player from team_data
    assert results["check_player_shield"].status == "skip"
    assert results["market"].status == "ok"  # independent checks still ran
    assert not report.healthy

    (alert,) = alerter.messages
    assert alert.startswith("🔴 FantasyBot health check: 1 of 22 checks failed")
    assert "✗ team_data: HTTP 500: Internal error" in alert
    assert "player_details" in alert and "check_player_shield" in alert


async def test_a_shape_change_is_reported_with_the_field(health, alerter, fake, b2c):
    for item in fake.data["market"]:
        del item["playerMaster"]
    report = await run_and_alert(health, alerter)
    detail = by_name(report)["market"].detail
    assert detail.startswith("unexpected shape: 0.playerMaster: Field required")
    assert "playerMaster" in alerter.messages[0]


async def test_dead_refresh_token_fails_auth_but_reads_continue(health, alerter, router, freeze):
    router.add("POST", TOKEN_URL, httpx.Response(400, json={"error": "invalid_grant"}))
    report = await run_and_alert(health, alerter)
    results = by_name(report)
    assert results["auth_refresh"].status == "fail"
    assert results["current_user"].status == "ok"  # current id_token still valid
    assert freeze.is_frozen()
    assert len(alerter.messages) == 2  # the freeze alert, then the health report
    assert "auth_refresh" in alerter.messages[1]


async def test_no_token_skips_everything(settings, tokens, freeze, http, clock, alerter):
    from fantasybot.api import FantasyClient

    client = FantasyClient(settings, tokens, freeze, http)
    report = await run_and_alert(HealthCheck(settings, tokens, client, clock=clock), alerter)
    results = by_name(report)
    assert results["auth_refresh"].status == "fail"
    assert "auth import" in results["auth_refresh"].detail
    assert {r.status for r in report.results[1:]} == {"skip"}


async def test_refresh_token_close_to_expiry_warns(health, alerter, router):
    router.add("POST", TOKEN_URL, b2c_ok(NOW + 3600, refresh_token_expires_in=86400))
    report = await run_and_alert(health, alerter)
    assert report.healthy
    assert [w.name for w in report.warnings] == ["refresh_token_expiry"]
    (alert,) = alerter.messages
    assert alert.startswith("🟡") and "auth import" in alert


async def test_configured_league_must_exist(health, settings, b2c):
    settings.league_id = "NOPE"
    report = await health.run()
    assert by_name(report)["leagues"].detail == "configured league NOPE is not on this account"
    assert by_name(report)["market"].status == "skip"


# -- fixtures ----------------------------------------------------------------


def test_anonymiser_scrubs_personal_data_and_keeps_shapes():
    anon = Anonymiser()
    out = anon.scrub({
        "id": 101, "managerName": "Pal Friend", "email": "pal@example.com",
        "avatar": "https://cdn.example/pal.png", "teamValue": 91_000_000,
        "createdAt": "2026-11-01T10:00:00+01:00", "discr": "marketPlayerTeam",
        "team": {"id": "T2", "manager": {"id": 101}}, "playerTeamId": "PT9", "isShielded": True,
    })
    assert out["id"] == 1 and out["team"]["manager"]["id"] == 1  # same real ID, same fake
    assert out["team"]["id"] == "id2" and out["playerTeamId"] == "id3"
    assert out["managerName"] == "xxxxxxxxxx" and out["email"] == "xxxxxxxxxxxx"
    assert out["avatar"] == "https://example.invalid/redacted"
    assert out["teamValue"] == 91_000_000 and out["isShielded"] is True
    assert out["createdAt"] == "2026-11-01T10:00:00+01:00" and out["discr"] == "marketPlayerTeam"


async def test_saved_fixtures_hold_no_personal_data(settings, tokens, freeze, http, store, clock,
                                                    tmp_path, b2c):
    from fantasybot.api import FantasyClient

    seed(store, id_exp=NOW + 3600)
    recorder = FixtureRecorder()
    client = FantasyClient(settings, tokens, freeze, http, on_response=recorder)
    await HealthCheck(settings, tokens, client, clock=clock).run()
    written = recorder.write(tmp_path / "fixtures")

    assert len(written) >= 19
    blob = "".join(p.read_text() for p in written)
    for secret in ("Me Myself", "Pal Friend", "me@example.com", "Friends League", "assets.example"):
        assert secret not in blob
    assert stat.S_IMODE((tmp_path / "fixtures").stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in written)
    # Relationships survive: my team's fake ID in the standing is the one in the market listing.
    ranking = json.loads((tmp_path / "fixtures" / "league_ranking.json").read_text())
    market = json.loads((tmp_path / "fixtures" / "market.json").read_text())
    assert ranking[0]["team"]["id"] == market[1]["playerTeam"]["teamId"]


# -- the CLI command ---------------------------------------------------------


def test_cli_healthcheck_exit_codes(tmp_path, monkeypatch, capsys, router, fake):
    import functools

    from cryptography.fernet import Fernet

    import fantasybot.cli as cli
    from fantasybot.auth import TokenStore
    from fantasybot.cli import main
    from fantasybot.runtime import open_runtime

    key = Fernet.generate_key()
    monkeypatch.setenv("FANTASYBOT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("FANTASYBOT_TOKEN_KEY", key.decode())
    monkeypatch.setenv("FANTASYBOT_REQUEST_DELAY", "0")
    seed(TokenStore(tmp_path / "tokens.enc", key), id_exp=2_000_000_000)
    router.add("POST", TOKEN_URL, b2c_ok(2_000_000_000))
    monkeypatch.setattr(cli, "open_runtime",
                        functools.partial(open_runtime, transport=httpx.MockTransport(router)))

    assert main(["healthcheck", "--no-alert"]) == 0
    assert "checks passed" in capsys.readouterr().out

    fake.overrides["market"] = httpx.Response(503)
    assert main(["healthcheck", "--no-alert"]) == 1
    assert "FAIL  market" in capsys.readouterr().out
