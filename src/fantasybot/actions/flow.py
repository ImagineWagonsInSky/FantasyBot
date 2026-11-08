"""Acting from Telegram: suggestion → live check → Confirm → (first time: are you sure?) → done.

Only your own private chat is obeyed (FANTASYBOT_TELEGRAM_CHAT_ID); anything
else is ignored without a reply. Buttons carry random IDs only; what an
action does is read from the store and re-checked against LaLiga at every
step. With FANTASYBOT_DRY_RUN=true (the default) nothing is ever sent.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from ..api import ApiError, FantasyClient, ResponseShapeError
from ..auth import AuthUnavailable, WritesFrozen
from ..config import Settings
from ..scout.analysis import SquadPlayer
from ..scout.briefing import BriefingError, LeagueView
from ..telegram import Button, TelegramBot, TelegramError
from .core import (
    KIND_NAMES,
    ActionRefused,
    Plan,
    Suggestion,
    check_limits,
    describe_request,
    execute,
    prepare,
    sell_suggestion,
    suggest,
)
from .store import ActionStore, Confirmation

log = logging.getLogger(__name__)

BriefingFn = Callable[[], Awaitable[LeagueView]]
HELP = ("Commands: /briefing (today's briefing with action buttons), /status (mode and limits), "
        "/help. Buttons under a briefing suggest actions; each asks before doing anything.")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ActionCenter:
    def __init__(self, settings: Settings, client: FantasyClient, bot: TelegramBot, store: ActionStore, *,
                 briefing: BriefingFn | None = None, clock: Callable[[], datetime] = _now) -> None:
        self._settings = settings
        self._client = client
        self._bot = bot
        self._store = store
        self._briefing = briefing
        self._clock = clock
        self._chat = str(settings.telegram_chat_id or "")

    # -- who may act -------------------------------------------------------------

    def authorized(self, chat: dict[str, Any] | None, user: dict[str, Any] | None) -> bool:
        return bool(self._chat and chat and user and chat.get("type") == "private"
                    and str(chat.get("id")) == self._chat and str(user.get("id")) == self._chat)

    async def say(self, text: str, buttons: list[list[Button]] | None = None) -> int:
        return await self._bot.send(self._chat, text, buttons)

    def _mode_line(self) -> str:
        if self._settings.dry_run:
            return "🧪 Test mode (FANTASYBOT_DRY_RUN=true): nothing is sent to LaLiga."
        return "⚠️ LIVE: this really happens in your league."

    # -- offering ----------------------------------------------------------------

    async def offer(self, view: LeagueView) -> int:
        """Sends the action buttons for a briefing. Returns how many were offered."""
        return await self.offer_suggestions(
            suggest(view, self._settings, self._clock()),
            "Want to act on any of this? Each button checks LaLiga again and asks before doing anything.")

    async def offer_sales(self, view: LeagueView, players: list[SquadPlayer]) -> int:
        """Sell buttons for the kickoff guard, when a negative balance would cost the jornada."""
        if "sell" not in self._settings.enabled_actions():
            return 0
        now = self._clock()
        return await self.offer_suggestions(
            [sell_suggestion(view, p, "covers your negative balance", now) for p in players],
            "List one of these to get back above 0? Nothing is sold until you accept an offer.")

    async def offer_suggestions(self, suggestions: list[Suggestion], intro: str) -> int:
        now = self._clock()
        rows = []
        for s in suggestions:
            p = self._store.add_proposal(s.kind, s.label, s.params, now, s.expires)
            rows.append([Button(label[:64], f"p:{p.id}:{i}") for i, label in enumerate(s.params["options"])])
        if not rows:
            return 0
        await self.say(f"{intro}\n{self._mode_line()}", rows)
        return len(rows)

    # -- updates -----------------------------------------------------------------

    async def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._on_tap(update["callback_query"])
        elif "message" in update:
            await self._on_message(update["message"])

    async def _on_message(self, message: dict[str, Any]) -> None:
        if not self.authorized(message.get("chat"), message.get("from")):
            log.warning("ignored a message from an unauthorised chat")
            return
        text = (message.get("text") or "").strip().split("@")[0]
        if text == "/briefing" and self._briefing is not None:
            try:
                view = await self._briefing()
            except BriefingError as exc:
                await self.say(f"Briefing failed: {exc}")
                return
            if not await self.offer(view):
                await self.say("Nothing to act on right now.")
        elif text == "/status":
            today = self._clock().date()
            kinds = ", ".join(sorted(self._settings.enabled_actions())) or "none"
            await self.say(f"{self._mode_line()}\nActions on: {kinds}\n"
                           f"Per-action limit: {self._settings.action_max_money / 1e6:.1f}M · "
                           f"real actions today: {self._store.live_actions_on(today)} of "
                           f"{self._settings.action_daily_limit}")
        else:
            await self.say(HELP)

    async def _on_tap(self, tap: dict[str, Any]) -> None:
        message = tap.get("message") or {}
        if not self.authorized(message.get("chat"), tap.get("from")):
            log.warning("ignored a button press from an unauthorised chat")
            return
        try:
            await self._bot.answer(tap["id"])
        except TelegramError as exc:
            log.warning("could not acknowledge a button press: %s", exc)
        data = tap.get("data") or ""
        try:
            if data.startswith("p:"):
                await self._propose(data)
            elif data.startswith("c:"):
                await self._confirm(data[2:])
            elif data.startswith("x:"):
                await self._cancel(data[2:])
        except ActionRefused as exc:
            await self.say(f"✋ {exc}")
        except (ApiError, ResponseShapeError, AuthUnavailable) as exc:
            await self.say(f"❌ Couldn't check with LaLiga, so nothing was done: {exc}")

    # -- steps -------------------------------------------------------------------

    async def _propose(self, data: str) -> None:
        try:
            _, pid, raw_option = data.split(":")
            option = int(raw_option)
        except ValueError:
            raise ActionRefused("I don't understand that button.") from None
        proposal = self._store.proposal(pid)
        if proposal is None:
            raise ActionRefused("I don't know that suggestion any more.")
        now = self._clock()
        plan = await prepare(proposal, option, self._client, self._settings, now)
        check_limits(plan, self._settings, self._store, now.date())
        nonce = self._store.add_confirmation(pid, "confirm", {**plan.to_dict(), "option": option}, now,
                                             now + timedelta(seconds=self._settings.confirm_seconds))
        lines = [plan.summary, *[f"⚠️ {w}" for w in plan.warnings], "", self._mode_line(),
                 f"This button works once, for {max(1, self._settings.confirm_seconds // 60)} min."]
        message_id = await self.say("\n".join(lines), [[Button("✅ Confirm", f"c:{nonce}"),
                                                         Button("✖ Cancel", f"x:{nonce}")]])
        self._store.set_confirmation_message(nonce, message_id)

    async def _claim(self, nonce: str) -> Confirmation:
        conf = self._store.confirmation(nonce)
        if conf is None:
            raise ActionRefused("I don't know that confirmation.")
        if conf.message_id:
            try:
                await self._bot.clear_buttons(self._chat, conf.message_id)
            except TelegramError:
                pass
        if not self._store.use_confirmation(nonce):
            raise ActionRefused("That button was already used.")
        if conf.expires <= self._clock():
            raise ActionRefused("That confirmation expired. Tap the suggestion again for a fresh check.")
        return conf

    async def _cancel(self, nonce: str) -> None:
        await self._claim(nonce)
        await self.say("Cancelled; nothing was done.")

    async def _confirm(self, nonce: str) -> None:
        conf = await self._claim(nonce)
        proposal = self._store.proposal(conf.proposal_id)
        if proposal is None:
            raise ActionRefused("I don't know that suggestion any more.")
        confirmed = Plan.from_dict(conf.plan)
        now = self._clock()
        live = not self._settings.dry_run

        if live and conf.stage == "confirm" and not self._store.done_before(proposal.kind):
            again = self._store.add_confirmation(proposal.id, "first_live", conf.plan, now,
                                                 now + timedelta(seconds=self._settings.confirm_seconds))
            message_id = await self.say(
                f"This would be your first real {KIND_NAMES[proposal.kind]} through FantasyBot, and "
                f"LaLiga can't undo it.\n\n{confirmed.summary}\n\nDo it for real?",
                [[Button("Yes, do it", f"c:{again}"), Button("No", f"x:{again}")]])
            self._store.set_confirmation_message(again, message_id)
            return

        # Check everything once more, right before sending.
        fresh = await prepare(proposal, conf.plan["option"], self._client, self._settings, now)
        if fresh.call != confirmed.call:
            raise ActionRefused("Something changed since you confirmed, so nothing was done:\n"
                                f"{fresh.summary}\nTap the suggestion again if you still want it.")
        check_limits(fresh, self._settings, self._store, now.date())
        league, team = proposal.params["league_id"], proposal.params["team_id"]
        try:
            result = await execute(fresh, self._client, league, team)
        except WritesFrozen as exc:
            self._store.record_execution(now, now.date(), proposal.id, proposal.kind, False, None, "frozen")
            raise ActionRefused(str(exc)) from None
        except ApiError as exc:
            self._store.record_execution(now, now.date(), proposal.id, proposal.kind, False, fresh.call,
                                         f"refused: {exc.message}")
            await self.say(f"❌ LaLiga refused it: {exc.message}")
            return

        request = {"method": result.method, "url": result.url, "body": result.body}
        if result.dry_run:
            self._store.record_execution(now, now.date(), proposal.id, proposal.kind, True, request,
                                         "simulated")
            await self.say("🧪 Test only, nothing was sent. FantasyBot would have sent:\n"
                           + describe_request(result, self._settings))
            return
        self._store.record_execution(now, now.date(), proposal.id, proposal.kind, False, request, "done")
        self._store.mark_done_before(proposal.kind, now)
        self._store.close_proposal(proposal.id)
        extra = " Bids are blind: you'll know when the market closes." if proposal.kind == "bid" else ""
        await self.say(f"✅ Done: LaLiga answered HTTP {result.status_code}.{extra}")


async def listen(center: ActionCenter, bot: TelegramBot, store: ActionStore, *, once: bool = False,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    """Long-polls Telegram and hands each update to the action center."""
    raw = store.get("telegram_offset")
    offset = int(raw) if raw else None
    backoff = 5
    while True:
        try:
            updates = await bot.updates(offset)
            backoff = 5
        except TelegramError as exc:
            log.warning("Telegram polling failed (%s); retrying in %ss", exc, backoff)
            await sleep(backoff)
            backoff = min(backoff * 2, 300)
            if once:
                return
            continue
        for update in updates:
            offset = int(update["update_id"]) + 1
            store.put("telegram_offset", str(offset))
            try:
                await center.handle(update)
            except Exception:  # one bad update must not stop the listener
                log.exception("failed to handle a Telegram update")
        if once:
            return
