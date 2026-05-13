"""DM chat picker.

When a user runs a chat-scoped command in a DM with the bot, we need to know
which group chat they mean. This module provides:

  resolve_chat(update, context, command_label) → int | None
    - In a group: returns the group's chat_id, no picker shown.
    - In a DM, if the user has already picked a chat in this conversation:
      returns the cached chat_id.
    - In a DM, if the user is in exactly one active chat: returns that chat_id
      (no picker — only one possible answer).
    - In a DM, if the user is in multiple chats: shows the picker, returns None.
      The caller must just `return` — the picker callback will re-dispatch the
      command once the user taps a chat.

  pick_callback_handler — registered once in main.py. Handles taps on the
    picker, sets context.user_data["selected_chat_id"], and re-dispatches by
    looking up the command handler in COMMAND_REGISTRY.

Cache lifetime: selected_chat_id lives in context.user_data and persists for
the user's session (PTB's user_data is per-user, in-memory by default). It
resets when the bot restarts, which is acceptable — one extra tap.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, ContextTypes
from telegram.helpers import escape

from . import db

log = logging.getLogger(__name__)

# Command name → async handler. Populated by register_command() at import time
# from each handlers module. Lets the picker re-dispatch after a tap.
COMMAND_REGISTRY: dict[str, Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]] = {}


def register_command(name: str, handler) -> None:
    """Used by handler modules to declare their picker-dispatchable command.

    Called at module import time. Idempotent."""
    COMMAND_REGISTRY[name] = handler


SELECTED_KEY = "selected_chat_id"
PICKER_CALLBACK_PREFIX = "pick_chat:"


async def resolve_chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command_label: str,
) -> Optional[int]:
    """Return the chat_id this command should operate on, or None if a picker
    was shown (in which case the caller should just return).

    `command_label` must match a key in COMMAND_REGISTRY; it's used to
    re-dispatch the command after the user taps a chat.
    """
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return None

    # Group chat → use it directly. Also remember it as the user's "last
    # selected" in case they jump to a DM later.
    if chat.type in ("group", "supergroup"):
        context.user_data[SELECTED_KEY] = chat.id
        return chat.id

    # DM. Was a chat already picked this session?
    cached = context.user_data.get(SELECTED_KEY)
    available = db.list_active_chats_for_user(user.id)
    available_ids = {c["telegram_chat_id"] for c in available}

    # Cached value is only valid if the user is still a member of that chat
    if cached is not None and cached in available_ids:
        return cached

    if not available:
        # Should not happen — gate() filters this out. But be safe.
        if update.effective_message:
            await update.effective_message.reply_text(
                "You're not a member of any group I'm registered with."
            )
        return None

    # Exactly one option? Auto-pick it.
    if len(available) == 1:
        only = available[0]["telegram_chat_id"]
        context.user_data[SELECTED_KEY] = only
        return only

    # Multiple options → show picker.
    await _show_picker(update, context, command_label, available)
    return None


async def _show_picker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command_label: str,
    chats: list[dict],
) -> None:
    """Render the inline-keyboard picker."""
    rows = []
    for c in chats:
        title = c.get("title") or f"Chat {c['telegram_chat_id']}"
        rows.append([
            InlineKeyboardButton(
                title,
                callback_data=f"{PICKER_CALLBACK_PREFIX}{command_label}:{c['telegram_chat_id']}",
            )
        ])
    kb = InlineKeyboardMarkup(rows)
    text = (
        f"You're a member of multiple groups. Which one is "
        f"<code>/{escape(command_label)}</code> for?"
    )
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=kb
    )


async def on_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle taps on the picker. Re-dispatch the original command."""
    q = update.callback_query
    if q is None or not q.data:
        return
    await q.answer()

    # Parse callback_data: "pick_chat:<command>:<chat_id>"
    parts = q.data.split(":", 2)
    if len(parts) != 3:
        await q.edit_message_text("Picker is stale. Run the command again.")
        return
    _, command_label, chat_id_str = parts
    try:
        chat_id = int(chat_id_str)
    except ValueError:
        await q.edit_message_text("Picker is stale. Run the command again.")
        return

    # Validate: user still in this chat?
    user = update.effective_user
    if user is None:
        return
    chat = db.get_chat(chat_id)
    member = db.get_chat_member(chat_id, user.id)
    if chat is None or member is None or chat.get("status") != "active":
        await q.edit_message_text(
            "You're no longer a member of that group. Run the command again."
        )
        return

    # Remember this pick for the rest of the session
    context.user_data[SELECTED_KEY] = chat_id

    # Look up and re-dispatch the original handler
    handler = COMMAND_REGISTRY.get(command_label)
    if handler is None:
        await q.edit_message_text(
            f"Unknown command /{escape(command_label)}. Run it again."
        )
        return

    # Acknowledge selection by editing the picker message; the handler will
    # send its own follow-up reply.
    title = chat.get("title") or f"Chat {chat_id}"
    try:
        await q.edit_message_text(
            f"📂 <b>{escape(title)}</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    await handler(update, context)


def build_picker_handlers() -> list:
    return [
        CallbackQueryHandler(on_pick_callback, pattern=rf"^{PICKER_CALLBACK_PREFIX}"),
    ]
