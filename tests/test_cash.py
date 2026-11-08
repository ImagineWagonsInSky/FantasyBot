from __future__ import annotations

from fantasybot.api.models import ActivityEntry
from fantasybot.scout.cash import estimate, feed_summary, ledgers, net_by_manager

M = 1_000_000


def entry(type_id: int, actor: int | None, amount: int, other: int | None = None, text: str = "",
          player: str | None = None):
    return ActivityEntry(activityTypeId=type_id, user1Id=actor, user2Id=other, amount=amount,
                         description=text or None, playerMasterId=player)


def test_feed_totals_ignore_jornada_payouts():
    net = net_by_manager([
        entry(1, 1, 10), entry(31, 1, 5), entry(33, 1, 4), entry(6, 1, 2),  # 1: -10 -5 +4 (payout ignored)
        entry(32, 2, 7, other=3),  # 2 steals from 3: 3 is paid the clause
        entry(1, 2, 3, other=3, text="Ana ha clausulado a X"),  # older feeds: type 1 + "clausul"
        entry(4, 3, 0), entry(9, 4, 100),  # shield (no money) and joining: ignored
    ])
    assert net == {"1": -11, "2": -10, "3": 10}


def test_a_sale_between_managers_credits_the_seller_once():
    buy_only = ledgers([entry(1, 1, 5 * M, other=2, player="P")])
    assert buy_only["2"].sold == 5 * M  # only the buyer's side was in the feed
    both_sides = ledgers([entry(1, 1, 5 * M, other=2, player="P"), entry(33, 2, 5 * M, player="P")])
    assert both_sides["2"].sold == 5 * M  # the seller's own entry is not counted twice


def test_estimate_uses_points_for_jornada_earnings():
    # Everyone started with 100M. You: 90 pts, spent 186M net, have 4M.
    feed = [entry(1, 1, 189 * M), entry(33, 1, 3 * M), entry(1, 2, 198 * M), entry(33, 2, 3 * M)]
    est = estimate(feed, my_manager_id=1, my_cash=4 * M, points={1: 900, 2: 1100, 3: 800},
                   team_values={2: 120 * M, 3: 90 * M})
    assert est.unexplained == 100 * M  # 4M - (-186M) - 90M from points
    assert est.by_manager == {"2": 15 * M, "3": 180 * M}  # 3 never traded: 100M + 80M
    assert est.usable and est.get(2) == 15 * M and est.get(1) is None


def test_an_estimate_past_the_debt_limit_is_rejected():
    feed = [entry(1, 2, 400 * M)]
    est = estimate(feed, 1, 4 * M, points={1: 900, 2: 1100}, team_values={2: 120 * M})
    assert est.implausible == ["2"] and not est.usable
    assert est.get(2) is None  # never shown or used


def test_no_cash_means_no_estimate_but_an_empty_feed_still_works():
    assert estimate([entry(1, 1, 6)], 1, None, {1: 0}) is None
    est = estimate([], 1, 10 * M, points={1: 0, 2: 50})
    assert est.by_manager == {"2": 15 * M}


def test_feed_summary_for_cash_check():
    summary = feed_summary([entry(1, 1, 5), entry(6, None, 2), entry(77, 1, 1)])
    assert summary["types"] == {1: 1, 6: 1, 77: 1}
    assert summary["without_manager"] == {6: 1}


def test_cash_check_prints_the_breakdown(tmp_path, monkeypatch, capsys, fake, router):
    import functools

    import httpx
    from cryptography.fernet import Fernet

    import fantasybot.cli as cli
    from fantasybot.auth import TokenStore
    from fantasybot.runtime import open_runtime

    from . import scout_league
    from .conftest import seed

    scout_league.build(fake)
    key = Fernet.generate_key()
    monkeypatch.setenv("FANTASYBOT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("FANTASYBOT_TOKEN_KEY", key.decode())
    monkeypatch.setenv("FANTASYBOT_REQUEST_DELAY", "0")
    seed(TokenStore(tmp_path / "tokens.enc", key), id_exp=2_000_000_000)
    monkeypatch.setattr(cli, "open_runtime",
                        functools.partial(open_runtime, transport=httpx.MockTransport(router)))
    assert cli.main(["cash-check"]) == 0
    out = capsys.readouterr().out
    assert "Types: 1 bought ×5, 6 jornada payout (ignored) ×2, 32 clause steal ×1, 33 sold ×2" in out
    assert "Unexplained part of your cash: 100.00M" in out
    rows = [line for line in out.splitlines() if line[:4] in ("You ", "Ana ", "Bea ", "Carl")]
    lines = {line.split()[0]: line for line in rows}
    assert lines["Carl"].split()[-1] == "21.00M"
    assert "5.00M" in lines["Carl"]  # credited for selling to Bea
    assert lines["You"].endswith("4.00M (real)")


async def test_impossible_estimates_are_not_shown(fake, store, settings, tokens, freeze, http, clock,
                                                   alerter):
    from fantasybot.api import FantasyClient
    from fantasybot.scout.briefing import run_briefing

    from . import scout_league
    from .conftest import NOW as TOKEN_NOW
    from .conftest import seed

    async def no_sleep(_):
        return None

    scout_league.build(fake)
    seed(store, id_exp=TOKEN_NOW + 3600)
    clock.now = scout_league.NOW.timestamp() - 60
    fake.data["league_activity_pages"][1]["elements"].append(
        {"id": "BIG", "activityTypeId": 1, "user1Id": 404, "amount": 300_000_000,
         "createdAt": "2026-10-01T10:00:00+02:00"})
    client = FantasyClient(settings, tokens, freeze, http, sleep=no_sleep, clock=lambda: 0.0)
    text = "\n".join(await run_briefing(client, settings, alerter, now=scout_league.NOW, send=False,
                                        sleep=no_sleep))
    assert "Rivals' cash: the estimate doesn't add up today" in text
    assert "Rivals' cash ≈" not in text and "within reach of" not in text
