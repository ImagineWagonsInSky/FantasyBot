from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from fantasybot.actions.flow import ActionCenter, listen
from fantasybot.actions.store import ActionStore
from fantasybot.scout.briefing import gather
from fantasybot.telegram import TelegramBot, TelegramError

from . import scout_league
from .conftest import NOW as TOKEN_NOW
from .conftest import seed
from .scout_league import NOW

BOT_TOKEN = "123456789:AAH-abcdefghijklmnopqrstuvwxyz012345"
CHAT = 4242
C1 = "/api/v1/competition/1"


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.pending: list[dict] = []
        self._id = 100

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if method == "sendMessage":
            self._id += 1
            self.sent.append({**body, "message_id": self._id})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self._id}})
        if method == "getUpdates":
            updates, self.pending = self.pending, []
            return httpx.Response(200, json={"ok": True, "result": updates})
        return httpx.Response(200, json={"ok": True, "result": True})

    @property
    def text(self) -> str:
        return self.sent[-1]["text"]

    def buttons(self, index: int = -1) -> dict[str, str]:
        rows = self.sent[index].get("reply_markup", {}).get("inline_keyboard", [])
        return {b["text"]: b["callback_data"] for row in rows for b in row}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now


@pytest.fixture
def tg(router) -> FakeTelegram:
    fake = FakeTelegram()
    router.add("POST", "https://api.telegram.org/", fake)
    return fake


@pytest.fixture
def act(settings, fake, store, tokens, freeze, http, clock, tg, tmp_path):
    from fantasybot.api import FantasyClient

    async def no_sleep(_):
        return None

    scout_league.build(fake)
    seed(store, id_exp=TOKEN_NOW + 3600)
    clock.now = NOW.timestamp() - 60
    settings.actions = True
    settings.telegram_chat_id = str(CHAT)
    client = FantasyClient(settings, tokens, freeze, http, sleep=no_sleep, clock=lambda: 0.0)
    bot = TelegramBot(http, BOT_TOKEN)
    actions = ActionStore(tmp_path / "state" / "actions.sqlite")
    when = Clock()
    center = ActionCenter(settings, client, bot, actions, clock=when)

    class Kit:
        pass

    kit = Kit()
    kit.center, kit.bot, kit.store, kit.clock, kit.client = center, bot, actions, when, client
    kit.settings, kit.fake, kit.tg = settings, fake, tg

    async def offer():
        view = await gather(client, settings, NOW, sleep=no_sleep)
        return await center.offer(view)

    async def tap(label_or_data: str, chat: int = CHAT, user: int = CHAT, kind: str = "private",
                  index: int = -1):
        data = tg.buttons(index).get(label_or_data, label_or_data)
        await center.handle({"update_id": 1, "callback_query": {
            "id": "cb1", "data": data, "from": {"id": user},
            "message": {"message_id": 1, "chat": {"id": chat, "type": kind}}}})

    kit.offer, kit.tap = offer, tap
    yield kit
    actions.close()


def labels(tg: FakeTelegram, index: int = -1) -> list[str]:
    return list(tg.buttons(index))


# -- what gets offered -----------------------------------------------------------


async def test_the_briefing_offers_buttons_in_test_mode(act):
    assert await act.offer() > 0
    offered = labels(act.tg)
    assert "🔁 Apply best XI 4-3-3 (+10.0 xP)" in offered
    assert "🎯 Steal Star · 3.00M" in offered and "🎯 Steal Cheap · 2.00M" in offered
    assert {"🛒 Isco 1.20M", "1.26M", "1.32M"} <= set(offered)
    assert not any("Listed" in label for label in offered)  # a manager's listing: needs Premium
    # Raise: Carl (≈21M) could take Fwd A (20M): +2M puts him out of reach; or +25%.
    assert "🛡️ Fwd A +2.00M (pay 1.00M)" in offered and "🛡️ Fwd A +5.00M (pay 2.50M)" in offered
    assert "Test mode" in act.tg.text and act.fake.writes == []


async def test_nothing_is_offered_when_actions_are_off(act):
    act.settings.actions = False
    assert await act.offer() == 0
    assert act.tg.sent == []


# -- the dry-run flow ------------------------------------------------------------


async def test_dry_run_steal_shows_the_request_and_sends_nothing(act):
    await act.offer()
    await act.tap("🎯 Steal Star · 3.00M")
    assert "Pay 3.00M to take Star from Ana.\nCash 4.00M → 1.00M." in act.tg.text
    assert "Test mode" in act.tg.text and "works once" in act.tg.text
    confirm = act.tg.buttons()["✅ Confirm"]
    await act.tap(confirm)
    assert act.tg.text == ("🧪 Test only, nothing was sent. FantasyBot would have sent:\n"
                           "POST /v1/competition/1/league/L1/buyout/PT60/pay\n"
                           '{"buyoutClauseToPay": 3000000}')
    assert act.fake.writes == []
    assert [e["outcome"] for e in act.store.executions()] == ["simulated"]
    await act.tap(confirm)
    assert act.tg.text == "✋ That button was already used."


async def test_confirmations_expire_and_can_be_cancelled(act):
    await act.offer()
    await act.tap("🎯 Steal Star · 3.00M")
    confirm = act.tg.buttons()["✅ Confirm"]
    act.clock.now = NOW + timedelta(minutes=3)
    await act.tap(confirm)
    assert "expired" in act.tg.text
    act.clock.now = NOW
    await act.tap("🎯 Steal Star · 3.00M", index=0)
    await act.tap(act.tg.buttons()["✖ Cancel"])
    assert act.tg.text == "Cancelled; nothing was done."
    assert act.store.executions() == []


@pytest.mark.parametrize(("chat", "user", "kind"), [(999, 999, "private"), (CHAT, 999, "private"),
                                                    (CHAT, CHAT, "group")])
async def test_only_your_private_chat_is_obeyed(act, chat, user, kind):
    await act.offer()
    sent = len(act.tg.sent)
    await act.tap("🎯 Steal Star · 3.00M", chat=chat, user=user, kind=kind)
    assert len(act.tg.sent) == sent  # no reply at all
    assert not any(m == "answerCallbackQuery" for m, _ in act.tg.calls)


async def test_bid_lineup_and_raise_requests(act):
    act.settings.premium = True  # bidding on a manager's listing
    await act.offer()
    await act.tap("🛒 Listed 10.00M")
    assert "more than your cash (4.00M)" in act.tg.text and "score 0 points" in act.tg.text
    await act.tap(act.tg.buttons()["✅ Confirm"])
    assert f"POST {C1.removeprefix('/api')}/league/L1/market/M5/bid\n" + '{"money": 10000000}' in act.tg.text

    await act.tap("🔁 Apply best XI 4-3-3 (+10.0 xP)", index=0)
    assert "Save lineup 4-3-3: GK Keeper · DEF Def D, Def B, Def A, Def C" in act.tg.text
    await act.tap(act.tg.buttons()["✅ Confirm"])
    body = json.loads(act.tg.text.splitlines()[-1])
    assert body == {"goalkeeper": "PT10", "defender": ["PT24", "PT22", "PT21", "PT23"],
                    "midfield": ["PT30", "PT31", "PT32"], "striker": ["PT40", "PT41", "PT42"],
                    "tactical_formation": [4, 3, 3]}

    await act.tap("🛡️ Fwd A +2.00M (pay 1.00M)", index=0)
    assert "Raise Fwd A's clause from 20.00M to 22.00M, for 1.00M." in act.tg.text
    await act.tap(act.tg.buttons()["✅ Confirm"])
    assert '{"factor": 2.0, "playerId": "PT40", "valueToIncrease": 2000000}' in act.tg.text
    assert act.fake.writes == []


# -- refusals --------------------------------------------------------------------


async def test_changes_since_the_briefing_are_shown_or_refused(act):
    await act.offer()
    star = next(p for p in act.fake.data["team_data_by_id"]["T2"]["players"] if p["playerTeamId"] == "PT60")
    star["buyoutClause"] = 3_500_000
    await act.tap("🎯 Steal Star · 3.00M")
    assert "The clause changed from 3.00M to 3.50M since the briefing." in act.tg.text
    confirm = act.tg.buttons()["✅ Confirm"]
    star["buyoutClause"] = 3_900_000  # changes again after you confirmed
    await act.tap(confirm)
    assert act.tg.text.startswith("✋ Something changed since you confirmed, so nothing was done")
    assert act.store.executions() == []

    star["isShielded"] = True
    await act.tap("🎯 Steal Star · 3.00M", index=0)
    assert act.tg.text == "✋ Ana has shielded Star."


async def test_limits_and_switches(act):
    await act.offer()
    act.fake.data["team_money"] = {"teamMoney": 1_000_000}
    await act.tap("🎯 Steal Star · 3.00M")
    assert "clauses can't be paid on credit" in act.tg.text

    act.fake.data["team_money"] = {"teamMoney": 4_000_000}
    act.settings.action_max_money = 2_500_000
    await act.tap("🎯 Steal Star · 3.00M", index=0)
    assert "over your per-action limit (2.50M" in act.tg.text

    act.settings.action_max_money = 20_000_000
    act.settings.action_kinds = "bid"
    await act.tap("🎯 Steal Star · 3.00M", index=0)
    assert act.tg.text == "✋ clause steals are switched off (FANTASYBOT_ACTION_KINDS)."


# -- live mode -------------------------------------------------------------------


async def test_first_real_action_of_a_kind_asks_twice(act):
    act.settings.dry_run = False
    await act.offer()
    assert "LIVE" in act.tg.text
    await act.tap("🎯 Steal Star · 3.00M")
    await act.tap(act.tg.buttons()["✅ Confirm"])
    assert act.tg.text.startswith("This would be your first real clause steal through FantasyBot")
    assert act.fake.writes == []
    await act.tap(act.tg.buttons()["Yes, do it"])
    assert act.tg.text == "✅ Done: LaLiga answered HTTP 200."
    (sent,) = act.fake.writes
    assert sent.url.path == f"{C1}/league/L1/buyout/PT60/pay"
    assert json.loads(sent.content) == {"buyoutClauseToPay": 3_000_000}

    # The second real steal needs only one confirmation.
    await act.tap("🎯 Steal Cheap · 2.00M", index=0)
    await act.tap(act.tg.buttons()["✅ Confirm"])
    assert act.tg.text == "✅ Done: LaLiga answered HTTP 200."
    assert len(act.fake.writes) == 2
    # A used suggestion can't be replayed.
    await act.tap("🎯 Steal Star · 3.00M", index=0)
    assert act.tg.text == "✋ That suggestion was already used."


async def test_daily_limit_of_real_actions(act):
    act.settings.dry_run = False
    act.settings.action_daily_limit = 1
    act.store.mark_done_before("steal", NOW)
    await act.offer()
    await act.tap("🎯 Steal Star · 3.00M")
    await act.tap(act.tg.buttons()["✅ Confirm"])
    assert act.tg.text.startswith("✅ Done")
    await act.tap("🎯 Steal Cheap · 2.00M", index=0)
    assert "today's limit of 1 real actions" in act.tg.text


async def test_frozen_writes_stop_a_live_action(act, freeze):
    act.settings.dry_run = False
    act.store.mark_done_before("steal", NOW)
    await act.offer()
    await act.tap("🎯 Steal Star · 3.00M")
    freeze.freeze("invalid_grant", "refresh token expired")
    await act.tap(act.tg.buttons()["✅ Confirm"])
    assert "writes are frozen" in act.tg.text and act.fake.writes == []
    assert act.store.executions()[0]["outcome"] == "frozen"


async def test_laliga_refusing_is_reported(act):
    act.settings.dry_run = False
    act.store.mark_done_before("steal", NOW)
    act.fake.write_response = httpx.Response(400, json={"message": "Jugador blindado"})
    await act.offer()
    await act.tap("🎯 Steal Star · 3.00M")
    await act.tap(act.tg.buttons()["✅ Confirm"])
    assert act.tg.text == "❌ LaLiga refused it: HTTP 400: Jugador blindado"
    assert act.store.executions()[0]["outcome"] == "refused: HTTP 400: Jugador blindado"


# -- commands and the listener ---------------------------------------------------


async def test_listener_handles_commands_and_remembers_its_place(act):
    act.tg.pending = [{"update_id": 77, "message": {"text": "/status", "from": {"id": CHAT},
                                                     "chat": {"id": CHAT, "type": "private"}}}]
    await listen(act.center, act.bot, act.store, once=True)
    assert "Test mode" in act.tg.text and "real actions today: 0 (no daily limit)" in act.tg.text
    assert act.store.get("telegram_offset") == "78"
    await listen(act.center, act.bot, act.store, once=True)
    assert act.tg.calls[-1] == ("getUpdates", {"timeout": 50, "allowed_updates": ["message",
                                                                                  "callback_query"],
                                               "offset": 78})


async def test_telegram_errors_never_contain_the_token(router, http):
    router.add("POST", "https://api.telegram.org/",
               httpx.Response(401, json={"ok": False, "description": f"bad token {BOT_TOKEN}"}))
    with pytest.raises(TelegramError) as excinfo:
        await TelegramBot(http, BOT_TOKEN).send("1", "hi")
    assert BOT_TOKEN not in str(excinfo.value) and "HTTP 401" in str(excinfo.value)


def test_listen_refuses_when_actions_are_off(tmp_path, monkeypatch, capsys):
    from cryptography.fernet import Fernet

    from fantasybot.cli import main

    monkeypatch.setenv("FANTASYBOT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("FANTASYBOT_TOKEN_KEY", Fernet.generate_key().decode())
    assert main(["listen", "--once"]) == 2
    assert "FANTASYBOT_ACTIONS=on" in capsys.readouterr().err


# -- selling ---------------------------------------------------------------------


async def test_selling_buttons(act):
    await act.offer()
    offered = labels(act.tg)
    assert "✅ Sell Mid A · 12.00M (+20%)" in offered  # Carl's direct offer, starter: needs +8%
    assert "✅ Sell Fwd B · 6.60M (+10%)" in offered  # Ana beats the market's 6.1M
    assert "🪟 List 14 players for LaLiga's offers" in offered
    assert {"💸 List Hurt 8.00M", "8.40M", "8.80M"} <= set(offered)
    assert not any(label.startswith(("✖ Decline", "↩ Withdraw")) for label in offered)
    assert all(len(b.encode()) <= 64 for b in act.tg.buttons().values())  # Telegram's limit


async def test_offers_below_the_threshold_get_no_accept_button(act):
    act.fake.data["player_offer_by_id"]["PT41"] = [
        {"id": "O0", "money": 6_200_000, "status": "pending"},  # market +3%: not enough for a starter
        {"id": "O1", "money": 5_500_000, "status": "pending", "bidderTeam": {"id": "T2"}}]  # Ana lowballs
    await act.offer()
    offered = labels(act.tg)
    assert not any(label.startswith("✅ Sell Fwd B") for label in offered)
    act.settings.starter_sell_threshold = 0.02
    await act.offer()
    assert "✅ Sell Fwd B · 6.20M (+3%)" in labels(act.tg)


async def test_accept_decline_sell_withdraw_relist_requests(act):
    act.fake.data["player_offer_by_id"]["PT30"].append(
        {"id": "O3", "money": 9_000_000, "status": "pending", "bidderTeam": {"id": "T3"}})
    await act.offer()
    await act.tap("✅ Sell Mid A · 12.00M (+20%)")
    assert "Sell Mid A to Carl for 12.00M (value 10.00M). He leaves your squad." in act.tg.text
    assert "Cash 4.00M → 16.00M." in act.tg.text
    await act.tap(act.tg.buttons()["✅ Confirm"])
    assert f"POST {C1.removeprefix('/api')}/league/L1/market/MK30/offer/O2/accept\n" \
           '{"offerMoney": 12000000}' in act.tg.text

    await act.tap("💸 List Hurt 8.00M", index=0)
    assert "List Hurt for 8.00M (value 8.00M)" in act.tg.text and "nothing is sold until" in act.tg.text
    await act.tap(act.tg.buttons()["✅ Confirm"])
    assert '/market/sell\n{"playerId": "PT20", "salePrice": 8000000}' in act.tg.text

    await act.tap("🪟 List 14 players for LaLiga's offers", index=0)
    assert act.tg.text.startswith("List 14 players at their value: Keeper 5.00M, Backup 1.00M")
    await act.tap(act.tg.buttons()["✅ Confirm"])
    assert act.tg.text.startswith("🧪 Test only, nothing was sent. FantasyBot would have sent 14 requests")
    assert "… and 2 more" in act.tg.text
    assert act.fake.writes == []


async def test_decline_and_withdraw_with_a_bench_shop_window(act):
    act.settings.shop_window = "bench"
    act.fake.data["player_offer_by_id"]["PT41"] = [
        {"id": "O1", "money": 5_500_000, "status": "pending", "bidderTeam": {"id": "T2"}}]
    await act.offer()
    await act.tap("✖ Decline Ana's 5.50M for Fwd B")
    await act.tap(act.tg.buttons()["✅ Confirm"])
    assert act.tg.text.endswith("POST /v1/competition/1/league/L1/market/M4/offer/O1/reject\n(no body)")
    await act.tap("↩ Withdraw Fwd B (he's in your best XI)", index=0)
    await act.tap(act.tg.buttons()["✅ Confirm"])
    assert act.tg.text.endswith("DELETE /v1/competition/1/league/L1/market/M4/delete\n(no body)")


async def test_offer_changes_and_relisting_are_refused(act):
    await act.offer()
    act.fake.data["player_offer_by_id"]["PT30"][0]["money"] = 11_000_000
    await act.tap("✅ Sell Mid A · 12.00M (+20%)")
    assert act.tg.text == "✋ Carl's offer changed to 11.00M; the next briefing will show it."
    act.fake.data["player_offer_by_id"]["PT30"] = []
    await act.tap("✅ Sell Mid A · 12.00M (+20%)", index=0)
    assert act.tg.text == "✋ Carl's offer for Mid A isn't open any more."
    # Fwd B is already listed: listing him again is refused.
    from fantasybot.actions.core import sell_suggestion
    from fantasybot.scout.briefing import gather as gather_view

    async def no_sleep(_):
        return None

    view = await gather_view(act.client, act.settings, NOW, sleep=no_sleep)
    fwd_b = next(p for p in view.my_squad if p.name == "Fwd B")
    await act.center.offer_suggestions([sell_suggestion(view, fwd_b, "test", NOW)], "test")
    await act.tap("💸 List Fwd B 6.00M")
    assert act.tg.text == "✋ Fwd B is already on the market."


async def test_first_real_sale_asks_twice(act):
    act.settings.dry_run = False
    await act.offer()
    await act.tap("✅ Sell Mid A · 12.00M (+20%)")
    await act.tap(act.tg.buttons()["✅ Confirm"])
    assert act.tg.text.startswith("This would be your first real sale (accepting an offer) through")
    await act.tap(act.tg.buttons()["Yes, do it"])
    (sent,) = act.fake.writes
    assert sent.url.path == f"{C1}/league/L1/market/MK30/offer/O2/accept"
    assert json.loads(sent.content) == {"offerMoney": 12_000_000}


async def test_live_relist_sends_each_listing_and_skips_the_daily_limit(act):
    act.settings.dry_run = False
    act.settings.action_daily_limit = 1
    act.store.mark_done_before("relist", NOW)
    act.store.mark_done_before("steal", NOW)
    await act.offer()
    await act.tap("🪟 List 14 players for LaLiga's offers")
    await act.tap(act.tg.buttons()["✅ Confirm"])
    assert act.tg.text == "✅ Listed 14 of 14."
    assert len(act.fake.writes) == 14 and all(w.url.path.endswith("/market/sell") for w in act.fake.writes)
    # Listing is reversible and spends nothing: the one real action today is still free.
    await act.tap("🎯 Steal Star · 3.00M", index=0)
    await act.tap(act.tg.buttons()["✅ Confirm"])
    assert act.tg.text.startswith("✅ Done")


async def test_kickoff_guard_offers_sell_buttons_for_a_negative_balance(act):
    from fantasybot.scout.guard import guard_check

    async def no_sleep(_):
        return None

    act.fake.data["team_money"] = {"teamMoney": -1_500_000}

    class Quiet:
        async def send(self, text):
            return True

    result = await guard_check(act.client, act.settings, Quiet(), now=NOW, sleep=no_sleep)
    assert result.message and result.to_sell
    assert await act.center.offer_sales(result.view, result.to_sell) == len(result.to_sell)
    assert act.tg.text.startswith("List one of these to get back above 0?")
    assert any(label.startswith("💸 List ") for label in labels(act.tg))


def test_zero_means_no_limit_of_your_own(settings, tmp_path):
    from datetime import date

    from fantasybot.actions.core import ActionRefused, Plan, check_limits

    settings.dry_run = False
    store = ActionStore(tmp_path / "limits.sqlite")
    big = Plan("steal", "steal someone", 500_000_000, {})
    check_limits(big, settings, store, date(2026, 11, 14))  # defaults: no caps of your own
    settings.action_max_money = 20_000_000
    with pytest.raises(ActionRefused, match="per-action limit"):
        check_limits(big, settings, store, date(2026, 11, 14))
    store.close()


async def test_a_managers_listing_needs_premium_even_from_an_old_button(act):
    act.settings.premium = True
    await act.offer()
    assert "🛒 Listed 10.00M" in labels(act.tg)
    act.settings.premium = False  # e.g. Premium ran out since the briefing
    await act.tap("🛒 Listed 10.00M")
    assert "listed by a manager: bidding on that needs Premium" in act.tg.text
    assert act.fake.writes == []
