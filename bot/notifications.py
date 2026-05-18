"""DM members (and the people who added guests) whenever a participant's
registration changes by someone other than themselves.

The public entry point is `diff_and_notify(...)`, which takes a
before/after snapshot of the participants for a game, diffs them, and
sends one DM per affected user. The actor's own changes are skipped —
the person who just tapped Leave / Join in the mini-app doesn't need a
DM about what they themselves just did. Same idea for guests: only the
*adder* gets DM'd, and only when the change came from somebody else
(admin removed their guest, waitlist auto-promoted their guest, etc.).

We match participants across the two snapshots by *identity*, not by
participant_id. Matching by member_id keeps remove+re-add sequences
(e.g. confirmed → waitlist via the manage mini-app) as a single
"status changed" DM instead of two separate "removed" + "added" DMs.
For guests the identity is (adder, name) — not perfect if one member
adds two guests with the same name, but that's a rare collision.

All DMs are best-effort. If a recipient has never DM'd the bot we get
a 403 from Telegram; we swallow and log at INFO. The chat-card refresh
remains the canonical record of state for everyone who didn't get the
DM.
"""
from __future__ import annotations

import logging
from typing import Optional
from zoneinfo import ZoneInfo

from telegram import Bot
from telegram.constants import ParseMode
from telegram.helpers import escape

from . import db, views

log = logging.getLogger(__name__)


# Hint appended to "you're still on the game" DMs so the recipient
# knows how to drop themselves (or their guest) without a second tap.
_LEAVE_HINT_MEMBER = (
    "If you can no longer make it, tap "
    "<b>📝 Update Registration</b> on the game card and choose Leave."
)
_LEAVE_HINT_GUEST = (
    "If they can no longer make it, tap "
    "<b>📝 Update Registration</b> on the game card and remove them "
    "from your guests."
)


# ─────────────────────── public entry ─────────────────────── #

async def diff_and_notify(
    *,
    bot: Bot,
    tz: ZoneInfo,
    game_id: int,
    before: list[dict],
    after: list[dict],
    actor_id: int,
) -> None:
    """Send one DM per status transition between `before` and `after`.

    Call sites: every mutating endpoint in http_server.py and every
    mutating handler in handlers/roster.py. Recommended pattern:

        before = db.get_participants(game_id)
        ... mutation logic ...
        after = db.get_participants(game_id)
        await notifications.diff_and_notify(
            bot=..., tz=..., game_id=game_id,
            before=before, after=after, actor_id=...,
        )

    The mutation step can be any combination of add / remove / move /
    swap / max-players-resize / etc. — we only look at the resulting
    state, never the intermediate operations.
    """
    game = db.get_game(game_id)
    if game is None:
        return  # game was deleted mid-flight; nobody to notify

    before_by_id = {_identity(p): p for p in before}
    after_by_id = {_identity(p): p for p in after}

    # Status transitions (and additions)
    for key, p_after in after_by_id.items():
        p_before = before_by_id.get(key)
        if p_before is None:
            await _send(bot, tz, game, p_after, "added", actor_id)
        elif p_before["status"] != p_after["status"]:
            await _send(
                bot, tz, game, p_after,
                f"to_{p_after['status']}",
                actor_id,
            )

    # Removals
    for key, p_before in before_by_id.items():
        if key not in after_by_id:
            await _send(bot, tz, game, p_before, "removed", actor_id)


# ─────────────────────── internals ─────────────────────── #

def _identity(p: dict) -> tuple:
    """Stable identity for matching the same person across snapshots.

    Member rows match on telegram user id. Guest rows match on
    (adder, name) since guests don't have a Telegram identity.
    """
    if p.get("member_id") is not None:
        return ("member", p["member_id"])
    return ("guest", p.get("added_by"), p.get("guest_name"))


def _dm_target(participant: dict, actor_id: int) -> Optional[int]:
    """Telegram user id to DM about this participant, or None to skip.

    Skips when:
      • the affected member IS the actor (they did it themselves)
      • the participant is a guest and the adder is the actor
      • the participant is a guest with no adder (shouldn't happen but
        we tolerate it rather than crashing)
    """
    if participant.get("member_id") is not None:
        member_id = participant["member_id"]
        return None if member_id == actor_id else member_id
    adder = participant.get("added_by")
    if adder is None or adder == actor_id:
        return None
    return adder


async def _send(
    bot: Bot,
    tz: ZoneInfo,
    game: dict,
    participant: dict,
    change: str,
    actor_id: int,
) -> None:
    """Build and send a single DM for one participant's transition."""
    target = _dm_target(participant, actor_id)
    if target is None:
        return

    is_guest = participant.get("member_id") is None
    name = (
        participant.get("guest_name") if is_guest
        else participant.get("member_name")
    ) or "?"
    when = views.format_when(game["scheduled_for"], tz)
    where = f"<b>{when}</b> @ {escape(game['location'])}"

    text = _build_message(change, is_guest, name, where, participant)
    if text is None:
        return  # change not recognized — silent

    try:
        await bot.send_message(
            chat_id=target, text=text, parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        # Most common cause: the user has never DM'd the bot, so
        # Telegram returns 403 Forbidden. Not worth surfacing.
        log.info("Couldn't DM user %s about %s: %s", target, change, e)


def _build_message(
    change: str,
    is_guest: bool,
    name: str,
    where: str,
    participant: dict,
) -> Optional[str]:
    """Render the HTML body of one DM, or None for an unknown change."""
    hint = _LEAVE_HINT_GUEST if is_guest else _LEAVE_HINT_MEMBER
    pos = participant.get("position")
    safe_name = escape(name)

    if change == "added":
        status = participant.get("status", "")
        # Append a status badge so the recipient knows where they
        # landed in one glance.
        suffix = {
            "confirmed": " <b>(Confirmed)</b>",
            "waitlist":  f" <b>(Waitlist #{pos})</b>",
        }.get(status, "")
        if is_guest:
            return (
                f"➕ An admin added your guest <b>{safe_name}</b> to a game.{suffix}\n\n"
                f"{where}\n\n"
                f"<i>{hint}</i>"
            )
        return (
            f"➕ You've been added to a game.{suffix}\n\n"
            f"{where}\n\n"
            f"<i>{hint}</i>"
        )

    if change == "removed":
        if is_guest:
            return (
                f"❌ Your guest <b>{safe_name}</b> was removed from a game.\n\n"
                f"{where}"
            )
        return (
            f"❌ You've been removed from a game.\n\n"
            f"{where}"
        )

    if change == "to_confirmed":
        if is_guest:
            return (
                f"🎉 Your guest <b>{safe_name}</b> is now <b>confirmed</b> for a game.\n\n"
                f"{where}\n\n"
                f"<i>{hint}</i>"
            )
        return (
            f"🎉 You're now <b>confirmed</b> for a game.\n\n"
            f"{where}\n\n"
            f"<i>{hint}</i>"
        )

    if change == "to_waitlist":
        if is_guest:
            return (
                f"⏳ Your guest <b>{safe_name}</b> has been moved to the "
                f"<b>waitlist (#{pos})</b> for a game.\n\n"
                f"{where}\n\n"
                f"<i>{hint}</i>"
            )
        return (
            f"⏳ You've been moved to the <b>waitlist (#{pos})</b> for a game.\n\n"
            f"{where}\n\n"
            f"<i>{hint}</i>"
        )

    return None
