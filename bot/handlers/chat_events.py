"""Handle Telegram chat lifecycle events for the bot.

Three relevant Telegram update types:

  my_chat_member — the bot's own membership in a chat changed (added, removed,
    promoted, demoted). This is how we know to register a new chat or pause an
    existing one.

  chat_member    — another user's membership in a chat changed. We only care
    about this to keep chat_members in sync when people leave/join the group,
    so admins-only commands don't accidentally include or exclude the wrong
    people. To receive chat_member updates the bot must be an admin AND the
    update type must be enabled in allowed_updates — see main.py.

  message with migrate_from_chat_id — the chat was converted from a regular
    group to a supergroup. Telegram assigns the supergroup a new chat_id, so
    we re-key all data from the old id to the new one. See on_chat_migrate.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ChatMemberHandler, ContextTypes, MessageHandler, filters

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


async def on_chat_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram converted a regular group to a supergroup.

    Triggered by a message with migrate_from_chat_id set (delivered into the
    NEW supergroup, where update.effective_chat.id is the new id). The
    counterpart message in the old group has migrate_to_chat_id set, but we
    don't need to handle that one — re-keying everything in a single pass
    from the new side is sufficient and avoids dueling handlers.

    All state (chats, chat_members, games, moneyballs) gets re-pointed to the
    new chat id atomically. If on_my_chat_member already created a stub row
    for the new id, the migration helper merges it.
    """
    msg = update.message or update.edited_message
    if msg is None:
        return
    old_chat_id = getattr(msg, "migrate_from_chat_id", None)
    if not old_chat_id:
        # This handler is also reached for "this chat was just migrated TO a
        # supergroup, here's the new id" — that side carries migrate_to_chat_id.
        # We rely on the migrate_from side to do the work, so ignore the other.
        return

    new_chat_id = update.effective_chat.id if update.effective_chat else None
    if not new_chat_id:
        log.warning("Migration event with no effective_chat — skipping")
        return

    log.info(
        "Chat migration detected: old=%s new=%s — re-keying data",
        old_chat_id, new_chat_id,
    )

    try:
        summary = db.migrate_chat_id(old_chat_id, new_chat_id)
    except Exception:
        log.exception(
            "Chat migration FAILED: old=%s new=%s. Data is in inconsistent state; "
            "manual intervention required.", old_chat_id, new_chat_id,
        )
        return

    log.info("Chat migration complete: %s", summary)

    # Best-effort confirmation in the chat. Skipped silently if it fails —
    # the migration itself already succeeded.
    try:
        await context.bot.send_message(
            chat_id=new_chat_id,
            text=(
                "♻️ Telegram converted this group to a supergroup, which "
                "gives it a new chat ID. I've moved all the bot's data over "
                "automatically — games, signups, leaderboard, everything. "
                "Carry on!"
            ),
        )
    except Exception as e:
        log.info("Couldn't send migration confirmation message: %s", e)


def build_chat_event_handlers() -> list:
    return [
        ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER),
        ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER),
        # Catch the service message that fires when a group becomes a
        # supergroup. Must be registered before any catch-all message handler.
        MessageHandler(filters.StatusUpdate.MIGRATE, on_chat_migrate),
    ]
