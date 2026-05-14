# Data Model

Current schema (after migrations 001 → 002 → 003). SQLite, FKs on, WAL mode.

## Tables

### `members`
Global user identity. One row per Telegram user, regardless of how many groups they're in.

| col | type | notes |
|---|---|---|
| `telegram_id` | INTEGER PK | Telegram user_id |
| `display_name` | TEXT | "First Last" or first name or username or `User{id}` |
| `username` | TEXT | nullable; Telegram @handle |
| `venmo_handle` | TEXT | nullable; not currently surfaced in UI |
| `created_at` | TEXT | ISO timestamp |

Upserted on every interaction via `touch_member()` so display_name + username stay fresh.

### `chats`
The group chats the bot is in.

| col | type | notes |
|---|---|---|
| `telegram_chat_id` | INTEGER PK | negative for groups/supergroups |
| `title` | TEXT | fetched from Telegram on first registration |
| `status` | TEXT | `active` or `paused` (bot was removed) |
| `created_at` | TEXT | ISO timestamp |

### `chat_members`
Per-chat membership + cached admin status.

| col | type | notes |
|---|---|---|
| `chat_id` | INTEGER | FK → `chats.telegram_chat_id` ON DELETE CASCADE |
| `telegram_user_id` | INTEGER | FK → `members.telegram_id` ON DELETE CASCADE |
| `role` | TEXT | `member` or `admin` |
| `joined_at` | TEXT | preserved on conflict (don't overwrite with `excluded.joined_at`) |
| `telegram_role_checked_at` | TEXT | nullable; refresh cadence is `ADMIN_CACHE_TTL = 5min` |
| | | PK (`chat_id`, `telegram_user_id`) |

Populated by `chats.sync_user_in_chat()`, called from `gate()` as a side effect of every group-chat update.

### `games`
One row per scheduled game.

| col | type | notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `scheduled_for` | TEXT NOT NULL | ISO timestamp; usually tz-aware |
| `location` | TEXT NOT NULL | |
| `organizer_id` | INTEGER NOT NULL | FK → `members.telegram_id` |
| `max_players` | INTEGER NOT NULL DEFAULT 4 | |
| `status` | TEXT NOT NULL DEFAULT 'open' | `open` \| `cancelled` \| `completed` |
| `notes` | TEXT | nullable |
| `chat_id` | INTEGER NOT NULL | which group this game belongs to (migration 002 made this NOT NULL) |
| `message_id` | INTEGER | latest rendered card; updated by `set_game_message_only` on re-render |
| `payment_amount_cents` | INTEGER | nullable; NULL or 0 = "no payment to track" |
| `created_at` | TEXT NOT NULL | |

Indexes: `idx_games_scheduled`, `idx_games_status`, `idx_games_chat`.

### `participants`
The roster for each game. Confirmed and waitlist rows share this table, distinguished by `status`.

| col | type | notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `game_id` | INTEGER NOT NULL | FK → `games.id` ON DELETE CASCADE |
| `status` | TEXT NOT NULL | `confirmed` \| `waitlist` |
| `position` | INTEGER NOT NULL | 1-based order within `status` (recompacted on changes) |
| `member_id` | INTEGER | FK → `members.telegram_id`; nullable for guests |
| `guest_name` | TEXT | nullable; non-null for guests |
| `added_by` | INTEGER NOT NULL | FK → `members.telegram_id` |
| `added_at` | TEXT NOT NULL | |
| `is_paid` | INTEGER NOT NULL DEFAULT 0 | 1 once paid; only meaningful when game has `payment_amount_cents > 0` |

Constraints:
- `CHECK ((member_id IS NOT NULL AND guest_name IS NULL) OR (member_id IS NULL AND guest_name IS NOT NULL))` — exactly one of member_id / guest_name is set.
- `UNIQUE INDEX uniq_member_per_game ON participants(game_id, member_id) WHERE member_id IS NOT NULL` — a member can only be on a roster once. Guests are unconstrained (the same physical person could appear twice as different "Pat" guests; that's fine).

Index: `idx_participants_game(game_id, status, position)`.

### `moneyballs`
One row per tournament instance.

| col | type | notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `game_id` | INTEGER NOT NULL | FK → `games.id` ON DELETE CASCADE |
| `chat_id` | INTEGER | added in migration 001; populated from the parent game |
| `status` | TEXT NOT NULL DEFAULT 'in_progress' | `in_progress` \| `completed` |
| `created_by` | INTEGER NOT NULL | FK → `members.telegram_id` |
| `created_at` | TEXT NOT NULL | |
| `completed_at` | TEXT | nullable; set when status transitions |

### `moneyball_players`
The 8-seat lineup for one tournament. Seat index (0..7) is the schedule's player index.

| col | type | notes |
|---|---|---|
| `moneyball_id` | INTEGER | FK → `moneyballs.id` ON DELETE CASCADE |
| `seat` | INTEGER | CHECK 0..7 |
| `member_id` | INTEGER | nullable for guests |
| `guest_name` | TEXT | nullable for members; added by inline schema migration |
| `added_by` | INTEGER | FK → `members.telegram_id` |
| | | PK (`moneyball_id`, `seat`) |

Same XOR constraint as `participants`. Inline `init_moneyball_schema()` self-migrates older DBs that lack `guest_name`.

### `moneyball_matches`
14 rows per tournament (7 rounds × 2 courts).

| col | type | notes |
|---|---|---|
| `moneyball_id` | INTEGER | FK → `moneyballs.id` ON DELETE CASCADE |
| `round` | INTEGER | CHECK 1..7 |
| `court` | INTEGER | CHECK 1..2 |
| `score_a` | INTEGER | nullable until scored |
| `score_b` | INTEGER | nullable until scored |
| `updated_at` | TEXT | last write timestamp |
| | | PK (`moneyball_id`, `round`, `court`) |

### `schema_migrations`
Migration ledger. One row per applied migration, keyed by name.

## Migrations

Applied in order at startup; tracked in `schema_migrations`. Idempotent.

### 001 multi_chat_schema
Adds `chats`, `chat_members`, `moneyballs.chat_id`. Backfills using `ALLOWED_GROUP_ID` env var: every existing `members` row becomes a `chat_members` row of that single chat; every game with NULL `chat_id` gets it filled in.

### 002 games_chat_id_not_null
Rebuilds `games` with `chat_id NOT NULL`. Uses the SQLite table-swap pattern (CREATE new, INSERT SELECT, DROP old, RENAME). **Crucial detail**: must disable FKs first, else `DROP TABLE games` cascades and obliterates every `participants` row. Re-enables in `finally`, runs `PRAGMA foreign_key_check` for safety. Any rows still NULL at migration time are dropped (logged with a WARNING — the operator can manually fix later).

### 003 payment_tracking
Adds `games.payment_amount_cents` (INTEGER, nullable) and `participants.is_paid` (INTEGER NOT NULL DEFAULT 0). Both via `ALTER TABLE ... ADD COLUMN` after column-presence check.

## The supergroup migration problem

When a Telegram **group** is converted to a **supergroup**, Telegram assigns it a brand new chat_id (the old id is dead). Every row referencing the old id (`chats`, `chat_members`, `games`, `moneyballs`) must be re-pointed atomically.

Trigger: Telegram emits a service message with `migrate_from_chat_id` set, delivered into the NEW supergroup (so `update.effective_chat.id` is already the new id). The counterpart message in the OLD group has `migrate_to_chat_id` — we ignore that side and re-key from the new side only.

Handler chain:
1. `bot/handlers/chat_events.on_chat_migrate` receives the message
2. Calls `db.migrate_chat_id(old_chat_id, new_chat_id)`
3. Function is idempotent — duplicate events are no-ops

Inside `migrate_chat_id`:
- Wrap in `transaction()` with `PRAGMA defer_foreign_keys = ON`. Without this, updating `chats.telegram_chat_id` would fire the `chat_members.chat_id` FK before we get to re-point those rows.
- Handle the "stub row" case: if `on_my_chat_member` already inserted a `chats` row for the new id (because someone interacted with the new supergroup before the migrate message arrived), pull its `chat_members` into a list, delete the stub, then re-add unique ones after the re-key. The old chat's data wins.
- Update parent row first (`chats`), then children (`chat_members`, `games`, `moneyballs`).
- Returns a summary dict for logging.

PR 6 added this. Before PR 6, a group-to-supergroup conversion would leave the bot with two stub chat rows and dangling games.

## Identity & uniqueness rules

- **members**: one row per Telegram user, ever. Display name updates on each interaction.
- **chat_members**: one row per (user, chat). Same user in 3 chats = 3 rows.
- **participants**: one row per signup. A member can appear in many games; only ONCE per game (unique partial index). Guests are unconstrained per game (rare but legal: "Pat" and "Pat" as two different guests).

## Time handling

- All TEXT timestamps stored as ISO. `datetime.fromisoformat()` to parse.
- `games.scheduled_for` is tz-aware. The newgame parser stamps it with the bot's configured TZ.
- "today" cutoffs (for `/games` vs `/past`) take TZ into account: a game at 9:30 AM today is still "upcoming" all day, only dropping at midnight tomorrow in the configured TZ. `list_upcoming_games(tz=...)` computes the day boundary.
- `list_games_in_range(start, end)` requires both bounds tz-aware.

## Money

- `payment_amount_cents` stored as integer cents. NULL and 0 are treated equivalently as "no payment to track"; we normalize on write (`update_game_payment_amount(None)` clears the column AND resets every participant's `is_paid` to 0).
- `views.format_money(cents)`: NULL/0 → `""`, whole dollars → `$5`, fractional → `$7.50`.
- Never float-math currency.

## Schema vs migration responsibilities

`db._create_schema()` defines what a **fresh** install starts with. `_migrations.MIGRATIONS` lists what an **existing** install needs to catch up. **They overlap**: new tables/columns should ideally be in both — base schema gets them on first run, migration handles upgrades. The migration system is the source of truth for older DBs; the base schema keeps fresh installs from needing every historical migration.

A new tenant table you add today: put the `CREATE TABLE` in both `db._create_schema()` AND a new migration. The migration uses `IF NOT EXISTS` so it's a no-op on fresh installs (table already there from base schema) but does the work on existing ones.
