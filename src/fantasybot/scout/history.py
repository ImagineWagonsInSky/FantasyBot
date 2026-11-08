"""Daily price history, plus a cache of finished match results.

LaLiga's API returns today's market value only, so the bot keeps its own
history: one row per player per day, in a SQLite file in the state directory
(the Docker state volume). Trends get more useful the longer it runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from ..api.models import ActivityEntry, Match, Player
from ..auth.store import ensure_private_dir
from .model import position_of

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    day          TEXT    NOT NULL,
    player_id    TEXT    NOT NULL,
    name         TEXT,
    position     INTEGER,
    market_value INTEGER,
    points       REAL,
    status       TEXT,
    PRIMARY KEY (day, player_id)
);
CREATE TABLE IF NOT EXISTS results (
    week          INTEGER NOT NULL,
    match_id      TEXT    NOT NULL,
    local_id      TEXT,
    visitor_id    TEXT,
    local_score   INTEGER,
    visitor_score INTEGER,
    match_date    TEXT,
    PRIMARY KEY (week, match_id)
);
CREATE TABLE IF NOT EXISTS activity (
    key     TEXT PRIMARY KEY,
    created TEXT,
    body    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS offers (
    offer_id   TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    player_id  TEXT,
    money      INTEGER NOT NULL,
    value      INTEGER NOT NULL,
    from_market INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS external_cache (
    source     TEXT NOT NULL,
    day        TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    body       TEXT NOT NULL,
    PRIMARY KEY (source, day)
);
CREATE TABLE IF NOT EXISTS cash_offsets (
    day    TEXT PRIMARY KEY,
    offset INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class Mover:
    player_id: str
    name: str
    value: int
    delta: int


class PriceHistory:
    def __init__(self, path: Path) -> None:
        ensure_private_dir(path.parent)
        created = not path.exists()
        self._db = sqlite3.connect(path)
        if created:
            os.chmod(path, 0o600)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> PriceHistory:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def record(self, day: date, players: Iterable[Player]) -> int:
        rows = [(day.isoformat(), str(p.id), p.display_name, position_of(p), p.marketValue,
                 p.points, p.playerStatus) for p in players if p.marketValue is not None]
        self._db.executemany("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        self._db.commit()
        return len(rows)

    def days(self) -> list[date]:
        rows = self._db.execute("SELECT DISTINCT day FROM snapshots ORDER BY day").fetchall()
        return [date.fromisoformat(r[0]) for r in rows]

    def _values_on(self, day: str) -> dict[str, tuple[str, int]]:
        rows = self._db.execute(
            "SELECT player_id, name, market_value FROM snapshots WHERE day = ?", (day,)).fetchall()
        return {pid: (name, value) for pid, name, value in rows if value is not None}

    def change(self, today: date, days: int) -> dict[str, int]:
        """Value change per player since the latest snapshot at least `days` old."""
        past = self._db.execute("SELECT MAX(day) FROM snapshots WHERE day <= ?",
                                ((today - timedelta(days=days)).isoformat(),)).fetchone()[0]
        if past is None:
            return {}
        now, then = self._values_on(today.isoformat()), self._values_on(past)
        return {pid: value - then[pid][1] for pid, (_, value) in now.items() if pid in then}

    def movers(self, today: date, days: int, limit: int,
               player_ids: set[str] | None = None) -> tuple[list[Mover], list[Mover]]:
        deltas = self.change(today, days)
        current = self._values_on(today.isoformat())
        moves = [Mover(pid, current[pid][0], current[pid][1], d) for pid, d in deltas.items()
                 if d != 0 and (player_ids is None or pid in player_ids)]
        risers = sorted((m for m in moves if m.delta > 0), key=lambda m: -m.delta)[:limit]
        fallers = sorted((m for m in moves if m.delta < 0), key=lambda m: m.delta)[:limit]
        return risers, fallers

    # -- match results (for fixture difficulty) --------------------------------

    def store_results(self, week: int, matches: Iterable[Match]) -> None:
        rows = [(week, str(m.id), _club(m.localId, m.local), _club(m.visitorId, m.visitor),
                 m.localScore, m.visitorScore, m.matchDate) for m in matches]
        self._db.executemany("INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        self._db.commit()

    def finished_weeks(self) -> set[int]:
        """Jornadas whose every match has a score: these never need fetching again."""
        rows = self._db.execute(
            "SELECT week FROM results GROUP BY week "
            "HAVING COUNT(*) > 0 AND SUM(local_score IS NULL OR visitor_score IS NULL) = 0").fetchall()
        return {r[0] for r in rows}

    def results(self) -> list[Match]:
        rows = self._db.execute("SELECT match_id, local_id, visitor_id, local_score, visitor_score, "
                                "match_date FROM results").fetchall()
        return [Match(id=r[0], localId=r[1], visitorId=r[2], localScore=r[3], visitorScore=r[4],
                      matchDate=r[5]) for r in rows]

    # -- league activity (for cash estimates) ----------------------------------

    def store_activity(self, entries: Iterable[ActivityEntry]) -> int:
        """Stores entries; returns how many were new."""
        new = 0
        for e in entries:
            body = e.model_dump_json()
            key = str(e.id) if e.id is not None else hashlib.sha256(body.encode()).hexdigest()[:24]
            cur = self._db.execute("INSERT OR IGNORE INTO activity VALUES (?, ?, ?)",
                                   (key, e.createdAt, body))
            new += cur.rowcount
        self._db.commit()
        return new

    def activity(self) -> list[ActivityEntry]:
        rows = self._db.execute("SELECT body FROM activity ORDER BY created").fetchall()
        return [ActivityEntry.model_validate(json.loads(r[0])) for r in rows]

    def record_offset(self, day: date, offset: int) -> int | None:
        """Stores today's calibration offset; returns the previous day's, if any."""
        prev = self._db.execute("SELECT offset FROM cash_offsets WHERE day < ? ORDER BY day DESC LIMIT 1",
                                (day.isoformat(),)).fetchone()
        self._db.execute("INSERT OR REPLACE INTO cash_offsets VALUES (?, ?)", (day.isoformat(), offset))
        self._db.commit()
        return prev[0] if prev else None

    # -- offers (to learn LaLiga's offer range) ---------------------------------

    def store_offer(self, day: date, offer_id: object, player_id: object, money: int, value: int,
                    from_market: bool) -> None:
        """First sighting only: the value at that moment is what the offer is judged against."""
        self._db.execute("INSERT OR IGNORE INTO offers VALUES (?, ?, ?, ?, ?, ?)",
                         (str(offer_id), day.isoformat(), str(player_id), money, value, int(from_market)))
        self._db.commit()

    def market_offer_range(self) -> tuple[int, float, float, float] | None:
        """(count, lowest, average, highest) premium of LaLiga's own offers seen so far."""
        rows = self._db.execute(
            "SELECT money, value FROM offers WHERE from_market = 1 AND value > 0").fetchall()
        if not rows:
            return None
        premiums = [m / v - 1 for m, v in rows]
        return len(premiums), min(premiums), sum(premiums) / len(premiums), max(premiums)

    # -- cached outside data (odds, real fixtures, probable lineups) -------------

    def cached(self, source: str, day: date) -> tuple[str, str] | None:
        row = self._db.execute("SELECT fetched_at, body FROM external_cache WHERE source = ? AND day = ?",
                               (source, day.isoformat())).fetchone()
        return (row[0], row[1]) if row else None

    def latest_cached(self, source: str) -> tuple[str, str] | None:
        row = self._db.execute("SELECT fetched_at, body FROM external_cache WHERE source = ? "
                               "ORDER BY day DESC LIMIT 1", (source,)).fetchone()
        return (row[0], row[1]) if row else None

    def put_cached(self, source: str, day: date, fetched_at: str, body: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO external_cache VALUES (?, ?, ?, ?)",
                         (source, day.isoformat(), fetched_at, body))
        self._db.commit()


def _club(club_id: object, club: object) -> str | None:
    if club_id is not None:
        return str(club_id)
    nested = getattr(club, "id", None)
    return str(nested) if nested is not None else None
