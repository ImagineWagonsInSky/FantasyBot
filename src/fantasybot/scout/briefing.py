"""The daily briefing: everything worth acting on today, in one Telegram message.

Only reads from LaLiga. Each piece of data is fetched independently, so one
failing endpoint drops one section instead of the whole briefing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, TypeVar

from ..alerts import Alerter
from ..api import ApiError, FantasyClient, ResponseShapeError
from ..api.models import ActivityEntry, Id, Lineup, MarketItem, Match, Player, StandingEntry, same_id
from ..auth import AuthUnavailable
from ..config import Settings
from .analysis import (
    Rival,
    SquadPlayer,
    assess_readiness,
    best_lineup,
    build_squad,
    clause_radar,
    credit_limit,
    exposed,
    find_my_entry,
    manager_name,
    money,
    next_kickoff,
    parse_formations,
    parse_time,
    rank_market,
    read_lineup,
    sell_to_cover,
)
from .cash import DRIFT_LIMIT, CashEstimates, estimate
from .difficulty import HORIZON, FixtureModel, club_records
from .history import PriceHistory
from .model import COACH, DEF, FWD, GK, MID, POSITION_LABELS, FixtureOutlook, position_label

log = logging.getLogger(__name__)
T = TypeVar("T")

TELEGRAM_CHUNK = 3800
MAX_DETAIL_CALLS = 45
MAX_ACTIVITY_PAGES = 40  # the first run reads the whole feed; later runs stop at known entries
ACTIVITY_LABELS = {1: "bought", 4: "shielded", 7: "invalid lineup", 9: "joined the league",
                   31: "signed", 32: "clause steal", 33: "sold"}


class BriefingError(Exception):
    """The basics (user, league, standing) couldn't be fetched."""


@dataclass
class LeagueView:
    now: datetime
    league_id: Id
    standing: list[StandingEntry]
    me: StandingEntry
    week: int | None
    squads: dict[str, list[SquadPlayer]] = field(default_factory=dict)
    balance: int | None = None
    lineup: Lineup | None = None
    matches: dict[int, list[Match]] = field(default_factory=dict)
    formations: list[tuple[int, int, int]] = field(default_factory=list)
    market: list[MarketItem] = field(default_factory=list)
    details: dict[str, Player] = field(default_factory=dict)
    players: list[Player] = field(default_factory=list)
    activity: list[ActivityEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    tz: tzinfo = timezone.utc
    fixtures: FixtureModel | None = None
    cash: CashEstimates | None = None

    def rival_cash(self, entry: StandingEntry) -> int | None:
        if self.cash is None or entry.team.manager is None:
            return None
        return self.cash.get(entry.team.manager.id)

    def outlook_fn(self) -> Callable[[Player], FixtureOutlook] | None:
        return self.fixtures.outlook if self.fixtures else None

    def when(self, dt: datetime | None) -> str:
        return when(dt, self.tz)

    @property
    def my_team_id(self) -> str:
        return str(self.me.team.id)

    @property
    def my_squad(self) -> list[SquadPlayer]:
        return self.squads.get(self.my_team_id, [])

    def rank_of(self, entry: StandingEntry) -> int:
        ordered = sorted(self.standing, key=lambda e: -(e.points or 0))
        return next(i for i, e in enumerate(ordered, 1) if same_id(e.team.id, entry.team.id))


def _describe(exc: Exception) -> str:
    if isinstance(exc, ApiError):
        return exc.message
    if isinstance(exc, ResponseShapeError):
        return f"unexpected shape ({exc.problem})"
    return str(exc)


async def gather(client: FantasyClient, settings: Settings, now: datetime, *, rivals: bool = True,
                 market: bool = True, details: bool = True, activity: bool = True,
                 results: bool = True,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> LeagueView:
    """Everything the briefing (or, with most flags off, the guard) needs.

    results=False uses only match results cached by earlier runs, so the
    hourly guard stays cheap."""
    errors: list[str] = []

    async def fetch(label: str, call: Callable[[], Awaitable[T]], default: T) -> T:
        try:
            return await call()
        except (ApiError, ResponseShapeError, ValueError) as exc:
            errors.append(f"{label}: {_describe(exc)}")
            return default
        finally:
            if settings.request_delay:
                await sleep(settings.request_delay)

    try:
        user = await client.get_current_user()
        leagues = await client.get_leagues()
        league_id = settings.league_id or (leagues[0].id if leagues else None)
        if league_id is None:
            raise BriefingError("this account is in no leagues")
        standing = await client.get_league_ranking(league_id)
    except (ApiError, ResponseShapeError, AuthUnavailable) as exc:
        raise BriefingError(_describe(exc)) from None
    me = find_my_entry(standing, user.user_id, settings.team_id)
    if me is None:
        raise BriefingError("your team is not in the league standing (set FANTASYBOT_TEAM_ID)")

    week_info = await fetch("current week", client.get_current_week, None)
    week = week_info.number if week_info else None
    view = LeagueView(now, league_id, standing, me, week, errors=errors, tz=settings.display_tz())

    teams = {view.my_team_id: me}
    if rivals:
        teams.update({str(e.team.id): e for e in standing})
    team_data = {}
    for team_id in teams:
        team_data[team_id] = await fetch(f"squad of {manager_name(teams[team_id])}",
                                         lambda t=team_id: client.get_team_data(league_id, t), None)
    view.balance = getattr(await fetch("your balance",
                                       lambda: client.get_team_money(view.my_team_id), None), "amount", None)
    view.lineup = await fetch("your lineup", lambda: client.get_current_lineup(view.my_team_id), None)
    if week is not None:
        for w in range(week, week + HORIZON):
            view.matches[w] = await fetch(f"jornada {w} fixtures", lambda w=w: client.get_matchday(w), [])
    names: dict[str, str] = {}
    if results:
        clubs = await fetch("club names", client.get_teams_master, [])
        names = {str(c.id): c.shortName or c.name or str(c.id) for c in clubs}
    with PriceHistory(settings.state_dir / "history.sqlite") as store:
        if results and week is not None:
            finished = store.finished_weeks()
            for w in range(1, week):
                if w not in finished:
                    past = await fetch(f"jornada {w} results", lambda w=w: client.get_matchday(w), None)
                    if past is not None:
                        store.store_results(w, past)
        for w, played in view.matches.items():
            store.store_results(w, played)
        known_results = store.results()
    kickoff = next_kickoff(view.matches, week, now)
    view.fixtures = FixtureModel(club_records(known_results, now), view.matches,
                                 kickoff.jornada if kickoff else week, names)
    view.formations = parse_formations(await fetch("formations", client.get_free_formations, []))
    if market:
        view.market = await fetch("market", lambda: client.get_market(league_id), [])
        view.players = await fetch("players", client.get_all_players, [])
    if activity:
        with PriceHistory(settings.state_dir / "history.sqlite") as store:
            for page in range(MAX_ACTIVITY_PAGES):
                entries = await fetch("activity", lambda p=page: client.get_league_activity(league_id, p),
                                      None)
                if not entries:
                    break
                if page == 0:
                    view.activity = entries
                if store.store_activity(entries) == 0:
                    break  # caught up with what earlier runs stored
            my_manager = me.team.manager.id if me.team.manager else user.user_id
            managers = [e.team.manager.id for e in standing if e.team.manager]
            view.cash = estimate(store.activity(), my_manager, view.balance, managers)
            if view.cash is not None:
                unexplained = view.cash.unexplained
                previous = store.record_offset(now.astimezone(view.tz).date(), unexplained)
                view.cash.uncertain = previous is not None and abs(previous - unexplained) > DRIFT_LIMIT

    if details:
        wanted: list[Id] = []
        mine = team_data.get(view.my_team_id)
        wanted += [p.playerMaster.id for p in (mine.players if mine else [])]
        wanted += [i.playerMaster.id for i in view.market]
        for pid in list(dict.fromkeys(str(p) for p in wanted))[:MAX_DETAIL_CALLS]:
            result = await fetch("player details",
                                 lambda p=pid: client.get_player_details(p, league_id), None)
            if result is not None:
                view.details[pid] = result.playerMaster

    for team_id, data in team_data.items():
        if data is not None:
            view.squads[team_id] = build_squad(data, view.details, week, view.outlook_fn())
    return view


# -- rendering -----------------------------------------------------------------


def when(dt: datetime | None, tz: tzinfo | None = None) -> str:
    if dt is None:
        return "?"
    return (dt.astimezone(tz) if tz else dt).strftime("%a %d %b %H:%M")


def _arrow(delta: int | None) -> str:
    if not delta:
        return ""
    return f" · {'↑' if delta > 0 else '↓'}{money(abs(delta))}"


def _names(players: list[SquadPlayer]) -> str:
    def one(p: SquadPlayer) -> str:
        fixture = f", {p.projection.fixture}" if p.projection.fixture else ""
        return f"{p.name} ({p.expected:.1f}{fixture})"

    return ", ".join(one(p) for p in players) or "-"


def _header(view: LeagueView, team_value: int | None) -> str:
    ordered = sorted(view.standing, key=lambda e: -(e.points or 0))
    leader = ordered[0]
    table = " · ".join(
        f"{i}. {'You' if same_id(e.team.id, view.me.team.id) else manager_name(e)} {e.points or 0:.0f}"
        for i, e in enumerate(ordered, 1))
    gap = (leader.points or 0) - (view.me.points or 0)
    behind = f" · {gap:.0f} behind {manager_name(leader)}" if gap > 0 else " · you're leading"
    limit = credit_limit(view.balance, team_value)
    lines = [
        f"☀️ FantasyBot · jornada {view.week or '?'} · {view.now.astimezone(view.tz).strftime('%a %d %b')}",
        table + behind,
        f"Cash {money(view.balance)} · max bid {money(limit)} · team value {money(team_value)}",
    ]
    if view.cash is not None:
        rivals = [e for e in ordered if not same_id(e.team.id, view.me.team.id)]
        est = " · ".join(f"{manager_name(e)} {money(view.rival_cash(e))}" for e in rivals
                         if view.rival_cash(e) is not None)
        if est:
            lines.append(f"Rivals' cash ≈ {est}" + (" (uncertain today)" if view.cash.uncertain else ""))
    return "\n".join(lines)


def _lineup_and_readiness(view: LeagueView) -> str:
    squad = view.my_squad
    if not squad:
        return "🔁 Lineup: your squad couldn't be loaded."
    plan = best_lineup(squad, view.formations)
    current = read_lineup(view.lineup, squad)
    kickoff = next_kickoff(view.matches, view.week, view.now)
    ready = assess_readiness(view.balance, current, squad, kickoff)
    lines = []
    deadline = (f"jornada {kickoff.jornada} {'in progress' if kickoff.in_progress else 'starts'} "
                f"{view.when(kickoff.first_match)}" if kickoff else "next jornada: fixtures unavailable")
    if ready.ready:
        lines.append(f"✅ Ready: {deadline}.")
    else:
        lines.append(f"⚠️ Not ready ({deadline}):")
        lines += [f" • {p}" for p in ready.problems]
    lines += [f" · {w}" for w in ready.warnings]
    if view.balance is not None and view.balance < 0 and plan:
        to_sell = sell_to_cover(-view.balance, squad, plan.starters + ([plan.coach] if plan.coach else []))
        if to_sell:
            lines.append(" Bench players whose value covers it: "
                         + ", ".join(f"{p.name} ({money(p.value)})" for p in to_sell))
    if plan is None:
        lines.append("🔁 Not enough players for any formation.")
        return "\n".join(lines)
    gain = plan.expected - current.expected
    best_ids = {str(p.player.id) for p in plan.starters}
    current_ids = {str(p.player.id) for p in current.members}
    if gain < 0.5 and best_ids <= current_ids:
        lines.append(f"🔁 Your lineup is already the best by expected points ({plan.expected:.1f}).")
    else:
        lines.append(f"🔁 Best XI {plan.formation_label()}: {plan.expected:.1f} expected pts "
                     f"({gain:+.1f} vs your saved lineup)")
        out = [p for p in current.members if str(p.player.id) not in best_ids]
        new = [p for p in plan.starters if str(p.player.id) not in current_ids]
        if out or new:
            lines.append(f" Out: {_names(out)}  →  In: {_names(new)}")
    parts = [f"{POSITION_LABELS[pos]} " + ", ".join(p.name for p in plan.by_position[pos])
             for pos in (GK, DEF, MID, FWD)]
    if plan.coach:
        parts.append(f"{POSITION_LABELS[COACH]} {plan.coach.name}")
    lines.append(" " + " · ".join(parts))
    return "\n".join(lines)


def _market(view: LeagueView, team_value: int | None, d1: dict[str, int], d3: dict[str, int]) -> str:
    if not view.market:
        return "🛒 Market: nothing listed, or it couldn't be loaded."
    limit = credit_limit(view.balance, team_value)
    picks = rank_market(view.market, view.details, view.week, view.balance, limit, d1, d3,
                        exclude_team=view.me.team.id, outlook=view.outlook_fn())
    closes = min((d for d in (parse_time(i.expirationDate) for i in view.market
                              if i.discr == "marketPlayerLeague") if d), default=None)
    useful = [p for p in picks if p.projection.outlook > 0]
    reachable = [p for p in useful if p.payable_cash or p.payable_credit or limit is None]
    lines = [f"🛒 Market{' · closes ' + view.when(closes) if closes else ''} "
             "(best 5 you can bid on, by expected points over the next 3 jornadas)"]
    if not reachable:
        lines.append(" • nothing you can bid on right now")
    for n, p in enumerate(reachable[:5], 1):
        flag = "✓" if p.payable_cash else ("⚠ credit" if p.payable_credit else "?")
        form = f" / {p.projection.form:.1f} form" if p.projection.form is not None else ""
        nxt = f" · next {p.projection.fixture}" if p.projection.fixture else ""
        bids = f" · {p.item.numberOfBids} bids" if p.item.numberOfBids else ""
        seller = " · from a manager" if p.item.discr == "marketPlayerTeam" else ""
        note = f" · {p.projection.note}" if p.projection.note else ""
        lines.append(f" {n}. {p.player.display_name} {position_label(p.player)} · "
                     f"{money(p.price)} · {p.projection.outlook:.1f} xP/j{nxt} · "
                     f"{p.projection.season_average:.1f} avg{form}"
                     f"{_arrow(p.change_3d)}{bids}{seller}{note} · {flag}")
    lines.append(" ✓ payable from cash · ⚠ needs credit: be back above 0 by kickoff")
    over = [p for p in useful if p not in reachable][:3]
    if over:
        lines.append(" Over your max bid: " + ", ".join(
            f"{p.player.display_name} ({money(p.price)}, {p.projection.outlook:.1f} xP/j)" for p in over))
    return "\n".join(lines)


def _clauses(view: LeagueView) -> str:
    rivals = [Rival(e, view.rank_of(e), view.squads.get(str(e.team.id), []))
              for e in view.standing if not same_id(e.team.id, view.me.team.id)]
    radar = clause_radar(rivals, view.balance, view.now)
    lines = [f"🎯 Clause steals you can pay now (cash {money(view.balance)}, no debt allowed)"]
    for t in radar.affordable[:5]:
        lines.append(f" • {t.player.label} from {t.rival.name} (#{t.rival.rank}) · clause {money(t.cost)} "
                     f"(value {money(t.player.value)}) · {_xp(t.player)}")
    if not radar.affordable:
        lines.append(" • none right now")
    if radar.unlocking_soon:
        lines.append(" Unlocking within 24h:")
        for t in radar.unlocking_soon[:3]:
            lines.append(f" • {t.player.label} from {t.rival.name} · {view.when(t.player.locked_until)} "
                         f"· clause {money(t.cost)} · {_xp(t.player)}")
    if radar.out_of_reach:
        best = radar.out_of_reach[0]
        short = best.cost - (view.balance or 0)
        lines.append(f" Best open target: {best.player.label} from {best.rival.name} · clause "
                     f"{money(best.cost)} · {money(short)} more cash needed")
    return "\n".join(lines)


def _xp(p: SquadPlayer) -> str:
    nxt = f", next {p.projection.fixture}" if p.projection.fixture else ""
    return f"{p.outlook:.1f} xP/j{nxt}"


def takers(view: LeagueView, clause: int) -> list[tuple[StandingEntry, int]]:
    """Rivals whose estimated cash covers a clause (clauses can't be paid on debt)."""
    out = []
    for e in view.standing:
        cash = view.rival_cash(e)
        if not same_id(e.team.id, view.me.team.id) and cash is not None and cash >= clause:
            out.append((e, cash))
    return out


def exposure_order(view: LeagueView) -> list[SquadPlayer]:
    """Exposed players, those a rival can actually afford first."""
    at_risk = exposed(view.my_squad, view.now)
    if view.cash is None:
        return at_risk
    return sorted(at_risk, key=lambda p: (not takers(view, p.clause or p.value), -p.outlook))


def _exposure(view: LeagueView) -> str:
    at_risk = exposure_order(view)
    if not at_risk:
        return "🛡️ None of your players can be taken by clause right now."
    lines = ["🛡️ Your players any rival can take right now:"]
    for p in at_risk[:5]:
        reach = ""
        if view.cash is not None:
            who = takers(view, p.clause or p.value)
            reach = (" · within reach of " + ", ".join(f"{manager_name(e)} (≈{money(c)})" for e, c in who)
                     if who else " · no rival can afford it (est.)")
        lines.append(f" • {p.label} · clause {money(p.clause)} (value {money(p.value)}) · {_xp(p)}{reach}")
    top = ", ".join(p.name for p in at_risk[:2])
    lines.append(f" Shield {top} in the app (free, 2 players, 24h). Raising a clause costs half "
                 "the increase: +10M costs 5M.")
    return "\n".join(lines)


def _movers(view: LeagueView, history: PriceHistory, days_recorded: int) -> str:
    if days_recorded < 2:
        return "📈 Price history started today; trends appear from tomorrow."
    today = view.now.astimezone(view.tz).date()
    mine = {str(p.player.id) for p in view.my_squad}
    up, down = history.movers(today, 1, 3, mine)
    lines = ["📈 Your players since yesterday: "
             + (", ".join(f"{m.name} +{money(m.delta)}" for m in up) or "no risers")
             + " | " + (", ".join(f"{m.name} {money(m.delta)}" for m in down) or "no fallers")]
    up, down = history.movers(today, 1, 3)
    lines.append(" League: " + (", ".join(f"{m.name} +{money(m.delta)}" for m in up) or "-")
                 + " | " + (", ".join(f"{m.name} {money(m.delta)}" for m in down) or "-"))
    return "\n".join(lines)


def _rival_moves(view: LeagueView) -> str:
    since = view.now - timedelta(hours=24)
    lines = []
    for entry in view.activity:
        created = parse_time(entry.createdAt) or parse_time(entry.timestamp if isinstance(
            entry.timestamp, str) else None)
        if created is None or created < since or entry.activityTypeId == 6:
            continue
        text = _extra(entry, "description")
        if not text:
            label = ACTIVITY_LABELS.get(entry.activityTypeId, f"activity {entry.activityTypeId}")
            player = _extra(entry, "playerName") or ""
            text = f"{label} {player} {money(entry.amount) if entry.amount else ''}".strip()
        lines.append(f" • {text}")
    if not lines:
        return "👀 No league moves in the last 24h."
    return "\n".join(["👀 League moves in the last 24h:"] + lines[:6])


def _extra(model: Any, name: str) -> str | None:
    value = getattr(model, name, None)
    return str(value).strip() if value not in (None, "") else None


def compose(view: LeagueView, history: PriceHistory) -> list[str]:
    team_value = view.me.teamValue or view.me.team.teamValue
    today = view.now.astimezone(view.tz).date()
    d1, d3 = history.change(today, 1), history.change(today, 3)
    days = len(history.days())
    sections = [
        _header(view, team_value),
        _lineup_and_readiness(view),
        _market(view, team_value, d1, d3),
        _clauses(view),
        _exposure(view),
        _movers(view, history, days),
        _rival_moves(view),
    ]
    if view.errors:
        sections.append("ℹ️ Missing today: " + "; ".join(view.errors[:4]))
    return pack(sections)


def pack(sections: list[str], limit: int = TELEGRAM_CHUNK) -> list[str]:
    messages, current = [], ""
    for section in sections:
        section = section[:limit]
        candidate = f"{current}\n\n{section}" if current else section
        if len(candidate) > limit:
            messages.append(current)
            current = section
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


async def briefing(client: FantasyClient, settings: Settings, alerter: Alerter, *,
                   now: datetime | None = None, send: bool = True,
                   sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
                   ) -> tuple[list[str], LeagueView]:
    """Gathers, records today's prices, composes and (optionally) sends the briefing."""
    now = now or datetime.now(timezone.utc)
    view = await gather(client, settings, now, sleep=sleep)
    with PriceHistory(settings.state_dir / "history.sqlite") as history:
        if view.players:
            history.record(now.astimezone(view.tz).date(), view.players)
        messages = compose(view, history)
    if send:
        for message in messages:
            await alerter.send(message)
    return messages, view


async def run_briefing(client: FantasyClient, settings: Settings, alerter: Alerter, *,
                       now: datetime | None = None, send: bool = True,
                       sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> list[str]:
    messages, _ = await briefing(client, settings, alerter, now=now, send=send, sleep=sleep)
    return messages
