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
  - `--save-fixtures DIR` also saves anonymised copies of the responses, so
    the models can be tightened against real data.

| Command | What it does |
| --- | --- |
| `auth gen-key --out FILE` | Create the key that encrypts the stored token |
| `auth import` | Paste a LaLiga token; it's verified, stored, and any freeze is lifted |
| `auth status` | Session expiry, refresh-token expiry, freeze state (never prints tokens) |
| `auth refresh` | Redeem the refresh token now |
| `healthcheck [--no-alert] [--save-fixtures DIR]` | Check every read endpoint |
| `telegram chats` / `telegram test` | Find your chat ID / send a test alert |
| `unfreeze` | Lift the write freeze without a new token |
| `about` | Version, license, credits |

## Setting it up on a Linux server

You need a Linux machine with systemd, Python 3.11 or newer, git and sudo,
plus a PC browser for step 4. It runs as its own unprivileged user, so the
bot can't read your files and can't modify its own code.

### 1. Install

```bash
sudo useradd --system --home-dir /var/lib/fantasybot --shell /usr/sbin/nologin fantasybot
```

```bash
sudo install -d -o fantasybot -g fantasybot -m 700 /var/lib/fantasybot
```

```bash
sudo git clone https://github.com/ImagineWagonsInSky/FantasyBot /opt/fantasybot
```

```bash
sudo python3 -m venv /opt/fantasybot/.venv
```

```bash
sudo /opt/fantasybot/.venv/bin/pip install --require-hashes -r /opt/fantasybot/requirements.lock
```

```bash
sudo /opt/fantasybot/.venv/bin/pip install --no-deps /opt/fantasybot
```

```bash
sudo install -m 755 /opt/fantasybot/deploy/fantasybot-admin /usr/local/bin/fantasybot-admin
```

`requirements.lock` pins every dependency by hash, so the server installs
exactly what was tested. `fantasybot-admin` runs a command as the service
user with the service's settings.

### 2. Settings and the encryption key

```bash
sudo install -d -o root -g fantasybot -m 750 /etc/fantasybot
```

```bash
sudo install -o root -g fantasybot -m 640 /opt/fantasybot/deploy/fantasybot.env.example /etc/fantasybot/fantasybot.env
```

```bash
sudo /opt/fantasybot/.venv/bin/fantasybot auth gen-key --out /etc/fantasybot/token_key
```

```bash
sudo chown root:fantasybot /etc/fantasybot/token_key && sudo chmod 640 /etc/fantasybot/token_key
```

If the key is ever lost, nothing is permanently broken: generate a new one
and redo step 4.

### 3. Telegram

1. In Telegram, message **@BotFather**, send `/newbot` and follow the
   prompts. It gives you a bot token.
2. Store the token in a file only the service can read. Paste it in the
   editor; that keeps it out of your shell history.

   ```bash
   sudo install -o root -g fantasybot -m 640 /dev/null /etc/fantasybot/telegram_bot_token
   ```

   ```bash
   sudo nano /etc/fantasybot/telegram_bot_token
   ```

3. Send your new bot any message, then list the chats it can see:

   ```bash
   fantasybot-admin telegram chats
   ```

4. Put the chat ID in `FANTASYBOT_TELEGRAM_CHAT_ID`:

   ```bash
   sudoedit /etc/fantasybot/fantasybot.env
   ```

5. Check that alerts arrive:

   ```bash
   fantasybot-admin telegram test
   ```

### 4. Give it your LaLiga session (once)

LaLiga retired email/password login for third-party apps, so the first
token has to come from a browser. This is the same manual method LaLigaApp
documents.

1. On a PC, open <https://miliga.laliga.com/> in Chrome or Firefox and press
   **F12** to open the developer tools. Go to the **Network** tab.
2. In the Network filter box, type `token?p=B2C_1A_5ULAIP_PARAMETRIZED_SIGNIN`.
3. Sign in. A request matching the filter appears. Click it, open
   **Response**, and copy the whole JSON, from `{` to `}`.
4. On the server, run this command and paste. The paste is not shown on
   screen and doesn't go into your shell history.

   ```bash
   fantasybot-admin auth import
   ```

5. Clear your clipboard. Close the tab rather than pressing "log out", as a
   precaution, since it isn't known whether logging out revokes the token.

`auth import` redeems the token once straight away. That proves it works,
and the copy you pasted is replaced by a freshly rotated one.

### 5. First run, then schedule it

```bash
fantasybot-admin healthcheck --no-alert
```

```bash
sudo cp /opt/fantasybot/deploy/fantasybot-healthcheck.service /opt/fantasybot/deploy/fantasybot-healthcheck.timer /etc/systemd/system/
```

```bash
sudo systemctl daemon-reload
```

```bash
sudo systemctl enable --now fantasybot-healthcheck.timer
```

The check then runs every morning around 09:20 local time. If the machine
was off at that time, it runs as soon as the machine is back. To see when
it runs next and what it said:

```bash
systemctl list-timers 'fantasybot*'
```

```bash
journalctl -u fantasybot-healthcheck --since today
```

### 6. Capturing real responses (to tighten the models)

```bash
fantasybot-admin healthcheck --no-alert --save-fixtures /var/lib/fantasybot/fixtures
```

```bash
sudo cp -r /var/lib/fantasybot/fixtures ~/fantasybot-fixtures && sudo chown -R "$USER" ~/fantasybot-fixtures
```

Names, emails, URLs and IDs are replaced. Numbers, dates and enum-like
fields are kept. Skim the files before sharing them anyway.

## When an alert arrives

- **"writes are frozen"**: LaLiga stopped accepting the refresh token, or it
  couldn't be renewed. Redo step 4. Reads keep working until the current
  session expires; writes stay blocked until you import a fresh token.
- **"refresh token expires …"**: redo step 4 before that date.
- **"unexpected shape: …"**: LaLiga changed a response. Capture fixtures
  (step 6) so the models can be updated.
- **"HTTP 5xx" or network errors**: usually a LaLiga outage. If it's still
  there the next day, look into it.

## Updating

```bash
sudo git -C /opt/fantasybot pull
```

```bash
sudo /opt/fantasybot/.venv/bin/pip install --require-hashes -r /opt/fantasybot/requirements.lock
```

```bash
sudo /opt/fantasybot/.venv/bin/pip install --no-deps --force-reinstall /opt/fantasybot
```

## Configuration

All settings are environment variables (in the service setup they live in
`/etc/fantasybot/fantasybot.env`).

| Variable | Default | |
| --- | --- | --- |
| `FANTASYBOT_DRY_RUN` | `true` | `false` sends real writes |
| `FANTASYBOT_COMPETITION_ID` | `1` | 1 = LaLiga EA Sports |
| `FANTASYBOT_LEAGUE_ID` | first league | |
| `FANTASYBOT_TEAM_ID` | discovered | your team in that league |
| `FANTASYBOT_STATE_DIR` | `~/.local/state/fantasybot` | encrypted token + freeze marker |
| `FANTASYBOT_TOKEN_KEY_FILE` | | file holding the encryption key (or `FANTASYBOT_TOKEN_KEY`) |
| `FANTASYBOT_TELEGRAM_BOT_TOKEN_FILE` | | file holding the bot token (or `FANTASYBOT_TELEGRAM_BOT_TOKEN`) |
| `FANTASYBOT_TELEGRAM_CHAT_ID` | | without it, alerts only go to the log |
| `FANTASYBOT_REFRESH_TOKEN_WARN_DAYS` | `3` | warn this long before the refresh token expires |
| `FANTASYBOT_REQUEST_DELAY` | `0.5` | seconds between health-check calls |

## Security notes

The thing worth stealing is the **refresh token**. With it, someone has
full control of your Fantasy account: bids, sales, lineup. Second come the
encryption key and the Telegram bot token. What's done about it:

- **The token is encrypted at rest.** The refresh token and id_token are
  encrypted (Fernet) in a mode-600 file. The key lives elsewhere and is
  readable only by the service user.
  - What this protects against: the state directory leaking through a
    backup, a copy or a commit.
  - What it doesn't protect against: someone who already has root or the
    `fantasybot` user on that machine.
- **Tokens stay out of sight.** They're never printed and never written to
  logs; logs and alerts redact anything token-shaped. HTTP request logging
  is off because Telegram puts the bot token in the URL. `auth import`
  reads the paste with echo off.
- **Writes are guarded.** They're dry runs by default, refused while writes
  are frozen, never retried after a failure, and IDs can't rewrite the
  request path.
- **The service is sandboxed.** It runs as its own user from root-owned
  code, on a read-only filesystem, with no capabilities and a restricted
  set of system calls (see `deploy/`). Dependencies are pinned by hash.
- **Captured responses are protected.** Saved fixtures are anonymised,
  mode 600, and git-ignored.

**Do containers add anything?** Docker would give roughly the same
isolation the systemd sandbox gives here. It wouldn't stop someone with
root (or in the `docker` group) on the host from reading the secrets, and
the key still has to be handed to the container somehow. It becomes worth
it if this moves to a VPS or another host, or to make deployment
repeatable.

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
