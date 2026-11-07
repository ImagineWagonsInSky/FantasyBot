"""Daily price history.

LaLiga's API returns today's market value only, so the bot keeps its own
history: one row per player per day, in a SQLite file in the state directory
(the Docker state volume). Trends get more useful the longer it runs.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from ..api.models import Player
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
)
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
        self._db.execute(_SCHEMA)
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
