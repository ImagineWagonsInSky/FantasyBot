"""Anonymised copies of real API responses, for tightening the models.

Real responses carry other people's data: manager names, emails, team
names, avatars, user IDs. Before anything is written to disk:

* every string becomes a same-length-ish placeholder, except dates and a
  short list of enum-like fields (discr, status, …) that matter for typing;
* every URL becomes example.invalid;
* every ID (keys named id, *Id, *_id) is replaced with a stable fake, so
  relationships between responses survive (the same team ID maps to the
  same fake everywhere in one capture);
* numbers, booleans and nulls are kept: money and points are not personal.

Files are written mode 600 into a mode 700 directory. Review them before
sharing anyway.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .auth.store import write_private_file

# Enum-like numbers that happen to be named *Id: keep them, they aren't identities.
KEEP_VALUE_KEYS = frozenset({"positionId", "activityTypeId"})
KEEP_STRING_KEYS = frozenset({
    "discr", "status", "playerStatus", "type", "position", "positionName", "matchState",
    "key", "option", "formation", "rewardedAdType", "currency", "lang", "leagueType",
})
_ID_KEY = re.compile(r"(^id$|Id$|_id$|^uuid$|^oid$|^sub$)")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$")
_URL = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


class Anonymiser:
    def __init__(self) -> None:
        self._ids: dict[str, int] = {}

    def _fake_id(self, value: int | str) -> int | str:
        number = self._ids.setdefault(str(value), len(self._ids) + 1)
        return number if isinstance(value, int) else f"id{number}"

    def scrub(self, value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {k: self.scrub(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub(v, key) for v in value]
        if value is None or isinstance(value, bool) or key in KEEP_VALUE_KEYS:
            return value
        if key is not None and _ID_KEY.search(key) and isinstance(value, (int, str)):
            return self._fake_id(value)
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            if key in KEEP_STRING_KEYS or _DATE.match(value):
                return value
            if _URL.match(value):
                return "https://example.invalid/redacted"
            return "x" * min(len(value), 12)
        return None


class FixtureRecorder:
    """Collects raw responses in memory; writes only the anonymised form."""

    def __init__(self) -> None:
        self.raw: dict[str, Any] = {}

    def __call__(self, endpoint: str, path: str, data: Any) -> None:
        self.raw.setdefault(endpoint, data)

    def write(self, directory: Path) -> list[Path]:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        anonymiser = Anonymiser()
        written = []
        for endpoint in sorted(self.raw):
            path = directory / f"{endpoint}.json"
            body = json.dumps(anonymiser.scrub(self.raw[endpoint]), indent=2, ensure_ascii=False)
            write_private_file(path, (body + "\n").encode())
            written.append(path)
        return written
