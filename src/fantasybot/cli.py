"""Command line entry point: `fantasybot ...`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path

from . import ATTRIBUTION, __version__
from .actions.flow import ActionCenter, listen
from .actions.store import ActionStore
from .alerts import LogAlerter, telegram_recent_chats
from .api import FantasyClient
from .auth import (
    AuthUnavailable,
    FreezeGuard,
    TokenImportError,
    TokenStore,
    TokenStoreError,
    parse_token_paste,
)
from .auth.store import write_private_file
from .config import NATIVE_CLIENT_ID, WEB_CLIENT_ID, ConfigError, Settings
from .fixtures import FixtureRecorder
from .healthcheck import HealthCheck, run_and_alert
from .redact import redact
from .runtime import Runtime, open_runtime
from .scout.briefing import BriefingError, LeagueView, briefing
from .scout.guard import run_guard
from .scout.history import PriceHistory
from .telegram import TelegramBot

log = logging.getLogger("fantasybot")


class RedactingFormatter(logging.Formatter):
    """Last line of defence: no token-shaped string reaches a log."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # httpx logs every request URL at INFO, and the Telegram URL contains the
    # bot token. Keep the HTTP stack quiet even in verbose mode.
    for name in ("httpx", "httpcore", "hpack"):
        logging.getLogger(name).setLevel(logging.WARNING)


# -- reading a pasted secret -------------------------------------------------


class PasteCollector:
    """Collects a paste until a JSON object closes, or a line ends for a bare token."""

    def __init__(self) -> None:
        self._chars: list[str] = []
        self._mode: str | None = None  # "json" or "line"
        self._depth = 0
        self._in_string = False
        self._escaped = False

    @property
    def text(self) -> str:
        return "".join(self._chars)

    def feed(self, chunk: str) -> bool:
        """Returns True once the paste is complete."""
        for ch in chunk:
            if ch == "\x04":  # Ctrl-D
                return True
            if self._mode is None:
                if ch.isspace():
                    continue
                self._mode = "json" if ch == "{" else "line"
            if self._mode == "line":
                if ch in "\r\n":
                    return True
                self._chars.append(ch)
                continue
            self._chars.append(ch)
            if self._in_string:
                if self._escaped:
                    self._escaped = False
                elif ch == "\\":
                    self._escaped = True
                elif ch == '"':
                    self._in_string = False
            elif ch == '"':
                self._in_string = True
            elif ch == "{":
                self._depth += 1
            elif ch == "}":
                self._depth -= 1
                if self._depth == 0:
                    return True
        return False


def read_paste(prompt: str) -> str:
    if not sys.stdin.isatty():
        return sys.stdin.read()
    import termios

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    raw = termios.tcgetattr(fd)
    # Non-canonical mode lifts the 4096-byte line limit that truncates long
    # single-line pastes; no echo keeps the token off screen and out of scrollback.
    raw[3] &= ~(termios.ICANON | termios.ECHO)
    raw[6][termios.VMIN] = 1
    raw[6][termios.VTIME] = 0
    print(prompt, file=sys.stderr, flush=True)
    collector = PasteCollector()
    try:
        termios.tcsetattr(fd, termios.TCSANOW, raw)
        while True:
            chunk = os.read(fd, 65536)
            if not chunk or collector.feed(chunk.decode("utf-8", errors="replace")):
                break
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, saved)
    print(f"(received {len(collector.text)} characters)", file=sys.stderr)
    return collector.text


# -- output helpers ----------------------------------------------------------


def _when(epoch: int | None, now: int) -> str:
    if not epoch:
        return "unknown"
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch))
    delta = epoch - now
    span = abs(delta)
    if span < 3600:
        rel = f"{span // 60} min"
    elif span < 172800:
        rel = f"{span // 3600} h"
    else:
        rel = f"{span // 86400} days"
    return f"{stamp} ({'in ' + rel if delta >= 0 else rel + ' ago'})"


def _client_label(client_id: str) -> str:
    return {WEB_CLIENT_ID: "web", NATIVE_CLIENT_ID: "native/Google"}.get(client_id, "custom")


def print_status(settings: Settings, store: TokenStore, freeze: FreezeGuard) -> int:
    now = int(time.time())
    print(f"State dir:        {settings.state_dir}")
    print(f"Dry run:          {'on (writes are simulated)' if settings.dry_run else 'OFF (writes are sent)'}")
    code = 0
    try:
        bundle = store.load()
    except TokenStoreError as exc:
        print(f"Token:            UNUSABLE: {exc}")
        bundle, code = None, 2
    if bundle is None and code == 0:
        print("Token:            none stored (run `fantasybot auth import`)")
        code = 2
    if bundle is not None:
        print(f"Client ID:        {bundle.client_id} ({_client_label(bundle.client_id)})")
        print(f"Session expires:  {_when(bundle.id_token_expires_at, now)}")
        print(f"Refresh expires:  {_when(bundle.refresh_token_expires_at, now)}")
        print(f"Last refresh:     {_when(bundle.refreshed_at, now)}")
        print(f"Last rotation:    {_when(bundle.rotated_at, now)}")
    state = freeze.state()
    if state is None:
        print("Writes:           allowed")
    else:
        print(f"Writes:           FROZEN since {_when(state.frozen_at, now)}")
        print(f"                  reason: {state.reason}")
        code = 2
    return code


# -- commands ----------------------------------------------------------------


def cmd_gen_key(args: argparse.Namespace) -> int:
    key = TokenStore.generate_key()
    if args.out is None:
        print(key.decode())
        return 0
    path = Path(args.out).expanduser()
    if path.exists() and args.if_missing:
        print(f"{path} already exists; keeping it.")
        return 0
    if path.exists():
        print(f"{path} already exists; refusing to overwrite it (the stored token would "
              "become unreadable). Delete it yourself if you really mean to.", file=sys.stderr)
        return 1
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key + b"\n")
    print(f"Wrote a new key to {path} (mode 600).")
    print("Point FANTASYBOT_TOKEN_KEY_FILE at it. If it is ever lost, generate a new one and run "
          "`fantasybot auth import` again.")
    return 0


async def cmd_import(rt: Runtime, args: argparse.Namespace) -> int:
    text = read_paste(
        "Paste the token JSON from miliga.laliga.com (or a bare refresh token). "
        "It will not be shown on screen."
    )
    try:
        fields = parse_token_paste(text)
        bundle = await rt.tokens.import_token(
            str(fields["refresh_token"]),
            client_id=args.client_id or fields["client_id"],  # type: ignore[arg-type]
            id_token=fields["id_token"],  # type: ignore[arg-type]
            refresh_token_expires_in=fields["refresh_token_expires_in"],  # type: ignore[arg-type]
            verify=not args.no_verify,
        )
    except (TokenImportError, AuthUnavailable) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    verb = "Stored" if args.no_verify else "Verified with LaLiga and stored"
    print(f"{verb} the token (client {_client_label(bundle.client_id)}).")
    print("You can now clear your clipboard.\n")
    return print_status(rt.settings, rt.store, rt.freeze)


async def cmd_refresh(rt: Runtime, args: argparse.Namespace) -> int:
    try:
        await rt.tokens.force_refresh()
    except AuthUnavailable as exc:
        print(f"Refresh failed: {exc}", file=sys.stderr)
        print_status(rt.settings, rt.store, rt.freeze)
        return 1
    print("Refreshed.\n")
    return print_status(rt.settings, rt.store, rt.freeze)


async def cmd_healthcheck(rt: Runtime, args: argparse.Namespace) -> int:
    recorder = FixtureRecorder() if args.save_fixtures else None
    client = (FantasyClient(rt.settings, rt.tokens, rt.freeze, rt.http, on_response=recorder)
              if recorder else rt.client)
    report = await run_and_alert(HealthCheck(rt.settings, rt.tokens, client), rt.alerter,
                                 alert=not args.no_alert)
    print(report.console_text())
    if recorder is not None:
        directory = Path(args.save_fixtures).expanduser()
        written = recorder.write(directory)
        print(f"\nSaved {len(written)} anonymised responses to {directory}/ (mode 600).")
        print("Names, emails, URLs and IDs are replaced; numbers and dates are kept.")
        print("Skim them before sharing.")
    return 0 if report.healthy else 1


TELEGRAM_TOKEN_SHAPE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


def cmd_telegram_set_token(settings: Settings) -> int:
    path = settings.telegram_bot_token_file
    if path is None:
        print("Set FANTASYBOT_TELEGRAM_BOT_TOKEN_FILE to where the token should be stored.",
              file=sys.stderr)
        return 2
    token = read_paste("Paste the bot token from @BotFather. It will not be shown on screen.").strip()
    if not TELEGRAM_TOKEN_SHAPE.match(token):
        print("That doesn't look like a bot token (expected 123456789:ABC…); nothing was stored.",
              file=sys.stderr)
        return 1
    write_private_file(path, (token + "\n").encode())
    print(f"Stored the bot token in {path} (mode 600).")
    return 0


async def cmd_telegram(rt: Runtime, args: argparse.Namespace) -> int:
    if args.telegram_command == "chats":
        token = rt.settings.resolved_telegram_token()
        if not token:
            print("Set FANTASYBOT_TELEGRAM_BOT_TOKEN_FILE first.", file=sys.stderr)
            return 2
        try:
            chats = await telegram_recent_chats(rt.http, token, rt.settings.telegram_api_base)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
        if not chats:
            print("No messages yet. Send your bot any message in Telegram, then run this again.")
            return 1
        for chat_id, label in chats:
            print(f"{chat_id}\t{label}")
        print("\nPut the one you want in FANTASYBOT_TELEGRAM_CHAT_ID.")
        return 0
    if isinstance(rt.alerter, LogAlerter):
        print("Telegram is not configured: set FANTASYBOT_TELEGRAM_BOT_TOKEN_FILE and "
              "FANTASYBOT_TELEGRAM_CHAT_ID.", file=sys.stderr)
        return 2
    sent = await rt.alerter.send("✅ FantasyBot test message: alerts reach this chat.")
    print("Sent." if sent else "Sending failed; see the log above.")
    return 0 if sent else 1


def telegram_bot(rt: Runtime) -> TelegramBot | None:
    token = rt.settings.resolved_telegram_token()
    if not token or not rt.settings.telegram_chat_id:
        return None
    return TelegramBot(rt.http, token, rt.settings.telegram_api_base)


def action_store(rt: Runtime) -> ActionStore:
    return ActionStore(rt.settings.state_dir / "actions.sqlite")


async def cmd_briefing(rt: Runtime, args: argparse.Namespace) -> int:
    try:
        messages, view = await briefing(rt.client, rt.settings, rt.alerter, send=not args.no_send)
    except BriefingError as exc:
        print(f"Briefing failed: {exc}", file=sys.stderr)
        return 1
    print("\n\n".join(messages))
    bot = telegram_bot(rt)
    if not args.no_send and rt.settings.actions and bot is not None:
        with action_store(rt) as store:
            offered = await ActionCenter(rt.settings, rt.client, bot, store).offer(view)
        print(f"\nOffered {offered} action(s) on Telegram.")
    return 0


async def cmd_listen(rt: Runtime, args: argparse.Namespace) -> int:
    if not rt.settings.actions:
        print("Acting from Telegram is off. Set FANTASYBOT_ACTIONS=on to try it (with "
              "FANTASYBOT_DRY_RUN=true, nothing is sent to LaLiga).", file=sys.stderr)
        return 2
    bot = telegram_bot(rt)
    if bot is None:
        print("Telegram isn't set up: it needs the bot token and FANTASYBOT_TELEGRAM_CHAT_ID.",
              file=sys.stderr)
        return 2

    async def fresh_briefing() -> LeagueView:
        _, view = await briefing(rt.client, rt.settings, rt.alerter)
        return view

    with action_store(rt) as store:
        center = ActionCenter(rt.settings, rt.client, bot, store, briefing=fresh_briefing)
        mode = "dry run: confirmed actions are only simulated" if rt.settings.dry_run else "LIVE"
        log.info("listening for Telegram commands (%s)", mode)
        await listen(center, bot, store, once=args.once)
    return 0


def cmd_actions_log(settings: Settings) -> int:
    with ActionStore(settings.state_dir / "actions.sqlite") as store:
        rows = store.executions()
    if not rows:
        print("No actions yet.")
    for r in rows:
        mode = "dry run" if r["dry_run"] else "LIVE"
        req = r["request"] or {}
        print(f"{r['at']}  {r['kind']:<7} {mode:<8} {r['outcome']:<10} {req.get('method', '')} "
              f"{req.get('url', '')} {req.get('body', '')}")
    return 0


async def cmd_guard(rt: Runtime, args: argparse.Namespace) -> int:
    try:
        message = await run_guard(rt.client, rt.settings, rt.alerter, send=not args.no_send,
                                  window_hours=args.hours)
    except BriefingError as exc:
        print(f"Guard failed: {exc}", file=sys.stderr)
        return 1
    print(message or "Nothing to fix before the next jornada.")
    return 0


async def cmd_snapshot(rt: Runtime, args: argparse.Namespace) -> int:
    import datetime as dt

    players = await rt.client.get_all_players()
    with PriceHistory(rt.settings.state_dir / "history.sqlite") as history:
        count = history.record(dt.datetime.now(rt.settings.display_tz()).date(), players)
        days = len(history.days())
    print(f"Recorded {count} player values (history now covers {days} day(s)).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fantasybot",
        description="LaLiga Fantasy automation.",
        epilog=ATTRIBUTION,
    )
    parser.add_argument("--version", action="version", version=f"fantasybot {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="manage the stored LaLiga session")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    gen = auth_sub.add_parser("gen-key", help="create a new token encryption key")
    gen.add_argument("--out", help="write the key to this file (mode 600) instead of stdout")
    gen.add_argument("--if-missing", action="store_true", help="do nothing if --out already exists")
    imp = auth_sub.add_parser("import", help="store a token pasted on stdin and lift any freeze")
    imp.add_argument("--client-id", help="B2C client that issued the token (default: try the known ones)")
    imp.add_argument("--no-verify", action="store_true",
                     help="store without redeeming it first (needs the full JSON)")
    auth_sub.add_parser("status", help="show session expiry and freeze state (never prints tokens)")
    auth_sub.add_parser("refresh", help="redeem the refresh token now")

    health = sub.add_parser("healthcheck", help="check every read endpoint; alert on Telegram if any breaks")
    health.add_argument("--save-fixtures", metavar="DIR",
                        help="also save anonymised responses to DIR (e.g. fixtures-capture)")
    health.add_argument("--no-alert", action="store_true", help="print the report, send nothing")

    tg = sub.add_parser("telegram", help="set up and test Telegram alerts")
    tg_sub = tg.add_subparsers(dest="telegram_command", required=True)
    tg_sub.add_parser("chats", help="list chats that messaged the bot, to find your chat ID")
    tg_sub.add_parser("test", help="send a test alert")
    tg_sub.add_parser("set-token", help="store the bot token pasted on stdin (not shown on screen)")

    brief = sub.add_parser("briefing", help="today's lineup, market, clause and rival briefing")
    brief.add_argument("--no-send", action="store_true", help="print it, don't send it to Telegram")
    guard = sub.add_parser("guard", help="alert only if the next jornada would cost you points")
    guard.add_argument("--no-send", action="store_true", help="print it, don't send it to Telegram")
    guard.add_argument("--hours", type=float, default=24.0,
                       help="start warning this many hours before kickoff (default 24)")
    sub.add_parser("snapshot", help="record today's player values in the price history")
    lis = sub.add_parser("listen", help="answer Telegram buttons and commands (needs FANTASYBOT_ACTIONS=on)")
    lis.add_argument("--once", action="store_true", help="handle one batch of updates, then exit")
    act = sub.add_parser("actions", help="actions taken from Telegram")
    act_sub = act.add_subparsers(dest="actions_command", required=True)
    act_sub.add_parser("log", help="every confirmed action, simulated or real")

    sub.add_parser("unfreeze", help="lift the write freeze without importing a new token")
    sub.add_parser("about", help="version, license and credits")
    return parser


async def _run_async(args: argparse.Namespace, settings: Settings) -> int:
    async with open_runtime(settings) as rt:
        if args.command == "auth" and args.auth_command == "import":
            return await cmd_import(rt, args)
        if args.command == "auth" and args.auth_command == "refresh":
            return await cmd_refresh(rt, args)
        if args.command == "healthcheck":
            return await cmd_healthcheck(rt, args)
        if args.command == "telegram":
            return await cmd_telegram(rt, args)
        if args.command == "briefing":
            return await cmd_briefing(rt, args)
        if args.command == "guard":
            return await cmd_guard(rt, args)
        if args.command == "snapshot":
            return await cmd_snapshot(rt, args)
        if args.command == "listen":
            return await cmd_listen(rt, args)
    raise AssertionError(f"unhandled command {args.command}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    os.umask(0o077)  # anything this process creates is private by default

    if args.command == "about":
        print(f"fantasybot {__version__}, GPL-3.0-or-later.\n{ATTRIBUTION}")
        return 0
    if args.command == "auth" and args.auth_command == "gen-key":
        return cmd_gen_key(args)

    try:
        settings = Settings()
        if args.command == "unfreeze":
            cleared = FreezeGuard(settings.freeze_path).clear()
            print("Write freeze lifted." if cleared else "Writes were not frozen.")
            return 0
        if args.command == "actions":
            return cmd_actions_log(settings)
        if args.command == "telegram" and args.telegram_command == "set-token":
            return cmd_telegram_set_token(settings)
        if args.command == "auth" and args.auth_command == "status":
            store = TokenStore(settings.token_path, settings.resolved_token_key())
            return print_status(settings, store, FreezeGuard(settings.freeze_path))
        return asyncio.run(_run_async(args, settings))
    except (ConfigError, TokenStoreError) as exc:
        print(f"fantasybot: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
