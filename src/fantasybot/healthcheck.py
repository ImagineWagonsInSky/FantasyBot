"""Daily health check: call every read endpoint and check its shape.

The sweep discovers the IDs it needs as it goes (user → league → your team →
its players → …). Each endpoint is checked independently; when a call fails,
the checks that needed its result are reported as skipped rather than as
more failures.

It starts by redeeming the refresh token. That tests the refresh flow every
day, and keeps the refresh token from expiring while the bot is idle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from .alerts import Alerter
from .api import ApiError, FantasyClient, ResponseShapeError
from .api.models import same_id
from .auth import AuthUnavailable, TokenManager
from .config import Settings
from .external import ExternalData, ExternalError
from .redact import redact

log = logging.getLogger(__name__)

Status = Literal["ok", "fail", "skip", "warn"]


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str = ""
    duration_ms: int = 0


@dataclass
class HealthReport:
    results: list[CheckResult] = field(default_factory=list)

    def _with(self, status: Status) -> list[CheckResult]:
        return [r for r in self.results if r.status == status]

    @property
    def failures(self) -> list[CheckResult]:
        return self._with("fail")

    @property
    def warnings(self) -> list[CheckResult]:
        return self._with("warn")

    @property
    def checks(self) -> list[CheckResult]:
        return [r for r in self.results if r.status != "warn"]

    @property
    def healthy(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        total = len(self.checks)
        if self.failures:
            return f"{len(self.failures)} of {total} checks failed"
        passed = len(self._with("ok"))
        tail = f", {len(self.warnings)} warning(s)" if self.warnings else ""
        skipped = total - passed
        return f"{passed} of {total} checks passed" + (f" ({skipped} skipped)" if skipped else "") + tail

    def console_text(self) -> str:
        icons = {"ok": "OK  ", "fail": "FAIL", "skip": "SKIP", "warn": "WARN"}
        lines = []
        for r in self.results:
            timing = f" ({r.duration_ms} ms)" if r.status in ("ok", "fail") else ""
            detail = f"  {r.detail}" if r.detail else ""
            lines.append(f"{icons[r.status]}  {r.name:<24}{timing}{detail}")
        lines.append("")
        lines.append(self.summary())
        return "\n".join(lines)

    def alert_text(self) -> str:
        head = "🔴" if self.failures else "🟡"
        lines = [f"{head} FantasyBot health check: {self.summary()}"]
        lines += [f"✗ {r.name}: {r.detail}" for r in self.failures]
        blocked = [r.name for r in self._with("skip") if r.detail.startswith("needs ")]
        if blocked:
            lines.append(f"⏭ not checked because of the above: {', '.join(blocked)}")
        lines += [f"⚠️ {r.detail}" for r in self.warnings]
        return "\n".join(lines)


class HealthCheck:
    def __init__(
        self,
        settings: Settings,
        tokens: TokenManager,
        client: FantasyClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        external: ExternalData | None = None,
    ) -> None:
        self._external = external
        self._settings = settings
        self._tokens = tokens
        self._client = client
        self._sleep = sleep
        self._clock = clock
        self._monotonic = monotonic

    async def run(self) -> HealthReport:
        report = HealthReport()
        ctx: dict[str, Any] = {}
        c = self._client

        async def check(name: str, needs: tuple[str, ...], call: Callable[[], Awaitable[Any]]) -> Any:
            missing = [n for n in needs if ctx.get(n) is None]
            if missing:
                report.results.append(CheckResult(name, "skip", f"needs {', '.join(missing)}"))
                return None
            started = self._monotonic()
            try:
                value = await call()
            except _Skip as skip:
                report.results.append(CheckResult(name, "skip", str(skip)))
                return None
            except Exception as exc:  # each check is isolated; report and move on
                report.results.append(CheckResult(name, "fail", _describe(exc),
                                                  self._elapsed(started)))
                if not isinstance(exc, (ApiError, ResponseShapeError, AuthUnavailable)):
                    log.exception("unexpected error in check %s", name)
                return None
            finally:
                if self._settings.request_delay:
                    await self._sleep(self._settings.request_delay)
            report.results.append(CheckResult(name, "ok", "", self._elapsed(started)))
            return value

        # Auth first: a daily refresh keeps the refresh token alive.
        bundle = await check("auth_refresh", (), self._tokens.force_refresh)
        if bundle is None:
            try:
                await self._tokens.bearer()  # frozen or failed, but reads may still work
                ctx["auth"] = True
            except AuthUnavailable:
                pass
        else:
            ctx["auth"] = True
            self._warn_refresh_expiry(report, bundle.refresh_token_expires_at)

        user = await check("current_user", ("auth",), c.get_current_user)
        if user is not None:
            ctx["user_id"] = user.user_id

        async def pick_league() -> Any:
            leagues = await c.get_leagues()
            wanted = self._settings.league_id
            if wanted:
                if not any(same_id(lg.id, wanted) for lg in leagues):
                    raise ApiError("leagues", f"configured league {wanted} is not on this account")
                ctx["league_id"] = wanted
            elif leagues:
                ctx["league_id"] = leagues[0].id
            else:
                raise ApiError("leagues", "this account is in no leagues")
            return leagues

        await check("leagues", ("auth",), pick_league)

        week = await check("current_week", ("auth",), c.get_current_week)
        if week is not None:
            ctx["week"] = week.number
            ctx["last_week"] = max(1, week.number - 1)  # a finished week always has data

        async def find_team() -> Any:
            rows = await c.get_league_ranking(ctx["league_id"])
            if self._settings.team_id:
                ctx["team_id"] = self._settings.team_id
            else:
                mine = next((r for r in rows if same_id(r.userId, ctx.get("user_id"))
                             or (r.team.manager and same_id(r.team.manager.id, ctx.get("user_id")))),
                            None)
                if mine is None:
                    raise ApiError("league_ranking", "your team is not in the league standing "
                                   "(set FANTASYBOT_TEAM_ID to skip discovery)")
                ctx["team_id"] = mine.team.id
            return rows

        team_needs = ("auth", "league_id") if self._settings.team_id else ("auth", "league_id", "user_id")
        await check("league_ranking", team_needs, find_team)
        await check("league_ranking_by_week", ("auth", "league_id", "last_week"),
                    lambda: c.get_league_ranking_by_week(ctx["league_id"], ctx["last_week"]))
        await check("league_activity", ("auth", "league_id"),
                    lambda: c.get_league_activity(ctx["league_id"], 0))

        async def team() -> Any:
            data = await c.get_team_data(ctx["league_id"], ctx["team_id"])
            owned = [p for p in data.players if p.player_team_id is not None]
            if owned:
                ctx["player_team_id"] = owned[0].player_team_id
                ctx["player_id"] = owned[0].playerMaster.id
            return data

        await check("team_data", ("auth", "league_id", "team_id"), team)
        await check("team_money", ("auth", "team_id"), lambda: c.get_team_money(ctx["team_id"]))
        await check("current_lineup", ("auth", "team_id"), lambda: c.get_current_lineup(ctx["team_id"]))
        await check("team_lineup", ("auth", "team_id", "week"),
                    lambda: c.get_team_lineup(ctx["team_id"], ctx["week"]))
        await check("free_formations", ("auth",), c.get_free_formations)
        await check("premium_formations", ("auth",), c.get_premium_formations)
        await check("premium_configuration", ("auth",), c.get_premium_configuration)

        async def market() -> Any:
            items = await c.get_market(ctx["league_id"])
            # Offers are only fetched for your own listings that have offers,
            # as LaLigaApp does; there may be none today.
            with_offers = next((i for i in items if i.playerTeam and i.playerTeam.playerTeamId
                                and same_id(i.seller_team_id, ctx.get("team_id"))
                                and (i.numberOfOffers or 0) > 0), None)
            ctx["offer_player_team_id"] = with_offers.playerTeam.playerTeamId if with_offers else None
            ctx["market_checked"] = True
            return items

        await check("market", ("auth", "league_id"), market)
        await check("players", ("auth",), c.get_all_players)
        await check("teams_master", ("auth",), c.get_teams_master)
        await check("matchday", ("auth", "week"), lambda: c.get_matchday(ctx["week"]))
        await check("match_stats", ("auth", "last_week"), lambda: c.get_match_stats(ctx["last_week"]))

        async def offers() -> Any:
            if ctx.get("offer_player_team_id") is None:
                raise _Skip("none of your listed players has offers right now")
            return await c.get_player_offer(ctx["league_id"], ctx["offer_player_team_id"])

        await check("player_offer", ("auth", "league_id", "market_checked"), offers)
        await check("player_details", ("auth", "league_id", "player_id"),
                    lambda: c.get_player_details(ctx["player_id"], ctx["league_id"]))
        await check("check_player_shield", ("auth", "league_id", "player_team_id"),
                    lambda: c.check_player_shield(ctx["league_id"], ctx["player_team_id"]))
        await self._outside(report)
        return report

    async def _outside(self, report: HealthReport) -> None:
        """Outside sources are optional: one breaking is a warning, never a failure."""
        ext = self._external
        if ext is None:
            return
        sources = [("odds_api", "The Odds API", ext.odds_enabled, ext.odds_status),
                   ("football_data", "football-data.org", ext.results_enabled, ext.football_data_status),
                   ("lineups", "Probable lineups", ext.lineups_enabled, ext.lineups_status)]
        for name, label, enabled, call in sources:
            if not enabled:
                report.results.append(CheckResult(name, "skip", "off" if name == "lineups"
                                                  else "not set up (optional)"))
                continue
            started = self._monotonic()
            try:
                detail = await call()
            except ExternalError as exc:
                report.results.append(CheckResult(name, "warn", f"{label}: {exc.message}"))
                continue
            report.results.append(CheckResult(name, "ok", detail, self._elapsed(started)))
            if name == "odds_api" and (ext.credits() or 999) < 40:
                report.results.append(CheckResult(
                    "odds_api_credits", "warn",
                    f"The Odds API: only {ext.credits_left} credits left this month; the briefing "
                    "falls back to results-based difficulty when they run out"))

    def _elapsed(self, started: float) -> int:
        return int((self._monotonic() - started) * 1000)

    def _warn_refresh_expiry(self, report: HealthReport, expires_at: int | None) -> None:
        if not expires_at:
            return
        days = (expires_at - self._clock()) / 86400
        if days < self._settings.refresh_token_warn_days:
            when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(expires_at))
            report.results.append(CheckResult(
                "refresh_token_expiry", "warn",
                f"the LaLiga refresh token expires {when} (in {max(days, 0):.1f} days); "
                "import a fresh token soon with `fantasybot auth import`"))


class _Skip(Exception):
    """Raised by a check that has nothing to test today."""


def _describe(exc: Exception) -> str:
    if isinstance(exc, ApiError):
        return exc.message
    if isinstance(exc, ResponseShapeError):
        return f"unexpected shape: {exc.problem}"
    if isinstance(exc, AuthUnavailable):
        return f"auth: {exc}"
    return redact(f"{type(exc).__name__}: {exc}")[:300]


async def run_and_alert(check: HealthCheck, alerter: Alerter, *, alert: bool = True) -> HealthReport:
    report = await check.run()
    if alert and (report.failures or report.warnings):
        await alerter.send(report.alert_text())
    return report
