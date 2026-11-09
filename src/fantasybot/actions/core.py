"""Suggested actions: what the briefing offers, the live checks, and carrying them out.

The kinds, all things you'd otherwise do in the app:
  lineup    save the suggested best XI
  bid       bid on a market player (minimum, +5% or +10%)
  steal     pay a rival player's buyout clause
  raise     raise one of your exposed players' clauses
  sell      list one of your players (at value, +5% or +10%)
  accept    accept an offer for one of your players
  decline   decline an offer
  withdraw  take a listing back

Nothing here trusts the briefing: prepare() re-reads LaLiga every time and
refuses if anything no longer holds. Shielding isn't offered: the API call
claims a rewarded ad was watched.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..api import ApiError, FantasyClient, LineupUpdate, WriteResult
from ..api.models import MarketItem, Offer, TeamPlayer, same_id
from ..config import Settings
from ..scout.analysis import (
    Rival,
    SquadPlayer,
    best_lineup,
    clause_radar,
    credit_limit,
    money,
    next_kickoff,
    parse_time,
    rank_market,
    read_lineup,
)
from ..scout.briefing import LeagueView, exposure_order, takers, verdicts
from ..scout.model import DEF, FWD, GK, MID
from ..scout.selling import bidder_name, on_sale, sell_candidates, shop_window, should_withdraw
from .store import ActionStore, Proposal

KIND_NAMES = {"lineup": "lineup change", "bid": "bid", "steal": "clause steal", "raise": "clause raise",
              "sell": "listing", "relist": "shop-window relist", "accept": "sale (accepting an offer)",
              "decline": "declined offer", "withdraw": "withdrawn listing"}
# Spend nothing and can be undone: these don't count towards the daily limit of real actions.
REVERSIBLE = frozenset({"sell", "relist", "withdraw", "decline"})
SELL_STEPS = (1.0, 1.05, 1.10)
BID_STEPS = (1.0, 1.05, 1.10)
LINEUP_MIN_GAIN = 0.5


class ActionRefused(Exception):
    """The action can't or shouldn't go ahead; the message says why."""


@dataclass
class Plan:
    kind: str
    summary: str
    money: int  # what it spends; 0 for a lineup
    call: dict[str, Any]  # exactly what execute() sends
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Plan:
        return cls(**{k: data[k] for k in ("kind", "summary", "money", "call", "warnings")})


@dataclass
class Suggestion:
    kind: str
    label: str
    params: dict[str, Any]
    expires: datetime


def _round_up(amount: float, step: int) -> int:
    return int(math.ceil(amount / step) * step)


# -- what the briefing offers --------------------------------------------------


def suggest(view: LeagueView, settings: Settings, now: datetime) -> list[Suggestion]:
    kinds = settings.enabled_actions()
    base = {"league_id": view.league_id, "team_id": view.me.team.id}
    out: list[Suggestion] = []
    squad = view.my_squad

    if "lineup" in kinds and squad:
        plan = best_lineup(squad, view.formations)
        current = read_lineup(view.lineup, squad)
        if plan and plan.expected - current.expected >= LINEUP_MIN_GAIN:
            kickoff = next_kickoff(view.matches, view.week, now)
            expires = (kickoff.first_match if kickoff and not kickoff.in_progress
                       else now + timedelta(hours=24))
            gain = plan.expected - current.expected
            out.append(Suggestion("lineup", f"best XI {plan.formation_label()}", {
                **base,
                "formation": list(plan.formation),
                "goalkeeper": plan.by_position[GK][0].player_team_id,
                "defender": [p.player_team_id for p in plan.by_position[DEF]],
                "midfield": [p.player_team_id for p in plan.by_position[MID]],
                "striker": [p.player_team_id for p in plan.by_position[FWD]],
                "options": [f"🔁 Apply best XI {plan.formation_label()} (+{gain:.1f} xP)"],
            }, expires))

    if "steal" in kinds:
        rivals = [Rival(e, view.rank_of(e), view.squads.get(str(e.team.id), []))
                  for e in view.standing if not same_id(e.team.id, view.me.team.id)]
        # Ordered by points swing when the rivals' picture is in, by your gain otherwise.
        targets = ([w.target for w in view.intel.swings] if view.intel is not None
                   else clause_radar(rivals, view.balance, now).affordable)
        for t in targets[:3]:
            out.append(Suggestion("steal", f"steal {t.player.name}", {
                **base,
                "owner_team_id": t.rival.entry.team.id, "owner": t.rival.name,
                "player_team_id": t.player.player_team_id, "name": t.player.name, "clause": t.cost,
                "options": [f"🎯 Steal {t.player.name} · {money(t.cost)}"],
            }, now + timedelta(hours=12)))

    if "bid" in kinds and view.market:
        team_value = view.me.teamValue or view.me.team.teamValue
        limit = credit_limit(view.balance, team_value)
        picks = rank_market(view.market, view.details, view.week, view.balance, limit, {}, {},
                            exclude_team=view.me.team.id, outlook=view.outlook_fn())
        offered = 0
        for pick in picks:
            if offered == 3:
                break
            if pick.projection.outlook <= 0 or pick.item.bid is not None:
                continue
            if not (pick.payable_cash or pick.payable_credit):
                continue
            amounts = {_round_up(pick.price * s, 10_000) for s in BID_STEPS}
            contest = view.intel.competition.get(str(pick.item.id)) if view.intel is not None else None
            to_win = contest.to_win if contest is not None else None
            if to_win is not None:
                amounts.add(to_win)  # what should beat the rivals likely to bid
            amounts = sorted(a for a in amounts if limit is None or a <= limit)
            if not amounts:
                continue
            name = pick.player.display_name

            def label(amount: int, first: bool, name: str = name, to_win: int | None = to_win) -> str:
                text = f"🛒 {name} {money(amount)}" if first else money(amount)
                return text + (" ⚔" if amount == to_win else "")

            expires = parse_time(pick.item.expirationDate) or now + timedelta(hours=12)
            out.append(Suggestion("bid", f"bid on {name}", {
                **base, "market_id": pick.item.id, "player_id": pick.player.id, "name": name,
                "amounts": amounts, "to_win": to_win,
                "options": [label(a, i == 0) for i, a in enumerate(amounts)],
            }, expires))
            offered += 1

    sales = on_sale(view.market, squad, view.offers, view.me.team.id) if squad else []
    keep = view.keep()
    if {"accept", "decline"} & kinds:
        judged = [v for v in verdicts(view) if v.sale.market_id is not None]
        for v in judged:
            s, best = v.sale, v.offer
            bidder = bidder_name(best, view.standing)
            common = {**base, "player_team_id": s.player_team_id, "name": s.name, "market_id": s.market_id,
                      "offer_id": best.id, "money": best.money, "bidder": bidder}
            if v.worth and "accept" in kinds:
                out.append(Suggestion("accept", f"accept {bidder}'s offer for {s.name}", {
                    **common, "options": [f"✅ Sell {s.name} · {money(best.money)} ({v.premium:+.0%})"]},
                    now + timedelta(hours=12)))
            elif not v.worth and v.premium < 0 and best.bidderTeam is not None and "decline" in kinds:
                # LaLiga's own offers just expire; a manager's lowball is worth an answer.
                out.append(Suggestion("decline", f"decline {bidder}'s offer for {s.name}", {
                    **common, "options": [f"✖ Decline {bidder}'s {money(best.money)} for {s.name}"]},
                    now + timedelta(hours=12)))
    if "relist" in kinds:
        window = shop_window(squad, keep, sales, settings.shop_window) if squad else []
        if window:
            players = [{"player_team_id": p.player_team_id, "name": p.name, "value": p.value} for p in window]
            out.append(Suggestion("relist", f"relist {len(window)} players", {
                **base, "players": players,
                "options": [f"🪟 List {len(window)} players for LaLiga's offers"]},
                now + timedelta(hours=12)))
    if "withdraw" in kinds:
        for s in sales:
            why = should_withdraw(s, keep, settings.shop_window == "everyone")
            if why and s.market_id is not None:
                out.append(Suggestion("withdraw", f"withdraw {s.name}", {
                    **base, "player_team_id": s.player_team_id, "name": s.name, "market_id": s.market_id,
                    "options": [f"↩ Withdraw {s.name} ({why})"[:64]]}, now + timedelta(hours=12)))
    if "sell" in kinds and squad:
        for c in sell_candidates(squad, keep, view.balance, view.change_3d, sales):
            out.append(sell_suggestion(view, c.player, c.reason, now))

    if "raise" in kinds and view.balance is not None:
        for p in exposure_order(view)[:2]:
            clause = p.clause or p.value
            options = []
            richest = max((c for _, c in takers(view, clause)), default=None)
            if richest is not None:  # out of reach of everyone's estimated cash
                options.append(_round_up(richest - clause + 1_000_000, 500_000))
            options.append(_round_up(clause * 0.25, 500_000))
            options = sorted({i for i in options if 0 < i // 2 <= view.balance})
            if not options:
                continue
            out.append(Suggestion("raise", f"raise {p.name}'s clause", {
                **base, "player_team_id": p.player_team_id, "name": p.name, "clause": clause,
                "increases": options,
                "options": [f"🛡️ {p.name} +{money(i)} (pay {money(i // 2)})" for i in options],
            }, now + timedelta(hours=12)))
    return out


def sell_suggestion(view: LeagueView, player: SquadPlayer, reason: str, now: datetime) -> Suggestion:
    prices = sorted({_round_up(player.value * s, 10_000) for s in SELL_STEPS})
    in_best_xi = any(str(k.player.id) == str(player.player.id) for k in view.keep())
    return Suggestion("sell", f"list {player.name}", {
        "league_id": view.league_id, "team_id": view.me.team.id,
        "player_team_id": player.player_team_id, "name": player.name, "value": player.value,
        "prices": prices, "reason": reason, "in_best_xi": in_best_xi,
        "options": [f"💸 List {player.name} {money(prices[0])}"] + [money(p) for p in prices[1:]],
    }, now + timedelta(hours=12))


async def _my_listing(client: FantasyClient, league: Any, team_id: Any,
                      player_team_id: Any) -> MarketItem | None:
    for item in await client.get_market(league):
        if (item.discr == "marketPlayerTeam" and same_id(item.seller_team_id, team_id) and item.playerTeam
                and same_id(item.playerTeam.playerTeamId, player_team_id) and not item.directOffer):
            return item
    return None


async def _live_offer(client: FantasyClient, league: Any, params: dict[str, Any]) -> Offer:
    offers = await client.get_player_offer(league, params["player_team_id"])
    offer = next((o for o in offers if same_id(o.id, params["offer_id"])), None)
    if offer is None or not offer.open:
        raise ActionRefused(f"{params['bidder']}'s offer for {params['name']} isn't open any more.")
    if offer.money != params["money"]:
        raise ActionRefused(f"{params['bidder']}'s offer changed to {money(offer.money)}; "
                            "the next briefing will show it.")
    return offer


async def _owned(client: FantasyClient, league: Any, team_id: Any, params: dict[str, Any]) -> TeamPlayer:
    squad = await client.get_team_data(league, team_id)
    owned = next((pt for pt in squad.players if same_id(pt.player_team_id, params["player_team_id"])), None)
    if owned is None:
        raise ActionRefused(f"{params['name']} isn't in your squad any more.")
    return owned


# -- live checks -----------------------------------------------------------------


async def prepare(proposal: Proposal, option: int, client: FantasyClient, settings: Settings,
                  now: datetime) -> Plan:
    """Re-reads LaLiga and returns exactly what the action would do, or refuses."""
    kind, params = proposal.kind, proposal.params
    if kind not in settings.enabled_actions():
        raise ActionRefused(f"{KIND_NAMES.get(kind, kind)}s are switched off (FANTASYBOT_ACTION_KINDS).")
    if proposal.status != "open":
        raise ActionRefused("That suggestion was already used.")
    if proposal.expires <= now:
        raise ActionRefused("That suggestion has expired; the next briefing will have fresh ones.")
    if not 0 <= option < len(params["options"]):
        raise ActionRefused("Unknown option.")
    league, team_id, name = params["league_id"], params["team_id"], params.get("name", "")
    cash = (await client.get_team_money(team_id)).amount

    if kind == "steal":
        owner = params["owner"]
        squad = await client.get_team_data(league, params["owner_team_id"])
        owned = next((pt for pt in squad.players
                      if same_id(pt.player_team_id, params["player_team_id"])), None)
        if owned is None:
            raise ActionRefused(f"{name} isn't in {owner}'s squad any more.")
        locked = parse_time(owned.buyoutClauseLockedEndTime)
        if locked is not None and locked > now:
            raise ActionRefused(f"{name}'s clause is locked until {locked.strftime('%a %d %b %H:%M')}.")
        if owned.isShielded:
            raise ActionRefused(f"{owner} has shielded {name}.")
        clause = owned.buyoutClause or owned.playerMaster.marketValue or 0
        if clause > cash:
            raise ActionRefused(f"The clause is {money(clause)} and you have {money(cash)}; "
                                "clauses can't be paid on credit.")
        warnings = []
        if clause != params["clause"]:
            warnings.append(f"The clause changed from {money(params['clause'])} to {money(clause)} "
                            "since the briefing.")
        return Plan(kind, f"Pay {money(clause)} to take {name} from {owner}.\n"
                          f"Cash {money(cash)} → {money(cash - clause)}.",
                    clause, {"player_team_id": owned.player_team_id, "amount": clause}, warnings)

    if kind == "bid":
        amount = params["amounts"][option]
        item = next((i for i in await client.get_market(league) if same_id(i.id, params["market_id"])), None)
        if item is None:
            raise ActionRefused(f"{name} isn't on the market any more.")
        closes = parse_time(item.expirationDate)
        if closes is not None and closes <= now:
            raise ActionRefused(f"The market for {name} has closed.")
        if item.bid is not None:
            raise ActionRefused(f"You already have a bid on {name}; change it in the app.")
        minimum = item.playerMaster.marketValue or item.salePrice or 0
        if amount < minimum:
            raise ActionRefused(f"The minimum bid for {name} is now {money(minimum)}.")
        rows = await client.get_league_ranking(league)
        me = next((r for r in rows if same_id(r.team.id, team_id)), None)
        limit = credit_limit(cash, (me.teamValue or me.team.teamValue) if me else None)
        if limit is not None and amount > limit:
            raise ActionRefused(f"{money(amount)} is over your max bid ({money(limit)}).")
        warnings = []
        if amount > cash:
            warnings.append(f"That's more than your cash ({money(cash)}). If you win, get back above 0 "
                            "before kickoff or you score 0 points for the jornada.")
        when = f" when the market closes {closes.strftime('%a %H:%M')}" if closes else ""
        return Plan(kind, f"Bid {money(amount)} for {name} (minimum {money(minimum)}). "
                          f"Bids are blind; it's settled{when}.",
                    amount, {"market_id": item.id, "amount": amount}, warnings)

    if kind == "raise":
        increase = params["increases"][option]
        cost = increase // 2
        squad = await client.get_team_data(league, team_id)
        owned = next((pt for pt in squad.players if same_id(pt.player_team_id, params["player_team_id"])),
                     None)
        if owned is None:
            raise ActionRefused(f"{name} isn't in your squad any more.")
        if cost > cash:
            raise ActionRefused(f"It costs {money(cost)} and you have {money(cash)}; "
                                "clause raises can't be paid on credit.")
        clause = owned.buyoutClause or owned.playerMaster.marketValue or 0
        return Plan(kind, f"Raise {name}'s clause from {money(clause)} to {money(clause + increase)}, "
                          f"for {money(cost)}.\nCash {money(cash)} → {money(cash - cost)}.",
                    cost, {"player_team_id": owned.player_team_id, "increase": increase})

    if kind == "lineup":
        squad = await client.get_team_data(league, team_id)
        by_id = {str(pt.player_team_id): pt for pt in squad.players}
        chosen = [params["goalkeeper"], *params["defender"], *params["midfield"], *params["striker"]]
        missing = [str(c) for c in chosen if str(c) not in by_id]
        if missing:
            raise ActionRefused("Your squad changed since the briefing; tap /briefing for a new lineup.")
        out = [by_id[str(c)].playerMaster.display_name for c in chosen
               if (by_id[str(c)].playerMaster.playerStatus or "ok") in ("injured", "suspended")]
        if out:
            raise ActionRefused(f"{', '.join(out)} can't play any more; tap /briefing for a new lineup.")

        def names(ids: list[Any]) -> str:
            return ", ".join(by_id[str(i)].playerMaster.display_name for i in ids)

        formation = "-".join(map(str, params["formation"]))
        summary = (f"Save lineup {formation}: GK {names([params['goalkeeper']])} · DEF "
                   f"{names(params['defender'])} · MID {names(params['midfield'])} · FWD "
                   f"{names(params['striker'])}. Your coach stays as he is.")
        lineup = {"goalkeeper": params["goalkeeper"], "defender": params["defender"],
                  "midfield": params["midfield"], "striker": params["striker"],
                  "tactical_formation": params["formation"]}
        return Plan(kind, summary, 0, {"lineup": lineup})

    if kind == "sell":
        price = params["prices"][option]
        owned = await _owned(client, league, team_id, params)
        if await _my_listing(client, league, team_id, params["player_team_id"]) is not None:
            raise ActionRefused(f"{name} is already on the market.")
        value = owned.playerMaster.marketValue or params["value"]
        warnings = []
        if price < value:
            warnings.append(f"That's below his value now ({money(value)}).")
        if params.get("in_best_xi"):
            warnings.append(f"{name} is in your best XI.")
        return Plan(kind, f"List {name} for {money(price)} (value {money(value)}). Offers come in while "
                          "he's listed; nothing is sold until you accept one.",
                    0, {"player_team_id": owned.player_team_id, "price": price}, warnings)

    if kind in ("accept", "decline"):
        await _owned(client, league, team_id, params)
        offer = await _live_offer(client, league, params)
        bidder, amount = params["bidder"], offer.money or 0
        if kind == "decline":
            return Plan(kind, f"Decline {bidder}'s {money(amount)} offer for {name}.", 0,
                        {"market_id": params["market_id"], "offer_id": offer.id})
        owned = await _owned(client, league, team_id, params)
        value = owned.playerMaster.marketValue or 0
        warnings = [f"That's below his value ({money(value)})."] if value and amount < value else []
        return Plan(kind, f"Sell {name} to {bidder} for {money(amount)} (value {money(value)}). "
                          f"He leaves your squad.\nCash {money(cash)} → {money(cash + amount)}.",
                    0, {"market_id": params["market_id"], "offer_id": offer.id, "money": amount}, warnings)

    if kind == "relist":
        squad = await client.get_team_data(league, team_id)
        by_id = {str(pt.player_team_id): pt for pt in squad.players}
        listed = {str(i.playerTeam.playerTeamId) for i in await client.get_market(league)
                  if i.discr == "marketPlayerTeam" and same_id(i.seller_team_id, team_id)
                  and i.playerTeam and not i.directOffer}
        todo = []
        for p in params["players"]:
            owned = by_id.get(str(p["player_team_id"]))
            if owned is None or str(p["player_team_id"]) in listed:
                continue
            value = owned.playerMaster.marketValue or p["value"]
            todo.append({"player_team_id": owned.player_team_id, "name": p["name"],
                         "price": _round_up(value, 10_000)})
        if not todo:
            raise ActionRefused("They're all listed already (or no longer yours).")
        names = ", ".join(f"{t['name']} {money(t['price'])}" for t in todo)
        return Plan(kind, f"List {len(todo)} players at their value: {names}.\nLaLiga makes an offer on "
                          "each; nothing is sold until you accept one.", 0, {"listings": todo})

    if kind == "withdraw":
        listing = await _my_listing(client, league, team_id, params["player_team_id"])
        if listing is None:
            raise ActionRefused(f"{name} isn't on the market any more.")
        return Plan(kind, f"Take {name} off the market (listed at {money(listing.salePrice)}).", 0,
                    {"market_id": listing.id})

    raise ActionRefused(f"Unknown action {kind}.")


def check_limits(plan: Plan, settings: Settings, store: ActionStore, today: Any) -> None:
    if plan.money > settings.action_max_money:
        raise ActionRefused(f"{money(plan.money)} is over your per-action limit "
                            f"({money(settings.action_max_money)}, FANTASYBOT_ACTION_MAX_MONEY). "
                            "Do it in the app, or raise the limit.")
    if (not settings.dry_run and plan.kind not in REVERSIBLE
            and store.live_actions_on(today, exclude=REVERSIBLE) >= settings.action_daily_limit):
        raise ActionRefused(f"That's today's limit of {settings.action_daily_limit} real actions "
                            "(FANTASYBOT_ACTION_DAILY_LIMIT).")


BatchResult = list[tuple[str, "WriteResult | ApiError"]]


async def execute(plan: Plan, client: FantasyClient, league_id: Any,
                  team_id: Any) -> WriteResult | BatchResult:
    call = plan.call
    if plan.kind == "relist":
        done: BatchResult = []
        for listing in call["listings"]:
            try:
                done.append((listing["name"], await client.sell_player_to_market(
                    league_id, listing["player_team_id"], listing["price"])))
            except ApiError as exc:  # keep going: one refusal shouldn't stop the rest
                done.append((listing["name"], exc))
        return done
    if plan.kind == "steal":
        return await client.pay_buyout_clause(league_id, call["player_team_id"], call["amount"])
    if plan.kind == "bid":
        return await client.make_bid(league_id, call["market_id"], call["amount"])
    if plan.kind == "raise":
        return await client.increase_buyout_clause(league_id, call["player_team_id"], 2.0, call["increase"])
    if plan.kind == "lineup":
        return await client.update_lineup(team_id, LineupUpdate(**call["lineup"]))
    if plan.kind == "sell":
        return await client.sell_player_to_market(league_id, call["player_team_id"], call["price"])
    if plan.kind == "accept":
        return await client.accept_offer(league_id, call["market_id"], call["offer_id"], call["money"])
    if plan.kind == "decline":
        return await client.decline_offer(league_id, call["market_id"], call["offer_id"])
    if plan.kind == "withdraw":
        return await client.withdraw_player_from_market(league_id, call["market_id"])
    raise ActionRefused(f"Unknown action {plan.kind}.")


def describe_request(result: WriteResult, settings: Settings) -> str:
    path = result.url.removeprefix(settings.api_base_url.rstrip("/"))
    body = json.dumps(result.body) if result.body is not None else "(no body)"
    return f"{result.method} {path}\n{body}"
