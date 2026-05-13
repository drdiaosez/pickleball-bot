"""Handle Telegram chat lifecycle events for the bot.

Two relevant Telegram update types:

  my_chat_member — the bot's own membership in a chat changed (added, removed,
    promoted, demoted). This is how we know to register a new chat or pause an
    existing one.

  chat_member    — another user's membership in a chat changed. We only care
    about this to keep chat_members in sync when people leave/join the group,
    so admins-only commands don't accidentally include or exclude the wrong
    people. To receive chat_member updates the bot must be an admin AND the
    update type must be enabled in allowed_updates — see main.py.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ChatMemberHandler, ContextTypes

from .. import chats as chats_mod, db

log = logging.getLogger(__name__)


# Telegram member status values: "creator", "administrator", "member",
# "restricted", "left", "kicked".  We treat creator/administrator as "admin"
# everywhere else; here we also use the raw status to tell whether the bot
# is in the chat at all.
_PRESENT = ("creator", "administrator", "member", "restricted")
_ABSENT = ("left", "kicked")


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bot's own membership in a chat changed."""
    cm = update.my_chat_member
    if cm is None:
        return
    chat = cm.chat
    if chat.type not in ("group", "supergroup"):
        return  # ignore private/channel transitions

    new_status = cm.new_chat_member.status
    old_status = cm.old_chat_member.status if cm.old_chat_member else None

    if new_status in _PRESENT and old_status not in _PRESENT:
        # Bot was just added (or unbanned) — register the chat as active.
        log.info("Bot added to chat: id=%s title=%r", chat.id, chat.title)
        await chats_mod.ensure_chat_registered(context.bot, chat, mark_active=True)
        # Try to greet so the group knows the bot is alive
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    "👋 Hi! I'm the Pickleball Bot. I'll help organize games for this group.\n\n"
                    "Try /newgame to schedule one, or /help to see everything I can do."
                ),
            )
        except Exception:
            log.exception("Couldn't send welcome message to chat %s", chat.id)

    elif new_status in _ABSENT and old_status in _PRESENT:
        # Bot was removed/kicked — pause the chat but keep the data.
        log.info(
            "Bot removed from chat: id=%s title=%r status=%s",
            chat.id, chat.title, new_status,
        )
        await chats_mod.mark_chat_paused(chat.id)


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Another user's membership changed.

    Only meaningful if the bot is an admin in the chat (Telegram only sends
    these updates to admin bots). If someone leaves, drop them from
    chat_members so admin pickers stay accurate. Joining is best handled
    lazily via the gate() sync — no point fetching admin status before the
    user does anything.
    """
    cm = update.chat_member
    if cm is None:
        return
    chat = cm.chat
    if chat.type not in ("group", "supergroup"):
        return
    if not db.is_chat_active(chat.id):
        return

    new_status = cm.new_chat_member.status
    user = cm.new_chat_member.user
    if user is None or user.is_bot:
        return

    if new_status in _ABSENT:
        log.info("User left chat %s: user=%s status=%s", chat.id, user.id, new_status)
        db.remove_chat_member(chat.id, user.id)
    # Joining/promotion changes will be picked up the next time the user
    # interacts (via gate() → sync_user_in_chat). No need to act here.


def build_chat_event_handlers() -> list:
    return [
        ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER),
        ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER),
    ]
