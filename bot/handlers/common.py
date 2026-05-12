"""Common handlers: /start, /help, error handler, member-touch and auth helpers."""
from __future__ import annotations

import logging
import os
import traceback
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from .. import db

log = logging.getLogger(__name__)


HELP_TEXT = (
    "<b>🎾 Pickleball Bot</b>\n\n"
    "<b>Commands</b>\n"
    "/newgame — schedule a new game\n"
    "/games — list upcoming games\n"
    "/mygames — games you're signed up for\n"
    "/past — recent past games\n"
    "/week — games this week (Mon-Sun)\n"
    "   <i>/week next, /week last, /week 5/18</i>\n"
    "/help — this message\n\n"
    "Everything else (joining, leaving, adding guests, swaps) happens "
    "through the buttons on each game card."
)


# ─────────────────────── authorization ─────────────────────── #

def _allowed_group_id() -> Optional[int]:
    """The group chat ID the bot is locked to, or None if unconfigured."""
    raw = os.environ.get("ALLOWED_GROUP_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.warning("ALLOWED_GROUP_ID is set but not an integer: %r", raw)
        return None


def is_authorized(update: Update) -> bool:
    """Return True if this update is allowed to use the bot.

    Rules:
      - If ALLOWED_GROUP_ID is unset, log the chat ID and allow (setup mode).
      - Messages in the allowed group → allowed.
      - DMs from a user whose telegram_id is in the members table → allowed
        (i.e., they've previously interacted via the group).
      - Anything else (DMs from strangers, other groups) → denied.
    """
    allowed = _allowed_group_id()
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return False

    if allowed is None:
        # Setup mode: log chat IDs so the operator can find theirs
        log.info(
            "AUTH (setup mode — set ALLOWED_GROUP_ID in .env to lock down): "
            "chat_id=%s chat_type=%s chat_title=%r user_id=%s",
            chat.id, chat.type, getattr(chat, "title", None), user.id,
        )
        return True

    # Messages in the allowed group — open to anyone in the group
    if chat.id == allowed:
        return True

    # DMs to the bot — only allow if the user is a known member.
    # (chat.type == "private" and chat.id == user.id in DMs)
    if chat.type == "private":
        if db.get_member(user.id) is not None:
            return True

    log.info(
        "AUTH DENIED chat_id=%s chat_type=%s user_id=%s username=%r",
        chat.id, chat.type, user.id, user.username,
    )
    return False


async def gate(update: Update) -> bool:
    """Use at the top of every handler: `if not await gate(update): return`.

    Sends a polite denial message to unauthorized users so they understand
    why the bot isn't responding.
    """
    if is_authorized(update):
        return True
    if update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Sorry, this bot is private to a specific group. "
                "If you're in that group, send any command there first."
            )
        except Exception:
            pass
    return False


# ─────────────────────── commands ─────────────────────── #

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update):
        return
    await touch_member(update)
    if update.effective_message:
        await update.effective_message.reply_html(HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update):
        return
    await touch_member(update)
    if update.effective_message:
        await update.effective_message.reply_html(HELP_TEXT)


async def touch_member(update: Update) -> None:
    """Upsert the user into the members table on every interaction."""
    user = update.effective_user
    if not user:
        return
    # Prefer "First Last", fall back to first name, then username
    name_parts = [user.first_name or "", user.last_name or ""]
    display_name = " ".join(p for p in name_parts if p).strip() or user.username or f"User{user.id}"
    db.upsert_member(user.id, display_name, user.username)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unhandled errors. In production we'd ship these somewhere."""
    log.error("Exception while handling update:", exc_info=context.error)
    tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    log.error(tb)

    # Try to tell the user something useful without leaking internals
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Something went wrong. Try again, and tell whoever runs this bot if it keeps happening."
            )
        except Exception:
            pass
