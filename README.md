# Pickleball Bot

Telegram bot for organizing pickleball games, managing signups, waitlists, and guests.

## Setup

1. **Create a bot.** Talk to [@BotFather](https://t.me/BotFather) on Telegram, run `/newbot`, and copy the token it gives you.

2. **Install Python 3.11+** and dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. **Configure.** Create a `.env` file:

   ```
   BOT_TOKEN=123456:ABC-your-token-here
   DB_PATH=db.sqlite
   TIMEZONE=America/Los_Angeles
   # Optional but recommended — locks the bot to one group chat.
   # Leave unset on first run, send any command in your group, then
   # check the logs (journalctl -u pickleball-bot) for the chat_id
   # and paste it here. Negative number, like -1002345678901.
   ALLOWED_GROUP_ID=
   ```

4. **Run it.**

   ```bash
   python -m bot.main
   ```

5. **Add the bot to your group chat.** Then in the group:
   - Open BotFather → `/setprivacy` → select your bot → **Disable** (so it can see all messages, needed for the `/newgame` flow).
   - Optionally, BotFather → `/setcommands` → paste the contents of `bot_commands.txt`.

## Commands

| Command | Description |
|---|---|
| `/newgame` | Schedule a new game (guided flow) |
| `/games` | List all upcoming games |
| `/mygames` | List games you're signed up for |
| `/help` | Show command list |

All other interactions (joining, waitlisting, adding guests, swapping, etc.) happen through inline buttons on game cards.

## Deployment

For production, run as a `systemd` service on a small VPS. Example unit file:

```ini
[Unit]
Description=Pickleball Bot
After=network.target

[Service]
Type=simple
User=bot
WorkingDirectory=/opt/pickleball-bot
EnvironmentFile=/opt/pickleball-bot/.env
ExecStart=/opt/pickleball-bot/venv/bin/python -m bot.main
Restart=always

[Install]
WantedBy=multi-user.target
```

## Architecture

- `bot/main.py` — entry point, registers handlers
- `bot/db.py` — SQLite helpers (schema, queries)
- `bot/views.py` — message formatting and keyboard builders
- `bot/handlers/games.py` — `/games`, `/mygames`, game card interactions
- `bot/handlers/newgame.py` — `/newgame` conversation flow
- `bot/handlers/roster.py` — join/leave/waitlist/swap/guest logic
- `bot/handlers/common.py` — `/start`, `/help`, error handler
