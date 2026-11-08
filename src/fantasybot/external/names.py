"""One key per club, whatever a source calls it.

"Athletic Bilbao" (odds), "Athletic Club" (football-data), "athletic"
(futbolfantasy) and LaLiga Fantasy's own name all become "athletic". Order
matters: "RCD Espanyol de Barcelona" must be Espanyol before Barcelona gets a
look, and "Rayo Vallecano de Madrid" Rayo before Madrid.
"""

from __future__ import annotations

import re
import unicodedata

# (key, patterns). Keys are futbolfantasy.com's team slugs.
_CLUBS: list[tuple[str, tuple[str, ...]]] = [
    ("espanyol", ("espanyol",)),
    ("real-sociedad", ("real sociedad",)),
    ("rayo-vallecano", ("rayo",)),
    ("athletic", ("athletic",)),
    ("atletico", ("atletico", "atleti")),
    ("real-madrid", ("real madrid",)),
    ("barcelona", ("barcelona", "barca")),
    ("betis", ("betis",)),
    ("celta", ("celta",)),
    ("alaves", ("alaves",)),
    ("deportivo", ("deportivo la coruna", "deportivo coruna", "rc deportivo", "coruna")),
    ("las-palmas", ("las palmas",)),
    ("racing", ("racing",)),
    ("sporting", ("sporting",)),
    ("elche", ("elche",)), ("getafe", ("getafe",)), ("levante", ("levante",)),
    ("malaga", ("malaga",)), ("osasuna", ("osasuna",)), ("sevilla", ("sevilla",)),
    ("valencia", ("valencia",)), ("villarreal", ("villarreal",)), ("girona", ("girona",)),
    ("mallorca", ("mallorca",)), ("leganes", ("leganes",)), ("valladolid", ("valladolid",)),
    ("oviedo", ("oviedo",)), ("cadiz", ("cadiz",)), ("granada", ("granada",)),
    ("almeria", ("almeria",)), ("zaragoza", ("zaragoza",)), ("eibar", ("eibar",)),
]


KNOWN_CLUBS = frozenset(key for key, _ in _CLUBS) | {"deportivo"}


def normalize(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", plain.lower()).strip()


def club_key(*names: str | None) -> str | None:
    """The first of the given names that identifies a club decides it."""
    for name in names:
        if not name:
            continue
        n = normalize(name)
        for key, patterns in _CLUBS:
            if any(re.search(rf"\b{re.escape(p)}\b", n) for p in patterns):
                return key
        if n == "deportivo":
            return "deportivo"
    for name in names:
        if name and normalize(name):
            return normalize(name).replace(" ", "-")
    return None
