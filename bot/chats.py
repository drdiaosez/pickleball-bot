"""Chat registration, membership sync, and Telegram admin caching.

This is the multi-chat orchestration layer that sits between Telegram events
and the chats / chat_members tables.

Two entry points:

  ensure_chat_registered(bot, chat) — called when we want to know about a chat:
    upserts the chats row, optionally enriches with title from Telegram.
    This is idempotent and safe to call many times.

  sync_user_in_chat(bot, chat_id, user_id) — called on every interaction in
    a registered group chat: upserts the chat_members row, and refreshes the
    user's Telegram admin status if the cached value is older than 5 minutes.

The admin cache lives in chat_members.telegram_role_checked_at (added in
migration 001). Cache window is 5 minutes — promoting yourself in Telegram
will be reflected within that window without hammering the API on every
button tap.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram import Bot, Chat
from telegram.error import TelegramError

from . import db

log = logging.getLogger(__name__)

# How long we trust a cached admin/member role before re-checking Telegram.
ADMIN_CACHE_TTL = timedelta(minutes=5)


# ─────────────────────── chat registration ─────────────────────── #

async def ensure_chat_registered(
    bot: Bot, chat: Chat, *, mark_active: bool = True
) -> Optional[dict]:
    """Make sure this chat exists in our `chats` table.

    For group chats (group / supergroup), upserts a row and optionally pulls
    the title from Telegram if we don't have one yet. For private DMs we
    return None — DMs aren't "chats" in the multi-tenant sense; they're a
    user surface that acts on behalf of one or more registered group chats.

    Returns the chats row as a dict, or None for non-group chats.
    """
    if chat.type not in ("group", "supergroup"):
        return None

    existing = db.get_chat(chat.id)

    # If we already know about this chat AND have a title, fast path.
    if existing and existing.get("title"):
        if mark_active and existing.get("status") != "active":
            db.update_chat_status(chat.id, "active")
            existing = db.get_chat(chat.id)
        return existing

    # Fetch the current title from Telegram (chat.title is set on most updates
    # already, but we double-check on first registration).
    title = chat.title
    if not title:
        try:
            fresh = await bot.get_chat(chat.id)
            title = fresh.title
        except TelegramError as e:
            log.warning("Couldn't fetch chat title for %s: %s", chat.id, e)
            title = None

    db.upsert_chat(chat.id, title=title, status="active" if mark_active else "paused")
    return db.get_chat(chat.id)


async def mark_chat_paused(chat_id: int) -> None:
    """Bot was removed / kicked — flip the chat to paused (don't delete data)."""
    db.update_chat_status(chat_id, "paused")


# ─────────────────────── membership sync ─────────────────────── #

async def sync_user_in_chat(bot: Bot, chat_id: int, user_id: int) -> Optional[str]:
    """Make sure (chat_id, user_id) is in chat_members, and refresh the role
    if the cache is stale.

    Returns the current role ('member' or 'admin'), or None if the chat is
    not registered (we don't sync members into chats we don't know about).
    """
    if db.get_chat(chat_id) is None:
        return None

    existing = db.get_chat_member(chat_id, user_id)
    now = datetime.now(timezone.utc)

    if existing is None:
        # First time we've seen this user in this chat — insert with default
        # role and fetch from Telegram immediately so admin actions work right
        # away after onboarding.
        role = await _fetch_telegram_role(bot, chat_id, user_id)
        db.upsert_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            role=role or "member",
            telegram_role_checked_at=now.isoformat(),
        )
        return role or "member"

    # Already exists — only re-fetch if the cached value is stale.
    checked = existing.get("telegram_role_checked_at")
    is_stale = True
    if checked:
        try:
            checked_dt = datetime.fromisoformat(checked)
            if checked_dt.tzinfo is None:
                checked_dt = checked_dt.replace(tzinfo=timezone.utc)
            is_stale = (now - checked_dt) >= ADMIN_CACHE_TTL
        except ValueError:
            pass

    if is_stale:
        role = await _fetch_telegram_role(bot, chat_id, user_id)
        if role is not None:
            db.upsert_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                role=role,
                telegram_role_checked_at=now.isoformat(),
            )
            return role
        # Telegram call failed — stretch the existing cached value rather than
        # losing access to the user. Don't update the timestamp so we'll retry
        # again on the next interaction.

    return existing.get("role", "member")


async def _fetch_telegram_role(bot: Bot, chat_id: int, user_id: int) -> Optional[str]:
    """Ask Telegram what status this user has in this chat.

    Returns 'admin' (for creator/administrator), 'member' (for member /
    restricted), or None if the user isn't in the chat or the call fails.
    Telegram statuses: creator, administrator, member, restricted, left, kicked.
    """
    try:
        cm = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except TelegramError as e:
        log.warning("get_chat_member(%s, %s) failed: %s", chat_id, user_id, e)
        return None

    status = getattr(cm, "status", "") or ""
    if status in ("left", "kicked"):
        return None
    if status in ("creator", "administrator"):
        return "admin"
    return "member"
