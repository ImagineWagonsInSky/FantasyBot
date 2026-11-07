# FantasyBot

Automation for LaLiga Fantasy. It keeps a LaLiga login alive unattended,
talks to the Fantasy API through a typed client, and checks every morning
that the API still looks the way the code expects, telling you on Telegram
when it doesn't.

## What's in here

- **Auth** (`fantasybot auth …`). Uses the refresh-token flow against
  LaLiga's B2C sign-in policy, with the `id_token` as the bearer. Each
  refresh rotates the refresh token, and the token is stored encrypted. If
  the session can't be renewed, writes freeze and one alert is sent.
- **API client** (`fantasybot.api.FantasyClient`). Every endpoint in
  LaLigaApp's `api.js` is a typed async method that returns pydantic models.
  - The competition ID comes from config.
  - Writes are dry runs unless `FANTASYBOT_DRY_RUN=false`, are refused while
    writes are frozen, and are never retried automatically.
  - Not ported: `scrapeTeamLineup` (LaLigaApp's own futbolfantasy.com
    scraper) and `shieldPlayer` (it tells LaLiga a rewarded ad was watched).
- **Health check** (`fantasybot healthcheck`). Redeems the refresh token,
  then calls every read endpoint and validates each response.
  - Your league, team and players are found automatically.
  - Prints a report, and sends one Telegram message if anything broke.
  - Can also save anonymised copies of the responses, so the models can be
    tightened against real data.

There are two ways to run it:

- **Docker**, on a laptop or a Raspberry Pi. This is the main way, and it's
  described below. The same setup moves from one machine to the other.
- **A systemd service** on a Linux server, without Docker. See
  [deploy/systemd.md](deploy/systemd.md).

## Running it in Docker

### What you need

- **Docker.**
  - Mac or Windows: [Docker Desktop](https://docs.docker.com/desktop/).
  - Linux or Raspberry Pi: [Docker Engine](https://docs.docker.com/engine/install/)
    with the Compose plugin.
- **A Raspberry Pi needs a 64-bit OS** (Raspberry Pi OS 64-bit, on a Pi 3,
  4 or 5). The image is built for x86_64 and arm64.
- **Windows** needs WSL 2. Run everything inside the Linux shell there. This
  hasn't been tested.
- **A normal folder for the code**, e.g. `~/FantasyBot`. On a Mac, keep it
  out of Documents, Desktop, Downloads and iCloud Drive: macOS stops
  scheduled jobs from reading those folders, and they may sync to the cloud.

Everything goes through `./fb`, a small script in the repo that runs the bot
in its container.

### First-time setup

1. Get the code and build the image. `init` also creates the encryption
   key, and `fantasybot.env` for your settings.

   ```bash
   git clone https://github.com/ImagineWagonsInSky/FantasyBot ~/FantasyBot
   ```

   ```bash
   cd ~/FantasyBot && ./fb init
   ```

2. **Telegram.** In Telegram, message **@BotFather**, send `/newbot` and
   follow the prompts. It gives you a bot token. Store it; the paste isn't
   shown:

   ```bash
   ./fb telegram set-token
   ```

   Send your new bot any message, then find your chat ID:

   ```bash
   ./fb telegram chats
   ```

   Put that number after `FANTASYBOT_TELEGRAM_CHAT_ID=` in `fantasybot.env`
   (any text editor works), then check alerts arrive:

   ```bash
   ./fb telegram test
   ```

3. **Your LaLiga session.** Copy your token as described in
   [Getting your LaLiga token](#getting-your-laliga-token), then:

   ```bash
   ./fb auth import
   ```

4. **First check.** Everything except `player_offer` should be OK.
   `player_offer` is skipped unless someone has an offer on one of your
   listed players.

   ```bash
   ./fb healthcheck --no-alert
   ```

5. **Run it every day.** The default is 09:20 local time; give another time
   as `HH:MM`.

   ```bash
   ./fb schedule install
   ```

   - **Mac:** this uses launchd. If the Mac is asleep at that time, the
     check runs when it wakes; if it's off, that day is skipped. Turn on
     Docker Desktop → Settings → General → **Start Docker Desktop when you
     sign in**. If Docker still isn't running when the check is due, the
     check starts it, and shows a notification if it can't.
   - **Linux and Raspberry Pi:** this uses a systemd user timer. A run
     missed while the machine was off happens at the next boot.

### Everyday commands

| Command | What it does |
| --- | --- |
| `./fb schedule status` | Is the daily run installed, and what did it last report |
| `./fb auth status` | Session expiry, refresh-token expiry, freeze state |
| `./fb healthcheck --no-alert` | Run the check now without messaging Telegram |
| `./fb fixtures` | Save anonymised API responses to `fixtures-capture/` |
| `./fb auth import` | Paste a fresh LaLiga token (also lifts a write freeze) |
| `./fb logs` | The daily run's log |
| `./fb help` | Everything else |

To update to a newer version:

```bash
git pull && ./fb rebuild
```

### Where things are kept

Everything that persists lives in two Docker volumes:

| Volume | Holds |
| --- | --- |
| `fantasybot_state` | The encrypted LaLiga session, and the write-freeze marker |
| `fantasybot_secrets` | The key that decrypts it, and the Telegram bot token |

The volumes live inside Docker, not in your home folder, so Time Machine,
iCloud and Dropbox never pick them up. Your non-secret settings are in
`fantasybot.env`, which git ignores.

### Moving to a Raspberry Pi later

Don't copy the volumes across. Set up fresh on the Pi, and leave no copy of
the token behind on the laptop.

1. **On the Pi**, install Docker Engine and add yourself to the `docker`
   group. Then repeat the first-time setup with a **fresh** LaLiga token.
   You can reuse the same Telegram bot and copy `fantasybot.env` over.
2. **Still on the Pi**, so the daily run happens without you being logged
   in:

   ```bash
   sudo loginctl enable-linger "$(id -un)"
   ```

3. **On the laptop**, stop the daily run:

   ```bash
   ./fb schedule remove
   ```

4. **Still on the laptop**, delete its copy of the session and key:

   ```bash
   docker volume rm fantasybot_state fantasybot_secrets
   ```

## Getting your LaLiga token

LaLiga retired email/password login for third-party apps, so the first
token has to come from a browser. This is the same manual method LaLigaApp
documents.

miliga.laliga.com is LaLiga's account site, not the game: it won't show
your Fantasy league or team, and it doesn't need to. You only sign in there
so LaLiga issues a token for your account, and the Fantasy API then finds
your leagues from it. **Sign in the same way you sign in to the Fantasy
app** (same email, or the same Google or Apple account). A different login
method can mean a different LaLiga account with no Fantasy team.

1. On a PC, open <https://miliga.laliga.com/> in Chrome or Firefox and press
   **F12** to open the developer tools. Go to the **Network** tab.
2. In the Network filter box, type `token?p=B2C_1A_5ULAIP_PARAMETRIZED_SIGNIN`.
3. Sign in. A request matching the filter appears. Click it, open
   **Response**, and copy the whole JSON, from `{` to `}`.
4. Run `./fb auth import` (or `fantasybot-admin auth import` on a systemd
   server) and paste. The paste is not shown on screen and doesn't go into
   your shell history.
5. Clear your clipboard. Close the tab rather than pressing "log out", as a
   precaution, since it isn't known whether logging out revokes the token.

`auth import` redeems the token once straight away. That proves it works,
and the copy you pasted is replaced by a freshly rotated one. Then run the
health check: if `leagues` shows OK, the token reaches your Fantasy league.

## When an alert arrives

- **"writes are frozen"**: LaLiga stopped accepting the refresh token, or it
  couldn't be renewed. Import a fresh token (see
  [Getting your LaLiga token](#getting-your-laliga-token)). Reads keep
  working until the current session expires; writes stay blocked until
  then.
- **"refresh token expires …"**: import a fresh token before that date.
- **"unexpected shape: …"**: LaLiga changed a response. Save fixtures
  (`./fb fixtures`) so the models can be updated.
- **"HTTP 5xx" or network errors**: usually a LaLiga outage. If it's still
  there the next day, look into it.

## Configuration

All settings are environment variables. With Docker, put them in
`fantasybot.env`; the file paths are fixed by `compose.yaml`. On a systemd
server they live in `/etc/fantasybot/fantasybot.env`. An empty value counts
as unset.

| Variable | Default | |
| --- | --- | --- |
| `FANTASYBOT_DRY_RUN` | `true` | `false` sends real writes |
| `FANTASYBOT_COMPETITION_ID` | `1` | 1 = LaLiga EA Sports |
| `FANTASYBOT_LEAGUE_ID` | first league | |
| `FANTASYBOT_TEAM_ID` | discovered | your team in that league |
| `FANTASYBOT_TELEGRAM_CHAT_ID` | | without it, alerts only go to the log |
| `FANTASYBOT_REFRESH_TOKEN_WARN_DAYS` | `3` | warn this long before the refresh token expires |
| `FANTASYBOT_REQUEST_DELAY` | `0.5` | seconds between health-check calls |
| `FANTASYBOT_STATE_DIR` | `~/.local/state/fantasybot` | encrypted token + freeze marker (set by Docker) |
| `FANTASYBOT_TOKEN_KEY_FILE` | | file holding the encryption key (set by Docker) |
| `FANTASYBOT_TELEGRAM_BOT_TOKEN_FILE` | | file holding the bot token (set by Docker) |

## Security notes

The thing worth stealing is the **refresh token**. With it, someone has
full control of your Fantasy account: bids, sales, lineup. Second come the
encryption key and the Telegram bot token. What's done about it:

- **The token is encrypted at rest.** The refresh token and id_token are
  encrypted (Fernet) in a mode-600 file. The key is kept in a different
  place (a separate volume, or a separate file on a systemd server).
  - What this protects against: the session leaking on its own, through a
    backup, a copy or a commit.
  - What it doesn't protect against: someone who can already run Docker,
    or log in as you or as root, on that machine. They can read both.
- **The container is locked down.** It never runs as root. Its filesystem
  is read-only, except the state volume and, during setup only, the secrets
  volume. It has no Linux capabilities, can't gain privileges, and has
  memory and process limits. It sees none of your files.
- **The image is reproducible.** The base image is pinned by digest, and
  every dependency is a prebuilt wheel checked against the hash in
  `requirements.lock`. The build only receives the files the image needs
  (see `.dockerignore`), so your settings and secrets are never sent to it.
- **Tokens stay out of sight.** They're never printed and never written to
  logs; logs and alerts redact anything token-shaped. HTTP request logging
  is off because Telegram puts the bot token in the URL. Pasting a token
  never echoes it.
- **Writes are guarded.** They're dry runs by default, refused while writes
  are frozen, never retried after a failure, and IDs can't rewrite the
  request path.
- **Captured responses are protected.** Saved fixtures are anonymised,
  mode 600, and git-ignored.

## Development

```bash
uv sync --extra dev
```

```bash
uv run pytest
```

The tests run offline against an in-memory fake of the LaLiga API
(`tests/fakeapi.py`) and of B2C.

## Credits and license

This product includes ‘LaLigaApp’ (c) 2025–present Externoak —
<https://github.com/Externoak/LaLigaApp/>. FantasyBot ports its auth flow,
API endpoint map and response adapters to Python. This attribution does not
imply endorsement by the original authors.

FantasyBot is licensed under GPL-3.0-or-later, including LaLigaApp's
additional attribution term (GPLv3 §7). See [LICENSE](LICENSE) and
[NOTICE](NOTICE).
