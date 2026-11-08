from __future__ import annotations

from fantasybot.api.models import ActivityEntry
from fantasybot.scout.cash import estimate, net_by_manager


def entry(type_id: int, actor: int, amount: int, other: int | None = None, text: str = ""):
    return ActivityEntry(activityTypeId=type_id, user1Id=actor, user2Id=other, amount=amount,
                         description=text or None)


def test_net_spending_by_manager():
    net = net_by_manager([
        entry(1, 1, 10), entry(31, 1, 5), entry(33, 1, 4), entry(6, 1, 2),  # 1: -10 -5 +4 +2
        entry(32, 2, 7, other=3),  # 2 steals from 3: 3 is paid the clause
        entry(1, 2, 3, other=3, text="Ana ha clausulado a X"),  # older feeds: type 1 + "clausul"
        entry(4, 3, 0), entry(9, 4, 100),  # shield (no money) and joining: ignored
    ])
    assert net == {"1": -9, "2": -10, "3": 10}


def test_estimates_are_calibrated_on_your_own_cash():
    feed = [entry(1, 1, 6), entry(33, 2, 5), entry(1, 3, 8)]
    est = estimate(feed, my_manager_id=1, my_cash=4, manager_ids=[1, 2, 3, 4])
    assert est.unexplained == 10  # 4 real - (-6) from the feed
    assert est.by_manager == {"2": 15, "3": 2, "4": 10}
    assert est.get(1) is None  # you're not a rival


def test_no_feed_or_no_cash_means_no_estimate():
    assert estimate([], 1, 4, [1, 2]) is None
    assert estimate([entry(1, 1, 6)], 1, None, [1, 2]) is None
