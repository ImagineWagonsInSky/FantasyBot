"""The write freeze.

When the session can no longer be kept alive, a marker file is created in
the state directory. While it exists every real write is refused, across
restarts and across processes. Only a human clears it, by importing a fresh
token (`fantasybot auth import`) or with `fantasybot unfreeze`.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

from pydantic import BaseModel, ValidationError

from .store import ensure_private_dir


class WritesFrozen(Exception):
    def __init__(self, state: FreezeState) -> None:
        super().__init__(
            f"writes are frozen since {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(state.frozen_at))}: "
            f"{state.reason}. Import a fresh token with `fantasybot auth import`."
        )
        self.state = state


class FreezeState(BaseModel):
    kind: str
    reason: str
    frozen_at: int


class FreezeGuard:
    def __init__(self, path: Path) -> None:
        self.path = path

    def state(self) -> FreezeState | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            return FreezeState.model_validate(json.loads(raw))
        except (ValueError, ValidationError):
            # Present but unreadable (e.g. being written right now): still frozen.
            return FreezeState(kind="unknown", reason="freeze marker is unreadable",
                               frozen_at=int(self.path.stat().st_mtime))

    def is_frozen(self) -> bool:
        return self.path.exists()

    def check(self) -> None:
        state = self.state()
        if state is not None:
            raise WritesFrozen(state)

    def freeze(self, kind: str, reason: str, now: int | None = None) -> bool:
        """Create the marker. Returns True only for the call that froze, so the
        caller alerts once per freeze, not once per blocked write."""
        ensure_private_dir(self.path.parent)
        state = FreezeState(kind=kind, reason=reason, frozen_at=int(now or time.time()))
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(state.model_dump_json())
        return True

    def clear(self) -> bool:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
            return True
        return False
