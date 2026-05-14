# Conventions

How code in this repo is written. Match these patterns when extending.

## Handler skeleton

Every command handler follows the same shape. Skipping any line is a bug.

```python
async def cmd_thing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .common import gate                    # late import avoids circular dep
    if not await gate(update):                  # 1. authorize + side-effect membership sync
        return
    await touch_member(update)                  # 2. upsert this user into members
    chat_id = await resolve_chat(update, context, "thing")  # 3. resolve target chat
    if chat_id is None:                         # picker was shown — caller must just return
        return
    tz = context.bot_data["tz"]                 # 4. tz from app state, set in main.py
    # ... do work, query db.* with chat_id, render via views.* ...
```

Notes:
- `gate()` is non-negotiable. Group chats need it for auth; DMs need it for the legacy/single-chat fallback. Side effect: keeps `chats` and `chat_members` fresh.
- `touch_member()` is cheap (an UPSERT) and keeps display names accurate.
- `resolve_chat()` is required for any command that operates on chat-scoped data. **Do not** look at `update.effective_chat.id` directly in DMs.
- The label passed to `resolve_chat` ("thing") must match a `register_command("thing", cmd_thing)` call at module level so the picker can re-dispatch after a tap.

For non-command handlers (callbacks, text dispatchers), still call `gate()`. `touch_member()` is optional if the handler can't have new users (e.g. mid-conversation states).

## Registering a new slash command

1. Write `async def cmd_name(...)` following the skeleton above in the appropriate handler module.
2. At module level: `register_command("name", cmd_name)` — populates `COMMAND_REGISTRY` for the DM picker.
3. In `main.py`, add `app.add_handler(CommandHandler("name", cmd_name))` in the right slot (see registration-order notes in MODULE_MAP).
4. Add it to `bot_commands.txt` and re-run BotFather's `/setcommands` against your bot (paste the whole file).
5. Update the `HELP_TEXT` block in `handlers/common.py`.

## Callback data format

Inline button callbacks use `"<action>:<arg1>[:<arg2>]"`:

```python
InlineKeyboardButton("Join", callback_data=f"join:{game_id}")
InlineKeyboardButton("Remove", callback_data=f"rm:{participant_id}")
InlineKeyboardButton("Swap", callback_data=f"swap_do:{wait_pid}:{conf_pid}")
```

Pattern matchers use anchored regex: `^join:` not `join:`. Roster's catch-all has NO pattern filter, which is why the chat-picker handler (`pick_chat:*`) must be registered BEFORE it — otherwise the picker callbacks get swallowed.

Keep payloads tiny — Telegram caps callback_data at 64 bytes. We store database IDs, not display strings.

## Database access

- Always go through `db.*` functions. No raw SQL in handlers.
- Multi-statement modifications use the `transaction()` context manager:
  ```python
  with db.transaction():
      db._conn.execute(...)
      db._conn.execute(...)
  ```
- Read-only queries don't need a transaction.
- Functions return `dict` or `list[dict]` — never `sqlite3.Row`.
- All ID parameters are `int`. Coerce defensively at API boundaries (`int(request.match_info["mb_id"])`).

## Chat scoping

Anywhere you call a `db.list_*` function that has an optional `chat_id` parameter, **pass it**. Always. The default behavior of "show across all chats" is a footgun that only exists for legacy/single-chat use. Multi-chat bugs almost always trace back to a `list_upcoming_games(tz=tz)` call that should have been `list_upcoming_games(tz=tz, chat_id=chat_id)`.

## Member identity vs guests

- `member_id` is the Telegram user_id. Use it as the foreign key everywhere.
- `guest_name` is a free-text label. Guests have no Telegram identity.
- `participants` and `moneyball_players` both enforce the XOR via CHECK constraint.
- When rendering, **always** branch on `if p["member_id"] is not None` — don't assume one or the other.
- `/merge` converts a guest's history into a member's history. Used when a longtime guest finally signs up — preserves their leaderboard points.

## HTML mode + escaping

All messages use HTML parse mode. Markdown is too brittle with names containing `_` or `*`.

- `from telegram.helpers import escape` and call `escape(user_supplied_string)` on everything that came from a user (display names, guest names, notes, locations, etc).
- Tags we use: `<b>`, `<i>`, `<code>`, `<pre>`. No `<a>` for user content — it'd require URL escaping too.
- Emoji are fine inline — they're plain text.

## Money formatting

Currency stored as integer cents. Never float math.
- `views.format_money(cents)` is the single source: NULL/0 → `""`, whole dollars → `$5`, fractional → `$7.50`.
- `views.game_has_payment(game)` is the boolean guard for showing paid-flag UI.
- Clearing `payment_amount_cents` also resets every participant's `is_paid` to 0 (see `db.update_game_payment_amount`).

## Time + timezone

- `context.bot_data["tz"]` is the configured `ZoneInfo`. Set once in `main.py` from `.env`.
- Pass `tz` into every `db.list_*` function that filters by "today" so the day boundary is correct.
- Datetimes stored as ISO strings. `datetime.fromisoformat()` to read. If the string has no tz, default to UTC: `dt.replace(tzinfo=timezone.utc)`.
- The newgame parser produces tz-aware datetimes already.

## Migrations

To add a schema change:

1. Add a `_NNN_name(conn)` function to `bot/_migrations.py`. Make it **idempotent** — check `PRAGMA table_info(...)` before adding columns, use `CREATE TABLE IF NOT EXISTS`, etc. The migration may be retried on next startup if it crashes partway.
2. Append `("NNN_name", _NNN_func)` to `MIGRATIONS`.
3. If it's a new table or column that a fresh install would need, also update `db._create_schema()` so brand-new DBs get it without running the migration. Use `IF NOT EXISTS` in both places.
4. If you need to disable FKs (e.g. for table swap), do it OUTSIDE `transaction()` — SQLite requires `PRAGMA foreign_keys` to be set when not in a transaction. See migration 002 for the pattern.
5. Test against both a fresh empty DB AND a copy of production.

## Card lifecycle

Each game has at most one "live" card message that gets edited as the roster changes.

- First post: `roster.post_game_card(context, chat_id, game_id)` → `db.set_game_message(game_id, chat_id, msg.message_id)` — rewrites BOTH chat_id and message_id. **Only safe on initial post**.
- Re-render in same chat: `roster.render_card_in_place(context, update, game_id, viewer_user_id)` — edits via `context.bot.edit_message_text(...)`.
- Re-render in a different chat (e.g. opening from `/games` in a DM): post a new message, then `db.set_game_message_only(game_id, msg.message_id)` — preserves the game's tenancy in its original group.

**Never** call `set_game_message` from a re-render path — it would silently move the game to whatever chat the user is currently in.

## Money ball schedule integrity

`bot/moneyball.SCHEDULE` and the schedule embedded in `bot/static/moneyball.html` must match exactly. If you change one without the other, scores will be recorded against the wrong matches. The schedule has been validated: 28 partner pairs (each appearing exactly once across 7 rounds), 28 opponent pairs (each twice). Don't "tweak" it.

## Admin checks

Admin status is per-chat, cached in `chat_members.role` with `telegram_role_checked_at`. Cache TTL is 5 minutes.

- `db.user_is_chat_admin(chat_id, user_id)` — fast cached read.
- Need fresh status (e.g. new chat owner who just promoted themselves)? Call `chats.sync_user_in_chat(bot, chat_id, user_id)` first. `_ensure_admin` in `handlers/merge.py` does exactly this.

## Logging

`logging.getLogger(__name__)` at the top of every module. INFO for routine events (chat registered, migration applied, money ball completed), WARNING for recoverable oddities (Telegram API failure → using stale cache), ERROR/exception for things that lost data or denied a user incorrectly.

The error handler in `handlers/common.error_handler` catches anything that escapes a handler — logs full traceback, replies with a generic message. Don't bare-`except: pass` in handlers; let it bubble.

## What NOT to do

- ❌ Build text or keyboards inline in handlers — go through `views.*`.
- ❌ Reach into another module's `_conn` or other private state — use the public functions.
- ❌ Use Markdown parse mode for any message.
- ❌ Pass float dollars anywhere — always integer cents.
- ❌ Skip `gate()` because "this handler is internal" — every entry point needs it.
- ❌ Hardcode `ALLOWED_GROUP_ID` checks in new code — that env var is legacy fallback only.
- ❌ Add a sleep() or `await asyncio.sleep()` to "work around" Telegram rate limits — the library handles backoff. If you're hitting limits, you're sending too many edit_message calls; debounce instead.
- ❌ Touch `SCHEDULE` in `bot/moneyball.py` without re-validating the partner/opponent invariants AND updating the Mini App.
- ❌ Use `set_game_message` from a re-render path.
- ❌ Call `bot.send_message` on a chat that isn't `is_chat_active()` — paused chats might have removed the bot.
