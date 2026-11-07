"""The kickoff guard: runs hourly, says nothing unless points are about to be lost.

Within 24h of a jornada's first match it checks the things that silently
cost points: a negative balance (0 points for the jornada), an incomplete
lineup, injured or suspended starters, slots holding players you sold. It
alerts once when it first sees a problem, and once more 3h before kickoff if
the problem is still there.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from ..alerts import Alerter
from ..api import FantasyClient
from ..auth.store import write_private_file
from ..config import Settings
from .analysis import assess_readiness, best_lineup, money, next_kickoff, read_lineup, sell_to_cover
from .briefing import gather

STATE_FILE = "guard.json"
KEEP_KEYS = 50


async def run_guard(client: FantasyClient, settings: Settings, alerter: Alerter, *,
                    now: datetime | None = None, send: bool = True, window_hours: float = 24,
                    final_hours: float = 3,
                    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> str | None:
    """Returns the alert text if one was due (and sent, unless send=False)."""
    now = now or datetime.now(timezone.utc)
    view = await gather(client, settings, now, rivals=False, market=False, details=False,
                        activity=False, sleep=sleep)
    kickoff = next_kickoff(view.matches, view.week, now)
    if kickoff is None or kickoff.in_progress:
        return None
    hours_left = (kickoff.first_match - now).total_seconds() / 3600
    if hours_left > window_hours:
        return None
    squad = view.my_squad
    current = read_lineup(view.lineup, squad)
    ready = assess_readiness(view.balance, current, squad, kickoff)
    if ready.ready:
        return None

    stage = "final" if hours_left <= final_hours else "early"
    key = "|".join([str(kickoff.jornada), stage, *sorted(ready.problems)])
    state_path = settings.state_dir / STATE_FILE
    try:
        sent: list[str] = json.loads(state_path.read_text()).get("sent", [])
    except (FileNotFoundError, ValueError):
        sent = []
    if key in sent:
        return None

    lines = [f"⏰ Jornada {kickoff.jornada} starts in {hours_left:.0f}h ({view.when(kickoff.first_match)}) "
             "and you'd lose points:"]
    lines += [f" • {p}" for p in ready.problems]
    if view.balance is not None and view.balance < 0:
        plan = best_lineup(squad, view.formations)
        keep = (plan.starters + ([plan.coach] if plan.coach else [])) if plan else []
        to_sell = sell_to_cover(-view.balance, squad, keep)
        if to_sell:
            lines.append(" Bench players whose value covers the "
                         f"{money(-view.balance)}: " + ", ".join(f"{p.name} ({money(p.value)})"
                                                                for p in to_sell))
    lines.append("Fix it in the app before kickoff.")
    message = "\n".join(lines)
    if send:  # a dry run (--no-send) must not stop the real alert later
        await alerter.send(message)
        write_private_file(state_path, json.dumps({"sent": (sent + [key])[-KEEP_KEYS:]}).encode())
    return message
