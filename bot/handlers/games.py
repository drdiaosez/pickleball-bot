"""/games and /mygames — listing handlers, plus /past and /week.

These produce a single message with one button per game. Tapping
a button opens that game's card in a new message (we don't replace the
list — people may want to see multiple games).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from .. import db, views
from ..chat_picker import resolve_chat, register_command
from .common import touch_member


async def cmd_games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .common import gate
    if not await gate(update):
        return
    await touch_member(update)
    chat_id = await resolve_chat(update, context, "games")
    if chat_id is None:
        return
    tz = context.bot_data["tz"]
    games = db.list_upcoming_games(tz=tz, chat_id=chat_id)
    text = views.render_game_list_header(len(games), "Upcoming games")
    if not games:
        await update.effective_message.reply_html(text)
        return
    kb = views.game_list_keyboard(games, tz)
    await update.effective_message.reply_html(text, reply_markup=kb)


async def cmd_mygames(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .common import gate
    if not await gate(update):
        return
    await touch_member(update)
    chat_id = await resolve_chat(update, context, "mygames")
    if chat_id is None:
        return
    tz = context.bot_data["tz"]
    user = update.effective_user
    games = db.list_games_for_member(user.id, tz=tz, chat_id=chat_id)
    text = views.render_game_list_header(len(games), "Your upcoming games")
    if not games:
        await update.effective_message.reply_html(text)
        return
    kb = views.game_list_keyboard(games, tz)
    await update.effective_message.reply_html(text, reply_markup=kb)


async def cmd_past(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .common import gate
    if not await gate(update):
        return
    await touch_member(update)
    chat_id = await resolve_chat(update, context, "past")
    if chat_id is None:
        return
    tz = context.bot_data["tz"]
    games = db.list_past_games(limit=50, tz=tz, chat_id=chat_id)
    text = views.render_game_list_header(len(games), "Past games")
    if not games:
        await update.effective_message.reply_html(
            "<b>Past games</b>\n<i>none yet</i>"
        )
        return
    kb = views.game_list_keyboard(games, tz)
    await update.effective_message.reply_html(text, reply_markup=kb)


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all games in a specific Mon-Sun week.

    Usage:
      /week           — this week (Mon-Sun containing today)
      /week next      — next Monday-Sunday
      /week last      — last Monday-Sunday
      /week 5/18      — the Mon-Sun week containing 5/18
      /week 5/18/2026 — same, explicit year
    """
    from .common import gate
    if not await gate(update):
        return
    await touch_member(update)
    chat_id = await resolve_chat(update, context, "week")
    if chat_id is None:
        return
    tz: ZoneInfo = context.bot_data["tz"]
    args_text = " ".join(context.args or []).strip().lower()

    monday = _resolve_week_start(args_text, tz)
    if monday is None:
        await update.effective_message.reply_html(
            "Couldn't read that. Try:\n"
            "• <code>/week</code> — this week\n"
            "• <code>/week next</code> — next week\n"
            "• <code>/week last</code> — last week\n"
            "• <code>/week 5/18</code> — the week containing that date"
        )
        return

    sunday_end = monday + timedelta(days=7)  # exclusive upper bound
    games = db.list_games_in_range(monday, sunday_end, chat_id=chat_id)
    label = (
        f"Week of {monday.strftime('%a %-m/%-d')} – "
        f"{(monday + timedelta(days=6)).strftime('%a %-m/%-d')}"
    )
    text = views.render_game_list_header(len(games), label)
    if not games:
        await update.effective_message.reply_html(
            f"<b>{label}</b>\n<i>no games scheduled</i>"
        )
        return
    kb = views.game_list_keyboard(games, tz)
    await update.effective_message.reply_html(text, reply_markup=kb)


def _resolve_week_start(arg: str, tz: ZoneInfo) -> "datetime | None":
    """Return the Monday 00:00 (in tz) for the requested week, or None.

    Accepts: '' or 'this', 'next', 'last', or 'M/D' / 'M/D/YYYY'.
    """
    now = datetime.now(tz)
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    this_monday = today_midnight - timedelta(days=today_midnight.weekday())

    if arg in ("", "this"):
        return this_monday
    if arg == "next":
        return this_monday + timedelta(days=7)
    if arg == "last" or arg == "prev" or arg == "previous":
        return this_monday - timedelta(days=7)

    # M/D or M/D/YYYY
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?$", arg)
    if m:
        month = int(m.group(1))
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else now.year
        if year < 100:
            year += 2000
        try:
            target = datetime(year, month, day, tzinfo=tz)
        except ValueError:
            return None
        # If year was inferred and the date is way in the past, assume they meant
        # next year (e.g., user types "/week 1/5" in December)
        if m.group(3) is None and target < now - timedelta(days=180):
            try:
                target = datetime(year + 1, month, day, tzinfo=tz)
            except ValueError:
                return None
        return target - timedelta(days=target.weekday())

    return None


def build_games_handlers() -> list:
    return [
        CommandHandler("games", cmd_games),
        CommandHandler("mygames", cmd_mygames),
        CommandHandler("past", cmd_past),
        CommandHandler("week", cmd_week),
    ]


# Register for the DM picker so it can re-dispatch these after a chat is chosen.
register_command("games", cmd_games)
register_command("mygames", cmd_mygames)
register_command("past", cmd_past)
register_command("week", cmd_week)
