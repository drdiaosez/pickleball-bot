"""Cached lookup of Telegram group admins.

The manage mini-app is gated to chat admins. We don't want to hit
`bot.get_chat_administrators` on every API call, so we cache the admin
set per chat for a short TTL. A TTL of ~60s is fine — admin changes
are rare and a stale result just means one extra round-trip to recover.

This module is intentionally tiny and bot-instance-aware: callers pass
the `telegram.Bot` they already have so we don't introduce a hidden
global. The cache lives module-level (process-local) which matches how
the rest of the bot uses module-level state (db._conn, etc.).
"""
from __future__ import annotations

import logging
import time
from typing import Set

from telegram import Bot
from telegram.constants import ChatMemberStatus

log = logging.getLogger(__name__)

_CACHE_TTL_S = 60
_CACHE: dict[int, tuple[Set[int], float]] = {}


async def get_chat_admins(bot: Bot, chat_id: int) -> Set[int]:
    """Return the set of user_ids who are admins (or the owner) of `chat_id`.

    Returns an empty set if the call fails — callers should treat an empty
    set as "no admins" rather than "fetch failed", which means the manage
    mini-app will refuse access. That's the safer default than accidentally
    granting admin powers if Telegram is briefly flaky.
    """
    now = time.time()
    cached = _CACHE.get(chat_id)
    if cached and cached[1] > now:
        return cached[0]

    try:
        members = await bot.get_chat_administrators(chat_id)
    except Exception as e:
        log.warning("get_chat_administrators(%s) failed: %s", chat_id, e)
        # Don't poison the cache with a failure — let the next call retry.
        return set()

    admin_ids: Set[int] = set()
    for m in members:
        # Both 'creator' and 'administrator' grant admin powers.
        if m.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
            admin_ids.add(m.user.id)

    _CACHE[chat_id] = (admin_ids, now + _CACHE_TTL_S)
    return admin_ids


async def is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Convenience wrapper — True iff `user_id` is currently a chat admin."""
    admins = await get_chat_admins(bot, chat_id)
    return user_id in admins


def invalidate(chat_id: int | None = None) -> None:
    """Drop cached admin sets. Pass None to clear everything.

    Not currently called from anywhere; exposed so a future admin-events
    handler could blow the cache when Telegram tells us the admin list
    changed.
    """
    if chat_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(chat_id, None)
