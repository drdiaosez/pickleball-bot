"""Smoke tests for the DB layer.

Not a full unit test suite — just enough to catch obvious regressions in
the join/leave/swap/promote flows. Run with: python -m bot.tests
"""
import os
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import db


def main():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite")
    tmp.close()
    db.init_db(tmp.name)

    tz = ZoneInfo("America/Los_Angeles")

    # Set up 5 members
    for i in range(1, 6):
        db.upsert_member(i, f"User{i}", f"user{i}")

    # Create a game with max 2 players (so we can easily fill it).
    # chat_id is required since migration 002 (was missing in the original
    # test scaffolding — fixed here as a drive-by).
    when = datetime.now(tz) + timedelta(days=1)
    gid = db.create_game(when, "Test Court", organizer_id=1, max_players=2, chat_id=-1001)
    print(f"✓ Created game {gid}")

    # User 1 joins → confirmed
    r = db.add_participant(gid, added_by=1, member_id=1)
    assert r["status"] == "confirmed", f"expected confirmed, got {r}"
    print(f"✓ User1 joined as {r['status']}")

    # User 2 joins → confirmed (game now full)
    r = db.add_participant(gid, added_by=2, member_id=2)
    assert r["status"] == "confirmed"
    print(f"✓ User2 joined as {r['status']}")

    # User 3 joins → waitlist
    r = db.add_participant(gid, added_by=3, member_id=3)
    assert r["status"] == "waitlist" and r["position"] == 1
    p3_id = r["participant_id"]
    print(f"✓ User3 went to waitlist position {r['position']}")

    # User 4 joins → waitlist position 2
    r = db.add_participant(gid, added_by=4, member_id=4)
    assert r["status"] == "waitlist" and r["position"] == 2
    p4_id = r["participant_id"]
    print(f"✓ User4 went to waitlist position {r['position']}")

    # User 5 adds a guest "Casey" → waitlist position 3
    r = db.add_participant(gid, added_by=5, guest_name="Casey")
    assert r["status"] == "waitlist" and r["position"] == 3
    print(f"✓ Guest 'Casey' went to waitlist position {r['position']}")

    # Duplicate join attempt — should raise
    try:
        db.add_participant(gid, added_by=1, member_id=1)
        assert False, "expected ValueError on duplicate join"
    except ValueError as e:
        print(f"✓ Duplicate join rejected: {e}")

    # User 1 leaves → auto-promote User3 (top of waitlist)
    user1_pid = db.member_is_in_game(gid, 1)["id"]
    promoted = db.remove_participant(user1_pid)
    assert promoted["member_id"] == 3, f"expected user3 promoted, got {promoted}"
    print(f"✓ User1 left, User3 auto-promoted to confirmed")

    # Waitlist should now be [User4, Casey] with positions 1, 2
    parts = db.get_participants(gid)
    waitlist = sorted([p for p in parts if p["status"] == "waitlist"], key=lambda x: x["position"])
    assert len(waitlist) == 2
    assert waitlist[0]["member_id"] == 4 and waitlist[0]["position"] == 1
    assert waitlist[1]["guest_name"] == "Casey" and waitlist[1]["position"] == 2
    print(f"✓ Waitlist renumbered correctly: User4@1, Casey@2")

    # Swap: bump User2 (confirmed) for User4 (waitlist top)
    user2_pid = db.member_is_in_game(gid, 2)["id"]
    user4_pid = db.member_is_in_game(gid, 4)["id"]
    new_conf, new_wait = db.swap_with_waitlist(user2_pid, user4_pid)
    assert new_conf["member_id"] == 4 and new_conf["status"] == "confirmed"
    assert new_wait["member_id"] == 2 and new_wait["status"] == "waitlist"
    assert new_wait["position"] == 1, f"bumped user should be #1 on waitlist, got {new_wait['position']}"
    print(f"✓ Swap: User4 → confirmed, User2 → waitlist #1 (soft swap)")

    # Casey should now be waitlist #2
    parts = db.get_participants(gid)
    waitlist = sorted([p for p in parts if p["status"] == "waitlist"], key=lambda x: x["position"])
    casey = next(p for p in waitlist if p["guest_name"] == "Casey")
    assert casey["position"] == 2, f"Casey should be #2, got {casey['position']}"
    print(f"✓ Casey bumped to waitlist #2")

    # Demote User3 → bottom of waitlist, auto-promote User2 (top)
    user3_pid = db.member_is_in_game(gid, 3)["id"]
    promoted = db.demote_to_waitlist(user3_pid)
    assert promoted is not None and promoted["member_id"] == 2
    print(f"✓ User3 demoted, User2 auto-promoted")

    # Confirmed should be [User4, User2]; waitlist should be [Casey, User3]
    parts = db.get_participants(gid)
    confirmed = [p for p in parts if p["status"] == "confirmed"]
    waitlist = sorted([p for p in parts if p["status"] == "waitlist"], key=lambda x: x["position"])
    assert {p["member_id"] for p in confirmed if p["member_id"]} == {2, 4}
    assert waitlist[0]["guest_name"] == "Casey"
    assert waitlist[1]["member_id"] == 3
    print(f"✓ Final state: confirmed={{User2, User4}}, waitlist=[Casey, User3]")

    # Remove guest Casey
    casey_pid = next(p["id"] for p in parts if p.get("guest_name") == "Casey")
    db.remove_participant(casey_pid)
    parts = db.get_participants(gid)
    waitlist = sorted([p for p in parts if p["status"] == "waitlist"], key=lambda x: x["position"])
    assert len(waitlist) == 1 and waitlist[0]["position"] == 1
    print(f"✓ Casey removed, waitlist renumbered")

    # Listing
    upcoming = db.list_upcoming_games()
    assert len(upcoming) == 1
    print(f"✓ list_upcoming_games returned {len(upcoming)} game")

    my_games_u2 = db.list_games_for_member(2)
    assert len(my_games_u2) == 1
    print(f"✓ User2 sees 1 game")

    # ─── Payment tracking ─── #
    # Create a second game WITH payment set
    when2 = datetime.now(tz) + timedelta(days=2)
    pay_gid = db.create_game(
        when2, "Pay Court", organizer_id=1, max_players=4,
        chat_id=-1001, payment_amount_cents=750,  # $7.50
    )
    g = db.get_game(pay_gid)
    assert g["payment_amount_cents"] == 750
    print(f"✓ Game created with payment_amount_cents=750")

    # Two users join — both default to unpaid
    r1 = db.add_participant(pay_gid, added_by=1, member_id=1)
    r2 = db.add_participant(pay_gid, added_by=2, member_id=2)
    p1 = db.get_participant(r1["participant_id"])
    p2 = db.get_participant(r2["participant_id"])
    assert p1["is_paid"] == 0 and p2["is_paid"] == 0
    print(f"✓ New participants default to is_paid=0")

    # Toggle user1 → paid
    updated = db.toggle_participant_paid(r1["participant_id"])
    assert updated["is_paid"] == 1
    print(f"✓ Toggling unpaid → paid works")

    # Toggle again → unpaid
    updated = db.toggle_participant_paid(r1["participant_id"])
    assert updated["is_paid"] == 0
    print(f"✓ Toggling paid → unpaid works")

    # set_participant_paid direct
    db.set_participant_paid(r1["participant_id"], True)
    db.set_participant_paid(r2["participant_id"], True)
    assert db.get_participant(r1["participant_id"])["is_paid"] == 1
    assert db.get_participant(r2["participant_id"])["is_paid"] == 1
    print(f"✓ set_participant_paid(True) works")

    # Update payment amount — paid flags should be preserved when amount stays > 0
    db.update_game_payment_amount(pay_gid, 1000)  # bump to $10
    assert db.get_game(pay_gid)["payment_amount_cents"] == 1000
    assert db.get_participant(r1["participant_id"])["is_paid"] == 1, "paid flag preserved on amount change"
    print(f"✓ Changing amount preserves paid flags")

    # Clear payment amount — paid flags should reset (they were meaningless without amount)
    db.update_game_payment_amount(pay_gid, None)
    assert db.get_game(pay_gid)["payment_amount_cents"] is None
    assert db.get_participant(r1["participant_id"])["is_paid"] == 0
    assert db.get_participant(r2["participant_id"])["is_paid"] == 0
    print(f"✓ Clearing payment resets all paid flags")

    # 0 should also clear (same effect as None)
    db.update_game_payment_amount(pay_gid, 500)
    db.set_participant_paid(r1["participant_id"], True)
    db.update_game_payment_amount(pay_gid, 0)
    assert db.get_game(pay_gid)["payment_amount_cents"] is None
    assert db.get_participant(r1["participant_id"])["is_paid"] == 0
    print(f"✓ Setting amount to 0 clears + resets paid flags")

    # parse_payment edge cases (tested directly because the handler-level
    # tests live in the conversation flow which is hard to invoke here)
    from .handlers.newgame import parse_payment
    assert parse_payment("5") == 500
    assert parse_payment("$5") == 500
    assert parse_payment("5.50") == 550
    assert parse_payment("$5.50") == 550
    assert parse_payment("5.5") == 550, "single-digit cents should be padded"
    assert parse_payment("10.00") == 1000
    assert parse_payment("0") == 0
    assert parse_payment("$0") == 0
    assert parse_payment("  $7.25  ") == 725, "whitespace tolerated"
    assert parse_payment("abc") is None
    assert parse_payment("") is None
    assert parse_payment("5.999") is None, "3+ decimal places rejected"
    assert parse_payment("$") is None
    print(f"✓ parse_payment handles all expected inputs")

    # format_money round-trip
    from .views import format_money
    assert format_money(None) == ""
    assert format_money(0) == ""
    assert format_money(500) == "$5"
    assert format_money(750) == "$7.50"
    assert format_money(1000) == "$10"
    assert format_money(1234) == "$12.34"
    print(f"✓ format_money formats correctly")

    os.unlink(tmp.name)
    print("\n🎾  All tests passed.")


if __name__ == "__main__":
    main()
