"""/merge — admin-only command to promote a guest's history to a member.

Usage:
  /merge "Guest Pat" @patjohnson
  /merge Pat @patjohnson
  /merge Pat 123456789       (telegram user ID also accepted)

Flow:
  1. Operator (a Telegram admin of the group) runs the command
  2. Bot replies with a preview
  3. Operator taps Confirm → merge runs

Admin status is determined per-chat: a user is an admin for the merge if
Telegram lists them as creator/administrator in the chat the merge targets.
This is fetched and cached via chat_members.role (see bot/chats.py).
"""
from __future__ import annotations

import logging
import re
import shlex

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes
from telegram.helpers import escape

from .. import chats as chats_mod, db
from ..chat_picker import resolve_chat, register_command
from .common import gate, touch_member

log = logging.getLogger(__name__)


async def _ensure_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    """Return True iff the current user is a Telegram admin of `chat_id`.

    Forces a fresh sync first (rather than trusting cached role) so a brand-new
    chat owner doesn't get told "you're not admin" before the 5-min cache
    populates. Replies with a denial message if not admin.
    """
    user = update.effective_user
    if not user:
        return False
    try:
        role = await chats_mod.sync_user_in_chat(update.get_bot(), chat_id, user.id)
    except Exception:
        log.exception("admin sync failed for chat=%s user=%s", chat_id, user.id)
        role = "member"

    if role == "admin":
        return True

    if update.effective_message:
        await update.effective_message.reply_text(
            "Only group admins can run /merge."
        )
    return False


# ─────────────────────────── /merge command ───────────────────────────

async def cmd_merge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update):
        return
    await touch_member(update)
    chat_id = await resolve_chat(update, context, "merge")
    if chat_id is None:
        return
    if not await _ensure_admin(update, context, chat_id):
        return

    # Parse args. We use shlex so users can quote multi-word guest names:
    #   /merge "Guest Pat" @pat
    raw = update.effective_message.text or ""
    raw = re.sub(r"^/merge(@\S+)?\s*", "", raw, count=1).strip()

    if not raw:
        await update.effective_message.reply_html(
            "<b>Usage:</b> <code>/merge &quot;Guest Pat&quot; @patjohnson</code>\n\n"
            "Merges a guest's past money ball and signup history into the named member."
        )
        return

    try:
        tokens = shlex.split(raw)
    except ValueError as e:
        await update.effective_message.reply_text(f"Could not parse arguments: {e}")
        return
    if len(tokens) < 2:
        await update.effective_message.reply_text(
            "Need two arguments: the guest name and the member."
        )
        return

    member_token = tokens[-1]
    guest_name = " ".join(tokens[:-1])

    member = db.find_member_by_username_or_id(member_token)
    if member is None:
        await update.effective_message.reply_html(
            f"Couldn't find member <code>{escape(member_token)}</code>. "
            f"They need to have interacted with the bot at least once "
            f"(or be a member of this group)."
        )
        return

    preview = db.find_guest_appearances(guest_name)
    total_entries = preview["moneyball_entries"] + preview["participant_entries"]
    if total_entries == 0:
        await update.effective_message.reply_html(
            f"No history found for guest <b>{escape(guest_name)}</b>. "
            f"Names are matched case-insensitively. Make sure the spelling matches."
        )
        return

    canonicals = preview["canonical_names"]
    canonical_label = (
        f"&ldquo;{escape(canonicals[0])}&rdquo;"
        if len(canonicals) == 1
        else "(multiple spellings: " + ", ".join(f"&ldquo;{escape(n)}&rdquo;" for n in canonicals) + ")"
    )

    msg = (
        "<b>Merge preview</b>\n\n"
        f"Guest: {canonical_label}\n"
        f"Member: <b>{escape(member['display_name'])}</b>"
        f"{' (@' + escape(member['username']) + ')' if member.get('username') else ''}\n\n"
        f"This would convert:\n"
        f"  • <b>{preview['moneyball_entries']}</b> money-ball appearance(s)\n"
        f"  • <b>{preview['participant_entries']}</b> game signup(s)\n\n"
        f"Touching {len(preview['moneyball_ids'])} money ball(s) and "
        f"{len(preview['game_ids'])} game(s).\n\n"
        f"<i>This rewrites historical records. It can't be undone automatically. "
        f"If the member is already in some of these money balls/games as themself, "
        f"those entries will be skipped (you can't be in the same roster twice).</i>"
    )

    # Stash the merge intent so the callback can act on it.
    # Include chat_id so the confirm step can re-verify admin status.
    context.user_data["pending_merge"] = {
        "guest_name": guest_name,
        "member_id": member["telegram_id"],
        "member_name": member["display_name"],
        "chat_id": chat_id,
    }
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✓ Confirm merge", callback_data="merge_yes"),
        InlineKeyboardButton("Cancel", callback_data="merge_no"),
    ]])
    await update.effective_message.reply_html(msg, reply_markup=kb)


async def on_merge_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update):
        return
    q = update.callback_query
    await q.answer()

    pending = context.user_data.get("pending_merge")
    if not pending:
        await q.edit_message_text(
            "Nothing to merge — start over with <code>/merge</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Re-verify admin against the chat that initiated the merge — not the chat
    # the callback is firing in (callback chat is the same as initiate chat by
    # construction, but defense in depth).
    if not await _ensure_admin(update, context, pending["chat_id"]):
        return

    if q.data == "merge_no":
        context.user_data.pop("pending_merge", None)
        await q.edit_message_text("Merge cancelled.")
        return

    if q.data != "merge_yes":
        return

    try:
        report = db.merge_guest_into_member(pending["guest_name"], pending["member_id"])
    except ValueError as e:
        await q.edit_message_text(f"Merge failed: {e}")
        return
    finally:
        context.user_data.pop("pending_merge", None)

    lines = [
        f"<b>✓ Merge complete</b>",
        f"Promoted &ldquo;{escape(pending['guest_name'])}&rdquo; → "
        f"<b>{escape(pending['member_name'])}</b>",
        "",
        f"  • {report['merged_moneyball_entries']} money-ball appearance(s) merged",
        f"  • {report['merged_participant_entries']} game signup(s) merged",
    ]
    skipped_mb = report["skipped_moneyball_ids"]
    skipped_games = report["skipped_game_ids"]
    if skipped_mb or skipped_games:
        lines.append("")
        lines.append("<i>Skipped (member was already on the roster):</i>")
        if skipped_mb:
            lines.append(f"  Money balls: {', '.join(str(i) for i in skipped_mb)}")
        if skipped_games:
            lines.append(f"  Games: {', '.join(str(i) for i in skipped_games)}")

    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML)


def build_merge_handlers() -> list:
    return [
        CommandHandler("merge", cmd_merge),
        CallbackQueryHandler(on_merge_callback, pattern=r"^merge_(yes|no)$"),
    ]


register_command("merge", cmd_merge)
