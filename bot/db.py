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
    """Open the connection and create tables if they don't exist."""
    global _conn
    _conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA foreign_keys = ON")
    _conn.execute("PRAGMA journal_mode = WAL")  # better concurrency
    _create_schema()


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
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            scheduled_for  TEXT NOT NULL,         -- ISO timestamp
            location       TEXT NOT NULL,
            organizer_id   INTEGER NOT NULL REFERENCES members(telegram_id),
            max_players    INTEGER NOT NULL DEFAULT 4,
            status         TEXT NOT NULL DEFAULT 'open',  -- open|cancelled|completed
            notes          TEXT,
            chat_id        INTEGER,               -- group chat where it was created
            message_id     INTEGER,               -- the card message we can edit
            created_at     TEXT NOT NULL DEFAULT (datetime('now'))
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


# ─────────────────────────── games ─────────────────────────── #

def create_game(
    scheduled_for: datetime,
    location: str,
    organizer_id: int,
    max_players: int = 4,
    notes: Optional[str] = None,
    chat_id: Optional[int] = None,
) -> int:
    assert _conn is not None
    cur = _conn.execute(
        """
        INSERT INTO games (scheduled_for, location, organizer_id, max_players, notes, chat_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (scheduled_for.isoformat(), location, organizer_id, max_players, notes, chat_id),
    )
    return cur.lastrowid


def get_game(game_id: int) -> Optional[dict]:
    assert _conn is not None
    row = _conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    return dict(row) if row else None


def set_game_message(game_id: int, chat_id: int, message_id: int) -> None:
    assert _conn is not None
    _conn.execute(
        "UPDATE games SET chat_id = ?, message_id = ? WHERE id = ?",
        (chat_id, message_id, game_id),
    )


def list_upcoming_games() -> list[dict]:
    """Open games whose scheduled time is in the future (or in the last 30 min,
    so a game that just started doesn't disappear mid-play). Soonest first.

    We pull all open games and filter in Python because the stored ISO strings
    have timezone offsets and SQLite's datetime() returns UTC — comparing them
    via SQL string comparison is fragile.
    """
    assert _conn is not None
    rows = _conn.execute(
        "SELECT * FROM games WHERE status = 'open' ORDER BY scheduled_for ASC"
    ).fetchall()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    out = []
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["scheduled_for"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if dt >= cutoff:
            out.append(dict(r))
    return out


def list_past_games(limit: int = 50) -> list[dict]:
    """Games whose scheduled time has already passed (open or completed).
    Most recent first. Capped to avoid huge replies."""
    assert _conn is not None
    rows = _conn.execute(
        "SELECT * FROM games WHERE status != 'cancelled' ORDER BY scheduled_for DESC"
    ).fetchall()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    out = []
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["scheduled_for"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if dt < cutoff:
            out.append(dict(r))
            if len(out) >= limit:
                break
    return out


def list_games_in_range(start: datetime, end: datetime) -> list[dict]:
    """All non-cancelled games where start <= scheduled_for < end.
    Both bounds must be timezone-aware. Soonest first."""
    assert _conn is not None
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


def list_games_for_member(member_id: int) -> list[dict]:
    """Upcoming games (future + ~30 min grace) that the member is in."""
    assert _conn is not None
    rows = _conn.execute(
        """
        SELECT g.* FROM games g
        JOIN participants p ON p.game_id = g.id
        WHERE g.status = 'open' AND p.member_id = ?
        ORDER BY g.scheduled_for ASC
        """,
        (member_id,),
    ).fetchall()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    out = []
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["scheduled_for"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if dt >= cutoff:
            out.append(dict(r))
    return out


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
