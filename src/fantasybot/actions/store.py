"""Where suggested actions, confirmations and the audit log live (actions.sqlite).

Button presses only carry short random IDs; everything an action does is read
back from here, never from the button itself.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..auth.store import ensure_private_dir

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id       TEXT PRIMARY KEY,
    kind     TEXT NOT NULL,
    label    TEXT NOT NULL,
    params   TEXT NOT NULL,
    created  TEXT NOT NULL,
    expires  TEXT NOT NULL,
    status   TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS confirmations (
    nonce       TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    stage       TEXT NOT NULL,
    plan        TEXT NOT NULL,
    created     TEXT NOT NULL,
    expires     TEXT NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0,
    message_id  INTEGER
);
CREATE TABLE IF NOT EXISTS executions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    day         TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    kind        TEXT NOT NULL,
    dry_run     INTEGER NOT NULL,
    request     TEXT,
    outcome     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS firsts (
    kind TEXT PRIMARY KEY,
    at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class Proposal:
    id: str
    kind: str
    label: str
    params: dict[str, Any]
    expires: datetime
    status: str = "open"


@dataclass
class Confirmation:
    nonce: str
    proposal_id: str
    stage: str  # "confirm", or "first_live" for the extra question before a first real action
    plan: dict[str, Any]
    expires: datetime
    used: bool
    message_id: int | None


class ActionStore:
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

    def __enter__(self) -> ActionStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # proposals ---------------------------------------------------------------

    def add_proposal(self, kind: str, label: str, params: dict[str, Any], now: datetime,
                     expires: datetime) -> Proposal:
        pid = secrets.token_urlsafe(6)
        self._db.execute("INSERT INTO proposals (id, kind, label, params, created, expires) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (pid, kind, label, json.dumps(params), now.isoformat(), expires.isoformat()))
        self._db.commit()
        return Proposal(pid, kind, label, params, expires)

    def proposal(self, pid: str) -> Proposal | None:
        row = self._db.execute("SELECT id, kind, label, params, expires, status FROM proposals "
                               "WHERE id = ?", (pid,)).fetchone()
        if row is None:
            return None
        return Proposal(row[0], row[1], row[2], json.loads(row[3]), datetime.fromisoformat(row[4]), row[5])

    def close_proposal(self, pid: str, status: str = "done") -> None:
        self._db.execute("UPDATE proposals SET status = ? WHERE id = ?", (status, pid))
        self._db.commit()

    # confirmations -----------------------------------------------------------

    def add_confirmation(self, proposal_id: str, stage: str, plan: dict[str, Any], now: datetime,
                         expires: datetime) -> str:
        nonce = secrets.token_urlsafe(9)
        self._db.execute("INSERT INTO confirmations (nonce, proposal_id, stage, plan, created, expires) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (nonce, proposal_id, stage, json.dumps(plan), now.isoformat(), expires.isoformat()))
        self._db.commit()
        return nonce

    def set_confirmation_message(self, nonce: str, message_id: int) -> None:
        self._db.execute("UPDATE confirmations SET message_id = ? WHERE nonce = ?", (message_id, nonce))
        self._db.commit()

    def confirmation(self, nonce: str) -> Confirmation | None:
        row = self._db.execute("SELECT nonce, proposal_id, stage, plan, expires, used, message_id "
                               "FROM confirmations WHERE nonce = ?", (nonce,)).fetchone()
        if row is None:
            return None
        return Confirmation(row[0], row[1], row[2], json.loads(row[3]), datetime.fromisoformat(row[4]),
                            bool(row[5]), row[6])

    def use_confirmation(self, nonce: str) -> bool:
        """Marks a confirmation used. True only for the one call that did it."""
        cur = self._db.execute("UPDATE confirmations SET used = 1 WHERE nonce = ? AND used = 0", (nonce,))
        self._db.commit()
        return cur.rowcount == 1

    # audit -------------------------------------------------------------------

    def record_execution(self, now: datetime, day: date, proposal_id: str, kind: str, dry_run: bool,
                         request: dict[str, Any] | None, outcome: str) -> None:
        self._db.execute("INSERT INTO executions (at, day, proposal_id, kind, dry_run, request, outcome) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (now.isoformat(), day.isoformat(), proposal_id, kind, int(dry_run),
                          json.dumps(request) if request is not None else None, outcome))
        self._db.commit()

    def live_actions_on(self, day: date) -> int:
        row = self._db.execute("SELECT COUNT(*) FROM executions WHERE day = ? AND dry_run = 0 "
                               "AND outcome = 'done'", (day.isoformat(),)).fetchone()
        return int(row[0])

    def executions(self) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT at, kind, dry_run, request, outcome FROM executions "
                                "ORDER BY id").fetchall()
        return [{"at": r[0], "kind": r[1], "dry_run": bool(r[2]),
                 "request": json.loads(r[3]) if r[3] else None, "outcome": r[4]} for r in rows]

    def done_before(self, kind: str) -> bool:
        return self._db.execute("SELECT 1 FROM firsts WHERE kind = ?", (kind,)).fetchone() is not None

    def mark_done_before(self, kind: str, now: datetime) -> None:
        self._db.execute("INSERT OR IGNORE INTO firsts VALUES (?, ?)", (kind, now.isoformat()))
        self._db.commit()

    # listener state ----------------------------------------------------------

    def get(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))
        self._db.commit()
