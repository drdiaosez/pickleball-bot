"""/newgame — a short guided conversation to schedule a game.

Flow:
  1. /newgame                  → ask for date & time
  2. user types "wed 6:30pm"   → parse, confirm, ask for location
  3. user types location       → ask for max players (default 4, skip with /skip)
  4. user types number or /skip → ask for notes (optional, /skip)
  5. user types notes or /skip → create, post the game card, end

Uses python-telegram-bot's ConversationHandler. We keep the parsing forgiving:
date/time accepts a bunch of natural inputs; we re-prompt on bad input.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .. import db, views
from ..chat_picker import resolve_chat
from .common import touch_member

# Conversation states
ASK_WHEN, ASK_LOCATION, ASK_MAX, ASK_NOTES = range(4)


# ─────────────────────── date parsing ─────────────────────── #

DAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def parse_datetime(text: str, tz: ZoneInfo) -> datetime | None:
    """Parse a forgiving natural-language datetime.

    Accepts things like:
      'wed 6:30pm', 'thursday 7pm', 'tomorrow 6pm', 'today 5:30pm',
      '5/14 6:30pm', '05/20 9:30am', '2026-05-14 18:30'

    Strategy: extract the *date* first (it has stricter syntax — slashes,
    named days, 'today'/'tomorrow'), strip it out, then parse what's left
    as the time. Parsing time-first would mis-grab '05' from '05/20' as
    hour 5.

    Returns timezone-aware datetime in `tz`, or None on failure.
    """
    s = text.strip().lower()
    now = datetime.now(tz)

    # Strict ISO first
    try:
        dt = datetime.fromisoformat(text.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt
    except ValueError:
        pass

    target_date: "date | None" = None
    remainder = s  # what's left after the date is stripped

    # 1. Numeric date like 5/14, 05/20, 5-14, 5/14/2026 — match this FIRST
    #    because it has the most distinctive syntax (slash or dash between digits).
    num_date = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", s)
    if num_date:
        month = int(num_date.group(1))
        day = int(num_date.group(2))
        year = int(num_date.group(3)) if num_date.group(3) else now.year
        if year < 100:
            year += 2000
        try:
            target_date = datetime(year, month, day).date()
        except ValueError:
            return None
        # If no year was given and the date landed in the *distant* past,
        # they probably meant next year (e.g. typing "1/5" in late December).
        # But if it's a recent past date (within 60 days), they almost
        # certainly mistyped or meant a past game — DON'T roll forward to
        # next year. Returning None forces a re-prompt instead of silently
        # creating a game 11 months out.
        if num_date.group(3) is None and target_date < now.date():
            days_behind = (now.date() - target_date).days
            if days_behind > 60:
                # User typed something far in the past (e.g. "1/5" in Dec)
                target_date = datetime(year + 1, month, day).date()
            else:
                # Recent past — refuse the parse, let the user retry
                return None
        remainder = (s[:num_date.start()] + " " + s[num_date.end():]).strip()

    # 2. 'today' / 'tomorrow'
    elif "tomorrow" in s or "tmrw" in s:
        target_date = (now + timedelta(days=1)).date()
        remainder = re.sub(r"\b(tomorrow|tmrw)\b", " ", s).strip()
    elif "today" in s:
        target_date = now.date()
        remainder = re.sub(r"\btoday\b", " ", s).strip()

    # 3. Day of week (mon, tuesday, etc.)
    else:
        for name, weekday in DAY_NAMES.items():
            if re.search(rf"\b{name}\b", s):
                days_ahead = (weekday - now.weekday()) % 7
                if days_ahead == 0:
                    # Same weekday — we'll decide whether to bump to next week
                    # *after* we know the time. Mark as today for now.
                    pass
                target_date = (now + timedelta(days=days_ahead)).date()
                remainder = re.sub(rf"\b{name}\b", " ", s).strip()
                break

    if target_date is None:
        return None

    # Now parse the time from what's left. Accept "6:30pm", "6pm", "18:30",
    # "6:30", "7". Bare numbers without colon or meridiem are still allowed
    # (default AM per group preference).
    time_match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", remainder)
    if not time_match:
        return None
    hour = int(time_match.group(1))
    minute = int(time_match.group(2)) if time_match.group(2) else 0
    meridiem = time_match.group(3)

    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    # No meridiem? Leave as-is — this group plays mornings, so a bare
    # "6:30" or "7" means AM. PM games need an explicit "pm".

    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None

    # If the date was a same-weekday match and the resulting time has
    # already passed today, push to next week.
    today_weekday = now.weekday()
    if target_date == now.date() and target_date.weekday() == today_weekday:
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # Only push forward if this looked like a "wed" / "tomorrow"-style request,
        # not a numeric date that happens to be today.
        if candidate <= now and not num_date and "today" not in s:
            target_date = (now + timedelta(days=7)).date()

    dt = datetime.combine(target_date, datetime.min.time()).replace(
        hour=hour, minute=minute, tzinfo=tz
    )
    return dt


# ─────────────────────── handlers ─────────────────────── #

async def start_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from .common import gate
    if not await gate(update):
        return ConversationHandler.END
    await touch_member(update)
    # Determine which chat this game belongs to. In a group, that's the
    # current chat. In a DM, this may pop a picker — if so, bail out of the
    # conversation; the user runs /newgame again after picking.
    chat_id = await resolve_chat(update, context, "newgame")
    if chat_id is None:
        return ConversationHandler.END
    context.user_data["newgame"] = {}
    context.user_data["newgame_chat_id"] = chat_id
    await update.effective_message.reply_html(
        "📅 <b>When is the game?</b>\n\n"
        "Examples:\n"
        "• <code>wed 7</code> (= 7am)\n"
        "• <code>tomorrow 6:30</code> (= 6:30am)\n"
        "• <code>5/14 8am</code>\n"
        "• <code>fri 7pm</code> (explicit for evening)\n\n"
        "Send /cancel to abort."
    )
    return ASK_WHEN


async def got_when(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tz: ZoneInfo = context.bot_data["tz"]
    text = update.effective_message.text or ""
    dt = parse_datetime(text, tz)
    if dt is None:
        await update.effective_message.reply_text(
            "I couldn't read that. Try something like 'wed 7' or 'tomorrow 6:30am'."
        )
        return ASK_WHEN
    if dt < datetime.now(tz) - timedelta(minutes=10):
        await update.effective_message.reply_text(
            "That looks like it's in the past. Try again?"
        )
        return ASK_WHEN

    context.user_data["newgame"]["scheduled_for"] = dt
    await update.effective_message.reply_html(
        f"Got it: <b>{views.format_when(dt.isoformat(), tz)}</b>\n\n"
        "📍 <b>Where?</b> (e.g. <code>Riverside Club, Court 3</code>)"
    )
    return ASK_LOCATION


async def got_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    location = (update.effective_message.text or "").strip()
    if not location:
        await update.effective_message.reply_text("Need a location. Try again.")
        return ASK_LOCATION
    if len(location) > 100:
        await update.effective_message.reply_text("Keep it under 100 chars.")
        return ASK_LOCATION

    context.user_data["newgame"]["location"] = location
    await update.effective_message.reply_html(
        "👥 <b>Max players?</b> (Send a number, or /skip for 4)"
    )
    return ASK_MAX


async def got_max(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    if text.startswith("/skip"):
        context.user_data["newgame"]["max_players"] = 4
    else:
        try:
            n = int(text)
            if not (2 <= n <= 32):
                raise ValueError
            context.user_data["newgame"]["max_players"] = n
        except ValueError:
            await update.effective_message.reply_text("Send a number between 2 and 32, or /skip.")
            return ASK_MAX

    await update.effective_message.reply_html(
        "📝 <b>Any notes?</b> (e.g. \"bring 4 balls\") — or /skip"
    )
    return ASK_NOTES


async def got_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    notes = None if text.startswith("/skip") else text[:200]

    data = context.user_data["newgame"]
    user = update.effective_user
    chat_id = context.user_data.get("newgame_chat_id")

    game_id = db.create_game(
        scheduled_for=data["scheduled_for"],
        location=data["location"],
        organizer_id=user.id,
        max_players=data["max_players"],
        notes=notes,
        chat_id=chat_id,
    )

    # Post the game card and remember its message_id so we can edit later
    from . import roster  # local import to avoid circular
    await roster.post_game_card(context, chat_id, game_id)

    await update.effective_message.reply_text("✓ Game posted above.")
    context.user_data.pop("newgame", None)
    context.user_data.pop("newgame_chat_id", None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("newgame", None)
    context.user_data.pop("newgame_chat_id", None)
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


def build_newgame_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("newgame", start_newgame)],
        states={
            ASK_WHEN:     [MessageHandler(filters.TEXT & ~filters.COMMAND, got_when)],
            ASK_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_location)],
            ASK_MAX: [
                MessageHandler(filters.Regex(r"^/skip"), got_max),
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_max),
            ],
            ASK_NOTES: [
                MessageHandler(filters.Regex(r"^/skip"), got_notes),
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_notes),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=False,  # so the flow follows a user across chats if needed
    )
