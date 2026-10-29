# Xrob Music Telegram monitoring

Xrob Music 4.4.12 includes in-app Telegram notifications plus an optional independent watchdog.

## Configure in Xrob Music

1. Create a Telegram bot using **@BotFather** and copy its bot token.
2. Open Xrob Music → **Settings → Maintenance → Telegram health alerts**.
3. Paste the bot token and your chat ID, enable monitoring, and optionally enable the daily report.
4. Save changes, then choose **Send test message**.
5. Open the bot in Telegram and send `/start` before testing a private chat. For a group, add the bot and use that group's chat ID; the bot must be permitted to message it.

The token is stored in the server-side settings file with restrictive file permissions and is never returned by `/api/settings` or sent back into frontend JavaScript. Keep `/data/settings.json` and backup archives private because restorable backups include application configuration secrets.

## What the in-app monitor reports

- Media volume disappears, is not mounted, or fails a real write probe.
- Database integrity check fails.
- New app errors (aggregated and rate-limited to avoid floods).
- A recovery transition when storage/database checks become healthy again.
- One daily health summary after 09:00 in the server's local time.
- A test message from Settings.

The app also creates one automatic app-data backup per day under `/data/backups`, checks its SQLite integrity, and keeps the number selected in Settings → Maintenance → Automatic backup retention. Choices are 1, 2 (default), 3, or 5 backups. When the retention value changes, expired snapshots are pruned the next time the automatic backup worker runs, even if today’s backup already exists. This backup includes the app database/settings/library index, **not the actual music files**. Continue to back up the NAS/music files separately.

## Important limitation: alerting when the whole app is down

An in-app worker cannot send a message while its own process or container is stopped. To detect a stopped/crashed Xrob Music add-on, run `app/telegram_watchdog.py` as a separate, always-on process (ideally on another machine or an independent Home Assistant add-on). Configure the watchdog with the same bot token/chat ID and the externally reachable health URL. It alerts after three failed checks by default, reports recovery, watches the health endpoint's mount status, and sends a daily report.

Required environment variables:

```sh
XROB_HEALTH_URL=http://homeassistant.local:8100/api/health
TELEGRAM_BOT_TOKEN=123456:replace-with-your-real-token
TELEGRAM_CHAT_ID=123456789
```

Optional values are `XROB_WATCHDOG_INTERVAL_SECONDS` (default 30), `XROB_WATCHDOG_FAILURES` (default 3), `XROB_WATCHDOG_DAILY_HOUR` (default 9), and `XROB_WATCHDOG_STATE_FILE` (default `./xrob-watchdog-state.json`). The state file prevents duplicate daily reports across watchdog restarts. Do not commit credentials to source control or paste real tokens into issue reports.

Example systemd service setup on a regular Linux host:

1. Copy `app/telegram_watchdog.py` to `/opt/xrob-watchdog/telegram_watchdog.py`.
2. Create `/etc/xrob-telegram-watchdog.env` with the environment variables above, then run `chmod 600 /etc/xrob-telegram-watchdog.env`.
3. Create `/etc/systemd/system/xrob-telegram-watchdog.service`:

```ini
[Unit]
Description=Xrob Music Telegram watchdog
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/xrob-telegram-watchdog.env
ExecStart=/usr/bin/python3 /opt/xrob-watchdog/telegram_watchdog.py
Restart=always
RestartSec=5
StateDirectory=xrob-telegram-watchdog
Environment=XROB_WATCHDOG_STATE_FILE=/var/lib/xrob-telegram-watchdog/state.json
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/xrob-telegram-watchdog

[Install]
WantedBy=multi-user.target
```

Then run `sudo systemctl daemon-reload && sudo systemctl enable --now xrob-telegram-watchdog`. If Xrob Music itself is on Home Assistant OS and you cannot run a host service, install the watchdog as a separate local add-on or run it on another always-on device. A watcher hosted inside the same Xrob Music container cannot detect that container stopping.


## Automation boundaries

The in-app Telegram worker checks health on an approximately 60-second loop. It sends a startup/online notice, alerts on storage and database health transitions, sends a rate-limited digest of newly recorded app errors (at most every five minutes), and optionally sends one daily report after 09:00 in the server's local time. The daily report includes storage/database health, active and queued downloads, app errors in the last 24 hours, and the selected automatic-backup retention.

The separate watchdog polls the configured health URL (30 seconds by default), alerts after three consecutive failures by default, reports recovery, and sends storage-state warnings and a separate daily status report. It must be configured with environment variables and run outside the app process. The bot sends notifications only; it does not automatically repair storage, restart downloads, or accept arbitrary commands.
