"""Encrypted on-disk token storage.

The whole bundle (id_token *and* refresh_token) is encrypted with Fernet
(AES-128-CBC + HMAC-SHA256). LaLigaApp kept the refresh token in plain
localStorage; here nothing token-shaped touches the disk unencrypted.

Writes are atomic (temp file + fsync + rename) so a crash mid-rotation never
leaves a truncated file, and a lock file serialises refreshes across
processes (the bot and the health check can run at the same time).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ValidationError


class TokenStoreError(Exception):
    """The token file exists but cannot be used (wrong key, corrupt, unreadable)."""


class TokenBundle(BaseModel):
    id_token: str
    refresh_token: str
    client_id: str
    id_token_expires_at: int
    refresh_token_expires_at: int | None = None
    imported_at: int
    refreshed_at: int  # last successful refresh
    rotated_at: int  # last time B2C handed us a new refresh token

    def __repr__(self) -> str:  # keep tokens out of logs and tracebacks
        return (f"TokenBundle(client_id={self.client_id!r}, "
                f"id_token_expires_at={self.id_token_expires_at}, <tokens redacted>)")

    __str__ = __repr__


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077:
        path.chmod(0o700)


def write_private_file(path: Path, data: bytes) -> None:
    """Atomically replace ``path`` with ``data``, readable by the owner only."""
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class TokenStore:
    def __init__(self, path: Path, key: bytes) -> None:
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError):
            raise TokenStoreError(
                "the token key is not a valid Fernet key; generate one with "
                "`fantasybot auth gen-key`"
            ) from None
        self.path = path
        self._lock_path = path.with_name(path.name + ".lock")

    @staticmethod
    def generate_key() -> bytes:
        return Fernet.generate_key()

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> TokenBundle | None:
        try:
            ciphertext = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise TokenStoreError(f"cannot read {self.path}: {exc.strerror}") from None
        try:
            plaintext = self._fernet.decrypt(ciphertext)
        except InvalidToken:
            raise TokenStoreError(
                f"cannot decrypt {self.path}: wrong key or the file is corrupted"
            ) from None
        try:
            return TokenBundle.model_validate(json.loads(plaintext))
        except (ValueError, ValidationError):
            raise TokenStoreError(f"{self.path} decrypted but is not a token bundle") from None

    def save(self, bundle: TokenBundle) -> None:
        write_private_file(self.path, self._fernet.encrypt(bundle.model_dump_json().encode()))

    @contextlib.asynccontextmanager
    async def lock(self) -> AsyncIterator[None]:
        """Exclusive cross-process lock around read-refresh-write."""
        ensure_private_dir(self._lock_path.parent)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
