# Running FantasyBot as a systemd service (no Docker)

This is the alternative to the Docker setup in the [README](../README.md):
the bot runs directly on a Linux server as its own unprivileged user, under a
sandboxed systemd service. Use it on a machine where you'd rather not run
Docker.

You need a Linux machine with systemd, Python 3.11 or newer, git and sudo,
plus a PC browser for step 4. It runs as its own unprivileged user, so the
bot can't read your files and can't modify its own code.

## 1. Install

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

## 2. Settings and the encryption key

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

## 3. Telegram

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

## 4. Give it your LaLiga session (once)

Copy your token as described in
[Getting your LaLiga token](../README.md#getting-your-laliga-token), then
run this on the server and paste. The paste is not shown on screen and
doesn't go into your shell history.

```bash
fantasybot-admin auth import
```

## 5. First run, then schedule it

```bash
fantasybot-admin healthcheck --no-alert
```

```bash
sudo cp /opt/fantasybot/deploy/fantasybot-healthcheck.service /opt/fantasybot/deploy/fantasybot-healthcheck.timer /opt/fantasybot/deploy/fantasybot-guard.service /opt/fantasybot/deploy/fantasybot-guard.timer /etc/systemd/system/
```

```bash
sudo systemctl daemon-reload
```

```bash
sudo systemctl enable --now fantasybot-healthcheck.timer fantasybot-guard.timer
```

The health check and briefing then run every morning around 09:20 local time, and the kickoff guard every hour. If the machine
was off at that time, it runs as soon as the machine is back. To see when
it runs next and what it said:

```bash
systemctl list-timers 'fantasybot*'
```

```bash
journalctl -u fantasybot-healthcheck --since today
```

## 6. Capturing real responses (to tighten the models)

```bash
fantasybot-admin healthcheck --no-alert --save-fixtures /var/lib/fantasybot/fixtures
```

```bash
sudo cp -r /var/lib/fantasybot/fixtures ~/fantasybot-fixtures && sudo chown -R "$USER" ~/fantasybot-fixtures
```

Names, emails, URLs and IDs are replaced. Numbers, dates and enum-like
fields are kept. Skim the files before sharing them anyway.

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
