"""Estimates of rivals' cash, which the API hides.

The usual way to work out a rival's money:

    starting budget − purchases + sales + 100,000 € × season points

* Jornada earnings come from the standings (EARN_PER_POINT per point), not
  from the feed: the feed may only show your own payouts.
* Purchases, sales and clause steals come from the league activity feed.
  When a manager buys from another manager, the seller is credited too,
  unless the feed also has the seller's own "sold" entry for that deal.
* What neither shows (the starting budget, daily rewards, clause raises) is
  worked out from your own team, where the real cash is known, and assumed
  to be the same for everyone.

Nobody can be more in debt than 20% of their team value. An estimate below
that is impossible, so the whole set is marked implausible instead of shown.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from ..api.models import ActivityEntry

SPENDS = {1, 31, 32}  # bought, signed, clause steal: user1 pays
SOLD = 33  # sold: user1 receives
EARNINGS = 6  # jornada payout: taken from the standings instead
CLAUSE_STEAL = 32
EARN_PER_POINT = 100_000
DEBT_LIMIT_SHARE = 0.20
DRIFT_LIMIT = 2_000_000  # a day-to-day change in the unexplained part beyond this is suspicious


def is_clause_steal(entry: ActivityEntry) -> bool:
    text = (entry.description or "").lower()
    return entry.activityTypeId == CLAUSE_STEAL or (entry.activityTypeId == 1 and "clausul" in text)


@dataclass
class Ledger:
    bought: int = 0
    sold: int = 0
    clauses_paid: int = 0
    clauses_received: int = 0

    @property
    def net(self) -> int:
        return self.sold + self.clauses_received - self.bought - self.clauses_paid


def ledgers(entries: Iterable[ActivityEntry]) -> dict[str, Ledger]:
    entries = list(entries)
    own_sales = {(str(e.user1Id), str(e.playerMasterId), e.amount) for e in entries
                 if e.activityTypeId == SOLD and e.user1Id is not None}
    books: dict[str, Ledger] = {}

    def book(manager: object) -> Ledger:
        return books.setdefault(str(manager), Ledger())

    for e in entries:
        if not e.amount or e.user1Id is None:
            continue
        if e.activityTypeId in SPENDS or is_clause_steal(e):
            steal = is_clause_steal(e)
            if steal:
                book(e.user1Id).clauses_paid += e.amount
            else:
                book(e.user1Id).bought += e.amount
            other = e.user2Id
            if (other is not None and str(other) != str(e.user1Id)
                    and (str(other), str(e.playerMasterId), e.amount) not in own_sales):
                if steal:
                    book(other).clauses_received += e.amount
                else:
                    book(other).sold += e.amount
        elif e.activityTypeId == SOLD:
            book(e.user1Id).sold += e.amount
    return books


def net_by_manager(entries: Iterable[ActivityEntry]) -> dict[str, int]:
    return {m: ledger.net for m, ledger in ledgers(entries).items()}


@dataclass
class CashEstimates:
    by_manager: dict[str, int]  # manager id -> estimated cash (rivals only)
    unexplained: int  # starting budget, rewards and the like, from your own cash
    entries: int
    uncertain: bool = False  # the unexplained part jumped since yesterday
    implausible: list[str] = field(default_factory=list)  # managers estimated beyond the debt limit

    @property
    def usable(self) -> bool:
        return not self.implausible

    def get(self, manager_id: object) -> int | None:
        if manager_id is None or not self.usable:
            return None
        return self.by_manager.get(str(manager_id))


def estimate(entries: list[ActivityEntry], my_manager_id: object, my_cash: int | None,
             points: Mapping[object, float], team_values: Mapping[object, int | None] | None = None,
             earn_per_point: int = EARN_PER_POINT) -> CashEstimates | None:
    """points: season points per manager id (from the standing)."""
    if my_cash is None or my_manager_id is None:
        return None
    net = net_by_manager(entries)
    earned = {str(m): int(earn_per_point * max(p or 0, 0)) for m, p in points.items()}
    me = str(my_manager_id)
    unexplained = my_cash - net.get(me, 0) - earned.get(me, 0)
    rivals = {m: net.get(m, 0) + earned[m] + unexplained for m in earned if m != me}
    values = {str(k): v for k, v in (team_values or {}).items()}
    impossible = [m for m, cash in rivals.items()
                  if values.get(m) and cash < -DEBT_LIMIT_SHARE * values[m]]
    return CashEstimates(rivals, unexplained, len(entries), implausible=impossible)


def feed_summary(entries: list[ActivityEntry]) -> dict[str, object]:
    """For `fantasybot cash-check`: what the feed contains."""
    types = Counter(e.activityTypeId for e in entries)
    anonymous = Counter(e.activityTypeId for e in entries if e.user1Id is None)
    dates = sorted(e.createdAt for e in entries if e.createdAt)
    return {"entries": len(entries), "types": dict(sorted(types.items())),
            "without_manager": dict(sorted(anonymous.items())),
            "first": dates[0] if dates else None, "last": dates[-1] if dates else None}
