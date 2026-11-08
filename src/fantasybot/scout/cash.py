"""Estimates of rivals' cash, which the API hides.

Every purchase, sale, clause steal and jornada payout appears in the league
activity feed with an amount and the managers involved, so each manager's
net spending can be added up. What the feed can't show (the starting budget,
daily rewards, clause raises) is estimated from your own team: your real
cash minus your own feed total. That same amount is assumed for everyone.

These are estimates. If the unexplained part of your own cash jumps from one
day to the next, the feed is missing something and the estimates are marked
uncertain.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..api.models import ActivityEntry

SPENDS = {1, 31, 32}  # bought, signed, clause steal: user1 pays
EARNS = {6, 33}  # jornada earnings, sold: user1 receives
CLAUSE_STEAL = 32
DRIFT_LIMIT = 2_000_000  # a day-to-day change in the unexplained part beyond this is suspicious


def is_clause_steal(entry: ActivityEntry) -> bool:
    text = (entry.description or "").lower()
    return entry.activityTypeId == CLAUSE_STEAL or (entry.activityTypeId == 1 and "clausul" in text)


def net_by_manager(entries: Iterable[ActivityEntry]) -> dict[str, int]:
    net: dict[str, int] = {}
    for e in entries:
        if not e.amount or e.user1Id is None:
            continue
        actor = str(e.user1Id)
        if e.activityTypeId in SPENDS or is_clause_steal(e):
            net[actor] = net.get(actor, 0) - e.amount
            if is_clause_steal(e) and e.user2Id is not None:  # the clause is paid to the owner
                owner = str(e.user2Id)
                net[owner] = net.get(owner, 0) + e.amount
        elif e.activityTypeId in EARNS:
            net[actor] = net.get(actor, 0) + e.amount
    return net


@dataclass
class CashEstimates:
    by_manager: dict[str, int]  # manager id -> estimated cash (rivals only)
    unexplained: int  # the part of your own cash the feed doesn't account for
    entries: int
    uncertain: bool = False

    def get(self, manager_id: object) -> int | None:
        return self.by_manager.get(str(manager_id)) if manager_id is not None else None


def estimate(entries: list[ActivityEntry], my_manager_id: object, my_cash: int | None,
             manager_ids: Iterable[object]) -> CashEstimates | None:
    if my_cash is None or my_manager_id is None or not entries:
        return None
    net = net_by_manager(entries)
    me = str(my_manager_id)
    unexplained = my_cash - net.get(me, 0)
    rivals = {str(m): net.get(str(m), 0) + unexplained for m in manager_ids if str(m) != me}
    return CashEstimates(rivals, unexplained, len(entries))
