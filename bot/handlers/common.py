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
    "/moneyball — start an 8-player money ball tournament\n"
    "/leaderboard — medals from recent money balls\n"
    "   <i>/leaderboard year, /leaderboard alltime</i>\n"
    "/merge — (admin) merge a guest's history into a member\n"
    "/help — this message\n\n"
    "Everything else (joining, leaving, adding guests, swaps) happens "
    "through the buttons on each game card.\n\n"
    "<i>Works in the group or in DM. If you DM and belong to multiple groups, "
    "the bot will ask which one you mean.</i>"
)


# ─────────────────────── authorization ─────────────────────── #

def _allowed_group_id() -> Optional[int]:
    """Legacy: the group chat ID the bot was originally locked to via .env.

    Only used as a fallback during the transition: if ALLOWED_GROUP_ID is set
    AND no chats are registered yet (fresh install), we permit that one group
    so the migration path stays smooth. Once migration 001 has run and
    populated the chats table, this fallback is effectively unused.
    """
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

    Multi-chat rules:
      - Group/supergroup chat → allowed iff the chat is registered AND active.
        Unregistered groups are silently ignored (logged but no response).
      - DM → allowed iff the user is a member of at least one active chat.
        (PR 4 will add the "which chat?" picker on top of this.)
      - Anything else → denied.

    Legacy fallback: if the chats table is empty and ALLOWED_GROUP_ID is set,
    behave like the old single-group bot. This only kicks in for setups that
    haven't had any chat registered yet.
    """
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return False

    # Group / supergroup: must be registered and active.
    if chat.type in ("group", "supergroup"):
        if db.is_chat_active(chat.id):
            return True
        # Legacy fallback for the original single-group setup
        legacy = _allowed_group_id()
        if legacy is not None and chat.id == legacy:
            return True
        log.info(
            "AUTH: ignoring unregistered group chat_id=%s title=%r user_id=%s",
            chat.id, getattr(chat, "title", None), user.id,
        )
        return False

    # DM: allowed if the user belongs to any active chat.
    if chat.type == "private":
        if db.list_active_chats_for_user(user.id):
            return True
        # Legacy fallback: known member of the single legacy chat
        legacy = _allowed_group_id()
        if legacy is not None and db.get_member(user.id) is not None:
            return True

    log.info(
        "AUTH DENIED chat_id=%s chat_type=%s user_id=%s username=%r",
        chat.id, chat.type, user.id, user.username,
    )
    return False


async def gate(update: Update) -> bool:
    """Use at the top of every handler: `if not await gate(update): return`.

    Also syncs chat membership as a side effect when authorized in a group —
    keeps chat_members fresh without every handler having to remember.
    """
    if not is_authorized(update):
        if update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "Sorry, this bot only works in groups it's been added to. "
                    "Ask whoever runs the bot to add it to your group."
                )
            except Exception:
                pass
        return False

    # Live membership sync for group chats. Best-effort: if Telegram is slow
    # or the call fails, we don't block the user's command.
    chat = update.effective_chat
    user = update.effective_user
    if chat and user and chat.type in ("group", "supergroup"):
        try:
            # IMPORTANT: upsert the user into `members` FIRST. The chat_members
            # table has a FK to members(telegram_id), so syncing chat_members
            # for a never-seen-before user (e.g. someone who just tapped a
            # button on an old game card without ever typing) raises
            # IntegrityError if `members` is empty for that id. Individual
            # handlers call touch_member() AFTER gate() returns True, which is
            # too late for the sync_user_in_chat() call below.
            await touch_member(update)

            # Late import to avoid circular dependency with bot.chats
            from .. import chats as chats_mod
            await chats_mod.ensure_chat_registered(update.get_bot(), chat)
            await chats_mod.sync_user_in_chat(update.get_bot(), chat.id, user.id)
        except Exception:
            log.exception("Membership sync failed for chat=%s user=%s", chat.id, user.id)

    return True


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
