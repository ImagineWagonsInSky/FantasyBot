"""Suggested actions: what the briefing offers, the live checks, and carrying them out.

Four kinds, all things you'd otherwise do in the app:
  lineup  save the suggested best XI
  bid     bid on a market player (minimum, +5% or +10%)
  steal   pay a rival player's buyout clause
  raise   raise one of your exposed players' clauses

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

from ..api import FantasyClient, LineupUpdate, WriteResult
from ..api.models import same_id
from ..config import Settings
from ..scout.analysis import (
    Rival,
    best_lineup,
    clause_radar,
    credit_limit,
    money,
    next_kickoff,
    parse_time,
    rank_market,
    read_lineup,
)
from ..scout.briefing import LeagueView, exposure_order, takers
from ..scout.model import DEF, FWD, GK, MID
from .store import ActionStore, Proposal

KIND_NAMES = {"lineup": "lineup change", "bid": "bid", "steal": "clause steal", "raise": "clause raise"}
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
        for t in clause_radar(rivals, view.balance, now).affordable[:3]:
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
            amounts = sorted({_round_up(pick.price * s, 10_000) for s in BID_STEPS})
            if limit is not None:
                amounts = [a for a in amounts if a <= limit]
            if not amounts:
                continue
            name = pick.player.display_name
            expires = parse_time(pick.item.expirationDate) or now + timedelta(hours=12)
            out.append(Suggestion("bid", f"bid on {name}", {
                **base, "market_id": pick.item.id, "player_id": pick.player.id, "name": name,
                "amounts": amounts,
                "options": [f"🛒 {name} {money(amounts[0])}"] + [money(a) for a in amounts[1:]],
            }, expires))
            offered += 1

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

    raise ActionRefused(f"Unknown action {kind}.")


def check_limits(plan: Plan, settings: Settings, store: ActionStore, today: Any) -> None:
    if plan.money > settings.action_max_money:
        raise ActionRefused(f"{money(plan.money)} is over your per-action limit "
                            f"({money(settings.action_max_money)}, FANTASYBOT_ACTION_MAX_MONEY). "
                            "Do it in the app, or raise the limit.")
    if not settings.dry_run and store.live_actions_on(today) >= settings.action_daily_limit:
        raise ActionRefused(f"That's today's limit of {settings.action_daily_limit} real actions "
                            "(FANTASYBOT_ACTION_DAILY_LIMIT).")


async def execute(plan: Plan, client: FantasyClient, league_id: Any, team_id: Any) -> WriteResult:
    call = plan.call
    if plan.kind == "steal":
        return await client.pay_buyout_clause(league_id, call["player_team_id"], call["amount"])
    if plan.kind == "bid":
        return await client.make_bid(league_id, call["market_id"], call["amount"])
    if plan.kind == "raise":
        return await client.increase_buyout_clause(league_id, call["player_team_id"], 2.0, call["increase"])
    if plan.kind == "lineup":
        return await client.update_lineup(team_id, LineupUpdate(**call["lineup"]))
    raise ActionRefused(f"Unknown action {plan.kind}.")


def describe_request(result: WriteResult, settings: Settings) -> str:
    path = result.url.removeprefix(settings.api_base_url.rstrip("/"))
    body = json.dumps(result.body) if result.body is not None else "(no body)"
    return f"{result.method} {path}\n{body}"
