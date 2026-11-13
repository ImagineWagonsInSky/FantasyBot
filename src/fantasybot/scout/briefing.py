"""The daily briefing: everything worth acting on today, in one Telegram message.

Only reads from LaLiga. Each piece of data is fetched independently, so one
failing endpoint drops one section instead of the whole briefing.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, TypeVar

from ..alerts import Alerter
from ..api import ApiError, FantasyClient, ResponseShapeError
from ..api.models import ActivityEntry, Id, Lineup, MarketItem, Match, Offer, Player, StandingEntry, same_id
from ..auth import AuthUnavailable
from ..config import Settings
from ..external import ExternalData, ExternalError
from ..external.football_data import DataCheck, cross_check, parse_matches, season_of
from ..external.lineups import LineupEntry, match_players
from ..external.names import KNOWN_CLUBS, club_key
from ..external.odds import MatchOdds, match_odds
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
from .difficulty import HORIZON, FixtureModel, club_records, player_club
from .history import PriceHistory
from .model import COACH, DEF, FWD, GK, MID, POSITION_LABELS, FixtureOutlook, position_label, position_of
from .rivals import (
    Intel,
    build_profiles,
    competition,
    gap_story,
    last_finished,
    projected_line,
    swing_targets,
)
from .selling import (
    SQUAD_LIMIT,
    OfferVerdict,
    bidder_name,
    judge,
    offer_ids_to_check,
    on_sale,
    sell_candidates,
    shop_window,
    should_withdraw,
)

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
    offers: dict[str, list[Offer]] = field(default_factory=dict)  # by your playerTeamId
    settings: Settings | None = None
    change_3d: dict[str, int] = field(default_factory=dict)  # value change by player id
    club_names: dict[str, str] = field(default_factory=dict)  # club id -> short name
    odds: dict[tuple[str, str], MatchOdds] = field(default_factory=dict)  # (home, away) club ids
    data_check: DataCheck | None = None  # LaLiga Fantasy's calendar vs football-data.org
    lineups: dict[str, LineupEntry] = field(default_factory=dict)  # player id -> probable lineup entry
    lineup_clubs: int = 0  # clubs whose probable lineup was read
    unmatched: list[tuple[str, str]] = field(default_factory=list)  # (club id, name) of starters not found
    intel: Intel | None = None  # what the rivals are up to (rivals.py)

    def keep(self) -> list[SquadPlayer]:
        """The best XI plus coach: who not to sell."""
        plan = best_lineup(self.my_squad, self.formations) if self.my_squad else None
        return (plan.starters + ([plan.coach] if plan.coach else [])) if plan else []

    def rival_cash(self, entry: StandingEntry) -> int | None:
        if self.cash is None or entry.team.manager is None:
            return None
        return self.cash.get(entry.team.manager.id)

    def outlook_fn(self) -> Callable[[Player], FixtureOutlook] | None:
        if self.fixtures is None and not self.lineups:
            return None
        fixtures, lineups = self.fixtures, self.lineups

        def outlook(player: Player) -> FixtureOutlook:
            base = fixtures.outlook(player) if fixtures else FixtureOutlook()
            entry = lineups.get(str(player.id))
            return dataclasses.replace(base, start=entry.chance) if entry else base

        return outlook

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


async def read_lineups(view: LeagueView, external: ExternalData, relevant: Iterable[Player],
                       aliases: dict[str, tuple[str | None, ...]], *, refresh: bool) -> None:
    """Probable lineups for the clubs of the given players (yours and the market's)."""
    by_club: dict[str, list[Player]] = {}
    for p in view.players or relevant:  # every LaLiga player when the list was fetched
        club = player_club(p)
        if club is not None:
            by_club.setdefault(club, []).append(p)
    clubs = list(dict.fromkeys(c for c in (player_club(p) for p in relevant) if c is not None))
    failed: list[str] = []
    for club in clubs:
        names = list(aliases.get(club, ()))
        ref = next((r for w in view.matches.values() for m in w for r in (m.local, m.visitor)
                    if r is not None and str(r.id) == club), None)
        if ref is not None:
            names += [ref.name, ref.shortName, ref.slug]
        slug = club_key(*names)
        if slug not in KNOWN_CLUBS:
            continue
        lineup = await outside("probable lineups", lambda s=slug: external.lineup(s, refresh=refresh),
                               lambda s=slug: external.lineup(s, refresh=False), failed)
        if lineup is None:
            continue
        view.lineup_clubs += 1
        matched = match_players(lineup, by_club.get(club, []))
        view.lineups.update(matched)
        view.unmatched += [(club, e.nickname) for e in lineup.entries
                           if e.starter and not any(e is m for m in matched.values())]
    if failed:
        more = f" (and {len(failed) - 1} more clubs)" if len(failed) > 1 else ""
        view.errors.append(failed[0] + more)


async def outside(label: str, fetch: Callable[[], Awaitable[T]], fallback: Callable[[], Awaitable[T]],
                  errors: list[str]) -> T | None:
    """Outside data never breaks a run: on failure, use the last copy and say so."""
    try:
        return await fetch()
    except ExternalError as exc:
        try:
            cached = await fallback()
        except ExternalError:
            cached = None
        errors.append(f"{label}: {exc.message}" + (" (using the last copy)" if cached else ""))
        return cached


def biddable(item: MarketItem, settings: Settings | None) -> bool:
    """LaLiga's own listings always; another manager's listing only with Premium."""
    return item.discr != "marketPlayerTeam" or bool(settings and settings.premium)


def _describe(exc: Exception) -> str:
    if isinstance(exc, ApiError):
        return exc.message
    if isinstance(exc, ResponseShapeError):
        return f"unexpected shape ({exc.problem})"
    return str(exc)


async def gather(client: FantasyClient, settings: Settings, now: datetime, *, rivals: bool = True,
                 market: bool = True, details: bool = True, activity: bool = True,
                 results: bool = True, external: ExternalData | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> LeagueView:
    """Everything the briefing (or, with most flags off, the guard) needs.

    results=False uses only match results and outside data cached by earlier
    runs, so the hourly guard stays cheap and spends no API credits."""
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
    view = LeagueView(now, league_id, standing, me, week, errors=errors, tz=settings.display_tz(),
                      settings=settings)

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
    aliases: dict[str, tuple[str | None, ...]] = {}
    if results:
        clubs = await fetch("club names", client.get_teams_master, [])
        names = {str(c.id): c.shortName or c.name or str(c.id) for c in clubs}
        aliases = {str(c.id): (c.name, c.shortName, c.slug) for c in clubs}
    for m in (m for w in view.matches.values() for m in w):  # matches carry club names too
        for ref in (m.local, m.visitor):
            if ref is not None and ref.id is not None and (ref.shortName or ref.name):
                names.setdefault(str(ref.id), ref.shortName or ref.name or "")
    view.club_names = names
    if external is not None and external.odds_enabled:
        events = await outside("odds", lambda: external.odds_events(refresh=results),
                               lambda: external.odds_events(refresh=False), errors)
        if events:
            upcoming = [m for w in sorted(view.matches) for m in view.matches[w]]
            view.odds = match_odds(events, upcoming, aliases)
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
        played = store.results_by_week()
    if external is not None and external.results_enabled:
        season = season_of(now.astimezone(view.tz))
        body = await outside("real fixtures", lambda: external.real_matches(season, refresh=results),
                             lambda: external.real_matches(season, refresh=False), errors)
        if body:
            view.data_check = cross_check(parse_matches(body), view.matches, played, now, aliases, view.when)
    kickoff = next_kickoff(view.matches, week, now)
    view.fixtures = FixtureModel(club_records(known_results, now), view.matches,
                                 kickoff.jornada if kickoff else week, names, odds=view.odds,
                                 postponed=view.data_check.postponed if view.data_check else ())
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
            points = {e.team.manager.id: e.points or 0 for e in standing if e.team.manager}
            values = {e.team.manager.id: e.teamValue or e.team.teamValue
                      for e in standing if e.team.manager}
            view.cash = estimate(store.activity(), my_manager, view.balance, points, values,
                                 settings.earn_per_point)
            if view.cash is not None:
                unexplained = view.cash.unexplained
                previous = store.record_offset(now.astimezone(view.tz).date(), unexplained)
                view.cash.uncertain = previous is not None and abs(previous - unexplained) > DRIFT_LIMIT

    if external is not None and external.lineups_enabled:
        mine = team_data.get(view.my_team_id)
        relevant = [p.playerMaster for p in (mine.players if mine else [])]
        relevant += [i.playerMaster for i in view.market]
        await read_lineups(view, external, relevant, aliases, refresh=results)

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

    if market:
        for ptid in offer_ids_to_check(view.market, view.my_squad, me.team.id)[:SQUAD_LIMIT]:
            view.offers[str(ptid)] = await fetch(
                "offers", lambda p=ptid: client.get_player_offer(league_id, p), [])
    if rivals and activity:
        view.intel = await gather_intel(client, settings, view, fetch, sleep)
    return view


async def gather_intel(client: FantasyClient, settings: Settings, view: LeagueView,
                       fetch: Callable[..., Awaitable[Any]],
                       sleep: Callable[[float], Awaitable[None]]) -> Intel:
    """The rivals' picture: profiles, bid competition, swing steals, trend (rivals.py)."""
    saved = {}
    for e in view.standing:
        if same_id(e.team.id, view.me.team.id):
            continue
        try:  # LaLiga may not show a rival's saved lineup; that's fine, it's optional
            lineup = await client.get_current_lineup(e.team.id)
        except (ApiError, ResponseShapeError, AuthUnavailable, ValueError):
            lineup = None
        finally:
            if settings.request_delay:
                await sleep(settings.request_delay)
        squad = view.squads.get(str(e.team.id), [])
        current = read_lineup(lineup, squad) if lineup is not None and squad else None
        if current is not None and len(current.members) >= 9:  # really theirs, and mostly set
            saved[str(e.team.id)] = current
    with PriceHistory(settings.state_dir / "history.sqlite") as store:
        if view.week is not None:
            stored = store.weeks_stored()
            for w in range(max(1, view.week - 5), view.week):
                if w in stored and w != view.week - 1:  # the last one may still get late points
                    continue
                rows = await fetch(f"jornada {w} standings",
                                   lambda w=w: client.get_league_ranking_by_week(view.league_id, w), None)
                if rows:
                    store.store_week(w, {str(r.team.id): float(r.points or 0) for r in rows})
        trend = store.weekly_points()
        profiles = build_profiles(view.standing, view.me, view.squads, store.activity(), view.cash,
                                  view.market, view.formations, view.now, store.value_near, saved)
        recap = last_finished(trend, view.week)
        if recap is not None and store.note("recap_week") == str(recap):
            recap = None
    team_value = view.me.teamValue or view.me.team.teamValue
    limit = credit_limit(view.balance, team_value)
    picks = rank_market(view.market, view.details, view.week, view.balance, limit, {}, {},
                        exclude_team=view.me.team.id, outlook=view.outlook_fn())
    contested = {}
    for pick in [p for p in picks if p.projection.outlook > 0 and biddable(p.item, settings)][:8]:
        contested[str(pick.item.id)] = competition(pick.item, pick.player.display_name,
                                                   position_of(pick.player), pick.price, profiles, limit)
    rivals = [Rival(e, view.rank_of(e), view.squads.get(str(e.team.id), []))
              for e in view.standing if not same_id(e.team.id, view.me.team.id)]
    swings = swing_targets(rivals, profiles, view.my_squad, view.balance, view.formations, view.now)
    mine = read_lineup(view.lineup, view.my_squad) if view.my_squad else None
    plan = best_lineup(view.my_squad, view.formations) if view.my_squad else None
    my_projection = (mine.expected if mine is not None and mine.filled >= 11
                     else plan.expected if plan else None)
    return Intel(profiles, contested, swings, my_projection, trend, recap)


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
    if view.cash is not None and not view.cash.usable:
        lines.append("Rivals' cash: the estimate doesn't add up today (it puts someone past the "
                     "debt limit), so it isn't used. `./fb cash-check` shows why.")
    elif view.cash is not None:
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


def _odds(view: LeagueView) -> str:
    """The next jornada as the bookmakers see it, favourites first."""
    kickoff = next_kickoff(view.matches, view.week, view.now)
    if not view.odds or kickoff is None:
        return ""
    mine = {player_club(p.player) for p in view.my_squad}
    rows = []
    for m in view.matches.get(kickoff.jornada, []):
        home = m.localId if m.localId is not None else (m.local.id if m.local else None)
        away = m.visitorId if m.visitorId is not None else (m.visitor.id if m.visitor else None)
        odds = view.odds.get((str(home), str(away)))
        if odds is None:
            continue

        def club(c: object) -> str:
            return view.club_names.get(str(c), str(c)) + ("★" if str(c) in mine else "")

        rows.append((max(odds.p_home, odds.p_away),
                     f" {club(home)} {odds.p_home:.0%} · draw {odds.p_draw:.0%} · {club(away)} "
                     f"{odds.p_away:.0%} (goals {odds.home_goals:.1f}–{odds.away_goals:.1f})"))
    if not rows:
        return ""
    rows.sort(key=lambda r: -r[0])
    return "\n".join([f"🎲 Jornada {kickoff.jornada} by the odds (★ = your players' clubs):"]
                     + [r for _, r in rows])


def _lineups(view: LeagueView) -> str:
    """Your squad against the probable lineups: who's likely benched, where sources disagree."""
    if not view.lineup_clubs:
        return ""
    # Your saved lineup is what scores; without one, the suggested XI.
    saved = read_lineup(view.lineup, view.my_squad).members if view.my_squad else []
    plan = best_lineup(view.my_squad, view.formations) if view.my_squad and not saved else None
    watched = {str(p.player.id) for p in (saved or (plan.starters if plan else []))}
    benched, disagree = [], []
    for p in view.my_squad:
        entry = view.lineups.get(str(p.player.id))
        if entry is None:
            continue
        status = (p.player.playerStatus or "ok").lower()
        if status in ("injured", "suspended") and entry.chance >= 0.6:
            disagree.append(f"{p.name} (LaLiga: {status}; futbolfantasy: {entry.chance:.0%} to start)")
        elif status == "ok" and entry.status == "injured":
            disagree.append(f"{p.name} (LaLiga: fit; futbolfantasy: injured)")
        elif str(p.player.id) in watched and entry.chance < 0.6:
            benched.append(f"{p.name} {entry.chance:.0%}")
    mine = {player_club(p.player) for p in view.my_squad}
    missed = [name for club, name in view.unmatched if club in mine]
    header = f"📋 Probable lineups (futbolfantasy.com, {view.lineup_clubs} clubs)"
    if not (benched or disagree or missed):
        return f"{header}: your likely starters should start; it's in the expected points."
    lines = [f"{header}:"]
    if benched:
        lines.append(" In your lineup but may not start: " + ", ".join(benched)
                     + ". The Best XI above already accounts for it")
    if disagree:
        lines.append(" Sources disagree: " + "; ".join(disagree) + ". Check before kickoff")
    if missed:
        lines.append(f" Couldn't match to LaLiga players: {', '.join(missed[:6])}")
    return "\n".join(lines)


CHECK_LABELS = {"postponed": "Not being played", "kickoff": "Kickoff moved",
                "score": "Result differs", "missing": "Result missing in LaLiga Fantasy"}


def _data_check(view: LeagueView) -> str:
    check = view.data_check
    if check is None:
        return ""
    unpaired = f" ({check.unpaired} couldn't be paired by club name)" if check.unpaired else ""
    if not check.problems:
        return (f"🔎 football-data.org agrees with LaLiga Fantasy: {check.fixtures} upcoming fixtures, "
                f"{check.results} recent results{unpaired}.")
    owners: dict[str, list[str]] = {}
    for p in view.my_squad:
        owners.setdefault(str(player_club(p.player)), []).append(p.name)
    lines = [f"🔎 LaLiga Fantasy vs football-data.org{unpaired}:"]
    for p in check.problems:
        match = f"{view.club_names.get(p.home, p.home)} v {view.club_names.get(p.away, p.away)}"
        line = f" • {CHECK_LABELS[p.kind]}: {match} (jornada {p.jornada})"
        if p.kind == "postponed":
            yours = owners.get(p.home, []) + owners.get(p.away, [])
            line += (f": {p.real}. Points only come at a later recierre, so they count half here"
                     + (f". Yours: {', '.join(yours)}" if yours else ""))
        else:
            line += f": LaLiga Fantasy {p.fantasy}, football-data.org {p.real}"
            if p.kind == "kickoff":
                line += ". Check the lineup deadline in the app"
        lines.append(line)
    return "\n".join(lines[:9])


def _ago(view: LeagueView, when: datetime | None) -> str:
    if when is None:
        return "no moves seen"
    days = (view.now - when).days
    return "last move today" if days < 1 else f"last move {days}d ago"


def _rivals(view: LeagueView) -> str:
    intel = view.intel
    if intel is None or not intel.profiles:
        return ""
    lines = ["🕵️ Rivals"]
    projected = projected_line(intel)
    if projected:
        saved = any(r.saved_xi is not None for r in intel.profiles)
        lines.append(f" Next jornada, projected: {projected}"
                     + (" (* their saved lineup)" if saved else " (each fielding their best XI)"))
    recent = sorted(w for w in intel.trend if view.week is None or w < view.week)[-3:]
    if recent:
        me = view.my_team_id

        def edge(team: str) -> float:  # their points minus yours over those jornadas
            return sum(intel.trend[w].get(team, 0) - intel.trend[w].get(me, 0) for w in recent)

        diffs = " · ".join(f"{r.name} {edge(r.team_id):+.0f}" for r in intel.profiles)
        lines.append(f" Last {len(recent)} jornadas, their points minus yours: {diffs}")
    for r in intel.profiles:
        bits = [f"#{r.rank} ({r.gap:+.0f} vs you)"]
        if r.cash is not None:
            bits.append(f"≈{money(r.cash)}")
        if r.need_text():
            bits.append(r.need_text())
        if r.injured:
            bits.append("out: " + ", ".join(r.injured))
        if r.overpay is not None:
            bits.append(f"pays {r.overpay:+.0%} over value ({len(r.premiums)} buys)")
        bits.append(_ago(view, r.last_move) + (f", {r.moves} moves in 14d" if r.moves >= 3 else ""))
        if r.steals_made:
            bits.append(f"{r.steals_made} clause steal{'s' if r.steals_made > 1 else ''}"
                        + (f" ({r.steals_from_you} from you)" if r.steals_from_you else ""))
        if r.listings:
            bits.append("selling " + ", ".join(f"{i.playerMaster.display_name} {money(i.salePrice)}"
                                               for i in r.listings[:2]))
        line = f" • {r.name}: " + " · ".join(bits)
        if r.in_red:
            line += " · ⚠ likely in the red: scores 0 unless they sell, so their players may hit the market"
        lines.append(line)
    for c in [c for c in intel.competition.values() if c.bidders][:3]:
        who = ", ".join(f"{b.rival.name} ({'; '.join(b.reasons)})" for b in c.bidders)
        tail = (f"bid {money(c.to_win)} to beat them" if c.to_win
                else "they can outbid your max" if c.out_of_reach else "")
        lines.append(f" Bidding on {c.name}: {who}" + (f" · {tail}" if tail else ""))
    if intel.swings:
        lines.append(" Best clause steals by swing: " + " · ".join(
            f"{s.target.player.name} from {s.target.rival.name} {s.swing:+.1f} xP/j "
            f"(you +{s.your_gain:.1f}" + (f", them −{s.their_loss:.1f}" if s.their_loss else "")
            + f") for {money(s.target.cost)}"
            for s in intel.swings[:3]))
    return "\n".join(lines)


def _recap(view: LeagueView) -> str:
    intel = view.intel
    if intel is None or intel.recap_week is None:
        return ""
    week = intel.recap_week
    points = intel.trend.get(week, {})
    me = view.my_team_id
    scores = [f"You {points.get(me, 0):.0f}"] + [f"{r.name} {points.get(r.team_id, 0):.0f}"
                                                 for r in intel.profiles]
    gaps = []
    for r in intel.profiles:
        before, now = gap_story(intel.trend, week, me, r.team_id, r.gap)
        gaps.append(f"{r.name} {before:+.0f} → {now:+.0f}")
    return (f"📊 Jornada {week} recap: " + " · ".join(scores)
            + "\n Gap (their points minus yours): " + " · ".join(gaps))


def _market(view: LeagueView, team_value: int | None, d1: dict[str, int], d3: dict[str, int]) -> str:
    if not view.market:
        return "🛒 Market: nothing listed, or it couldn't be loaded."
    limit = credit_limit(view.balance, team_value)
    picks = rank_market(view.market, view.details, view.week, view.balance, limit, d1, d3,
                        exclude_team=view.me.team.id, outlook=view.outlook_fn())
    closes = min((d for d in (parse_time(i.expirationDate) for i in view.market
                              if i.discr == "marketPlayerLeague") if d), default=None)
    useful = [p for p in picks if p.projection.outlook > 0]
    premium_only = [p for p in useful if not biddable(p.item, view.settings)]
    useful = [p for p in useful if biddable(p.item, view.settings)]
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
    if premium_only:
        lines.append(" Listed by managers (bidding needs Premium): " + ", ".join(
            f"{p.player.display_name} ({money(p.price)}, {p.projection.outlook:.1f} xP/j)"
            for p in premium_only[:3]))
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
    settings = view.settings or Settings()
    if settings.shields:
        top = ", ".join(p.name for p in at_risk[:2])
        lines.append(f" Shield {top} in the app. Raising a clause costs half the increase: "
                     "+10M costs 5M.")
    else:
        lines.append(" Protect one by raising his clause: it costs half the increase (+10M costs 5M).")
    return "\n".join(lines)


def _vs_value(amount: int | None, value: int) -> str:
    if not amount or not value:
        return ""
    return f" ({(amount / value - 1) * 100:+.0f}% vs value)"


def verdicts(view: LeagueView) -> list[OfferVerdict]:
    """Best offer per player on sale, judged against your thresholds; best premium first."""
    settings = view.settings or Settings()
    keep = view.keep()
    found = [judge(s, keep, view.change_3d, settings.sell_threshold, settings.starter_sell_threshold)
             for s in on_sale(view.market, view.my_squad, view.offers, view.me.team.id)]
    return sorted((v for v in found if v is not None), key=lambda v: -v.premium)


def _selling(view: LeagueView, history: PriceHistory | None = None) -> str:
    squad = view.my_squad
    if not squad:
        return "💸 Selling: your squad couldn't be loaded."
    settings = view.settings or Settings()
    keep = view.keep()
    sales = on_sale(view.market, squad, view.offers, view.me.team.id)
    judged = {str(v.sale.player_team_id): v for v in verdicts(view)}
    full = " (full: sell before you can buy)" if len(squad) >= SQUAD_LIMIT else ""
    lines = [f"💸 Selling · squad {len(squad)}/{SQUAD_LIMIT}{full}"]
    ordered = sorted(sales, key=lambda s: -(judged[str(s.player_team_id)].premium
                                            if str(s.player_team_id) in judged else -9))
    for s in ordered[:8]:
        status = f"listed {money(s.listed_price)}" if s.listed_price is not None else "not listed"
        closes = f" · closes {view.when(s.expires)}" if s.expires else ""
        best = s.best
        offer = (f"best offer {money(best.money)} from {bidder_name(best, view.standing)}"
                 f"{_vs_value(best.money, s.value)}") if best else "no offers yet"
        v = judged.get(str(s.player_team_id))
        mark = ""
        if v is not None and v.worth:
            mark = " · ⭐ worth taking" + (f" ({v.note})" if v.note else "")
        lines.append(f" • {s.name} · {status} (value {money(s.value)}){closes} · {offer}{mark}")
        why = should_withdraw(s, keep, settings.shop_window == "everyone")
        if why:
            lines.append(f"   ↩ think about withdrawing: {why}")
    if len(sales) > 8:
        lines.append(f"   … and {len(sales) - 8} more listed")
    stats = history.market_offer_range() if history else None
    if stats and stats[0] >= 5:
        count, low, avg, high = stats
        lines.append(f" LaLiga's offers so far: {low:+.0%} to {high:+.0%} of value "
                     f"(average {avg:+.1%}, {count} seen)")
    window = shop_window(squad, keep, sales, settings.shop_window)
    if window:
        lines.append(f" 🪟 {len(window)} player(s) not listed today: list them so LaLiga makes an offer "
                     f"on each (accepting is always your call).")
    candidates = sell_candidates(squad, keep, view.balance, view.change_3d, sales)
    if candidates:
        lines.append(" Worth selling (not in your best XI):")
        lines += [f" • {c.player.label} · value {money(c.player.value)} · {c.reason}" for c in candidates]
    if len(lines) == 1:
        lines.append(" • nothing listed, no offers, nothing worth selling")
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
    view.change_3d = d3
    days = len(history.days())
    sections = [
        _header(view, team_value),
        _recap(view),
        _lineup_and_readiness(view),
        _lineups(view),
        _odds(view),
        _data_check(view),
        _market(view, team_value, d1, d3),
        _clauses(view),
        _exposure(view),
        _selling(view, history),
        _rivals(view),
        _movers(view, history, days),
        _rival_moves(view),
    ]
    if view.errors:
        sections.append("ℹ️ Missing today: " + "; ".join(view.errors[:4]))
    return pack([s for s in sections if s])


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
                   now: datetime | None = None, send: bool = True, external: ExternalData | None = None,
                   sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
                   ) -> tuple[list[str], LeagueView]:
    """Gathers, records today's prices, composes and (optionally) sends the briefing."""
    now = now or datetime.now(timezone.utc)
    view = await gather(client, settings, now, external=external, sleep=sleep)
    with PriceHistory(settings.state_dir / "history.sqlite") as history:
        today = now.astimezone(view.tz).date()
        if view.players:
            history.record(today, view.players)
        for sale in on_sale(view.market, view.my_squad, view.offers, view.me.team.id):
            for offer in sale.offers:
                if offer.money and sale.value:
                    history.store_offer(today, offer.id, sale.player.player.id if sale.player else None,
                                        offer.money, sale.value, offer.bidderTeam is None)
        messages = compose(view, history)
    if send:
        remember(settings, view, messages)
        for message in messages:
            await alerter.send(message)
    return messages, view


def remember(settings: Settings, view: LeagueView, messages: list[str]) -> None:
    """After a briefing goes out: recap each jornada once, keep the text for /details."""
    with PriceHistory(settings.state_dir / "history.sqlite") as history:
        if view.intel is not None and view.intel.recap_week is not None:
            history.set_note("recap_week", str(view.intel.recap_week))
        history.set_note("last_briefing", json.dumps({"at": view.now.isoformat(), "messages": messages}))


def last_briefing(settings: Settings) -> tuple[datetime, list[str]] | None:
    with PriceHistory(settings.state_dir / "history.sqlite") as history:
        raw = history.note("last_briefing")
    if not raw:
        return None
    data = json.loads(raw)
    return datetime.fromisoformat(data["at"]), list(data["messages"])


async def run_briefing(client: FantasyClient, settings: Settings, alerter: Alerter, *,
                       now: datetime | None = None, send: bool = True, external: ExternalData | None = None,
                       sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> list[str]:
    messages, _ = await briefing(client, settings, alerter, now=now, send=send, external=external,
                                 sleep=sleep)
    return messages
