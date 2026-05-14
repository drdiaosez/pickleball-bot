# Pickleball Bot — Project Context

> **Read this first.** This file is the orientation pass. The companion files (`MODULE_MAP.md`, `DATA_MODEL.md`, `CONVENTIONS.md`) go deeper on each axis.

Live repo: https://github.com/drdiaosez/pickleball-bot

## What it is

A Telegram bot for organizing pickleball games in one or more group chats. Features:

- **Game scheduling** — `/newgame` (date, time, location, max players, optional per-person payment, notes). Multiple concurrent games supported.
- **Signups** — tap-to-join cards with confirmed roster + waitlist + auto-promotion. Members can add named guests.
- **Roster ops** — leave, demote, soft-swap with waitlist, remove (admin), payment tracking (✅/⬜).
- **Browsing** — `/games`, `/mygames`, `/past`, `/week [next | last | 5/18]`.
- **Money ball** — `/moneyball` launches an 8-player Mini App. Hardcoded 7-round × 2-court schedule where every player partners every other player exactly once and opposes every other player exactly twice. Scores live-sync across all 8 phones via the Mini App calling a backend HTTP API.
- **Leaderboard** — `/leaderboard [90d | year | alltime]`. Medals from completed money balls: gold 3 / silver 2 / bronze 1 leaderboard pts.
- **Merge** — `/merge` admin-only command that promotes a guest's history into a member account.
- **Multi-chat** — bot can be in any number of groups; data is scoped per chat. DMs that touch multiple groups get a "which group?" picker.

## Stack

- **Python 3.11+** (uses `zoneinfo`)
- **python-telegram-bot 21.6** (asyncio, ConversationHandler, CallbackQueryHandler)
- **aiohttp 3.10** (HTTP API + Mini App static serving — same process, same event loop as the bot)
- **SQLite** via raw `sqlite3` (one shared connection, WAL mode, `isolation_level=None`, FKs on)
- **No ORM, no framework** — handlers → views → db
- **python-dotenv** for `.env`
- **Caddy** in front handles HTTPS / Let's Encrypt; bot listens on `127.0.0.1:8080`
- **systemd** runs the single process

## Process model

```
  Telegram → Caddy (TLS) → aiohttp :8080 ↔ bot polling loop
                                      ↓
                                  db.sqlite
```

One Python process. One asyncio loop. One SQLite connection. The Mini App is static HTML at `bot/static/moneyball.html`, served by the same aiohttp server that exposes the JSON API. **There is no separate web service, no worker queue, no broker.** This is intentional — the bot is small and a single process is the right shape.

## The Mini App

`bot/static/moneyball.html` is a self-contained HTML file (no build step, no bundler). It uses Telegram's `WebApp.initData` HMAC to authenticate to the backend API. The 7-round × 2-court schedule is **hardcoded** in two places — `bot/moneyball.SCHEDULE` and the Mini App JS — and they MUST stay in sync. Don't edit one without the other.

Auth flow:
1. Mini App boots → reads `Telegram.WebApp.initDataUnsafe.start_param` (the `mb_id`)
2. Calls `GET/POST /api/moneyball/<mb_id>...` with `X-Telegram-Init-Data: <raw initData>` header
3. `bot/http_server.verify_init_data` recomputes the HMAC against `BOT_TOKEN` and accepts/rejects
4. Authorized request also checks the caller is a member of the chat that owns this money ball (cross-chat leak prevention)

## Architectural invariants (do not violate)

1. **Every group-chat update goes through `gate()`** at the top of every handler. It enforces authorization AND triggers a side-effect membership sync (`ensure_chat_registered` + `sync_user_in_chat`). Skipping it = stale `chat_members` + possible auth bypass.
2. **Every DM-eligible command calls `resolve_chat()`** to figure out which group it's for. Returns chat_id, OR `None` (picker was shown — caller must just `return`).
3. **Games belong to a chat.** `games.chat_id` is `NOT NULL` (migration 002). Every game-scoped query that fans out to a group MUST filter by chat_id, or different groups will see each other's games. The bot started single-chat and was retrofitted; the unfiltered list functions are intentionally left in place but should not be the default call site.
4. **The card's `chat_id` is sacred.** `db.set_game_message()` rewrites both chat_id and message_id and is for the FIRST post only. Re-renders (e.g. opening a game from a DM) MUST use `db.set_game_message_only()`, which only touches message_id. Mixing these up moves a game from one group to another.
5. **Members are global; chat_members is per-chat.** A Telegram user has one row in `members` even if they're in five groups the bot is in. `chat_members(chat_id, telegram_user_id)` is the composite key that scopes them to a group, plus caches `role` and `telegram_role_checked_at` (admin status, refreshed every 5 min).
6. **`participants.member_id XOR guest_name`** — exactly one is set. Guests have no `telegram_id`. A `UNIQUE INDEX` enforces one row per (game, member); guests are unconstrained.
7. **Telegram's supergroup migration changes the chat_id.** When a regular group is converted to a supergroup, Telegram assigns a NEW negative chat_id. `db.migrate_chat_id(old, new)` re-keys every reference (chats, chat_members, games, moneyballs) atomically with deferred FK checks. PR 6 added this. The handler is `on_chat_migrate` in `bot/handlers/chat_events.py`.
8. **Schedule is data, not config.** The 7-round money ball pairing isn't a setting — it's a mathematical structure. Don't try to make it configurable.

## Environment (.env)

```
BOT_TOKEN=...                          # from BotFather (required)
DB_PATH=/home/bot/pickleball-bot/db.sqlite
TIMEZONE=America/Los_Angeles           # IANA name; affects "today" cutoffs
ALLOWED_GROUP_ID=-1002345678901        # legacy fallback only (see below)
PUBLIC_URL=https://pickle.example.com  # required for /moneyball
MINIAPP_SHORT_NAME=play                # the short name registered via BotFather /newapp
HTTP_HOST=127.0.0.1
HTTP_PORT=8080
DEV_BYPASS_USER_ID=                    # local dev only; pair with DEV_BYPASS_SECRET
DEV_BYPASS_SECRET=                     # to bypass initData verification
```

**`ALLOWED_GROUP_ID` is legacy.** The bot was originally locked to one group via this env var. Migration 001 reads it once to seed the `chats` table during the multi-chat conversion. After that, auth is driven by `chats.status = 'active'`, and `ALLOWED_GROUP_ID` is only consulted as a fallback when the `chats` table is empty. New deployments don't need it.

## PR history (latest first)

- **PR 6 — supergroup migration**: handles Telegram converting a regular group → supergroup by re-keying all `chat_id` references atomically. Added `bot/handlers/chat_events.on_chat_migrate` and `db.migrate_chat_id`.
- **PR 5 — admin merge command**: `/merge "Guest Name" @member` to promote a guest's leaderboard history into a member account. Admin status checked per-chat via cached Telegram role.
- **PR 4 — DM picker**: When a user DMs the bot and belongs to multiple registered groups, show an inline-keyboard picker for "which group is this command for?" Powered by `bot/chat_picker.py` and the `register_command()` indirection.
- **PR 3 — payment tracking**: `payment_amount_cents` on games, `is_paid` on participants. Money rendered as `$5` (whole dollars) or `$7.50`. Migration 003.
- **PR 2 — multi-chat conversion**: `chats` and `chat_members` tables, `games.chat_id` made NOT NULL. Migrations 001 + 002. This is the big "no longer single-group" PR.
- **PR 1 — Mini App money ball**: 8-player tournament with the live-syncing HTML app.
- **PR 0 — base**: signups, waitlist, guests, scheduling.

## Where to start when making changes

| If you're changing... | Touch these files |
|---|---|
| The game card layout | `bot/views.py` only (don't put HTML in handlers) |
| Roster button behavior | `bot/handlers/roster.py` + `db.py` |
| The DB schema | New migration in `bot/_migrations.py`; **also** update `db._create_schema()` if it's a new table so fresh installs match. Migrations run AFTER base schema is created. |
| Add a new slash command | Handler module → register in `main.py` → if it's DM-eligible, `register_command()` for the picker → add to `bot_commands.txt` for BotFather |
| Money ball schedule | Don't (see invariant 8) |
| Mini App scoring UI | `bot/static/moneyball.html` (vanilla JS, no build) + matching API in `bot/http_server.py` |
| Auth / chat scoping | `bot/handlers/common.gate()` + `bot/chats.py` |
