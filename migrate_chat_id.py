#!/usr/bin/env python3
"""Re-key a chat after a Telegram regular-group → supergroup migration.

Usage:
    python3 migrate_chat_id.py --db /home/bot/pickleball-bot/db.sqlite \
        --old -5249408335 --new -1003990916602

What it does, in order:
    1. Backs up the SQLite file to <db>.bak-<timestamp>.
    2. Shows current counts for the OLD and NEW chat ids in every relevant table.
    3. In a single transaction:
         - Deletes the stub row in `chats` for NEW (created by on_my_chat_member
           when the supergroup appeared), if present.
         - Updates `chats.telegram_chat_id` OLD → NEW.
         - Updates `chat_members.chat_id`, `games.chat_id`, `moneyballs.chat_id`
           OLD → NEW.
       Handles UNIQUE-collisions in chat_members (if a user happens to be in
       both rows already) by preferring the OLD row's role.
    4. Shows post-migration counts.
    5. Requires you to type 'yes' to commit; otherwise rolls back.

Run with the bot STOPPED:
    sudo systemctl stop pickleball-bot
    sudo -u bot python3 migrate_chat_id.py --db /home/bot/pickleball-bot/db.sqlite \
        --old -5249408335 --new -1003990916602
    sudo systemctl start pickleball-bot

Don't forget to update ALLOWED_GROUP_ID in .env to the NEW id afterwards.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path


TABLES_WITH_CHAT_ID = [
    ("chat_members", "chat_id"),
    ("games", "chat_id"),
    ("moneyballs", "chat_id"),
]


def counts(conn: sqlite3.Connection, chat_id: int) -> dict[str, int]:
    out: dict[str, int] = {}
    # chats table uses telegram_chat_id as PK
    row = conn.execute(
        "SELECT COUNT(*) FROM chats WHERE telegram_chat_id = ?", (chat_id,)
    ).fetchone()
    out["chats"] = row[0]
    for table, col in TABLES_WITH_CHAT_ID:
        # moneyballs may not exist yet on fresh installs
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (chat_id,)
        ).fetchone()
        out[table] = row[0]
    return out


def show(label: str, c: dict[str, int]) -> None:
    print(f"  {label}:")
    for k, v in c.items():
        print(f"    {k:<14} {v}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True, help="Path to db.sqlite")
    p.add_argument("--old", type=int, required=True, help="Old chat_id (regular group)")
    p.add_argument("--new", type=int, required=True, help="New chat_id (supergroup)")
    args = p.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: {db_path} not found", file=sys.stderr)
        return 1

    # 1. Backup
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_suffix(db_path.suffix + f".bak-{ts}")
    shutil.copy2(db_path, backup)
    print(f"Backup written: {backup}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # 2. Before
    print("\nBEFORE:")
    before_old = counts(conn, args.old)
    before_new = counts(conn, args.new)
    show(f"OLD ({args.old})", before_old)
    show(f"NEW ({args.new})", before_new)

    if before_old.get("chats", 0) == 0:
        print(f"\nERROR: no row in chats for OLD id {args.old}. Nothing to migrate.")
        return 1

    # Sanity: title check
    old_title = conn.execute(
        "SELECT title FROM chats WHERE telegram_chat_id = ?", (args.old,)
    ).fetchone()
    new_title = conn.execute(
        "SELECT title FROM chats WHERE telegram_chat_id = ?", (args.new,)
    ).fetchone()
    print(f"\nOLD title: {old_title['title'] if old_title else '(none)'}")
    print(f"NEW title: {new_title['title'] if new_title else '(none)'}")

    # 3. Perform migration in a transaction.
    # Defer FK checks until COMMIT so intermediate states (e.g. updating
    # chats.telegram_chat_id while chat_members still reference the old id)
    # don't trip the constraint.
    try:
        conn.execute("BEGIN")
        conn.execute("PRAGMA defer_foreign_keys = ON")

        # If NEW already exists in chats (stub from on_my_chat_member), drop it
        # first so the UPDATE doesn't collide on the PK.
        if before_new.get("chats", 0) > 0:
            # Move any chat_members rows that point at NEW out of the way first;
            # we'll merge them after the OLD rows are re-keyed.
            new_member_ids = [
                r["telegram_user_id"]
                for r in conn.execute(
                    "SELECT telegram_user_id FROM chat_members WHERE chat_id = ?",
                    (args.new,),
                )
            ]
            # Detach them temporarily by deleting; we'll re-add ones that aren't
            # already present in OLD after re-key.
            conn.execute("DELETE FROM chat_members WHERE chat_id = ?", (args.new,))
            conn.execute("DELETE FROM chats WHERE telegram_chat_id = ?", (args.new,))
        else:
            new_member_ids = []

        # Re-key the chats row
        conn.execute(
            "UPDATE chats SET telegram_chat_id = ? WHERE telegram_chat_id = ?",
            (args.new, args.old),
        )

        # Re-key dependent tables
        for table, col in TABLES_WITH_CHAT_ID:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            conn.execute(
                f"UPDATE {table} SET {col} = ? WHERE {col} = ?", (args.new, args.old)
            )

        # Re-add any chat_members from the stub NEW that weren't in OLD.
        # (Anyone the stub knew about who hadn't messaged the bot in the old
        # group — rare, but possible.)
        for uid in new_member_ids:
            present = conn.execute(
                "SELECT 1 FROM chat_members WHERE chat_id = ? AND telegram_user_id = ?",
                (args.new, uid),
            ).fetchone()
            if not present:
                conn.execute(
                    "INSERT INTO chat_members (chat_id, telegram_user_id, role) "
                    "VALUES (?, ?, 'member')",
                    (args.new, uid),
                )

        # 4. After (still inside transaction)
        print("\nAFTER (uncommitted):")
        after_old = counts(conn, args.old)
        after_new = counts(conn, args.new)
        show(f"OLD ({args.old})", after_old)
        show(f"NEW ({args.new})", after_new)

        # 5. Confirm
        print("\nReady to commit. OLD rows should be 0, NEW rows should match what")
        print("OLD had before. Type 'yes' to commit, anything else to roll back.")
        ans = input("commit? ").strip().lower()
        if ans == "yes":
            conn.commit()
            print("Committed.")
        else:
            conn.rollback()
            print("Rolled back. DB unchanged.")
            return 0

    except Exception as e:
        conn.rollback()
        print(f"\nERROR during migration: {e}", file=sys.stderr)
        print("Transaction rolled back. DB unchanged.", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(f"\nDon't forget: update ALLOWED_GROUP_ID to {args.new} in .env, then")
    print("    sudo systemctl start pickleball-bot")
    return 0


if __name__ == "__main__":
    sys.exit(main())
