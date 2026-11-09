"""The morning run: the briefing, the plan, and what gets sent.

With a plan (an Anthropic key stored, FANTASYBOT_ADVISOR on), you get one
message: what to do today, each action with its button. The full briefing
is kept for /details, which the listener answers. Without the listener (or
with FANTASYBOT_BRIEFING_STYLE=both) the full briefing follows the plan.

Without a plan, or when it can't be made today, the full briefing and its
buttons go out as before, with a line saying why there's no plan.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .actions.core import KIND_NAMES, suggest
from .actions.flow import ActionCenter
from .advisor import AdvisorError, DailyPlan, make_plan, render
from .alerts import Alerter
from .api import FantasyClient
from .config import Settings
from .external import ExternalData
from .scout.briefing import LeagueView, briefing, remember

ALL_KINDS = ",".join(KIND_NAMES)


@dataclass
class Daily:
    messages: list[str]  # the full briefing
    view: LeagueView
    plan: DailyPlan | None = None
    plan_text: str | None = None
    problem: str | None = None  # why there's no plan today


async def prepare(client: FantasyClient, settings: Settings, alerter: Alerter, *,
                  external: ExternalData | None = None, now: datetime | None = None, with_plan: bool = True,
                  sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                  http_client: Any = None) -> Daily:
    messages, view = await briefing(client, settings, alerter, now=now, send=False, external=external,
                                    sleep=sleep)
    daily = Daily(messages, view)
    if not with_plan or settings.advisor_key() is None:
        return daily
    # Every kind is a candidate, so the plan can advise even on actions you don't take from
    # Telegram; only the kinds you've switched on get buttons.
    everything = settings.model_copy(update={"actions": True, "action_kinds": ALL_KINDS})
    try:
        daily.plan = await make_plan(view, suggest(view, everything, view.now), settings,
                                     http_client=http_client)
    except AdvisorError as exc:
        daily.problem = exc.message
        return daily
    daily.plan_text = render(daily.plan, view)
    return daily


def plan_only(daily: Daily, settings: Settings, center: ActionCenter | None) -> bool:
    """Only the plan goes out when /details can be answered (the listener is in use)."""
    return daily.plan is not None and center is not None and settings.briefing_style == "plan"


async def deliver(daily: Daily, settings: Settings, alerter: Alerter, center: ActionCenter | None) -> None:
    only = plan_only(daily, settings, center)
    if daily.plan is not None and daily.plan_text is not None:
        if center is not None:
            footer = "\n/details for the full briefing" if only else ""
            await center.offer_plan(daily.plan_text + footer, daily.plan.picks)
        else:
            await alerter.send(daily.plan_text)
    if not only:
        messages = list(daily.messages)
        if daily.problem:
            messages.append(f"🧠 No plan today: {daily.problem}")
        for message in messages:
            await alerter.send(message)
        if center is not None and daily.plan is None:
            await center.offer(daily.view)
    remember(settings, daily.view, daily.messages)
