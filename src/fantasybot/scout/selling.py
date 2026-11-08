"""Selling: your listings, offers on your players, and who's worth selling.

Offers reach you two ways: on players you've listed, and as direct offers on
players you haven't. Both show up as market entries with you as the seller,
and in your squad data (playerMarket); get_player_offer has the details.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from ..api.models import Id, MarketItem, Offer, StandingEntry, same_id
from .analysis import SquadPlayer, manager_name, parse_time, sell_to_cover

IDLE_OUTLOOK = 1.0  # bench players expected to score less than this per jornada


@dataclass
class OnSale:
    """One of your players that's listed, or has offers without being listed."""

    player_team_id: Id
    player: SquadPlayer | None
    market_id: Id | None  # what accept/decline/withdraw need
    listed_price: int | None  # None: not listed, only direct offers
    expires: datetime | None
    offers: list[Offer] = field(default_factory=list)

    @property
    def best(self) -> Offer | None:
        open_offers = [o for o in self.offers if o.open and o.money]
        return max(open_offers, key=lambda o: o.money or 0) if open_offers else None

    @property
    def name(self) -> str:
        return self.player.name if self.player else f"player {self.player_team_id}"

    @property
    def value(self) -> int:
        return self.player.value if self.player else 0


def offer_ids_to_check(market: Iterable[MarketItem], squad: Iterable[SquadPlayer],
                       my_team: Id) -> list[Id]:
    """playerTeamIds of your players that have offers waiting."""
    ids: dict[str, Id] = {}
    for item in market:
        if (item.discr == "marketPlayerTeam" and same_id(item.seller_team_id, my_team)
                and item.playerTeam and item.playerTeam.playerTeamId is not None
                and (item.numberOfOffers or 0) > 0):
            ids[str(item.playerTeam.playerTeamId)] = item.playerTeam.playerTeamId
    for p in squad:
        if p.market and (p.market.get("numberOfOffers") or 0) > 0 and p.player_team_id is not None:
            ids.setdefault(str(p.player_team_id), p.player_team_id)
    return list(ids.values())


def on_sale(market: Iterable[MarketItem], squad: list[SquadPlayer], offers: dict[str, list[Offer]],
            my_team: Id) -> list[OnSale]:
    by_team_id = {str(p.player_team_id): p for p in squad if p.player_team_id is not None}
    out: dict[str, OnSale] = {}
    for item in market:
        if item.discr != "marketPlayerTeam" or not same_id(item.seller_team_id, my_team):
            continue
        ptid = item.playerTeam.playerTeamId if item.playerTeam else None
        if ptid is None:
            continue
        out[str(ptid)] = OnSale(ptid, by_team_id.get(str(ptid)), item.id,
                                None if item.directOffer else item.salePrice,
                                parse_time(item.expirationDate), offers.get(str(ptid), []))
    for key, found in offers.items():
        if key not in out and found:
            player = by_team_id.get(key)
            market_id = player.market.get("id") if player and player.market else None
            out[key] = OnSale(player.player_team_id if player else key, player, market_id, None, None, found)
    return sorted(out.values(), key=lambda s: -((s.best.money or 0) if s.best else 0))


def bidder_name(offer: Offer, standing: list[StandingEntry]) -> str:
    if offer.bidderTeam is None or offer.bidderTeam.id is None:
        return "the market"
    row = next((e for e in standing if same_id(e.team.id, offer.bidderTeam.id)), None)
    return manager_name(row) if row else (offer.bidderTeam.name or "a manager")


@dataclass
class SellCandidate:
    player: SquadPlayer
    reason: str


def sell_candidates(squad: list[SquadPlayer], keep: list[SquadPlayer], balance: int | None,
                    change_3d: dict[str, int], already: Iterable[OnSale],
                    limit: int = 3) -> list[SellCandidate]:
    """Bench players worth selling: to cover a negative balance first, then falling
    in value, then unavailable, then hardly scoring."""
    keep_ids = {str(p.player.id) for p in keep}
    listed = {str(s.player_team_id) for s in already if s.listed_price is not None}
    bench = [p for p in squad if str(p.player.id) not in keep_ids
             and str(p.player_team_id) not in listed and p.value > 0]
    out: list[SellCandidate] = []

    def add(p: SquadPlayer, reason: str) -> None:
        if all(c.player is not p for c in out):
            out.append(SellCandidate(p, reason))

    if balance is not None and balance < 0:
        for p in sell_to_cover(-balance, bench, []):
            add(p, "covers your negative balance")
    for p in sorted(bench, key=lambda p: change_3d.get(str(p.player.id), 0)):
        drop = change_3d.get(str(p.player.id), 0)
        if drop < 0:
            add(p, f"value down {abs(drop) / 1e6:.2f}M in 3 days")
    for p in bench:
        status = (p.player.playerStatus or "ok").lower()
        if status in ("injured", "suspended"):
            add(p, status)
    for p in sorted(bench, key=lambda p: p.outlook):
        if p.outlook < IDLE_OUTLOOK:
            add(p, f"hardly scores ({p.outlook:.1f} xP/j)")
    return out[:limit]


def should_withdraw(s: OnSale, keep: list[SquadPlayer]) -> str | None:
    """A reason to take a listing back, if there is one."""
    if s.listed_price is None or s.player is None:
        return None
    if s.player.value > s.listed_price:
        return f"listed at {s.listed_price / 1e6:.2f}M but now worth {s.player.value / 1e6:.2f}M"
    if any(str(k.player.id) == str(s.player.player.id) for k in keep):
        return "he's in your best XI"
    return None
