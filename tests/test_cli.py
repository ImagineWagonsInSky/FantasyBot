from __future__ import annotations

import stat

from cryptography.fernet import Fernet

from fantasybot.auth import FreezeGuard, TokenStore
from fantasybot.cli import main

from .conftest import NOW, seed


def test_gen_key_writes_a_private_file_and_never_overwrites(tmp_path, capsys):
    out = tmp_path / "keys" / "token_key"
    assert main(["auth", "gen-key", "--out", str(out)]) == 0
    key = out.read_bytes().strip()
    Fernet(key)  # valid
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert main(["auth", "gen-key", "--out", str(out)]) == 1
    assert out.read_bytes().strip() == key


def test_status_never_prints_tokens(tmp_path, monkeypatch, capsys):
    key = Fernet.generate_key()
    key_file = tmp_path / "key"
    key_file.write_bytes(key)
    key_file.chmod(0o600)
    state = tmp_path / "state"
    monkeypatch.setenv("FANTASYBOT_STATE_DIR", str(state))
    monkeypatch.setenv("FANTASYBOT_TOKEN_KEY_FILE", str(key_file))
    bundle = seed(TokenStore(state / "tokens.enc", key), id_exp=NOW + 3600,
                  refresh_token="refresh-token-value")

    assert main(["auth", "status"]) == 0
    out = capsys.readouterr().out
    assert "refresh-token-value" not in out and bundle.id_token not in out
    assert "Writes:           allowed" in out

    FreezeGuard(state / "frozen.json").freeze("invalid_grant", "test reason")
    assert main(["auth", "status"]) == 2
    assert "FROZEN" in capsys.readouterr().out

    assert main(["unfreeze"]) == 0
    assert main(["auth", "status"]) == 0


def test_missing_key_is_a_clear_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FANTASYBOT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("FANTASYBOT_TOKEN_KEY", raising=False)
    monkeypatch.delenv("FANTASYBOT_TOKEN_KEY_FILE", raising=False)
    assert main(["auth", "status"]) == 2
    assert "gen-key" in capsys.readouterr().err


def test_about_credits_laligaapp(capsys):
    assert main(["about"]) == 0
    out = capsys.readouterr().out
    assert "LaLigaApp" in out and "Externoak" in out and "GPL-3.0-or-later" in out


def test_import_from_stdin_stores_the_rotated_token(tmp_path, monkeypatch, capsys):
    import functools
    import io

    import httpx

    import fantasybot.cli as cli
    from fantasybot.runtime import open_runtime

    from .conftest import Router, b2c_ok

    key = Fernet.generate_key()
    state = tmp_path / "state"
    monkeypatch.setenv("FANTASYBOT_STATE_DIR", str(state))
    monkeypatch.setenv("FANTASYBOT_TOKEN_KEY", key.decode())
    router = Router()
    router.add("POST", "https://login.laliga.es/", b2c_ok(NOW + 3600, refresh_token="rt-rotated"))
    monkeypatch.setattr(cli, "open_runtime",
                        functools.partial(open_runtime, transport=httpx.MockTransport(router)))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"id_token": "a.b.c", "refresh_token": "rt-pasted"}'))

    assert main(["auth", "import"]) == 0
    out = capsys.readouterr().out
    assert "Verified with LaLiga and stored" in out
    assert "rt-rotated" not in out and "rt-pasted" not in out
    assert TokenStore(state / "tokens.enc", key).load().refresh_token == "rt-rotated"
