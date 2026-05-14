"""SQLite layer.

We use sqlite3 directly (no ORM) — the schema is small enough that an ORM
adds more noise than value. All functions are thin wrappers that return
plain dicts or lists.

Concurrency note: python-telegram-bot dispatches handlers on a single
asyncio loop, so we don't need a connection pool. We open one connection
at startup and use it for everything; SQLite serializes writes internally.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

# Module-level connection, initialized by init_db()
_conn: Optional[sqlite3.Connection] = None


def init_db(path: str) -> None:
    """Open the connection and create tables if they don't exist,
    then run any pending migrations."""
    global _conn
    _conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA foreign_keys = ON")
    _conn.execute("PRAGMA journal_mode = WAL")  # better concurrency
    _create_schema()
    # Migrations run AFTER base schema creation. They handle upgrades on
    # existing databases; new databases get whatever the current migrations
    # produce on top of the base schema.
    from . import _migrations
    _migrations.apply_all(_conn)


def _create_schema() -> None:
    assert _conn is not None
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS members (
            telegram_id   INTEGER PRIMARY KEY,
            display_name  TEXT NOT NULL,
            username      TEXT,
            venmo_handle  TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS games (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            scheduled_for         TEXT NOT NULL,         -- ISO timestamp
            location              TEXT NOT NULL,
            organizer_id          INTEGER NOT NULL REFERENCES members(telegram_id),
            max_players           INTEGER NOT NULL DEFAULT 4,
            status                TEXT NOT NULL DEFAULT 'open',  -- open|cancelled|completed
            notes                 TEXT,
            chat_id               INTEGER,               -- group chat where it was created
            message_id            INTEGER,               -- the card message we can edit
            payment_amount_cents  INTEGER,               -- per-person cost in cents; NULL = none
            created_at            TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_games_scheduled ON games(scheduled_for);
        CREATE INDEX IF NOT EXISTS idx_games_status ON games(status);

        CREATE TABLE IF NOT EXISTS participants (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            status      TEXT NOT NULL,            -- confirmed|waitlist
            position    INTEGER NOT NULL,         -- order within status
            member_id   INTEGER REFERENCES members(telegram_id),
            guest_name  TEXT,
            added_by    INTEGER NOT NULL REFERENCES members(telegram_id),
            added_at    TEXT NOT NULL DEFAULT (datetime('now')),
            is_paid     INTEGER NOT NULL DEFAULT 0,  -- 1 once they've paid; only meaningful when game has payment_amount

            -- exactly one of member_id / guest_name must be set
            CHECK (
                (member_id IS NOT NULL AND guest_name IS NULL) OR
                (member_id IS NULL     AND guest_name IS NOT NULL)
            )
        );

        CREATE INDEX IF NOT EXISTS idx_participants_game ON participants(game_id, status, position);

        -- A member can only appear once per game (guests are unconstrained)
        CREATE UNIQUE INDEX IF NOT EXISTS uniq_member_per_game
            ON participants(game_id, member_id)
            WHERE member_id IS NOT NULL;
        """
    )


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Run a block atomically. isolation_level=None means we manage txns
    explicitly with BEGIN/COMMIT."""
    assert _conn is not None
    _conn.execute("BEGIN")
    try:
        yield _conn
        _conn.execute("COMMIT")
    except Exception:
        _conn.execute("ROLLBACK")
        raise


# ─────────────────────────── members ─────────────────────────── #

def upsert_member(telegram_id: int, display_name: str, username: Optional[str]) -> None:
    """Called on every interaction — keeps the member table fresh."""
    assert _conn is not None
    _conn.execute(
        """
        INSERT INTO members (telegram_id, display_name, username)
        VALUES (?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            display_name = excluded.display_name,
            username     = excluded.username
        """,
        (telegram_id, display_name, username),
    )


def get_member(telegram_id: int) -> Optional[dict]:
    assert _conn is not None
    row = _conn.execute(
        "SELECT * FROM members WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    return dict(row) if row else None


# ─────────────────────── chats / chat_members ─────────────────────── #

def get_chat(telegram_chat_id: int) -> Optional[dict]:
    """Fetch the chats row, or None."""
    assert _conn is not None
    row = _conn.execute(
        "SELECT * FROM chats WHERE telegram_chat_id = ?", (telegram_chat_id,)
    ).fetchone()
    return dict(row) if row else None


def upsert_chat(telegram_chat_id: int, title: Optional[str] = None, status: str = "active") -> None:
    """Create or update a chats row. Doesn't overwrite an existing title with NULL."""
    assert _conn is not None
    _conn.execute(
        """
        INSERT INTO chats (telegram_chat_id, title, status)
        VALUES (?, ?, ?)
        ON CONFLICT(telegram_chat_id) DO UPDATE SET
            title  = COALESCE(excluded.title, chats.title),
            status = excluded.status
        """,
        (telegram_chat_id, title, status),
    )


def update_chat_status(telegram_chat_id: int, status: str) -> None:
    assert _conn is not None
    _conn.execute(
        "UPDATE chats SET status = ? WHERE telegram_chat_id = ?",
        (status, telegram_chat_id),
    )


def migrate_chat_id(old_chat_id: int, new_chat_id: int) -> dict:
    """Re-key every reference from old_chat_id to new_chat_id.

    Called when Telegram converts a regular group to a supergroup. Telegram
    assigns the supergroup a brand new chat_id, so all rows that reference the
    old id (chats, chat_members, games, moneyballs) need to be re-pointed.

    Handles the common case where the bot's `on_my_chat_member` handler has
    already inserted a stub row for the new supergroup: we merge by preferring
    the old chat's data (it has the real history, the stub only knows about
    users who have messaged since the migration).

    Idempotent: calling it twice with the same args is a no-op on the second
    call (because old rows no longer exist).

    Returns a summary dict of rows touched, for logging.
    """
    assert _conn is not None

    if old_chat_id == new_chat_id:
        return {"no_op": "old and new ids are identical"}

    old_row = get_chat(old_chat_id)
    if old_row is None:
        # Nothing to migrate. Could happen if we get a duplicate MIGRATE event
        # after the first one already succeeded.
        return {"no_op": f"no rows for old chat_id {old_chat_id}"}

    summary: dict = {"old_chat_id": old_chat_id, "new_chat_id": new_chat_id}

    # Use a transaction with deferred FK checks. The FK on
    # chat_members.chat_id -> chats(telegram_chat_id) would otherwise fire
    # mid-statement when we change chats.telegram_chat_id while chat_members
    # rows still point at the old value.
    with transaction() as conn:
        conn.execute("PRAGMA defer_foreign_keys = ON")

        # If a stub row for the new id already exists (created by
        # on_my_chat_member when the supergroup first appeared), pull off any
        # chat_members rows it has so they don't conflict with the old rows
        # we're about to re-key. We'll re-add the unique ones afterwards.
        stub_member_ids: list[int] = []
        if get_chat(new_chat_id) is not None:
            stub_member_ids = [
                r["telegram_user_id"]
                for r in conn.execute(
                    "SELECT telegram_user_id FROM chat_members WHERE chat_id = ?",
                    (new_chat_id,),
                )
            ]
            conn.execute("DELETE FROM chat_members WHERE chat_id = ?", (new_chat_id,))
            conn.execute("DELETE FROM chats WHERE telegram_chat_id = ?", (new_chat_id,))
            summary["stub_removed"] = True
            summary["stub_member_ids"] = stub_member_ids

        # Re-key the parent row first; FK checks are deferred so the children
        # can be re-pointed in subsequent statements within this txn.
        conn.execute(
            "UPDATE chats SET telegram_chat_id = ? WHERE telegram_chat_id = ?",
            (new_chat_id, old_chat_id),
        )

        # Re-key dependent tables.
        for table in ("chat_members", "games", "moneyballs"):
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            cur = conn.execute(
                f"UPDATE {table} SET chat_id = ? WHERE chat_id = ?",
                (new_chat_id, old_chat_id),
            )
            summary[f"{table}_updated"] = cur.rowcount

        # Re-add any users that the stub knew about but the old chat didn't
        # (rare: users who joined the group AFTER it migrated, before this
        # handler fires). Default role; the next interaction's gate() will
        # refresh from Telegram.
        added_back = 0
        for uid in stub_member_ids:
            existing = conn.execute(
                "SELECT 1 FROM chat_members WHERE chat_id = ? AND telegram_user_id = ?",
                (new_chat_id, uid),
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO chat_members (chat_id, telegram_user_id, role) "
                    "VALUES (?, ?, 'member')",
                    (new_chat_id, uid),
                )
                added_back += 1
        if stub_member_ids:
            summary["stub_members_readded"] = added_back

    return summary


def is_chat_active(telegram_chat_id: int) -> bool:
    """True iff the chats row exists and status = 'active'."""
    row = get_chat(telegram_chat_id)
    return bool(row and row.get("status") == "active")


def list_active_chats_for_user(user_id: int) -> list[dict]:
    """All active chats this user is a member of, ordered by joined_at.
    Used by the DM picker in PR 4."""
    assert _conn is not None
    rows = _conn.execute(
        """
        SELECT c.* FROM chats c
        JOIN chat_members cm ON cm.chat_id = c.telegram_chat_id
        WHERE cm.telegram_user_id = ? AND c.status = 'active'
        ORDER BY cm.joined_at ASC
        """,
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_chat_member(chat_id: int, user_id: int) -> Optional[dict]:
    assert _conn is not None
    row = _conn.execute(
        "SELECT * FROM chat_members WHERE chat_id = ? AND telegram_user_id = ?",
        (chat_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def upsert_chat_member(
    chat_id: int,
    user_id: int,
    role: str = "member",
    telegram_role_checked_at: Optional[str] = None,
) -> None:
    """Insert or update a chat_members row.

    Updates `role` and `telegram_role_checked_at` on conflict, but doesn't
    touch joined_at (preserves the original join timestamp).
    """
    assert _conn is not None
    _conn.execute(
        """
        INSERT INTO chat_members (chat_id, telegram_user_id, role, telegram_role_checked_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id, telegram_user_id) DO UPDATE SET
            role = excluded.role,
            telegram_role_checked_at = COALESCE(
                excluded.telegram_role_checked_at,
                chat_members.telegram_role_checked_at
            )
        """,
        (chat_id, user_id, role, telegram_role_checked_at),
    )


def remove_chat_member(chat_id: int, user_id: int) -> None:
    """Used when Telegram tells us a user left or was kicked."""
    assert _conn is not None
    _conn.execute(
        "DELETE FROM chat_members WHERE chat_id = ? AND telegram_user_id = ?",
        (chat_id, user_id),
    )


def user_is_chat_admin(chat_id: int, user_id: int) -> bool:
    """Cached admin check. Callers needing freshness should sync first."""
    row = get_chat_member(chat_id, user_id)
    return bool(row and row.get("role") == "admin")


# ─────────────────────────── games ─────────────────────────── #

def create_game(
    scheduled_for: datetime,
    location: str,
    organizer_id: int,
    max_players: int = 4,
    notes: Optional[str] = None,
    chat_id: Optional[int] = None,
    payment_amount_cents: Optional[int] = None,
) -> int:
    assert _conn is not None
    cur = _conn.execute(
        """
        INSERT INTO games (scheduled_for, location, organizer_id, max_players, notes, chat_id, payment_amount_cents)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (scheduled_for.isoformat(), location, organizer_id, max_players, notes, chat_id, payment_amount_cents),
    )
    return cur.lastrowid


def get_game(game_id: int) -> Optional[dict]:
    assert _conn is not None
    row = _conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    return dict(row) if row else None


def set_game_message(game_id: int, chat_id: int, message_id: int) -> None:
    """Set the canonical card location (chat_id + message_id).

    Used ONLY when first posting a card for a new game, where the posting
    chat IS the chat the game belongs to. Do NOT call from re-renders that
    open the card in a DM — that would rewrite the game's tenancy. Use
    set_game_message_only() for those.
    """
    assert _conn is not None
    _conn.execute(
        "UPDATE games SET chat_id = ?, message_id = ? WHERE id = ?",
        (chat_id, message_id, game_id),
    )


def set_game_message_only(game_id: int, message_id: int) -> None:
    """Update just the canonical message reference, NOT the chat_id.

    Used when re-rendering a card in a different chat (e.g. opening a game
    from `/games` in a DM). The game still belongs to its original group;
    we just point at the most recent rendered copy for future edits.
    """
    assert _conn is not None
    _conn.execute(
        "UPDATE games SET message_id = ? WHERE id = ?",
        (message_id, game_id),
    )


def list_upcoming_games(tz: Optional["ZoneInfo"] = None, chat_id: Optional[int] = None) -> list[dict]:
    """Open games scheduled for today or any future day (in `tz`).
    Sooner first.

    A game at 9:30 AM today still counts as "upcoming" all day, even after
    it's been played — only at midnight tomorrow does it drop off. Pass tz
    to control which day-boundary is used; defaults to UTC if omitted.
    Pass chat_id to restrict to a specific group chat.
    """
    assert _conn is not None
    if chat_id is not None:
        rows = _conn.execute(
            "SELECT * FROM games WHERE status = 'open' AND chat_id = ? ORDER BY scheduled_for ASC",
            (chat_id,),
        ).fetchall()
    else:
        rows = _conn.execute(
            "SELECT * FROM games WHERE status = 'open' ORDER BY scheduled_for ASC"
        ).fetchall()
    if tz is None:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("UTC")
    today_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["scheduled_for"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if dt >= today_start:
            out.append(dict(r))
    return out


def list_past_games(limit: int = 50, tz: Optional["ZoneInfo"] = None, chat_id: Optional[int] = None) -> list[dict]:
    """Games whose scheduled date is before today (in `tz`), most recent first.
    Capped at `limit` to avoid huge replies. Pass chat_id to restrict to one group."""
    assert _conn is not None
    if chat_id is not None:
        rows = _conn.execute(
            "SELECT * FROM games WHERE status != 'cancelled' AND chat_id = ? ORDER BY scheduled_for DESC",
            (chat_id,),
        ).fetchall()
    else:
        rows = _conn.execute(
            "SELECT * FROM games WHERE status != 'cancelled' ORDER BY scheduled_for DESC"
        ).fetchall()
    if tz is None:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("UTC")
    today_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["scheduled_for"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if dt < today_start:
            out.append(dict(r))
            if len(out) >= limit:
                break
    return out


def list_games_for_member(member_id: int, tz: Optional["ZoneInfo"] = None, chat_id: Optional[int] = None) -> list[dict]:
    """Upcoming games (today or later in `tz`) that the member is in.
    Pass chat_id to restrict to one group chat."""
    assert _conn is not None
    if chat_id is not None:
        rows = _conn.execute(
            """
            SELECT g.* FROM games g
            JOIN participants p ON p.game_id = g.id
            WHERE g.status = 'open' AND p.member_id = ? AND g.chat_id = ?
            ORDER BY g.scheduled_for ASC
            """,
            (member_id, chat_id),
        ).fetchall()
    else:
        rows = _conn.execute(
            """
            SELECT g.* FROM games g
            JOIN participants p ON p.game_id = g.id
            WHERE g.status = 'open' AND p.member_id = ?
            ORDER BY g.scheduled_for ASC
            """,
            (member_id,),
        ).fetchall()
    if tz is None:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("UTC")
    today_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["scheduled_for"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if dt >= today_start:
            out.append(dict(r))
    return out


def list_games_in_range(start: datetime, end: datetime, chat_id: Optional[int] = None) -> list[dict]:
    """All non-cancelled games where start <= scheduled_for < end.
    Both bounds must be timezone-aware. Soonest first.
    Pass chat_id to restrict to one group chat."""
    assert _conn is not None
    if chat_id is not None:
        rows = _conn.execute(
            "SELECT * FROM games WHERE status != 'cancelled' AND chat_id = ? ORDER BY scheduled_for ASC",
            (chat_id,),
        ).fetchall()
    else:
        rows = _conn.execute(
            "SELECT * FROM games WHERE status != 'cancelled' ORDER BY scheduled_for ASC"
        ).fetchall()
    out = []
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["scheduled_for"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if start <= dt < end:
            out.append(dict(r))
    return out


def list_members_not_in_game(game_id: int, chat_id: Optional[int] = None) -> list[dict]:
    """All known members who aren't already on a game's roster (confirmed or waitlist).
    Used to populate the 'Add Member' picker. Sorted by display_name.
    If chat_id is given, restricts to members of that chat only."""
    assert _conn is not None
    if chat_id is not None:
        rows = _conn.execute(
            """
            SELECT m.* FROM members m
            JOIN chat_members cm ON cm.telegram_user_id = m.telegram_id AND cm.chat_id = ?
            WHERE NOT EXISTS (
                SELECT 1 FROM participants p
                WHERE p.game_id = ? AND p.member_id = m.telegram_id
            )
            ORDER BY LOWER(m.display_name) ASC
            """,
            (chat_id, game_id),
        ).fetchall()
    else:
        rows = _conn.execute(
            """
            SELECT m.* FROM members m
            WHERE NOT EXISTS (
                SELECT 1 FROM participants p
                WHERE p.game_id = ? AND p.member_id = m.telegram_id
            )
            ORDER BY LOWER(m.display_name) ASC
            """,
            (game_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def cancel_game(game_id: int) -> None:
    assert _conn is not None
    _conn.execute("UPDATE games SET status = 'cancelled' WHERE id = ?", (game_id,))


def delete_game(game_id: int) -> None:
    """Hard-delete a game and all its participants (cascade)."""
    assert _conn is not None
    _conn.execute("DELETE FROM games WHERE id = ?", (game_id,))


def update_game_time(game_id: int, scheduled_for: datetime) -> None:
    assert _conn is not None
    _conn.execute(
        "UPDATE games SET scheduled_for = ? WHERE id = ?",
        (scheduled_for.isoformat(), game_id),
    )


def update_game_location(game_id: int, location: str) -> None:
    assert _conn is not None
    _conn.execute("UPDATE games SET location = ? WHERE id = ?", (location, game_id))


def update_game_notes(game_id: int, notes: Optional[str]) -> None:
    """Set or clear a game's notes/description. Pass None to clear."""
    assert _conn is not None
    _conn.execute("UPDATE games SET notes = ? WHERE id = ?", (notes, game_id))


def update_game_payment_amount(game_id: int, amount_cents: Optional[int]) -> None:
    """Set or clear a game's per-person payment amount.

    Pass None (or 0) to clear; we normalize both to NULL so the UI treats
    them the same way ("no payment to track"). When a payment amount is
    cleared, we also reset every participant's paid flag — those marks
    were meaningless without an amount and would be confusing if a future
    edit re-introduced one.
    """
    assert _conn is not None
    if amount_cents is None or amount_cents <= 0:
        with transaction():
            _conn.execute("UPDATE games SET payment_amount_cents = NULL WHERE id = ?", (game_id,))
            _conn.execute("UPDATE participants SET is_paid = 0 WHERE game_id = ?", (game_id,))
    else:
        _conn.execute(
            "UPDATE games SET payment_amount_cents = ? WHERE id = ?",
            (int(amount_cents), game_id),
        )


def set_participant_paid(participant_id: int, paid: bool) -> Optional[dict]:
    """Set a participant's is_paid flag. Returns the updated participant row."""
    assert _conn is not None
    _conn.execute(
        "UPDATE participants SET is_paid = ? WHERE id = ?",
        (1 if paid else 0, participant_id),
    )
    return get_participant(participant_id)


def toggle_participant_paid(participant_id: int) -> Optional[dict]:
    """Flip is_paid on a participant. Returns the updated participant row."""
    p = get_participant(participant_id)
    if not p:
        return None
    return set_participant_paid(participant_id, not bool(p.get("is_paid")))


# ─────────────────────────── guest → member merge ─────────────────────────── #

def find_guest_appearances(guest_name: str) -> dict:
    """Return what would be merged if `guest_name` were promoted to a member.

    Returns:
      {
        "guest_name_input": str,            # what the caller passed
        "match_count": int,                 # how many distinct case-insensitive matches we found
        "canonical_names": list[str],       # the actual spellings found (for display)
        "moneyball_entries": int,           # count of moneyball_players rows
        "participant_entries": int,         # count of participants rows
        "moneyball_ids": list[int],
        "game_ids": list[int],
      }
    """
    assert _conn is not None
    needle = guest_name.strip().lower()

    canonical = _conn.execute(
        """
        SELECT DISTINCT name FROM (
            SELECT guest_name AS name FROM moneyball_players
                WHERE guest_name IS NOT NULL AND LOWER(TRIM(guest_name)) = ?
            UNION
            SELECT guest_name AS name FROM participants
                WHERE guest_name IS NOT NULL AND LOWER(TRIM(guest_name)) = ?
        )
        """,
        (needle, needle),
    ).fetchall()
    canonical_names = [r["name"] for r in canonical]

    mb_rows = _conn.execute(
        """
        SELECT id, moneyball_id FROM (
            SELECT rowid AS id, moneyball_id FROM moneyball_players
            WHERE LOWER(TRIM(guest_name)) = ?
        )
        """,
        (needle,),
    ).fetchall()
    moneyball_ids = sorted({r["moneyball_id"] for r in mb_rows})

    p_rows = _conn.execute(
        """
        SELECT id, game_id FROM participants
        WHERE LOWER(TRIM(guest_name)) = ?
        """,
        (needle,),
    ).fetchall()
    game_ids = sorted({r["game_id"] for r in p_rows})

    return {
        "guest_name_input": guest_name,
        "match_count": len(canonical_names),
        "canonical_names": canonical_names,
        "moneyball_entries": len(mb_rows),
        "participant_entries": len(p_rows),
        "moneyball_ids": moneyball_ids,
        "game_ids": game_ids,
    }


def merge_guest_into_member(guest_name: str, member_id: int) -> dict:
    """Promote every guest entry matching `guest_name` (case-insensitive, trimmed)
    to belong to `member_id`.

    Constraints to respect:
      - In `moneyball_players`, a money ball can't have the same member at two
        seats — if the member already occupies a seat in a money ball that also
        has the guest, the merge is unsafe for that money ball.
      - In `participants`, the same member can't be in a game's roster twice
        (DB enforces this). Skip games where member is already a participant.

    Returns a report:
      {
        "merged_moneyball_entries": int,
        "merged_participant_entries": int,
        "skipped_moneyball_ids": list[int],   # had a conflict
        "skipped_game_ids": list[int],        # member already in roster
        "renamed_moneyball_ids": list[int],   # all touched
        "renamed_game_ids": list[int],        # all touched
      }
    """
    assert _conn is not None
    if get_member(member_id) is None:
        raise ValueError(f"member {member_id} not found")

    needle = guest_name.strip().lower()
    merged_mb = 0
    merged_p = 0
    skipped_mb_ids: list[int] = []
    skipped_game_ids: list[int] = []
    touched_mb_ids: set[int] = set()
    touched_game_ids: set[int] = set()

    with transaction():
        # ── moneyball_players ──
        mb_rows = _conn.execute(
            """
            SELECT moneyball_id, seat FROM moneyball_players
            WHERE LOWER(TRIM(guest_name)) = ?
            """,
            (needle,),
        ).fetchall()

        for r in mb_rows:
            mb_id = r["moneyball_id"]
            # Conflict? Same member already in this money ball as another seat
            conflict = _conn.execute(
                """
                SELECT 1 FROM moneyball_players
                WHERE moneyball_id = ? AND member_id = ?
                """,
                (mb_id, member_id),
            ).fetchone()
            if conflict:
                skipped_mb_ids.append(mb_id)
                continue
            _conn.execute(
                """
                UPDATE moneyball_players
                SET member_id = ?, guest_name = NULL
                WHERE moneyball_id = ? AND seat = ?
                """,
                (member_id, mb_id, r["seat"]),
            )
            merged_mb += 1
            touched_mb_ids.add(mb_id)

        # ── participants (game signups) ──
        p_rows = _conn.execute(
            """
            SELECT id, game_id FROM participants
            WHERE LOWER(TRIM(guest_name)) = ?
            """,
            (needle,),
        ).fetchall()

        for r in p_rows:
            game_id = r["game_id"]
            existing = _conn.execute(
                "SELECT 1 FROM participants WHERE game_id = ? AND member_id = ?",
                (game_id, member_id),
            ).fetchone()
            if existing:
                skipped_game_ids.append(game_id)
                continue
            _conn.execute(
                """
                UPDATE participants
                SET member_id = ?, guest_name = NULL
                WHERE id = ?
                """,
                (member_id, r["id"]),
            )
            merged_p += 1
            touched_game_ids.add(game_id)

    return {
        "merged_moneyball_entries": merged_mb,
        "merged_participant_entries": merged_p,
        "skipped_moneyball_ids": sorted(set(skipped_mb_ids)),
        "skipped_game_ids": sorted(set(skipped_game_ids)),
        "renamed_moneyball_ids": sorted(touched_mb_ids),
        "renamed_game_ids": sorted(touched_game_ids),
    }


def find_member_by_username_or_id(query: str) -> Optional[dict]:
    """Resolve a string to a member. Accepts:
      - "@username" or "username"
      - "12345" (Telegram user ID)
    Returns member dict or None.
    """
    assert _conn is not None
    q = query.strip().lstrip("@")
    # Try numeric ID first
    if q.isdigit():
        return get_member(int(q))
    # Then username (case-insensitive)
    row = _conn.execute(
        "SELECT * FROM members WHERE LOWER(username) = LOWER(?)", (q,)
    ).fetchone()
    return dict(row) if row else None


def update_game_max(game_id: int, max_players: int) -> dict:
    """Change max_players. Returns {'demoted': [...], 'promoted': [...]}
    so callers can notify the affected members.

    Shrinking moves the most-recently-added confirmed players to the top of
    the waitlist (preserving their joining order at the top). Growing
    auto-promotes from the front of the waitlist.
    """
    assert _conn is not None
    demoted_ids: list[int] = []
    promoted_ids: list[int] = []
    with transaction():
        current = confirmed_count(game_id)
        if max_players < current:
            excess = current - max_players
            to_demote = _conn.execute(
                """
                SELECT id FROM participants
                WHERE game_id = ? AND status = 'confirmed'
                ORDER BY position DESC
                LIMIT ?
                """,
                (game_id, excess),
            ).fetchall()
            _conn.execute(
                "UPDATE participants SET position = position + ? WHERE game_id = ? AND status = 'waitlist'",
                (excess, game_id),
            )
            for i, row in enumerate(to_demote, start=1):
                _conn.execute(
                    "UPDATE participants SET status = 'waitlist', position = ? WHERE id = ?",
                    (i, row["id"]),
                )
                demoted_ids.append(row["id"])
            _renumber(game_id, "confirmed")
        elif max_players > current:
            slots_to_fill = max_players - current
            promote_rows = _conn.execute(
                """
                SELECT id FROM participants
                WHERE game_id = ? AND status = 'waitlist'
                ORDER BY position ASC
                LIMIT ?
                """,
                (game_id, slots_to_fill),
            ).fetchall()
            for row in promote_rows:
                new_pos = _next_position(game_id, "confirmed")
                _conn.execute(
                    "UPDATE participants SET status = 'confirmed', position = ? WHERE id = ?",
                    (new_pos, row["id"]),
                )
                promoted_ids.append(row["id"])
            _renumber(game_id, "waitlist")

        _conn.execute("UPDATE games SET max_players = ? WHERE id = ?", (max_players, game_id))

    return {
        "demoted": [get_participant(pid) for pid in demoted_ids],
        "promoted": [get_participant(pid) for pid in promoted_ids],
    }


# ─────────────────────────── participants ─────────────────────────── #

def get_participants(game_id: int) -> list[dict]:
    """Returns all participants with member display_name resolved."""
    assert _conn is not None
    rows = _conn.execute(
        """
        SELECT
            p.*,
            m.display_name AS member_name,
            adder.display_name AS adder_name
        FROM participants p
        LEFT JOIN members m     ON m.telegram_id = p.member_id
        JOIN members adder      ON adder.telegram_id = p.added_by
        WHERE p.game_id = ?
        ORDER BY p.status DESC, p.position ASC
        """,
        (game_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_participant(participant_id: int) -> Optional[dict]:
    assert _conn is not None
    row = _conn.execute(
        """
        SELECT
            p.*,
            m.display_name AS member_name,
            adder.display_name AS adder_name
        FROM participants p
        LEFT JOIN members m     ON m.telegram_id = p.member_id
        JOIN members adder      ON adder.telegram_id = p.added_by
        WHERE p.id = ?
        """,
        (participant_id,),
    ).fetchone()
    return dict(row) if row else None


def _next_position(game_id: int, status: str) -> int:
    assert _conn is not None
    row = _conn.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 AS next FROM participants WHERE game_id = ? AND status = ?",
        (game_id, status),
    ).fetchone()
    return row["next"]


def confirmed_count(game_id: int) -> int:
    assert _conn is not None
    row = _conn.execute(
        "SELECT COUNT(*) AS n FROM participants WHERE game_id = ? AND status = 'confirmed'",
        (game_id,),
    ).fetchone()
    return row["n"]


def member_is_in_game(game_id: int, member_id: int) -> Optional[dict]:
    """Returns the participant row if the member is in the game (confirmed or waitlist)."""
    assert _conn is not None
    row = _conn.execute(
        "SELECT * FROM participants WHERE game_id = ? AND member_id = ?",
        (game_id, member_id),
    ).fetchone()
    return dict(row) if row else None


def add_participant(
    game_id: int,
    added_by: int,
    member_id: Optional[int] = None,
    guest_name: Optional[str] = None,
    force_waitlist: bool = False,
) -> dict:
    """Add someone to a game. Auto-decides confirmed vs waitlist based on capacity.

    Returns dict with keys: status, position, participant_id.
    """
    assert _conn is not None
    game = get_game(game_id)
    if not game:
        raise ValueError("game not found")

    # Member uniqueness is enforced by the DB, but we want a clean error
    if member_id is not None:
        existing = member_is_in_game(game_id, member_id)
        if existing:
            raise ValueError(f"already_{existing['status']}")

    with transaction():
        if force_waitlist or confirmed_count(game_id) >= game["max_players"]:
            status = "waitlist"
        else:
            status = "confirmed"
        position = _next_position(game_id, status)

        cur = _conn.execute(
            """
            INSERT INTO participants (game_id, status, position, member_id, guest_name, added_by)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (game_id, status, position, member_id, guest_name, added_by),
        )
        return {"status": status, "position": position, "participant_id": cur.lastrowid}


def remove_participant(participant_id: int) -> Optional[dict]:
    """Remove a participant. If they were confirmed, promote the oldest waitlist entry.

    Returns the promoted participant dict (or None if nobody was promoted).
    """
    assert _conn is not None
    p = get_participant(participant_id)
    if not p:
        return None

    promoted = None
    with transaction():
        _conn.execute("DELETE FROM participants WHERE id = ?", (participant_id,))

        # Renumber positions in the slot they vacated
        _renumber(p["game_id"], p["status"])

        # If they were confirmed, promote top of waitlist
        if p["status"] == "confirmed":
            promoted_row = _conn.execute(
                """
                SELECT * FROM participants
                WHERE game_id = ? AND status = 'waitlist'
                ORDER BY position ASC LIMIT 1
                """,
                (p["game_id"],),
            ).fetchone()
            if promoted_row:
                pid = promoted_row["id"]
                new_pos = _next_position(p["game_id"], "confirmed")
                _conn.execute(
                    "UPDATE participants SET status = 'confirmed', position = ? WHERE id = ?",
                    (new_pos, pid),
                )
                _renumber(p["game_id"], "waitlist")
                promoted = get_participant(pid)

    return promoted


def _renumber(game_id: int, status: str) -> None:
    """Recompact positions to 1..N after a deletion."""
    assert _conn is not None
    rows = _conn.execute(
        "SELECT id FROM participants WHERE game_id = ? AND status = ? ORDER BY position ASC",
        (game_id, status),
    ).fetchall()
    for i, r in enumerate(rows, start=1):
        _conn.execute("UPDATE participants SET position = ? WHERE id = ?", (i, r["id"]))


def swap_with_waitlist(confirmed_pid: int, waitlist_pid: int) -> tuple[dict, dict]:
    """Soft swap: move a confirmed participant to the top of the waitlist
    and promote a waitlisted participant to confirmed.

    The previously-confirmed person becomes waitlist position 1 (not deleted).
    Returns (newly_confirmed, newly_waitlisted).
    """
    assert _conn is not None
    confirmed = get_participant(confirmed_pid)
    waitlisted = get_participant(waitlist_pid)
    if not confirmed or not waitlisted:
        raise ValueError("participant not found")
    if confirmed["status"] != "confirmed" or waitlisted["status"] != "waitlist":
        raise ValueError("invalid swap states")
    if confirmed["game_id"] != waitlisted["game_id"]:
        raise ValueError("not same game")

    game_id = confirmed["game_id"]

    with transaction():
        # Bump everyone on the waitlist by 1 to make room at position 1
        _conn.execute(
            "UPDATE participants SET position = position + 1 WHERE game_id = ? AND status = 'waitlist'",
            (game_id,),
        )
        # Move confirmed → waitlist position 1
        _conn.execute(
            "UPDATE participants SET status = 'waitlist', position = 1 WHERE id = ?",
            (confirmed_pid,),
        )
        # Move waitlisted → confirmed (take the freed slot)
        new_pos = _next_position(game_id, "confirmed")
        _conn.execute(
            "UPDATE participants SET status = 'confirmed', position = ? WHERE id = ?",
            (new_pos, waitlist_pid),
        )
        # Recompact both slots
        _renumber(game_id, "confirmed")
        _renumber(game_id, "waitlist")

    return get_participant(waitlist_pid), get_participant(confirmed_pid)


def promote_top_of_waitlist(game_id: int) -> Optional[dict]:
    """Manually promote the top of the waitlist (used when an empty slot exists)."""
    assert _conn is not None
    row = _conn.execute(
        "SELECT * FROM participants WHERE game_id = ? AND status = 'waitlist' ORDER BY position ASC LIMIT 1",
        (game_id,),
    ).fetchone()
    if not row:
        return None
    pid = row["id"]
    with transaction():
        new_pos = _next_position(game_id, "confirmed")
        _conn.execute(
            "UPDATE participants SET status = 'confirmed', position = ? WHERE id = ?",
            (new_pos, pid),
        )
        _renumber(game_id, "waitlist")
    return get_participant(pid)


def demote_to_waitlist(participant_id: int) -> Optional[dict]:
    """Move a confirmed participant down to the bottom of the waitlist.

    If anyone on the waitlist exists, promote the top one to fill the slot.
    Returns the auto-promoted participant (or None).
    """
    assert _conn is not None
    p = get_participant(participant_id)
    if not p or p["status"] != "confirmed":
        return None

    game_id = p["game_id"]
    promoted = None
    with transaction():
        new_wait_pos = _next_position(game_id, "waitlist")
        _conn.execute(
            "UPDATE participants SET status = 'waitlist', position = ? WHERE id = ?",
            (new_wait_pos, participant_id),
        )
        _renumber(game_id, "confirmed")

        # Auto-promote top of waitlist (excluding the one we just demoted)
        top = _conn.execute(
            """
            SELECT * FROM participants
            WHERE game_id = ? AND status = 'waitlist' AND id != ?
            ORDER BY position ASC LIMIT 1
            """,
            (game_id, participant_id),
        ).fetchone()
        if top:
            new_pos = _next_position(game_id, "confirmed")
            _conn.execute(
                "UPDATE participants SET status = 'confirmed', position = ? WHERE id = ?",
                (new_pos, top["id"]),
            )
            _renumber(game_id, "waitlist")
            promoted = get_participant(top["id"])

    return promoted
