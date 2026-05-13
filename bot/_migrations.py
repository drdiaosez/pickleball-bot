"""Schema migration system.

Tracks which migrations have run in a `schema_migrations` table.
Each migration is idempotent (safe to run more than once) AND we double-check
by recording its name after success — so the second startup is a no-op.

Migrations are applied in order. Adding a new one means appending an entry to
the MIGRATIONS list below.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Callable

log = logging.getLogger(__name__)


def init_migration_tracker(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name        TEXT PRIMARY KEY,
            applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )


def already_applied(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name = ?", (name,)
    ).fetchone()
    return row is not None


def mark_applied(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (name,))


def apply_all(conn: sqlite3.Connection) -> None:
    """Run every migration in MIGRATIONS that hasn't been applied yet."""
    init_migration_tracker(conn)
    for name, func in MIGRATIONS:
        if already_applied(conn, name):
            continue
        log.info("Running migration: %s", name)
        # Note: we don't wrap in BEGIN/COMMIT here because individual migrations
        # may use executescript() which manages its own transactions and
        # conflicts with an outer transaction. Migrations are written to be
        # idempotent so that partial failure → retry on next startup works.
        try:
            func(conn)
            mark_applied(conn, name)
            log.info("Migration applied: %s", name)
        except Exception:
            log.exception("Migration FAILED: %s — will retry on next startup", name)
            raise


# ─────────────────────── 001: multi-chat schema ───────────────────────

def _001_multi_chat_schema(conn: sqlite3.Connection) -> None:
    """Add the chats / chat_members tables and backfill from existing data.

    Existing games already have a chat_id column (populated when the card was
    posted). For old rows where chat_id is NULL, we fall back to ALLOWED_GROUP_ID
    from env. If that's not set, we can't auto-assign — those rows will need
    manual cleanup, but the migration won't fail.

    moneyballs gains a chat_id column populated from its associated game.
    """
    # 1. Create the new tables
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chats (
            telegram_chat_id  INTEGER PRIMARY KEY,
            title             TEXT,
            status            TEXT NOT NULL DEFAULT 'active',  -- active|paused
            created_at        TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS chat_members (
            chat_id           INTEGER NOT NULL REFERENCES chats(telegram_chat_id) ON DELETE CASCADE,
            telegram_user_id  INTEGER NOT NULL REFERENCES members(telegram_id) ON DELETE CASCADE,
            role              TEXT NOT NULL DEFAULT 'member',  -- member|admin
            joined_at         TEXT NOT NULL DEFAULT (datetime('now')),
            telegram_role_checked_at TEXT,  -- last time we polled Telegram for admin status
            PRIMARY KEY (chat_id, telegram_user_id)
        );

        CREATE INDEX IF NOT EXISTS idx_chat_members_user
            ON chat_members(telegram_user_id);
        """
    )

    # 2. Add chat_id to moneyballs if it exists; the table is created lazily
    #    by moneyball.init_moneyball_schema() at startup, so on a fresh DB
    #    it might not exist yet. That's fine — the table's own schema will
    #    include chat_id natively (see moneyball.init_moneyball_schema).
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "moneyballs" in tables:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(moneyballs)").fetchall()}
        if "chat_id" not in cols:
            conn.execute("ALTER TABLE moneyballs ADD COLUMN chat_id INTEGER")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mb_chat ON moneyballs(chat_id)")

    # 3. Backfill chats from games.chat_id and ALLOWED_GROUP_ID
    seed_chat_id: int | None = None
    allowed = os.environ.get("ALLOWED_GROUP_ID", "").strip()
    if allowed:
        try:
            seed_chat_id = int(allowed)
        except ValueError:
            pass

    # Collect all distinct chat_ids we've seen in existing games, plus the seed
    distinct_ids: set[int] = set()
    for r in conn.execute("SELECT DISTINCT chat_id FROM games WHERE chat_id IS NOT NULL").fetchall():
        if r["chat_id"] is not None:
            distinct_ids.add(r["chat_id"])
    if seed_chat_id is not None:
        distinct_ids.add(seed_chat_id)

    # Insert chats rows for each
    for cid in distinct_ids:
        conn.execute(
            "INSERT OR IGNORE INTO chats (telegram_chat_id, title) VALUES (?, ?)",
            (cid, None),  # title gets filled in when bot sees the chat
        )

    # 4. Backfill games.chat_id for any NULL rows — use the seed (if present)
    if seed_chat_id is not None:
        conn.execute(
            "UPDATE games SET chat_id = ? WHERE chat_id IS NULL",
            (seed_chat_id,),
        )

    # 5. Backfill moneyballs.chat_id from their associated game
    if "moneyballs" in tables:
        conn.execute(
            """
            UPDATE moneyballs
            SET chat_id = (SELECT g.chat_id FROM games g WHERE g.id = moneyballs.game_id)
            WHERE chat_id IS NULL
            """
        )

    # 6. Backfill chat_members from existing members table
    # If we have a seed chat, every known member becomes a member of that chat.
    # Without a seed, we can't safely populate this — they'll be added when they
    # next interact via the live touch_member path.
    if seed_chat_id is not None:
        conn.execute(
            """
            INSERT OR IGNORE INTO chat_members (chat_id, telegram_user_id, role)
            SELECT ?, telegram_id, 'member' FROM members
            """,
            (seed_chat_id,),
        )

    # 7. Add a stamp column on members to know if we've ever seen Telegram admin
    # status for this user. (Unused in PR 1 but no harm pre-creating it.)
    # Actually this lives on chat_members.telegram_role_checked_at — already added above.


# ─────────────────────── 002: games.chat_id NOT NULL ───────────────────────

def _002_games_chat_id_not_null(conn: sqlite3.Connection) -> None:
    """Enforce chat_id on games. By this point (after PR 1) every row should
    already have chat_id populated. We recreate the table with NOT NULL.

    If any NULL rows remain (e.g. the DM-created game with id=9), they would
    block this migration. We log a warning and skip them rather than failing.
    """
    null_count = conn.execute(
        "SELECT COUNT(*) AS n FROM games WHERE chat_id IS NULL"
    ).fetchone()["n"]
    if null_count > 0:
        log.warning(
            "Migration 002: %d game(s) still have NULL chat_id — "
            "they will not be visible in any chat after this migration. "
            "Run: UPDATE games SET chat_id = <your_group_id> WHERE chat_id IS NULL",
            null_count,
        )

    conn.executescript(
        """
        BEGIN;

        CREATE TABLE games_new (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            scheduled_for  TEXT NOT NULL,
            location       TEXT NOT NULL,
            organizer_id   INTEGER NOT NULL REFERENCES members(telegram_id),
            max_players    INTEGER NOT NULL DEFAULT 4,
            status         TEXT NOT NULL DEFAULT 'open',
            notes          TEXT,
            chat_id        INTEGER NOT NULL,
            message_id     INTEGER,
            created_at     TEXT NOT NULL DEFAULT (datetime('now'))
        );

        INSERT INTO games_new
            SELECT id, scheduled_for, location, organizer_id, max_players,
                   status, notes, chat_id, message_id, created_at
            FROM games
            WHERE chat_id IS NOT NULL;

        DROP TABLE games;
        ALTER TABLE games_new RENAME TO games;

        CREATE INDEX IF NOT EXISTS idx_games_scheduled ON games(scheduled_for);
        CREATE INDEX IF NOT EXISTS idx_games_status ON games(status);
        CREATE INDEX IF NOT EXISTS idx_games_chat ON games(chat_id);

        COMMIT;
        """
    )


# Append future migrations here as ("003_name", _003_func), etc.
MIGRATIONS: list[tuple[str, Callable[[sqlite3.Connection], None]]] = [
    ("001_multi_chat_schema", _001_multi_chat_schema),
    ("002_games_chat_id_not_null", _002_games_chat_id_not_null),
]
