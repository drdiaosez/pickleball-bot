# Module Map

File-by-file orientation. Read after `PROJECT_CONTEXT.md`.

```
pickleball-bot/
├── README.md             — user-facing deploy instructions
├── requirements.txt      — python-telegram-bot 21.6, aiohttp 3.10, python-dotenv 1.0.1
├── bot_commands.txt      — BotFather command list (paste into /setcommands)
└── bot/
    ├── __init__.py       — empty
    ├── main.py           — entry point; wires the bot
    ├── db.py             — SQLite layer (raw sqlite3, one connection)
    ├── _migrations.py    — schema migration runner + the migrations themselves
    ├── chats.py          — multi-chat orchestration (register, sync membership, admin cache)
    ├── chat_picker.py    — DM "which group?" picker + COMMAND_REGISTRY
    ├── views.py          — message formatting + keyboard builders (HTML mode)
    ├── moneyball.py      — money ball schema, schedule, standings
    ├── http_server.py    — aiohttp server (Mini App + JSON API)
    ├── tests.py          — DB smoke tests (run: python -m bot.tests)
    ├── static/
    │   └── moneyball.html — self-contained Mini App (vanilla JS, no build)
    └── handlers/
        ├── __init__.py    — empty
        ├── common.py      — gate(), touch_member(), /start, /help, error handler
        ├── newgame.py     — /newgame ConversationHandler (5 states)
        ├── games.py       — /games, /mygames, /past, /week
        ├── roster.py      — every game-card button callback (the big one)
        ├── moneyball.py   — /moneyball, /leaderboard, mb_pick callback
        ├── merge.py       — /merge admin command
        └── chat_events.py — my_chat_member, chat_member, migrate events
```

## `bot/main.py` — entry point (~140 lines)

Reads `.env`, initializes DB + moneyball schema, builds the `Application`, registers handlers, starts polling + HTTP server. **Handler registration order matters** and is documented inline:

1. `/start`, `/help`
2. `/newgame` ConversationHandler — registered before generic text handler so its text state captures work
3. `/games`, `/mygames`, `/past`, `/week`
4. `/moneyball`, `/leaderboard`, mb_pick callback
5. `/merge` + its confirmation callback
6. `/cancelguest`, `/canceledit` (must be before generic text handler in roster)
7. **Chat picker callback** (`pick_chat:*`) — **MUST be before** roster's catch-all `CallbackQueryHandler`, which has no pattern filter and would otherwise swallow these
8. Roster catch-all callbacks + text dispatcher
9. `chat_events` handlers (my_chat_member, chat_member, migrate)

Polling uses `allowed_updates=["message", "callback_query", "my_chat_member", "chat_member"]`. The `chat_member` updates only arrive if the bot is an admin in the chat.

HTTP server is created with a `completion_callback` that posts money-ball results back to the originating group chat. The callback is built from `mb_handlers.schedule_completion_announcement(app, chat_id_for_game)`.

## `bot/db.py` — SQLite layer (~1150 lines)

Direct `sqlite3` usage, no ORM. Module-global `_conn` initialized by `init_db(path)`. WAL mode, FKs on, `isolation_level=None` (we manage txns explicitly via `transaction()` context manager).

**Sections** (search for the divider comments):

- `members` — `upsert_member`, `get_member`. Display name + username, no per-chat info here.
- `chats / chat_members` — `get_chat`, `upsert_chat`, `update_chat_status`, `migrate_chat_id` (supergroup re-key — see DATA_MODEL), `is_chat_active`, `list_active_chats_for_user` (powers DM picker), `get_chat_member`, `upsert_chat_member`, `remove_chat_member`, `user_is_chat_admin`.
- `games` — CRUD + `list_upcoming_games`, `list_past_games`, `list_games_for_member`, `list_games_in_range` (all accept optional `chat_id` filter; CALL SITES must pass it).
  - `set_game_message(game_id, chat_id, message_id)` — for FIRST card post only; rewrites both fields.
  - `set_game_message_only(game_id, message_id)` — for re-renders in a different chat (DM); preserves `chat_id`.
- `participants` — `add_participant` (auto-routes confirmed vs waitlist), `remove_participant` (auto-promotes top of waitlist), `swap_with_waitlist` (soft swap; demote one to position 1 of waitlist, promote one), `promote_top_of_waitlist`, `demote_to_waitlist`, `_renumber` (recompact positions to 1..N).
- Helpers: `_next_position(game_id, status)`, `confirmed_count(game_id)`, `member_is_in_game`.

**Patterns**:
- All functions either accept ints directly or return `dict` / `list[dict]` (never SQLite Row objects to handlers).
- Multi-statement modifications wrap in `with transaction():`.
- Date columns are TEXT ISO strings; we parse with `datetime.fromisoformat` and default tz to UTC when none.

## `bot/_migrations.py` — schema versioning

Tracks applied migrations in a `schema_migrations` table. Each migration is **idempotent** (uses `IF NOT EXISTS`, checks before-state). Sequence:

- **001 multi_chat_schema** — creates `chats` and `chat_members`; backfills using `ALLOWED_GROUP_ID` + existing `games.chat_id`s. Adds `moneyballs.chat_id` if the table exists. Backfills `chat_members` from `members` if a seed chat is known.
- **002 games_chat_id_not_null** — rebuilds `games` with `chat_id NOT NULL` via the SQLite table-swap pattern. **CRITICAL**: disables FKs for the duration, otherwise `DROP TABLE games` cascades and nukes every `participants` row. Re-enables FKs in `finally`. Runs `PRAGMA foreign_key_check` for safety.
- **003 payment_tracking** — adds `games.payment_amount_cents` and `participants.is_paid`.

Adding a migration: write `_004_name(conn)`, append `("004_name", _004_func)` to `MIGRATIONS`. Run on every startup; idempotency makes re-running safe.

## `bot/chats.py` — multi-chat orchestration (~160 lines)

Two public entry points:

- `ensure_chat_registered(bot, chat)` — upsert chats row, fetch title from Telegram on first-seen. Returns the row or `None` for DMs.
- `sync_user_in_chat(bot, chat_id, user_id)` — upsert chat_members row, re-fetch admin status from Telegram if cache is older than `ADMIN_CACHE_TTL` (5 min). Stretches stale cache (doesn't bump timestamp) on Telegram errors so we don't lose access on transient API failures.

Both are called from `gate()` (in `handlers/common.py`) as a side effect of every group-chat update.

`_fetch_telegram_role` maps Telegram's `creator|administrator|member|restricted|left|kicked` to our `admin|member|None`.

## `bot/chat_picker.py` — DM picker (~200 lines)

When a user DMs the bot and belongs to multiple registered groups, we need to disambiguate. `resolve_chat(update, context, command_label)`:

- Group chat → returns chat.id (no picker).
- DM, user in 0 chats → message "you're not a member of any group I'm registered with", returns None.
- DM, user in 1 chat → returns that chat_id (no picker).
- DM, user in 2+ chats → shows picker with one button per chat, returns None.
- DM after picker tap → `_PICKER_ONESHOT_KEY` user_data flag is set by `on_pick_callback`; `resolve_chat` pops it and returns immediately. The flag is one-shot so the **next** command re-prompts (users like switching contexts).

The picker's callback (`pick_chat:<command>:<chat_id>`) re-dispatches by looking up `COMMAND_REGISTRY[command_label]` and calling it directly. Modules populate this registry at **import time** via `register_command()` calls — so the registry is ready before `main.py` registers handlers.

**`/newgame` is special**: it's a ConversationHandler and can't be cleanly re-dispatched from a callback. The picker tells the user to retype `/newgame`; the one-shot flag is still set, so the retyped command routes correctly without prompting again.

**Handler registration ORDER**: `build_picker_handlers()` MUST be added before `roster.build_roster_handlers()`. Roster's catch-all `CallbackQueryHandler` has no pattern filter and would swallow `pick_chat:*` otherwise. See main.py's comments.

## `bot/views.py` — formatting (~330 lines)

All HTML message rendering and `InlineKeyboardMarkup` construction. Handlers should not build text or keyboards inline — go through views.

Key functions:
- `format_when`, `format_when_short` — datetime formatting with tz
- `format_money(cents)` — `None`/`0` → `""`, whole dollars → `$5`, fractional → `$7.50`
- `game_has_payment(game)` — guard for paid-flag UI
- `participant_display(p, show_paid=False)` — name + paid badge
- `render_game_card(game, participants, tz, organizer_name)` — main card body
- `game_card_keyboard(game_id, viewer_in_game, game_full, has_payment)` — buttons under a card
- `render_game_list_header`, `game_list_keyboard` — for `/games` etc.
- Manage view rendering helpers

HTML parse mode throughout (more forgiving with names containing `_` or `*`). Always `escape()` user-supplied strings.

## `bot/moneyball.py` — money ball persistence (~470 lines)

Schema (created by `init_moneyball_schema()` at startup, lazily, with a one-shot legacy-schema migration inline):
- `moneyballs(id, game_id, chat_id, status, created_by, created_at, completed_at)`
- `moneyball_players(moneyball_id, seat 0..7, member_id, guest_name, added_by)` — guest support added later than the original schema; the `executescript` block has a self-migration to add `guest_name` to old DBs.
- `moneyball_matches(moneyball_id, round 1..7, court 1..2, score_a, score_b, updated_at)`

`SCHEDULE` is a `list[list[list[list[int]]]]`: rounds × courts × teams × seats. Validated invariant: 28 partner pairs (each appears once), 28 opponent pairs (each twice). **Mirrored in the Mini App JS — keep them in sync.**

`compute_standings(mb)` — wins → point differential → points scored. Used by both the API and the leaderboard.

## `bot/http_server.py` — aiohttp server (~265 lines)

Routes:
- `GET /` — health check (Caddy reads this implicitly via reverse_proxy)
- `GET /moneyball` — Mini App HTML (mb_id read from `start_param` by JS)
- `GET /moneyball/{mb_id}` — Mini App HTML (legacy/direct access)
- `GET /api/moneyball/{mb_id}` — state (player roster, scores, standings, schedule)
- `POST /api/moneyball/{mb_id}/score` — record one match score; broadcasts via polling on the client side

Auth via `@web.middleware auth_middleware`:
1. `/api/*` routes require `X-Telegram-Init-Data` header
2. `verify_init_data` recomputes HMAC against `BOT_TOKEN`, checks freshness (24h max), returns user_id
3. Cached for 5min by initData hash
4. Verifies user is in `members`
5. For moneyball-scoped routes, also verifies user is in the chat that owns the moneyball (cross-chat leak protection)

Local dev: set `DEV_BYPASS_USER_ID` + `DEV_BYPASS_SECRET` to skip HMAC; client sends `X-Dev-Bypass: <secret>` header.

`completion_callback` is wired up by `main.py` to `mb_handlers.schedule_completion_announcement`, which posts the medal recap message into the group chat when a money ball goes from in_progress → completed.

## `bot/handlers/common.py` — auth + shared helpers (~180 lines)

- `gate(update)` — call at the top of EVERY handler. Returns True if authorized. Side effect: triggers chat registration + member sync for group updates. **Skipping `gate()` = bug.**
- `is_authorized(update)` — pure check, no side effects. Group: must be in `chats` with status='active' (legacy: `ALLOWED_GROUP_ID` fallback when chats is empty). DM: user must be in at least one active chat.
- `touch_member(update)` — upsert the user into `members`. Called by every command handler after `gate()`.
- `cmd_start`, `cmd_help` — both show `HELP_TEXT` block defined at top.
- `error_handler` — logs + responds with a generic "something went wrong" so users aren't left hanging.

## `bot/handlers/newgame.py` — ConversationHandler (~355 lines)

5 states: `ASK_WHEN` → `ASK_LOCATION` → `ASK_MAX` → `ASK_PAYMENT` → `ASK_NOTES` → end (create + post card).

`parse_datetime()` is forgiving: accepts `wed 6:30pm`, `thursday 7pm`, `tomorrow 6pm`, `today 5:30pm`, `5/14 6:30pm`, `2026-05-14 18:30`. Strategy: extract the date first (stricter syntax), strip it, parse the rest as time.

`/skip` is accepted at the optional steps (max players defaults to 4, payment defaults to none, notes default to empty).

## `bot/handlers/games.py` — listing (~180 lines)

`/games`, `/mygames`, `/past`, `/week [next | last | DATE]`. Every command:
1. `gate()`
2. `touch_member()`
3. `resolve_chat()` (returns None → just return)
4. Query DB filtered by chat_id
5. Render via views

Week parsing: `next` = next Mon-Sun, `last` = prev Mon-Sun, otherwise parse a date and use the Mon-Sun week containing it.

## `bot/handlers/roster.py` — game card buttons (~1050 lines)

The biggest file. Every callback on a game card lives here. Callback data format: `"<action>:<arg1>[:<arg2>]"`. Action list at top of the file. Notable patterns:

- `render_card_in_place(context, update, game_id, viewer_user_id)` — edits the message in place rather than posting a new one
- Card buttons are "viewer-aware": Join shows for not-in-game viewers, Leave for in-game viewers
- "Manage" view is admin-flavored (anyone can open it, but destructive buttons require admin or organizer)
- Soft swap flow: tap waitlist person → pick which confirmed person they're swapping with → execute. The displaced confirmed person becomes waitlist position 1 (not deleted).
- Payment toggle: `pay_toggle:<participant_id>` flips `is_paid`, only meaningful when game has `payment_amount_cents`
- `/cancelguest` and `/canceledit` are text commands that cancel in-progress text prompts (e.g. waiting for a guest name); they're registered as `CommandHandler`s BEFORE the generic text dispatcher in roster.

## `bot/handlers/moneyball.py` — tournament handlers (~450 lines)

`/moneyball` (no arg) → list games with exactly 8 confirmed players, ask which to launch. `/moneyball <game_id>` → direct launch.

Mini App launch uses Telegram deep links: `https://t.me/<bot_username>/<MINIAPP_SHORT_NAME>?startapp=<mb_id>`. This format is required for URL buttons in group chats (regular HTTPS URLs don't auto-launch the registered Mini App).

`/leaderboard [year | alltime]` defaults to last 90 days. Gold 3, silver 2, bronze 1.

`schedule_completion_announcement(app, chat_id_for_game)` returns a callable that the HTTP server's completion_callback invokes — posts a medal recap message to the group.

## `bot/handlers/merge.py` — admin command (~220 lines)

`/merge "Guest Name" @member_handle` (or telegram user_id). Promotes a guest's history into a member account — every `participants` row with `guest_name = "Guest Name"` gets `member_id = <user>`, `guest_name = NULL`. Also re-points money-ball `moneyball_players` rows.

Admin check is per-chat: `_ensure_admin()` forces a fresh sync (no cache trust) so brand-new chat owners can merge without waiting for the 5-min cache to populate.

Confirmation flow: bot replies with a preview, operator taps "Confirm" callback, merge runs in a transaction.

## `bot/handlers/chat_events.py` — Telegram chat lifecycle (~170 lines)

Three handlers:

- `on_my_chat_member` — bot was added/removed/promoted/demoted. Added → `ensure_chat_registered(mark_active=True)` + greeting message. Removed → `mark_chat_paused()` (don't delete data; can be un-paused by re-adding).
- `on_chat_member` — another user's status changed. Bot must be admin for Telegram to send these. Used to drop `chat_members` rows on leave/kick. Joining is handled lazily via `gate()` on next interaction.
- `on_chat_migrate` — fires on the `migrate_from_chat_id` service message that Telegram emits in the NEW supergroup. Calls `db.migrate_chat_id(old, new)` to re-key everything atomically. The counterpart `migrate_to_chat_id` message in the old group is ignored — re-keying from the new side is sufficient and avoids dueling handlers.

## `bot/static/moneyball.html` — Mini App

Self-contained HTML file. Vanilla JS. Reads `Telegram.WebApp.initDataUnsafe.start_param` for `mb_id`. Polls `GET /api/moneyball/<mb_id>` every ~4s for live updates. Posts scores to `POST /api/moneyball/<mb_id>/score`. Sends `Telegram.WebApp.initData` raw in the `X-Telegram-Init-Data` header.

**Hardcodes the 7-round schedule** to render seat-to-team mapping. Mirror of `bot/moneyball.SCHEDULE`. If you change one, change both.
