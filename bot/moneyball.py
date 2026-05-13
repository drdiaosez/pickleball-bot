"""Money ball persistence.

Schema additions (created lazily by init_moneyball_schema):
  moneyballs           — one row per tournament
  moneyball_players    — 8 rows per tournament, mapping seat 0..7 to a member
  moneyball_matches    — 14 rows per tournament (7 rounds × 2 courts)

The schedule itself is hardcoded (SCHEDULE) — it's a mathematical structure,
not data. Storing it would be redundant and risk drift between the Mini App
and the backend.

Standings logic lives here too, so it's identical for the live API, the
leaderboard, and any other place that needs to know who won.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from . import db  # access to db._conn and db.transaction

# Same schedule as the Mini App. Validated: 28 partner pairs (each once),
# 28 opponent pairs (each twice). DO NOT change without re-validating.
SCHEDULE = [
    [[[0, 1], [2, 3]], [[4, 5], [6, 7]]],
    [[[0, 2], [4, 6]], [[1, 3], [5, 7]]],
    [[[0, 3], [4, 7]], [[1, 2], [5, 6]]],
    [[[0, 4], [1, 5]], [[2, 6], [3, 7]]],
    [[[0, 5], [2, 7]], [[1, 4], [3, 6]]],
    [[[0, 6], [3, 5]], [[1, 7], [2, 4]]],
    [[[0, 7], [1, 6]], [[2, 5], [3, 4]]],
]
TOTAL_ROUNDS = len(SCHEDULE)
COURTS_PER_ROUND = 2
TOTAL_MATCHES = TOTAL_ROUNDS * COURTS_PER_ROUND  # 14


def init_moneyball_schema() -> None:
    """Idempotent: create the moneyball tables if they don't exist,
    and migrate any older schema to the current one."""
    assert db._conn is not None
    db._conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS moneyballs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id       INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            status        TEXT NOT NULL DEFAULT 'in_progress',  -- in_progress|completed
            created_by    INTEGER NOT NULL REFERENCES members(telegram_id),
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_mb_game ON moneyballs(game_id);
        CREATE INDEX IF NOT EXISTS idx_mb_status ON moneyballs(status);

        CREATE TABLE IF NOT EXISTS moneyball_players (
            moneyball_id INTEGER NOT NULL REFERENCES moneyballs(id) ON DELETE CASCADE,
            seat         INTEGER NOT NULL CHECK (seat BETWEEN 0 AND 7),
            member_id    INTEGER REFERENCES members(telegram_id),
            guest_name   TEXT,
            added_by     INTEGER REFERENCES members(telegram_id),
            PRIMARY KEY (moneyball_id, seat),
            CHECK (
                (member_id IS NOT NULL AND guest_name IS NULL) OR
                (member_id IS NULL     AND guest_name IS NOT NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS idx_mbp_member ON moneyball_players(member_id);

        CREATE TABLE IF NOT EXISTS moneyball_matches (
            moneyball_id INTEGER NOT NULL REFERENCES moneyballs(id) ON DELETE CASCADE,
            round        INTEGER NOT NULL CHECK (round BETWEEN 1 AND 7),
            court        INTEGER NOT NULL CHECK (court BETWEEN 1 AND 2),
            score_a      INTEGER,
            score_b      INTEGER,
            updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (moneyball_id, round, court)
        );
        """
    )

    # Migration: if the old moneyball_players schema is in use (member_id NOT NULL,
    # no guest_name column), rewrite the table. Detect by inspecting pragma.
    cols = db._conn.execute("PRAGMA table_info(moneyball_players)").fetchall()
    col_names = {c["name"] for c in cols}
    if "guest_name" not in col_names:
        db._conn.executescript(
            """
            BEGIN;
            CREATE TABLE moneyball_players_new (
                moneyball_id INTEGER NOT NULL REFERENCES moneyballs(id) ON DELETE CASCADE,
                seat         INTEGER NOT NULL CHECK (seat BETWEEN 0 AND 7),
                member_id    INTEGER REFERENCES members(telegram_id),
                guest_name   TEXT,
                added_by     INTEGER REFERENCES members(telegram_id),
                PRIMARY KEY (moneyball_id, seat),
                CHECK (
                    (member_id IS NOT NULL AND guest_name IS NULL) OR
                    (member_id IS NULL     AND guest_name IS NOT NULL)
                )
            );
            INSERT INTO moneyball_players_new (moneyball_id, seat, member_id)
                SELECT moneyball_id, seat, member_id FROM moneyball_players;
            DROP TABLE moneyball_players;
            ALTER TABLE moneyball_players_new RENAME TO moneyball_players;
            CREATE INDEX idx_mbp_member ON moneyball_players(member_id);
            COMMIT;
            """
        )


# ─────────────────────────────────────────────
# Creation / lookup
# ─────────────────────────────────────────────

def create_moneyball(game_id: int, created_by: int, entries: list[dict]) -> int:
    """Create a money ball for the given game.

    `entries` is a list of 8 dicts, each with EITHER:
      {"member_id": int, "added_by": int (or None)}        for members
      {"guest_name": str, "added_by": int}                 for guests

    Caller is responsible for shuffling. Returns the new moneyball id.
    """
    assert db._conn is not None
    if len(entries) != 8:
        raise ValueError(f"expected 8 entries, got {len(entries)}")

    # Sanity: members must be unique, guests by trimmed-lower name within this MB
    member_ids = [e["member_id"] for e in entries if e.get("member_id")]
    guest_names = [(e.get("guest_name") or "").strip().lower() for e in entries if not e.get("member_id")]
    if len(set(member_ids)) != len(member_ids):
        raise ValueError("duplicate members in roster")
    if len(set(guest_names)) != len(guest_names):
        raise ValueError("duplicate guest names in roster")

    with db.transaction():
        cur = db._conn.execute(
            "INSERT INTO moneyballs (game_id, created_by) VALUES (?, ?)",
            (game_id, created_by),
        )
        mb_id = cur.lastrowid
        for seat, e in enumerate(entries):
            db._conn.execute(
                """
                INSERT INTO moneyball_players (moneyball_id, seat, member_id, guest_name, added_by)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    mb_id, seat,
                    e.get("member_id"),
                    e.get("guest_name"),
                    e.get("added_by", created_by),
                ),
            )
        # Pre-create 14 empty match rows
        for r in range(1, TOTAL_ROUNDS + 1):
            for c in range(1, COURTS_PER_ROUND + 1):
                db._conn.execute(
                    "INSERT INTO moneyball_matches (moneyball_id, round, court) VALUES (?, ?, ?)",
                    (mb_id, r, c),
                )
    return mb_id


def get_moneyball(mb_id: int) -> Optional[dict]:
    """Returns a fully-hydrated dict suitable for the API response:
      {
        id, game_id, status, created_at, completed_at,
        game: { scheduled_for, location },
        players: [{seat, member_id, name}, ... 8 entries],
        matches: [[ {scoreA, scoreB}, {scoreA, scoreB} ], ... 7 rows],
      }
    """
    assert db._conn is not None
    mb = db._conn.execute("SELECT * FROM moneyballs WHERE id = ?", (mb_id,)).fetchone()
    if not mb:
        return None
    mb = dict(mb)

    game = db._conn.execute(
        "SELECT scheduled_for, location FROM games WHERE id = ?", (mb["game_id"],)
    ).fetchone()
    mb["game"] = dict(game) if game else None

    player_rows = db._conn.execute(
        """
        SELECT
            p.seat,
            p.member_id,
            p.guest_name,
            p.added_by,
            COALESCE(m.display_name, p.guest_name) AS name,
            CASE WHEN p.member_id IS NULL THEN 1 ELSE 0 END AS is_guest,
            adder.display_name AS added_by_name
        FROM moneyball_players p
        LEFT JOIN members m     ON m.telegram_id = p.member_id
        LEFT JOIN members adder ON adder.telegram_id = p.added_by
        WHERE p.moneyball_id = ?
        ORDER BY p.seat ASC
        """,
        (mb_id,),
    ).fetchall()
    mb["players"] = [dict(r) for r in player_rows]

    match_rows = db._conn.execute(
        """
        SELECT round, court, score_a, score_b
        FROM moneyball_matches
        WHERE moneyball_id = ?
        ORDER BY round ASC, court ASC
        """,
        (mb_id,),
    ).fetchall()

    # Shape into [round_idx][court_idx] = {scoreA, scoreB}
    matches: list[list[dict]] = [[{"scoreA": None, "scoreB": None}, {"scoreA": None, "scoreB": None}] for _ in range(TOTAL_ROUNDS)]
    for r in match_rows:
        matches[r["round"] - 1][r["court"] - 1] = {
            "scoreA": r["score_a"],
            "scoreB": r["score_b"],
        }
    mb["matches"] = matches
    return mb


def get_moneyball_for_game(game_id: int) -> Optional[dict]:
    """Find the most recent money ball for a given game (in_progress or completed)."""
    assert db._conn is not None
    row = db._conn.execute(
        "SELECT id FROM moneyballs WHERE game_id = ? ORDER BY id DESC LIMIT 1",
        (game_id,),
    ).fetchone()
    return get_moneyball(row["id"]) if row else None


def update_match_score(mb_id: int, round_num: int, court: int,
                       score_a: Optional[int], score_b: Optional[int]) -> dict:
    """Upsert a match score. Returns the updated moneyball.
    Pass None for both scores to clear a match."""
    assert db._conn is not None
    if not (1 <= round_num <= TOTAL_ROUNDS):
        raise ValueError("invalid round")
    if not (1 <= court <= COURTS_PER_ROUND):
        raise ValueError("invalid court")
    if score_a is not None and score_b is not None:
        if score_a == score_b:
            raise ValueError("scores cannot tie")
        if score_a < 0 or score_b < 0 or score_a > 99 or score_b > 99:
            raise ValueError("score out of range")
    with db.transaction():
        db._conn.execute(
            """
            UPDATE moneyball_matches
            SET score_a = ?, score_b = ?, updated_at = datetime('now')
            WHERE moneyball_id = ? AND round = ? AND court = ?
            """,
            (score_a, score_b, mb_id, round_num, court),
        )
        # If all 14 matches are scored, mark as completed
        remaining = db._conn.execute(
            """
            SELECT COUNT(*) AS n FROM moneyball_matches
            WHERE moneyball_id = ? AND (score_a IS NULL OR score_b IS NULL)
            """,
            (mb_id,),
        ).fetchone()["n"]
        if remaining == 0:
            db._conn.execute(
                "UPDATE moneyballs SET status = 'completed', completed_at = datetime('now') WHERE id = ?",
                (mb_id,),
            )
        else:
            db._conn.execute(
                "UPDATE moneyballs SET status = 'in_progress', completed_at = NULL WHERE id = ?",
                (mb_id,),
            )
    return get_moneyball(mb_id)


def delete_moneyball(mb_id: int) -> None:
    assert db._conn is not None
    db._conn.execute("DELETE FROM moneyballs WHERE id = ?", (mb_id,))


# ─────────────────────────────────────────────
# Standings — used by Mini App and leaderboard
# ─────────────────────────────────────────────

def compute_standings(mb: dict) -> list[dict]:
    """Returns the 8 players sorted by:
      1. Wins desc
      2. Point differential desc
      3. Points for desc (tertiary tiebreaker — no ties allowed in medals)
    Each dict: {seat, member_id, name, wins, losses, points_for, points_against, diff, played}
    """
    by_seat = {
        p["seat"]: {
            "seat": p["seat"],
            "member_id": p.get("member_id"),
            "guest_name": p.get("guest_name"),
            "is_guest": bool(p.get("is_guest")),
            "name": p["name"],
            "wins": 0,
            "losses": 0,
            "points_for": 0,
            "points_against": 0,
            "diff": 0,
            "played": 0,
        }
        for p in mb["players"]
    }

    for r_idx, round_def in enumerate(SCHEDULE):
        for c_idx, (team_a, team_b) in enumerate(round_def):
            m = mb["matches"][r_idx][c_idx]
            sa, sb = m["scoreA"], m["scoreB"]
            if sa is None or sb is None:
                continue
            a_wins = sa > sb
            for seat in team_a:
                s = by_seat[seat]
                s["played"] += 1
                s["points_for"] += sa
                s["points_against"] += sb
                if a_wins: s["wins"] += 1
                else:      s["losses"] += 1
            for seat in team_b:
                s = by_seat[seat]
                s["played"] += 1
                s["points_for"] += sb
                s["points_against"] += sa
                if not a_wins: s["wins"] += 1
                else:           s["losses"] += 1

    stats = list(by_seat.values())
    for s in stats:
        s["diff"] = s["points_for"] - s["points_against"]
    stats.sort(key=lambda s: (-s["wins"], -s["diff"], -s["points_for"]))
    return stats


# ─────────────────────────────────────────────
# Leaderboard — medals across many money balls
# ─────────────────────────────────────────────

MEDAL_POINTS = {1: 3, 2: 2, 3: 1}  # gold, silver, bronze


def compute_leaderboard(scope: str = "90d") -> list[dict]:
    """Compute medal leaderboard. Scope is one of:
        '90d'      — last 90 days (default for /leaderboard)
        'year'     — current calendar year
        'alltime'  — all completed money balls

    Returns rows sorted by points desc, then gold count desc, then silver, bronze.
    Each row: {member_id, name, gold, silver, bronze, points, total_played}
    """
    assert db._conn is not None
    cutoff_clause = ""
    params: list = []
    if scope == "90d":
        cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        cutoff_clause = "AND mb.completed_at >= ?"
        params.append(cutoff)
    elif scope == "year":
        year_start = datetime(datetime.now(timezone.utc).year, 1, 1, tzinfo=timezone.utc).isoformat()
        cutoff_clause = "AND mb.completed_at >= ?"
        params.append(year_start)
    elif scope == "alltime":
        pass
    else:
        raise ValueError(f"unknown scope: {scope!r}")

    rows = db._conn.execute(
        f"""
        SELECT mb.id FROM moneyballs mb
        WHERE mb.status = 'completed' {cutoff_clause}
        ORDER BY mb.completed_at ASC
        """,
        params,
    ).fetchall()

    # Aggregate medals. Key: ('member', telegram_id) or ('guest', lower-trim name).
    by_key: dict[tuple, dict] = {}

    def key_for(player_or_standing: dict) -> tuple:
        if player_or_standing.get("member_id"):
            return ("member", player_or_standing["member_id"])
        return ("guest", (player_or_standing.get("guest_name") or "").strip().lower())

    def init_row(player_or_standing: dict) -> dict:
        is_guest = not player_or_standing.get("member_id")
        return {
            "key": key_for(player_or_standing),
            "member_id": player_or_standing.get("member_id"),
            "guest_name": player_or_standing.get("guest_name") if is_guest else None,
            "is_guest": is_guest,
            "name": player_or_standing["name"] + (" (guest)" if is_guest else ""),
            "gold": 0, "silver": 0, "bronze": 0,
            "points": 0,
            "total_played": 0,
        }

    for row in rows:
        mb = get_moneyball(row["id"])
        if not mb:
            continue
        standings = compute_standings(mb)
        for rank in (1, 2, 3):
            player = standings[rank - 1]
            k = key_for(player)
            if k not in by_key:
                by_key[k] = init_row(player)
            if rank == 1: by_key[k]["gold"] += 1
            elif rank == 2: by_key[k]["silver"] += 1
            elif rank == 3: by_key[k]["bronze"] += 1
            by_key[k]["points"] += MEDAL_POINTS[rank]

        for p in mb["players"]:
            k = key_for(p)
            if k not in by_key:
                by_key[k] = init_row(p)
            by_key[k]["total_played"] += 1

    result = list(by_key.values())
    result.sort(key=lambda r: (-r["points"], -r["gold"], -r["silver"], -r["bronze"], r["name"].lower()))
    return result


# ─────────────────────────────────────────────
# For /moneyball command — find eligible games
# ─────────────────────────────────────────────

def list_eligible_games_for_moneyball() -> list[dict]:
    """Games with exactly 8 confirmed participants (members + guests).
    Used by /moneyball to show selectable games.
    """
    assert db._conn is not None
    upcoming = db.list_upcoming_games()
    eligible = []
    for g in upcoming:
        confirmed = db.confirmed_count(g["id"])
        if confirmed == 8:
            eligible.append(g)
    return eligible
