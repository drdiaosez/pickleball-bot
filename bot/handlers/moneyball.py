"""/moneyball and /leaderboard handlers.

/moneyball              — list games with 8 confirmed players, pick one
/moneyball <game_id>    — direct launch for a specific game (power-user shortcut)
/leaderboard            — medals over the last 90 days (default)
/leaderboard year       — current calendar year
/leaderboard alltime    — all completed money balls
"""
from __future__ import annotations

import logging
import os
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes
from telegram.helpers import escape

from .. import db, moneyball, views
from .common import gate, touch_member

log = logging.getLogger(__name__)


def _public_url() -> str:
    """Where the Mini App is hosted. Set in .env as PUBLIC_URL,
    e.g. https://pickle.example.com"""
    return os.environ.get("PUBLIC_URL", "").rstrip("/")


def _miniapp_url(mb_id: int) -> str:
    base = _public_url()
    if not base:
        return ""
    return f"{base}/moneyball/{mb_id}"


# ─────────────────────────────────────────────
# /moneyball
# ─────────────────────────────────────────────

async def cmd_moneyball(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update):
        return
    await touch_member(update)
    tz: ZoneInfo = context.bot_data["tz"]

    if not _public_url():
        await update.effective_message.reply_text(
            "Money ball isn't configured yet — the operator needs to set PUBLIC_URL in .env."
        )
        return

    # If user passed a game id, jump straight to launching for that game
    args = context.args or []
    if args and args[0].isdigit():
        game_id = int(args[0])
        await _start_or_resume(update, context, game_id)
        return

    # Otherwise list eligible games
    games = moneyball.list_eligible_games_for_moneyball()
    if not games:
        await update.effective_message.reply_html(
            "<b>Money Ball</b>\n<i>No games with 8 confirmed players right now.</i>\n\n"
            "Money ball needs exactly 8 signed-up players. Get the roster full and try again."
        )
        return

    rows = []
    for g in games:
        label = f"{views.format_when_short(g['scheduled_for'], tz)} @ {g['location']}"
        if len(label) > 50: label = label[:49] + "…"
        rows.append([InlineKeyboardButton(label, callback_data=f"mb_pick:{g['id']}")])
    kb = InlineKeyboardMarkup(rows)
    await update.effective_message.reply_html(
        "<b>Money Ball</b>\nPick a game to play:",
        reply_markup=kb,
    )


async def on_mb_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback when user taps a game in the /moneyball list."""
    if not await gate(update):
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    try:
        game_id = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        return
    await _start_or_resume(update, context, game_id)


async def _start_or_resume(
    update: Update, context: ContextTypes.DEFAULT_TYPE, game_id: int
) -> None:
    """Create a new money ball or resume an existing one for this game."""
    tz: ZoneInfo = context.bot_data["tz"]
    game = db.get_game(game_id)
    if not game:
        await update.effective_message.reply_text("That game no longer exists.")
        return

    # Existing money ball?
    existing = moneyball.get_moneyball_for_game(game_id)
    if existing:
        mb_id = existing["id"]
        await _post_launch_card(update, context, mb_id, resuming=True)
        return

    # Create a new one — only if exactly 8 confirmed participants
    parts = db.get_participants(game_id)
    confirmed = [p for p in parts if p["status"] == "confirmed"]
    if len(confirmed) != 8:
        await update.effective_message.reply_text(
            f"Money ball needs 8 confirmed participants. This game has "
            f"{len(confirmed)} confirmed."
        )
        return

    # Shuffle for random seat assignment
    random.shuffle(confirmed)

    # Build entries for create_moneyball: members and guests pass through differently
    entries = []
    for p in confirmed:
        if p.get("member_id"):
            entries.append({
                "member_id": p["member_id"],
                "added_by": p.get("added_by"),
            })
        else:
            entries.append({
                "guest_name": p["guest_name"],
                "added_by": p["added_by"],
            })

    user_id = update.effective_user.id
    try:
        mb_id = moneyball.create_moneyball(game_id, created_by=user_id, entries=entries)
    except ValueError as e:
        await update.effective_message.reply_text(f"Couldn't start money ball: {e}")
        return

    await _post_launch_card(update, context, mb_id, resuming=False)


async def _post_launch_card(
    update: Update, context: ContextTypes.DEFAULT_TYPE, mb_id: int, resuming: bool
) -> None:
    """Post a card with a 'Open Money Ball' web_app button."""
    tz = context.bot_data["tz"]
    mb = moneyball.get_moneyball(mb_id)
    if not mb:
        return
    when = views.format_when(mb["game"]["scheduled_for"], tz)
    roster = ", ".join(p["name"] for p in mb["players"])

    title = "🏆 <b>Money Ball — resuming</b>" if resuming else "🏆 <b>Money Ball — let's go</b>"
    msg = (
        f"{title}\n"
        f"<i>{escape(when)} @ {escape(mb['game']['location'])}</i>\n\n"
        f"<b>Roster:</b> {escape(roster)}\n\n"
        f"7 rounds · 2 courts · every partner once, every opponent twice. "
        f"Tap below to open the bracket and start scoring."
    )

    url = _miniapp_url(mb_id)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎾 Open Money Ball", web_app=WebAppInfo(url=url))
    ]])
    await update.effective_message.reply_html(msg, reply_markup=kb)


# ─────────────────────────────────────────────
# Completion announcement (called from http_server when last score posts)
# ─────────────────────────────────────────────

def schedule_completion_announcement(application, chat_id_resolver):
    """Returns a callback suitable for http_server.create_app.
    `application` is the telegram Application object so we can post messages;
    `chat_id_resolver(game_id) -> chat_id` figures out which chat to post in.
    """
    def callback(mb_id: int, mb: dict, standings: list[dict]) -> None:
        # Schedule the async work on the bot's event loop
        import asyncio
        asyncio.create_task(_announce_completion(application, chat_id_resolver, mb_id, mb, standings))
    return callback


async def _announce_completion(application, chat_id_resolver, mb_id, mb, standings):
    tz: ZoneInfo = application.bot_data["tz"]
    chat_id = chat_id_resolver(mb["game_id"])
    if chat_id is None:
        log.warning("No chat_id found for game %s; can't announce completion", mb["game_id"])
        return

    medals = [("🥇 Gold", standings[0]), ("🥈 Silver", standings[1]), ("🥉 Bronze", standings[2])]
    lines = [
        "🏆 <b>Money Ball — final</b>",
        f"<i>{views.format_when(mb['game']['scheduled_for'], tz)} @ {escape(mb['game']['location'])}</i>",
        "",
    ]
    for label, s in medals:
        diff = f"{s['diff']:+d}"
        lines.append(f"{label}  <b>{escape(s['name'])}</b>  ({s['wins']}–{s['losses']}, diff {diff})")
    lines.append("")
    lines.append("<b>Full standings</b>")
    for i, s in enumerate(standings, 1):
        diff = f"{s['diff']:+d}"
        lines.append(f"  {i}. {escape(s['name'])}  {s['wins']}–{s['losses']}  diff {diff}")

    try:
        await application.bot.send_message(
            chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.HTML
        )
    except Exception as e:
        log.error("Failed to announce completion: %s", e)


# ─────────────────────────────────────────────
# /leaderboard
# ─────────────────────────────────────────────

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update):
        return
    await touch_member(update)

    args = context.args or []
    scope = "90d"
    label = "Last 90 days"
    if args:
        a = args[0].lower()
        if a in ("year", "ytd"):
            scope, label = "year", f"{datetime.now().year} year-to-date"
        elif a in ("alltime", "all", "lifetime"):
            scope, label = "alltime", "All-time"
        elif a in ("90d", "recent"):
            pass
        else:
            await update.effective_message.reply_text(
                "Usage: /leaderboard | /leaderboard year | /leaderboard alltime"
            )
            return

    rows = moneyball.compute_leaderboard(scope)
    if not rows:
        await update.effective_message.reply_html(
            f"<b>🏆 Leaderboard — {label}</b>\n<i>No completed money balls yet.</i>"
        )
        return

    lines = [f"<b>🏆 Leaderboard — {label}</b>", ""]
    # Only show players who have at least one medal OR have played
    medalists = [r for r in rows if r["points"] > 0]
    others = [r for r in rows if r["points"] == 0]

    if medalists:
        # Header
        lines.append("<code>#   Player              Pts  🥇 🥈 🥉</code>")
        for i, r in enumerate(medalists, 1):
            name = r["name"][:18]
            name_pad = name.ljust(18)
            lines.append(
                f"<code>{i:<3} {escape(name_pad)} {r['points']:>3}  "
                f"{r['gold']:>2} {r['silver']:>2} {r['bronze']:>2}</code>"
            )

    if others:
        lines.append("")
        lines.append("<i>Played, no medals yet:</i>")
        lines.append(", ".join(escape(r["name"]) for r in others))

    await update.effective_message.reply_html("\n".join(lines))


# ─────────────────────────────────────────────
# Handler builders
# ─────────────────────────────────────────────

def build_moneyball_handlers() -> list:
    return [
        CommandHandler("moneyball", cmd_moneyball),
        CommandHandler("leaderboard", cmd_leaderboard),
        CallbackQueryHandler(on_mb_pick, pattern=r"^mb_pick:\d+$"),
    ]
