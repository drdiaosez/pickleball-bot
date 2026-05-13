# Pickleball Bot

Telegram bot for organizing pickleball games — signups, waitlists, guests, scheduling, and 8-player money-ball tournaments with live scoring and a medals leaderboard.

## What it does

- **Game scheduling**: `/newgame` (date, time, court, max players), with concurrent games supported
- **Signups**: tap-to-join cards with confirmed roster and waitlist, auto-promotion
- **Guests**: members can add non-member guests to any game
- **Editing**: change time/location/notes/max players, or delete a game, from the Manage view
- **Browsing**: `/games`, `/mygames`, `/past`, `/week [next | last | 5/18]`
- **Money ball**: `/moneyball` launches an 8-player Mini App with a 7-round, 2-court rotation guaranteeing every partner once and every opponent twice. Live scoring shared across all 8 phones.
- **Leaderboard**: `/leaderboard` (90 days / year / all-time) ranks members by medal points from completed money balls (gold = 3, silver = 2, bronze = 1)
- **Privacy**: bot is locked to one group via `ALLOWED_GROUP_ID`

## Architecture

The Telegram polling loop and the HTTP server run in the same Python process, sharing the same asyncio event loop and SQLite connection. One systemd service manages everything. Caddy in front handles HTTPS via Let's Encrypt.

```
  Telegram → Caddy (TLS) → aiohttp :8080 ↔ bot polling loop
                                      ↓
                                  db.sqlite
```

## Deployment (full live setup)

### 1. Get a domain pointed at your droplet

Cheapest path: register a `.xyz` for ~$2/yr at Namecheap or Cloudflare Registrar. Add an **A record** pointing your subdomain (e.g. `pickle.yourdomain.com`) to the droplet's IPv4. DNS takes 5–60 minutes to propagate.

Or use [DuckDNS](https://www.duckdns.org/) for a free `yourname.duckdns.org` subdomain.

### 2. Install Caddy on the droplet

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy
```

### 3. Configure Caddy

```bash
sudo nano /etc/caddy/Caddyfile
```

Replace contents with:

```caddy
pickle.yourdomain.com {
    reverse_proxy 127.0.0.1:8080
    encode gzip
}
```

Then `sudo systemctl reload caddy`. Caddy auto-fetches a Let's Encrypt cert (~30 seconds).

Test it:
```bash
curl https://pickle.yourdomain.com/
# should return "ok"
```

### 4. Open firewall ports

```bash
sudo ufw allow 80
sudo ufw allow 443
```

### 5. Update `.env`

```
BOT_TOKEN=...
DB_PATH=/home/bot/pickleball-bot/db.sqlite
TIMEZONE=America/Los_Angeles
ALLOWED_GROUP_ID=-1002345678901
PUBLIC_URL=https://pickle.yourdomain.com
HTTP_HOST=127.0.0.1
HTTP_PORT=8080
```

### 6. Install new dependency and restart

```bash
sudo -u bot bash -c '
  cd /home/bot/pickleball-bot
  source venv/bin/activate
  pip install -r requirements.txt
'
sudo systemctl restart pickleball-bot
sudo journalctl -u pickleball-bot -n 30 --no-pager
```

Look for:
```
Bot starting…
Telegram polling started
HTTP server listening on 127.0.0.1:8080
```

### 7. Try it

In your group: `/moneyball`. Pick a game with 8 confirmed players. Tap **🎾 Open Money Ball**.

## Commands

| Command | Description |
|---|---|
| `/newgame` | Schedule a new game |
| `/games` | List upcoming games |
| `/mygames` | Games you're signed up for |
| `/past` | Recent past games |
| `/week [next \| last \| date]` | Games in a specific Mon-Sun week |
| `/moneyball [game_id]` | Start an 8-player money-ball tournament |
| `/leaderboard [year \| alltime]` | Medal leaderboard (default: last 90 days) |
| `/help` | Show command list |

## Money ball rules

- Exactly 8 confirmed members (guests don't count)
- 7 rounds × 2 simultaneous matches on 2 courts
- Every player partners with every other player exactly once, plays against every other player exactly twice
- Scores entered through the Mini App (live-syncs across all 8 phones every 4 seconds)
- Standings: wins → point differential → points scored
- Gold = 3 leaderboard pts, Silver = 2, Bronze = 1
