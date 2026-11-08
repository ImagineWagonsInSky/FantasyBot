"""Probable lineups from futbolfantasy.com, matched to LaLiga Fantasy players.

The page parsing is ported from LaLigaApp's src/services/onceParser.js:

* in season, a list: rows `.jugador.tipo_lista` under an element whose class
  contains `jugadores-titulares` (or `jugadores-suplentes`), with the start
  probability in `data-probabilidad` (or the text "NN%"), the position and
  starter flag on the previous sibling (`data-posicion`, `data-onceff`), and
  the full name in the `/jugadores/{slug}` link;
* before the season, a pitch ("once tipo"): `.camiseta-wrapper[data-onceff]`
  shirts whose first `.juggadores a.juggador` is the starter and the others
  his alternatives, with `data-lesion` for fitness and no percentages.

Names are matched within one club, so a short roster and the position keep
the matcher simple: exact names first, then one name contained in the
other, then a close spelling, one LaLiga player per entry.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from typing import Any

from ..api.models import Player
from ..scout.model import COACH, DEF, FWD, GK, MID, position_of
from .html import Node, parse
from .names import normalize

# Without a published percentage: how likely a listed starter / alternative is to start.
STARTER_DEFAULT, ALTERNATIVE_DEFAULT = 0.8, 0.2
MIN_SCORE = 0.75
_NOISE = ("publicidad", "estadist", "mercado", "fantasy", "jornada", "equipo")


@dataclass(frozen=True)
class LineupEntry:
    name: str  # full name, from the player's link when there is one
    nickname: str  # as shown on the page
    position: int | None
    starter: bool
    probability: int | None  # 0-100, when the page gives one
    status: str = "available"  # "available", "doubt" or "injured"

    @property
    def chance(self) -> float:
        """Chance of starting, 0-1."""
        if self.probability is not None:
            return max(0, min(self.probability, 100)) / 100
        return STARTER_DEFAULT if self.starter else ALTERNATIVE_DEFAULT


@dataclass(frozen=True)
class TeamLineup:
    coach: str | None
    entries: list[LineupEntry]

    def to_json(self) -> dict[str, Any]:
        return {"coach": self.coach, "entries": [asdict(e) for e in self.entries]}

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> TeamLineup:
        return TeamLineup(data.get("coach"), [LineupEntry(**e) for e in data.get("entries") or []])


def _position(text: str | None) -> int | None:
    p = (text or "").lower()
    if not p:
        return None
    if "por" in p or "gk" in p or p == "1":
        return GK
    if any(w in p for w in ("def", "back", "lateral", "central")) or p == "2":
        return DEF
    if any(w in p for w in ("med", "mid", "centro", "mc")) or p == "3":
        return MID
    if any(w in p for w in ("del", "att", "forward", "atac")) or p == "4":
        return FWD
    return None


def _slug_name(href: str | None) -> str:
    m = re.search(r"/jugadores/([^/?#]+)", href or "")
    if not m:
        return ""
    return " ".join(w.capitalize() for w in m.group(1).replace("-", " ").split())


def _link(node: Node | None) -> Node | None:
    if node is None:
        return None
    return node.find(lambda n: n.tag == "a" and "/jugadores/" in (n.get("href") or ""))


def _list_entry(row: Node, starter: bool) -> LineupEntry | None:
    name_node = row.find(lambda n: bool(n.classes & {"nombre", "player-name", "name", "jugador-nombre"})
                         or n.tag in ("h4", "h3", "strong"))
    nickname = (name_node.text() if name_node else "") or row.get("data-nombre") or ""
    position = None
    sibling = row.previous_element
    if sibling is not None:
        flag = (sibling.get("data-onceff") or "").lower()
        if flag in ("titular", "starter"):
            starter = True
        elif flag in ("suplente", "bench", "reserva"):
            starter = False
        position = _position(sibling.get("data-posicion") or sibling.get("data-position"))
    text = row.text()
    if len(nickname) < 2:
        m = re.search(r"([A-Za-zÀ-ÿñÑ .'-]{2,40})", text)
        nickname = m.group(1).strip() if m else ""
    nickname = re.sub(r"\s*\d+\s*%\s*$", "", nickname).strip()
    if (len(nickname) < 2 or re.match(r"^(jugador|player|unknown|\d+%)", nickname, re.I)
            or "€" in nickname or "pts" in nickname or "jornada" in nickname.lower()):
        return None
    raw = row.get("data-probabilidad") or row.get("data-probability") or row.get("data-prob")
    if not raw:
        m = re.search(r"(\d+)\s*%", text)
        raw = m.group(1) if m else None
    try:
        probability = int(str(raw).replace("%", "").strip()) if raw else None
    except ValueError:
        probability = None
    if position is None:
        position = _position(" ".join([*row.classes, *(row.parent.classes if row.parent else [])]))
    status = "available"
    if row.find(lambda n: bool(n.classes & {"lesionado", "injured"})):
        status = "injured"
    elif row.find(lambda n: bool(n.classes & {"duda", "doubt"})):
        status = "doubt"
    full = _slug_name((_link(row) or _link(sibling) or Node("a", {}, None)).get("href")) or nickname
    return LineupEntry(full, nickname, position, starter, probability, status)


def _pitch_position(wrapper: Node) -> int | None:
    if "portero" in wrapper.classes:
        return GK
    m = re.search(r"top:\s*(\d+(?:\.\d+)?)%", wrapper.get("style") or "")
    if not m:
        return None
    top = float(m.group(1))  # own goal at the bottom
    return GK if top > 80 else DEF if top > 55 else MID if top > 30 else FWD


def _pitch_entries(doc: Node) -> list[LineupEntry]:
    wrappers = doc.find_all(lambda n: n.has_classes("camiseta-wrapper") and "data-onceff" in n.attrs)
    status_of = {"0": "injured", "1": "doubt", "2": "doubt"}
    seen: set[str] = set()
    out: list[LineupEntry] = []

    def anchors(wrapper: Node) -> list[Node]:
        box = wrapper.find(lambda n: n.has_classes("juggadores"))
        return box.find_all(lambda n: n.tag == "a" and n.has_classes("juggador")) if box else []

    def add(wrapper: Node, anchor: Node, main: bool) -> None:
        label = anchor.find(lambda n: n.has_classes("truncate-name"))
        display = label.text() if label else ("" if anchor.find(lambda n: n.tag == "div") else anchor.text())
        if len(display) < 2 or display.lower() in seen:
            return
        seen.add(display.lower())
        alt = ""
        if main:
            img = wrapper.find(lambda n: n.tag == "img" and n.parent is not None
                               and any(a.tag == "a" and a.has_classes("camiseta") for a in n.ancestors()))
            alt = (img.get("alt") or "").strip() if img else ""
        starter = main and (wrapper.get("data-onceff") or "").lower() == "titular"
        out.append(LineupEntry(_slug_name(anchor.get("href")) or alt or display, display,
                               _pitch_position(wrapper), starter, None,
                               status_of.get(anchor.get("data-lesion") or "", "available")))

    for w in wrappers:  # starters first, then alternatives, each player once
        first = anchors(w)[:1]
        if first:
            add(w, first[0], True)
    for w in wrappers:
        for a in anchors(w)[1:]:
            add(w, a, False)
    return out


def parse_lineup(html: str) -> TeamLineup | None:
    if not html or not html.strip():
        return None
    doc = parse(html)
    coach_node = doc.find(lambda n: n.has_classes("nombre-entrenador"))
    coach = coach_node.text() if coach_node else None

    def rows_under(marker: str) -> list[Node]:
        return [n for n in doc.find_all(lambda n: n.has_classes("jugador", "tipo_lista"))
                if any(marker in (a.get("class") or "") for a in n.ancestors())]

    entries = [e for e in (_list_entry(r, True) for r in rows_under("jugadores-titulares")) if e]
    entries += [e for e in (_list_entry(r, False) for r in rows_under("jugadores-suplentes")) if e]
    if not entries:
        rows = doc.find_all(lambda n: n.has_classes("jugador", "tipo_lista"))
        entries = [e for e in (_list_entry(r, i < 11) for i, r in enumerate(rows)) if e]
    if not any(e.starter for e in entries):
        entries = _pitch_entries(doc)
    entries = [e for e in entries if 3 <= len(e.name) <= 50
               and not any(word in e.name.lower() for word in _NOISE)]
    if not entries and not coach:
        return None
    return TeamLineup(coach, entries)


# -- matching to LaLiga Fantasy players -------------------------------------------


def _forms(*names: str | None) -> list[str]:
    return [n for n in (normalize(x) for x in names if x) if n]


def _score(entry: LineupEntry, player: Player) -> float:
    theirs = _forms(entry.name, entry.nickname)
    slug = getattr(player, "slug", None)  # an extra field LaLiga sends
    ours = _forms(player.nickname, player.name, str(slug).replace("-", " ") if slug else None)
    if not theirs or not ours:
        return 0.0
    best = 0.0
    for a in theirs:
        for b in ours:
            if a == b:
                best = 1.0
                break
            ta, tb = set(a.split()), set(b.split())
            if tb <= ta or ta <= tb:  # "einar" in "einar galilea"
                best = max(best, 0.9)
            sa, sb = a.replace(" ", ""), b.replace(" ", "")
            if min(len(sa), len(sb)) >= 4 and (sa in sb or sb in sa):  # "dela" in "adriandelafuente"
                best = max(best, 0.82)
            long_b = [t for t in b.split() if len(t) >= 3]
            if long_b and all(any(t.startswith(x) for t in a.split()) for x in long_b):  # "vini" → "vinicius"
                best = max(best, 0.8)
            best = max(best, SequenceMatcher(None, a, b).ratio() * 0.95)
    position = position_of(player)
    if entry.position is not None and position is not None and position != entry.position:
        best -= 0.15  # positions do differ between sources, so this only tips close calls
    return best


def match_players(lineup: TeamLineup, roster: Iterable[Player]) -> dict[str, LineupEntry]:
    """LaLiga Fantasy player id → his entry, one entry per player, best matches first."""
    roster = [p for p in roster if position_of(p) != COACH]
    pairs = sorted(((_score(e, p), i, p) for i, e in enumerate(lineup.entries) for p in roster),
                   key=lambda t: -t[0])
    taken_entries: set[int] = set()
    out: dict[str, LineupEntry] = {}
    for score, i, player in pairs:
        if score < MIN_SCORE:
            break
        if i in taken_entries or str(player.id) in out:
            continue
        taken_entries.add(i)
        out[str(player.id)] = lineup.entries[i]
    return out
