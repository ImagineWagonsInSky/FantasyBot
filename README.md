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
- **Scouting** (`fantasybot briefing`, `fantasybot guard`). A morning briefing
  on Telegram, and an hourly guard that only speaks up when the next jornada
  would cost you points. Both only read from LaLiga: they recommend, and you
  act in the app. See [The daily briefing](#the-daily-briefing).
- **Acting from Telegram** (optional, off by default). Buttons under the
  briefing for the actions it suggests. Each one checks LaLiga again and asks
  you to confirm; until you switch off dry run, confirming only shows what
  would be sent. See [Acting from Telegram](#acting-from-telegram).

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

   Then preview today's briefing without sending it:

   ```bash
   ./fb briefing --no-send
   ```

5. **Run it every day.** This installs the morning briefing (with the health
   check) and the hourly kickoff guard. The default is 09:20 local time; give
   another time as `HH:MM`. Pick a time before your league's market closes
   (shown in the briefing), so you can still bid.

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
| `./fb briefing --no-send` | Print today's briefing (without `--no-send`, also send it) |
| `./fb guard --no-send` | Would the next jornada cost you points right now? |
| `./fb schedule status` | Are the scheduled runs installed, and what did they last report |
| `./fb auth status` | Session expiry, refresh-token expiry, freeze state |
| `./fb healthcheck --no-alert` | Run the check now without messaging Telegram |
| `./fb fixtures` | Save anonymised API responses to `fixtures-capture/` |
| `./fb auth import` | Paste a fresh LaLiga token (also lifts a write freeze) |
| `./fb logs` | The daily run's log |
| `./fb listen start` / `stop` | The listener for Telegram buttons (see [Acting from Telegram](#acting-from-telegram)) |
| `./fb actions log` | Every confirmed action, simulated or real |
| `./fb cash-check` | What the rivals' cash estimate is built from |
| `./fb help` | Everything else |

To update to a newer version:

```bash
git pull && ./fb rebuild
```

### Where things are kept

Everything that persists lives in two Docker volumes:

| Volume | Holds |
| --- | --- |
| `fantasybot_state` | The encrypted LaLiga session, the write-freeze marker, the price history (`history.sqlite`) and which guard alerts were sent |
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

## The daily briefing

Every morning, one Telegram message (two if it's long) with what's worth
acting on today:

- **Standing and money.** Where you are, the gap to the leader, your cash,
  your maximum bid (cash + 20% of team value), and each rival's estimated
  cash (see below).
- **Readiness.** Anything that would cost points in the next jornada: a
  negative balance, an incomplete lineup, injured or suspended starters,
  players you've sold still in the lineup. Doubts, and starters who didn't
  play last jornada, are listed as warnings.
- **Best XI.** The lineup and formation with the most expected points from
  your squad, the gain over your saved lineup, and who to swap. It never
  changes your lineup; you do that in the app.
- **Market.** The best five players you can actually bid on before the
  market closes, marked ✓ (payable from cash) or ⚠ (needs credit: you must
  be back above 0 by kickoff). Players over your maximum bid get one line.
- **Clause steals.** Rival players you can take right now by paying their
  buyout clause from cash (debt isn't allowed for clauses), best first and
  with the owner's rank. Also: players unlocking in the next 24h, and the
  best target you can't afford yet. Shielded players are left out.
- **Your exposure.** Your players any rival could take right now, which
  rivals can afford each one, and which two to shield. Shielding is free, lasts 24h without Premium, and you do it
  in the app. Raising a clause costs half the increase.
- **Selling.** Your listings and every offer on your players, listed or not,
  best offer first, compared with value. Listings you should take back (he's
  now worth more than the asking price, or he's in your best XI). Bench
  players worth selling: to cover a negative balance, falling in value,
  injured, or hardly scoring.
- **Price moves.** Your players' and the league's biggest risers and fallers
  since yesterday. LaLiga's API only gives today's values, so the bot keeps
  its own history; trends appear from the second day.
- **League moves.** What happened in the league in the last 24h.

**Expected points** are deliberately simple, so you can see why one player
ranks above another:

- 60% recent form (the last three finished jornadas, counting jornadas a
  player didn't play as 0) plus 40% season average.
- Injured or suspended: 0. Doubt: half.
- A fit player who didn't play last jornada: ×0.7 (rotation risk).
- **Fixtures**, rated from this season's results:
  - attackers gain against defences that concede a lot;
  - defenders and keepers lose against attacks that score a lot;
  - coaches follow the strength gap between the two clubs;
  - home is worth +5%, away −5%.

  Early in the season a club's rating is pulled towards average, and each
  fixture is capped between ×0.75 and ×1.30. A player whose club has no
  match that jornada is worth 0.

Lineups use next jornada's fixture. Market, clause and exposure picks use
the average over the next three jornadas (shown as "xP/j"), because you keep
a player for weeks. The model doesn't know about probable lineups yet.

**Rivals' cash** is hidden by the API, so it's estimated the way people do
it by hand:

```
starting budget − purchases + sales + 100,000 € × season points
```

- **Jornada earnings** come from the standings (100,000 € per point,
  `FANTASYBOT_EARN_PER_POINT`), not from the feed, which may only show your
  own payouts.
- **Purchases, sales and clause steals** come from the league activity feed.
  When one manager buys from another, the seller is credited too. A clause
  steal pays the player's owner.
- **What neither source shows** (starting budget, daily rewards, clause
  raises) is worked out from your own team, where the real cash is known, and
  assumed to be the same for everyone.

Nobody can be more in debt than 20% of their team value, so if an estimate
comes out below that, the briefing says the numbers don't add up and doesn't
use them. `./fb cash-check` shows the full breakdown per manager. If the
unexplained part jumps by more than 2M overnight, the estimates are marked
"uncertain today". The full feed is read once, then only new entries each
day.

**The kickoff guard** runs every hour and stays silent unless the jornada
starts within 24h and something would cost you points. It alerts once when it
first sees a problem, and once more 3h before kickoff if it's still there.

### The rules that matter

These come from LaLiga's help centre, guides, and real responses from the
API. Check anything marked "guide" in the app before relying on it.

| Rule | Source |
| --- | --- |
| A negative balance when the jornada starts scores 0 points for it | [LaLiga help](https://laligafantasy.zendesk.com/hc/en-us/articles/115002391113-What-does-it-mean-to-be-in-the-red-or-have-a-negative-balance) |
| Max bid = cash + 20% of squad value; clauses can't be paid on debt | [LaLiga help](https://laligafantasy.zendesk.com/hc/en-us/articles/360007533594-Can-I-keep-a-negative-balance) |
| Raising a clause costs half the increase | [LaLiga help](https://laligafantasy.zendesk.com/hc/en-us/articles/360025683593-How-are-release-clauses-set) |
| Clauses in practice sit at or just above market value | the API |
| Blind bids, highest wins, ties go to the first bid; minimum bid = market value | [guide](https://fantasy-tracker.com/guias/laliga-fantasy/), LaLigaApp |
| Recently signed players are locked from clause steals for a while; the API gives the exact unlock time | [guide](https://fantasy-tracker.com/guias/laliga-fantasy/), the API |
| Shield: free, 2 players, 24h (48h with Premium), only on open clauses | [LaLiga](https://www.laliga.com/noticias/el-doble-blindaje-llega-a-laliga-fantasy), [DAZN](https://www.dazn.com/es-ES/news/f%C3%BAtbol/que-es-blindaje-laliga-fantasy-como-funciona/p2wqlqgud2ls1s0o2z5ypimpl) |
| Points come from Opta stats plus a 0–4 bonus (`marca_points` in the API) | [LaLiga](https://www.laliga.com/en-FR/news/a-new-version-of-laliga-fantasy-launches-with-100percent-objective-scoring), the API |

## Acting from Telegram

The briefing can end with buttons for the actions it suggests:

- **Apply the best XI.**
- **Bid on a market player:** the minimum, +5% or +10%.
- **Steal a rival's player** by paying his clause.
- **Raise one of your clauses:** enough to put him out of reach of everyone's
  estimated cash, or +25%.
- **List a player for sale:** at value, +5% or +10%. Nothing is sold until
  you accept an offer.
- **Accept or decline an offer.** Accepting can't be undone, so the first real
  one asks twice like everything else.
- **Withdraw a listing**, when there's a reason to.

When the kickoff guard warns that a negative balance would cost you the
jornada, it also offers sell buttons for the bench players that would cover
it. Shielding isn't offered: the API call for it claims you watched an ad.

**Should you keep everyone listed to collect offers?** Mostly no:

- **You don't need to list a player to get offers.** Rivals can already make
  direct offers on any of your players, and the briefing shows those too.
- **What listing adds** is a place in the market list and, according to
  guides, LaLiga's own offer near market value. That's a fair-price exit, not
  a profit.
- **It shows your hand.** Friends see who you're willing to give up, and when
  you're short of cash.
- **Listings expire,** so "always listed" means relisting every day.

What does make sense is a shop window: list bench players you'd genuinely let
go at +5–10% over value. If someone overpays, you gain; if not, you've lost
nothing. Check in the app whether listing has any limits or side effects;
the bot can't see that.

It's off unless you switch it on, and even then **nothing reaches LaLiga
until you turn off dry run**:

1. **Every tap re-checks LaLiga live.** Is the player still listed? Is the
   clause the same and unshielded? Do you have the cash? Then it tells you
   exactly what would happen, with Confirm and Cancel.
2. **A Confirm button works once, and only for 2 minutes.** Right before
   sending, everything is checked again; if anything changed, nothing is
   done.
3. **With `FANTASYBOT_DRY_RUN=true` (the default)**, confirming only shows
   the exact request FantasyBot would have sent. Use this to try it out.
4. **With dry run off, the first real action of each kind asks a second
   time** ("this would be your first real clause steal… do it for real?").
5. **Hard limits apply.** Each real action is capped at
   `FANTASYBOT_ACTION_MAX_MONEY` (20M by default), real actions at
   `FANTASYBOT_ACTION_DAILY_LIMIT` per day (5), and nothing happens while
   writes are frozen.
6. **Only your private chat with the bot is obeyed;** everything else is
   ignored without a reply. Every confirmed action is in `./fb actions log`.

**Trying it (test mode).** In `fantasybot.env`, set `FANTASYBOT_ACTIONS=on`
and leave `FANTASYBOT_DRY_RUN=true`. Then:

```bash
./fb listen start
```

Send `/briefing` to your bot (or wait for the morning one), tap a button,
and confirm. You'll see the request that would have been sent. `/status`
shows the mode and today's count.

**Going live, when you're happy.** Set `FANTASYBOT_DRY_RUN=false`, then:

```bash
./fb listen restart
```

Your first real action of each kind will ask twice.

**Stopping it:**

```bash
./fb listen stop
```

LaLiga's terms likely aren't keen on automation. Every action here is
started by you, one tap at a time, so it's no different from using the app.
That's deliberate; there's no auto-pilot.

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
| `FANTASYBOT_REQUEST_DELAY` | `0.5` | seconds between API calls in the health check and briefing |
| `FANTASYBOT_TIMEZONE` | `Europe/Madrid` | timezone for times in the briefing and alerts |
| `FANTASYBOT_ACTIONS` | `off` | `on` offers action buttons on Telegram (needs `./fb listen start`) |
| `FANTASYBOT_ACTION_KINDS` | `lineup,bid,steal,raise,sell,accept,decline,withdraw` | which actions to offer |
| `FANTASYBOT_EARN_PER_POINT` | `100000` | money per point after each jornada (rivals' cash) |
| `FANTASYBOT_ACTION_MAX_MONEY` | `20000000` | the most one real action may spend |
| `FANTASYBOT_ACTION_DAILY_LIMIT` | `5` | real actions allowed per day |
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
- **Telegram actions are fenced in.** They're off by default and answer only
  your private chat. Every step is re-checked against LaLiga, confirmations
  are single-use and expire after 2 minutes, real actions are capped per
  action and per day, and every attempt is logged. A stolen bot token alone
  can't trigger anything: commands must come from your own Telegram
  account.
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
